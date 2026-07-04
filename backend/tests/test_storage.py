"""
Unit tests for app.storage — the PdfStorage backend used by the M4 upload /
PDF-serving endpoints.
"""

from __future__ import annotations

import pytest

from app.storage.local import LocalFilesystemStorage


def test_local_storage_save_and_load_roundtrip(tmp_path) -> None:
    storage = LocalFilesystemStorage(tmp_path)
    storage.save("expenses/abc.pdf", b"%PDF-1.4 fake content")
    assert storage.load("expenses/abc.pdf") == b"%PDF-1.4 fake content"


def test_local_storage_creates_nested_directories(tmp_path) -> None:
    storage = LocalFilesystemStorage(tmp_path / "nested" / "dir")
    storage.save("a/b/c.pdf", b"data")
    assert storage.load("a/b/c.pdf") == b"data"


def test_local_storage_missing_key_raises_file_not_found(tmp_path) -> None:
    storage = LocalFilesystemStorage(tmp_path)
    with pytest.raises(FileNotFoundError):
        storage.load("does/not/exist.pdf")


def test_local_storage_overwrites_existing_key(tmp_path) -> None:
    storage = LocalFilesystemStorage(tmp_path)
    storage.save("k.pdf", b"first")
    storage.save("k.pdf", b"second")
    assert storage.load("k.pdf") == b"second"


def test_local_storage_rejects_path_traversal(tmp_path) -> None:
    storage = LocalFilesystemStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.save("../escape.pdf", b"data")
