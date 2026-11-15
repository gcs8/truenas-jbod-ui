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

from app.config import BMCConfig, SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.quantastor_api import QuantastorRESTClient
from app.services.slot_detail_store import SlotDetailStore
from app.services.supermicro_bmc import BMCInventory, SupermicroBMCService
from app.services.truenas_ws import TrueNASAPIError, TrueNASRawData, build_ddp_error


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
        with patch("app.services.inventory.logger.exception"):
            bundle = await service._collect_inventory_source_bundle()
        snapshot = InventorySnapshot(
            slots=[],
            refresh_interval_seconds=30,
            sources=bundle.sources,
            warnings=bundle.warnings,
        )
        payload = snapshot.model_dump(mode="json")
        return payload, json.dumps(payload, sort_keys=True)

    async def test_success_control_keeps_the_named_reachable_status(self) -> None:
        payload, serialized = await self._serialized_bundle()

        self.assertEqual(payload["sources"]["api"]["message"], "TrueNAS API reachable.")
        self.assertNotIn(PRIVATE_MARKER, serialized)

    async def test_ddp_details_are_replaced_by_fixed_public_reasons(self) -> None:
        error = build_ddp_error(
            "pool.query",
            {"reason": "synthetic failure", "data": {"trace": PRIVATE_MARKER}},
        )
        payload, serialized = await self._serialized_bundle(api_error=error)

        self.assertEqual(payload["sources"]["api"]["message"], "TrueNAS API collection failed.")
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
        error.verify_message = PRIVATE_MARKER
        payload, serialized = await self._serialized_bundle(api_error=error)

        self.assertEqual(
            payload["sources"]["api"]["message"],
            "TrueNAS API certificate verification failed.",
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
        errors: list[TrueNASAPIError] = []

        with patch.object(client, "_request_json", return_value={"error": {"detail": PRIVATE_MARKER}}):
            with self.assertRaises(TrueNASAPIError) as raised:
                client._fetch_required_list("storageSystemEnum")
            errors.append(raised.exception)

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
            errors.append(raised.exception)

        with patch(
            "app.services.quantastor_api.urlopen_with_tls_config",
            side_effect=URLError(PRIVATE_MARKER),
        ):
            with self.assertRaises(TrueNASAPIError) as raised:
                client._request_json("storageSystemEnum")
            errors.append(raised.exception)

        for error in errors:
            with self.subTest(error=type(error.__cause__ or error).__name__):
                payload, serialized = await self._serialized_bundle(
                    platform="quantastor",
                    api_error=error,
                )
                self.assertEqual(
                    payload["sources"]["api"]["message"],
                    "Quantastor API collection failed.",
                )
                self.assertNotIn(PRIVATE_MARKER, serialized)

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
        ):
            inventory = service.fetch_inventory()

        self.assertEqual(
            inventory.warnings,
            ["BMC Redfish inventory unavailable.", "BMC web inventory unavailable."],
        )
        self.assertNotIn(PRIVATE_MARKER, json.dumps(inventory.warnings))

    def test_optional_redfish_storage_detail_is_value_free(self) -> None:
        service = self._service()
        inventory = BMCInventory()
        with patch.object(
            service,
            "_redfish_get_json",
            side_effect=[{}, TrueNASAPIError(PRIVATE_MARKER)],
        ):
            service._populate_redfish_inventory(inventory)

        self.assertEqual(
            inventory.source_details["redfish_storage_error"],
            "BMC Redfish storage inventory unavailable.",
        )
        self.assertNotIn(PRIVATE_MARKER, json.dumps(inventory.source_details))


if __name__ == "__main__":
    unittest.main()
