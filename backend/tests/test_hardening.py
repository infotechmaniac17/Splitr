"""
Regression tests for the M1 hardening pass (C1, H1–H3, M1–M6).

Each test is named after the finding it covers.  Tests decorated with
@pytest.mark.postgres are skipped on SQLite (they need FOR UPDATE locking,
Postgres triggers, or enforced CHECK constraints).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ledger import (
    load_expense_shares,
    post_expense_to_ledger,
    post_settlement_to_ledger,
)
from app.domain.models import (
    Expense,
    ExpenseLineItem,
    ExpenseSource,
    ExpenseStatus,
    Group,
    GroupMember,
    GroupMemberRole,
    ItemAssignment,
    LedgerEntry,
    LedgerEntryType,
    LineItemKind,
    ParseStatus,
    Settlement,
    SettlementMethod,
    User,
)

# ---------------------------------------------------------------------------
# Shared helpers (mirror what other test files use)
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, name: str, email: str | None = None) -> User:
    user = User(name=name, email=email or f"{name.lower()}_{uuid.uuid4().hex[:6]}@test.com")
    db.add(user)
    await db.flush()
    return user


async def _make_group(db: AsyncSession, creator: User) -> Group:
    group = Group(name="Hardening Test Group", created_by=creator.id)
    db.add(group)
    await db.flush()
    db.add(GroupMember(group_id=group.id, user_id=creator.id, role=GroupMemberRole.admin))
    await db.flush()
    return group


async def _add_member(db: AsyncSession, group: Group, user: User) -> None:
    db.add(GroupMember(group_id=group.id, user_id=user.id, role=GroupMemberRole.member))
    await db.flush()


async def _make_expense(
    db: AsyncSession, group: Group | None, payer: User, total: int
) -> Expense:
    expense = Expense(
        group_id=group.id if group else None,
        paid_by=payer.id,
        vendor="Test",
        currency="INR",
        total_minor=total,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
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
    db: AsyncSession, line_item: ExpenseLineItem, user: User, share: int
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
# H1: load_expense_shares accumulates across multiple line items
# ---------------------------------------------------------------------------


async def test_h1_load_expense_shares_accumulates_multi_item(
    db_session: AsyncSession,
) -> None:
    """
    H1 regression: same user assigned on 3 separate line items — load_expense_shares
    must return the *sum*, not just the last assignment.
    """
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    group = await _make_group(db_session, alice)
    await _add_member(db_session, group, bob)

    expense = await _make_expense(db_session, group, alice, total=900)

    # Three line items: alice assigned on all three (300 each), bob on none.
    li1 = await _make_line_item(db_session, expense, 300, line_no=1)
    li2 = await _make_line_item(db_session, expense, 300, line_no=2)
    li3 = await _make_line_item(db_session, expense, 300, line_no=3)

    await _assign(db_session, li1, alice, 300)
    await _assign(db_session, li2, alice, 300)
    await _assign(db_session, li3, alice, 300)
    await db_session.commit()

    shares = await load_expense_shares(db_session, expense.id)

    assert shares[alice.id] == 900, (
        f"Expected alice share=900, got {shares.get(alice.id)}. "
        "H1 bug: load_expense_shares must ACCUMULATE not overwrite."
    )
    # Sum must reconcile with expense total.
    assert sum(shares.values()) == expense.total_minor


async def test_h1_multi_item_confirm_succeeds(client: AsyncClient) -> None:
    """
    H1 API-level regression: expense with 3 items all assigned to one user
    must confirm and produce a correct (non-doubled) balance.
    """
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_h1@test.com"})
    r_bob = await client.post("/api/v1/users", json={"name": "Bob", "email": "bob_h1@test.com"})
    alice = r_alice.json()
    bob = r_bob.json()

    r_group = await client.post("/api/v1/groups", json={"name": "H1 Group", "created_by": alice["id"]})
    group = r_group.json()
    await client.post(f"/api/v1/groups/{group['id']}/members", json={"user_id": bob["id"]})

    # Alice pays 900, split 300/300/300 but ALL assigned to alice (payer).
    # Bob gets 0 share. Confirm should succeed and balance == 0.
    r_exp = await client.post(
        "/api/v1/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["id"],
            "total_minor": 900,
            "shares": {alice["id"]: 900},
        },
    )
    assert r_exp.status_code == 201
    expense = r_exp.json()

    r_confirm = await client.post(f"/api/v1/expenses/{expense['id']}/confirm")
    assert r_confirm.status_code == 200

    # No one owes anyone.
    balances_resp = await client.get(f"/api/v1/groups/{group['id']}/balances")
    assert balances_resp.json()["balances"] == []


# ---------------------------------------------------------------------------
# H2: Negative share rejection
# ---------------------------------------------------------------------------


async def test_h2_negative_payer_share_rejected_at_api(client: AsyncClient) -> None:
    """
    H2: payer share of -500 with other=1500 (sum=1000=total) must be rejected
    at the API layer with HTTP 422.
    """
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_h2@test.com"})
    r_bob = await client.post("/api/v1/users", json={"name": "Bob", "email": "bob_h2@test.com"})
    alice = r_alice.json()
    bob = r_bob.json()

    r_group = await client.post("/api/v1/groups", json={"name": "H2 Group", "created_by": alice["id"]})
    group = r_group.json()
    await client.post(f"/api/v1/groups/{group['id']}/members", json={"user_id": bob["id"]})

    # payer=-500, other=1500 → sum=1000=total but payer share is negative.
    resp = await client.post(
        "/api/v1/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["id"],
            "total_minor": 1000,
            "shares": {alice["id"]: -500, bob["id"]: 1500},
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


async def test_h2_negative_payer_share_rejected_at_domain(
    db_session: AsyncSession,
) -> None:
    """
    H2: post_expense_to_ledger raises ValueError when payer has a negative share.
    """
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, total=1000)

    # payer=-500 + other=1500 sums to 1000 but violates non-negativity.
    bad_shares = {alice.id: -500, bob.id: 1500}
    with pytest.raises(ValueError, match="Negative share"):
        await post_expense_to_ledger(db_session, expense, bad_shares)


async def test_h2_negative_non_payer_share_also_rejected(
    db_session: AsyncSession,
) -> None:
    """Non-payer negative share is also rejected at the domain layer."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, total=1000)

    bad_shares = {alice.id: 1500, bob.id: -500}
    with pytest.raises(ValueError, match="Negative share"):
        await post_expense_to_ledger(db_session, expense, bad_shares)


