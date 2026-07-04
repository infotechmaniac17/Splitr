"""
Tests for app.extraction.router — Stage 0 text-layer detection
(ARCHITECTURE.md §2.2).
"""

from __future__ import annotations

import io
import time
from pathlib import Path

from app.extraction.router import has_text_layer, route

FIXTURES_DIR = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "invoices"


def test_fixtures_dir_exists() -> None:
    assert FIXTURES_DIR.is_dir(), f"expected fixtures at {FIXTURES_DIR}"


def test_route_detects_text_layer_on_synthetic_invoices() -> None:
    for name in ("amazon_good.pdf", "swiggy_good.pdf", "zepto_broken.pdf"):
        pdf_path = FIXTURES_DIR / name
        assert pdf_path.is_file(), f"missing fixture {pdf_path}"
        assert route(pdf_path) == "text"


def test_route_accepts_bytes_source() -> None:
    pdf_bytes = (FIXTURES_DIR / "amazon_good.pdf").read_bytes()
    assert route(pdf_bytes) == "text"


def test_route_accepts_bytesio_source() -> None:
    pdf_bytes = (FIXTURES_DIR / "amazon_good.pdf").read_bytes()
    assert route(io.BytesIO(pdf_bytes)) == "text"


def test_no_text_layer_routes_to_vision() -> None:
    # A blank/near-empty PDF (via reportlab with no drawString calls) has no
    # meaningful text layer and must route to vision.
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.showPage()
    c.save()
    buf.seek(0)
    assert route(buf) == "vision"


def test_stage0_is_fast() -> None:
    """
    ARCHITECTURE.md §2.2 targets <100ms for Stage 0. We assert a generous
    500ms ceiling here (CI machines vary) so this test catches gross
    regressions without being flaky on slow runners.
    """
    pdf_path = FIXTURES_DIR / "amazon_good.pdf"
    start = time.perf_counter()
    has_text_layer(pdf_path)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 500
