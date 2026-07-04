export function Avatar({
  name,
  selected = false,
  size = "md",
}: {
  name: string;
  selected?: boolean;
  size?: "sm" | "md";
}) {
  const dims = size === "sm" ? "h-7 w-7 text-xs" : "h-9 w-9 text-sm";
  return (
    <span
      className={`inline-flex ${dims} items-center justify-center rounded-full font-semibold ring-2 transition ${
        selected
          ? "bg-brand-600 text-white ring-brand-600"
          : "bg-gray-200 text-gray-700 ring-transparent"
      }`}
      title={name}
    >
      {name.slice(0, 1).toUpperCase()}
    </span>
  );
}
