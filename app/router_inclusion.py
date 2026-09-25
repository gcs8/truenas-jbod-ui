from __future__ import annotations

from fastapi import APIRouter, FastAPI


def include_router_preserving_route_objects(
    app: FastAPI,
    router: APIRouter,
) -> None:
    """Include a router so its APIRoute objects sit directly in ``app.routes``.

    FastAPI 0.141 wraps an included router in one ``_IncludedRouter`` entry;
    tests and URL lookups expect the individual routes instead.
    """

    route_count = len(app.router.routes)
    app.include_router(router)
    included_routes = app.router.routes[route_count:]
    if (
        len(included_routes) == 1
        and getattr(included_routes[0], "original_router", None) is router
    ):
        app.router.routes[route_count:] = router.routes
        mark_routes_changed = getattr(app.router, "_mark_routes_changed", None)
        if mark_routes_changed is not None:
            mark_routes_changed()
