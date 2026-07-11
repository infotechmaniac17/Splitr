"""
Deterministic validation engine (ARCHITECTURE.md §2.2, Stage 1) — pure
Python, no AI, and the SOLE gate for parse_status='parsed'.

Invariants checked (all arithmetic in integer minor units — never float,
per CLAUDE.md invariant #1):
  - Σ(qty × unit_price) per line == line_total (±1 minor unit)
  - Σ(all line totals) == invoice_total_minor (±1 minor unit)
    (fees/taxes are positive line totals, discounts/refunds negative, so a
    plain sum over every line already encodes "items + fees + taxes -
    discounts == invoice_total")
  - dates parse
  - currency is a recognized/consistent code
  - quantity > 0
  - no negative unit prices except explicit discount/refund lines
  - sign convention: discount/refund total_minor <= 0, everything else >= 0

No code path outside this module may set parse_status='parsed'
(project invariant #4) — app.extraction.pipeline is the only caller and it
routes every result through validate_extraction() first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING

from app.domain.gst import GstCheckResult, base_item_totals_minor, check_gst_invariants
from app.domain.models import LineItemKind

if TYPE_CHECKING:
    from app.extraction.schema import ExtractedInvoice

# ±1 minor unit tolerance absorbs sub-paisa rounding in vendor invoices
# (ARCHITECTURE.md §2.2 validation spec).
TOLERANCE_MINOR = 1

VALID_CURRENCIES = {"INR", "USD", "EUR", "GBP"}

_SIGNED_NEGATIVE_KINDS = {LineItemKind.discount, LineItemKind.refund}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)


@dataclass
class ValidationIssue:
    code: str
    message: str
    line_no: int | None = None


@dataclass
class ValidationResult:
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def mismatch_prompt(self) -> str:
        """
        Human-readable mismatch text injected into the retry prompt
        (ARCHITECTURE.md §2.2 example: "your line items sum to 842 but total
        is 857 — you likely missed a fee row; re-extract").
        """
        return "; ".join(i.message for i in self.issues)


def parse_date(raw: str | None) -> date | None:
    """Best-effort date parse across common invoice date formats."""
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def validate_extraction(invoice: ExtractedInvoice) -> ValidationResult:
    """Run every deterministic check against one extracted invoice."""
    issues: list[ValidationIssue] = []

    if not invoice.line_items:
        issues.append(ValidationIssue("no_line_items", "No line items were extracted."))

    for li in invoice.line_items:
        if li.quantity <= 0:
            issues.append(
                ValidationIssue(
                    "bad_quantity",
                    f"line {li.line_no}: quantity {li.quantity} must be > 0",
                    li.line_no,
                )
            )

        if (
            li.unit_price_minor is not None
            and li.unit_price_minor < 0
            and li.kind not in _SIGNED_NEGATIVE_KINDS
        ):
            issues.append(
                ValidationIssue(
                    "negative_unit_price",
                    f"line {li.line_no}: unit_price_minor {li.unit_price_minor} "
                    f"is negative for kind={li.kind.value}",
                    li.line_no,
                )
            )

        if li.kind in _SIGNED_NEGATIVE_KINDS:
            if li.total_minor > 0:
                issues.append(
                    ValidationIssue(
                        "sign_convention",
                        f"line {li.line_no}: kind={li.kind.value} total_minor "
                        f"must be <= 0, got {li.total_minor}",
                        li.line_no,
                    )
                )
        elif li.total_minor < 0:
            issues.append(
                ValidationIssue(
                    "sign_convention",
                    f"line {li.line_no}: kind={li.kind.value} total_minor "
                    f"must be >= 0, got {li.total_minor}",
                    li.line_no,
                )
            )

        if li.unit_price_minor is not None:
            expected = int((li.quantity * li.unit_price_minor).to_integral_value())
            if abs(expected - li.total_minor) > TOLERANCE_MINOR:
                issues.append(
                    ValidationIssue(
                        "line_arithmetic",
                        f"line {li.line_no}: quantity({li.quantity}) x "
                        f"unit_price_minor({li.unit_price_minor}) = {expected}, "
                        f"but total_minor is {li.total_minor}",
                        li.line_no,
                    )
                )

    lines_sum = sum(li.total_minor for li in invoice.line_items)
    if abs(lines_sum - invoice.invoice_total_minor) > TOLERANCE_MINOR:
        issues.append(
            ValidationIssue(
                "invoice_total_mismatch",
                f"line items sum to {lines_sum} but invoice total is "
                f"{invoice.invoice_total_minor} — you likely missed a fee, "
                "tax, or discount row; re-extract.",
            )
        )

    if invoice.currency not in VALID_CURRENCIES:
        issues.append(
            ValidationIssue(
                "currency_unrecognized",
                f"currency {invoice.currency!r} is not a recognized code "
                f"(expected one of {sorted(VALID_CURRENCIES)})",
            )
        )

    if invoice.invoice_date is not None and parse_date(invoice.invoice_date) is None:
        issues.append(
            ValidationIssue(
                "bad_date",
                f"invoice_date {invoice.invoice_date!r} could not be parsed",
            )
        )

    return ValidationResult(ok=not issues, issues=issues)


def validate_gst(invoice: ExtractedInvoice) -> GstCheckResult:
    """
    M6 item 4: run the GST-specific arithmetic invariants (app/domain/gst.py)
    against a freshly extracted invoice.

    Deliberately SEPARATE from validate_extraction() / parse_status: a GST
    reconciliation failure here does NOT flip parse_status to
    'needs_review' -- it feeds `expenses.needs_review` instead (set by
    app/extraction/tasks.py). Keeping the two independent means a GST-only
    inconsistency never has to be retrofitted into parse_status's already-
    exhaustively-enumerated legal transition graph (see
    app/domain/pg_guards.py's EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V2
    docstring for how carefully that graph is scoped to grepped, real code
    paths) or its Postgres trigger. It also means an invoice whose BASE
    arithmetic is fine but whose GST breakdown looks off is not blocked
    from human review via the needs_review CORRECTION flow (PUT
    .../line-items is gated on parse_status='needs_review', which this
    does not set) -- instead it's blocked at a later, more appropriate
    point: confirmation (see app/api/expenses.py).
    """
    # M6 item 5: item_totals now comes from the single shared definition
    # (app.domain.gst.is_base_gst_line / base_item_totals_minor) also used
    # by app.domain.splitting.compute_allocation, so the validator and the
    # allocator can never disagree about what "the base" means.
    item_totals = base_item_totals_minor(invoice.line_items, invoice.gst_mode)
    discount_amount = abs(
        sum(
            li.total_minor
            for li in invoice.line_items
            if li.kind == LineItemKind.discount
        )
    )
    tax_component_amounts = [tc.amount_minor for tc in invoice.tax_components]
    line_gst_amounts = [
        li.gst_amount_minor
        for li in invoice.line_items
        if li.gst_amount_minor is not None
    ]
    has_tax_kind_line_items = any(
        li.kind == LineItemKind.tax for li in invoice.line_items
    )
    return check_gst_invariants(
        gst_mode=invoice.gst_mode,
        item_totals_minor=item_totals,
        discount_amount_minor=discount_amount,
        tax_component_amounts_minor=tax_component_amounts,
        invoice_total_minor=invoice.invoice_total_minor,
        line_gst_amounts_minor=line_gst_amounts,
        has_line_gst_data=bool(line_gst_amounts),
        has_component_data=bool(tax_component_amounts),
        has_tax_kind_line_items=has_tax_kind_line_items,
    )
