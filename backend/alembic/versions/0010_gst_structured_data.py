"""GST structured data — M6 item 4

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-04 00:05:00.000000

Adds GST/tax structured data on top of the existing amount-based
kind='tax' expense_line_items mechanism (extends, does not replace it --
mirrors migration 0009's relationship between its new discount_* snapshot
columns and the pre-existing kind='discount' line items).

Schema changes
--------------
1. expenses.gst_mode        TEXT NOT NULL DEFAULT 'none'
                             CHECK IN ('none', 'invoice_exclusive',
                                       'invoice_inclusive', 'item_level')
   expenses.needs_review    BOOLEAN NOT NULL DEFAULT false
   (see app/domain/models.py:Expense's comment on these two columns for the
   full rationale, in particular why `needs_review` is a column distinct
   from parse_status='needs_review'.)

2. New table expense_tax_components (one row per named GST component per
   expense, e.g. separate CGST + SGST rows):
     id             UUID PK
     expense_id     UUID NOT NULL FK -> expenses(id)
     name           TEXT NOT NULL CHECK IN ('CGST','SGST','IGST','GST','CESS')
     rate           NUMERIC(5,2) NULL CHECK (rate IS NULL OR 0 <= rate <= 100)
     amount_minor   BIGINT NOT NULL CHECK (amount_minor >= 0)
     UNIQUE (expense_id, name)

3. expense_line_items.gst_rate           NUMERIC(5,2) NULL, same 0-100 CHECK
   expense_line_items.gst_amount_minor   BIGINT NULL CHECK (IS NULL OR >= 0)
   Only meaningful when the parent expense's gst_mode == 'item_level'.

Immutability -- three different mechanisms for three different column
groups, deliberately NOT uniform (see app/domain/pg_guards.py for the full
DDL and reasoning inline):

  a) expense_tax_components -- a brand-new child table with a DIRECT
     expense_id FK. This is exactly the case
     reject_mutation_if_expense_confirmed('expense_id', 'direct')
     (migration 0006 / M6 item 1's generic guard) was already built for --
     its own docstring named expense_tax_components as the planned direct-
     FK use case. No escape hatch beyond the existing same-transaction
     xmin check is needed: nothing appends new rows to this table after
     confirmation the way refunds append to expense_line_items.

  b) expenses.gst_mode -- lives directly ON the expenses row, so there is
     no child table / FK-join for the generic function to resolve; OLD/NEW
     already ARE the expense row. Folded into
     guard_expense_financial_immutability() (CREATE OR REPLACE, V3 -> V4,
     same trigger object trg_expense_immutability from migration 0002) --
     the exact same treatment migration 0009 gave the discount_* columns,
     for the exact same reason (see that migration's docstring).

  c) expense_line_items.gst_rate / gst_amount_minor -- these live on an
     EXISTING child table with a direct expense_id FK, which on paper looks
     like case (a). We deliberately do NOT attach
     reject_mutation_if_expense_confirmed('expense_id', 'direct') to the
     whole expense_line_items table, because expense_line_items already has
     a load-bearing exception to "immutable once confirmed":
     POST /expenses/{id}/refunds legitimately INSERTs a brand-new kind=
     'refund' expense_line_items row on an ALREADY-CONFIRMED expense, in a
     transaction separate from the original confirm (existing, tested
     behaviour -- see tests/test_api_m2.py). The generic function's only
     INSERT escape hatch for that append-after-confirm pattern is
     join_mode='via_line_item' + kind='refund', scoped to item_assignments
     -- there is no equivalent escape hatch for 'direct' mode. Attaching
     the trigger to expense_line_items as a whole would rewrite/break that
     existing refund behaviour, which is out of scope for this item and not
     asked for. Building a new, refund-aware variant of the generic
     function (or a column-scoped trigger that only inspects gst_rate/
     gst_amount_minor) would be the "heavier" alternative the item-4 spec
     explicitly allowed skipping in favour of a documented lighter
     approach. We take the lighter approach: these two columns are
     protected purely at the APPLICATION layer -- they are only ever
     written inside the same original_status-confirmed guard in
     app/extraction/tasks.py:_persist_pipeline_result that already protects
     every other line-item write (parse_status, line item replacement,
     etc.), and no other code path (in particular, not the refund flow)
     ever touches them. This is a real, deliberate gap relative to full
     DB-level enforcement -- flagged explicitly rather than silently
     assumed equivalent to (a).

Backfill (Postgres only, `_is_postgres()`-gated like every other data-shape
migration in this codebase -- SQLite test runs start with an empty
`expenses` table): every existing expense with at least one kind='tax'
line item gets exactly one expense_tax_components row:
  name='GST', rate=NULL, amount_minor = SUM(those lines' total_minor)
(tax line totals are stored POSITIVE, unlike discount/refund which are
negative -- confirmed against the validation engine's sign-convention
invariant in app/extraction/validation.py: "sign convention: discount/
refund total_minor <= 0, everything else >= 0" -- so no abs() needed here,
unlike migration 0009's discount backfill which negates a negative sum).
gst_mode is set to 'invoice_exclusive' for those expenses (a standalone
positive tax line only makes sense as GST added on top, not GST already
included in the total). Expenses with no tax line items are left with
gst_mode='none' (the column default) and no expense_tax_components row.

Idempotency: this is a single UPDATE...FROM / INSERT...SELECT pair
executed exactly once per upgrade() call -- it does not re-run itself
within one migration application, so there is no double-insert within a
single upgrade(). A normal upgrade -> downgrade -> upgrade cycle is safe
because downgrade() drops the expense_tax_components table outright
(dropping the backfilled rows along with it) before a second upgrade()
would ever re-run the backfill -- there is nothing left over for the
UNIQUE(expense_id, name) constraint to collide with. Deliberately NOT
guarded with an "already backfilled" marker/flag column, since the
DROP TABLE in downgrade() already makes that moot for the documented
upgrade/downgrade/upgrade round-trip this migration is tested against.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
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
    EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V3,
    EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V4,
    TAX_COMPONENT_CONFIRM_GUARD_TRIGGER_DDL,
)


def _is_postgres() -> bool:
    """Return True when running against a PostgreSQL backend."""
    ctx = op.get_context()
    return ctx.dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column(
            "gst_mode",
            sa.Text(),
            nullable=False,
            server_default="none",
            comment="How this invoice expresses GST -- see GstMode.",
        ),
    )
    op.add_column(
        "expenses",
        sa.Column(
            "needs_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment=(
                "Set when GST-specific arithmetic invariants "
                "(app/domain/gst.py) fail to reconcile, independent of "
                "parse_status. Confirmation is blocked while this is true."
            ),
        ),
    )

    op.create_table(
        "expense_tax_components",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "expense_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("expenses.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "name IN ('CGST', 'SGST', 'IGST', 'GST', 'CESS')",
            name="ck_tax_component_name",
        ),
        sa.CheckConstraint(
            "rate IS NULL OR (rate >= 0 AND rate <= 100)",
            name="ck_tax_component_rate_range",
        ),
        sa.CheckConstraint("amount_minor >= 0", name="ck_tax_component_amount_nonneg"),
        sa.UniqueConstraint("expense_id", "name", name="uq_tax_component_expense_name"),
    )

    op.add_column(
        "expense_line_items",
        sa.Column("gst_rate", sa.Numeric(precision=5, scale=2), nullable=True),
    )
    op.add_column(
        "expense_line_items",
        sa.Column("gst_amount_minor", sa.BigInteger(), nullable=True),
    )

    if not _is_postgres():
        # SQLite: columns/table only, no extra CHECKs beyond what
        # create_table already declared, no backfill (empty table in test
        # DBs), no triggers. See module docstring.
        return

    op.execute(
        sa.text(
            "ALTER TABLE expenses ADD CONSTRAINT ck_expense_gst_mode "
            "CHECK (gst_mode IN ('none', 'invoice_exclusive', "
            "'invoice_inclusive', 'item_level'))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE expense_line_items "
            "ADD CONSTRAINT ck_line_item_gst_rate_range "
            "CHECK (gst_rate IS NULL OR (gst_rate >= 0 AND gst_rate <= 100))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE expense_line_items "
            "ADD CONSTRAINT ck_line_item_gst_amount_nonneg "
            "CHECK (gst_amount_minor IS NULL OR gst_amount_minor >= 0)"
        )
    )

    # ------------------------------------------------------------------
    # Backfill: expenses with existing kind='tax' line items.
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO expense_tax_components (id, expense_id, name, rate, amount_minor)
            SELECT gen_random_uuid(), t.expense_id, 'GST', NULL, t.total_tax
            FROM (
                SELECT expense_id, SUM(total_minor) AS total_tax
                FROM expense_line_items
                WHERE kind = 'tax'
                GROUP BY expense_id
            ) t
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE expenses e
            SET gst_mode = 'invoice_exclusive'
            FROM (
                SELECT DISTINCT expense_id
                FROM expense_line_items
                WHERE kind = 'tax'
            ) t
            WHERE e.id = t.expense_id
            """
        )
    )

    # ------------------------------------------------------------------
    # Attach the direct-FK confirm guard to the new child table, and widen
    # guard_expense_financial_immutability() to also guard gst_mode.
    # ------------------------------------------------------------------
    op.execute(sa.text(TAX_COMPONENT_CONFIRM_GUARD_TRIGGER_DDL))
    op.execute(sa.text(EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V4))


def downgrade() -> None:
    if _is_postgres():
        # Restore the pre-0010 function body (no gst_mode guarded) before
        # dropping the column it references.
        op.execute(sa.text(EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V3))
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_tax_component_confirm_guard "
                "ON expense_tax_components"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE expense_line_items DROP CONSTRAINT IF EXISTS "
                "ck_line_item_gst_amount_nonneg"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE expense_line_items DROP CONSTRAINT IF EXISTS "
                "ck_line_item_gst_rate_range"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE expenses DROP CONSTRAINT IF EXISTS ck_expense_gst_mode"
            )
        )

    op.drop_column("expense_line_items", "gst_amount_minor")
    op.drop_column("expense_line_items", "gst_rate")
    op.drop_table("expense_tax_components")
    op.drop_column("expenses", "needs_review")
    op.drop_column("expenses", "gst_mode")
