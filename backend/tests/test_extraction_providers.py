"""
Tests for the ExtractionProvider interface and its concrete implementations.

GEMINI_API_KEY and OPENAI_API_KEY are both empty in this environment (by
design — no real keys are configured), and neither vendor SDK
(`google-genai`, `openai`) is installed. These tests confirm that fact
produces a clean, non-crashing degrade path end-to-end — never an
unhandled exception — which is the graceful-degradation contract CLAUDE.md
requires.
"""

from __future__ import annotations

import pytest

from app.extraction.providers import (
    GeminiProvider,
    NullProvider,
    OpenAIProvider,
    get_default_provider,
)
from app.extraction.providers.base import ExtractionRequest


def _text_request() -> ExtractionRequest:
    return ExtractionRequest(mode="text", schema={"type": "object"}, text="hello")


@pytest.mark.asyncio
async def test_null_provider_never_configured() -> None:
    provider = NullProvider()
    assert provider.is_configured() is False
    result = await provider.extract(_text_request())
    assert not result.ok
    assert result.error is not None


@pytest.mark.asyncio
async def test_gemini_provider_degrades_without_key() -> None:
    provider = GeminiProvider(api_key="")
    assert provider.is_configured() is False
    result = await provider.extract(_text_request())
    assert not result.ok
    assert "GEMINI_API_KEY" in (result.error or "")


@pytest.mark.asyncio
async def test_gemini_provider_degrades_without_sdk_even_with_key() -> None:
    # A key is "set" but google-genai is not installed in this environment —
    # must still degrade cleanly, never raise.
    provider = GeminiProvider(api_key="fake-key-for-test")
    assert provider.is_configured() is True
    result = await provider.extract(_text_request())
    assert not result.ok
    assert result.error is not None


@pytest.mark.asyncio
async def test_openai_provider_degrades_without_key() -> None:
    provider = OpenAIProvider(api_key="")
    assert provider.is_configured() is False
    result = await provider.extract(_text_request())
    assert not result.ok
    assert "OPENAI_API_KEY" in (result.error or "")


@pytest.mark.asyncio
async def test_openai_provider_degrades_without_sdk_even_with_key() -> None:
    provider = OpenAIProvider(api_key="fake-key-for-test")
    assert provider.is_configured() is True
    result = await provider.extract(_text_request())
    assert not result.ok
    assert result.error is not None


def test_get_default_provider_falls_back_to_null_when_no_keys_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force empty keys regardless of ambient env / local .env: this test is
    # about the no-keys degrade path, not about what this machine has set.
    from app.config import settings

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    provider = get_default_provider()
    assert isinstance(provider, NullProvider)
    assert provider.is_configured() is False
