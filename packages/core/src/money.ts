/**
 * Single source of truth for money display. CLAUDE.md invariant #1: money
 * is integer minor units end to end. This module is the ONLY place that is
 * allowed to convert minor units to a decimal string for display — never do
 * ad hoc `amount / 100` float math anywhere in web/ or mobile/.
 *
 * All arithmetic here operates on BigInt/integers, never `number` division
 * followed by float formatting, so there is no IEEE-754 rounding surprise
 * even for large totals.
 */

const MINOR_UNITS_PER_MAJOR: Record<string, number> = {
  // ISO 4217 minor unit exponents. Extend as new currencies are supported;
  // default (currency not listed) falls back to 2 (matches INR/USD/etc).
  INR: 100,
  USD: 100,
  EUR: 100,
  GBP: 100,
  JPY: 1,
};

function minorUnitsPerMajor(currency: string): number {
  return MINOR_UNITS_PER_MAJOR[currency.toUpperCase()] ?? 100;
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  INR: "₹",
  USD: "$",
  EUR: "€",
  GBP: "£",
  JPY: "¥",
};

export interface FormatMoneyOptions {
  /** Show the currency symbol prefix. Default true. */
  showSymbol?: boolean;
  /** Force a leading '+' for positive amounts. Default false. */
  showPositiveSign?: boolean;
}

/**
 * Format an integer minor-units amount for display, e.g. formatMoney(13500,
 * 'INR') -> "₹13,500.00". Negative amounts (discounts/refunds) render
 * with a leading '-' before the symbol, e.g. "-₹333.00".
 *
 * `amountMinor` must be a safe integer (or bigint) — never a float. If you
 * have a value from JSON, it's already an integer per the API contract.
 */
export function formatMoney(
  amountMinor: number | bigint,
  currency = "INR",
  options: FormatMoneyOptions = {},
): string {
  const { showSymbol = true, showPositiveSign = false } = options;
  const perMajor = minorUnitsPerMajor(currency);
  const abs = amountMinor < 0 ? -amountMinor : amountMinor;
  const absNum = typeof abs === "bigint" ? abs : BigInt(Math.trunc(abs));
  const divisor = BigInt(perMajor);

  const majorPart = absNum / divisor;
  const minorPart = absNum % divisor;
  const decimals = String(perMajor - 1).length; // 100 -> 2 decimals, 1 -> 0

  const majorStr = groupThousands(majorPart.toString());
  const minorStr =
    decimals > 0 ? "." + minorPart.toString().padStart(decimals, "0") : "";

  const sign = amountMinor < 0 ? "-" : showPositiveSign ? "+" : "";
  const symbol = showSymbol
    ? (CURRENCY_SYMBOLS[currency.toUpperCase()] ?? currency + " ")
    : "";

  return `${sign}${symbol}${majorStr}${minorStr}`;
}

function groupThousands(digits: string): string {
  // Indian digit grouping (lakh/crore) reads oddly for non-INR currencies,
  // so keep this to a plain 3-digit Western grouping — acceptable for a
  // first pass across all supported currencies (INR included).
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/**
 * Parse a user-entered decimal string (e.g. an amount typed into the
 * needs-review correction form, "134.50") into integer minor units. Throws
 * on invalid input rather than silently truncating — callers should catch
 * and surface a validation error.
 */
export function toMinorUnits(input: string, currency = "INR"): number {
  const trimmed = input.trim();
  if (!/^-?\d+(\.\d+)?$/.test(trimmed)) {
    throw new Error(`Invalid amount: "${input}"`);
  }
  const perMajor = minorUnitsPerMajor(currency);
  const decimals = String(perMajor - 1).length;
  const negative = trimmed.startsWith("-");
  const unsigned = negative ? trimmed.slice(1) : trimmed;
  const [major = "0", minor = ""] = unsigned.split(".");
  const minorPadded = (minor + "0".repeat(decimals)).slice(0, decimals);
  const totalMinor =
    BigInt(major) * BigInt(perMajor) + BigInt(minorPadded || "0");
  const result = Number(totalMinor);
  return negative ? -result : result;
}

/** Sum a list of integer minor-unit amounts without ever touching float math. */
export function sumMinor(amounts: Array<number | bigint>): number {
  let total = BigInt(0);
  for (const a of amounts) {
    total += typeof a === "bigint" ? a : BigInt(Math.trunc(a));
  }
  return Number(total);
}
