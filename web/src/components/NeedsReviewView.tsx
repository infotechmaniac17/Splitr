"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  AllocationMethod,
  DiscountScope,
  LineItemKind,
  validationIssueLabels,
  type ExpenseResponse,
  type LineItemCreate,
  type RawExtraction,
  type ValidationIssue,
} from "@splitr/core";
import { Money } from "@/components/Money";
import { api, formatApiError } from "@/lib/api";

function lineItemsFromExpense(expense: ExpenseResponse): LineItemCreate[] {
  return expense.line_items.map((li) => ({
    line_no: li.line_no,
    kind: li.kind,
    description: li.description ?? "",
    quantity: li.quantity,
    unit_price_minor: li.unit_price_minor,
    total_minor: li.total_minor,
    allocation: li.allocation ?? undefined,
    discount_scope: li.discount_scope ?? undefined,
    parent_line_no: undefined,
  }));
}

export function NeedsReviewView({
  expense,
  onCorrected,
}: {
  expense: ExpenseResponse;
  onCorrected: (expense: ExpenseResponse) => void;
}) {
  const [rows, setRows] = useState<LineItemCreate[]>(() =>
    lineItemsFromExpense(expense),
  );
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [rawError, setRawError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getRawExtraction(expense.id)
      .then((raw: RawExtraction) => {
        const last = raw.attempts[raw.attempts.length - 1];
        setIssues(last?.validation?.issues ?? []);
      })
      .catch(() =>
        setRawError("Could not load validation details for this expense."),
      );
  }, [expense.id]);

  const issuesByLine = useMemo(() => {
    const map = new Map<number | null, ValidationIssue[]>();
    for (const issue of issues) {
      const key = issue.line_no;
      map.set(key, [...(map.get(key) ?? []), issue]);
    }
    return map;
  }, [issues]);

  const invoiceLevelIssues = issuesByLine.get(null) ?? [];

  const linesSum = rows.reduce((sum, r) => sum + r.total_minor, 0);
  const reconciles = linesSum === expense.total_minor;

  function updateRow(idx: number, patch: Partial<LineItemCreate>) {
    setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  }

  function addRow() {
    setRows((prev) => [
      ...prev,
      {
        line_no: (prev.at(-1)?.line_no ?? 0) + 1,
        kind: LineItemKind.item,
        description: "",
        quantity: "1",
        unit_price_minor: null,
        total_minor: 0,
      },
    ]);
  }

  function removeRow(idx: number) {
    setRows((prev) => prev.filter((_, i) => i !== idx));
  }

  async function handleSubmit() {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const corrected = await api.submitLineItemCorrection(expense.id, {
        line_items: rows,
      });
      onCorrected(corrected);
    } catch (err) {
      setSubmitError(formatApiError(err, "Could not save corrections"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-bold">Needs review</h1>
        <p className="text-sm text-gray-500">
          {expense.vendor ?? "This invoice"} didn&apos;t pass automatic
          validation. Fix the highlighted fields below, then resubmit.
        </p>
      </div>

      {invoiceLevelIssues.length > 0 && (
        <div className="rounded-lg bg-red-50 p-3 text-sm text-red-700">
          <ul className="list-disc pl-4">
            {invoiceLevelIssues.map((issue, i) => (
              <li key={i}>
                {issue.message || validationIssueLabels[issue.code]}
              </li>
            ))}
          </ul>
        </div>
      )}
      {rawError && <p className="text-xs text-gray-400">{rawError}</p>}

      <div className="grid gap-4 md:grid-cols-2">
        <div className="order-2 md:order-1">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">
            Original PDF
          </p>
          <iframe
            title="Invoice PDF"
            src={api.pdfUrl(expense.id)}
            className="h-64 w-full rounded-lg border border-gray-200 md:h-[32rem]"
          />
        </div>

        <div className="order-1 flex flex-col gap-3 md:order-2">
          <p className="text-xs font-semibold uppercase tracking-wide text-gray-400">
            Line items
          </p>
          {rows.map((row, idx) => {
            const lineIssues = issuesByLine.get(row.line_no ?? null) ?? [];
            const flagged = lineIssues.length > 0;
            return (
              <div
                key={idx}
                className={`flex flex-col gap-2 rounded-lg border p-3 text-sm ${
                  flagged ? "border-red-400 bg-red-50" : "border-gray-200"
                }`}
              >
                {flagged && (
                  <p className="text-xs font-medium text-red-700">
                    {lineIssues.map((i) => i.message).join("; ")}
                  </p>
                )}
                <div className="grid grid-cols-2 gap-2">
                  <select
                    value={row.kind}
                    onChange={(e) =>
                      updateRow(idx, {
                        kind: e.target.value as LineItemCreate["kind"],
                      })
                    }
                    className="rounded border border-gray-300 px-2 py-1"
                  >
                    {Object.values(LineItemKind).map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                  <input
                    value={row.description ?? ""}
                    onChange={(e) =>
                      updateRow(idx, { description: e.target.value })
                    }
                    placeholder="Description"
                    className="rounded border border-gray-300 px-2 py-1"
                  />
                  <input
                    value={row.quantity}
                    onChange={(e) =>
                      updateRow(idx, { quantity: e.target.value })
                    }
                    placeholder="Qty"
                    className="rounded border border-gray-300 px-2 py-1"
                  />
                  <input
                    type="number"
                    value={row.unit_price_minor ?? ""}
                    onChange={(e) =>
                      updateRow(idx, {
                        unit_price_minor:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    placeholder="Unit price (minor)"
                    className="rounded border border-gray-300 px-2 py-1"
                  />
                  <input
                    type="number"
                    value={row.total_minor}
                    onChange={(e) =>
                      updateRow(idx, { total_minor: Number(e.target.value) })
                    }
                    placeholder="Total (minor)"
                    className="col-span-2 rounded border border-gray-300 px-2 py-1"
                  />
                  {row.kind === LineItemKind.discount && (
                    <select
                      value={row.discount_scope ?? ""}
                      onChange={(e) =>
                        updateRow(idx, {
                          discount_scope: (e.target.value || undefined) as
                            DiscountScope | undefined,
                        })
                      }
                      className="col-span-2 rounded border border-gray-300 px-2 py-1"
                    >
                      <option value="">Discount scope…</option>
                      <option value={DiscountScope.item}>Item</option>
                      <option value={DiscountScope.cart}>Cart</option>
                    </select>
                  )}
                  {row.kind !== LineItemKind.item &&
                    row.kind !== LineItemKind.discount && (
                      <select
                        value={row.allocation ?? ""}
                        onChange={(e) =>
                          updateRow(idx, {
                            allocation: (e.target.value || undefined) as
                              AllocationMethod | undefined,
                          })
                        }
                        className="col-span-2 rounded border border-gray-300 px-2 py-1"
                      >
                        <option value="">Allocation…</option>
                        <option value={AllocationMethod.equal}>Equal</option>
                        <option value={AllocationMethod.proportional}>
                          Proportional
                        </option>
                        <option value={AllocationMethod.manual}>Manual</option>
                      </select>
                    )}
                </div>
                <button
                  type="button"
                  onClick={() => removeRow(idx)}
                  className="self-end text-xs text-red-600"
                >
                  Remove row
                </button>
              </div>
            );
          })}

          <button
            type="button"
            onClick={addRow}
            className="rounded-lg border-2 border-dashed border-gray-300 py-2 text-sm text-gray-600"
          >
            + Add line
          </button>

          <div
            className={`flex items-center justify-between rounded-lg px-3 py-2 text-sm font-medium ${
              reconciles
                ? "bg-emerald-100 text-emerald-800"
                : "bg-amber-100 text-amber-800"
            }`}
          >
            <span>Lines sum</span>
            <span>
              <Money minor={linesSum} currency={expense.currency} /> /{" "}
              <Money minor={expense.total_minor} currency={expense.currency} />
            </span>
          </div>

          {submitError && <p className="text-sm text-red-600">{submitError}</p>}

          <button
            type="button"
            onClick={handleSubmit}
            disabled={submitting}
            className="rounded-xl bg-brand-600 px-4 py-3 font-semibold text-white disabled:opacity-50"
          >
            {submitting ? "Validating…" : "Resubmit for validation"}
          </button>

          <Link
            href={`/expenses/manual?groupId=${expense.group_id ?? ""}`}
            className="text-center text-xs font-medium text-gray-500 underline"
          >
            Or start over with quick manual entry
          </Link>
        </div>
      </div>
    </div>
  );
}
