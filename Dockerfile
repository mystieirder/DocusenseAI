# ─────────────────────────────────────────────────────────────────────
#  DocuSense AI — single-service cloud image (Render / Hugging Face Spaces)
# ─────────────────────────────────────────────────────────────────────
#  ONE container serves the API *and* the frontend (SERVE_FRONTEND=true),
#  ingests uploads in a background thread (INGEST_MODE=thread — no Celery/
#  Redis), and embeds via the Gemini API (EMBED_BACKEND=gemini — no torch),
#  so it fits a free 512 MB instance. Build context is the repo root.
#
#  Local build/run:
#     docker build -t docusense .
#     docker run -p 7860:7860 --env-file .env.render docusense
FROM python:3.11-slim

# tesseract → OCR fallback for scanned PDFs/images (FR-01).
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libglib2.0-0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/tmp/hf

WORKDIR /app

# Base (slim) requirements only — no sentence-transformers/torch.
COPY backend/requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code + the static frontend (served same-origin at "/").
COPY backend/app ./app
COPY frontend ./frontend

# Free-tier defaults baked in; every one is overridable via the host's env vars.
ENV ENV=production \
    DEBUG=false \
    SERVE_FRONTEND=true \
    INGEST_MODE=thread \
    EMBED_BACKEND=gemini \
    GEMINI_EMBED_DIM=768 \
    S3_ADDRESSING_STYLE=path \
    S3_USE_SSE=false \
    CORS_ORIGINS=[]

# HF Spaces expects 7860; Render injects $PORT. Bind whichever is present.
EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
