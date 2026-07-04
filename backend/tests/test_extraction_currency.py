"""
Tests for app.extraction.currency — parsing ₹/Rs./INR strings and Indian
comma-grouped numbering into integer minor units at the boundary.
"""

from __future__ import annotations

import pytest

from app.extraction.currency import detect_currency, parse_amount_to_minor


@pytest.mark.parametrize(
    "raw,expected_minor",
    [
        ("₹857.00", 85700),
        ("Rs. 200", 20000),
        ("Rs 200.50", 20050),
        ("INR 1,234.56", 123456),
        ("1,23,456.78", 12345678),  # Indian numbering: 1,23,456.78
        ("12,345.00", 1234500),
        ("40", 4000),
        ("-12.50", -1250),
        ("(50.00)", -5000),
        ("₹ (857.00)", -85700),  # symbol + parenthesised negative
        ("Rs. (50.00)", -5000),
        (857, 857),  # already-int passthrough
    ],
)
def test_parse_amount_to_minor(raw: str, expected_minor: int) -> None:
    assert parse_amount_to_minor(raw) == expected_minor


def test_parse_amount_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_amount_to_minor("")


def test_parse_amount_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_amount_to_minor("N/A")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("₹857.00", "INR"),
        ("Rs. 200", "INR"),
        ("INR 1,234", "INR"),
        ("$40.00", "USD"),
        ("USD 12.50", "USD"),
        ("no symbol here", "INR"),  # falls back to default
    ],
)
def test_detect_currency(raw: str, expected: str) -> None:
    assert detect_currency(raw) == expected
