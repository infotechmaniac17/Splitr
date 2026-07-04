import Link from "next/link";
import type { ExpenseResponse } from "@splitr/core";
import { Money } from "@/components/Money";
import { StatusBadge } from "@/components/StatusBadge";

export function ConfirmedSummary({ expense }: { expense: ExpenseResponse }) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">{expense.vendor ?? "Expense"}</h1>
        <StatusBadge status={expense.parse_status} />
      </div>
      <div className="rounded-xl border border-gray-200 p-5 text-center">
        <p className="text-sm text-gray-500">Total</p>
        <p className="text-3xl font-bold">
          <Money minor={expense.total_minor} currency={expense.currency} />
        </p>
        {expense.confirmed_at && (
          <p className="mt-1 text-xs text-gray-400">
            Confirmed {new Date(expense.confirmed_at).toLocaleString()}
          </p>
        )}
      </div>
      <p className="text-center text-sm text-gray-500">
        This expense has been posted to the ledger and can&apos;t be edited directly —
        corrections happen as new refund/adjustment entries.
      </p>
      {expense.group_id && (
        <Link
          href={`/groups/${expense.group_id}`}
          className="rounded-lg bg-gray-900 px-4 py-3 text-center text-sm font-semibold text-white"
        >
          Back to group
        </Link>
      )}
    </div>
  );
}
