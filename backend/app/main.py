"""
DocuSense AI — FastAPI application entrypoint.

Wires together auth, documents, chat, summary, and annotation routers; optionally
serves the frontend same-origin (SERVE_FRONTEND); configures CORS for the split
dev/self-host setup; and on startup ensures the database schema (pgvector + tables
+ indexes) and the object-storage bucket exist. Exposes /health for host health checks.

Run: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from . import storage
from .routers import auth, documents, chat, summary, annotations, admin

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("docusense")


def _resolve_frontend_dir():
    """Find the frontend directory (containing index.html) across dev / Docker layouts."""
    here = os.path.dirname(os.path.abspath(__file__))          # .../backend/app
    candidates = [
        settings.FRONTEND_DIR,
        os.path.join(os.getcwd(), settings.FRONTEND_DIR),
        "/app/frontend",
        os.path.join(here, "..", "frontend"),                  # backend/frontend
        os.path.join(here, "..", "..", "frontend"),            # repo-root/frontend
    ]
    for c in candidates:
        try:
            if c and os.path.isfile(os.path.join(c, "index.html")):
                return os.path.abspath(c)
        except Exception:
            continue
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting %s (env=%s, llm_provider=%s, embed_backend=%s, ingest_mode=%s)",
             settings.APP_NAME, settings.ENV, settings.LLM_PROVIDER,
             settings.EMBED_BACKEND, settings.INGEST_MODE)

    # Production safety: never boot with the default signing key.
    if settings.is_production and settings.secret_is_default:
        raise RuntimeError(
            "Refusing to start in a production environment with the default SECRET_KEY. "
            "Generate one: python -c \"import secrets;print(secrets.token_urlsafe(48))\""
        )
    if settings.is_production and settings.DEBUG:
        log.warning("DEBUG=true in a production environment — set DEBUG=false.")

    try:
        init_db()
        log.info("Database initialized")
    except Exception:
        log.exception("Database initialization failed")
    try:
        storage.ensure_bucket()
        log.info("Object storage ready (bucket=%s)", settings.S3_BUCKET)
    except Exception:
        log.exception("Could not verify object-storage bucket")

    yield  # ── application runs ──


app = FastAPI(
    title=settings.APP_NAME,
    version="2.0.0",
    description="SRS-compliant document review system: hybrid RAG, grounded chat, "
                "summaries, and annotations.",
    lifespan=lifespan,
)

# CORS is only needed for the split frontend/backend setup (dev on :5500, or a
# separately-hosted UI). When SERVE_FRONTEND serves the UI same-origin it's a no-op.
if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Baseline hardening headers on every response.

    CSP is intentionally omitted — the frontend loads PDF.js/jsPDF from a CDN and
    a strict policy would need per-host allowances (documented in DEPLOY.md). HSTS
    is only sent in production, where the host terminates TLS.
    """
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if settings.is_production:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": app.version}


app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(summary.router)
app.include_router(annotations.router)
app.include_router(admin.router)

# Mount the static frontend LAST, so /api/* and /health take precedence over it.
if settings.SERVE_FRONTEND:
    _fe = _resolve_frontend_dir()
    if _fe:

        @app.get("/", include_in_schema=False)
        async def landing():
            """Serve landing page at root."""
            lp = os.path.join(_fe, "landing.html")
            if os.path.isfile(lp):
                return FileResponse(lp)
            return FileResponse(os.path.join(_fe, "index.html"))

        @app.get("/app", include_in_schema=False)
        @app.get("/app/{rest:path}", include_in_schema=False)
        async def app_page(rest: str = ""):
            """Serve the main app at /app."""
            return FileResponse(os.path.join(_fe, "index.html"))

        # Serve static assets (JS, CSS, images) from the frontend dir
        app.mount("/", StaticFiles(directory=_fe, html=False), name="frontend")
        log.info("Serving landing at / and app at /app from %s", _fe)
    else:
        log.warning("SERVE_FRONTEND=true but no index.html found (FRONTEND_DIR=%s)",
                    settings.FRONTEND_DIR)