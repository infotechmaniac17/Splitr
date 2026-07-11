"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AllocationPreviewResponse, ExpenseResponse } from "@splitr/core";
import { ApiError, LineItemKind, formatMoney } from "@splitr/core";
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
 * Extracts the reconciled-total figure (integer minor units) from a
 * `total_mismatch_with_discount` issue message for DISPLAY purposes only
 * (the "Update total to ₹X" button's label) -- see
 * backend/app/domain/gst.py's check_gst_invariants, whose message always
 * ends with the literal phrase "update total to {N}." where N is the
 * server-computed reconciled total in minor units. This performs no
 * arithmetic on the extracted figure (CLAUDE.md invariant #1) -- it's a
 * plain regex extraction of a number the server already computed and wrote
 * out in English; the actual total_minor mutation is always done entirely
 * server-side via POST /expenses/{id}/accept-computed-total.
 */
function parseReconciledTotalMinor(message: string): number | null {
  const match = message.match(/update total to ([\d,]+)/i);
  if (!match) return null;
  const digits = match[1].replace(/,/g, "");
  if (!/^\d+$/.test(digits)) return null;
  const value = Number(digits);
  return Number.isSafeInteger(value) ? value : null;
}

/**
 * Invoice review & assignment screen for a draft (parse_status='parsed')
 * expense -- covers both the "item assignment screen" and the assignment-
 * related parts of the "invoice review" screen from the task brief (the
 * PDF-preview + editable-table needs_review flow lives in
 * NeedsReviewView.tsx instead, since that's a distinct parse_status).
 *
 * Row selection state hydrates from `line_items[].assignments` (GET
 * /expenses/{id} now embeds each line's current assignments -- M6-M8
 * total-reconciliation ruling, item 6) on mount, so this screen no longer
 * starts "blind" (all rows appearing unassigned) on a fresh load/reload of
 * an expense that already has some assignments. Every toggle after that is
 * still persisted immediately (optimistic local update, no rollback-to-
 * server-state on failure -- just an inline row error so the user can
 * retry).
 */
export function AssignmentScreen({
  expense,
  members,
  onConfirmed,
  onExpenseUpdated,
}: {
  expense: ExpenseResponse;
  members: RememberedMember[];
  onConfirmed: (expense: ExpenseResponse) => void;
  onExpenseUpdated: (expense: ExpenseResponse) => void;
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

  // Lazy initializer only -- runs once on mount, seeded from server truth
  // (line.assignments). Deliberately does NOT re-sync on every `expense`
  // prop update (e.g. after a discount patch or accept-computed-total),
  // which would clobber any in-flight local toggle a user just made.
  const [rowSelections, setRowSelections] = useState<Map<string, Set<string>>>(
    () =>
      new Map(
        itemLines.map((li) => [
          li.id,
          new Set(li.assignments.map((a) => a.user_id)),
        ]),
      ),
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
  // M6-M8 total-reconciliation ruling, item 3: set only when a confirm
  // attempt 422s with a `total_mismatch_with_discount` issue -- the
  // reconciled minor-units figure is parsed from the SERVER's own message
  // text (never computed client-side) purely to label the one-click "Update
  // total to ₹X" button; the actual mutation always goes through
  // POST /expenses/{id}/accept-computed-total, which recomputes and writes
  // the figure server-side.
  const [totalMismatchReconciledMinor, setTotalMismatchReconciledMinor] =
    useState<number | null>(null);
  const [acceptingTotal, setAcceptingTotal] = useState(false);

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

  // M6-M8 total-reconciliation ruling, API GAPS item 8: the "which lines
  // still need assignment" problem is now driven by the allocation-preview
  // response's structured `unassigned_lines` problem (code + count +
  // line_ids), i.e. SERVER truth, rather than this component's own local
  // `rowSelections` state -- the server is the one that actually decides
  // whether an expense can be confirmed. Falls back to the local,
  // optimistic count only while the preview hasn't loaded yet (first
  // render), so the header doesn't flash "all assigned" before the first
  // fetch resolves.
  const unassignedProblem = preview?.problems.find(
    (p) => p.code === "unassigned_lines",
  );
  const unassignedLineIds = useMemo(
    () => new Set(unassignedProblem?.line_ids ?? []),
    [unassignedProblem],
  );
  const localUnassignedCount = itemLines.filter((li) => {
    const s = rowSelections.get(li.id);
    return !s || s.size === 0;
  }).length;
  const unassignedCount = preview
    ? unassignedLineIds.size
    : localUnassignedCount;

  function scrollToFirstUnassigned() {
    const first = itemLines.find((li) =>
      preview ? unassignedLineIds.has(li.id) : !rowSelections.get(li.id)?.size,
    );
    if (first) {
      rowRefs.current
        .get(first.id)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  // Confirmation is only truly blocked by problems OTHER than
  // 'needs_review' (unassigned_lines, split_error, ...) -- 'needs_review'
  // alone (the GST/total invariant flag) still lets compute_allocation run
  // and populate `members` (see backend/app/api/expenses.py:
  // get_allocation_preview), so the Confirm button stays clickable to let
  // the user discover the SPECIFIC 422 reason (and, for a
  // total_mismatch_with_discount reason, the one-click fix below) instead
  // of being permanently greyed out with no way to see why.
  const blockingProblems =
    preview?.problems.filter((p) => p.code !== "needs_review") ?? [];
  const canConfirm =
    !confirming &&
    !previewLoading &&
    preview != null &&
    blockingProblems.length === 0 &&
    preview.members.length > 0;

  async function handleConfirm() {
    setConfirming(true);
    setConfirmError(null);
    setTotalMismatchReconciledMinor(null);
    try {
      const confirmed = await api.confirmExpense(expense.id);
      onConfirmed(confirmed);
    } catch (err) {
      setConfirmError(formatApiError(err, "Could not confirm expense"));
      if (
        err instanceof ApiError &&
        typeof err.detail === "string" &&
        err.detail.includes("total_mismatch_with_discount")
      ) {
        setTotalMismatchReconciledMinor(parseReconciledTotalMinor(err.detail));
      }
    } finally {
      setConfirming(false);
    }
  }

  async function handleAcceptComputedTotal() {
    setAcceptingTotal(true);
    setConfirmError(null);
    try {
      const updated = await api.acceptComputedTotal(expense.id);
      onExpenseUpdated(updated);
      setTotalMismatchReconciledMinor(null);
      refetchPreview();
    } catch (err) {
      setConfirmError(formatApiError(err, "Could not update total"));
    } finally {
      setAcceptingTotal(false);
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
          onUpdated={(updated) => {
            onExpenseUpdated(updated);
            refetchPreview();
          }}
        />
      </section>

      {expense.tax_components.length > 0 && (
        <section className="flex flex-col gap-1 rounded-lg bg-gray-50 px-3 py-2 text-xs text-gray-500">
          <p className="font-semibold uppercase tracking-wide text-gray-400">
            GST
          </p>
          {expense.tax_components.map((tc, i) => (
            <div key={i} className="flex items-center justify-between">
              <span>
                {tc.name}
                {tc.rate != null ? ` (${tc.rate}%)` : ""}
              </span>
              <Money minor={tc.amount_minor} currency={expense.currency} />
            </div>
          ))}
          {preview &&
            preview.exclusive_gst_minor != null &&
            preview.exclusive_gst_minor !== 0 && (
              <div className="flex items-center justify-between border-t border-gray-200 pt-1 font-semibold text-gray-600">
                <span>Total GST</span>
                <Money
                  minor={preview.exclusive_gst_minor}
                  currency={expense.currency}
                />
              </div>
            )}
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
        gstMode={expense.gst_mode}
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
          {preview.problems.map((p, i) =>
            p.code === "unassigned_lines" ? (
              <button
                key={i}
                type="button"
                onClick={scrollToFirstUnassigned}
                className="text-left underline"
              >
                {p.count ?? p.line_ids?.length ?? 0} item
                {(p.count ?? 0) === 1 ? "" : "s"} need assignment — tap to jump
                to the first one
              </button>
            ) : (
              <p key={i}>{p.message}</p>
            ),
          )}
        </div>
      )}

      {confirmError && (
        <div className="flex flex-col gap-2">
          <p className="text-sm text-red-600">{confirmError}</p>
          {totalMismatchReconciledMinor != null && (
            <button
              type="button"
              onClick={handleAcceptComputedTotal}
              disabled={acceptingTotal}
              className="self-start rounded-lg border border-brand-300 bg-brand-50 px-3 py-1.5 text-xs font-semibold text-brand-700 disabled:opacity-50"
            >
              {acceptingTotal
                ? "Updating…"
                : `Update total to ${formatMoney(totalMismatchReconciledMinor, expense.currency)}`}
            </button>
          )}
        </div>
      )}

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
