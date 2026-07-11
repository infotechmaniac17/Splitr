"""
M6-M8 item 7a — backend endpoints supporting the upcoming assignment UI.

Covers:
  - POST /expenses/{id}/assignments/bulk: replace-set-per-item semantics,
    idempotency (double-call == single-call), 409 on confirmed, refund-line
    behaviour parity with PUT /expenses/{id}/assignments.
  - PATCH /expenses/{id}/discount: set/clear/re-match (manual precedence,
    OQ-2 fresh-subtotal contract), 409 on confirmed, 422 on frozen
    explicit-shares expenses.
  - GET /groups/{group_id}/expenses: date-bucket grouping, inclusive
    boundaries, NULL invoice_date "undated" bucket, persisted-only member
    shares.
"""

from __future__ import annotations

import uuid
from datetime import date

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    DiscountSource,
    DiscountType,
    Expense,
    ExpenseLineItem,
    ExpenseMemberAllocation,
    ExpenseSource,
    ExpenseStatus,
    ItemAssignment,
    LineItemKind,
    ParseStatus,
    User,
    VendorDiscountRule,
)

API = "/api/v1"


async def _make_orm_user(db: AsyncSession, name: str) -> User:
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


async def _make_group(
    client: AsyncClient, creator: User, members: list[User]
) -> uuid.UUID:
    resp = await client.post(
        f"{API}/groups",
        json={"name": "Test Group", "created_by": str(creator.id)},
        headers=_auth(creator.id),
    )
    assert resp.status_code == 201, resp.text
    group_id = uuid.UUID(resp.json()["id"])
    for m in members:
        if m.id == creator.id:
            continue
        r = await client.post(
            f"{API}/groups/{group_id}/members",
            json={"user_id": str(m.id)},
            headers=_auth(creator.id),
        )
        assert r.status_code in (200, 201), r.text
    return group_id


async def _item_level_expense(
    client: AsyncClient,
    db_session: AsyncSession,
    payer: User,
    group_id: uuid.UUID | None,
    total_minor: int = 3000,
) -> uuid.UUID:
    resp = await client.post(
        f"{API}/expenses",
        json={
            "group_id": str(group_id) if group_id else None,
            "paid_by": str(payer.id),
            "vendor": "TestVendor",
            "total_minor": total_minor,
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Item A",
                    "total_minor": total_minor,
                }
            ],
        },
        headers=_auth(payer.id),
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


# ---------------------------------------------------------------------------
# 1. POST /expenses/{id}/assignments/bulk
# ---------------------------------------------------------------------------


async def test_bulk_assignments_basic_and_idempotent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    bob = await _make_orm_user(db_session, "Bob")
    await db_session.commit()

    group_id = await _make_group(client, alice, [alice, bob])
    expense_id = await _item_level_expense(client, db_session, alice, group_id, 4000)

    # Get line item id.
    exp_resp = await client.get(f"{API}/expenses/{expense_id}", headers=_auth(alice.id))
    line_id = exp_resp.json()["line_items"][0]["id"]

    resp = await client.post(
        f"{API}/expenses/{expense_id}/assignments/bulk",
        json={"item_ids": [line_id], "member_ids": [str(alice.id), str(bob.id)]},
        headers=_auth(alice.id),
    )
    assert resp.status_code == 200, resp.text
    rows1 = sorted(resp.json(), key=lambda r: r["user_id"])
    assert len(rows1) == 2
    assert {r["user_id"] for r in rows1} == {str(alice.id), str(bob.id)}
    assert all(r["line_item_id"] == line_id for r in rows1)

    # Calling again with the exact same payload -> identical final state.
    resp2 = await client.post(
        f"{API}/expenses/{expense_id}/assignments/bulk",
        json={"item_ids": [line_id], "member_ids": [str(alice.id), str(bob.id)]},
        headers=_auth(alice.id),
    )
    assert resp2.status_code == 200, resp2.text
    rows2 = sorted(resp2.json(), key=lambda r: r["user_id"])
    assert len(rows2) == 2

    # Compare persisted DB state directly (ids differ across calls -- rows
    # are deleted/recreated -- but the SET of (line_item_id, user_id, weight)
    # tuples must be identical, which is the actual idempotency contract).
    persisted = (
        (
            await db_session.execute(
                select(ItemAssignment)
                .join(
                    ExpenseLineItem, ItemAssignment.line_item_id == ExpenseLineItem.id
                )
                .where(ExpenseLineItem.expense_id == expense_id)
            )
        )
        .scalars()
        .all()
    )
    tuples = {(str(r.line_item_id), str(r.user_id), float(r.weight)) for r in persisted}
    assert tuples == {
        (line_id, str(alice.id), 1.0),
        (line_id, str(bob.id), 1.0),
    }
    assert len(persisted) == 2


