"""
End-to-end API tests using AsyncClient + in-memory SQLite.

Covers:
  - POST /api/v1/users — create user, duplicate email rejected
  - POST /api/v1/groups — create group (creator auto-added as admin)
  - POST /api/v1/groups/{id}/members — add member
  - POST /api/v1/expenses — create manual expense (equal split)
  - POST /api/v1/expenses/{id}/confirm — confirm and post to ledger
  - GET /api/v1/groups/{id}/balances — correct after confirm
  - GET /api/v1/users/{id}/balance — correct after confirm
  - POST /api/v1/settlements — records payment, reduces balance
  - Equal split with non-divisible total reconciles exactly
  - Explicit shares validation (bad sum rejected)
  - Property-style test: random totals with equal splits always produce
    correct balances
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


async def create_user(client: AsyncClient, name: str, email: str) -> dict:
    resp = await client.post("/api/v1/users", json={"name": name, "email": email})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_group(client: AsyncClient, name: str, created_by: str) -> dict:
    resp = await client.post(
        "/api/v1/groups", json={"name": name, "created_by": created_by}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def add_member(client: AsyncClient, group_id: str, user_id: str) -> dict:
    resp = await client.post(
        f"/api/v1/groups/{group_id}/members", json={"user_id": user_id}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_expense(
    client: AsyncClient,
    group_id: str,
    paid_by: str,
    total_minor: int,
    participants: list[str] | None = None,
    shares: dict[str, int] | None = None,
) -> dict:
    payload: dict = {
        "group_id": group_id,
        "paid_by": paid_by,
        "vendor": "Test Vendor",
        "currency": "INR",
        "total_minor": total_minor,
    }
    if participants is not None:
        payload["participants"] = participants
    if shares is not None:
        payload["shares"] = shares
    resp = await client.post("/api/v1/expenses", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def confirm_expense(client: AsyncClient, expense_id: str) -> dict:
    resp = await client.post(f"/api/v1/expenses/{expense_id}/confirm")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def get_balances(client: AsyncClient, group_id: str) -> dict:
    resp = await client.get(f"/api/v1/groups/{group_id}/balances")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def get_user_balance(client: AsyncClient, user_id: str) -> dict:
    resp = await client.get(f"/api/v1/users/{user_id}/balance")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def settle(
    client: AsyncClient,
    group_id: str,
    payer_id: str,
    payee_id: str,
    amount: int,
    method: str = "cash",
) -> dict:
    resp = await client.post(
        "/api/v1/settlements",
        json={
            "group_id": group_id,
            "payer_id": payer_id,
            "payee_id": payee_id,
            "amount_minor": amount,
            "method": method,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# User tests
# ---------------------------------------------------------------------------


async def test_create_user(client: AsyncClient) -> None:
    user = await create_user(client, "Alice", "alice@example.com")
    assert user["name"] == "Alice"
    assert user["email"] == "alice@example.com"
    assert "id" in user


async def test_duplicate_email_rejected(client: AsyncClient) -> None:
    await create_user(client, "Alice", "alice@example.com")
    resp = await client.post(
        "/api/v1/users", json={"name": "Alice2", "email": "alice@example.com"}
    )
    assert resp.status_code == 409


async def test_get_user(client: AsyncClient) -> None:
    user = await create_user(client, "Alice", "alice@test.com")
    resp = await client.get(f"/api/v1/users/{user['id']}")
    assert resp.status_code == 200
    assert resp.json()["email"] == "alice@test.com"


async def test_get_user_not_found(client: AsyncClient) -> None:
    # No registered user exists for this random UUID, so the auto-auth test
    # helper can't mint a valid token for it either -- the auth gate (PII
    # leak fix) now rejects with 401 before the route's own 404 check runs.
    # See tests/test_authorization.py for the authenticated 404/403 cases.
    resp = await client.get(f"/api/v1/users/{uuid.uuid4()}")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Group tests
# ---------------------------------------------------------------------------


async def test_create_group(client: AsyncClient) -> None:
    alice = await create_user(client, "Alice", "alice@example.com")
    group = await create_group(client, "Goa Trip", alice["id"])
    assert group["name"] == "Goa Trip"
    assert group["created_by"] == alice["id"]


async def test_add_member_to_group(client: AsyncClient) -> None:
    alice = await create_user(client, "Alice", "alice@example.com")
    bob = await create_user(client, "Bob", "bob@example.com")
    group = await create_group(client, "Dinner", alice["id"])
    member = await add_member(client, group["id"], bob["id"])
    assert member["user_id"] == bob["id"]
    assert member["group_id"] == group["id"]


async def test_add_duplicate_member_rejected(client: AsyncClient) -> None:
    alice = await create_user(client, "Alice", "alice@example.com")
    group = await create_group(client, "Dinner", alice["id"])
    # Alice is already a member (auto-added at creation).
    resp = await client.post(
        f"/api/v1/groups/{group['id']}/members",
        json={"user_id": alice["id"]},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Expense + confirm + balances
# ---------------------------------------------------------------------------


async def test_create_confirm_expense_equal_split(client: AsyncClient) -> None:
    """
    Full flow: create users → group → expense (equal split) → confirm →
    verify balances and user net balance.
    """
    alice = await create_user(client, "Alice", "alice@example.com")
    bob = await create_user(client, "Bob", "bob@example.com")
    group = await create_group(client, "Test", alice["id"])
    await add_member(client, group["id"], bob["id"])

    # Alice pays 1000, split equally.
    expense = await create_expense(
        client,
        group["id"],
        alice["id"],
        total_minor=1000,
        participants=[alice["id"], bob["id"]],
    )
    assert expense["parse_status"] == "parsed"

    confirmed = await confirm_expense(client, expense["id"])
    assert confirmed["parse_status"] == "confirmed"

    # Balances: Bob owes Alice 500.
    balances = await get_balances(client, group["id"])
    assert len(balances["balances"]) == 1
    b = balances["balances"][0]
    assert b["debtor_id"] == bob["id"]
    assert b["creditor_id"] == alice["id"]
    assert b["net_amount_minor"] == 500

    # User net balances.
    alice_bal = await get_user_balance(client, alice["id"])
    bob_bal = await get_user_balance(client, bob["id"])
    assert alice_bal["net_balance_minor"] == 500
    assert bob_bal["net_balance_minor"] == -500


async def test_expense_with_explicit_shares(client: AsyncClient) -> None:
    """Explicit shares work and are validated."""
    alice = await create_user(client, "Alice", "alice@example.com")
    bob = await create_user(client, "Bob", "bob@example.com")
    group = await create_group(client, "Test", alice["id"])
    await add_member(client, group["id"], bob["id"])

    expense = await create_expense(
        client,
        group["id"],
        alice["id"],
        total_minor=1000,
        shares={alice["id"]: 300, bob["id"]: 700},
    )
    await confirm_expense(client, expense["id"])

    balances = await get_balances(client, group["id"])
    b = balances["balances"][0]
    assert b["debtor_id"] == bob["id"]
    assert b["net_amount_minor"] == 700


async def test_expense_bad_shares_sum_rejected(client: AsyncClient) -> None:
    """Explicit shares that don't sum to total_minor are rejected at API level."""
    alice = await create_user(client, "Alice", "alice@example.com")
    bob = await create_user(client, "Bob", "bob@example.com")
    group = await create_group(client, "Test", alice["id"])
    await add_member(client, group["id"], bob["id"])

    resp = await client.post(
        "/api/v1/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["id"],
            "total_minor": 1000,
            "shares": {alice["id"]: 300, bob["id"]: 600},  # sum=900 != 1000
        },
    )
    assert resp.status_code == 422


