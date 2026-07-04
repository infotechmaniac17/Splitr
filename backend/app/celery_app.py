"""
Celery application instance — async job runner (CLAUDE.md stack: Celery +
Redis). Broker/result-backend URL comes from settings.REDIS_URL (env var),
matching the docker-compose Redis service.

Task modules register themselves on this instance (see
app/extraction/tasks.py). Import this module (not a fresh Celery(...)) from
anywhere that needs to enqueue a task.
"""

from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery(
    "splitr",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.extraction.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)
