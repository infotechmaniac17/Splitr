"""
Provider factory — the only place that decides which ExtractionProvider
implementation is active. Pipeline code depends only on the ExtractionProvider
interface (base.py), never on a concrete vendor class, so providers stay
swappable per CLAUDE.md.
"""

from __future__ import annotations

from app.extraction.providers.base import (
    ExtractionProvider,
    ExtractionRequest,
    ExtractionResult,
    NullProvider,
)
from app.extraction.providers.gemini import GeminiProvider
from app.extraction.providers.openai import OpenAIProvider

__all__ = [
    "ExtractionProvider",
    "ExtractionRequest",
    "ExtractionResult",
    "NullProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "get_default_provider",
]


def get_default_provider() -> ExtractionProvider:
    """
    Pick the first configured provider (Gemini preferred per ARCHITECTURE.md
    §2.2 cost/accuracy comparison; OpenAI as fallback). Degrades to
    NullProvider — never raises — if neither GEMINI_API_KEY nor
    OPENAI_API_KEY is set.
    """
    gemini = GeminiProvider()
    if gemini.is_configured():
        return gemini

    openai_provider = OpenAIProvider()
    if openai_provider.is_configured():
        return openai_provider

    return NullProvider()
