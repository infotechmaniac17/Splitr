"""expenses(group_id, invoice_date) composite index — M6-M8 item 7a

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-11 00:00:00.000000

GET /groups/{group_id}/expenses (M6-M8 item 7a) filters on
`group_id = :group_id AND invoice_date BETWEEN :from AND :to` and groups by
invoice_date. Migration 0001 only ever created a single-column index on
group_id (`ix_expenses_group_id`) plus one on paid_by -- there was no
composite (group_id, invoice_date) index for this query to use, so it would
have had to scan every expense row for the group and sort in memory. This
migration adds it.

Column order (group_id, invoice_date) matches the query shape: group_id is
always an equality filter (every caller of this endpoint supplies exactly
one group_id), invoice_date is the inequality-range/sort column -- the
standard "equality columns first, range column last" composite index
ordering.

The old single-column `ix_expenses_group_id` index is left in place
(unmodified) -- other existing queries (e.g. `_assert_active_group_members`
callers, confirm_expense's membership check) filter on group_id alone
without touching invoice_date, and Postgres can still use the leading
column of this new composite index for those, but there is no reason to
force a migration of every other call site's plan just to drop a
single-column index that costs little and isn't in this task's scope.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_expenses_group_id_invoice_date",
        "expenses",
        ["group_id", "invoice_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_expenses_group_id_invoice_date", table_name="expenses")