# ---------------------------------------------------------------------------
# H3: Postgres trigger guards (postgres-only)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_h3_trigger_blocks_ledger_update(db_session: AsyncSession) -> None:
    """
    H3: raw UPDATE on ledger_entries raises DBAPIError via Postgres trigger.
    (The ORM session guard handles ORM-layer mutations; this tests the DB-level
    trigger that also catches raw SQL updates.)
    """
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, 1000)

    entry = LedgerEntry(
        group_id=group.id,
        expense_id=expense.id,
        debtor_id=bob.id,
        creditor_id=alice.id,
        amount_minor=1000,
        entry_type=LedgerEntryType.expense_share,
    )
    db_session.add(entry)
    await db_session.commit()

    # Raw SQL UPDATE bypasses the ORM guard; the Postgres trigger must catch it.
    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.update(LedgerEntry)
            .where(LedgerEntry.id == entry.id)
            .values(amount_minor=999)
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_h3_trigger_blocks_ledger_delete(db_session: AsyncSession) -> None:
    """H3: raw DELETE on ledger_entries raises DBAPIError via Postgres trigger."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, 500)

    entry = LedgerEntry(
        group_id=group.id,
        expense_id=expense.id,
        debtor_id=bob.id,
        creditor_id=alice.id,
        amount_minor=500,
        entry_type=LedgerEntryType.expense_share,
    )
    db_session.add(entry)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.delete(LedgerEntry).where(LedgerEntry.id == entry.id)
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_h3_trigger_blocks_confirmed_expense_financial_mutation(
    db_session: AsyncSession,
) -> None:
    """
    H3: Postgres trigger prevents updating a financial column on a confirmed expense.
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, 1000)

    # Manually set parse_status = confirmed.
    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.update(Expense)
            .where(Expense.id == expense.id)
            .values(total_minor=9999)
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_h3_trigger_allows_voiding_confirmed_expense(
    db_session: AsyncSession,
) -> None:
    """
    H3: voiding a confirmed expense (status='voided') is allowed by the trigger
    because only financial columns are protected, not the status column.
    """
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, 1000)
    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    # Updating status to 'voided' must NOT raise.
    await db_session.execute(
        sa.update(Expense)
        .where(Expense.id == expense.id)
        .values(status="voided")
    )
    await db_session.commit()  # must succeed


@pytest.mark.postgres
async def test_h3_trigger_blocks_confirmed_expense_delete(
    db_session: AsyncSession,
) -> None:
    """H3: DELETE of a confirmed expense is blocked by the trigger."""
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, 1000)
    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.delete(Expense).where(Expense.id == expense.id)
        )
        await db_session.flush()


