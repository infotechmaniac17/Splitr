"""hardening — M1 finance invariants

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-03 00:01:00.000000

PostgreSQL-only DDL additions (triggers + CHECK constraints).
SQLite is used only for testing; these changes are silently skipped on
non-Postgres dialects because SQLite does not support trigger functions and
does not enforce most CHECK constraints meaningfully.

Changes
-------
1. CHECK CONSTRAINT  settlements.amount_minor > 0          (M2/M3)
2. CHECK CONSTRAINT  expenses.total_minor > 0              (M2/M3)
3. TRIGGER FUNCTION  guard_ledger_append_only()            (H3)
   TRIGGER           trg_ledger_append_only                (H3)
   — BEFORE UPDATE OR DELETE on ledger_entries: always raises.
4. TRIGGER FUNCTION  guard_expense_financial_immutability() (H3)
   TRIGGER           trg_expense_immutability               (H3)
   — BEFORE UPDATE OR DELETE on expenses:
       * DELETE  → raises when OLD.parse_status = 'confirmed'
       * UPDATE  → raises when OLD.parse_status = 'confirmed' AND any of
                   (total_minor, subtotal_minor, paid_by, currency, group_id)
                   differs from OLD values.
         Benign updates (status='voided', parse_status transitions, etc.) pass.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# DDL strings live in app/domain/pg_guards so tests can reuse them directly.
# The backend/ directory is always on sys.path when alembic runs from there.
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

_backend_dir = str(Path(__file__).resolve().parent.parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.domain.pg_guards import (  # noqa: E402
    ALL_TRIGGER_DDL,
)


def _is_postgres() -> bool:
    """Return True when running against a PostgreSQL backend."""
    ctx = op.get_context()
    return ctx.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite (used in unit tests) — skip Postgres-only DDL.
        return

    # ------------------------------------------------------------------
    # M2/M3: CHECK constraints
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            "ALTER TABLE settlements "
            "ADD CONSTRAINT ck_settlement_amount_positive "
            "CHECK (amount_minor > 0)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE expenses "
            "ADD CONSTRAINT ck_expense_total_positive "
            "CHECK (total_minor > 0)"
        )
    )

    # ------------------------------------------------------------------
    # H3: Append-only trigger on ledger_entries + financial-immutability
    #     trigger on expenses
    # ------------------------------------------------------------------
    for ddl in ALL_TRIGGER_DDL:
        op.execute(sa.text(ddl))


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_expense_immutability ON expenses"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS guard_expense_financial_immutability"))
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_ledger_append_only ON ledger_entries")
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS guard_ledger_append_only"))
    op.execute(
        sa.text(
            "ALTER TABLE expenses DROP CONSTRAINT IF EXISTS ck_expense_total_positive"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE settlements "
            "DROP CONSTRAINT IF EXISTS ck_settlement_amount_positive"
        )
    )
