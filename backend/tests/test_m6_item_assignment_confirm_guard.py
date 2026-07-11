"""
M6 item 1: DB-level guard preventing mutation of item_assignments once the
parent expense's parse_status = 'confirmed'.

Covers:
  - raw SQL INSERT/UPDATE/DELETE against a confirmed expense's assignments
    -> rejected by the Postgres trigger (trg_item_assignment_confirm_guard).
  - the same operations via the SQLAlchemy ORM -> rejected identically
    (proves the trigger fires regardless of access path, not just an
    ORM-layer guard).
  - a draft (non-confirmed) expense's assignments remain fully mutable
    (regression negative test -- the trigger must not over-block).
  - the confirm flow itself (POST /expenses/{id}/confirm) still succeeds
    end-to-end -- the trigger must not block the confirm transaction's own
    writes (freezing share_minor / inserting audit assignment rows) that
    happen in the same transaction as flipping parse_status to 'confirmed'.

All @pytest.mark.postgres -- these exercise a real Postgres trigger and are
skipped on the default SQLite tier (SQLite cannot execute trigger functions).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    Expense,
    ExpenseLineItem,
    ExpenseSource,
    ExpenseStatus,
    Group,
    GroupMember,
    GroupMemberRole,
    ItemAssignment,
    LineItemKind,
    ParseStatus,
    User,
)

# ---------------------------------------------------------------------------
# Shared helpers (mirror tests/test_hardening.py)
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, name: str, email: str | None = None) -> User:
    user = User(
        name=name, email=email or f"{name.lower()}_{uuid.uuid4().hex[:6]}@test.com"
    )
    db.add(user)
    await db.flush()
    return user


async def _make_group(db: AsyncSession, creator: User) -> Group:
    group = Group(name="M6 Guard Test Group", created_by=creator.id)
    db.add(group)
    await db.flush()
    db.add(
        GroupMember(group_id=group.id, user_id=creator.id, role=GroupMemberRole.admin)
    )
    await db.flush()
    return group


async def _add_member(db: AsyncSession, group: Group, user: User) -> None:
    db.add(GroupMember(group_id=group.id, user_id=user.id, role=GroupMemberRole.member))
    await db.flush()


async def _make_expense(
    db: AsyncSession,
    group: Group | None,
    payer: User,
    total: int,
    parse_status: ParseStatus = ParseStatus.parsed,
) -> Expense:
    expense = Expense(
        group_id=group.id if group else None,
        paid_by=payer.id,
        vendor="Test",
        currency="INR",
        total_minor=total,
        source=ExpenseSource.manual,
        parse_status=parse_status,
        status=ExpenseStatus.active,
    )
    db.add(expense)
    await db.flush()
    return expense


async def _make_line_item(
    db: AsyncSession, expense: Expense, total: int, line_no: int = 1
) -> ExpenseLineItem:
    li = ExpenseLineItem(
        expense_id=expense.id,
        line_no=line_no,
        kind=LineItemKind.item,
        description="Test item",
        quantity=1,
        total_minor=total,
    )
    db.add(li)
    await db.flush()
    return li


async def _assign(
    db: AsyncSession, line_item: ExpenseLineItem, user: User, share: int | None
) -> ItemAssignment:
    a = ItemAssignment(
        line_item_id=line_item.id,
        user_id=user.id,
        weight=1,
        share_minor=share,
    )
    db.add(a)
    await db.flush()
    return a


# ---------------------------------------------------------------------------
# 1. Raw SQL against a confirmed expense's assignments -> rejected
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_raw_sql_update_blocked_on_confirmed_expense_assignment(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    group = await _make_group(db_session, alice)
    await _add_member(db_session, group, bob)
    expense = await _make_expense(db_session, group, alice, 1000)
    li = await _make_line_item(db_session, expense, 1000)
    assignment = await _assign(db_session, li, bob, 1000)
    await db_session.commit()

    # Confirm the expense in a separate, already-committed transaction.
    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("UPDATE item_assignments SET share_minor = 500 WHERE id = :id"),
            {"id": str(assignment.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_raw_sql_insert_blocked_on_confirmed_expense_assignment(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    carol = await _make_user(db_session, "Carol")
    group = await _make_group(db_session, alice)
    await _add_member(db_session, group, bob)
    await _add_member(db_session, group, carol)
    expense = await _make_expense(db_session, group, alice, 1000)
    li = await _make_line_item(db_session, expense, 1000)
    await _assign(db_session, li, bob, 1000)
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text(
                "INSERT INTO item_assignments "
                "(id, line_item_id, user_id, weight, share_minor) "
                "VALUES (:id, :line_item_id, :user_id, 1, 0)"
            ),
            {
                "id": str(uuid.uuid4()),
                "line_item_id": str(li.id),
                "user_id": str(carol.id),
            },
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_raw_sql_delete_blocked_on_confirmed_expense_assignment(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    group = await _make_group(db_session, alice)
    await _add_member(db_session, group, bob)
    expense = await _make_expense(db_session, group, alice, 1000)
    li = await _make_line_item(db_session, expense, 1000)
    assignment = await _assign(db_session, li, bob, 1000)
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("DELETE FROM item_assignments WHERE id = :id"),
            {"id": str(assignment.id)},
        )
        await db_session.flush()


# ---------------------------------------------------------------------------
# 2. Same operations via the SQLAlchemy ORM -> rejected identically (proves
#    the trigger fires regardless of access path, not just an ORM guard).
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_orm_update_blocked_on_confirmed_expense_assignment(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    group = await _make_group(db_session, alice)
    await _add_member(db_session, group, bob)
    expense = await _make_expense(db_session, group, alice, 1000)
    li = await _make_line_item(db_session, expense, 1000)
    assignment = await _assign(db_session, li, bob, 1000)
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.update(ItemAssignment)
            .where(ItemAssignment.id == assignment.id)
            .values(share_minor=1)
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_orm_insert_blocked_on_confirmed_expense_assignment(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    carol = await _make_user(db_session, "Carol")
    group = await _make_group(db_session, alice)
    await _add_member(db_session, group, bob)
    await _add_member(db_session, group, carol)
    expense = await _make_expense(db_session, group, alice, 1000)
    li = await _make_line_item(db_session, expense, 1000)
    await _assign(db_session, li, bob, 1000)
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    db_session.add(ItemAssignment(line_item_id=li.id, user_id=carol.id, weight=1))
    with pytest.raises(DBAPIError):
        await db_session.flush()


@pytest.mark.postgres
async def test_orm_delete_blocked_on_confirmed_expense_assignment(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    group = await _make_group(db_session, alice)
    await _add_member(db_session, group, bob)
    expense = await _make_expense(db_session, group, alice, 1000)
    li = await _make_line_item(db_session, expense, 1000)
    assignment = await _assign(db_session, li, bob, 1000)
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    await db_session.delete(assignment)
    with pytest.raises(DBAPIError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# 3. Draft (non-confirmed) expense assignments remain fully mutable
#    (regression negative test).
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_draft_expense_assignments_remain_fully_mutable(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    carol = await _make_user(db_session, "Carol")
    group = await _make_group(db_session, alice)
    await _add_member(db_session, group, bob)
    await _add_member(db_session, group, carol)
    # parse_status='parsed' (NOT confirmed).
    expense = await _make_expense(db_session, group, alice, 1000)
    li = await _make_line_item(db_session, expense, 1000)
    assignment = await _assign(db_session, li, bob, 1000)
    await db_session.commit()

    # UPDATE succeeds.
    await db_session.execute(
        sa.update(ItemAssignment)
        .where(ItemAssignment.id == assignment.id)
        .values(share_minor=400)
    )
    await db_session.commit()

    # INSERT succeeds.
    new_assignment = ItemAssignment(
        line_item_id=li.id, user_id=carol.id, weight=1, share_minor=600
    )
    db_session.add(new_assignment)
    await db_session.commit()

    # DELETE succeeds.
    await db_session.delete(new_assignment)
    await db_session.commit()

    reloaded = await db_session.get(ItemAssignment, assignment.id)
    assert reloaded is not None
    assert reloaded.share_minor == 400


# ---------------------------------------------------------------------------
# 4. Confirm flow itself still works end-to-end -- the trigger must not
#    block the confirm transaction's own reads/writes leading up to (and
#    including finalizing children after) setting status='confirmed'.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_confirm_flow_still_succeeds_with_guard_installed(
    client: AsyncClient,
) -> None:
    """
    End-to-end: create a group expense with item-level assignments, confirm
    it via the API, and verify the frozen share_minor audit rows were written
    -- proving the trg_item_assignment_confirm_guard trigger does not block
    the confirm transaction's own writes to item_assignments (which happen
    in the same transaction as flipping parse_status to 'confirmed').
    """
    r_alice = await client.post(
        "/api/v1/users", json={"name": "Alice", "email": "alice_m6g@test.com"}
    )
    r_bob = await client.post(
        "/api/v1/users", json={"name": "Bob", "email": "bob_m6g@test.com"}
    )
    alice = r_alice.json()
    bob = r_bob.json()

    r_group = await client.post(
        "/api/v1/groups", json={"name": "M6 Guard Group", "created_by": alice["id"]}
    )
    group = r_group.json()
    await client.post(
        f"/api/v1/groups/{group['id']}/members", json={"user_id": bob["id"]}
    )

    r_exp = await client.post(
        "/api/v1/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["id"],
            "total_minor": 1000,
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Shared item",
                    "quantity": "1",
                    "total_minor": 1000,
                }
            ],
        },
    )
    assert r_exp.status_code == 201, r_exp.text
    expense = r_exp.json()
    line_item_id = expense["line_items"][0]["id"]

    # Assign the whole line to Bob before confirming.
    r_assign = await client.put(
        f"/api/v1/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": line_item_id, "user_id": bob["id"], "weight": 1}
            ]
        },
    )
    assert r_assign.status_code == 200, r_assign.text

    r_confirm = await client.post(f"/api/v1/expenses/{expense['id']}/confirm")
    assert r_confirm.status_code == 200, r_confirm.text

    # The confirm flow must have frozen share_minor = 1000 on Bob's
    # assignment row -- this write happens in item_assignments, in the SAME
    # transaction that set expenses.parse_status = 'confirmed', and must not
    # have been rejected by the trigger.
    r_shares = await client.get(f"/api/v1/expenses/{expense['id']}/shares")
    assert r_shares.status_code == 200, r_shares.text
    shares = r_shares.json()["shares"]
    assert shares[bob["id"]] == 1000, shares

    balances_resp = await client.get(f"/api/v1/groups/{group['id']}/balances")
    balances = balances_resp.json()["balances"]
    assert len(balances) == 1
    assert balances[0]["net_amount_minor"] == 1000


@pytest.mark.postgres
async def test_confirm_flow_second_idempotent_call_also_succeeds(
    client: AsyncClient,
) -> None:
    """
    Re-confirming an already-confirmed expense (idempotent path, no ledger
    re-post) must also succeed -- it performs a read-only early return before
    touching item_assignments at all, so the guard trigger is never even
    consulted on this path, but we verify it doesn't regress either.
    """
    r_alice = await client.post(
        "/api/v1/users", json={"name": "Alice", "email": "alice_m6g2@test.com"}
    )
    alice = r_alice.json()

    r_exp = await client.post(
        "/api/v1/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 500,
            "participants": [alice["id"]],
        },
    )
    assert r_exp.status_code == 201
    expense = r_exp.json()

    r_confirm1 = await client.post(f"/api/v1/expenses/{expense['id']}/confirm")
    assert r_confirm1.status_code == 200

    r_confirm2 = await client.post(f"/api/v1/expenses/{expense['id']}/confirm")
    assert r_confirm2.status_code == 200


# ---------------------------------------------------------------------------
# 5. The refund-INSERT escape hatch cannot be abused to alter historical
#    splits: inserting refund-kind assignment rows must leave the ORIGINAL
#    assignment rows byte-identical, and shift the group balance by exactly
#    the refund amount -- nothing else.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_refund_insert_does_not_alter_original_assignments_or_balance(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    Confirm a 1000-minor expense split evenly (weight 1/1) between Alice
    (payer) and Bob, then post a 200-minor refund on that line. The refund
    exercises the guard's "INSERT onto a refund line" escape hatch. Assert:
      - the ORIGINAL (pre-refund) item_assignments rows -- fetched directly
        from the DB by id, not through the aggregate /shares endpoint
        (which correctly nets in the new refund row and is NOT what we're
        testing here) -- are byte-identical (id, share_minor unchanged)
        after the refund's INSERT: the escape hatch only appends a new row
        on the new refund line, it does not let the refund touch history.
      - the group balance shifts by EXACTLY the refund's ratio-share
        (nothing else): before the refund Bob owes Alice 500; a 200 refund
        split evenly (1/1 weight) reduces that debt by exactly 100.
    """
    r_alice = await client.post(
        "/api/v1/users", json={"name": "Alice", "email": "alice_refund@test.com"}
    )
    r_bob = await client.post(
        "/api/v1/users", json={"name": "Bob", "email": "bob_refund@test.com"}
    )
    alice = r_alice.json()
    bob = r_bob.json()

    r_group = await client.post(
        "/api/v1/groups", json={"name": "Refund Guard Group", "created_by": alice["id"]}
    )
    group = r_group.json()
    await client.post(
        f"/api/v1/groups/{group['id']}/members", json={"user_id": bob["id"]}
    )

    r_exp = await client.post(
        "/api/v1/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["id"],
            "total_minor": 1000,
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Shared item",
                    "quantity": "1",
                    "total_minor": 1000,
                }
            ],
        },
    )
    assert r_exp.status_code == 201, r_exp.text
    expense = r_exp.json()
    line_item_id = uuid.UUID(expense["line_items"][0]["id"])

    r_assign = await client.put(
        f"/api/v1/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {
                    "line_item_id": str(line_item_id),
                    "user_id": alice["id"],
                    "weight": 1,
                },
                {"line_item_id": str(line_item_id), "user_id": bob["id"], "weight": 1},
            ]
        },
    )
    assert r_assign.status_code == 200, r_assign.text

    r_confirm = await client.post(f"/api/v1/expenses/{expense['id']}/confirm")
    assert r_confirm.status_code == 200, r_confirm.text

    balances_before = (
        await client.get(f"/api/v1/groups/{group['id']}/balances")
    ).json()["balances"]
    assert len(balances_before) == 1
    assert balances_before[0]["net_amount_minor"] == 500  # Bob owes Alice 500

    # Snapshot the ORIGINAL (pre-refund) assignment rows directly from the DB
    # -- (id, share_minor) pairs on the original line item only.
    original_rows_before = (
        await db_session.execute(
            sa.select(ItemAssignment.id, ItemAssignment.share_minor)
            .where(ItemAssignment.line_item_id == line_item_id)
            .order_by(ItemAssignment.id)
        )
    ).all()
    assert len(original_rows_before) == 2
    assert {row.share_minor for row in original_rows_before} == {500}

    # Post a 200-minor refund on the (only) original line item -- this
    # triggers the guard's refund-INSERT escape hatch on the CONFIRMED
    # expense (a brand-new ExpenseLineItem of kind='refund' plus new
    # item_assignments rows attached to it).
    r_refund = await client.post(
        f"/api/v1/expenses/{expense['id']}/refunds",
        json={"parent_line_id": str(line_item_id), "amount_minor": 200},
    )
    assert r_refund.status_code == 201, r_refund.text

    # The ORIGINAL assignment rows must be byte-identical (same ids, same
    # share_minor) -- the refund's INSERT must not have touched history.
    original_rows_after = (
        await db_session.execute(
            sa.select(ItemAssignment.id, ItemAssignment.share_minor)
            .where(ItemAssignment.line_item_id == line_item_id)
            .order_by(ItemAssignment.id)
        )
    ).all()
    assert original_rows_after == original_rows_before

    # Balance must shift by EXACTLY Bob's ratio-share of the refund (100 of
    # the 200 refunded, 1/1 weight split) -- nothing more, nothing less.
    balances_after = (
        await client.get(f"/api/v1/groups/{group['id']}/balances")
    ).json()["balances"]
    assert len(balances_after) == 1
    expected_after = balances_before[0]["net_amount_minor"] - 100
    assert balances_after[0]["net_amount_minor"] == expected_after == 400