# ---------------------------------------------------------------------------
# C1: Atomic confirm — two concurrent confirms write ledger exactly once
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_c1_concurrent_confirm_atomic(client: AsyncClient) -> None:
    """
    C1: Two concurrent POST /expenses/{id}/confirm requests must result in
    ledger entries written exactly once (not doubled).

    SELECT ... FOR UPDATE serializes the two requests: the first acquires the
    row lock, confirms, commits; the second waits, then gets the lock, sees
    parse_status='confirmed', and returns the idempotent response.
    """
    # --- Setup ---
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_c1@test.com"})
    r_bob = await client.post("/api/v1/users", json={"name": "Bob", "email": "bob_c1@test.com"})
    alice = r_alice.json()
    bob = r_bob.json()

    r_group = await client.post(
        "/api/v1/groups", json={"name": "C1 Group", "created_by": alice["id"]}
    )
    group = r_group.json()
    await client.post(f"/api/v1/groups/{group['id']}/members", json={"user_id": bob["id"]})

    r_exp = await client.post(
        "/api/v1/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["id"],
            "total_minor": 1000,
            "participants": [alice["id"], bob["id"]],
        },
    )
    assert r_exp.status_code == 201
    expense = r_exp.json()

    # --- Two concurrent confirms ---
    r1, r2 = await asyncio.gather(
        client.post(f"/api/v1/expenses/{expense['id']}/confirm"),
        client.post(f"/api/v1/expenses/{expense['id']}/confirm"),
    )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    # --- Verify: Bob owes Alice exactly 500 (not 1000 from a double-post) ---
    balances_resp = await client.get(f"/api/v1/groups/{group['id']}/balances")
    balances = balances_resp.json()["balances"]
    assert len(balances) == 1, f"Expected 1 balance entry, got {len(balances)}: {balances}"
    assert balances[0]["net_amount_minor"] == 500, (
        f"Expected 500 (single post), got {balances[0]['net_amount_minor']} "
        "(double-post would give 1000)."
    )


# ---------------------------------------------------------------------------
# M1: Group membership enforcement
# ---------------------------------------------------------------------------


