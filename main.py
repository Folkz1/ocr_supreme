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
app = FastAPI(
    title="Hub de Processamento de Documentos",
    description="Processa XML, PDF, Imagens, Planilhas, DOCX, HTML, TXT e arquivos compactados (.zip, .rar) para extração de texto.",
    version="3.0.0"
)

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

def is_archive_file(filename: str) -> Tuple[bool, str]:
    """Verifica se o arquivo é um arquivo compactado suportado."""
    lower_name = filename.lower()
    if lower_name.endswith('.zip'):
        return True, 'zip'
    elif lower_name.endswith('.rar'):
        return True, 'rar'
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
                        is_archive, nested_archive_type = is_archive_file(file_info.filename)

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
                            is_archive, nested_archive_type = is_archive_file(file_info.filename)

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

# --- Endpoints ---

@app.post("/process-file/", response_model=ProcessResponse, dependencies=[Depends(verify_api_key)])
async def process_file(file: UploadFile = File(...)) -> ProcessResponse:
    """
    Endpoint principal para processar arquivos.
    Suporta: PDF, Imagens, Planilhas, DOCX, XML, HTML, TXT, ZIP e RAR (com extração recursiva).
    """
    contents = await file.read()

    # Verifica se é um arquivo compactado
    is_archive, archive_type = is_archive_file(file.filename)

    if is_archive:
        # Redireciona para o endpoint de arquivos compactados
        return await process_archive(file)

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

@app.post("/process-archive/", response_model=ArchiveProcessResponse, dependencies=[Depends(verify_api_key)])
async def process_archive(file: UploadFile = File(...)) -> ArchiveProcessResponse:
    """
    Processa arquivos compactados (.zip, .rar) de forma recursiva.
    Extrai todos os arquivos e processa cada um para extrair texto.
    """
    contents = await file.read()

    # Verifica o tipo de arquivo
    is_archive, archive_type = is_archive_file(file.filename)

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
    return {"status": "healthy", "version": "3.0.0"}

@app.get("/")
async def root():
    """Endpoint raiz com informações sobre a API."""
    return {
        "name": "Hub de Processamento de Documentos",
        "version": "3.0.0",
        "description": "API para processamento de documentos e arquivos compactados",
        "endpoints": {
            "/process-file/": "Processa um arquivo individual",
            "/process-archive/": "Processa arquivos compactados (.zip, .rar) recursivamente",
            "/health": "Verifica o status do serviço"
        }
    }
