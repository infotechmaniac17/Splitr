"""
Shared FastAPI dependencies for the Splitr API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db as _get_db
from app.domain.auth import TokenError, decode_token
from app.domain.models import User
from app.extraction.tasks import enqueue_extraction as _enqueue_extraction
from app.storage import PdfStorage, get_default_storage

# Re-export so route modules only need to import from app.api.deps.
get_db = _get_db

# auto_error=False: we raise our own 401 with a consistent detail message
# (and WWW-Authenticate header) rather than FastAPI's default HTTPBearer copy.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(_get_db),
) -> User:
    """
    Resolve the JWT access token in the `Authorization: Bearer <token>`
    header to a real `User` row. This is the source of truth for "who is
    making this request" everywhere it's wired in (see app/api/expenses.py,
    app/api/groups.py, app/api/settlements.py) -- request payloads that
    still carry an explicit actor id (paid_by, created_by, payer_id, ...)
    are cross-checked against this identity, never trusted alone.
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or not credentials.credentials:
        raise unauthorized

    try:
        payload = decode_token(credentials.credentials, settings.SECRET_KEY)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if payload.type != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh tokens cannot be used to authenticate requests; "
            "exchange it at POST /auth/refresh for an access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await db.get(User, payload.sub)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User for this token no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user

# Type alias for the injectable extraction-enqueue hook (see
# app.extraction.tasks.enqueue_extraction docstring).
ExtractionEnqueuer = Callable[[UUID, bytes, str | None], Awaitable[None]]

# Module-level singleton storage backend — cheap to construct (local
# filesystem just mkdir's a directory; S3 just builds a boto3 client), so a
# fresh instance per request isn't needed. Tests override via
# app.dependency_overrides[get_storage], same pattern as get_db.
_default_storage = get_default_storage()


async def get_storage() -> PdfStorage:
    return _default_storage


async def get_extraction_enqueuer() -> ExtractionEnqueuer:
    return _enqueue_extraction
