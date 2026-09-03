from __future__ import annotations

import inspect
from functools import wraps
from types import ModuleType
from typing import Any, Callable, MutableMapping

from fastapi import APIRouter, FastAPI


class MainModuleAPIRouter(APIRouter):
    """Bind moved handlers to the main module's patchable compatibility facade."""

    def __init__(
        self,
        main_module: ModuleType,
        route_globals: MutableMapping[str, Any],
    ) -> None:
        super().__init__()
        self._main_module = main_module
        self._route_globals = route_globals
        self.sync_main_globals()

    def sync_main_globals(self) -> None:
        for name, value in vars(self._main_module).items():
            if not name.startswith("__") or name == "__version__":
                self._route_globals[name] = value

    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        **kwargs: Any,
    ) -> None:
        @wraps(endpoint)
        async def late_bound_endpoint(*args: Any, **call_kwargs: Any) -> Any:
            self.sync_main_globals()
            result = endpoint(*args, **call_kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        super().add_api_route(path, late_bound_endpoint, **kwargs)


def include_router_preserving_route_objects(
    app: FastAPI,
    router: APIRouter,
) -> None:
    """Include a router while retaining the app.routes compatibility contract."""

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
