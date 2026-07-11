"""
M2 splitting engine — ARCHITECTURE.md §4.

Pure computation: no I/O, no ORM dependency.  The API layer adapts ORM rows
into `LineInput` values and calls `compute_shares`.

Algorithm (mirrors the §4 pseudocode exactly):

1. Item-level lines — kind='item', kind='refund', and item-scoped discounts —
   are split among their assignees by weight using largest-remainder rounding.
   A refund/discount line with no assignments of its own inherits the
   assignment weights of its parent line (parent_line_id), so refunds flow
   back along exactly the path the money came.

2. Cart-level lines — taxes, fees, tips, cart-scoped discounts — are spread
   across participants according to the line's `allocation`:
     - 'equal':        1/n per participant with an item share
     - 'proportional': by each participant's share of the item subtotal
                       (the default when allocation is NULL)
     - 'manual':       by the line's own assignment weights
   All spreading uses largest-remainder rounding, so every line's allocations
   sum exactly to the line total.

3. Hard invariant: sum(shares) == expense total.  Violation raises SplitError
   before anything reaches the ledger.

Negative final shares (e.g. a discount larger than a user's items) are
rejected: the ledger only accepts non-negative shares, and such a split is
almost always a data-entry error.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

from app.domain.gst import is_base_gst_line
from app.domain.models import (
    AllocationMethod,
    DiscountScope,
    DiscountType,
    GstMode,
    LineItemKind,
)
from app.domain.rounding import allocate_largest_remainder, percent_of_minor


class SplitError(ValueError):
    """Raised when an expense cannot be split into valid shares."""


# ---------------------------------------------------------------------------
# M6 item 5: discount + GST allocation, layered on top of the untouched
# compute_shares() above.
# ---------------------------------------------------------------------------
#
# Indian delivery-app convention: a printed/coupon discount is applied to the
# pre-tax subtotal, and GST is computed on the ALREADY-DISCOUNTED amount (not
# on the pre-discount subtotal). This governs the discount-before-GST stage
# ordering in compute_allocation below. Re-verify against real Swiggy/
# Zomato/Amazon PDFs when samples with BOTH a discount and exclusive GST
# become available -- this is a documented assumption, not a confirmed fact
# from an actual invoice.
DISCOUNT_BEFORE_GST = True


def lines_from_orm(line_items: list[Any]) -> list[LineInput]:
    """
    Adapt ExpenseLineItem ORM rows (with assignments loaded) to LineInput.

    Assignments are sorted by user_id so the largest-remainder tie-break is
    deterministic regardless of row insertion order.
    """
    lines: list[LineInput] = []
    for li in line_items:
        assignments = tuple(
            (
                uuid.UUID(str(a.user_id)),
                Fraction(a.weight),  # Decimal → exact Fraction
            )
            for a in sorted(li.assignments, key=lambda a: str(a.user_id))
        )
        lines.append(
            LineInput(
                line_id=uuid.UUID(str(li.id)),
                kind=li.kind,
                total_minor=int(li.total_minor),
                discount_scope=li.discount_scope,
                allocation=li.allocation,
                parent_line_id=(
                    uuid.UUID(str(li.parent_line_id))
                    if li.parent_line_id is not None
                    else None
                ),
                assignments=assignments,
            )
        )
    return lines


@dataclass(frozen=True)
class LineInput:
    """Dialect-free view of one expense_line_items row."""

    line_id: uuid.UUID
    kind: LineItemKind
    total_minor: int
    discount_scope: DiscountScope | None = None
    allocation: AllocationMethod | None = None
    parent_line_id: uuid.UUID | None = None
    # (user_id, weight) pairs; weight must be > 0.
    assignments: tuple[tuple[uuid.UUID, Fraction], ...] = ()


@dataclass(frozen=True)
class SplitResult:
    # Final share per user; sums exactly to the expense total.
    shares: dict[uuid.UUID, int]
    # Per-line allocation per user (audit / share_minor freezing).
    line_allocations: dict[uuid.UUID, dict[uuid.UUID, int]] = field(
        default_factory=dict
    )


def is_item_level(line: LineInput) -> bool:
    """§4 step 1: lines split directly among assignees."""
    return (
        line.kind == LineItemKind.item
        or line.kind == LineItemKind.refund
        or line.discount_scope == DiscountScope.item
    )


def _weights_to_ratios(
    line: LineInput,
    assignments: tuple[tuple[uuid.UUID, Fraction], ...],
) -> dict[uuid.UUID, Fraction]:
    """Normalize positive weights into ratios summing to 1."""
    if not assignments:
        raise SplitError(f"Line {line.line_id} has no assignments")
    for user_id, weight in assignments:
        if weight <= 0:
            raise SplitError(
                f"Line {line.line_id}: weight {weight} for user {user_id} must be > 0"
            )
    total_weight = sum(w for _, w in assignments)
    return {user_id: w / total_weight for user_id, w in assignments}


def compute_shares(
    lines: list[LineInput],
    total_minor: int,
) -> SplitResult:
    """
    Split an expense into per-user integer shares.

    Args:
        lines:        All line items of the expense (items, fees, discounts,
                      refunds).  Item-level lines must carry assignments, or
                      (for refunds/item discounts) point at a parent line
                      that does.
        total_minor:  The expense total; must equal the sum of line totals.

    Returns:
        SplitResult whose shares sum exactly to `total_minor`.

    Raises:
        SplitError: line totals don't sum to total_minor, an item line lacks
                    assignments, a manual cart line lacks assignments, a
                    weight is non-positive, or a final share is negative.
    """
    if not lines:
        raise SplitError("Expense has no line items to split")

    lines_sum = sum(line.total_minor for line in lines)
    if lines_sum != total_minor:
        raise SplitError(
            f"Line totals sum to {lines_sum} but expense total is "
            f"{total_minor}; refusing to split"
        )

    by_id: dict[uuid.UUID, LineInput] = {line.line_id: line for line in lines}
    shares: dict[uuid.UUID, int] = {}
    line_allocations: dict[uuid.UUID, dict[uuid.UUID, int]] = {}

    # ------------------------------------------------------------------
    # Step 1 — item-level lines, split by assignee weights.
    # ------------------------------------------------------------------
    item_lines = [line for line in lines if is_item_level(line)]
    cart_lines = [line for line in lines if not is_item_level(line)]

    if not item_lines:
        raise SplitError("Expense has no item-level lines")

    for line in item_lines:
        assignments = line.assignments
        if not assignments and line.parent_line_id is not None:
            # Refund / item discount inherits the parent item's ratios.
            parent = by_id.get(line.parent_line_id)
            if parent is None:
                raise SplitError(
                    f"Line {line.line_id} references unknown parent "
                    f"{line.parent_line_id}"
                )
            assignments = parent.assignments
        ratios = _weights_to_ratios(line, assignments)
        allocation = allocate_largest_remainder(line.total_minor, ratios)
        line_allocations[line.line_id] = allocation
        for user_id, amount in allocation.items():
            shares[user_id] = shares.get(user_id, 0) + amount

    item_subtotal = sum(shares.values())

    # ------------------------------------------------------------------
    # Step 2 — cart-level lines: fees, taxes, tips, cart discounts.
    # ------------------------------------------------------------------
    if item_subtotal > 0:
        proportions: dict[uuid.UUID, Fraction] = {
            user_id: Fraction(amount, item_subtotal)
            for user_id, amount in shares.items()
        }
    else:
        # Degenerate cart (all items free or fully refunded): proportional
        # is undefined, fall back to an equal spread among participants.
        proportions = {user_id: Fraction(1, len(shares)) for user_id in shares}

    for line in cart_lines:
        if line.allocation == AllocationMethod.equal:
            targets = {user_id: Fraction(1, len(shares)) for user_id in shares}
        elif line.allocation == AllocationMethod.manual:
            targets = _weights_to_ratios(line, line.assignments)
        else:  # proportional — the default
            targets = proportions

        allocation = allocate_largest_remainder(line.total_minor, targets)
        line_allocations[line.line_id] = allocation
        for user_id, amount in allocation.items():
            shares[user_id] = shares.get(user_id, 0) + amount

    # ------------------------------------------------------------------
    # Step 3 — hard invariants.
    # ------------------------------------------------------------------
    total = sum(shares.values())
    if total != total_minor:
        raise SplitError(f"Computed shares sum to {total}, expected {total_minor}")

    negative = {u: v for u, v in shares.items() if v < 0}
    if negative:
        raise SplitError(
            "Split produces negative shares (discount/refund exceeds a "
            f"user's items): { {str(u): v for u, v in negative.items()} }"
        )

    # Belt-and-braces: mirrors the invariant asserted by the ledger.
    assert sum(shares.values()) == total_minor

    return SplitResult(shares=shares, line_allocations=line_allocations)


# ---------------------------------------------------------------------------
# M6 item 5: pure data shapes for discount + GST allocation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscountSpec:
    """
    Sourced ONLY from an Expense's frozen discount_* snapshot columns
    (discount_type/discount_value_minor/discount_percent/
    discount_threshold_minor) -- never re-derived from vendor_discount_rules
    or from kind='discount' line items. See discount_spec_from_expense.
    """

    type: DiscountType
    value_minor: int | None
    percent: Decimal | None
    threshold_minor: int


@dataclass(frozen=True)
class GstSpec:
    """
    Sourced from an Expense's gst_mode plus its expense_tax_components rows
    (component_total_minor) and, for gst_mode='item_level', each line's own
    gst_amount_minor (per_line_gst_minor). See gst_spec_from_orm.
    """

    mode: GstMode
    component_total_minor: int
    per_line_gst_minor: dict[uuid.UUID, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MemberBreakdown:
    """
    One member's final allocation for an expense.

    Single uniform formula, valid for EVERY gst_mode:
        total_minor = base_minor + discount_minor + gst_minor

    base_minor:     this member's share of the pre-discount base subtotal,
                     NET of any embedded GST. For 'none' / 'invoice_inclusive'
                     / 'invoice_exclusive', this is simply the member's share
                     of the base line total (there is no embedded GST to net
                     out for those modes -- gst_minor is 0 or a genuinely
                     separate additive amount). For 'item_level', the item
                     line's own total_minor already includes its GST, so
                     base_minor here is (that share) MINUS gst_minor, to
                     avoid double-counting it in total_minor.
    discount_minor: SIGNED NEGATIVE (or zero) -- matches the codebase-wide
                     convention that kind='discount'/kind='refund' line
                     totals are negative.
    gst_minor:      non-negative. For 'invoice_exclusive', genuinely
                     additive GST computed on the post-discount base. For
                     'item_level', the embedded GST portion of this
                     member's item lines (netted out of base_minor above,
                     so it's not double-counted -- see base_minor). For
                     'none'/'invoice_inclusive', always 0.
    total_minor:    the member's actual final amount owed for this expense.
    """

    base_minor: int
    discount_minor: int
    gst_minor: int
    total_minor: int


@dataclass(frozen=True)
class AllocationResult:
    members: dict[uuid.UUID, MemberBreakdown]
    # Pre-discount, pre-GST base subtotal (items + fees + tip + refunds).
    subtotal_minor: int
    # Non-negative magnitude of the discount actually applied (0 if no
    # discount, or if discount_recorded_but_inert is True).
    applied_discount_minor: int
    # gst.component_total_minor when gst_mode == 'invoice_exclusive' and
    # actually added on top of members' totals; 0 for every other mode.
    exclusive_gst_minor: int
    # True when a discount snapshot exists but the (fresh-computed) base
    # subtotal is below its threshold, so it contributed 0 to allocation.
    discount_recorded_but_inert: bool
    # The untouched compute_shares() SplitResult over the base line set --
    # callers use base_result.line_allocations to freeze
    # item_assignments.share_minor exactly as before this feature existed.
    base_result: SplitResult


def _shares_to_ratios(shares: dict[uuid.UUID, int]) -> dict[uuid.UUID, Fraction]:
    """
    Build a ratio dict (summing to 1) proportional to `shares`, falling back
    to an equal split if every share is zero (mirrors compute_shares' own
    zero-item-subtotal fallback for cart-level lines).

    Pre-sorted by str(user_id): allocate_largest_remainder itself has NO
    internal tie-break key (see its docstring) -- every existing caller in
    this codebase (lines_from_orm, compute_shares) relies entirely on the
    CALLER pre-sorting by str(user_id) for deterministic largest-remainder
    tie-breaks. This helper preserves that exact discipline for
    compute_allocation's own discount/GST distributions.
    """
    ordered_ids = sorted(shares.keys(), key=str)
    total = sum(shares.values())
    if total == 0:
        n = len(ordered_ids)
        return {uid: Fraction(1, n) for uid in ordered_ids}
    return {uid: Fraction(shares[uid], total) for uid in ordered_ids}


def resolve_discount_amount(
    discount: DiscountSpec | None, base_subtotal_minor: int
) -> tuple[int, bool]:
    """
    Compute the (applied_discount_minor, discount_recorded_but_inert) pair
    for a DiscountSpec against a fresh-computed base subtotal -- the SAME
    threshold/type/cap logic compute_allocation itself uses for its
    discount stage, extracted here so any OTHER caller that needs to know
    "how much discount will actually apply" (without running a full
    allocation) uses the exact same rule.

    THIS IS LOAD-BEARING for app.api.expenses.confirm_expense's recomputed
    GST invariant check: once a discount can be sourced purely from the
    expense.discount_* SNAPSHOT (no kind='discount' line items at all --
    e.g. a manually-entered or vendor-rule discount), the GST invariant's
    `discount_amount_minor` input must reflect what will ACTUALLY be
    deducted by compute_allocation, not just the (possibly zero) sum of
    kind='discount' line items -- otherwise the validator and the allocator
    would disagree about the invoice's own arithmetic, exactly the failure
    mode the governing shared-base-line-set principle exists to prevent.

    Returns (0, False) for discount=None.
    """
    if discount is None:
        return 0, False
    if base_subtotal_minor < discount.threshold_minor:
        return 0, True
    if discount.type == DiscountType.flat:
        applied = min(discount.value_minor or 0, base_subtotal_minor)
    else:  # percent
        applied = min(
            percent_of_minor(base_subtotal_minor, discount.percent or Decimal(0)),
            base_subtotal_minor,
        )
    return applied, False


def compute_allocation(
    lines: list[LineInput],
    total_minor: int,
    *,
    discount: DiscountSpec | None = None,
    gst: GstSpec | None = None,
) -> AllocationResult:
    """
    Layer discount + GST allocation on top of compute_shares().

    compute_shares() itself is NEVER modified or duplicated -- this function
    calls it (unchanged) for all per-line/per-item weight math, and only
    adds the discount/GST math on top.

    Short-circuit (byte-identical no-op): when there is no discount AND no
    GST effect (gst is None, or gst.mode is 'none'/'invoice_inclusive' --
    neither of which allocates anything additional), this produces EXACTLY
    what plain compute_shares(lines, total_minor) would over the full,
    unmodified line set -- i.e. every pre-existing caller/test of
    compute_shares continues to see identical behaviour whether or not it
    goes through this function.

    When a discount snapshot OR an additive/breakdown-bearing GST mode
    (invoice_exclusive, item_level) is present, the "base" line set is
    narrowed to app.domain.gst.is_base_gst_line's definition (items + fees +
    tip + refunds, excluding kind='discount' and kind='tax' lines) -- the
    EXACT SAME shared helper app.domain.gst.check_gst_invariants' callers
    use to build item_totals_minor, so the validator and this allocator can
    never disagree about what "the base" is. Any old-style kind='discount'/
    kind='tax' line items on the expense are NOT double-counted: the
    DiscountSpec/GstSpec snapshots are the sole source of truth for
    discount/GST money once either is present (see
    app.domain.gst.check_discount_consistency for the separate consistency
    check between the snapshot and any such lines).
    """
    gst_mode = gst.mode if gst is not None else GstMode.none
    gst_is_additive_or_itemized = gst is not None and gst_mode in (
        GstMode.invoice_exclusive,
        GstMode.item_level,
    )

    if discount is None and not gst_is_additive_or_itemized:
        base_result = compute_shares(lines, total_minor)
        noop_members: dict[uuid.UUID, MemberBreakdown] = {
            uid: MemberBreakdown(
                base_minor=amount,
                discount_minor=0,
                gst_minor=0,
                total_minor=amount,
            )
            for uid, amount in base_result.shares.items()
        }
        return AllocationResult(
            members=noop_members,
            subtotal_minor=total_minor,
            applied_discount_minor=0,
            exclusive_gst_minor=0,
            discount_recorded_but_inert=False,
            base_result=base_result,
        )

    # ------------------------------------------------------------------
    # Narrow to the shared base line set and run the untouched splitting
    # engine over it, exactly as compute_shares would over any other line
    # set -- all weight/allocation-method logic is reused unmodified.
    # ------------------------------------------------------------------
    base_lines = [line for line in lines if is_base_gst_line(line, gst_mode)]
    if not base_lines:
        raise SplitError(
            "Expense has no base (item/fee/tip/refund) lines to allocate "
            "discount/GST against"
        )
    subtotal_minor = sum(line.total_minor for line in base_lines)
    base_result = compute_shares(base_lines, subtotal_minor)
    base_shares = base_result.shares

    # ------------------------------------------------------------------
    # Discount stage (DISCOUNT_BEFORE_GST).
    # ------------------------------------------------------------------
    applied_discount, discount_recorded_but_inert = resolve_discount_amount(
        discount, subtotal_minor
    )
    discount_alloc: dict[uuid.UUID, int] = dict.fromkeys(base_shares, 0)

    if applied_discount > 0:
        ratios = _shares_to_ratios(base_shares)
        discount_alloc = allocate_largest_remainder(-applied_discount, ratios)
        if sum(discount_alloc.values()) != -applied_discount:
            raise SplitError(
                f"Discount allocation failed to reconcile to -{applied_discount}"
            )
        for uid, base_amount in base_shares.items():
            if base_amount + discount_alloc.get(uid, 0) < 0:
                raise SplitError(
                    f"Discount allocation makes user {uid}'s running total negative"
                )

    post_discount = {
        uid: base_shares[uid] + discount_alloc.get(uid, 0) for uid in base_shares
    }

    # ------------------------------------------------------------------
    # GST stage.
    # ------------------------------------------------------------------
    gst_alloc: dict[uuid.UUID, int] = dict.fromkeys(base_shares, 0)
    exclusive_gst_minor = 0

    if gst is not None and gst_mode == GstMode.invoice_exclusive:
        exclusive_gst_minor = gst.component_total_minor
        if exclusive_gst_minor > 0:
            post_subtotal = sum(post_discount.values())
            if post_subtotal == 0:
                # Edge case: the discount consumed the ENTIRE base subtotal.
                # There is no meaningful post-discount ratio to distribute
                # GST by, so fall back to the PRE-discount base shares (the
                # only remaining signal for "who ordered what"). Documented
                # edge case, not a guess -- see AllocationResult tests.
                ratio_source = base_shares
            else:
                ratio_source = post_discount
            ratios = _shares_to_ratios(ratio_source)
            gst_alloc = allocate_largest_remainder(exclusive_gst_minor, ratios)
            if sum(gst_alloc.values()) != exclusive_gst_minor:
                raise SplitError(
                    "Exclusive GST allocation failed to reconcile to "
                    f"{exclusive_gst_minor}"
                )
    elif gst is not None and gst_mode == GstMode.item_level:
        by_id = {line.line_id: line for line in base_lines}
        for line_id, amount in gst.per_line_gst_minor.items():
            if amount <= 0:
                continue
            line = by_id.get(line_id)
            if line is None:
                # The GST-bearing line isn't part of the base set (e.g. it
                # was a kind='tax'/kind='discount' line, which shouldn't
                # carry gst_amount_minor in practice) -- nothing to
                # distribute against.
                continue
            assignments = line.assignments
            if not assignments and line.parent_line_id is not None:
                parent = by_id.get(line.parent_line_id)
                if parent is not None:
                    assignments = parent.assignments
            # Reuses the EXISTING weight-resolution mechanism: a GST-bearing
            # line with no resolvable assignments raises the same SplitError
            # compute_shares itself would have already raised while
            # computing base_result above (every kind='item' line must have
            # assignments or a parent with assignments) -- by the time we
            # reach this stage, base_result already succeeded, so this call
            # cannot newly fail for that reason; kept for defense in depth
            # if a future GST-bearing kind is added that compute_shares does
            # not already validate.
            ratios = _weights_to_ratios(line, assignments)
            per_line_alloc = allocate_largest_remainder(amount, ratios)
            for uid, amt in per_line_alloc.items():
                gst_alloc[uid] = gst_alloc.get(uid, 0) + amt

    # ------------------------------------------------------------------
    # Assemble members + final invariants.
    # ------------------------------------------------------------------
    members: dict[uuid.UUID, MemberBreakdown] = {}
    for uid, base_amount in base_shares.items():
        disc_amount = discount_alloc.get(uid, 0)
        gst_amount = gst_alloc.get(uid, 0)
        # Single uniform formula for every mode: total = base + discount +
        # gst. For gst_mode='item_level', gst_amount is ALREADY embedded
        # inside base_shares[uid] (the item line's own total_minor includes
        # its tax), so `base_minor` stored here is the amount NET of that
        # embedded GST -- base_amount - gst_amount -- so the formula still
        # reconciles without double-counting. For every other mode,
        # gst_amount is either 0 (none/invoice_inclusive) or a genuinely
        # additive amount computed on top (invoice_exclusive), so
        # base_minor is simply base_amount unchanged.
        stored_base = (
            base_amount - gst_amount if gst_mode == GstMode.item_level else base_amount
        )
        total_amount = stored_base + disc_amount + gst_amount
        members[uid] = MemberBreakdown(
            base_minor=stored_base,
            discount_minor=disc_amount,
            gst_minor=gst_amount,
            total_minor=total_amount,
        )

    grand_total = sum(m.total_minor for m in members.values())
    expected_total = subtotal_minor - applied_discount + exclusive_gst_minor
    if grand_total != expected_total:
        raise SplitError(
            f"Allocation reconciliation failed: members sum to {grand_total}, "
            f"expected {expected_total}"
        )

    negative = {u: m.total_minor for u, m in members.items() if m.total_minor < 0}
    if negative:
        raise SplitError(f"Allocation produced negative member totals: {negative}")

    return AllocationResult(
        members=members,
        subtotal_minor=subtotal_minor,
        applied_discount_minor=applied_discount,
        exclusive_gst_minor=exclusive_gst_minor,
        discount_recorded_but_inert=discount_recorded_but_inert,
        base_result=base_result,
    )


# ---------------------------------------------------------------------------
# M6 item 5: pure adapters — ORM rows in, frozen dataclasses out. No
# session, no queries; callers must load everything first.
# ---------------------------------------------------------------------------


def discount_spec_from_expense(expense: Any) -> DiscountSpec | None:
    """None if the expense has no discount snapshot at all."""
    if expense.discount_type is None:
        return None
    return DiscountSpec(
        type=expense.discount_type,
        value_minor=(
            int(expense.discount_value_minor)
            if expense.discount_value_minor is not None
            else None
        ),
        percent=(
            Decimal(str(expense.discount_percent))
            if expense.discount_percent is not None
            else None
        ),
        threshold_minor=(
            int(expense.discount_threshold_minor)
            if expense.discount_threshold_minor is not None
            else 0
        ),
    )


def gst_spec_from_orm(
    expense: Any,
    line_items: list[Any],
    tax_components: list[Any],
) -> GstSpec:
    """Always returns a GstSpec (mode='none' is a valid, common case)."""
    component_total = sum(int(tc.amount_minor) for tc in tax_components)
    per_line = {
        uuid.UUID(str(li.id)): int(li.gst_amount_minor)
        for li in line_items
        if li.gst_amount_minor is not None
    }
    return GstSpec(
        mode=expense.gst_mode,
        component_total_minor=component_total,
        per_line_gst_minor=per_line,
    )
