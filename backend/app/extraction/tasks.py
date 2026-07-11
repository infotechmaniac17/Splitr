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
import logging
import sys
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app

# Windows' default ProactorEventLoop can't run psycopg's async driver
# ("Psycopg cannot use the 'ProactorEventLoop' to run in async mode").
# The Celery worker process never otherwise touches asyncio policy, so
# every task run silently failed on Windows dev machines -- the expense
# row stayed parse_status='queued' forever with no visible error.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from app.db import AsyncSessionLocal
from app.domain.gst import base_item_totals_minor
from app.domain.models import (
    Expense,
    ExpenseLineItem,
    ExpenseTaxComponent,
    ParseStatus,
)
from app.domain.vendor_discount import (
    apply_extracted_discount_snapshot,
    apply_vendor_discount_snapshot,
)
from app.extraction.pipeline import PipelineResult, run_extraction_pipeline
from app.extraction.providers import get_default_provider
from app.extraction.validation import parse_date, validate_gst

if TYPE_CHECKING:
    from app.extraction.providers.base import ExtractionProvider
    from app.extraction.router import PdfSource

logger = logging.getLogger(__name__)


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
    # Capture parse_status BEFORE this function mutates anything on `expense`.
    #
    # 'confirmed' is a terminal parse_status (see the state machine trigger,
    # EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V3 in app/domain/pg_guards.py)
    # with zero legal outbound transitions. No real code path re-enters this
    # pipeline against an already-confirmed expense today, so this guard is
    # defense in depth against a future bug -- e.g. a manual re-enqueue of
    # process_expense_pdf against an already-confirmed expense's id.
    #
    # The guard below covers the ENTIRE body of this function, not just the
    # vendor-discount snapshot call: every mutation this function makes to
    # `expense` (parse_status, raw_extraction, vendor, invoice_number,
    # currency, invoice_date, subtotal_minor, total_minor, and the line-item
    # replacement) is skipped outright when the expense started out
    # confirmed. A prior version of this guard only gated the discount
    # snapshot call, which was too late: SQLAlchemy's autoflush had already
    # queued the parse_status/raw_extraction UPDATE by the time any later
    # query ran, and Postgres's state-machine trigger rejected it with an
    # unhandled ProgrammingError instead of the app failing gracefully. By
    # returning here, before any attribute on `expense` is touched, no write
    # is ever attempted and nothing reaches the DB for the trigger to reject.
    original_status = expense.parse_status
    if original_status == ParseStatus.confirmed:
        attempted_invoice_number = (
            pipeline_result.invoice.invoice_number if pipeline_result.invoice else None
        )
        logger.warning(
            "Extraction pipeline ran against already-confirmed expense "
            "%s; this should be unreachable (confirmed is terminal and can "
            "never change). Refusing to write: would have set "
            "parse_status=%r, invoice_number=%r, raw_extraction=<%d bytes "
            "of pipeline output>. Skipping all persistence for this run "
            "(logged skip, not a silent no-op and not a raised exception "
            "-- the DB write is never attempted, so the state-machine "
            "trigger never gets a chance to reject it).",
            expense.id,
            pipeline_result.parse_status,
            attempted_invoice_number,
            len(str(pipeline_result.raw_extraction)),
        )
        return

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

    # M6 item 4: GST mode is metadata (not money) so it's set unconditionally,
    # like subtotal_minor above -- it does not wait on validation the way
    # total_minor does below.
    expense.gst_mode = invoice.gst_mode

    # M6 item 4: GST-specific arithmetic invariants are DELIBERATELY kept
    # separate from parse_status (see app.extraction.validation.validate_gst
    # docstring for the full rationale) -- they feed this dedicated boolean
    # instead. A GST reconciliation failure never overwrites/adjusts any
    # extracted number; it only flags the expense for review before it can
    # be confirmed (see app/api/expenses.py's confirm_expense).
    gst_check = validate_gst(invoice)
    expense.needs_review = not gst_check.ok

    # M6 item 4 (discount follow-up to item 3): populate discount_* directly
    # from a printed coupon/promo line, BEFORE evaluating vendor rules below
    # -- a matched vendor rule is more specific/intentional and must win if
    # both are present on the same run (same precedence
    # apply_vendor_discount_snapshot already gives 'vendor_rule' over a
    # historical 'extracted' backfill).
    #
    # By this point the whole-function guard at the top has already returned
    # early for any expense that started out confirmed, so `original_status
    # != ParseStatus.confirmed` always holds here -- this check is now
    # unreachable dead code kept only as a second line of defense (belt and
    # braces) alongside apply_extracted_discount_snapshot/
    # apply_vendor_discount_snapshot's own internal guards, in case a future
    # refactor moves these calls above the early return.
    if original_status != ParseStatus.confirmed:
        apply_extracted_discount_snapshot(expense, invoice)

    # M6 item 3: re-evaluate vendor discount rules on every extraction run
    # (including re-parses) -- see app.domain.vendor_discount for the full
    # gating/precedence rules (never overwrites a manual snapshot; never
    # touches an already-confirmed expense).
    if original_status != ParseStatus.confirmed:
        # M6 item 5 (OQ-2 fix): pass the freshly-computed base subtotal of
        # the in-memory invoice about to be persisted -- the same shared
        # definition (app.domain.gst.base_item_totals_minor) the item-5
        # allocator uses for its own threshold check -- rather than letting
        # apply_vendor_discount_snapshot fall back to expense.subtotal_minor
        # (the LLM's own self-reported, unvalidated subtotal figure, which
        # need not agree with items+fees+tip actually summed from the lines
        # being persisted).
        fresh_subtotal = base_item_totals_minor(invoice.line_items, invoice.gst_mode)
        await apply_vendor_discount_snapshot(
            session, expense, subtotal_override_minor=fresh_subtotal
        )

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
                gst_rate=li_in.gst_rate,
                gst_amount_minor=li_in.gst_amount_minor,
            )
        )

    # M6 item 4: replace any previously-extracted tax components — same
    # full-replacement contract as the line items above (a re-parse fully
    # replaces, never patches). Reachable only inside this whole-function
    # confirmed-guard, exactly like every other write in this function; see
    # the module-level guard comment at the top of this function and
    # migration 0010's docstring for why this table (unlike
    # expense_line_items) is safe to fully guard at the DB level too via
    # reject_mutation_if_expense_confirmed('expense_id', 'direct').
    existing_components = await session.execute(
        select(ExpenseTaxComponent).where(ExpenseTaxComponent.expense_id == expense.id)
    )
    for tc in existing_components.scalars().all():
        await session.delete(tc)
    await session.flush()

    for tc_in in invoice.tax_components:
        session.add(
            ExpenseTaxComponent(
                expense_id=expense.id,
                name=tc_in.name,
                rate=tc_in.rate,
                amount_minor=tc_in.amount_minor,
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
