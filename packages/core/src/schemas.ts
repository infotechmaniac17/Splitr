/**
 * Zod schemas mirroring backend/app/api/schemas.py (Pydantic v2, the
 * OpenAPI-facing contract). Field names/types/nullability must match
 * exactly — see docs/API_CONTRACT.md.
 *
 * Money fields are always integers in minor units (paise) per
 * CLAUDE.md invariant #1. `quantity` is a string-serialized Decimal
 * (NUMERIC(10,3) on the wire, per API_CONTRACT.md §2) — never parse it
 * to a JS `number` for arithmetic; it's display-only in the assignment UI.
 */

import { z } from "zod";
import {
  AllocationMethod,
  DiscountScope,
  DiscountSource,
  DiscountType,
  ExpenseSource,
  ExpenseStatus,
  GroupMemberRole,
  LedgerEntryType,
  LineItemKind,
  ParseStatus,
  SettlementMethod,
  ValidationIssueCode,
} from "./enums";

const uuid = z.string().uuid();

// Backend Decimal fields (e.g. discount_percent) are display-only here — we
// never parse them to a JS number for arithmetic (CLAUDE.md invariant #1).
// Pydantic v2's JSON encoding of a bare Decimal field can surface as either
// a JSON string or a JSON number depending on the field; accept either shape
// on the wire and normalize to a string so every consumer treats it
// identically (display-only, e.g. "50" or "12.5").
const decimalDisplayString = z.preprocess(
  (v) => (v === null || v === undefined ? v : String(v)),
  z.string(),
);

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------

export const userCreateSchema = z.object({
  name: z.string(),
  email: z.string(),
  phone: z.string().nullable().optional(),
  avatar_url: z.string().nullable().optional(),
  default_currency: z.string().default("INR"),
});
// NOTE: request/payload schemas use z.input (pre-default-fill shape) so
// callers can omit fields that have a `.default(...)`, e.g. `currency`.
// Response schemas use z.infer (post-parse output shape) as usual.
export type UserCreate = z.input<typeof userCreateSchema>;

export const userResponseSchema = z.object({
  id: uuid,
  name: z.string(),
  email: z.string(),
  phone: z.string().nullable(),
  avatar_url: z.string().nullable(),
  default_currency: z.string(),
  created_at: z.string(),
});
export type UserResponse = z.infer<typeof userResponseSchema>;

// ---------------------------------------------------------------------------
// Auth — mirrors backend/app/api/schemas.py's Register/Login/Refresh/Token
// shapes exactly. Token lifetimes (see backend/app/domain/auth.py):
//   access token:  15 minutes  (ACCESS_TOKEN_EXPIRE_MINUTES)
//   refresh token: 30 days     (REFRESH_TOKEN_EXPIRE_DAYS)
// `expires_in` on both token responses is the ACCESS token's remaining
// lifetime in seconds (900), even on /auth/refresh's response, which never
// returns a new refresh token — the original refresh token is reused until
// it expires or the user logs in again (no rotation in this pass).
// ---------------------------------------------------------------------------

export const registerRequestSchema = z.object({
  name: z.string(),
  email: z.string(),
  password: z.string().min(8).max(128),
  phone: z.string().nullable().optional(),
  avatar_url: z.string().nullable().optional(),
  default_currency: z.string().default("INR"),
});
export type RegisterRequest = z.input<typeof registerRequestSchema>;

export const loginRequestSchema = z.object({
  email: z.string(),
  password: z.string(),
});
export type LoginRequest = z.input<typeof loginRequestSchema>;

export const refreshRequestSchema = z.object({
  refresh_token: z.string(),
});
export type RefreshRequest = z.input<typeof refreshRequestSchema>;

export const tokenResponseSchema = z.object({
  access_token: z.string(),
  refresh_token: z.string(),
  token_type: z.string(),
  expires_in: z.number().int(),
  user: userResponseSchema,
});
export type TokenResponse = z.infer<typeof tokenResponseSchema>;

export const accessTokenResponseSchema = z.object({
  access_token: z.string(),
  token_type: z.string(),
  expires_in: z.number().int(),
});
export type AccessTokenResponse = z.infer<typeof accessTokenResponseSchema>;

// ---------------------------------------------------------------------------
// Groups
// ---------------------------------------------------------------------------

