"""
Vision path (ARCHITECTURE.md §2.2, image-only branch): scans/screenshots have
no text layer, so Stage 0 routes them here. Pages are rendered to PNG images
(pypdfium2) and sent directly to a vision-capable LLM (Gemini Flash vision).

Live-verified against GeminiProvider (see
backend/scripts/live_extraction_smoke_test.py) — Gemini Flash accepts PNG
page images directly in the `contents` list alongside the prompt. See
app/extraction/providers/{gemini,openai}.py for the swap-in seam. This module
only builds the (provider-agnostic) inputs: rendered page images + prompt.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING

import pypdfium2 as pdfium

if TYPE_CHECKING:
    from app.extraction.router import PdfSource

DEFAULT_DPI = 150
DEFAULT_MAX_PAGES = 5


def render_pages_to_png(
    pdf_source: PdfSource,
    dpi: int = DEFAULT_DPI,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[bytes]:
    """Render up to `max_pages` pages of the PDF to PNG bytes."""
    if isinstance(pdf_source, (bytes, bytearray)):
        pdf = pdfium.PdfDocument(io.BytesIO(pdf_source))
    elif isinstance(pdf_source, Path):
        pdf = pdfium.PdfDocument(str(pdf_source))
    else:
        pdf = pdfium.PdfDocument(pdf_source)

    scale = dpi / 72
    images: list[bytes] = []
    try:
        page_count = min(len(pdf), max_pages)
        for i in range(page_count):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            images.append(buf.getvalue())
    finally:
        pdf.close()
    return images


def build_vision_prompt(
    vendor_hint: str | None = None,
    retry_context: str | None = None,
) -> str:
    parts: list[str] = [
        "You are an invoice line-item extraction engine. The attached "
        "image(s) are page(s) of a scanned or screenshotted receipt. Extract "
        "every line item, fee, tax, tip, and discount into the JSON schema "
        "provided out-of-band. All money amounts MUST be expressed in "
        "INTEGER MINOR UNITS (paise). Never use floats. Discount and refund "
        "line totals must be negative; every other kind must be >= 0.",
    ]
    if vendor_hint:
        parts.append(f"Vendor hint: {vendor_hint}.")
    if retry_context:
        parts.append(
            "=== Retry: your previous extraction failed validation ===\n"
            + retry_context
        )
    return "\n\n".join(parts)
