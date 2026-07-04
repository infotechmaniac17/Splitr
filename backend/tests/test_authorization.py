"""
Regression tests for the cross-group data leak finding: several read
endpoints (expense detail/pdf/raw-extraction/shares, group detail/balances,
user balance) had zero auth/membership checks, so any authenticated (or
even unauthenticated) caller could read another group's financial data by
guessing or being handed a UUID.

Uses the `client` fixture directly with explicit Authorization headers
(same pattern as tests/test_auth.py), not the auto-auth helper, so these
tests genuinely exercise "user A is authenticated but is NOT a member of
user B's group/expense".
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

API = "/api/v1"


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient, name: str) -> dict:
    resp = await client.post(
        f"{API}/auth/register",
        json={
            "name": name,
            "email": f"{name.lower()}-{uuid.uuid4().hex[:8]}@test.com",
            "password": "correct-horse-battery",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _make_group_and_expense(
    client: AsyncClient, owner: dict
) -> tuple[dict, dict]:
    """Create a group (owned by `owner`) and a personal-free expense inside it."""
    group_resp = await client.post(
        f"{API}/groups",
        json={"name": "Private Trip", "created_by": owner["user"]["id"]},
        headers=_auth_headers(owner["access_token"]),
    )
    assert group_resp.status_code == 201, group_resp.text
    group = group_resp.json()

    expense_resp = await client.post(
        f"{API}/expenses",
        json={
            "group_id": group["id"],
            "paid_by": owner["user"]["id"],
            "total_minor": 500,
            "participants": [owner["user"]["id"]],
        },
        headers=_auth_headers(owner["access_token"]),
    )
    assert expense_resp.status_code == 201, expense_resp.text
    return group, expense_resp.json()


# ---------------------------------------------------------------------------
# Expense detail
# ---------------------------------------------------------------------------


async def test_expense_detail_rejects_non_member(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    mallory = await _register(client, "Mallory")
    _, expense = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/expenses/{expense['id']}",
        headers=_auth_headers(mallory["access_token"]),
    )
    assert resp.status_code == 403


async def test_expense_detail_requires_authentication(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    _, expense = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/expenses/{expense['id']}", headers={"Authorization": ""}
    )
    assert resp.status_code == 401


async def test_expense_detail_allows_group_member(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    bob = await _register(client, "Bob")
    group, expense = await _make_group_and_expense(client, alice)
    add_resp = await client.post(
        f"{API}/groups/{group['id']}/members",
        json={"user_id": bob["user"]["id"]},
        headers=_auth_headers(alice["access_token"]),
    )
    assert add_resp.status_code == 201

    resp = await client.get(
        f"{API}/expenses/{expense['id']}",
        headers=_auth_headers(bob["access_token"]),
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Expense PDF
# ---------------------------------------------------------------------------


async def test_expense_pdf_rejects_non_member(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    mallory = await _register(client, "Mallory")
    _, expense = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/expenses/{expense['id']}/pdf",
        headers=_auth_headers(mallory["access_token"]),
    )
    # Membership gate must fire before the "no PDF stored" 404 -- an
    # outsider should get 403, never learn anything about the expense.
    assert resp.status_code == 403


async def test_expense_pdf_allows_payer(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    _, expense = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/expenses/{expense['id']}/pdf",
        headers=_auth_headers(alice["access_token"]),
    )
    # No PDF was ever uploaded (manual expense) -- but the auth gate passes,
    # so this is a 404 "no PDF stored", not a 403.
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Expense raw-extraction
# ---------------------------------------------------------------------------


async def test_expense_raw_extraction_rejects_non_member(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    mallory = await _register(client, "Mallory")
    _, expense = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/expenses/{expense['id']}/raw-extraction",
        headers=_auth_headers(mallory["access_token"]),
    )
    assert resp.status_code == 403


async def test_expense_raw_extraction_requires_authentication(
    client: AsyncClient,
) -> None:
    alice = await _register(client, "Alice")
    _, expense = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/expenses/{expense['id']}/raw-extraction",
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Expense shares
# ---------------------------------------------------------------------------


async def test_expense_shares_rejects_non_member(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    mallory = await _register(client, "Mallory")
    _, expense = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/expenses/{expense['id']}/shares",
        headers=_auth_headers(mallory["access_token"]),
    )
    assert resp.status_code == 403


async def test_expense_shares_allows_payer(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    _, expense = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/expenses/{expense['id']}/shares",
        headers=_auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Group detail
# ---------------------------------------------------------------------------


async def test_group_detail_rejects_non_member(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    mallory = await _register(client, "Mallory")
    group, _ = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/groups/{group['id']}",
        headers=_auth_headers(mallory["access_token"]),
    )
    assert resp.status_code == 403


async def test_group_detail_requires_authentication(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    group, _ = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/groups/{group['id']}", headers={"Authorization": ""}
    )
    assert resp.status_code == 401


async def test_group_detail_allows_member(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    group, _ = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/groups/{group['id']}",
        headers=_auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Group balances
# ---------------------------------------------------------------------------


async def test_group_balances_rejects_non_member(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    mallory = await _register(client, "Mallory")
    group, _ = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/groups/{group['id']}/balances",
        headers=_auth_headers(mallory["access_token"]),
    )
    assert resp.status_code == 403


async def test_group_balances_allows_member(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    group, _ = await _make_group_and_expense(client, alice)

    resp = await client.get(
        f"{API}/groups/{group['id']}/balances",
        headers=_auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# User balance -- self only (cross-group financial summary)
# ---------------------------------------------------------------------------


async def test_user_balance_rejects_other_user(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    mallory = await _register(client, "Mallory")

    resp = await client.get(
        f"{API}/users/{alice['user']['id']}/balance",
        headers=_auth_headers(mallory["access_token"]),
    )
    assert resp.status_code == 403


async def test_user_balance_rejects_shared_group_member_who_isnt_self(
    client: AsyncClient,
) -> None:
    """
    Even a fellow group member of Alice's may not read Alice's balance --
    compute_user_net_balance aggregates across ALL of Alice's groups, not
    just the one shared with the caller, so "shared group" is not a safe
    carve-out here.
    """
    alice = await _register(client, "Alice")
    bob = await _register(client, "Bob")
    group_resp = await client.post(
        f"{API}/groups",
        json={"name": "Shared", "created_by": alice["user"]["id"]},
        headers=_auth_headers(alice["access_token"]),
    )
    group = group_resp.json()
    await client.post(
        f"{API}/groups/{group['id']}/members",
        json={"user_id": bob["user"]["id"]},
        headers=_auth_headers(alice["access_token"]),
    )

    resp = await client.get(
        f"{API}/users/{alice['user']['id']}/balance",
        headers=_auth_headers(bob["access_token"]),
    )
    assert resp.status_code == 403


async def test_user_balance_allows_self(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")

    resp = await client.get(
        f"{API}/users/{alice['user']['id']}/balance",
        headers=_auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200, resp.text


async def test_user_balance_requires_authentication(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")

    resp = await client.get(
        f"{API}/users/{alice['user']['id']}/balance",
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401
