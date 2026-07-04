"""initial schema — M1

Revision ID: 0001
Revises:
Create Date: 2026-07-03 00:00:00.000000

Targets PostgreSQL 15+.
Enum columns use VARCHAR(50) + CHECK constraints (native_enum=False) for
simplicity and easy Alembic diff management.
JSONB used for raw_extraction.
All money columns are BIGINT (minor units / paise).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("phone", sa.Text, nullable=True),
        sa.Column("avatar_url", sa.Text, nullable=True),
        sa.Column(
            "default_currency", sa.String(3), nullable=False, server_default="INR"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # ------------------------------------------------------------------
    # groups
    # ------------------------------------------------------------------
    op.create_table(
        "groups",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("simplify_debts", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------
    # group_members
    # ------------------------------------------------------------------
    op.create_table(
        "group_members",
        sa.Column(
            "group_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("groups.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "role",
            sa.String(50),
            nullable=False,
            server_default="member",
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_group_member_role"),
    )

    # ------------------------------------------------------------------
    # subgroups
    # ------------------------------------------------------------------
    op.create_table(
        "subgroups",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("groups.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
    )

    # ------------------------------------------------------------------
    # subgroup_members
    # ------------------------------------------------------------------
    op.create_table(
        "subgroup_members",
        sa.Column(
            "subgroup_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("subgroups.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.PrimaryKeyConstraint("subgroup_id", "user_id"),
    )

    # ------------------------------------------------------------------
    # settlements  (defined before ledger_entries which references it)
    # ------------------------------------------------------------------
    op.create_table(
        "settlements",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id", sa.Uuid(as_uuid=True), sa.ForeignKey("groups.id"), nullable=True
        ),
        sa.Column(
            "payer_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "payee_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("method", sa.String(50), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "settled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "method IN ('upi', 'cash', 'bank', 'other')",
            name="ck_settlement_method",
        ),
    )

    # ------------------------------------------------------------------
    # expenses
    # ------------------------------------------------------------------
    op.create_table(
        "expenses",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id", sa.Uuid(as_uuid=True), sa.ForeignKey("groups.id"), nullable=True
        ),
        sa.Column(
            "paid_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("vendor", sa.Text, nullable=True),
        sa.Column("invoice_date", sa.Date, nullable=True),
        sa.Column("invoice_number", sa.Text, nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("subtotal_minor", sa.BigInteger, nullable=True),
        sa.Column("total_minor", sa.BigInteger, nullable=False),
        sa.Column("source", sa.String(50), nullable=False, server_default="manual"),
        sa.Column(
            "parse_status", sa.String(50), nullable=False, server_default="parsed"
        ),
        sa.Column("pdf_object_key", sa.Text, nullable=True),
        sa.Column("raw_extraction", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('pdf', 'manual')",
            name="ck_expense_source",
        ),
        sa.CheckConstraint(
            "parse_status IN ('queued', 'parsed', 'needs_review', 'confirmed', 'failed')",
            name="ck_expense_parse_status",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'voided')",
            name="ck_expense_status",
        ),
    )

    # ------------------------------------------------------------------
    # expense_line_items
    # ------------------------------------------------------------------
    op.create_table(
        "expense_line_items",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "expense_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("expenses.id"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "quantity",
            sa.Numeric(precision=10, scale=3),
            nullable=False,
            server_default="1",
        ),
        sa.Column("unit_price_minor", sa.BigInteger, nullable=True),
        sa.Column("total_minor", sa.BigInteger, nullable=False),
        sa.Column("bundle_group_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "parent_line_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("expense_line_items.id"),
            nullable=True,
        ),
        sa.Column("discount_scope", sa.String(50), nullable=True),
        sa.Column("allocation", sa.String(50), nullable=True),
        sa.CheckConstraint(
            "kind IN ('item', 'tax', 'delivery_fee', 'platform_fee', "
            "'packing_fee', 'tip', 'discount', 'refund')",
            name="ck_line_item_kind",
        ),
        sa.CheckConstraint(
            "discount_scope IS NULL OR discount_scope IN ('item', 'cart')",
            name="ck_discount_scope",
        ),
        sa.CheckConstraint(
            "allocation IS NULL OR allocation IN ('equal', 'proportional', 'manual')",
            name="ck_allocation_method",
        ),
    )

    # ------------------------------------------------------------------
    # item_assignments
    # ------------------------------------------------------------------
    op.create_table(
        "item_assignments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "line_item_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("expense_line_items.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "weight",
            sa.Numeric(precision=10, scale=4),
            nullable=False,
            server_default="1",
        ),
        sa.Column("share_minor", sa.BigInteger, nullable=True),
        sa.UniqueConstraint("line_item_id", "user_id", name="uq_item_assignment"),
    )

    # ------------------------------------------------------------------
    # ledger_entries  (append-only)
    # ------------------------------------------------------------------
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id", sa.Uuid(as_uuid=True), sa.ForeignKey("groups.id"), nullable=True
        ),
        sa.Column(
            "expense_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("expenses.id"),
            nullable=True,
        ),
        sa.Column(
            "settlement_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("settlements.id"),
            nullable=True,
        ),
        sa.Column(
            "debtor_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "creditor_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("entry_type", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_ledger_amount_positive"),
        sa.CheckConstraint(
            "entry_type IN ('expense_share', 'refund_reversal', 'settlement', 'adjustment')",
            name="ck_ledger_entry_type",
        ),
    )

    # ------------------------------------------------------------------
    # Indexes for common query patterns
    # ------------------------------------------------------------------
    op.create_index("ix_ledger_group_id", "ledger_entries", ["group_id"])
    op.create_index("ix_ledger_debtor_id", "ledger_entries", ["debtor_id"])
    op.create_index("ix_ledger_creditor_id", "ledger_entries", ["creditor_id"])
    op.create_index("ix_ledger_expense_id", "ledger_entries", ["expense_id"])
    op.create_index("ix_expenses_group_id", "expenses", ["group_id"])
    op.create_index("ix_expenses_paid_by", "expenses", ["paid_by"])
    op.create_index(
        "ix_item_assignments_line_item_id", "item_assignments", ["line_item_id"]
    )


def downgrade() -> None:
    op.drop_table("ledger_entries")
    op.drop_table("item_assignments")
    op.drop_table("expense_line_items")
    op.drop_table("expenses")
    op.drop_table("settlements")
    op.drop_table("subgroup_members")
    op.drop_table("subgroups")
    op.drop_table("group_members")
    op.drop_table("groups")
    op.drop_table("users")
