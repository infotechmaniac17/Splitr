"use client";

import { useState } from "react";
import {
  DiscountType,
  discountSourceLabels,
  formatMoney,
  toMinorUnits,
  type ExpenseResponse,
} from "@splitr/core";
import { Money } from "@/components/Money";
import { api, formatApiError } from "@/lib/api";

/**
 * Discount block for the invoice review screen. Driven entirely by the
 * expense's persisted discount snapshot (discount_type/value/percent/
 * threshold/source) plus the allocation-preview's `discount_recorded_but_
 * inert` flag, which is the ONLY signal that distinguishes "a vendor rule
 * matched and is currently applied" from "a vendor rule/manual discount is
 * recorded but the current subtotal is below its threshold" -- this
 * component never re-derives that itself.
 */
export function DiscountBlock({
  expense,
  discountRecordedButInert,
  onUpdated,
}: {
  expense: ExpenseResponse;
  discountRecordedButInert: boolean;
  onUpdated: (expense: ExpenseResponse) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [type, setType] = useState<DiscountType>(DiscountType.flat);
  const [valueInput, setValueInput] = useState("");
  const [thresholdInput, setThresholdInput] = useState("0");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasDiscount = expense.discount_type != null;
  const isVendorRule = expense.discount_source === "vendor_rule";
  const isManual = expense.discount_source === "manual";

  async function clearDiscount() {
    setSubmitting(true);
    setError(null);
    try {
      const updated = await api.patchExpenseDiscount(expense.id, {
        discount_type: null,
      });
      onUpdated(updated);
    } catch (err) {
      setError(formatApiError(err, "Could not clear discount"));
    } finally {
      setSubmitting(false);
    }
  }

  function startEditing() {
    setType((expense.discount_type as DiscountType) ?? DiscountType.flat);
    setValueInput(
      expense.discount_type === DiscountType.flat
        ? formatMoney(expense.discount_value_minor ?? 0, expense.currency, {
            showSymbol: false,
          })
        : (expense.discount_percent ?? ""),
    );
    setThresholdInput(
      formatMoney(expense.discount_threshold_minor ?? 0, expense.currency, {
        showSymbol: false,
      }),
    );
    setEditing(true);
    setError(null);
  }

  async function submitManual(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const thresholdMinor = toMinorUnits(
        thresholdInput || "0",
        expense.currency,
      );
      const payload =
        type === DiscountType.flat
          ? {
              discount_type: DiscountType.flat,
              discount_value_minor: toMinorUnits(
                valueInput || "0",
                expense.currency,
              ),
              discount_percent: null,
              discount_threshold_minor: thresholdMinor,
            }
          : {
              discount_type: DiscountType.percent,
              discount_value_minor: null,
              discount_percent: valueInput,
              discount_threshold_minor: thresholdMinor,
            };
      const updated = await api.patchExpenseDiscount(expense.id, payload);
      onUpdated(updated);
      setEditing(false);
    } catch (err) {
      setError(formatApiError(err, "Could not save discount"));
    } finally {
      setSubmitting(false);
    }
  }

  if (expense.is_frozen_shares) {
    return (
      <div className="rounded-lg bg-gray-50 px-3 py-2 text-xs text-gray-400">
        Discount editing isn&apos;t available for this expense (its shares are
        frozen — the explicit-shares/equal-split flow never consumes a discount
        snapshot). Recreate it with itemized line items to use vendor discounts.
      </div>
    );
  }

  if (editing) {
    return (
      <form
        onSubmit={submitManual}
        className="flex flex-col gap-2 rounded-lg border border-gray-200 p-3 text-sm"
      >
        <div className="flex gap-2">
          <select
            value={type}
            onChange={(e) => setType(e.target.value as DiscountType)}
            className="rounded border border-gray-300 px-2 py-1"
          >
            <option value={DiscountType.flat}>Flat (₹)</option>
            <option value={DiscountType.percent}>Percent (%)</option>
          </select>
          <input
            value={valueInput}
            onChange={(e) => setValueInput(e.target.value)}
            placeholder={type === DiscountType.flat ? "Amount ₹" : "Percent"}
            className="w-28 rounded border border-gray-300 px-2 py-1"
            required
          />
        </div>
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          Minimum order total (₹) for this discount to apply
          <input
            value={thresholdInput}
            onChange={(e) => setThresholdInput(e.target.value)}
            className="rounded border border-gray-300 px-2 py-1"
          />
        </label>
        {error && <p className="text-xs text-red-600">{error}</p>}
        <div className="flex gap-2">
          <button
            type="submit"
            disabled={submitting}
            className="rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
          >
            {submitting ? "Saving…" : "Save discount"}
          </button>
          <button
            type="button"
            onClick={() => setEditing(false)}
            className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs"
          >
            Cancel
          </button>
        </div>
      </form>
    );
  }

  if (!hasDiscount) {
    return (
      <div className="flex items-center justify-between rounded-lg bg-gray-50 px-3 py-2 text-sm">
        <span className="text-gray-400">No discount</span>
        <button
          type="button"
          onClick={startEditing}
          className="text-xs font-semibold text-brand-700"
        >
          Add discount
        </button>
      </div>
    );
  }

  const discountLabel =
    expense.discount_type === DiscountType.flat ? (
      <Money
        minor={expense.discount_value_minor ?? 0}
        currency={expense.currency}
      />
    ) : (
      `${expense.discount_percent}%`
    );
  const thresholdLabel = (
    <Money
      minor={expense.discount_threshold_minor ?? 0}
      currency={expense.currency}
    />
  );

  return (
    <div className="flex flex-col gap-2 rounded-lg px-3 py-2 text-sm">
      {discountRecordedButInert ? (
        <div className="rounded-lg bg-gray-100 px-3 py-2 text-gray-500">
          {expense.vendor ? `${expense.vendor}: ` : ""}
          {discountLabel} off on {thresholdLabel}+ — not applied (subtotal below
          threshold)
        </div>
      ) : (
        <div className="rounded-lg bg-emerald-50 px-3 py-2 text-emerald-800">
          {isVendorRule && expense.vendor ? `${expense.vendor}: ` : ""}
          {discountLabel} off{" "}
          {expense.discount_threshold_minor ? <>on {thresholdLabel}+ </> : null}
          — applied
        </div>
      )}
      <div className="flex items-center justify-between text-xs text-gray-400">
        <span>
          Source:{" "}
          {expense.discount_source
            ? discountSourceLabels[expense.discount_source]
            : "—"}
        </span>
        <div className="flex gap-3">
          <button
            type="button"
            onClick={startEditing}
            className="font-semibold text-brand-700"
          >
            {isManual ? "Edit" : "Override"}
          </button>
          <button
            type="button"
            onClick={clearDiscount}
            disabled={submitting}
            className="font-semibold text-gray-500"
          >
            Clear
          </button>
        </div>
      </div>
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}