async def test_m1_non_member_expense_create_rejected(client: AsyncClient) -> None:
    """M1: expense create is rejected when paid_by is not an active group member."""
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m1a@test.com"})
    r_outsider = await client.post("/api/v1/users", json={"name": "Outsider", "email": "outsider_m1@test.com"})
    alice = r_alice.json()
    outsider = r_outsider.json()

    r_group = await client.post(
        "/api/v1/groups", json={"name": "M1 Group", "created_by": alice["id"]}
    )
    group = r_group.json()
    # outsider is NOT added to the group.

    resp = await client.post(
        "/api/v1/expenses",
        json={
            "group_id": group["id"],
            "paid_by": outsider["id"],
            "total_minor": 500,
            "participants": [outsider["id"]],
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


async def test_m1_non_member_participant_expense_create_rejected(client: AsyncClient) -> None:
    """M1: expense create rejected when a participant is not an active group member."""
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m1b@test.com"})
    r_outsider = await client.post("/api/v1/users", json={"name": "Outsider", "email": "outsider_m1b@test.com"})
    alice = r_alice.json()
    outsider = r_outsider.json()

    r_group = await client.post(
        "/api/v1/groups", json={"name": "M1 Group B", "created_by": alice["id"]}
    )
    group = r_group.json()

    resp = await client.post(
        "/api/v1/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["id"],
            "total_minor": 1000,
            "shares": {alice["id"]: 500, outsider["id"]: 500},
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


async def test_m1_non_member_settlement_rejected(client: AsyncClient) -> None:
    """M1: settlement is rejected when payer is not an active group member."""
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m1c@test.com"})
    r_bob = await client.post("/api/v1/users", json={"name": "Bob", "email": "bob_m1c@test.com"})
    r_outsider = await client.post("/api/v1/users", json={"name": "Outsider", "email": "outsider_m1c@test.com"})
    alice = r_alice.json()
    bob = r_bob.json()
    outsider = r_outsider.json()

    r_group = await client.post(
        "/api/v1/groups", json={"name": "M1 Group C", "created_by": alice["id"]}
    )
    group = r_group.json()
    await client.post(f"/api/v1/groups/{group['id']}/members", json={"user_id": bob["id"]})
    # outsider NOT added.

    resp = await client.post(
        "/api/v1/settlements",
        json={
            "group_id": group["id"],
            "payer_id": outsider["id"],
            "payee_id": alice["id"],
            "amount_minor": 100,
            "method": "cash",
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


async def test_m1_no_group_id_skips_membership_check(client: AsyncClient) -> None:
    """M1: personal expense (group_id=NULL) skips membership check — any user allowed."""
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m1d@test.com"})
    r_bob = await client.post("/api/v1/users", json={"name": "Bob", "email": "bob_m1d@test.com"})
    alice = r_alice.json()
    bob = r_bob.json()

    # No group_id.
    resp = await client.post(
        "/api/v1/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 500,
            "participants": [alice["id"], bob["id"]],
        },
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# M2/M3: CHECK constraints on amount_minor / total_minor
# ---------------------------------------------------------------------------


async def test_m2_settlement_zero_amount_rejected_at_api(client: AsyncClient) -> None:
    """M2/M3: settlement with amount_minor=0 is rejected at Pydantic level."""
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m2@test.com"})
    r_bob = await client.post("/api/v1/users", json={"name": "Bob", "email": "bob_m2@test.com"})
    alice = r_alice.json()
    bob = r_bob.json()

    resp = await client.post(
        "/api/v1/settlements",
        json={
            "payer_id": alice["id"],
            "payee_id": bob["id"],
            "amount_minor": 0,
            "method": "cash",
        },
    )
    assert resp.status_code == 422


async def test_m3_expense_zero_total_rejected_at_api(client: AsyncClient) -> None:
    """M2/M3: expense with total_minor=0 is rejected at Pydantic level."""
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m3@test.com"})
    alice = r_alice.json()

    resp = await client.post(
        "/api/v1/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 0,
            "participants": [alice["id"]],
        },
    )
    assert resp.status_code == 422


@pytest.mark.postgres
async def test_m2_settlement_negative_amount_check_constraint(
    db_session: AsyncSession,
) -> None:
    """
    M2/M3: direct DB insert of settlement with amount_minor=0 is blocked by
    Postgres CHECK constraint ck_settlement_amount_positive.
    """
    from sqlalchemy.exc import IntegrityError

    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob")

    bad_settlement = Settlement(
        payer_id=alice.id,
        payee_id=bob.id,
        amount_minor=0,  # violates CHECK
        method=SettlementMethod.cash,
    )
    db_session.add(bad_settlement)
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


@pytest.mark.postgres
async def test_m3_expense_zero_total_check_constraint(
    db_session: AsyncSession,
) -> None:
    """
    M2/M3: direct DB insert of expense with total_minor=0 is blocked by
    Postgres CHECK constraint ck_expense_total_positive.
    """
    from sqlalchemy.exc import IntegrityError

    alice = await _make_user(db_session, "Alice")

    bad_expense = Expense(
        paid_by=alice.id,
        currency="INR",
        total_minor=0,  # violates CHECK
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
    )
    db_session.add(bad_expense)
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


# ---------------------------------------------------------------------------
# M4: quantity must be Decimal in Pydantic schema (no float arithmetic)
# ---------------------------------------------------------------------------


async def test_m4_quantity_accepts_decimal(client: AsyncClient) -> None:
    """M4: line item with quantity as decimal string/value is accepted."""
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m4@test.com"})
    alice = r_alice.json()

    resp = await client.post(
        "/api/v1/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 750,
            "participants": [alice["id"]],
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Half kg rice",
                    "quantity": "0.5",   # Decimal-compatible string
                    "unit_price_minor": 1500,
                    "total_minor": 750,
                }
            ],
        },
    )
    assert resp.status_code == 201
    line_items = resp.json()["line_items"]
    assert len(line_items) == 1
    # Quantity is stored as NUMERIC and returned; verify it round-trips.
    assert float(line_items[0]["quantity"]) == pytest.approx(0.5)


async def test_m4_quantity_zero_rejected(client: AsyncClient) -> None:
    """M4: quantity=0 is rejected (gt=0 constraint)."""
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m4b@test.com"})
    alice = r_alice.json()

    resp = await client.post(
        "/api/v1/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 100,
            "participants": [alice["id"]],
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "quantity": "0",   # must be > 0
                    "total_minor": 100,
                }
            ],
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# M5: Settlement payer == payee rejected
# ---------------------------------------------------------------------------


