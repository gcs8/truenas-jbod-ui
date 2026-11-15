"""Remote collector failures must publish fixed source-status reasons only."""

from __future__ import annotations

import io
import json
import ssl
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app import routes as app_routes
from app.config import BMCConfig, SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, SlotView
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.quantastor_api import QuantastorRESTClient
from app.services.slot_detail_store import SlotDetailStore
from app.services.supermicro_bmc import BMCInventory, SupermicroBMCService
from app.services.truenas_ws import (
    TrueNASAPIError,
    TrueNASRawData,
    build_ddp_error,
    build_jsonrpc_error,
)


PRIVATE_MARKER = "SYNTHETIC-REMOTE-DETAIL"


class InventoryStatusErrorTextTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        *,
        platform: str = "core",
        api_error: BaseException | None = None,
        ssh_error: BaseException | None = None,
        bmc_error: BaseException | None = None,
    ) -> InventoryService:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        settings = Settings()
        system = SystemConfig(
            id="synthetic-status",
            truenas=TrueNASConfig(platform=platform),
            ssh=SSHConfig(enabled=ssh_error is not None, host="source.example.test"),
            bmc=BMCConfig(enabled=bmc_error is not None, host="bmc.example.test"),
        )
        api = AsyncMock()
        if api_error is None:
            api.fetch_all.return_value = TrueNASRawData(
                enclosures=[],
                disks=[],
                pools=[],
                disk_temperatures={},
                smart_test_results=[],
            )
        else:
            api.fetch_all.side_effect = api_error
        ssh = AsyncMock()
        if ssh_error is not None:
            ssh.run_planned_commands.side_effect = ssh_error
        bmc = None
        if bmc_error is not None:
            bmc = MagicMock()
            bmc.fetch_inventory.side_effect = bmc_error
        return InventoryService(
            settings,
            system,
            api,
            ssh,
            bmc,
            MappingStore(str(root / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(root / "slot_detail_cache.json")),
        )

    async def _serialized_bundle(self, **service_kwargs) -> tuple[dict, str]:
        service = self._service(**service_kwargs)
        with patch("app.services.inventory.logger.warning"):
            bundle = await service._collect_inventory_source_bundle()
        snapshot = InventorySnapshot(
            slots=[],
            refresh_interval_seconds=30,
            sources=bundle.sources,
            warnings=bundle.warnings,
        )
        payload = snapshot.model_dump(mode="json")
        return payload, json.dumps(payload, sort_keys=True)

    @staticmethod
    def _error_with_cause(cause: BaseException) -> TrueNASAPIError:
        try:
            raise cause
        except BaseException as caught:
            try:
                raise TrueNASAPIError(PRIVATE_MARKER) from caught
            except TrueNASAPIError as error:
                return error

    @staticmethod
    def _smart_slot() -> SlotView:
        return SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            enclosure_id="synthetic-enclosure",
            present=True,
            device_name="da0",
            serial="SANITIZED-SMART",
            transport_protocol="SAS",
        )

    @staticmethod
    def _route(path: str, method: str):
        return next(
            getattr(route, "endpoint")
            for route in app_routes.build_router().routes
            if getattr(route, "path", None) == path and method in getattr(route, "methods", set())
        )

    async def test_success_control_keeps_the_named_reachable_status(self) -> None:
        payload, serialized = await self._serialized_bundle()

        self.assertEqual(payload["sources"]["api"]["message"], "TrueNAS API reachable.")
        self.assertNotIn(PRIVATE_MARKER, serialized)

    async def test_ddp_details_are_replaced_by_fixed_public_reasons(self) -> None:
        error = build_ddp_error(
            "pool.query",
            {"reason": "synthetic failure", "data": {"trace": PRIVATE_MARKER}},
        )
        self.assertNotIn(PRIVATE_MARKER, str(error))
        payload, serialized = await self._serialized_bundle(api_error=error)

        self.assertEqual(
            payload["sources"]["api"]["message"],
            "TrueNAS API reported a collection error.",
        )
        self.assertNotIn(PRIVATE_MARKER, serialized)

        busy = build_ddp_error(
            "pool.query",
            {"reason": f"maximum number of concurrent calls; {PRIVATE_MARKER}"},
        )
        payload, serialized = await self._serialized_bundle(api_error=busy)

        self.assertEqual(payload["sources"]["api"]["message"], "TrueNAS API is busy; collection will retry.")
        self.assertNotIn(PRIVATE_MARKER, serialized)

    async def test_certificate_details_are_replaced_by_a_fixed_public_reason(self) -> None:
        error = ssl.SSLCertVerificationError(1, "synthetic certificate failure")
        error.verify_code = 10
        error.verify_message = PRIVATE_MARKER
        payload, serialized = await self._serialized_bundle(api_error=error)

        self.assertEqual(
            payload["sources"]["api"]["message"],
            "TrueNAS API certificate verification failed (certificate expired).",
        )
        self.assertNotIn(PRIVATE_MARKER, serialized)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(payload["warnings"]))

    async def test_quantastor_payload_body_and_reason_are_not_published(self) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(
                host="http://source.example.test",
                platform="quantastor",
                api_user="synthetic-user",
                api_password="synthetic-password",
            )
        )
        errors: list[tuple[TrueNASAPIError, str]] = []

        with patch.object(client, "_request_json", return_value={"error": {"detail": PRIVATE_MARKER}}):
            with self.assertRaises(TrueNASAPIError) as raised:
                client._fetch_required_list("storageSystemEnum")
            errors.append((raised.exception, "Quantastor API reported a collection error."))

        headers = Message()
        http_error = HTTPError(
            "http://source.example.test/qstorapi/storageSystemEnum",
            503,
            "synthetic HTTP failure",
            headers,
            io.BytesIO(PRIVATE_MARKER.encode()),
        )
        with patch("app.services.quantastor_api.urlopen_with_tls_config", side_effect=http_error):
            with self.assertRaises(TrueNASAPIError) as raised:
                client._request_json("storageSystemEnum")
            errors.append((raised.exception, "Quantastor API reported a collection error (code 503)."))

        with patch(
            "app.services.quantastor_api.urlopen_with_tls_config",
            side_effect=URLError(PRIVATE_MARKER),
        ):
            with self.assertRaises(TrueNASAPIError) as raised:
                client._request_json("storageSystemEnum")
            errors.append((raised.exception, "Quantastor API could not be reached."))

        for error, expected_message in errors:
            with self.subTest(error=type(error.__cause__ or error).__name__):
                self.assertNotIn(PRIVATE_MARKER, str(error))
                payload, serialized = await self._serialized_bundle(
                    platform="quantastor",
                    api_error=error,
                )
                self.assertEqual(
                    payload["sources"]["api"]["message"],
                    expected_message,
                )
                self.assertNotIn(PRIVATE_MARKER, serialized)

    async def test_api_failure_categories_are_fixed_and_value_free(self) -> None:
        auth_error = self._error_with_cause(
            HTTPError(
                "https://source.example.test/api",
                403,
                PRIVATE_MARKER,
                Message(),
                io.BytesIO(PRIVATE_MARKER.encode()),
            )
        )
        method_error = build_jsonrpc_error(
            "disk.smartctl",
            {
                "code": -32601,
                "message": PRIVATE_MARKER,
                "data": {"reason": PRIVATE_MARKER},
            },
        )
        remote_error = build_ddp_error(
            "pool.query",
            {"error": 5, "data": {"trace": PRIVATE_MARKER}},
        )
        cases = (
            (auth_error, "TrueNAS API authentication failed."),
            (TimeoutError(PRIVATE_MARKER), "TrueNAS API collection timed out."),
            (ConnectionRefusedError(PRIVATE_MARKER), "TrueNAS API could not be reached."),
            (method_error, "TrueNAS API does not support a required API method."),
            (remote_error, "TrueNAS API reported a collection error (code 5)."),
        )

        for error, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                payload, serialized = await self._serialized_bundle(api_error=error)
                self.assertEqual(payload["sources"]["api"]["message"], expected_message)
                self.assertNotIn(PRIVATE_MARKER, serialized)

    async def test_api_failure_log_is_value_safe_and_diagnosable(self) -> None:
        error = build_ddp_error(
            "pool.query",
            {"reason": PRIVATE_MARKER, "data": {"trace": PRIVATE_MARKER}},
        )
        service = self._service(api_error=error)

        with self.assertLogs("app.services.inventory", level="WARNING") as logs:
            await service._collect_inventory_source_bundle()

        rendered = "\n".join(logs.output)
        self.assertIn("Platform API collection failed", rendered)
        self.assertIn("category=remote_error", rendered)
        self.assertIn("exception=TrueNASAPIError", rendered)
        self.assertNotIn(PRIVATE_MARKER, rendered)

    async def test_smart_failure_uses_fixed_reason_and_value_safe_log(self) -> None:
        service = self._service()
        slot = self._smart_slot()
        service.get_snapshot = AsyncMock(
            return_value=InventorySnapshot(
                slots=[slot],
                refresh_interval_seconds=30,
                selected_enclosure_id=slot.enclosure_id,
            )
        )
        smart_error = build_ddp_error(
            "disk.smartctl",
            {"reason": "synthetic", "data": {"trace": PRIVATE_MARKER}},
        )

        with (
            patch.object(
                service.truenas_client,
                "fetch_disk_smartctl",
                new=AsyncMock(side_effect=smart_error),
                create=True,
            ),
            self.assertLogs("app.services.inventory", level="WARNING") as logs,
        ):
            summary = await service.get_slot_smart_summary(
                slot.slot,
                selected_enclosure_id=slot.enclosure_id,
            )

        self.assertEqual(summary.message, "SMART data collection failed.")
        rendered = "\n".join(logs.output)
        self.assertIn("SMART API collection failed", rendered)
        self.assertIn("category=remote_error", rendered)
        self.assertNotIn(PRIVATE_MARKER, rendered)
        self.assertNotIn(PRIVATE_MARKER, summary.model_dump_json())

    async def test_smart_routes_replace_raised_error_detail(self) -> None:
        service = MagicMock()
        service.system = SystemConfig(
            id="synthetic-route",
            truenas=TrueNASConfig(platform="core"),
        )
        service.get_slot_smart_summary = AsyncMock(side_effect=TrueNASAPIError(PRIVATE_MARKER))
        service.get_storage_view_slot_smart_summary = AsyncMock(
            side_effect=TrueNASAPIError(PRIVATE_MARKER)
        )
        registry = MagicMock()
        registry.get_service.return_value = service
        cases = (
            (
                self._route("/api/slots/{slot}/smart", "GET"),
                {
                    "slot": 0,
                    "system_id": None,
                    "enclosure_id": None,
                    "fresh": False,
                },
            ),
            (
                self._route(
                    "/api/storage-views/{view_id}/slots/{slot_index}/smart",
                    "GET",
                ),
                {
                    "view_id": "synthetic-view",
                    "slot_index": 0,
                    "system_id": None,
                    "enclosure_id": None,
                    "fresh": False,
                },
            ),
        )

        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(
                app_routes,
                "ensure_read_slot_bounds",
                new=AsyncMock(return_value="verified"),
            ),
        ):
            for route, kwargs in cases:
                with self.subTest(path=route.__name__):
                    with (
                        self.assertLogs("app.routes", level="WARNING") as logs,
                        self.assertRaises(HTTPException) as raised,
                    ):
                        await route(**kwargs)
                    self.assertEqual(raised.exception.status_code, 400)
                    self.assertEqual(
                        raised.exception.detail,
                        "SMART data could not be loaded.",
                    )
                    self.assertNotIn(PRIVATE_MARKER, "\n".join(logs.output))

    async def test_locator_failure_uses_fixed_reason_and_value_safe_log(self) -> None:
        service = self._service(bmc_error=TrueNASAPIError(PRIVATE_MARKER))
        assert service.bmc_service is not None

        with (
            patch.object(
                service.bmc_service,
                "get_uid_status",
                side_effect=TrueNASAPIError(PRIVATE_MARKER),
            ),
            self.assertLogs("app.services.inventory", level="WARNING") as logs,
        ):
            status = await service.get_system_locator_status()

        self.assertEqual(status.reason, "BMC locator status unavailable.")
        rendered = "\n".join(logs.output)
        self.assertIn("BMC locator status read failed", rendered)
        self.assertIn("category=remote_error", rendered)
        self.assertNotIn(PRIVATE_MARKER, rendered)
        self.assertNotIn(PRIVATE_MARKER, status.model_dump_json())

    async def test_sibling_source_failures_use_fixed_public_reasons(self) -> None:
        payload, serialized = await self._serialized_bundle(ssh_error=RuntimeError(PRIVATE_MARKER))
        self.assertEqual(
            payload["sources"]["ssh"]["message"],
            "SSH diagnostics collection failed.",
        )
        self.assertNotIn(PRIVATE_MARKER, serialized)

        payload, serialized = await self._serialized_bundle(bmc_error=RuntimeError(PRIVATE_MARKER))
        self.assertEqual(
            payload["sources"]["bmc"]["message"],
            "BMC / IPMI diagnostics collection failed.",
        )
        self.assertNotIn(PRIVATE_MARKER, serialized)


