"""SQLAlchemy engine, session factory, and DB bootstrap."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from .config import settings

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Ensure pgvector exists, then create all tables + indexes."""
    from . import models  # noqa: F401  (register mappers)

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)
