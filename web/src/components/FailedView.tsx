import Link from "next/link";
import type { ExpenseResponse } from "@splitr/core";

/** parse_status='failed': corrupted/unsupported PDF -> Quick Manual Entry (ARCHITECTURE.md §3). */
export function FailedView({ expense }: { expense: ExpenseResponse }) {
  return (
    <div className="flex flex-col items-center gap-4 pt-8 text-center">
      <span className="text-4xl">⚠️</span>
      <h1 className="text-xl font-bold">Couldn&apos;t parse this PDF</h1>
      <p className="text-sm text-gray-500">
        The file looks corrupted or is an unsupported format. You can still record this expense
        by hand — it only takes a few seconds.
      </p>
      <Link
        href={`/expenses/manual${expense.group_id ? `?groupId=${expense.group_id}` : ""}`}
        className="w-full rounded-xl bg-brand-600 px-4 py-3 text-center font-semibold text-white"
      >
        Enter manually
      </Link>
    </div>
  );
}
