import { parseStatusLabels, type ParseStatus } from "@splitr/core";

const STYLES: Record<ParseStatus, string> = {
  queued: "bg-amber-100 text-amber-800",
  parsed: "bg-emerald-100 text-emerald-800",
  needs_review: "bg-red-100 text-red-800",
  confirmed: "bg-brand-100 text-brand-700",
  failed: "bg-gray-200 text-gray-700",
};

export function StatusBadge({ status }: { status: ParseStatus }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STYLES[status]}`}
    >
      {parseStatusLabels[status]}
    </span>
  );
}
