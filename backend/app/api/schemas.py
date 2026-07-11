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

from pydantic import BaseModel, Field, field_serializer, model_validator

from app.domain.models import (
    AllocationMethod,
    DiscountScope,
    DiscountSource,
    DiscountType,
    ExpenseSource,
    ExpenseStatus,
    GroupMemberRole,
    GstMode,
    LedgerEntryType,
    LineItemKind,
    ParseStatus,
    SettlementMethod,
    TaxComponentName,
)
from app.extraction.vendor_detect import normalize_vendor_text

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


class GroupMemberInfo(BaseModel):
    """One row of GET /groups/{group_id}/members -- membership + a slim
    profile projection (name/avatar only, matching UserPublicResponse) so
    the roster can be rendered without a second round-trip per member."""

    user_id: uuid.UUID
    name: str
    avatar_url: str | None
    role: GroupMemberRole
    joined_at: datetime


class GroupMembersResponse(BaseModel):
    group_id: uuid.UUID
    members: list[GroupMemberInfo]


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


class LineItemAssignmentInfo(BaseModel):
    """
    M6-M8 total-reconciliation ruling, item 6: one user's CURRENT assignment
    on a line item, embedded read-only inside LineItemResponse.assignments
    so the assignment-UI can render "who's on this line" from the same
    GET /expenses/{id} response that already carries the line items --
    no extra round-trip, and no N+1 query (populated from the SAME
    `.assignments` relationship _load_lines_with_assignments already
    eager-loads via selectinload).

    Deliberately id-less (no ItemAssignment.id / share_minor here) -- this
    is a lightweight "current assignment" projection for the UI, not a
    replacement for GET /expenses/{id}/shares or the allocation-preview
    endpoint, which remain the source of truth for computed money.
    """

    user_id: uuid.UUID
    # Numeric(10,4) on the wire, serialized as a string -- same convention
    # as LineItemResponse.quantity below, so no IEEE-754 float enters a
    # money-adjacent field.
    weight: Decimal

    model_config = {"from_attributes": True}

    @field_serializer("weight")
    def serialize_weight(self, value: Decimal) -> str:
        return str(value)


