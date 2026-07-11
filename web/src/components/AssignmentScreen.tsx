"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AllocationPreviewResponse, ExpenseResponse } from "@splitr/core";
import { LineItemKind } from "@splitr/core";
import { CartLevelRow } from "@/components/CartLevelRow";
import { DiscountBlock } from "@/components/DiscountBlock";
import { ItemsTable } from "@/components/ItemsTable";
import { SplitPreviewPanel } from "@/components/SplitPreviewPanel";
import { StatusBadge } from "@/components/StatusBadge";
import { Avatar } from "@/components/Avatar";
import { Money } from "@/components/Money";
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

const ROW_SAVE_DEBOUNCE_MS = 500;

/**
 * Invoice review & assignment screen for a draft (parse_status='parsed')
 * expense -- covers both the "item assignment screen" and the assignment-
 * related parts of the "invoice review" screen from the task brief (the
 * PDF-preview + editable-table needs_review flow lives in
 * NeedsReviewView.tsx instead, since that's a distinct parse_status).
 *
 * ASSIGNMENT-READ GAP: there is no backend endpoint that returns the
 * CURRENT per-line-item assignments of a draft expense (GET /expenses/{id}
 * doesn't include them; GET /expenses/{id}/allocation-preview and
 * .../shares only return per-MEMBER totals, never which member is on which
 * line). So, exactly like the pre-existing AssignmentScreen this replaces,
 * row selection state starts empty on every mount/reload -- it does not
 * rehydrate from the server. Unlike the old screen, though, every toggle
 * here is persisted immediately (per the task brief), so the underlying
 * data is never lost -- only this component's own visual state resets on
 * reload. See the final report for the exact endpoint that would fix this.
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
        (li) =>
          li.kind === LineItemKind.item || li.kind === LineItemKind.refund,
      ),
    [expense.line_items],
  );
  const cartLines = useMemo(
    () => expense.line_items.filter((li) => CART_LEVEL.includes(li.kind)),
    [expense.line_items],
  );

  const [rowSelections, setRowSelections] = useState<Map<string, Set<string>>>(
    () => new Map(itemLines.map((li) => [li.id, new Set<string>()])),
  );
  const [rowSaving, setRowSaving] = useState<Set<string>>(new Set());
  const [rowError, setRowError] = useState<Map<string, string>>(new Map());
  const [checkedRows, setCheckedRows] = useState<Set<string>>(new Set());
  const [bulkTarget, setBulkTarget] = useState<Set<string>>(new Set());
  const [bulkSubmitting, setBulkSubmitting] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);

  const [preview, setPreview] = useState<AllocationPreviewResponse | null>(
    null,
  );
  const [previewLoading, setPreviewLoading] = useState(true);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [confirming, setConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const rowRefs = useRef<Map<string, HTMLTableRowElement>>(new Map());
  // Mirrors `rowSelections` synchronously so the debounced persistRow below
  // (which fires after ROW_SAVE_DEBOUNCE_MS) always reads the latest
  // selections rather than a stale render-time closure.
  const rowSelectionsRef = useRef(rowSelections);
  useEffect(() => {
    rowSelectionsRef.current = rowSelections;
  }, [rowSelections]);

  const refetchPreview = useCallback(() => {
    setPreviewLoading(true);
    setPreviewError(null);
    api
      .getAllocationPreview(expense.id)
      .then(setPreview)
      .catch((err) =>
        setPreviewError(formatApiError(err, "Could not load split preview")),
      )
      .finally(() => setPreviewLoading(false));
  }, [expense.id]);

  useEffect(() => {
    refetchPreview();
  }, [refetchPreview]);

  // M1 (quick manual entry) expenses freeze share_minor at create time and
  // never carry real line items -- discount editing is structurally
  // unconsumable for them (see backend/app/api/expenses.py:
  // patch_expense_discount's 422). There is no boolean flag exposed for
  // this; expenses/manual/page.tsx (the only creator of this shape) always
  // leaves exactly one synthetic line item, so that's the best signal
  // available client-side without risking the 422. See final report.
  const frozenSharesLikely =
    expense.source === "manual" && expense.line_items.length <= 1;

  async function persistRow(lineId: string, selected: Set<string>) {
    setRowSaving((prev) => new Set(prev).add(lineId));
    setRowError((prev) => {
      const next = new Map(prev);
      next.delete(lineId);
      return next;
    });
    try {
      if (selected.size > 0) {
        await api.bulkPutAssignments(expense.id, {
          item_ids: [lineId],
          member_ids: [...selected],
        });
      } else {
        // The bulk endpoint requires at least one member_id, so clearing a
        // row to empty has to go through the full-replace PUT instead --
        // flattening every OTHER row's currently known local selection (see
        // the assignment-read gap noted in the module docstring: this is
        // only as complete as this session's own edits).
        const assignments = [];
        for (const [
          otherLineId,
          otherSelected,
        ] of rowSelectionsRef.current.entries()) {
          if (otherLineId === lineId) continue;
          for (const userId of otherSelected) {
            assignments.push({
              line_item_id: otherLineId,
              user_id: userId,
              weight: "1",
            });
          }
        }
        if (assignments.length > 0) {
          await api.putAssignments(expense.id, { assignments });
        }
      }
      refetchPreview();
    } catch (err) {
      setRowError((prev) =>
        new Map(prev).set(lineId, formatApiError(err, "Could not save")),
      );
      // Rollback: re-fetch nothing (we don't have server truth to roll back
      // to -- see module docstring); instead just flag the row failed so
      // the user can retry the toggle.
    } finally {
      setRowSaving((prev) => {
        const next = new Set(prev);
        next.delete(lineId);
        return next;
      });
    }
  }

  function toggleMember(lineId: string, userId: string) {
    setRowSelections((prev) => {
      const next = new Map(prev);
      const current = new Set(next.get(lineId) ?? []);
      if (current.has(userId)) current.delete(userId);
      else current.add(userId);
      next.set(lineId, current);

      const existingTimer = timers.current.get(lineId);
      if (existingTimer) clearTimeout(existingTimer);
      timers.current.set(
        lineId,
        setTimeout(() => {
          void persistRow(lineId, current);
        }, ROW_SAVE_DEBOUNCE_MS),
      );

      return next;
    });
  }

  function toggleChecked(lineId: string) {
    setCheckedRows((prev) => {
      const next = new Set(prev);
      if (next.has(lineId)) next.delete(lineId);
      else next.add(lineId);
      return next;
    });
  }

  function toggleBulkTarget(userId: string) {
    setBulkTarget((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });
  }

  async function applyBulkAssign() {
    if (checkedRows.size === 0 || bulkTarget.size === 0) return;
    setBulkSubmitting(true);
    setBulkError(null);
    try {
      const itemIds = [...checkedRows];
      const memberIds = [...bulkTarget];
      await api.bulkPutAssignments(expense.id, {
        item_ids: itemIds,
        member_ids: memberIds,
      });
      setRowSelections((prev) => {
        const next = new Map(prev);
        for (const id of itemIds) next.set(id, new Set(memberIds));
        return next;
      });
      setCheckedRows(new Set());
      refetchPreview();
    } catch (err) {
      setBulkError(formatApiError(err, "Could not bulk-assign"));
    } finally {
      setBulkSubmitting(false);
    }
  }

  // Non-money, integer count only (row membership, never a *_minor sum) --
  // purely for the "N of M items assigned" visual + scroll-to-first-
  // unassigned affordance.
  const unassignedCount = itemLines.filter((li) => {
    const s = rowSelections.get(li.id);
    return !s || s.size === 0;
  }).length;

  function scrollToFirstUnassigned() {
    const first = itemLines.find((li) => {
      const s = rowSelections.get(li.id);
      return !s || s.size === 0;
    });
    if (first) {
      rowRefs.current
        .get(first.id)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  const canConfirm =
    !confirming &&
    !previewLoading &&
    preview != null &&
    preview.problems.length === 0 &&
    preview.members.length > 0;

  async function handleConfirm() {
    setConfirming(true);
    setConfirmError(null);
    try {
      const confirmed = await api.confirmExpense(expense.id);
      onConfirmed(confirmed);
    } catch (err) {
      setConfirmError(formatApiError(err, "Could not confirm expense"));
    } finally {
      setConfirming(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 pb-6">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h1 className="text-xl font-bold">{expense.vendor ?? "Expense"}</h1>
          <p className="text-xs text-gray-400">
            {expense.invoice_date ?? "No invoice date"} ·{" "}
            {itemLines.length - unassignedCount}/{itemLines.length} items
            assigned
          </p>
        </div>
        <StatusBadge status={expense.parse_status} />
      </div>

      <section>
        <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-400">
          Discount
        </p>
        <DiscountBlock
          expense={expense}
          discountRecordedButInert={
            preview?.discount_recorded_but_inert ?? false
          }
          frozenSharesLikely={frozenSharesLikely}
          onUpdated={() => refetchPreview()}
        />
      </section>

      {preview &&
        preview.exclusive_gst_minor != null &&
        preview.exclusive_gst_minor !== 0 && (
          <section className="rounded-lg bg-gray-50 px-3 py-2 text-xs text-gray-500">
            GST:{" "}
            <Money
              minor={preview.exclusive_gst_minor}
              currency={expense.currency}
              className="font-semibold"
            />{" "}
            (see split preview below for the full per-member breakdown; a
            printed per-item GST rate column isn&apos;t shown here — see final
            report for the API gap).
          </section>
        )}

      {checkedRows.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-brand-200 bg-brand-50 px-3 py-2">
          <span className="text-xs font-semibold text-brand-700">
            Assign {checkedRows.size} selected to:
          </span>
          {members.map((m) => (
            <button
              type="button"
              key={m.id}
              onClick={() => toggleBulkTarget(m.id)}
              className="flex flex-col items-center gap-0.5 text-[10px]"
              aria-pressed={bulkTarget.has(m.id)}
            >
              <Avatar name={m.name} selected={bulkTarget.has(m.id)} size="sm" />
            </button>
          ))}
          <button
            type="button"
            onClick={applyBulkAssign}
            disabled={bulkSubmitting || bulkTarget.size === 0}
            className="rounded-lg bg-brand-600 px-3 py-1 text-xs font-semibold text-white disabled:opacity-50"
          >
            {bulkSubmitting ? "Assigning…" : "Apply"}
          </button>
        </div>
      )}
      {bulkError && <p className="text-xs text-red-600">{bulkError}</p>}

      <ItemsTable
        lines={itemLines}
        members={members}
        rowSelections={rowSelections}
        onToggleMember={toggleMember}
        rowSaving={rowSaving}
        rowError={rowError}
        checkedRows={checkedRows}
        onToggleChecked={toggleChecked}
        currency={expense.currency}
        rowRefs={rowRefs}
      />

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

      <SplitPreviewPanel
        preview={preview}
        loading={previewLoading}
        error={previewError}
        members={members}
        currency={expense.currency}
      />

      {preview && preview.problems.length > 0 && (
        <div className="flex flex-col gap-1 rounded-lg bg-amber-50 p-3 text-sm text-amber-800">
          {preview.problems.map((p, i) => (
            <button
              key={i}
              type="button"
              onClick={scrollToFirstUnassigned}
              className="text-left underline"
            >
              {p.message}
            </button>
          ))}
        </div>
      )}
      {unassignedCount > 0 && (
        <button
          type="button"
          onClick={scrollToFirstUnassigned}
          className="text-left text-sm font-medium text-amber-700 underline"
        >
          {unassignedCount} item{unassignedCount === 1 ? "" : "s"} unassigned —
          tap to jump to the first one
        </button>
      )}

      {confirmError && <p className="text-sm text-red-600">{confirmError}</p>}

      <button
        type="button"
        onClick={handleConfirm}
        disabled={!canConfirm}
        className="sticky bottom-16 rounded-xl bg-brand-600 px-4 py-3 font-semibold text-white shadow-lg disabled:bg-gray-300"
      >
        {confirming ? "Confirming…" : "Confirm expense"}
      </button>
    </div>
  );
}
