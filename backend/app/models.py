"""SQLAlchemy ORM models."""
import uuid
from datetime import datetime

from sqlalchemy import (Column, Integer, String, Text, DateTime, Boolean,
                        ForeignKey, Index, Computed)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector

from .database import Base
from .config import settings


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id                          = Column(Integer, primary_key=True)
    email                       = Column(String(255), unique=True, nullable=False, index=True)
    name                        = Column(String(255), nullable=False)
    password_hash               = Column(String(255), nullable=False)
    role                        = Column(String(20), nullable=False, default="user")
    created_at                  = Column(DateTime, default=datetime.utcnow, nullable=False)
    # email verification
    is_verified                 = Column(Boolean, default=False, nullable=False)
    verification_token          = Column(String(128), nullable=True, index=True)
    verification_token_expiry   = Column(DateTime, nullable=True)
    # password reset
    reset_token                 = Column(String(128), nullable=True, index=True)
    reset_token_expiry          = Column(DateTime, nullable=True)

    documents = relationship("Document", back_populates="owner", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id          = Column(String(36), primary_key=True, default=_uuid)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name        = Column(String(512), nullable=False)
    mime_type   = Column(String(128))
    storage_key = Column(String(512))
    page_count  = Column(Integer, default=0)
    status      = Column(String(20), default="processing", nullable=False)
    error       = Column(Text)
    summary     = Column(JSONB)
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    owner  = relationship("User", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id          = Column(Integer, primary_key=True)
    doc_id      = Column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_num    = Column(Integer, nullable=False)
    chunk_index = Column(Integer, nullable=False, default=0)
    bbox_json   = Column(JSONB)
    content     = Column(Text, nullable=False)
    tsv         = Column(TSVECTOR, Computed("to_tsvector('english', content)", persisted=True))
    embedding   = Column(Vector(settings.effective_embed_dim))

    document = relationship("Document", back_populates="chunks")

    __table_args__ = (
        Index("idx_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("idx_chunks_embedding", "embedding",
              postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id             = Column(Integer, primary_key=True)
    doc_id         = Column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id        = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role           = Column(String(20), nullable=False)
    content        = Column(Text, nullable=False)
    citations_json = Column(JSONB)
    created_at     = Column(DateTime, default=datetime.utcnow, nullable=False)


class Annotation(Base):
    __tablename__ = "annotations"

    id            = Column(Integer, primary_key=True)
    doc_id        = Column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    page_num      = Column(Integer, nullable=False)
    rect_coords   = Column(JSONB)
    selected_text = Column(Text)
    ai_notes      = Column(Text)
    tags          = Column(JSONB)
    action        = Column(String(40))
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False)