"""
FastAPI application factory.

The `app` object at module level is what uvicorn loads:
    uvicorn app.main:app --reload

Routers are registered here as milestones are built out.

Windows note: psycopg (v3) requires the SelectorEventLoop; the policy is set
at import time so that `asyncio.run(...)` calls inside tests and scripts also
pick it up automatically.
"""

import sys

# psycopg3 does not support ProactorEventLoop (Windows default).
# Force SelectorEventLoop on Windows before any async code runs.
if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings


def create_app() -> FastAPI:
    app = FastAPI(
        title="Splitr API",
        description=(
            "Item-level expense splitting — upload invoices, assign line items, "
            "settle who owes whom."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.DEBUG else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Built-in routes
    # ------------------------------------------------------------------
    @app.get("/health", tags=["meta"], summary="Liveness check")
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.APP_ENV}

    # ------------------------------------------------------------------
    # Feature routers
    # ------------------------------------------------------------------
    from app.api.router import router as v1_router  # noqa: PLC0415

    app.include_router(v1_router)

    return app


app = create_app()
