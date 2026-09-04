from __future__ import annotations

# Handler globals are populated from app.main by MainModuleAPIRouter.
# pyright: reportUndefinedVariable=false
# ruff: noqa: F821

from types import ModuleType
from typing import Any

from app.route_compat import MainModuleAPIRouter


def build_router(main_module: ModuleType) -> MainModuleAPIRouter:
    router = MainModuleAPIRouter(main_module, globals())

    def route_service(system_id: str | None, **perf_metadata: Any) -> Any:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            **perf_metadata,
        )
        return service

    def mapping_revision_conflict_response(exc: MappingRevisionConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "mapping_revision_conflict",
                "detail": MappingRevisionConflict.public_detail,
                "current_revision": exc.current_revision,
            },
        )

    async def load_snapshot_export_sources(
        *,
        service: Any,
        payload: SnapshotExportRequest,
        enclosure_id: str | None,
        stage_prefix: str,
        settings: Any,
    ) -> tuple[Any, Any, Any, Any, Any, Any]:
        snapshot, smart_summary_cache = await _load_snapshot_export_source(
            service=service,
            payload=payload,
            enclosure_id=enclosure_id,
            stage_prefix=stage_prefix,
            settings=settings,
        )
        storage_view_runtime, storage_view_smart_summary_cache = await _load_storage_view_export_source(
            service=service,
            payload=payload,
            snapshot=snapshot,
            enclosure_id=enclosure_id,
        )
        live_enclosure_snapshots, live_enclosure_smart_summary_cache = await _load_live_enclosure_export_sources(
            service=service,
            payload=payload,
            snapshot=snapshot,
            smart_summary_cache=smart_summary_cache,
            enclosure_id=enclosure_id,
            stage_prefix=stage_prefix,
            settings=settings,
        )
        return (
            snapshot,
            smart_summary_cache,
            storage_view_runtime,
            storage_view_smart_summary_cache,
            live_enclosure_snapshots,
            live_enclosure_smart_summary_cache,
        )

    @router.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> HTMLResponse:
        current_settings = get_settings()
        service = route_service(system_id, enclosure_id=enclosure_id)
        admin_launch_url = await asyncio.to_thread(resolve_admin_launch_url, request, current_settings)
        snapshot = await service.get_snapshot(
            selected_enclosure_id=enclosure_id,
            allow_stale_cache=True,
        )
        storage_view_runtime = await service.get_storage_view_runtime(
            selected_enclosure_id=enclosure_id,
            snapshot=snapshot,
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            build_index_context(
                request=request,
                snapshot=snapshot,
                storage_view_runtime=storage_view_runtime,
                settings=current_settings,
                history_configured=bool(current_settings.history.service_url),
                admin_launch_url=admin_launch_url,
                app_version=__version__,
                release_status=get_release_status_service().snapshot(),
            ),
        )

    @router.get("/sas-fabric", response_class=HTMLResponse)
    async def sas_fabric_view(
        request: Request,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> HTMLResponse:
        current_settings = get_settings()
        service = route_service(
            system_id,
            enclosure_id=enclosure_id,
            sas_fabric_view=True,
        )
        snapshot = await service.get_snapshot(
            selected_enclosure_id=enclosure_id,
            allow_stale_cache=True,
        )
        fabric = await service.get_sas_fabric_snapshot(
            selected_enclosure_id=snapshot.selected_enclosure_id or enclosure_id,
        )
        bootstrap = {
            "snapshot": snapshot.model_dump(mode="json"),
            "fabric": fabric.model_dump(mode="json"),
            "systemId": snapshot.selected_system_id or service.system.id,
            "enclosureId": snapshot.selected_enclosure_id or enclosure_id,
            "appVersion": __version__,
        }
        return templates.TemplateResponse(
            request,
            "sas_fabric.html",
            {
                "request": request,
                "snapshot": snapshot,
                "fabric": fabric,
                "settings": current_settings,
                "app_version": __version__,
                "bootstrap_json": json.dumps(bootstrap),
            },
        )

    @router.get("/api/inventory", response_model=InventorySnapshot)
    async def get_inventory(
        force: bool = False,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> InventorySnapshot:
        service = route_service(
            system_id,
            enclosure_id=enclosure_id,
            force_refresh=force,
        )
        return await service.get_snapshot(
            force_refresh=force,
            selected_enclosure_id=enclosure_id,
            allow_stale_cache=not force,
        )

    @router.get("/api/sas-fabric", response_model=SasFabricSnapshot)
    async def get_sas_fabric(
        force: bool = False,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> SasFabricSnapshot:
        service = route_service(
            system_id,
            enclosure_id=enclosure_id,
            force_refresh=force,
            sas_fabric=True,
        )
        return await service.get_sas_fabric_snapshot(
            force_refresh=force,
            selected_enclosure_id=enclosure_id,
        )

    @router.post(
        "/api/sas-fabric/aliases",
        dependencies=[Depends(require_read_ui_mutation_authorization)],
    )
    async def save_sas_fabric_alias(
        payload: SasFabricAliasRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        service = route_service(
            system_id,
            enclosure_id=enclosure_id,
            sas_fabric_alias=payload.object_id,
        )
        try:
            result = service.save_sas_fabric_alias(
                object_id=payload.object_id,
                object_kind=payload.object_kind,
                label=payload.label,
                selected_enclosure_id=enclosure_id,
                scope=payload.scope,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @router.get("/api/storage-views", response_model=StorageViewRuntimePayload)
    async def get_storage_views(
        force: bool = False,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> StorageViewRuntimePayload:
        service = route_service(
            system_id,
            enclosure_id=enclosure_id,
            force_refresh=force,
        )
        return await service.get_storage_view_runtime(
            force_refresh=force,
            selected_enclosure_id=enclosure_id,
        )

    @router.post(
        "/api/slots/{slot}/led",
        dependencies=[Depends(require_read_ui_mutation_authorization)],
    )
    async def set_slot_led(
        slot: int,
        payload: LedRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        await ensure_slot_bounds(slot, service, enclosure_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, slot=slot, enclosure_id=enclosure_id)
        try:
            await service.set_slot_led(
                slot,
                payload.action,
                selected_enclosure_id=enclosure_id,
                invalidate_snapshot=False,
            )
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        service.invalidate_physical_enclosure_snapshot_cache(
            reason="route.set_slot_led",
            enclosure_id=enclosure_id,
            invalidate_source_bundle=True,
        )
        snapshot = await service.get_snapshot(force_refresh=True, selected_enclosure_id=enclosure_id)
        return JSONResponse({"ok": True, "snapshot": snapshot.model_dump(mode="json")})

    @router.get("/api/system-locator", response_model=SystemLocatorStatusView)
    async def get_system_locator(
        system_id: str | None = None,
    ) -> SystemLocatorStatusView:
        service = route_service(system_id)
        return await service.get_system_locator_status()

    @router.post(
        "/api/system-locator",
        response_model=SystemLocatorStatusView,
        dependencies=[Depends(require_read_ui_mutation_authorization)],
    )
    async def set_system_locator(
        payload: SystemLocatorRequest,
        system_id: str | None = None,
    ) -> SystemLocatorStatusView:
        service = route_service(
            system_id,
            locator_active=payload.active,
        )
        try:
            return await service.set_system_locator(payload.active)
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/api/slots/{slot}/mapping",
        dependencies=[Depends(require_read_ui_mutation_authorization)],
    )
    async def save_mapping(
        slot: int,
        payload: MappingRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        await ensure_slot_bounds(slot, service, enclosure_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, slot=slot, enclosure_id=enclosure_id)
        mapping_payload = {
            "serial": payload.serial,
            "device_name": payload.device_name,
            "gptid": payload.gptid,
            "notes": payload.notes,
        }
        try:
            mapping = await service.save_mapping(
                slot,
                mapping_payload,
                selected_enclosure_id=enclosure_id,
                expected_revision=payload.expected_revision,
                invalidate_snapshot=False,
            )
        except MappingRevisionConflict as exc:
            return mapping_revision_conflict_response(exc)

        led_warning = None
        led_changed = False
        if payload.clear_identify_after_save:
            try:
                await service.set_slot_led(
                    slot,
                    LedAction.clear,
                    selected_enclosure_id=enclosure_id,
                    invalidate_snapshot=False,
                )
                led_changed = True
            except Exception as exc:  # noqa: BLE001 - surface as non-fatal warning.
                logger.warning("Failed to clear identify LED after saving slot %s mapping: %s", slot, exc)
                led_warning = "Saved mapping, but failed to clear the identify LED; see application logs."

        service.invalidate_physical_enclosure_snapshot_cache(
            reason="route.save_mapping",
            enclosure_id=mapping.enclosure_id,
            invalidate_source_bundle=led_changed,
        )
        snapshot = await service.get_snapshot(force_refresh=True, selected_enclosure_id=enclosure_id)
        return JSONResponse(
            {
                "ok": True,
                "mapping": mapping.model_dump(mode="json"),
                "snapshot": snapshot.model_dump(mode="json"),
                "warning": led_warning,
            }
        )

    @router.delete(
        "/api/slots/{slot}/mapping",
        dependencies=[Depends(require_read_ui_mutation_authorization)],
    )
    async def clear_mapping(
        slot: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        expected_revision: str = Query(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        await ensure_slot_bounds(slot, service, enclosure_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, slot=slot, enclosure_id=enclosure_id)
        try:
            cleared = await service.clear_mapping(
                slot,
                selected_enclosure_id=enclosure_id,
                expected_revision=expected_revision,
                invalidate_snapshot=False,
            )
        except MappingRevisionConflict as exc:
            return mapping_revision_conflict_response(exc)
        if cleared:
            service.invalidate_physical_enclosure_snapshot_cache(
                reason="route.clear_mapping",
                enclosure_id=enclosure_id,
            )
        snapshot = await service.get_snapshot(force_refresh=cleared, selected_enclosure_id=enclosure_id)
        return JSONResponse({"ok": cleared, "snapshot": snapshot.model_dump(mode="json")})

    @router.get("/api/mappings/export", response_model=MappingBundle)
    async def export_mappings(
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> MappingBundle:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        return await service.export_mapping_bundle(selected_enclosure_id=enclosure_id)

    @router.post("/api/mappings/import/preview")
    async def preview_mapping_import(
        payload: MappingBundle,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        try:
            preview = await service.preview_mapping_bundle(
                payload,
                selected_enclosure_id=enclosure_id,
            )
        except ValueError:
            return JSONResponse(status_code=422, content={"detail": INVALID_MAPPING_BUNDLE_DETAIL})
        return JSONResponse(preview)

    @router.post(
        "/api/mappings/import",
        dependencies=[Depends(require_read_ui_mutation_authorization)],
    )
    async def import_mappings(
        payload: MappingImportConfirmation,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        try:
            result = await service.import_mapping_bundle(
                payload.bundle,
                selected_enclosure_id=enclosure_id,
                expected_revision=payload.expected_revision,
                import_digest=payload.import_digest,
                invalidate_snapshot=False,
            )
        except MappingRevisionConflict as exc:
            return mapping_revision_conflict_response(exc)
        except MappingImportDigestMismatch as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": "mapping_import_digest_mismatch",
                    "detail": MappingImportDigestMismatch.public_detail,
                    "current_revision": exc.current_revision,
                    "current_import_digest": exc.current_import_digest,
                },
            )
        except ValueError:
            return JSONResponse(status_code=422, content={"detail": INVALID_MAPPING_BUNDLE_DETAIL})
        service.invalidate_snapshot_cache(reason="route.import_mappings")
        snapshot = await service.get_snapshot(selected_enclosure_id=enclosure_id)
        return JSONResponse(
            {
                "ok": True,
                **result,
                "snapshot": snapshot.model_dump(mode="json"),
            }
        )

    @router.get("/api/slots/{slot}/smart", response_model=SmartSummaryView)
    async def get_slot_smart_summary(
        slot: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        fresh: bool = False,
    ) -> SmartSummaryView:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        await ensure_slot_bounds(slot, service, enclosure_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, slot=slot, enclosure_id=enclosure_id)
        try:
            return await service.get_slot_smart_summary(
                slot,
                selected_enclosure_id=enclosure_id,
                allow_stale_cache=not fresh,
            )
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/storage-views/{view_id}/slots/{slot_index}/smart", response_model=SmartSummaryView)
    async def get_storage_view_slot_smart_summary(
        view_id: str,
        slot_index: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        fresh: bool = False,
    ) -> SmartSummaryView:
        service = route_service(
            system_id,
            storage_view_id=view_id,
            slot=slot_index,
            enclosure_id=enclosure_id,
        )
        try:
            return await service.get_storage_view_slot_smart_summary(
                view_id,
                slot_index,
                selected_enclosure_id=enclosure_id,
                allow_stale_cache=not fresh,
            )
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/storage-views/{view_id}/slots/{slot_index}/history")
    async def get_storage_view_slot_history(
        view_id: str,
        slot_index: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        window_hours: int | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        history_slot, history_enclosure_id = await service.resolve_storage_view_slot_history_target(
            view_id,
            slot_index,
            selected_enclosure_id=enclosure_id,
        )
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            storage_view_id=view_id,
            slot=slot_index,
            history_slot=history_slot,
            enclosure_id=history_enclosure_id,
        )
        history_backend = get_history_backend()
        payload = await history_backend.get_slot_history(
            history_slot,
            service.system.id,
            history_enclosure_id,
            window_hours=window_hours,
        )
        return JSONResponse(payload)

    @router.post("/api/slots/smart-batch", response_model=SmartBatchResponse)
    async def get_slot_smart_summaries(
        payload: SmartBatchRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        fresh: bool = False,
    ) -> SmartBatchResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        layout_slot_count = await resolve_layout_slot_count(service, enclosure_id)
        for slot in payload.slots:
            check_slot_bounds(slot, layout_slot_count)
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            enclosure_id=enclosure_id,
            slot_count=len(payload.slots),
            smart_batch_max_concurrency=payload.max_concurrency,
        )
        try:
            summaries = await service.get_slot_smart_summaries(
                payload.slots,
                selected_enclosure_id=enclosure_id,
                max_concurrency=payload.max_concurrency,
                allow_stale_cache=not fresh,
            )
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SmartBatchResponse(summaries=summaries)

    @router.get("/api/history/status")
    async def get_history_status() -> JSONResponse:
        history_backend = get_history_backend()
        return JSONResponse(await history_backend.get_status())

    @router.get("/api/slots/{slot}/history")
    async def get_slot_history(
        slot: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        window_hours: int | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        await ensure_slot_bounds(slot, service, enclosure_id)
        # Scope the query to the system the registry resolved, exactly like
        # every sibling route; an unknown or retired ``system_id`` otherwise
        # gets bounds-checked against the default system but queried as the
        # raw string, so this panel and ``/api/history/scope`` disagree (#286).
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            slot=slot,
            enclosure_id=enclosure_id,
            history_window_hours=window_hours,
        )
        history_backend = get_history_backend()
        payload = await history_backend.get_slot_history(
            slot,
            service.system.id,
            enclosure_id,
            window_hours=window_hours,
        )
        return JSONResponse(payload)

    @router.get("/api/history/scope")
    async def get_history_scope(
        system_id: str | None = None,
        enclosure_id: str | None = None,
        slots: list[int] | None = Query(default=None),
        window_hours: int | None = None,
        metrics: list[str] | None = Query(default=None),
        event_limit: int = Query(default=12, ge=0, le=1000),
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        normalized_slots = sorted({int(slot) for slot in (slots or [])})
        if normalized_slots:
            layout_slot_count = await resolve_layout_slot_count(service, enclosure_id)
            for slot in normalized_slots:
                check_slot_bounds(slot, layout_slot_count)
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            enclosure_id=enclosure_id,
            slot_count=len(normalized_slots),
            history_window_hours=window_hours,
        )
        history_backend = get_history_backend()
        payload = await history_backend.get_scope_history(
            system_id=service.system.id,
            enclosure_id=enclosure_id,
            slots=normalized_slots,
            window_hours=window_hours,
            metrics=metrics,
            event_limit=event_limit,
        )
        return JSONResponse(
            {
                "configured": history_backend.configured,
                "system_id": service.system.id,
                "enclosure_id": enclosure_id,
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
        event_limit: int = Query(default=12, ge=0, le=1000),
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        try:
            runtime = await service.get_storage_view_runtime(
                force_refresh=False,
                selected_enclosure_id=enclosure_id,
            )
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime_view = next((view for view in runtime.views if view.id == view_id), None)
        if not runtime_view:
            raise HTTPException(status_code=404, detail=f"Storage view {view_id!r} is not present for this system.")

        display_slot_by_target: dict[tuple[str | None, int], list[int]] = {}
        slots_by_enclosure: dict[str | None, set[int]] = {}
        for runtime_slot in runtime_view.slots:
            if runtime_slot.snapshot_slot is not None:
                history_slot = int(runtime_slot.snapshot_slot)
                history_enclosure_id = enclosure_id or runtime_view.backing_enclosure_id
            else:
                history_slot = int(runtime_slot.slot_index)
                history_enclosure_id = f"storage-view:{runtime_view.id}"
            slots_by_enclosure.setdefault(history_enclosure_id, set()).add(history_slot)
            display_slot_by_target.setdefault(
                (history_enclosure_id, history_slot),
                [],
            ).append(int(runtime_slot.slot_index))

        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            storage_view_id=view_id,
            slot_count=len(runtime_view.slots),
            history_window_hours=window_hours,
        )
        history_backend = get_history_backend()
        histories_by_display_slot: dict[str, dict[str, Any]] = {}
        for history_enclosure_id, history_slots in slots_by_enclosure.items():
            scope_payload = await history_backend.get_scope_history(
                system_id=service.system.id,
                enclosure_id=history_enclosure_id,
                slots=sorted(history_slots),
                window_hours=window_hours,
                metrics=metrics,
                event_limit=event_limit,
            )
            for history_slot, history_payload in scope_payload.items():
                for display_slot in display_slot_by_target.get((history_enclosure_id, int(history_slot)), []):
                    histories_by_display_slot[str(display_slot)] = history_payload

        return JSONResponse(
            {
                "configured": history_backend.configured,
                "system_id": service.system.id,
                "storage_view_id": runtime_view.id,
                "histories": histories_by_display_slot,
            }
        )

    @router.post("/api/export/enclosure-snapshot")
    async def export_enclosure_snapshot(
        request: Request,
        payload: SnapshotExportRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> Response:
        settings = get_settings()
        service = route_service(system_id, enclosure_id=enclosure_id)
        (
            snapshot,
            smart_summary_cache,
            storage_view_runtime,
            storage_view_smart_summary_cache,
            live_enclosure_snapshots,
            live_enclosure_smart_summary_cache,
        ) = await load_snapshot_export_sources(
            service=service,
            payload=payload,
            enclosure_id=enclosure_id,
            stage_prefix="route.export_snapshot",
            settings=settings,
        )
        exporter = get_snapshot_export_service()
        try:
            with perf_stage("route.export_snapshot.build_artifact"):
                artifact = await exporter.build_enclosure_snapshot_export(
                    request=request,
                    snapshot=snapshot,
                    smart_summary_cache=smart_summary_cache,
                    live_enclosure_snapshots=live_enclosure_snapshots,
                    live_enclosure_smart_summary_cache=live_enclosure_smart_summary_cache,
                    storage_view_runtime=storage_view_runtime,
                    storage_view_smart_summary_cache=storage_view_smart_summary_cache,
                    selected_slot=payload.selected_slot,
                    selected_storage_view_id=payload.selected_storage_view_id,
                    history_window_hours=payload.history_window_hours,
                    history_panel_open=payload.history_panel_open,
                    io_chart_mode=payload.io_chart_mode,
                    redact_sensitive=payload.redact_sensitive,
                    configured_hostnames=collect_configured_hostnames(service.system.model_dump(mode="json")),
                    packaging=payload.packaging,
                    allow_oversize=payload.allow_oversize,
                )
        except SnapshotExportTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return Response(
            content=artifact.content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                "X-Export-Size-Bytes": str(artifact.size_bytes),
                "X-Export-HTML-Size-Bytes": str(artifact.html_size_bytes),
                "X-Export-Packaging": artifact.packaging,
                "X-Export-Redaction": artifact.redaction,
                "X-Export-Size-Limit-Bytes": str(artifact.size_limit_bytes),
            },
        )

    @router.post("/api/export/enclosure-snapshot/estimate")
    async def estimate_enclosure_snapshot(
        request: Request,
        payload: SnapshotExportRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        settings = get_settings()
        service = route_service(system_id, enclosure_id=enclosure_id)
        (
            snapshot,
            smart_summary_cache,
            storage_view_runtime,
            storage_view_smart_summary_cache,
            live_enclosure_snapshots,
            live_enclosure_smart_summary_cache,
        ) = await load_snapshot_export_sources(
            service=service,
            payload=payload,
            enclosure_id=enclosure_id,
            stage_prefix="route.export_snapshot_estimate",
            settings=settings,
        )
        exporter = get_snapshot_export_service()
        with perf_stage("route.export_snapshot_estimate.build_estimate"):
            estimate = await exporter.estimate_enclosure_snapshot_export(
                request=request,
                snapshot=snapshot,
                smart_summary_cache=smart_summary_cache,
                live_enclosure_snapshots=live_enclosure_snapshots,
                live_enclosure_smart_summary_cache=live_enclosure_smart_summary_cache,
                storage_view_runtime=storage_view_runtime,
                storage_view_smart_summary_cache=storage_view_smart_summary_cache,
                selected_slot=payload.selected_slot,
                selected_storage_view_id=payload.selected_storage_view_id,
                history_window_hours=payload.history_window_hours,
                history_panel_open=payload.history_panel_open,
                io_chart_mode=payload.io_chart_mode,
                redact_sensitive=payload.redact_sensitive,
                configured_hostnames=collect_configured_hostnames(service.system.model_dump(mode="json")),
                packaging=payload.packaging,
                allow_oversize=payload.allow_oversize,
            )
        return JSONResponse(estimate)

    @router.get("/livez")
    async def livez() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
            },
            status_code=200,
        )

    @router.get("/healthz")
    async def healthz() -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(None)
        snapshot = service.peek_cached_snapshot()
        if snapshot is None:
            return JSONResponse(
                {
                    "status": "ok",
                    "dependency_status": "unknown",
                    "last_updated": None,
                    "sources": {},
                    "warnings": [],
                    "cache_state": "empty",
                },
                status_code=200,
            )
        api_status = snapshot.sources.get("api")
        return JSONResponse(
            {
                "status": "ok",
                "dependency_status": "ok" if api_status and api_status.ok else "degraded",
                "last_updated": snapshot.last_updated.isoformat(),
                "sources": snapshot.model_dump(mode="json").get("sources", {}),
                "warnings": snapshot.warnings,
                "cache_state": "cached",
            },
            status_code=200,
        )

    return router
