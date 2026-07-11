"use client";

import { lineItemKindLabels, type LineItemResponse } from "@splitr/core";
import { Avatar } from "@/components/Avatar";
import { Money } from "@/components/Money";
import type { RememberedMember } from "@/lib/local-store";

/**
 * Item-level line table for the invoice review & assignment screen.
 * Column order mirrors the PDF: [checkbox] | item | qty | unit price |
 * amount | gst rate.
 *
 * GST-RATE COLUMN GAP: backend/app/api/schemas.py's LineItemResponse never
 * serializes `gst_rate`/`gst_amount_minor` (they exist as ORM columns --
 * see app/domain/models.py -- but are not on the Pydantic response model),
 * and ExpenseResponse never serializes `gst_mode` either, so there is no
 * way for this client to know whether an expense is gst_mode='item_level'
 * or to read a line's rate at all. The gst-rate column is therefore
 * omitted entirely rather than guessed at or computed client-side -- see
 * the final report for the exact backend fields that would need to be
 * added.
 */
export function ItemsTable({
  lines,
  members,
  rowSelections,
  onToggleMember,
  rowSaving,
  rowError,
  checkedRows,
  onToggleChecked,
  currency,
  rowRefs,
}: {
  lines: LineItemResponse[];
  members: RememberedMember[];
  rowSelections: Map<string, Set<string>>;
  onToggleMember: (lineId: string, userId: string) => void;
  rowSaving: Set<string>;
  rowError: Map<string, string>;
  checkedRows: Set<string>;
  onToggleChecked: (lineId: string) => void;
  currency: string;
  rowRefs?: React.MutableRefObject<Map<string, HTMLTableRowElement>>;
}) {
  if (lines.length === 0) {
    return (
      <p className="rounded-lg bg-gray-50 p-4 text-sm text-gray-400">
        No item lines on this expense yet.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-gray-200">
      <table className="w-full min-w-[560px] border-collapse text-sm">
        <thead>
          <tr className="border-b border-gray-200 bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
            <th className="w-8 px-2 py-2"></th>
            <th className="px-2 py-2">Item</th>
            <th className="px-2 py-2 text-right">Qty</th>
            <th className="px-2 py-2 text-right">Unit price</th>
            <th className="px-2 py-2 text-right">Amount</th>
            <th className="px-2 py-2">Assign to</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => {
            const isRefund = line.kind === "refund";
            const selected = rowSelections.get(line.id) ?? new Set<string>();
            const unassigned = selected.size === 0;
            const saving = rowSaving.has(line.id);
            const error = rowError.get(line.id);
            return (
              <tr
                key={line.id}
                ref={(el) => {
                  if (rowRefs && el) rowRefs.current.set(line.id, el);
                }}
                className={`border-b border-gray-100 align-top last:border-0 ${
                  isRefund ? "bg-red-50/40" : unassigned ? "bg-amber-50/40" : ""
                }`}
              >
                <td className="px-2 py-2">
                  <input
                    type="checkbox"
                    checked={checkedRows.has(line.id)}
                    onChange={() => onToggleChecked(line.id)}
                    aria-label={`Select ${line.description ?? "item"} for bulk assign`}
                  />
                </td>
                <td className="px-2 py-2">
                  <p className={`font-medium leading-tight ${isRefund ? "text-red-700" : ""}`}>
                    {line.description || lineItemKindLabels[line.kind]}
                  </p>
                  {isRefund && (
                    <span className="text-[10px] font-semibold uppercase tracking-wide text-red-500">
                      Refund
                    </span>
                  )}
                  {unassigned && !isRefund && (
                    <p className="text-[10px] font-medium text-amber-600">Unassigned</p>
                  )}
                </td>
                <td className="whitespace-nowrap px-2 py-2 text-right text-gray-500">
                  {line.quantity}
                </td>
                <td className="whitespace-nowrap px-2 py-2 text-right text-gray-500">
                  {line.unit_price_minor != null ? (
                    <Money minor={line.unit_price_minor} currency={currency} />
                  ) : (
                    "—"
                  )}
                </td>
                <td className="whitespace-nowrap px-2 py-2 text-right">
                  <Money
                    minor={line.total_minor}
                    currency={currency}
                    className={`font-semibold ${isRefund ? "text-red-600" : ""}`}
                  />
                </td>
                <td className="px-2 py-2">
                  <div className="flex flex-wrap items-center gap-2">
                    {members.map((m) => (
                      <button
                        type="button"
                        key={m.id}
                        onClick={() => onToggleMember(line.id, m.id)}
                        className="flex flex-col items-center gap-0.5 text-[10px]"
                        aria-pressed={selected.has(m.id)}
                      >
                        <Avatar name={m.name} selected={selected.has(m.id)} size="sm" />
                      </button>
                    ))}
                    {saving && <span className="text-[10px] text-gray-400">Saving…</span>}
                  </div>
                  {error && <p className="mt-1 text-[10px] text-red-600">{error}</p>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
