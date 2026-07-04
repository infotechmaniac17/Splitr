"""
Provider-agnostic extraction interface (ARCHITECTURE.md §2.2).

Pipeline code (app.extraction.pipeline) must only ever talk to this
interface — never import a vendor SDK directly. Concrete providers
(GeminiProvider, OpenAIProvider) live behind it and are swappable via
app.extraction.providers.get_default_provider().

Hard rule: `extract()` MUST NOT raise. Any failure (missing API key, SDK not
installed, network error, malformed response) must degrade to an
`ExtractionResult` with `error` set, so the pipeline can fall back to
parse_status='needs_review' instead of crashing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

ExtractionMode = Literal["text", "vision"]


@dataclass
class ExtractionRequest:
    """Everything a provider needs to run one extraction attempt."""

    mode: ExtractionMode
    schema: dict[str, Any]
    text: str | None = None
    images: list[bytes] | None = None
    # Mismatch description injected into the prompt on retry (ARCHITECTURE.md
    # §2.2: "your line items sum to 842 but total is 857 ... re-extract").
    retry_context: str | None = None
    vendor_hint: str | None = None


@dataclass
class ExtractionResult:
    """Raw provider output. Persisted verbatim to expenses.raw_extraction."""

    provider: str
    raw: dict[str, Any] | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.raw is not None


class ExtractionProvider(ABC):
    """Abstract base — never hardcode a vendor SDK into pipeline logic."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """True if this provider has the credentials it needs (env var set)."""

    @abstractmethod
    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """
        Run one extraction attempt. MUST NOT raise — on any failure return
        ExtractionResult(raw=None, error=<clear message>).
        """


class NullProvider(ExtractionProvider):
    """
    Used when no provider is configured (both GEMINI_API_KEY and
    OPENAI_API_KEY are empty). Ensures the pipeline degrades gracefully to
    'needs_review' with a clear error instead of crashing or silently
    fabricating data.
    """

    name = "none"

    def is_configured(self) -> bool:
        return False

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        return ExtractionResult(
            provider=self.name,
            raw=None,
            error=(
                "No extraction provider configured: set GEMINI_API_KEY or "
                "OPENAI_API_KEY to enable PDF extraction."
            ),
        )
