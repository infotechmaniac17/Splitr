"""
Regression tests for the prod SECRET_KEY guard: booting with APP_ENV=production
and the default placeholder SECRET_KEY must fail loudly at startup (Settings
construction), not silently at request time.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_production_with_default_secret_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Explicitly force the class default -- CI sets a real SECRET_KEY env var
    # (to silence PyJWT's InsecureKeyLengthWarning in unrelated tests), which
    # would otherwise shadow the default this test needs to exercise.
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(APP_ENV="production")


def test_production_with_overridden_secret_key_is_fine() -> None:
    settings = Settings(APP_ENV="production", SECRET_KEY="a-real-random-secret")
    assert settings.SECRET_KEY == "a-real-random-secret"


def test_development_with_default_secret_key_is_fine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dev default must still work out of the box -- only production is gated.
    monkeypatch.delenv("SECRET_KEY", raising=False)
    settings = Settings(APP_ENV="development")
    assert settings.SECRET_KEY == "change-me-in-production-to-a-random-32-byte-string"