export const groupCreateSchema = z.object({
  name: z.string(),
  created_by: uuid,
  simplify_debts: z.boolean().default(true),
});
export type GroupCreate = z.input<typeof groupCreateSchema>;

export const groupResponseSchema = z.object({
  id: uuid,
  name: z.string(),
  created_by: uuid,
  simplify_debts: z.boolean(),
  created_at: z.string(),
});
export type GroupResponse = z.infer<typeof groupResponseSchema>;

export const groupMemberAddSchema = z.object({
  user_id: uuid,
  role: z.nativeEnum(GroupMemberRole).default(GroupMemberRole.member),
});
export type GroupMemberAdd = z.input<typeof groupMemberAddSchema>;

export const groupMemberResponseSchema = z.object({
  group_id: uuid,
  user_id: uuid,
  role: z.nativeEnum(GroupMemberRole),
  joined_at: z.string(),
});
export type GroupMemberResponse = z.infer<typeof groupMemberResponseSchema>;

export const groupMemberInfoSchema = z.object({
  user_id: uuid,
  name: z.string(),
  avatar_url: z.string().nullable(),
  role: z.nativeEnum(GroupMemberRole),
  joined_at: z.string(),
});
export type GroupMemberInfo = z.infer<typeof groupMemberInfoSchema>;

export const groupMembersResponseSchema = z.object({
  group_id: uuid,
  members: z.array(groupMemberInfoSchema),
});
export type GroupMembersResponse = z.infer<typeof groupMembersResponseSchema>;

// ---------------------------------------------------------------------------
// Line items
// ---------------------------------------------------------------------------

export const lineItemCreateSchema = z.object({
  line_no: z.number().int().default(1),
  kind: z.nativeEnum(LineItemKind).default(LineItemKind.item),
  description: z.string().nullable().optional(),
  quantity: z.string().default("1"), // Decimal on the wire; keep as string
  unit_price_minor: z.number().int().nullable().optional(),
  total_minor: z.number().int(),
  allocation: z.nativeEnum(AllocationMethod).nullable().optional(),
  discount_scope: z.nativeEnum(DiscountScope).nullable().optional(),
  parent_line_no: z.number().int().nullable().optional(),
});
export type LineItemCreate = z.input<typeof lineItemCreateSchema>;

export const lineItemResponseSchema = z.object({
  id: uuid,
  expense_id: uuid,
  line_no: z.number().int(),
  kind: z.nativeEnum(LineItemKind),
  description: z.string().nullable(),
  quantity: z.string(),
  unit_price_minor: z.number().int().nullable(),
  total_minor: z.number().int(),
  allocation: z.nativeEnum(AllocationMethod).nullable(),
  discount_scope: z.nativeEnum(DiscountScope).nullable().optional(),
  parent_line_id: uuid.nullable().optional(),
  bundle_group_id: uuid.nullable().optional(),
});
export type LineItemResponse = z.infer<typeof lineItemResponseSchema>;

// ---------------------------------------------------------------------------
// Item assignments
// ---------------------------------------------------------------------------

export const assignmentInSchema = z.object({
  line_item_id: uuid,
  user_id: uuid,
  weight: z.string().default("1"), // Decimal on the wire
});
export type AssignmentIn = z.input<typeof assignmentInSchema>;

export const assignmentsPutSchema = z.object({
  assignments: z.array(assignmentInSchema).min(1),
});
export type AssignmentsPut = z.input<typeof assignmentsPutSchema>;

export const assignmentResponseSchema = z.object({
  id: uuid,
  line_item_id: uuid,
  user_id: uuid,
  weight: z.string(),
  share_minor: z.number().int().nullable(),
});
export type AssignmentResponse = z.infer<typeof assignmentResponseSchema>;

export const sharesResponseSchema = z.object({
  expense_id: uuid,
  shares: z.record(uuid, z.number().int()),
});
export type SharesResponse = z.infer<typeof sharesResponseSchema>;

// ---------------------------------------------------------------------------
// M6-M8 item 7a: bulk assignment convenience endpoint
// ---------------------------------------------------------------------------

export const bulkAssignmentInSchema = z.object({
  item_ids: z.array(uuid).min(1),
  member_ids: z.array(uuid).min(1),
});
export type BulkAssignmentIn = z.input<typeof bulkAssignmentInSchema>;

