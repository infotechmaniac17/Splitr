"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type {
  GroupExpensesGroupedResponse,
  GroupMemberInfo,
} from "@splitr/core";
import { parseStatusLabels } from "@splitr/core";
import { IdentityGate } from "@/components/IdentityGate";
import { Money } from "@/components/Money";
import { Avatar } from "@/components/Avatar";
import { api, formatApiError } from "@/lib/api";

/**
 * Date-grouped expenses list (M6-M8 item 7a): GET /groups/{id}/expenses,
 * buckets keyed by invoice_date, undated (date=null) bucket rendered last.
 * Per-member summaries are rendered straight off the response's
 * `member_shares` -- never aggregated/summed client-side.
 */
function GroupExpensesContent({ groupId }: { groupId: string }) {
  const [data, setData] = useState<GroupExpensesGroupedResponse | null>(null);
  const [members, setMembers] = useState<GroupMemberInfo[]>([]);
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  function nameFor(userId: string): string {
    return (
      members.find((m) => m.user_id === userId)?.name ?? userId.slice(0, 8)
    );
  }

  function load() {
    setLoading(true);
    setError(null);
    api
      .getGroupExpenses(groupId, {
        from: fromDate || undefined,
        to: toDate || undefined,
      })
      .then(setData)
      .catch((err) => setError(formatApiError(err, "Could not load expenses")))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    api
      .getGroupMembers(groupId)
      .then((res) => setMembers(res.members))
      .catch(() => setMembers([]));
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId]);

  // Undated bucket is placed last by the API's own ordering (SQL ORDER BY
  // invoice_date IS NULL, invoice_date, created_at); rendered here in
  // whatever order the response already provides, never re-sorted.
  const datedBuckets = data?.buckets.filter((b) => b.date !== null) ?? [];
  const undatedBucket = data?.buckets.find((b) => b.date === null) ?? null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Expenses</h1>
        <Link
          href={`/groups/${groupId}`}
          className="text-xs font-medium text-brand-700"
        >
          Back to group
        </Link>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          load();
        }}
        className="flex flex-wrap items-end gap-2"
      >
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          From
          <input
            type="date"
            value={fromDate}
            onChange={(e) => setFromDate(e.target.value)}
            className="rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-gray-500">
          To
          <input
            type="date"
            value={toDate}
            onChange={(e) => setToDate(e.target.value)}
            className="rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </label>
        <button
          type="submit"
          className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm font-medium text-white"
        >
          Filter
        </button>
        {(fromDate || toDate) && (
          <button
            type="button"
            onClick={() => {
              setFromDate("");
              setToDate("");
              load();
            }}
            className="text-sm text-gray-500 underline"
          >
            Clear
          </button>
        )}
      </form>

      {loading && <p className="text-sm text-gray-400">Loading…</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}

      {!loading && !error && data && data.buckets.length === 0 && (
        <p className="rounded-lg bg-gray-50 p-4 text-sm text-gray-400">
          No expenses in this range yet.
        </p>
      )}

      {!loading &&
        !error &&
        [...datedBuckets, ...(undatedBucket ? [undatedBucket] : [])].map(
          (bucket) => (
            <section
              key={bucket.date ?? "undated"}
              className="flex flex-col gap-2"
            >
              <h2 className="text-sm font-semibold text-gray-500">
                {bucket.date ?? "Undated"}
              </h2>
              {bucket.expenses.map((exp) => (
                <Link
                  key={exp.id}
                  href={`/expenses/${exp.id}`}
                  className="flex flex-col gap-1 rounded-xl border border-gray-200 p-3"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium">
                      {exp.vendor ?? "Expense"}
                    </span>
                    <Money minor={exp.total_minor} className="font-semibold" />
                  </div>
                  <div className="flex items-center justify-between text-xs text-gray-400">
                    <span>{parseStatusLabels[exp.parse_status]}</span>
                    <span>Paid by {nameFor(exp.paid_by)}</span>
                  </div>
                  {exp.member_shares.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-2">
                      {exp.member_shares.map((s) => (
                        <span
                          key={s.user_id}
                          className="flex items-center gap-1 rounded-full bg-gray-50 px-2 py-0.5 text-[11px]"
                        >
                          <Avatar name={nameFor(s.user_id)} size="sm" />
                          {nameFor(s.user_id)}
                          <Money minor={s.share_minor} />
                        </span>
                      ))}
                    </div>
                  )}
                </Link>
              ))}
            </section>
          ),
        )}
    </div>
  );
}

export default function GroupExpensesPage({
  params,
}: {
  params: { groupId: string };
}) {
  const { groupId } = params;
  return (
    <IdentityGate>
      <GroupExpensesContent groupId={groupId} />
    </IdentityGate>
  );
}
