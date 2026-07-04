"""
Vendor auto-detection + few-shot prompt injection (ARCHITECTURE.md §2.2,
"vendor hint system"): sniffs pdfplumber-extracted invoice text for known
vendor identifiers so the extraction prompt can be enriched with
vendor-specific few-shot examples even when the user did not supply a
`vendor_hint`.

Detection is deliberately conservative: it only fires on distinctive strings
(brand names, registered legal entity names, own domains) that are very
unlikely to appear in an invoice from a different vendor, and it refuses to
pick a winner if the text matches more than one vendor. An incorrect hint
injected into the prompt is worse than no hint at all — it can bias the LLM
toward the wrong line-item shape (e.g. Swiggy's platform_fee row on an
Amazon invoice) — so ambiguous or generic text returns None rather than
guessing (task rule: "never guess wrong").

Image-only PDFs have no pre-extraction text to sniff (that is the whole
reason Stage 0 routes them to the vision path) — auto-detection is
intentionally NOT run there; the vision path relies solely on a
user-supplied vendor_hint. Running OCR just to sniff a vendor name would
duplicate the vision LLM's own job for near-zero benefit, so that is out of
scope here (ARCHITECTURE.md §2.2 vision branch).
"""

from __future__ import annotations

import re

# Known vendors we ship few-shot examples for (see FEW_SHOT_EXAMPLES below).
# Each pattern list is deliberately narrow: brand name + registered legal
# entity name + own domain — not generic words ("delivery", "order") that
# could appear on any invoice.
_VENDOR_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "Swiggy": [
        re.compile(r"\bswiggy\b", re.IGNORECASE),
        re.compile(r"bundl\s+technologies", re.IGNORECASE),
        re.compile(r"swiggy\.com", re.IGNORECASE),
    ],
    "Zomato": [
        re.compile(r"\bzomato\b", re.IGNORECASE),
        re.compile(r"zomato\s+media", re.IGNORECASE),
        re.compile(r"zomato\.com", re.IGNORECASE),
    ],
    "Amazon": [
        re.compile(r"\bamazon\b", re.IGNORECASE),
        re.compile(r"amazon\s+seller\s+services", re.IGNORECASE),
        re.compile(r"clicktech\s+retail", re.IGNORECASE),
        re.compile(r"amazon\.in", re.IGNORECASE),
    ],
    "Zepto": [
        re.compile(r"\bzepto\b", re.IGNORECASE),
        re.compile(r"kiranakart", re.IGNORECASE),
    ],
    "Blinkit": [
        re.compile(r"\bblinkit\b", re.IGNORECASE),
        re.compile(r"blink\s+commerce", re.IGNORECASE),
        re.compile(r"grofers", re.IGNORECASE),
    ],
}

# Matches standard 15-character GSTIN tokens (2-digit state code + 10-char
# PAN + entity code + checksum). Surfaced for potential future use, but
# deliberately NOT mapped to a vendor here: a single vendor operates many
# state-specific GSTINs under one or more legal entities that change over
# time, so a hardcoded GSTIN->vendor table would be either incomplete or
# wrong. Guessing wrong is explicitly worse than no hint (see module
# docstring), so GSTIN matching stays informational-only until backed by a
# maintained registry.
GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z0-9]\b")


def detect_vendor(text: str | None) -> str | None:
    """
    Best-effort vendor detection from pre-LLM extracted invoice text
    (pdfplumber output, text path only). Returns one of "Swiggy", "Zomato",
    "Amazon", "Zepto", "Blinkit", or None if no confident match is found.

    Conservative by design: only fires if *exactly one* known vendor's
    patterns match. If the text matches more than one vendor's markers
    (e.g. an ambiguous forwarded email, or a generic invoice mentioning
    multiple brands), we treat that as low confidence and return None
    rather than pick one arbitrarily.
    """
    if not text:
        return None

    matches = [
        vendor
        for vendor, patterns in _VENDOR_PATTERNS.items()
        if any(p.search(text) for p in patterns)
    ]

    if len(matches) == 1:
        return matches[0]
    return None


def resolve_vendor(vendor_hint: str | None, text: str | None) -> str | None:
    """
    Resolve the vendor to use for prompt-building: an explicit
    user-supplied `vendor_hint` always wins over auto-detection.
    Auto-detection only fills in when the caller supplied no hint at all.
    """
    if vendor_hint:
        return vendor_hint
    return detect_vendor(text)


