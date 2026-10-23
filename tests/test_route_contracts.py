from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.routing import APIRoute

# Must precede admin_service.main, which builds its app at import time.
import tests.admin_test_env  # noqa: F401  (must precede admin_service.main)
from admin_service import main as admin_main
from admin_service import routes as admin_routes
from app import main as app_main
from app import route_support as app_route_support
from app import routes as app_routes
from app.models.domain import SystemBackupExportRequest
from app.services.inventory import SnapshotStateBusyError, UnknownEnclosureError
from app.services.snapshot_export import SnapshotExportBusyError


APP_ROUTE_MATRIX = [
    ("/openapi.json", ("GET", "HEAD"), "openapi", None, None),
    ("/static", (), "static", None, None),
    ("/metrics", ("GET",), "prometheus_metrics_endpoint", "starlette.responses.JSONResponse", None),
    ("/", ("GET",), "index", "starlette.responses.HTMLResponse", None),
    ("/api/release-status", ("GET",), "get_release_status", "starlette.responses.JSONResponse", None),
    ("/api/upgrade-notice/dismiss", ("POST",), "dismiss_upgrade_notice", "starlette.responses.JSONResponse", None),
    ("/sas-fabric", ("GET",), "sas_fabric_view", "starlette.responses.HTMLResponse", None),
    ("/api/inventory", ("GET",), "get_inventory", "starlette.responses.JSONResponse", "app.models.domain.InventoryReadResponse"),
    ("/api/read-ui/auth/verify", ("GET",), "verify_read_ui_credentials", "starlette.responses.JSONResponse", None),
    ("/api/sas-fabric", ("GET",), "get_sas_fabric", "starlette.responses.JSONResponse", "app.models.domain.SasFabricSnapshot"),
    ("/api/sas-fabric/aliases", ("POST",), "save_sas_fabric_alias", "starlette.responses.JSONResponse", None),
    ("/api/storage-views", ("GET",), "get_storage_views", "starlette.responses.JSONResponse", "app.models.domain.StorageViewRuntimePayload"),
    ("/api/slots/{slot}/led", ("POST",), "set_slot_led", "starlette.responses.JSONResponse", None),
    ("/api/systems/{system_id}/disk-inventory-sync", ("POST",), "sync_disk_inventory", "starlette.responses.JSONResponse", None),
    ("/api/system-locator", ("GET",), "get_system_locator", "starlette.responses.JSONResponse", "app.models.domain.SystemLocatorStatusView"),
    ("/api/system-locator", ("POST",), "set_system_locator", "starlette.responses.JSONResponse", "app.models.domain.SystemLocatorStatusView"),
    ("/api/slots/{slot}/mapping", ("POST",), "save_mapping", "starlette.responses.JSONResponse", None),
    ("/api/slots/{slot}/mapping", ("DELETE",), "clear_mapping", "starlette.responses.JSONResponse", None),
    ("/api/mappings/export", ("GET",), "export_mappings", "starlette.responses.JSONResponse", "app.models.domain.MappingBundle"),
    ("/api/mappings/import/preview", ("POST",), "preview_mapping_import", "starlette.responses.JSONResponse", None),
    ("/api/mappings/import", ("POST",), "import_mappings", "starlette.responses.JSONResponse", None),
    ("/api/slots/{slot}/smart", ("GET",), "get_slot_smart_summary", "starlette.responses.JSONResponse", "app.models.domain.SmartSummaryView"),
    ("/api/storage-views/{view_id}/slots/{slot_index}/smart", ("GET",), "get_storage_view_slot_smart_summary", "starlette.responses.JSONResponse", "app.models.domain.SmartSummaryView"),
    ("/api/storage-views/{view_id}/slots/{slot_index}/history", ("GET",), "get_storage_view_slot_history", "starlette.responses.JSONResponse", None),
    ("/api/slots/smart-batch", ("POST",), "get_slot_smart_summaries", "starlette.responses.JSONResponse", "app.models.domain.SmartBatchResponse"),
    ("/api/storage-views/{view_id}/slots/smart-batch", ("POST",), "get_storage_view_slot_smart_summaries", "starlette.responses.JSONResponse", "app.models.domain.SmartBatchResponse"),
    ("/api/history/status", ("GET",), "get_history_status", "starlette.responses.JSONResponse", None),
    ("/api/history/refresh", ("POST",), "refresh_history_proxy", "starlette.responses.JSONResponse", None),
    ("/api/history/scopes/bundle", ("POST",), "get_history_scopes_bundle", "starlette.responses.JSONResponse", None),
    ("/api/slots/{slot}/history", ("GET",), "get_slot_history", "starlette.responses.JSONResponse", None),
    ("/api/history/scope", ("GET",), "get_history_scope", "starlette.responses.JSONResponse", None),
    ("/api/storage-views/{view_id}/history", ("GET",), "get_storage_view_history", "starlette.responses.JSONResponse", None),
    ("/api/export/enclosure-snapshot", ("POST",), "export_enclosure_snapshot", "starlette.responses.JSONResponse", None),
    ("/api/export/enclosure-snapshot/estimate", ("POST",), "estimate_enclosure_snapshot", "starlette.responses.JSONResponse", None),
    ("/livez", ("GET",), "livez", "starlette.responses.JSONResponse", None),
    ("/healthz", ("GET",), "healthz", "starlette.responses.JSONResponse", None),
]

