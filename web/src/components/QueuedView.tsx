import { StatusBadge } from "@/components/StatusBadge";

export function QueuedView() {
  return (
    <div className="flex flex-col items-center gap-4 pt-16 text-center">
      <div className="h-10 w-10 animate-spin rounded-full border-4 border-brand-200 border-t-brand-600" />
      <StatusBadge status="queued" />
      <p className="max-w-xs text-sm text-gray-500">
        Extracting line items from your invoice. This usually takes a few seconds — this page
        will update automatically.
      </p>
    </div>
  );
}
