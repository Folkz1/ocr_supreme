import os
import io
import fitz  # PyMuPDF
from typing import Optional, List, Dict, Tuple
import magic
import pandas as pd
from PIL import Image
import pytesseract
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import docx2txt
import tempfile
import zipfile
import rarfile
import shutil

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Depends, Request
from pydantic import BaseModel

# --- Configuração do rarfile para usar unar ---
rarfile.UNRAR_TOOL = "unar"

# --- Configuráveis por ENV (para a triagem de PDF) ---
PAGE_TEXT_THRESHOLD = int(os.getenv("PAGE_TEXT_THRESHOLD", "50"))
OCR_PAGE_CHAR_THRESHOLD = int(os.getenv("OCR_PAGE_CHAR_THRESHOLD", "15"))
RENDER_SCALE = float(os.getenv("RENDER_SCALE", "2.0"))
OCR_LANG = os.getenv("OCR_LANG", "por+eng")
OCR_MAX_PAGES_TO_CHECK = int(os.getenv("OCR_MAX_PAGES_TO_CHECK", "10"))
MAX_IMAGE_FRAMES = int(os.getenv("MAX_IMAGE_FRAMES", "5"))
MAX_RECURSION_DEPTH = int(os.getenv("MAX_RECURSION_DEPTH", "10"))

# --- Autenticação por API Key ---
API_KEY = os.getenv("API_KEY")
API_KEY_HEADER_NAME = os.getenv("API_KEY_HEADER_NAME", "X-API-Key")

def verify_api_key(request: Request):
    """Valida a API Key enviada no header configurável (padrão: X-API-Key)."""
    expected = API_KEY
    header_name = API_KEY_HEADER_NAME
    provided = request.headers.get(header_name)

    if not expected:
        raise HTTPException(status_code=401, detail="API Key não configurada no servidor")
    if not provided or provided != expected:
        raise HTTPException(status_code=401, detail="API Key inválida")
    return True


# --- Modelos de Resposta ---
class DataResponse(BaseModel):
    content_type: Optional[str] = None
    content: Optional[str] = None

class ProcessResponse(BaseModel):
    filename: str
    status: str
    message: str
    data: DataResponse

class ArchiveFileInfo(BaseModel):
    filename: str
    path_in_archive: str
    size: int
    extracted_text: Optional[str] = None
    status: str
    error: Optional[str] = None

class ArchiveProcessResponse(BaseModel):
    filename: str
    archive_type: str
    status: str
    message: str
    total_files: int
    processed_files: int
    files: List[ArchiveFileInfo]

# --- App FastAPI ---
APP_VERSION = "3.2.0"
app = FastAPI(
    title="Hub de Processamento de Documentos",
    description="Processa XML, PDF, Imagens, Planilhas, DOCX, HTML, TXT e arquivos compactados (.zip, .rar) para extração de texto.",
    version=APP_VERSION
)

@app.on_event("startup")
async def startup_event():
    print(f"=== OCR Supreme v{APP_VERSION} iniciado ===")
    print(f"=== Detecção de ZIP por assinatura ATIVA ===")

# --- Funções de Processamento para cada formato ---

def process_spreadsheet(contents: bytes) -> str:
    """Converte o conteúdo de um arquivo XLS ou XLSX para uma string CSV."""
    df = pd.read_excel(io.BytesIO(contents), engine=None)
    return df.to_csv(index=False)

def process_xml(contents: bytes) -> str:
    """Extrai todo o conteúdo de texto de um arquivo XML."""
    root = ET.fromstring(contents)
    text_content = ""
    for elem in root.iter():
        if elem.text:
            text_content += elem.text.strip() + "\n"
    return text_content

def process_html(contents: bytes) -> str:
    """Extrai o texto puro de um conteúdo HTML."""
    soup = BeautifulSoup(contents, 'lxml')
    return soup.get_text(separator="\n", strip=True)

