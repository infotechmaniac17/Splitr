"""item_assignments confirm-immutability guard — M6 item 1

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-04 00:01:00.000000

PostgreSQL-only DDL (mirrors migration 0002's pattern: silently skipped on
SQLite, which cannot execute trigger functions).

Adds:
1. TRIGGER FUNCTION  reject_mutation_if_expense_confirmed()
   A generic, reusable guard parameterized via trigger arguments
   (TG_ARGV[0] = FK column name, TG_ARGV[1] = join mode 'direct' |
   'via_line_item'). Rejects INSERT/UPDATE/DELETE on a child row once its
   parent expense's parse_status = 'confirmed', EXCEPT for mutations made
   within the SAME transaction that flips the expense to confirmed (the
   confirm flow itself freezes item_assignments.share_minor and inserts
   audit rows for un-assigned allocation as part of that same atomic
   transaction). Same-transaction detection compares the expense row's
   xmin against the current transaction id.

   A second, narrower escape hatch also allows INSERT-only of new audit
   rows attached to a refund line (expense_line_items.kind='refund') on an
   already-confirmed expense, in a *different* transaction than the
   original confirm -- POST /expenses/{id}/refunds legitimately does this
   today (discovered by running the full existing suite against this
   guard). item_assignments thus behaves like the ledger itself once its
   expense is confirmed: append-only, not fully frozen. UPDATE/DELETE of
   any existing row remain blocked unconditionally once confirmed.

   TRUST BOUNDARY: the same-transaction escape hatch grants blanket
   permission to mutate item_assignments to ANY code that runs inside the
   same DB transaction as the statement that sets
   expenses.parse_status = 'confirmed' -- not just today's freeze-shares
   code specifically. This guard does not protect against bugs introduced
   inside that transaction; it only catches mutation attempts from a
   later, separate transaction against an already-committed confirmed
   expense. See the longer comment in app/domain/pg_guards.py next to the
   xmin / pg_current_xact_id() check.

   Intended to be reused, unmodified, by later child tables that need the
   identical "immutable once parent expense confirmed" rule (planned:
   expense_tax_components in M6 item 4). Only attached to item_assignments
   in this migration -- no premature attachment to tables that don't exist
   yet.

2. TRIGGER  trg_item_assignment_confirm_guard
   BEFORE INSERT OR UPDATE OR DELETE on item_assignments, using the
   function above with ('line_item_id', 'via_line_item') -- item_assignments
   has no direct expense_id column; its parent is resolved via
   expense_line_items.expense_id.

3. CREATE OR REPLACE of guard_expense_financial_immutability() (the
   expenses-table trigger FUNCTION from migration 0002; the TRIGGER object
   itself, trg_expense_immutability, is untouched -- it already points at
   this function by name and picks up the new body automatically). Folds
   in a re-audit finding: that function guarded specific *financial*
   columns of a confirmed expense but never guarded parse_status itself,
   so `UPDATE expenses SET parse_status = 'parsed' WHERE ...` on an
   already-confirmed expense used to succeed silently.

   Legal parse_status transition graph, derived from grepping every
   assignment to parse_status in app/ (not guessed):
     queued       -> parsed        (app/extraction/tasks.py, pipeline
                                     validation passes)
     queued       -> needs_review  (app/extraction/tasks.py, pipeline
                                     validation fails / provider down)
     needs_review -> parsed        (PUT .../line-items correction endpoint
                                     in app/api/expenses.py; gated there on
                                     OLD.parse_status == 'needs_review')
     parsed       -> confirmed     (POST .../confirm, via
                                     post_expense_to_ledger())

   Explicitly NOT included (because no current code path exercises them --
   flagged rather than guessed):
     - any transition INTO 'failed': the enum/CHECK constraint (migration
       0001) allow the value, but nothing in app/ ever sets it. It remains
       reachable via direct row INSERT only (row creation is not a
       transition and is not restricted by this trigger -- only UPDATEs
       that change parse_status are validated).
     - 'queued' -> 'confirmed' directly: manual (non-upload) expense
       creation starts a brand-new row already at 'parsed' (an INSERT of a
       new row, not a transition of an existing 'queued' one -- see
       POST /expenses in app/api/expenses.py). No code path transitions an
       existing queued row straight to confirmed.

   Confirmed is TERMINAL, unconditionally, with NO same-transaction escape
   hatch (unlike the item_assignments guard above) -- the confirm flow
   only ever needs to write 'confirmed' once and never needs to revert or
   re-write it, even within its own transaction, so there is no
   legitimate case to protect.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
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
    EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL,
    EXPENSE_TRIGGER_FUNCTION_DDL,
    GENERIC_CONFIRM_GUARD_FUNCTION_DDL,
    ITEM_ASSIGNMENT_CONFIRM_GUARD_TRIGGER_DDL,
)


def _is_postgres() -> bool:
    """Return True when running against a PostgreSQL backend."""
    ctx = op.get_context()
    return ctx.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite (used in unit tests) — skip Postgres-only DDL.
        return

    op.execute(sa.text(GENERIC_CONFIRM_GUARD_FUNCTION_DDL))
    op.execute(sa.text(ITEM_ASSIGNMENT_CONFIRM_GUARD_TRIGGER_DDL))

    # Upgrade guard_expense_financial_immutability() in place (CREATE OR
    # REPLACE) to add the parse_status state-machine check. The trigger
    # object itself (trg_expense_immutability, from migration 0002) does
    # not need to be touched.
    op.execute(sa.text(EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL))


def downgrade() -> None:
    if not _is_postgres():
        return

    # Restore guard_expense_financial_immutability() to its pre-0006 body
    # (financial-column guard only, no parse_status state machine).
    op.execute(sa.text(EXPENSE_TRIGGER_FUNCTION_DDL))

    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_item_assignment_confirm_guard "
            "ON item_assignments"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_mutation_if_expense_confirmed"))
