"""
Tests for app.extraction.vendor_detect (M6 — vendor auto-detection +
few-shot prompt injection, ARCHITECTURE.md §2.2 "vendor hint system").
"""

from __future__ import annotations

from app.extraction.pipeline import _build_request
from app.extraction.vendor_detect import (
    build_few_shot_block,
    detect_vendor,
    resolve_vendor,
)
from app.extraction.vision_path import build_vision_prompt

# ---------------------------------------------------------------------------
# detect_vendor
# ---------------------------------------------------------------------------


def test_detect_vendor_matches_swiggy() -> None:
    text = "Order Summary\nBundl Technologies Pvt Ltd\nswiggy.com\nGSTIN: 29AAECB1234F1Z5"
    assert detect_vendor(text) == "Swiggy"


def test_detect_vendor_matches_zomato() -> None:
    text = "Thank you for ordering with Zomato!\nZomato Media Pvt Ltd"
    assert detect_vendor(text) == "Zomato"


def test_detect_vendor_matches_amazon() -> None:
    text = "Tax Invoice\nSold by: Clicktech Retail Private Limited\nwww.amazon.in"
    assert detect_vendor(text) == "Amazon"


def test_detect_vendor_matches_zepto() -> None:
    text = "Zepto order confirmation\nKiranaKart Technologies Pvt Ltd"
    assert detect_vendor(text) == "Zepto"


def test_detect_vendor_matches_blinkit() -> None:
    text = "Your Blinkit order has been delivered\nBlink Commerce Pvt Ltd"
    assert detect_vendor(text) == "Blinkit"


def test_detect_vendor_no_false_positive_on_generic_invoice() -> None:
    text = (
        "INVOICE\nAcme Traders Pvt Ltd\nInvoice #1234\n"
        "Item: Widget A  Qty: 2  Rate: 100.00  Amount: 200.00\n"
        "Grand Total: Rs. 200.00"
    )
    assert detect_vendor(text) is None


def test_detect_vendor_returns_none_on_empty_or_missing_text() -> None:
    assert detect_vendor("") is None
    assert detect_vendor(None) is None


def test_detect_vendor_returns_none_on_ambiguous_multi_vendor_text() -> None:
    # Forwarded confirmation mentioning two brands — refuse to guess.
    text = "Your Swiggy order was cancelled; refund processed via Zomato Pay."
    assert detect_vendor(text) is None


# ---------------------------------------------------------------------------
# resolve_vendor — explicit hint always wins over auto-detection
# ---------------------------------------------------------------------------


def test_resolve_vendor_prefers_explicit_hint_over_detection() -> None:
    text = "Order Summary\nBundl Technologies Pvt Ltd\nswiggy.com"
    assert resolve_vendor("Amazon", text) == "Amazon"


def test_resolve_vendor_falls_back_to_detection_when_no_hint() -> None:
    text = "Order Summary\nBundl Technologies Pvt Ltd\nswiggy.com"
    assert resolve_vendor(None, text) == "Swiggy"


def test_resolve_vendor_returns_none_when_no_hint_and_no_match() -> None:
    assert resolve_vendor(None, "Generic Invoice, Acme Traders") is None
    assert resolve_vendor(None, None) is None


def test_resolve_vendor_returns_none_when_hint_is_empty_string() -> None:
    # Falsy hint ("") must be treated like "no hint given", not a real hint.
    text = "Order Summary\nBundl Technologies Pvt Ltd\nswiggy.com"
    assert resolve_vendor("", text) == "Swiggy"


# ---------------------------------------------------------------------------
# build_few_shot_block
# ---------------------------------------------------------------------------


def test_few_shot_block_present_for_known_vendor() -> None:
    block = build_few_shot_block("Swiggy")
    assert block is not None
    assert "platform_fee" in block
    assert "packing_fee" in block


def test_few_shot_block_matching_is_case_insensitive() -> None:
    assert build_few_shot_block("swiggy") == build_few_shot_block("Swiggy")
    assert build_few_shot_block("AMAZON") == build_few_shot_block("Amazon")


def test_few_shot_block_none_for_unknown_vendor() -> None:
    assert build_few_shot_block("Some Random Vendor") is None
    assert build_few_shot_block(None) is None
    assert build_few_shot_block("") is None


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def test_text_prompt_contains_few_shot_block_when_vendor_hinted(tmp_path) -> None:
    from app.extraction.text_path import build_text_prompt

    content = {"text": "Order Summary\nfoo bar", "tables": []}
    prompt = build_text_prompt(content, vendor_hint="Zepto")
    assert "Vendor hint: Zepto." in prompt
    assert "Handling Charge" in prompt  # Zepto few-shot marker


def test_text_prompt_has_no_few_shot_block_without_vendor_hint() -> None:
    from app.extraction.text_path import build_text_prompt

    content = {"text": "Order Summary\nfoo bar", "tables": []}
    prompt = build_text_prompt(content, vendor_hint=None)
    assert "Vendor hint" not in prompt
    assert "Known vendor quirks" not in prompt


def test_vision_prompt_contains_few_shot_block_when_vendor_hinted() -> None:
    prompt = build_vision_prompt(vendor_hint="Blinkit")
    assert "Vendor hint: Blinkit." in prompt
    assert "Handling Fee" in prompt  # Blinkit few-shot marker


def test_vision_prompt_only_uses_explicit_hint_never_autodetects() -> None:
    # No text layer available to the vision path at all — passing no hint
    # must never surface a vendor few-shot block, regardless of content.
    prompt = build_vision_prompt(vendor_hint=None)
    assert "Vendor hint" not in prompt
    assert "Known vendor quirks" not in prompt


# ---------------------------------------------------------------------------
# Pipeline wiring — auto-detection fills in only when no explicit hint given
# ---------------------------------------------------------------------------


def test_build_request_auto_detects_vendor_for_text_route_when_no_hint(tmp_path) -> None:
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "swiggy.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(72, 720, "Order Summary")
    c.drawString(72, 700, "Bundl Technologies Pvt Ltd")
    c.drawString(72, 680, "swiggy.com")
    c.drawString(72, 660, "Item: Chicken Biryani  Qty: 1  Amount: 220.00")
    c.save()

    request = _build_request(pdf_path, "text", schema={}, vendor_hint=None, retry_context=None)
    assert request.vendor_hint == "Swiggy"
    assert "Known vendor quirks for Swiggy" in request.text


def test_build_request_explicit_hint_overrides_autodetection(tmp_path) -> None:
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "swiggy.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(72, 720, "Order Summary")
    c.drawString(72, 700, "Bundl Technologies Pvt Ltd")
    c.drawString(72, 680, "swiggy.com")
    c.save()

    request = _build_request(
        pdf_path, "text", schema={}, vendor_hint="Amazon", retry_context=None
    )
    assert request.vendor_hint == "Amazon"
    assert "Known vendor quirks for Amazon" in request.text