def process_docx(contents: bytes) -> str:
    """Extrai o texto de um arquivo .docx usando um arquivo temporário."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
            tmp.write(contents)
            tmp.flush()
            tmp_path = tmp.name
        return docx2txt.process(tmp_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar DOCX: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

def process_image_ocr(contents: bytes) -> str:
    """Executa OCR em uma imagem (inclui suporte a TIFF multi-página)."""
    try:
        img = Image.open(io.BytesIO(contents))
        texts = []
        n_frames = getattr(img, "n_frames", 1)
        if n_frames and n_frames > 1:
            frames_to_read = min(n_frames, MAX_IMAGE_FRAMES)
            for i in range(frames_to_read):
                try:
                    img.seek(i)
                    texts.append(pytesseract.image_to_string(img, lang=OCR_LANG))
                except Exception:
                    continue
            return "\n".join(filter(None, (t or "" for t in texts)))
        else:
            return pytesseract.image_to_string(img, lang=OCR_LANG)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro no OCR da imagem: {e}")

def process_text(contents: bytes) -> str:
    """Lê o conteúdo de um arquivo de texto simples (TXT, CSV)."""
    return contents.decode('utf-8', errors='ignore')

# --- Lógica de Triagem Específica para PDF ---

def process_pdf_force_ocr(contents: bytes) -> Tuple[str, str, Dict]:
    """
    Processa PDF forçando OCR em todas as páginas, ignorando detecção de imagens.
    Tenta extrair texto de todas as páginas usando OCR quando necessário.
    
    Returns:
        Tupla (status, text_content, debug_info)
    """
    accumulated_text = []
    debug = {"pages_scanned": 0, "pages_ocr_applied": 0, "total_pages": 0}
    
    try:
        pdf = fitz.open(stream=contents, filetype="pdf")
        total_pages = pdf.page_count
        debug["total_pages"] = total_pages
        
        for i in range(total_pages):
            debug["pages_scanned"] += 1
            page = pdf.load_page(i)
            
            # Primeiro tenta extrair texto nativo
            page_text = (page.get_text("text") or "").strip()
            
            # Se não há texto suficiente, aplica OCR
            if len(page_text) < PAGE_TEXT_THRESHOLD:
                debug["pages_ocr_applied"] += 1
                img = render_page_to_pil(page, scale=RENDER_SCALE)
                text_from_image = quick_ocr_on_image(img, lang=OCR_LANG)
                
                if text_from_image:
                    accumulated_text.append(text_from_image)
                elif page_text:
                    # Se OCR não retornou nada mas havia algum texto, usa o texto
                    accumulated_text.append(page_text)
            else:
                # Usa o texto nativo
                accumulated_text.append(page_text)
        
        pdf.close()
        
        final_text = "\n\n".join(accumulated_text)
        
        if final_text.strip():
            return "processed", final_text, debug
        else:
            return "ocr_failed", "", debug
            
    except Exception as e:
        return "error", "", {"error": str(e)}

def render_page_to_pil(page: fitz.Page, scale: float) -> Image.Image:
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

def quick_ocr_on_image(img: Image.Image, lang: str) -> str:
    config = "--psm 6"
    try:
        return pytesseract.image_to_string(img, lang=lang, config=config) or ""
    except Exception:
        return ""

def triage_pdf_fail_fast(contents: bytes):
    """Faz triagem fail-fast do PDF para classificar o tipo de processamento necessário."""
    accumulated_text = []
    debug = {"pages_scanned": 0, "pages_ocr_checked": 0, "images_detected": 0, "total_pages": 0}

    try:
        pdf = fitz.open(stream=contents, filetype="pdf")
        total_pages = pdf.page_count
        debug["total_pages"] = total_pages

        has_images = False
        pages_with_images = []

        for i in range(total_pages):
            debug["pages_scanned"] += 1
            page = pdf.load_page(i)

            image_list = page.get_images()
            if image_list:
                has_images = True
                pages_with_images.append(i)
                debug["images_detected"] += 1

            page_text = (page.get_text("text") or "").strip()

            if len(page_text) < PAGE_TEXT_THRESHOLD:
                if debug["pages_ocr_checked"] < OCR_MAX_PAGES_TO_CHECK:
                    debug["pages_ocr_checked"] += 1
                    img = render_page_to_pil(page, scale=RENDER_SCALE)
                    text_from_image = quick_ocr_on_image(img, lang=OCR_LANG)

                    if len(text_from_image.strip()) > OCR_PAGE_CHAR_THRESHOLD:
                        debug["reason"] = f"page_{i}_ocr_chars_found"
                        debug["pages_with_images"] = pages_with_images
                        pdf.close()

                        if total_pages == 1:
                            return "single_page_pdf", "", debug
                        elif has_images:
                            return "multipage_pdf_with_images", "", debug
                        else:
                            return "multipage_pdf_text_only", "", debug

                    if text_from_image:
                        accumulated_text.append(text_from_image)
                else:
                    debug["reason"] = f"page_{i}_low_text_ocr_limit_exceeded"
                    debug["pages_with_images"] = pages_with_images
                    pdf.close()

                    if total_pages == 1:
                        return "single_page_pdf", "", debug
                    elif has_images:
                        return "multipage_pdf_with_images", "", debug
                    else:
                        return "multipage_pdf_text_only", "", debug
            else:
                accumulated_text.append(page_text)

        debug["pages_with_images"] = pages_with_images
        pdf.close()

        if has_images:
            if total_pages == 1:
                return "single_page_pdf", "\n\n".join(accumulated_text), debug
            else:
                return "multipage_pdf_with_images", "\n\n".join(accumulated_text), debug
        else:
            if total_pages == 1:
                return "single_page_pdf", "\n\n".join(accumulated_text), debug
            else:
                return "multipage_pdf_text_only", "\n\n".join(accumulated_text), debug

    except Exception as e:
        return "error", "", {"error": str(e)}

# --- Funções para processamento de arquivos compactados ---

def is_archive_file(filename: str, contents: bytes = None) -> Tuple[bool, str]:
    """Verifica se o arquivo é um arquivo compactado suportado."""
    lower_name = filename.lower()

    # Verifica pela extensão primeiro - extensão tem prioridade
    if lower_name.endswith('.zip'):
        # Se temos conteúdo, tenta validar, mas retorna True mesmo se falhar
        if contents:
            try:
                # Verifica assinatura ZIP ou usa zipfile
                if contents.startswith(b'PK') or zipfile.is_zipfile(io.BytesIO(contents)):
                    return True, 'zip'
            except Exception:
                pass
            # Mesmo se a validação falhar, confia na extensão
            return True, 'zip'
        return True, 'zip'

    if lower_name.endswith('.rar'):
        return True, 'rar'

    # Se não detectou pela extensão, tenta detectar pela assinatura do conteúdo
    if contents:
        try:
            # Verifica assinatura ZIP (PK\x03\x04 ou PK\x05\x06)
            if contents.startswith(b'PK\x03\x04') or contents.startswith(b'PK\x05\x06'):
                return True, 'zip'
            # Verifica assinatura RAR (Rar!\x1a\x07)
            if contents.startswith(b'Rar!\x1a\x07'):
                return True, 'rar'
        except Exception:
            pass

    return False, ''

def extract_archive_recursive(contents: bytes, archive_type: str, current_path: str = "", depth: int = 0) -> List[Dict]:
    """
    Extrai recursivamente arquivos de um arquivo compactado (.zip ou .rar).

    Args:
        contents: Conteúdo do arquivo compactado em bytes
        archive_type: Tipo do arquivo ('zip' ou 'rar')
        current_path: Caminho atual dentro do arquivo (para recursão)
        depth: Profundidade atual da recursão

    Returns:
        Lista de dicionários contendo informações sobre os arquivos extraídos
    """
    if depth > MAX_RECURSION_DEPTH:
        return [{
            "filename": current_path,
            "status": "error",
            "error": f"Profundidade máxima de recursão atingida ({MAX_RECURSION_DEPTH})"
        }]

    extracted_files = []

    try:
        if archive_type == 'zip':
            with zipfile.ZipFile(io.BytesIO(contents)) as archive:
                for file_info in archive.filelist:
                    if file_info.is_dir():
                        continue

                    file_path = os.path.join(current_path, file_info.filename) if current_path else file_info.filename

                    try:
                        file_contents = archive.read(file_info.filename)

                        # Verifica se é outro arquivo compactado
                        is_archive, nested_archive_type = is_archive_file(file_info.filename, file_contents)

                        if is_archive:
                            # Extrai recursivamente
                            nested_files = extract_archive_recursive(
                                file_contents,
                                nested_archive_type,
                                file_path,
                                depth + 1
                            )
                            extracted_files.extend(nested_files)
                        else:
                            # Processa o arquivo
                            extracted_files.append({
                                "filename": os.path.basename(file_info.filename),
                                "path_in_archive": file_path,
                                "size": file_info.file_size,
                                "contents": file_contents,
                                "status": "extracted"
                            })

                    except Exception as e:
                        extracted_files.append({
                            "filename": os.path.basename(file_info.filename),
                            "path_in_archive": file_path,
                            "size": file_info.file_size,
                            "status": "error",
                            "error": str(e)
                        })

        elif archive_type == 'rar':
            with tempfile.NamedTemporaryFile(delete=False, suffix=".rar") as tmp:
                tmp.write(contents)
                tmp.flush()
                tmp_path = tmp.name

            try:
                with rarfile.RarFile(tmp_path) as archive:
                    for file_info in archive.infolist():
                        if file_info.isdir():
                            continue

                        file_path = os.path.join(current_path, file_info.filename) if current_path else file_info.filename

                        try:
                            file_contents = archive.read(file_info.filename)

                            # Verifica se é outro arquivo compactado
                            is_archive, nested_archive_type = is_archive_file(file_info.filename, file_contents)

                            if is_archive:
                                # Extrai recursivamente
                                nested_files = extract_archive_recursive(
                                    file_contents,
                                    nested_archive_type,
                                    file_path,
                                    depth + 1
                                )
                                extracted_files.extend(nested_files)
                            else:
                                # Processa o arquivo
                                extracted_files.append({
                                    "filename": os.path.basename(file_info.filename),
                                    "path_in_archive": file_path,
                                    "size": file_info.file_size,
                                    "contents": file_contents,
                                    "status": "extracted"
                                })

                        except Exception as e:
                            extracted_files.append({
                                "filename": os.path.basename(file_info.filename),
                                "path_in_archive": file_path,
                                "size": file_info.file_size,
                                "status": "error",
                                "error": str(e)
                            })
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

    except Exception as e:
        extracted_files.append({
            "filename": current_path or "root",
            "status": "error",
            "error": f"Erro ao extrair arquivo: {str(e)}"
        })

    return extracted_files

def process_file_content(filename: str, contents: bytes) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Processa o conteúdo de um arquivo e extrai texto.

    Returns:
        Tupla (status, extracted_text, error)
    """
    try:
        mime_type = magic.from_buffer(contents, mime=True)
        original_mime = mime_type

        # Se o mime_type é octet-stream, tenta detectar pela extensão do arquivo
        if mime_type == "application/octet-stream":
            lower_filename = filename.lower()
            if lower_filename.endswith('.pdf'):
                mime_type = "application/pdf"
            elif lower_filename.endswith(('.xls', '.xlsx')):
                mime_type = "application/vnd.ms-excel"
            elif lower_filename.endswith('.docx'):
                mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif lower_filename.endswith(('.xml',)):
                mime_type = "application/xml"
            elif lower_filename.endswith(('.html', '.htm')):
                mime_type = "text/html"
            elif lower_filename.endswith(('.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.gif')):
                mime_type = "image/jpeg"  # Genérico para imagens
            elif lower_filename.endswith(('.txt', '.csv')):
                mime_type = "text/plain"

            if mime_type != original_mime:
                print(f"Ajustado mime_type de {original_mime} para {mime_type} baseado na extensão do arquivo: {filename}")

        # Verifica assinaturas
        is_pdf_signature = contents.startswith(b"%PDF")
        is_zip = False
        zip_names = []

        try:
            is_zip = zipfile.is_zipfile(io.BytesIO(contents))
            if is_zip:
                with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                    zip_names = zf.namelist()
        except Exception:
            pass

        # PDF
        if ("pdf" in mime_type) or is_pdf_signature:
            pdf_classification, accumulated_text, debug = triage_pdf_fail_fast(contents)

            if accumulated_text:
                return "processed", accumulated_text, None
            else:
                return "requires_ocr", None, "PDF requer OCR"

        # Planilhas
        elif "sheet" in mime_type or "excel" in mime_type or (is_zip and "xl/workbook.xml" in zip_names):
            csv_content = process_spreadsheet(contents)
            return "processed", csv_content, None

        # DOCX
        elif ("vnd.openxmlformats-officedocument.wordprocessingml.document" in mime_type) or (is_zip and ("word/document.xml" in zip_names)):
            text_content = process_docx(contents)
            return "processed", text_content, None

        # XML
        elif mime_type in ("application/xml", "text/xml"):
            text_content = process_xml(contents)
            return "processed", text_content, None

        # HTML
        elif "html" in mime_type:
            text_content = process_html(contents)
            return "processed", text_content, None

        # Imagens
        elif "image" in mime_type:
            text_content = process_image_ocr(contents)
            return "processed", text_content, None

        # Texto
        elif "text" in mime_type:
            text_content = process_text(contents)
            return "processed", text_content, None

        else:
            return "unsupported", None, f"Tipo de arquivo não suportado: {mime_type}"

    except Exception as e:
        return "error", None, str(e)

