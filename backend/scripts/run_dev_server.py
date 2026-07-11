"""
Dev-server runner that works on Windows regardless of uvicorn flags.

Why this exists: psycopg (v3) cannot run on asyncio's ProactorEventLoop.
uvicorn >= 0.49 builds its event loop via a loop *factory* passed straight to
`asyncio.run(loop_factory=...)`, which NEVER consults the event-loop policy —
so the `WindowsSelectorEventLoopPolicy` set in `app.main` is silently ignored.
That factory returns `ProactorEventLoop` on win32 unless
`config.use_subprocess` is true (i.e. `--reload` or `--workers > 1`, see
uvicorn/loops/asyncio.py + uvicorn/config.py).

Consequences:
  - `uvicorn app.main:app --reload`  -> works (subprocess path -> Selector).
  - `uvicorn app.main:app`           -> crashes on first DB call with
    `psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop'`.

This runner drives `uvicorn.Server.serve()` under an explicitly-selected
SelectorEventLoop so it works with or without reload semantics. Use it for
non-reload runs (e2e scripts, CI smoke tests):

    backend/.venv/Scripts/python.exe scripts/run_dev_server.py [--port 8000]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn

# Make `app.main:app` importable no matter which directory this is run from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    config = uvicorn.Config("app.main:app", host=args.host, port=args.port)
    server = uvicorn.Server(config)
    # asyncio.run() (unlike uvicorn's own runner) DOES honor the policy set
    # above, so this always gets a SelectorEventLoop on Windows.
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
