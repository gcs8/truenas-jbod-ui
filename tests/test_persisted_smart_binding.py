"""Persisted slot details belong to the appliance that produced them (#1207).

A configured system ID names a slot in the settings, not an appliance. When a
reload keeps the ID but points it at another endpoint, the slot-detail store is
reused, so its rows must say which collection wrote them and every reader must
refuse rows written for a different one. Synthetic fixtures only: the
appliance client is a mock that always fails, no network is touched.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import PathConfig, Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import SlotView, SmartSummaryView
from app.route_support import ensure_read_slot_bounds
from app.services.inventory import slot_detail_collection_binding
from app.services.inventory_registry import InventoryRegistry
from app.services.slot_detail_store import SlotDetailCacheEntry
from app.services.truenas_ws import TrueNASAPIError

SYSTEM_ID = "synthetic-system"
SHELF = "synthetic-shelf"


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
        moved = _system("https://source-a.example.test").model_copy(
            update={"ssh": SSHConfig(enabled=False, port=2222)},
        )
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
        # Control: the same primary and extra hosts, written differently.
        self._start(_system(self.API, ssh=_ssh("source-a.example.test", "source-b.example.test")))
        _, service = self._reload_unavailable(
            _system(self.API, label="Renamed", ssh=_ssh(" Source-A.example.test ", "SOURCE-B.example.test")),
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
            slot_detail_collection_binding(ha("SOURCE-B.example.test", "source-c.example.test")),
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


if __name__ == "__main__":
    unittest.main()
