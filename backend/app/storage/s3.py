"""
S3-compatible PdfStorage (AWS S3, GCS interop, MinIO — ARCHITECTURE.md §1.2).

`boto3` is not in the project's core dependency set yet (see pyproject.toml
— it is optional and only imported lazily by `get_default_storage()` when
`OBJECT_STORAGE_BUCKET` is actually configured). Install it in whatever
environment sets that env var: `pip install boto3`.
"""

from __future__ import annotations

from typing import Any


class S3Storage:
    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ) -> None:
        try:
            import boto3  # noqa: PLC0415
        except (
            ImportError
        ) as exc:  # pragma: no cover - exercised only when misconfigured
            raise RuntimeError(
                "OBJECT_STORAGE_BUCKET is set but boto3 is not installed. "
                "Run `pip install boto3` (or unset OBJECT_STORAGE_BUCKET to "
                "fall back to local filesystem storage)."
            ) from exc

        self.bucket = bucket
        self._client: Any = boto3.client(
            "s3", endpoint_url=endpoint_url, region_name=region_name
        )

    def save(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def load(self, key: str) -> bytes:
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=key)
        except self._client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(key) from exc
        body: bytes = obj["Body"].read()
        return body
