"""Persisted slot details belong to the appliance that produced them (#1207).

A configured system ID names a slot in the settings, not an appliance. When a
reload keeps the ID but points it at another endpoint, the slot-detail store is
reused, so its rows must say which collection wrote them and every reader must
refuse rows written for a different one. Synthetic fixtures only: the
appliance client is a mock that always fails, no network is touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import http.client
import logging
import os
import ssl
import tempfile
import types
import typing
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from pydantic import BaseModel, ValidationError

from app import routes as app_routes
from app.config import BMCConfig, PathConfig, Settings, SSHConfig, SystemConfig, TrueNASConfig, normalize_text
from app.models.domain import SlotView, SmartBatchRequest, SmartSummaryView
from app.route_support import ensure_read_slot_bounds
from app.services import inventory as inventory_module
from app.services import truenas_ws as truenas_ws_module
from app.services.inventory import slot_detail_collection_binding
from app.services.inventory_registry import InventoryRegistry
from app.services.quantastor_api import QuantastorRESTClient
from app.services.slot_detail_store import SlotDetailCacheEntry
from app.services.supermicro_bmc import SupermicroBMCService
from app.services.truenas_ws import TrueNASAPIError, TrueNASRawData, TrueNASWebsocketClient

SYSTEM_ID = "synthetic-system"
SHELF = "synthetic-shelf"


# ---------------------------------------------------------------------------
# What the transports really send. The real clients run through urllib,
# http.client and the WebSocket library up to the point where bytes would
# leave: TCP is never opened, the TLS handshake is replaced by a recorder.
# These observations do not use the destination plan, so a test comparing
# them with the binding checks the plan against the wire, not against itself.

class _Captured(Exception):
    """Raised where the transport would first touch the network."""


_PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "ws_proxy", "wss_proxy", "socks_proxy")


@contextlib.contextmanager
def _no_ambient_proxy():
    # Proxy variables are deployment configuration, not part of a system's
    # destination; keep them out of these observations.
    with patch.dict(os.environ):
        for name in list(os.environ):
            if name.lower() in _PROXY_VARIABLES:
                del os.environ[name]
        yield


@contextlib.contextmanager
def _http_wire():
    seen: dict = {}
    original_putrequest = http.client.HTTPConnection.putrequest

    def putrequest(conn, method, url, *args, **kwargs):
        tls = isinstance(conn, http.client.HTTPSConnection)
        # The whole request target, query included: a query-routing gateway
        # can pick the machine by it.
        seen["request"] = ("https" if tls else "http", conn.host.lower(), conn.port, url)
        return original_putrequest(conn, method, url, *args, **kwargs)

    def connect(conn):
        seen["connect"] = (conn.host.lower(), conn.port)
        if isinstance(conn, http.client.HTTPSConnection):
            conn.sock = Mock()  # never written to: the TLS wrap below stops first
            return
        raise _Captured()

    def wrap_socket(context, sock, *args, server_hostname=None, **kwargs):
        seen["server_name"] = server_hostname.lower() if server_hostname else server_hostname
        seen["checks_hostname"] = context.check_hostname
        raise _Captured()

    with _no_ambient_proxy(), \
            patch.object(http.client.HTTPConnection, "putrequest", putrequest), \
            patch.object(http.client.HTTPConnection, "connect", connect), \
            patch.object(ssl.SSLContext, "wrap_socket", wrap_socket):
        yield seen


def _quantastor_wire(config: TrueNASConfig) -> dict:
    client = QuantastorRESTClient(config.model_copy(update={"api_user": "synthetic", "api_password": "synthetic"}))
    with _http_wire() as seen:
        try:
            client._request_json("physicalDiskEnum")
        except _Captured:
            pass
        except (TrueNASAPIError, ValueError) as exc:
            return {"refused": (type(exc).__name__, config.host)}
    return {key: seen.get(key) for key in ("request", "server_name")}


def _bmc_wire(service: SupermicroBMCService) -> dict:
    with _http_wire() as seen:
        try:
            service._redfish_get_json("/redfish/v1/Systems")
        except _Captured:
            pass
        except (TrueNASAPIError, ValueError, http.client.HTTPException) as exc:
            # A base URL no connection can be made to (for example a
            # non-numeric port) is still a distinct destination.
            return {"refused": (type(exc).__name__, service.base_url)}
    return {key: seen.get(key) for key in ("request", "server_name")}


def _truenas_wire(config: TrueNASConfig) -> dict:
    """The URI and TLS server name the WebSocket session hands to the socket layer."""
    client = TrueNASWebsocketClient(config.model_copy(update={"api_key": "synthetic"}))
    seen: dict = {}
    real_connect = truenas_ws_module.connect

    def spy_connect(uri, **kwargs):
        seen["uri"] = uri
        return real_connect(uri, **kwargs)

    async def create_connection(loop, factory, host=None, port=None, **kwargs):
        seen["socket"] = (host.lower() if host else host, port)
        server_name = kwargs.get("server_hostname")
        if kwargs.get("ssl") is not None and server_name is None:
            server_name = host  # asyncio presents the connect host by default
        seen["server_name"] = server_name.lower() if server_name else None
        raise _Captured()

    async def open_session():
        async with client._session():
            pass

    with _no_ambient_proxy(), patch.object(truenas_ws_module, "connect", spy_connect), \
            patch.object(asyncio.BaseEventLoop, "create_connection", create_connection):
        try:
            asyncio.run(open_session())
        except _Captured:
            pass
        except (TrueNASAPIError, ValueError) as exc:
            return {"refused": (type(exc).__name__, config.host)}
    uri = seen.get("uri") or ""
    scheme, _, rest = uri.partition("://")
    path = "/" + rest.split("/", 1)[1] if "/" in rest else ""
    return {"request": (scheme, seen.get("socket"), path), "server_name": seen.get("server_name")}


def _api_wire(service) -> dict:
    client = service.truenas_client
    if isinstance(client, QuantastorRESTClient):
        return _quantastor_wire(client.config)
    return _truenas_wire(client.config)


def _system(
    api_url: str, *, platform: str = "core", label: str | None = None,
    tls_server_name: str | None = None, ssh: SSHConfig | None = None, **truenas_fields,
) -> SystemConfig:
    return SystemConfig(
        id=SYSTEM_ID,
        label=label,
        truenas=TrueNASConfig(platform=platform, host=api_url, tls_server_name=tls_server_name, **truenas_fields),
        ssh=ssh or SSHConfig(enabled=False),
    )


def _ssh(primary: str, *extra: str, **fields) -> SSHConfig:
    return SSHConfig(enabled=True, host=primary, extra_hosts=list(extra), user="synthetic", **fields)


def _settings(root: Path, system: SystemConfig) -> Settings:
    return Settings(
        systems=[system],
        default_system_id=system.id,
        paths=PathConfig(**{name: str(root / f"{name}.json") for name in PathConfig.model_fields}),
        config_file=str(root / "synthetic-settings.yaml"),
    )


def _slot(serial: str = "SANITIZED-DISK-A") -> SlotView:
    return SlotView(
        slot=0, slot_label="0", row_index=0, column_index=0, enclosure_id=SHELF,
        present=True, state="healthy", device_name="da0", smart_device_names=["da0"],
        serial=serial,
    )


class _QuietInventoryLogs:
    """The failing synthetic collection logs warnings; keep test output readable."""

    def __enter__(self):
        self._logger = logging.getLogger("app.services.inventory")
        self._level = self._logger.level
        self._logger.setLevel(logging.CRITICAL)
        return self

    def __exit__(self, *exc):
        self._logger.setLevel(self._level)
        return False


class PersistedSmartBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.old_system = _system("https://source-a.example.test")
        self.original = InventoryRegistry(_settings(self.root, self.old_system))
        self.predecessor = self.original.get_service(SYSTEM_ID)
        # The row is written the way the predecessor's own SMART read writes it.
        entry = self.predecessor._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=17),
        )
        self.assertIsNotNone(entry)
        self.original.slot_detail_store.save_entries([entry])

    def _reload(self, system: SystemConfig) -> InventoryRegistry:
        successor = InventoryRegistry(_settings(self.root, system), previous=self.original)
        # The store is reused across the reload; that is what makes this a risk.
        self.assertIs(successor.slot_detail_store, self.original.slot_detail_store)
        return successor

    def _layout_unavailable_service(self, registry: InventoryRegistry):
        service = registry.get_service(SYSTEM_ID)
        service.truenas_client.fetch_all = AsyncMock(side_effect=TrueNASAPIError("Synthetic source unavailable"))
        with _QuietInventoryLogs():
            bounds = asyncio.run(ensure_read_slot_bounds(0, service, SHELF))
        # The real read-bound resolver admits the cache-only fallback.
        self.assertEqual(bounds, "unavailable")
        return service

    def test_endpoint_change_refuses_predecessor_rows_for_single_and_batch_reads(self) -> None:
        service = self._layout_unavailable_service(self._reload(_system("https://source-b.example.test")))

        single = service.get_cached_slot_smart_summary_without_layout(0, SHELF)
        batch = service.get_cached_slot_smart_summaries_without_layout([0], SHELF)

        self.assertIsNone(single, "another appliance's SMART row was served as this one's")
        self.assertEqual(batch, {})

    def test_same_endpoint_reload_still_serves_its_own_rows(self) -> None:
        # Control: a display-only change keeps the same collection identity.
        service = self._layout_unavailable_service(
            self._reload(_system("https://source-a.example.test", label="Renamed shelf")),
        )

        single = service.get_cached_slot_smart_summary_without_layout(0, SHELF)
        batch = service.get_cached_slot_smart_summaries_without_layout([0], SHELF)

        self.assertIsNotNone(single)
        self.assertEqual(single.power_on_hours, 17)
        self.assertTrue(single.available)
        self.assertEqual(batch[0].power_on_hours, 17)

    def test_platform_change_at_same_endpoint_refuses_predecessor_rows(self) -> None:
        successor = self._reload(_system("https://source-a.example.test", platform="scale"))
        service = successor.get_service(SYSTEM_ID)

        self.assertIsNone(service.get_cached_slot_smart_summary_without_layout(0, SHELF))

    def test_ssh_port_change_at_same_host_refuses_predecessor_rows(self) -> None:
        # A forwarded SSH port on the same host can reach another appliance.
        # The port is a destination while SSH reads are on.
        ssh_on = _system("https://source-a.example.test", ssh=_ssh("source-a.example.test"))
        self.original = InventoryRegistry(_settings(self.root, ssh_on), previous=self.original)
        self.predecessor = self.original.get_service(SYSTEM_ID)
        self.original.slot_detail_store.save_entries([self.predecessor._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=17),
        )])
        self.assertIsNotNone(self.predecessor._build_persisted_smart_summary(_slot()))
        moved = _system("https://source-a.example.test", ssh=_ssh("source-a.example.test", port=2222))
        service = self._reload(moved).get_service(SYSTEM_ID)

        self.assertIsNone(service.get_cached_slot_smart_summary_without_layout(0, SHELF))
        self.assertIsNone(service._build_persisted_smart_summary(_slot()))

    def test_late_predecessor_write_stays_bound_to_the_old_endpoint(self) -> None:
        successor = self._reload(_system("https://source-b.example.test"))
        service = self._layout_unavailable_service(successor)
        # A SMART read the predecessor started before the reload finishes now.
        late = self.predecessor._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=23),
        )
        self.assertIsNotNone(late)
        successor.slot_detail_store.save_entries([late])

        self.assertIsNone(service.get_cached_slot_smart_summary_without_layout(0, SHELF))
        self.assertEqual(service.get_cached_slot_smart_summaries_without_layout([0], SHELF), {})

    def test_successor_row_is_served_and_predecessor_cannot_read_it(self) -> None:
        successor = self._reload(_system("https://source-b.example.test"))
        service = self._layout_unavailable_service(successor)
        own = service._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=321),
        )
        self.assertIsNotNone(own)
        successor.slot_detail_store.save_entries([own])

        served = service.get_cached_slot_smart_summary_without_layout(0, SHELF)
        self.assertIsNotNone(served)
        self.assertEqual(served.power_on_hours, 321)
        self.assertIsNone(self.predecessor.get_cached_slot_smart_summary_without_layout(0, SHELF))

    def test_layout_readers_refuse_predecessor_rows_for_the_same_disk(self) -> None:
        # Readers that hold a live slot view also gate on the binding, not only
        # on the disk identity: the row is the predecessor's observation.
        service = self._reload(_system("https://source-b.example.test")).get_service(SYSTEM_ID)
        live = _slot()

        self.assertIsNone(service._build_persisted_smart_summary(live))
        self.assertIsNone(service._previous_slot_detail_entry(live))
        rebuilt = service._build_slot_detail_entry(live, smart_summary=None)
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.smart_fields, {}, "predecessor SMART was carried into a successor row")

    def test_layout_readers_still_serve_own_rows(self) -> None:
        # Control for the layout readers on an unchanged endpoint.
        service = self._reload(_system("https://source-a.example.test", label="Renamed shelf")).get_service(SYSTEM_ID)

        persisted = service._build_persisted_smart_summary(_slot())
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.power_on_hours, 17)
        rebuilt = service._build_slot_detail_entry(_slot(), smart_summary=None)
        self.assertEqual(rebuilt.smart_fields.get("power_on_hours"), 17)
        self.assertTrue(rebuilt.smart_stale)

    def test_unbound_row_is_not_served(self) -> None:
        # A row with no recorded collection identity cannot show which
        # appliance wrote it, so it is refused rather than guessed.
        store = self.original.slot_detail_store
        store.save_entries([SlotDetailCacheEntry(
            system_id=SYSTEM_ID, enclosure_id=SHELF, slot=0, identifiers=["sanitized-disk-a", "da0"],
            slot_fields={"serial": "SANITIZED-DISK-A"},
            smart_fields={"available": True, "power_on_hours": 5},
        )])
        service = self._layout_unavailable_service(
            self._reload(_system("https://source-a.example.test", label="Renamed shelf")),
        )

        self.assertIsNone(service.get_cached_slot_smart_summary_without_layout(0, SHELF))
        self.assertIsNone(service._build_persisted_smart_summary(_slot()))


class PersistedSmartDestinationTests(unittest.TestCase):
    """The binding follows the destination a read would use, not a set of names.

    A row is refused when the primary SSH host changes roles with an extra host
    or when the effective TLS server name changes; it is kept for edits that do
    not move the read (credentials, timeouts, an equivalent server name).
    """

    API = "https://api.example.test"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _start(self, system: SystemConfig, *, slot: SlotView | None = None) -> None:
        self.original = InventoryRegistry(_settings(self.root, system))
        self.predecessor = self.original.get_service(SYSTEM_ID)
        entry = self.predecessor._build_slot_detail_entry(
            slot or _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=17),
        )
        self.assertIsNotNone(entry)
        self.original.slot_detail_store.save_entries([entry])

    def _reload_unavailable(self, system: SystemConfig):
        successor = InventoryRegistry(_settings(self.root, system), previous=self.original)
        self.assertIs(successor.slot_detail_store, self.original.slot_detail_store)
        service = successor.get_service(SYSTEM_ID)
        failure = TrueNASAPIError("Synthetic source unavailable")
        service.truenas_client.fetch_all = AsyncMock(side_effect=failure)
        service.ssh_probe.run_commands = AsyncMock(side_effect=failure)
        service.ssh_probe.run_planned_commands = AsyncMock(side_effect=failure)
        if service.bmc_service is not None:
            service.bmc_service.fetch_inventory = Mock(side_effect=failure)
        with _QuietInventoryLogs():
            bounds = asyncio.run(ensure_read_slot_bounds(0, service, SHELF))
        self.assertEqual(bounds, "unavailable")
        return successor, service

    def _assert_refused(self, service) -> None:
        self.assertIsNone(
            service.get_cached_slot_smart_summary_without_layout(0, SHELF),
            "a row written for another destination was served",
        )
        self.assertEqual(service.get_cached_slot_smart_summaries_without_layout([0], SHELF), {})
        self.assertIsNone(service._build_persisted_smart_summary(_slot()))

    def _assert_served(self, service) -> None:
        single = service.get_cached_slot_smart_summary_without_layout(0, SHELF)
        self.assertIsNotNone(single, "an unchanged destination lost its own row")
        self.assertEqual(single.power_on_hours, 17)
        self.assertEqual(service.get_cached_slot_smart_summaries_without_layout([0], SHELF)[0].power_on_hours, 17)

    def test_primary_and_extra_ssh_host_swap_refuses_predecessor_rows(self) -> None:
        self._start(_system(self.API, ssh=_ssh("source-a.example.test", "source-b.example.test")))
        swapped = _system(self.API, ssh=_ssh("source-b.example.test", "source-a.example.test"))
        successor, service = self._reload_unavailable(swapped)
        self._assert_refused(service)
        self.assertNotEqual(
            slot_detail_collection_binding(swapped), slot_detail_collection_binding(self.predecessor.system),
        )
        # A SMART read the predecessor started before the swap lands afterwards.
        late = self.predecessor._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=23),
        )
        successor.slot_detail_store.save_entries([late])
        self._assert_refused(service)

    def test_unchanged_primary_ssh_host_keeps_rows(self) -> None:
        # Control: the same primary and extra hosts, with the surrounding
        # spaces every read strips before it connects.
        self._start(_system(self.API, ssh=_ssh("source-a.example.test", "source-b.example.test")))
        _, service = self._reload_unavailable(
            _system(self.API, label="Renamed", ssh=_ssh(" source-a.example.test ", " source-b.example.test ")),
        )
        self._assert_served(service)

    def test_ha_node_host_reassignment_refuses_predecessor_rows(self) -> None:
        # Which controller a host answers for decides where a QuantaStor read goes.
        def ha(first: str, second: str) -> SystemConfig:
            return _system(self.API, platform="quantastor", ssh=_ssh(
                "source-a.example.test", ha_enabled=True,
                ha_nodes=[{"system_id": "node-1", "host": first}, {"system_id": "node-2", "host": second}],
            ))
        before = ha("source-b.example.test", "source-c.example.test")
        self.assertNotEqual(
            slot_detail_collection_binding(before),
            slot_detail_collection_binding(ha("source-c.example.test", "source-b.example.test")),
        )
        self.assertEqual(
            slot_detail_collection_binding(before),
            slot_detail_collection_binding(ha(" source-b.example.test ", "source-c.example.test")),
        )

    def test_tls_server_name_change_refuses_predecessor_rows(self) -> None:
        self._start(_system(self.API, tls_server_name="source-a.example.test"))
        moved = _system(self.API, tls_server_name="source-b.example.test")
        _, service = self._reload_unavailable(moved)
        self._assert_refused(service)
        self.assertNotEqual(
            slot_detail_collection_binding(moved), slot_detail_collection_binding(self.predecessor.system),
        )

    def test_equivalent_effective_tls_server_name_keeps_rows(self) -> None:
        # Control: an override naming the URL's own host selects the same server.
        self._start(_system(self.API))
        _, service = self._reload_unavailable(_system(self.API, tls_server_name=" API.example.test "))
        self._assert_served(service)

    def test_tls_server_name_is_ignored_without_tls(self) -> None:
        # A plain-HTTP API sends no server name, so the override moves nothing.
        plain = "http://api.example.test"
        self.assertEqual(
            slot_detail_collection_binding(_system(plain, tls_server_name="source-a.example.test")),
            slot_detail_collection_binding(_system(plain, tls_server_name="source-b.example.test")),
        )

    def test_credential_and_timeout_edits_keep_rows(self) -> None:
        self._start(_system(self.API, ssh=_ssh("source-a.example.test", "source-b.example.test")))
        edited = _system(
            self.API, api_key="synthetic-rotated-key", timeout_seconds=40, verify_ssl=True,
            ssh=_ssh(
                "source-a.example.test", "source-b.example.test",
                password="synthetic-rotated", sudo_password="synthetic-rotated",
                key_path="/synthetic/rotated-key", timeout_seconds=45,
            ),
        )
        self.assertEqual(
            slot_detail_collection_binding(edited), slot_detail_collection_binding(self.predecessor.system),
        )
        _, service = self._reload_unavailable(edited)
        self._assert_served(service)

    def test_stable_field_backfill_refuses_a_mismatched_binding(self) -> None:
        recorded = _slot().model_copy(update={"model": "SANITIZED-MODEL"})
        self._start(_system(self.API, ssh=_ssh("source-a.example.test", "source-b.example.test")), slot=recorded)
        _, moved = self._reload_unavailable(
            _system(self.API, ssh=_ssh("source-b.example.test", "source-a.example.test")),
        )
        live = _slot()
        moved._apply_persisted_slot_details([live])
        self.assertIsNone(live.model, "another destination's stable fields were backfilled")

        # Control: the same destination backfills the missing field.
        own = self.predecessor
        live = _slot()
        own._apply_persisted_slot_details([live])
        self.assertEqual(live.model, "SANITIZED-MODEL")

    def test_identity_unknown_marking_refuses_a_mismatched_binding(self) -> None:
        self._start(_system(self.API, ssh=_ssh("source-a.example.test", "source-b.example.test")))
        _, moved = self._reload_unavailable(
            _system(self.API, ssh=_ssh("source-b.example.test", "source-a.example.test")),
        )
        unknown = _slot().model_copy(update={"serial": None})
        self.assertEqual(moved._slot_identity_state(unknown), "unknown")
        self.assertIsNone(
            moved._build_slot_detail_entry(unknown, smart_summary=None),
            "another destination's row was marked as this one's evidence",
        )

        # Control: the writing destination records the withholding decision.
        marked = self.predecessor._build_slot_detail_entry(unknown, smart_summary=None)
        self.assertIsNotNone(marked)
        self.assertTrue(marked.identity_unknown)
        self.assertEqual(marked.smart_fields.get("power_on_hours"), 17)

    # One input at a time, every other destination input held fixed.

    def _assert_reload_refuses(self, before: SystemConfig, after: SystemConfig) -> None:
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._start(before)
        self.assertNotEqual(slot_detail_collection_binding(after), slot_detail_collection_binding(before))
        _, service = self._reload_unavailable(after)
        self._assert_refused(service)

    def _assert_reload_keeps(self, before: SystemConfig, after: SystemConfig) -> None:
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._start(before)
        self.assertEqual(slot_detail_collection_binding(after), slot_detail_collection_binding(before))
        _, service = self._reload_unavailable(after)
        self._assert_served(service)

    def test_primary_ssh_host_alone_moves_the_binding(self) -> None:
        for platform in ("core", "scale", "quantastor"):
            with self.subTest(platform=platform):
                self._assert_reload_refuses(
                    _system(self.API, platform=platform, ssh=_ssh("source-a.example.test", "source-b.example.test")),
                    _system(self.API, platform=platform, ssh=_ssh("source-c.example.test", "source-b.example.test")),
                )

    def test_extra_ssh_host_alone_moves_the_binding_where_reads_use_extra_hosts(self) -> None:
        # SCALE SES discovery and QuantaStor reads try the extra hosts in order.
        before_extras = ("source-b.example.test", "source-c.example.test")
        edits = {
            "added": (*before_extras, "source-d.example.test"),
            "removed": before_extras[:1],
            "replaced": ("source-b.example.test", "source-d.example.test"),
            "reordered": tuple(reversed(before_extras)),
        }
        for platform in ("scale", "quantastor"):
            for name, extras in edits.items():
                with self.subTest(platform=platform, edit=name):
                    self._assert_reload_refuses(
                        _system(self.API, platform=platform, ssh=_ssh("source-a.example.test", *before_extras)),
                        _system(self.API, platform=platform, ssh=_ssh("source-a.example.test", *extras)),
                    )

    def test_extra_ssh_host_edit_keeps_rows_where_reads_use_only_the_primary(self) -> None:
        # CORE and Linux reads go to the primary SSH host only, so an edit of
        # the extra hosts moves no read and the saved data stays.
        before = ("source-b.example.test", "source-c.example.test")
        for extras in (("source-b.example.test", "source-d.example.test"), tuple(reversed(before))):
            with self.subTest(extras=extras):
                self._assert_reload_keeps(
                    _system(self.API, ssh=_ssh("source-a.example.test", *before)),
                    _system(self.API, ssh=_ssh("source-a.example.test", *extras)),
                )
                self.assertEqual(
                    slot_detail_collection_binding(_system(self.API, platform="linux", ssh=_ssh("a.example.test", *before))),
                    slot_detail_collection_binding(_system(self.API, platform="linux", ssh=_ssh("a.example.test", *extras))),
                )

    def test_ha_system_id_alone_moves_the_binding(self) -> None:
        # A read for one controller goes to the host configured for its ID.
        def ha(node_id: str) -> SystemConfig:
            return _system(self.API, platform="quantastor", ssh=_ssh(
                "source-a.example.test", ha_enabled=True,
                ha_nodes=[{"system_id": node_id, "host": "source-b.example.test"}],
            ))

        self._assert_reload_refuses(ha("node-1"), ha("node-2"))

    def test_ssh_port_edit_keeps_rows_while_ssh_reads_are_off(self) -> None:
        # With SSH reads off, the port moves no read.
        self._assert_reload_keeps(
            _system(self.API, ssh=SSHConfig(enabled=False, host="source-a.example.test")),
            _system(self.API, ssh=SSHConfig(enabled=False, host="source-a.example.test", port=2222)),
        )

    def test_bmc_destination_is_bound_only_while_the_bmc_is_read(self) -> None:
        # BMC drive data (block sizes) can enter the persisted SMART fields, so
        # an enabled BMC's endpoint is a destination like any other.
        def bmc(host: str, *, enabled: bool, platform: str = "core") -> SystemConfig:
            return _system(self.API, platform=platform, ssh=_ssh("source-a.example.test")).model_copy(
                update={"bmc": BMCConfig(enabled=enabled, host=host, username="synthetic")},
            )

        self.assertNotEqual(
            slot_detail_collection_binding(bmc("bmc-a.example.test", enabled=True, platform="esxi")),
            slot_detail_collection_binding(bmc("bmc-b.example.test", enabled=True, platform="esxi")),
        )

        self._assert_reload_refuses(
            bmc("bmc-a.example.test", enabled=True), bmc("bmc-b.example.test", enabled=True),
        )
        self._assert_reload_refuses(
            bmc("bmc-a.example.test", enabled=False), bmc("bmc-a.example.test", enabled=True),
        )
        # Control: a BMC that is not read moves nothing.
        self._assert_reload_keeps(
            bmc("bmc-a.example.test", enabled=False), bmc("bmc-b.example.test", enabled=False),
        )


CACHED_UNAVAILABLE = "Cached SMART data is unavailable while the inventory layout cannot be resolved."


def _route_endpoint(router, path: str, method: str):
    return next(
        route.endpoint for route in router.routes
        if getattr(route, "path", "") == path and method in (getattr(route, "methods", None) or set())
    )


class _SmartRouteCase(unittest.TestCase):
    """A saved row, a registry reload and both registered physical SMART routes."""

    API = "https://api.example.test"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.router = app_routes.build_router()
        self.single = _route_endpoint(self.router, "/api/slots/{slot}/smart", "GET")
        self.batch = _route_endpoint(self.router, "/api/slots/smart-batch", "POST")

    def _start(self, system: SystemConfig) -> None:
        self.original = InventoryRegistry(_settings(self.root, system))
        self.predecessor = self.original.get_service(SYSTEM_ID)
        self.original.slot_detail_store.save_entries([self.predecessor._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=17),
        )])

    def _reload(self, system: SystemConfig) -> InventoryRegistry:
        successor = InventoryRegistry(_settings(self.root, system), previous=self.original)
        service = successor.get_service(SYSTEM_ID)
        failure = TrueNASAPIError("Synthetic source unavailable")
        service.truenas_client.fetch_all = AsyncMock(side_effect=failure)
        service.ssh_probe.run_commands = AsyncMock(side_effect=failure)
        service.ssh_probe.run_planned_commands = AsyncMock(side_effect=failure)
        return successor

    def _routes(self, registry: InventoryRegistry) -> tuple[SmartSummaryView, SmartSummaryView]:
        """The single and batch answers for slot 0, each after checking the layout was unavailable."""
        with patch.object(app_routes, "get_inventory_registry", return_value=registry), _QuietInventoryLogs():
            single = asyncio.run(self.single(slot=0, system_id=SYSTEM_ID, enclosure_id=SHELF, fresh=False))
            batch = asyncio.run(self.batch(
                payload=SmartBatchRequest(slots=[0]), system_id=SYSTEM_ID, enclosure_id=SHELF, fresh=False,
            ))
        self.assertEqual(single.layout_bounds, "unavailable")
        self.assertEqual(batch.layout_bounds, "unavailable")
        self.assertEqual([item.slot for item in batch.summaries], [0])
        return single, batch.summaries[0].summary

    def _assert_routes_refuse(self, registry: InventoryRegistry) -> None:
        for answer in self._routes(registry):
            self.assertFalse(answer.available, "the other destination's saved SMART was served")
            self.assertIsNone(answer.power_on_hours)
            self.assertEqual(answer.message, CACHED_UNAVAILABLE)


class QuantastorHaModeTests(_SmartRouteCase):
    """The QuantaStor HA mode decides which SSH hosts a SMART read uses.

    With no HA node configured, turning HA mode on drops the API host from
    the SSH hosts, so the same host list reaches another machine first.
    """

    def _system(self, *, ha_enabled: bool, ha_nodes: list[dict] | None = None, label: str | None = None):
        return _system(self.API, platform="quantastor", label=label, ssh=_ssh(
            "api.example.test", "source-b.example.test", ha_enabled=ha_enabled, ha_nodes=ha_nodes or [],
        ))

    def test_ha_mode_toggle_moves_the_read_and_the_binding(self) -> None:
        for before, after in ((False, True), (True, False)):
            with self.subTest(ha_enabled_before=before):
                self._tmp.cleanup()
                self._tmp = tempfile.TemporaryDirectory()
                self.root = Path(self._tmp.name)
                self._start(self._system(ha_enabled=before))
                successor = self._reload(self._system(ha_enabled=after))
                service = successor.get_service(SYSTEM_ID)
                # The read really moves: the API host is tried first only without HA mode.
                hosts = {
                    False: ["api.example.test", "source-b.example.test"],
                    True: ["source-b.example.test"],
                }
                self.assertEqual(self.predecessor._build_quantastor_preferred_hosts(_slot()), hosts[before])
                self.assertEqual(service._build_quantastor_preferred_hosts(_slot()), hosts[after])
                self._assert_routes_refuse(successor)
                self.assertNotEqual(
                    slot_detail_collection_binding(service.system),
                    slot_detail_collection_binding(self.predecessor.system),
                )
                # A SMART read the predecessor started before the reload lands now.
                successor.slot_detail_store.save_entries([self.predecessor._build_slot_detail_entry(
                    _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=23),
                )])
                self._assert_routes_refuse(successor)

    def test_unchanged_ha_mode_keeps_rows_through_both_routes(self) -> None:
        # Control: a reload that keeps the HA mode serves the saved row.
        self._start(self._system(ha_enabled=True))
        successor = self._reload(self._system(ha_enabled=True, label="Renamed"))
        for answer in self._routes(successor):
            self.assertTrue(answer.available)
            self.assertEqual(answer.power_on_hours, 17)

    def test_ha_mode_toggle_with_configured_nodes_keeps_rows(self) -> None:
        # Configured HA nodes already take the API host out of the SSH hosts,
        # so the toggle moves no read and the saved data stays.
        nodes = [{"system_id": "node-1", "host": "source-c.example.test"}]
        self._start(self._system(ha_enabled=False, ha_nodes=nodes))
        successor = self._reload(self._system(ha_enabled=True, ha_nodes=nodes))
        self.assertEqual(
            successor.get_service(SYSTEM_ID)._build_quantastor_preferred_hosts(_slot()),
            self.predecessor._build_quantastor_preferred_hosts(_slot()),
        )
        for answer in self._routes(successor):
            self.assertEqual(answer.power_on_hours, 17)


class TransportDestinationTests(_SmartRouteCase):
    """The binding follows what the API and BMC transports really send.

    That is the whole request base (scheme, host, port and the path prefix,
    with its case kept, and the JSON-RPC version where it is in the URL) and
    the TLS server name the transport presents for the current settings. A
    trust setting moves the binding only where it changes that name.
    """

    def _api(self, api_url: str, *, platform: str, label: str | None = None, **truenas) -> SystemConfig:
        return _system(api_url, platform=platform, label=label, **truenas)

    def _reload(self, system: SystemConfig) -> InventoryRegistry:
        successor = super()._reload(system)
        bmc = successor.get_service(SYSTEM_ID).bmc_service
        if bmc is not None:
            bmc.fetch_inventory = Mock(side_effect=TrueNASAPIError("Synthetic source unavailable"))
        return successor

    def _fresh_root(self) -> None:
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def _assert_moved_through_both_routes(self, before: SystemConfig, after: SystemConfig) -> None:
        self._fresh_root()
        self._start(before)
        successor = self._reload(after)
        self._assert_routes_refuse(successor)
        self.assertNotEqual(slot_detail_collection_binding(after), slot_detail_collection_binding(before))
        # A SMART read the predecessor started before the reload lands now.
        successor.slot_detail_store.save_entries([self.predecessor._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=23),
        )])
        self._assert_routes_refuse(successor)

    def _assert_kept_through_both_routes(self, before: SystemConfig, after: SystemConfig) -> None:
        self._fresh_root()
        self._start(before)
        successor = self._reload(after)
        self.assertEqual(slot_detail_collection_binding(after), slot_detail_collection_binding(before))
        for answer in self._routes(successor):
            self.assertTrue(answer.available, "an unchanged destination lost its own row")
            self.assertEqual(answer.power_on_hours, 17)

    def test_api_path_prefix_change_moves_the_request_and_the_binding(self) -> None:
        # A path-routing proxy in front of several appliances picks the
        # machine by this prefix; both API clients send it.
        for platform in ("core", "quantastor"):
            with self.subTest(platform=platform):
                before = self._api("https://api.example.test/appliance-a", platform=platform)
                after = self._api("https://api.example.test/appliance-b", platform=platform)
                old_wire, new_wire = (
                    _api_wire(InventoryRegistry(_settings(self.root, system)).get_service(SYSTEM_ID))
                    for system in (before, after)
                )
                self.assertIn("/appliance-a/", old_wire["request"][-1])
                self.assertIn("/appliance-b/", new_wire["request"][-1])
                self._assert_moved_through_both_routes(before, after)

    def test_api_path_prefix_case_is_kept(self) -> None:
        for platform in ("core", "quantastor"):
            with self.subTest(platform=platform):
                lower = self._api("https://api.example.test/appliance-a", platform=platform)
                upper = self._api("https://api.example.test/Appliance-A", platform=platform)
                self.assertNotEqual(_api_wire(self._service(lower)), _api_wire(self._service(upper)))
                self.assertNotEqual(slot_detail_collection_binding(lower), slot_detail_collection_binding(upper))

    def test_unchanged_api_request_keeps_rows_through_both_routes(self) -> None:
        # Control: a trailing slash is dropped by the URL builders, so the
        # same request goes out and the saved data stays.
        for platform in ("core", "quantastor"):
            with self.subTest(platform=platform):
                before = self._api("https://api.example.test/appliance-a", platform=platform)
                after = self._api("https://api.example.test/appliance-a/", platform=platform, label="Renamed")
                self.assertEqual(_api_wire(self._service(before)), _api_wire(self._service(after)))
                self._assert_kept_through_both_routes(before, after)

    def test_quantastor_verify_ssl_moves_the_server_name_with_an_override(self) -> None:
        # The QuantaStor HTTP transport presents a differing server-name
        # override only while it checks the certificate's host name.
        def system(verify: bool) -> SystemConfig:
            return self._api(
                self.API, platform="quantastor", tls_server_name="source-b.example.test", verify_ssl=verify,
            )

        names = {verify: _api_wire(self._service(system(verify)))["server_name"] for verify in (False, True)}
        self.assertEqual(names, {False: "api.example.test", True: "source-b.example.test"})
        for before, after in ((False, True), (True, False)):
            with self.subTest(verify_ssl_before=before):
                self._assert_moved_through_both_routes(system(before), system(after))

    def test_verify_ssl_that_moves_no_server_name_keeps_rows(self) -> None:
        cases = {
            # No override: the URL host is presented either way.
            "quantastor without an override": lambda verify: self._api(
                self.API, platform="quantastor", verify_ssl=verify,
            ),
            # The WebSocket session presents the override whatever the trust setting.
            "core with an override": lambda verify: self._api(
                self.API, platform="core", tls_server_name="source-b.example.test", verify_ssl=verify,
            ),
        }
        for name, system in cases.items():
            with self.subTest(case=name):
                self.assertEqual(_api_wire(self._service(system(False))), _api_wire(self._service(system(True))))
                self._assert_kept_through_both_routes(system(False), system(True))

    def test_plain_http_sends_no_server_name(self) -> None:
        plain = "http://api.example.test"
        for platform in ("core", "quantastor"):
            with self.subTest(platform=platform):
                variants = [
                    self._api(plain, platform=platform, tls_server_name=name, verify_ssl=verify)
                    for name in (None, "source-a.example.test", "source-b.example.test")
                    for verify in (False, True)
                ]
                wires = [_api_wire(self._service(system)) for system in variants]
                self.assertTrue(all(wire["server_name"] is None for wire in wires), wires)
                self.assertEqual(len({slot_detail_collection_binding(system) for system in variants}), 1)

    def test_bmc_base_path_moves_the_binding(self) -> None:
        def bmc(host: str, label: str | None = None) -> SystemConfig:
            return self._api(self.API, platform="core", label=label).model_copy(
                update={"bmc": BMCConfig(enabled=True, host=host, username="synthetic")},
            )

        before, after = bmc("bmc.example.test/bmc-a"), bmc("bmc.example.test/bmc-b")
        old_wire, new_wire = (_bmc_wire(self._service(system).bmc_service) for system in (before, after))
        self.assertEqual(old_wire["request"][-1], "/bmc-a/redfish/v1/Systems")
        self.assertEqual(new_wire["request"][-1], "/bmc-b/redfish/v1/Systems")
        self._assert_moved_through_both_routes(before, after)
        # Control: the BMC service drops a trailing slash, so nothing moves.
        same = bmc("https://bmc.example.test/bmc-a/", label="Renamed")
        self.assertEqual(_bmc_wire(self._service(same).bmc_service), old_wire)
        self._assert_kept_through_both_routes(before, same)

    def _bmc(self, host: str, label: str | None = None) -> SystemConfig:
        return self._api(self.API, platform="core", label=label).model_copy(
            update={"bmc": BMCConfig(enabled=True, host=host, username="synthetic")},
        )

    def test_bmc_query_moves_the_request_and_the_binding(self) -> None:
        # The BMC service keeps a query in its base URL and appends the
        # request path to it as text, so a query-routing gateway in front of
        # several BMCs is told which one by the query.
        gateway = "https://bmc.example.test/gateway?appliance={}&path="
        before, after = self._bmc(gateway.format("a")), self._bmc(gateway.format("b"))
        old_wire, new_wire = (_bmc_wire(self._service(system).bmc_service) for system in (before, after))
        self.assertEqual(old_wire["request"][-1], "/gateway?appliance=a&path=/redfish/v1/Systems")
        self.assertEqual(new_wire["request"][-1], "/gateway?appliance=b&path=/redfish/v1/Systems")
        self._assert_moved_through_both_routes(before, after)
        # Control: the same query, a reload that renames only, keeps the row.
        same = self._bmc(gateway.format("a"), label="Renamed")
        self.assertEqual(_bmc_wire(self._service(same).bmc_service), old_wire)
        self._assert_kept_through_both_routes(before, same)

    def test_bmc_fragment_is_bound_only_by_what_it_does_to_the_request(self) -> None:
        # urllib drops the fragment before sending. A fragment in the base
        # URL also swallows the request path appended after it, so only the
        # base path goes out.
        def wire(host: str):
            return _bmc_wire(self._service(self._bmc(host)).bmc_service)["request"]

        def binding(host: str) -> str:
            return slot_detail_collection_binding(self._bmc(host))

        self.assertEqual(wire("bmc.example.test/gw#a")[-1], "/gw")
        self.assertEqual(wire("bmc.example.test/gw#a"), wire("bmc.example.test/gw#b"))
        self.assertEqual(binding("bmc.example.test/gw#a"), binding("bmc.example.test/gw#b"))
        self.assertNotEqual(wire("bmc.example.test/gw"), wire("bmc.example.test/gw#a"))
        self.assertNotEqual(binding("bmc.example.test/gw"), binding("bmc.example.test/gw#a"))

    def test_api_query_the_url_builder_drops_keeps_rows(self) -> None:
        # The API clients build their request base without the configured
        # URL's query, so a query-only edit sends the same request.
        for platform in ("core", "quantastor"):
            with self.subTest(platform=platform):
                before = self._api("https://api.example.test/appliance-a?route=a", platform=platform)
                after = self._api("https://api.example.test/appliance-a?route=b", platform=platform)
                old_wire = _api_wire(self._service(before))
                self.assertNotIn("route=", old_wire["request"][-1])
                self.assertEqual(old_wire, _api_wire(self._service(after)))
                self._assert_kept_through_both_routes(before, after)

    def test_bmc_binding_without_a_query_is_unchanged_by_query_support(self) -> None:
        # Rows saved with the earlier form of the binding stay valid for a
        # BMC URL without a query: these digests were taken before the query
        # was bound, and a query-less base is still (scheme, host, path).
        expected = {
            "bmc.example.test/bmc-a": (("https", "bmc.example.test", "/bmc-a"), "sha256:bf75553e80e0a2138d45f370bc4ef27f"),
            "https://bmc.example.test:8443": (("https", "bmc.example.test:8443", ""), "sha256:46951f8569ea41f202049fd5293e5742"),
        }
        for host, (resource, digest) in expected.items():
            with self.subTest(host=host):
                system = SystemConfig.model_validate({
                    "id": SYSTEM_ID, "truenas": {"platform": "core", "host": self.API}, "ssh": {"enabled": False},
                    "bmc": {"enabled": True, "host": host, "username": "synthetic"},
                })
                self.assertEqual(inventory_module.collection_destination_plan(system).bmc_resource, resource)
                self.assertEqual(slot_detail_collection_binding(system), digest)

    def test_jsonrpc_version_moves_the_binding_only_where_it_is_in_the_url(self) -> None:
        def scale(dialect: str, version: str) -> SystemConfig:
            return self._api(self.API, platform="scale", api_dialect=dialect, api_version=version)

        cases = {
            "jsonrpc version": (scale("jsonrpc", "current"), scale("jsonrpc", "v25.10.0"), True),
            "ddp version": (scale("ddp", "current"), scale("ddp", "v25.10.0"), False),
            "dialect": (scale("ddp", "current"), scale("jsonrpc", "current"), True),
        }
        for name, (before, after, moves) in cases.items():
            with self.subTest(case=name):
                wire_moved = _api_wire(self._service(before)) != _api_wire(self._service(after))
                self.assertEqual(wire_moved, moves)
                self.assertEqual(
                    slot_detail_collection_binding(before) != slot_detail_collection_binding(after), moves,
                )

    def _service(self, system: SystemConfig):
        return InventoryRegistry(_settings(self.root, system)).get_service(SYSTEM_ID)


class RuntimeRoutingTests(unittest.TestCase):
    """Which node of one configured system answers a read is not a destination change.

    The binding protects against configuration pointing a system at another
    machine. The nodes of one QuantaStor HA pair read the same shelves, and a
    row with a live slot view is also checked against the disk's identity, so
    a learned move of the reader from one node to the other keeps the saved
    row of a strongly identified disk.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        system = _system("https://api.example.test", platform="quantastor", ssh=_ssh(
            "api.example.test", ha_enabled=True, ha_nodes=[
                {"system_id": "node-1", "host": "source-a.example.test"},
                {"system_id": "node-2", "host": "source-b.example.test"},
            ],
        ))
        self.registry = InventoryRegistry(_settings(Path(self._tmp.name), system))
        self.service = self.registry.get_service(SYSTEM_ID)

    @staticmethod
    def _owned_by(node: str, serial: str = "SANITIZED-DISK-A") -> SlotView:
        return _slot(serial).model_copy(update={"raw_status": {"quantastor_pool_owner_system_id": node}})

    def test_learned_move_between_ha_nodes_keeps_the_saved_row(self) -> None:
        binding = slot_detail_collection_binding(self.service.system)
        on_a = self._owned_by("node-1")
        self.assertEqual(self.service._build_quantastor_preferred_hosts(on_a)[0], "source-a.example.test")
        self.registry.slot_detail_store.save_entries([self.service._build_slot_detail_entry(
            on_a, smart_summary=SmartSummaryView(available=True, power_on_hours=17),
        )])

        # The pool fails over: the same disk is now read through node B, and
        # the remembered good host follows it.
        on_b = self._owned_by("node-2")
        self.service._quantastor_preferred_ses_host = "source-b.example.test"
        self.assertEqual(self.service._build_quantastor_preferred_hosts(on_b)[0], "source-b.example.test")
        self.assertEqual(slot_detail_collection_binding(self.service.system), binding)

        persisted = self.service._build_persisted_smart_summary(on_b)
        self.assertIsNotNone(persisted, "a failover inside one system withheld the saved SMART")
        self.assertEqual(persisted.power_on_hours, 17)
        cached = self.service.get_cached_slot_smart_summary_without_layout(0, SHELF)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.power_on_hours, 17)

        # Control: the disk-identity check still applies to the live slot view.
        self.assertIsNone(self.service._build_persisted_smart_summary(self._owned_by("node-2", "SANITIZED-DISK-B")))


