"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, type ExpenseResponse } from "@splitr/core";
import { api, formatApiError } from "@/lib/api";

const TERMINAL_STATUSES = new Set(["parsed", "needs_review", "confirmed", "failed"]);

/**
 * Polls GET /expenses/{id} until parse_status leaves the async "queued"
 * state (or an in-flight request fails outright). Backs off gradually
 * since extraction typically takes 2-20s (ARCHITECTURE.md §2.1) — no need
 * to hammer the API every 500ms for that whole window.
 */
export function useExpensePolling(expenseId: string | null, initial?: ExpenseResponse) {
  const [expense, setExpense] = useState<ExpenseResponse | null>(initial ?? null);
  const [error, setError] = useState<string | null>(null);
  const attempt = useRef(0);

  useEffect(() => {
    if (!expenseId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      try {
        const result = await api.getExpense(expenseId as string);
        if (cancelled) return;
        setExpense(result);
        setError(null);
        if (TERMINAL_STATUSES.has(result.parse_status)) return;
      } catch (err) {
        if (cancelled) return;
        setError(formatApiError(err, "Failed to fetch expense status"));
        // 403 (not authorized for this expense) and 401 (session gone, and
        // the api client's own silent-refresh-and-retry already failed) are
        // permanent for this poll loop -- retrying on a timer won't help.
        if (err instanceof ApiError && (err.status === 403 || err.status === 401)) {
          return;
        }
      }
      attempt.current += 1;
      const delayMs = Math.min(1000 * 1.5 ** attempt.current, 8000);
      timer = setTimeout(poll, delayMs);
    }

    void poll();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [expenseId]);

  return { expense, error, setExpense };
}
