"""
M2 splitting engine tests — ARCHITECTURE.md §4.

Covers:
  - the §4 worked example, digit for digit
  - equal / proportional / manual cart-line allocation
  - item-scoped discounts and refunds inheriting parent ratios
  - BOGO (free line at zero cost)
  - weighted assignments
  - error paths (mismatched totals, unassigned items, negative shares,
    bad weights, unknown parents)
  - property-style tests: random carts must ALWAYS reconcile
    (Invariant 1: sum(shares) == total_minor)
"""

from __future__ import annotations

import uuid
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.models import AllocationMethod, DiscountScope, LineItemKind
from app.domain.splitting import (
    LineInput,
    SplitError,
    compute_shares,
)

# Stable user IDs for readability.
A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
C = uuid.UUID("00000000-0000-0000-0000-00000000000c")


def _lid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def item(
    n: int,
    total: int,
    assignees: dict[uuid.UUID, int | Fraction],
    **kw,
) -> LineInput:
    return LineInput(
        line_id=_lid(n),
        kind=LineItemKind.item,
        total_minor=total,
        assignments=tuple((u, Fraction(w)) for u, w in assignees.items()),
        **kw,
    )


def cart_line(
    n: int,
    kind: LineItemKind,
    total: int,
    allocation: AllocationMethod | None = None,
    assignees: dict[uuid.UUID, int | Fraction] | None = None,
) -> LineInput:
    return LineInput(
        line_id=_lid(n),
        kind=kind,
        total_minor=total,
        allocation=allocation,
        discount_scope=(DiscountScope.cart if kind == LineItemKind.discount else None),
        assignments=tuple((u, Fraction(w)) for u, w in (assignees or {}).items()),
    )


# ---------------------------------------------------------------------------
# The §4 worked example
# ---------------------------------------------------------------------------


def test_architecture_worked_example_proportional() -> None:
    """A=₹20 items, B=₹40, cart discount −₹10, delivery +₹3 → 17.67 / 35.33."""
    lines = [
        item(1, 2000, {A: 1}),
        item(2, 4000, {B: 1}),
        cart_line(3, LineItemKind.discount, -1000),  # proportional default
        cart_line(4, LineItemKind.delivery_fee, 300),
    ]
    result = compute_shares(lines, 5300)
    assert result.shares == {A: 1767, B: 3533}
    assert sum(result.shares.values()) == 5300
    # Step-by-step allocations match §4 exactly.
    assert result.line_allocations[_lid(3)] == {A: -333, B: -667}
    assert result.line_allocations[_lid(4)] == {A: 100, B: 200}


def test_architecture_worked_example_equal_fee() -> None:
    """§4 step 4 variant: delivery fee spread equally → 150 each."""
    lines = [
        item(1, 2000, {A: 1}),
        item(2, 4000, {B: 1}),
        cart_line(3, LineItemKind.discount, -1000),
        cart_line(4, LineItemKind.delivery_fee, 300, AllocationMethod.equal),
    ]
    result = compute_shares(lines, 5300)
    assert result.line_allocations[_lid(4)] == {A: 150, B: 150}
    assert result.shares == {A: 1817, B: 3483}
    assert sum(result.shares.values()) == 5300


# ---------------------------------------------------------------------------
# Allocation methods
# ---------------------------------------------------------------------------


def test_manual_cart_allocation_uses_line_assignments() -> None:
    lines = [
        item(1, 1000, {A: 1}),
        item(2, 1000, {B: 1}),
        cart_line(3, LineItemKind.tip, 300, AllocationMethod.manual, {A: 2, B: 1}),
    ]
    result = compute_shares(lines, 2300)
    assert result.line_allocations[_lid(3)] == {A: 200, B: 100}
    assert result.shares == {A: 1200, B: 1100}


def test_manual_cart_line_can_target_non_item_user() -> None:
    """Manual targets may include users with no item shares (e.g. tip payer)."""
    lines = [
        item(1, 1000, {A: 1}),
        cart_line(2, LineItemKind.tip, 100, AllocationMethod.manual, {C: 1}),
    ]
    result = compute_shares(lines, 1100)
    assert result.shares == {A: 1000, C: 100}


def test_equal_allocation_covers_all_item_participants() -> None:
    lines = [
        item(1, 900, {A: 1, B: 1, C: 1}),
        cart_line(2, LineItemKind.platform_fee, 100, AllocationMethod.equal),
    ]
    result = compute_shares(lines, 1000)
    # 100/3 → 33/33/34 by largest remainder; equal remainders tie-break
    # deterministically, and the sum always reconciles.
    assert sum(result.line_allocations[_lid(2)].values()) == 100
    assert sum(result.shares.values()) == 1000


def test_proportional_is_default_for_tax() -> None:
    lines = [
        item(1, 3000, {A: 1}),
        item(2, 1000, {B: 1}),
        cart_line(3, LineItemKind.tax, 200),
    ]
    result = compute_shares(lines, 4200)
    assert result.line_allocations[_lid(3)] == {A: 150, B: 50}


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------


