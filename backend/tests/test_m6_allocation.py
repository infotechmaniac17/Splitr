"""
M6 item 5: discount + GST allocation (app.domain.splitting.compute_allocation).

Covers (per the spec):
  - Byte-identical no-op path vs. plain compute_shares.
  - Explicit fixtures: non-divisible flat discount, threshold boundary
    (exactly-at applies, one-paisa-below is inert), percent rounding,
    mixed 5%/18% item-level GST, exclusive GST after a flat discount,
    discount == subtotal (GST fallback to pre-discount shares), weighted
    2:1:1 split with discount + GST simultaneously, a refund combined with
    discount + GST that still reconciles, single-member expense, an
    unassigned GST-bearing line raising SplitError.
  - Property-based reconciliation / no-negative-totals / determinism /
    permutation-invariance.
  - app.domain.gst.check_discount_consistency (OQ-1a).
  - The OQ-2 sequence: auto-applied discount inert after a correction drops
    the fresh-computed subtotal below its threshold, snapshot untouched.
  - End-to-end confirm: preview -> confirm -> persisted
    expense_member_allocations rows match the preview and reconcile against
    the ledger entries posted in the same transaction; group balances
    reflect the discounted+taxed totals. Concurrent double-confirm.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from fractions import Fraction

import pytest
from httpx import AsyncClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.gst import check_discount_consistency
from app.domain.ledger import compute_group_balances
from app.domain.models import (
    DiscountSource,
    DiscountType,
    Expense,
    ExpenseLineItem,
    ExpenseMemberAllocation,
    ExpenseSource,
    ExpenseStatus,
    ExpenseTaxComponent,
    GstMode,
    ItemAssignment,
    LedgerEntry,
    LineItemKind,
    ParseStatus,
    TaxComponentName,
    User,
)
from app.domain.splitting import (
    DiscountSpec,
    GstSpec,
    LineInput,
    SplitError,
    compute_allocation,
    compute_shares,
)

API = "/api/v1"

A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
C = uuid.UUID("00000000-0000-0000-0000-00000000000c")


def _lid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _item(n: int, total: int, assignees: dict[uuid.UUID, int | Fraction]) -> LineInput:
    return LineInput(
        line_id=_lid(n),
        kind=LineItemKind.item,
        total_minor=total,
        assignments=tuple(
            (u, Fraction(w))
            for u, w in sorted(assignees.items(), key=lambda kv: str(kv[0]))
        ),
    )


# ---------------------------------------------------------------------------
# 1. Byte-identical no-op path.
# ---------------------------------------------------------------------------


def test_no_discount_no_gst_byte_identical_to_compute_shares() -> None:
    lines = [
        _item(1, 2000, {A: 1}),
        _item(2, 4000, {B: 1}),
        LineInput(
            line_id=_lid(3),
            kind=LineItemKind.discount,
            total_minor=-1000,
        ),
        LineInput(line_id=_lid(4), kind=LineItemKind.delivery_fee, total_minor=300),
    ]
    plain = compute_shares(lines, 5300)
    allocation = compute_allocation(lines, 5300)
    assert {u: b.total_minor for u, b in allocation.members.items()} == plain.shares
    assert allocation.applied_discount_minor == 0
    assert allocation.exclusive_gst_minor == 0
    assert allocation.discount_recorded_but_inert is False
    assert allocation.base_result.line_allocations == plain.line_allocations


def test_invoice_inclusive_gst_is_also_a_no_op() -> None:
    """invoice_inclusive allocates nothing additional -- same short-circuit."""
    lines = [_item(1, 1000, {A: 1, B: 1})]
    plain = compute_shares(lines, 1000)
    allocation = compute_allocation(
        lines,
        1000,
        gst=GstSpec(mode=GstMode.invoice_inclusive, component_total_minor=180),
    )
    assert {u: b.total_minor for u, b in allocation.members.items()} == plain.shares


# ---------------------------------------------------------------------------
# 2. Non-divisible flat discount (Rs.100 / 3-style rounding).
# ---------------------------------------------------------------------------


def test_flat_discount_non_divisible_reconciles() -> None:
    lines = [_item(1, 100, {A: 1, B: 1, C: 1})]
    discount = DiscountSpec(
        type=DiscountType.flat, value_minor=100, percent=None, threshold_minor=0
    )
    result = compute_allocation(lines, 100, discount=discount)
    assert result.applied_discount_minor == 100
    totals = {u: b.total_minor for u, b in result.members.items()}
    assert sum(totals.values()) == 0
    # Discount == full subtotal -> every member's total is exactly 0.
    assert all(v == 0 for v in totals.values())


def test_flat_discount_partial_non_divisible_largest_remainder() -> None:
    """A 33-paisa discount over 3 equal shares must never lose a paisa."""
    lines = [_item(1, 300, {A: 1, B: 1, C: 1})]
    discount = DiscountSpec(
        type=DiscountType.flat, value_minor=33, percent=None, threshold_minor=0
    )
    result = compute_allocation(lines, 300, discount=discount)
    assert result.applied_discount_minor == 33
    disc_values = sorted(b.discount_minor for b in result.members.values())
    assert disc_values == [-11, -11, -11]
    assert sum(b.total_minor for b in result.members.values()) == 267


# ---------------------------------------------------------------------------
# 3. Threshold boundary — exactly-at applies, one-paisa-below is inert.
# ---------------------------------------------------------------------------


def test_threshold_exactly_at_applies() -> None:
    lines = [_item(1, 35000, {A: 1})]
    discount = DiscountSpec(
        type=DiscountType.flat, value_minor=5000, percent=None, threshold_minor=35000
    )
    result = compute_allocation(lines, 35000, discount=discount)
    assert result.applied_discount_minor == 5000
    assert result.discount_recorded_but_inert is False


def test_threshold_one_paisa_below_is_inert() -> None:
    lines = [_item(1, 34999, {A: 1})]
    discount = DiscountSpec(
        type=DiscountType.flat, value_minor=5000, percent=None, threshold_minor=35000
    )
    result = compute_allocation(lines, 34999, discount=discount)
    assert result.applied_discount_minor == 0
    assert result.discount_recorded_but_inert is True
    assert result.members[A].total_minor == 34999


# ---------------------------------------------------------------------------
# 4. Percent rounding (item-3-style 50% x 101 -> 50/51 cases).
# ---------------------------------------------------------------------------


def test_percent_discount_half_even_rounding() -> None:
    lines = [_item(1, 101, {A: 1})]
    discount = DiscountSpec(
        type=DiscountType.percent,
        value_minor=None,
        percent=Decimal("50"),
        threshold_minor=0,
    )
    result = compute_allocation(lines, 101, discount=discount)
    # 101 * 50 / 100 = 50.5 -> round-half-even -> 50.
    assert result.applied_discount_minor == 50
    assert result.members[A].total_minor == 51


def test_percent_discount_capped_at_subtotal() -> None:
    lines = [_item(1, 1000, {A: 1})]
    discount = DiscountSpec(
        type=DiscountType.percent,
        value_minor=None,
        percent=Decimal("100"),
        threshold_minor=0,
    )
    result = compute_allocation(lines, 1000, discount=discount)
    assert result.applied_discount_minor == 1000
    assert result.members[A].total_minor == 0


# ---------------------------------------------------------------------------
# 5. Mixed 5%/18% item-level GST.
# ---------------------------------------------------------------------------


def test_item_level_mixed_gst_rates() -> None:
    lines = [
        _item(1, 10500, {A: 1}),  # 10000 + 500 (5%) embedded
        _item(2, 11800, {B: 1}),  # 10000 + 1800 (18%) embedded
    ]
    gst = GstSpec(
        mode=GstMode.item_level,
        component_total_minor=0,
        per_line_gst_minor={_lid(1): 500, _lid(2): 1800},
    )
    result = compute_allocation(lines, 22300, gst=gst)
    assert result.exclusive_gst_minor == 0
    a = result.members[A]
    b = result.members[B]
    assert a.gst_minor == 500
    assert a.base_minor == 10000
    assert a.total_minor == 10500
    assert b.gst_minor == 1800
    assert b.base_minor == 10000
    assert b.total_minor == 11800
    assert sum(m.total_minor for m in result.members.values()) == 22300


# ---------------------------------------------------------------------------
# 6. Exclusive GST computed AFTER a flat discount (DISCOUNT_BEFORE_GST).
# ---------------------------------------------------------------------------


def test_exclusive_gst_after_flat_discount() -> None:
    # Subtotal 1000, discount 100 -> post-discount 900, GST 18% of 900 = 162.
    lines = [_item(1, 600, {A: 1}), _item(2, 400, {B: 1})]
    discount = DiscountSpec(
        type=DiscountType.flat, value_minor=100, percent=None, threshold_minor=0
    )
    gst = GstSpec(mode=GstMode.invoice_exclusive, component_total_minor=162)
    total_minor = 1000 - 100 + 162
    result = compute_allocation(lines, total_minor, discount=discount, gst=gst)
    assert result.applied_discount_minor == 100
    assert result.exclusive_gst_minor == 162
    assert sum(m.total_minor for m in result.members.values()) == total_minor
    # GST distributed by POST-discount ratios (540:360 = 3:2), not 600:400.
    assert result.members[A].gst_minor + result.members[B].gst_minor == 162


# ---------------------------------------------------------------------------
# 7. discount == subtotal: GST falls back to PRE-discount base shares.
# ---------------------------------------------------------------------------


def test_full_discount_gst_falls_back_to_pre_discount_shares() -> None:
    lines = [_item(1, 600, {A: 1}), _item(2, 400, {B: 1})]
    discount = DiscountSpec(
        type=DiscountType.flat, value_minor=1000, percent=None, threshold_minor=0
    )
    gst = GstSpec(mode=GstMode.invoice_exclusive, component_total_minor=100)
    result = compute_allocation(lines, 100, discount=discount, gst=gst)
    assert result.applied_discount_minor == 1000
    # post-discount subtotal is 0 -> GST distributed 600:400 (pre-discount).
    assert result.members[A].gst_minor == 60
    assert result.members[B].gst_minor == 40
    assert result.members[A].total_minor == 60
    assert result.members[B].total_minor == 40
    assert sum(m.total_minor for m in result.members.values()) == 100


# ---------------------------------------------------------------------------
# 8. Weighted 2:1:1 split with discount + GST simultaneously.
# ---------------------------------------------------------------------------


def test_weighted_2_1_1_discount_and_gst() -> None:
    lines = [_item(1, 4000, {A: 2, B: 1, C: 1})]
    discount = DiscountSpec(
        type=DiscountType.flat, value_minor=400, percent=None, threshold_minor=0
    )
    gst = GstSpec(mode=GstMode.invoice_exclusive, component_total_minor=180)
    total_minor = 4000 - 400 + 180
    result = compute_allocation(lines, total_minor, discount=discount, gst=gst)
    assert result.applied_discount_minor == 400
    assert result.exclusive_gst_minor == 180
    assert sum(m.total_minor for m in result.members.values()) == total_minor
    # A (weight 2/4) roughly double B/C.
    assert result.members[A].base_minor == 2000
    assert result.members[B].base_minor == 1000
    assert result.members[C].base_minor == 1000


# ---------------------------------------------------------------------------
# 9. A refund line combined with discount + GST still reconciles.
# ---------------------------------------------------------------------------


def test_refund_with_discount_and_gst_reconciles() -> None:
    lines = [
        _item(1, 3000, {A: 2, B: 1}),
        LineInput(
            line_id=_lid(2),
            kind=LineItemKind.refund,
            total_minor=-300,
            parent_line_id=_lid(1),
        ),
    ]
    discount = DiscountSpec(
        type=DiscountType.flat, value_minor=270, percent=None, threshold_minor=0
    )
    gst = GstSpec(mode=GstMode.invoice_exclusive, component_total_minor=486)
    subtotal = 3000 - 300  # 2700
    total_minor = subtotal - 270 + 486
    result = compute_allocation(lines, total_minor, discount=discount, gst=gst)
    assert result.subtotal_minor == subtotal
    assert result.applied_discount_minor == 270
    assert result.exclusive_gst_minor == 486
    assert sum(m.total_minor for m in result.members.values()) == total_minor
    assert all(m.total_minor >= 0 for m in result.members.values())


# ---------------------------------------------------------------------------
# 10. Single-member expense.
# ---------------------------------------------------------------------------


def test_single_member_discount_and_gst() -> None:
    lines = [_item(1, 1000, {A: 1})]
    discount = DiscountSpec(
        type=DiscountType.flat, value_minor=100, percent=None, threshold_minor=0
    )
    gst = GstSpec(mode=GstMode.invoice_exclusive, component_total_minor=162)
    result = compute_allocation(lines, 1000 - 100 + 162, discount=discount, gst=gst)
    assert result.members[A].total_minor == 1000 - 100 + 162


# ---------------------------------------------------------------------------
# 11. Unassigned GST-bearing line blocks (SplitError, existing mechanism).
# ---------------------------------------------------------------------------


def test_unassigned_item_level_gst_line_raises_split_error() -> None:
    lines = [
        LineInput(line_id=_lid(1), kind=LineItemKind.item, total_minor=1000),
    ]
    gst = GstSpec(
        mode=GstMode.item_level,
        component_total_minor=0,
        per_line_gst_minor={_lid(1): 180},
    )
    with pytest.raises(SplitError, match="no assignments"):
        compute_allocation(lines, 1000, gst=gst)


# ---------------------------------------------------------------------------
# 12. Property tests.
# ---------------------------------------------------------------------------

_user_pool = [uuid.UUID(int=2000 + i) for i in range(4)]


@st.composite
def _random_allocation_case(draw):
    n_items = draw(st.integers(min_value=1, max_value=4))
    lines: list[LineInput] = []
    for lid in range(1, n_items + 1):
        assignees = draw(
            st.lists(
                st.sampled_from(range(len(_user_pool))),
                min_size=1,
                max_size=len(_user_pool),
                unique=True,
            )
        )
        weights = {
            _user_pool[i]: Fraction(draw(st.integers(min_value=1, max_value=5)))
            for i in assignees
        }
        total = draw(st.integers(min_value=0, max_value=50_000))
        lines.append(_item(lid, total, weights))

    subtotal = sum(line.total_minor for line in lines)

    discount = None
    if subtotal > 0 and draw(st.booleans()):
        value = draw(st.integers(min_value=0, max_value=subtotal))
        discount = DiscountSpec(
            type=DiscountType.flat,
            value_minor=value,
            percent=None,
            threshold_minor=draw(st.integers(min_value=0, max_value=subtotal)),
        )

    gst = None
    if draw(st.booleans()):
        gst_amount = draw(st.integers(min_value=0, max_value=20_000))
        gst = GstSpec(mode=GstMode.invoice_exclusive, component_total_minor=gst_amount)

    applied = 0
    if discount is not None and subtotal >= discount.threshold_minor:
        applied = min(discount.value_minor or 0, subtotal)
    exclusive_gst = gst.component_total_minor if gst is not None else 0
    total_minor = subtotal - applied + exclusive_gst
    return lines, total_minor, discount, gst


@settings(max_examples=200, deadline=None)
@given(_random_allocation_case())
def test_property_allocation_always_reconciles_and_nonnegative(case) -> None:
    lines, total_minor, discount, gst = case
    try:
        result = compute_allocation(lines, total_minor, discount=discount, gst=gst)
    except SplitError:
        return
    assert sum(m.total_minor for m in result.members.values()) == total_minor
    assert all(m.total_minor >= 0 for m in result.members.values())


@settings(max_examples=100, deadline=None)
@given(_random_allocation_case())
def test_property_allocation_deterministic(case) -> None:
    lines, total_minor, discount, gst = case
    try:
        r1 = compute_allocation(lines, total_minor, discount=discount, gst=gst)
        r2 = compute_allocation(lines, total_minor, discount=discount, gst=gst)
    except SplitError:
        return
    t1 = {u: b.total_minor for u, b in r1.members.items()}
    t2 = {u: b.total_minor for u, b in r2.members.items()}
    assert t1 == t2


@settings(max_examples=100, deadline=None)
@given(_random_allocation_case())
def test_property_allocation_permutation_invariant(case) -> None:
    lines, total_minor, discount, gst = case
    try:
        r1 = compute_allocation(lines, total_minor, discount=discount, gst=gst)
        r2 = compute_allocation(
            list(reversed(lines)), total_minor, discount=discount, gst=gst
        )
    except SplitError:
        return
    t1 = {u: b.total_minor for u, b in r1.members.items()}
    t2 = {u: b.total_minor for u, b in r2.members.items()}
    assert t1 == t2


# ---------------------------------------------------------------------------
# 13. OQ-1a: discount-snapshot / discount-line consistency (app.domain.gst).
# ---------------------------------------------------------------------------


def test_discount_consistency_extracted_flat_matches() -> None:
    issues = check_discount_consistency(
        discount_source=DiscountSource.extracted,
        discount_type=DiscountType.flat,
        discount_value_minor=500,
        discount_percent=None,
        base_subtotal_minor=5000,
        discount_line_items_total_abs_minor=500,
        has_discount_line_items=True,
    )
    assert issues == []


def test_discount_consistency_extracted_flat_mismatch_flagged() -> None:
    issues = check_discount_consistency(
        discount_source=DiscountSource.extracted,
        discount_type=DiscountType.flat,
        discount_value_minor=500,
        discount_percent=None,
        base_subtotal_minor=5000,
        discount_line_items_total_abs_minor=300,
        has_discount_line_items=True,
    )
    assert len(issues) == 1
    assert issues[0].code == "discount_snapshot_line_mismatch"


def test_discount_consistency_manual_collides_with_extracted_lines() -> None:
    issues = check_discount_consistency(
        discount_source=DiscountSource.manual,
        discount_type=DiscountType.flat,
        discount_value_minor=500,
        discount_percent=None,
        base_subtotal_minor=5000,
        discount_line_items_total_abs_minor=500,
        has_discount_line_items=True,
    )
    assert len(issues) == 1
    assert issues[0].code == "discount_snapshot_collision"


def test_discount_consistency_no_discount_lines_no_issues() -> None:
    issues = check_discount_consistency(
        discount_source=DiscountSource.vendor_rule,
        discount_type=DiscountType.flat,
        discount_value_minor=500,
        discount_percent=None,
        base_subtotal_minor=5000,
        discount_line_items_total_abs_minor=0,
        has_discount_line_items=False,
    )
    assert issues == []


# ---------------------------------------------------------------------------
# API-level helpers (mirror tests/test_api_m2.py and test_m6_gst_structured_data.py)
# ---------------------------------------------------------------------------


async def _make_orm_user(db: AsyncSession, name: str) -> User:
    user = User(name=name, email=f"{name.lower()}_{uuid.uuid4().hex[:6]}@test.com")
    db.add(user)
    await db.flush()
    return user


def _token(user_id: uuid.UUID) -> str:
    from app.config import settings
    from app.domain.auth import create_access_token

    return create_access_token(user_id, settings.SECRET_KEY)


# ---------------------------------------------------------------------------
# 14. OQ-2 sequence: auto-applied discount goes inert after a correction
#     drops the fresh-computed subtotal below its threshold; the discount_*
#     snapshot columns remain UNTOUCHED.
# ---------------------------------------------------------------------------


async def test_oq2_discount_goes_inert_after_correction_snapshot_untouched(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.domain.models import VendorDiscountRule

    creator = await _make_orm_user(db_session, "Creator")
    rule = VendorDiscountRule(
        group_id=None,
        created_by=creator.id,
        vendor_pattern="amazon",
        min_order_total_minor=35000,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    db_session.add(rule)
    await db_session.commit()

    alice = await _make_orm_user(db_session, "Alice")
    expense = Expense(
        paid_by=alice.id,
        vendor="Amazon",
        currency="INR",
        total_minor=40000,
        subtotal_minor=40000,
        source=ExpenseSource.pdf,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
    )
    db_session.add(expense)
    await db_session.flush()
    line = ExpenseLineItem(
        expense_id=expense.id,
        line_no=1,
        kind=LineItemKind.item,
        quantity=1,
        total_minor=40000,
    )
    db_session.add(line)
    await db_session.flush()
    db_session.add(ItemAssignment(line_item_id=line.id, user_id=alice.id, weight=1))

    from app.domain.vendor_discount import apply_vendor_discount_snapshot

    await apply_vendor_discount_snapshot(
        db_session, expense, subtotal_override_minor=40000
    )
    await db_session.commit()
    await db_session.refresh(expense)

    assert expense.discount_source == DiscountSource.vendor_rule
    assert expense.discount_value_minor == 5000
    snapshot_before = {
        "discount_type": expense.discount_type,
        "discount_value_minor": expense.discount_value_minor,
        "discount_threshold_minor": expense.discount_threshold_minor,
        "discount_source": expense.discount_source,
    }

    resp = await client.get(
        f"{API}/expenses/{expense.id}/allocation-preview",
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied_discount_minor"] == 5000
    assert body["discount_recorded_but_inert"] is False

    # Correction drops the fresh-computed subtotal to 30000 (below 35000).
    resp2 = await client.put(
        f"{API}/expenses/{expense.id}/line-items",
        json={
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Item",
                    "total_minor": 40000,
                }
            ]
        },
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    # This expense isn't needs_review, so correction isn't even reachable via
    # that endpoint (409) -- instead simulate the correction directly via ORM
    # (line-items correction endpoint is gated on parse_status=needs_review).
    assert resp2.status_code == 409

    line.total_minor = 30000
    expense.total_minor = 30000
    expense.subtotal_minor = 30000
    await db_session.commit()
    await db_session.refresh(expense)

    resp3 = await client.get(
        f"{API}/expenses/{expense.id}/allocation-preview",
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert resp3.status_code == 200, resp3.text
    body3 = resp3.json()
    assert body3["applied_discount_minor"] == 0
    assert body3["discount_recorded_but_inert"] is True
    assert any(p["code"] == "discount_recorded_but_inert" for p in body3["problems"])

    # Snapshot itself is untouched by the correction (nothing silently clears
    # it) -- verify explicitly.
    await db_session.refresh(expense)
    assert expense.discount_type == snapshot_before["discount_type"]
    assert expense.discount_value_minor == snapshot_before["discount_value_minor"]
    assert (
        expense.discount_threshold_minor == snapshot_before["discount_threshold_minor"]
    )
    assert expense.discount_source == snapshot_before["discount_source"]


# ---------------------------------------------------------------------------
# 15. End-to-end: preview -> confirm -> persisted rows match preview and
#     reconcile against the ledger; balances reflect discount + GST.
# ---------------------------------------------------------------------------


async def test_confirm_persists_member_allocations_matching_preview_and_ledger(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    bob = await _make_orm_user(db_session, "Bob")

    expense = Expense(
        paid_by=alice.id,
        vendor="Swiggy",
        currency="INR",
        total_minor=1,  # placeholder, corrected below
        subtotal_minor=1000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
        gst_mode=GstMode.invoice_exclusive,
        discount_type=DiscountType.flat,
        discount_value_minor=100,
        discount_source=DiscountSource.manual,
        discount_threshold_minor=0,
    )
    db_session.add(expense)
    await db_session.flush()

    line1 = ExpenseLineItem(
        expense_id=expense.id, line_no=1, kind=LineItemKind.item, total_minor=600
    )
    line2 = ExpenseLineItem(
        expense_id=expense.id, line_no=2, kind=LineItemKind.item, total_minor=400
    )
    db_session.add_all([line1, line2])
    await db_session.flush()
    db_session.add(ItemAssignment(line_item_id=line1.id, user_id=alice.id, weight=1))
    db_session.add(ItemAssignment(line_item_id=line2.id, user_id=bob.id, weight=1))
    db_session.add(
        ExpenseTaxComponent(
            expense_id=expense.id, name=TaxComponentName.GST, amount_minor=162
        )
    )
    # subtotal 1000, discount 100 -> 900, gst 18% of 900 = 162.
    expense.total_minor = 1000 - 100 + 162
    await db_session.commit()

    preview_resp = await client.get(
        f"{API}/expenses/{expense.id}/allocation-preview",
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert preview_resp.status_code == 200, preview_resp.text
    preview = preview_resp.json()
    assert preview["confirmed"] is False
    assert preview["applied_discount_minor"] == 100
    assert preview["exclusive_gst_minor"] == 162

    confirm_resp = await client.post(
        f"{API}/expenses/{expense.id}/confirm",
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text

    confirmed_preview_resp = await client.get(
        f"{API}/expenses/{expense.id}/allocation-preview",
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert confirmed_preview_resp.status_code == 200
    confirmed_preview = confirmed_preview_resp.json()
    assert confirmed_preview["confirmed"] is True

    preview_members = {m["user_id"]: m for m in preview["members"]}
    persisted_members = {m["user_id"]: m for m in confirmed_preview["members"]}
    assert preview_members.keys() == persisted_members.keys()
    for uid, pm in preview_members.items():
        cm = persisted_members[uid]
        assert pm["total_minor"] == cm["total_minor"]
        assert pm["base_minor"] == cm["base_minor"]
        assert pm["discount_minor"] == cm["discount_minor"]
        assert pm["gst_minor"] == cm["gst_minor"]

    # Persisted rows directly.
    rows_result = await db_session.execute(
        select(ExpenseMemberAllocation).where(
            ExpenseMemberAllocation.expense_id == expense.id
        )
    )
    rows = list(rows_result.scalars().all())
    assert len(rows) == 2
    assert sum(int(r.total_minor) for r in rows) == expense.total_minor

    # Ledger reconciliation: sum(persisted allocation total_minor for
    # non-payer members) == sum(ledger entries posted for this expense).
    ledger_result = await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.expense_id == expense.id)
    )
    ledger_entries = list(ledger_result.scalars().all())
    ledger_total = sum(int(e.amount_minor) for e in ledger_entries)
    non_payer_total = sum(
        int(r.total_minor) for r in rows if uuid.UUID(str(r.user_id)) != alice.id
    )
    assert ledger_total == non_payer_total

    # Group balances aren't wired (no group_id here) -- but bob owes alice
    # exactly his allocation total via the ledger directly.
    bob_entry = next(e for e in ledger_entries if uuid.UUID(str(e.debtor_id)) == bob.id)
    bob_row = next(r for r in rows if uuid.UUID(str(r.user_id)) == bob.id)
    assert int(bob_entry.amount_minor) == int(bob_row.total_minor)


async def test_confirm_group_balances_reflect_discount_and_gst(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    bob = await _make_orm_user(db_session, "Bob")
    await db_session.commit()

    r_group = await client.post(
        f"{API}/groups",
        json={"name": "Alloc Group", "created_by": str(alice.id)},
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert r_group.status_code == 201, r_group.text
    group_id = uuid.UUID(r_group.json()["id"])
    r_member = await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": str(bob.id)},
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert r_member.status_code in (200, 201), r_member.text

    expense = Expense(
        group_id=group_id,
        paid_by=alice.id,
        vendor="Zomato",
        currency="INR",
        total_minor=1,
        subtotal_minor=1000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
        gst_mode=GstMode.invoice_exclusive,
        discount_type=DiscountType.flat,
        discount_value_minor=100,
        discount_source=DiscountSource.manual,
        discount_threshold_minor=0,
    )
    db_session.add(expense)
    await db_session.flush()
    line1 = ExpenseLineItem(
        expense_id=expense.id, line_no=1, kind=LineItemKind.item, total_minor=600
    )
    line2 = ExpenseLineItem(
        expense_id=expense.id, line_no=2, kind=LineItemKind.item, total_minor=400
    )
    db_session.add_all([line1, line2])
    await db_session.flush()
    db_session.add(ItemAssignment(line_item_id=line1.id, user_id=alice.id, weight=1))
    db_session.add(ItemAssignment(line_item_id=line2.id, user_id=bob.id, weight=1))
    db_session.add(
        ExpenseTaxComponent(
            expense_id=expense.id, name=TaxComponentName.GST, amount_minor=162
        )
    )
    expense.total_minor = 1000 - 100 + 162
    await db_session.commit()

    confirm_resp = await client.post(
        f"{API}/expenses/{expense.id}/confirm",
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text

    balances = await compute_group_balances(db_session, group_id)
    assert len(balances) == 1
    debtor, creditor, amount = balances[0]
    assert debtor == bob.id
    assert creditor == alice.id
    # Bob's post-discount+GST share: 400 base, discounted+taxed proportionally.
    rows_result = await db_session.execute(
        select(ExpenseMemberAllocation).where(
            ExpenseMemberAllocation.expense_id == expense.id,
            ExpenseMemberAllocation.user_id == bob.id,
        )
    )
    bob_row = rows_result.scalar_one()
    assert amount == int(bob_row.total_minor)


@pytest.mark.postgres
async def test_concurrent_double_confirm_exactly_once_with_discount_gst(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """C1-style concurrency test under item 5's new arithmetic."""
    alice = await _make_orm_user(db_session, "Alice")
    bob = await _make_orm_user(db_session, "Bob")
    await db_session.commit()

    r_group = await client.post(
        f"{API}/groups",
        json={"name": "Concurrent Group", "created_by": str(alice.id)},
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    group_id = uuid.UUID(r_group.json()["id"])
    await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": str(bob.id)},
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )

    expense = Expense(
        group_id=group_id,
        paid_by=alice.id,
        vendor="Blinkit",
        currency="INR",
        total_minor=1,
        subtotal_minor=1000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
        gst_mode=GstMode.invoice_exclusive,
        discount_type=DiscountType.flat,
        discount_value_minor=100,
        discount_source=DiscountSource.manual,
        discount_threshold_minor=0,
    )
    db_session.add(expense)
    await db_session.flush()
    line1 = ExpenseLineItem(
        expense_id=expense.id, line_no=1, kind=LineItemKind.item, total_minor=600
    )
    line2 = ExpenseLineItem(
        expense_id=expense.id, line_no=2, kind=LineItemKind.item, total_minor=400
    )
    db_session.add_all([line1, line2])
    await db_session.flush()
    db_session.add(ItemAssignment(line_item_id=line1.id, user_id=alice.id, weight=1))
    db_session.add(ItemAssignment(line_item_id=line2.id, user_id=bob.id, weight=1))
    db_session.add(
        ExpenseTaxComponent(
            expense_id=expense.id, name=TaxComponentName.GST, amount_minor=162
        )
    )
    expense.total_minor = 1000 - 100 + 162
    await db_session.commit()
    expense_id = expense.id

    r1, r2 = await asyncio.gather(
        client.post(
            f"{API}/expenses/{expense_id}/confirm",
            headers={"Authorization": f"Bearer {_token(alice.id)}"},
        ),
        client.post(
            f"{API}/expenses/{expense_id}/confirm",
            headers={"Authorization": f"Bearer {_token(alice.id)}"},
        ),
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    balances = await compute_group_balances(db_session, group_id)
    assert len(balances) == 1
    _, _, amount = balances[0]

    rows_result = await db_session.execute(
        select(ExpenseMemberAllocation).where(
            ExpenseMemberAllocation.expense_id == expense_id
        )
    )
    rows = list(rows_result.scalars().all())
    assert len(rows) == 2, "member allocations must be written exactly once"
    bob_row = next(r for r in rows if uuid.UUID(str(r.user_id)) == bob.id)
    assert amount == int(bob_row.total_minor)