// ---------------------------------------------------------------------------
// M6 item 5: discount + GST allocation preview
// ---------------------------------------------------------------------------

export const memberBreakdownResponseSchema = z.object({
  user_id: uuid,
  base_minor: z.number().int(),
  discount_minor: z.number().int(),
  gst_minor: z.number().int(),
  total_minor: z.number().int(),
});
export type MemberBreakdownResponse = z.infer<typeof memberBreakdownResponseSchema>;

export const allocationProblemSchema = z.object({
  code: z.string(),
  message: z.string(),
});
export type AllocationProblem = z.infer<typeof allocationProblemSchema>;

export const allocationPreviewResponseSchema = z.object({
  expense_id: uuid,
  confirmed: z.boolean(),
  // Always present in the response (Pydantic Field(default_factory=list)/
  // default=False fields are still always serialized, never omitted) --
  // deliberately NOT `.default(...)` here: combined with this client's
  // `z.ZodType<T>`-typed generic `request()` helper, a top-level
  // `.default()` on a response field makes TypeScript infer T from the
  // schema's (optional) INPUT side rather than its OUTPUT side, silently
  // making every consumer treat an always-present array/boolean as
  // possibly-undefined.
  members: z.array(memberBreakdownResponseSchema),
  subtotal_minor: z.number().int().nullable().optional(),
  applied_discount_minor: z.number().int().nullable().optional(),
  exclusive_gst_minor: z.number().int().nullable().optional(),
  discount_recorded_but_inert: z.boolean(),
  problems: z.array(allocationProblemSchema),
});
export type AllocationPreviewResponse = z.infer<typeof allocationPreviewResponseSchema>;

// M6-M8 item 7a: PATCH /expenses/{id}/discount payload. `discount_type:
// null/undefined` means CLEAR the manual snapshot and re-run vendor-rule
// auto-matching (see backend/app/api/expenses.py:patch_expense_discount).
export const expenseDiscountPatchSchema = z.object({
  discount_type: z.nativeEnum(DiscountType).nullable().optional(),
  discount_value_minor: z.number().int().nullable().optional(),
  discount_percent: z.union([z.string(), z.number()]).nullable().optional(),
  discount_threshold_minor: z.number().int().default(0),
});
export type ExpenseDiscountPatch = z.input<typeof expenseDiscountPatchSchema>;

export const refundCreateSchema = z.object({
  parent_line_id: uuid,
  amount_minor: z.number().int().positive(),
  description: z.string().nullable().optional(),
  idempotency_key: z.string().max(255).nullable().optional(),
});
export type RefundCreate = z.input<typeof refundCreateSchema>;

// ---------------------------------------------------------------------------
// Expenses
// ---------------------------------------------------------------------------

export const expenseCreateSchema = z.object({
  group_id: uuid.nullable().optional(),
  paid_by: uuid,
  vendor: z.string().nullable().optional(),
  invoice_date: z.string().nullable().optional(), // YYYY-MM-DD
  invoice_number: z.string().nullable().optional(),
  currency: z.string().default("INR"),
  total_minor: z.number().int().positive(),
  line_items: z.array(lineItemCreateSchema).default([]),
  participants: z.array(uuid).nullable().optional(),
  shares: z.record(uuid, z.number().int()).nullable().optional(),
});
export type ExpenseCreate = z.input<typeof expenseCreateSchema>;

