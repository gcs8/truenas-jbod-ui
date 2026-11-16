"""Remote collector failures must publish fixed source-status reasons only."""

from __future__ import annotations

import asyncio
import errno
import inspect
import io
import json
import logging
import re
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from app import logging_config
from app import main as app_main
from app import route_support
from app import routes as app_routes
from app.config import BMCConfig, SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import (
    EnclosureOption,
    InventorySnapshot,
    LedAction,
    LedRequest,
    ManualMapping,
    MappingBundle,
    MappingRequest,
    SlotView,
    SmartBatchRequest,
    SnapshotExportRequest,
    SourceStatus,
    StorageViewRuntimePayload,
    SystemLocatorRequest,
)
from app.services.inventory import (
    InventoryService,
    InventorySourceBundle,
    SnapshotStateBusyError,
    SupersededSnapshotRefreshError,
)
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.quantastor_api import QuantastorRESTClient
from app.services.slot_detail_store import SlotDetailStore
from app.services.snapshot_export import SnapshotExportService
from app.services.parsers import ParsedSSHData
from app.services.ssh_probe import SSHCommandResult
from app.services.supermicro_bmc import BMCInventory, SupermicroBMCService
from app.services.tls_context import TlsTrustConfigurationError
from app.services import truenas_ws as truenas_ws_module
from app.services.truenas_ws import (
    TrueNASAPIError,
    TrueNASRawData,
    TrueNASWebsocketClient,
    build_ddp_error,
    build_jsonrpc_error,
    classify_remote_failure,
    log_failure,
    normalize_disk_inventory_rows,
)
from tests.test_snapshot_export import FakeHistoryBackend, build_request
from tests.test_storage_view_smart_batch import _runtime as storage_view_runtime


PRIVATE_MARKER = "SYNTHETIC-REMOTE-DETAIL"


class _InventoryFixture(unittest.IsolatedAsyncioTestCase):
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


