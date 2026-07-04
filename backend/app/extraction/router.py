"""
Stage 0 — text-layer detection (ARCHITECTURE.md §2.2).

Classifies a PDF as having a selectable text layer (route: text path via
pdfplumber + LLM) or being image-only / scanned (route: vision path via
rendered page images + vision LLM). Target latency is <100ms; this module
only reads text from the first few pages (extraction of the full document
happens later, only on the chosen path) to keep the check cheap.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pdfplumber

PdfSource = bytes | bytearray | str | Path | io.BytesIO

# Below this many extracted characters (across the sampled pages), the PDF is
# treated as having no usable text layer.
MIN_CHARS_FOR_TEXT_LAYER = 20

# Only sample the first few pages for the Stage-0 check — invoices are
# 1-3 pages, and we want this to stay well under 100ms even on longer docs.
DEFAULT_MAX_SAMPLE_PAGES = 3


def _open_pdf(pdf_source: PdfSource) -> Any:  # pdfplumber ships no type stubs
    if isinstance(pdf_source, (bytes, bytearray)):
        return pdfplumber.open(io.BytesIO(pdf_source))
    if isinstance(pdf_source, Path):
        return pdfplumber.open(str(pdf_source))
    return pdfplumber.open(pdf_source)


def has_text_layer(
    pdf_source: PdfSource,
    min_chars: int = MIN_CHARS_FOR_TEXT_LAYER,
    max_pages: int = DEFAULT_MAX_SAMPLE_PAGES,
) -> bool:
    """
    True if the PDF has a selectable text layer (digital PDF / e-invoice),
    False if it looks image-only (scan/screenshot with no embedded text).
    """
    total_chars = 0
    with _open_pdf(pdf_source) as pdf:
        for page in pdf.pages[:max_pages]:
            text = page.extract_text() or ""
            total_chars += len(text.strip())
            if total_chars >= min_chars:
                return True
    return total_chars >= min_chars


def route(pdf_source: PdfSource) -> str:
    """Stage 0 router: returns 'text' or 'vision'."""
    return "text" if has_text_layer(pdf_source) else "vision"