# ---------------------------------------------------------------------------
# Every field of the system configuration model, one at a time.
#
# The cases are generated from ``SystemConfig`` itself, so a field added later
# is swept without editing this test. For each baseline configuration, each
# field is changed alone; the test then compares what the registry-built
# readers would contact (the API and BMC requests and TLS server names as
# their transports send them, SSH hosts in try order with their port and
# controller association) with the destination plan and the binding.

# Learned hosts sit between the configured ones; reading with them shows
# where each configured host is tried relative to them.
_STANDALONE_RAW = TrueNASRawData(
    enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[],
    cli_network_ports=[{"storageSystemId": "node-1", "ipAddress": "192.0.2.77", "gateway": "192.0.2.1"}],
)
_CLUSTER_RAW = TrueNASRawData(
    enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[],
    systems=[
        {"id": "node-1", "storageSystemClusterId": "cluster-1", "sshHost": "learned-1.example.test"},
        {"id": "node-2", "storageSystemClusterId": "cluster-1", "sshHost": "learned-2.example.test"},
    ],
)


def _view() -> dict:
    return {
        "id": "synthetic-view", "label": "Synthetic view", "kind": "manual", "template_id": "fat-twin-rear-2",
        "binding": {"mode": "serial", "serials": ["SANITIZED-DISK-A"]},
    }


