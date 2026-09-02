"""
Ingestion pipeline — broker-agnostic core plus dispatch.

`run_ingest(doc_id)` is the actual work: download from object storage →
extract + chunk + embed (rag.process_document) → bulk-insert chunks → set
page_count + status. It's idempotent (clears prior chunks first) and captures
all failures on the Document row (status="failed", error=…) for the UI.

How it gets triggered depends on settings.INGEST_MODE:
  • "celery" → enqueue to the Celery worker (docker-compose / self-host).
  • "thread" → run in a background daemon thread (free hosts with no worker).
  • "auto"   → Celery if the broker is reachable, else a thread.

This lets the SAME codebase run the full async-worker architecture in Docker and
a zero-extra-service deployment on a free tier.
"""
import logging
import threading

from .config import settings
from .database import SessionLocal
from .models import Document, DocumentChunk
from . import storage, rag

log = logging.getLogger("docusense.tasks")


# ── Core pipeline (called inline, from a thread, or from Celery) ─────────
def run_ingest(doc_id: str) -> dict:
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if not doc:
            log.warning("run_ingest: document %s not found", doc_id)
            return {"status": "missing", "doc_id": doc_id}

        try:
            content = storage.download_bytes(doc.storage_key)
            page_count, chunks = rag.process_document(content, doc.name)

            if page_count > settings.MAX_PAGES:
                doc.status = "failed"
                doc.error = f"Document exceeds the {settings.MAX_PAGES}-page limit ({page_count} pages)."
                db.commit()
                return {"status": "failed", "reason": "too_many_pages", "pages": page_count}

            # Idempotent re-ingest: drop any existing chunks first.
            db.query(DocumentChunk).filter(DocumentChunk.doc_id == doc_id).delete()
            db.flush()

            for c in chunks:
                db.add(DocumentChunk(
                    doc_id=doc_id,
                    page_num=c["page_num"],
                    chunk_index=c["chunk_index"],
                    bbox_json=c.get("bbox"),
                    content=c["content"],
                    embedding=c.get("embedding"),
                ))

            doc.page_count = page_count
            if chunks:
                doc.status = "ready"
                doc.error = None
            else:
                doc.status = "failed"
                doc.error = "No extractable text was found in this document."
            db.commit()
            log.info("Ingested %s: %d pages, %d chunks", doc_id, page_count, len(chunks))
            return {"status": doc.status, "pages": page_count, "chunks": len(chunks)}

        except Exception as e:
            db.rollback()
            doc = db.get(Document, doc_id)
            if doc:
                doc.status = "failed"
                doc.error = str(e)[:2000]
                db.commit()
            log.exception("Ingestion failed for %s", doc_id)
            return {"status": "failed", "error": str(e)}
    finally:
        db.close()


# ── Background-thread mode (no Celery/Redis needed) ──────────────────────
def ingest_in_thread(doc_id: str) -> None:
    """Fire-and-forget ingestion in a daemon thread. Upload returns immediately;
    the client polls GET /api/documents/{id} until status flips to ready/failed."""
    threading.Thread(
        target=run_ingest, args=(doc_id,), name=f"ingest-{doc_id}", daemon=True
    ).start()


# ── Optional Celery task (defined only if celery imports cleanly) ────────
try:
    from .celery_app import celery

    @celery.task(name="ingest_document", bind=True)
    def ingest_document(self, doc_id: str) -> dict:   # pragma: no cover (needs a worker)
        return run_ingest(doc_id)
except Exception as e:                                 # celery absent on a slim image
    celery = None
    ingest_document = None
    log.info("Celery unavailable (%s); ingestion will use thread mode.", e)


def _broker_reachable(timeout: float = 0.5) -> bool:
    """Quick liveness probe of the Celery/Redis broker so 'auto' can decide."""
    url = settings.CELERY_BROKER_URL
    if not url or not url.startswith(("redis://", "rediss://")):
        return False
    try:
        import redis
        client = redis.from_url(url, socket_connect_timeout=timeout, socket_timeout=timeout)
        return bool(client.ping())
    except Exception:
        return False


def enqueue_ingest(doc_id: str) -> str:
    """
    Trigger ingestion according to INGEST_MODE. Returns the mode actually used
    ("celery" | "thread"). Falls back to thread mode if Celery isn't usable, so an
    upload never fails just because there's no worker.
    """
    mode = (settings.INGEST_MODE or "auto").lower()

    if mode == "celery" and ingest_document is not None:
        ingest_document.delay(doc_id)
        return "celery"

    if mode == "auto" and ingest_document is not None and _broker_reachable():
        try:
            ingest_document.delay(doc_id)
            return "celery"
        except Exception:
            log.warning("Celery enqueue failed; falling back to thread mode.")

    ingest_in_thread(doc_id)
    return "thread"
