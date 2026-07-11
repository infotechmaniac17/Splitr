"""expense discount snapshot columns + guard extension — M6 item 3

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-04 00:04:00.000000

Adds SNAPSHOT discount metadata columns directly to `expenses` (extending,
not replacing, the pre-existing extracted-discount mechanism of
kind='discount' expense_line_items rows -- those still exist unchanged for
per-line discount detail; these new columns are a single expense-level
summary snapshot, e.g. for a matched vendor rule or a manually-entered
whole-invoice discount that never had per-line detail extracted).

Columns added to `expenses`
----------------------------
  discount_type             TEXT NULL, CHECK IN ('flat', 'percent')
  discount_value_minor      BIGINT NULL
  discount_percent          NUMERIC(5,2) NULL
  discount_threshold_minor  BIGINT NULL
  discount_source           TEXT NULL, CHECK IN
                             ('manual', 'vendor_rule', 'extracted')
  discount_rule_id          FK vendor_discount_rules(id) ON DELETE SET NULL

INVARIANT (see app/domain/models.py:Expense's column `comment=` kwargs,
which are also applied as real Postgres COMMENT ON COLUMN below): these are
SNAPSHOTS frozen at the moment a discount is applied. They are NEVER
re-derived from vendor_discount_rules after the fact -- if the rule that
produced discount_rule_id is later edited, deactivated, or deleted-from-the-
app's-perspective (soft delete only, see migration 0008's docstring), this
expense's discount_* values do not change. This mirrors item_assignments.
share_minor being frozen at confirmation (ARCHITECTURE.md's "corrections
are new signed entries" convention) -- historical financial facts are never
retroactively rewritten by unrelated later edits.

Backfill (Postgres only, guarded by `_is_postgres()` like every other
data-shape migration in this codebase — SQLite test runs start with an
empty `expenses` table, so there's nothing to backfill there): every
existing expense that has at least one kind='discount' line item gets:
  discount_source        = 'extracted'
  discount_type           = 'flat'
  discount_value_minor    = abs(SUM(those lines' total_minor))
(line totals for discount/refund lines are stored negative per
ARCHITECTURE.md §3 and the Amazon few-shot example in
app/extraction/vendor_detect.py -- `unit_price_minor: -20000` -- so we take
the absolute value of the negative sum to get a positive flat-discount
amount.) Expenses with no discount line items are left with all discount_*
columns NULL (no discount known).

Guard extension
----------------
Extends guard_expense_financial_immutability() (CREATE OR REPLACE, same
function/trigger object as migrations 0002/0006/0007 -- see
app/domain/pg_guards.py's EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V3 for
the full rationale on why this function, not
reject_mutation_if_expense_confirmed(), is the right place to add this
check) so that once an expense is confirmed, none of these new discount_*
columns (including discount_rule_id) may be changed either -- same
treatment as total_minor/subtotal_minor/paid_by/currency/group_id.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
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
    EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V2,
    EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V3,
)


def _is_postgres() -> bool:
    """Return True when running against a PostgreSQL backend."""
    ctx = op.get_context()
    return ctx.dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column(
            "discount_type",
            sa.Text(),
            nullable=True,
            comment=(
                "Snapshot of the applied discount's type at the time it was "
                "applied. Never re-derived from vendor_discount_rules later."
            ),
        ),
    )
    op.add_column(
        "expenses",
        sa.Column(
            "discount_value_minor",
            sa.BigInteger(),
            nullable=True,
            comment="Snapshot: flat discount amount in minor units, if discount_type='flat'.",
        ),
    )
    op.add_column(
        "expenses",
        sa.Column(
            "discount_percent",
            sa.Numeric(precision=5, scale=2),
            nullable=True,
            comment="Snapshot: percent discount (0-100], if discount_type='percent'.",
        ),
    )
    op.add_column(
        "expenses",
        sa.Column(
            "discount_threshold_minor",
            sa.BigInteger(),
            nullable=True,
            comment=(
                "Snapshot of the vendor rule's min_order_total_minor at "
                "application time, for audit/display only -- not re-checked."
            ),
        ),
    )
    op.add_column(
        "expenses",
        sa.Column(
            "discount_source",
            sa.Text(),
            nullable=True,
            comment=(
                "Where this discount snapshot came from: 'manual' "
                "(user-entered, never auto-overwritten), 'vendor_rule' "
                "(matched by app.domain.vendor_discount.match_rule), or "
                "'extracted' (summed from kind='discount' line items by the "
                "PDF pipeline)."
            ),
        ),
    )
    op.add_column(
        "expenses",
        sa.Column(
            "discount_rule_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("vendor_discount_rules.id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "The vendor_discount_rules row that produced this snapshot, "
                "if discount_source='vendor_rule'. Informational lineage "
                "only -- the discount_* values above are never re-read from "
                "this rule."
            ),
        ),
    )

    if not _is_postgres():
        # SQLite: columns only, no CHECK constraints, no backfill (test DB
        # starts empty), no trigger. See module docstring.
        return

    op.execute(
        sa.text(
            "ALTER TABLE expenses "
            "ADD CONSTRAINT ck_expense_discount_type "
            "CHECK (discount_type IS NULL OR discount_type IN ('flat', 'percent'))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE expenses "
            "ADD CONSTRAINT ck_expense_discount_source "
            "CHECK (discount_source IS NULL OR discount_source IN "
            "('manual', 'vendor_rule', 'extracted'))"
        )
    )

    # ------------------------------------------------------------------
    # Backfill: expenses with existing kind='discount' line items.
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            UPDATE expenses e
            SET discount_source = 'extracted',
                discount_type = 'flat',
                discount_value_minor = d.abs_total
            FROM (
                SELECT expense_id, ABS(SUM(total_minor)) AS abs_total
                FROM expense_line_items
                WHERE kind = 'discount'
                GROUP BY expense_id
            ) d
            WHERE e.id = d.expense_id
            """
        )
    )

    # ------------------------------------------------------------------
    # Extend guard_expense_financial_immutability() to also guard the new
    # discount_* columns once an expense is confirmed.
    # ------------------------------------------------------------------
    op.execute(sa.text(EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V3))


def downgrade() -> None:
    if _is_postgres():
        # Restore the pre-0009 function body (no discount_* columns guarded)
        # before dropping the columns those checks reference.
        op.execute(sa.text(EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V2))
        op.execute(
            sa.text(
                "ALTER TABLE expenses DROP CONSTRAINT IF EXISTS "
                "ck_expense_discount_source"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE expenses DROP CONSTRAINT IF EXISTS "
                "ck_expense_discount_type"
            )
        )

    op.drop_column("expenses", "discount_rule_id")
    op.drop_column("expenses", "discount_source")
    op.drop_column("expenses", "discount_threshold_minor")
    op.drop_column("expenses", "discount_percent")
    op.drop_column("expenses", "discount_value_minor")
    op.drop_column("expenses", "discount_type")
