"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { toMinorUnits } from "@splitr/core";
import { IdentityGate } from "@/components/IdentityGate";
import { Avatar } from "@/components/Avatar";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { rememberExpense, type RememberedMember } from "@/lib/local-store";

/**
 * Quick Manual Entry fallback (ARCHITECTURE.md §3 edge-case table): total-
 * first entry that's enough to save and split equally immediately. Also
 * reachable from a `failed` parse_status (corrupted/unsupported PDF).
 */
function ManualEntryContent() {
  const { user } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const groupId = searchParams.get("groupId");

  const [vendor, setVendor] = useState("");
  const [amountInput, setAmountInput] = useState("");
  const [members, setMembers] = useState<RememberedMember[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    if (groupId) {
      api
        .getGroupMembers(groupId)
        .then((res) => {
          const m = res.members.map((x) => ({ id: x.user_id, name: x.name }));
          setMembers(m);
          setSelected(new Set(m.map((x) => x.id)));
        })
        .catch(() => {
          setMembers([]);
          setSelected(new Set());
        });
    } else {
      setMembers([{ id: user.id, name: user.name }]);
      setSelected(new Set([user.id]));
    }
  }, [groupId, user]);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!user) return;
    setSubmitting(true);
    setError(null);
    try {
      const totalMinor = toMinorUnits(amountInput);
      const expense = await api.createExpense({
        group_id: groupId ?? null,
        paid_by: user.id,
        vendor: vendor || null,
        currency: "INR",
        total_minor: totalMinor,
        participants: Array.from(selected),
      });
      rememberExpense(user.id, {
        id: expense.id,
        groupId: expense.group_id ?? null,
      });
      router.push(`/expenses/${expense.id}`);
    } catch (err) {
      setError(formatApiError(err, "Could not save expense"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-5">
      <h1 className="text-xl font-bold">Quick manual entry</h1>

      <label className="flex flex-col gap-1 text-sm font-medium text-gray-700">
        Vendor (optional)
        <input
          value={vendor}
          onChange={(e) => setVendor(e.target.value)}
          placeholder="Swiggy"
          className="rounded-lg border border-gray-300 px-3 py-2 text-base"
        />
      </label>

      <label className="flex flex-col gap-1 text-sm font-medium text-gray-700">
        Total amount (₹)
        <input
          required
          inputMode="decimal"
          value={amountInput}
          onChange={(e) => setAmountInput(e.target.value)}
          placeholder="857.00"
          className="rounded-lg border border-gray-300 px-3 py-2 text-base"
        />
      </label>

      <div>
        <p className="mb-2 text-sm font-medium text-gray-700">
          Split equally between
        </p>
        <div className="flex flex-wrap gap-3">
          {members.map((m) => (
            <button
              type="button"
              key={m.id}
              onClick={() => toggle(m.id)}
              className="flex flex-col items-center gap-1 text-xs"
            >
              <Avatar name={m.name} selected={selected.has(m.id)} />
              {m.name}
            </button>
          ))}
        </div>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <button
        type="submit"
        disabled={submitting || selected.size === 0 || !amountInput}
        className="rounded-lg bg-brand-600 px-4 py-3 font-semibold text-white disabled:opacity-50"
      >
        {submitting ? "Saving…" : "Save expense"}
      </button>
    </form>
  );
}

export default function ManualEntryPage() {
  return (
    <IdentityGate>
      <Suspense fallback={null}>
        <ManualEntryContent />
      </Suspense>
    </IdentityGate>
  );
}