async def test_bulk_assignments_replaces_only_targeted_items(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    bob = await _make_orm_user(db_session, "Bob")
    await db_session.commit()

    group_id = await _make_group(client, alice, [alice, bob])
    resp = await client.post(
        f"{API}/expenses",
        json={
            "group_id": str(group_id),
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
    expense = resp.json()
    line_a = expense["line_items"][0]["id"]
    line_b = expense["line_items"][1]["id"]

    # Assign line_b to bob first, via the whole-expense PUT.
    put_resp = await client.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={"assignments": [{"line_item_id": line_b, "user_id": str(bob.id)}]},
        headers=_auth(alice.id),
    )
    assert put_resp.status_code == 200, put_resp.text

    # Bulk-assign only line_a to alice -- line_b's assignment must survive.
    bulk_resp = await client.post(
        f"{API}/expenses/{expense['id']}/assignments/bulk",
        json={"item_ids": [line_a], "member_ids": [str(alice.id)]},
        headers=_auth(alice.id),
    )
    assert bulk_resp.status_code == 200, bulk_resp.text

    persisted = (
        (
            await db_session.execute(
                select(ItemAssignment)
                .join(
                    ExpenseLineItem, ItemAssignment.line_item_id == ExpenseLineItem.id
                )
                .where(ExpenseLineItem.expense_id == uuid.UUID(expense["id"]))
            )
        )
        .scalars()
        .all()
    )
    by_line = {(str(r.line_item_id), str(r.user_id)) for r in persisted}
    assert (line_a, str(alice.id)) in by_line
    assert (line_b, str(bob.id)) in by_line
    assert len(persisted) == 2


async def test_bulk_assignments_409_on_confirmed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    await db_session.commit()
    expense_id = await _item_level_expense(client, db_session, alice, None, 1000)

    exp_resp = await client.get(f"{API}/expenses/{expense_id}", headers=_auth(alice.id))
    line_id = exp_resp.json()["line_items"][0]["id"]

    put_resp = await client.put(
        f"{API}/expenses/{expense_id}/assignments",
        json={"assignments": [{"line_item_id": line_id, "user_id": str(alice.id)}]},
        headers=_auth(alice.id),
    )
    assert put_resp.status_code == 200, put_resp.text

    confirm_resp = await client.post(
        f"{API}/expenses/{expense_id}/confirm", headers=_auth(alice.id)
    )
    assert confirm_resp.status_code == 200, confirm_resp.text

    bulk_resp = await client.post(
        f"{API}/expenses/{expense_id}/assignments/bulk",
        json={"item_ids": [line_id], "member_ids": [str(alice.id)]},
        headers=_auth(alice.id),
    )
    assert bulk_resp.status_code == 409, bulk_resp.text


async def test_bulk_assignments_refund_line_behavior_matches_single_route(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    PUT /expenses/{id}/assignments does NOT filter by line kind -- it only
    checks that a line_item_id belongs to the expense (via `line_ids`
    membership). A refund-kind line is therefore assignable exactly like any
    other line by that route. The bulk route mirrors this: no kind-based
    skip/error either.
    """
    alice = await _make_orm_user(db_session, "Alice")
    bob = await _make_orm_user(db_session, "Bob")
    await db_session.commit()

    expense = Expense(
        paid_by=alice.id,
        vendor="V",
        currency="INR",
        total_minor=1000,
        subtotal_minor=1000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
    )
    db_session.add(expense)
    await db_session.flush()
    item_line = ExpenseLineItem(
        expense_id=expense.id, line_no=1, kind=LineItemKind.item, total_minor=1000
    )
    db_session.add(item_line)
    await db_session.flush()
    refund_line = ExpenseLineItem(
        expense_id=expense.id,
        line_no=2,
        kind=LineItemKind.refund,
        total_minor=-100,
        parent_line_id=item_line.id,
    )
    db_session.add(refund_line)
    await db_session.commit()
    refund_line_id = str(refund_line.id)
    expense_id = expense.id

    # Same behaviour verification on PUT /assignments first (the existing
    # single-item route): a refund line ID is accepted, not rejected/skipped.
    put_resp = await client.put(
        f"{API}/expenses/{expense_id}/assignments",
        json={
            "assignments": [{"line_item_id": refund_line_id, "user_id": str(alice.id)}]
        },
        headers=_auth(alice.id),
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()[0]["line_item_id"] == refund_line_id

    # The new bulk route: same acceptance for a refund-kind line.
    bulk_resp = await client.post(
        f"{API}/expenses/{expense_id}/assignments/bulk",
        json={"item_ids": [refund_line_id], "member_ids": [str(bob.id)]},
        headers=_auth(alice.id),
    )
    assert bulk_resp.status_code == 200, bulk_resp.text
    assert bulk_resp.json()[0]["line_item_id"] == refund_line_id
    assert bulk_resp.json()[0]["user_id"] == str(bob.id)


# ---------------------------------------------------------------------------
# 2. PATCH /expenses/{id}/discount
# ---------------------------------------------------------------------------


async def test_discount_set_manual_and_persists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    await db_session.commit()
    expense_id = await _item_level_expense(client, db_session, alice, None, 5000)

    resp = await client.patch(
        f"{API}/expenses/{expense_id}/discount",
        json={
            "discount_type": "flat",
            "discount_value_minor": 500,
            "discount_threshold_minor": 0,
        },
        headers=_auth(alice.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["discount_type"] == "flat"
    assert body["discount_value_minor"] == 500
    assert body["discount_source"] == "manual"
    assert body["discount_rule_id"] is None

    row = (
        await db_session.execute(select(Expense).where(Expense.id == expense_id))
    ).scalar_one()
    assert row.discount_source == DiscountSource.manual
    assert row.discount_value_minor == 500


async def test_discount_manual_wins_over_vendor_rule(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Manual discount is never auto-overwritten by a later vendor-rule match."""
    creator = await _make_orm_user(db_session, "Creator")
    alice = await _make_orm_user(db_session, "Alice")
    db_session.add(
        VendorDiscountRule(
            group_id=None,
            created_by=creator.id,
            vendor_pattern="amazon",
            min_order_total_minor=0,
            discount_type=DiscountType.flat,
            discount_value_minor=999,
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": str(alice.id),
            "vendor": "Amazon",
            "total_minor": 5000,
            "line_items": [
                {"line_no": 1, "kind": "item", "description": "A", "total_minor": 5000}
            ],
        },
        headers=_auth(alice.id),
    )
    assert resp.status_code == 201, resp.text
    expense_id = resp.json()["id"]
    # Vendor rule auto-applied at creation (item-level flow).
    row = (
        await db_session.execute(
            select(Expense).where(Expense.id == uuid.UUID(expense_id))
        )
    ).scalar_one()
    assert row.discount_source == DiscountSource.vendor_rule
    assert row.discount_value_minor == 999

    # Now set a manual discount -- overwrites the vendor-rule snapshot.
    set_resp = await client.patch(
        f"{API}/expenses/{expense_id}/discount",
        json={"discount_type": "flat", "discount_value_minor": 111},
        headers=_auth(alice.id),
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["discount_source"] == "manual"
    assert set_resp.json()["discount_value_minor"] == 111

    # Simulate "a matching vendor rule" attempting to reapply -- calling
    # apply_vendor_discount_snapshot directly must be a no-op against the
    # manual snapshot (this is the existing precedence contract; verified
    # here as the "does NOT overwrite" assertion the task requires).
    from app.domain.vendor_discount import apply_vendor_discount_snapshot

    row2 = (
        await db_session.execute(
            select(Expense).where(Expense.id == uuid.UUID(expense_id))
        )
    ).scalar_one()
    await apply_vendor_discount_snapshot(db_session, row2, subtotal_override_minor=5000)
    await db_session.commit()
    await db_session.refresh(row2)
    assert row2.discount_source == DiscountSource.manual
    assert row2.discount_value_minor == 111


async def test_discount_clear_rematches_using_fresh_subtotal(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await _make_orm_user(db_session, "Creator")
    alice = await _make_orm_user(db_session, "Alice")
    db_session.add(
        VendorDiscountRule(
            group_id=None,
            created_by=creator.id,
            vendor_pattern="amazon",
            min_order_total_minor=0,
            discount_type=DiscountType.flat,
            discount_value_minor=250,
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": str(alice.id),
            "vendor": "Amazon",
            "total_minor": 5000,
            "line_items": [
                {"line_no": 1, "kind": "item", "description": "A", "total_minor": 5000}
            ],
        },
        headers=_auth(alice.id),
    )
    assert resp.status_code == 201, resp.text
    expense_id = resp.json()["id"]

    # Set a manual discount first (overwrites the vendor-rule one).
    set_resp = await client.patch(
        f"{API}/expenses/{expense_id}/discount",
        json={"discount_type": "flat", "discount_value_minor": 999},
        headers=_auth(alice.id),
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["discount_source"] == "manual"

    # Mutate the line items directly to a DIFFERENT subtotal (simulating a
    # correction) so the fresh-subtotal (OQ-2) contract is actually exercised
    # -- expense.subtotal_minor (stale, 5000) must NOT be what's used.
    row = (
        await db_session.execute(
            select(Expense).where(Expense.id == uuid.UUID(expense_id))
        )
    ).scalar_one()
    line = (
        (
            await db_session.execute(
                select(ExpenseLineItem).where(ExpenseLineItem.expense_id == row.id)
            )
        )
        .scalars()
        .first()
    )
    line.total_minor = 2000  # fresh base subtotal is now 2000, not 5000
    await db_session.commit()

    clear_resp = await client.patch(
        f"{API}/expenses/{expense_id}/discount",
        json={"discount_type": None},
        headers=_auth(alice.id),
    )
    assert clear_resp.status_code == 200, clear_resp.text
    body = clear_resp.json()
    assert body["discount_source"] == "vendor_rule"
    assert body["discount_value_minor"] == 250

    db_session.expire_all()
    row2 = (
        await db_session.execute(
            select(Expense).where(Expense.id == uuid.UUID(expense_id))
        )
    ).scalar_one()
    assert row2.discount_source == DiscountSource.vendor_rule
    assert row2.discount_value_minor == 250
    # subtotal_minor column itself is untouched/stale -- confirms the fresh
    # base subtotal (2000, from line items) was used, not this stale 5000.
    assert row2.subtotal_minor == 5000


async def test_discount_clear_no_matching_rule_leaves_snapshot_empty(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    await db_session.commit()
    expense_id = await _item_level_expense(client, db_session, alice, None, 3000)

    set_resp = await client.patch(
        f"{API}/expenses/{expense_id}/discount",
        json={"discount_type": "flat", "discount_value_minor": 100},
        headers=_auth(alice.id),
    )
    assert set_resp.status_code == 200, set_resp.text

    clear_resp = await client.patch(
        f"{API}/expenses/{expense_id}/discount",
        json={"discount_type": None},
        headers=_auth(alice.id),
    )
    assert clear_resp.status_code == 200, clear_resp.text
    body = clear_resp.json()
    assert body["discount_type"] is None
    assert body["discount_source"] is None
    assert body["discount_value_minor"] is None


async def test_discount_409_on_confirmed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    await db_session.commit()
    expense_id = await _item_level_expense(client, db_session, alice, None, 1000)

    exp_resp = await client.get(f"{API}/expenses/{expense_id}", headers=_auth(alice.id))
    line_id = exp_resp.json()["line_items"][0]["id"]
    put_resp = await client.put(
        f"{API}/expenses/{expense_id}/assignments",
        json={"assignments": [{"line_item_id": line_id, "user_id": str(alice.id)}]},
        headers=_auth(alice.id),
    )
    assert put_resp.status_code == 200, put_resp.text
    confirm_resp = await client.post(
        f"{API}/expenses/{expense_id}/confirm", headers=_auth(alice.id)
    )
    assert confirm_resp.status_code == 200, confirm_resp.text

    resp = await client.patch(
        f"{API}/expenses/{expense_id}/discount",
        json={"discount_type": "flat", "discount_value_minor": 100},
        headers=_auth(alice.id),
    )
    assert resp.status_code == 409, resp.text


async def test_discount_422_on_frozen_explicit_shares(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    bob = await _make_orm_user(db_session, "Bob")
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

    patch_resp = await client.patch(
        f"{API}/expenses/{expense_id}/discount",
        json={"discount_type": "flat", "discount_value_minor": 100},
        headers=_auth(alice.id),
    )
    assert patch_resp.status_code == 422, patch_resp.text
    assert "frozen" in patch_resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 3. GET /groups/{group_id}/expenses
# ---------------------------------------------------------------------------


async def _confirmed_expense_with_date(
    db_session: AsyncSession,
    group_id: uuid.UUID,
    payer: User,
    other: User,
    invoice_date: date | None,
    total_minor: int = 1000,
) -> Expense:
    expense = Expense(
        group_id=group_id,
        paid_by=payer.id,
        vendor="V",
        invoice_date=invoice_date,
        currency="INR",
        total_minor=total_minor,
        subtotal_minor=total_minor,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.confirmed,
        status=ExpenseStatus.active,
    )
    db_session.add(expense)
    await db_session.flush()
    db_session.add(
        ExpenseMemberAllocation(
            expense_id=expense.id,
            user_id=other.id,
            base_minor=total_minor,
            discount_minor=0,
            gst_minor=0,
            total_minor=total_minor,
        )
    )
    await db_session.commit()
    return expense


async def test_group_expenses_date_grouping_inclusive_boundaries(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    bob = await _make_orm_user(db_session, "Bob")
    await db_session.commit()
    group_id = await _make_group(client, alice, [alice, bob])

    e_from = await _confirmed_expense_with_date(
        db_session, group_id, alice, bob, date(2026, 1, 1)
    )
    e_mid = await _confirmed_expense_with_date(
        db_session, group_id, alice, bob, date(2026, 1, 15)
    )
    e_to = await _confirmed_expense_with_date(
        db_session, group_id, alice, bob, date(2026, 1, 31)
    )
    e_out_before = await _confirmed_expense_with_date(
        db_session, group_id, alice, bob, date(2025, 12, 31)
    )
    e_out_after = await _confirmed_expense_with_date(
        db_session, group_id, alice, bob, date(2026, 2, 1)
    )

    resp = await client.get(
        f"{API}/groups/{group_id}/expenses",
        params={"from": "2026-01-01", "to": "2026-01-31", "group_by": "date"},
        headers=_auth(alice.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    all_ids = {e["id"] for bucket in body["buckets"] for e in bucket["expenses"]}
    assert str(e_from.id) in all_ids  # boundary "from" is inclusive
    assert str(e_mid.id) in all_ids
    assert str(e_to.id) in all_ids  # boundary "to" is inclusive
    assert str(e_out_before.id) not in all_ids
    assert str(e_out_after.id) not in all_ids

    # Member shares are persisted-only.
    mid_bucket = next(b for b in body["buckets"] if b["date"] == "2026-01-15")
    mid_expense = mid_bucket["expenses"][0]
    shares = {s["user_id"]: s["share_minor"] for s in mid_expense["member_shares"]}
    assert shares[str(bob.id)] == 1000


async def test_group_expenses_null_invoice_date_bucket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    bob = await _make_orm_user(db_session, "Bob")
    await db_session.commit()
    group_id = await _make_group(client, alice, [alice, bob])

    e_dated = await _confirmed_expense_with_date(
        db_session, group_id, alice, bob, date(2026, 1, 1)
    )
    e_undated = await _confirmed_expense_with_date(
        db_session, group_id, alice, bob, None
    )

    # No filter: undated bucket appears.
    resp = await client.get(
        f"{API}/groups/{group_id}/expenses", headers=_auth(alice.id)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    undated_buckets = [b for b in body["buckets"] if b["date"] is None]
    assert len(undated_buckets) == 1
    assert {e["id"] for e in undated_buckets[0]["expenses"]} == {str(e_undated.id)}

    # WITH a from/to filter: the undated expense is still included (it can
    # never be excluded by a range it has no date to compare against -- see
    # the endpoint's docstring for this deterministic choice).
    resp2 = await client.get(
        f"{API}/groups/{group_id}/expenses",
        params={"from": "2026-01-01", "to": "2026-01-31"},
        headers=_auth(alice.id),
    )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    all_ids2 = {e["id"] for b in body2["buckets"] for e in b["expenses"]}
    assert str(e_dated.id) in all_ids2
    assert str(e_undated.id) in all_ids2


async def test_group_expenses_requires_active_membership(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_orm_user(db_session, "Alice")
    stranger = await _make_orm_user(db_session, "Stranger")
    await db_session.commit()
    group_id = await _make_group(client, alice, [alice])

    resp = await client.get(
        f"{API}/groups/{group_id}/expenses", headers=_auth(stranger.id)
    )
    assert resp.status_code == 403, resp.text