# ---------------------------------------------------------------------------
# 6. parse_status state machine (folded-in re-audit fix): confirmed is
#    terminal, and only the derived legal transitions are permitted. See
#    migration 0006's docstring for the full transition-graph derivation.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_raw_sql_confirmed_to_draft_transition_rejected(
    db_session: AsyncSession,
) -> None:
    """
    FIXED (was a documented gap): raw UPDATE flipping parse_status from
    'confirmed' back to 'parsed' must now be rejected at the DB level.
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, 1000)
    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("UPDATE expenses SET parse_status = 'parsed' WHERE id = :id"),
            {"id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_orm_confirmed_to_draft_transition_rejected(
    db_session: AsyncSession,
) -> None:
    """Same as above, via the SQLAlchemy ORM update() construct."""
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, 1000)
    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.update(Expense)
            .where(Expense.id == expense.id)
            .values(parse_status=ParseStatus.queued.value)
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_confirmed_to_confirmed_noop_still_allowed(
    db_session: AsyncSession,
) -> None:
    """
    Setting parse_status = 'confirmed' again (no actual change, e.g. as a
    side effect of an unrelated UPDATE touching the same row) is NOT a
    transition and must not be rejected -- only genuine attempts to change
    away from 'confirmed' are illegal.
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, 1000)
    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    # status is a different column; parse_status stays 'confirmed' -> not a
    # transition, must succeed (this is also how voiding a confirmed expense
    # keeps working, per test_h3_trigger_allows_voiding_confirmed_expense in
    # tests/test_hardening.py).
    await db_session.execute(
        sa.update(Expense).where(Expense.id == expense.id).values(status="voided")
    )
    await db_session.commit()

    reloaded = await db_session.get(Expense, expense.id)
    assert reloaded is not None
    assert reloaded.parse_status == ParseStatus.confirmed


