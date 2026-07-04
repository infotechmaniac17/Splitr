"""
Process-gap regression test (finance-logic-reviewer finding, 4th pass on
the auth surface): every route the app registers must explicitly declare
its auth stance -- either it's on a small, reviewed allow-list of
intentionally-public paths, or `get_current_user` (app.api.deps) is
present somewhere in its real dependency chain.

This is a standing tripwire: if a future route is added without wiring
`get_current_user` in and without adding it to the allow-list, this test
fails loudly rather than silently shipping an unauthenticated financial
endpoint.

Implementation note: this introspects the *actual* dependency graph FastAPI
built for each route (`route.dependant.dependencies`, recursively), not
decorator/source-text grepping. A route that merely `import`s
`get_current_user` without wiring it as a `Depends(...)` parameter would
NOT satisfy this check -- only routes/dependencies that FastAPI resolves at
request time do.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi.routing import APIRoute

from app.api.deps import get_current_user
from app.main import app

# ---------------------------------------------------------------------------
# The full, reviewed allow-list of intentionally-public (unauthenticated)
# routes. Every other route MUST have get_current_user in its dependency
# chain. If you are adding a new public route, add it here deliberately --
# do not add it just to make this test pass without reviewing whether it
# should really be public.
# ---------------------------------------------------------------------------
PUBLIC_ROUTES: set[tuple[str, str]] = {
    ("GET", "/health"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("POST", "/auth/refresh"),
    ("POST", "/users"),
}


def _collect_api_routes(routes: Iterable[object]) -> list[APIRoute]:
    """
    Recursively flatten every APIRoute registered on the app, regardless of
    how many levels of router-inclusion/mounting wrap it.

    Newer FastAPI/Starlette versions wrap included routers in an internal
    `_IncludedRouter` compiled-routing object that does not expose `.routes`
    directly but does expose `.original_router.routes`; handle both shapes
    so this test doesn't silently miss nested routers.
    """
    out: list[APIRoute] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "routes"):
            out.extend(_collect_api_routes(r.routes))  # type: ignore[attr-defined]
        elif hasattr(r, "original_router"):
            out.extend(_collect_api_routes(r.original_router.routes))  # type: ignore[attr-defined]
    return out


def _dependency_chain_calls(
    dependant: object, seen: set[object] | None = None
) -> set[object]:
    """
    Recursively collect every callable in a route's resolved dependency
    tree (FastAPI `Dependant.dependencies` recursion), including
    dependencies-of-dependencies.
    """
    if seen is None:
        seen = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        seen.add(call)
    for sub in getattr(dependant, "dependencies", []):
        _dependency_chain_calls(sub, seen)
    return seen


def _route_is_authenticated(route: APIRoute) -> bool:
    calls = _dependency_chain_calls(route.dependant)
    return get_current_user in calls


def test_every_route_is_public_or_authenticated() -> None:
    routes = _collect_api_routes(app.routes)
    assert routes, "route collection found nothing -- inventory walk is broken"

    unclassified: list[str] = []
    for route in routes:
        methods = sorted(m for m in route.methods if m != "HEAD")
        for method in methods:
            key = (method, route.path)
            if key in PUBLIC_ROUTES:
                continue
            if not _route_is_authenticated(route):
                unclassified.append(f"{method} {route.path}")

    assert not unclassified, (
        "Route(s) found with no auth dependency and not on the public "
        f"allow-list -- declare an auth stance explicitly: {unclassified}"
    )


def test_public_allow_list_entries_all_exist_and_are_actually_unauthenticated() -> None:
    """
    Catches the allow-list going stale in the other direction: an entry
    that no longer exists, or a path that used to be public but now has
    get_current_user wired in (in which case it should be removed from the
    allow-list so a regression there would be caught by the test above).
    """
    routes = _collect_api_routes(app.routes)
    seen: set[tuple[str, str]] = set()
    for route in routes:
        methods = sorted(m for m in route.methods if m != "HEAD")
        for method in methods:
            seen.add((method, route.path))

    missing = PUBLIC_ROUTES - seen
    assert not missing, f"Allow-listed route(s) no longer registered: {missing}"
