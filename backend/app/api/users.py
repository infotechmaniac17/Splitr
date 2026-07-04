"""
User endpoints.

POST /users is an intentionally public/unauthenticated passthrough (kept
for backward compatibility, e.g. creating a placeholder group member who
never logs in themselves). Real acting users should use POST /auth/register
instead (app/api/auth.py), which also sets a password and returns a
token pair. Users created here have password_hash=NULL and cannot log in
via POST /auth/login until a password is set for them.

All other routes in this module require authentication via get_current_user.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.api.schemas import UserCreate, UserPublicResponse, UserResponse
from app.domain.models import GroupMember, User

router = APIRouter(prefix="/users", tags=["users"])


async def _shares_active_group(
    db: AsyncSession,
    user_a: uuid.UUID,
    user_b: uuid.UUID,
) -> bool:
    """True if user_a and user_b are both currently-active members of at
    least one common group."""
    a_groups = select(GroupMember.group_id).where(
        GroupMember.user_id == user_a, GroupMember.left_at.is_(None)
    )
    result = await db.execute(
        select(GroupMember.user_id)
        .where(GroupMember.user_id == user_b)
        .where(GroupMember.left_at.is_(None))
        .where(GroupMember.group_id.in_(a_groups))
    )
    return result.scalar_one_or_none() is not None


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
) -> User:
    user = User(
        name=payload.name,
        email=payload.email,
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
    return user


@router.get("/{user_id}", response_model=UserResponse | UserPublicResponse)
async def get_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> User | UserPublicResponse:
    """
    Fetch a user's profile.

    Fix for the PII leak finding: this endpoint previously had zero auth --
    any caller who could guess/obtain a user UUID got that user's full
    profile (name, email, phone). Now:
      - the caller fetching themself gets the full profile.
      - a caller who shares an active group with the target gets a slim
        name/avatar-only projection (enough to render a group member list;
        see web/mobile getUser() call sites -- they only ever need a
        display name, never a stranger's email/phone).
      - anyone else (no shared group) gets 404, never 403: this endpoint
        must never let a caller distinguish "UUID does not exist" from
        "UUID exists but you're not authorized to see it" -- otherwise a
        stranger could enumerate valid user UUIDs by watching for 403 vs
        404. Existence is only ever confirmed to the caller themself or to
        someone who already shares an active group with the target.
    """
    if user_id == current_user.id:
        user = await db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return user

    # Authorization check MUST happen before (or fused with) existence so
    # that "doesn't exist" and "exists but not authorized" are
    # indistinguishable to a non-self caller -- both yield plain 404.
    if not await _shares_active_group(db, current_user.id, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return UserPublicResponse.model_validate(user)
