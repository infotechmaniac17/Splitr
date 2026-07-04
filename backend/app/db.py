"""
SQLAlchemy 2.x engine and session setup.

Async engine + session factory for the FastAPI app.
The DeclarativeBase (Base) is imported by all model modules so that
Alembic's env.py can discover their metadata for autogenerate.

Usage in route handlers:
    from app.db import get_db
    from sqlalchemy.ext.asyncio import AsyncSession

    @router.get("/items")
    async def list_items(db: AsyncSession = Depends(get_db)):
        result = await db.execute(select(Item))
        ...
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,  # recover from dropped DB connections
    pool_size=10,
    max_overflow=20,
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Declarative base — all models inherit from this
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    """
    Shared declarative base.  Import and subclass this in every model file
    so that Base.metadata picks them up for Alembic autogenerate.

    Example:
        from app.db import Base

        class User(Base):
            __tablename__ = "users"
            id: Mapped[uuid.UUID] = mapped_column(primary_key=True, ...)
    """


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession; automatically closed on request teardown."""
    async with AsyncSessionLocal() as session:
        yield session
