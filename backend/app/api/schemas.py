"""
Pydantic v2 request/response schemas for the Splitr M1 API.

Auth is out of scope for M1 — user IDs are accepted directly in payloads.
All money fields are integers (minor units / paise).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.domain.models import (
    AllocationMethod,
    DiscountScope,
    ExpenseSource,
    ExpenseStatus,
    GroupMemberRole,
    LedgerEntryType,
    LineItemKind,
    ParseStatus,
    SettlementMethod,
)

# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class UserCreate(BaseModel):
    name: str
    email: str
    phone: str | None = None
    avatar_url: str | None = None
    default_currency: str = "INR"


class UserResponse(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    phone: str | None
    avatar_url: str | None
    default_currency: str
    created_at: datetime

    model_config = {"from_attributes": True}


class UserPublicResponse(BaseModel):
    """
    Minimal profile shape for GET /users/{user_id} when the caller is not
    the target user themself: name + avatar only, no email/phone. Returned
    to authenticated callers who share an active group with the target
    (e.g. rendering a group member list) -- never to non-members, who get
    403 instead (see app/api/users.py:get_user).
    """

    id: uuid.UUID
    name: str
    avatar_url: str | None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str = Field(..., min_length=8, max_length=128)
    phone: str | None = None
    avatar_url: str | None = None
    default_currency: str = "INR"


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    """Returned by /auth/register and /auth/login."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access token lifetime, seconds
    user: UserResponse


