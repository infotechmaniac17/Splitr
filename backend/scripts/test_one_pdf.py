"""Ad-hoc timing test: run extraction pipeline on one real Amazon PDF."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.extraction.pipeline import run_extraction_pipeline
from app.extraction.providers.gemini import GeminiProvider
from app.extraction.router import route
from app.extraction.text_path import extract_text_and_tables


async def main(pdf_path: str) -> None:
    data = Path(pdf_path).read_bytes()
    print(f"file size: {len(data)} bytes")

    t0 = time.time()
    r = route(data)
    print(f"route(): {time.time()-t0:.3f}s -> {r}")

    t0 = time.time()
    content = extract_text_and_tables(data)
    print(f"extract_text_and_tables(): {time.time()-t0:.3f}s")
    print(f"text length: {len(content['text'])} chars, tables: {len(content['tables'])}")

    provider = GeminiProvider()
    print(f"provider configured: {provider.is_configured()}")

    t0 = time.time()
    result = await run_extraction_pipeline(data, provider)
    print(f"full pipeline: {time.time()-t0:.3f}s")
    print(f"parse_status: {result.parse_status}")
    print(f"route used: {result.route}")
    if result.validation:
        print(f"validation ok: {result.validation.ok}")
        for issue in result.validation.issues:
            print(f"  issue: {issue.code} {issue.message}")
    if result.invoice:
        print(f"vendor: {result.invoice.vendor}")
        print(f"line items: {len(result.invoice.line_items)}")
        print(f"subtotal_minor: {result.invoice.subtotal_minor}")
    else:
        print("raw_extraction:", result.raw_extraction)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
