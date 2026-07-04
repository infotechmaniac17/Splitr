"""
Alembic environment — wired to app.config.settings.DATABASE_URL so the
migration runner always uses the same credentials as the application.

Running migrations (from backend/ directory):
    alembic upgrade head
    alembic downgrade -1
    alembic revision --autogenerate -m "add users table"

The env.py uses an *async* engine (psycopg v3) via run_sync so it is
consistent with the app's async SQLAlchemy setup.  The sync alembic API is
still used for the migration context; run_sync bridges the two.

IMPORTANT: import every model module in the "Import models" block below so
that Base.metadata includes their tables for autogenerate.
"""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

# psycopg3 requires SelectorEventLoop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ---------------------------------------------------------------------------
# Make sure `app` package is importable when running from backend/
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Import models here so Base.metadata discovers their tables.
# ---------------------------------------------------------------------------
import app.domain.models  # noqa: F401  — registers all M1 tables with Base.metadata
from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Alembic Config object — gives access to values in alembic.ini
# ---------------------------------------------------------------------------
config = context.config

# Override the URL from alembic.ini with the one from app config.
# This is the single source of truth for DB credentials.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode: generate SQL without a live DB connection
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode: connect and run migrations
# ---------------------------------------------------------------------------
def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
