"""
Celery application — async document ingestion (FR-01).

Uploads return immediately with status "processing"; this worker pulls the file
from object storage, extracts + chunks + embeds it, writes chunks to Postgres,
and flips the document to "ready" (or "failed"). Redis is broker + result backend.

Run the worker with:
    celery -A app.celery_app.celery worker --loglevel=info --concurrency=2
"""
from celery import Celery

from .config import settings

celery = Celery(
    "docusense",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.tasks"],          # worker imports task definitions from here
)

celery.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_max_tasks_per_child=20,  # recycle workers to release the embedding model's memory
    task_time_limit=1800,           # 30 min hard cap per document
    task_soft_time_limit=1680,
)