def _baselines() -> dict[str, SystemConfig]:
    nodes = [
        {"system_id": "node-1", "label": "Synthetic left", "host": "source-c.example.test"},
        {"system_id": "node-2", "label": "Synthetic right", "host": "source-d.example.test"},
    ]

    def make(platform: str, *, ssh: dict | None = None, bmc: dict | None = None, **truenas) -> SystemConfig:
        return SystemConfig.model_validate({
            "id": SYSTEM_ID, "label": "Synthetic", "default_profile_id": None,
            "enclosure_profiles": {"synthetic-enclosure": "synthetic-profile"},
            "storage_views": [_view()], "view_order": ["view:synthetic-view"],
            "truenas": {"platform": platform, "host": "https://api.example.test", **truenas},
            "ssh": ssh if ssh is not None else {"enabled": False},
            "bmc": bmc or {"enabled": False},
        })

    ssh = {"enabled": True, "host": "source-a.example.test",
           "extra_hosts": ["source-b.example.test", "source-e.example.test"], "user": "synthetic", "ha_nodes": nodes}
    return {
        "core": make("core", ssh=ssh, tls_server_name="sni.example.test"),
        # A path prefix (behind a path-routing proxy) is part of every request.
        "core-path-prefix": make("core", host="https://api.example.test/appliance-a", ssh={**ssh, "enabled": False}),
        "core-plain-http": make("core", host="http://api.example.test", ssh=ssh),
        "core-ssh-off": make("core", ssh={**ssh, "enabled": False}),
        "scale": make("scale", ssh=ssh, api_dialect="jsonrpc"),
        "linux": make("linux", ssh=ssh),
        "esxi-bmc": make(
            "esxi", ssh=ssh, bmc={"enabled": True, "host": "bmc.example.test/bmc-a", "username": "synthetic"},
        ),
        "ipmi-bmc": make("ipmi", bmc={"enabled": True, "host": "https://bmc.example.test:8443"}),
        # A query-routing BMC gateway (the BMC service sends the query), and
        # an API URL whose query the API client drops.
        "core-bmc-query": make(
            "core", host="https://api.example.test/appliance-a?route=a", ssh={**ssh, "enabled": False},
            bmc={"enabled": True, "host": "https://bmc.example.test/gateway?appliance=a&path=", "username": "synthetic"},
        ),
        "quantastor-standalone": make(
            "quantastor", ssh={**ssh, "host": "api.example.test", "ha_nodes": [], "ha_enabled": False},
        ),
        "quantastor-ha": make("quantastor", ssh={**ssh, "ha_enabled": True}),
        # A server-name override the HTTP transport presents only while it
        # checks host names, and a path prefix.
        "quantastor-sni-path": make(
            "quantastor", host="https://api.example.test/appliance-a", tls_server_name="sni.example.test",
            ssh={"enabled": False},
        ),
    }


