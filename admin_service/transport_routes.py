"""SSH key and TLS trust endpoints of the admin service.

``admin_service.routes.build_router`` composes the admin router and calls
``add_transport_routes`` at the place these routes have always had in its route
table. It adds them to that router; this module never imports
``admin_service.routes``.

``admin_service.routes`` passes itself in as ``routes``. The settings reload,
the SSH key manager, the TLS trust store service and the TLS host check are
read from it when a request runs, so a replacement made on
``admin_service.routes`` still takes effect. Request model classes are imported
from the module that defines them; they are the same objects
``admin_service.routes`` imports.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import ModuleType

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
)
from fastapi.responses import JSONResponse

from app.services.ssh_key_manager import list_keys_with_warnings
from app.models.domain import (
    SSHKeyGenerateRequest,
    TLSCertificateImportRequest,
    TLSCertificateInspectRequest,
    TLSRemoteCertificateTrustRequest,
)


def add_transport_routes(router: APIRouter, routes: ModuleType) -> None:
    """Add the SSH key and TLS trust routes, between ESXi host prep and QuantaStor discovery."""

    @router.get("/api/admin/ssh-keys")
    async def list_ssh_keys() -> JSONResponse:
        settings = routes.reload_app_settings()
        key_manager = routes.SSHKeyManager(settings.config_file)
        keys, warnings = await asyncio.to_thread(list_keys_with_warnings, key_manager, settings)
        return JSONResponse({"ok": True, "keys": keys, "ssh_key_warnings": warnings})

    @router.post("/api/admin/ssh-keys/generate")
    async def generate_ssh_key(payload: SSHKeyGenerateRequest) -> JSONResponse:
        settings = routes.reload_app_settings()
        key_manager = routes.SSHKeyManager(settings.config_file)
        try:
            generated_key = await asyncio.to_thread(key_manager.generate_keypair, payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        keys, warnings = await asyncio.to_thread(list_keys_with_warnings, key_manager, settings)
        return JSONResponse({"ok": True, "key": generated_key, "keys": keys, "ssh_key_warnings": warnings})

    @router.post("/api/admin/tls/inspect")
    async def inspect_tls_certificate(payload: TLSCertificateInspectRequest) -> JSONResponse:
        settings = routes.reload_app_settings()
        trust_service = routes.TLSTrustStoreService(settings.config_file)
        try:
            inspection = await asyncio.to_thread(
                trust_service.inspect_remote_certificate,
                payload.host,
                payload.timeout_seconds,
                tls_server_name=payload.tls_server_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "inspection": inspection})

    @router.post("/api/admin/tls/import")
    async def import_tls_bundle(payload: TLSCertificateImportRequest) -> JSONResponse:
        settings = routes.reload_app_settings()
        trust_service = routes.TLSTrustStoreService(settings.config_file)
        try:
            imported = await asyncio.to_thread(
                trust_service.import_pem_bundle,
                payload.pem_text,
                bundle_name=payload.bundle_name,
                system_id=payload.system_id,
                host=payload.host,
            )
            validation = None
            if payload.host:
                validation = await asyncio.to_thread(
                    trust_service.validate_bundle_for_host,
                    payload.host,
                    imported["bundle_path"],
                    tls_server_name=payload.tls_server_name,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **imported, "validation": validation})

    @router.post("/api/admin/tls/trust-remote")
    async def trust_remote_tls_certificate(payload: TLSRemoteCertificateTrustRequest) -> JSONResponse:
        settings = routes.reload_app_settings()
        trust_service = routes.TLSTrustStoreService(settings.config_file)
        try:
            trusted = await asyncio.to_thread(
                trust_service.trust_remote_certificate,
                payload.host,
                timeout_seconds=payload.timeout_seconds,
                bundle_name=payload.bundle_name,
                system_id=payload.system_id,
                tls_server_name=payload.tls_server_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **trusted})

    @router.get("/api/admin/tls/saved-status")
    async def saved_tls_bundle_status(system_id: str = Query(..., max_length=128)) -> JSONResponse:
        # #954: a reload re-checks the saved bundle so the admin can show the real
        # failure (expired, name mismatch, unreachable) instead of "not validated
        # yet". No probe runs when verification is off or no bundle is saved.
        settings = routes.reload_app_settings()
        system = next((item for item in settings.systems if item.id == system_id), None)
        if system is None:
            raise HTTPException(status_code=404, detail="That system is not in the saved config.")
        truenas = system.truenas
        bundle_path = str(truenas.tls_ca_bundle_path or "").strip()
        if not truenas.verify_ssl or not bundle_path or not routes.host_uses_tls(str(truenas.host or "")):
            return JSONResponse({"ok": True, "system_id": system.id, "validation": None})
        trust_service = routes.TLSTrustStoreService(settings.config_file)
        try:
            validation = await asyncio.to_thread(
                trust_service.validate_bundle_for_host,
                truenas.host,
                bundle_path,
                timeout_seconds=min(int(truenas.timeout_seconds or 10), 10),
                tls_server_name=truenas.tls_server_name,
            )
        except ValueError:
            # Fixed wording only: the exception text is not echoed (CodeQL
            # py/stack-trace-exposure). The service raises ValueError for a
            # missing bundle or a non-HTTPS target.
            bundle_present = await asyncio.to_thread(Path(bundle_path).is_file)
            validation = {
                "validated": False,
                "host": truenas.host,
                "bundle_path": bundle_path,
                "detail": (
                    "The saved bundle could not be checked: the bundle file could not be found."
                    if not bundle_present
                    else "The saved bundle could not be checked against this host."
                ),
            }
        return JSONResponse({"ok": True, "system_id": system.id, "validation": validation})
