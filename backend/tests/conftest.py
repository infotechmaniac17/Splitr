"""
Pytest configuration and shared fixtures for the Splitr backend test suite.

Default backend: in-memory SQLite (via aiosqlite) — no live services needed.
Postgres backend: set the TEST_DATABASE_URL environment variable to a
  postgresql+psycopg://... URL.  The entire suite then runs against Postgres;
  tests decorated with @pytest.mark.postgres are also executed (they are
  skipped on SQLite because they require Postgres-specific features such as
  SELECT ... FOR UPDATE, triggers, and enforced CHECK constraints).

SQLite notes:
  - CHECK constraints are parsed but NOT enforced at the DB level.
    Application-level guards (sum assertion, amount > 0 checks, ORM
    append-only session event listener) cover these invariants in tests.
  - UUID columns are stored as CHAR(32) strings — SQLAlchemy handles the
    conversion transparently.
  - JSONB falls back to sa.JSON (TEXT in SQLite) transparently.
  - FOR UPDATE is silently ignored.

Each test function gets its own isolated database (StaticPool for SQLite,
drop-all + create-all for Postgres) to ensure full isolation.
"""

from __future__ import annotations

import os
import sys

# psycopg3 requires SelectorEventLoop on Windows — must be set before
# any async code (same as app/main.py).
if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# Import models so Base.metadata is populated before create_all.
import app.domain.models  # noqa: F401
from app.db import Base, get_db
from app.main import app
from tests.auth_test_utils import attach_auto_auth

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

SQLITE_URL = "sqlite+aiosqlite:///:memory:"

_TEST_DATABASE_URL: str = os.environ.get("TEST_DATABASE_URL", "")


def _using_postgres() -> bool:
    return "postgresql" in _TEST_DATABASE_URL


# ---------------------------------------------------------------------------
# Postgres-only mark: skip on SQLite, run on Postgres
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres: mark test as requiring a real PostgreSQL backend "
        "(skipped when TEST_DATABASE_URL is not a Postgres URL).",
    )


@pytest.fixture(autouse=True)
def _skip_if_not_postgres(request: pytest.FixtureRequest) -> None:
    """Skip tests marked @pytest.mark.postgres when not running on Postgres."""
    if request.node.get_closest_marker("postgres") and not _using_postgres():
        pytest.skip(
            "requires Postgres — set TEST_DATABASE_URL to a postgresql+psycopg:// URL"
        )


# ---------------------------------------------------------------------------
# Postgres trigger DDL (shared module from alembic/ package)
# ---------------------------------------------------------------------------


def _postgres_trigger_ddl() -> list[str]:
    """Return the trigger DDL statements from app/domain/pg_guards.py."""
    from app.domain.pg_guards import ALL_TRIGGER_DDL  # noqa: PLC0415

    return list(ALL_TRIGGER_DDL)


# ---------------------------------------------------------------------------
# Per-test engine + session
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    """
    Function-scoped async engine.

    SQLite: in-memory StaticPool (fast, isolated per test).
    Postgres: drops and recreates all tables + installs trigger functions so
              each test starts with a clean schema.
    """
    if _using_postgres():
        eng = create_async_engine(_TEST_DATABASE_URL, echo=False)
        async with eng.begin() as conn:
            # Full schema reset, not just Base.metadata.drop_all: this fixture
            # builds schema directly from ORM models, bypassing Alembic. If
            # `alembic upgrade head` is ever run against this same database
            # (e.g. per CLAUDE.md's pre-merge migration check), it stamps
            # alembic_version — a table outside Base.metadata that drop_all
            # cannot see or remove, leaving it orphaned pointing at a version
            # whose tables this fixture just deleted. Dropping the whole
            # schema guarantees no cross-tool bookkeeping table can survive.
            await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            await conn.execute(sa.text("CREATE SCHEMA public"))
            await conn.run_sync(Base.metadata.create_all)
            # Install Postgres triggers (H3) for every test so postgres-marked
            # trigger tests see the real DB-level guards.
            for ddl in _postgres_trigger_ddl():
                await conn.execute(sa.text(ddl))
        yield eng
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await eng.dispose()
    else:
        eng = create_async_engine(
            SQLITE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield eng
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await eng.dispose()


@pytest.fixture
async def db_session(engine):
    """
    Async SQLAlchemy session bound to the test engine.

    Does NOT auto-commit — tests call session.commit() explicitly or rely
    on the session being flushed.  Rolls back any un-committed state on
    teardown.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
        await session.rollback()


# ---------------------------------------------------------------------------
# FastAPI test client with DB override
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(engine):
    """
    AsyncClient wired to the FastAPI app with get_db overridden to use the
    test engine.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def _override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # See tests/auth_test_utils.py: auto-attaches a Bearer token for the
        # existing M1-M4 test suites, which predate real auth and identify
        # the acting user purely via request-body fields (paid_by, ...).
        attach_auto_auth(ac)
        yield ac
    app.dependency_overrides.clear()
