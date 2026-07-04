"""
Storage interface for raw uploaded PDFs.

Deliberately tiny — expenses.pdf_object_key (ARCHITECTURE.md §3) is an
opaque string key; callers never need to know whether it resolves to a
local path or an S3 key. Methods are sync (plain file/HTTP I/O) — callers
running inside an async route must offload via `asyncio.to_thread` to avoid
blocking the event loop (see app/api/expenses.py).
"""

from __future__ import annotations

from typing import Protocol


class PdfStorage(Protocol):
    def save(self, key: str, data: bytes) -> None:
        """Persist `data` under `key`. Overwrites if the key already exists."""
        ...

    def load(self, key: str) -> bytes:
        """
        Return the bytes stored under `key`.

        Raises FileNotFoundError if the key does not exist.
        """
        ...
