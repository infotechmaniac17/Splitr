"""
Largest-remainder rounding for proportional allocation of money amounts.

This is the single implementation of ARCHITECTURE.md §4 rounding logic.
It is a pure function with no I/O and is reused by every splitting path
(M1 equal-split, M2 proportional fees/discounts, etc.).

Key guarantee: allocate_largest_remainder(amount, ratios) always returns
shares that sum EXACTLY to `amount`, regardless of rounding.

For negative amounts (discounts, refunds), the same algorithm applies:
trunc-toward-zero floors are used, and the residual is distributed to
the participants with the largest absolute fractional remainders first.

Worked example (ARCHITECTURE.md §4):
    allocate_largest_remainder(-1000, {A: Fraction(1,3), B: Fraction(2,3)})
    → {A: -333, B: -667}   (sums exactly to -1000)
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from math import trunc
from typing import TypeVar

K = TypeVar("K")


def percent_of_minor(subtotal_minor: int, percent: Decimal) -> int:
    """
    Single-value (non-multi-way) rounding of `subtotal_minor * percent / 100`
    to the nearest minor unit, using round-half-even.

    This is deliberately NOT `allocate_largest_remainder`: that function
    distributes a fixed total across several *parties* so the parts sum
    exactly to the total. This helper instead rounds a single derived
    amount (e.g. "18% of 12345 paise") with no such multi-way reconciliation
    to satisfy. Round-half-even is the standard, deterministic tie-break for
    this shape of computation (see app/domain/vendor_discount.py's original
    docstring, which is where this convention was first established for
    percent-based vendor discounts and now also backs percent-based
    discount/GST math in app/domain/splitting.py). Extracted here so both
    modules share exactly one rounding rule instead of two copies that could
    silently drift apart.

    Does not cap the result against `subtotal_minor` -- callers apply their
    own caps (e.g. "a discount can never exceed the subtotal it discounts").
    """
    exact = Decimal(subtotal_minor) * percent / Decimal(100)
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def allocate_largest_remainder(
    amount: int,
    ratios: dict[K, Fraction],
) -> dict[K, int]:
    """
    Distribute `amount` (integer minor units) among keys in `ratios`.

    Args:
        amount:  The total to distribute (positive, negative, or zero).
        ratios:  Mapping from participant key to their fraction of the total.
                 Values should sum to 1 (enforced by caller; this function
                 distributes whatever residual exists).

    Returns:
        Dict mapping each key to an integer share.  The shares sum exactly
        to `amount`.

    Raises:
        ValueError: if `ratios` is empty but `amount` is non-zero.
    """
    if not ratios:
        if amount != 0:
            raise ValueError("Cannot allocate non-zero amount to empty ratio set")
        return {}

    if amount == 0:
        return {u: 0 for u in ratios}

    # Exact (Fraction) allocations — no floating-point error.
    exact: dict[K, Fraction] = {u: Fraction(amount) * r for u, r in ratios.items()}

    # Floor toward zero (matches ARCHITECTURE.md pseudocode trunc_toward_zero).
    floors: dict[K, int] = {u: trunc(v) for u, v in exact.items()}

    residual: int = amount - sum(floors.values())

    if residual != 0:
        sign = 1 if residual > 0 else -1
        # Sort by absolute fractional remainder descending so the participant
        # with the largest truncation error gets the extra unit first.
        # abs(exact - floor) is always >= 0 regardless of sign of amount.
        for u in sorted(ratios, key=lambda u: abs(exact[u] - floors[u]), reverse=True):
            if residual == 0:
                break
            floors[u] += sign
            residual -= sign

    # Hard invariant — must always hold.
    assert sum(floors.values()) == amount, (
        f"allocate_largest_remainder violated sum invariant: "
        f"got {sum(floors.values())}, expected {amount}"
    )

    return floors
