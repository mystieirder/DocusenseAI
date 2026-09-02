"""
Application configuration — loaded from environment / .env via pydantic-settings.

Every service (API, Celery worker) imports `settings` from here, so all config
lives in one place. Defaults are dev-friendly and match docker-compose; the
free-tier cloud deploy overrides a handful of them via environment variables
(see .env.render.example and DEPLOY.md).
"""
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── App ──────────────────────────────────────────────────────────────
    APP_NAME: str = "DocuSense AI"
    ENV: str = "development"
    DEBUG: bool = True

    # ── Auth / JWT (FR §4 security: JWT/OAuth2 + RBAC) ────────────────────
    SECRET_KEY: str = "change-me-please-use-a-long-random-secret"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # Emails granted the 'admin' role on register/login (RBAC bootstrap for a solo owner).
    ADMIN_EMAILS: List[str] = []

    # ── Database (PostgreSQL + pgvector) ─────────────────────────────────
    DATABASE_URL: str = "postgresql+psycopg://docusense:docusense@postgres:5432/docusense"

    # ── Redis / Celery (async ingestion) ─────────────────────────────────
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"
    # How uploads get ingested:
    #   "auto"   → use Celery if the broker is reachable, else run in a background thread
    #   "celery" → always enqueue to Celery (docker-compose / self-host with a worker)
    #   "thread" → always ingest in-process in a daemon thread (free hosts with no worker)
    INGEST_MODE: str = "auto"

    # ── Object storage (S3 / MinIO / Supabase Storage / Cloudflare R2) ───
    S3_ENDPOINT_URL: str = "http://minio:9000"             # "" ⇒ real AWS S3
    S3_PUBLIC_ENDPOINT_URL: str = "http://localhost:9000"  # browser-reachable host for presigned URLs
    S3_ACCESS_KEY: str = "docusense"
    S3_SECRET_KEY: str = "docusense-secret"
    S3_BUCKET: str = "docusense-documents"
    S3_REGION: str = "us-east-1"
    # Supabase Storage & some S3 gateways require path-style addressing; AWS uses "virtual".
    S3_ADDRESSING_STYLE: str = "auto"                      # "auto" | "path" | "virtual"
    # AES-256 server-side encryption. MinIO(+KMS)/AWS honor it; Supabase/R2 reject the
    # header, so default OFF for managed free stores (upload_bytes also falls back safely).
    S3_USE_SSE: bool = False
    PRESIGN_EXPIRE_SECONDS: int = 3600

    # ── LLM provider ─────────────────────────────────────────────────────
    # "gemini" | "mistral" | "groq"
    LLM_PROVIDER: str = "gemini"
    LLM_MAX_OUTPUT_TOKENS: int = 8192

    # Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"

    # Mistral
    MISTRAL_API_KEY: str = ""
    MISTRAL_MODEL: str = "mistral-small-latest"
    MISTRAL_VISION_MODEL: str = "pixtral-12b-latest"
    MISTRAL_EMBED_MODEL: str = "mistral-embed"
    MISTRAL_EMBED_DIM: int = 1024

    # Groq (OpenAI-compatible, free tier, no vision/embed models)
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # ── Embeddings / retrieval ───────────────────────────────────────────
    # EMBED_BACKEND selects the dense-embedding provider:
    #   "local"   → sentence-transformers (needs torch, ~1.5 GB RAM; best for self-host / big hosts)
    #   "gemini"  → Gemini embedding API (no torch, tiny footprint; best for free 512 MB hosts)
    #   "keyword" → no dense vectors; retrieval degrades to sparse full-text only
    EMBED_BACKEND: str = "local"
    EMBED_MODEL: str = "all-MiniLM-L6-v2"        # used when EMBED_BACKEND=local
    EMBED_DIM: int = 384                          # dim of the local model
    GEMINI_EMBED_MODEL: str = "gemini-embedding-001"   # used when EMBED_BACKEND=gemini
    GEMINI_EMBED_DIM: int = 768                   # Matryoshka output dim (fits pgvector cheaply)
    EMBED_BATCH: int = 100                        # texts per embedding batch
    RETRIEVAL_TOP_K: int = 8
    HYBRID_ALPHA: float = 0.5          # dense weight in hybrid fusion (0=sparse only, 1=dense only)

    # ── Ingestion limits (FR-01.1 / FR-01.2) ─────────────────────────────
    MAX_UPLOAD_MB: int = 50
    MAX_PAGES: int = 300
    OCR_PAGE_LIMIT: int = 60
    LOW_TEXT_DENSITY: float = 0.20     # OCR fallback below 20% printable-char density
    # OCR engine for scanned / low-text pages:
    #   "auto"      → Gemini vision if GEMINI_API_KEY is set, else local Tesseract
    #   "gemini"    → always use the multimodal Gemini model (fast on tiny free hosts;
    #                 no tesseract binary; costs one Gemini call per OCR'd page)
    #   "tesseract" → always use the local Tesseract binary (self-host / big hosts)
    OCR_BACKEND: str = "auto"
    # Pages transcribed per Gemini vision request. Batching many pages into one
    # call is what keeps scanned-PDF OCR under the Gemini free-tier request limit
    # (~20 req/min): a 26-page scan becomes ~9 calls instead of 26. Keep it small
    # enough that the combined response fits LLM_MAX_OUTPUT_TOKENS (~3 dense pages).
    OCR_BATCH_PAGES: int = 3
    CHUNK_CHARS: int = 1000
    CHUNK_OVERLAP: int = 150

    # ── SMTP (email) ─────────────────────────────────────────────────────
    SMTP_HOST:     str = "smtp-relay.brevo.com"
    SMTP_PORT:     int = 587
    SMTP_USER:     str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM:     str = ""

    # ── Frontend / CORS ──────────────────────────────────────────────────
    # When SERVE_FRONTEND=true the API also serves frontend/index.html at "/"
    # (same-origin → no CORS needed). docker-compose leaves this false and uses nginx.
    SERVE_FRONTEND: bool = False
    FRONTEND_DIR: str = "frontend"     # path to the built frontend (relative to repo root or absolute)
    CORS_ORIGINS: List[str] = ["http://localhost:5500", "http://127.0.0.1:5500"]

    # ── Derived helpers ──────────────────────────────────────────────────
    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @property
    def allowed_extensions(self) -> set:
        return {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg"}

    @property
    def effective_embed_dim(self) -> int:
        """Dimension of the pgvector column — depends on the active embedding backend."""
        if self.EMBED_BACKEND == "gemini":
            return self.GEMINI_EMBED_DIM
        if self.EMBED_BACKEND == "mistral":
            return self.MISTRAL_EMBED_DIM
        return self.EMBED_DIM  # local (384) or keyword (unused)

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() in ("production", "prod", "staging")

    @property
    def secret_is_default(self) -> bool:
        return (not self.SECRET_KEY) or self.SECRET_KEY == "change-me-please-use-a-long-random-secret"

    @property
    def admin_email_set(self) -> set:
        """Normalized set of emails that should hold the 'admin' role."""
        return {e.strip().lower() for e in self.ADMIN_EMAILS if e and e.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()