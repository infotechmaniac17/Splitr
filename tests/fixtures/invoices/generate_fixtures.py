"""
Generates the golden-test fixture PDFs for the M3 extraction pipeline.

Run with:
    cd backend && .venv/Scripts/python.exe ../tests/fixtures/invoices/generate_fixtures.py

Each fixture is a synthetic, programmatically generated invoice with a real
selectable text layer (reportlab draws text via drawString, which pdfplumber
can extract) — this is what lets Stage 0 route them down the text path.

The companion `expected_*.json` files are the single source of truth for what
a (simulated) LLM extraction of that fixture should produce; backend tests
load them to build MockProvider responses, so the fixture PDF content and the
golden expected JSON never drift apart silently.
"""

from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

FIXTURES_DIR = Path(__file__).parent


def _draw_invoice(path: Path, title: str, lines: list[str]) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    y = height - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, title)
    y -= 30
    c.setFont("Helvetica", 11)
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.showPage()
    c.save()


# ---------------------------------------------------------------------------
# Fixture 1: Amazon-style — items + tax + delivery fee, all reconciling.
# ---------------------------------------------------------------------------

AMAZON_GOOD = {
    "pdf": "amazon_good.pdf",
    "title": "Amazon.in — Tax Invoice",
    "lines": [
        "Invoice Number: AMZ-2026-0001842",
        "Invoice Date: 2026-06-15",
        "Currency: INR",
        "",
        "Item                              Qty   Unit Price   Total",
        "USB-C Cable 1m                     2      Rs. 299.00   Rs. 598.00",
        "Wireless Mouse                     1      Rs. 799.00   Rs. 799.00",
        "",
        "Subtotal:                                             Rs. 1397.00",
        "GST (Tax):                                            Rs. 70.00",
        "Delivery Fee:                                         Rs. 40.00",
        "Invoice Total:                                        Rs. 1507.00",
    ],
    "expected": {
        "vendor": "Amazon",
        "invoice_date": "2026-06-15",
        "invoice_number": "AMZ-2026-0001842",
        "currency": "INR",
        "line_items": [
            {
                "line_no": 1,
                "kind": "item",
                "description": "USB-C Cable 1m",
                "quantity": "2",
                "unit_price_minor": 29900,
                "total_minor": 59800,
            },
            {
                "line_no": 2,
                "kind": "item",
                "description": "Wireless Mouse",
                "quantity": "1",
                "unit_price_minor": 79900,
                "total_minor": 79900,
            },
            {
                "line_no": 3,
                "kind": "tax",
                "description": "GST",
                "quantity": "1",
                "unit_price_minor": 7000,
                "total_minor": 7000,
            },
            {
                "line_no": 4,
                "kind": "delivery_fee",
                "description": "Delivery Fee",
                "quantity": "1",
                "unit_price_minor": 4000,
                "total_minor": 4000,
            },
        ],
        "invoice_total_minor": 150700,
        "subtotal_minor": 139700,
    },
}


# ---------------------------------------------------------------------------
# Fixture 2: Swiggy-style — items + packing fee + platform fee + discount.
# ---------------------------------------------------------------------------

