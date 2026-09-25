from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from admin_service.config import get_admin_settings
from admin_service.route_support import (
    BASE_DIR,
    _basic_auth_matches,
    _request_origin_allowed,
    _shutdown_after_ttl,
    admin_error_response,
    cross_origin_rejection_detail,
    get_esxi_host_prep_service,
    get_release_status_service,
    logger,
    system_not_configured_exception_handler,
    validate_admin_public_origin,
)
from admin_service.routes import build_router
from app import __version__
from app.metrics import install_metrics, metrics_path
from app.router_inclusion import include_router_preserving_route_objects
from app.services.inventory_registry import SystemNotConfiguredError


def create_app() -> FastAPI:
    admin_settings = get_admin_settings()
    validate_admin_public_origin(admin_settings)
    admin_metrics_path = metrics_path()
    if (
        admin_metrics_path in {"/", "/livez", "/healthz", "/openapi.json"}
        or admin_metrics_path == "/static"
        or admin_metrics_path.startswith("/static/")
        or admin_metrics_path == "/api"
        or admin_metrics_path.startswith("/api/")
    ):
        raise ValueError(
            "METRICS_PATH must not overlap an admin UI, health, static, or API route."
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        shutdown_task: asyncio.Task[None] | None = None
        release_task: asyncio.Task[None] | None = None
        try:
            cleanup_summary = await asyncio.to_thread(
                get_esxi_host_prep_service().prune_stale_packages
            )
        except Exception as exc:
            logger.warning(
                "Host-prep staging cleanup failed safely error_type=%s",
                type(exc).__name__,
            )
        else:
            logger.info(
                "Host-prep staging cleanup completed removed=%s skipped=%s failed=%s limited=%s",
                cleanup_summary["removed"],
                cleanup_summary["skipped"],
                cleanup_summary["failed"],
                cleanup_summary["limited"],
            )
        if admin_settings.auto_stop_seconds > 0:
            shutdown_task = asyncio.create_task(_shutdown_after_ttl(admin_settings.auto_stop_seconds))
        release_task = asyncio.create_task(get_release_status_service().run_periodic_refresh())
        try:
            yield
        finally:
            if release_task is not None and not release_task.done():
                release_task.cancel()
                try:
                    await release_task
                except asyncio.CancelledError:
                    pass
            if shutdown_task is not None and not shutdown_task.done():
                shutdown_task.cancel()
                try:
                    await shutdown_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title=admin_settings.app_name,
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def enforce_admin_authentication(request: Request, call_next):
        public_paths = {
            "/livez",
            "/healthz",
            admin_metrics_path,
        }
        if (
            request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
            and not _request_origin_allowed(request, admin_settings)
        ):
            return admin_error_response(cross_origin_rejection_detail(request, admin_settings), 403)
        if (
            admin_settings.auth_mode != "basic"
            or request.url.path in public_paths
            or _basic_auth_matches(request.headers.get("authorization"), admin_settings)
        ):
            return await call_next(request)
        return admin_error_response(
            "Admin authentication required.",
            401,
            headers={"WWW-Authenticate": 'Basic realm="truenas-jbod-admin"'},
        )

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    install_metrics(app, service_name="enclosure-admin", version=__version__)

    include_router_preserving_route_objects(
        app,
        build_router(admin_settings),
    )
    app.add_exception_handler(SystemNotConfiguredError, system_not_configured_exception_handler)

    # Registered on Starlette's HTTPException so router-raised failures (404 for an
    # unknown admin path, 405 for a wrong method) carry a correlation id too;
    # fastapi.HTTPException is a subclass, so one handler covers both.
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return admin_error_response(
            exc.detail,
            exc.status_code,
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        return admin_error_response(
            "Unhandled admin service error; see admin logs.",
            500,
            log_level=logging.ERROR,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    return app

app = create_app()
