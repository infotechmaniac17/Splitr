"""
Debt simplification (min-cash-flow) — M6.

Pure domain logic, no I/O: takes pairwise net balances (as produced by
`app.domain.ledger.compute_group_balances`) and produces a minimal set of
*suggested* transactions that would zero out every participant's net
balance, via the greedy "largest creditor meets largest debtor" algorithm
described in ARCHITECTURE.md §3:

    "if simplify_debts, run greedy min-cash-flow (repeatedly match largest
    debtor with largest creditor) — O(n log n), provably <= n-1
    transactions."

IMPORTANT: this module never touches the database and never posts to the
ledger. It only *suggests* transactions. Actually recording a settlement
still goes through `app.domain.ledger.post_settlement_to_ledger` (via the
POST /settlements endpoint), which is the only code path allowed to write
settlement ledger entries. Blurring that line would let a client "confirm"
a settlement simply by asking for simplified debts, bypassing the
append-only settlement audit trail.
"""

from __future__ import annotations

import heapq
import uuid

# A pairwise balance triple, matching the shape returned by
# ledger.compute_group_balances: (debtor_id, creditor_id, net_amount_minor)
# with net_amount_minor > 0 (debtor owes creditor that amount).
PairwiseBalance = tuple[uuid.UUID, uuid.UUID, int]

# A suggested transaction: (payer_id, payee_id, amount_minor). Paying
# amount_minor from payer_id to payee_id would settle that portion of debt.
SuggestedTransaction = tuple[uuid.UUID, uuid.UUID, int]


def simplify_group_debts(
    pairwise_balances: list[PairwiseBalance],
) -> list[SuggestedTransaction]:
    """
    Greedy min-cash-flow debt simplification.

    Args:
        pairwise_balances: list of (debtor_id, creditor_id, net_amount_minor)
            triples, each net_amount_minor > 0, e.g. as returned by
            `ledger.compute_group_balances`. Pairs that already cancel to
            zero must be omitted by the caller (compute_group_balances
            already does this).

    Returns:
        A list of (payer_id, payee_id, amount_minor) suggested transactions
        such that, if every one of them were actually settled, every
        participant's net balance would become exactly zero. At most
        n-1 transactions are returned for n participants with a nonzero
        net balance (each matching step fully zeroes out at least one
        participant, so the count of remaining nonzero-balance
        participants strictly decreases every iteration).

    Raises:
        ValueError: if any input net_amount_minor is not > 0 (a malformed
            pairwise balance -- see compute_group_balances's invariant that
            net_amount_minor is always positive).
        AssertionError: if the net balances don't sum to zero (the ledger
            is a closed system -- see ledger.py module docstring), or if
            the output transactions fail to reconcile exactly against the
            input, or a self-payment / non-positive amount was generated
            (all indicate an algorithm bug, never a caller input problem).

    This function performs NO I/O and does not mutate the ledger; see the
    module docstring.
    """
    # Step 1: net every user to a single signed balance.
    #   net[creditor] += amount ; net[debtor] -= amount
    net: dict[uuid.UUID, int] = {}
    for debtor_id, creditor_id, amount in pairwise_balances:
        if amount <= 0:
            raise ValueError(
                f"net_amount_minor must be > 0, got {amount} for "
                f"({debtor_id}, {creditor_id})"
            )
        net[debtor_id] = net.get(debtor_id, 0) - amount
        net[creditor_id] = net.get(creditor_id, 0) + amount

    # Step 2: the ledger is a closed system -- money owed must equal money
    # due, so the sum of all net balances must be exactly zero.
    if sum(net.values()) != 0:
        raise AssertionError(
            "Net balances do not sum to zero -- the ledger is not a closed "
            f"system (sum={sum(net.values())}). This indicates a bug in "
            "the caller's balance computation, not bad user input."
        )

    # Snapshot of what each user's net balance *should* be once every
    # suggested transaction is applied -- used for the final reconciliation
    # assertion below.
    expected_net = dict(net)

    # Step 3: max-heaps (via negation, since heapq is a min-heap) of
    # creditors (positive net) and debtors (negative net).
    creditors: list[tuple[int, uuid.UUID]] = []
    debtors: list[tuple[int, uuid.UUID]] = []
    for user_id, amount in net.items():
        if amount > 0:
            heapq.heappush(creditors, (-amount, user_id))
        elif amount < 0:
            # amount is already negative, so this min-heap naturally pops
            # the most negative (largest debt) first.
            heapq.heappush(debtors, (amount, user_id))

    # Step 4: repeatedly match the largest creditor with the largest
    # debtor, settling min(|amounts|), until all balances are zero.
    transactions: list[SuggestedTransaction] = []
    while creditors and debtors:
        neg_credit, creditor_id = heapq.heappop(creditors)
        credit_amount = -neg_credit
        neg_debt, debtor_id = heapq.heappop(debtors)
        debt_amount = -neg_debt

        settle = min(credit_amount, debt_amount)
        transactions.append((debtor_id, creditor_id, settle))

        remaining_credit = credit_amount - settle
        remaining_debt = debt_amount - settle
        if remaining_credit > 0:
            heapq.heappush(creditors, (-remaining_credit, creditor_id))
        if remaining_debt > 0:
            heapq.heappush(debtors, (-remaining_debt, debtor_id))

    # Defensive: a closed (sum-zero) system must resolve both heaps
    # completely in lockstep.
    if creditors or debtors:  # pragma: no cover - unreachable if sum==0
        raise AssertionError(
            "Debt simplification failed to fully resolve all balances "
            f"(remaining creditors={creditors}, debtors={debtors})"
        )

    # Step 5: reconcile -- replaying the suggested transactions must
    # reproduce exactly the input net balances, unit for unit. No minor
    # unit dropped or duplicated, and no self-payments.
    replay: dict[uuid.UUID, int] = {}
    for payer_id, payee_id, amount in transactions:
        if payer_id == payee_id:  # pragma: no cover - unreachable by construction
            raise AssertionError(f"Self-payment generated for user {payer_id}")
        if amount <= 0:  # pragma: no cover - unreachable by construction
            raise AssertionError(f"Non-positive transaction amount {amount}")
        replay[payer_id] = replay.get(payer_id, 0) - amount
        replay[payee_id] = replay.get(payee_id, 0) + amount

    for user_id, expected in expected_net.items():
        actual = replay.get(user_id, 0)
        if actual != expected:  # pragma: no cover - unreachable by construction
            raise AssertionError(
                f"Reconciliation failed for user {user_id}: expected net "
                f"{expected}, got {actual} from suggested transactions"
            )

    assert len(transactions) <= max(len(expected_net) - 1, 0), (
        "Debt simplification produced more than n-1 transactions "
        f"({len(transactions)} for {len(expected_net)} participants)"
    )

    return transactions
