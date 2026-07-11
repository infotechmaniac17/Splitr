"""reserve queued<->failed parse_status transitions — M6 item 1 addendum

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-04 00:02:00.000000

PostgreSQL-only DDL (mirrors migrations 0002/0006's pattern: silently
skipped on SQLite).

Follow-up re-audit finding on migration 0006's parse_status state machine:
that trigger declared NO legal transition into or out of 'failed'. Since
'failed' is reachable via direct INSERT (row creation is unrestricted --
only UPDATE transitions are validated), a row inserted as 'failed' could
never leave that state -- confirmed by inspection: OLD.parse_status =
'failed' matched none of migration 0006's ELSIF branches, so any UPDATE
attempting to move away from 'failed' was unconditionally rejected as an
undeclared transition.

docs/ARCHITECTURE.md documents 'failed' as real, intended product
behaviour (not dead code):
  - "Corrupted/unsupported PDF — manual fallback": parse_status='failed'
    opens the Quick Manual Entry flow.
  - Pipeline rationale point 4: "failed parses can be replayed against
    improved prompts/models later without asking users to re-upload" --
    i.e. a failed extraction is expected to be retried, not stuck forever.

Neither the Quick Manual Entry flow nor a retry/replay endpoint exists in
app/ yet (same "not yet wired" status as 'failed' itself today), so this
migration RESERVES 'failed' for that future work by declaring exactly the
two transitions the architecture doc's own rationale implies, rather than
leaving the trigger's legal-list silently incomplete relative to a state
the schema has allowed since migration 0001:

    queued -> failed   (pipeline: PDF corrupted/unsupported, cannot even
                         attempt validation -- distinct from needs_review,
                         which implies a partial extraction attempt)
    failed -> queued   (retry/replay against improved prompts/models)

Deliberately NOT added: failed -> parsed (the Quick Manual Entry flow's
eventual landing state) -- plausible from the architecture doc, but no
code path is even sketched for it yet; add it explicitly, with its own
justification, when that flow is actually built. Do not assume this
migration already permits it.

Applies via CREATE OR REPLACE of guard_expense_financial_immutability()
(same function migration 0006 replaced) -- no new trigger object, no new
function name. trg_expense_immutability continues to point at the same
function name and picks up this new body automatically.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
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
    EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V2,
)


def _is_postgres() -> bool:
    """Return True when running against a PostgreSQL backend."""
    ctx = op.get_context()
    return ctx.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite (used in unit tests) — skip Postgres-only DDL.
        return

    op.execute(sa.text(EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V2))


def downgrade() -> None:
    if not _is_postgres():
        return

    # Restore migration 0006's function body (no queued<->failed edges).
    op.execute(sa.text(EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL))