# Fields no mechanical change can be made to, with the reason. This is not a
# place to park a field whose case fails: every entry is checked to really
# have no accepted change.
_UNMUTABLE: dict[str, str] = {}


def _string_candidates(value: str | None, leaf: str) -> list[str]:
    # Tried in order; the first one the model accepts and keeps is used. The
    # later ones satisfy validators of specific fields (a pinned API version,
    # a view-order entry, an M.2 length).
    out = [f"{value}-mutated"] if value else []
    return [*out, f"mutated-{leaf.replace('_', '-')}.example.test", "v99.1.0", "view:mutated", "2280"]


def _strip_optional(annotation):
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        inner = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return inner[0] if len(inner) == 1 else annotation, True
    return annotation, False


def _is_model(annotation) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _scalar_candidates(annotation, value, leaf: str) -> list:
    annotation, optional = _strip_optional(annotation)
    origin = typing.get_origin(annotation)
    out: list = [None] if optional and value is not None else []
    if origin is typing.Literal:
        return out + [choice for choice in typing.get_args(annotation) if choice != value]
    if annotation is bool:
        return out + [not value]
    if annotation is int:
        return out + ([value + 1, value - 1] if isinstance(value, int) else []) + [1, 4]
    if annotation is float:
        return out + ([value + 1.5] if isinstance(value, (int, float)) else []) + [2.5]
    if annotation is str or annotation is typing.Any:
        return out + _string_candidates(value, leaf)
    if origin is dict:
        key_type, value_type = typing.get_args(annotation)
        key = 0 if key_type is int else f"mutated-{leaf}"
        return out + [{**(value or {}), key: item} for item in _scalar_candidates(value_type, None, leaf)[:3]]
    if _is_model(annotation):
        return out + ([_new_model(annotation, leaf)] if value is None else [])
    return out


