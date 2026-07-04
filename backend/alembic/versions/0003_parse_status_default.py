"""fix expenses.parse_status server default: parsed -> queued

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-03 00:02:00.000000

Migration 0001 created expenses.parse_status with server_default='parsed',
which contradicts the ORM default (ParseStatus.queued) and the M3 design:
any insert that bypasses the ORM (raw SQL, Celery workers, admin scripts)
would silently receive 'parsed' and skip the validation engine.

The safe state must be the default at every layer, so the column default
becomes 'queued'.  Existing rows are untouched (defaults only apply to new
inserts that omit the column).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("expenses", "parse_status", server_default="queued")


def downgrade() -> None:
    op.alter_column("expenses", "parse_status", server_default="parsed")
