/** Human-readable labels for enum wire values (used by both web + mobile). */

import {
  DiscountSource,
  LineItemKind,
  ParseStatus,
  ValidationIssueCode,
} from "./enums";

export const lineItemKindLabels: Record<LineItemKind, string> = {
  item: "Item",
  tax: "Tax",
  delivery_fee: "Delivery fee",
  platform_fee: "Platform fee",
  packing_fee: "Packing fee",
  tip: "Tip",
  discount: "Discount",
  refund: "Refund",
};

export const parseStatusLabels: Record<ParseStatus, string> = {
  queued: "Processing…",
  parsed: "Ready to assign",
  needs_review: "Needs review",
  confirmed: "Confirmed",
  failed: "Could not parse",
};

export const validationIssueLabels: Record<ValidationIssueCode, string> = {
  no_line_items: "No line items found",
  bad_quantity: "Invalid quantity",
  negative_unit_price: "Negative unit price",
  sign_convention: "Incorrect sign (discount/refund must be negative)",
  line_arithmetic: "Quantity × unit price doesn't match line total",
  invoice_total_mismatch: "Line items don't add up to the invoice total",
  currency_unrecognized: "Unrecognized currency",
  bad_date: "Invalid date",
};

export const discountSourceLabels: Record<DiscountSource, string> = {
  manual: "Manual",
  vendor_rule: "Vendor rule",
  extracted: "Extracted from invoice",
};

/** Line-item kinds that represent cart-level rows (fees/discounts/tax) vs items. */
export const CART_LEVEL_KINDS: readonly LineItemKind[] = [
  "tax",
  "delivery_fee",
  "platform_fee",
  "packing_fee",
  "tip",
  "discount",
];