class AccessTokenResponse(BaseModel):
    """Returned by /auth/refresh -- a new access token only (no rotation)."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


class GroupCreate(BaseModel):
    name: str
    created_by: uuid.UUID
    simplify_debts: bool = True


class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    created_by: uuid.UUID
    simplify_debts: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class GroupMemberAdd(BaseModel):
    user_id: uuid.UUID
    role: GroupMemberRole = GroupMemberRole.member


class GroupMemberResponse(BaseModel):
    group_id: uuid.UUID
    user_id: uuid.UUID
    role: GroupMemberRole
    joined_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Line items
# ---------------------------------------------------------------------------


class LineItemCreate(BaseModel):
    line_no: int = 1
    kind: LineItemKind = LineItemKind.item
    description: str | None = None
    # M4: Decimal (not float) so no IEEE-754 rounding enters any path that
    # feeds into money arithmetic. The ORM column is NUMERIC(10,3).
    quantity: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))
    unit_price_minor: int | None = None
    total_minor: int
    allocation: AllocationMethod | None = None
    # M2: discount/refund plumbing.
    discount_scope: DiscountScope | None = None
    # References the line_no of the parent item within the same payload
    # (IDs don't exist yet at create time). Used by refunds and item-scoped
    # discounts to inherit the parent's assignment ratios.
    parent_line_no: int | None = None

    @model_validator(mode="after")
    def validate_sign_conventions(self) -> LineItemCreate:
        # ARCHITECTURE.md §3: total_minor is signed — negative for
        # discount/refund rows, non-negative for everything else.
        if self.kind in (LineItemKind.discount, LineItemKind.refund):
            if self.total_minor >= 0:
                raise ValueError(
                    f"{self.kind} line total_minor must be negative, "
                    f"got {self.total_minor}"
                )
        elif self.total_minor < 0:
            raise ValueError(
                f"{self.kind} line total_minor must be >= 0, got {self.total_minor}"
            )
        if (
            self.unit_price_minor is not None
            and self.unit_price_minor < 0
            and self.kind not in (LineItemKind.discount, LineItemKind.refund)
        ):
            raise ValueError("unit_price_minor cannot be negative for this kind")
        return self


class LineItemResponse(BaseModel):
    id: uuid.UUID
    expense_id: uuid.UUID
    line_no: int
    kind: LineItemKind
    description: str | None
    quantity: Any
    unit_price_minor: int | None
    total_minor: int
    allocation: AllocationMethod | None
    discount_scope: DiscountScope | None = None
    parent_line_id: uuid.UUID | None = None
    bundle_group_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Item assignments (M2)
# ---------------------------------------------------------------------------


class AssignmentIn(BaseModel):
    line_item_id: uuid.UUID
    user_id: uuid.UUID
    weight: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))


class AssignmentsPut(BaseModel):
    """Replaces ALL assignments of the expense (pre-confirmation only)."""

    assignments: list[AssignmentIn] = Field(..., min_length=1)


class AssignmentResponse(BaseModel):
    id: uuid.UUID
    line_item_id: uuid.UUID
    user_id: uuid.UUID
    weight: Any
    share_minor: int | None

    model_config = {"from_attributes": True}


class SharesResponse(BaseModel):
    expense_id: uuid.UUID
    shares: dict[uuid.UUID, int]


class RefundCreate(BaseModel):
    """Post-confirmation refund against one original item line."""

    parent_line_id: uuid.UUID
    amount_minor: int = Field(..., gt=0)  # positive; stored negative
    description: str | None = None
    # Optional client-supplied key; a retried request with the same key
    # returns the existing state instead of double-posting the refund.
    idempotency_key: str | None = Field(default=None, max_length=255)


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


class ExpenseCreate(BaseModel):
    """
    Manual expense creation payload.

    Exactly one of `participants` or `shares` must be provided:
      - participants: equal split among listed user IDs (uses largest-remainder)
      - shares: explicit {user_id: amount_minor} mapping (must sum to total_minor)

    The paid_by user is automatically included in splits; their share is the
    residual (total - others' shares) and does not generate a ledger entry.
    """

    group_id: uuid.UUID | None = None
    paid_by: uuid.UUID
    vendor: str | None = None
    invoice_date: date | None = None
    invoice_number: str | None = None
    currency: str = "INR"
    total_minor: int = Field(..., gt=0)
    line_items: list[LineItemCreate] = Field(default_factory=list)

    # Split specification — exactly one must be provided.
    participants: list[uuid.UUID] | None = None
    shares: dict[uuid.UUID, int] | None = None

    @model_validator(mode="after")
    def validate_split_spec(self) -> ExpenseCreate:
        if self.participants is None and self.shares is None:
            # M2: item-level flow — line items now, assignments later via
            # PUT /expenses/{id}/assignments. Line totals must reconcile.
            if not self.line_items:
                raise ValueError(
                    "Either 'participants' or 'shares' must be provided "
                    "(or 'line_items' for the item-level flow)"
                )
            lines_sum = sum(li.total_minor for li in self.line_items)
            if lines_sum != self.total_minor:
                raise ValueError(
                    f"line_items totals sum to {lines_sum} but total_minor "
                    f"is {self.total_minor}"
                )
            line_nos = [li.line_no for li in self.line_items]
            if len(set(line_nos)) != len(line_nos):
                raise ValueError("line_no values must be unique")
            for li in self.line_items:
                if li.parent_line_no is not None and li.parent_line_no not in line_nos:
                    raise ValueError(
                        f"parent_line_no {li.parent_line_no} does not match "
                        "any line in this payload"
                    )
            return self
        if self.participants is not None and self.shares is not None:
            raise ValueError("Provide either 'participants' or 'shares', not both")
        if self.shares is not None:
            # H2: reject any negative individual share up front — a negative
            # payer share can satisfy the sum check while hiding an invalid split.
            for uid, amt in self.shares.items():
                if amt < 0:
                    raise ValueError(
                        f"Share for user {uid} is {amt}; all shares must be >= 0."
                    )
            total = sum(self.shares.values())
            if total != self.total_minor:
                raise ValueError(
                    f"shares sum to {total} but total_minor is {self.total_minor}"
                )
        if self.participants is not None and len(self.participants) == 0:
            raise ValueError("participants list cannot be empty")
        return self


class ExpenseResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID | None
    paid_by: uuid.UUID
    vendor: str | None
    invoice_date: date | None
    invoice_number: str | None
    currency: str
    subtotal_minor: int | None
    total_minor: int
    source: ExpenseSource
    parse_status: ParseStatus
    status: ExpenseStatus
    created_at: datetime
    confirmed_at: datetime | None
    line_items: list[LineItemResponse] = Field(default_factory=list)
    # M4: not in the frozen v1 contract's base ExpenseResponse shape
    # (API_CONTRACT.md §2), but the upload / needs-review flows need a way
    # to reference the stored PDF (§4 point 1: "expense.pdf_object_key").
    # Optional so any consumer still coded against the base v1 shape parses
    # this response unchanged.
    pdf_object_key: str | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# raw_extraction (audit / needs_review correction UI) — API_CONTRACT.md §3-4
# ---------------------------------------------------------------------------


class ValidationIssueResponse(BaseModel):
    code: str
    message: str
    line_no: int | None = None


class ExtractionValidationResponse(BaseModel):
    ok: bool
    issues: list[ValidationIssueResponse] = Field(default_factory=list)


class ExtractionAttemptResponse(BaseModel):
    attempt: int
    provider: str
    route: str | None = None
    raw: Any | None = None
    validation: ExtractionValidationResponse | None = None
    error: str | None = None


class RawExtractionResponse(BaseModel):
    attempts: list[ExtractionAttemptResponse] = Field(default_factory=list)
    final_error: str | None = None


# ---------------------------------------------------------------------------
# needs_review correction (M4) — API_CONTRACT.md §4
# ---------------------------------------------------------------------------


class LineItemsCorrection(BaseModel):
    """
    PUT /expenses/{id}/line-items payload (API_CONTRACT.md §4): the
    corrected working set of line items for a `needs_review` expense.
    Re-validated server-side by the same deterministic engine that gates
    parse_status='parsed' for the PDF pipeline (invariant #4) before the
    transition out of needs_review is allowed.
    """

    line_items: list[LineItemCreate] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


class PairwiseBalance(BaseModel):
    debtor_id: uuid.UUID
    creditor_id: uuid.UUID
    net_amount_minor: int


class GroupBalancesResponse(BaseModel):
    group_id: uuid.UUID
    balances: list[PairwiseBalance]


class UserBalanceResponse(BaseModel):
    user_id: uuid.UUID
    net_balance_minor: int  # positive = owed to user, negative = user owes others


class SuggestedTransaction(BaseModel):
    """One suggested (not yet recorded) payment from payer to payee."""

    payer_id: uuid.UUID
    payee_id: uuid.UUID
    amount_minor: int


class SimplifiedDebtsResponse(BaseModel):
    """
    GET /groups/{group_id}/simplified-debts.

    `simplified=True`: `transactions` is the minimal min-cash-flow set
    (<= n-1 entries) that would zero out every member's balance.
    `simplified=False`: the group has `simplify_debts=False`; `transactions`
    is instead the raw pairwise balances (same shape, one entry per
    non-cancelling debtor/creditor pair, no netting-reduction applied).

    Either way, these are suggestions only -- no ledger entries are posted
    by this endpoint. Recording an actual payment still requires calling
    POST /settlements.
    """

    group_id: uuid.UUID
    simplified: bool
    transactions: list[SuggestedTransaction]


# ---------------------------------------------------------------------------
# Ledger entries (read-only)
# ---------------------------------------------------------------------------


class LedgerEntryResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID | None
    expense_id: uuid.UUID | None
    settlement_id: uuid.UUID | None
    debtor_id: uuid.UUID
    creditor_id: uuid.UUID
    amount_minor: int
    entry_type: LedgerEntryType
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Settlements
# ---------------------------------------------------------------------------


class SettlementCreate(BaseModel):
    group_id: uuid.UUID | None = None
    payer_id: uuid.UUID
    payee_id: uuid.UUID
    amount_minor: int = Field(..., gt=0)
    method: SettlementMethod = SettlementMethod.other
    note: str | None = None

    @model_validator(mode="after")
    def validate_payer_not_payee(self) -> SettlementCreate:
        # M5: self-settlement creates a ledger self-loop that never cancels and
        # would corrupt balance queries.
        if self.payer_id == self.payee_id:
            raise ValueError("Settlement payer and payee cannot be the same user.")
        return self


class SettlementResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID | None
    payer_id: uuid.UUID
    payee_id: uuid.UUID
    amount_minor: int
    method: SettlementMethod
    note: str | None
    settled_at: datetime

    model_config = {"from_attributes": True}