def _new_model(model: type[BaseModel], leaf: str) -> dict:
    """A new element with every field set, so normalisation cannot drop it."""
    data: dict = {}
    for name, info in model.model_fields.items():
        annotation, _ = _strip_optional(info.annotation)
        if _is_model(annotation) or typing.get_origin(annotation) in (list, dict):
            continue
        candidates = _scalar_candidates(annotation, None, f"{leaf}-{name}")
        if candidates:
            data[name] = candidates[0]
    if model.__name__ == "StorageViewConfig":
        data.update(kind="manual", template_id="fat-twin-rear-2")
    return data


def _field_cases(model: type[BaseModel], data: dict, path: tuple = ()):
    """Yield (path, operation, candidate values) for every field reachable in ``data``."""
    for name, info in model.model_fields.items():
        here = (*path, name)
        annotation, optional = _strip_optional(info.annotation)
        value = data.get(name)
        origin = typing.get_origin(annotation)
        if _is_model(annotation) and isinstance(value, dict):
            yield from _field_cases(annotation, value, here)
            if optional:
                yield here, "unset", [None]
            continue
        if origin is list:
            (element,) = typing.get_args(annotation)
            items = list(value or [])
            new_items = (
                [_new_model(element, name)] if _is_model(element)
                else _scalar_candidates(element, None, name)
            )
            yield here, "append", [[*items, item] for item in new_items]
            if items:
                yield here, "drop-last", [items[:-1]]
            if len(items) > 1:
                yield here, "reverse", [list(reversed(items))]
            for index, item in enumerate(items):
                if _is_model(element) and isinstance(item, dict):
                    yield from _field_cases(element, item, (*here, index))
                elif not _is_model(element):
                    yield (*here, index), "change", _scalar_candidates(element, item, name)
            continue
        yield here, "change", _scalar_candidates(info.annotation, value, name)


