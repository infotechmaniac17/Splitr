"""
Celery task wiring for the PDF extraction pipeline (M3).

`process_expense_pdf` is the async worker body — it is exposed separately
from the Celery-decorated wrapper so tests (and any other async caller) can
await it directly against a test DB session without needing a running
Celery worker or Redis broker. `process_expense_pdf_task` is the actual
Celery entrypoint enqueued by the API layer.

Contract: the `expenses` row referenced by `expense_id` must already exist
(created by the upload endpoint — out of scope for M3) with parse_status
typically 'queued'. This task runs the tiered hybrid pipeline, persists the
raw model output to `raw_extraction` (write-once; this task is the only
writer and never mutates a previously-written raw_extraction after the fact
— a re-run fully replaces it, it does not patch it), replaces any
line items with the freshly extracted ones, and sets parse_status to
'parsed' or 'needs_review' per the deterministic validation engine — never
'parsed' without having passed validation (invariant #4).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.db import AsyncSessionLocal
from app.domain.models import Expense, ExpenseLineItem, ParseStatus
from app.extraction.pipeline import PipelineResult, run_extraction_pipeline
from app.extraction.providers import get_default_provider
from app.extraction.validation import parse_date

if TYPE_CHECKING:
    from app.extraction.providers.base import ExtractionProvider
    from app.extraction.router import PdfSource


async def process_expense_pdf(
    expense_id: uuid.UUID,
    pdf_source: PdfSource,
    provider: ExtractionProvider | None = None,
    vendor_hint: str | None = None,
) -> ParseStatus:
    """
    Run the extraction pipeline for an existing expense row and persist the
    result. Returns the resulting parse_status.
    """
    provider = provider or get_default_provider()
    async with AsyncSessionLocal() as session:
        return await _process_with_session(
            session, expense_id, pdf_source, provider, vendor_hint
        )


async def _process_with_session(
    session: AsyncSession,
    expense_id: uuid.UUID,
    pdf_source: PdfSource,
    provider: ExtractionProvider,
    vendor_hint: str | None,
) -> ParseStatus:
    result = await session.execute(select(Expense).where(Expense.id == expense_id))
    expense = result.scalar_one_or_none()
    if expense is None:
        raise ValueError(f"Expense {expense_id} not found")

    pipeline_result: PipelineResult = await run_extraction_pipeline(
        pdf_source, provider, vendor_hint
    )

    await _persist_pipeline_result(session, expense, pipeline_result)
    await session.commit()
    return pipeline_result.parse_status


async def _persist_pipeline_result(
    session: AsyncSession,
    expense: Expense,
    pipeline_result: PipelineResult,
) -> None:
    # raw_extraction is write-once per run: assigned exactly here, exactly
    # once, never subsequently mutated in place.
    expense.parse_status = pipeline_result.parse_status
    expense.raw_extraction = pipeline_result.raw_extraction

    if pipeline_result.invoice is None:
        return  # needs_review with no usable JSON at all — nothing else to persist

    invoice = pipeline_result.invoice
    expense.vendor = invoice.vendor or expense.vendor
    expense.invoice_number = invoice.invoice_number or expense.invoice_number
    expense.currency = invoice.currency or expense.currency
    parsed_date = parse_date(invoice.invoice_date)
    if parsed_date is not None:
        expense.invoice_date = parsed_date
    expense.subtotal_minor = invoice.subtotal_minor

    # Only trust the model's stated invoice total once it has passed the
    # validation engine — an unvalidated total must never overwrite the
    # expense's total_minor (money invariant).
    if pipeline_result.parse_status == ParseStatus.parsed:
        expense.total_minor = invoice.invoice_total_minor

    # Replace any previously-extracted line items — a re-parse is a full
    # replacement, not a patch (matches raw_extraction's write-once contract
    # at the pipeline-result granularity).
    existing = await session.execute(
        select(ExpenseLineItem).where(ExpenseLineItem.expense_id == expense.id)
    )
    for li in existing.scalars().all():
        await session.delete(li)
    await session.flush()

    for li_in in invoice.line_items:
        session.add(
            ExpenseLineItem(
                expense_id=expense.id,
                line_no=li_in.line_no,
                kind=li_in.kind,
                description=li_in.description,
                quantity=li_in.quantity,
                unit_price_minor=li_in.unit_price_minor,
                total_minor=li_in.total_minor,
            )
        )


@celery_app.task(name="extraction.process_expense_pdf")  # type: ignore[untyped-decorator]
def process_expense_pdf_task(
    expense_id: str,
    pdf_bytes: bytes,
    vendor_hint: str | None = None,
) -> str:
    """Celery entrypoint — synchronous wrapper around the async worker body."""
    status = asyncio.run(
        process_expense_pdf(uuid.UUID(expense_id), pdf_bytes, vendor_hint=vendor_hint)
    )
    return str(status)


async def enqueue_extraction(
    expense_id: uuid.UUID,
    pdf_bytes: bytes,
    vendor_hint: str | None = None,
) -> None:
    """
    Default production hook used by `POST /expenses/upload` (M4) to kick off
    extraction: publishes `process_expense_pdf_task` to Celery/Redis and
    returns immediately — the expense row stays `parse_status='queued'`
    until the worker picks it up.

    Async (even though `.delay()` itself is a fast, synchronous publish call)
    so it matches the `app.api.deps.get_extraction_enqueuer` dependency
    signature and so tests can override it with a coroutine that runs the
    pipeline inline (no live Celery worker / Redis broker required — see
    tests/test_upload.py), consistent with CLAUDE.md's SQLite-tier-needs-no-
    services testing requirement.
    """
    process_expense_pdf_task.delay(str(expense_id), pdf_bytes, vendor_hint)