class LineItemResponse(BaseModel):
    id: uuid.UUID
    expense_id: uuid.UUID
    line_no: int
    kind: LineItemKind
    description: str | None
    # Decimal on the wire, serialized as a string (see packages/core/src/
    # schemas.ts's lineItemResponseSchema -- `quantity: z.string()`) so no
    # IEEE-754 float ever enters the frontend's parsing of a money-adjacent
    # field. Previously typed `Any`, which let FastAPI's default JSON
    # encoder emit a bare Decimal as a float, breaking the frontend's zod
    # validation on every expense with line items.
    quantity: Decimal
    unit_price_minor: int | None
    total_minor: int
    allocation: AllocationMethod | None
    discount_scope: DiscountScope | None = None
    parent_line_id: uuid.UUID | None = None
    bundle_group_id: uuid.UUID | None = None
    # M6 item 4 (API GAPS follow-up, M6-M8 item 5): per-item GST detail --
    # only meaningful (non-NULL) when the parent expense's gst_mode ==
    # 'item_level'; NULL on every other line, matching the DB columns
    # exactly (see app.domain.models.ExpenseLineItem.gst_rate/
    # gst_amount_minor).
    gst_rate: Decimal | None = None
    gst_amount_minor: int | None = None
    # M6-M8 total-reconciliation ruling, item 6: current assignments on this
    # line (see LineItemAssignmentInfo above). Populated from the SAME
    # eager-loaded `.assignments` relationship every ExpenseResponse route
    # already loads -- never a separate query.
    assignments: list[LineItemAssignmentInfo] = Field(default_factory=list)

    model_config = {"from_attributes": True}

    @field_serializer("quantity")
    def serialize_quantity(self, value: Decimal) -> str:
        return str(value)

    @field_serializer("gst_rate")
    def serialize_gst_rate(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


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


class BulkAssignmentIn(BaseModel):
    """
    POST /expenses/{id}/assignments/bulk payload (M6-M8 item 7a).

    Replace-set semantics PER ITEM (not the whole expense, unlike
    AssignmentsPut above): for every line item in `item_ids`, its existing
    assignments are replaced with one equal-weight (weight=1) row per member
    in `member_ids`. Convenience bulk-assign for the assignment UI ("assign
    these people to these items") -- for per-line weighted splits use
    PUT /expenses/{id}/assignments instead.
    """

    item_ids: list[uuid.UUID] = Field(..., min_length=1)
    member_ids: list[uuid.UUID] = Field(..., min_length=1)


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


# ---------------------------------------------------------------------------
# M6 item 5: discount + GST allocation preview
# ---------------------------------------------------------------------------


class MemberBreakdownResponse(BaseModel):
    """Mirrors app.domain.splitting.MemberBreakdown 1:1."""

    user_id: uuid.UUID
    base_minor: int
    discount_minor: int
    gst_minor: int
    total_minor: int


class AllocationProblem(BaseModel):
    """
    One reason a DRAFT expense's allocation could not be (fully) computed,
    or an informational note about it -- never a 500; see
    GET /expenses/{id}/allocation-preview.

    `count` / `line_ids` (M6-M8 total-reconciliation ruling, API GAPS item
    8): optional structured detail for problems about a SET of lines --
    currently only `unassigned_lines` populates them. Every other existing
    code (`needs_review`, `discount_recorded_but_inert`, `split_error`)
    leaves both None, unchanged from before this field was added.
    """

    code: str
    message: str
    count: int | None = None
    line_ids: list[uuid.UUID] | None = None


class AllocationPreviewResponse(BaseModel):
    expense_id: uuid.UUID
    # True for a confirmed expense (persisted expense_member_allocations
    # rows, never re-computed); False for a draft (live compute_allocation).
    confirmed: bool
    members: list[MemberBreakdownResponse] = Field(default_factory=list)
    subtotal_minor: int | None = None
    applied_discount_minor: int | None = None
    exclusive_gst_minor: int | None = None
    discount_recorded_but_inert: bool = False
    # Non-empty only for a draft expense whose allocation could not be
    # computed (e.g. unassigned lines) or that is flagged needs_review --
    # `members` is then empty rather than partially populated.
    problems: list[AllocationProblem] = Field(default_factory=list)


class RefundCreate(BaseModel):
    """Post-confirmation refund against one original item line."""

    parent_line_id: uuid.UUID
    amount_minor: int = Field(..., gt=0)  # positive; stored negative
    description: str | None = None
    # Optional client-supplied key; a retried request with the same key
    # returns the existing state instead of double-posting the refund.
    idempotency_key: str | None = Field(default=None, max_length=255)


class TaxComponentResponse(BaseModel):
    """
    M6 item 4 (API GAPS follow-up, M6-M8 item 5): one persisted
    expense_tax_components row (CGST/SGST/IGST/GST/CESS), embedded in
    ExpenseResponse.tax_components. Mirrors app.domain.models.
    ExpenseTaxComponent 1:1.
    """

    name: TaxComponentName
    rate: Decimal | None
    amount_minor: int

    model_config = {"from_attributes": True}

    @field_serializer("rate")
    def serialize_rate(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


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
    # M6 item 3: discount snapshot (see app/domain/vendor_discount.py). All
    # optional/None for expenses with no known discount, matching the
    # "additive, backward compatible" convention already used for
    # pdf_object_key above.
    #
    # M6 item 5 UPDATE: these fields USED to be informational/audit-only
    # (they never fed into total_minor, line item shares, or ledger
    # balances). That is no longer true: app.domain.splitting.
    # compute_allocation now reads this exact snapshot (via
    # discount_spec_from_expense) at confirmation time and layers it into
    # each member's actual owed amount -- see
    # GET /expenses/{id}/allocation-preview and POST /expenses/{id}/confirm.
    _DISCOUNT_FIELD_NOTE = (
        "Snapshot at the time the discount was applied (manual entry, a "
        "matched vendor rule, or an extracted printed line). As of M6 item "
        "5, this DOES feed into each member's actual owed amount at "
        "confirmation (see app.domain.splitting.compute_allocation and "
        "GET /expenses/{id}/allocation-preview) -- it is no longer purely "
        "informational."
    )
    discount_type: DiscountType | None = Field(
        default=None,
        description="Discount shape ('flat' or 'percent') from the matched "
        "vendor rule or manual entry. " + _DISCOUNT_FIELD_NOTE,
    )
    discount_value_minor: int | None = Field(
        default=None,
        description="Flat discount amount in minor units, when "
        "discount_type='flat'. " + _DISCOUNT_FIELD_NOTE,
    )
    discount_percent: Decimal | None = Field(
        default=None,
        description="Discount percentage, when discount_type='percent'. "
        + _DISCOUNT_FIELD_NOTE,
    )
    discount_threshold_minor: int | None = Field(
        default=None,
        description="The min_order_total_minor threshold of the matched "
        "rule, if any. " + _DISCOUNT_FIELD_NOTE,
    )
    discount_source: DiscountSource | None = Field(
        default=None,
        description="Where this discount snapshot came from: 'manual', "
        "'vendor_rule', or 'extracted'. " + _DISCOUNT_FIELD_NOTE,
    )
    discount_rule_id: uuid.UUID | None = Field(
        default=None,
        description="The VendorDiscountRule that produced this snapshot, "
        "if discount_source='vendor_rule'. " + _DISCOUNT_FIELD_NOTE,
    )
    # M6 item 4 (finance-logic-reviewer CRITICAL fix follow-up): this was
    # previously invisible via the API entirely, even though
    # POST /expenses/{id}/confirm's 422 response already documented it as
    # the reason confirmation is blocked. Additive/optional, defaulted, to
    # match the backward-compatible convention already used above for
    # pdf_object_key / discount_*.
    needs_review: bool = Field(
        default=False,
        description="Set when GST-specific arithmetic invariants "
        "(app/domain/gst.py) fail to reconcile against the CURRENTLY "
        "persisted line items / tax components. Independent of "
        "parse_status. Confirmation is blocked while this is true.",
    )
    # M6-M8 total-reconciliation ruling -- API GAPS items 5 and 7.
    gst_mode: GstMode = Field(
        default=GstMode.none,
        description="How this invoice expresses GST -- see "
        "app.domain.models.GstMode. Always set (defaults to 'none').",
    )
    tax_components: list[TaxComponentResponse] = Field(
        default_factory=list,
        description="Persisted expense_tax_components rows (CGST/SGST/"
        "IGST/GST/CESS), if any.",
    )
    is_frozen_shares: bool = Field(
        default=False,
        description="True iff this expense's item_assignments are all "
        "frozen (the M1 explicit-shares/equal-split flow, which never "
        "runs compute_allocation) -- the SAME predicate "
        "app.api.expenses._resolve_allocation, patch_expense_discount, "
        "and accept_computed_total use for their 422 guards (see "
        "app.domain.splitting.is_frozen_shares). False for an item-level "
        "(M2) draft expense with pending, unfrozen assignments.",
    )

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
# Vendor discount rules (M6 item 3)
# ---------------------------------------------------------------------------


def _validate_discount_shape(
    discount_type: DiscountType,
    discount_value_minor: int | None,
    discount_percent: Decimal | None,
) -> None:
    """Shared shape validation mirroring the DB CHECK constraint in
    migration 0008 (ck_vendor_rule_discount_value_shape): exactly one of the
    two discount shapes may be set, matching its own discount_type."""
    if discount_type == DiscountType.flat:
        if discount_value_minor is None or discount_value_minor <= 0:
            raise ValueError(
                "discount_value_minor must be > 0 when discount_type='flat'"
            )
        if discount_percent is not None:
            raise ValueError(
                "discount_percent must not be set when discount_type='flat'"
            )
    else:  # percent
        if discount_percent is None or not (
            Decimal("0") < discount_percent <= Decimal("100")
        ):
            raise ValueError(
                "discount_percent must be > 0 and <= 100 when discount_type='percent'"
            )
        if discount_value_minor is not None:
            raise ValueError(
                "discount_value_minor must not be set when discount_type='percent'"
            )


class ExpenseDiscountPatch(BaseModel):
    """
    PATCH /expenses/{id}/discount payload (M6-M8 item 7a, draft expenses
    only).

    `discount_type=None` means CLEAR the current snapshot and re-run
    vendor-rule auto-matching (see app.api.expenses.patch_expense_discount) --
    this is the only way to get back to a vendor-rule-sourced snapshot after
    a manual one was set, since manual always wins and is never
    auto-overwritten.

    `discount_type` set means SET a manual discount snapshot (always wins
    over any vendor rule from then on, per app.domain.vendor_discount's
    "manual always wins" precedence).
    """

    discount_type: DiscountType | None = None
    discount_value_minor: int | None = None
    discount_percent: Decimal | None = None
    discount_threshold_minor: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate(self) -> ExpenseDiscountPatch:
        if self.discount_type is not None:
            _validate_discount_shape(
                self.discount_type, self.discount_value_minor, self.discount_percent
            )
        return self


class VendorDiscountRuleCreate(BaseModel):
    """
    Create a vendor discount rule.

    `group_id=None` creates a "creator-global" rule (usable by its creator
    across any of their groups -- see GET /vendor-discount-rules/global).
    `group_id=<uuid>` creates a rule scoped to that one group (any admin of
    the group may create/edit it).

    `vendor_pattern` is normalized (`.strip().lower()`, via
    app.extraction.vendor_detect.normalize_vendor_text) HERE at the schema
    layer before it ever reaches the DB or the CRUD layer -- the stored
    value is always pre-normalized so app.domain.vendor_discount.match_rule
    can compare it directly against an already-normalized vendor string.
    """

    group_id: uuid.UUID | None = None
    vendor_pattern: str = Field(..., min_length=1)
    min_order_total_minor: int = Field(default=0, ge=0)
    discount_type: DiscountType
    discount_value_minor: int | None = None
    discount_percent: Decimal | None = None

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> VendorDiscountRuleCreate:
        self.vendor_pattern = normalize_vendor_text(self.vendor_pattern)
        _validate_discount_shape(
            self.discount_type, self.discount_value_minor, self.discount_percent
        )
        return self


class VendorDiscountRuleUpdate(BaseModel):
    """
    Partial update of a vendor discount rule. Any field omitted (None) is
    left unchanged, EXCEPT `active` which always has an explicit boolean
    default matching "no change" semantics via the endpoint (see
    app/api/vendor_discount_rules.py) -- soft-delete (active=false) has its
    own dedicated DELETE-verb endpoint, so `active` is not normally touched
    here, but is accepted for symmetry/reactivation.

    If either discount_type/value/percent field is supplied, ALL THREE of
    (discount_type, discount_value_minor, discount_percent) must be
    supplied together so the shape can be validated as a whole -- partial
    discount-shape edits are rejected rather than guessed.
    """

    vendor_pattern: str | None = Field(default=None, min_length=1)
    min_order_total_minor: int | None = Field(default=None, ge=0)
    discount_type: DiscountType | None = None
    discount_value_minor: int | None = None
    discount_percent: Decimal | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> VendorDiscountRuleUpdate:
        if self.vendor_pattern is not None:
            self.vendor_pattern = normalize_vendor_text(self.vendor_pattern)
        discount_fields_set = (
            self.discount_type is not None
            or self.discount_value_minor is not None
            or self.discount_percent is not None
        )
        if discount_fields_set:
            if self.discount_type is None:
                raise ValueError(
                    "discount_type must be supplied when changing "
                    "discount_value_minor/discount_percent"
                )
            _validate_discount_shape(
                self.discount_type, self.discount_value_minor, self.discount_percent
            )
        return self


class VendorDiscountRuleResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID | None
    created_by: uuid.UUID
    vendor_pattern: str
    min_order_total_minor: int
    discount_type: DiscountType
    discount_value_minor: int | None
    discount_percent: Decimal | None
    active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VendorDiscountRulesListResponse(BaseModel):
    rules: list[VendorDiscountRuleResponse]


# ---------------------------------------------------------------------------
# Grouped group-expenses list (M6-M8 item 7a)
# ---------------------------------------------------------------------------


class ExpenseMemberShare(BaseModel):
    """
    One member's PERSISTED (never recomputed) share of an expense --
    expense_member_allocations.total_minor for an item-5-confirmed expense,
    or the sum of frozen item_assignments.share_minor for that user
    otherwise. Members with no frozen share yet (a draft item-level expense
    before confirmation) simply do not appear here -- see
    GET /groups/{group_id}/expenses.
    """

    user_id: uuid.UUID
    share_minor: int


class GroupExpenseSummary(BaseModel):
    id: uuid.UUID
    vendor: str | None
    invoice_date: date | None
    total_minor: int
    paid_by: uuid.UUID
    parse_status: ParseStatus
    member_shares: list[ExpenseMemberShare] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class GroupExpensesBucket(BaseModel):
    """
    One date bucket. `date=None` is the deterministic "undated" bucket for
    expenses with invoice_date IS NULL -- see
    GET /groups/{group_id}/expenses's docstring for why NULL invoice_date is
    never silently folded into a `created_at`-derived date instead.
    """

    date: date | None
    expenses: list[GroupExpenseSummary]


class GroupExpensesGroupedResponse(BaseModel):
    group_id: uuid.UUID
    buckets: list[GroupExpensesBucket]


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
