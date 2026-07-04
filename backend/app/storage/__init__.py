"""
Object storage for uploaded PDFs (ARCHITECTURE.md §1.2: "S3 / GCS with
lifecycle rules"). Kept behind a small interface (`PdfStorage`) exactly like
`app.extraction.providers` keeps LLM vendors swappable — application code
(the upload/serving endpoints) depends only on the interface, never on a
concrete backend.

`get_default_storage()` picks:
  - `S3Storage` when `settings.OBJECT_STORAGE_BUCKET` is set (production —
    any S3-compatible store: AWS S3, GCS interop, MinIO, per .env.example).
  - `LocalFilesystemStorage` otherwise (dev/tests — no external service
    required, mirrors `NullProvider`'s "never crashes on missing config"
    posture for extraction providers).
"""

from __future__ import annotations

from app.config import settings
from app.storage.base import PdfStorage
from app.storage.local import LocalFilesystemStorage

__all__ = ["PdfStorage", "LocalFilesystemStorage", "get_default_storage"]


def get_default_storage() -> PdfStorage:
    if settings.OBJECT_STORAGE_BUCKET:
        from app.storage.s3 import S3Storage  # noqa: PLC0415 — lazy: boto3 optional

        return S3Storage(
            bucket=settings.OBJECT_STORAGE_BUCKET,
            endpoint_url=settings.OBJECT_STORAGE_ENDPOINT or None,
            region_name=settings.AWS_REGION,
        )
    return LocalFilesystemStorage(settings.PDF_STORAGE_DIR)
