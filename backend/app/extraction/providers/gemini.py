"""
Gemini Flash provider (ARCHITECTURE.md §2.2 — recommended text + vision
model: cheap, adaptive, near-zero per-invoice cost).

SWAP-IN SEAM: this is the one place a real Gemini API key/SDK plugs in.
`google-genai` is a declared runtime dependency (pyproject.toml) and
GEMINI_API_KEY is read from env via app.config.settings; both are still
checked defensively (missing key / missing package) and either missing
condition degrades `extract()` to a clear ExtractionResult(error=...)
rather than raising, so a misconfigured deployment never crashes the
pipeline — it only ever produces parse_status='needs_review'.

Verified live against google-genai==2.10.0 (see
backend/scripts/live_extraction_smoke_test.py): `response_schema` only
accepts a restricted OpenAPI-3.0 schema subset (no $defs/$ref support),
so this provider uses `response_json_schema` instead, which is the SDK's
documented full-JSON-Schema input and is what our pydantic-generated
schema ($defs + $ref + anyOf) actually needs.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import settings
from app.extraction.providers.base import (
    ExtractionProvider,
    ExtractionRequest,
    ExtractionResult,
)


class GeminiProvider(ExtractionProvider):
    name = "gemini"
    # gemini-2.0-flash has zero free-tier quota on many API keys as of the
    # google-genai 2.x SDK generation; gemini-2.5-flash is natively
    # multimodal (text + vision) and is what we actually verified works
    # against the live API. Kept as one model for both modes since Flash
    # handles images directly in the same `contents` list.
    model_text = "gemini-2.5-flash"
    model_vision = "gemini-2.5-flash"

    def __init__(self, api_key: str | None = None) -> None:
        # Read from env via app.config.settings unless explicitly overridden
        # (tests pass api_key= directly rather than mutating process env).
        self._api_key = api_key if api_key is not None else settings.GEMINI_API_KEY

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        if not self.is_configured():
            return ExtractionResult(
                provider=self.name,
                raw=None,
                error="GEMINI_API_KEY not set; Gemini provider unavailable.",
            )

        try:
            import google.genai as genai  # noqa: PLC0415
            from google.genai import types as genai_types  # noqa: PLC0415
        except ImportError:
            return ExtractionResult(
                provider=self.name,
                raw=None,
                error=(
                    "google-genai package is not installed. "
                    "Run `pip install google-genai` to enable the Gemini path."
                ),
            )

        try:
            client = genai.Client(api_key=self._api_key)
            prompt = _build_prompt(request)
            contents: list[Any] = [prompt]
            if request.mode == "vision" and request.images:
                for img_bytes in request.images:
                    contents.append(
                        genai_types.Part.from_bytes(
                            data=img_bytes, mime_type="image/png"
                        )
                    )

            model = self.model_text if request.mode == "text" else self.model_vision
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config={
                    "temperature": 0,
                    "response_mime_type": "application/json",
                    # `request.schema` is a plain pydantic-generated JSON
                    # Schema (uses $defs/$ref/anyOf/pattern). The SDK's
                    # `response_schema` field only accepts a restricted
                    # OpenAPI-3.0 subset and will silently mishandle $refs;
                    # `response_json_schema` is the documented alternative
                    # for full JSON Schema input (verified against the
                    # installed google-genai==2.10.0 SDK).
                    "response_json_schema": request.schema,
                },
            )
            raw = json.loads(response.text)
            return ExtractionResult(provider=self.name, raw=raw)
        except Exception as exc:  # noqa: BLE001 — never crash the pipeline
            return ExtractionResult(provider=self.name, raw=None, error=str(exc))


def _build_prompt(request: ExtractionRequest) -> str:
    parts = [
        "Extract structured invoice data as strict JSON matching the "
        "provided schema. All money amounts are integer minor units (paise). "
        "Never use floats.",
    ]
    if request.vendor_hint:
        parts.append(f"Vendor hint: {request.vendor_hint}.")
    if request.mode == "text" and request.text:
        parts.append(request.text)
    if request.retry_context:
        parts.append(
            "IMPORTANT — the previous attempt failed validation: "
            + request.retry_context
        )
    return "\n\n".join(parts)