async def test_expense_must_provide_split_spec(client: AsyncClient) -> None:
    """Expense without participants or shares is rejected."""
    alice = await create_user(client, "Alice", "alice@example.com")
    group = await create_group(client, "Test", alice["id"])

    resp = await client.post(
        "/api/v1/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["id"],
            "total_minor": 1000,
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


async def test_settlement_reduces_balance(client: AsyncClient) -> None:
    """Settlement reduces the pairwise balance correctly."""
    alice = await create_user(client, "Alice", "alice@example.com")
    bob = await create_user(client, "Bob", "bob@example.com")
    group = await create_group(client, "Test", alice["id"])
    await add_member(client, group["id"], bob["id"])

    expense = await create_expense(
        client, group["id"], alice["id"], 1000, participants=[alice["id"], bob["id"]]
    )
    await confirm_expense(client, expense["id"])

    # Bob pays Alice 300.
    await settle(client, group["id"], bob["id"], alice["id"], 300, "upi")

    balances = await get_balances(client, group["id"])
    b = balances["balances"][0]
    assert b["net_amount_minor"] == 200  # 500 - 300


async def test_full_settlement_clears_balance(client: AsyncClient) -> None:
    """Full settlement results in empty balance list."""
    alice = await create_user(client, "Alice", "alice@example.com")
    bob = await create_user(client, "Bob", "bob@example.com")
    group = await create_group(client, "Test", alice["id"])
    await add_member(client, group["id"], bob["id"])

    expense = await create_expense(
        client, group["id"], alice["id"], 1000, participants=[alice["id"], bob["id"]]
    )
    await confirm_expense(client, expense["id"])
    await settle(client, group["id"], bob["id"], alice["id"], 500, "bank")

    balances = await get_balances(client, group["id"])
    assert balances["balances"] == []

    alice_bal = await get_user_balance(client, alice["id"])
    bob_bal = await get_user_balance(client, bob["id"])
    assert alice_bal["net_balance_minor"] == 0
    assert bob_bal["net_balance_minor"] == 0


# ---------------------------------------------------------------------------
# Non-divisible totals reconcile exactly
# ---------------------------------------------------------------------------


async def test_non_divisible_total_reconciles(client: AsyncClient) -> None:
    """
    1 paise split 3 ways can't be divided equally, but must still
    reconcile exactly with largest-remainder rounding.
    """
    alice = await create_user(client, "Alice", "alice@example.com")
    bob = await create_user(client, "Bob", "bob@example.com")
    carol = await create_user(client, "Carol", "carol@example.com")
    group = await create_group(client, "Test", alice["id"])
    await add_member(client, group["id"], bob["id"])
    await add_member(client, group["id"], carol["id"])

    # 100 paise split 3 ways → 34 + 33 + 33 = 100.
    expense = await create_expense(
        client,
        group["id"],
        alice["id"],
        100,
        participants=[alice["id"], bob["id"], carol["id"]],
    )
    await confirm_expense(client, expense["id"])

    # Net: Alice is creditor for 100 - her own share.
    # The two debtors' shares sum exactly to what Alice is owed.
    balances = await get_balances(client, group["id"])
    total_owed = sum(b["net_amount_minor"] for b in balances["balances"])

    # Alice paid 100; her share is ~33. Others owe ~67 total.
    # Regardless of exact rounding, total_owed + alice_share == 100.
    alice_bal = await get_user_balance(client, alice["id"])
    assert alice_bal["net_balance_minor"] == total_owed


async def test_prime_total_three_users(client: AsyncClient) -> None:
    """97 paise (prime) split 3 ways — must always sum to 97."""
    alice = await create_user(client, "Alice", "alice@example.com")
    bob = await create_user(client, "Bob", "bob@example.com")
    carol = await create_user(client, "Carol", "carol@example.com")
    group = await create_group(client, "Test", alice["id"])
    await add_member(client, group["id"], bob["id"])
    await add_member(client, group["id"], carol["id"])

    expense = await create_expense(
        client,
        group["id"],
        alice["id"],
        97,
        participants=[alice["id"], bob["id"], carol["id"]],
    )
    confirmed = await confirm_expense(client, expense["id"])
    assert confirmed["parse_status"] == "confirmed"
    assert confirmed["total_minor"] == 97

    # Validate balances sum = alice's net (everyone else owes alice).
    await get_balances(client, group["id"])
    alice_bal = await get_user_balance(client, alice["id"])
    bob_bal = await get_user_balance(client, bob["id"])
    carol_bal = await get_user_balance(client, carol["id"])

    # Conservation: all nets sum to 0.
    assert alice_bal["net_balance_minor"] + bob_bal["net_balance_minor"] + carol_bal["net_balance_minor"] == 0


# ---------------------------------------------------------------------------
# Property-style test: random expenses always produce correct balances
# ---------------------------------------------------------------------------


@given(
    total_minor=st.integers(min_value=1, max_value=1_000_000),
    n_participants=st.integers(min_value=2, max_value=5),
)
@settings(max_examples=50, deadline=5000)
def test_random_expense_nets_sum_to_zero(
    total_minor: int,
    n_participants: int,
) -> None:
    """
    Property: for any total and any number of participants in an equal split,
    the sum of all user net balances is zero (pure math check).
    """
    from fractions import Fraction

    from app.domain.rounding import allocate_largest_remainder

    user_ids = [uuid.uuid4() for _ in range(n_participants)]
    ratios: dict[uuid.UUID, Fraction] = {uid: Fraction(1, n_participants) for uid in user_ids}
    shares = allocate_largest_remainder(total_minor, ratios)

    assert sum(shares.values()) == total_minor

    # Payer is user_ids[0]; compute net balances.
    payer = user_ids[0]
    nets: dict[uuid.UUID, int] = {uid: 0 for uid in user_ids}
    for uid, share in shares.items():
        if uid == payer:
            continue
        nets[payer] += share  # payer is credited
        nets[uid] -= share    # participant is debited

    assert sum(nets.values()) == 0


# ---------------------------------------------------------------------------
# Health check (sanity)
# ---------------------------------------------------------------------------


async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
