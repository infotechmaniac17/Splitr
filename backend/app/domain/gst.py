"""
M6 item 4: GST/tax invariant checks — pure functions, no I/O (CLAUDE.md:
domain logic lives in app/domain/, is pure, and trivially testable;
mirrors app/extraction/validation.py's style for the base arithmetic
invariants).

Two call sites build the SAME core check from different sources:
  - app.extraction.validation.validate_gst -- from an in-memory
    ExtractedInvoice at extraction time, used to set the persisted
    `expenses.needs_review` boolean (see app/extraction/tasks.py for why
    this is a boolean independent of parse_status, not a parse_status
    transition).
  - app.api.expenses.confirm_expense -- from the CURRENTLY PERSISTED
    expense_line_items / expense_tax_components rows, at confirm time. The
    persisted `needs_review` boolean has no "why" attached, so the confirm
    endpoint recomputes the same check against current DB state to name
    the specific failed invariant in its 4xx response.

All arithmetic is on plain Python ints (money in minor units) — never
float, per CLAUDE.md invariant #1.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.domain.models import DiscountSource, DiscountType, GstMode, LineItemKind
from app.domain.rounding import percent_of_minor

# ±1 minor unit tolerance, matching app/extraction/validation.py's
# TOLERANCE_MINOR (sub-paisa rounding on vendor invoices).
TOLERANCE_MINOR = 1


@dataclass
class GstIssue:
    code: str
    message: str


@dataclass
class GstCheckResult:
    ok: bool
    issues: list[GstIssue] = field(default_factory=list)

    def detail(self) -> str:
        """Human-readable summary naming every failed invariant."""
        return "; ".join(f"{i.code}: {i.message}" for i in self.issues)


# ---------------------------------------------------------------------------
# M6 item 5 (governing principle): THE single, shared definition of which
# lines count as the GST/discount "base" (an expense's item totals). Used
# identically by:
#   - this module's callers (app.extraction.validation.validate_gst,
#     app.api.expenses.confirm_expense) to build `item_totals_minor` below;
#   - app.domain.splitting.compute_allocation, to build the actual line set
#     it feeds into compute_shares() for the pre-discount/pre-GST "base"
#     shares.
# One definition, one import each side -- the validator and the allocator
# can never disagree about what "the base" means because they are not
# allowed to each reimplement the filter.
# ---------------------------------------------------------------------------


def is_base_gst_line(line: Any, gst_mode: GstMode) -> bool:
    """
    True for every line EXCEPT kind='discount' and kind='tax' -- i.e. items,
    fees, tip, and (signed) refunds. Accepts any object exposing `.kind`
    (ExtractedLineItem, ExpenseLineItem ORM rows, and
    app.domain.splitting.LineInput all qualify) so the same predicate works
    at extraction time, confirm time, and inside the pure allocator.

    `gst_mode` is accepted but not currently branched on: today the base
    definition is identical for every mode (see check_gst_invariants'
    docstring -- item_totals_minor is computed the same way whether the
    invoice is 'none', 'invoice_exclusive', 'invoice_inclusive', or
    'item_level'). It is kept as a parameter so a future mode-specific
    carve-out has one obvious place to add it, rather than forking the
    filter separately at each call site.
    """
    return line.kind not in (LineItemKind.tax, LineItemKind.discount)


def base_item_totals_minor(lines: Iterable[Any], gst_mode: GstMode) -> int:
    """Sum of total_minor over every line for which is_base_gst_line is True."""
    return sum(
        int(line.total_minor) for line in lines if is_base_gst_line(line, gst_mode)
    )


def check_gst_invariants(
    *,
    gst_mode: GstMode,
    item_totals_minor: int,
    discount_amount_minor: int,
    tax_component_amounts_minor: list[int],
    invoice_total_minor: int,
    line_gst_amounts_minor: list[int],
    has_line_gst_data: bool,
    has_component_data: bool,
    has_tax_kind_line_items: bool = False,
) -> GstCheckResult:
    """
    Check the GST-specific arithmetic invariants for one invoice/expense.

    `item_totals_minor` is the sum of every line total EXCLUDING kind='tax'
    and kind='discount' lines (i.e. items + fees + tip) -- this is a
    deliberate, documented interpretation of the spec's "sum(item totals)":
    a narrower reading (only kind='item' lines) would spuriously fail on
    completely ordinary invoices carrying a delivery/platform/packing fee,
    since those fees are never GST/discount lines but are still part of the
    pre-tax subtotal. See the M6 item 4 report for this call.

    kind='refund' lines are INTENTIONALLY INCLUDED in `item_totals_minor`,
    signed (a refund's total_minor is negative, per
    app/extraction/validation.py's sign convention), so a partial refund
    correctly nets against the original item total it reverses. This is
    load-bearing for invariant reconciliation on any expense that has had a
    refund applied to it — do NOT exclude refund lines from this sum
    without a deliberate spec change; doing so would silently
    over-reconcile (treat the refunded amount as still present) on any
    gst_mode other than 'none'.

    `discount_amount_minor` must already be a non-negative magnitude (the
    caller is responsible for turning a stored/extracted negative discount
    total into a positive amount before calling this).

    `has_tax_kind_line_items` (M6 item 5, OQ-1b): whether the invoice/expense
    ALSO carries at least one kind='tax' line item. OQ-1b asked what the
    validator's current invoice_inclusive handling does with kind='tax'
    lines -- the answer (read from this function's own item_totals_minor
    formula, unchanged by this parameter): a kind='tax' line is EXCLUDED
    from item_totals_minor for every gst_mode, including invoice_inclusive,
    by is_base_gst_line/base_item_totals_minor above (which every call site
    now uses) -- there is no branch on gst_mode in that filter, so this was
    never actually ambiguous in the arithmetic itself. What WAS unaddressed
    is the separate, structural question of whether a standalone tax LINE
    coexisting with gst_mode='invoice_inclusive' is itself suspicious data
    (inclusive means "tax is baked into prices/total", so a printed,
    separately-broken-out tax line under that mode indicates either a
    mis-detected gst_mode or an extraction that partially treated the
    invoice as exclusive) -- we flag that combination as a new invariant
    below rather than silently accepting it, since it is exactly the kind of
    "looks fine numerically, is wrong structurally" case this module exists
    to catch.

    Invariants (exact to the paisa, ±TOLERANCE_MINOR):
      - none:              nothing to check UNLESS a discount is actually in
                           play (discount_amount_minor > 0), in which case
                           item_totals - discount == invoice_total (see
                           `total_mismatch_with_discount` below).
      - invoice_exclusive: item_totals - discount + Σ(tax components)
                           == invoice_total
      - invoice_inclusive: item_totals - discount == invoice_total;
                           tax components are informational only, but each
                           must still be <= invoice_total (sanity bound).
      - item_level:        Σ(line gst_amount_minor) == Σ(tax component
                           amounts), but ONLY when both sides have at least
                           one data point -- an invoice that prints
                           per-item rates but never printed a components
                           summary line (or vice versa) has nothing to
                           reconcile against, so the check is skipped
                           rather than manufacturing a false positive. ALSO,
                           same as 'none': item_totals - discount ==
                           invoice_total whenever discount_amount_minor > 0
                           (`total_mismatch_with_discount`).

    M6-M8 total-reconciliation ruling (b) -- `total_mismatch_with_discount`
    (gst_mode in {'none', 'item_level'} only):

    The bug this closes: `expense.total_minor` is ALWAYS user-declared
    (invariant per the ruling -- it is never silently re-derived from a
    matched vendor rule or a manual discount). For gst_mode='none' this
    function previously returned `ok=True` unconditionally, and for
    'item_level' it only ever compared line-level GST to tax components,
    never the discount against the declared total. So an expense whose
    total_minor was declared GROSS (sum of items, no discount subtracted)
    could pass this check, then acquire an effective discount snapshot
    (manual, vendor_rule -- app.domain.vendor_discount) and die at
    app.domain.ledger.post_expense_to_ledger's last-resort tripwire
    ("Share sum ... does not equal expense total ...") instead of a named,
    actionable validator issue.

    Scoping decision (why `discount_amount_minor > 0`, not merely "a
    discount source is present"): this invariant is gated STRICTLY on
    whether a discount is actually about to subtract real money --
    `discount_amount_minor` here is always the EFFECTIVE, threshold-
    resolved amount (see app.domain.splitting.resolve_discount_amount's
    docstring, OQ-2) -- 0 both when there is no discount snapshot/line at
    all AND when one exists but is below its threshold (inert). Gating on
    that single boolean, rather than "a discount_source column is set",
    is deliberate and was checked against every existing legal flow that
    predates this invariant:
      - Pure M1 explicit-shares/equal-split expenses (no line items, or
        line items that don't sum to total_minor by design -- shares are
        frozen directly, never fed through this check at all in practice,
        but even if they were: no discount is ever attached to that flow,
        so discount_amount_minor is always 0 there -- see
        app.api.expenses._resolve_allocation's frozen-shares guard).
      - Manual expenses with NO line_items (a single synthetic
        "whole expense" line covering total_minor exactly) and no
        discount: item_totals == total_minor already, discount_amount_minor
        is 0 -- check is skipped, byte-identical to pre-existing behaviour.
      - An expense with a discount snapshot that is below its threshold
        (inert): discount_amount_minor is 0 by resolve_discount_amount's
        own contract -- this invariant is correctly SKIPPED (nothing was
        actually deducted, so there is nothing to reconcile against; see
        `discount_recorded_but_inert` in app.domain.splitting, the existing
        mechanism for surfacing that state).
    Only once a discount will ACTUALLY subtract money (discount_amount_minor
    > 0) does the declared total owe an explanation for where that money
    went -- exactly the repro this ruling fixes.

    Never adjusts/corrects any figure -- only reports issues.
    """
    issues: list[GstIssue] = []

    if gst_mode == GstMode.none:
        if discount_amount_minor > 0:
            expected = item_totals_minor - discount_amount_minor
            if abs(expected - invoice_total_minor) > TOLERANCE_MINOR:
                issues.append(
                    GstIssue(
                        "total_mismatch_with_discount",
                        f"gst_mode='none': item totals ({item_totals_minor}) "
                        f"- discount ({discount_amount_minor}) = {expected} "
                        "(the reconciled total), but the declared expense "
                        f"total is {invoice_total_minor} -- update total to "
                        f"{expected}.",
                    )
                )
        return GstCheckResult(ok=not issues, issues=issues)

    tax_component_sum = sum(tax_component_amounts_minor)

    if gst_mode == GstMode.invoice_exclusive:
        expected = item_totals_minor - discount_amount_minor + tax_component_sum
        if abs(expected - invoice_total_minor) > TOLERANCE_MINOR:
            issues.append(
                GstIssue(
                    "gst_exclusive_mismatch",
                    f"invoice_exclusive: item totals ({item_totals_minor}) - "
                    f"discount ({discount_amount_minor}) + tax components "
                    f"({tax_component_sum}) = {expected}, but invoice total "
                    f"is {invoice_total_minor}",
                )
            )
    elif gst_mode == GstMode.invoice_inclusive:
        expected = item_totals_minor - discount_amount_minor
        if abs(expected - invoice_total_minor) > TOLERANCE_MINOR:
            issues.append(
                GstIssue(
                    "gst_inclusive_mismatch",
                    f"invoice_inclusive: item totals ({item_totals_minor}) - "
                    f"discount ({discount_amount_minor}) = {expected}, but "
                    f"invoice total is {invoice_total_minor}",
                )
            )
        for amount in tax_component_amounts_minor:
            if amount > invoice_total_minor:
                issues.append(
                    GstIssue(
                        "gst_component_exceeds_total",
                        f"invoice_inclusive: a tax component amount "
                        f"({amount}) exceeds the invoice total "
                        f"({invoice_total_minor})",
                    )
                )
        if has_tax_kind_line_items:
            issues.append(
                GstIssue(
                    "gst_inclusive_with_tax_line",
                    "invoice_inclusive: a separate kind='tax' line item is "
                    "present, but 'inclusive' means tax should already be "
                    "baked into item/invoice totals, not broken out as its "
                    "own line -- this combination indicates a likely "
                    "mis-detected gst_mode or a partially-exclusive "
                    "extraction and needs human review.",
                )
            )
    elif gst_mode == GstMode.item_level:
        if discount_amount_minor > 0:
            expected_total = item_totals_minor - discount_amount_minor
            if abs(expected_total - invoice_total_minor) > TOLERANCE_MINOR:
                issues.append(
                    GstIssue(
                        "total_mismatch_with_discount",
                        f"item_level: item totals ({item_totals_minor}) - "
                        f"discount ({discount_amount_minor}) = "
                        f"{expected_total} (the reconciled total), but the "
                        f"declared expense total is {invoice_total_minor} "
                        f"-- update total to {expected_total}.",
                    )
                )
        if has_line_gst_data and has_component_data:
            line_sum = sum(line_gst_amounts_minor)
            if abs(line_sum - tax_component_sum) > TOLERANCE_MINOR:
                issues.append(
                    GstIssue(
                        "gst_item_level_mismatch",
                        f"item_level: sum of line-item gst_amount_minor "
                        f"({line_sum}) != sum of tax component amounts "
                        f"({tax_component_sum})",
                    )
                )
        # item_level with only one side populated for the line/component
        # check: intentionally skipped, see docstring above -- unaffected
        # by the total_mismatch_with_discount check above, which is
        # independent of has_line_gst_data/has_component_data.

    return GstCheckResult(ok=not issues, issues=issues)


# ---------------------------------------------------------------------------
# M6 item 5 (OQ-1a): discount-snapshot / discount-line consistency.
#
# Deliberately a SEPARATE function, not folded into check_gst_invariants:
# these two invariants are about the discount SNAPSHOT columns on the
# `expenses` row (discount_source/discount_type/discount_value_minor/
# discount_percent) versus kind='discount' line items -- a comparison that
# only makes sense once an Expense row exists with those columns populated
# (i.e. at confirm time / correction time, via app.api.expenses), not at
# raw-ExtractedInvoice validation time before any Expense row is even
# persisted (app.extraction.validation.validate_gst has no discount_source
# to compare against yet). Callers that DO have both sides available should
# fold this function's issues into the same combined ok/needs_review
# decision as check_gst_invariants' own issues -- see
# app/api/expenses.py:confirm_expense.
# ---------------------------------------------------------------------------


def check_discount_consistency(
    *,
    discount_source: DiscountSource | None,
    discount_type: DiscountType | None,
    discount_value_minor: int | None,
    discount_percent: Decimal | None,
    base_subtotal_minor: int,
    discount_line_items_total_abs_minor: int,
    has_discount_line_items: bool,
) -> list[GstIssue]:
    """
    Two named invariants, never adjusting/guessing -- only reporting:

    1. discount_snapshot_line_mismatch: if discount_source == 'extracted'
       AND kind='discount' line items exist, the snapshot amount (computed
       from discount_type/value/percent against `base_subtotal_minor`, using
       the SAME percent_of_minor rounding rule the allocator itself uses)
       must equal the magnitude of those line items' sum, within
       TOLERANCE_MINOR. A mismatch here means the same run that populated
       the snapshot from a printed discount line (see
       app.domain.vendor_discount.apply_extracted_discount_snapshot)
       disagrees with the very lines it was supposedly summarized from --
       almost always a sign of a subsequent line-item correction the
       snapshot was never re-derived from.

    2. discount_snapshot_collision: if discount_source is 'manual' or
       'vendor_rule' AND extracted kind='discount' line items ALSO exist on
       the same expense, these are two genuinely different discounts that
       must never be silently summed or arbitrated by this function --
       flagged for human review instead.
    """
    issues: list[GstIssue] = []
    if not has_discount_line_items:
        return issues

    if discount_source == DiscountSource.extracted:
        if discount_type == DiscountType.flat:
            snapshot_amount: int | None = discount_value_minor or 0
        elif discount_type == DiscountType.percent:
            snapshot_amount = percent_of_minor(
                base_subtotal_minor, discount_percent or Decimal(0)
            )
        else:
            snapshot_amount = None

        if snapshot_amount is not None and (
            abs(snapshot_amount - discount_line_items_total_abs_minor) > TOLERANCE_MINOR
        ):
            issues.append(
                GstIssue(
                    "discount_snapshot_line_mismatch",
                    f"discount_source='extracted' snapshot amount "
                    f"({snapshot_amount}) does not match the sum of "
                    f"kind='discount' line items "
                    f"({discount_line_items_total_abs_minor})",
                )
            )
    elif discount_source in (DiscountSource.manual, DiscountSource.vendor_rule):
        issues.append(
            GstIssue(
                "discount_snapshot_collision",
                f"discount_source={discount_source.value!r} snapshot "
                "coexists with extracted kind='discount' line items on the "
                "same expense -- two different discounts, not summed or "
                "arbitrated automatically; needs human review.",
            )
        )

    return issues
