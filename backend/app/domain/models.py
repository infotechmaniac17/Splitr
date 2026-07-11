"""
SQLAlchemy 2.x ORM models implementing ARCHITECTURE.md §3 schema.

All money columns are BIGINT (minor units / paise). Enum columns use
native_enum=False for cross-dialect compatibility (SQLite tests, Postgres prod).
JSON/JSONB columns fall back to sa.JSON on SQLite.

The append-only guard for LedgerEntry is registered as a Session event listener
at module import time and raises on any UPDATE or DELETE of that table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base

# ---------------------------------------------------------------------------
# Python enum definitions
# ---------------------------------------------------------------------------


class GroupMemberRole(StrEnum):
    admin = "admin"
    member = "member"


class LineItemKind(StrEnum):
    item = "item"
    tax = "tax"
    delivery_fee = "delivery_fee"
    platform_fee = "platform_fee"
    packing_fee = "packing_fee"
    tip = "tip"
    discount = "discount"
    refund = "refund"


class DiscountScope(StrEnum):
    item = "item"
    cart = "cart"


class AllocationMethod(StrEnum):
    equal = "equal"
    proportional = "proportional"
    manual = "manual"


class ExpenseSource(StrEnum):
    pdf = "pdf"
    manual = "manual"


class DiscountType(StrEnum):
    flat = "flat"
    percent = "percent"


class DiscountSource(StrEnum):
    manual = "manual"
    vendor_rule = "vendor_rule"
    extracted = "extracted"


class ParseStatus(StrEnum):
    queued = "queued"
    parsed = "parsed"
    needs_review = "needs_review"
    confirmed = "confirmed"
    failed = "failed"


class GstMode(StrEnum):
    """
    M6 item 4: how GST/tax is expressed on this expense's invoice.

    none               -- no GST signal detected at all.
    invoice_exclusive   -- GST is broken out as separate positive tax line(s)
                            /components on top of the item/fee subtotal.
    invoice_inclusive   -- invoice states prices/total already include GST
                            ("inclusive of GST", "GST included").
    item_level          -- per-line-item GST rates detected (restaurant-style
                            5%/18% per dish), captured on
                            expense_line_items.gst_rate/gst_amount_minor.
    """

    none = "none"
    invoice_exclusive = "invoice_exclusive"
    invoice_inclusive = "invoice_inclusive"
    item_level = "item_level"


class TaxComponentName(StrEnum):
    """
    M6 item 4: recognized Indian GST component names.

    Member NAMES intentionally match their VALUES exactly (unlike most other
    enums in this module, which use lowercase names) -- sa.Enum(...,
    native_enum=False) stores a member's `.name`, not its `.value`, unless
    `values_callable` is passed (see app.domain.models._enum). Every other
    StrEnum here happens to have name == value.lower() with a lowercase DB
    representation, so this was never an issue before; GST component codes
    are conventionally uppercase (matching the CHECK constraint and the
    Indian GST terminology itself), so the names are kept uppercase too.
    """

    CGST = "CGST"
    SGST = "SGST"
    IGST = "IGST"
    GST = "GST"
    CESS = "CESS"


class ExpenseStatus(StrEnum):
    active = "active"
    voided = "voided"


class LedgerEntryType(StrEnum):
    expense_share = "expense_share"
    refund_reversal = "refund_reversal"
    settlement = "settlement"
    adjustment = "adjustment"


class SettlementMethod(StrEnum):
    upi = "upi"
    cash = "cash"
    bank = "bank"
    other = "other"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _uuid4() -> uuid.UUID:
    return uuid.uuid4()


def _jsonb_column() -> sa.JSON:
    """JSON that uses native JSONB on PostgreSQL."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _enum(enum_class: type[StrEnum], name: str) -> sa.Enum:
    return sa.Enum(enum_class, native_enum=False, name=name, length=50)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    email: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    # Nullable: users created via POST /users before the auth build-out (and
    # any test fixture / seed user) have no password on file and cannot log
    # in via POST /auth/login until one is set. See alembic 0005.
    password_hash: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    default_currency: Mapped[str] = mapped_column(
        sa.String(3), nullable=False, default="INR"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_now_utc
    )

    # Relationships
    group_memberships: Mapped[list[GroupMember]] = relationship(
        "GroupMember", back_populates="user"
    )
    expenses_paid: Mapped[list[Expense]] = relationship(
        "Expense", back_populates="paid_by_user", foreign_keys="Expense.paid_by"
    )


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    simplify_debts: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_now_utc
    )

    # Relationships
    members: Mapped[list[GroupMember]] = relationship(
        "GroupMember", back_populates="group"
    )
    expenses: Mapped[list[Expense]] = relationship("Expense", back_populates="group")


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (sa.PrimaryKeyConstraint("group_id", "user_id"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("groups.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    role: Mapped[GroupMemberRole] = mapped_column(
        _enum(GroupMemberRole, "group_member_role"),
        nullable=False,
        default=GroupMemberRole.member,
    )
    joined_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_now_utc
    )
    left_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # Relationships
    group: Mapped[Group] = relationship("Group", back_populates="members")
    user: Mapped[User] = relationship("User", back_populates="group_memberships")


class Subgroup(Base):
    __tablename__ = "subgroups"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("groups.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # Relationships
    members: Mapped[list[SubgroupMember]] = relationship(
        "SubgroupMember", back_populates="subgroup"
    )


class SubgroupMember(Base):
    __tablename__ = "subgroup_members"
    __table_args__ = (sa.PrimaryKeyConstraint("subgroup_id", "user_id"),)

    subgroup_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("subgroups.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )

    # Relationships
    subgroup: Mapped[Subgroup] = relationship("Subgroup", back_populates="members")


class VendorDiscountRule(Base):
    """
    M6 item 3: a rule that auto-applies a known vendor promotion/discount
    to draft expenses matching that vendor.

    group_id NULL means a "creator-global" rule: usable across every group
    the creator belongs to (see app/api/vendor_discount_rules.py), editable
    only by created_by. group_id set means the rule is scoped to that one
    group and editable by any admin of it.

    vendor_pattern is stored PRE-NORMALIZED (via
    app.extraction.vendor_detect.normalize_vendor_text, applied at write
    time in the Pydantic schema / CRUD layer -- see
    app/api/schemas.py:VendorDiscountRuleCreate) so
    app.domain.vendor_discount.match_rule can compare it directly against
    an already-normalized vendor string without re-normalizing stored data
    on every read.

    Never hard-deleted: expenses.discount_rule_id has a FK to this table
    (ON DELETE SET NULL) for historical lineage, so "deleting" a rule is
    always a soft delete (active=false) via the CRUD API.
    """

    __tablename__ = "vendor_discount_rules"
    __table_args__ = (
        sa.CheckConstraint(
            "min_order_total_minor >= 0", name="ck_vendor_rule_min_order_nonneg"
        ),
        sa.CheckConstraint(
            "discount_type IN ('flat', 'percent')",
            name="ck_vendor_rule_discount_type",
        ),
        sa.CheckConstraint(
            "(discount_type = 'flat' AND discount_value_minor > 0 "
            " AND discount_percent IS NULL)"
            " OR "
            "(discount_type = 'percent' AND discount_percent > 0 "
            " AND discount_percent <= 100 AND discount_value_minor IS NULL)",
            name="ck_vendor_rule_discount_value_shape",
        ),
        sa.Index(
            "ix_vendor_discount_rules_group_active_pattern",
            "group_id",
            "active",
            "vendor_pattern",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("groups.id"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    # Pre-normalized (see class docstring) — .strip().lower() applied at
    # write time via app.extraction.vendor_detect.normalize_vendor_text.
    vendor_pattern: Mapped[str] = mapped_column(sa.Text, nullable=False)
    min_order_total_minor: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    discount_type: Mapped[DiscountType] = mapped_column(
        _enum(DiscountType, "vendor_rule_discount_type"), nullable=False
    )
    discount_value_minor: Mapped[int | None] = mapped_column(
        sa.BigInteger, nullable=True
    )
    discount_percent: Mapped[Any] = mapped_column(
        sa.Numeric(precision=5, scale=2), nullable=True
    )
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=_now_utc,
        onupdate=_now_utc,
    )


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        # M2/M3: DB-level guard — total_minor must be positive.
        # SQLite parses but does not enforce CHECK; Postgres enforces it.
        sa.CheckConstraint("total_minor > 0", name="ck_expense_total_positive"),
        # M6 item 3: discount_type, if set, must be one of the two supported
        # kinds. SQLite parses but does not enforce; Postgres does.
        sa.CheckConstraint(
            "discount_type IS NULL OR discount_type IN ('flat', 'percent')",
            name="ck_expense_discount_type",
        ),
        sa.CheckConstraint(
            "discount_source IS NULL OR discount_source IN "
            "('manual', 'vendor_rule', 'extracted')",
            name="ck_expense_discount_source",
        ),
        # M6 item 4: gst_mode is NOT NULL (unlike discount_type/source, which
        # are NULL until a discount is known) -- every expense has SOME GST
        # mode, even if it's the neutral 'none'.
        sa.CheckConstraint(
            "gst_mode IN ('none', 'invoice_exclusive', 'invoice_inclusive', "
            "'item_level')",
            name="ck_expense_gst_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("groups.id"), nullable=True
    )
    paid_by: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    vendor: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR")
    subtotal_minor: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    total_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    source: Mapped[ExpenseSource] = mapped_column(
        _enum(ExpenseSource, "expense_source"),
        nullable=False,
        default=ExpenseSource.manual,
    )
    # M6: Default is 'queued' (the neutral starting state for any new expense
    # row).  Manual-expense creation at the API layer explicitly overrides this
    # to 'parsed' so the confirm flow works; PDF expenses start truly queued.
    parse_status: Mapped[ParseStatus] = mapped_column(
        _enum(ParseStatus, "parse_status"),
        nullable=False,
        default=ParseStatus.queued,
        server_default="queued",
    )
    pdf_object_key: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    raw_extraction: Mapped[dict[str, Any] | None] = mapped_column(
        _jsonb_column(), nullable=True
    )
    status: Mapped[ExpenseStatus] = mapped_column(
        _enum(ExpenseStatus, "expense_status"),
        nullable=False,
        default=ExpenseStatus.active,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_now_utc
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # -----------------------------------------------------------------
    # M6 item 3: vendor discount rules — SNAPSHOT columns.
    #
    # These are frozen at the moment a discount is applied (manually, by a
    # matched vendor rule, or extracted from the PDF's own line items) and
    # are NEVER re-derived from vendor_discount_rules later. If the rule
    # that produced discount_rule_id is subsequently edited, deactivated,
    # or (never actually) deleted, this expense's discount_* values do NOT
    # change -- they are historical fact, exactly like item_assignments.
    # share_minor is frozen at confirmation. See app/domain/vendor_discount.
    # py for the matching logic that populates these at application time.
    # -----------------------------------------------------------------
    discount_type: Mapped[DiscountType | None] = mapped_column(
        _enum(DiscountType, "expense_discount_type"),
        nullable=True,
        comment=(
            "Snapshot of the applied discount's type at the time it was "
            "applied. Never re-derived from vendor_discount_rules later."
        ),
    )
    discount_value_minor: Mapped[int | None] = mapped_column(
        sa.BigInteger,
        nullable=True,
        comment="Snapshot: flat discount amount in minor units, if discount_type='flat'.",
    )
    discount_percent: Mapped[Any] = mapped_column(
        sa.Numeric(precision=5, scale=2),
        nullable=True,
        comment="Snapshot: percent discount (0-100], if discount_type='percent'.",
    )
    discount_threshold_minor: Mapped[int | None] = mapped_column(
        sa.BigInteger,
        nullable=True,
        comment=(
            "Snapshot of the vendor rule's min_order_total_minor at "
            "application time, for audit/display only -- not re-checked."
        ),
    )
    discount_source: Mapped[DiscountSource | None] = mapped_column(
        _enum(DiscountSource, "expense_discount_source"),
        nullable=True,
        comment=(
            "Where this discount snapshot came from: 'manual' (user-entered, "
            "never auto-overwritten), 'vendor_rule' (matched by "
            "app.domain.vendor_discount.match_rule), or 'extracted' (summed "
            "from kind='discount' line items by the PDF pipeline)."
        ),
    )
    discount_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("vendor_discount_rules.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "The vendor_discount_rules row that produced this snapshot, if "
            "discount_source='vendor_rule'. Informational lineage only -- "
            "the discount_* values above are never re-read from this rule."
        ),
    )

    # -----------------------------------------------------------------
    # M6 item 4: GST structured data.
    #
    # gst_mode records how the invoice expressed tax (see GstMode). Like
    # discount_type/discount_source above, this is written ONLY inside the
    # original_status-confirmed guard in app/extraction/tasks.py
    # (_persist_pipeline_result) or at manual-expense creation time, and is
    # frozen once confirmed (see pg_guards.EXPENSE_STATE_MACHINE_GUARD_
    # FUNCTION_DDL_V4).
    #
    # needs_review is a SEPARATE signal from parse_status='needs_review':
    # it is set when the GST-specific arithmetic invariants (app/domain/
    # gst.py) don't reconcile, even though the BASE arithmetic invariants
    # (app/extraction/validation.py:validate_extraction) already passed and
    # parse_status is 'parsed'. Kept deliberately independent of the
    # parse_status state machine (see app/extraction/tasks.py for the full
    # rationale) so GST-only inconsistencies don't have to be retrofitted
    # into that enum's already-exhaustively-enumerated legal transition
    # graph. POST /expenses/{id}/confirm rejects confirmation while this is
    # true (see app/api/expenses.py).
    # -----------------------------------------------------------------
    gst_mode: Mapped[GstMode] = mapped_column(
        _enum(GstMode, "expense_gst_mode"),
        nullable=False,
        default=GstMode.none,
        server_default="none",
        comment="How this invoice expresses GST -- see GstMode.",
    )
    needs_review: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        default=False,
        server_default=sa.false(),
        comment=(
            "Set when GST-specific arithmetic invariants (app/domain/gst.py) "
            "fail to reconcile, independent of parse_status. Confirmation is "
            "blocked while this is true."
        ),
    )

    @property
    def is_frozen_shares(self) -> bool:
        """
        M6-M8 total-reconciliation ruling (item 7): True iff this expense's
        item_assignments are all frozen (the M1 explicit-shares/equal-split
        flow). Delegates to app.domain.splitting.is_frozen_shares -- the
        SAME predicate app.api.expenses._resolve_allocation,
        patch_expense_discount, and accept_computed_total use for their 422
        guards, so this API-visible flag can never disagree with them.

        Requires `self.line_items` (and each line's `.assignments`) to
        already be eagerly loaded (selectinload) by the caller -- accessing
        this property on a lazy, un-loaded relationship inside an async
        context raises MissingGreenlet, by design (no implicit N+1 lazy
        load is ever triggered by serializing an ExpenseResponse).

        Deferred import: app.domain.splitting imports app.domain.models
        (for its enums), so importing it at module scope here would be
        circular.
        """
        from app.domain.splitting import is_frozen_shares as _is_frozen_shares

        return _is_frozen_shares(self.line_items)

    # Relationships
    group: Mapped[Group | None] = relationship("Group", back_populates="expenses")
    paid_by_user: Mapped[User] = relationship(
        "User", back_populates="expenses_paid", foreign_keys=[paid_by]
    )
    line_items: Mapped[list[ExpenseLineItem]] = relationship(
        "ExpenseLineItem",
        back_populates="expense",
        cascade="all, delete-orphan",
        order_by="ExpenseLineItem.line_no",
    )
    tax_components: Mapped[list[ExpenseTaxComponent]] = relationship(
        "ExpenseTaxComponent",
        back_populates="expense",
        cascade="all, delete-orphan",
    )
    member_allocations: Mapped[list[ExpenseMemberAllocation]] = relationship(
        "ExpenseMemberAllocation",
        back_populates="expense",
        cascade="all, delete-orphan",
    )


class ExpenseLineItem(Base):
    __tablename__ = "expense_line_items"
    __table_args__ = (
        # M2 reviewer HIGH: refund retries must not double-post. NULLs are
        # exempt from uniqueness on both SQLite and Postgres.
        sa.UniqueConstraint(
            "expense_id", "idempotency_key", name="uq_line_item_idempotency_key"
        ),
        # M6 item 4: per-item GST rate/amount, only meaningful when
        # expense.gst_mode == 'item_level'. NULL on every other line (fees,
        # tax-kind lines, discounts, refunds, and item lines on invoices
        # that aren't item_level).
        sa.CheckConstraint(
            "gst_rate IS NULL OR (gst_rate >= 0 AND gst_rate <= 100)",
            name="ck_line_item_gst_rate_range",
        ),
        sa.CheckConstraint(
            "gst_amount_minor IS NULL OR gst_amount_minor >= 0",
            name="ck_line_item_gst_amount_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    expense_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("expenses.id"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    kind: Mapped[LineItemKind] = mapped_column(
        _enum(LineItemKind, "line_item_kind"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    quantity: Mapped[Any] = mapped_column(
        sa.Numeric(precision=10, scale=3), nullable=False, default=1
    )
    unit_price_minor: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    total_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    bundle_group_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), nullable=True
    )
    parent_line_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("expense_line_items.id"), nullable=True
    )
    discount_scope: Mapped[DiscountScope | None] = mapped_column(
        _enum(DiscountScope, "discount_scope"), nullable=True
    )
    allocation: Mapped[AllocationMethod | None] = mapped_column(
        _enum(AllocationMethod, "allocation_method"), nullable=True
    )
    # Set only on refund lines created via POST /expenses/{id}/refunds when
    # the client supplies a key; guards against duplicate posting on retry.
    idempotency_key: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # M6 item 4: per-item GST detail (see __table_args__ CHECKs above and
    # GstMode.item_level). No DB-level immutability trigger is attached to
    # these two columns specifically -- see migration 0010's docstring for
    # why (expense_line_items as a whole has never had a confirm-guard
    # trigger, because refund lines legitimately get INSERTed onto an
    # already-confirmed expense; these GST columns are protected purely at
    # the application layer, by the same original_status guard in
    # app/extraction/tasks.py that protects every other line-item write).
    gst_rate: Mapped[Any] = mapped_column(
        sa.Numeric(precision=5, scale=2), nullable=True
    )
    gst_amount_minor: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)

    # Relationships
    expense: Mapped[Expense] = relationship("Expense", back_populates="line_items")
    assignments: Mapped[list[ItemAssignment]] = relationship(
        "ItemAssignment",
        back_populates="line_item",
        cascade="all, delete-orphan",
    )


class ItemAssignment(Base):
    __tablename__ = "item_assignments"
    __table_args__ = (
        sa.UniqueConstraint("line_item_id", "user_id", name="uq_item_assignment"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    line_item_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("expense_line_items.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    weight: Mapped[Any] = mapped_column(
        sa.Numeric(precision=10, scale=4), nullable=False, default=1
    )
    share_minor: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)

    # Relationships
    line_item: Mapped[ExpenseLineItem] = relationship(
        "ExpenseLineItem", back_populates="assignments"
    )


class ExpenseTaxComponent(Base):
    """
    M6 item 4: one named GST/tax component (CGST/SGST/IGST/GST/CESS) on an
    expense -- e.g. a Rs.1000 order with 9% CGST + 9% SGST produces two rows
    here (each amount_minor=9000... in paise, i.e. 90*100), rather than a
    single opaque kind='tax' expense_line_items row. Extends, does not
    replace, that pre-existing line-item mechanism (see
    app/extraction/tasks.py and app/domain/gst.py for how the two interact).

    UNIQUE(expense_id, name): at most one row per named component per
    expense -- CGST and SGST can coexist, but you cannot have two CGST rows
    (a second printed CGST line means the extractor should have summed them
    into one component, not created a duplicate).

    Immutable once the parent expense is confirmed, via the generic
    reject_mutation_if_expense_confirmed('expense_id', 'direct') trigger
    (migration 0010) -- see that migration's docstring for why this table
    (unlike expense_line_items) is safe to attach the generic direct-FK
    guard to unmodified: nothing besides the guarded pipeline-persistence
    path ever writes to this table, so there is no refund-style append
    pattern to carve an escape hatch for.
    """

    __tablename__ = "expense_tax_components"
    __table_args__ = (
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

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    expense_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("expenses.id"), nullable=False
    )
    name: Mapped[TaxComponentName] = mapped_column(
        _enum(TaxComponentName, "tax_component_name"), nullable=False
    )
    rate: Mapped[Any] = mapped_column(sa.Numeric(precision=5, scale=2), nullable=True)
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)

    # Relationships
    expense: Mapped[Expense] = relationship("Expense", back_populates="tax_components")


class ExpenseMemberAllocation(Base):
    """
    M6 item 5: one row per (expense, member) recording the FINAL discount +
    GST breakdown produced by app.domain.splitting.compute_allocation at
    confirmation time -- an audit/read-model summary table, not itself the
    source of truth for money (the ledger_entries posted in the SAME
    confirm transaction are that source of truth; see
    app/api/expenses.py:confirm_expense, where `shares` fed to
    post_expense_to_ledger are these same breakdowns' total_minor values).

    Written ONLY inside the confirm transaction, after parse_status has
    already flipped to 'confirmed' -- covered by
    reject_mutation_if_expense_confirmed()'s existing same-transaction
    (xmin) escape hatch (migration 0006), the exact same mechanism that
    already lets confirm_expense freeze item_assignments.share_minor in the
    same transaction. No new guard function/version needed: this is a plain
    child table with a DIRECT expense_id FK, exactly what the generic
    function already handles (see MEMBER_ALLOCATION_CONFIRM_GUARD_TRIGGER_
    DDL in app/domain/pg_guards.py, modeled on migration 0010's
    TAX_COMPONENT_CONFIRM_GUARD_TRIGGER_DDL).

    No backfill for historical confirmed expenses (pre-item-5): a legacy
    confirmed expense's allocation can be trivially synthesized at READ time
    as base-only (base_minor=frozen share_minor, discount_minor=0,
    gst_minor=0, total_minor=share_minor) -- see
    GET /expenses/{id}/allocation-preview, which does exactly this minimal
    synthesis rather than a real backfill migration.
    """

    __tablename__ = "expense_member_allocations"
    __table_args__ = (
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

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    expense_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("expenses.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    base_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    discount_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    gst_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    total_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)

    # Relationships
    expense: Mapped[Expense] = relationship(
        "Expense", back_populates="member_allocations"
    )


class LedgerEntry(Base):
    """
    Append-only source of truth for money flows.

    amount_minor MUST be > 0. Direction is encoded in debtor_id/creditor_id:
      - expense_share: debtor owes creditor (the payer)
      - settlement:    debtor=payee, creditor=payer (reverses the debt)
      - refund_reversal / adjustment: similarly directional
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        sa.CheckConstraint("amount_minor > 0", name="ck_ledger_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("groups.id"), nullable=True
    )
    expense_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("expenses.id"), nullable=True
    )
    settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("settlements.id"), nullable=True
    )
    debtor_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    creditor_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        _enum(LedgerEntryType, "ledger_entry_type"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_now_utc
    )


class Settlement(Base):
    __tablename__ = "settlements"
    __table_args__ = (
        # M2/M3: settlement amount must always be positive (direction encoded
        # in payer_id/payee_id, not by sign).
        sa.CheckConstraint("amount_minor > 0", name="ck_settlement_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=_uuid4
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("groups.id"), nullable=True
    )
    payer_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    payee_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    method: Mapped[SettlementMethod] = mapped_column(
        _enum(SettlementMethod, "settlement_method"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    settled_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_now_utc
    )


# ---------------------------------------------------------------------------
# Append-only guard — raises on any UPDATE or DELETE of LedgerEntry rows.
# Registered on the Session class so it covers all session instances.
# ---------------------------------------------------------------------------


@event.listens_for(Session, "before_flush")
def _guard_ledger_append_only(
    session: Session, flush_context: Any, instances: Any
) -> None:
    for obj in session.dirty:
        if isinstance(obj, LedgerEntry):
            raise RuntimeError(
                "LedgerEntry rows are immutable. "
                "Use a new entry with entry_type='adjustment' for corrections."
            )
    for obj in session.deleted:
        if isinstance(obj, LedgerEntry):
            raise RuntimeError(
                "LedgerEntry rows cannot be deleted. Ledger is append-only."
            )
