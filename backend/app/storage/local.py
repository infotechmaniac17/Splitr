"""
Filesystem-backed PdfStorage — default for dev/tests (no external service
required). Keys are relative paths under `base_dir`; `..`/absolute-path
traversal is rejected so a crafted key can never escape the storage root.
"""

from __future__ import annotations

from pathlib import Path


class LocalFilesystemStorage:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        path = (self.base_dir / key).resolve()
        base = self.base_dir.resolve()
        if base not in path.parents and path != base:
            raise ValueError(f"Storage key {key!r} escapes the storage root")
        return path

    def save(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def load(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()
