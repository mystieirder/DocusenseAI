"""
Admin routes (FR §4 — RBAC).

Every endpoint here is gated by `require_role("admin")`, so a normal user's
token yields 403. This is what makes the 'admin' role a real, enforced control
rather than an unused column. Grant the role by listing an email in ADMIN_EMAILS
(applied on register and reconciled on login — see routers/auth.py).
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_role
from ..models import User, Document, DocumentChunk, Annotation, ChatMessage
from ..schemas import UserOut

log = logging.getLogger("docusense.admin")
router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/stats")
def instance_stats(db: Session = Depends(get_db),
                   _admin: User = Depends(require_role("admin"))):
    """Instance-wide counts — a lightweight operational dashboard for the owner."""
    status_rows = (
        db.query(Document.status, func.count(Document.id))
        .group_by(Document.status)
        .all()
    )
    return {
        "users": db.query(func.count(User.id)).scalar() or 0,
        "documents": db.query(func.count(Document.id)).scalar() or 0,
        "documents_by_status": {s: c for s, c in status_rows},
        "chunks": db.query(func.count(DocumentChunk.id)).scalar() or 0,
        "chat_messages": db.query(func.count(ChatMessage.id)).scalar() or 0,
        "annotations": db.query(func.count(Annotation.id)).scalar() or 0,
    }


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db),
               _admin: User = Depends(require_role("admin"))):
    """List all accounts (admin only)."""
    return db.query(User).order_by(User.created_at.desc()).all()