class InventoryStatusErrorTextTests(_InventoryFixture):
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

    async def test_quantastor_failures_use_fixed_public_reasons(self) -> None:
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
        payload, serialized = await self._serialized_bundle(ssh_error=ConnectionResetError(PRIVATE_MARKER))
        self.assertEqual(
            payload["sources"]["ssh"]["message"],
            "SSH diagnostics could not be reached.",
        )
        self.assertNotIn(PRIVATE_MARKER, serialized)

        payload, serialized = await self._serialized_bundle(bmc_error=TimeoutError(PRIVATE_MARKER))
        self.assertEqual(
            payload["sources"]["bmc"]["message"],
            "BMC / IPMI diagnostics collection timed out.",
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


class _RecordedLogs(logging.Handler):
    """Every record any logger emits while active, at every level."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def __enter__(self) -> "_RecordedLogs":
        root = logging.getLogger()
        self._previous_level = root.level
        root.addHandler(self)
        root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *_exc_info: object) -> None:
        root = logging.getLogger()
        root.removeHandler(self)
        root.setLevel(self._previous_level)

    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)

    def matching(self, fragment: str) -> list[logging.LogRecord]:
        return [record for record in self.records if fragment in record.getMessage()]


def _raised_by(call) -> BaseException:
    try:
        call()
    except BaseException as caught:  # noqa: BLE001 - the test inspects what the producer raised.
        return caught
    raise AssertionError("the producer did not raise")


async def _raised_by_async(awaitable) -> BaseException:
    try:
        await awaitable
    except BaseException as caught:  # noqa: BLE001 - the test inspects what the producer raised.
        return caught
    raise AssertionError("the producer did not raise")


def _jsonrpc_error_with_remote_reason(method: str = "pool.query") -> TrueNASAPIError:
    return build_jsonrpc_error(
        method,
        {
            "code": -32001,
            "message": PRIVATE_MARKER,
            "data": {"errname": "EINVAL", "reason": PRIVATE_MARKER, "trace": PRIVATE_MARKER},
        },
    )


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def read(self, *_args: object) -> bytes:
        return self._body


class _FakeOpener:
    def __init__(self, *, error: BaseException | None = None, body: bytes = b"") -> None:
        self.error = error
        self.body = body

    def open(self, *_args: object, **_kwargs: object) -> _FakeResponse:
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.body)


class FailureOriginTests(_InventoryFixture):
    """Origin decides the text: service sentences pass, remote text never does."""

    async def _bundle_and_logs(self, **service_kwargs) -> tuple[dict, str, _RecordedLogs]:
        service = self._service(**service_kwargs)
        with _RecordedLogs() as logs:
            bundle = await service._collect_inventory_source_bundle()
        snapshot = InventorySnapshot(
            slots=[],
            refresh_interval_seconds=30,
            sources=bundle.sources,
            warnings=bundle.warnings,
        )
        payload = snapshot.model_dump(mode="json")
        return payload, json.dumps(payload, sort_keys=True), logs

    def _smart_service(self, *, platform: str, ssh_enabled: bool) -> InventoryService:
        service = self._service(platform=platform)
        service.system = service.system.model_copy(
            update={"ssh": SSHConfig(enabled=ssh_enabled, host="source.example.test", user="svcuser")}
        )
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            enclosure_id="synthetic-enclosure",
            present=True,
            device_name="sda",
            serial="SANITIZED-SMART",
            transport_protocol="SAS",
        )
        service.get_snapshot = AsyncMock(
            return_value=InventorySnapshot(
                slots=[slot],
                refresh_interval_seconds=30,
                selected_enclosure_id=slot.enclosure_id,
            )
        )
        return service

    @staticmethod
    def _ssh_smart_result(stderr: str, exit_code: int | None):
        async def run(_planner, *, initial_commands, host=None):
            return [
                SSHCommandResult(
                    command=initial_commands[0],
                    ok=False,
                    stdout="",
                    stderr=stderr,
                    exit_code=exit_code,
                )
            ]

        return AsyncMock(side_effect=run)

    async def test_unexpected_local_exception_is_its_own_category_with_frames(self) -> None:
        cases = (
            ("api", {"api_error": KeyError(PRIVATE_MARKER)}, "TrueNAS API", "KeyError"),
            ("ssh", {"ssh_error": ValueError(PRIVATE_MARKER)}, "SSH diagnostics", "ValueError"),
            ("bmc", {"bmc_error": AttributeError(PRIVATE_MARKER)}, "BMC / IPMI diagnostics", "AttributeError"),
        )
        for source, kwargs, label, type_name in cases:
            with self.subTest(source=source):
                payload, serialized, logs = await self._bundle_and_logs(**kwargs)
                self.assertEqual(
                    payload["sources"][source]["message"],
                    f"{label} collection stopped on an internal error; see the service log.",
                )
                internal = logs.matching("category=internal_error")
                self.assertEqual(len(internal), 1, logs.text())
                record = internal[0]
                self.assertEqual(record.levelno, logging.ERROR)
                self.assertEqual(record.name, "app.services.inventory")
                self.assertIn(f"exception={type_name}", record.getMessage())
                # Stack frames, so the bug can be found; no exception message.
                self.assertIn("inventory.py", record.getMessage())
                self.assertFalse(record.exc_info)
                self.assertNotIn(PRIVATE_MARKER, logs.text())
                self.assertNotIn(PRIVATE_MARKER, serialized)

    async def test_collector_failures_stay_at_error(self) -> None:
        cases = (
            ("api", {"api_error": ConnectionResetError(PRIVATE_MARKER)}, "Platform API collection failed"),
            ("ssh", {"ssh_error": ConnectionResetError(PRIVATE_MARKER)}, "SSH diagnostics collection failed"),
            ("bmc", {"bmc_error": ConnectionResetError(PRIVATE_MARKER)}, "BMC diagnostics collection failed"),
        )
        for source, kwargs, operation in cases:
            with self.subTest(source=source):
                _payload, _serialized, logs = await self._bundle_and_logs(**kwargs)
                records = logs.matching(operation)
                self.assertEqual(len(records), 1, logs.text())
                self.assertEqual(records[0].levelno, logging.ERROR)
                self.assertEqual(
                    records[0].getMessage(),
                    f"{operation} (category=unreachable, exception=ConnectionResetError)",
                )

    def test_category_is_decided_by_type_and_errno_not_by_text(self) -> None:
        wrapped = self._error_with_cause(KeyError(PRIVATE_MARKER))
        cases = (
            (KeyError(PRIVATE_MARKER), "internal_error"),
            (TypeError(PRIVATE_MARKER), "internal_error"),
            (PermissionError(errno.EACCES, PRIVATE_MARKER), "internal_error"),
            (ConnectionRefusedError(errno.ECONNREFUSED, PRIVATE_MARKER), "unreachable"),
            (OSError(errno.ECONNRESET, PRIVATE_MARKER), "unreachable"),
            (json.JSONDecodeError(PRIVATE_MARKER, "{", 0), "response_invalid"),
            (ssl.SSLError(1, PRIVATE_MARKER), "tls_failed"),
            # An adapter wrapper stays a remote failure whatever it wraps.
            (wrapped, "remote_error"),
            # Words in a message never change the category.
            (KeyError("connection refused timed out ENOMETHOD"), "internal_error"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                self.assertEqual(classify_remote_failure(error)[0], expected)

    async def test_service_sentences_reach_source_status_unchanged(self) -> None:
        row_limit = _raised_by(lambda: normalize_disk_inventory_rows([{}] * 4097))
        bundle_refusal = TlsTrustConfigurationError(
            "config/ca-bundle.pem",
            FileNotFoundError(errno.ENOENT, "No such file or directory"),
        )
        for error in (row_limit, bundle_refusal):
            with self.subTest(error=type(error).__name__):
                payload, _serialized, logs = await self._bundle_and_logs(api_error=error)
                self.assertEqual(payload["sources"]["api"]["message"], str(error))
                self.assertTrue(logs.matching("category=local"), logs.text())

    async def test_tls_and_websocket_status_failures_get_their_own_categories(self) -> None:
        cases = (
            (ssl.SSLError(1, PRIVATE_MARKER), "TrueNAS API TLS connection failed."),
            (
                InvalidStatus(Response(401, PRIVATE_MARKER, Headers(), PRIVATE_MARKER.encode())),
                "TrueNAS API authentication failed.",
            ),
            (
                InvalidStatus(Response(503, PRIVATE_MARKER, Headers(), PRIVATE_MARKER.encode())),
                "TrueNAS API reported a collection error (code 503).",
            ),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                payload, serialized, logs = await self._bundle_and_logs(api_error=error)
                self.assertEqual(payload["sources"]["api"]["message"], expected)
                self.assertNotIn(PRIVATE_MARKER, serialized)
                self.assertNotIn(PRIVATE_MARKER, logs.text())

    async def test_missing_intermediate_certificate_codes_are_labelled(self) -> None:
        for verify_code, label in ((2, "issuer certificate not available"), (21, "certificate chain incomplete")):
            with self.subTest(verify_code=verify_code):
                error = ssl.SSLCertVerificationError(1, PRIVATE_MARKER)
                error.verify_code = verify_code
                error.verify_message = PRIVATE_MARKER
                payload, serialized, _logs = await self._bundle_and_logs(api_error=error)
                self.assertEqual(
                    payload["sources"]["api"]["message"],
                    f"TrueNAS API certificate verification failed ({label}).",
                )
                self.assertNotIn(PRIVATE_MARKER, serialized)

    async def test_api_failure_log_names_the_method_and_error_name(self) -> None:
        error = build_ddp_error(
            "disk.query",
            {"error": 13, "errname": "EACCES", "reason": PRIVATE_MARKER, "trace": {"frames": [PRIVATE_MARKER]}},
        )
        _payload, serialized, logs = await self._bundle_and_logs(api_error=error)

        records = logs.matching("Platform API collection failed")
        self.assertEqual(len(records), 1, logs.text())
        self.assertEqual(records[0].levelno, logging.ERROR)
        self.assertEqual(
            records[0].getMessage(),
            "Platform API collection failed (category=remote_error, code=13, method=disk.query, "
            "errname=EACCES, exception=TrueNASAPIError)",
        )
        self.assertNotIn(PRIVATE_MARKER, logs.text())
        self.assertNotIn(PRIVATE_MARKER, serialized)

    def test_older_dialect_keeps_only_a_well_formed_error_name(self) -> None:
        error = build_ddp_error(
            "disk.query",
            {"error": 13, "errname": "EACCES", "reason": PRIVATE_MARKER, "trace": PRIVATE_MARKER},
        )
        self.assertEqual(str(error), "disk.query failed (remote error, EACCES, code 13).")
        self.assertEqual(getattr(error, "errname", None), "EACCES")
        self.assertEqual(getattr(error, "method", None), "disk.query")
        for errname in ("eacces", "E" * 33, f"EACCES {PRIVATE_MARKER}", "", None, 13):
            with self.subTest(errname=errname):
                error = build_ddp_error("disk.query", {"error": 13, "errname": errname, "reason": PRIVATE_MARKER})
                self.assertIsNone(getattr(error, "errname", None))
                self.assertEqual(str(error), "disk.query failed (remote error, code 13).")

    def test_jsonrpc_reason_is_kept_apart_bounded_and_clean(self) -> None:
        error = build_jsonrpc_error(
            "enclosure.set_slot_status",
            {
                "code": -32001,
                "message": "Method call error",
                "data": {"errname": "EINVAL", "reason": "Slot 3\x1b[31m is\nnot populated" + "x" * 400},
            },
        )
        self.assertEqual(str(error), "enclosure.set_slot_status failed (remote error, EINVAL, code -32001).")
        reason = error.reason or ""
        self.assertTrue(reason.startswith("Slot 3 [31m is not populated"))
        self.assertLessEqual(len(reason), 300)
        self.assertFalse(any(not character.isprintable() for character in reason))

    async def test_ssh_smart_turned_off_shows_the_service_sentence(self) -> None:
        service = self._smart_service(platform="scale", ssh_enabled=False)
        with _RecordedLogs() as logs:
            summary = await service.get_slot_smart_summary(0, selected_enclosure_id="synthetic-enclosure")

        self.assertEqual(summary.message, "SMART detail needs SSH on this system, and SSH is turned off.")
        self.assertTrue(logs.matching("SMART SSH collection failed (kind=ssh_disabled, exit_status=none)"), logs.text())

    async def test_ssh_smart_guidance_is_shown_and_remote_output_is_not(self) -> None:
        guidance_suffix = "Re-run the SSH access setup in the admin console to fix the sudo permissions."
        cases = (
            (
                f"Sorry, user svcuser is not allowed to execute '/usr/sbin/smartctl -x -j /dev/sda' "
                f"as root on {PRIVATE_MARKER}.",
                1,
                f"/dev/sda: The SSH user svcuser is not allowed to run smartctl on /dev/sda. {guidance_suffix}",
                "kind=sudo_denied, exit_status=1",
            ),
            (
                f"sudo: a password is required {PRIVATE_MARKER}",
                1,
                f"/dev/sda: smartctl needs a sudo password on this host. {guidance_suffix}",
                "kind=sudo_password, exit_status=1",
            ),
            (
                PRIVATE_MARKER,
                2,
                "SMART data collection over SSH failed.",
                "kind=command_failed, exit_status=2",
            ),
        )
        for stderr, exit_code, expected, log_fields in cases:
            with self.subTest(log_fields=log_fields):
                service = self._smart_service(platform="scale", ssh_enabled=True)
                service._run_ssh_planned_commands = self._ssh_smart_result(stderr, exit_code)
                with _RecordedLogs() as logs:
                    summary = await service.get_slot_smart_summary(0, selected_enclosure_id="synthetic-enclosure")
                self.assertEqual(summary.message, expected)
                self.assertTrue(logs.matching(f"SMART SSH collection failed ({log_fields})"), logs.text())
                self.assertNotIn(PRIVATE_MARKER, summary.model_dump_json())
                self.assertNotIn(PRIVATE_MARKER, logs.text())

    async def test_core_ssh_fallback_after_an_api_failure_uses_a_fixed_reason(self) -> None:
        service = self._smart_service(platform="core", ssh_enabled=True)
        service._run_ssh_planned_commands = self._ssh_smart_result(PRIVATE_MARKER, 2)
        api_error = build_ddp_error("disk.smartctl", {"error": 5, "reason": PRIVATE_MARKER})
        with (
            patch.object(
                service.truenas_client,
                "fetch_disk_smartctl",
                new=AsyncMock(side_effect=api_error),
                create=True,
            ),
            _RecordedLogs() as logs,
        ):
            summary = await service.get_slot_smart_summary(0, selected_enclosure_id="synthetic-enclosure")

        self.assertEqual(summary.message, "SMART data collection over SSH failed.")
        self.assertTrue(
            logs.matching(
                "SMART API collection failed (category=remote_error, code=5, method=disk.smartctl, "
                "exception=TrueNASAPIError)"
            ),
            logs.text(),
        )
        self.assertTrue(logs.matching("SMART SSH collection failed (kind=command_failed, exit_status=2)"), logs.text())
        self.assertNotIn(PRIVATE_MARKER, summary.model_dump_json())
        self.assertNotIn(PRIVATE_MARKER, logs.text())

    async def test_smart_timeout_keeps_the_service_sentence(self) -> None:
        service = self._smart_service(platform="core", ssh_enabled=False)

        async def never_answers(*_args, **_kwargs):
            await asyncio.sleep(5)

        with (
            patch.object(service, "_smart_call_timeout_seconds", return_value=0.01),
            patch.object(
                service.truenas_client,
                "fetch_disk_smartctl",
                new=AsyncMock(side_effect=never_answers),
                create=True,
            ),
            _RecordedLogs(),
        ):
            summary = await service.get_slot_smart_summary(0, selected_enclosure_id="synthetic-enclosure")

        self.assertEqual(summary.message, "SMART lookup for sda timed out after 0.01s; the disk did not answer.")

    async def test_smart_batch_fallback_uses_origin(self) -> None:
        service = self._smart_service(platform="core", ssh_enabled=False)
        local_error = await _raised_by_async(service.get_slot_smart_summary(7))
        remote_error = build_ddp_error("disk.smartctl", {"error": 5, "reason": PRIVATE_MARKER})
        for error, expected in (
            (remote_error, "SMART data collection failed."),
            (local_error, "Slot 07 is not present in the current snapshot."),
        ):
            with self.subTest(expected=expected):
                service._get_slot_smart_summary_for_slot_view = AsyncMock(side_effect=error)
                with _RecordedLogs() as logs:
                    items = await service.get_slot_smart_summaries([0])
                self.assertEqual([item.summary.message for item in items], [expected])
                self.assertNotIn(PRIVATE_MARKER, items[0].summary.model_dump_json())
                self.assertNotIn(PRIVATE_MARKER, logs.text())

    async def test_smart_routes_show_service_sentences_with_status_400(self) -> None:
        service = self._smart_service(platform="core", ssh_enabled=False)
        service.get_storage_view_runtime = AsyncMock(return_value=StorageViewRuntimePayload(views=[]))
        registry = MagicMock()
        registry.get_service.return_value = service
        cases = (
            (
                self._route("/api/slots/{slot}/smart", "GET"),
                {"slot": 7, "system_id": None, "enclosure_id": None, "fresh": False},
                "Slot 07 is not present in the current snapshot.",
            ),
            (
                self._route("/api/storage-views/{view_id}/slots/{slot_index}/smart", "GET"),
                {"view_id": "missing", "slot_index": 0, "system_id": None, "enclosure_id": None, "fresh": False},
                'The saved view "missing" does not exist on this system.',
            ),
            (
                self._route("/api/storage-views/{view_id}/slots/smart-batch", "POST"),
                {
                    "view_id": "missing",
                    "payload": SmartBatchRequest(slots=[0]),
                    "system_id": None,
                    "enclosure_id": None,
                    "fresh": False,
                },
                'The saved view "missing" does not exist on this system.',
            ),
        )
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "ensure_read_slot_bounds", new=AsyncMock(return_value="verified")),
        ):
            for route, kwargs, expected in cases:
                with self.subTest(route=route.__name__):
                    with _RecordedLogs() as logs, self.assertRaises(HTTPException) as raised:
                        await route(**kwargs)
                    self.assertEqual(raised.exception.status_code, 400)
                    self.assertEqual(raised.exception.detail, expected)
                    self.assertFalse(logs.matching("SMART request failed"), logs.text())

    async def test_read_sinks_never_carry_the_remote_reason(self) -> None:
        error = _jsonrpc_error_with_remote_reason()
        self.assertEqual(error.reason, PRIVATE_MARKER)
        payload, serialized, logs = await self._bundle_and_logs(api_error=error)
        self.assertEqual(
            payload["sources"]["api"]["message"],
            "TrueNAS API reported a collection error (code -32001).",
        )
        self.assertNotIn(PRIVATE_MARKER, serialized)
        self.assertNotIn(PRIVATE_MARKER, logs.text())

        service = MagicMock()
        service.system = SystemConfig(id="synthetic-route", truenas=TrueNASConfig(platform="scale"))
        service.get_slot_smart_summary = AsyncMock(side_effect=error)
        service.get_storage_view_runtime = AsyncMock(side_effect=error)
        service.preview_mapping_bundle = AsyncMock(side_effect=error)
        registry = MagicMock()
        registry.get_service.return_value = service
        cases = (
            (
                self._route("/api/slots/{slot}/smart", "GET"),
                {"slot": 0, "system_id": None, "enclosure_id": None, "fresh": False},
                "SMART data could not be loaded.",
            ),
            (
                self._route("/api/storage-views/{view_id}/history", "GET"),
                {
                    "view_id": "synthetic-view",
                    "system_id": None,
                    "enclosure_id": None,
                    "window_hours": 24,
                    "metrics": None,
                    "event_limit": 12,
                    "metric_limit": 60,
                },
                "Storage view data could not be loaded.",
            ),
            (
                self._route("/api/mappings/import/preview", "POST"),
                {"payload": MappingBundle(mappings=[]), "system_id": None, "enclosure_id": None},
                "The mapping import preview could not be prepared.",
            ),
        )
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "ensure_read_slot_bounds", new=AsyncMock(return_value="verified")),
        ):
            for route, kwargs, expected in cases:
                with self.subTest(route=route.__name__):
                    with _RecordedLogs() as logs, self.assertRaises(HTTPException) as raised:
                        await route(**kwargs)
                    self.assertEqual(raised.exception.status_code, 400)
                    self.assertEqual(raised.exception.detail, expected)
                    self.assertTrue(logs.matching("category=remote_error"), logs.text())
                    self.assertNotIn(PRIVATE_MARKER, logs.text())

        export_service = MagicMock()
        export_service.get_storage_view_runtime = AsyncMock(return_value=storage_view_runtime(2))
        export_service.get_storage_view_slot_smart_summary = AsyncMock(side_effect=error)
        with _RecordedLogs() as logs:
            _runtime, cache = await route_support._load_storage_view_export_source(
                service=export_service,
                payload=SnapshotExportRequest(include_storage_views=True),
                snapshot=InventorySnapshot(slots=[], refresh_interval_seconds=30),
                enclosure_id=None,
            )
        self.assertEqual(
            {entry["message"] for view in cache.values() for entry in view.values()},
            {"SMART data collection failed."},
        )
        self.assertNotIn(PRIVATE_MARKER, json.dumps(cache))
        self.assertNotIn(PRIVATE_MARKER, logs.text())

    async def test_export_storage_view_cache_keeps_service_sentences(self) -> None:
        local_service = self._smart_service(platform="core", ssh_enabled=False)
        local_service.get_storage_view_runtime = AsyncMock(return_value=StorageViewRuntimePayload(views=[]))
        local_error = await _raised_by_async(local_service.get_storage_view_slot_smart_summary("missing", 0))
        export_service = MagicMock()
        export_service.get_storage_view_runtime = AsyncMock(return_value=storage_view_runtime(1))
        export_service.get_storage_view_slot_smart_summary = AsyncMock(side_effect=local_error)

        _runtime, cache = await route_support._load_storage_view_export_source(
            service=export_service,
            payload=SnapshotExportRequest(include_storage_views=True),
            snapshot=InventorySnapshot(slots=[], refresh_interval_seconds=30),
            enclosure_id=None,
        )

        self.assertEqual(
            [entry["message"] for view in cache.values() for entry in view.values()],
            ['The saved view "missing" does not exist on this system.'],
        )

    async def test_mapping_preview_keeps_the_service_refusal(self) -> None:
        service = self._service()
        registry = MagicMock()
        registry.get_service.return_value = service
        route = self._route("/api/mappings/import/preview", "POST")
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            self.assertRaises(HTTPException) as raised,
        ):
            await route(
                payload=MappingBundle(mappings=[]),
                system_id=None,
                enclosure_id="virtual-system:synthetic-status",
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail,
            "Bay mapping is not available because this system has no known enclosure.",
        )

    async def test_healthz_and_export_carry_fixed_status_only(self) -> None:
        payload, _serialized, _logs = await self._bundle_and_logs(api_error=_jsonrpc_error_with_remote_reason())
        snapshot = InventorySnapshot(
            slots=[],
            refresh_interval_seconds=30,
            sources={name: SourceStatus(**value) for name, value in payload["sources"].items()},
            warnings=payload["warnings"],
        )
        service = MagicMock()
        service.peek_cached_snapshot.return_value = snapshot
        registry = MagicMock()
        registry.get_service.return_value = service
        registry.settings.systems = []
        healthz = next(route for route in app_main.app.routes if getattr(route, "path", None) == "/healthz")
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(startup_problems=())))
        with patch("app.routes.get_inventory_registry", return_value=registry):
            response = await healthz.endpoint(request)
        body = json.loads(response.body)
        self.assertIn("TrueNAS API reported a collection error (code -32001).", body["summary"])
        self.assertNotIn(PRIVATE_MARKER, response.body.decode())

        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), route_support.templates)
        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache={},
            selected_slot=None,
            history_window_hours=24,
            io_chart_mode="total",
        )
        self.assertIn("TrueNAS API reported a collection error (code -32001).", rendered.html)
        self.assertNotIn(PRIVATE_MARKER, rendered.html)

    async def test_locator_status_uses_origin(self) -> None:
        service = self._service(bmc_error=TrueNASAPIError(PRIVATE_MARKER))
        real_bmc = SupermicroBMCService(
            BMCConfig(enabled=True, host="bmc.example.test", username="synthetic-user", password="synthetic-password")
        )
        with patch.object(real_bmc, "_build_https_opener", return_value=_FakeOpener(body=b"<html></html>")):
            local_error = _raised_by(real_bmc.get_uid_status)
        cases = (
            (local_error, "Supermicro BMC web login did not establish a session cookie.", "category=authentication_failed"),
            (
                KeyError(PRIVATE_MARKER),
                "BMC locator status read stopped on an internal error; see the service log.",
                "category=internal_error",
            ),
        )
        assert service.bmc_service is not None
        for error, expected, log_fragment in cases:
            with self.subTest(expected=expected):
                with (
                    patch.object(service.bmc_service, "get_uid_status", side_effect=error),
                    _RecordedLogs() as logs,
                ):
                    status = await service.get_system_locator_status()
                self.assertEqual(status.reason, expected)
                self.assertTrue(logs.matching(log_fragment), logs.text())
                self.assertNotIn(PRIVATE_MARKER, logs.text())

    async def test_authorized_actions_add_the_remote_reason(self) -> None:
        led_error = build_jsonrpc_error(
            "enclosure.set_slot_status",
            {
                "code": -32001,
                "message": "Method call error",
                "data": {"errname": "EINVAL", "reason": "Slot 3 is\nnot populated", "trace": PRIVATE_MARKER},
            },
        )
        service = MagicMock()
        service.system = SystemConfig(id="synthetic-route", truenas=TrueNASConfig(platform="scale"))
        service.set_slot_led = AsyncMock(side_effect=led_error)
        registry = MagicMock()
        registry.get_service.return_value = service
        led_route = self._route("/api/slots/{slot}/led", "POST")
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "add_perf_metadata"),
            _RecordedLogs() as logs,
            self.assertRaises(HTTPException) as raised,
        ):
            await led_route(
                slot=3,
                payload=LedRequest(action=LedAction.identify),
                system_id=None,
                enclosure_id=None,
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail,
            "enclosure.set_slot_status failed (remote error, EINVAL, code -32001): Slot 3 is not populated",
        )
        # The route's own log line keeps the value-safe message only.
        self.assertTrue(logs.matching("Locate light refusal"), logs.text())
        self.assertNotIn("not populated", logs.text())
        self.assertNotIn(PRIVATE_MARKER, logs.text() + raised.exception.detail)

        bmc = SupermicroBMCService(
            BMCConfig(enabled=True, host="bmc.example.test", username="synthetic-user", password="synthetic-password")
        )
        refusal = HTTPError(
            f"https://bmc.example.test/cgi/op.cgi?{PRIVATE_MARKER}",
            500,
            "synthetic BMC refusal",
            Message(),
            io.BytesIO(PRIVATE_MARKER.encode()),
        )
        bmc_error = _raised_by(
            lambda: bmc._post_web_text(
                _FakeOpener(error=refusal),
                "/cgi/op.cgi",
                {"op": "misc_uid"},
                csrf="synthetic",
                referer="https://bmc.example.test/",
            )
        )
        locator_service = self._service(bmc_error=TrueNASAPIError(PRIVATE_MARKER))
        assert locator_service.bmc_service is not None
        locator_service.bmc_service.set_uid_active.side_effect = bmc_error
        registry.get_service.return_value = locator_service
        locator_route = self._route("/api/system-locator", "POST")
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "add_perf_metadata"),
            self.assertRaises(HTTPException) as raised,
        ):
            await locator_route(payload=SystemLocatorRequest(active=True), system_id=None)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail,
            "Supermicro BMC request failed: HTTP Error 500: synthetic BMC refusal",
        )
        self.assertNotIn(PRIVATE_MARKER, raised.exception.detail)

    def test_only_authorized_routes_read_the_remote_reason(self) -> None:
        authorization = app_routes.require_read_ui_mutation_authorization
        readers: set[tuple[str, str]] = set()
        for route in app_routes.build_router().routes:
            source = inspect.getsource(route.endpoint)
            if "_authorized_action_failure_detail(" not in source:
                continue
            for method in route.methods:
                readers.add((method, route.path))
            dependencies = [dependency.dependency for dependency in route.dependencies]
            self.assertIn(authorization, dependencies, route.path)
        self.assertEqual(
            readers,
            {("POST", "/api/slots/{slot}/led"), ("POST", "/api/system-locator")},
        )
        # Nothing outside the routes module reads the helper.
        app_root = Path(app_routes.__file__).resolve().parent
        users = sorted(
            str(path.relative_to(app_root))
            for path in app_root.rglob("*.py")
            if "_authorized_action_failure_detail(" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(users, ["routes.py"])


class AdapterMessageTests(unittest.IsolatedAsyncioTestCase):
    """Each adapter writes its own message; remote text stays off it."""

    def _bmc(self) -> SupermicroBMCService:
        return SupermicroBMCService(
            BMCConfig(enabled=True, host="bmc.example.test", username="synthetic-user", password="synthetic-password")
        )

    def test_redfish_errors_keep_remote_text_off_the_message(self) -> None:
        service = self._bmc()
        body = json.dumps({"error": {"message": "synthetic redfish refusal"}}).encode()
        http_error = HTTPError(
            f"https://bmc.example.test/redfish/v1/Systems/1?{PRIVATE_MARKER}",
            500,
            PRIVATE_MARKER,
            Message(),
            io.BytesIO(body),
        )
        with patch.object(service, "_build_https_opener", return_value=_FakeOpener(error=http_error)):
            error = _raised_by(lambda: service._redfish_get_json("/redfish/v1/Systems/1"))
        self.assertEqual(str(error), "Redfish request failed with HTTP 500.")
        self.assertEqual(getattr(error, "reason", None), "synthetic redfish refusal")
        self.assertEqual(getattr(error, "method", None), "/redfish/v1/Systems/1")
        self.assertEqual(classify_remote_failure(error), ("remote_error", 500))

        with patch.object(service, "_build_https_opener", return_value=_FakeOpener(error=URLError(PRIVATE_MARKER))):
            error = _raised_by(lambda: service._redfish_get_json("/redfish/v1/Systems/1"))
        self.assertEqual(str(error), "Redfish request failed.")
        self.assertEqual(classify_remote_failure(error)[0], "unreachable")

    def test_bmc_web_errors_keep_remote_text_off_the_message(self) -> None:
        service = self._bmc()
        error = _raised_by(
            lambda: service._fetch_web_text(
                _FakeOpener(error=URLError(PRIVATE_MARKER)),
                "https://bmc.example.test/cgi/url_redirect.cgi?url_name=sys_storage",
            )
        )
        self.assertEqual(str(error), "Supermicro BMC request failed.")
        self.assertEqual(getattr(error, "method", None), "/cgi/url_redirect.cgi")
        self.assertEqual(classify_remote_failure(error)[0], "unreachable")

    def test_bmc_service_sentences_reach_warnings_with_their_category(self) -> None:
        service = self._bmc()
        with (
            patch.object(service, "_populate_redfish_inventory"),
            patch.object(service, "_populate_redfish_simple_storage_fallback"),
            patch.object(service, "_build_https_opener", return_value=_FakeOpener(body=b"<html></html>")),
            _RecordedLogs() as logs,
        ):
            inventory = service.fetch_inventory()
        self.assertEqual(inventory.warnings, ["Supermicro BMC web login did not establish a session cookie."])
        self.assertTrue(
            logs.matching("BMC web inventory failed (category=authentication_failed, method=/cgi/login.cgi"),
            logs.text(),
        )

    def test_quantastor_errors_name_the_endpoint(self) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(
                host="http://source.example.test",
                platform="quantastor",
                api_user="synthetic-user",
                api_password="synthetic-password",
            )
        )
        with patch("app.services.quantastor_api.urlopen_with_tls_config", side_effect=URLError(PRIVATE_MARKER)):
            error = _raised_by(lambda: client._request_json("storageSystemEnum"))
        self.assertEqual(getattr(error, "method", None), "storageSystemEnum")
        self.assertNotIn(PRIVATE_MARKER, str(error))

    async def test_websocket_adapter_messages_are_fixed(self) -> None:
        class ScriptedSocket:
            def __init__(self, reply: dict) -> None:
                self.reply = reply
                self.send = AsyncMock()

            async def recv(self) -> str:
                return json.dumps(self.reply)

        client = TrueNASWebsocketClient(TrueNASConfig(platform="core", api_key="synthetic-key"))
        error = await _raised_by_async(
            client._await_ddp_reply(
                ScriptedSocket({"msg": "nosub", "id": "request-1", "detail": PRIVATE_MARKER}),
                "disk.query",
                "request-1",
            )
        )
        self.assertEqual(str(error), "Unexpected response for disk.query.")
        self.assertEqual(classify_remote_failure(error)[0], "response_invalid")

        error = await _raised_by_async(
            client._perform_handshake(ScriptedSocket({"msg": "failed", "version": PRIVATE_MARKER}), None)
        )
        self.assertEqual(str(error), "Unexpected websocket handshake response.")
        self.assertEqual(classify_remote_failure(error)[0], "response_invalid")

    async def test_login_failure_and_bmc_refusal_are_authentication_failures(self) -> None:
        client = TrueNASWebsocketClient(
            TrueNASConfig(platform="scale", api_dialect="jsonrpc", api_key="synthetic-key")
        )
        with patch.object(client, "_login", new=AsyncMock(return_value=False)):
            login_error = await _raised_by_async(client._perform_handshake(MagicMock(), None))
        self.assertEqual(str(login_error), "TrueNAS API authentication failed.")
        self.assertIsNone(login_error.__cause__)
        self.assertEqual(classify_remote_failure(login_error)[0], "authentication_failed")

        bmc = self._bmc()
        with patch.object(
            bmc,
            "_build_https_opener",
            return_value=_FakeOpener(body=b"Invalid Username or Password"),
        ):
            with patch(
                "app.services.supermicro_bmc.http.cookiejar.CookieJar.__iter__",
                side_effect=lambda *_args: iter([object()]),
            ):
                refusal = _raised_by(bmc._open_web_session)
        self.assertEqual(str(refusal), "Supermicro BMC rejected the configured username or password.")
        self.assertEqual(classify_remote_failure(refusal)[0], "authentication_failed")


# Appliance-chosen identifiers in a cache key: a marker plus terminal controls.
HOSTILE_IDENTIFIER = PRIVATE_MARKER + "\x1b[31m\n"
INTERNAL_RECORD_HEADER = "; stack frames, exception message omitted:\n"
_FRAME_LINE = re.compile(r"  [A-Za-z0-9_.<>-]+:\d+ in [A-Za-z_<>][A-Za-z0-9_<>]*")


def _formatted(record: logging.LogRecord) -> str:
    """The record as both maintained service formatters write it."""

    return "\n".join(
        (
            logging_config.JsonFormatter(service_name="synthetic").format(record),
            logging_config.SafeTextFormatter(service_name="synthetic").format(record),
        )
    )


def _deep_local_fault(depth: int) -> None:
    if depth:
        _deep_local_fault(depth - 1)
        return
    raise RuntimeError(PRIVATE_MARKER)


class LogOriginTests(_InventoryFixture):
    """Log lines name fixed operations and structured facts, never remote values."""

    def assert_value_free(self, logs: _RecordedLogs) -> None:
        for record in logs.records:
            rendered = record.getMessage() + "\n" + _formatted(record)
            self.assertNotIn(PRIVATE_MARKER, rendered, rendered)
            self.assertNotIn("\x1b", rendered, rendered)

    def assert_internal_record(self, record: logging.LogRecord, operation: str, fields: str) -> list[str]:
        message = record.getMessage()
        header, _, frames = message.partition(INTERNAL_RECORD_HEADER)
        self.assertEqual(header, f"{operation} failed (category=internal_error, {fields})")
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertFalse(record.exc_info)
        lines = frames.splitlines()
        self.assertTrue(lines, message)
        for line in lines:
            self.assertRegex(line, _FRAME_LINE)
        return lines

    async def test_background_callbacks_log_a_fixed_operation_only(self) -> None:
        service = self._service()
        smart_key = ("synthetic-status", "core", HOSTILE_IDENTIFIER, 1, (HOSTILE_IDENTIFIER,), ("serial", PRIVATE_MARKER))
        remote = TrueNASAPIError("pool.query failed (remote error).", category="unreachable", reason=PRIVATE_MARKER)

        def failing(error: BaseException):
            async def fail(*_args, **_kwargs):
                raise error

            return fail

        async def run_source(error: BaseException) -> None:
            with patch.object(service, "_background_source_bundle_refresh", new=failing(error)):
                task = service._schedule_background_source_bundle_refresh()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0)

        labels: list[str] = []
        start_background_refresh = service._start_background_refresh

        def record_start(label, work, **kwargs):
            labels.append(label)
            return start_background_refresh(label, work, **kwargs)

        async def run_snapshot(error: BaseException) -> None:
            with (
                patch.object(service, "_background_snapshot_refresh", new=failing(error)),
                patch.object(service, "_start_background_refresh", new=record_start),
            ):
                service._schedule_background_snapshot_refresh(HOSTILE_IDENTIFIER)
                await asyncio.gather(service._snapshot_refresh_tasks[HOSTILE_IDENTIFIER], return_exceptions=True)
                await asyncio.sleep(0)

        async def run_smart(error: BaseException) -> None:
            with patch.object(service, "_background_refresh_smart_summary", new=failing(error)):
                service._schedule_background_smart_refresh(smart_key, self._smart_slot())
                await asyncio.gather(service._smart_refresh_tasks[smart_key], return_exceptions=True)
                await asyncio.sleep(0)

        cases = (
            ("Background inventory source refresh", run_source),
            ("Background snapshot refresh", run_snapshot),
            ("Background SMART refresh", run_smart),
        )
        for operation, run in cases:
            with self.subTest(operation=operation, origin="internal"):
                with _RecordedLogs() as logs:
                    await run(RuntimeError(PRIVATE_MARKER))
                records = logs.matching(f"{operation} failed")
                self.assertEqual(len(records), 1, logs.text())
                self.assert_internal_record(records[0], operation, "exception=RuntimeError")
                self.assert_value_free(logs)
            with self.subTest(operation=operation, origin="remote"):
                with _RecordedLogs() as logs:
                    await run(remote)
                records = logs.matching(f"{operation} failed")
                self.assertEqual(len(records), 1, logs.text())
                self.assertEqual(
                    records[0].getMessage(),
                    f"{operation} failed (category=unreachable, exception=TrueNASAPIError)",
                )
                self.assert_value_free(logs)
        # The refresh label, which the abandonment warning prints, holds no raw key.
        self.assertEqual(labels, ["snapshot (unlisted)", "snapshot (unlisted)"])

    async def test_background_refresh_catch_alls_log_fixed_operations(self) -> None:
        service = self._service()
        smart_key = ("synthetic-status", "core", HOSTILE_IDENTIFIER, 1, (HOSTILE_IDENTIFIER,), ("serial", PRIVATE_MARKER))
        with _RecordedLogs() as logs:
            with patch.object(
                service,
                "_schedule_background_source_bundle_refresh",
                side_effect=RuntimeError(PRIVATE_MARKER),
            ):
                await service._background_snapshot_refresh(HOSTILE_IDENTIFIER)
            with patch.object(
                service,
                "_get_slot_smart_summary_for_slot_view",
                new=AsyncMock(side_effect=RuntimeError(PRIVATE_MARKER)),
            ):
                await service._background_refresh_smart_summary(smart_key, self._smart_slot(), (0, 0, 0))
            with patch.object(
                service,
                "_get_inventory_source_bundle",
                new=AsyncMock(side_effect=RuntimeError(PRIVATE_MARKER)),
            ):
                self.assertIsNone(await service._background_source_bundle_refresh())
        for operation in (
            "Background snapshot refresh",
            "Background SMART refresh",
            "Background inventory source refresh",
        ):
            records = logs.matching(f"{operation} failed")
            self.assertEqual(len(records), 1, logs.text())
            self.assert_internal_record(records[0], operation, "exception=RuntimeError")
        self.assert_value_free(logs)

        # Expected local outcomes keep their own reason token and no key.
        with _RecordedLogs() as logs:
            for error in (SnapshotStateBusyError(reason="invalidated"), SupersededSnapshotRefreshError()):
                with patch.object(service, "_schedule_background_source_bundle_refresh", side_effect=error):
                    await service._background_snapshot_refresh(HOSTILE_IDENTIFIER)
            with patch.object(
                service,
                "_get_inventory_source_bundle",
                new=AsyncMock(side_effect=SnapshotStateBusyError(reason="invalidated")),
            ):
                await service._background_source_bundle_refresh()
        messages = [record.getMessage() for record in logs.records]
        self.assertIn("Background snapshot refresh failed (reason=invalidated).", messages)
        self.assertIn("Background snapshot refresh was superseded (reason=newer-topology).", messages)
        self.assertIn("Background inventory source refresh failed (reason=invalidated).", messages)
        self.assertTrue(all(not record.exc_info for record in logs.records), messages)
        self.assert_value_free(logs)

    async def test_overlay_catch_alls_log_internal_errors_without_their_message(self) -> None:
        cases = (
            ("scale", ("_fetch_scale_ses_overlay",), ("SCALE SES diagnostics collection",)),
            (
                "quantastor",
                ("_fetch_quantastor_cli_overlay", "_fetch_quantastor_ses_overlay"),
                ("Quantastor CLI diagnostics collection", "Quantastor SES diagnostics collection"),
            ),
        )
        for platform, overlays, operations in cases:
            with self.subTest(platform=platform):
                service = self._service(platform=platform)
                service.system = service.system.model_copy(
                    update={"ssh": SSHConfig(enabled=True, host="source.example.test", user="svcuser")}
                )
                service.ssh_probe.run_planned_commands = AsyncMock(return_value=[])
                with _RecordedLogs() as logs:
                    with (
                        patch.object(service, overlays[0], new=AsyncMock(side_effect=RuntimeError(PRIVATE_MARKER))),
                        patch.object(
                            service,
                            overlays[-1],
                            new=AsyncMock(side_effect=RuntimeError(PRIVATE_MARKER)),
                        ),
                    ):
                        await service._collect_inventory_source_bundle()
                for operation in operations:
                    records = logs.matching(f"{operation} failed")
                    self.assertEqual(len(records), 1, logs.text())
                    self.assert_internal_record(records[0], operation, "exception=RuntimeError")
                self.assert_value_free(logs)

    async def test_layout_guard_logs_an_internal_error_without_its_message(self) -> None:
        cases = (
            (RuntimeError(HOSTILE_IDENTIFIER), "internal"),
            (TrueNASAPIError("pool.query failed (remote error).", category="unreachable", reason=PRIVATE_MARKER), "remote"),
        )
        for error, origin in cases:
            with self.subTest(origin=origin):
                service = AsyncMock()
                service.get_snapshot.side_effect = error
                with _RecordedLogs() as logs, self.assertRaises(HTTPException) as raised:
                    await route_support.resolve_layout_slots(service, "synthetic-enclosure")
                self.assertEqual(raised.exception.status_code, 503)
                self.assertEqual(raised.exception.detail, route_support.ENCLOSURE_LAYOUT_UNAVAILABLE_DETAIL)
                records = logs.matching("Slot bounds layout read failed")
                self.assertEqual(len(records), 1, logs.text())
                if origin == "internal":
                    self.assert_internal_record(records[0], "Slot bounds layout read", "exception=RuntimeError")
                else:
                    self.assertEqual(records[0].levelno, logging.DEBUG)
                    self.assertEqual(
                        records[0].getMessage(),
                        "Slot bounds layout read failed (category=unreachable, exception=TrueNASAPIError)",
                    )
                self.assert_value_free(logs)

    async def test_trusted_fallback_warning_names_only_a_well_formed_enclosure_id(self) -> None:
        for enclosure_id, shown in (("node-a", "node-a"), (HOSTILE_IDENTIFIER, "(unlisted)")):
            with self.subTest(shown=shown):
                service = self._service(platform="quantastor")
                trusted = InventorySnapshot(
                    slots=[self._smart_slot()],
                    refresh_interval_seconds=30,
                    selected_system_id=service.system.id,
                    selected_system_platform="quantastor",
                    selected_enclosure_id=enclosure_id,
                    enclosures=[EnclosureOption(id=enclosure_id, label="Node A")],
                    platform_context={"topology_complete": True},
                )
                incomplete = InventorySnapshot(
                    slots=[],
                    refresh_interval_seconds=30,
                    selected_system_id=service.system.id,
                    selected_system_platform="quantastor",
                    selected_enclosure_id=enclosure_id,
                    platform_context={"topology_complete": False},
                )
                service._cache[enclosure_id] = trusted
                service._cache_until[enclosure_id] = datetime.now(timezone.utc) - timedelta(seconds=1)
                service._source_bundle = InventorySourceBundle(
                    raw_data=TrueNASRawData(enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[]),
                    ssh_outputs={},
                    ssh_collected=False,
                    warnings=[],
                    sources={},
                    scale_ses_data=ParsedSSHData(),
                    quantastor_ses_data=ParsedSSHData(),
                )
                service._source_bundle_until = datetime.now(timezone.utc) + timedelta(seconds=30)
                service._build_snapshot = AsyncMock(return_value=incomplete)
                with _RecordedLogs() as logs:
                    result = await service._get_snapshot_result(force_refresh=True, selected_enclosure_id=enclosure_id)
                self.assertEqual(result.cache_state, "trusted-fallback")
                self.assertIn(
                    f"Preserving previously trusted snapshot for {shown} because the refreshed topology is incomplete.",
                    [record.getMessage() for record in logs.records],
                )
                self.assert_value_free(logs)

    async def test_smart_batch_storage_error_names_only_a_well_formed_enclosure_id(self) -> None:
        route = self._route("/api/slots/smart-batch", "POST")
        for enclosure_id, shown in (("shelf-a", "shelf-a"), (HOSTILE_IDENTIFIER, "(unlisted)")):
            with self.subTest(shown=shown):
                service = MagicMock()
                service.system = SystemConfig(id="synthetic-route", truenas=TrueNASConfig(platform="core"))
                service.get_slot_smart_summaries = AsyncMock(side_effect=PermissionError(errno.EACCES, "Permission denied"))
                registry = MagicMock()
                registry.get_service.return_value = service
                with (
                    patch.object(app_routes, "get_inventory_registry", return_value=registry),
                    patch.object(app_routes, "resolve_read_layout_slots", new=AsyncMock(return_value=(None, "verified"))),
                    patch.object(app_routes, "add_perf_metadata"),
                    _RecordedLogs() as logs,
                    self.assertRaises(HTTPException) as raised,
                ):
                    await route(payload=SmartBatchRequest(slots=[0]), system_id=None, enclosure_id=enclosure_id, fresh=False)
                self.assertEqual(raised.exception.status_code, 500)
                records = logs.matching("SMART batch slot-detail cache write failed")
                self.assertEqual(len(records), 1, logs.text())
                self.assertIn(f", enclosure={shown});", records[0].getMessage())
                self.assert_value_free(logs)

    def test_log_identifier_admits_only_bounded_plain_identifiers(self) -> None:
        log_identifier = getattr(truenas_ws_module, "log_identifier", None)
        self.assertIsNotNone(log_identifier, "the one strict identifier formatter")
        for value in ("shelf-a", "__service_no_enclosure__", "5003048001c1043f", "/dev/ses0", "a" * 96):
            with self.subTest(value=value[:12]):
                self.assertEqual(log_identifier(value), value)
        for value in ("a" * 97, "", " shelf", "shelf a", "shelf\n", "shelf\x1b[31m", "sh\u00e9lf", "-shelf", None, 5):
            with self.subTest(value=repr(value)[:12]):
                self.assertEqual(log_identifier(value), "(unlisted)")

    def test_internal_error_frames_are_bounded_base_names(self) -> None:
        error = _raised_by(lambda: _deep_local_fault(40))
        target = logging.getLogger("tests.synthetic_internal")
        with _RecordedLogs() as logs:
            log_failure(target, "Synthetic read", error)
        self.assertEqual(len(logs.records), 1)
        lines = self.assert_internal_record(logs.records[0], "Synthetic read", "exception=RuntimeError")
        self.assertEqual(len(lines), 20)
        # The innermost frames are kept: the raise site comes last.
        self.assertTrue(lines[-1].endswith(" in _deep_local_fault"), lines[-1])
        rendered = logs.text() + _formatted(logs.records[0])
        self.assertNotIn("/", rendered.split(INTERNAL_RECORD_HEADER, 1)[1])
        self.assertNotIn("raise RuntimeError", rendered)
        self.assert_value_free(logs)

        # A local fault wrapped by an adapter is listed by its own frames.
        wrapped = self._error_with_cause(PermissionError(errno.EACCES, PRIVATE_MARKER))
        wrapped.category = "internal_error"
        with _RecordedLogs() as logs:
            log_failure(target, "Synthetic read", wrapped, level=logging.DEBUG)
        self.assertEqual(len(logs.records), 1)
        lines = self.assert_internal_record(
            logs.records[0],
            "Synthetic read",
            "errno=13, exception=TrueNASAPIError, cause=PermissionError",
        )
        self.assertTrue(any(line.endswith(" in _error_with_cause") for line in lines), lines)
        self.assert_value_free(logs)

    def test_failure_log_fields_ignore_linked_exception_text(self) -> None:
        cause = ValueError(PRIVATE_MARKER)
        cause.__context__ = KeyError(PRIVATE_MARKER)
        error = TrueNASAPIError(
            "pool.query failed (remote error).",
            category="remote_error",
            reason=PRIVATE_MARKER,
            method="pool.query",
            errname="EINVAL",
        )
        error.__cause__ = cause
        target = logging.getLogger("tests.synthetic_remote")
        with _RecordedLogs() as logs:
            log_failure(target, "Synthetic read", error)
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            ["Synthetic read failed (category=remote_error, method=pool.query, errname=EINVAL, exception=TrueNASAPIError)"],
        )
        self.assert_value_free(logs)


class WrappedLocalFaultTests(_InventoryFixture):
    """A category local code sets survives wrapping, in read and action paths."""

    INTERNAL_ACTION_DETAIL = "System locator action stopped on an internal error; see the service log."

    def _bmc(self) -> SupermicroBMCService:
        return SupermicroBMCService(
            BMCConfig(enabled=True, host="bmc.example.test", username="synthetic-user", password="***")
        )

    def _bmc_calls(self, service: SupermicroBMCService, opener: _FakeOpener):
        return (
            ("redfish", lambda: service._redfish_get_json("/redfish/v1/Systems/1")),
            ("web", lambda: service._fetch_web_text(opener, "https://bmc.example.test/cgi/url_redirect.cgi")),
            (
                "post",
                lambda: service._post_web_text(
                    opener, "/cgi/op.cgi", {"op": "misc_uid"}, csrf="synthetic", referer="https://bmc.example.test/"
                ),
            ),
        )

    def test_bmc_adapter_keeps_a_local_os_error_internal(self) -> None:
        for errno_value, expected in ((errno.EACCES, "internal_error"), (errno.ECONNREFUSED, "unreachable")):
            local = OSError(errno_value, PRIVATE_MARKER)
            service = self._bmc()
            opener = _FakeOpener(error=local)
            with patch.object(service, "_build_https_opener", return_value=opener):
                for name, call in self._bmc_calls(service, opener):
                    with self.subTest(call=name, expected=expected):
                        error = _raised_by(call)
                        self.assertIsInstance(error, TrueNASAPIError)
                        self.assertEqual(classify_remote_failure(error)[0], expected)
                        self.assertNotIn(PRIVATE_MARKER, str(error))
                        if expected == "internal_error":
                            # A local fault has no remote reason to show anyone.
                            self.assertIsNone(error.reason)
                            with _RecordedLogs() as logs:
                                log_failure(logging.getLogger("tests.synthetic_bmc"), "BMC read", error)
                            self.assertEqual(len(logs.records), 1)
                            self.assertEqual(logs.records[0].levelno, logging.ERROR)
                            self.assertIn("errno=13", logs.records[0].getMessage())
                            self.assertIn(INTERNAL_RECORD_HEADER, logs.records[0].getMessage())
                            self.assertNotIn(PRIVATE_MARKER, logs.text())
                        else:
                            # Control: a transport failure keeps its bounded reason.
                            self.assertTrue(error.reason)

    def test_bmc_inventory_reports_a_local_fault_as_internal(self) -> None:
        service = self._bmc()
        opener = _FakeOpener(error=PermissionError(errno.EACCES, PRIVATE_MARKER))
        with (
            patch.object(service, "_build_https_opener", return_value=opener),
            patch.object(service, "_populate_redfish_simple_storage_fallback"),
            _RecordedLogs() as logs,
        ):
            inventory = service.fetch_inventory()
        self.assertEqual(
            inventory.warnings,
            [
                "BMC Redfish inventory stopped on an internal error; see the service log.",
                "BMC web inventory stopped on an internal error; see the service log.",
            ],
        )
        for operation in ("BMC Redfish inventory", "BMC web inventory"):
            records = logs.matching(f"{operation} failed (category=internal_error")
            self.assertEqual(len(records), 1, logs.text())
            self.assertEqual(records[0].levelno, logging.ERROR)
            self.assertIn(INTERNAL_RECORD_HEADER, records[0].getMessage())
        self.assertNotIn(PRIVATE_MARKER, logs.text() + json.dumps(inventory.warnings))

    async def test_quantastor_source_reports_a_local_fault_as_internal(self) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(
                host="http://source.example.test",
                platform="quantastor",
                api_user="synthetic-user",
                api_password="synthetic-password",
            )
        )
        for cause, expected in (
            (PermissionError(errno.EACCES, PRIVATE_MARKER), "collection stopped on an internal error; see the service log."),
            (ConnectionRefusedError(errno.ECONNREFUSED, PRIVATE_MARKER), "could not be reached."),
        ):
            with self.subTest(cause=type(cause).__name__):
                with patch("app.services.quantastor_api.urlopen_with_tls_config", side_effect=cause):
                    wrapped = _raised_by(lambda: client._request_json("storageSystemEnum"))
                service = self._service(platform="quantastor", api_error=wrapped)
                with _RecordedLogs() as logs:
                    bundle = await service._collect_inventory_source_bundle()
                message = bundle.sources["api"].message
                self.assertTrue(message.endswith(expected), message)
                records = logs.matching("Platform API collection failed")
                self.assertEqual(len(records), 1, logs.text())
                self.assertEqual(records[0].levelno, logging.ERROR)
                self.assertEqual(INTERNAL_RECORD_HEADER in records[0].getMessage(), isinstance(cause, PermissionError))
                self.assertNotIn(PRIVATE_MARKER, logs.text() + message)

    async def _locator_post(self, error: BaseException) -> tuple[HTTPException, _RecordedLogs]:
        service = self._service(bmc_error=TrueNASAPIError("synthetic"))
        assert service.bmc_service is not None
        service.bmc_service.set_uid_active.side_effect = error
        registry = MagicMock()
        registry.get_service.return_value = service
        route = self._route("/api/system-locator", "POST")
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "add_perf_metadata"),
            _RecordedLogs() as logs,
            self.assertRaises(HTTPException) as raised,
        ):
            await route(payload=SystemLocatorRequest(active=True), system_id=None)
        return raised.exception, logs

    async def test_locator_action_shows_typed_reasons_and_hides_local_faults(self) -> None:
        bmc = self._bmc()
        refusal = HTTPError(
            "https://bmc.example.test/cgi/op.cgi",
            500,
            "synthetic BMC refusal",
            Message(),
            io.BytesIO(b""),
        )
        post = lambda opener: bmc._post_web_text(  # noqa: E731 - one call shape, two openers.
            opener, "/cgi/op.cgi", {"op": "misc_uid"}, csrf="synthetic", referer="https://bmc.example.test/"
        )

        # Control: a typed BMC failure still carries its bounded reason.
        response, logs = await self._locator_post(_raised_by(lambda: post(_FakeOpener(error=refusal))))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.detail, "Supermicro BMC request failed: HTTP Error 500: synthetic BMC refusal")
        self.assertFalse(logs.matching("category=internal_error"), logs.text())

        local_faults = (
            (
                "unexpected exception",
                RuntimeError(HOSTILE_IDENTIFIER + "x" * 700),
                "method=set_uid_active, exception=TrueNASAPIError, cause=RuntimeError",
                "mock.py:",
            ),
            (
                "wrapped local OSError",
                _raised_by(lambda: post(_FakeOpener(error=PermissionError(errno.EACCES, PRIVATE_MARKER)))),
                "method=/cgi/op.cgi, errno=13, exception=TrueNASAPIError, cause=PermissionError",
                "test_inventory_status_error_text.py:",
            ),
        )
        for name, error, fields, origin_frame in local_faults:
            with self.subTest(case=name):
                response, logs = await self._locator_post(error)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.detail, self.INTERNAL_ACTION_DETAIL)
                records = logs.matching("System locator action failed (category=internal_error")
                self.assertEqual(len(records), 1, logs.text())
                lines = LogOriginTests.assert_internal_record(self, records[0], "System locator action", fields)
                # The frames are those of the fault itself, not of the wrapper.
                self.assertTrue(lines[-1].startswith(f"  {origin_frame}"), lines)
                rendered = logs.text() + "".join(_formatted(record) for record in logs.records)
                self.assertNotIn(PRIVATE_MARKER, rendered + response.detail)
                self.assertNotIn("\x1b", rendered + response.detail)



# A value an appliance or a caller could choose: a marker, terminal and line
# controls, a NUL, DEL, a C1 control and a long run, so a record that kept it
# would be obvious.
LONG_HOSTILE_VALUE = PRIVATE_MARKER + "\x1b[31m\r\n\x00\x7f\x9b" + "x" * 5000


def _assert_record_value_free(test: unittest.TestCase, record: logging.LogRecord) -> None:
    rendered = record.getMessage() + "\n" + _formatted(record)
    for fragment in (PRIVATE_MARKER, "\x1b", "\x00", "\x7f", "\x9b", "\r", "x" * 100):
        test.assertNotIn(fragment, rendered, rendered[:400])
    test.assertFalse(record.exc_info)


class SmartBatchLocalFaultLogTests(_InventoryFixture):
    """SMART batch local faults log a fixed operation, fields and a checked identifier."""

    def _registry(self, error: BaseException) -> MagicMock:
        service = MagicMock()
        service.system = SystemConfig(id="synthetic-route", truenas=TrueNASConfig(platform="core"))
        service.get_slot_smart_summaries = AsyncMock(side_effect=error)
        service.get_storage_view_slot_smart_summaries = AsyncMock(side_effect=error)
        registry = MagicMock()
        registry.get_service.return_value = service
        return registry

    async def _call(self, twin: str, identifier: str, error: BaseException) -> tuple[HTTPException, _RecordedLogs]:
        registry = self._registry(error)
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "resolve_read_layout_slots", new=AsyncMock(return_value=(None, "verified"))),
            patch.object(app_routes, "add_perf_metadata"),
            _RecordedLogs() as logs,
            self.assertRaises(HTTPException) as raised,
        ):
            if twin == "enclosure":
                route = self._route("/api/slots/smart-batch", "POST")
                await route(payload=SmartBatchRequest(slots=[0]), system_id=None, enclosure_id=identifier, fresh=False)
            else:
                route = self._route("/api/storage-views/{view_id}/slots/smart-batch", "POST")
                await route(
                    view_id=identifier,
                    payload=SmartBatchRequest(slots=[0]),
                    system_id=None,
                    enclosure_id=None,
                    fresh=False,
                )
        return raised.exception, logs

    async def test_smart_batch_local_faults_log_fixed_operations_and_checked_identifiers(self) -> None:
        twins = (
            ("enclosure", "SMART batch", "enclosure", "shelf-a"),
            ("view", "Storage-view SMART batch", "view", "archive-view"),
        )
        for twin, prefix, field, plain in twins:
            faults = (
                (
                    "storage",
                    lambda: PermissionError(errno.EACCES, LONG_HOSTILE_VALUE),
                    f"{prefix} slot-detail cache write",
                    app_routes.SMART_BATCH_LOCAL_STORAGE_DETAIL,
                    "category=internal_error, errno=13, exception=PermissionError",
                ),
                (
                    "trust",
                    lambda: TlsTrustConfigurationError(
                        LONG_HOSTILE_VALUE, FileNotFoundError(errno.ENOENT, LONG_HOSTILE_VALUE)
                    ),
                    f"{prefix} TLS CA bundle load",
                    app_routes.SMART_BATCH_TLS_TRUST_DETAIL,
                    "category=local, exception=TlsTrustConfigurationError",
                ),
            )
            for fault, make_error, operation, detail, fields in faults:
                for identifier, shown in ((plain, plain), (LONG_HOSTILE_VALUE, "(unlisted)")):
                    with self.subTest(twin=twin, fault=fault, shown=shown):
                        response, logs = await self._call(twin, identifier, make_error())
                        self.assertEqual(response.status_code, 500)
                        self.assertEqual(response.detail, detail)
                        records = logs.matching(f"{operation} failed")
                        self.assertEqual(len(records), 1, logs.text()[:400])
                        record = records[0]
                        self.assertEqual(record.levelno, logging.ERROR)
                        header = record.getMessage().partition(INTERNAL_RECORD_HEADER)[0]
                        self.assertEqual(header, f"{operation} failed ({fields}, {field}={shown})")
                        for other in logs.records:
                            _assert_record_value_free(self, other)


class ActionPathLogTests(_InventoryFixture):
    """Locate-light and mapping-save records hold fixed words, fields and checked identifiers."""

    def test_locate_light_result_line_names_only_a_well_formed_device(self) -> None:
        service = self._service()
        for device, shown in (("/dev/ses0", "/dev/ses0"), (LONG_HOSTILE_VALUE, "(unlisted)")):
            with self.subTest(shown=shown):
                with _RecordedLogs() as logs:
                    service._log_led_action_result(
                        self._smart_slot(),
                        device=device,
                        element=3,
                        action=LedAction.identify,
                        exit_status=1,
                        duration_seconds=0.25,
                    )
                records = logs.matching("Locate light command")
                self.assertEqual(len(records), 1, logs.text()[:400])
                self.assertIn(f" device={shown} element=3 ", records[0].getMessage())
                _assert_record_value_free(self, records[0])

    async def _save_with_failed_clear(self, error: BaseException) -> tuple[JSONResponse, _RecordedLogs]:
        service = MagicMock()
        service.system = SystemConfig(id="synthetic-route", truenas=TrueNASConfig(platform="core"))
        service.save_mapping = AsyncMock(
            return_value=ManualMapping(system_id="synthetic-route", enclosure_id="enc-a", slot=2, serial="SANITIZED")
        )
        service.set_slot_led = AsyncMock(side_effect=error)
        service.peek_cached_snapshot.return_value = None
        service.get_snapshot = AsyncMock(return_value=InventorySnapshot(slots=[], refresh_interval_seconds=30))
        registry = MagicMock()
        registry.get_service.return_value = service
        route = self._route("/api/slots/{slot}/mapping", "POST")
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "ensure_slot_bounds", new=AsyncMock()),
            patch.object(app_routes, "add_perf_metadata"),
            _RecordedLogs() as logs,
        ):
            response = await route(
                slot=2,
                payload=MappingRequest(expected_revision="a" * 64, serial="SANITIZED", clear_identify_after_save=True),
                system_id=None,
                enclosure_id="enc-a",
            )
        return response, logs

    async def test_mapping_save_light_clear_failure_logs_structured_fields(self) -> None:
        remote = TrueNASAPIError("SSH LED action failed.", reason=LONG_HOSTILE_VALUE)
        cases = (
            (remote, logging.WARNING, "Mapping identify-light clear failed (category=remote_error, exception=TrueNASAPIError, bay=2)"),
            (RuntimeError(LONG_HOSTILE_VALUE), logging.ERROR, None),
        )
        for error, level, expected in cases:
            with self.subTest(error=type(error).__name__):
                response, logs = await self._save_with_failed_clear(error)
                body = json.loads(response.body)
                # The operator-facing warning is unchanged.
                self.assertEqual(body["warning"], "Saved mapping, but failed to clear the identify LED; see application logs.")
                records = logs.matching("Mapping identify-light clear failed")
                self.assertEqual(len(records), 1, logs.text()[:400])
                self.assertEqual(records[0].levelno, level)
                if expected is not None:
                    self.assertEqual(records[0].getMessage(), expected)
                else:
                    LogOriginTests.assert_internal_record(
                        self, records[0], "Mapping identify-light clear", "exception=RuntimeError, bay=2"
                    )
                for record in logs.records:
                    _assert_record_value_free(self, record)

    def _ssh_led_service(self, platform: str) -> InventoryService:
        service = self._service(platform=platform)
        service.system = service.system.model_copy(
            update={"ssh": SSHConfig(enabled=True, host="source.example.test", user="svcuser")}
        )
        return service

    @staticmethod
    def _failed_command(command: str = "synthetic") -> SSHCommandResult:
        return SSHCommandResult(command=command, ok=False, stdout="", stderr=LONG_HOSTILE_VALUE, exit_code=1)

    async def _ssh_led_errors(self) -> list[tuple[str, BaseException]]:
        errors: list[tuple[str, BaseException]] = []
        slot = self._smart_slot().model_copy(update={"raw_status": {"vendor_slot_number": 3}})

        service = self._ssh_led_service("core")
        service._run_led_ssh_sequence = AsyncMock(
            return_value=[(self._failed_command(), 0.01), (self._failed_command(), 0.0)]
        )
        errors.append(("unifi action", await _raised_by_async(service._set_unifi_slot_led_over_ssh(slot, LedAction.identify))))

        async def failing_preflight(_host, commands, *, then=None):
            results = [self._failed_command(command) for command in commands]
            if then is not None:
                then(results)
            return [(result, 0.0) for result in results]

        service._run_led_ssh_sequence = failing_preflight
        core_target = {"ssh_host": None, "ses_device": "/dev/ses0", "ses_element_id": 3, "enclosure_id": "shelf-a"}
        errors.append(
            (
                "core preflight",
                await _raised_by_async(
                    service._set_core_slot_led_over_ssh(slot, LedAction.identify, True, core_target, [core_target])
                ),
            )
        )
        sg_targets = [
            {"ssh_host": None, "ses_device": "/dev/sg3", "ses_element_id": 3, "ses_slot_number": 2, "enclosure_id": "shelf-a"},
            {"ssh_host": "node-b.example.test", "ses_device": "/dev/sg9", "ses_element_id": 3, "ses_slot_number": 2, "enclosure_id": "shelf-a"},
        ]
        errors.append(
            (
                "sg_ses every path",
                await _raised_by_async(
                    service._set_sg_ses_slot_led_over_ssh(slot, LedAction.identify, True, sg_targets)
                ),
            )
        )

        async def one_failed_one_moved(host, commands, *, then=None):
            if host is None:
                results = [self._failed_command(command) for command in commands]
            else:
                results = [SSHCommandResult(command=command, ok=True, stdout="", exit_code=0) for command in commands]
            if then is not None:
                then(results)
            return [(result, 0.0) for result in results]

        service._run_led_ssh_sequence = one_failed_one_moved
        errors.append(
            (
                "sg_ses moved after a failure",
                await _raised_by_async(
                    service._set_sg_ses_slot_led_over_ssh(slot, LedAction.identify, True, sg_targets)
                ),
            )
        )
        return errors

    async def test_ssh_locate_light_output_reaches_only_the_bounded_action_reason(self) -> None:
        led_route = self._route("/api/slots/{slot}/led", "POST")
        for name, error in await self._ssh_led_errors():
            with self.subTest(producer=name):
                self.assertIsInstance(error, TrueNASAPIError)
                # The message is this service's own sentence; the output is only the reason.
                message = str(error)
                for fragment in (PRIVATE_MARKER, "\x1b", "\x00", "xxxx"):
                    self.assertNotIn(fragment, message)
                reason = error.reason or ""
                self.assertIn(PRIVATE_MARKER, reason)
                self.assertLessEqual(len(reason), 300)
                self.assertTrue(all(character.isprintable() for character in reason), repr(reason[:60]))

                service = MagicMock()
                service.system = SystemConfig(id="synthetic-route", truenas=TrueNASConfig(platform="core"))
                service.set_slot_led = AsyncMock(side_effect=error)
                registry = MagicMock()
                registry.get_service.return_value = service
                with (
                    patch.object(app_routes, "get_inventory_registry", return_value=registry),
                    patch.object(app_routes, "add_perf_metadata"),
                    _RecordedLogs() as logs,
                    self.assertRaises(HTTPException) as raised,
                ):
                    await led_route(slot=0, payload=LedRequest(action=LedAction.identify), system_id=None, enclosure_id=None)
                detail = raised.exception.detail
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(detail, f"{message.rstrip('.')}: {reason}")
                self.assertLessEqual(len(detail), len(message) + 302)
                self.assertTrue(all(character.isprintable() for character in detail), repr(detail[:80]))
                records = logs.matching("Locate light refusal")
                self.assertEqual(len(records), 1, logs.text()[:400])
                self.assertIn("(category=remote_error, exception=", records[0].getMessage())
                for record in logs.records:
                    _assert_record_value_free(self, record)

    async def test_ordinary_led_refusal_keeps_its_sentence_in_the_response_and_log(self) -> None:
        refusal = TrueNASAPIError("LED control is not available for slot 00 on this system.")
        service = MagicMock()
        service.system = SystemConfig(id="synthetic-route", truenas=TrueNASConfig(platform="core"))
        service.set_slot_led = AsyncMock(side_effect=refusal)
        registry = MagicMock()
        registry.get_service.return_value = service
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "add_perf_metadata"),
            _RecordedLogs() as logs,
            self.assertRaises(HTTPException) as raised,
        ):
            await self._route("/api/slots/{slot}/led", "POST")(
                slot=0, payload=LedRequest(action=LedAction.identify), system_id=None, enclosure_id=None
            )
        self.assertEqual(raised.exception.detail, "LED control is not available for slot 00 on this system.")
        self.assertEqual(
            [record.getMessage() for record in logs.matching("Locate light refusal")],
            [
                "Locate light refusal system=synthetic-route bay 0 action=IDENTIFY "
                "reason=LED control is not available for slot 00 on this system. "
                "(category=remote_error, exception=TrueNASAPIError)"
            ],
        )

    async def test_partial_locate_failures_are_bounded_and_control_free(self) -> None:
        from tests.test_inventory import partial_locate_fixture

        service, _sent = partial_locate_fixture(self, same_host=True, reverse=False, failure="action", observed=True)
        probes = {}
        original = service._ssh_probe_for_host

        def hostile_probe(host):
            if host not in probes:
                probe = original(host)
                session = probe.open_session.return_value
                inner = session.run_command_owned.side_effect

                def run(command, inner=inner):
                    result = inner(command)
                    return result if result.ok else self._failed_command(command)

                session.run_command_owned = AsyncMock(side_effect=run)
                probes[host] = probe
            return probes[host]

        service._ssh_probe_for_host = hostile_probe
        result = await service.set_slot_led(0, LedAction.identify, "shelf-a", invalidate_snapshot=False)
        self.assertTrue(result["partial"])
        self.assertEqual(len(result["failures"]), 1)
        failure = result["failures"][0]
        self.assertIn(PRIVATE_MARKER, failure)
        self.assertLessEqual(len(failure), 240)
        self.assertTrue(all(character.isprintable() for character in failure), repr(failure[:60]))


class UnusableReplyShapeTests(unittest.IsolatedAsyncioTestCase):
    """A decoded reply that is not an object is an unusable response, not a local fault."""

    NON_OBJECTS = ([], [PRIVATE_MARKER], 5, PRIVATE_MARKER, None, True)

    def _bmc(self) -> SupermicroBMCService:
        return SupermicroBMCService(
            BMCConfig(enabled=True, host="bmc.example.test", username="synthetic-user", password="***")
        )

    def test_redfish_root_that_is_not_an_object_is_an_unusable_response(self) -> None:
        service = self._bmc()
        opener = _FakeOpener(body=json.dumps({"Model": "synthetic"}).encode())
        with patch.object(service, "_build_https_opener", return_value=opener):
            self.assertEqual(service._redfish_get_json("/redfish/v1/Systems/1"), {"Model": "synthetic"})
        for value in self.NON_OBJECTS:
            with self.subTest(value=type(value).__name__):
                opener = _FakeOpener(body=json.dumps(value).encode())
                with patch.object(service, "_build_https_opener", return_value=opener):
                    error = _raised_by(lambda: service._redfish_get_json("/redfish/v1/Systems/1"))
                self.assertIsInstance(error, TrueNASAPIError)
                self.assertEqual(str(error), "Redfish returned an unusable response.")
                self.assertEqual(error.method, "/redfish/v1/Systems/1")
                self.assertEqual(classify_remote_failure(error), ("response_invalid", None))

    def test_bmc_inventory_reports_a_non_object_root_as_unavailable(self) -> None:
        service = self._bmc()
        with (
            patch.object(service, "_build_https_opener", return_value=_FakeOpener(body=b"[]")),
            patch.object(service, "_populate_web_inventory"),
            _RecordedLogs() as logs,
        ):
            try:
                inventory = service.fetch_inventory()
            except AttributeError as escaped:
                self.fail(f"an unusable BMC response escaped as a local fault ({type(escaped).__name__})")
        self.assertEqual(inventory.warnings, ["BMC Redfish inventory unavailable."])
        self.assertEqual(
            [record.getMessage() for record in logs.matching("BMC Redfish inventory failed")],
            [
                "BMC Redfish inventory failed (category=response_invalid, "
                "method=/redfish/v1/Systems/1, exception=TrueNASAPIError)"
            ],
        )

    async def test_ddp_reply_that_is_not_an_object_is_an_unusable_response(self) -> None:
        class ScriptedSocket:
            def __init__(self, reply) -> None:
                self.reply = reply
                self.send = AsyncMock()

            async def recv(self) -> str:
                return json.dumps(self.reply)

        client = TrueNASWebsocketClient(TrueNASConfig(platform="core", api_key="synthetic-key"))
        control = await client._await_ddp_reply(
            ScriptedSocket({"msg": "result", "id": "request-1", "result": [1]}), "disk.query", "request-1"
        )
        self.assertEqual(control, [1])
        for value in self.NON_OBJECTS:
            with self.subTest(reader="call", value=type(value).__name__):
                error = await _raised_by_async(
                    client._await_ddp_reply(ScriptedSocket(value), "disk.query", "request-1")
                )
                self.assertIsInstance(error, TrueNASAPIError)
                self.assertEqual(str(error), "disk.query returned an unusable reply.")
                self.assertEqual(error.method, "disk.query")
                self.assertEqual(classify_remote_failure(error), ("response_invalid", None))
            with self.subTest(reader="handshake", value=type(value).__name__):
                error = await _raised_by_async(client._perform_handshake(ScriptedSocket(value), None))
                self.assertIsInstance(error, TrueNASAPIError)
                self.assertEqual(classify_remote_failure(error), ("response_invalid", None))

    async def test_ddp_dispatcher_reports_a_non_object_reply_as_unusable(self) -> None:
        class QueuedSocket:
            def __init__(self, reply) -> None:
                self.reply = reply
                self.queue: asyncio.Queue[str] = asyncio.Queue()

            async def send(self, payload: str) -> None:
                request = json.loads(payload)
                reply = self.reply
                if isinstance(reply, dict):
                    reply = {**reply, "id": request["id"]}
                self.queue.put_nowait(json.dumps(reply))

            async def recv(self) -> str:
                return await self.queue.get()

        control = truenas_ws_module._MiddlewareCallDispatcher(QueuedSocket({"msg": "result", "result": "ok"}))
        try:
            self.assertEqual(await control.call("disk.smartctl", ["da0"]), "ok")
        finally:
            await control.close()
        for value in self.NON_OBJECTS:
            with self.subTest(value=type(value).__name__):
                dispatcher = truenas_ws_module._MiddlewareCallDispatcher(QueuedSocket(value))
                try:
                    error = await _raised_by_async(dispatcher.call("disk.smartctl", ["da0"]))
                finally:
                    await dispatcher.close()
                self.assertIsInstance(error, TrueNASAPIError)
                self.assertEqual(classify_remote_failure(error), ("response_invalid", None))


class _FailingBody(io.BufferedIOBase):
    """An HTTP error body whose read fails with a given exception."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def readable(self) -> bool:
        return True

    def read(self, *_args: object) -> bytes:
        raise self.error


class ErrorBodyReadOriginTests(unittest.TestCase):
    """A fault while reading an HTTP error body keeps its origin."""

    def _http_error(self, error: BaseException) -> HTTPError:
        return HTTPError("https://bmc.example.test/redfish/v1", 500, "synthetic", Message(), _FailingBody(error))

    def _adapters(self, error: BaseException):
        bmc = SupermicroBMCService(
            BMCConfig(enabled=True, host="bmc.example.test", username="synthetic-user", password="***")
        )
        quantastor = QuantastorRESTClient(
            TrueNASConfig(
                host="http://source.example.test",
                platform="quantastor",
                api_user="synthetic-user",
                api_password="synthetic-password",
            )
        )

        def bmc_call():
            with patch.object(bmc, "_build_https_opener", return_value=_FakeOpener(error=self._http_error(error))):
                bmc._redfish_get_json("/redfish/v1/Systems/1")

        def quantastor_call():
            with patch(
                "app.services.quantastor_api.urlopen_with_tls_config",
                side_effect=self._http_error(error),
            ):
                quantastor._request_json("storageSystemEnum")

        return (
            ("bmc", bmc_call, "Redfish request failed with HTTP 500; error response could not be read."),
            (
                "quantastor",
                quantastor_call,
                "Quantastor endpoint storageSystemEnum failed (500); error response could not be read.",
            ),
        )

    def test_error_body_read_faults_keep_their_origin(self) -> None:
        cases = (
            (PermissionError(errno.EACCES, PRIVATE_MARKER), ("internal_error", None)),
            # Control: a transport fault after the status arrived stays the reported error.
            (ConnectionResetError(errno.ECONNRESET, PRIVATE_MARKER), ("remote_error", 500)),
        )
        for cause, expected in cases:
            for name, call, message in self._adapters(cause):
                with self.subTest(adapter=name, cause=type(cause).__name__):
                    error = _raised_by(call)
                    self.assertIsInstance(error, TrueNASAPIError)
                    self.assertEqual(str(error), message)
                    self.assertEqual(classify_remote_failure(error), expected)
                    self.assertIsNone(error.reason)
                    self.assertIs(error.__cause__, cause)

    def test_error_body_read_origin_is_decided_by_type_and_errno(self) -> None:
        # The base OSError type carrying a local errno (a library that sets
        # errno itself raises this shape rather than PermissionError).
        base_eacces = OSError(PRIVATE_MARKER)
        base_eacces.errno = errno.EACCES
        self.assertIs(type(base_eacces), OSError)
        cases = (
            (base_eacces, ("internal_error", None)),
            (OSError(errno.ENOSPC, "connection reset by peer"), ("internal_error", None)),
            (OSError(errno.EIO, PRIVATE_MARKER), ("internal_error", None)),
            (FileNotFoundError(errno.ENOENT, PRIVATE_MARKER), ("internal_error", None)),
            (PermissionError(errno.EACCES, "connection reset by peer"), ("internal_error", None)),
            # Control: a transport fault stays the reported error, whatever its message says.
            (ConnectionResetError(errno.ECONNRESET, "permission denied"), ("remote_error", 500)),
        )
        for cause, expected in cases:
            for name, call, message in self._adapters(cause):
                with self.subTest(adapter=name, cause=type(cause).__name__, errno=cause.errno):
                    error = _raised_by(call)
                    self.assertIsInstance(error, TrueNASAPIError)
                    self.assertEqual(str(error), message)
                    self.assertEqual(classify_remote_failure(error), expected)
                    self.assertIsNone(error.reason)
                    self.assertIs(error.__cause__, cause)


def _outermost_caller() -> None:
    _deep_local_fault(40)


def _cause_site() -> None:
    raise PermissionError(errno.EACCES, PRIVATE_MARKER)


def _context_site() -> None:
    raise KeyError(PRIVATE_MARKER)


class FailureRecordContractTests(unittest.TestCase):
    """The internal-error record, origin, local category and identifier contracts."""

    def _record(self, error: BaseException) -> tuple[logging.LogRecord, list[str]]:
        with _RecordedLogs() as logs:
            log_failure(logging.getLogger("tests.synthetic_contract"), "Synthetic read", error)
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        frames = record.getMessage().partition(INTERNAL_RECORD_HEADER)[2].splitlines()
        return record, frames

    def test_frames_are_the_innermost_ones_of_the_chain(self) -> None:
        error = _raised_by(_outermost_caller)
        _record, frames = self._record(error)
        expected = [
            f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}"
            for frame in __import__("traceback").extract_tb(error.__traceback__)[-20:]
        ]
        self.assertEqual(frames, expected)
        names = [line.rsplit(" in ", 1)[1] for line in frames]
        self.assertEqual(names[-1], "_deep_local_fault")
        # The outer callers fall outside the kept window.
        self.assertNotIn("_outermost_caller", names)
        self.assertNotIn("_raised_by", names)

    def test_an_explicit_cause_is_the_origin_before_the_context(self) -> None:
        cause = _raised_by(_cause_site)
        error: BaseException | None = None
        try:
            _context_site()
        except KeyError:
            try:
                raise TrueNASAPIError("Synthetic read failed.", category="internal_error") from cause
            except TrueNASAPIError as caught:
                error = caught
        assert error is not None
        self.assertIsInstance(error.__context__, KeyError)
        record, frames = self._record(error)
        self.assertIn("cause=PermissionError", record.getMessage())
        names = [line.rsplit(" in ", 1)[1] for line in frames]
        self.assertIn("_cause_site", names)
        self.assertNotIn("_context_site", names)

    def test_a_local_service_error_marked_internal_is_an_internal_error(self) -> None:
        error = truenas_ws_module.LocalServiceError("Synthetic sentence.", category="internal_error")
        self.assertEqual(classify_remote_failure(error), ("internal_error", None))
        record, _frames = self._record(error)
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIn("category=internal_error", record.getMessage())
        self.assertIn(INTERNAL_RECORD_HEADER, record.getMessage())

    def test_identifier_formatter_refuses_containers(self) -> None:
        log_identifier = truenas_ws_module.log_identifier
        for value in (("shelf-a",), ("shelf", "a"), ["shelf-a"], {"shelf-a"}, b"shelf-a"):
            with self.subTest(value=repr(value)):
                self.assertEqual(log_identifier(value), "(unlisted)")

    def test_action_reason_is_rebounded_when_set_after_construction(self) -> None:
        error = TrueNASAPIError("enclosure.set_slot_status failed (remote error).", category="remote_error")
        error.reason = LONG_HOSTILE_VALUE
        detail = app_routes._authorized_action_failure_detail(error, operation="Locate light action")
        self.assertTrue(detail.startswith("enclosure.set_slot_status failed (remote error): "), detail[:80])
        self.assertLessEqual(len(detail), len("enclosure.set_slot_status failed (remote error): ") + 300)
        self.assertTrue(all(character.isprintable() for character in detail))

    def test_action_reason_replaces_every_control_character(self) -> None:
        message = "enclosure.set_slot_status failed (remote error)."
        for code in (*range(0x20), 0x7F, *range(0x80, 0xA0)):
            with self.subTest(code=f"U+{code:04X}"):
                error = TrueNASAPIError(message, category="remote_error", reason=f"slot{chr(code)}busy")
                self.assertEqual(error.reason, "slot busy")
                detail = app_routes._authorized_action_failure_detail(error, operation="Locate light action")
                self.assertEqual(detail, "enclosure.set_slot_status failed (remote error): slot busy")
        # Control: printable text, non-ASCII letters included, is kept as reported.
        error = TrueNASAPIError(message, category="remote_error", reason="Slot 3 \u2013 caf\u00e9 busy")
        self.assertEqual(error.reason, "Slot 3 \u2013 caf\u00e9 busy")


