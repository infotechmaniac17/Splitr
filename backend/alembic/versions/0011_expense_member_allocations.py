"""expense_member_allocations — M6 item 5

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-05 00:00:00.000000

Adds `expense_member_allocations`, a per-(expense, member) audit/read-model
table recording the FINAL discount + GST breakdown produced by
app.domain.splitting.compute_allocation at confirmation time (see that
module's docstring for the allocation algorithm and
app/api/expenses.py:confirm_expense for exactly when/how rows are inserted).

Schema
------
    id             UUID PK
    expense_id     UUID NOT NULL FK -> expenses(id)
    user_id        UUID NOT NULL FK -> users(id)
    base_minor     BIGINT NOT NULL
    discount_minor BIGINT NOT NULL CHECK (discount_minor <= 0)
    gst_minor      BIGINT NOT NULL CHECK (gst_minor >= 0)
    total_minor    BIGINT NOT NULL
                   CHECK (total_minor = base_minor + discount_minor + gst_minor)
    UNIQUE (expense_id, user_id)

Why this is NOT the source of truth for money
-----------------------------------------------
The ledger_entries rows posted by post_expense_to_ledger() in the SAME
confirm transaction (fed from these same breakdowns' total_minor values --
see confirm_expense) are the actual source of truth, per CLAUDE.md's
append-only-ledger invariant. This table is a read-model/audit summary: it
lets GET /expenses/{id}/allocation-preview return a confirmed expense's
breakdown without re-running compute_allocation (which would be wrong to do
post-confirmation -- the frozen numbers must never silently change if
splitting rules/rounding are later touched, exactly like
item_assignments.share_minor).

Immutability -- reuses reject_mutation_if_expense_confirmed() unmodified
--------------------------------------------------------------------------
This is a plain child table with a DIRECT expense_id FK -- exactly the case
reject_mutation_if_expense_confirmed('expense_id', 'direct') was already
built for (see migration 0006's docstring, and migration 0010's
expense_tax_components for the precedent this follows). No new guard
function/version is needed:
  - Rows are written ONLY inside POST /expenses/{id}/confirm's transaction,
    AFTER post_expense_to_ledger() has already flipped parse_status to
    'confirmed' within that SAME transaction -- covered by the guard's
    existing same-transaction (xmin) escape hatch, the identical mechanism
    that already lets that same function freeze item_assignments.share_minor
    in the same transaction (migration 0006).
  - Nothing appends new rows to this table for an already-confirmed expense
    in a LATER, separate transaction (there is no "reallocate after
    confirm" flow analogous to POST /expenses/{id}/refunds), so -- like
    expense_tax_components -- no refund-style INSERT escape hatch is needed
    either.
See app/domain/pg_guards.py's MEMBER_ALLOCATION_CONFIRM_GUARD_TRIGGER_DDL
for the exact DDL, modeled 1:1 on TAX_COMPONENT_CONFIRM_GUARD_TRIGGER_DDL.

No backfill for historical confirmed expenses
-----------------------------------------------
Every expense confirmed before this migration has no expense_member_
allocations rows and never will (there's no compute_allocation "replay"
across historical ledger postings, and item_assignments.share_minor already
IS the frozen truth for those old expenses). A byte-identical
"base_minor=share_minor, discount_minor=0, gst_minor=0,
total_minor=share_minor" row can trivially be SYNTHESIZED AT READ TIME for
such an expense -- and GET /expenses/{id}/allocation-preview does exactly
that minimal synthesis when it finds no persisted rows for an already-
confirmed expense, rather than this migration attempting a real backfill
(there is nothing to backfill FROM: pre-item-5 confirmed expenses never had
a discount/GST breakdown to reconstruct beyond the share itself).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
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
    MEMBER_ALLOCATION_CONFIRM_GUARD_TRIGGER_DDL,
)


def _is_postgres() -> bool:
    """Return True when running against a PostgreSQL backend."""
    ctx = op.get_context()
    return ctx.dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "expense_member_allocations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "expense_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("expenses.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("base_minor", sa.BigInteger(), nullable=False),
        sa.Column("discount_minor", sa.BigInteger(), nullable=False),
        sa.Column("gst_minor", sa.BigInteger(), nullable=False),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "discount_minor <= 0", name="ck_member_alloc_discount_nonpos"
        ),
        sa.CheckConstraint("gst_minor >= 0", name="ck_member_alloc_gst_nonneg"),
        sa.CheckConstraint(
            "total_minor = base_minor + discount_minor + gst_minor",
            name="ck_member_alloc_total_reconciles",
        ),
        sa.UniqueConstraint(
            "expense_id", "user_id", name="uq_member_allocation_expense_user"
        ),
    )

    if not _is_postgres():
        # SQLite: table + CHECK constraints as declared above only (SQLite
        # parses CHECK constraints but does not enforce them -- see
        # CLAUDE.md's CI tier split); no trigger, no backfill (empty table
        # in test DBs). See module docstring.
        return

    op.execute(sa.text(MEMBER_ALLOCATION_CONFIRM_GUARD_TRIGGER_DDL))


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_member_allocation_confirm_guard "
                "ON expense_member_allocations"
            )
        )

    op.drop_table("expense_member_allocations")