async def test_m5_settlement_payer_equals_payee_rejected_at_api(
    client: AsyncClient,
) -> None:
    """M5: settlement where payer_id == payee_id is rejected with HTTP 422."""
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m5@test.com"})
    alice = r_alice.json()

    resp = await client.post(
        "/api/v1/settlements",
        json={
            "payer_id": alice["id"],
            "payee_id": alice["id"],
            "amount_minor": 500,
            "method": "cash",
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


async def test_m5_settlement_payer_equals_payee_rejected_at_domain(
    db_session: AsyncSession,
) -> None:
    """M5: post_settlement_to_ledger raises ValueError when payer == payee."""
    alice = await _make_user(db_session, "Alice")

    with pytest.raises(ValueError, match="payer and payee cannot be the same"):
        await post_settlement_to_ledger(
            db_session,
            group_id=None,
            payer_id=alice.id,
            payee_id=alice.id,
            amount_minor=500,
            method=SettlementMethod.cash,
        )


# ---------------------------------------------------------------------------
# M6: Expense ORM default parse_status is 'queued'
# ---------------------------------------------------------------------------


async def test_m6_bare_expense_defaults_to_queued(db_session: AsyncSession) -> None:
    """
    M6: a bare Expense() insert (without explicit parse_status) must default
    to ParseStatus.queued, not 'parsed'.
    """
    alice = await _make_user(db_session, "Alice")

    expense = Expense(
        paid_by=alice.id,
        currency="INR",
        total_minor=100,
        source=ExpenseSource.manual,
        # parse_status intentionally omitted — must default to 'queued'.
        status=ExpenseStatus.active,
    )
    db_session.add(expense)
    await db_session.commit()

    loaded = await db_session.get(Expense, expense.id)
    assert loaded is not None
    assert loaded.parse_status == ParseStatus.queued, (
        f"Expected queued, got {loaded.parse_status}. "
        "M6: ORM default must be queued; API create_expense sets parsed explicitly."
    )


async def test_m6_manual_expense_api_sets_parsed(client: AsyncClient) -> None:
    """
    M6: POST /expenses (manual) explicitly sets parse_status='parsed' so the
    expense can immediately be confirmed.
    """
    r_alice = await client.post("/api/v1/users", json={"name": "Alice", "email": "alice_m6@test.com"})
    alice = r_alice.json()

    resp = await client.post(
        "/api/v1/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 200,
            "participants": [alice["id"]],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["parse_status"] == "parsed"


async def test_m6_raw_insert_server_default_is_queued(db_session: AsyncSession) -> None:
    """
    M6 (re-audit): an INSERT that bypasses the ORM entirely (raw SQL, Celery
    workers, admin scripts) must also default parse_status to 'queued' via the
    column's server_default — not 'parsed', which would skip the M3 validation
    engine.
    """
    alice = await _make_user(db_session, "Alice")

    expense_id = uuid.uuid4()
    # Core insert omitting parse_status → DB server_default applies.
    await db_session.execute(
        sa.insert(Expense.__table__).values(
            id=expense_id,
            paid_by=alice.id,
            currency="INR",
            total_minor=100,
            source=ExpenseSource.manual.value,
            status=ExpenseStatus.active.value,
        )
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            sa.select(Expense.__table__.c.parse_status).where(
                Expense.__table__.c.id == expense_id
            )
        )
    ).scalar_one()
    assert row == "queued", (
        f"Expected server_default 'queued', got {row!r}. "
        "Raw inserts must not bypass the validation gate."
    )


async def test_confirm_gate_rejects_non_parsed_statuses(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """
    Re-audit LOW finding: confirm must reject expenses whose parse_status is
    queued / needs_review / failed — only 'parsed' (or the idempotent
    'confirmed') may reach ledger posting.
    """
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()

    for bad_status in (ParseStatus.queued, ParseStatus.needs_review, ParseStatus.failed):
        expense = Expense(
            paid_by=alice.id,
            currency="INR",
            total_minor=500,
            source=ExpenseSource.manual,
            parse_status=bad_status,
            status=ExpenseStatus.active,
        )
        db_session.add(expense)
        await db_session.flush()
        li = await _make_line_item(db_session, expense, 500)
        await _assign(db_session, li, alice, 500)
        await db_session.commit()

        # Expense created directly via db_session (not the API), so the
        # test client's auto-auth helper never learned its paid_by -- mint
        # a token for alice (the payer) directly to authenticate as her.
        from app.config import settings as _settings  # noqa: PLC0415
        from app.domain.auth import (
            create_access_token as _create_access_token,  # noqa: PLC0415
        )

        token = _create_access_token(alice.id, _settings.SECRET_KEY)
        resp = await client.post(
            f"/api/v1/expenses/{expense.id}/confirm",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409, (
            f"parse_status={bad_status.value}: expected 409, got {resp.status_code}"
        )
        assert "not in a confirmable state" in resp.json()["detail"]