def _with(data: dict, path: tuple, value) -> dict:
    changed = copy.deepcopy(data)
    cursor = changed
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    return changed


def _mutations(base: SystemConfig):
    """Yield (dotted path, operation, mutated config); paths with no accepted change yield None."""
    data = base.model_dump()
    for path, operation, candidates in _field_cases(SystemConfig, data):
        dotted = ".".join(str(part) for part in path)
        accepted = None
        for candidate in candidates:
            try:
                mutated = SystemConfig.model_validate(_with(data, path, candidate))
            except (ValidationError, ValueError, TypeError):
                continue
            if mutated != base:
                accepted = mutated
                break
        yield dotted, operation, accepted


def _observed_destinations(service, system_ids: set[str]) -> dict:
    """What the registry-built readers of ``service`` would contact.

    The API and BMC entries are what their transports really send (see
    `_api_wire`), not anything derived from the destination plan.
    """
    system = service.system
    bmc = service.bmc_service
    observed: dict = {
        # The platform picks which readers run at all.
        "platform": system.truenas.platform,
        "api": _api_wire(service),
        "bmc": _bmc_wire(bmc) if bmc is not None and bmc.config.enabled and bmc.base_url else None,
    }
    if system.ssh.enabled:
        probe = service.ssh_probe
        observed["ssh_primary"] = (normalize_text(probe.config.host), probe.config.port)
        # Each reader below runs only on its own platform (the inventory
        # collection gates SCALE SES and the QuantaStor overlays by platform).
        if system.truenas.platform == "scale":
            observed["scale_hosts"] = service._build_scale_ssh_hosts()
        if system.truenas.platform == "quantastor":
            observed["quantastor_hosts"] = [
                service._build_configured_quantastor_hosts(raw_data=raw)
                for raw in (None, _STANDALONE_RAW, _CLUSTER_RAW)
            ]
            observed["quantastor_node_hosts"] = {
                system_id: service._configured_quantastor_host_for_system(system_id)
                for system_id in sorted(system_ids)
            }
    return observed


