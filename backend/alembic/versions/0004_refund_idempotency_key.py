"""add expense_line_items.idempotency_key for safe refund retries

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-03 00:03:00.000000

Reviewer HIGH finding (M2): POST /expenses/{id}/refunds had no idempotency
protection — a network retry or double-click would append two refund lines
and double-post refund_reversal ledger entries.

Clients may now send an idempotency_key with a refund; it is stored on the
refund line and guarded by a unique constraint per expense, so a retried
request is detected and returns the existing state instead of re-posting.

NULL keys are exempt (both SQLite and Postgres allow multiple NULLs in a
unique constraint), so non-refund lines and keyless refunds are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "expense_line_items",
        sa.Column("idempotency_key", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_line_item_idempotency_key",
        "expense_line_items",
        ["expense_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_line_item_idempotency_key", "expense_line_items", type_="unique"
    )
    op.drop_column("expense_line_items", "idempotency_key")
