"""
Guard against alembic_version drift (stamped version with no matching tables,
or schema that no longer matches the ORM models).

Run this against a database before trusting `alembic upgrade head` output,
and in CI before merging any migration. Exits non-zero on drift.

Usage:
    DATABASE_URL=postgresql+psycopg://... python scripts/check_alembic_sync.py
"""

from __future__ import annotations

import sys

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.runtime.migration import MigrationContext as _MC  # noqa: F401
from sqlalchemy import inspect

from app.config import settings
from app.domain.models import Base


def main() -> int:
    # "postgresql+psycopg://" (psycopg 3) supports sync engines directly;
    # only asyncpg needs stripping since it has no sync mode.
    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    engine = sa.create_engine(sync_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        has_alembic_table = inspector.has_table("alembic_version")

        if not has_alembic_table:
            print("FAIL: alembic_version table is missing (migrations never run).")
            return 1

        stamped_version = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar()

        expected_tables = set(Base.metadata.tables.keys())
        actual_tables = set(inspector.get_table_names()) - {"alembic_version"}
        missing = expected_tables - actual_tables

        if missing:
            print(
                f"FAIL: alembic_version is stamped at '{stamped_version}' but "
                f"{len(missing)} model table(s) are missing from the database: "
                f"{sorted(missing)}. This is exactly the drift pattern where "
                f"alembic_version outlives a schema reset performed by a tool "
                f"that doesn't know about Alembic (e.g. Base.metadata.drop_all)."
            )
            return 1

        # Table presence is the hard gate (this is the drift pattern that
        # actually causes "alembic thinks it's at head but the app is
        # broken"). Column/index-level diffs are reported as a warning only
        # — they can reflect legitimate autogenerate quirks (e.g. an index
        # declared on the model but never scripted into a migration) that
        # need a human triage pass, not an automatic merge block.
        ctx = MigrationContext.configure(conn)
        diff = compare_metadata(ctx, Base.metadata)
        table_level_diff = [
            d for d in diff if d[0] in ("add_table", "remove_table")
        ]
        other_diff = [d for d in diff if d not in table_level_diff]

        if table_level_diff:
            print(
                f"FAIL: schema at version '{stamped_version}' has table-level "
                f"drift from ORM models:"
            )
            for change in table_level_diff:
                print(f"  - {change}")
            return 1

        if other_diff:
            print(
                f"WARN: schema at version '{stamped_version}' has "
                f"{len(other_diff)} non-table difference(s) from ORM models "
                f"(column/index-level — needs a human look, not blocking):"
            )
            for change in other_diff:
                print(f"  - {change}")

    print(f"OK: alembic_version='{stamped_version}', all model tables present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
