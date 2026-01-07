FROM python:3.10-slim

# FORCE REBUILD: 2026-01-07-v3.1.1
ARG CACHEBUST=1

# Define variáveis de ambiente para a aplicação
ENV APP_DIR=/app \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR ${APP_DIR}

# Instala dependências do sistema necessárias
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    tesseract-ocr \
    tesseract-ocr-por \
    tesseract-ocr-eng \
    unar \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copia arquivos de dependências
COPY requirements.txt .

# Instala as dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# FORCE NO CACHE - Copia código da aplicação
ARG APP_VERSION=3.1.1
ENV APP_VERSION=${APP_VERSION}
ADD main.py /app/main.py

# Expõe a porta do container
EXPOSE ${PORT}

# Health check - usa GET em vez de HEAD (--spider)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 -O /dev/null http://localhost:${PORT}/health || exit 1

# Inicia a aplicação usando o servidor Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
