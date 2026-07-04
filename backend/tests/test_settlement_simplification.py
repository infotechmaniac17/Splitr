"""
Tests for app.domain.settlement_simplification (M6 debt simplification).

Covers:
  - Pure unit tests: simple triangle debt, already-zero net, negative/zero
    amount rejected.
  - Property-style test: randomized multi-user debt graphs always produce
    <= n-1 transactions, reconcile exactly against the input net balances,
    and never contain a self-payment.
  - Empty input -> empty output.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.settlement_simplification import simplify_group_debts

API = "/api/v1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _net_from_pairwise(
    pairwise: list[tuple[uuid.UUID, uuid.UUID, int]],
) -> dict[uuid.UUID, int]:
    net: dict[uuid.UUID, int] = {}
    for debtor_id, creditor_id, amount in pairwise:
        net[debtor_id] = net.get(debtor_id, 0) - amount
        net[creditor_id] = net.get(creditor_id, 0) + amount
    return net


def _net_from_transactions(
    transactions: list[tuple[uuid.UUID, uuid.UUID, int]],
) -> dict[uuid.UUID, int]:
    net: dict[uuid.UUID, int] = {}
    for payer_id, payee_id, amount in transactions:
        net[payer_id] = net.get(payer_id, 0) - amount
        net[payee_id] = net.get(payee_id, 0) + amount
    return net


# ---------------------------------------------------------------------------
# Basic unit tests
# ---------------------------------------------------------------------------


def test_empty_input_yields_empty_output() -> None:
    assert simplify_group_debts([]) == []


def test_simple_pair_passthrough() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    result = simplify_group_debts([(a, b, 500)])
    assert result == [(a, b, 500)]


def test_triangle_debt_simplifies_to_two_transactions() -> None:
    """
    Classic min-cash-flow example: A owes B 10, B owes C 10, so really A
    should just pay C 10 and B is fully settled with 0 involvement -- but
    with 3 participants (A owes 10, C is owed 10, B nets to 0), the greedy
    algorithm should produce exactly 1 transaction (A -> C, 10), not 2.
    """
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pairwise = [(a, b, 1000), (b, c, 1000)]
    result = simplify_group_debts(pairwise)
    assert result == [(a, c, 1000)]


def test_already_net_zero_per_user_yields_empty_output() -> None:
    """A owes B 500 and B owes A 500 nets to exactly zero for both users."""
    a, b = uuid.uuid4(), uuid.uuid4()
    result = simplify_group_debts([(a, b, 500), (b, a, 500)])
    assert result == []


def test_rejects_non_positive_amount() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    try:
        simplify_group_debts([(a, b, 0)])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-positive amount")

    try:
        simplify_group_debts([(a, b, -10)])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for negative amount")


def test_no_self_payments_and_at_most_n_minus_1_transactions() -> None:
    users = [uuid.uuid4() for _ in range(5)]
    # A star of debts: users[1..4] each owe users[0] varying amounts.
    pairwise = [(users[i], users[0], 100 * i) for i in range(1, 5)]
    result = simplify_group_debts(pairwise)
    assert len(result) <= len(users) - 1
    for payer, payee, amount in result:
        assert payer != payee
        assert amount > 0


# ---------------------------------------------------------------------------
# Property-style test: randomized multi-user debt graphs
# ---------------------------------------------------------------------------


@given(
    n_users=st.integers(min_value=2, max_value=8),
    data=st.data(),
)
@settings(max_examples=200)
def test_property_random_debt_graphs_reconcile(n_users: int, data: st.DataObject) -> None:
    users = [uuid.uuid4() for _ in range(n_users)]

    n_edges = data.draw(st.integers(min_value=0, max_value=n_users * 3))
    pairwise: list[tuple[uuid.UUID, uuid.UUID, int]] = []
    for _ in range(n_edges):
        i, j = data.draw(
            st.tuples(
                st.integers(min_value=0, max_value=n_users - 1),
                st.integers(min_value=0, max_value=n_users - 1),
            ).filter(lambda pair: pair[0] != pair[1])
        )
        amount = data.draw(st.integers(min_value=1, max_value=100_000))
        pairwise.append((users[i], users[j], amount))

    expected_net = _net_from_pairwise(pairwise)

    result = simplify_group_debts(pairwise)

    # (a) at most n-1 transactions for the participants actually involved.
    n_participants = len({u for u, amt in expected_net.items() if amt != 0})
    assert len(result) <= max(n_participants - 1, 0)

    # (b) sums reconcile exactly against the input net balances.
    actual_net = _net_from_transactions(result)
    for user_id, expected_amount in expected_net.items():
        assert actual_net.get(user_id, 0) == expected_amount
    # No extra users introduced, and no minor unit created out of thin air.
    for user_id, amount in actual_net.items():
        assert expected_net.get(user_id, 0) == amount

    # (c) no self-payments, all amounts strictly positive.
    for payer, payee, amount in result:
        assert payer != payee
        assert amount > 0

    # (d) balances that already net to zero for every user yield no output.
    if all(v == 0 for v in expected_net.values()):
        assert result == []


# ---------------------------------------------------------------------------
# Integration: GET /groups/{id}/simplified-debts endpoint gating
# ---------------------------------------------------------------------------


async def _create_user(client: AsyncClient, name: str, email: str) -> dict:
    resp = await client.post(API + "/users", json={"name": name, "email": email})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_group(
    client: AsyncClient, name: str, created_by: str, simplify_debts: bool = True
) -> dict:
    resp = await client.post(
        API + "/groups",
        json={"name": name, "created_by": created_by, "simplify_debts": simplify_debts},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _add_member(client: AsyncClient, group_id: str, user_id: str) -> None:
    resp = await client.post(
        f"{API}/groups/{group_id}/members", json={"user_id": user_id}
    )
    assert resp.status_code == 201, resp.text


async def _create_and_confirm_expense(
    client: AsyncClient,
    group_id: str,
    paid_by: str,
    total_minor: int,
    participants: list[str],
) -> None:
    resp = await client.post(
        API + "/expenses",
        json={
            "group_id": group_id,
            "paid_by": paid_by,
            "total_minor": total_minor,
            "participants": participants,
        },
    )
    assert resp.status_code == 201, resp.text
    expense_id = resp.json()["id"]
    confirm_resp = await client.post(f"{API}/expenses/{expense_id}/confirm")
    assert confirm_resp.status_code == 200, confirm_resp.text


async def _make_chain_debt_group(
    client: AsyncClient, simplify_debts: bool
) -> tuple[dict, dict, dict, dict]:
    """
    Alice pays 300 split equally among {alice, bob, carol} (bob and carol
    each owe alice 100), then bob pays 300 split equally among {bob, carol}
    (carol owes bob 150). Net: carol owes 250, of which bob nets to +50
    (150 - 100) and alice nets to +200 (100 + 100). This is NOT already
    pairwise-simplified (carol has a direct debt to both alice and bob), so
    it exercises real min-cash-flow reduction.
    """
    alice = await _create_user(client, "Alice", f"alice-{uuid.uuid4().hex[:8]}@t.com")
    bob = await _create_user(client, "Bob", f"bob-{uuid.uuid4().hex[:8]}@t.com")
    carol = await _create_user(client, "Carol", f"carol-{uuid.uuid4().hex[:8]}@t.com")

    group = await _create_group(client, "Trip", alice["id"], simplify_debts=simplify_debts)
    await _add_member(client, group["id"], bob["id"])
    await _add_member(client, group["id"], carol["id"])

    await _create_and_confirm_expense(
        client, group["id"], alice["id"], 300, [alice["id"], bob["id"], carol["id"]]
    )
    await _create_and_confirm_expense(
        client, group["id"], bob["id"], 300, [bob["id"], carol["id"]]
    )
    return alice, bob, carol, group


async def test_simplified_debts_endpoint_default_true_reduces_transactions(
    client: AsyncClient,
) -> None:
    alice, bob, carol, group = await _make_chain_debt_group(client, simplify_debts=True)

    raw_resp = await client.get(f"{API}/groups/{group['id']}/balances")
    assert raw_resp.status_code == 200, raw_resp.text
    raw_balances = raw_resp.json()["balances"]

    resp = await client.get(f"{API}/groups/{group['id']}/simplified-debts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["simplified"] is True

    transactions = body["transactions"]
    # 3 participants with nonzero net -> at most 2 transactions.
    assert len(transactions) <= 2
    # Simplification must actually reduce (or match) the raw edge count,
    # never introduce more transactions than the un-simplified graph.
    assert len(transactions) <= len(raw_balances)

    # Reconcile: net balance per user from simplified transactions must
    # match net balance per user from the raw (unsimplified) balances.
    def _net(entries: list[dict], payer_key: str, payee_key: str, amount_key: str) -> dict:
        net: dict[str, int] = {}
        for e in entries:
            net[e[payer_key]] = net.get(e[payer_key], 0) - e[amount_key]
            net[e[payee_key]] = net.get(e[payee_key], 0) + e[amount_key]
        return net

    raw_net = _net(raw_balances, "debtor_id", "creditor_id", "net_amount_minor")
    simplified_net = _net(transactions, "payer_id", "payee_id", "amount_minor")
    assert simplified_net == raw_net

    for txn in transactions:
        assert txn["payer_id"] != txn["payee_id"]
        assert txn["amount_minor"] > 0


async def test_simplified_debts_endpoint_false_returns_raw_balances(
    client: AsyncClient,
) -> None:
    alice, bob, carol, group = await _make_chain_debt_group(client, simplify_debts=False)

    raw_resp = await client.get(f"{API}/groups/{group['id']}/balances")
    assert raw_resp.status_code == 200, raw_resp.text
    raw_balances = raw_resp.json()["balances"]

    resp = await client.get(f"{API}/groups/{group['id']}/simplified-debts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["simplified"] is False

    transactions = body["transactions"]
    assert len(transactions) == len(raw_balances)
    got = {(t["payer_id"], t["payee_id"], t["amount_minor"]) for t in transactions}
    expected = {
        (b["debtor_id"], b["creditor_id"], b["net_amount_minor"]) for b in raw_balances
    }
    assert got == expected
