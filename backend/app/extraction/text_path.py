"""
Text path (ARCHITECTURE.md §2.2, digital-PDF branch): pdfplumber extracts raw
text + detected tables, which are handed to the LLM as context alongside the
strict JSON schema. pdfplumber never invents numbers — the LLM only
*structures* text that is already exact, which is why this path has
near-zero transcription error on digital PDFs.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import pdfplumber

if TYPE_CHECKING:
    from app.extraction.router import PdfSource


class ExtractedContent(TypedDict):
    text: str
    tables: list[str]


def _open_pdf(pdf_source: PdfSource) -> Any:  # pdfplumber ships no type stubs
    if isinstance(pdf_source, (bytes, bytearray)):
        return pdfplumber.open(io.BytesIO(pdf_source))
    if isinstance(pdf_source, Path):
        return pdfplumber.open(str(pdf_source))
    return pdfplumber.open(pdf_source)


def extract_text_and_tables(pdf_source: PdfSource) -> ExtractedContent:
    """Pull raw text + rendered tables out of every page of the PDF."""
    text_parts: list[str] = []
    table_parts: list[str] = []
    with _open_pdf(pdf_source) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
            for table in page.extract_tables() or []:
                rendered = "\n".join(
                    " | ".join(cell or "" for cell in row) for row in table
                )
                table_parts.append(rendered)
    return {"text": "\n".join(text_parts).strip(), "tables": table_parts}


def build_text_prompt(
    content: ExtractedContent,
    vendor_hint: str | None = None,
    retry_context: str | None = None,
) -> str:
    """
    Build the prompt for the text-path LLM call. The strict JSON Schema
    itself is passed out-of-band via the provider's structured-output
    parameter (see app.extraction.schema.extraction_json_schema), not
    embedded in this prompt text.
    """
    parts: list[str] = [
        "You are an invoice line-item extraction engine. Extract every line "
        "item, fee, tax, tip, and discount from the invoice text below into "
        "the JSON schema provided out-of-band. All money amounts MUST be "
        "expressed in INTEGER MINOR UNITS (paise) — e.g. Rs. 12.50 -> 1250. "
        "Never use floats. Discount and refund line totals must be negative; "
        "every other kind must be >= 0.",
    ]
    if vendor_hint:
        parts.append(f"Vendor hint: {vendor_hint}.")
    parts.append("=== Invoice text ===\n" + content["text"])
    if content["tables"]:
        parts.append("=== Detected tables ===\n" + "\n---\n".join(content["tables"]))
    if retry_context:
        parts.append(
            "=== Retry: your previous extraction failed validation ===\n"
            + retry_context
        )
    return "\n\n".join(parts)
