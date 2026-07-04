"""
Tests for app.extraction.validation — the deterministic (pure Python, no AI)
validation engine that is the SOLE gate for parse_status='parsed'
(ARCHITECTURE.md §2.2, project invariant #4).
"""

from __future__ import annotations

from decimal import Decimal

from app.extraction.schema import ExtractedInvoice, ExtractedLineItem
from app.extraction.validation import parse_date, validate_extraction


def _line(**kwargs) -> ExtractedLineItem:
    defaults = {
        "line_no": 1,
        "kind": "item",
        "description": "Widget",
        "quantity": Decimal("1"),
        "unit_price_minor": 1000,
        "total_minor": 1000,
    }
    defaults.update(kwargs)
    return ExtractedLineItem.model_validate(defaults)


def _invoice(lines: list[ExtractedLineItem], **kwargs) -> ExtractedInvoice:
    defaults = {
        "vendor": "Test Vendor",
        "invoice_date": "2026-06-15",
        "invoice_number": "INV-1",
        "currency": "INR",
        "line_items": lines,
        "invoice_total_minor": sum(li.total_minor for li in lines),
        "subtotal_minor": None,
    }
    defaults.update(kwargs)
    return ExtractedInvoice.model_validate(defaults)


def test_valid_invoice_passes() -> None:
    lines = [
        _line(line_no=1, total_minor=1000, unit_price_minor=500, quantity=Decimal("2")),
        _line(line_no=2, kind="tax", total_minor=100, unit_price_minor=100, quantity=Decimal("1")),
    ]
    invoice = _invoice(lines)
    result = validate_extraction(invoice)
    assert result.ok
    assert result.issues == []


def test_line_arithmetic_mismatch_fails() -> None:
    # 2 x 500 = 1000, but total_minor claims 1200.
    line = _line(quantity=Decimal("2"), unit_price_minor=500, total_minor=1200)
    invoice = _invoice([line], invoice_total_minor=1200)
    result = validate_extraction(invoice)
    assert not result.ok
    assert any(i.code == "line_arithmetic" for i in result.issues)


def test_invoice_total_mismatch_fails_and_message_matches_architecture_example() -> None:
    lines = [_line(total_minor=842, unit_price_minor=None)]
    invoice = _invoice(lines, invoice_total_minor=857)
    result = validate_extraction(invoice)
    assert not result.ok
    issue = next(i for i in result.issues if i.code == "invoice_total_mismatch")
    assert "842" in issue.message
    assert "857" in issue.message
    prompt = result.mismatch_prompt()
    assert "842" in prompt and "857" in prompt


def test_within_tolerance_passes() -> None:
    # off by exactly 1 minor unit — within tolerance.
    lines = [_line(total_minor=999, unit_price_minor=None)]
    invoice = _invoice(lines, invoice_total_minor=1000)
    result = validate_extraction(invoice)
    assert result.ok


def test_negative_quantity_fails() -> None:
    line = _line(quantity=Decimal("-1"))
    invoice = _invoice([line])
    result = validate_extraction(invoice)
    assert not result.ok
    assert any(i.code == "bad_quantity" for i in result.issues)


def test_zero_quantity_fails() -> None:
    line = _line(quantity=Decimal("0"))
    invoice = _invoice([line])
    result = validate_extraction(invoice)
    assert not result.ok
    assert any(i.code == "bad_quantity" for i in result.issues)


def test_negative_unit_price_on_item_fails() -> None:
    line = _line(kind="item", unit_price_minor=-500, total_minor=-500)
    invoice = _invoice([line])
    result = validate_extraction(invoice)
    assert not result.ok
    assert any(i.code == "negative_unit_price" for i in result.issues)


def test_negative_unit_price_allowed_for_discount() -> None:
    line = _line(kind="discount", unit_price_minor=-500, total_minor=-500)
    invoice = _invoice([line])
    result = validate_extraction(invoice)
    assert result.ok


def test_discount_with_positive_total_fails_sign_convention() -> None:
    line = _line(kind="discount", unit_price_minor=-500, total_minor=500)
    invoice = _invoice([line], invoice_total_minor=500)
    result = validate_extraction(invoice)
    assert not result.ok
    assert any(i.code == "sign_convention" for i in result.issues)


def test_item_with_negative_total_fails_sign_convention() -> None:
    line = _line(kind="item", unit_price_minor=None, total_minor=-500)
    invoice = _invoice([line], invoice_total_minor=-500)
    result = validate_extraction(invoice)
    assert not result.ok
    assert any(i.code == "sign_convention" for i in result.issues)


def test_no_line_items_fails() -> None:
    invoice = _invoice([], invoice_total_minor=0)
    result = validate_extraction(invoice)
    assert not result.ok
    assert any(i.code == "no_line_items" for i in result.issues)


def test_unrecognized_currency_fails() -> None:
    line = _line()
    invoice = _invoice([line], currency="ZZZ")
    result = validate_extraction(invoice)
    assert not result.ok
    assert any(i.code == "currency_unrecognized" for i in result.issues)


def test_unparseable_date_fails() -> None:
    line = _line()
    invoice = _invoice([line], invoice_date="not-a-date")
    result = validate_extraction(invoice)
    assert not result.ok
    assert any(i.code == "bad_date" for i in result.issues)


def test_missing_date_is_not_an_error() -> None:
    line = _line()
    invoice = _invoice([line], invoice_date=None)
    result = validate_extraction(invoice)
    assert result.ok


def test_parse_date_formats() -> None:
    assert parse_date("2026-06-15") is not None
    assert parse_date("15-06-2026") is not None
    assert parse_date("15/06/2026") is not None
    assert parse_date(None) is None
    assert parse_date("garbage") is None
