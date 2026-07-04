"""
Auth backend tests: register / login / refresh / me, plus the
actor-authorization gates now wired into the money-mutating endpoints
(POST /expenses, POST /expenses/upload, POST /groups, POST /settlements,
and expense sub-resources: confirm / assignments / refunds / line-items).

Uses the `client` fixture (tests/conftest.py) directly with explicit
Authorization headers (not the auto-auth helper in
tests/auth_test_utils.py, which exists only to keep the pre-auth M1-M4
suites green) so these tests exercise the real register -> login ->
Bearer-token flow end-to-end.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

API = "/api/v1"


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(
    client: AsyncClient, name: str, email: str | None = None, password: str = "correct-horse-battery"
) -> dict:
    resp = await client.post(
        f"{API}/auth/register",
        json={
            "name": name,
            "email": email or f"{name.lower()}-{uuid.uuid4().hex[:8]}@test.com",
            "password": password,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# register / login / refresh / me
# ---------------------------------------------------------------------------


async def test_register_creates_user_and_returns_token_pair(client: AsyncClient) -> None:
    body = await _register(client, "Alice")
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] == 15 * 60
    assert body["user"]["name"] == "Alice"
    assert "password" not in body["user"]
    assert "password_hash" not in body["user"]


async def test_register_duplicate_email_rejected(client: AsyncClient) -> None:
    email = f"dupe-{uuid.uuid4().hex[:8]}@test.com"
    await _register(client, "Alice", email=email)
    resp = await client.post(
        f"{API}/auth/register",
        json={"name": "Alice2", "email": email, "password": "another-password"},
    )
    assert resp.status_code == 409


async def test_register_rejects_short_password(client: AsyncClient) -> None:
    resp = await client.post(
        f"{API}/auth/register",
        json={"name": "Alice", "email": "short@test.com", "password": "short"},
    )
    assert resp.status_code == 422


async def test_login_success(client: AsyncClient) -> None:
    email = f"login-{uuid.uuid4().hex[:8]}@test.com"
    await _register(client, "Alice", email=email, password="hunter2-hunter2")
    resp = await client.post(
        f"{API}/auth/login", json={"email": email, "password": "hunter2-hunter2"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["email"] == email


async def test_login_rejects_bad_password(client: AsyncClient) -> None:
    email = f"login-bad-{uuid.uuid4().hex[:8]}@test.com"
    await _register(client, "Alice", email=email, password="the-real-password")
    resp = await client.post(
        f"{API}/auth/login", json={"email": email, "password": "wrong-password"}
    )
    assert resp.status_code == 401


async def test_login_rejects_unknown_email(client: AsyncClient) -> None:
    resp = await client.post(
        f"{API}/auth/login",
        json={"email": "nobody-here@test.com", "password": "whatever12345"},
    )
    assert resp.status_code == 401


async def test_login_runs_argon2_verify_even_for_unknown_email(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Timing side-channel regression: login must call verify_password even
    when no such user exists, against the fixed DUMMY_PASSWORD_HASH -- never
    short-circuit the (expensive) argon2 verify just because `user is None`.
    Otherwise response time leaks whether an email is registered.
    """
    import app.api.auth as auth_module
    from app.domain.auth import DUMMY_PASSWORD_HASH

    calls: list[tuple[str, str]] = []
    real_verify_password = auth_module.verify_password

    def spy_verify_password(password: str, password_hash: str) -> bool:
        calls.append((password, password_hash))
        return real_verify_password(password, password_hash)

    monkeypatch.setattr(auth_module, "verify_password", spy_verify_password)

    resp = await client.post(
        f"{API}/auth/login",
        json={"email": "definitely-nobody@test.com", "password": "whatever12345"},
    )
    assert resp.status_code == 401
    assert len(calls) == 1
    password, password_hash = calls[0]
    assert password == "whatever12345"
    assert password_hash == DUMMY_PASSWORD_HASH


