import type { LineItemResponse } from "@splitr/core";
import { Avatar } from "@/components/Avatar";
import { Money } from "@/components/Money";
import type { RememberedMember } from "@/lib/local-store";

export function LineItemCard({
  line,
  members,
  assignedUserIds,
  onToggle,
  currency,
}: {
  line: LineItemResponse;
  members: RememberedMember[];
  assignedUserIds: Set<string>;
  onToggle: (userId: string) => void;
  currency: string;
}) {
  const unassigned = assignedUserIds.size === 0;

  return (
    <div
      className={`rounded-xl border p-3 transition ${
        unassigned ? "border-amber-300 bg-amber-50/40" : "border-gray-200"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="font-medium leading-tight">
            {line.description || "Item"}
          </p>
          <p className="text-xs text-gray-400">
            Qty {line.quantity}
            {line.unit_price_minor != null && (
              <>
                {" · "}
                <Money minor={line.unit_price_minor} currency={currency} /> each
              </>
            )}
          </p>
        </div>
        <Money
          minor={line.total_minor}
          currency={currency}
          className="font-semibold"
        />
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {members.map((m) => (
          <button
            type="button"
            key={m.id}
            onClick={() => onToggle(m.id)}
            className="flex flex-col items-center gap-1 text-[10px]"
            aria-pressed={assignedUserIds.has(m.id)}
          >
            <Avatar
              name={m.name}
              selected={assignedUserIds.has(m.id)}
              size="sm"
            />
            <span className="max-w-[3rem] truncate">{m.name}</span>
          </button>
        ))}
        {members.length === 0 && (
          <p className="text-xs text-gray-400">
            No group members cached in this browser.
          </p>
        )}
      </div>
    </div>
  );
}
