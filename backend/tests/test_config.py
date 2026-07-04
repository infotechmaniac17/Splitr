"""
Regression tests for the prod SECRET_KEY guard: booting with APP_ENV=production
and the default placeholder SECRET_KEY must fail loudly at startup (Settings
construction), not silently at request time.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_production_with_default_secret_key_raises() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(APP_ENV="production")


def test_production_with_overridden_secret_key_is_fine() -> None:
    settings = Settings(APP_ENV="production", SECRET_KEY="a-real-random-secret")
    assert settings.SECRET_KEY == "a-real-random-secret"


def test_development_with_default_secret_key_is_fine() -> None:
    # Dev default must still work out of the box -- only production is gated.
    settings = Settings(APP_ENV="development")
    assert settings.SECRET_KEY == "change-me-in-production-to-a-random-32-byte-string"
