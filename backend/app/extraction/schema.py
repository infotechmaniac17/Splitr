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
    DiscountType,  # noqa: TC001 — needed at runtime for pydantic
    GstMode,  # noqa: TC001 — needed at runtime for pydantic
    LineItemKind,  # noqa: TC001 — needed at runtime for pydantic
    TaxComponentName,  # noqa: TC001 — needed at runtime for pydantic
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
    # M6 item 4: only populated when the invoice shows a PER-LINE-ITEM GST
    # rate (restaurant-style 5%/18% per dish) -- i.e. when the overall
    # invoice's gst_mode ends up 'item_level'. NULL on every other line.
    gst_rate: Decimal | None = Field(default=None, ge=0, le=100)
    gst_amount_minor: int | None = Field(default=None, ge=0)


class ExtractedTaxComponent(BaseModel):
    """
    M6 item 4: one named GST component (CGST/SGST/IGST/GST/CESS) with an
    optional printed rate, extracted as STRUCTURED data alongside (not
    instead of) any amount-based kind='tax' expense_line_items row that
    covers the same tax. See app/domain/gst.py for how the two are cross-
    checked against each other under gst_mode='item_level'.
    """

    name: TaxComponentName
    rate: Decimal | None = Field(default=None, ge=0, le=100)
    amount_minor: int = Field(ge=0)


class ExtractedDiscount(BaseModel):
    """
    M6 item 4 (discount follow-up to item 3): a structured summary of a
    single printed coupon/promo/discount line, extracted directly into the
    same shape as expenses.discount_* (see app/domain/vendor_discount.py).
    Populated only when the invoice text itself states a discount -- this
    is independent of (and never overwrites) a 'manual' snapshot, and is
    itself overwritten by a later-matched vendor rule -- see
    app/extraction/tasks.py's persistence ordering.
    """

    type: DiscountType
    value_minor: int | None = Field(default=None, gt=0)
    percent: Decimal | None = Field(default=None, gt=0, le=100)
    threshold_minor: int | None = Field(default=None, ge=0)


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
    # M6 item 4: GST structured data -- see GstMode and ExtractedTaxComponent.
    gst_mode: GstMode = GstMode.none
    tax_components: list[ExtractedTaxComponent] = Field(default_factory=list)
    # M6 item 4 (discount follow-up): structured discount summary, if the
    # invoice printed one. See ExtractedDiscount.
    discount: ExtractedDiscount | None = None


def extraction_json_schema() -> dict[str, Any]:
    """
    Strict JSON Schema for LLM structured-output mode (schema-enforced,
    temp=0) — ARCHITECTURE.md §2.2.

    Passed to the provider as the schema-enforcement parameter (Gemini's
    `response_schema`, OpenAI's `json_schema` response_format) so the model's
    output is constrained at generation time, not merely validated afterward.
    """
    return ExtractedInvoice.model_json_schema()
