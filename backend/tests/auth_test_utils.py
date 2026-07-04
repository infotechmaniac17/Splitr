"""
Test-only helper: automatically attaches a valid Bearer access token to
HTTP requests made against the money-mutating endpoints that now require
authentication (POST /expenses, POST /expenses/upload, PUT/POST on an
existing expense's sub-resources, POST /settlements, POST /groups,
POST /groups/{id}/members).

Why this exists: the M1-M4 test suites (tests/test_api.py,
tests/test_api_m2.py, tests/test_hardening.py,
tests/test_upload_and_review.py) were written before real auth existed and
exercise these endpoints purely by passing user ids in the request body
(paid_by, created_by, payer_id, ...) -- exactly the "client-submitted
actor" pattern the new auth layer now cross-checks against the
authenticated caller. Rather than rewrite ~2,000 lines of existing,
already-reviewed test call sites to thread tokens through every call,
this helper inspects each outgoing request the same way a real client
would derive "who am I acting as" (the body/form field that already names
the actor, or the actor recorded when the parent resource -- an expense or
group -- was created) and mints a JWT directly for that user id via
app.domain.auth.create_access_token. This is legitimate for tests only:
it bypasses the password/login flow entirely (which is exactly what a
handcrafted test fixture token normally does), never the DB-level
authorization checks themselves (_assert_actor_authorized_for_expense,
paid_by-matches-caller, group-membership, etc. still run for real).

New auth-specific behavior (register/login/refresh/reject-bad-password,
reject-mismatched-actor, reject-non-member) is covered end-to-end without
this helper in tests/test_auth.py.
"""

from __future__ import annotations

import re
import uuid as uuid_mod
from typing import Any
from urllib.parse import urlparse

from httpx import AsyncClient, Response

from app.config import settings
from app.domain.auth import create_access_token

_EXPENSE_SUBRESOURCE_RE = re.compile(
    r"^/api/v1/expenses/([0-9a-fA-F-]{36})/(confirm|assignments|refunds|line-items)$"
)
_GROUP_MEMBERS_RE = re.compile(r"^/api/v1/groups/([0-9a-fA-F-]{36})/members$")
# Read endpoints that gained membership/actor checks (cross-group data leak
# fix) -- also need auto-auth so the pre-auth M1-M4 suites keep passing.
_EXPENSE_READ_RE = re.compile(
    r"^/api/v1/expenses/([0-9a-fA-F-]{36})(?:/(?:pdf|raw-extraction|shares))?$"
)
_GROUP_READ_RE = re.compile(
    r"^/api/v1/groups/([0-9a-fA-F-]{36})(?:/balances|/simplified-debts)?$"
)
_USER_BALANCE_RE = re.compile(r"^/api/v1/users/([0-9a-fA-F-]{36})/balance$")
# GET /users/{id} (profile) -- gained a self-or-shared-group auth gate (PII
# leak fix). The pre-auth suites only ever fetch a user's own just-created
# profile this way, so auto-auth "as themself" (self case always returns the
# full profile, matching what these tests assert).
_USER_PROFILE_RE = re.compile(r"^/api/v1/users/([0-9a-fA-F-]{36})$")


class _AutoAuthRegistry:
    def __init__(self) -> None:
        self._token_by_user: dict[str, str] = {}
        self._paid_by_of_expense: dict[str, str] = {}
        self._creator_of_group: dict[str, str] = {}

    def token_for(self, user_id: str) -> str:
        if user_id not in self._token_by_user:
            self._token_by_user[user_id] = create_access_token(
                uuid_mod.UUID(user_id), settings.SECRET_KEY
            )
        return self._token_by_user[user_id]

    def resolve_actor(
        self,
        method: str,
        path: str,
        json_body: Any,
        form_data: Any,
    ) -> str | None:
        method = method.upper()
        if method == "POST" and path == "/api/v1/expenses":
            return (json_body or {}).get("paid_by")
        if method == "POST" and path == "/api/v1/expenses/upload":
            return (form_data or {}).get("paid_by")
        if method == "POST" and path == "/api/v1/settlements":
            return (json_body or {}).get("payer_id")
        if method == "POST" and path == "/api/v1/groups":
            return (json_body or {}).get("created_by")
        m = _EXPENSE_SUBRESOURCE_RE.match(path)
        if m and method in ("POST", "PUT"):
            return self._paid_by_of_expense.get(m.group(1))
        m2 = _GROUP_MEMBERS_RE.match(path)
        if m2 and method == "POST":
            return self._creator_of_group.get(m2.group(1))
        if method == "GET":
            m3 = _EXPENSE_READ_RE.match(path)
            if m3:
                return self._paid_by_of_expense.get(m3.group(1))
            m4 = _GROUP_READ_RE.match(path)
            if m4:
                return self._creator_of_group.get(m4.group(1))
            m5 = _USER_BALANCE_RE.match(path)
            if m5:
                return m5.group(1)
            m6 = _USER_PROFILE_RE.match(path)
            if m6:
                return m6.group(1)
        return None

    def observe(self, method: str, path: str, resp: Response) -> None:
        if resp.status_code >= 400:
            return
        try:
            body = resp.json()
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        if method == "POST" and path in ("/api/v1/expenses", "/api/v1/expenses/upload"):
            eid, paid_by = body.get("id"), body.get("paid_by")
            if eid and paid_by:
                self._paid_by_of_expense[str(eid)] = str(paid_by)
        elif method == "POST" and path == "/api/v1/groups":
            gid, created_by = body.get("id"), body.get("created_by")
            if gid and created_by:
                self._creator_of_group[str(gid)] = str(created_by)


def attach_auto_auth(ac: AsyncClient) -> AsyncClient:
    """Monkeypatch `ac.request` to auto-attach a Bearer token. See module docstring."""
    registry = _AutoAuthRegistry()
    original_request = ac.request

    async def patched_request(method: str, url: Any, **kwargs: Any) -> Response:
        headers = kwargs.get("headers")
        headers_dict: dict[str, str] = dict(headers) if headers else {}
        has_auth = any(k.lower() == "authorization" for k in headers_dict)
        path = urlparse(str(url)).path
        if not has_auth:
            actor = registry.resolve_actor(
                method, path, kwargs.get("json"), kwargs.get("data")
            )
            if actor:
                headers_dict["Authorization"] = f"Bearer {registry.token_for(actor)}"
                kwargs["headers"] = headers_dict
        resp = await original_request(method, url, **kwargs)
        registry.observe(method.upper(), path, resp)
        return resp

    ac.request = patched_request  # type: ignore[method-assign]
    return ac