@pytest.mark.postgres
async def test_illegal_skip_transition_needs_review_to_confirmed_rejected(
    db_session: AsyncSession,
) -> None:
    """
    'needs_review' -> 'confirmed' skips the 'parsed' validation gate
    entirely and is not exercised by any current code path (the confirm
    endpoint itself independently requires parse_status == 'parsed' before
    it will even attempt this write) -- confirmed illegal via (a), must be
    rejected at the DB level too as defense in depth.
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(
        db_session, group, alice, 1000, parse_status=ParseStatus.needs_review
    )
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.update(Expense)
            .where(Expense.id == expense.id)
            .values(parse_status=ParseStatus.confirmed.value)
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_illegal_skip_transition_queued_to_confirmed_rejected(
    db_session: AsyncSession,
) -> None:
    """
    'queued' -> 'confirmed' directly (skipping both 'parsed' and any review
    step) is not exercised by any current code path either -- confirmed
    illegal via (a); a manual (non-upload) expense creation instead starts
    a brand-new row already at 'parsed' (an INSERT, not a transition).
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(
        db_session, group, alice, 1000, parse_status=ParseStatus.queued
    )
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.update(Expense)
            .where(Expense.id == expense.id)
            .values(parse_status=ParseStatus.confirmed.value)
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_legal_transitions_still_permitted(db_session: AsyncSession) -> None:
    """
    Sanity check that the state-machine trigger does not over-block: every
    transition in the derived legal graph still succeeds via raw UPDATE.
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)

    # queued -> parsed
    e1 = await _make_expense(
        db_session, group, alice, 100, parse_status=ParseStatus.queued
    )
    await db_session.commit()
    await db_session.execute(
        sa.update(Expense)
        .where(Expense.id == e1.id)
        .values(parse_status=ParseStatus.parsed.value)
    )
    await db_session.commit()

    # queued -> needs_review
    e2 = await _make_expense(
        db_session, group, alice, 100, parse_status=ParseStatus.queued
    )
    await db_session.commit()
    await db_session.execute(
        sa.update(Expense)
        .where(Expense.id == e2.id)
        .values(parse_status=ParseStatus.needs_review.value)
    )
    await db_session.commit()

    # needs_review -> parsed
    await db_session.execute(
        sa.update(Expense)
        .where(Expense.id == e2.id)
        .values(parse_status=ParseStatus.parsed.value)
    )
    await db_session.commit()

    # parsed -> confirmed
    await db_session.execute(
        sa.update(Expense)
        .where(Expense.id == e2.id)
        .values(parse_status=ParseStatus.confirmed.value)
    )
    await db_session.commit()

    reloaded = await db_session.get(Expense, e2.id)
    assert reloaded is not None
    assert reloaded.parse_status == ParseStatus.confirmed


@pytest.mark.postgres
async def test_confirm_flow_end_to_end_exercises_state_machine_trigger(
    client: AsyncClient,
) -> None:
    """
    End-to-end confirm (parsed -> confirmed, the one legal path into the
    terminal state) still works with the state-machine trigger installed --
    explicit state-machine-aware coverage beyond the pre-existing H1/C1
    confirm tests in tests/test_hardening.py, which predate this trigger.
    """
    r_alice = await client.post(
        "/api/v1/users", json={"name": "Alice", "email": "alice_sm@test.com"}
    )
    alice = r_alice.json()

    r_exp = await client.post(
        "/api/v1/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 500,
            "participants": [alice["id"]],
        },
    )
    assert r_exp.status_code == 201
    expense = r_exp.json()
    assert expense["parse_status"] == "parsed"

    r_confirm = await client.post(f"/api/v1/expenses/{expense['id']}/confirm")
    assert r_confirm.status_code == 200, r_confirm.text
    assert r_confirm.json()["parse_status"] == "confirmed"


# ---------------------------------------------------------------------------
# 7. 'failed' addendum: reserved queued<->failed transitions (migration
#    0007). 'failed' is documented in docs/ARCHITECTURE.md as a real,
#    intended state (Quick Manual Entry fallback / replay-after-fix), not
#    dead code -- kept in the enum/CHECK, and given explicit legal
#    transitions so it can't get INSERTed and then be permanently stuck.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_failed_can_still_be_set_at_insert_time(
    db_session: AsyncSession,
) -> None:
    """
    INSERT is unrestricted by the state-machine trigger (only UPDATE
    transitions are validated) -- a row can still be created directly with
    parse_status='failed', matching the pre-existing
    test_confirm_gate_rejects_non_parsed_statuses fixture pattern in
    tests/test_hardening.py.
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(
        db_session, group, alice, 1000, parse_status=ParseStatus.failed
    )
    await db_session.commit()

    reloaded = await db_session.get(Expense, expense.id)
    assert reloaded is not None
    assert reloaded.parse_status == ParseStatus.failed


