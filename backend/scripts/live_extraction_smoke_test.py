"""
Manual, human-run smoke test for the M3 extraction pipeline against the
REAL Gemini API (not a scripted test double).

This is intentionally NOT part of the pytest suite:
  - it costs real API quota / money per run
  - it is flaky by nature (network + a live model's own reasoning)
  - CLAUDE.md's CI section documents exactly two tiers (SQLite, Postgres) —
    there is no "live LLM" tier, and this script must never be picked up by
    `pytest` (it is not named test_*.py and lives outside tests/).

What it does:
  Runs app.extraction.pipeline.run_extraction_pipeline() against the three
  synthetic fixtures in tests/fixtures/invoices/ using the real
  GeminiProvider (reads GEMINI_API_KEY from the environment / repo-root
  .env — never hardcoded, never printed).

Usage (from backend/):
    .venv/Scripts/python.exe scripts/live_extraction_smoke_test.py

Requires GEMINI_API_KEY to be set (e.g. in the repo-root .env). If it is
not set, the script reports that clearly and exits — it does not fail
loudly, mirroring the pipeline's own graceful-degradation contract.
"""

from __future__ import annotations

import asyncio
import json

# Load ONLY the LLM API key vars from the repo-root .env (GEMINI_API_KEY /
# OPENAI_API_KEY live there) before anything imports app.config.settings, so
# the key is visible to Settings(). We deliberately do NOT blanket-load the
# whole root .env into the environment: it also defines DATABASE_URL (with a
# different driver than backend/.env's own DATABASE_URL), and os.environ
# takes priority over backend/.env in pydantic-settings — blindly loading it
# would silently swap the DB driver out from under app.db.
import os  # noqa: E402
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
try:
    from dotenv import dotenv_values

    _root_env = dotenv_values(_REPO_ROOT / ".env")
    for _key in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        if _root_env.get(_key) and not os.environ.get(_key):
            os.environ[_key] = _root_env[_key]
except ImportError:
    pass

# Make `app` importable when run as a plain script from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.extraction.pipeline import run_extraction_pipeline  # noqa: E402
from app.extraction.providers import GeminiProvider  # noqa: E402

FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "invoices"

FIXTURES = [
    ("amazon_good.pdf", "Amazon"),
    ("swiggy_good.pdf", "Swiggy"),
    ("zepto_broken.pdf", "Zepto"),
]


def _summarize(result) -> dict:  # noqa: ANN001
    summary: dict = {
        "route": result.route,
        "parse_status": result.parse_status.value
        if hasattr(result.parse_status, "value")
        else result.parse_status,
        "num_attempts": len(result.raw_extraction.get("attempts", [])),
    }
    if result.invoice is not None:
        summary["invoice_total_minor"] = result.invoice.invoice_total_minor
        summary["line_item_count"] = len(result.invoice.line_items)
        summary["line_items_sum"] = sum(
            li.total_minor for li in result.invoice.line_items
        )
    if result.validation is not None:
        summary["validation_ok"] = result.validation.ok
        summary["validation_issues"] = [
            {"code": i.code, "message": i.message} for i in result.validation.issues
        ]
    if "final_error" in result.raw_extraction:
        summary["final_error"] = result.raw_extraction["final_error"]
    return summary


async def main() -> None:
    provider = GeminiProvider()
    if not provider.is_configured():
        print(
            "GEMINI_API_KEY is not set — nothing to smoke-test. "
            "Set it in the repo-root .env and re-run."
        )
        return

    print(f"Using provider={provider.name}, model={provider.model_text}\n")

    for pdf_name, vendor_hint in FIXTURES:
        pdf_path = FIXTURES_DIR / pdf_name
        print(f"=== {pdf_name} ===")
        result = await run_extraction_pipeline(
            pdf_path, provider, vendor_hint=vendor_hint
        )
        summary = _summarize(result)
        print(json.dumps(summary, indent=2))
        print()


if __name__ == "__main__":
    asyncio.run(main())