class DdpReplyMarkerTests(unittest.IsolatedAsyncioTestCase):
    """The older dialect's two known reply markers, and the typed method-unavailable test."""

    def test_older_dialect_markers_are_one_named_constant(self) -> None:
        markers = getattr(truenas_ws_module, "DDP_REPLY_MARKERS", None)
        self.assertIsNotNone(markers, "the older dialect's reply markers live in one named constant")
        self.assertEqual(
            dict(markers or {}),
            {"busy": "maximum number of concurrent calls", "method_unavailable": "ENOMETHOD"},
        )
        busy = build_ddp_error("disk.query", {"error": 5, "reason": "Maximum number of concurrent calls (20) reached"})
        self.assertIsInstance(busy, truenas_ws_module.TrueNASAPIBusyError)
        self.assertEqual(classify_remote_failure(busy)[0], "busy")
        missing = build_ddp_error("disk.smartctl", {"error": 5, "reason": "ENOMETHOD"})
        self.assertEqual((missing.category, missing.errname), ("method_unavailable", "ENOMETHOD"))
        ordinary = build_ddp_error("disk.query", {"error": 5, "reason": "synthetic refusal"})
        self.assertEqual(classify_remote_failure(ordinary), ("remote_error", 5))
        for error in (busy, missing, ordinary):
            self.assertNotIn("concurrent", str(error))
            self.assertNotIn("refusal", str(error))

    @staticmethod
    def _scale_client() -> TrueNASWebsocketClient:
        return TrueNASWebsocketClient(TrueNASConfig(platform="scale", api_key="synthetic-key"))

    @staticmethod
    def _session():
        class Session:
            async def __aenter__(self):
                return MagicMock()

            async def __aexit__(self, *_exc_info):
                return None

        return Session()

    async def _consumer_outcomes(self, error: BaseException) -> dict[str, bool]:
        outcomes: dict[str, bool] = {}
        client = self._scale_client()
        with (
            patch.object(client, "_session", side_effect=self._session),
            patch.object(client, "_call", new=AsyncMock(side_effect=error)),
        ):
            raised = await _raised_by_async(client._fetch_disk_smartctl("da0"))
        outcomes["single"] = isinstance(raised, truenas_ws_module.LocalServiceError)

        dispatcher = MagicMock()
        dispatcher.call = AsyncMock(side_effect=error)
        dispatcher.close = AsyncMock()
        with (
            patch.object(client, "_session", side_effect=self._session),
            patch.object(client, "_make_dispatcher", return_value=dispatcher),
        ):
            raised = await _raised_by_async(client._run_smartctl_batch(["da0"], ["-a", "-j"], 1))
        outcomes["batch"] = isinstance(raised, truenas_ws_module.LocalServiceError)

        with _RecordedLogs() as logs:
            self.assertEqual(await client._fetch_smart_test_results(AsyncMock(side_effect=error)), [])
        outcomes["history"] = bool(logs.matching("smart.test.results is unavailable"))
        return outcomes

    async def test_method_unavailable_is_read_from_the_typed_error_name(self) -> None:
        typed_only = TrueNASAPIError(
            "disk.smartctl failed (method unavailable).",
            errname="ENOMETHOD",
            category="method_unavailable",
            method="disk.smartctl",
        )
        lookalike = build_ddp_error("disk.smartctl", {"error": 22, "errname": "ENOMETHODS"})
        self.assertIn("ENOMETHOD", str(lookalike))
        cases = (
            ("typed error name", typed_only, True),
            ("older dialect error name", build_ddp_error("disk.smartctl", {"error": 22, "errname": "ENOMETHOD"}), True),
            ("older dialect reply marker", build_ddp_error("disk.smartctl", {"reason": "ENOMETHOD"}), True),
            ("JSON-RPC missing method", build_jsonrpc_error("disk.smartctl", {"code": -32601, "message": "x"}), True),
            ("different error name", lookalike, False),
            ("ordinary failure", build_ddp_error("disk.smartctl", {"error": 22, "reason": "synthetic"}), False),
        )
        for name, error, unavailable in cases:
            with self.subTest(case=name):
                outcomes = await self._consumer_outcomes(error)
                self.assertEqual(outcomes, {"single": unavailable, "batch": unavailable, "history": unavailable})


if __name__ == "__main__":
    unittest.main()
