"""vendor_discount_rules table — M6 item 3

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-04 00:03:00.000000

Adds the `vendor_discount_rules` table (see app/domain/models.py:
VendorDiscountRule and app/domain/vendor_discount.py for the matching
logic). A rule auto-applies a known vendor promotion/discount to draft
expenses matching that vendor.

Columns
-------
  id                     UUID PK
  group_id               FK groups, NULL = "creator-global" rule (usable
                          by its creator across any of their groups; see
                          app/api/vendor_discount_rules.py for the
                          authorization split between group-scoped and
                          global rules)
  created_by             FK users, NOT NULL
  vendor_pattern         TEXT NOT NULL — PRE-NORMALIZED (lower/stripped via
                          app.extraction.vendor_detect.normalize_vendor_text)
                          at write time in the Pydantic schema / CRUD layer,
                          so match_rule() can compare it directly against an
                          already-normalized vendor string without
                          re-normalizing stored data on every read.
  min_order_total_minor  BIGINT NOT NULL DEFAULT 0, CHECK >= 0
  discount_type          TEXT NOT NULL, CHECK IN ('flat', 'percent')
  discount_value_minor   BIGINT NULL — set iff discount_type='flat'
  discount_percent       NUMERIC(5,2) NULL — set iff discount_type='percent'
  active                 BOOLEAN NOT NULL DEFAULT true — soft-delete flag;
                          rows are NEVER hard-deleted because
                          expenses.discount_rule_id (migration 0009) has an
                          FK to this table for historical lineage.
  created_at, updated_at

CHECK constraints (enforced on Postgres; parsed-but-not-enforced on SQLite,
matching every other CHECK constraint in this codebase per CLAUDE.md's
testing-tiers note):
  - ck_vendor_rule_min_order_nonneg: min_order_total_minor >= 0
  - ck_vendor_rule_discount_type: discount_type IN ('flat', 'percent')
  - ck_vendor_rule_discount_value_shape: exactly one of
    (discount_value_minor set + discount_type='flat') or
    (discount_percent set, 0 < percent <= 100, + discount_type='percent')
    is satisfied -- the two discount shapes are mutually exclusive.

Index: (group_id, active, vendor_pattern) -- the exact shape of the lookup
`find_matching_rule()` in app/domain/vendor_discount.py performs (active
rules for a vendor pattern, scoped to a group or global).

This migration is Postgres-only for the CHECK constraints and index details
that SQLite can't enforce/would need different syntax for, but the TABLE
itself is created on both dialects via the ORM-driven `op.create_table` (so
SQLite test runs get the table, just without enforced CHECKs -- consistent
with how expenses.total_minor's CHECK is handled: the constraint is declared
in both the ORM model's __table_args__ (so SQLAlchemy's create_all in tests
also attempts it) and mirrored here for the Alembic-driven path).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    """Return True when running against a PostgreSQL backend."""
    ctx = op.get_context()
    return ctx.dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "vendor_discount_rules",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("groups.id"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("vendor_pattern", sa.Text(), nullable=False),
        sa.Column(
            "min_order_total_minor",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("discount_type", sa.Text(), nullable=False),
        sa.Column("discount_value_minor", sa.BigInteger(), nullable=True),
        sa.Column("discount_percent", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_vendor_discount_rules_group_active_pattern",
        "vendor_discount_rules",
        ["group_id", "active", "vendor_pattern"],
    )

    if not _is_postgres():
        # SQLite: table + index only. CHECK constraints are parsed but not
        # enforced by SQLite anyway, and ALTER TABLE ADD CONSTRAINT is not
        # supported there — skip, matching migration 0002's precedent.
        return

    op.execute(
        sa.text(
            "ALTER TABLE vendor_discount_rules "
            "ADD CONSTRAINT ck_vendor_rule_min_order_nonneg "
            "CHECK (min_order_total_minor >= 0)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE vendor_discount_rules "
            "ADD CONSTRAINT ck_vendor_rule_discount_type "
            "CHECK (discount_type IN ('flat', 'percent'))"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE vendor_discount_rules "
            "ADD CONSTRAINT ck_vendor_rule_discount_value_shape "
            "CHECK ("
            "(discount_type = 'flat' AND discount_value_minor > 0 "
            " AND discount_percent IS NULL)"
            " OR "
            "(discount_type = 'percent' AND discount_percent > 0 "
            " AND discount_percent <= 100 AND discount_value_minor IS NULL)"
            ")"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_vendor_discount_rules_group_active_pattern",
        table_name="vendor_discount_rules",
    )
    op.drop_table("vendor_discount_rules")
