"""
Auth endpoints: register / login / refresh / me.

Replaces the client-side-only "who are you" picker that web/src/lib/
current-user.tsx documents as a stopgap (POST/GET /users + localStorage).
POST /users still exists (unauthenticated, no password) for
backward-compatibility with anything that only needs to create a bare user
record (e.g. adding a placeholder group member who never logs in
themselves); real acting users should register here instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.api.schemas import (
    AccessTokenResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.config import settings
from app.domain.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    DUMMY_PASSWORD_HASH,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.domain.models import User

router = APIRouter(prefix="/auth", tags=["auth"])

_ACCESS_TOKEN_EXPIRE_SECONDS = ACCESS_TOKEN_EXPIRE_MINUTES * 60


def _issue_token_pair(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, settings.SECRET_KEY),
        refresh_token=create_refresh_token(user.id, settings.SECRET_KEY),
        expires_in=_ACCESS_TOKEN_EXPIRE_SECONDS,
        user=UserResponse.model_validate(user),
    )


@router.post(
    "/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED
)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Create a user with a password and immediately log them in (returns a
    fresh access/refresh token pair, same shape as POST /auth/login).
    """
    user = User(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        phone=payload.phone,
        avatar_url=payload.avatar_url,
        default_currency=payload.default_currency,
    )
    db.add(user)
    try:
        await db.commit()
        await db.refresh(user)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email '{payload.email}' is already registered",
        ) from exc
    return _issue_token_pair(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    # Deliberately identical error for "no such user" and "wrong password"
    # (and for "user has no password set", e.g. a pre-auth POST /users
    # fixture) -- don't leak which case it was.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password",
    )
    # Timing side-channel fix: always run the (expensive) argon2 verify,
    # even when there's no real user/hash to check against, so a caller
    # can't distinguish "no such account" from "wrong password" by response
    # time (user-enumeration oracle). verify_password's own result is
    # ignored in that case -- `password_hash_to_check` is never a real
    # account's hash unless `user` is a genuine, password-having account.
    password_hash_to_check = (
        str(user.password_hash) if user is not None and user.password_hash else DUMMY_PASSWORD_HASH
    )
    password_ok = verify_password(payload.password, password_hash_to_check)
    if user is None or not user.password_hash or not password_ok:
        raise invalid
    return _issue_token_pair(user)


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> AccessTokenResponse:
    """
    Exchange a refresh token for a new access token. The refresh token
    itself is NOT rotated/re-issued (out of scope for this pass -- see
    app/domain/auth.py docstring on statelessness); clients keep reusing
    the same refresh token until it expires (30 days) or login again.
    """
    try:
        decoded = decode_token(payload.refresh_token, settings.SECRET_KEY)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    if decoded.type != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not a refresh token",
        )
    user = await db.get(User, decoded.sub)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User for this token no longer exists",
        )
    return AccessTokenResponse(
        access_token=create_access_token(user.id, settings.SECRET_KEY),
        expires_in=_ACCESS_TOKEN_EXPIRE_SECONDS,
    )


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
