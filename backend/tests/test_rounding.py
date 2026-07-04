"""
Tests for app.domain.rounding.allocate_largest_remainder.

Covers:
  - Deterministic worked examples (ARCHITECTURE.md §4)
  - Hypothesis property tests: sum always equals amount
  - Edge cases: zero amount, single participant, exact division
"""

from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.rounding import allocate_largest_remainder

# ---------------------------------------------------------------------------
# Deterministic examples from ARCHITECTURE.md §4
# ---------------------------------------------------------------------------


def test_architecture_doc_example_discount() -> None:
    """
    Cart discount -1000, split ⅓ / ⅔.
    Expected: A=-333, B=-667  (sums exactly to -1000).
    """
    result = allocate_largest_remainder(
        -1000,
        {
            "A": Fraction(1, 3),
            "B": Fraction(2, 3),
        },
    )
    assert result["A"] == -333
    assert result["B"] == -667
    assert sum(result.values()) == -1000


def test_architecture_doc_example_fee_proportional() -> None:
    """
    Delivery fee 300 (₹3), split ⅓ / ⅔.
    Expected: A=100, B=200  (exact — no rounding needed).
    """
    result = allocate_largest_remainder(
        300,
        {
            "A": Fraction(1, 3),
            "B": Fraction(2, 3),
        },
    )
    assert result["A"] == 100
    assert result["B"] == 200
    assert sum(result.values()) == 300


def test_architecture_doc_example_fee_equal() -> None:
    """
    Delivery fee 300, equal split between 2.
    Expected: A=150, B=150.
    """
    result = allocate_largest_remainder(
        300,
        {
            "A": Fraction(1, 2),
            "B": Fraction(1, 2),
        },
    )
    assert result["A"] == 150
    assert result["B"] == 150
    assert sum(result.values()) == 300


def test_non_divisible_three_way() -> None:
    """
    53 paise split equally among 3 — can't be divided evenly.
    Two get 18, one gets 17, sum == 53.
    """
    result = allocate_largest_remainder(
        53,
        {i: Fraction(1, 3) for i in range(3)},
    )
    assert sum(result.values()) == 53
    values = sorted(result.values())
    assert values.count(18) == 2
    assert values.count(17) == 1


def test_single_participant() -> None:
    """Single participant gets the whole amount."""
    result = allocate_largest_remainder(1000, {"A": Fraction(1)})
    assert result == {"A": 1000}


def test_zero_amount() -> None:
    """Zero amount → all shares are zero."""
    result = allocate_largest_remainder(
        0,
        {"A": Fraction(1, 2), "B": Fraction(1, 2)},
    )
    assert result == {"A": 0, "B": 0}


def test_empty_ratios_zero_amount() -> None:
    """Empty ratios with zero amount returns empty dict."""
    result = allocate_largest_remainder(0, {})
    assert result == {}


def test_empty_ratios_nonzero_amount_raises() -> None:
    """Empty ratios with non-zero amount should raise."""
    with pytest.raises(ValueError, match="empty ratio set"):
        allocate_largest_remainder(100, {})


def test_exact_division() -> None:
    """When amount divides exactly, no largest-remainder adjustment needed."""
    result = allocate_largest_remainder(
        100,
        {"A": Fraction(1, 4), "B": Fraction(3, 4)},
    )
    assert result["A"] == 25
    assert result["B"] == 75
    assert sum(result.values()) == 100


def test_large_amount_many_participants() -> None:
    """Large amount with many participants always reconciles."""
    n = 7
    amount = 10000
    result = allocate_largest_remainder(
        amount,
        {i: Fraction(1, n) for i in range(n)},
    )
    assert sum(result.values()) == amount


def test_negative_large_amount() -> None:
    """Large negative amount still reconciles."""
    amount = -99999
    result = allocate_largest_remainder(
        amount,
        {i: Fraction(1, 9) for i in range(9)},
    )
    assert sum(result.values()) == amount


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------


@given(
    amount=st.integers(min_value=-10_000_000, max_value=10_000_000),
    n_users=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=500)
def test_equal_split_always_reconciles(amount: int, n_users: int) -> None:
    """Property: equal split always sums exactly to amount."""
    ratios: dict[int, Fraction] = {i: Fraction(1, n_users) for i in range(n_users)}
    result = allocate_largest_remainder(amount, ratios)
    assert sum(result.values()) == amount


@given(
    amount=st.integers(min_value=-10_000_000, max_value=10_000_000),
    weights=st.lists(
        st.integers(min_value=1, max_value=10_000),
        min_size=1,
        max_size=15,
    ),
)
@settings(max_examples=500)
def test_weighted_split_always_reconciles(amount: int, weights: list[int]) -> None:
    """Property: weighted split with Fraction ratios always sums exactly to amount."""
    total_w = sum(weights)
    ratios: dict[int, Fraction] = {
        i: Fraction(w, total_w) for i, w in enumerate(weights)
    }
    result = allocate_largest_remainder(amount, ratios)
    assert sum(result.values()) == amount


@given(
    amount=st.integers(min_value=1, max_value=10_000_000),
    weights=st.lists(
        st.integers(min_value=1, max_value=10_000),
        min_size=2,
        max_size=10,
    ),
)
@settings(max_examples=300)
def test_all_shares_non_negative_for_positive_amount(
    amount: int, weights: list[int]
) -> None:
    """
    Property: for a positive amount with positive weights, all shares are >= 0.
    (Shares can be 0 only if a participant's ratio rounds down to 0.)
    """
    total_w = sum(weights)
    ratios: dict[int, Fraction] = {
        i: Fraction(w, total_w) for i, w in enumerate(weights)
    }
    result = allocate_largest_remainder(amount, ratios)
    assert all(v >= 0 for v in result.values())
    assert sum(result.values()) == amount