def process_file_content_force_ocr(filename: str, contents: bytes) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Processa o conteúdo de um arquivo e extrai texto, SEMPRE tentando OCR quando aplicável.
    Ignora detecção de imagens e força OCR em PDFs e imagens.
    
    Returns:
        Tupla (status, extracted_text, error)
    """
    try:
        mime_type = magic.from_buffer(contents, mime=True)
        original_mime = mime_type

        # Se o mime_type é octet-stream, tenta detectar pela extensão do arquivo
        if mime_type == "application/octet-stream":
            lower_filename = filename.lower()
            if lower_filename.endswith('.pdf'):
                mime_type = "application/pdf"
            elif lower_filename.endswith(('.xls', '.xlsx')):
                mime_type = "application/vnd.ms-excel"
            elif lower_filename.endswith('.docx'):
                mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif lower_filename.endswith(('.xml',)):
                mime_type = "application/xml"
            elif lower_filename.endswith(('.html', '.htm')):
                mime_type = "text/html"
            elif lower_filename.endswith(('.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.gif')):
                mime_type = "image/jpeg"  # Genérico para imagens
            elif lower_filename.endswith(('.txt', '.csv')):
                mime_type = "text/plain"

            if mime_type != original_mime:
                print(f"[force_ocr] Ajustado mime_type de {original_mime} para {mime_type} baseado na extensão do arquivo: {filename}")

        # Verifica assinaturas
        is_pdf_signature = contents.startswith(b"%PDF")
        is_zip = False
        zip_names = []

        try:
            is_zip = zipfile.is_zipfile(io.BytesIO(contents))
            if is_zip:
                with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                    zip_names = zf.namelist()
        except Exception:
            pass

        # PDF - FORÇA OCR
        if ("pdf" in mime_type) or is_pdf_signature:
            status, text_content, debug = process_pdf_force_ocr(contents)
            
            if status == "processed":
                return "processed", text_content, None
            elif status == "ocr_failed":
                return "requires_external_ocr", None, "OCR local falhou"
            else:
                return "error", None, debug.get("error", "Erro ao processar PDF")

        # Planilhas
        elif "sheet" in mime_type or "excel" in mime_type or (is_zip and "xl/workbook.xml" in zip_names):
            csv_content = process_spreadsheet(contents)
            return "processed", csv_content, None

        # DOCX
        elif ("vnd.openxmlformats-officedocument.wordprocessingml.document" in mime_type) or (is_zip and ("word/document.xml" in zip_names)):
            text_content = process_docx(contents)
            return "processed", text_content, None

        # XML
        elif mime_type in ("application/xml", "text/xml"):
            text_content = process_xml(contents)
            return "processed", text_content, None

        # HTML
        elif "html" in mime_type:
            text_content = process_html(contents)
            return "processed", text_content, None

        # Imagens - FORÇA OCR
        elif "image" in mime_type:
            try:
                text_content = process_image_ocr(contents)
                if text_content and text_content.strip():
                    return "processed", text_content, None
                else:
                    return "requires_external_ocr", None, "OCR não extraiu texto"
            except Exception as e:
                return "requires_external_ocr", None, f"Erro no OCR: {str(e)}"

        # Texto
        elif "text" in mime_type:
            text_content = process_text(contents)
            return "processed", text_content, None

        else:
            return "unsupported", None, f"Tipo de arquivo não suportado: {mime_type}"

    except Exception as e:
        return "error", None, str(e)

# --- Endpoints ---

@app.post("/process-file/", response_model=ProcessResponse, dependencies=[Depends(verify_api_key)])
async def process_file(file: UploadFile = File(...)) -> ProcessResponse:
    """
    Endpoint principal para processar arquivos.
    Suporta: PDF, Imagens, Planilhas, DOCX, XML, HTML, TXT, ZIP e RAR (com extração recursiva).
    """
    contents = await file.read()

    # Debug: mostra informações do arquivo recebido
    print(f"DEBUG: Arquivo recebido: {file.filename}, tamanho: {len(contents)} bytes")
    print(f"DEBUG: Primeiros bytes: {contents[:20] if contents else 'vazio'}")

    # Verifica se é um arquivo compactado - prioriza detecção por assinatura
    is_archive = False
    archive_type = ''

    # Detecta ZIP pela assinatura (PK) - mais confiável que extensão
    if contents and contents.startswith(b'PK'):
        try:
            if zipfile.is_zipfile(io.BytesIO(contents)):
                is_archive = True
                archive_type = 'zip'
                print(f"DEBUG: Detectado como ZIP pela assinatura PK")
        except Exception as e:
            print(f"DEBUG: Erro ao verificar ZIP: {e}")

    # Detecta RAR pela assinatura
    if not is_archive and contents and contents.startswith(b'Rar!\x1a\x07'):
        is_archive = True
        archive_type = 'rar'
        print(f"DEBUG: Detectado como RAR pela assinatura")

    # Se não detectou pela assinatura, tenta pela extensão
    if not is_archive:
        is_archive, archive_type = is_archive_file(file.filename, contents)
        if is_archive:
            print(f"DEBUG: Detectado como {archive_type} pela extensão")

    if is_archive:
        # Processa como arquivo compactado diretamente
        try:
            # Extrai arquivos recursivamente
            extracted_files = extract_archive_recursive(contents, archive_type)

            # Processa cada arquivo extraído
            processed_files = []
            for file_info in extracted_files:
                if file_info.get("status") == "error":
                    processed_files.append(ArchiveFileInfo(
                        filename=file_info.get("filename", "unknown"),
                        path_in_archive=file_info.get("path_in_archive", ""),
                        size=file_info.get("size", 0),
                        status="error",
                        error=file_info.get("error")
                    ))
                    continue

                # Processa o conteúdo do arquivo
                status, extracted_text, error = process_file_content(
                    file_info["filename"],
                    file_info["contents"]
                )

                processed_files.append(ArchiveFileInfo(
                    filename=file_info["filename"],
                    path_in_archive=file_info["path_in_archive"],
                    size=file_info["size"],
                    extracted_text=extracted_text,
                    status=status,
                    error=error
                ))

            total_files = len(processed_files)
            successfully_processed = sum(1 for f in processed_files if f.status == "processed")

            # Retorna como ProcessResponse com o texto extraído de cada arquivo
            all_texts = []
            for f in processed_files:
                if f.extracted_text:
                    all_texts.append(f"=== {f.path_in_archive} ===\n{f.extracted_text}")
                elif f.error:
                    all_texts.append(f"=== {f.path_in_archive} ===\n[ERRO: {f.error}]")
                else:
                    all_texts.append(f"=== {f.path_in_archive} ===\n[Sem texto extraído]")

            combined_text = "\n\n".join(all_texts)

            return ProcessResponse(
                filename=file.filename,
                status="processed",
                message=f"Arquivo compactado (.{archive_type}) processado com sucesso. {successfully_processed}/{total_files} arquivos extraídos.",
                data=DataResponse(
                    content_type="text/plain",
                    content=combined_text
                )
            )

        except Exception as e:
            print(f"Erro ao processar arquivo compactado {file.filename}: {e}")
            raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo compactado: {str(e)}")

    # Processa arquivo normal
    mime_type = magic.from_buffer(contents, mime=True)
    is_pdf_signature = contents.startswith(b"%PDF")
    is_zip = False
    zip_names = []

    try:
        is_zip = zipfile.is_zipfile(io.BytesIO(contents))
        if is_zip:
            with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                zip_names = zf.namelist()
    except Exception:
        pass

    print(f"Arquivo recebido: {file.filename}, Tipo MIME: {mime_type}, is_pdf_sig={is_pdf_signature}, is_zip={is_zip}")

    try:
        if ("pdf" in mime_type) or is_pdf_signature:
            pdf_classification, accumulated_text, debug = triage_pdf_fail_fast(contents)
            print(f"Triage PDF debug info: {debug}")

            if pdf_classification == "single_page_pdf":
                if accumulated_text:
                    return ProcessResponse(filename=file.filename, status="processed", message="PDF de página única processado com sucesso.", data=DataResponse(content_type="text/plain", content=accumulated_text))
                else:
                    return ProcessResponse(filename=file.filename, status="single_page_pdf", message="PDF de página única que precisa de OCR.", data=DataResponse())

            elif pdf_classification == "multipage_pdf_with_images":
                if accumulated_text:
                    return ProcessResponse(filename=file.filename, status="multipage_pdf_with_images", message="PDF multipáginas com imagens - use Textract para múltiplas páginas.", data=DataResponse(content_type="text/plain", content=accumulated_text))
                else:
                    return ProcessResponse(filename=file.filename, status="multipage_pdf_with_images", message="PDF multipáginas com imagens que precisa de OCR - use Textract para múltiplas páginas.", data=DataResponse())

            elif pdf_classification == "multipage_pdf_text_only":
                if accumulated_text:
                    return ProcessResponse(filename=file.filename, status="processed", message="PDF multipáginas apenas texto processado com sucesso.", data=DataResponse(content_type="text/plain", content=accumulated_text))
                else:
                    return ProcessResponse(filename=file.filename, status="multipage_pdf_text_only", message="PDF multipáginas apenas texto que precisa de OCR.", data=DataResponse())

            elif pdf_classification == "error":
                return ProcessResponse(filename=file.filename, status="error", message="Erro ao processar PDF.", data=DataResponse())

            else:
                return ProcessResponse(filename=file.filename, status="requires_ocr", message="O PDF precisa de processamento OCR robusto.", data=DataResponse())

        elif "sheet" in mime_type or "excel" in mime_type or (is_zip and "xl/workbook.xml" in zip_names):
            csv_content = process_spreadsheet(contents)
            return ProcessResponse(filename=file.filename, status="processed", message="Planilha convertida para CSV com sucesso.", data=DataResponse(content_type="text/csv", content=csv_content))

        elif ("vnd.openxmlformats-officedocument.wordprocessingml.document" in mime_type) or (is_zip and ("word/document.xml" in zip_names)):
            text_content = process_docx(contents)
            return ProcessResponse(filename=file.filename, status="processed", message="DOCX processado com sucesso.", data=DataResponse(content_type="text/plain", content=text_content))

        elif mime_type in ("application/xml", "text/xml"):
            text_content = process_xml(contents)
            return ProcessResponse(filename=file.filename, status="processed", message="XML processado com sucesso.", data=DataResponse(content_type="text/plain", content=text_content))

        elif "html" in mime_type:
            text_content = process_html(contents)
            return ProcessResponse(filename=file.filename, status="processed", message="HTML processado com sucesso.", data=DataResponse(content_type="text/plain", content=text_content))

        elif "image" in mime_type:
            return ProcessResponse(filename=file.filename, status="requires_ocr", message="A imagem precisa de processamento OCR robusto.", data=DataResponse())

        elif "text" in mime_type:
            text_content = process_text(contents)
            return ProcessResponse(filename=file.filename, status="processed", message="Arquivo de texto processado com sucesso.", data=DataResponse(content_type="text/plain", content=text_content))

        else:
            raise HTTPException(status_code=400, detail=f"Tipo de arquivo não suportado: {mime_type}.")

    except Exception as e:
        print(f"Erro ao processar o arquivo {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Ocorreu um erro interno ao processar o arquivo. Detalhe: {str(e)}")

@app.post("/onlyocr/", response_model=ProcessResponse, dependencies=[Depends(verify_api_key)])
async def only_ocr(file: UploadFile = File(...)) -> ProcessResponse:
    """
    Endpoint que sempre tenta fazer OCR de todos os documentos, mesmo com imagens.
    Só escala para processamento externo se o OCR falhar completamente.
    Funciona exatamente como /process-file mas ignora a detecção de imagens.
    """
    contents = await file.read()

    # Debug: mostra informações do arquivo recebido
    print(f"DEBUG [onlyocr]: Arquivo recebido: {file.filename}, tamanho: {len(contents)} bytes")
    print(f"DEBUG [onlyocr]: Primeiros bytes: {contents[:20] if contents else 'vazio'}")

    # Verifica se é um arquivo compactado - prioriza detecção por assinatura
    is_archive = False
    archive_type = ''

    # Detecta ZIP pela assinatura (PK) - mais confiável que extensão
    if contents and contents.startswith(b'PK'):
        try:
            if zipfile.is_zipfile(io.BytesIO(contents)):
                is_archive = True
                archive_type = 'zip'
                print(f"DEBUG [onlyocr]: Detectado como ZIP pela assinatura PK")
        except Exception as e:
            print(f"DEBUG [onlyocr]: Erro ao verificar ZIP: {e}")

    # Detecta RAR pela assinatura
    if not is_archive and contents and contents.startswith(b'Rar!\x1a\x07'):
        is_archive = True
        archive_type = 'rar'
        print(f"DEBUG [onlyocr]: Detectado como RAR pela assinatura")

    # Se não detectou pela assinatura, tenta pela extensão
    if not is_archive:
        is_archive, archive_type = is_archive_file(file.filename, contents)
        if is_archive:
            print(f"DEBUG [onlyocr]: Detectado como {archive_type} pela extensão")

    if is_archive:
        # Processa como arquivo compactado diretamente
        try:
            # Extrai arquivos recursivamente
            extracted_files = extract_archive_recursive(contents, archive_type)

            # Processa cada arquivo extraído com OCR forçado
            processed_files = []
            for file_info in extracted_files:
                if file_info.get("status") == "error":
                    processed_files.append(ArchiveFileInfo(
                        filename=file_info.get("filename", "unknown"),
                        path_in_archive=file_info.get("path_in_archive", ""),
                        size=file_info.get("size", 0),
                        status="error",
                        error=file_info.get("error")
                    ))
                    continue

                # Processa o conteúdo do arquivo com OCR forçado
                status, extracted_text, error = process_file_content_force_ocr(
                    file_info["filename"],
                    file_info["contents"]
                )

                processed_files.append(ArchiveFileInfo(
                    filename=file_info["filename"],
                    path_in_archive=file_info["path_in_archive"],
                    size=file_info["size"],
                    extracted_text=extracted_text,
                    status=status,
                    error=error
                ))

            total_files = len(processed_files)
            successfully_processed = sum(1 for f in processed_files if f.status == "processed")

            # Retorna como ProcessResponse com o texto extraído de cada arquivo
            all_texts = []
            for f in processed_files:
                if f.extracted_text:
                    all_texts.append(f"=== {f.path_in_archive} ===\n{f.extracted_text}")
                elif f.error:
                    all_texts.append(f"=== {f.path_in_archive} ===\n[ERRO: {f.error}]")
                else:
                    all_texts.append(f"=== {f.path_in_archive} ===\n[Sem texto extraído]")

            combined_text = "\n\n".join(all_texts)

            return ProcessResponse(
                filename=file.filename,
                status="processed",
                message=f"Arquivo compactado (.{archive_type}) processado com sucesso. {successfully_processed}/{total_files} arquivos extraídos.",
                data=DataResponse(
                    content_type="text/plain",
                    content=combined_text
                )
            )

        except Exception as e:
            print(f"Erro ao processar arquivo compactado {file.filename}: {e}")
            raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo compactado: {str(e)}")

    # Processa arquivo normal com OCR forçado
    mime_type = magic.from_buffer(contents, mime=True)
    is_pdf_signature = contents.startswith(b"%PDF")
    is_zip = False
    zip_names = []

    try:
        is_zip = zipfile.is_zipfile(io.BytesIO(contents))
        if is_zip:
            with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                zip_names = zf.namelist()
    except Exception:
        pass

    print(f"[onlyocr] Arquivo recebido: {file.filename}, Tipo MIME: {mime_type}, is_pdf_sig={is_pdf_signature}, is_zip={is_zip}")

    try:
        # PDF - SEMPRE tenta OCR, ignora detecção de imagens
        if ("pdf" in mime_type) or is_pdf_signature:
            status, text_content, debug = process_pdf_force_ocr(contents)
            print(f"[onlyocr] PDF Force OCR debug info: {debug}")

            if status == "processed":
                return ProcessResponse(
                    filename=file.filename,
                    status="processed",
                    message="PDF processado com sucesso usando OCR.",
                    data=DataResponse(content_type="text/plain", content=text_content)
                )
            elif status == "ocr_failed":
                # Só escala se o OCR falhou completamente
                return ProcessResponse(
                    filename=file.filename,
                    status="requires_external_ocr",
                    message="OCR local falhou. Necessário processamento externo (Textract).",
                    data=DataResponse()
                )
            else:  # error
                return ProcessResponse(
                    filename=file.filename,
                    status="error",
                    message=f"Erro ao processar PDF: {debug.get('error', 'Erro desconhecido')}",
                    data=DataResponse()
                )

        # Planilhas
        elif "sheet" in mime_type or "excel" in mime_type or (is_zip and "xl/workbook.xml" in zip_names):
            csv_content = process_spreadsheet(contents)
            return ProcessResponse(
                filename=file.filename,
                status="processed",
                message="Planilha convertida para CSV com sucesso.",
                data=DataResponse(content_type="text/csv", content=csv_content)
            )

        # DOCX
        elif ("vnd.openxmlformats-officedocument.wordprocessingml.document" in mime_type) or (is_zip and ("word/document.xml" in zip_names)):
            text_content = process_docx(contents)
            return ProcessResponse(
                filename=file.filename,
                status="processed",
                message="DOCX processado com sucesso.",
                data=DataResponse(content_type="text/plain", content=text_content)
            )

        # XML
        elif mime_type in ("application/xml", "text/xml"):
            text_content = process_xml(contents)
            return ProcessResponse(
                filename=file.filename,
                status="processed",
                message="XML processado com sucesso.",
                data=DataResponse(content_type="text/plain", content=text_content)
            )

        # HTML
        elif "html" in mime_type:
            text_content = process_html(contents)
            return ProcessResponse(
                filename=file.filename,
                status="processed",
                message="HTML processado com sucesso.",
                data=DataResponse(content_type="text/plain", content=text_content)
            )

        # Imagens - SEMPRE tenta OCR
        elif "image" in mime_type:
            try:
                text_content = process_image_ocr(contents)
                if text_content and text_content.strip():
                    return ProcessResponse(
                        filename=file.filename,
                        status="processed",
                        message="Imagem processada com OCR com sucesso.",
                        data=DataResponse(content_type="text/plain", content=text_content)
                    )
                else:
                    return ProcessResponse(
                        filename=file.filename,
                        status="requires_external_ocr",
                        message="OCR local não extraiu texto. Necessário processamento externo.",
                        data=DataResponse()
                    )
            except Exception as e:
                return ProcessResponse(
                    filename=file.filename,
                    status="requires_external_ocr",
                    message=f"Erro no OCR local: {str(e)}. Necessário processamento externo.",
                    data=DataResponse()
                )

        # Texto
        elif "text" in mime_type:
            text_content = process_text(contents)
            return ProcessResponse(
                filename=file.filename,
                status="processed",
                message="Arquivo de texto processado com sucesso.",
                data=DataResponse(content_type="text/plain", content=text_content)
            )

        else:
            raise HTTPException(status_code=400, detail=f"Tipo de arquivo não suportado: {mime_type}.")

    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao processar o arquivo {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Ocorreu um erro interno ao processar o arquivo. Detalhe: {str(e)}")

@app.post("/process-archive/", response_model=ArchiveProcessResponse, dependencies=[Depends(verify_api_key)])
async def process_archive(file: UploadFile = File(...)) -> ArchiveProcessResponse:
    """
    Processa arquivos compactados (.zip, .rar) de forma recursiva.
    Extrai todos os arquivos e processa cada um para extrair texto.
    """
    contents = await file.read()

    # Verifica o tipo de arquivo
    is_archive, archive_type = is_archive_file(file.filename, contents)

    if not is_archive:
        raise HTTPException(status_code=400, detail="Arquivo não é um arquivo compactado suportado (.zip ou .rar)")

    try:
        # Extrai arquivos recursivamente
        extracted_files = extract_archive_recursive(contents, archive_type)

        # Processa cada arquivo extraído
        processed_files = []
        for file_info in extracted_files:
            if file_info.get("status") == "error":
                processed_files.append(ArchiveFileInfo(
                    filename=file_info.get("filename", "unknown"),
                    path_in_archive=file_info.get("path_in_archive", ""),
                    size=file_info.get("size", 0),
                    status="error",
                    error=file_info.get("error")
                ))
                continue

            # Processa o conteúdo do arquivo
            status, extracted_text, error = process_file_content(
                file_info["filename"],
                file_info["contents"]
            )

            processed_files.append(ArchiveFileInfo(
                filename=file_info["filename"],
                path_in_archive=file_info["path_in_archive"],
                size=file_info["size"],
                extracted_text=extracted_text,
                status=status,
                error=error
            ))

        total_files = len(processed_files)
        successfully_processed = sum(1 for f in processed_files if f.status == "processed")

        return ArchiveProcessResponse(
            filename=file.filename,
            archive_type=archive_type,
            status="processed",
            message=f"Arquivo compactado processado com sucesso. {successfully_processed}/{total_files} arquivos processados.",
            total_files=total_files,
            processed_files=successfully_processed,
            files=processed_files
        )

    except Exception as e:
        print(f"Erro ao processar arquivo compactado {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo compactado: {str(e)}")

@app.get("/health")
async def health_check():
    """Endpoint de health check para verificar se o serviço está funcionando."""
    return {"status": "healthy", "version": APP_VERSION}

@app.get("/")
async def root():
    """Endpoint raiz com informações sobre a API."""
    return {
        "name": "Hub de Processamento de Documentos",
        "version": APP_VERSION,
        "description": "API para processamento de documentos e arquivos compactados",
        "endpoints": {
            "/process-file/": "Processa um arquivo individual",
            "/onlyocr/": "Processa um arquivo sempre tentando OCR (ignora detecção de imagens)",
            "/process-archive/": "Processa arquivos compactados (.zip, .rar) recursivamente",
            "/health": "Verifica o status do serviço"
        }
    }
