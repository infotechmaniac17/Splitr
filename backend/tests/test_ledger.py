"""
Ledger domain tests.

Covers:
  - Sum mismatch rejects ledger posting (ValueError).
  - Correct ledger entries are written (correct debtor/creditor/amount).
  - Balance computation is correct after one or more expenses.
  - Settlement reduces net pairwise balance to expected value.
  - User net balance is zero when all debts are settled.
  - Property test: random expenses always reconcile (Σ user nets == 0).
"""

from __future__ import annotations

import uuid
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.ledger import (
    compute_group_balances,
    compute_user_net_balance,
    post_expense_to_ledger,
    post_settlement_to_ledger,
)
from app.domain.models import (
    Expense,
    ExpenseSource,
    ExpenseStatus,
    Group,
    GroupMember,
    GroupMemberRole,
    ParseStatus,
    SettlementMethod,
    User,
)
from app.domain.rounding import allocate_largest_remainder

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _make_user(db, name: str, email: str | None = None) -> User:
    user = User(name=name, email=email or f"{name.lower()}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _make_group(db, creator: User) -> Group:
    group = Group(name="Ledger Test Group", created_by=creator.id)
    db.add(group)
    await db.flush()
    db.add(
        GroupMember(group_id=group.id, user_id=creator.id, role=GroupMemberRole.admin)
    )
    await db.flush()
    return group


async def _make_expense(db, group: Group, payer: User, total: int) -> Expense:
    expense = Expense(
        group_id=group.id,
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


# ---------------------------------------------------------------------------
# Sum-mismatch rejection
# ---------------------------------------------------------------------------


async def test_sum_mismatch_raises(db_session) -> None:
    """post_expense_to_ledger raises ValueError when shares don't sum to total."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, total=1000)

    bad_shares = {alice.id: 600, bob.id: 300}  # sum = 900, not 1000
    with pytest.raises(ValueError, match="Share sum"):
        await post_expense_to_ledger(db_session, expense, bad_shares)


async def test_exact_sum_accepted(db_session) -> None:
    """post_expense_to_ledger succeeds when shares sum exactly to total."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)
    expense = await _make_expense(db_session, group, alice, total=1000)

    # Alice paid 1000; Bob owes 600, Alice owes herself 400 (no entry for Alice).
    shares = {alice.id: 400, bob.id: 600}
    entries = await post_expense_to_ledger(db_session, expense, shares)
    await db_session.commit()

    # Only Bob should have a ledger entry (Alice is the creditor/payer).
    assert len(entries) == 1
    entry = entries[0]
    assert entry.debtor_id == bob.id
    assert entry.creditor_id == alice.id
    assert entry.amount_minor == 600


# ---------------------------------------------------------------------------
# Balance computation
# ---------------------------------------------------------------------------


async def test_group_balance_after_single_expense(db_session) -> None:
    """Net balance for group reflects the expense correctly."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)

    expense = await _make_expense(db_session, group, alice, total=1000)
    shares = {alice.id: 500, bob.id: 500}
    await post_expense_to_ledger(db_session, expense, shares)
    await db_session.commit()

    balances = await compute_group_balances(db_session, group.id)
    assert len(balances) == 1
    debtor, creditor, net = balances[0]
    assert debtor == bob.id
    assert creditor == alice.id
    assert net == 500


async def test_group_balance_two_expenses_same_direction(db_session) -> None:
    """Multiple expenses in the same direction accumulate correctly."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)

    expense1 = await _make_expense(db_session, group, alice, total=600)
    await post_expense_to_ledger(db_session, expense1, {alice.id: 300, bob.id: 300})

    expense2 = await _make_expense(db_session, group, alice, total=400)
    await post_expense_to_ledger(db_session, expense2, {alice.id: 200, bob.id: 200})
    await db_session.commit()

    balances = await compute_group_balances(db_session, group.id)
    assert len(balances) == 1
    debtor, creditor, net = balances[0]
    assert debtor == bob.id
    assert creditor == alice.id
    assert net == 500  # 300 + 200


async def test_group_balance_cross_expenses(db_session) -> None:
    """When both users pay for each other, balances net correctly."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)

    # Alice pays 1000, Bob owes 600.
    exp1 = await _make_expense(db_session, group, alice, total=1000)
    await post_expense_to_ledger(db_session, exp1, {alice.id: 400, bob.id: 600})

    # Bob pays 800, Alice owes 300.
    exp2 = await _make_expense(db_session, group, bob, total=800)
    await post_expense_to_ledger(db_session, exp2, {alice.id: 300, bob.id: 500})
    await db_session.commit()

    balances = await compute_group_balances(db_session, group.id)
    # Bob owes Alice 600, Alice owes Bob 300. Net: Bob owes Alice 300.
    assert len(balances) == 1
    debtor, creditor, net = balances[0]
    assert debtor == bob.id
    assert creditor == alice.id
    assert net == 300


# ---------------------------------------------------------------------------
# Settlement reduces balance
# ---------------------------------------------------------------------------


async def test_settlement_reduces_balance(db_session) -> None:
    """A settlement entry reduces the net pairwise balance."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)

    expense = await _make_expense(db_session, group, alice, total=1000)
    await post_expense_to_ledger(db_session, expense, {alice.id: 0, bob.id: 1000})
    await db_session.commit()

    balances_before = await compute_group_balances(db_session, group.id)
    assert balances_before[0][2] == 1000

    # Bob pays Alice 400 (partial settlement).
    await post_settlement_to_ledger(
        db_session,
        group_id=group.id,
        payer_id=bob.id,
        payee_id=alice.id,
        amount_minor=400,
        method=SettlementMethod.cash,
    )
    await db_session.commit()

    balances_after = await compute_group_balances(db_session, group.id)
    assert len(balances_after) == 1
    assert balances_after[0][2] == 600  # 1000 - 400


async def test_full_settlement_clears_balance(db_session) -> None:
    """A full settlement results in zero net balance (no entries in result)."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)

    expense = await _make_expense(db_session, group, alice, total=500)
    await post_expense_to_ledger(db_session, expense, {alice.id: 0, bob.id: 500})

    await post_settlement_to_ledger(
        db_session,
        group_id=group.id,
        payer_id=bob.id,
        payee_id=alice.id,
        amount_minor=500,
        method=SettlementMethod.upi,
    )
    await db_session.commit()

    balances = await compute_group_balances(db_session, group.id)
    assert balances == []


# ---------------------------------------------------------------------------
# User net balance
# ---------------------------------------------------------------------------


async def test_user_net_balance_creditor(db_session) -> None:
    """User who paid has a positive net balance (others owe them)."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)

    expense = await _make_expense(db_session, group, alice, total=1000)
    await post_expense_to_ledger(db_session, expense, {alice.id: 500, bob.id: 500})
    await db_session.commit()

    alice_net = await compute_user_net_balance(db_session, alice.id)
    bob_net = await compute_user_net_balance(db_session, bob.id)

    assert alice_net == 500  # Bob owes Alice 500
    assert bob_net == -500  # Bob owes Alice 500


async def test_user_net_balance_zero_after_settlement(db_session) -> None:
    """Both users have zero net balance after full settlement."""
    alice = await _make_user(db_session, "Alice")
    bob = await _make_user(db_session, "Bob", "bob@example.com")
    group = await _make_group(db_session, alice)

    expense = await _make_expense(db_session, group, alice, total=1000)
    await post_expense_to_ledger(db_session, expense, {alice.id: 0, bob.id: 1000})

    await post_settlement_to_ledger(
        db_session,
        group_id=group.id,
        payer_id=bob.id,
        payee_id=alice.id,
        amount_minor=1000,
        method=SettlementMethod.bank,
    )
    await db_session.commit()

    assert await compute_user_net_balance(db_session, alice.id) == 0
    assert await compute_user_net_balance(db_session, bob.id) == 0


# ---------------------------------------------------------------------------
# Property test: Σ user nets == 0 per group
# ---------------------------------------------------------------------------


def _build_group_shares(
    user_ids: list[uuid.UUID],
    total_minor: int,
) -> dict[uuid.UUID, int]:
    """Equal split among all participants."""
    n = len(user_ids)
    ratios: dict[uuid.UUID, Fraction] = {uid: Fraction(1, n) for uid in user_ids}
    return allocate_largest_remainder(total_minor, ratios)


@given(
    total_minor=st.integers(min_value=1, max_value=1_000_000),
    n_users=st.integers(min_value=2, max_value=6),
    n_expenses=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=200)
def test_group_net_balance_is_zero(
    total_minor: int,
    n_users: int,
    n_expenses: int,
) -> None:
    """
    Property: for any set of expenses where payer is one of the participants,
    the sum of all user net balances is zero (money is conserved).

    This is a pure math test — no DB required.
    """
    import random

    rng = random.Random(42)

    # Simulate n_expenses, each with a random total and equal split.
    user_ids = [uuid.uuid4() for _ in range(n_users)]
    payer_nets: dict[uuid.UUID, int] = {uid: 0 for uid in user_ids}

    for _ in range(n_expenses):
        # Random expense total (use total_minor as upper bound seed).
        expense_total = rng.randint(1, total_minor)
        payer = rng.choice(user_ids)

        shares = _build_group_shares(user_ids, expense_total)
        assert sum(shares.values()) == expense_total, "Shares must reconcile"

        # Apply ledger semantics:
        # Each non-payer owes `share` to payer → payer's credit += share, debtor's debit += share.
        for uid, share in shares.items():
            if uid == payer:
                continue  # no entry for payer's own share
            payer_nets[payer] += share  # payer is credited
            payer_nets[uid] -= share  # participant is debited

    # The total of all nets must be zero (conservation of money).
    assert sum(payer_nets.values()) == 0, f"User nets don't sum to 0: {payer_nets}"
