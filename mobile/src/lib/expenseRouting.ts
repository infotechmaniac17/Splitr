import type { ExpenseResponse } from "@splitr/core";

/**
 * Central place for "what screen does this expense's parse_status route
 * to" — matches the web app's priority order (task brief: dashboard ->
 * upload -> assignment -> needs-review fallback) and the state machine in
 * API_CONTRACT.md §1:
 *
 *   queued        -> still polling on the upload screen (caller should keep
 *                     polling rather than navigate away)
 *   parsed        -> assignment screen (assign line items to people)
 *   needs_review  -> correction screen (fallback when validation failed)
 *   confirmed     -> expense detail (read-only summary)
 *   failed        -> expense detail, which offers Quick Manual Entry
 *                     (ARCHITECTURE.md §3 edge case table)
 */
export function nextRouteForExpense(expense: ExpenseResponse): string {
  switch (expense.parse_status) {
    case "needs_review":
      return `/expense/${expense.id}/review`;
    case "parsed":
      return `/expense/${expense.id}/assign`;
    case "queued":
    case "confirmed":
    case "failed":
    default:
      return `/expense/${expense.id}`;
  }
}
