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
from fractions import Fraction
from typing import Any

from app.domain.models import AllocationMethod, DiscountScope, LineItemKind
from app.domain.rounding import allocate_largest_remainder


class SplitError(ValueError):
    """Raised when an expense cannot be split into valid shares."""


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
