"""
Pure auth primitives: password hashing and JWT issuance/verification.

Kept in app/domain/ (mypy strict, no I/O) per CLAUDE.md -- these functions
take everything they need as arguments (secret key, expiry) rather than
reaching into app.config or a database themselves, so they're trivially
testable and so app/api/auth.py (thin) stays the only place that wires them
to a request/response and a DB session.

Token shapes (documented here as the single source of truth -- keep the
frontend/mobile clients and app/api/auth.py in sync with this):

  access token  (type="access"):  15 minutes, used to authenticate requests
                                   via `Authorization: Bearer <token>`.
  refresh token (type="refresh"): 30 days, used ONLY against
                                   POST /auth/refresh to mint a new access
                                   token. Never accepted by get_current_user.

Both are signed HS256 JWTs with claims:
    sub  -- user id (str(UUID))
    type -- "access" | "refresh"
    iat  -- issued-at (unix timestamp)
    exp  -- expiry (unix timestamp)
    jti  -- random per-token id (uuid4 hex; not tracked server-side in this
            pass -- refresh tokens are stateless/unrevocable until a
            denylist or rotation store is added, see CLAUDE.md follow-ups)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 30

TokenType = Literal["access", "refresh"]

_password_hasher = PasswordHasher()

# Fixed dummy argon2id hash (of an arbitrary, never-used password, hashed
# once at import time with the exact same parameters real user hashes use)
# so that "user not found" and "user has no password set" login paths still
# pay the same argon2 verification cost as a real user lookup. Without this,
# a short-circuiting `if not user or not verify_password(...)` skips the
# expensive hash entirely when the account doesn't exist, making response
# time a timing oracle for user enumeration. Never used to authenticate
# anything for real -- verify_password() against it always returns False
# (the plaintext that produced it is discarded immediately below).
DUMMY_PASSWORD_HASH = _password_hasher.hash(uuid.uuid4().hex)


# ---------------------------------------------------------------------------
# Password hashing (argon2id, via argon2-cffi)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a plaintext password. Never store/log the plaintext."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-safe verification against a stored argon2 hash."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# JWT issuance / verification
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """Raised for any invalid/expired/malformed/wrong-type token."""


@dataclass(frozen=True)
class TokenPayload:
    sub: uuid.UUID
    type: TokenType
    iat: datetime
    exp: datetime
    jti: str


def _encode(
    user_id: uuid.UUID,
    token_type: TokenType,
    secret_key: str,
    expires_delta: timedelta,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret_key, algorithm=ALGORITHM)


def create_access_token(
    user_id: uuid.UUID,
    secret_key: str,
    expires_delta: timedelta = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
) -> str:
    return _encode(user_id, "access", secret_key, expires_delta)


def create_refresh_token(
    user_id: uuid.UUID,
    secret_key: str,
    expires_delta: timedelta = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
) -> str:
    return _encode(user_id, "refresh", secret_key, expires_delta)


def decode_token(token: str, secret_key: str) -> TokenPayload:
    """
    Decode and validate signature + expiry. Does NOT check token type --
    callers (get_current_user / the refresh endpoint) must check
    `payload.type` themselves for the type they expect.
    """
    try:
        raw = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid") from exc

    try:
        sub = uuid.UUID(str(raw["sub"]))
        token_type = raw["type"]
        iat = datetime.fromtimestamp(raw["iat"], tz=UTC)
        exp = datetime.fromtimestamp(raw["exp"], tz=UTC)
        jti = raw["jti"]
    except (KeyError, ValueError, TypeError) as exc:
        raise TokenError("Token payload is malformed") from exc

    if token_type not in ("access", "refresh"):
        raise TokenError("Token has an unrecognized type")

    return TokenPayload(sub=sub, type=token_type, iat=iat, exp=exp, jti=jti)
