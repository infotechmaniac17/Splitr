"""
Golden tests for the full M3 extraction pipeline (ARCHITECTURE.md §2.2):
    Stage 0 route -> text/vision path -> LLM -> deterministic validation
    -> retry-with-mismatch -> parse_status='parsed' | 'needs_review'

The LLM call is mocked (no real GEMINI_API_KEY/OPENAI_API_KEY is configured
in this environment) via a MockProvider test double that implements the same
ExtractionProvider interface production code depends on — swapping in a real
provider later requires no pipeline changes.

Fixtures live at tests/fixtures/invoices/ (repo root) with companion
`<name>.expected.json` files as the single source of truth for what a
correct/deliberately-flawed extraction of that PDF looks like.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.models import Expense, ExpenseLineItem, ExpenseSource, ParseStatus, User
from app.extraction.pipeline import run_extraction_pipeline
from app.extraction.providers.base import (
    ExtractionProvider,
    ExtractionRequest,
    ExtractionResult,
)
from app.extraction.tasks import _persist_pipeline_result

FIXTURES_DIR = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "invoices"


def _load_expected(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


class ScriptedProvider(ExtractionProvider):
    """
    Test double: returns a pre-scripted JSON payload per call, in order.
    Mirrors exactly what the pipeline expects of a real ExtractionProvider —
    it is only ever driven through the ExtractionProvider interface.
    """

    name = "scripted"

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[ExtractionRequest] = []

    def is_configured(self) -> bool:
        return True

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls.append(request)
        if not self._responses:
            return ExtractionResult(provider=self.name, raw=None, error="no more scripted responses")
        raw = self._responses.pop(0)
        return ExtractionResult(provider=self.name, raw=raw)


class ErroringProvider(ExtractionProvider):
    """Simulates a configured-but-failing provider (e.g. network error)."""

    name = "erroring"

    def is_configured(self) -> bool:
        return True

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        return ExtractionResult(provider=self.name, raw=None, error="simulated network failure")


# ---------------------------------------------------------------------------
# Golden happy-path fixtures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amazon_good_fixture_routes_text_and_parses() -> None:
    expected = _load_expected("amazon_good.expected.json")
    provider = ScriptedProvider([expected])

    result = await run_extraction_pipeline(
        FIXTURES_DIR / "amazon_good.pdf", provider, vendor_hint="Amazon"
    )

    assert result.route == "text"
    assert result.parse_status == ParseStatus.parsed
    assert result.validation is not None and result.validation.ok
    assert result.invoice is not None
    assert result.invoice.vendor == "Amazon"
    assert result.invoice.invoice_total_minor == 150700
    assert len(provider.calls) == 1  # no retry needed
    # raw_extraction persists the model output verbatim, never mutated after.
    assert result.raw_extraction["attempts"][0]["raw"] == expected


@pytest.mark.asyncio
async def test_swiggy_good_fixture_routes_text_and_parses() -> None:
    expected = _load_expected("swiggy_good.expected.json")
    provider = ScriptedProvider([expected])

    result = await run_extraction_pipeline(
        FIXTURES_DIR / "swiggy_good.pdf", provider, vendor_hint="Swiggy"
    )

    assert result.route == "text"
    assert result.parse_status == ParseStatus.parsed
    assert result.validation is not None and result.validation.ok
    assert result.invoice is not None
    assert result.invoice.invoice_total_minor == 36000
    # Discount line is present and correctly signed.
    discount_lines = [li for li in result.invoice.line_items if li.kind == "discount"]
    assert len(discount_lines) == 1
    assert discount_lines[0].total_minor == -5000


# ---------------------------------------------------------------------------
# Golden broken fixture — deliberately fails validation on both attempts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zepto_broken_fixture_lands_in_needs_review_after_retry() -> None:
    expected_broken = _load_expected("zepto_broken.expected.json")
    # Same flawed extraction returned on both the original attempt and the
    # retry — simulates a model that keeps hallucinating/dropping the same
    # delivery_fee row even after being told about the mismatch.
    provider = ScriptedProvider([expected_broken, expected_broken])

    result = await run_extraction_pipeline(
        FIXTURES_DIR / "zepto_broken.pdf", provider, vendor_hint="Zepto"
    )

    assert result.route == "text"
    assert result.parse_status == ParseStatus.needs_review
    assert result.validation is not None and not result.validation.ok
    assert any(i.code == "invoice_total_mismatch" for i in result.validation.issues)

    # Retry-with-mismatch actually happened: two attempts, and the second
    # request had the mismatch injected into its prompt.
    assert len(provider.calls) == 2
    assert provider.calls[0].retry_context is None
    assert provider.calls[1].retry_context is not None
    assert "16500" in provider.calls[1].retry_context
    assert "19000" in provider.calls[1].retry_context

    attempts = result.raw_extraction["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["validation"]["ok"] is False
    assert attempts[1]["validation"]["ok"] is False


@pytest.mark.asyncio
async def test_zepto_fixture_recovers_if_retry_fixes_the_mismatch() -> None:
    """
    Same broken first attempt, but the retry succeeds (model adds the
    missing delivery_fee row) — proves the retry path can recover, not just
    fail twice.
    """
    expected_broken = _load_expected("zepto_broken.expected.json")
    fixed = dict(expected_broken)
    fixed["line_items"] = [
        *expected_broken["line_items"],
        {
            "line_no": 3,
            "kind": "delivery_fee",
            "description": "Delivery Fee",
            "quantity": "1",
            "unit_price_minor": 2500,
            "total_minor": 2500,
        },
    ]
    provider = ScriptedProvider([expected_broken, fixed])

    result = await run_extraction_pipeline(
        FIXTURES_DIR / "zepto_broken.pdf", provider, vendor_hint="Zepto"
    )

    assert result.parse_status == ParseStatus.parsed
    assert len(provider.calls) == 2
    assert provider.calls[1].retry_context is not None


# ---------------------------------------------------------------------------
# Graceful degradation — no provider / erroring provider never crashes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_api_keys_degrade_to_needs_review_not_a_crash() -> None:
    from app.extraction.providers import NullProvider

    result = await run_extraction_pipeline(
        FIXTURES_DIR / "amazon_good.pdf", NullProvider()
    )
    assert result.parse_status == ParseStatus.needs_review
    assert result.invoice is None
    assert "GEMINI_API_KEY" in result.raw_extraction["final_error"]


@pytest.mark.asyncio
async def test_erroring_provider_degrades_to_needs_review_without_retry() -> None:
    result = await run_extraction_pipeline(
        FIXTURES_DIR / "amazon_good.pdf", ErroringProvider()
    )
    assert result.parse_status == ParseStatus.needs_review
    assert result.raw_extraction["final_error"] == "simulated network failure"


@pytest.mark.asyncio
async def test_malformed_json_schema_triggers_retry_then_needs_review() -> None:
    provider = ScriptedProvider([{"not": "matching schema"}, {"still": "wrong"}])
    result = await run_extraction_pipeline(FIXTURES_DIR / "amazon_good.pdf", provider)
    assert result.parse_status == ParseStatus.needs_review
    assert len(provider.calls) == 2


# ---------------------------------------------------------------------------
# End-to-end DB persistence (mirrors the Celery task body) against SQLite.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_pipeline_result_sets_parsed_and_writes_line_items(db_session) -> None:
    user = User(name="Alice", email="alice@example.com")
    db_session.add(user)
    await db_session.flush()

    expense = Expense(
        paid_by=user.id,
        source=ExpenseSource.pdf,
        total_minor=1,  # placeholder pre-parse value; CHECK constraint requires > 0
        parse_status=ParseStatus.queued,
    )
    db_session.add(expense)
    await db_session.flush()

    expected = _load_expected("amazon_good.expected.json")
    provider = ScriptedProvider([expected])
    pipeline_result = await run_extraction_pipeline(
        FIXTURES_DIR / "amazon_good.pdf", provider, vendor_hint="Amazon"
    )

    await _persist_pipeline_result(db_session, expense, pipeline_result)
    await db_session.commit()

    assert expense.parse_status == ParseStatus.parsed
    assert expense.total_minor == 150700
    assert expense.vendor == "Amazon"
    assert expense.raw_extraction is not None
    assert expense.raw_extraction["attempts"][0]["raw"] == expected

    from sqlalchemy import select

    rows = (
        await db_session.execute(
            select(ExpenseLineItem).where(ExpenseLineItem.expense_id == expense.id)
        )
    ).scalars().all()
    assert len(rows) == 4
    assert sum(int(r.total_minor) for r in rows) == 150700


@pytest.mark.asyncio
async def test_persist_pipeline_result_needs_review_does_not_overwrite_total(db_session) -> None:
    user = User(name="Bob", email="bob@example.com")
    db_session.add(user)
    await db_session.flush()

    expense = Expense(
        paid_by=user.id,
        source=ExpenseSource.pdf,
        total_minor=50_00,  # placeholder, deliberately different from the model's stated total
        parse_status=ParseStatus.queued,
    )
    db_session.add(expense)
    await db_session.flush()
    original_total = expense.total_minor

    expected_broken = _load_expected("zepto_broken.expected.json")
    provider = ScriptedProvider([expected_broken, expected_broken])
    pipeline_result = await run_extraction_pipeline(
        FIXTURES_DIR / "zepto_broken.pdf", provider, vendor_hint="Zepto"
    )

    await _persist_pipeline_result(db_session, expense, pipeline_result)
    await db_session.commit()

    assert expense.parse_status == ParseStatus.needs_review
    # An unvalidated model total must never silently overwrite total_minor.
    assert expense.total_minor == original_total
