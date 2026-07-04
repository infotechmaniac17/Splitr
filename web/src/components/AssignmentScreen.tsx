"use client";

import { useMemo, useState } from "react";
import type { AssignmentIn, ExpenseResponse } from "@splitr/core";
import { LineItemKind } from "@splitr/core";
import { CartLevelRow } from "@/components/CartLevelRow";
import { LineItemCard } from "@/components/LineItemCard";
import { UnassignedChip } from "@/components/UnassignedChip";
import { api, formatApiError } from "@/lib/api";
import type { RememberedMember } from "@/lib/local-store";

const CART_LEVEL: readonly string[] = [
  LineItemKind.tax,
  LineItemKind.delivery_fee,
  LineItemKind.platform_fee,
  LineItemKind.packing_fee,
  LineItemKind.tip,
  LineItemKind.discount,
];

/**
 * Priority-3 screen: item-level assignment. Toggling avatars is optimistic
 * (pure local state, per CLAUDE.md/task rules) — assignments are only sent
 * to the server, and the expense confirmed, when the user taps "Confirm",
 * which awaits the server the whole way (it writes the ledger).
 */
export function AssignmentScreen({
  expense,
  members,
  onConfirmed,
}: {
  expense: ExpenseResponse;
  members: RememberedMember[];
  onConfirmed: (expense: ExpenseResponse) => void;
}) {
  const itemLines = useMemo(
    () =>
      expense.line_items.filter(
        (li) => li.kind === LineItemKind.item || li.kind === LineItemKind.refund,
      ),
    [expense.line_items],
  );
  const cartLines = useMemo(
    () => expense.line_items.filter((li) => CART_LEVEL.includes(li.kind)),
    [expense.line_items],
  );

  const [assignments, setAssignments] = useState<Map<string, Set<string>>>(
    () => new Map(itemLines.map((li) => [li.id, new Set<string>()])),
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const unassignedMinor = itemLines.reduce((sum, li) => {
    const assigned = assignments.get(li.id);
    return assigned && assigned.size > 0 ? sum : sum + li.total_minor;
  }, 0);

  const canConfirm = itemLines.length > 0 && unassignedMinor === 0 && !submitting;

  function toggle(lineId: string, userId: string) {
    setAssignments((prev) => {
      const next = new Map(prev);
      const current = new Set(next.get(lineId) ?? []);
      if (current.has(userId)) current.delete(userId);
      else current.add(userId);
      next.set(lineId, current);
      return next;
    });
  }

  async function handleConfirm() {
    setSubmitting(true);
    setError(null);
    try {
      const payloadAssignments: AssignmentIn[] = [];
      for (const [lineItemId, userIds] of assignments.entries()) {
        for (const userId of userIds) {
          payloadAssignments.push({ line_item_id: lineItemId, user_id: userId, weight: "1" });
        }
      }
      await api.putAssignments(expense.id, { assignments: payloadAssignments });
      const confirmed = await api.confirmExpense(expense.id);
      onConfirmed(confirmed);
    } catch (err) {
      setError(formatApiError(err, "Could not confirm expense"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 pb-6">
      <UnassignedChip minor={unassignedMinor} />

      <div className="flex flex-col gap-3">
        {itemLines.map((li) => (
          <LineItemCard
            key={li.id}
            line={li}
            members={members}
            assignedUserIds={assignments.get(li.id) ?? new Set()}
            onToggle={(userId) => toggle(li.id, userId)}
            currency={expense.currency}
          />
        ))}
      </div>

      {cartLines.length > 0 && (
        <div className="flex flex-col gap-2">
          <p className="text-xs font-semibold uppercase tracking-wide text-gray-400">
            Fees & discounts (auto-split)
          </p>
          {cartLines.map((li) => (
            <CartLevelRow key={li.id} line={li} currency={expense.currency} />
          ))}
        </div>
      )}

      {error && <p className="text-sm text-red-600">{error}</p>}

      <button
        type="button"
        onClick={handleConfirm}
        disabled={!canConfirm}
        className="sticky bottom-16 rounded-xl bg-brand-600 px-4 py-3 font-semibold text-white shadow-lg disabled:bg-gray-300"
      >
        {submitting ? "Confirming…" : "Confirm expense"}
      </button>
    </div>
  );
}