@pytest.mark.postgres
async def test_failed_to_queued_retry_transition_permitted(
    db_session: AsyncSession,
) -> None:
    """
    failed -> queued (retry/replay against improved prompts/models, per
    ARCHITECTURE.md's pipeline rationale) is now an explicitly legal
    transition.
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(
        db_session, group, alice, 1000, parse_status=ParseStatus.failed
    )
    await db_session.commit()

    await db_session.execute(
        sa.update(Expense)
        .where(Expense.id == expense.id)
        .values(parse_status=ParseStatus.queued.value)
    )
    await db_session.commit()

    reloaded = await db_session.get(Expense, expense.id)
    assert reloaded is not None
    assert reloaded.parse_status == ParseStatus.queued


@pytest.mark.postgres
async def test_queued_to_failed_transition_permitted(
    db_session: AsyncSession,
) -> None:
    """
    queued -> failed (corrupted/unsupported PDF, per ARCHITECTURE.md's
    Quick Manual Entry fallback) is now an explicitly legal transition.
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(
        db_session, group, alice, 1000, parse_status=ParseStatus.queued
    )
    await db_session.commit()

    await db_session.execute(
        sa.update(Expense)
        .where(Expense.id == expense.id)
        .values(parse_status=ParseStatus.failed.value)
    )
    await db_session.commit()

    reloaded = await db_session.get(Expense, expense.id)
    assert reloaded is not None
    assert reloaded.parse_status == ParseStatus.failed


