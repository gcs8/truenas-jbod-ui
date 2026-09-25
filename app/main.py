from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import get_settings
from app.http_auth import configured_origin_identity
from app.logging_config import configure_logging
from app.metrics import install_metrics
from app.perf import install_perf_timing_middleware
from app.read_ui_auth_config import load_read_ui_auth_settings
from app.route_support import (
    BASE_DIR,
    EXCEPTION_RESPONSES,
    get_inventory_registry,
    get_release_status_service,
    logger,
    mapped_exception_handler,
    mapping_durability_exception_handler,
    mapping_scope_conflict_exception_handler,
    split_known_hosts_paths,
    ui_writable_directories,
    warm_admin_probe,
)
from app.router_inclusion import include_router_preserving_route_objects
from app.routes import build_router
from app.services.mapping_store import (
    MappingDurabilityError,
    MappingScopeConflict,
)
from app.services.profile_registry import build_profile_reference_warnings
from app.services.storage_writability import (
    probe_known_hosts_files,
    probe_writable_directories,
)


def create_app() -> FastAPI:
    startup_settings = get_settings()
    operator_auth_settings = load_read_ui_auth_settings()
    if (
        operator_auth_settings.auth_mode == "basic"
        and configured_origin_identity(startup_settings.app.public_origin) is None
    ):
        raise ValueError(
            "APP_PUBLIC_ORIGIN must be an absolute HTTP(S) origin when ADMIN_AUTH_MODE=basic."
        )
    configure_logging(startup_settings)
    for warning in build_profile_reference_warnings(startup_settings):
        logger.warning("Configuration warning: %s", warning["message"])

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        warm_task: asyncio.Task[None] | None = None
        release_task: asyncio.Task[None] | None = None
        if startup_settings.app.startup_warm_cache_enabled:
            registry = get_inventory_registry()
            warm_task = asyncio.create_task(
                registry.prewarm_all(warm_smart=startup_settings.app.startup_warm_smart_enabled)
            )
        release_task = asyncio.create_task(get_release_status_service().run_periodic_refresh())
        # A daemon thread, so a slow DNS miss here never holds up shutdown.
        threading.Thread(
            target=warm_admin_probe, args=(startup_settings,), name="admin-probe-warm", daemon=True,
        ).start()
        try:
            yield
        finally:
            if release_task is not None and not release_task.done():
                release_task.cancel()
                try:
                    await release_task
                except asyncio.CancelledError:
                    pass
            if warm_task is not None and not warm_task.done():
                warm_task.cancel()
                try:
                    await warm_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="TrueNAS JBOD Enclosure UI",
        version=__version__,
        docs_url="/docs" if startup_settings.app.debug else None,
        redoc_url="/redoc" if startup_settings.app.debug else None,
        lifespan=lifespan,
    )
    app.state.operator_auth_settings = operator_auth_settings
    app.state.read_ui_public_origin = startup_settings.app.public_origin
    writable_directories = tuple(ui_writable_directories(startup_settings))
    _, configured_known_hosts = split_known_hosts_paths(startup_settings)
    known_hosts_files = tuple(configured_known_hosts)
    startup_problems = [
        *probe_writable_directories(writable_directories),
        *probe_known_hosts_files(known_hosts_files),
    ]
    for problem in startup_problems:
        logger.error("%s", problem)
    app.state.startup_problems = tuple(startup_problems)
    app.state.writable_directories = writable_directories
    app.state.known_hosts_files = known_hosts_files
    app.state.storage_checked_at_monotonic = time.monotonic()

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    install_metrics(app, service_name="enclosure-ui", version=__version__)
    install_perf_timing_middleware(app, startup_settings)

    include_router_preserving_route_objects(app, build_router())
    for mapped_exception_type in EXCEPTION_RESPONSES:
        app.add_exception_handler(mapped_exception_type, mapped_exception_handler)
    app.add_exception_handler(
        MappingScopeConflict,
        mapping_scope_conflict_exception_handler,
    )
    app.add_exception_handler(
        MappingDurabilityError,
        mapping_durability_exception_handler,
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled application error", exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse(
            {"ok": False, "detail": "Something went wrong on the server. The application log has details."},
            status_code=500,
        )

    return app

app = create_app()
