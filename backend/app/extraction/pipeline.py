"""
Tiered hybrid extraction pipeline (ARCHITECTURE.md §2.2) — orchestrates:

    Stage 0 route -> text or vision path -> LLM (structured output, temp=0)
    -> deterministic validation engine -> one retry with mismatch injected
    -> parse_status='parsed' | 'needs_review'

This is the ONLY place allowed to decide parse_status; it always routes
through app.extraction.validation.validate_extraction() before returning
'parsed' (project invariant #4).

Never raises. A missing/misconfigured/erroring provider degrades to
parse_status='needs_review' with a clear error recorded in raw_extraction —
extraction failures never crash the caller and never silently drop data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.domain.models import ParseStatus
from app.extraction.providers.base import ExtractionProvider, ExtractionRequest
from app.extraction.router import PdfSource, route
from app.extraction.schema import ExtractedInvoice, extraction_json_schema
from app.extraction.text_path import build_text_prompt, extract_text_and_tables
from app.extraction.validation import ValidationResult, validate_extraction
from app.extraction.vendor_detect import resolve_vendor
from app.extraction.vision_path import build_vision_prompt, render_pages_to_png

# One original attempt + one retry-with-mismatch (ARCHITECTURE.md §2.2).
MAX_ATTEMPTS = 2


@dataclass
class PipelineResult:
    parse_status: ParseStatus
    raw_extraction: dict[str, Any]
    invoice: ExtractedInvoice | None
    route: str  # "text" | "vision"
    validation: ValidationResult | None = None


async def run_extraction_pipeline(
    pdf_source: PdfSource,
    provider: ExtractionProvider,
    vendor_hint: str | None = None,
) -> PipelineResult:
    schema = extraction_json_schema()
    detected_route = route(pdf_source)

    attempts: list[dict[str, Any]] = []
    retry_context: str | None = None
    invoice: ExtractedInvoice | None = None
    validation: ValidationResult | None = None

    for attempt_no in range(1, MAX_ATTEMPTS + 1):
        request = _build_request(
            pdf_source, detected_route, schema, vendor_hint, retry_context
        )

        result = await provider.extract(request)

        attempt_record: dict[str, Any] = {
            "attempt": attempt_no,
            "provider": result.provider,
            "route": detected_route,
        }

        if not result.ok:
            # Provider unavailable or failed outright — no AI signal to act
            # on, so there is nothing a retry could improve. Stop here.
            attempt_record["error"] = result.error
            attempts.append(attempt_record)
            return PipelineResult(
                parse_status=ParseStatus.needs_review,
                raw_extraction={"attempts": attempts, "final_error": result.error},
                invoice=None,
                route=detected_route,
                validation=None,
            )

        attempt_record["raw"] = result.raw

        try:
            invoice = ExtractedInvoice.model_validate(result.raw)
        except ValidationError as exc:
            attempt_record["schema_error"] = str(exc)
            attempts.append(attempt_record)
            retry_context = (
                f"Your JSON did not match the required schema: {exc}. "
                "Re-extract, strictly matching the schema."
            )
            invoice = None
            continue

        validation = validate_extraction(invoice)
        attempt_record["validation"] = {
            "ok": validation.ok,
            "issues": [
                {"code": i.code, "message": i.message, "line_no": i.line_no}
                for i in validation.issues
            ],
        }
        attempts.append(attempt_record)

        if validation.ok:
            return PipelineResult(
                parse_status=ParseStatus.parsed,
                raw_extraction={"attempts": attempts},
                invoice=invoice,
                route=detected_route,
                validation=validation,
            )

        retry_context = validation.mismatch_prompt()

    # Exhausted retries — still failing validation.
    return PipelineResult(
        parse_status=ParseStatus.needs_review,
        raw_extraction={"attempts": attempts},
        invoice=invoice,
        route=detected_route,
        validation=validation,
    )


def _build_request(
    pdf_source: PdfSource,
    detected_route: str,
    schema: dict[str, Any],
    vendor_hint: str | None,
    retry_context: str | None,
) -> ExtractionRequest:
    if detected_route == "text":
        content = extract_text_and_tables(pdf_source)
        # Auto-detect from the pre-extracted text layer only when the caller
        # gave no explicit hint (resolve_vendor never overrides an explicit
        # vendor_hint — see app.extraction.vendor_detect).
        resolved_vendor = resolve_vendor(vendor_hint, content["text"])
        prompt = build_text_prompt(content, resolved_vendor, retry_context)
        return ExtractionRequest(
            mode="text",
            schema=schema,
            text=prompt,
            retry_context=retry_context,
            vendor_hint=resolved_vendor,
        )

    # Vision route: no pre-extracted text to sniff a vendor from, so only
    # an explicit user-supplied vendor_hint is ever used here (see
    # app.extraction.vision_path.build_vision_prompt docstring).
    images = render_pages_to_png(pdf_source)
    prompt = build_vision_prompt(vendor_hint, retry_context)
    return ExtractionRequest(
        mode="vision",
        schema=schema,
        images=images,
        text=prompt,
        retry_context=retry_context,
        vendor_hint=vendor_hint,
    )
