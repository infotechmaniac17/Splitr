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


class ParseStatus(StrEnum):
    queued = "queued"
    parsed = "parsed"
    needs_review = "needs_review"
    confirmed = "confirmed"
    failed = "failed"


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


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        # M2/M3: DB-level guard — total_minor must be positive.
        # SQLite parses but does not enforce CHECK; Postgres enforces it.
        sa.CheckConstraint("total_minor > 0", name="ck_expense_total_positive"),
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


class ExpenseLineItem(Base):
    __tablename__ = "expense_line_items"
    __table_args__ = (
        # M2 reviewer HIGH: refund retries must not double-post. NULLs are
        # exempt from uniqueness on both SQLite and Postgres.
        sa.UniqueConstraint(
            "expense_id", "idempotency_key", name="uq_line_item_idempotency_key"
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