@pytest.mark.postgres
async def test_failed_to_anything_else_still_rejected(
    db_session: AsyncSession,
) -> None:
    """
    'failed' is reserved for retry (-> queued) only -- direct transitions
    to 'parsed', 'needs_review', or 'confirmed' remain illegal (no code
    path or documented product flow does this today; see migration 0007's
    docstring for what was deliberately NOT added).
    """
    for target in (
        ParseStatus.parsed,
        ParseStatus.needs_review,
        ParseStatus.confirmed,
    ):
        # Fresh user/group/expense per iteration: db_session.rollback()
        # expires all session-bound objects, and re-using an expired ORM
        # object across iterations (e.g. `group.id`) would trigger an
        # implicit lazy-load that async SQLAlchemy cannot perform outside
        # an awaited call (MissingGreenlet) -- simplest fix is to not reuse
        # any object across a rollback boundary.
        alice = await _make_user(db_session, "Alice")
        group = await _make_group(db_session, alice)
        expense = await _make_expense(
            db_session, group, alice, 1000, parse_status=ParseStatus.failed
        )
        await db_session.commit()

        with pytest.raises(DBAPIError):
            await db_session.execute(
                sa.update(Expense)
                .where(Expense.id == expense.id)
                .values(parse_status=target.value)
            )
            await db_session.flush()
        await db_session.rollback()