class DestinationPlanFieldSweepTests(unittest.TestCase):
    """No single configuration edit moves a read without moving the binding, or the reverse.

    This covers configured routing. Which node of one system answers at
    runtime is not a configuration edit; see `RuntimeRoutingTests`.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.cases: list[tuple[str, str, str, SystemConfig, SystemConfig | None]] = []
        for baseline_name, base in _baselines().items():
            for dotted, operation, mutated in _mutations(base):
                cls.cases.append((baseline_name, dotted, operation, base, mutated))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _service(self, system: SystemConfig):
        return InventoryRegistry(_settings(self.root, system)).get_service(system.id)

    @staticmethod
    def _system_ids(*systems: SystemConfig) -> set[str]:
        return {"node-1", "node-2", "node-unconfigured"} | {
            node.system_id for system in systems for node in system.ssh.ha_nodes if node.system_id
        }

    def test_every_field_was_swept(self) -> None:
        unmutable = {dotted for _b, dotted, _op, _base, mutated in self.cases if mutated is None}
        self.assertEqual(
            unmutable, set(_UNMUTABLE),
            "each field must be changed by the sweep or listed with a reason",
        )
        swept = {dotted for _b, dotted, _op, _base, _m in self.cases}
        for name in SystemConfig.model_fields:
            self.assertTrue(any(dotted.split(".")[0] == name for dotted in swept), name)

    def test_a_field_that_moves_a_read_moves_the_binding(self) -> None:
        for baseline_name, dotted, operation, base, mutated in self.cases:
            if mutated is None:
                continue
            with self.subTest(baseline=baseline_name, field=dotted, operation=operation):
                ids = self._system_ids(base, mutated)
                moved = (
                    _observed_destinations(self._service(base), ids)
                    != _observed_destinations(self._service(mutated), ids)
                )
                rebound = slot_detail_collection_binding(base) != slot_detail_collection_binding(mutated)
                self.assertEqual(
                    rebound, moved,
                    "the binding moved without the read" if rebound else "the read moved without the binding",
                )

    def test_binding_moves_exactly_with_the_destination_plan(self) -> None:
        plan_of = inventory_module.collection_destination_plan
        for baseline_name, dotted, operation, base, mutated in self.cases:
            if mutated is None:
                continue
            with self.subTest(baseline=baseline_name, field=dotted, operation=operation):
                ids = self._system_ids(base, mutated)
                plan_changed = plan_of(base) != plan_of(mutated)
                self.assertEqual(
                    slot_detail_collection_binding(base) != slot_detail_collection_binding(mutated), plan_changed,
                )
                self.assertEqual(
                    _observed_destinations(self._service(base), ids)
                    != _observed_destinations(self._service(mutated), ids),
                    plan_changed,
                    "the plan and the readers disagree about whether the read moved",
                )


if __name__ == "__main__":
    unittest.main()