ADMIN_ROUTE_MATRIX = [
    ("/openapi.json", ("GET", "HEAD"), "openapi", None, None),
    ("/static", (), "static", None, None),
    ("/metrics", ("GET",), "prometheus_metrics_endpoint", "starlette.responses.JSONResponse", None),
    ("/", ("GET",), "index", "starlette.responses.HTMLResponse", None),
    ("/api/admin/state", ("GET",), "get_admin_state", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime", ("GET",), "get_admin_runtime", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime-behavior", ("POST",), "update_runtime_behavior", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime/containers/{container_key}/stop", ("POST",), "stop_container", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime/containers/{container_key}/start", ("POST",), "start_container", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime/containers/{container_key}/restart", ("POST",), "restart_container", "starlette.responses.JSONResponse", None),
    ("/api/admin/backup/export", ("POST",), "export_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/debug/export", ("POST",), "export_debug_bundle", "starlette.responses.JSONResponse", None),
    ("/api/admin/backup/inspect", ("POST",), "inspect_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/backup/import", ("POST",), "import_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/esxi-host-prep/upload", ("POST",), "upload_esxi_host_prep_package", "starlette.responses.JSONResponse", None),
    ("/api/admin/esxi-host-prep/install", ("POST",), "install_esxi_host_prep_package", "starlette.responses.JSONResponse", None),
    ("/api/admin/ssh-keys", ("GET",), "list_ssh_keys", "starlette.responses.JSONResponse", None),
    ("/api/admin/ssh-keys/generate", ("POST",), "generate_ssh_key", "starlette.responses.JSONResponse", None),
    ("/api/admin/tls/inspect", ("POST",), "inspect_tls_certificate", "starlette.responses.JSONResponse", None),
    ("/api/admin/tls/import", ("POST",), "import_tls_bundle", "starlette.responses.JSONResponse", None),
    ("/api/admin/tls/trust-remote", ("POST",), "trust_remote_tls_certificate", "starlette.responses.JSONResponse", None),
    ("/api/admin/tls/saved-status", ("GET",), "saved_tls_bundle_status", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/quantastor-nodes", ("POST",), "discover_quantastor_nodes", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup", ("POST",), "create_system", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/demo", ("POST",), "create_demo_system", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/{system_id}", ("DELETE",), "delete_system", "starlette.responses.JSONResponse", None),
    ("/api/admin/history/purge-orphaned", ("POST",), "purge_orphaned_history", "starlette.responses.JSONResponse", None),
    ("/api/admin/history/orphaned", ("GET",), "list_orphaned_history", "starlette.responses.JSONResponse", None),
    ("/api/admin/history/systems", ("GET",), "list_history_systems", "starlette.responses.JSONResponse", None),
    ("/api/admin/history/adopt-removed-system", ("POST",), "adopt_removed_system_history", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/bootstrap", ("POST",), "bootstrap_service_account", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/sudoers-preview", ("POST",), "preview_sudoers_file", "starlette.responses.JSONResponse", None),
    ("/api/admin/storage-views/candidates", ("GET",), "list_storage_view_candidates", "starlette.responses.JSONResponse", None),
    ("/api/admin/storage-views/live-enclosures", ("GET",), "list_storage_view_live_enclosures", "starlette.responses.JSONResponse", None),
    ("/api/admin/profiles", ("POST",), "save_profile", "starlette.responses.JSONResponse", None),
    ("/api/admin/profiles/{profile_id}", ("DELETE",), "delete_profile", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups", ("GET",), "list_backups", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/run", ("POST",), "run_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/policy", ("GET",), "get_backup_policy", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/policy", ("PUT",), "save_backup_policy", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/lifecycle/plan", ("GET",), "plan_backup_grooming", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/lifecycle/apply", ("POST",), "apply_backup_grooming", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/targets/{target_id}/test", ("POST",), "test_backup_target", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/{artifact_id}", ("GET",), "get_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/{artifact_id}/verify", ("POST",), "verify_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/{artifact_id}/preserve", ("POST",), "preserve_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/{artifact_id}/preserve", ("DELETE",), "unpreserve_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/{artifact_id}/download", ("GET",), "download_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/{artifact_id}/restore/inspect", ("POST",), "inspect_catalog_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/backups/{artifact_id}/restore/import", ("POST",), "import_catalog_backup", "starlette.responses.JSONResponse", None),
    ("/healthz", ("GET",), "healthz", "starlette.responses.JSONResponse", None),
    ("/livez", ("GET",), "livez", "starlette.responses.JSONResponse", None),
]


def _symbol(value: object) -> str | None:
    if value is None:
        return None
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    if module and name:
        return f"{module}.{name}"
    placeholder = getattr(value, "value", None)
    if placeholder is not None and placeholder is not value:
        return _symbol(placeholder)
    return type(value).__name__


def _route_matrix(application) -> list[tuple[object, ...]]:
    return [
        (
            getattr(route, "path", None),
            tuple(sorted(getattr(route, "methods", None) or [])),
            getattr(route, "name", None),
            _symbol(route.response_class) if isinstance(route, APIRoute) else None,
            _symbol(route.response_model) if isinstance(route, APIRoute) else None,
        )
        for route in application.routes
    ]


def _create_app_node(module) -> ast.FunctionDef:
    source_path = Path(inspect.getsourcefile(module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )


class OpeningOrderRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from app.config import Settings, SystemConfig
        from app.models.domain import EnclosureOption
        from tests.test_inventory import InventoryStorageViewCandidateTests, build_inventory_service

        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.system = SystemConfig(id="synthetic-system", label="Synthetic system")
        self.settings = Settings(systems=[self.system])
        self.service = build_inventory_service(
            self.settings, self.system, AsyncMock(), AsyncMock(), self.scratch.name,
        )
        self.fixture = InventoryStorageViewCandidateTests
        self.options = [EnclosureOption(id=f"enc-{letter}", label=f"Shelf {letter}") for letter in "ab"]
        self.snapshots = {
            option.id: self.fixture._view_order_snapshot(
                self.system.id, option.id, option.label, index + 1, self.options,
            ) for index, option in enumerate(self.options)
        }

        async def snapshot(*, selected_enclosure_id=None, **kwargs):
            return self.snapshots[selected_enclosure_id or self.options[0].id]

        self.service.get_snapshot = AsyncMock(side_effect=snapshot)
        async def opening_snapshot(enclosure_id, **kwargs):
            return self.snapshots[enclosure_id]
        self.service._get_opening_order_snapshot = AsyncMock(side_effect=opening_snapshot)
        self.service._get_inventory_source_bundle = AsyncMock(return_value=self.fixture._empty_source_bundle())
        # These coordination tests supply snapshots instead of running their
        # producer. Model the producer's exact per-snapshot source/read binding;
        # unassociated-snapshot regressions below deliberately omit this entry.
        import weakref
        source = self.service._get_inventory_source_bundle.return_value
        mappings = self.service.mapping_store.load_read_snapshot()
        for supplied in self.snapshots.values():
            self.service._view_order_read_inputs[id(supplied)] = (
                weakref.ref(supplied), source, self.service._snapshot_topology_generation,
                mappings, [], self.service._source_bundle_generation, object(),
            )
        registry = MagicMock()
        registry.get_service.return_value = self.service
        for target, value in (
            ("get_inventory_registry", registry), ("get_settings", self.settings),
            ("resolve_admin_launch_url", None), ("runtime_warnings_for", []),
        ):
            self.enterContext(patch.object(app_routes, target, return_value=value))
        self.application = app_main.create_app()

    async def request(self, path, query=b""):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await self.application({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": query, "headers": [], "client": ("127.0.0.1", 1),
            "server": ("test", 80), "root_path": "",
        }, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        return start["status"], b"".join(
            message.get("body", b"") for message in messages if message["type"] == "http.response.body"
        )

    async def test_different_enclosure_requests_wait_and_keep_both_order_contexts(self):
        await self.exercise_order_collision()

    async def test_same_context_requests_share_order_scoring(self):
        await self.exercise_order_collision(same_context=True)

    async def test_page_waits_for_order_scoring_instead_of_returning_busy_json(self):
        await self.exercise_order_collision(page=True)

    async def exercise_order_collision(self, *, same_context=False, page=False):
        entered = asyncio.Event()
        release = asyncio.Event()
        original = self.service._get_opening_order_snapshot

        async def secondary(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        self.service._get_opening_order_snapshot = AsyncMock(side_effect=secondary)
        first = asyncio.create_task(self.request("/api/storage-views", b"enclosure_id=enc-a"))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(self.request(
            "/" if page else "/api/storage-views",
            b"enclosure_id=enc-a" if same_context else b"enclosure_id=enc-b",
        ))
        try:
            await asyncio.sleep(0)
        finally:
            release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual([status for status, body in responses], [200, 200])
        self.assertEqual(self.service._get_opening_order_snapshot.await_count, 1 if same_context else 2)
        if page:
            self.assertIn(b"<!doctype html>", responses[1][1].lower())
            return
        if same_context:
            self.assertEqual(json.loads(responses[0][1]), json.loads(responses[1][1]))
            return
        expected = [json.loads(body)["view_order"] for status, body in responses]
        self.service._get_opening_order_snapshot.reset_mock()
        for index, enclosure in enumerate(("enc-a", "enc-b", "enc-a", "enc-b")):
            status, body = await self.request("/api/storage-views", f"enclosure_id={enclosure}".encode())
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["view_order"], expected[index % 2])
        self.service._get_opening_order_snapshot.assert_not_awaited()

    async def test_forced_order_burst_bounds_waiters_and_falls_back_without_busy(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def secondary(enclosure_id, **kwargs):
            entered.set()
            await release.wait()
            return self.snapshots[enclosure_id]

        self.service._get_opening_order_snapshot = AsyncMock(side_effect=secondary)
        self.system.view_order = ["enclosure:enc-b", "view:primary-chassis"]
        tasks = [asyncio.create_task(self.request(
            "/api/storage-views", b"enclosure_id=enc-a&force=true",
        ))]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            tasks.extend(asyncio.create_task(self.request(
                "/" if index % 2 else "/api/storage-views",
                b"enclosure_id=enc-a&force=true",
            )) for index in range(64))
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertLessEqual(sum(not task.done() for task in tasks), 9)
            self.assertLessEqual(len(self.service._view_order_recompute_lock._waiters or ()), 8)
            overflow = [task.result() for task in tasks if task.done()]
            self.assertGreaterEqual(len(overflow), 56)
            for status, body in overflow:
                self.assertEqual(status, 200, body)
                if body.startswith(b'{'):
                    self.assertEqual(json.loads(body)["view_order"], [
                        "enclosure:enc-b", "view:primary-chassis", "enclosure:enc-a",
                    ])
                else:
                    self.assertIn(b"<!doctype html>", body.lower())
                    self.assertIn(b"Opening-view scoring was skipped", body)
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 2)
            self.assertTrue(all(status == 200 for status, _ in responses))
            self.assertEqual(self.service._get_opening_order_snapshot.await_count, 1)
            self.assertFalse(self.service._view_order_recompute_lock.locked())
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_forced_order_waiters_do_not_repeat_invalidated_scoring(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def secondary(enclosure_id, **kwargs):
            entered.set()
            await release.wait()
            # Real secondary publication advances this generation and prevents
            # the opening-order result from being cached. Waiters must not each
            # force another secondary build when that happens.
            self.service._snapshot_publication_generation += 1
            self.service._view_order_cache.clear()
            return self.snapshots[enclosure_id]

        self.service._get_opening_order_snapshot = AsyncMock(side_effect=secondary)
        tasks = [asyncio.create_task(self.request(
            "/api/storage-views", b"enclosure_id=enc-a&force=true",
        ))]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            tasks.extend(asyncio.create_task(self.request(
                "/api/storage-views", b"enclosure_id=enc-a&force=true",
            )) for _ in range(4))
            for _ in range(10):
                await asyncio.sleep(0)
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 2)
            self.assertTrue(all(status == 200 for status, _ in responses))
            self.assertEqual(self.service._get_opening_order_snapshot.await_count, 1)
            self.assertEqual(len(self.service._view_order_cache), 0)
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_order_timeout_returns_page_fallback_and_releases_admission(self):
        entered = asyncio.Event()

        async def secondary(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        original = self.service._get_opening_order_snapshot
        self.service._get_opening_order_snapshot = AsyncMock(side_effect=secondary)
        with patch("app.services.inventory.OPENING_ORDER_TIMEOUT_SECONDS", 0.05, create=True):
            tasks = [asyncio.create_task(self.request("/", b"enclosure_id=enc-a"))]
            try:
                await asyncio.wait_for(entered.wait(), 2)
                tasks.append(asyncio.create_task(self.request("/", b"enclosure_id=enc-b")))
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 0.5)
                for status, body in responses:
                    self.assertEqual(status, 200, body)
                    self.assertIn(b"Opening-view scoring was skipped", body)
                self.assertFalse(self.service._view_order_recompute_lock.locked())
                self.assertEqual(self.service._view_order_waiters, 0)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.service._get_opening_order_snapshot = original
        status, _ = await self.request("/api/storage-views", b"enclosure_id=enc-a")
        self.assertEqual(status, 200)
        self.assertTrue(self.service._view_order_cache)

    async def test_order_cancellation_releases_owner_and_waiter_admission(self):
        entered = asyncio.Event()

        async def secondary(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        original = self.service._get_opening_order_snapshot
        self.service._get_opening_order_snapshot = AsyncMock(side_effect=secondary)
        tasks = [asyncio.create_task(self.request("/api/storage-views", b"enclosure_id=enc-a"))]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            tasks.append(asyncio.create_task(self.request("/api/storage-views", b"enclosure_id=enc-b")))
            for _ in range(10):
                await asyncio.sleep(0)
            tasks[1].cancel()
            await asyncio.gather(tasks[1], return_exceptions=True)
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            self.assertFalse(self.service._view_order_recompute_lock.locked())
            self.assertEqual(self.service._view_order_waiters, 0)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.service._get_opening_order_snapshot = original
        self.assertEqual((await self.request("/api/storage-views", b"enclosure_id=enc-b"))[0], 200)

    def use_real_snapshot_producers(self):
        from app.services.inventory import InventoryService

        for name in ("get_snapshot", "_get_inventory_source_bundle", "_get_opening_order_snapshot"):
            setattr(self.service, name, getattr(InventoryService, name).__get__(self.service))
        bundles = []

        async def collect():
            # Every native collection returns a distinct bundle. Reusing one
            # object here would conceal the forced S1/S2 runtime defect.
            bundle = self.fixture._empty_source_bundle()
            bundle.raw_data.enclosures = [
                {"id": option.id, "name": option.label, "elements": [
                    {"slot": slot, "dev": f"da{index * 4 + slot}", "status": "OK"}
                    for slot in range(1, index + 2)
                ]} for index, option in enumerate(self.options)
            ]
            bundles.append(bundle)
            return bundle

        self.service._collect_inventory_source_bundle = AsyncMock(side_effect=collect)
        self.settings.app.source_bundle_cache_ttl_seconds = 60
        return bundles

    async def test_forced_runtime_scores_exact_active_source_without_recollection(self):
        bundles = self.use_real_snapshot_producers()
        status, body = await self.request("/api/storage-views", b"enclosure_id=enc-a&force=true")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["view_order"][0], "enclosure:enc-b")
        self.assertEqual(len(bundles), 1)
        self.assertFalse(any("scoring was skipped" in warning
                             for warning in self.service._cache["enc-a"].warnings))
        self.assertEqual(set(self.service._cache), {"enc-a"})
        self.assertFalse(self.service._snapshot_refresh_tasks)

    async def test_mapping_read_invalidation_cannot_be_readmitted_to_order_scoring(self):
        import threading
        from app.services.inventory import SnapshotStateBusyError

        self.use_real_snapshot_producers()
        active = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id="enc-b")
        source = self.service._source_bundle
        entered, release = threading.Event(), threading.Event()
        native_read = self.service.mapping_store.load_read_snapshot

        def read_then_wait():
            value = native_read()
            entered.set()
            if not release.wait(3):
                raise RuntimeError("synthetic mapping-read safety bound exceeded")
            return value

        self.service.mapping_store.load_read_snapshot = read_then_wait
        rejected = []
        native_build = self.service._build_snapshot

        async def build(*args, **kwargs):
            candidate = await native_build(*args, **kwargs)
            rejected.append(candidate)
            return candidate

        self.service._build_snapshot = build
        task = asyncio.create_task(self.service._get_snapshot_result(
            selected_enclosure_id="enc-a", force_source_refresh=False,
        ))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            generation = self.service._snapshot_topology_generation
            self.service.invalidate_snapshot_cache(
                reason="synthetic.mapping-change", cache_keys=["enc-a"], invalidate_source_bundle=False,
            )
            self.assertGreater(self.service._snapshot_topology_generation, generation)
            release.set()
            with self.assertRaises(SnapshotStateBusyError):
                await task
            result = await self.service._get_opening_order_snapshot(
                "enc-a", source_bundle=source, active_snapshot=active,
            )
            self.assertIsNone(result, "pre-invalidation mapping read was readmitted to optional scoring")
            self.assertEqual(len(rejected), 1)
            self.assertIsNone(self.service._opening_order_inputs_for(rejected[0]),
                              "rejected mapping read retained a post-invalidation association")
            self.assertIsNone(await self.service._get_opening_order_snapshot(
                "enc-b", source_bundle=source, active_snapshot=rejected[0],
            ))
            self.assertNotIn("enc-a", self.service._cache)
            self.assertFalse(self.service._view_order_cache)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_runtime_source_associations_survive_other_snapshot_builds(self):
        bundles = self.use_real_snapshot_producers()
        self.settings.app.snapshot_cache_ttl_seconds = 60
        active = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id="enc-a")
        source = self.service._source_bundle
        other = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id="enc-b")
        self.assertIsNot(self.service._source_bundle, source)
        self.assertIsNone(await self.service._get_opening_order_snapshot(
            "enc-b", source_bundle=self.service._source_bundle, active_snapshot=active,
        ))
        before = len(bundles)
        native_snapshot = self.service.get_snapshot
        # Supplied snapshots do not rebuild, even on a forced runtime call.
        self.service.get_snapshot = AsyncMock(side_effect=AssertionError("supplied snapshot rebuilt"))
        for force in (False, True):
            for tolerant in (False, True):
                with self.subTest(force=force, tolerant=tolerant):
                    runtime = await self.service.get_storage_view_runtime(
                        snapshot=active, force_refresh=force, tolerate_hidden_view_targets=tolerant,
                    )
                    self.assertEqual(runtime.view_order[0], "enclosure:enc-b")
                    self.assertIs(self.service._view_order_source_bundle, source)
                    self.assertEqual(len(bundles), before)
                    self.service._view_order_cache.clear()
        self.service.get_snapshot = native_snapshot
        runtime = await self.service.get_storage_view_runtime(selected_enclosure_id="enc-a")
        self.assertEqual(runtime.view_order[0], "enclosure:enc-b")
        self.assertIs(self.service._view_order_source_bundle, source)
        self.assertIs(self.service._cache["enc-b"], other)
        self.assertFalse(any("scoring was skipped" in warning for warning in active.warnings))
        self.assertEqual(len(bundles), before)
        # A model copy or deserialized legacy snapshot has no association. It
        # cannot borrow the latest builder's read or the existing scored cache.
        legacy = active.model_copy(deep=True)
        runtime = await self.service.get_storage_view_runtime(snapshot=legacy)
        self.assertEqual(runtime.view_order[0], "enclosure:enc-a")
        self.assertTrue(any("current correlation inputs are unavailable" in warning for warning in legacy.warnings))
        _, retention = await self.service.get_snapshot_with_system_retention(
            selected_enclosure_id="enc-a",
        )
        self.assertIsNotNone(retention)

    async def test_single_enclosure_unassociated_snapshot_cannot_score_or_cache(self):
        await self.exercise_missing_order_association(single_enclosure=True)

    async def test_all_known_unassociated_snapshot_cannot_score_or_cache(self):
        await self.exercise_missing_order_association(single_enclosure=False)

    async def exercise_missing_order_association(self, *, single_enclosure):
        from app.models.domain import StorageViewRuntimeView

        self.use_real_snapshot_producers()
        active = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id="enc-a")
        source = self.service._opening_order_inputs_for(active)[0]
        if single_enclosure:
            active.enclosures = active.enclosures[:1]
        view = StorageViewRuntimeView(
            id="visible", label="Synthetic visible view", kind="manual",
            template_id="manual", matched_count=100,
        )
        arguments = dict(
            source_bundle=source, runtime_views=[view],
            ha_primary_view_id=None, ha_owner_enclosure_id=None, force_refresh=False,
        )
        known = {option.id: active for option in active.enclosures}
        # Positive control: an associated snapshot really scores the high-count
        # view first, so fallback must not merely happen to equal scored order.
        scored = await self.service._effective_runtime_view_order(
            active_snapshot=active, known_snapshots=known, **arguments,
        )
        self.assertEqual(scored[0], "view:visible")
        self.assertTrue(self.service._view_order_cache)
        self.service._view_order_cache.clear()
        legacy = active.model_copy(deep=True)
        self.assertIsNone(self.service._opening_order_inputs_for(legacy))
        secondary = AsyncMock(side_effect=AssertionError("unassociated snapshot entered secondary scoring"))
        with patch.object(self.service, "_get_opening_order_snapshot", secondary):
            fallback = await self.service._effective_runtime_view_order(
                active_snapshot=legacy,
                known_snapshots={option.id: legacy for option in legacy.enclosures},
                **arguments,
            )
        self.assertEqual(fallback[0], "enclosure:enc-a")
        self.assertTrue(any("current correlation inputs are unavailable" in warning for warning in legacy.warnings))
        self.assertFalse(self.service._view_order_cache)
        secondary.assert_not_awaited()

    async def test_order_refuses_invalidation_or_source_advancement_while_awaiting(self):
        self.use_real_snapshot_producers()
        active = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id="enc-a")
        original = self.service._get_opening_order_snapshot
        for invalidate in (False, True):
            with self.subTest(invalidate=invalidate):
                entered, release = asyncio.Event(), asyncio.Event()

                async def held(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    entered.set()
                    await release.wait()
                    return result

                self.service._get_opening_order_snapshot = held
                self.service._view_order_cache.clear()
                task = asyncio.create_task(self.service.get_storage_view_runtime(snapshot=active))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    if invalidate:
                        self.service.invalidate_snapshot_cache(
                            reason="synthetic.order-invalidation", cache_keys=[], invalidate_source_bundle=True,
                        )
                    else:
                        await self.service._get_inventory_source_bundle(force_refresh=True)
                    release.set()
                    runtime = await asyncio.wait_for(task, 2)
                    self.assertEqual(runtime.view_order[0], "enclosure:enc-a")
                    self.assertFalse(self.service._view_order_cache)
                    self.assertFalse(self.service._view_order_recompute_lock.locked())
                    self.assertEqual(self.service._view_order_waiters, 0)
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
        self.service._get_opening_order_snapshot = original

    async def test_order_does_not_enter_retained_slot_detail_worker(self):
        await self.exercise_retained_order_boundary(store=True)

    async def test_order_reuses_expired_fetched_sources_without_collecting(self):
        await self.exercise_retained_order_boundary(store=False)

    async def test_order_ignores_fresh_secondary_cache_from_older_sources(self):
        await self.exercise_retained_order_boundary(store=True, force=False, stale=False)

    async def test_order_ignores_stale_secondary_cache_without_background_work(self):
        await self.exercise_retained_order_boundary(store=True, force=False, stale=True)

    async def exercise_retained_order_boundary(self, *, store, force=True, stale=False):
        import threading
        from datetime import datetime, timedelta, timezone
        from app.services.inventory import InventoryService, _drain_inventory_worker

        self.service._get_opening_order_snapshot = InventoryService._get_opening_order_snapshot.__get__(self.service)
        bundle = self.fixture._empty_source_bundle()
        bundle.raw_data.enclosures = [
            {"id": option.id, "name": option.label, "elements": [
                {"slot": slot, "dev": f"da{index * 4 + slot}", "status": "OK"}
                for slot in range(1, index + 2)
            ]} for index, option in enumerate(self.options)
        ]
        self.service._collect_inventory_source_bundle = AsyncMock(return_value=bundle)
        self.service._get_inventory_source_bundle = InventoryService._get_inventory_source_bundle.__get__(self.service)
        active = await InventoryService.get_snapshot(self.service, force_refresh=True, selected_enclosure_id="enc-a")
        async def selected_snapshot(*, selected_enclosure_id=None, **kwargs):
            if selected_enclosure_id in (None, "enc-a"):
                return active
            return await InventoryService.get_snapshot(
                self.service, selected_enclosure_id=selected_enclosure_id, **kwargs,
            )
        self.service.get_snapshot = AsyncMock(side_effect=selected_snapshot)
        self.service._get_inventory_source_bundle = AsyncMock(return_value=bundle)
        self.service._source_bundle_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        old_secondary = self.fixture._view_order_snapshot(self.system.id, "enc-b", "Shelf b", 0, self.options)
        self.service._cache["enc-b"] = old_secondary
        self.service._cache_until["enc-b"] = datetime.now(timezone.utc) + timedelta(seconds=-1 if stale else 60)
        publication_generation = self.service._snapshot_publication_generation
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked():
            calls.append("worker")
            entered.set()
            if not release.wait(3):
                raise RuntimeError("synthetic retained-worker safety bound exceeded")
            return {}

        if store:
            self.service.slot_detail_store.load_all = blocked
            self.service.mapping_store.load_read_snapshot = blocked
            self.service.mapping_store.load_lookup_snapshot = blocked
        else:
            async def collect():
                await _drain_inventory_worker(asyncio.get_running_loop().run_in_executor(None, blocked))
                return bundle
            self.service._collect_inventory_source_bundle = AsyncMock(side_effect=collect)
            async def fetched_then_native(**kwargs):
                if "allow_stale_cache" in kwargs:
                    return bundle
                return await InventoryService._get_inventory_source_bundle(self.service, **kwargs)
            self.service._get_inventory_source_bundle = AsyncMock(side_effect=fetched_then_native)
        # The runtime already owns the fetched source. A secondary scorer must
        # not re-enter either native persistence or a source flight, even after
        # that source expires. Release only after observing response readiness.
        task = None
        try:
            with patch("app.services.inventory.OPENING_ORDER_TIMEOUT_SECONDS", 0.03):
                task = asyncio.create_task(self.request(
                    "/api/storage-views", f"enclosure_id=enc-a&force={'true' if force else 'false'}".encode(),
                ))
                await asyncio.sleep(0.12)
                self.assertTrue(task.done(), "opening order retained work past its deadline")
                status, body = task.result()
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["view_order"][0], "enclosure:enc-b")
                self.assertEqual(self.service._get_inventory_source_bundle.await_count, 0)
                self.assertFalse(release.is_set())
                self.assertEqual(calls, [], "optional scoring entered a retained worker")
                self.assertIs(self.service._cache["enc-b"], old_secondary)
                self.assertEqual(self.service._snapshot_publication_generation, publication_generation)
                self.assertFalse(self.service._snapshot_refresh_tasks)
                self.assertFalse(self.service._view_order_recompute_lock.locked())
                self.assertEqual(self.service._view_order_waiters, 0)
                self.service._view_order_cache.clear()
                self.assertEqual((await self.request("/api/storage-views", b"enclosure_id=enc-a"))[0], 200)
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    async def test_order_missing_or_invalidated_read_inputs_falls_back_without_producers(self):
        self.use_real_snapshot_producers()
        active = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id="enc-a")
        entry = self.service._view_order_read_inputs[id(active)]
        self.system.view_order = ["enclosure:enc-b"]
        self.service._get_secondary_runtime_snapshot = AsyncMock(side_effect=AssertionError("retained producer"))
        for inputs in (
            None,
            (entry[0], entry[1], self.service._snapshot_topology_generation - 1, *entry[3:]),
        ):
            with self.subTest(inputs_available=inputs is not None):
                self.service._view_order_read_inputs.clear()
                if inputs is not None:
                    self.service._view_order_read_inputs[id(active)] = inputs
                status, body = await self.request("/", b"enclosure_id=enc-a")
                self.assertEqual(status, 200, body)
                self.assertIn(b"current correlation inputs are unavailable", body)
                self.assertFalse(self.service._view_order_cache)
                self.assertFalse(self.service._view_order_recompute_lock.locked())
        self.service._get_secondary_runtime_snapshot.assert_not_awaited()

    async def test_order_context_cache_stays_bounded_when_preferences_change(self):
        from app.services.inventory import SYSTEM_RETENTION_MAX_ENCLOSURES

        for index in range(2 * SYSTEM_RETENTION_MAX_ENCLOSURES + 3):
            self.system.view_order = [f"enclosure:missing-{index}"]
            await self.service.get_storage_view_runtime(selected_enclosure_id="enc-a")
        self.assertEqual(len(self.service._view_order_cache), 2 * SYSTEM_RETENTION_MAX_ENCLOSURES)

    async def test_forced_refresh_recorrelates_once_after_newer_background_sources(self):
        await self.exercise_source_overlap()

    async def test_unrelated_foreground_source_supersession_still_fails_closed(self):
        await self.exercise_source_overlap(foreground=True)

    async def test_second_background_source_supersession_is_bounded_and_cannot_publish_stale(self):
        await self.exercise_source_overlap(repeat=True)

    async def test_changed_enclosures_do_not_admit_a_retry(self):
        await self.exercise_source_overlap(change_topology=True)

    async def test_source_invalidation_during_retry_does_not_publish(self):
        await self.exercise_source_overlap(invalidate_retry=True)

    async def exercise_source_overlap(self, *, foreground=False, repeat=False,
                                      change_topology=False, invalidate_retry=False):
        from datetime import datetime, timedelta, timezone
        from app.services.inventory import InventoryService

        # Mock only appliance collection. Use real source flights, correlation,
        # background scheduling, publication fences and the HTTP inventory route.
        self.service.get_snapshot = InventoryService.get_snapshot.__get__(self.service)
        self.service._get_inventory_source_bundle = InventoryService._get_inventory_source_bundle.__get__(self.service)
        bundles = []
        for index in range(4 if repeat else 3):
            bundle = self.fixture._empty_source_bundle()
            bundle.raw_data.enclosures = [
                {"id": option.id, "name": option.label, "elements": []} for option in self.options
            ]
            bundle.raw_data.enclosures[0]["elements"] = [{"slot": 1, "dev": "da0", "status": "OK"}]
            bundle.raw_data.disks = [{"name": "da0", "serial": f"SANITIZED-S{index}", "size": 1000}]
            bundle.warnings = [f"source S{index}"]
            if change_topology and index == 2:
                bundle.raw_data.enclosures.append({"id": "enc-c", "name": "Shelf C", "elements": []})
            bundles.append(bundle)
        self.service._collect_inventory_source_bundle = AsyncMock(side_effect=bundles)
        await self.service.get_snapshot(force_refresh=True, selected_enclosure_id="enc-a")
        await self.service._get_snapshot_result(selected_enclosure_id="enc-b", force_source_refresh=False)
        public_a = self.service._cache["enc-a"]
        entered = asyncio.Event()
        release = asyncio.Event()
        retry_entered = asyncio.Event()
        retry_release = asyncio.Event()
        original_build = self.service._build_snapshot
        a_builds = []

        async def build(*args, **kwargs):
            snapshot = await original_build(*args, **kwargs)
            if kwargs.get("selected_enclosure_id") == "enc-a":
                a_builds.append(snapshot)
                if len(a_builds) == 1:
                    entered.set()
                    await release.wait()
                elif repeat or invalidate_retry:
                    retry_entered.set()
                    await retry_release.wait()
            return snapshot

        self.service._build_snapshot = build
        first = asyncio.create_task(self.request("/api/inventory", b"enclosure_id=enc-a&force=true"))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(a_builds[0].warnings, ["source S1"])
        async def refresh_other():
            self.service._cache_until["enc-b"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            await self.service.get_snapshot(selected_enclosure_id="enc-b", allow_stale_cache=True)
            await asyncio.wait_for(self.service._snapshot_refresh_tasks["enc-b"], 2)

        try:
            if foreground:
                await self.service.get_snapshot(force_refresh=True, selected_enclosure_id="enc-b")
            else:
                await refresh_other()
            self.assertEqual(self.service._cache["enc-b"].warnings, ["source S2"])
            # Even an expired S2 must be pinned for re-correlation, not fetched
            # again or replaced by an unrelated source flight.
            self.service._source_bundle_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        finally:
            release.set()
        if repeat or invalidate_retry:
            await asyncio.wait_for(retry_entered.wait(), 2)
            try:
                if repeat:
                    await refresh_other()
                else:
                    self.service.invalidate_snapshot_cache(
                        reason="test.source_invalidation", cache_keys=[], invalidate_source_bundle=True,
                    )
            finally:
                retry_release.set()
        status, body = await asyncio.wait_for(first, 2)
        if foreground or repeat or change_topology or invalidate_retry:
            self.assertEqual(status, 503, body)
            self.assertEqual(len(a_builds), 2 if repeat or invalidate_retry else 1)
            self.assertIs(self.service._cache["enc-a"], public_a)
            self.assertEqual(self.service._snapshot_source_publications["enc-a"], bundles[0].publication)
            self.assertEqual(self.service._collect_inventory_source_bundle.await_count, 4 if repeat else 3)
            self.assertEqual(self.service._canonical_options_source_publication, bundles[-1].publication)
            self.assertEqual(set(self.service._cache), {"enc-a", "enc-b"})
            return
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["warnings"], ["source S2"])
        self.assertIn("SANITIZED-S2", [slot["serial"] for slot in json.loads(body)["slots"]])
        self.assertNotIn("SANITIZED-S1", [slot["serial"] for slot in json.loads(body)["slots"]])
        self.assertEqual(len(a_builds), 2)
        self.assertIs(self.service._cache["enc-a"], a_builds[1])
        self.assertEqual(self.service._snapshot_source_publications["enc-a"], bundles[2].publication)
        self.assertEqual(self.service._collect_inventory_source_bundle.await_count, 3)
        self.assertEqual(self.service._canonical_options_source_publication, bundles[2].publication)

    async def test_over_cap_page_and_runtime_use_saved_then_discovery_without_scoring(self):
        from app.models.domain import EnclosureOption
        from app.services.inventory import SYSTEM_RETENTION_MAX_ENCLOSURES

        self.options = [EnclosureOption(id=f"enc-{index}", label=f"Shelf {index}")
                        for index in range(SYSTEM_RETENTION_MAX_ENCLOSURES + 1)]
        self.snapshots = {
            option.id: self.fixture._view_order_snapshot(
                self.system.id, option.id, option.label, 1, self.options,
            ) for option in self.options
        }
        self.service._get_opening_order_snapshot = AsyncMock(side_effect=AssertionError("scoring over cap"))
        for saved in ([], ["enclosure:enc-2", "view:primary-chassis", "enclosure:missing"]):
            with self.subTest(saved=saved):
                self.system.view_order = saved
                expected = list(dict.fromkeys([
                    *(entry for entry in saved if entry != "enclosure:missing"),
                    *(f"enclosure:{option.id}" for option in self.options), "view:primary-chassis",
                ]))
                status, body = await self.request("/", b"enclosure_id=enc-0")
                self.assertEqual(status, 200, body)
                self.assertIn(b"Opening-view scoring was skipped", body)
                runtime_line = next(line.strip() for line in body.decode().splitlines()
                                    if line.strip().startswith("storageViewsRuntime:"))
                page_runtime = json.loads(runtime_line.split(":", 1)[1].rstrip(","))
                self.assertEqual(page_runtime["view_order"], expected)
                for _ in range(2):
                    status, body = await self.request("/api/storage-views", b"enclosure_id=enc-0")
                    self.assertEqual(status, 200, body)
                    self.assertEqual(json.loads(body)["view_order"], expected)
                self.assertEqual(sum("Opening-view scoring was skipped" in warning
                                     for warning in self.snapshots["enc-0"].warnings), 1)
        self.service._get_opening_order_snapshot.assert_not_awaited()
        # This is a page fallback, not permission to weaken retention accounting.
        from app.services.inventory import SystemRetentionTooLargeError
        with self.assertRaises(SystemRetentionTooLargeError):
            await self.service.get_system_disk_retention(snapshot=self.snapshots["enc-0"])