def test_weighted_item_split() -> None:
    """Alice weight 3, Bob weight 1 → 75/25 of the line."""
    lines = [item(1, 1000, {A: 3, B: 1})]
    result = compute_shares(lines, 1000)
    assert result.shares == {A: 750, B: 250}


def test_fractional_weights() -> None:
    lines = [item(1, 999, {A: Fraction(1, 2), B: Fraction(1, 4), C: Fraction(1, 4)})]
    result = compute_shares(lines, 999)
    assert sum(result.shares.values()) == 999
    # A gets half (499.5 → rounding), B and C a quarter each.
    assert result.shares[A] in (499, 500)


# ---------------------------------------------------------------------------
# Discounts and refunds
# ---------------------------------------------------------------------------


def test_item_scoped_discount_inherits_parent_ratios() -> None:
    """Item discount with no own assignments follows the parent item."""
    lines = [
        item(1, 2000, {A: 1, B: 1}),
        LineInput(
            line_id=_lid(2),
            kind=LineItemKind.discount,
            total_minor=-500,
            discount_scope=DiscountScope.item,
            parent_line_id=_lid(1),
        ),
    ]
    result = compute_shares(lines, 1500)
    assert result.shares == {A: 750, B: 750}
    assert result.line_allocations[_lid(2)] == {A: -250, B: -250}


def test_item_scoped_discount_with_own_assignments() -> None:
    """An item discount CAN carry its own assignments (targeted coupon)."""
    lines = [
        item(1, 2000, {A: 1, B: 1}),
        LineInput(
            line_id=_lid(2),
            kind=LineItemKind.discount,
            total_minor=-400,
            discount_scope=DiscountScope.item,
            assignments=((A, Fraction(1)),),
        ),
    ]
    result = compute_shares(lines, 1600)
    assert result.shares == {A: 600, B: 1000}


def test_refund_inherits_parent_ratios() -> None:
    """Refund flows back along exactly the path the money came (2:1 split)."""
    lines = [
        item(1, 3000, {A: 2, B: 1}),
        LineInput(
            line_id=_lid(2),
            kind=LineItemKind.refund,
            total_minor=-900,
            parent_line_id=_lid(1),
        ),
    ]
    result = compute_shares(lines, 2100)
    assert result.line_allocations[_lid(2)] == {A: -600, B: -300}
    assert result.shares == {A: 1400, B: 700}


def test_cart_discount_never_loses_a_paisa() -> None:
    """−1000 across ⅓/⅔ → −333/−667 (§4 rounding), never −333/−666."""
    lines = [
        item(1, 2000, {A: 1}),
        item(2, 4000, {B: 1}),
        cart_line(3, LineItemKind.discount, -1000),
    ]
    result = compute_shares(lines, 5000)
    assert result.line_allocations[_lid(3)] == {A: -333, B: -667}


# ---------------------------------------------------------------------------
# BOGO / bundles
# ---------------------------------------------------------------------------


def test_bogo_free_item_costs_zero() -> None:
    """Paid pizza → Alice; free pizza → Bob at zero cost (default policy)."""
    bundle = uuid.uuid4()
    lines = [
        LineInput(
            line_id=_lid(1),
            kind=LineItemKind.item,
            total_minor=29900,
            assignments=((A, Fraction(1)),),
        ),
        LineInput(
            line_id=_lid(2),
            kind=LineItemKind.item,
            total_minor=0,
            assignments=((B, Fraction(1)),),
        ),
    ]
    assert lines[0].line_id != bundle  # bundle id is orthogonal to splitting
    result = compute_shares(lines, 29900)
    assert result.shares == {A: 29900, B: 0}


def test_zero_subtotal_falls_back_to_equal_for_fees() -> None:
    """Everything free but a delivery fee exists → spread equally."""
    lines = [
        item(1, 0, {A: 1}),
        item(2, 0, {B: 1}),
        cart_line(3, LineItemKind.delivery_fee, 99),
    ]
    result = compute_shares(lines, 99)
    assert sum(result.shares.values()) == 99
    assert abs(result.shares[A] - result.shares[B]) <= 1


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_line_totals_must_match_expense_total() -> None:
    with pytest.raises(SplitError, match="refusing to split"):
        compute_shares([item(1, 1000, {A: 1})], 1100)


def test_unassigned_item_line_rejected() -> None:
    with pytest.raises(SplitError, match="no assignments"):
        compute_shares(
            [LineInput(line_id=_lid(1), kind=LineItemKind.item, total_minor=100)],
            100,
        )


def test_manual_cart_line_without_assignments_rejected() -> None:
    lines = [
        item(1, 100, {A: 1}),
        cart_line(2, LineItemKind.tip, 50, AllocationMethod.manual),
    ]
    with pytest.raises(SplitError, match="no assignments"):
        compute_shares(lines, 150)


def test_zero_weight_rejected() -> None:
    with pytest.raises(SplitError, match="must be > 0"):
        compute_shares([item(1, 100, {A: 0, B: 1})], 100)


