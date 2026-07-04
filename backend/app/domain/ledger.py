"""
Ledger posting and balance computation.

All money mutations happen inside a single DB transaction.  The append-only
constraint is enforced both here (no update/delete code paths exist) and by
the SQLAlchemy Session event listener registered in models.py.

Balance conventions
-------------------
LedgerEntry directions:
  expense_share:    debtor=participant, creditor=paid_by_user
  settlement:       debtor=payee,       creditor=payer   (reverses the debt)
  refund_reversal:  debtor=creditor,    creditor=debtor  (from original entry)
  adjustment:       caller-specified

Net balance for a user  = Σ(creditor entries) − Σ(debtor entries)
  Positive → others owe this user.
  Negative → this user owes others.

For Σ all users in a group: each expense_share entry creates +amount for
creditor and −amount for debtor; settlement entries cancel them out.
Therefore Σ all nets == 0 (money is conserved).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    Expense,
    ExpenseLineItem,
    ItemAssignment,
    LedgerEntry,
    LedgerEntryType,
    ParseStatus,
    Settlement,
    SettlementMethod,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Ledger posting
# ---------------------------------------------------------------------------


async def post_expense_to_ledger(
    db: AsyncSession,
    expense: Expense,
    shares: dict[uuid.UUID, int],
) -> list[LedgerEntry]:
    """
    Atomically post ledger entries for a confirmed expense.

    Args:
        db:       Active async session (caller owns the transaction).
        expense:  The Expense ORM object to confirm.
        shares:   Mapping {user_id: share_minor}.  Must sum exactly to
                  expense.total_minor (raises ValueError otherwise).

    Returns:
        List of inserted LedgerEntry objects.

    Raises:
        ValueError: if sum(shares) != expense.total_minor, or if any
                    share is negative (which would violate amount_minor > 0).
    """
    # H2: All shares (including payer's) must be non-negative BEFORE the sum
    # check.  A negative payer share can mask a bogus split that still sums to
    # total_minor (e.g. payer=-500, other=1500, total=1000 — sum passes but
    # the signed entry would violate amount_minor > 0).
    for user_id, amount in shares.items():
        if amount < 0:
            raise ValueError(
                f"Negative share {amount} for user {user_id} is not allowed. "
                "All shares (including payer's) must be >= 0."
            )

    total = sum(shares.values())
    if total != expense.total_minor:
        raise ValueError(
            f"Share sum {total} does not equal expense total {expense.total_minor}. "
            "Refusing to post to ledger."
        )

    entries: list[LedgerEntry] = []
    paid_by: uuid.UUID = uuid.UUID(str(expense.paid_by))

    for user_id, amount in shares.items():
        if user_id == paid_by:
            # Payer's own share — no debt to themselves.
            continue
        if amount == 0:
            continue
        # amount < 0 is already rejected above; this branch is unreachable but
        # kept as a safeguard.
        if amount < 0:  # pragma: no cover
            raise ValueError(
                f"Negative share {amount} for user {user_id} is not allowed "
                "in expense_share ledger entries.  Use a refund_reversal entry."
            )
        entry = LedgerEntry(
            group_id=expense.group_id,
            expense_id=expense.id,
            debtor_id=user_id,
            creditor_id=paid_by,
            amount_minor=amount,
            entry_type=LedgerEntryType.expense_share,
        )
        db.add(entry)
        entries.append(entry)

    # Freeze parse_status and record confirmation timestamp.
    expense.parse_status = ParseStatus.confirmed
    expense.confirmed_at = _now_utc()

    return entries


async def post_refund_to_ledger(
    db: AsyncSession,
    expense: Expense,
    refund_shares: dict[uuid.UUID, int],
) -> list[LedgerEntry]:
    """
    Post refund_reversal entries for a refund on a CONFIRMED expense.

    `refund_shares` maps each participant to their (positive) portion of the
    refunded amount, computed from the original item's assignment ratios.
    The refund lands in the payer's account, so the payer now owes each
    participant their portion back:

        debtor=paid_by, creditor=participant, type='refund_reversal'

    The payer's own portion generates no entry (no debt to themselves).
    Already-settled debts are not rewritten — the reversal simply shifts the
    current net balance (append-only ledger).

    Raises:
        ValueError: if any share is negative or all shares are zero.
    """
    for user_id, amount in refund_shares.items():
        if amount < 0:
            raise ValueError(
                f"Negative refund share {amount} for user {user_id}. "
                "Refund shares must be >= 0."
            )
    if sum(refund_shares.values()) <= 0:
        raise ValueError("Refund shares must sum to a positive amount")

    entries: list[LedgerEntry] = []
    paid_by: uuid.UUID = uuid.UUID(str(expense.paid_by))

    for user_id, amount in refund_shares.items():
        if user_id == paid_by or amount == 0:
            continue
        entry = LedgerEntry(
            group_id=expense.group_id,
            expense_id=expense.id,
            debtor_id=paid_by,      # reversed vs expense_share
            creditor_id=user_id,    # money flows back to the participant
            amount_minor=amount,
            entry_type=LedgerEntryType.refund_reversal,
        )
        db.add(entry)
        entries.append(entry)

    return entries


async def post_settlement_to_ledger(
    db: AsyncSession,
    group_id: uuid.UUID | None,
    payer_id: uuid.UUID,
    payee_id: uuid.UUID,
    amount_minor: int,
    method: SettlementMethod,
    note: str | None = None,
) -> tuple[Settlement, LedgerEntry]:
    """
    Record a settlement payment and post the corresponding ledger entry.

    The settlement entry uses REVERSED debtor/creditor so that pairwise
    netting works: the entry (debtor=payee, creditor=payer) cancels out
    the original expense_share entry (debtor=payer, creditor=payee).

    Args:
        db:           Active async session.
        group_id:     Group context (nullable for personal settlements).
        payer_id:     User who is paying.
        payee_id:     User who is receiving the payment.
        amount_minor: Amount paid (must be > 0).
        method:       Payment method.
        note:         Optional note.

    Returns:
        (Settlement, LedgerEntry) tuple.

    Raises:
        ValueError: if amount_minor <= 0.
    """
    if amount_minor <= 0:
        raise ValueError(f"Settlement amount must be positive, got {amount_minor}")
    # M5: a settlement between the same user is meaningless and would corrupt
    # the net-balance graph (it would add a self-loop that never cancels).
    if payer_id == payee_id:
        raise ValueError(
            f"Settlement payer and payee cannot be the same user ({payer_id})."
        )

    settlement = Settlement(
        group_id=group_id,
        payer_id=payer_id,
        payee_id=payee_id,
        amount_minor=amount_minor,
        method=method,
        note=note,
    )
    db.add(settlement)
    await db.flush()  # populate settlement.id

    # Settlement entry reverses the debt direction so that pairwise netting
    # correctly cancels out expense_share entries.
    entry = LedgerEntry(
        group_id=group_id,
        settlement_id=settlement.id,
        debtor_id=payee_id,    # reversed
        creditor_id=payer_id,  # reversed
        amount_minor=amount_minor,
        entry_type=LedgerEntryType.settlement,
    )
    db.add(entry)

    return settlement, entry


# ---------------------------------------------------------------------------
# Balance computation
# ---------------------------------------------------------------------------


async def compute_group_balances(
    db: AsyncSession,
    group_id: uuid.UUID,
) -> list[tuple[uuid.UUID, uuid.UUID, int]]:
    """
    Compute net pairwise balances for a group.

    Returns a list of (debtor_id, creditor_id, net_amount_minor) tuples
    where net_amount_minor > 0.  Pairs that cancel out completely are omitted.
    """
    result = await db.execute(
        select(
            LedgerEntry.debtor_id,
            LedgerEntry.creditor_id,
            func.sum(LedgerEntry.amount_minor).label("total"),
        )
        .where(LedgerEntry.group_id == group_id)
        .group_by(LedgerEntry.debtor_id, LedgerEntry.creditor_id)
    )
    rows = result.all()

    # Build gross per-direction totals.
    gross: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    for row in rows:
        debtor_id = uuid.UUID(str(row.debtor_id))
        creditor_id = uuid.UUID(str(row.creditor_id))
        gross[(debtor_id, creditor_id)] = int(row.total)

    # Net pairwise.
    balances: list[tuple[uuid.UUID, uuid.UUID, int]] = []
    processed: set[frozenset[uuid.UUID]] = set()

    for (debtor, creditor), _ in gross.items():
        pair: frozenset[uuid.UUID] = frozenset({debtor, creditor})
        if pair in processed:
            continue
        processed.add(pair)

        forward = gross.get((debtor, creditor), 0)
        backward = gross.get((creditor, debtor), 0)
        net = forward - backward

        if net > 0:
            balances.append((debtor, creditor, net))
        elif net < 0:
            balances.append((creditor, debtor, -net))

    return balances


async def compute_user_net_balance(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """
    Compute the net balance for a user across ALL groups and personal expenses.

    Positive → others owe this user (user is net creditor).
    Negative → this user owes others (user is net debtor).
    """
    owed_to_user = await db.scalar(
        select(func.sum(LedgerEntry.amount_minor)).where(
            LedgerEntry.creditor_id == user_id
        )
    )
    owed_by_user = await db.scalar(
        select(func.sum(LedgerEntry.amount_minor)).where(
            LedgerEntry.debtor_id == user_id
        )
    )

    credit: int = int(owed_to_user) if owed_to_user is not None else 0
    debit: int = int(owed_by_user) if owed_by_user is not None else 0
    return credit - debit


async def load_expense_shares(
    db: AsyncSession,
    expense_id: uuid.UUID,
) -> dict[uuid.UUID, int]:
    """
    Load the pre-computed share_minor values from item_assignments for an expense.

    Returns a mapping {user_id: share_minor}.
    Assignments without a share_minor value are excluded.
    """
    result = await db.execute(
        select(ItemAssignment.user_id, ItemAssignment.share_minor)
        .join(
            ExpenseLineItem,
            ItemAssignment.line_item_id == ExpenseLineItem.id,
        )
        .where(ExpenseLineItem.expense_id == expense_id)
    )
    rows = result.all()
    # H1: accumulate (not overwrite) shares across multiple line items so that
    # a user assigned to 3 items gets the correct total, not just the last.
    shares: dict[uuid.UUID, int] = {}
    for row in rows:
        if row.share_minor is not None:
            uid = uuid.UUID(str(row.user_id))
            shares[uid] = shares.get(uid, 0) + int(row.share_minor)
    return shares
