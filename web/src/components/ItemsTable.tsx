"use client";

import {
  GstMode,
  lineItemKindLabels,
  type LineItemResponse,
} from "@splitr/core";
import { Avatar } from "@/components/Avatar";
import { Money } from "@/components/Money";
import type { RememberedMember } from "@/lib/local-store";

/**
 * Item-level line table for the invoice review & assignment screen.
 * Column order mirrors the PDF: [checkbox] | item | qty | unit price |
 * amount | [gst rate] | assign to.
 *
 * The GST-rate column only renders when `gstMode === 'item_level'`
 * (backend/app/domain/models.py's GstMode) -- every other line's
 * gst_rate/gst_amount_minor is null by construction (see
 * LineItemResponse's doc comment in packages/core/src/schemas.ts), so
 * showing the column for other modes would just be a column of dashes.
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
  gstMode,
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
  gstMode: string;
}) {
  const showGstColumn = gstMode === GstMode.item_level;
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
            {showGstColumn && <th className="px-2 py-2 text-right">GST</th>}
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
                  <p
                    className={`font-medium leading-tight ${isRefund ? "text-red-700" : ""}`}
                  >
                    {line.description || lineItemKindLabels[line.kind]}
                  </p>
                  {isRefund && (
                    <span className="text-[10px] font-semibold uppercase tracking-wide text-red-500">
                      Refund
                    </span>
                  )}
                  {unassigned && !isRefund && (
                    <p className="text-[10px] font-medium text-amber-600">
                      Unassigned
                    </p>
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
                {showGstColumn && (
                  <td className="whitespace-nowrap px-2 py-2 text-right text-gray-500">
                    {line.gst_rate != null ? (
                      <>
                        {line.gst_rate}%
                        {line.gst_amount_minor != null && (
                          <>
                            {" · "}
                            <Money
                              minor={line.gst_amount_minor}
                              currency={currency}
                            />
                          </>
                        )}
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                )}
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
                        <Avatar
                          name={m.name}
                          selected={selected.has(m.id)}
                          size="sm"
                        />
                      </button>
                    ))}
                    {saving && (
                      <span className="text-[10px] text-gray-400">Saving…</span>
                    )}
                  </div>
                  {error && (
                    <p className="mt-1 text-[10px] text-red-600">{error}</p>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
