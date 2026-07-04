import { formatMoney } from "@splitr/core";

/**
 * The ONLY place in web/ that should call formatMoney directly for
 * inline rendering — every screen renders money through this component so
 * there is a single, auditable point where minor units become display
 * text (CLAUDE.md invariant #1 / task brief).
 */
export function Money({
  minor,
  currency = "INR",
  className,
  showPositiveSign,
}: {
  minor: number;
  currency?: string;
  className?: string;
  showPositiveSign?: boolean;
}) {
  return (
    <span className={className}>
      {formatMoney(minor, currency, { showPositiveSign })}
    </span>
  );
}
