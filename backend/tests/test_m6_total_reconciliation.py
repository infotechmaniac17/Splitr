"""
M6-M8 total-reconciliation ruling (b): total_minor stays user-declared,
never rule-derived.

Covers:
  - app.domain.gst.check_gst_invariants' new `total_mismatch_with_discount`
    invariant: gst_mode='none' and 'item_level', discount present / absent /
    below-threshold, exact boundary at TOLERANCE_MINOR.
  - Snapshot-time flagging: apply_vendor_discount_snapshot sets
    needs_review=True when the matched rule de-reconciles the declared
    total; the manual-set path (PATCH /expenses/{id}/discount) does the
    same, and can also CLEAR needs_review once reconciliation is restored.
  - POST /expenses/{id}/accept-computed-total: happy path, all guard
    statuses (404/403/409 confirmed/409 voided/422 frozen shares/422
    non-positive result), and needs_review clearing.
  - The original repro (manual expense, vendor-rule discount snapshot,
    gross total_minor) now dies at the VALIDATOR (422
    total_mismatch_with_discount), never at app.domain.ledger's
    "Share sum ... does not equal expense total ..." tripwire.
  - GET /expenses/{id}/allocation-preview's new `unassigned_lines`
    structured problem, emitted before compute_allocation runs.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.gst import check_gst_invariants
from app.domain.models import (
    DiscountSource,
    DiscountType,
    Expense,
    ExpenseLineItem,
    ExpenseSource,
    ExpenseStatus,
    GstMode,
    ItemAssignment,
    LineItemKind,
    ParseStatus,
    User,
    VendorDiscountRule,
)

API = "/api/v1"


async def _make_user(db: AsyncSession, name: str) -> User:
    user = User(name=name, email=f"{name.lower()}_{uuid.uuid4().hex[:6]}@test.com")
    db.add(user)
    await db.flush()
    return user


def _token(user_id: uuid.UUID) -> str:
    from app.config import settings
    from app.domain.auth import create_access_token

    return create_access_token(user_id, settings.SECRET_KEY)


def _auth(user_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(user_id)}"}


# ---------------------------------------------------------------------------
# 1. gst.py-level unit tests for total_mismatch_with_discount.
# ---------------------------------------------------------------------------


def test_none_mode_no_discount_skips_check_even_if_items_dont_reconcile() -> None:
    """Pre-existing legal M1/M2 flows (no discount at all) must be untouched."""
    result = check_gst_invariants(
        gst_mode=GstMode.none,
        item_totals_minor=12345,
        discount_amount_minor=0,
        tax_component_amounts_minor=[],
        invoice_total_minor=99999,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=False,
    )
    assert result.ok


def test_none_mode_discount_reconciles_ok() -> None:
    result = check_gst_invariants(
        gst_mode=GstMode.none,
        item_totals_minor=40000,
        discount_amount_minor=5000,
        tax_component_amounts_minor=[],
        invoice_total_minor=35000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=False,
    )
    assert result.ok


def test_none_mode_discount_mismatch_flagged() -> None:
    """The original repro's exact numbers: 40000 items, 500-off rule,
    total_minor left at gross 40000."""
    result = check_gst_invariants(
        gst_mode=GstMode.none,
        item_totals_minor=40000,
        discount_amount_minor=5000,
        tax_component_amounts_minor=[],
        invoice_total_minor=40000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=False,
    )
    assert not result.ok
    assert result.issues[0].code == "total_mismatch_with_discount"
    assert "35000" in result.detail()  # reconciled total
    assert "40000" in result.detail()  # declared total


def test_none_mode_discount_below_threshold_inert_skips_check() -> None:
    """Caller passes discount_amount_minor=0 (resolve_discount_amount's
    contract) when a snapshot exists but is below its threshold -- the
    invariant must not fire even though a "discount" nominally exists."""
    result = check_gst_invariants(
        gst_mode=GstMode.none,
        item_totals_minor=1000,
        discount_amount_minor=0,  # inert: below threshold
        tax_component_amounts_minor=[],
        invoice_total_minor=1000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=False,
    )
    assert result.ok


def test_none_mode_exact_boundary_at_tolerance() -> None:
    # expected = 40000 - 5000 = 35000; declared 35001 is within TOLERANCE_MINOR=1.
    at_tolerance = check_gst_invariants(
        gst_mode=GstMode.none,
        item_totals_minor=40000,
        discount_amount_minor=5000,
        tax_component_amounts_minor=[],
        invoice_total_minor=35001,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=False,
    )
    assert at_tolerance.ok

    # 2 minor units off -- must fail.
    beyond_tolerance = check_gst_invariants(
        gst_mode=GstMode.none,
        item_totals_minor=40000,
        discount_amount_minor=5000,
        tax_component_amounts_minor=[],
        invoice_total_minor=35002,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=False,
    )
    assert not beyond_tolerance.ok
    assert beyond_tolerance.issues[0].code == "total_mismatch_with_discount"


def test_item_level_no_discount_untouched() -> None:
    """item_level's pre-existing line/component invariant is unaffected when
    no discount is in play."""
    result = check_gst_invariants(
        gst_mode=GstMode.item_level,
        item_totals_minor=30000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[4100],
        invoice_total_minor=34100,
        line_gst_amounts_minor=[500, 3600],
        has_line_gst_data=True,
        has_component_data=True,
    )
    assert result.ok


def test_item_level_discount_reconciles_ok() -> None:
    result = check_gst_invariants(
        gst_mode=GstMode.item_level,
        item_totals_minor=34100,
        discount_amount_minor=4100,
        tax_component_amounts_minor=[],
        invoice_total_minor=30000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=False,
    )
    assert result.ok


def test_item_level_discount_mismatch_flagged_independent_of_line_gst_data() -> None:
    """Fires even when has_line_gst_data/has_component_data are both False --
    unconditional on discount_amount_minor > 0, unlike gst_item_level_mismatch."""
    result = check_gst_invariants(
        gst_mode=GstMode.item_level,
        item_totals_minor=34100,
        discount_amount_minor=4100,
        tax_component_amounts_minor=[],
        invoice_total_minor=34100,  # should be 30000
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=False,
    )
    assert not result.ok
    codes = {i.code for i in result.issues}
    assert "total_mismatch_with_discount" in codes
    assert "gst_item_level_mismatch" not in codes  # skipped: no line/component data


def test_item_level_discount_and_line_gst_mismatch_both_flagged() -> None:
    """Both invariants can fire together and are named independently."""
    result = check_gst_invariants(
        gst_mode=GstMode.item_level,
        item_totals_minor=34100,
        discount_amount_minor=4100,
        tax_component_amounts_minor=[9999],
        invoice_total_minor=999999,  # wrong on both counts
        line_gst_amounts_minor=[500, 3600],
        has_line_gst_data=True,
        has_component_data=True,
    )
    assert not result.ok
    codes = {i.code for i in result.issues}
    assert "total_mismatch_with_discount" in codes
    assert "gst_item_level_mismatch" in codes


# ---------------------------------------------------------------------------
# 2. Snapshot-time flagging (apply_vendor_discount_snapshot) and the
#    manual-set path (PATCH /expenses/{id}/discount).
# ---------------------------------------------------------------------------


async def test_vendor_rule_snapshot_flags_needs_review_on_mismatch(
    db_session: AsyncSession,
) -> None:
    """The exact repro: 2 items summing 40000, total_minor=40000, a
    Rs.500-off-Rs.3500+ rule auto-snapshots at create -- flags needs_review
    immediately, before confirm is ever attempted."""
    from app.domain.vendor_discount import apply_vendor_discount_snapshot

    creator = await _make_user(db_session, "Creator")
    rule = VendorDiscountRule(
        group_id=None,
        created_by=creator.id,
        vendor_pattern="amazon",
        min_order_total_minor=350000,
        discount_type=DiscountType.flat,
        discount_value_minor=50000,
    )
    db_session.add(rule)
    await db_session.commit()

    alice = await _make_user(db_session, "Alice")
    expense = Expense(
        paid_by=alice.id,
        vendor="Amazon",
        currency="INR",
        total_minor=4000000,
        subtotal_minor=4000000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
    )
    db_session.add(expense)
    await db_session.flush()
    assert expense.needs_review is False

    await apply_vendor_discount_snapshot(
        db_session, expense, subtotal_override_minor=4000000
    )
    await db_session.commit()
    await db_session.refresh(expense)

    assert expense.discount_source == DiscountSource.vendor_rule
    assert expense.needs_review is True


async def test_vendor_rule_snapshot_no_flag_when_reconciled(
    db_session: AsyncSession,
) -> None:
    """No mismatch -- e.g. gst_mode='invoice_exclusive' (out of this
    function's checked scope) -- must not spuriously flag."""
    from app.domain.vendor_discount import apply_vendor_discount_snapshot

    creator = await _make_user(db_session, "Creator")
    rule = VendorDiscountRule(
        group_id=None,
        created_by=creator.id,
        vendor_pattern="zomato",
        min_order_total_minor=0,
        discount_type=DiscountType.flat,
        discount_value_minor=100,
    )
    db_session.add(rule)
    await db_session.commit()

    alice = await _make_user(db_session, "Alice")
    expense = Expense(
        paid_by=alice.id,
        vendor="Zomato",
        currency="INR",
        total_minor=900,  # already reconciled: 1000 - 100 = 900
        subtotal_minor=1000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
    )
    db_session.add(expense)
    await db_session.flush()

    await apply_vendor_discount_snapshot(
        db_session, expense, subtotal_override_minor=1000
    )
    await db_session.commit()
    await db_session.refresh(expense)

    assert expense.discount_source == DiscountSource.vendor_rule
    assert expense.needs_review is False


async def test_patch_discount_manual_set_flags_needs_review_on_mismatch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = Expense(
        paid_by=alice.id,
        vendor="Amazon",
        currency="INR",
        total_minor=40000,
        subtotal_minor=40000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
    )
    db_session.add(expense)
    await db_session.flush()
    line = ExpenseLineItem(
        expense_id=expense.id, line_no=1, kind=LineItemKind.item, total_minor=40000
    )
    db_session.add(line)
    await db_session.flush()
    db_session.add(ItemAssignment(line_item_id=line.id, user_id=alice.id, weight=1))
    await db_session.commit()

    resp = await client.patch(
        f"{API}/expenses/{expense.id}/discount",
        json={"discount_type": "flat", "discount_value_minor": 5000},
        headers=_auth(alice.id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["needs_review"] is True

    await db_session.refresh(expense)
    assert expense.needs_review is True


async def test_patch_discount_clear_restores_reconciliation_and_clears_flag(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = Expense(
        paid_by=alice.id,
        vendor="Amazon",
        currency="INR",
        total_minor=40000,
        subtotal_minor=40000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
    )
    db_session.add(expense)
    await db_session.flush()
    line = ExpenseLineItem(
        expense_id=expense.id, line_no=1, kind=LineItemKind.item, total_minor=40000
    )
    db_session.add(line)
    await db_session.flush()
    db_session.add(ItemAssignment(line_item_id=line.id, user_id=alice.id, weight=1))
    await db_session.commit()

    set_resp = await client.patch(
        f"{API}/expenses/{expense.id}/discount",
        json={"discount_type": "flat", "discount_value_minor": 5000},
        headers=_auth(alice.id),
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["needs_review"] is True

    clear_resp = await client.patch(
        f"{API}/expenses/{expense.id}/discount",
        json={"discount_type": None},
        headers=_auth(alice.id),
    )
    assert clear_resp.status_code == 200, clear_resp.text
    body = clear_resp.json()
    assert body["discount_type"] is None
    assert body["needs_review"] is False


# ---------------------------------------------------------------------------
# 3. POST /expenses/{id}/accept-computed-total.
# ---------------------------------------------------------------------------


async def _mismatched_draft_expense(
    db: AsyncSession, alice: User, *, total_minor: int = 40000
) -> Expense:
    expense = Expense(
        paid_by=alice.id,
        vendor="Amazon",
        currency="INR",
        total_minor=total_minor,
        subtotal_minor=total_minor,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
        discount_source=DiscountSource.manual,
        discount_threshold_minor=0,
    )
    db.add(expense)
    await db.flush()
    line1 = ExpenseLineItem(
        expense_id=expense.id, line_no=1, kind=LineItemKind.item, total_minor=20000
    )
    line2 = ExpenseLineItem(
        expense_id=expense.id, line_no=2, kind=LineItemKind.item, total_minor=20000
    )
    db.add_all([line1, line2])
    await db.flush()
    return expense


async def test_accept_computed_total_happy_path(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _mismatched_draft_expense(db_session, alice)
    await db_session.commit()
    expense_id = expense.id

    preview_before = await client.get(
        f"{API}/expenses/{expense_id}/allocation-preview", headers=_auth(alice.id)
    )
    assert preview_before.status_code == 200

    resp = await client.post(
        f"{API}/expenses/{expense_id}/accept-computed-total", headers=_auth(alice.id)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_minor"] == 35000  # 40000 - 5000
    assert body["needs_review"] is False

    await db_session.refresh(expense)
    assert expense.total_minor == 35000
    assert expense.needs_review is False

    # Now confirmable without any assignment 422 -- wire assignments then
    # confirm to prove the reconciled total actually posts cleanly.
    db_session.add_all(
        [
            ItemAssignment(
                line_item_id=(
                    (
                        await db_session.execute(
                            select(ExpenseLineItem).where(
                                ExpenseLineItem.expense_id == expense_id,
                                ExpenseLineItem.line_no == 1,
                            )
                        )
                    )
                    .scalar_one()
                    .id
                ),
                user_id=alice.id,
                weight=1,
            ),
            ItemAssignment(
                line_item_id=(
                    (
                        await db_session.execute(
                            select(ExpenseLineItem).where(
                                ExpenseLineItem.expense_id == expense_id,
                                ExpenseLineItem.line_no == 2,
                            )
                        )
                    )
                    .scalar_one()
                    .id
                ),
                user_id=alice.id,
                weight=1,
            ),
        ]
    )
    await db_session.commit()

    confirm_resp = await client.post(
        f"{API}/expenses/{expense_id}/confirm", headers=_auth(alice.id)
    )
    assert confirm_resp.status_code == 200, confirm_resp.text


async def test_accept_computed_total_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    resp = await client.post(
        f"{API}/expenses/{uuid.uuid4()}/accept-computed-total", headers=_auth(alice.id)
    )
    assert resp.status_code == 404


async def test_accept_computed_total_403_unauthorized(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    mallory = await _make_user(db_session, "Mallory")
    expense = await _mismatched_draft_expense(db_session, alice)
    await db_session.commit()

    resp = await client.post(
        f"{API}/expenses/{expense.id}/accept-computed-total", headers=_auth(mallory.id)
    )
    assert resp.status_code == 403


async def test_accept_computed_total_409_confirmed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = Expense(
        paid_by=alice.id,
        vendor="Amazon",
        currency="INR",
        total_minor=1000,
        subtotal_minor=1000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.confirmed,
        status=ExpenseStatus.active,
    )
    db_session.add(expense)
    await db_session.flush()
    await db_session.commit()

    resp = await client.post(
        f"{API}/expenses/{expense.id}/accept-computed-total", headers=_auth(alice.id)
    )
    assert resp.status_code == 409


async def test_accept_computed_total_409_voided(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _mismatched_draft_expense(db_session, alice)
    expense.status = ExpenseStatus.voided
    await db_session.commit()

    resp = await client.post(
        f"{API}/expenses/{expense.id}/accept-computed-total", headers=_auth(alice.id)
    )
    assert resp.status_code == 409


async def test_accept_computed_total_422_frozen_shares(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    await db_session.commit()
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": str(alice.id),
            "vendor": "V",
            "total_minor": 10000,
            "shares": {str(alice.id): 4000, str(bob.id): 6000},
        },
        headers=_auth(alice.id),
    )
    assert resp.status_code == 201, resp.text
    expense_id = resp.json()["id"]

    accept_resp = await client.post(
        f"{API}/expenses/{expense_id}/accept-computed-total", headers=_auth(alice.id)
    )
    assert accept_resp.status_code == 422, accept_resp.text
    assert "frozen" in accept_resp.json()["detail"].lower()


async def test_accept_computed_total_422_nonpositive_result(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = Expense(
        paid_by=alice.id,
        vendor="Amazon",
        currency="INR",
        total_minor=1000,
        subtotal_minor=1000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
        discount_type=DiscountType.flat,
        discount_value_minor=1000,  # discount == full subtotal
        discount_source=DiscountSource.manual,
        discount_threshold_minor=0,
    )
    db_session.add(expense)
    await db_session.flush()
    line = ExpenseLineItem(
        expense_id=expense.id, line_no=1, kind=LineItemKind.item, total_minor=1000
    )
    db_session.add(line)
    await db_session.flush()
    db_session.add(ItemAssignment(line_item_id=line.id, user_id=alice.id, weight=1))
    await db_session.commit()

    resp = await client.post(
        f"{API}/expenses/{expense.id}/accept-computed-total", headers=_auth(alice.id)
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# 4. Ledger tripwire unreachable: the original repro dies at the validator,
#    never at app.domain.ledger's "Share sum ... does not equal ..." message.
# ---------------------------------------------------------------------------


async def test_repro_dies_at_validator_not_ledger_tripwire(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    Exact repro from the bug report: manual expense, 2 items summing 40000,
    total_minor=40000, a vendor rule (Rs.500-off-Rs.3500+) auto-snapshots a
    discount at create -- POST confirm must now fail with a 422 naming
    `total_mismatch_with_discount`, and the response body must NOT contain
    ledger.py's "Share sum" tripwire message.
    """
    creator = await _make_user(db_session, "Creator")
    rule = VendorDiscountRule(
        group_id=None,
        created_by=creator.id,
        vendor_pattern="amazon",
        min_order_total_minor=350000,
        discount_type=DiscountType.flat,
        discount_value_minor=50000,
    )
    db_session.add(rule)
    await db_session.commit()

    alice = await _make_user(db_session, "Alice")
    await db_session.commit()

    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": str(alice.id),
            "vendor": "Amazon",
            "total_minor": 4000000,
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "A",
                    "total_minor": 2000000,
                },
                {
                    "line_no": 2,
                    "kind": "item",
                    "description": "B",
                    "total_minor": 2000000,
                },
            ],
        },
        headers=_auth(alice.id),
    )
    assert resp.status_code == 201, resp.text
    expense_id = resp.json()["id"]

    row = (
        await db_session.execute(
            select(Expense).where(Expense.id == uuid.UUID(expense_id))
        )
    ).scalar_one()
    assert row.discount_source == DiscountSource.vendor_rule
    assert row.needs_review is True  # flagged immediately at creation (item 2)

    # Wire assignments so the ONLY remaining blocker is the GST/total
    # invariant, not "no assignments".
    line_items = resp.json()["line_items"]
    assign_resp = await client.put(
        f"{API}/expenses/{expense_id}/assignments",
        json={
            "assignments": [
                {"line_item_id": li["id"], "user_id": str(alice.id)}
                for li in line_items
            ]
        },
        headers=_auth(alice.id),
    )
    assert assign_resp.status_code == 200, assign_resp.text

    confirm_resp = await client.post(
        f"{API}/expenses/{expense_id}/confirm", headers=_auth(alice.id)
    )
    assert confirm_resp.status_code == 422, confirm_resp.text
    detail = confirm_resp.json()["detail"]
    assert "total_mismatch_with_discount" in detail
    assert "Share sum" not in detail


# ---------------------------------------------------------------------------
# 5. allocation-preview's unassigned_lines structured problem.
# ---------------------------------------------------------------------------


async def test_allocation_preview_unassigned_lines_structured(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": str(alice.id),
            "vendor": "V",
            "total_minor": 1000,
            "line_items": [
                {"line_no": 1, "kind": "item", "description": "A", "total_minor": 600},
                {"line_no": 2, "kind": "item", "description": "B", "total_minor": 400},
            ],
        },
        headers=_auth(alice.id),
    )
    assert resp.status_code == 201, resp.text
    expense_id = resp.json()["id"]
    line_items = resp.json()["line_items"]
    line1_id = next(li["id"] for li in line_items if li["line_no"] == 1)

    # Only assign line 1 -- line 2 stays unassigned.
    assign_resp = await client.put(
        f"{API}/expenses/{expense_id}/assignments",
        json={"assignments": [{"line_item_id": line1_id, "user_id": str(alice.id)}]},
        headers=_auth(alice.id),
    )
    assert assign_resp.status_code == 200, assign_resp.text

    preview_resp = await client.get(
        f"{API}/expenses/{expense_id}/allocation-preview", headers=_auth(alice.id)
    )
    assert preview_resp.status_code == 200, preview_resp.text
    body = preview_resp.json()
    assert body["confirmed"] is False
    assert body["members"] == []
    problems = body["problems"]
    assert len(problems) == 1
    problem = problems[0]
    assert problem["code"] == "unassigned_lines"
    assert problem["count"] == 1
    line2_id = next(li["id"] for li in line_items if li["line_no"] == 2)
    assert problem["line_ids"] == [line2_id]


async def test_allocation_preview_no_unassigned_lines_no_problem(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": str(alice.id),
            "vendor": "V",
            "total_minor": 1000,
            "line_items": [
                {"line_no": 1, "kind": "item", "description": "A", "total_minor": 1000},
            ],
        },
        headers=_auth(alice.id),
    )
    assert resp.status_code == 201, resp.text
    expense_id = resp.json()["id"]
    line1_id = resp.json()["line_items"][0]["id"]

    assign_resp = await client.put(
        f"{API}/expenses/{expense_id}/assignments",
        json={"assignments": [{"line_item_id": line1_id, "user_id": str(alice.id)}]},
        headers=_auth(alice.id),
    )
    assert assign_resp.status_code == 200, assign_resp.text

    preview_resp = await client.get(
        f"{API}/expenses/{expense_id}/allocation-preview", headers=_auth(alice.id)
    )
    assert preview_resp.status_code == 200, preview_resp.text
    body = preview_resp.json()
    assert body["confirmed"] is False
    assert not any(p["code"] == "unassigned_lines" for p in body["problems"])
    assert len(body["members"]) == 1


# ---------------------------------------------------------------------------
# 6. API-shape smoke tests for the additive fields (item 5-7 API gaps).
# ---------------------------------------------------------------------------


async def test_expense_response_carries_gst_mode_tax_components_and_frozen_flag(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": str(alice.id),
            "vendor": "V",
            "total_minor": 1000,
            "shares": {str(alice.id): 1000},
        },
        headers=_auth(alice.id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["gst_mode"] == "none"
    assert body["tax_components"] == []
    assert body["is_frozen_shares"] is True  # M1 explicit-shares flow


async def test_line_item_response_carries_assignments(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": str(alice.id),
            "vendor": "V",
            "total_minor": 1000,
            "line_items": [
                {"line_no": 1, "kind": "item", "description": "A", "total_minor": 1000},
            ],
        },
        headers=_auth(alice.id),
    )
    assert resp.status_code == 201, resp.text
    expense_id = resp.json()["id"]
    line1_id = resp.json()["line_items"][0]["id"]
    assert resp.json()["is_frozen_shares"] is False  # item-level, no shares yet

    await client.put(
        f"{API}/expenses/{expense_id}/assignments",
        json={"assignments": [{"line_item_id": line1_id, "user_id": str(alice.id)}]},
        headers=_auth(alice.id),
    )

    get_resp = await client.get(f"{API}/expenses/{expense_id}", headers=_auth(alice.id))
    assert get_resp.status_code == 200
    line = get_resp.json()["line_items"][0]
    assert len(line["assignments"]) == 1
    assert line["assignments"][0]["user_id"] == str(alice.id)
    assert Decimal(line["assignments"][0]["weight"]) == Decimal("1")