SWIGGY_GOOD = {
    "pdf": "swiggy_good.pdf",
    "title": "Swiggy — Order Receipt",
    "lines": [
        "Order ID: SWG-99182734",
        "Order Date: 2026-06-20",
        "Currency: INR",
        "",
        "Item                              Qty   Unit Price   Total",
        "Butter Naan                        3      Rs. 45.00    Rs. 135.00",
        "Paneer Tikka                       1      Rs. 249.00   Rs. 249.00",
        "",
        "Item Total:                                           Rs. 384.00",
        "Packing Charges:                                      Rs. 20.00",
        "Platform Fee:                                         Rs. 6.00",
        "Discount:                                            -Rs. 50.00",
        "Grand Total:                                          Rs. 360.00",
    ],
    "expected": {
        "vendor": "Swiggy",
        "invoice_date": "2026-06-20",
        "invoice_number": "SWG-99182734",
        "currency": "INR",
        "line_items": [
            {
                "line_no": 1,
                "kind": "item",
                "description": "Butter Naan",
                "quantity": "3",
                "unit_price_minor": 4500,
                "total_minor": 13500,
            },
            {
                "line_no": 2,
                "kind": "item",
                "description": "Paneer Tikka",
                "quantity": "1",
                "unit_price_minor": 24900,
                "total_minor": 24900,
            },
            {
                "line_no": 3,
                "kind": "packing_fee",
                "description": "Packing Charges",
                "quantity": "1",
                "unit_price_minor": 2000,
                "total_minor": 2000,
            },
            {
                "line_no": 4,
                "kind": "platform_fee",
                "description": "Platform Fee",
                "quantity": "1",
                "unit_price_minor": 600,
                "total_minor": 600,
            },
            {
                "line_no": 5,
                "kind": "discount",
                "description": "Discount",
                "quantity": "1",
                "unit_price_minor": -5000,
                "total_minor": -5000,
            },
        ],
        "invoice_total_minor": 36000,
        "subtotal_minor": 38400,
    },
}


# ---------------------------------------------------------------------------
# Fixture 3: Zepto-style — deliberately broken: the (simulated) LLM omits the
# delivery fee row, so items sum to 16500 but the printed total is 19000.
# Used to prove the retry-with-mismatch path still lands in 'needs_review'
# when the model keeps dropping the same row.
# ---------------------------------------------------------------------------

ZEPTO_BROKEN = {
    "pdf": "zepto_broken.pdf",
    "title": "Zepto — Order Invoice",
    "lines": [
        "Order ID: ZPT-55021",
        "Order Date: 2026-06-25",
        "Currency: INR",
        "",
        "Item                              Qty   Unit Price   Total",
        "Milk 1L                            2      Rs. 60.00    Rs. 120.00",
        "Bread                              1      Rs. 45.00    Rs. 45.00",
        "",
        "Item Total:                                           Rs. 165.00",
        "Delivery Fee:                                         Rs. 25.00",
        "Grand Total:                                          Rs. 190.00",
    ],
    # "expected" here models what the flawed LLM extraction returns (missing
    # the delivery_fee line) on BOTH attempts — the mismatch retry does not
    # fix a persistent omission, so the expense must land in needs_review.
    "expected_broken": {
        "vendor": "Zepto",
        "invoice_date": "2026-06-25",
        "invoice_number": "ZPT-55021",
        "currency": "INR",
        "line_items": [
            {
                "line_no": 1,
                "kind": "item",
                "description": "Milk 1L",
                "quantity": "2",
                "unit_price_minor": 6000,
                "total_minor": 12000,
            },
            {
                "line_no": 2,
                "kind": "item",
                "description": "Bread",
                "quantity": "1",
                "unit_price_minor": 4500,
                "total_minor": 4500,
            },
        ],
        # Model states the correct printed total, but its line items don't
        # reconcile to it (missing delivery_fee row) — exactly the failure
        # mode ARCHITECTURE.md §2.2 describes.
        "invoice_total_minor": 19000,
        "subtotal_minor": 16500,
    },
}

ALL_FIXTURES = [AMAZON_GOOD, SWIGGY_GOOD, ZEPTO_BROKEN]


def main() -> None:
    for fixture in ALL_FIXTURES:
        pdf_path = FIXTURES_DIR / fixture["pdf"]
        _draw_invoice(pdf_path, fixture["title"], fixture["lines"])
        print(f"wrote {pdf_path}")

        expected_key = "expected" if "expected" in fixture else "expected_broken"
        json_name = fixture["pdf"].replace(".pdf", ".expected.json")
        json_path = FIXTURES_DIR / json_name
        json_path.write_text(json.dumps(fixture[expected_key], indent=2) + "\n")
        print(f"wrote {json_path}")


if __name__ == "__main__":
    main()
