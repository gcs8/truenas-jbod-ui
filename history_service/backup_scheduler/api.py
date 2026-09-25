"""Internal HTTP API of the backup scheduler, served on a Unix socket.

Only the admin sidecar mounts the socket directory; it proxies the public
``/api/admin/backups/*`` routes here after its own auth and origin checks.
Paths mirror the public contract under ``/internal/backups``. Restore is not
served here: the admin sidecar downloads the archive through
``/internal/backups/{id}/download`` and runs its existing inspect/import code.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from history_service.backup_archive.catalog import CatalogError
from history_service.backup_scheduler.service import (
    ArchiveIntegrityError,
    ArtifactNotFoundError,
    BackupScheduler,
    SchedulerBusyError,
    describe_error,
)

logger = logging.getLogger(__name__)
MAX_REASON_CHARS = 200
_ACTOR_HEADER = "X-Backup-Actor"


def _actor(request: Request) -> str:
    raw = " ".join(str(request.headers.get(_ACTOR_HEADER) or "admin").split())
    return raw[:64] or "admin"


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    return payload


def _plan_item(item: Any, *, with_kind: bool) -> dict[str, Any]:
    payload = {
        "id": item.record.artifact_id,
        "location": item.record.location,
        "backup_class": item.record.backup_class,
        "reason": item.reason,
    }
    if with_kind:
        payload["kind"] = item.kind
    return payload


def build_app(scheduler: BackupScheduler) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ArtifactNotFoundError)
    async def not_found(_: Request, exc: ArtifactNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc) or "Backup not found."}, status_code=404)

    @app.exception_handler(SchedulerBusyError)
    async def busy(_: Request, exc: SchedulerBusyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/internal/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/internal/backups")
    async def library() -> dict[str, Any]:
        return await asyncio.to_thread(scheduler.library)

    @app.post("/internal/backups/run")
    async def run(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        backup_class = payload.get("backup_class")
        if backup_class not in ("config", "full"):
            raise HTTPException(status_code=400, detail="backup_class must be config or full.")
        await asyncio.to_thread(scheduler.start_run, backup_class)  # SchedulerBusyError -> 409
        return JSONResponse({"ok": True, "backup_class": backup_class, "state": "started"}, status_code=202)

    @app.get("/internal/backups/lifecycle/plan")
    async def plan() -> dict[str, Any]:
        token, expires_at, grooming = await asyncio.to_thread(scheduler.plan)
        return {
            "plan_token": token,
            "expires_at": expires_at.isoformat(),
            "items": [_plan_item(item, with_kind=True) for item in grooming.items],
            "guarded": [_plan_item(item, with_kind=False) for item in grooming.guarded],
        }

    @app.post("/internal/backups/lifecycle/apply")
    async def apply(request: Request) -> dict[str, Any]:
        payload = await _json_body(request)
        try:
            result = await asyncio.to_thread(scheduler.apply, payload.get("plan_token"))
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "ok": result.complete,
            "deleted": [item.record.artifact_id for item in result.deleted],
            "already_missing": [item.record.artifact_id for item in result.already_missing],
            "failed": (
                {"id": result.failed.record.artifact_id, "error": (result.error or "")[:300]}
                if result.failed is not None
                else None
            ),
            "not_attempted": [item.record.artifact_id for item in result.not_attempted],
        }

    @app.post("/internal/backups/targets/{target_id}/test")
    async def test_target(target_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(scheduler.test_target, target_id)

    @app.get("/internal/backups/{artifact_id}")
    async def detail(artifact_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(scheduler.detail, artifact_id)

    @app.post("/internal/backups/{artifact_id}/verify")
    async def verify(artifact_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(scheduler.verify, artifact_id)

    @app.post("/internal/backups/{artifact_id}/preserve")
    async def preserve(artifact_id: str, request: Request) -> dict[str, Any]:
        payload = await _json_body(request)
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON_CHARS:
            raise HTTPException(status_code=400, detail=f"reason must be 1-{MAX_REASON_CHARS} characters of text.")
        try:
            artifact = await asyncio.to_thread(
                scheduler.preserve, artifact_id, reason=reason.strip(), actor=_actor(request)
            )
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "artifact": artifact}

    @app.delete("/internal/backups/{artifact_id}/preserve")
    async def unpreserve(artifact_id: str, request: Request) -> dict[str, Any]:
        try:
            artifact = await asyncio.to_thread(scheduler.unpreserve, artifact_id, actor=_actor(request))
        except CatalogError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "artifact": artifact}

    @app.get("/internal/backups/{artifact_id}/download")
    async def download(artifact_id: str) -> FileResponse:
        stack = contextlib.ExitStack()
        try:
            record, path = await asyncio.to_thread(stack.enter_context, scheduler.materialize(artifact_id))
        except ArtifactNotFoundError:
            stack.close()
            raise
        except ArchiveIntegrityError as exc:
            stack.close()
            raise HTTPException(status_code=409, detail=f"{exc} Verify it, or use another copy.") from exc
        except Exception as exc:  # noqa: BLE001 - remote fetch failures are operator-facing
            stack.close()
            raise HTTPException(status_code=502, detail=f"Could not read the backup: {describe_error(exc)}") from exc
        filename = record.name.rsplit("/", 1)[-1]
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=filename,
            headers={"X-Backup-Sha256": record.sha256, "X-Backup-Class": record.backup_class},
            background=BackgroundTask(stack.close),
        )

    return app
