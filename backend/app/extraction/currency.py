"""
Currency string parsing — converts to integer minor units immediately at the
boundary (project invariant #1: money is BIGINT minor units, never float).

Handles:
  - Symbols/prefixes: ₹, Rs., Rs, INR, $, USD
  - Indian comma-grouped numbering: 1,23,456.78 (as well as plain 123,456.78)
  - Parenthesised negatives: (857.00) -> -857.00 (common in ledger exports)
  - Leading minus signs: -857.00

This module is a pure-function utility available to any extraction code path
that needs to parse a currency string independently of the LLM's own paise
conversion (e.g. normalizing vendor-hint text, or a defensive re-parse of a
value the model returned as a string instead of an int).
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_SYMBOL_TOKENS = ("₹", "Rs.", "Rs", "INR", "USD", "$")

_STRIP_NON_NUMERIC_RE = re.compile(r"[^\d.]")

_CURRENCY_MAP = {
    "₹": "INR",
    "Rs.": "INR",
    "Rs": "INR",
    "INR": "INR",
    "$": "USD",
    "USD": "USD",
}


def detect_currency(raw: str, default: str = "INR") -> str:
    """Best-effort currency code detection from a free-text amount string."""
    for token in _SYMBOL_TOKENS:
        if token in raw:
            return _CURRENCY_MAP[token]
    return default


def parse_amount_to_minor(raw: str | int | Decimal) -> int:
    """
    Parse a currency amount into integer minor units (paise/cents).

    Accepts already-numeric input (int/Decimal) as a convenience, but
    the primary contract is parsing strings such as:
      "₹1,23,456.78", "Rs. 857", "INR 40.00", "(50.00)", "-12.5"

    float is deliberately not accepted — callers must not let money pass
    through binary-float representation (project invariant #1).

    Raises:
        ValueError: if no numeric content can be recovered from `raw`.
    """
    if isinstance(raw, int):
        return raw
    if isinstance(raw, Decimal):
        return _to_minor(raw)

    s = raw.strip()
    if not s:
        raise ValueError("Cannot parse amount from empty string")

    # Strip currency symbols/prefixes (order matters: "Rs." before "Rs")
    # before sign detection, so "₹ (857.00)" / "Rs. (50.00)" are recognized.
    for token in _SYMBOL_TOKENS:
        s = s.replace(token, "")
    s = s.strip()

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()

    # Drop thousands separators (including Indian 2-3 grouping) and any
    # remaining non-numeric characters except the decimal point.
    s = _STRIP_NON_NUMERIC_RE.sub("", s)

    if not s or s == ".":
        raise ValueError(f"Cannot parse amount from {raw!r}")

    try:
        dec = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse amount from {raw!r}") from exc

    if negative:
        dec = -dec

    return _to_minor(dec)


def _to_minor(dec: Decimal) -> int:
    minor = (dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(minor)
