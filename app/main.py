from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app import __version__
from app.logging_config import configure_logging
from app.models.domain import InventorySnapshot, LedAction, LedRequest, MappingBundle, MappingRequest, SmartSummaryView
from app.services.inventory_registry import InventoryRegistry
from app.services.truenas_ws import TrueNASAPIError

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

logger = logging.getLogger(__name__)


@lru_cache
def get_inventory_registry() -> InventoryRegistry:
    settings = get_settings()
    configure_logging(settings)
    return InventoryRegistry(settings)
def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="TrueNAS JBOD Enclosure UI",
        version=__version__,
        docs_url="/docs" if settings.app.debug else None,
        redoc_url="/redoc" if settings.app.debug else None,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> HTMLResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        snapshot = await service.get_snapshot(selected_enclosure_id=enclosure_id)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "snapshot": snapshot,
                "settings": settings,
                "initial_snapshot_json": json.dumps(snapshot.model_dump(mode="json")),
            },
        )

    @app.get("/api/inventory", response_model=InventorySnapshot)
    async def get_inventory(
        force: bool = False,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> InventorySnapshot:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        return await service.get_snapshot(force_refresh=force, selected_enclosure_id=enclosure_id)

    @app.post("/api/slots/{slot}/led")
    async def set_slot_led(
        slot: int,
        payload: LedRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        ensure_slot_bounds(settings, slot)
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        try:
            await service.set_slot_led(slot, payload.action, selected_enclosure_id=enclosure_id)
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snapshot = await service.get_snapshot(force_refresh=True, selected_enclosure_id=enclosure_id)
        return JSONResponse({"ok": True, "snapshot": snapshot.model_dump(mode="json")})

    @app.post("/api/slots/{slot}/mapping")
    async def save_mapping(
        slot: int,
        payload: MappingRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        ensure_slot_bounds(settings, slot)
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        mapping_payload = {
            "serial": payload.serial,
            "device_name": payload.device_name,
            "gptid": payload.gptid,
            "notes": payload.notes,
        }
        mapping = await service.save_mapping(slot, mapping_payload, selected_enclosure_id=enclosure_id)

        led_warning = None
        if payload.clear_identify_after_save:
            try:
                await service.set_slot_led(slot, LedAction.clear, selected_enclosure_id=enclosure_id)
            except Exception as exc:  # noqa: BLE001 - surface as non-fatal warning.
                led_warning = str(exc)

        snapshot = await service.get_snapshot(force_refresh=True, selected_enclosure_id=enclosure_id)
        return JSONResponse(
            {
                "ok": True,
                "mapping": mapping.model_dump(mode="json"),
                "snapshot": snapshot.model_dump(mode="json"),
                "warning": led_warning,
            }
        )

    @app.delete("/api/slots/{slot}/mapping")
    async def clear_mapping(
        slot: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        ensure_slot_bounds(settings, slot)
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        cleared = await service.clear_mapping(slot, selected_enclosure_id=enclosure_id)
        snapshot = await service.get_snapshot(force_refresh=True, selected_enclosure_id=enclosure_id)
        return JSONResponse({"ok": cleared, "snapshot": snapshot.model_dump(mode="json")})

    @app.get("/api/mappings/export", response_model=MappingBundle)
    async def export_mappings(
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> MappingBundle:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        return await service.export_mapping_bundle(selected_enclosure_id=enclosure_id)

    @app.post("/api/mappings/import")
    async def import_mappings(
        payload: MappingBundle,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        imported = await service.import_mapping_bundle(payload, selected_enclosure_id=enclosure_id)
        snapshot = await service.get_snapshot(force_refresh=True, selected_enclosure_id=enclosure_id)
        return JSONResponse(
            {
                "ok": True,
                "imported": imported,
                "snapshot": snapshot.model_dump(mode="json"),
            }
        )

    @app.get("/api/slots/{slot}/smart", response_model=SmartSummaryView)
    async def get_slot_smart_summary(
        slot: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> SmartSummaryView:
        ensure_slot_bounds(settings, slot)
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        try:
            return await service.get_slot_smart_summary(slot, selected_enclosure_id=enclosure_id)
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(None)
        snapshot = await service.get_snapshot()
        api_status = snapshot.sources.get("api")
        return JSONResponse(
            {
                "status": "ok",
                "dependency_status": "ok" if api_status and api_status.ok else "degraded",
                "last_updated": snapshot.last_updated.isoformat(),
                "sources": snapshot.model_dump(mode="json").get("sources", {}),
                "warnings": snapshot.warnings,
            },
            status_code=200,
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"ok": False, "detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled application error")
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)

    return app


def ensure_slot_bounds(settings: Settings, slot: int) -> None:
    if slot < 0 or slot >= settings.layout.slot_count:
        raise HTTPException(status_code=404, detail=f"Slot {slot} is outside configured layout.")


app = create_app()
