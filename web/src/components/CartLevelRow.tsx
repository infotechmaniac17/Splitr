import { lineItemKindLabels, type LineItemResponse } from "@splitr/core";
import { Money } from "@/components/Money";

const ALLOCATION_LABELS: Record<string, string> = {
  equal: "Split equally",
  proportional: "Split proportionally",
  manual: "Manual split",
};

/**
 * Read-only row for cart-level lines (fees/tax/discount/tip). These are not
 * individually tap-assigned — the splitting engine spreads them across
 * whoever is assigned to item lines, per `allocation` (ARCHITECTURE.md §4).
 */
export function CartLevelRow({
  line,
  currency,
}: {
  line: LineItemResponse;
  currency: string;
}) {
  const allocation = line.allocation ?? "proportional";
  return (
    <div className="flex items-center justify-between rounded-lg bg-gray-50 px-3 py-2 text-sm">
      <div>
        <p className="font-medium text-gray-700">
          {line.description || lineItemKindLabels[line.kind]}
        </p>
        <p className="text-xs text-gray-400">
          {lineItemKindLabels[line.kind]} ·{" "}
          {ALLOCATION_LABELS[allocation] ?? allocation}
        </p>
      </div>
      <Money
        minor={line.total_minor}
        currency={currency}
        className={
          line.total_minor < 0 ? "font-semibold text-red-600" : "font-semibold"
        }
      />
    </div>
  );
}
