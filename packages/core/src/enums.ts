/**
 * Enums mirrored 1:1 from backend/app/domain/models.py (StrEnum members).
 * Keep in sync with the backend — these are the wire values, not display
 * labels (see labels.ts for human-readable strings).
 */

export const GroupMemberRole = {
  admin: "admin",
  member: "member",
} as const;
export type GroupMemberRole = (typeof GroupMemberRole)[keyof typeof GroupMemberRole];

export const LineItemKind = {
  item: "item",
  tax: "tax",
  delivery_fee: "delivery_fee",
  platform_fee: "platform_fee",
  packing_fee: "packing_fee",
  tip: "tip",
  discount: "discount",
  refund: "refund",
} as const;
export type LineItemKind = (typeof LineItemKind)[keyof typeof LineItemKind];

export const DiscountScope = {
  item: "item",
  cart: "cart",
} as const;
export type DiscountScope = (typeof DiscountScope)[keyof typeof DiscountScope];

export const AllocationMethod = {
  equal: "equal",
  proportional: "proportional",
  manual: "manual",
} as const;
export type AllocationMethod = (typeof AllocationMethod)[keyof typeof AllocationMethod];

export const ExpenseSource = {
  pdf: "pdf",
  manual: "manual",
} as const;
export type ExpenseSource = (typeof ExpenseSource)[keyof typeof ExpenseSource];

export const ParseStatus = {
  queued: "queued",
  parsed: "parsed",
  needs_review: "needs_review",
  confirmed: "confirmed",
  failed: "failed",
} as const;
export type ParseStatus = (typeof ParseStatus)[keyof typeof ParseStatus];

export const ExpenseStatus = {
  active: "active",
  voided: "voided",
} as const;
export type ExpenseStatus = (typeof ExpenseStatus)[keyof typeof ExpenseStatus];

export const LedgerEntryType = {
  expense_share: "expense_share",
  refund_reversal: "refund_reversal",
  settlement: "settlement",
  adjustment: "adjustment",
} as const;
export type LedgerEntryType = (typeof LedgerEntryType)[keyof typeof LedgerEntryType];

export const SettlementMethod = {
  upi: "upi",
  cash: "cash",
  bank: "bank",
  other: "other",
} as const;
export type SettlementMethod = (typeof SettlementMethod)[keyof typeof SettlementMethod];

export const DiscountType = {
  flat: "flat",
  percent: "percent",
} as const;
export type DiscountType = (typeof DiscountType)[keyof typeof DiscountType];

export const DiscountSource = {
  manual: "manual",
  vendor_rule: "vendor_rule",
  extracted: "extracted",
} as const;
export type DiscountSource = (typeof DiscountSource)[keyof typeof DiscountSource];

/** Stable validation issue codes, API_CONTRACT.md §4. */
export const ValidationIssueCode = {
  no_line_items: "no_line_items",
  bad_quantity: "bad_quantity",
  negative_unit_price: "negative_unit_price",
  sign_convention: "sign_convention",
  line_arithmetic: "line_arithmetic",
  invoice_total_mismatch: "invoice_total_mismatch",
  currency_unrecognized: "currency_unrecognized",
  bad_date: "bad_date",
} as const;
export type ValidationIssueCode =
  (typeof ValidationIssueCode)[keyof typeof ValidationIssueCode];