# ---------------------------------------------------------------------------
# Few-shot examples, keyed by canonical vendor name.
#
# Each block is 2-3 short illustrative line-item JSON snippets covering that
# vendor's well-known invoice quirks, to steer the LLM toward correctly
# classifying fee/discount rows it might otherwise miss or mis-kind. These
# are illustrative shape examples only — never copied verbatim into a real
# extraction, and never treated as ground truth by the validation engine
# (which remains the only source of arithmetic trust, per ARCHITECTURE.md
# §2.2).
# ---------------------------------------------------------------------------

FEW_SHOT_EXAMPLES: dict[str, str] = {
    "Swiggy": (
        '{"line_no": 1, "kind": "item", "description": "Chicken Biryani", '
        '"quantity": "1", "unit_price_minor": 22000, "total_minor": 22000}\n'
        '{"line_no": 2, "kind": "platform_fee", "description": "Platform fee", '
        '"quantity": "1", "unit_price_minor": 500, "total_minor": 500}\n'
        '{"line_no": 3, "kind": "packing_fee", "description": '
        '"Restaurant packaging charges", "quantity": "1", '
        '"unit_price_minor": 2000, "total_minor": 2000}'
    ),
    "Zomato": (
        '{"line_no": 1, "kind": "item", "description": "Paneer Butter Masala", '
        '"quantity": "1", "unit_price_minor": 28000, "total_minor": 28000}\n'
        '{"line_no": 2, "kind": "platform_fee", "description": "Zomato Platform fee", '
        '"quantity": "1", "unit_price_minor": 600, "total_minor": 600}\n'
        '{"line_no": 3, "kind": "delivery_fee", "description": "Delivery Partner Fee", '
        '"quantity": "1", "unit_price_minor": 4000, "total_minor": 4000}'
    ),
    "Amazon": (
        '{"line_no": 1, "kind": "item", "description": '
        '"boAt Airdopes 141 (MRP Rs. 2999.00, you paid Rs. 1299.00)", '
        '"quantity": "1", "unit_price_minor": 129900, "total_minor": 129900}\n'
        '{"line_no": 2, "kind": "discount", "description": "Promotion Applied", '
        '"quantity": "1", "unit_price_minor": -20000, "total_minor": -20000}\n'
        "# Note: Amazon invoices show MRP struck through next to the actual "
        "selling price — always extract the SELLING price paid, not the MRP."
    ),
    "Zepto": (
        '{"line_no": 1, "kind": "item", "description": "Amul Milk 500ml", '
        '"quantity": "2", "unit_price_minor": 2700, "total_minor": 5400}\n'
        '{"line_no": 2, "kind": "delivery_fee", "description": "Delivery Fee", '
        '"quantity": "1", "unit_price_minor": 1500, "total_minor": 1500}\n'
        '{"line_no": 3, "kind": "platform_fee", "description": "Handling Charge", '
        '"quantity": "1", "unit_price_minor": 900, "total_minor": 900}'
    ),
    "Blinkit": (
        '{"line_no": 1, "kind": "item", "description": "Britannia Bread 400g", '
        '"quantity": "1", "unit_price_minor": 4500, "total_minor": 4500}\n'
        '{"line_no": 2, "kind": "delivery_fee", "description": "Delivery Partner Fee", '
        '"quantity": "1", "unit_price_minor": 2500, "total_minor": 2500}\n'
        '{"line_no": 3, "kind": "platform_fee", "description": "Handling Fee", '
        '"quantity": "1", "unit_price_minor": 700, "total_minor": 700}'
    ),
}


def build_few_shot_block(vendor: str | None) -> str | None:
    """
    Return the vendor-specific few-shot example block for prompt injection,
    or None if `vendor` is unset or doesn't match a known vendor. Matching
    is case-insensitive against the canonical names above so a user-typed
    hint like "swiggy" or "SWIGGY" still matches.
    """
    if not vendor:
        return None
    normalized = vendor.strip().lower()
    for canonical, example in FEW_SHOT_EXAMPLES.items():
        if canonical.lower() == normalized:
            return (
                f"Known vendor quirks for {canonical} — illustrative example "
                "line items (JSON Lines shape only; these are NOT this "
                "invoice's real data, do not copy the values):\n" + example
            )
    return None
