"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { toMinorUnits, type AllocationPreviewResponse, type ExpenseResponse } from "@splitr/core";
import { Money } from "@/components/Money";
import { StatusBadge } from "@/components/StatusBadge";
import { SplitPreviewPanel } from "@/components/SplitPreviewPanel";
import { api, formatApiError } from "@/lib/api";
import type { RememberedMember } from "@/lib/local-store";

/**
 * Refunds panel gated per M6-M8 item 7a: the refund flow is not yet
 * discount/GST-aware (backend/app/api/expenses.py:create_refund returns 409
 * when any persisted expense_member_allocations row for this expense has a
 * nonzero discount_minor/gst_minor) -- we detect that from the SAME
 * allocation-preview response the split panel already fetched (persisted,
 * confirmed=true), rather than letting the user hit the 409 blind.
 */
function RefundPanel({
  expense,
  preview,
  onRefunded,
}: {
  expense: ExpenseResponse;
  preview: AllocationPreviewResponse | null;
  onRefunded: (expense: ExpenseResponse) => void;
}) {
  const itemLines = expense.line_items.filter((li) => li.kind === "item");
  const [parentLineId, setParentLineId] = useState(itemLines[0]?.id ?? "");
  const [amountInput, setAmountInput] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const blocked =
    preview != null &&
    preview.members.some((m) => m.discount_minor !== 0 || m.gst_minor !== 0);

  if (itemLines.length === 0) return null;

  if (blocked) {
    return (
      <div className="rounded-lg bg-gray-100 px-3 py-2 text-xs text-gray-500">
        Refunds unavailable on discounted/taxed expenses — this expense was confirmed with
        an applied discount and/or allocated GST, which the refund flow can&apos;t yet account
        for. Record a correcting settlement instead, or void and recreate the expense.
      </div>
    );
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const amountMinor = toMinorUnits(amountInput, expense.currency);
      const updated = await api.createRefund(expense.id, {
        parent_line_id: parentLineId,
        amount_minor: amountMinor,
      });
      onRefunded(updated);
      setAmountInput("");
    } catch (err) {
      setError(formatApiError(err, "Could not record refund"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2 rounded-lg border border-gray-200 p-3 text-sm">
      <p className="text-xs font-semibold uppercase tracking-wide text-gray-400">Record a refund</p>
      <select
        value={parentLineId}
        onChange={(e) => setParentLineId(e.target.value)}
        className="rounded border border-gray-300 px-2 py-1"
      >
        {itemLines.map((li) => (
          <option key={li.id} value={li.id}>
            {li.description || "Item"}
          </option>
        ))}
      </select>
      <input
        value={amountInput}
        onChange={(e) => setAmountInput(e.target.value)}
        placeholder="Refund amount"
        required
        className="rounded border border-gray-300 px-2 py-1"
      />
      {error && <p className="text-xs text-red-600">{error}</p>}
      <button
        type="submit"
        disabled={submitting}
        className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
      >
        {submitting ? "Recording…" : "Record refund"}
      </button>
    </form>
  );
}

export function ConfirmedSummary({
  expense: initialExpense,
  members = [],
}: {
  expense: ExpenseResponse;
  members?: RememberedMember[];
}) {
  const [expense, setExpense] = useState(initialExpense);
  const [preview, setPreview] = useState<AllocationPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [previewError, setPreviewError] = useState<string | null>(null);

  function refetchPreview() {
    setPreviewLoading(true);
    setPreviewError(null);
    api
      .getAllocationPreview(expense.id)
      .then(setPreview)
      .catch((err) => setPreviewError(formatApiError(err, "Could not load split")))
      .finally(() => setPreviewLoading(false));
  }

  useEffect(() => {
    refetchPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expense.id]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">{expense.vendor ?? "Expense"}</h1>
        <StatusBadge status={expense.parse_status} />
      </div>
      <div className="rounded-xl border border-gray-200 p-5 text-center">
        <p className="text-sm text-gray-500">Total</p>
        <p className="text-3xl font-bold">
          <Money minor={expense.total_minor} currency={expense.currency} />
        </p>
        {expense.confirmed_at && (
          <p className="mt-1 text-xs text-gray-400">
            Confirmed {new Date(expense.confirmed_at).toLocaleString()}
          </p>
        )}
      </div>

      <SplitPreviewPanel
        preview={preview}
        loading={previewLoading}
        error={previewError}
        members={members}
        currency={expense.currency}
      />

      <RefundPanel
        expense={expense}
        preview={preview}
        onRefunded={(updated) => {
          setExpense(updated);
          refetchPreview();
        }}
      />

      <p className="text-center text-sm text-gray-500">
        This expense has been posted to the ledger and can&apos;t be edited directly —
        corrections happen as new refund/adjustment entries.
      </p>
      {expense.group_id && (
        <Link
          href={`/groups/${expense.group_id}`}
          className="rounded-lg bg-gray-900 px-4 py-3 text-center text-sm font-semibold text-white"
        >
          Back to group
        </Link>
      )}
    </div>
  );
}