class BMCWarningErrorTextTests(unittest.TestCase):
    def _service(self) -> SupermicroBMCService:
        return SupermicroBMCService(
            BMCConfig(
                enabled=True,
                host="bmc.example.test",
                username="synthetic-user",
                password="synthetic-password",
            )
        )

    def test_collector_warnings_use_fixed_reasons(self) -> None:
        service = self._service()
        with (
            patch.object(
                service,
                "_populate_redfish_inventory",
                side_effect=TrueNASAPIError(PRIVATE_MARKER),
            ),
            patch.object(
                service,
                "_populate_web_inventory",
                side_effect=TrueNASAPIError(PRIVATE_MARKER),
            ),
            patch.object(service, "_populate_redfish_simple_storage_fallback"),
            self.assertLogs("app.services.supermicro_bmc", level="WARNING") as logs,
        ):
            inventory = service.fetch_inventory()

        self.assertEqual(
            inventory.warnings,
            ["BMC Redfish inventory unavailable.", "BMC web inventory unavailable."],
        )
        rendered = "\n".join(logs.output)
        self.assertIn("BMC Redfish inventory failed", rendered)
        self.assertIn("BMC web inventory failed", rendered)
        self.assertEqual(rendered.count("category=remote_error"), 2)
        self.assertNotIn(PRIVATE_MARKER, rendered)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(inventory.warnings))

    def test_optional_redfish_storage_detail_is_value_free(self) -> None:
        service = self._service()
        inventory = BMCInventory()
        with (
            patch.object(
                service,
                "_redfish_get_json",
                side_effect=[{}, TrueNASAPIError(PRIVATE_MARKER)],
            ),
            self.assertLogs("app.services.supermicro_bmc", level="WARNING") as logs,
        ):
            service._populate_redfish_inventory(inventory)

        self.assertEqual(
            inventory.source_details["redfish_storage_error"],
            "BMC Redfish storage inventory unavailable.",
        )
        rendered = "\n".join(logs.output)
        self.assertIn("BMC Redfish storage inventory failed", rendered)
        self.assertIn("category=remote_error", rendered)
        self.assertNotIn(PRIVATE_MARKER, rendered)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(inventory.source_details))


if __name__ == "__main__":
    unittest.main()
