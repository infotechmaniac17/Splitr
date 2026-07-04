"""
Pydantic models for the LLM structured-output contract (ARCHITECTURE.md §2.2,
§3 `expense_line_items`).

`ExtractedInvoice` is the strict schema handed to the provider in structured-
output mode (temp=0) and used to parse/validate whatever JSON comes back
*before* it reaches the deterministic validation engine (app.extraction.validation).

All money fields are integer minor units (paise) — never float — per project
invariant #1. The LLM is instructed (see text_path.py / vision_path.py prompts)
to do the paise conversion itself; `app.extraction.currency` is available for
any code path that needs to parse a raw currency string independently (e.g.
vendor-hint heuristics, or normalizing values the model returns as strings).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.domain.models import (
    LineItemKind,  # noqa: TC001 — needed at runtime for pydantic
)


class ExtractedLineItem(BaseModel):
    """One row of expense_line_items as produced by the extractor."""

    line_no: int = Field(ge=1)
    kind: LineItemKind
    description: str | None = None
    quantity: Decimal = Field(default=Decimal("1"))
    # Signed: negative for discount/refund kinds (validated downstream).
    unit_price_minor: int | None = None
    total_minor: int


class ExtractedInvoice(BaseModel):
    """Top-level structured-output payload for one invoice/expense."""

    vendor: str | None = None
    # Kept as a raw string (not `date`) so a malformed date from the model is
    # a validation-engine finding, not a silent pydantic coercion/crash.
    invoice_date: str | None = None
    invoice_number: str | None = None
    currency: str = "INR"
    line_items: list[ExtractedLineItem] = Field(default_factory=list)
    invoice_total_minor: int
    subtotal_minor: int | None = None


def extraction_json_schema() -> dict[str, Any]:
    """
    Strict JSON Schema for LLM structured-output mode (schema-enforced,
    temp=0) — ARCHITECTURE.md §2.2.

    Passed to the provider as the schema-enforcement parameter (Gemini's
    `response_schema`, OpenAI's `json_schema` response_format) so the model's
    output is constrained at generation time, not merely validated afterward.
    """
    return ExtractedInvoice.model_json_schema()
