"""History endpoints of the main web application.

``app.routes.build_router`` composes the main router and calls the two
functions below at the places these routes have always had in its route table.
They add the routes to that router; this module never imports ``app.routes``.

``app.routes`` passes itself in as ``routes``. Every function and limit these
routes used from ``app.routes`` is read from it when the route is registered
(the mutation guard) or when a request runs (everything else), so a
replacement made on ``app.routes`` still takes effect. Exception and request
model classes are imported from the modules that define them; they are the
same objects ``app.routes`` imports.
"""

from __future__ import annotations

import json
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from types import ModuleType
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.route_support import (
    HistoryRefreshProxyRequest,
    HistoryScopeProxyRequest,
    HistoryScopesProxyRequest,
)
from app.services.history_backend import (
    HistoryBackendBusyError,
    HistoryBackendPolicyError,
)
from app.services.truenas_ws import TrueNASAPIError
from history_service.operation_bounds import (
    HistoryBudgetExceeded,
    HistoryRequestShapeError,
)


def _history_service_refusal_detail(slot_count: int) -> str:
    """Page text for a read the history service refused under its own policy."""
    return (
        f"The history service refused this request for {slot_count} slots. "
        "Check the history service log."
    )


def add_storage_view_slot_history_route(router: APIRouter, routes: ModuleType) -> None:
    """Add the per-slot history route of a storage view, between its SMART routes."""

    @router.get("/api/storage-views/{view_id}/slots/{slot_index}/history")
    async def get_storage_view_slot_history(
        view_id: str,
        slot_index: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        window_hours: int | None = None,
    ) -> JSONResponse:
        registry = routes.get_inventory_registry()
        service = registry.get_service(system_id)
        history_slot, history_enclosure_id = await service.resolve_storage_view_slot_history_target(
            view_id,
            slot_index,
            selected_enclosure_id=enclosure_id,
        )
        routes.add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            storage_view_id=view_id,
            slot=slot_index,
            history_slot=history_slot,
            enclosure_id=history_enclosure_id,
        )
        history_backend = routes.get_history_backend()
        payload = await history_backend.get_slot_history(
            history_slot,
            service.system.id,
            history_enclosure_id,
            window_hours=window_hours,
        )
        return JSONResponse(payload)


