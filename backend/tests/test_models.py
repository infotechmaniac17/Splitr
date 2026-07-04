"""
Model / constraint tests.

These tests run against in-memory SQLite. They verify:
  - Unique constraint on users.email
  - Unique constraint on item_assignments(line_item_id, user_id)
  - Append-only Session guard raises on UPDATE of LedgerEntry
  - Append-only Session guard raises on DELETE of LedgerEntry
  - amount_minor > 0 is enforced at the application layer
  - Enum values are stored and retrieved correctly
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db, name: str = "Alice", email: str | None = None) -> User:
    user = User(name=name, email=email or f"{name.lower()}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _make_group(db, creator: User) -> Group:
    group = Group(name="Test Group", created_by=creator.id)
    db.add(group)
    await db.flush()
    member = GroupMember(group_id=group.id, user_id=creator.id, role=GroupMemberRole.admin)
    db.add(member)
    await db.flush()
    return group


async def _make_expense(db, group: Group, payer: User, total: int = 1000) -> Expense:
    expense = Expense(
        group_id=group.id,
        paid_by=payer.id,
        vendor="Test Vendor",
        currency="INR",
        total_minor=total,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
    )
    db.add(expense)
    await db.flush()
    return expense


async def _make_line_item(db, expense: Expense, total: int = 1000) -> ExpenseLineItem:
    li = ExpenseLineItem(
        expense_id=expense.id,
        line_no=1,
        kind=LineItemKind.item,
        description="Test item",
        quantity=1,
        total_minor=total,
    )
    db.add(li)
    await db.flush()
    return li


async def _make_ledger_entry(
    db,
    group: Group,
    expense: Expense,
    debtor: User,
    creditor: User,
    amount: int = 500,
) -> LedgerEntry:
    entry = LedgerEntry(
        group_id=group.id,
        expense_id=expense.id,
        debtor_id=debtor.id,
        creditor_id=creditor.id,
        amount_minor=amount,
        entry_type=LedgerEntryType.expense_share,
    )
    db.add(entry)
    await db.flush()
    return entry


# ---------------------------------------------------------------------------
# User unique email
# ---------------------------------------------------------------------------


async def test_user_email_unique(db_session) -> None:
    """Creating two users with the same email raises IntegrityError."""
    alice = User(name="Alice", email="alice@example.com")
    db_session.add(alice)
    await db_session.flush()

    duplicate = User(name="Alice2", email="alice@example.com")
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# ItemAssignment unique (line_item_id, user_id)
# ---------------------------------------------------------------------------


async def test_item_assignment_unique(db_session) -> None:
    """Assigning the same user to the same line item twice raises IntegrityError."""
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice)
    li = await _make_line_item(db_session, expense)

    a1 = ItemAssignment(line_item_id=li.id, user_id=alice.id, weight=1, share_minor=1000)
    db_session.add(a1)
    await db_session.flush()

    a2 = ItemAssignment(line_item_id=li.id, user_id=alice.id, weight=1, share_minor=1000)
    db_session.add(a2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Append-only guard: UPDATE
# ---------------------------------------------------------------------------


async def test_ledger_entry_update_raises(db_session) -> None:
    """Attempting to modify a LedgerEntry raises RuntimeError."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice)

    entry = await _make_ledger_entry(db_session, group, expense, bob, alice, 500)
    await db_session.commit()

    # Reload and attempt mutation.
    result = await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.id == entry.id)
    )
    loaded_entry = result.scalar_one()
    loaded_entry.amount_minor = 999  # attempt update

    with pytest.raises(RuntimeError, match="immutable"):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Append-only guard: DELETE
# ---------------------------------------------------------------------------


async def test_ledger_entry_delete_raises(db_session) -> None:
    """Attempting to delete a LedgerEntry raises RuntimeError."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice)

    entry = await _make_ledger_entry(db_session, group, expense, bob, alice, 500)
    await db_session.commit()

    result = await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.id == entry.id)
    )
    loaded_entry = result.scalar_one()

    await db_session.delete(loaded_entry)
    with pytest.raises(RuntimeError, match="append-only"):
        await db_session.flush()


# ---------------------------------------------------------------------------
# amount_minor > 0 enforced at app layer
# ---------------------------------------------------------------------------


async def test_ledger_amount_must_be_positive(db_session) -> None:
    """
    The application checks amount_minor > 0 before writing.
    Direct insertion of a zero/negative amount bypasses the app layer
    and would only be caught by the DB CHECK constraint (not enforced in SQLite),
    so here we test the domain function's guard instead.
    """
    from app.domain.ledger import post_expense_to_ledger

    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, total=1000)

    # Attempt to post with a negative share — should raise ValueError.
    bad_shares = {alice.id: 1500, bob.id: -500}
    with pytest.raises(ValueError, match="Negative share"):
        await post_expense_to_ledger(db_session, expense, bad_shares)


# ---------------------------------------------------------------------------
# Enum round-trip
# ---------------------------------------------------------------------------


async def test_group_member_role_enum(db_session) -> None:
    """GroupMemberRole enum values round-trip through the ORM correctly."""
    alice = await _make_user(db_session, "Alice")
    await _make_group(db_session, alice)

    result = await db_session.execute(
        select(GroupMember).where(GroupMember.user_id == alice.id)
    )
    member = result.scalar_one()
    assert member.role == GroupMemberRole.admin


async def test_ledger_entry_type_enum(db_session) -> None:
    """LedgerEntryType enum values round-trip correctly."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice)

    entry = await _make_ledger_entry(db_session, group, expense, bob, alice, 500)
    await db_session.commit()

    result = await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.id == entry.id)
    )
    loaded = result.scalar_one()
    assert loaded.entry_type == LedgerEntryType.expense_share


async def test_settlement_method_enum(db_session) -> None:
    """SettlementMethod enum round-trips correctly."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")

    settlement = Settlement(
        payer_id=bob.id,
        payee_id=alice.id,
        amount_minor=1000,
        method=SettlementMethod.upi,
    )
    db_session.add(settlement)
    await db_session.commit()

    result = await db_session.execute(
        select(Settlement).where(Settlement.id == settlement.id)
    )
    loaded = result.scalar_one()
    assert loaded.method == SettlementMethod.upi
