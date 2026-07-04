import { Money } from "@/components/Money";

export function UnassignedChip({ minor }: { minor: number }) {
  const settled = minor === 0;
  return (
    <div
      className={`sticky top-14 z-10 flex items-center justify-between rounded-full px-4 py-2 text-sm font-semibold shadow ${
        settled ? "bg-emerald-100 text-emerald-800" : "bg-amber-100 text-amber-800"
      }`}
    >
      <span>{settled ? "All items assigned" : "Unassigned"}</span>
      <Money minor={minor} />
    </div>
  );
}