def add_history_routes(router: APIRouter, routes: ModuleType) -> None:
    """Add the history status, refresh, bundle, slot, scope and storage-view routes."""

    @router.get("/api/history/status")
    async def get_history_status() -> JSONResponse:
        history_backend = routes.get_history_backend()
        payload = await history_backend.get_status()
        return JSONResponse(
            {
                **payload,
                "collector": routes.project_public_collector_status(
                    payload.get("collector"),
                    last_error_detail=routes.HISTORY_BACKEND_DEGRADED_DETAIL,
                ),
            }
        )

    @router.post(
        "/api/history/refresh",
        dependencies=[Depends(routes.require_read_ui_mutation_authorization)],
    )
    async def refresh_history_proxy(payload: HistoryRefreshProxyRequest) -> JSONResponse:
        history_backend = routes.get_history_backend()
        try:
            result = await history_backend.refresh(payload.mode)
        except HistoryBackendPolicyError as exc:
            raise HTTPException(status_code=exc.status_code, detail="The history service refused this request. Check the history service log.") from exc
        return JSONResponse(result)

    history_scopes_request_schema = HistoryScopesProxyRequest.model_json_schema()
    history_scopes_request_schema["properties"]["scopes"]["items"] = (
        HistoryScopeProxyRequest.model_json_schema()
    )
    history_scopes_request_schema.pop("$defs", None)

    @router.post(
        "/api/history/scopes/bundle",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": history_scopes_request_schema,
                    }
                },
            }
        },
    )
    async def get_history_scopes_bundle(request: Request) -> JSONResponse:
        if not routes._is_json_media_type(request.headers.get("content-type")):
            raise HTTPException(
                status_code=415,
                detail="Send this request with Content-Type: application/json.",
            )
        body = await routes.read_limited_request_body(
            request,
            limit=routes.MAX_HISTORY_SCOPES_REQUEST_BYTES,
            detail="History request is too large.",
        )
        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="History request must be valid JSON.") from exc
        try:
            payload = HistoryScopesProxyRequest.model_validate(document)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="History request body is invalid.") from exc
        history_backend = routes.get_history_backend()
        try:
            result = await history_backend.get_scopes_history(
                scopes=[scope.model_dump() for scope in payload.scopes],
                metrics=payload.metrics,
                since=payload.since,
                event_limit=payload.event_limit,
                metric_limit=payload.metric_limit,
            )
        except HistoryBackendBusyError:
            return routes._history_read_busy_response()
        except HistoryBackendPolicyError as exc:
            raise HTTPException(status_code=exc.status_code, detail="The history service refused this request. Check the history service log.") from exc
        except (HistoryRequestShapeError, HistoryBudgetExceeded, ValueError) as exc:
            raise HTTPException(
                status_code=413 if isinstance(exc, HistoryBudgetExceeded) else 422,
                detail=str(exc),
            ) from exc
        return JSONResponse(result)

    @router.get("/api/slots/{slot}/history")
    async def get_slot_history(
        slot: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        window_hours: int | None = None,
    ) -> JSONResponse:
        registry = routes.get_inventory_registry()
        service = registry.get_service(system_id)
        layout_bounds = await routes.ensure_read_slot_bounds(slot, service, enclosure_id)
        resolved_system_id = service.system.id
        routes.add_perf_metadata(
            system_id=resolved_system_id,
            platform=service.system.truenas.platform,
            slot=slot,
            enclosure_id=enclosure_id,
            history_window_hours=window_hours,
        )
        history_backend = routes.get_history_backend()
        payload = await history_backend.get_slot_history(
            slot,
            resolved_system_id,
            enclosure_id,
            window_hours=window_hours,
        )
        return JSONResponse({**payload, "layout_bounds": layout_bounds})

    @router.get("/api/history/scope")
    async def get_history_scope(
        system_id: str | None = None,
        enclosure_id: str | None = None,
        slots: list[int] | None = Query(default=None),
        window_hours: int | None = None,
        metrics: list[str] | None = Query(default=None),
        event_limit: int = Query(default=12),
        metric_limit: int = 60,
    ) -> JSONResponse:
        requested_slots = [int(slot) for slot in (slots or [])]
        if len(requested_slots) > routes.MAX_TARGETS:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"History can be requested for at most {routes.MAX_TARGETS} slots at a time; "
                    f"{len(requested_slots)} requested."
                ),
            )
        registry = routes.get_inventory_registry()
        service = registry.get_service(system_id)
        if not requested_slots or not isinstance(window_hours, int) or not 1 <= window_hours <= 8760:
            raise HTTPException(status_code=422, detail="Choose at least one slot and a time range between 1 hour and 1 year.")
        selected_metrics = metrics or list(routes.ALLOWED_HISTORY_METRICS)
        since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        try:
            routes.build_history_read_plan(
                scopes=[{
                    "system_id": service.system.id,
                    "enclosure_id": enclosure_id,
                    "slots": requested_slots,
                }],
                metrics=selected_metrics,
                since=since,
                event_limit=event_limit,
                metric_limit=metric_limit,
            )
        except (HistoryRequestShapeError, HistoryBudgetExceeded) as exc:
            raise HTTPException(
                status_code=413 if isinstance(exc, HistoryBudgetExceeded) else 422,
                detail=str(exc),
            ) from exc
        normalized_slots = sorted(set(requested_slots))
        layout_slots, layout_bounds = await routes.resolve_read_layout_slots(service, enclosure_id)
        for slot in normalized_slots:
            if layout_slots is not None:
                routes.check_slot_bounds(slot, layout_slots)
        routes.add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            enclosure_id=enclosure_id,
            slot_count=len(normalized_slots),
            history_window_hours=window_hours,
        )
        history_backend = routes.get_history_backend()
        try:
            payload = await history_backend.get_scope_history(
                system_id=service.system.id,
                enclosure_id=enclosure_id,
                slots=normalized_slots,
                window_hours=window_hours,
                metrics=selected_metrics,
                event_limit=event_limit,
                metric_limit=metric_limit,
            )
        except HistoryBackendBusyError:
            return routes._history_read_busy_response()
        except HistoryBackendPolicyError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=_history_service_refusal_detail(len(normalized_slots)),
            ) from exc
        except (HistoryRequestShapeError, HistoryBudgetExceeded) as exc:
            raise HTTPException(
                status_code=413 if isinstance(exc, HistoryBudgetExceeded) else 422,
                detail=str(exc),
            ) from exc
        return JSONResponse(
            {
                "configured": history_backend.configured,
                "system_id": service.system.id,
                "enclosure_id": enclosure_id,
                "layout_bounds": layout_bounds,
                "histories": {str(slot): history for slot, history in payload.items()},
            }
        )

    @router.get("/api/storage-views/{view_id}/history")
    async def get_storage_view_history(
        view_id: str,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        window_hours: int | None = None,
        metrics: list[str] | None = Query(default=None),
        event_limit: int = Query(default=12),
        metric_limit: int = 60,
    ) -> JSONResponse:
        if not isinstance(window_hours, int) or not 1 <= window_hours <= 8760:
            raise HTTPException(status_code=422, detail="Choose a time range between 1 hour and 1 year.")
        registry = routes.get_inventory_registry()
        service = registry.get_service(system_id)
        try:
            runtime = await service.get_storage_view_runtime(
                force_refresh=False,
                selected_enclosure_id=enclosure_id,
            )
        except TrueNASAPIError as exc:
            raise routes._read_failure_http_exception(
                exc,
                operation="Storage view history request",
                detail=routes.STORAGE_VIEW_HISTORY_FAILURE_DETAIL,
            ) from exc
        runtime_view = next((view for view in runtime.views if view.id == view_id), None)
        if not runtime_view:
            raise HTTPException(status_code=404, detail=f'The saved view "{view_id}" does not exist on this system.')

        display_slot_by_target: dict[tuple[str | None, int], list[int]] = {}
        slots_by_enclosure: dict[str | None, set[int]] = {}
        bay_enclosure_id = (
            runtime_view.backing_enclosure_id
            if getattr(runtime_view, "source", None) == "selected_enclosure_snapshot"
            and runtime_view.backing_enclosure_id
            else enclosure_id or runtime_view.backing_enclosure_id
        )
        for runtime_slot in runtime_view.slots:
            # A bay number only names a disk inside its own enclosure; a candidate
            # from another enclosure reads the view's own history scope instead.
            if runtime_slot.snapshot_slot is not None and runtime_slot.snapshot_enclosure_id in {None, bay_enclosure_id}:
                history_slot = int(runtime_slot.snapshot_slot)
                history_enclosure_id = bay_enclosure_id
            else:
                history_slot = int(runtime_slot.slot_index)
                history_enclosure_id = f"storage-view:{runtime_view.id}"
            slots_by_enclosure.setdefault(history_enclosure_id, set()).add(history_slot)
            display_slot_by_target.setdefault(
                (history_enclosure_id, history_slot),
                [],
            ).append(int(runtime_slot.slot_index))

        routes.add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            storage_view_id=view_id,
            slot_count=len(runtime_view.slots),
            history_window_hours=window_hours,
        )
        selected_metrics = metrics or list(routes.ALLOWED_HISTORY_METRICS)
        scopes = [
            {
                "system_id": service.system.id,
                "enclosure_id": history_enclosure_id,
                "slots": sorted(history_slots),
            }
            for history_enclosure_id, history_slots in slots_by_enclosure.items()
        ]
        history_backend = routes.get_history_backend()
        try:
            payload = await history_backend.get_scopes_history(
                scopes=scopes,
                since=(datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat(),
                metrics=selected_metrics,
                event_limit=event_limit,
                metric_limit=metric_limit,
            )
        except HistoryBackendBusyError:
            return routes._history_read_busy_response()
        except HistoryBackendPolicyError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=_history_service_refusal_detail(
                    sum(len(scope["slots"]) for scope in scopes)
                ),
            ) from exc
        except (HistoryRequestShapeError, HistoryBudgetExceeded) as exc:
            # The local read planner rejected the request shape or budget
            # before any backend call (#1044); other errors stay server errors.
            raise HTTPException(
                status_code=413 if isinstance(exc, HistoryBudgetExceeded) else 422,
                detail=str(exc),
            ) from exc
        histories_by_display_slot: dict[str, dict[str, Any]] = {}
        for scope_payload in payload.get("scopes", []):
            if not isinstance(scope_payload, dict):
                continue
            history_enclosure_id = scope_payload.get("enclosure_id")
            histories = scope_payload.get("histories")
            if not isinstance(histories, dict):
                continue
            for history_slot, history_payload in histories.items():
                if not isinstance(history_payload, dict):
                    continue
                for display_slot in display_slot_by_target.get(
                    (history_enclosure_id, int(history_slot)), []
                ):
                    histories_by_display_slot[str(display_slot)] = history_payload

        return JSONResponse(
            {
                "configured": history_backend.configured,
                "system_id": service.system.id,
                "storage_view_id": runtime_view.id,
                "histories": histories_by_display_slot,
            }
        )
