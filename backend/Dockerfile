# DocuSense AI — API + Celery worker image (used by docker-compose).
#
# Slim by default (base requirements only → Gemini embedding backend). Pass
# --build-arg INSTALL_LOCAL_EMBED=true to also install sentence-transformers +
# torch for the local embedding backend (docker-compose does this).
FROM python:3.11-slim

# System deps: tesseract for OCR (FR-01 scanned-page fallback). PyMuPDF, psycopg,
# reportlab (and torch, when installed) ship manylinux wheels — no compiler needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libglib2.0-0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface

WORKDIR /app

ARG INSTALL_LOCAL_EMBED=false

COPY requirements.txt requirements-local.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt && \
    if [ "$INSTALL_LOCAL_EMBED" = "true" ]; then pip install -r requirements-local.txt; fi

COPY app ./app

EXPOSE 8000

# Overridden by the worker service in docker-compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
