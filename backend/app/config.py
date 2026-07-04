"""
Application configuration loaded from environment variables / .env file.

All service URLs default to the values that docker-compose.yml exposes on
localhost so that a plain `uvicorn app.main:app` works after `make up`
without any .env tweaks.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Dev-only placeholder -- fine as a default so `uvicorn app.main:app` works
# out of the box locally, but must never reach production unchanged (see
# Settings.check_secret_key_in_production below).
_DEFAULT_SECRET_KEY = "change-me-in-production-to-a-random-32-byte-string"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Database — PostgreSQL via psycopg (v3)
    # URL format: postgresql+psycopg://user:password@host:port/dbname
    # ------------------------------------------------------------------
    DATABASE_URL: str = "postgresql+psycopg://splitr:splitr@localhost:5435/splitr"

    # ------------------------------------------------------------------
    # Redis — used by Celery (broker + result backend) and cache
    # ------------------------------------------------------------------
    REDIS_URL: str = "redis://localhost:6379/0"

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    APP_ENV: str = "development"
    DEBUG: bool = False

    # Secret key for JWT / session signing (override in production)
    SECRET_KEY: str = _DEFAULT_SECRET_KEY

    # LLM provider API keys (never committed; set in .env only)
    OPENAI_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # Object storage (S3-compatible)
    OBJECT_STORAGE_BUCKET: str = ""
    OBJECT_STORAGE_ENDPOINT: str = ""
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "ap-south-1"

    # Local filesystem fallback for PdfStorage when OBJECT_STORAGE_BUCKET is
    # unset (dev/tests — see app/storage/__init__.py:get_default_storage).
    # Matches the root .gitignore's `storage/` entry (never commit raw PDFs).
    PDF_STORAGE_DIR: str = "storage/pdfs"

    @model_validator(mode="after")
    def check_secret_key_in_production(self) -> "Settings":
        """
        Fail loudly at startup (not silently at request time) if APP_ENV is
        production and SECRET_KEY was never overridden. Booting production
        with the well-known placeholder means anyone can forge valid
        access/refresh JWTs for any user id.
        """
        if self.APP_ENV == "production" and self.SECRET_KEY == _DEFAULT_SECRET_KEY:
            raise ValueError(
                "SECRET_KEY is still the default placeholder while "
                "APP_ENV=production. Set a real random SECRET_KEY via the "
                "environment (e.g. `openssl rand -hex 32`) before starting "
                "the app in production."
            )
        return self


# Singleton — import this everywhere: `from app.config import settings`
settings = Settings()