class RouteContractTests(unittest.TestCase):
    def test_main_route_matrix_is_frozen(self) -> None:
        self.assertEqual(_route_matrix(app_main.create_app()), APP_ROUTE_MATRIX)

    def test_inventory_route_maps_unknown_enclosure_to_bounded_404(self) -> None:
        service = MagicMock()
        service.get_snapshot = AsyncMock(side_effect=UnknownEnclosureError())
        registry = MagicMock()
        registry.get_service.return_value = service
        application = app_main.create_app()
        messages: list[dict[str, object]] = []

        async def invoke() -> None:
            request_sent = False

            async def receive() -> dict[str, object]:
                nonlocal request_sent
                if not request_sent:
                    request_sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message: dict[str, object]) -> None:
                messages.append(message)

            await application(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/api/inventory",
                    "raw_path": b"/api/inventory",
                    "query_string": b"enclosure_id=caller-controlled-value",
                    "headers": [],
                    "client": ("127.0.0.1", 1),
                    "server": ("test", 80),
                    "root_path": "",
                },
                receive,
                send,
            )

        with patch.object(app_routes, "get_inventory_registry", return_value=registry):
            asyncio.run(invoke())

        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        self.assertEqual(start["status"], 404)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "detail": "Requested enclosure is not available for this system."},
        )
        self.assertNotIn(b"caller-controlled-value", body)

    def test_partial_locate_http_result_uses_real_aggregation_and_refreshes_once(self) -> None:
        for action in ("IDENTIFY", "CLEAR"):
            for same_host, failure in ((True, "action"), (False, "action"), (False, "preflight")):
                for reverse in (False, True):
                    with self.subTest(action=action, same_host=same_host, failure=failure, reverse=reverse):
                        status, payload, service = self.locate_http_result(
                            action=action, same_host=same_host, failure=failure, reverse=reverse,
                        )
                        self.assertEqual(status, 200, payload)
                        self.assertTrue(payload["ok"])
                        self.assertTrue(payload["partial"])
                        self.assertFalse(payload["confirmed"])
                        self.assertIs(payload["identify_active"], True if action == "IDENTIFY" else None)
                        self.assertEqual(len(payload["paths"]), 2)
                        self.assertEqual(len(payload["failures"]), 1)
                        self.assertLessEqual(len(payload["failures"][0]), 240)
                        service.schedule_led_snapshot_refresh.assert_called_once_with(enclosure_id="shelf-a")
                        service.invalidate_physical_enclosure_snapshot_cache.assert_not_called()

    def test_moved_locate_http_result_preserves_refusal_and_exceptional_refresh(self) -> None:
        status, payload, service = self.locate_http_result(failure="moved")
        self.assertEqual(status, 400)
        self.assertIn("switched through one enclosure path", payload["detail"])
        self.assertNotIn(("shelf-a", 0), service._led_slot_targets)
        service.schedule_led_snapshot_refresh.assert_called_once_with(enclosure_id="shelf-a")

    def test_total_locate_http_failure_remains_a_refusal(self) -> None:
        for action in ("IDENTIFY", "CLEAR"):
            for failure in ("preflight", "all_actions"):
                with self.subTest(action=action, failure=failure):
                    status, _payload, service = self.locate_http_result(action=action, same_host=True, failure=failure)
                    self.assertEqual(status, 400)
                    service.schedule_led_snapshot_refresh.assert_not_called()

    def test_mapping_save_partial_clear_warns_and_persists_with_real_led_aggregation(self) -> None:
        from app.models.domain import LedAction

        for same_host, failure in ((True, "action"), (False, "action"), (False, "preflight")):
            for reverse in (False, True):
                with self.subTest(same_host=same_host, failure=failure, reverse=reverse):
                    status, payload, service, sent, results = self.mapping_save_http_result(
                        same_host=same_host, failure=failure, reverse=reverse,
                    )
                    self.assertEqual(status, 200, payload)
                    self.assertTrue(payload["ok"])
                    self.assert_mapping_save_persisted(payload, service)
                    self.assertEqual(len(results), 1)
                    result = results[0]
                    self.assertTrue(result["partial"])
                    self.assertFalse(result["confirmed"])
                    self.assertIsNone(result["identify_active"])
                    self.assertEqual(
                        {path["ses_device"]: path["identify_active"] for path in result["paths"]},
                        {"/dev/sg27": False, "/dev/sg9": None},
                    )
                    self.assertEqual(
                        payload["warning"],
                        "Saved mapping, but the identify LED clear was only partially confirmed; "
                        "another path failed and may still be lit.",
                    )
                    self.assertNotIn("synthetic failure", json.dumps(payload))
                    self.assertLessEqual(len(payload["warning"]), 240)
                    service.set_slot_led.assert_awaited_once_with(
                        0, LedAction.clear, selected_enclosure_id="shelf-a", invalidate_snapshot=False,
                    )
                    self.assert_mapping_refresh_owner(service, led_changed=True)
                    service.schedule_led_snapshot_refresh.assert_not_called()
                    actions = [command for _host, command in sent if "--dev-slot-num" in command]
                    self.assertEqual(len(actions), 2 if failure == "action" else 1)
                    self.assertTrue(all("--clear=ident" in command for command in actions))

    def test_mapping_save_complete_clear_retains_no_warning(self) -> None:
        for same_host in (False, True):
            with self.subTest(same_host=same_host):
                status, payload, service, _sent, results = self.mapping_save_http_result(
                    same_host=same_host, failure="none",
                )
                self.assertEqual(status, 200, payload)
                self.assertIsNone(payload["warning"])
                self.assert_mapping_save_persisted(payload, service)
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0]["confirmed"])
                self.assertIs(results[0]["identify_active"], False)
                self.assertNotIn("partial", results[0])
                self.assert_mapping_refresh_owner(service, led_changed=True)
                service.schedule_led_snapshot_refresh.assert_not_called()

    def test_mapping_save_total_clear_failure_and_moved_target_keep_existing_warning(self) -> None:
        for failure, same_host in (("all_actions", True), ("preflight", True), ("moved", False)):
            for reverse in (False, True):
                with self.subTest(failure=failure, reverse=reverse):
                    status, payload, service, sent, results = self.mapping_save_http_result(
                        same_host=same_host, failure=failure, reverse=reverse,
                    )
                    self.assertEqual(status, 200, payload)
                    self.assert_mapping_save_persisted(payload, service)
                    self.assertEqual(results, [])
                    self.assertEqual(
                        payload["warning"],
                        "Saved mapping, but failed to clear the identify LED; see application logs.",
                    )
                    self.assertNotIn("synthetic failure", json.dumps(payload))
                    service.set_slot_led.assert_awaited_once()
                    self.assert_mapping_refresh_owner(service, led_changed=False)
                    if failure == "moved":
                        self.assertNotIn(("shelf-a", 0), service._led_slot_targets)
                        service.schedule_led_snapshot_refresh.assert_called_once_with(enclosure_id="shelf-a")
                        self.assertFalse(any(
                            "--dev-slot-num" in command and command.endswith("/dev/sg9")
                            for _host, command in sent
                        ))
                    else:
                        service.schedule_led_snapshot_refresh.assert_not_called()
                    if failure == "preflight":
                        self.assertFalse(any("--dev-slot-num" in command for _host, command in sent))

    def test_mapping_save_clear_opt_out_sends_no_led_action(self) -> None:
        status, payload, service, sent, results = self.mapping_save_http_result(
            clear_identify_after_save=False,
        )
        self.assertEqual(status, 200, payload)
        self.assert_mapping_save_persisted(payload, service)
        self.assertIsNone(payload["warning"])
        self.assertEqual(results, [])
        self.assertEqual(sent, [])
        service.set_slot_led.assert_not_awaited()
        service.schedule_led_snapshot_refresh.assert_not_called()
        self.assert_mapping_refresh_owner(service, led_changed=False)

    def test_mapping_save_auth_and_revision_gates_precede_auto_clear(self) -> None:
        for options, expected_status in (
            ({"expected_revision": "f" * 64}, 409),
            ({"expected_revision": None}, 422),
            ({"origin": "http://other.example.test"}, 403),
        ):
            with self.subTest(options=options):
                status, _payload, service, sent, results = self.mapping_save_http_result(**options)
                self.assertEqual(status, expected_status)
                self.assertEqual(service.mapping_store.load_all(), {})
                self.assertEqual(sent, [])
                self.assertEqual(results, [])
                service.set_slot_led.assert_not_awaited()
                service.invalidate_physical_enclosure_snapshot_cache.assert_not_called()
                service.schedule_led_snapshot_refresh.assert_not_called()
                self.assertFalse(any(call.kwargs.get("force_refresh") for call in service.get_snapshot.await_args_list))

    def assert_mapping_save_persisted(self, payload, service):
        from app.services.mapping_store import MappingStore

        persisted = MappingStore(service.mapping_store.file_path).get_mapping(service.system.id, "shelf-a", 0)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.model_dump(mode="json"), payload["mapping"])
        self.assertEqual(persisted.serial, "SANITIZED-MAPPING-978")
        self.assertEqual(payload["snapshot"]["selected_system_id"], service.system.id)
        self.assertEqual(payload["snapshot"]["selected_enclosure_id"], "shelf-a")

    def assert_mapping_refresh_owner(self, service, *, led_changed):
        service.invalidate_physical_enclosure_snapshot_cache.assert_called_once_with(
            reason="route.save_mapping", enclosure_id="shelf-a", invalidate_source_bundle=led_changed,
        )
        self.assertEqual(
            [call.kwargs for call in service.get_snapshot.await_args_list if call.kwargs.get("force_refresh")],
            [{"force_refresh": True, "selected_enclosure_id": "shelf-a"}],
        )

    def mapping_save_http_result(self, *, origin="http://127.0.0.1", **options):
        from tests.test_inventory import partial_locate_fixture

        body = {"serial": "SANITIZED-MAPPING-978"}
        for key in ("clear_identify_after_save", "expected_revision"):
            if key in options:
                body[key] = options.pop(key)
        service, sent = partial_locate_fixture(self, observed=False, **options)
        # Keep snapshot collection synthetic; mapping persistence, bounds, LED
        # aggregation, session sequencing and target preflight remain real.
        service.get_snapshot = AsyncMock(return_value=service._cache["shelf-a"])
        revision = service.mapping_store.save_revision(service.system.id, "shelf-a", 0)
        body.setdefault("expected_revision", revision)
        if body["expected_revision"] is None:
            del body["expected_revision"]
        led_results = []
        real_set_slot_led = service.set_slot_led

        async def capture_led_result(*args, **kwargs):
            result = await real_set_slot_led(*args, **kwargs)
            led_results.append(result)
            return result

        service.set_slot_led = AsyncMock(side_effect=capture_led_result)
        registry = MagicMock()
        registry.get_service.return_value = service
        application = app_main.create_app()
        application.state.operator_auth_settings = SimpleNamespace(auth_mode="network", allowed_host_names=frozenset())
        application.state.read_ui_public_origin = "http://127.0.0.1"
        messages = []

        async def invoke():
            async def receive():
                return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

            async def send(message):
                messages.append(message)

            await application(
                {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "POST", "scheme": "http", "path": "/api/slots/0/mapping",
                    "raw_path": b"/api/slots/0/mapping",
                    "query_string": b"system_id=synthetic-led-test&enclosure_id=shelf-a",
                    "headers": [(b"content-type", b"application/json"), (b"host", b"127.0.0.1"), (b"origin", origin.encode())],
                    "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
                }, receive, send,
            )

        with patch.object(app_routes, "get_inventory_registry", return_value=registry):
            asyncio.run(invoke())
        start = next(message for message in messages if message["type"] == "http.response.start")
        response_body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return start["status"], json.loads(response_body), service, sent, led_results

    def locate_http_result(self, *, action="IDENTIFY", **fixture_options):
        from tests.test_inventory import partial_locate_fixture

        service, _sent = partial_locate_fixture(self, observed=action == "IDENTIFY", **fixture_options)
        registry = MagicMock()
        registry.get_service.return_value = service
        application = app_main.create_app()
        application.state.operator_auth_settings = SimpleNamespace(auth_mode="network", allowed_host_names=frozenset())
        application.state.read_ui_public_origin = "http://127.0.0.1"
        messages = []

        async def invoke():
            async def receive():
                return {"type": "http.request", "body": json.dumps({"action": action}).encode(), "more_body": False}

            async def send(message):
                messages.append(message)

            await application(
                {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "POST", "scheme": "http", "path": "/api/slots/0/led",
                    "raw_path": b"/api/slots/0/led",
                    "query_string": b"system_id=synthetic-led-test&enclosure_id=shelf-a",
                    "headers": [(b"content-type", b"application/json"), (b"host", b"127.0.0.1"), (b"origin", b"http://127.0.0.1")],
                    "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
                }, receive, send,
            )

        with patch.object(app_routes, "get_inventory_registry", return_value=registry):
            asyncio.run(invoke())
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return start["status"], json.loads(body), service

    def test_inventory_retention_scope_system_reports_system_wide_totals(self) -> None:
        from app.models.domain import InventorySnapshot, InventorySummary
        from app.services.inventory_accounting import DiskRetentionAccounting

        snapshot = InventorySnapshot(
            slots=[],
            refresh_interval_seconds=30,
            summary=InventorySummary(
                disk_count=36,
                source_disk_count=36,
                rendered_unique_disk_count=24,
                unplaced_disk_count=12,
            ),
        )
        service = MagicMock()
        service.get_snapshot = AsyncMock(return_value=snapshot)
        service.get_snapshot_with_system_retention = AsyncMock(
            return_value=(snapshot, DiskRetentionAccounting(36, 36, 0, 0))
        )
        registry = MagicMock()
        registry.get_service.return_value = service
        route = next(
            route for route in app_main.create_app().routes
            if getattr(route, "path", None) == "/api/inventory"
        )
        request = MagicMock()
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "runtime_warnings_for", return_value=[]),
            patch.object(app_routes, "live_write_policy", return_value=None),
        ):
            enclosure = json.loads(asyncio.run(route.endpoint(request=request)).body)
            system = json.loads(
                asyncio.run(route.endpoint(request=request, retention_scope="system")).body
            )

        self.assertEqual(enclosure["summary"]["unplaced_disk_count"], 12)
        # The system scope is admitted before its snapshot is built, so a
        # refused request never refreshes; the plain scope is unchanged.
        service.get_snapshot.assert_awaited_once_with(
            force_refresh=False, selected_enclosure_id=None, allow_stale_cache=True,
        )
        service.get_snapshot_with_system_retention.assert_awaited_once_with(
            force_refresh=False, selected_enclosure_id=None,
        )
        self.assertEqual(
            {key: system["summary"][key] for key in (
                "source_disk_count",
                "rendered_unique_disk_count",
                "duplicate_disk_view_count",
                "unplaced_disk_count",
            )},
            {
                "source_disk_count": 36,
                "rendered_unique_disk_count": 36,
                "duplicate_disk_view_count": 0,
                "unplaced_disk_count": 0,
            },
        )
        self.assertEqual(system["summary"]["disk_count"], 36)
        self.assertEqual(set(system["summary"]), set(enclosure["summary"]))

    def test_capacity_errors_map_to_retryable_503_with_their_own_retry_hint(self) -> None:
        cases = (
            (SnapshotStateBusyError(), "1", "The server is busy. Try again in a moment."),
            (SnapshotExportBusyError(), "5", "The server is busy. Try again in a moment."),
        )
        for error, retry_after, detail in cases:
            with self.subTest(error=type(error).__name__):
                self.assertIs(app_main.app.exception_handlers[type(error)], app_main.mapped_exception_handler)
                response = asyncio.run(app_main.mapped_exception_handler(MagicMock(), error))

                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers["retry-after"], retry_after)
                self.assertEqual(json.loads(response.body), {"ok": False, "detail": detail})

    def test_system_retention_refusals_map_to_bounded_503s(self) -> None:
        from app.services.inventory import SystemRetentionBusyError

        for error, retry_after in ((SystemRetentionBusyError(), "5"),):
            with self.subTest(error=type(error).__name__):
                self.assertIs(app_main.app.exception_handlers[type(error)], app_main.mapped_exception_handler)
                response = asyncio.run(app_main.mapped_exception_handler(MagicMock(), error))
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers.get("retry-after"), retry_after)
                self.assertEqual(json.loads(response.body), {"ok": False, "detail": str(error)})

    def test_retention_scope_too_large_has_typed_bounded_count_contract(self) -> None:
        from app.services.inventory import SYSTEM_RETENTION_MAX_ENCLOSURES, SystemRetentionTooLargeError

        self.assertIs(app_main.app.exception_handlers[SystemRetentionTooLargeError], app_main.mapped_exception_handler)
        response = asyncio.run(app_main.mapped_exception_handler(MagicMock(), SystemRetentionTooLargeError()))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("retry-after", response.headers)
        self.assertEqual(json.loads(response.body), {
            "ok": False,
            "error": "retention_scope_too_large",
            "detail": "This system has too many enclosures for a system-wide disk check.",
            # The exception does not carry the exact size, only proof it exceeds the cap.
            "enclosure_count_minimum": SYSTEM_RETENTION_MAX_ENCLOSURES + 1,
            "enclosure_cap": SYSTEM_RETENTION_MAX_ENCLOSURES,
        })

    def test_unknown_enclosure_maps_to_404_without_a_retry_hint(self) -> None:
        response = asyncio.run(app_main.mapped_exception_handler(MagicMock(), UnknownEnclosureError()))

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("retry-after", response.headers)
        self.assertEqual(
            json.loads(response.body),
            {"ok": False, "detail": "Requested enclosure is not available for this system."},
        )

    def test_slot_bounds_reuse_the_exception_response_table(self) -> None:
        for error, status_code, retry_after in (
            (UnknownEnclosureError(), 404, None),
            (SnapshotStateBusyError(), 503, "1"),
        ):
            with self.subTest(error=type(error).__name__):
                service = SimpleNamespace(get_snapshot=AsyncMock(side_effect=error))
                with self.assertRaises(app_main.HTTPException) as raised:
                    asyncio.run(app_route_support.resolve_layout_slots(service, None))

                self.assertEqual(raised.exception.status_code, status_code)
                self.assertEqual(raised.exception.detail, str(error))
                self.assertEqual((raised.exception.headers or {}).get("Retry-After"), retry_after)

    def test_slot_bounds_report_unavailable_when_discovery_found_no_enclosures(self) -> None:
        unreachable = SimpleNamespace(selected_enclosure_id=None, enclosures=[])
        wrong_shelf = SimpleNamespace(
            selected_enclosure_id="enc-b", enclosures=[SimpleNamespace(id="enc-b")]
        )
        for snapshot, status_code in ((unreachable, 503), (wrong_shelf, 404)):
            with self.subTest(selected=snapshot.selected_enclosure_id):
                service = SimpleNamespace(get_snapshot=AsyncMock(return_value=snapshot))
                with self.assertRaises(app_main.HTTPException) as raised:
                    asyncio.run(app_route_support.resolve_layout_slots(service, "enc-a"))
                self.assertEqual(raised.exception.status_code, status_code)

        service = SimpleNamespace(get_snapshot=AsyncMock(return_value=unreachable))
        self.assertEqual(
            asyncio.run(app_route_support.resolve_read_layout_slots(service, "enc-a")),
            (None, "unavailable"),
        )

    def test_admin_route_matrix_is_frozen(self) -> None:
        self.assertEqual(_route_matrix(admin_main.create_app()), ADMIN_ROUTE_MATRIX)

    def test_critical_openapi_request_and_response_contracts_are_frozen(self) -> None:
        app_schema = app_main.create_app().openapi()
        admin_schema = admin_main.create_app().openapi()

        mapping_path = app_schema["paths"]["/api/slots/{slot}/mapping"]
        self.assertEqual(set(mapping_path), {"post", "delete"})
        self.assertEqual(mapping_path["post"]["operationId"], "save_mapping_api_slots__slot__mapping_post")
        self.assertEqual(mapping_path["delete"]["operationId"], "clear_mapping_api_slots__slot__mapping_delete")
        self.assertEqual(set(mapping_path["post"]["responses"]), {"200", "422"})
        self.assertEqual(set(mapping_path["delete"]["responses"]), {"200", "422"})

        import_operation = app_schema["paths"]["/api/mappings/import"]["post"]
        request_schema = import_operation["requestBody"]["content"]["application/json"]["schema"]
        component = app_schema["components"]["schemas"][request_schema["$ref"].rsplit("/", 1)[-1]]
        self.assertEqual(
            set(component["required"]),
            {"bundle", "expected_revision", "import_digest", "confirmed"},
        )

        export_operation = admin_schema["paths"]["/api/admin/backup/export"]["post"]
        self.assertEqual(export_operation["operationId"], "export_backup_api_admin_backup_export_post")
        self.assertEqual(set(export_operation["responses"]), {"200", "422"})
        import_admin = admin_schema["paths"]["/api/admin/backup/import"]["post"]
        self.assertEqual(import_admin["operationId"], "import_backup_api_admin_backup_import_post")
        self.assertEqual(
            [parameter["name"] for parameter in import_admin["parameters"]],
            ["stop_services", "restart_services"],
        )

    def test_sas_fabric_endpoint_name_supports_real_url_lookup(self) -> None:
        application = app_main.create_app()
        self.assertEqual(str(application.url_path_for("sas_fabric_view")), "/sas-fabric")

    def test_main_create_app_delegates_routes_to_apirouter(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("app.routes"))
        create_app = _create_app_node(app_main)
        nested = [node for node in create_app.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        route_decorators = [
            decorator
            for node in ast.walk(create_app)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
        ]
        self.assertEqual(route_decorators, [])
        end_lineno = create_app.end_lineno
        if end_lineno is None:
            self.fail("create_app has no end line")
        self.assertLessEqual(end_lineno - create_app.lineno + 1, 110)
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "include_router_preserving_route_objects"
                for node in ast.walk(create_app)
            )
        )
        self.assertEqual(
            {node.name for node in nested},
            {"lifespan", "http_exception_handler", "unhandled_exception_handler"},
        )

    def test_admin_create_app_delegates_routes_to_apirouter(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("admin_service.routes"))
        create_app = _create_app_node(admin_main)
        route_decorators = [
            decorator
            for node in ast.walk(create_app)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
        ]
        self.assertEqual(route_decorators, [])
        end_lineno = create_app.end_lineno
        if end_lineno is None:
            self.fail("create_app has no end line")
        self.assertLessEqual(end_lineno - create_app.lineno + 1, 120)
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and (
                    (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "include_router"
                    )
                    or (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "include_router_preserving_route_objects"
                    )
                )
                for node in ast.walk(create_app)
            )
        )

    def test_route_modules_do_not_import_their_main_compatibility_facades(self) -> None:
        for relative_path, forbidden_module in (
            ("app/routes.py", "app.main"),
            ("admin_service/routes.py", "admin_service.main"),
        ):
            with self.subTest(route_module=relative_path):
                tree = ast.parse(Path(relative_path).read_text(encoding="utf-8"))
                imported_modules = {
                    node.module
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                }
                self.assertNotIn(forbidden_module, imported_modules)
        # Route modules import their collaborators, so file-wide lint suppressions
        # (which would hide a mistyped name) must not come back.
        for relative_path in (
            "app/main.py",
            "app/routes.py",
            "admin_service/main.py",
            "admin_service/routes.py",
        ):
            with self.subTest(blanket_suppression=relative_path):
                source = Path(relative_path).read_text(encoding="utf-8")
                self.assertNotRegex(source, r"(?m)^# ruff: noqa")
                self.assertNotRegex(source, r"(?m)^# pyright: report")

    def test_main_routes_deduplicate_contiguous_service_perf_preambles(self) -> None:
        route_path = Path(__file__).resolve().parents[1] / "app" / "routes.py"
        tree = ast.parse(route_path.read_text(encoding="utf-8"))
        build_router = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "build_router"
        )
        duplicate_endpoints: list[str] = []
        for endpoint in build_router.body:
            if (
                not isinstance(endpoint, (ast.FunctionDef, ast.AsyncFunctionDef))
                or endpoint.name == "route_service"
            ):
                continue
            statements = endpoint.body
            for index in range(len(statements) - 2):
                first, second, third = statements[index : index + 3]
                first_call = first.value if isinstance(first, ast.Assign) else None
                second_call = second.value if isinstance(second, ast.Assign) else None
                third_call = third.value if isinstance(third, ast.Expr) else None
                if not (
                    isinstance(first_call, ast.Call)
                    and isinstance(first_call.func, ast.Name)
                    and first_call.func.id == "get_inventory_registry"
                    and isinstance(second_call, ast.Call)
                    and isinstance(second_call.func, ast.Attribute)
                    and second_call.func.attr == "get_service"
                    and isinstance(third_call, ast.Call)
                    and isinstance(third_call.func, ast.Name)
                    and third_call.func.id == "add_perf_metadata"
                ):
                    continue
                duplicate_endpoints.append(endpoint.name)
        self.assertEqual(duplicate_endpoints, [])

    def test_route_handler_resolves_routes_module_patch_after_app_creation(self) -> None:
        application = app_main.create_app()
        route = cast(
            APIRoute,
            next(route for route in application.routes if getattr(route, "path", None) == "/healthz"),
        )
        service = MagicMock()
        service.peek_cached_snapshot.return_value = None
        registry = MagicMock()
        registry.get_service.return_value = service

        with patch.object(app_routes, "get_inventory_registry", return_value=registry) as getter:
            response = asyncio.run(route.endpoint(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(startup_problems=())))))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["cache_state"], "empty")
        getter.assert_called_once_with()

    def test_admin_handler_resolves_temporary_file_response_patch_after_app_creation(self) -> None:
        application = admin_main.create_app()
        route = cast(
            APIRoute,
            next(
                route
                for route in application.routes
                if getattr(route, "path", None) == "/api/admin/backup/export"
            ),
        )
        artifact = SimpleNamespace(
            path=Path("/tmp/synthetic-backup.zip"),
            filename="synthetic-backup.zip",
            media_type="application/zip",
            manifest={"packaging": "zip"},
            cleanup=MagicMock(),
        )
        maintenance = SimpleNamespace(stopped_containers=[], restarted_containers=[], restart_failures={})
        service = MagicMock()
        service.export_bundle.return_value = (artifact, maintenance)
        sentinel = object()

        with (
            patch.object(admin_routes, "get_maintenance_service", return_value=service),
            patch.object(admin_routes, "TemporaryFileResponse", return_value=sentinel) as response_class,
        ):
            response = asyncio.run(
                route.endpoint(
                    SystemBackupExportRequest(
                        packaging="zip",
                        encrypt=True,
                        passphrase="test-passphrase",
                    ),
                    stop_services=False,
                    restart_services=True,
                )
            )

        self.assertIs(response, sentinel)
        response_class.assert_called_once()
        artifact.cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