export const expenseResponseSchema = z.object({
  id: uuid,
  group_id: uuid.nullable(),
  paid_by: uuid,
  vendor: z.string().nullable(),
  invoice_date: z.string().nullable(),
  invoice_number: z.string().nullable(),
  currency: z.string(),
  subtotal_minor: z.number().int().nullable(),
  total_minor: z.number().int(),
  source: z.nativeEnum(ExpenseSource),
  parse_status: z.nativeEnum(ParseStatus),
  status: z.nativeEnum(ExpenseStatus),
  created_at: z.string(),
  confirmed_at: z.string().nullable(),
  line_items: z.array(lineItemResponseSchema),
  // Not in the default ExpenseResponse per API_CONTRACT.md §2, but the
  // upload endpoint / needs-review flows need a way to reference the PDF.
  // Optional so parsing the base contract shape never fails if absent.
  pdf_object_key: z.string().nullable().optional(),
  // M6-M8 item 7a: discount snapshot (see backend/app/api/schemas.py's
  // ExpenseResponse doc comment — this DOES feed into each member's actual
  // owed amount at confirmation, it is not purely informational).
  discount_type: z.nativeEnum(DiscountType).nullable().optional(),
  discount_value_minor: z.number().int().nullable().optional(),
  discount_percent: decimalDisplayString.nullable().optional(),
  discount_threshold_minor: z.number().int().nullable().optional(),
  discount_source: z.nativeEnum(DiscountSource).nullable().optional(),
  discount_rule_id: uuid.nullable().optional(),
  // Set when GST-specific arithmetic invariants fail to reconcile;
  // independent of parse_status. Confirmation is blocked while true.
  // Always present (see the response-schema `.default()` note on
  // allocationPreviewResponseSchema above for why this isn't `.default()`).
  needs_review: z.boolean(),
});
export type ExpenseResponse = z.infer<typeof expenseResponseSchema>;

// ---------------------------------------------------------------------------
// raw_extraction (audit / needs_review correction UI) — API_CONTRACT.md §3-4
// ---------------------------------------------------------------------------

export const validationIssueSchema = z.object({
  code: z.nativeEnum(ValidationIssueCode),
  message: z.string(),
  line_no: z.number().int().nullable(),
});
export type ValidationIssue = z.infer<typeof validationIssueSchema>;

export const extractionAttemptSchema = z.object({
  attempt: z.number().int(),
  provider: z.string(),
  route: z.enum(["text", "vision"]).optional(),
  raw: z.unknown().optional(),
  validation: z
    .object({ ok: z.boolean(), issues: z.array(validationIssueSchema) })
    .optional(),
  error: z.string().optional(),
});
export type ExtractionAttempt = z.infer<typeof extractionAttemptSchema>;

export const rawExtractionSchema = z.object({
  attempts: z.array(extractionAttemptSchema),
  final_error: z.string().optional(),
});
export type RawExtraction = z.infer<typeof rawExtractionSchema>;

// ---------------------------------------------------------------------------
// Balances
// ---------------------------------------------------------------------------

export const pairwiseBalanceSchema = z.object({
  debtor_id: uuid,
  creditor_id: uuid,
  net_amount_minor: z.number().int(),
});
export type PairwiseBalance = z.infer<typeof pairwiseBalanceSchema>;

export const groupBalancesResponseSchema = z.object({
  group_id: uuid,
  balances: z.array(pairwiseBalanceSchema),
});
export type GroupBalancesResponse = z.infer<typeof groupBalancesResponseSchema>;

export const userBalanceResponseSchema = z.object({
  user_id: uuid,
  net_balance_minor: z.number().int(),
});
export type UserBalanceResponse = z.infer<typeof userBalanceResponseSchema>;

// ---------------------------------------------------------------------------
// Ledger entries (read-only)
// ---------------------------------------------------------------------------

export const ledgerEntryResponseSchema = z.object({
  id: uuid,
  group_id: uuid.nullable(),
  expense_id: uuid.nullable(),
  settlement_id: uuid.nullable(),
  debtor_id: uuid,
  creditor_id: uuid,
  amount_minor: z.number().int(),
  entry_type: z.nativeEnum(LedgerEntryType),
  created_at: z.string(),
});
export type LedgerEntryResponse = z.infer<typeof ledgerEntryResponseSchema>;

// ---------------------------------------------------------------------------
// Settlements
// ---------------------------------------------------------------------------

export const settlementCreateSchema = z.object({
  group_id: uuid.nullable().optional(),
  payer_id: uuid,
  payee_id: uuid,
  amount_minor: z.number().int().positive(),
  method: z.nativeEnum(SettlementMethod).default(SettlementMethod.other),
  note: z.string().nullable().optional(),
});
export type SettlementCreate = z.input<typeof settlementCreateSchema>;

export const settlementResponseSchema = z.object({
  id: uuid,
  group_id: uuid.nullable(),
  payer_id: uuid,
  payee_id: uuid,
  amount_minor: z.number().int(),
  method: z.nativeEnum(SettlementMethod),
  note: z.string().nullable(),
  settled_at: z.string(),
});
export type SettlementResponse = z.infer<typeof settlementResponseSchema>;

