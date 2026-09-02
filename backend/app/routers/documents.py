"""
Document routes (FR-01) — upload, status polling, listing, file access, delete.

Upload is validated (extension, size ≤ MAX_UPLOAD_MB, magic-byte sniff, PDF page
cap), stored in S3/MinIO, then handed to Celery for async ingestion. The document
is returned immediately with status="processing"; the client polls GET /{id} until
status becomes "ready" or "failed". File bytes are never served through the API —
clients fetch them via a short-lived presigned URL.
"""
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..models import User, Document
from ..schemas import DocumentOut, PresignedUrlOut
from .. import storage, tasks

log = logging.getLogger("docusense.documents")
router = APIRouter(prefix="/api/documents", tags=["documents"])

# Magic-byte signatures per extension (defense against spoofed content types).
_MAGIC = {
    ".pdf":  [b"%PDF"],
    ".png":  [b"\x89PNG\r\n\x1a\n"],
    ".jpg":  [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".docx": [b"PK\x03\x04"],          # docx is a zip container
    ".txt":  [],                        # text has no reliable signature
}


def _ext(filename: str) -> str:
    name = (filename or "").lower()
    dot = name.rfind(".")
    return name[dot:] if dot != -1 else ""


def _sniff_ok(ext: str, head: bytes) -> bool:
    sigs = _MAGIC.get(ext, [])
    if not sigs:
        return True
    return any(head.startswith(sig) for sig in sigs)


def _pdf_page_count(content: bytes) -> int:
    try:
        import fitz
        with fitz.open(stream=content, filetype="pdf") as d:
            return d.page_count
    except Exception:
        return 0


def _owned_doc(db: Session, user: User, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


@router.post("", response_model=DocumentOut, status_code=201)
def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ext = _ext(file.filename)
    if ext not in settings.allowed_extensions:
        raise HTTPException(400, f"Unsupported file type '{ext or file.filename}'. "
                                 f"Allowed: {', '.join(sorted(settings.allowed_extensions))}")

    content = file.file.read()
    size = len(content)
    if size == 0:
        raise HTTPException(400, "The uploaded file is empty.")
    if size > settings.max_upload_bytes:
        raise HTTPException(413, f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.")

    if not _sniff_ok(ext, content[:16]):
        raise HTTPException(400, "File contents do not match the file extension.")

    if ext == ".pdf":
        pages = _pdf_page_count(content)
        if pages > settings.MAX_PAGES:
            raise HTTPException(400, f"PDF exceeds the {settings.MAX_PAGES}-page limit "
                                     f"({pages} pages).")

    doc = Document(
        user_id=user.id,
        name=file.filename,
        mime_type=file.content_type,
        status="processing",
    )
    db.add(doc)
    db.flush()                                    # get doc.id before building the key

    doc.storage_key = f"{user.id}/{doc.id}/{file.filename}"
    try:
        storage.upload_bytes(doc.storage_key, content, file.content_type or "application/octet-stream")
    except Exception as e:
        db.rollback()
        log.exception("Storage upload failed")
        raise HTTPException(502, f"Could not store the file: {e}")

    db.commit()
    db.refresh(doc)

    # Kick off ingestion (Celery worker or in-process thread, per INGEST_MODE).
    try:
        mode = tasks.enqueue_ingest(doc.id)
        log.info("Queued ingestion for %s via %s mode", doc.id, mode)
    except Exception as e:
        log.exception("Failed to start ingestion")
        doc.status = "failed"
        doc.error = f"Could not start ingestion: {e}"
        db.commit()

    return doc


@router.get("", response_model=list[DocumentOut])
def list_documents(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return (
        db.query(Document)
        .filter(Document.user_id == user.id)
        .order_by(Document.uploaded_at.desc())
        .all()
    )


@router.get("/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _owned_doc(db, user, doc_id)


@router.get("/{doc_id}/file", response_model=PresignedUrlOut)
def get_document_file(doc_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = _owned_doc(db, user, doc_id)
    if not doc.storage_key:
        raise HTTPException(404, "No file is associated with this document.")
    url = storage.presigned_get_url(doc.storage_key)
    return PresignedUrlOut(url=url, expires_in=settings.PRESIGN_EXPIRE_SECONDS)


@router.get("/{doc_id}/content")
def get_document_content(doc_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """
    Stream the raw file bytes through the API for the in-app viewer.
    For .docx files, extracts and returns plain text instead of raw binary.
    """
    doc = _owned_doc(db, user, doc_id)
    if not doc.storage_key:
        raise HTTPException(404, "No file is associated with this document.")
    try:
        data = storage.download_bytes(doc.storage_key)
    except Exception as e:
        raise HTTPException(502, f"Could not read the file: {e}")

    # For DOCX files, extract plain text so the frontend can display it
    if doc.name.lower().endswith(".docx"):
        try:
            from app.rag import _extract_docx
            pages, _ = _extract_docx(data)
            text = "\n\n".join(
                block["text"]
                for page in pages
                for block in page.get("blocks", [])
                if block.get("text", "").strip()
            )
            return Response(
                text.encode("utf-8"),
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": f'inline; filename="{doc.name}.txt"',
                         "Cache-Control": "private, max-age=300"},
            )
        except Exception as e:
            # Fall through to raw bytes if extraction fails
            pass

    return Response(
        data,
        media_type=doc.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{doc.name}"',
                 "Cache-Control": "private, max-age=300"},
    )


@router.delete("/{doc_id}", status_code=204)
def delete_document(doc_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = _owned_doc(db, user, doc_id)
    if doc.storage_key:
        storage.delete_object(doc.storage_key)     # best-effort; DB is source of truth
    db.delete(doc)                                  # chunks / messages / annotations cascade
    db.commit()
    return None