def test_negative_final_share_rejected() -> None:
    """Discount bigger than a user's items → negative share → SplitError."""
    lines = [
        item(1, 100, {A: 1}),
        item(2, 5000, {B: 1}),
        LineInput(
            line_id=_lid(3),
            kind=LineItemKind.discount,
            total_minor=-500,
            discount_scope=DiscountScope.item,
            assignments=((A, Fraction(1)),),
        ),
    ]
    with pytest.raises(SplitError, match="negative"):
        compute_shares(lines, 4600)


def test_unknown_parent_rejected() -> None:
    lines = [
        item(1, 1000, {A: 1}),
        LineInput(
            line_id=_lid(2),
            kind=LineItemKind.refund,
            total_minor=-100,
            parent_line_id=_lid(99),
        ),
    ]
    with pytest.raises(SplitError, match="unknown parent"):
        compute_shares(lines, 900)


def test_no_lines_rejected() -> None:
    with pytest.raises(SplitError, match="no line items"):
        compute_shares([], 0)


def test_fee_only_expense_rejected() -> None:
    with pytest.raises(SplitError, match="no item-level lines"):
        compute_shares([cart_line(1, LineItemKind.tip, 100)], 100)


# ---------------------------------------------------------------------------
# Property tests — random carts must ALWAYS reconcile (Invariant 1)
# ---------------------------------------------------------------------------

_user_pool = [uuid.UUID(int=1000 + i) for i in range(6)]

_assignee_sets = st.lists(
    st.sampled_from(range(len(_user_pool))), min_size=1, max_size=6, unique=True
)


@st.composite
def random_cart(draw):
    """A random cart: 1–8 items, optional fees/discount, consistent total."""
    n_items = draw(st.integers(min_value=1, max_value=8))
    lines: list[LineInput] = []
    lid = 0
    for _ in range(n_items):
        lid += 1
        assignees = draw(_assignee_sets)
        weights = {
            _user_pool[i]: Fraction(draw(st.integers(min_value=1, max_value=9)))
            for i in assignees
        }
        total = draw(st.integers(min_value=0, max_value=100_000))
        lines.append(
            LineInput(
                line_id=uuid.UUID(int=lid),
                kind=LineItemKind.item,
                total_minor=total,
                assignments=tuple(weights.items()),
            )
        )

    subtotal = sum(line.total_minor for line in lines)

    # Optional fee rows.
    for kind in (
        LineItemKind.delivery_fee,
        LineItemKind.platform_fee,
        LineItemKind.tax,
        LineItemKind.tip,
    ):
        if draw(st.booleans()):
            lid += 1
            allocation = draw(
                st.sampled_from(
                    [None, AllocationMethod.equal, AllocationMethod.proportional]
                )
            )
            lines.append(
                LineInput(
                    line_id=uuid.UUID(int=lid),
                    kind=kind,
                    total_minor=draw(st.integers(min_value=0, max_value=20_000)),
                    allocation=allocation,
                )
            )

    # Optional cart discount, capped well below subtotal to avoid
    # legitimately-negative shares (tested separately).
    if subtotal > 0 and draw(st.booleans()):
        lid += 1
        lines.append(
            LineInput(
                line_id=uuid.UUID(int=lid),
                kind=LineItemKind.discount,
                total_minor=-draw(st.integers(min_value=1, max_value=subtotal // 4))
                if subtotal >= 4
                else -1,
                discount_scope=DiscountScope.cart,
            )
        )

    total_minor = sum(line.total_minor for line in lines)
    return lines, total_minor


@settings(max_examples=300, deadline=None)
@given(random_cart())
def test_property_random_carts_always_reconcile(cart) -> None:
    """Invariant 1: sum(shares) == total_minor for ANY random cart."""
    lines, total_minor = cart
    try:
        result = compute_shares(lines, total_minor)
    except SplitError as exc:
        # The only acceptable failure is a legitimately negative share
        # (proportional discount can undercut a tiny item share).
        assert "negative" in str(exc).lower()
        return
    assert sum(result.shares.values()) == total_minor
    assert all(v >= 0 for v in result.shares.values())
    # Per-line allocations each reconcile to their line totals.
    for line in lines:
        if line.line_id in result.line_allocations:
            assert (
                sum(result.line_allocations[line.line_id].values()) == line.total_minor
            )


@settings(max_examples=200, deadline=None)
@given(
    amount=st.integers(min_value=-1_000_000, max_value=1_000_000),
    n_users=st.integers(min_value=1, max_value=10),
    data=st.data(),
)
def test_property_single_line_any_weights_reconciles(
    amount: int, n_users: int, data
) -> None:
    """One line, arbitrary weights, positive or negative amount."""
    weights = {
        uuid.UUID(int=i): Fraction(data.draw(st.integers(min_value=1, max_value=1000)))
        for i in range(n_users)
    }
    line = LineInput(
        line_id=uuid.UUID(int=999),
        kind=LineItemKind.item,
        total_minor=amount,
        assignments=tuple(weights.items()),
    )
    try:
        result = compute_shares([line], amount)
    except SplitError as exc:
        assert amount < 0 and "negative" in str(exc).lower()
        return
    assert sum(result.shares.values()) == amount
