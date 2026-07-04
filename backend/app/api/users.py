"""
User endpoints.

Legacy passthrough (M1 — no auth, user IDs accepted directly): kept for
backward compatibility (e.g. creating a placeholder group member who never
logs in themselves). Real acting users should use POST /auth/register
instead (app/api/auth.py), which also sets a password and returns a
token pair. Users created here have password_hash=NULL and cannot log in
via POST /auth/login until a password is set for them.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.schemas import UserCreate, UserResponse
from app.domain.models import User

router = APIRouter(prefix="/users", tags=["users"])


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


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user