async def test_login_runs_argon2_verify_for_passwordless_legacy_user(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same timing fix, for a real row with password_hash=NULL."""
    import app.api.auth as auth_module
    from app.domain.auth import DUMMY_PASSWORD_HASH

    email = f"legacy-timing-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post(f"{API}/users", json={"name": "Legacy", "email": email})
    assert resp.status_code == 201

    calls: list[tuple[str, str]] = []
    real_verify_password = auth_module.verify_password

    def spy_verify_password(password: str, password_hash: str) -> bool:
        calls.append((password, password_hash))
        return real_verify_password(password, password_hash)

    monkeypatch.setattr(auth_module, "verify_password", spy_verify_password)

    resp = await client.post(
        f"{API}/auth/login", json={"email": email, "password": "whatever12345"}
    )
    assert resp.status_code == 401
    assert len(calls) == 1
    assert calls[0][1] == DUMMY_PASSWORD_HASH


async def test_login_rejects_user_with_no_password_set(client: AsyncClient) -> None:
    # Created via the legacy passwordless POST /users, not /auth/register.
    email = f"legacy-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post(f"{API}/users", json={"name": "Legacy", "email": email})
    assert resp.status_code == 201
    resp = await client.post(
        f"{API}/auth/login", json={"email": email, "password": "anything12345"}
    )
    assert resp.status_code == 401


async def test_refresh_issues_new_access_token(client: AsyncClient) -> None:
    body = await _register(client, "Alice")
    resp = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": body["refresh_token"]}
    )
    assert resp.status_code == 200, resp.text
    new_body = resp.json()
    assert new_body["access_token"]
    assert new_body["access_token"] != body["access_token"]
    assert new_body["expires_in"] == 15 * 60

    # The new access token actually works against a protected endpoint.
    me = await client.get(f"{API}/auth/me", headers=_auth_headers(new_body["access_token"]))
    assert me.status_code == 200
    assert me.json()["id"] == body["user"]["id"]


async def test_refresh_rejects_access_token(client: AsyncClient) -> None:
    body = await _register(client, "Alice")
    resp = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": body["access_token"]}
    )
    assert resp.status_code == 401


async def test_access_token_rejected_by_refresh_only_is_symmetric(
    client: AsyncClient,
) -> None:
    """A refresh token must not authenticate requests via get_current_user."""
    body = await _register(client, "Alice")
    resp = await client.get(
        f"{API}/auth/me", headers=_auth_headers(body["refresh_token"])
    )
    assert resp.status_code == 401


async def test_me_requires_authentication(client: AsyncClient) -> None:
    resp = await client.get(f"{API}/auth/me")
    assert resp.status_code == 401


async def test_me_rejects_garbage_token(client: AsyncClient) -> None:
    resp = await client.get(f"{API}/auth/me", headers=_auth_headers("not-a-jwt"))
    assert resp.status_code == 401


async def test_me_returns_current_user(client: AsyncClient) -> None:
    body = await _register(client, "Alice")
    resp = await client.get(f"{API}/auth/me", headers=_auth_headers(body["access_token"]))
    assert resp.status_code == 200
    assert resp.json()["email"] == body["user"]["email"]


# ---------------------------------------------------------------------------
# Actor-authorization gates on money-mutating endpoints
# ---------------------------------------------------------------------------


async def test_create_expense_requires_authentication(client: AsyncClient) -> None:
    alice = (await _register(client, "Alice"))["user"]
    # Explicit empty Authorization header defeats tests/auth_test_utils.py's
    # auto-attach (which only fires when no Authorization header is present
    # at all) so this genuinely exercises the unauthenticated path.
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 1000,
            "participants": [alice["id"]],
        },
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401


async def test_create_expense_rejects_mismatched_actor(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    bob = await _register(client, "Bob")
    # Bob is authenticated but the payload claims Alice paid.
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["user"]["id"],
            "total_minor": 1000,
            "participants": [alice["user"]["id"], bob["user"]["id"]],
        },
        headers=_auth_headers(bob["access_token"]),
    )
    assert resp.status_code == 403


async def test_create_expense_succeeds_when_actor_matches_paid_by(
    client: AsyncClient,
) -> None:
    alice = await _register(client, "Alice")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["user"]["id"],
            "total_minor": 1000,
            "participants": [alice["user"]["id"]],
        },
        headers=_auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 201, resp.text


async def test_confirm_expense_rejects_non_member_actor(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    outsider = await _register(client, "Mallory")

    group_resp = await client.post(
        f"{API}/groups",
        json={"name": "Trip", "created_by": alice["user"]["id"]},
        headers=_auth_headers(alice["access_token"]),
    )
    assert group_resp.status_code == 201
    group = group_resp.json()

    expense_resp = await client.post(
        f"{API}/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["user"]["id"],
            "total_minor": 500,
            "participants": [alice["user"]["id"]],
        },
        headers=_auth_headers(alice["access_token"]),
    )
    assert expense_resp.status_code == 201
    expense = expense_resp.json()

    resp = await client.post(
        f"{API}/expenses/{expense['id']}/confirm",
        headers=_auth_headers(outsider["access_token"]),
    )
    assert resp.status_code == 403


async def test_confirm_expense_succeeds_for_group_member_who_is_not_payer(
    client: AsyncClient,
) -> None:
    alice = await _register(client, "Alice")
    bob = await _register(client, "Bob")

    group_resp = await client.post(
        f"{API}/groups",
        json={"name": "Trip", "created_by": alice["user"]["id"]},
        headers=_auth_headers(alice["access_token"]),
    )
    group = group_resp.json()
    add_resp = await client.post(
        f"{API}/groups/{group['id']}/members",
        json={"user_id": bob["user"]["id"]},
        headers=_auth_headers(alice["access_token"]),
    )
    assert add_resp.status_code == 201

    expense_resp = await client.post(
        f"{API}/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["user"]["id"],
            "total_minor": 500,
            "participants": [alice["user"]["id"], bob["user"]["id"]],
        },
        headers=_auth_headers(alice["access_token"]),
    )
    expense = expense_resp.json()

    # Bob is a group member (not the payer) -- allowed to confirm.
    resp = await client.post(
        f"{API}/expenses/{expense['id']}/confirm",
        headers=_auth_headers(bob["access_token"]),
    )
    assert resp.status_code == 200, resp.text


async def test_settlement_rejects_third_party_actor(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    bob = await _register(client, "Bob")
    outsider = await _register(client, "Mallory")

    resp = await client.post(
        f"{API}/settlements",
        json={
            "payer_id": alice["user"]["id"],
            "payee_id": bob["user"]["id"],
            "amount_minor": 100,
        },
        headers=_auth_headers(outsider["access_token"]),
    )
    assert resp.status_code == 403


async def test_group_create_rejects_mismatched_actor(client: AsyncClient) -> None:
    alice = await _register(client, "Alice")
    bob = await _register(client, "Bob")
    resp = await client.post(
        f"{API}/groups",
        json={"name": "Trip", "created_by": alice["user"]["id"]},
        headers=_auth_headers(bob["access_token"]),
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("endpoint_needs_auth", [True])
async def test_group_members_add_requires_active_membership(
    client: AsyncClient, endpoint_needs_auth: bool
) -> None:
    alice = await _register(client, "Alice")
    outsider = await _register(client, "Mallory")
    target = await _register(client, "Target")

    group_resp = await client.post(
        f"{API}/groups",
        json={"name": "Trip", "created_by": alice["user"]["id"]},
        headers=_auth_headers(alice["access_token"]),
    )
    group = group_resp.json()

    resp = await client.post(
        f"{API}/groups/{group['id']}/members",
        json={"user_id": target["user"]["id"]},
        headers=_auth_headers(outsider["access_token"]),
    )
    assert resp.status_code == 403