// ---------------------------------------------------------------------------
// M4 ASSUMPTION — upload + needs-review correction endpoints.
//
// Not defined by the frozen v1 API_CONTRACT.md (upload is explicitly flagged
// "out of scope" for M3/the contract doc, and the line-item correction PUT
// is explicitly flagged as "not yet built" in API_CONTRACT.md §4). These
// shapes are the client-side contract this web app codes against; see the
// README note in web/ for how to wire the real backend routes once they
// land. Kept here (not invented ad hoc in web/) so mobile can reuse them.
// ---------------------------------------------------------------------------

export const expenseUploadResponseSchema = expenseResponseSchema;
export type ExpenseUploadResponse = z.infer<typeof expenseUploadResponseSchema>;

export const lineItemsCorrectionSchema = z.object({
  line_items: z.array(lineItemCreateSchema),
});
export type LineItemsCorrection = z.input<typeof lineItemsCorrectionSchema>;

// ---------------------------------------------------------------------------
// Vendor discount rules (M6 item 3 / M6-M8 item 7a UI)
// ---------------------------------------------------------------------------

export const vendorDiscountRuleCreateSchema = z.object({
  group_id: uuid.nullable().optional(),
  vendor_pattern: z.string().min(1),
  min_order_total_minor: z.number().int().nonnegative().default(0),
  discount_type: z.nativeEnum(DiscountType),
  discount_value_minor: z.number().int().nullable().optional(),
  discount_percent: z.union([z.string(), z.number()]).nullable().optional(),
});
export type VendorDiscountRuleCreate = z.input<typeof vendorDiscountRuleCreateSchema>;

export const vendorDiscountRuleUpdateSchema = z.object({
  vendor_pattern: z.string().min(1).nullable().optional(),
  min_order_total_minor: z.number().int().nonnegative().nullable().optional(),
  discount_type: z.nativeEnum(DiscountType).nullable().optional(),
  discount_value_minor: z.number().int().nullable().optional(),
  discount_percent: z.union([z.string(), z.number()]).nullable().optional(),
  active: z.boolean().nullable().optional(),
});
export type VendorDiscountRuleUpdate = z.input<typeof vendorDiscountRuleUpdateSchema>;

export const vendorDiscountRuleResponseSchema = z.object({
  id: uuid,
  group_id: uuid.nullable(),
  created_by: uuid,
  vendor_pattern: z.string(),
  min_order_total_minor: z.number().int(),
  discount_type: z.nativeEnum(DiscountType),
  discount_value_minor: z.number().int().nullable(),
  discount_percent: decimalDisplayString.nullable(),
  active: z.boolean(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type VendorDiscountRuleResponse = z.infer<typeof vendorDiscountRuleResponseSchema>;

export const vendorDiscountRulesListResponseSchema = z.object({
  rules: z.array(vendorDiscountRuleResponseSchema),
});
export type VendorDiscountRulesListResponse = z.infer<
  typeof vendorDiscountRulesListResponseSchema
>;

// ---------------------------------------------------------------------------
// Grouped group-expenses list (M6-M8 item 7a)
// ---------------------------------------------------------------------------

export const expenseMemberShareSchema = z.object({
  user_id: uuid,
  share_minor: z.number().int(),
});
export type ExpenseMemberShare = z.infer<typeof expenseMemberShareSchema>;

export const groupExpenseSummarySchema = z.object({
  id: uuid,
  vendor: z.string().nullable(),
  invoice_date: z.string().nullable(),
  total_minor: z.number().int(),
  paid_by: uuid,
  parse_status: z.nativeEnum(ParseStatus),
  member_shares: z.array(expenseMemberShareSchema),
});
export type GroupExpenseSummary = z.infer<typeof groupExpenseSummarySchema>;

export const groupExpensesBucketSchema = z.object({
  date: z.string().nullable(),
  expenses: z.array(groupExpenseSummarySchema),
});
export type GroupExpensesBucket = z.infer<typeof groupExpensesBucketSchema>;

export const groupExpensesGroupedResponseSchema = z.object({
  group_id: uuid,
  buckets: z.array(groupExpensesBucketSchema),
});
export type GroupExpensesGroupedResponse = z.infer<
  typeof groupExpensesGroupedResponseSchema
>;
