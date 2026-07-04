"""
OpenAI (GPT-4o-mini / GPT-4o) provider — secondary option behind
ExtractionProvider, per ARCHITECTURE.md §2.2 comparative table.

Same swap-in seam pattern as gemini.py: OPENAI_API_KEY empty and/or the
`openai` package not installed both degrade gracefully rather than raising.
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


class OpenAIProvider(ExtractionProvider):
    name = "openai"
    model_text = "gpt-4o-mini"
    model_vision = "gpt-4o-mini"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else settings.OPENAI_API_KEY

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        if not self.is_configured():
            return ExtractionResult(
                provider=self.name,
                raw=None,
                error="OPENAI_API_KEY not set; OpenAI provider unavailable.",
            )

        try:
            from openai import AsyncOpenAI  # noqa: PLC0415
        except ImportError:
            return ExtractionResult(
                provider=self.name,
                raw=None,
                error=(
                    "openai package is not installed. "
                    "Run `pip install openai` to enable the OpenAI path."
                ),
            )

        try:
            client = AsyncOpenAI(api_key=self._api_key)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": _system_prompt(request)},
            ]
            if request.mode == "vision" and request.images:
                import base64  # noqa: PLC0415

                content: list[dict[str, Any]] = [
                    {"type": "text", "text": request.text or ""}
                ]
                for img_bytes in request.images:
                    b64 = base64.b64encode(img_bytes).decode("ascii")
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        }
                    )
                messages.append({"role": "user", "content": content})
            else:
                messages.append({"role": "user", "content": request.text or ""})

            model = self.model_text if request.mode == "text" else self.model_vision
            response = await client.chat.completions.create(
                model=model,
                temperature=0,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "extracted_invoice",
                        "schema": request.schema,
                    },
                },
            )
            content_str = response.choices[0].message.content or "{}"
            raw = json.loads(content_str)
            return ExtractionResult(provider=self.name, raw=raw)
        except Exception as exc:  # noqa: BLE001 — never crash the pipeline
            return ExtractionResult(provider=self.name, raw=None, error=str(exc))


def _system_prompt(request: ExtractionRequest) -> str:
    parts = [
        "Extract structured invoice data as strict JSON matching the "
        "provided schema. All money amounts are integer minor units (paise). "
        "Never use floats.",
    ]
    if request.vendor_hint:
        parts.append(f"Vendor hint: {request.vendor_hint}.")
    if request.retry_context:
        parts.append(
            "IMPORTANT — the previous attempt failed validation: "
            + request.retry_context
        )
    return "\n\n".join(parts)
