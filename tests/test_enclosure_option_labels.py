from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import EnclosureOption, InventorySnapshot, SasFabricAlias
from app.services.inventory import (
    InventoryService,
    disambiguate_enclosure_option_labels,
    finalize_enclosure_option_labels,
)
from app.services.mapping_store import MappingStore
from app.services.parsers import parse_ssh_outputs
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.sas_fabric_alias_store import SasFabricAliasStore
from app.services.truenas_ws import TrueNASRawData


def _option(option_id: str, label: str, **extra) -> EnclosureOption:
    return EnclosureOption(id=option_id, label=label, **extra)


class DisambiguateEnclosureOptionLabelsTests(unittest.TestCase):
    def test_unique_labels_are_untouched(self) -> None:
        options = [
            _option("50050cc11ac013fc", "Dell MD1280 84 Bay"),
            _option("5000c50012345678", "Front 24 Bay"),
        ]
        self.assertEqual(disambiguate_enclosure_option_labels(options), options)

    def test_two_identical_shelves_get_their_id_tail_and_drawers_follow_the_parent(self) -> None:
        # Issue #213: two MD1280s on one host, three names shown twice each.
        options = [
            _option("50050cc11ac013fc::dell-md1280-drawer-top-42", "Dell MD1280 Drawer 1-42 (Top)"),
            _option("50050cc11ac01479::dell-md1280-drawer-top-42", "Dell MD1280 Drawer 1-42 (Top)"),
            _option("50050cc11ac013fc::dell-md1280-drawer-bottom-42", "Dell MD1280 Drawer 43-84 (Bottom)"),
            _option("50050cc11ac01479::dell-md1280-drawer-bottom-42", "Dell MD1280 Drawer 43-84 (Bottom)"),
            _option("50050cc11ac013fc", "Dell MD1280 84 Bay"),
            _option("50050cc11ac01479", "Dell MD1280 84 Bay"),
        ]
        labels = [option.label for option in disambiguate_enclosure_option_labels(options)]
        self.assertEqual(
            labels,
            [
                "Dell MD1280 Drawer 1-42 (Top) [13fc]",
                "Dell MD1280 Drawer 1-42 (Top) [1479]",
                "Dell MD1280 Drawer 43-84 (Bottom) [13fc]",
                "Dell MD1280 Drawer 43-84 (Bottom) [1479]",
                "Dell MD1280 84 Bay [13fc]",
                "Dell MD1280 84 Bay [1479]",
            ],
        )
        self.assertEqual(len(set(labels)), len(labels))

    def test_only_colliding_labels_are_suffixed(self) -> None:
        options = [
            _option("50050cc11ac013fc", "Dell MD1280 84 Bay"),
            _option("50050cc11ac01479", "Dell MD1280 84 Bay"),
            _option("5000c50012345678", "Front 24 Bay"),
        ]
        labels = [option.label for option in disambiguate_enclosure_option_labels(options)]
        self.assertEqual(labels, ["Dell MD1280 84 Bay [13fc]", "Dell MD1280 84 Bay [1479]", "Front 24 Bay"])

    def test_tail_grows_until_the_ids_differ(self) -> None:
        options = [
            _option("5000c500aaaa1234", "Front 24 Bay"),
            _option("5000c500bbbb1234", "Front 24 Bay"),
        ]
        labels = [option.label for option in disambiguate_enclosure_option_labels(options)]
        self.assertEqual(labels, ["Front 24 Bay [a1234]", "Front 24 Bay [b1234]"])

    def test_pass_is_idempotent_and_keeps_ids_and_order(self) -> None:
        options = [
            _option("50050cc11ac013fc", "Dell MD1280 84 Bay", slot_count=84),
            _option("50050cc11ac01479", "Dell MD1280 84 Bay", slot_count=84),
        ]
        once = disambiguate_enclosure_option_labels(options)
        twice = disambiguate_enclosure_option_labels(once)
        self.assertEqual([option.label for option in twice], [option.label for option in once])
        self.assertEqual([option.id for option in twice], [option.id for option in options])
        self.assertEqual([option.slot_count for option in twice], [84, 84])
        # The inputs are not mutated.
        self.assertEqual(options[0].label, "Dell MD1280 84 Bay")


class EnclosureAliasOptionLabelTests(unittest.TestCase):
    @staticmethod
    def _alias(object_id: str, label: str) -> SasFabricAlias:
        return SasFabricAlias(
            system_id="system-a",
            object_id=object_id,
            object_kind="enclosure",
            label=label,
        )

    def test_alias_replaces_shelf_label_and_prefixes_drawer_descriptors(self) -> None:
        options = [
            _option("50050cc11ac013fc::dell-md1280-drawer-top-42", "Dell MD1280 Drawer 1-42 (Top)"),
            _option("50050cc11ac013fc", "Dell MD1280 84 Bay"),
            _option("50050cc11ac01479", "Dell MD1280 84 Bay"),
        ]

        resolved = finalize_enclosure_option_labels(
            options,
            [self._alias("50050cc11ac013fc", "Archive East")],
        )
        by_id = {option.id: option for option in resolved}

        self.assertEqual(by_id["50050cc11ac013fc"].label, "Archive East")
        self.assertEqual(
            by_id["50050cc11ac013fc::dell-md1280-drawer-top-42"].label,
            "Archive East · Drawer 1-42 (Top)",
        )
        self.assertEqual(by_id["50050cc11ac01479"].label, "Dell MD1280 84 Bay")
        self.assertEqual(by_id["50050cc11ac013fc"].raw_label, "Dell MD1280 84 Bay")
        self.assertEqual(by_id["50050cc11ac013fc"].alias, "Archive East")
        self.assertIsNone(by_id["50050cc11ac01479"].alias)

    def test_aliases_override_id_tails_even_when_aliases_collide(self) -> None:
        options = [
            _option("50050cc11ac013fc", "Dell MD1280 84 Bay"),
            _option("50050cc11ac01479", "Dell MD1280 84 Bay"),
        ]

        resolved = finalize_enclosure_option_labels(
            options,
            [
                self._alias("50050cc11ac013fc", "Archive"),
                self._alias("50050cc11ac01479", "Archive"),
            ],
        )

        self.assertEqual([option.label for option in resolved], ["Archive", "Archive"])
        self.assertEqual([option.raw_label for option in resolved], ["Dell MD1280 84 Bay", "Dell MD1280 84 Bay"])

    def test_save_and_clear_use_base_enclosure_id_and_system_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SasFabricAliasStore(Path(temp_dir) / "sas_fabric_aliases.json")
            service = object.__new__(InventoryService)
            service.system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="scale"))
            service.sas_fabric_alias_store = store
            service.invalidate_physical_enclosure_snapshot_cache = Mock()

            saved = service.save_sas_fabric_alias(
                object_id="50050cc11ac013fc::dell-md1280-drawer-top-42",
                object_kind="enclosure",
                label="Archive East",
                selected_enclosure_id="50050cc11ac013fc::dell-md1280-drawer-top-42",
                scope="enclosure",
            )
            aliases = store.list_aliases("system-a")

            self.assertTrue(saved["ok"])
            self.assertEqual(len(aliases), 1)
            self.assertEqual(aliases[0].object_id, "50050cc11ac013fc")
            self.assertEqual(aliases[0].object_kind, "enclosure")
            self.assertIsNone(aliases[0].enclosure_id)

            cleared = service.save_sas_fabric_alias(
                object_id="50050cc11ac013fc::dell-md1280-drawer-bottom-42",
                object_kind="enclosure",
                label="",
                selected_enclosure_id="50050cc11ac013fc::dell-md1280-drawer-bottom-42",
                scope="enclosure",
            )

            self.assertTrue(cleared["cleared"])
            self.assertEqual(store.list_aliases("system-a"), [])

    def test_unreadable_alias_store_falls_back_to_inferred_labels_and_tails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sas_fabric_aliases.json"
            path.write_text("{}", encoding="utf-8")
            service = object.__new__(InventoryService)
            service.system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="scale"))
            service.sas_fabric_alias_store = SasFabricAliasStore(path)
            options = [
                _option("50050cc11ac013fc", "Dell MD1280 84 Bay"),
                _option("50050cc11ac01479", "Dell MD1280 84 Bay"),
            ]

            with patch("app.services.sas_fabric_alias_store.Path.open", side_effect=OSError("synthetic unreadable store")):
                resolved = service._finalize_enclosure_options(options)

        self.assertEqual([option.label for option in resolved], ["Dell MD1280 84 Bay [13fc]", "Dell MD1280 84 Bay [1479]"])
        self.assertEqual([option.raw_label for option in resolved], ["Dell MD1280 84 Bay", "Dell MD1280 84 Bay"])

    def test_enclosure_alias_save_and_clear_invalidate_default_base_and_drawer_cache_keys(self) -> None:
        settings = Settings()
        with tempfile.TemporaryDirectory() as temp_dir:
            service = InventoryService(
                settings,
                SystemConfig(id="system-a", truenas=TrueNASConfig(platform="scale")),
                AsyncMock(),
                AsyncMock(),
                None,
                MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
                ProfileRegistry(settings),
                SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
                SasFabricAliasStore(Path(temp_dir) / "sas_fabric_aliases.json"),
            )
            future = datetime.now(timezone.utc) + timedelta(minutes=5)

            def seed_cache() -> None:
                for key in ("__default__", "enc-a", "enc-a::drawer-top", "enc-other"):
                    service._cache[key] = InventorySnapshot(slots=[], refresh_interval_seconds=30)
                    service._cache_until[key] = future

            seed_cache()
            service.save_sas_fabric_alias(
                object_id="enc-a::drawer-top",
                object_kind="enclosure",
                label="Archive East",
                selected_enclosure_id="enc-a::drawer-top",
                scope="system",
            )
            self.assertEqual(set(service._cache), {"enc-other"})
            self.assertEqual(set(service._cache_until), {"enc-other"})

            seed_cache()
            service.save_sas_fabric_alias(
                object_id="enc-a::drawer-bottom",
                object_kind="enclosure",
                label="",
                selected_enclosure_id="enc-a::drawer-bottom",
                scope="system",
            )
            self.assertEqual(set(service._cache), {"enc-other"})
            self.assertEqual(set(service._cache_until), {"enc-other"})


def _md1280_aes_output(logical_id: str) -> str:
    return "\n".join(
        [
            "  DELL      EN-8435A-E6EBD    3535",
            f"  Primary enclosure logical identifier (hex): {logical_id}",
            "Additional element status diagnostic page:",
            "  additional element status descriptor list",
            "    Element type: Array device slot, subenclosure id: 0 [ti=0]",
        ]
        + [
            line
            for slot in (0, 1, 43, 44)
            for line in (
                f"      Element index: {slot}  eiioe=0",
                "        Transport protocol: SAS",
                f"        number of phys: 1, not all phys: 1, device slot number: {slot}",
                "        phy index: 0",
                "          SAS device type: no SAS device attached",
                "          target port for: SATA_device",
                f"          attached SAS address: 0x{logical_id[:-2]}01",
                f"          SAS address: 0x{logical_id[:-2]}{50 + slot:02x}",
            )
        ]
    )


class DummyScaleClient:
    async def fetch_all(self) -> TrueNASRawData:
        return TrueNASRawData(enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[])


class TwoIdenticalShelvesSelectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_md1280_shelves_are_distinguishable_in_the_selector(self) -> None:
        settings = Settings()
        system = SystemConfig(
            id="md1280-pair",
            truenas=TrueNASConfig(platform="scale"),
            ssh=SSHConfig(enabled=True, host="192.0.2.51", user="jbodmap", commands=[]),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            service = InventoryService(
                settings,
                system,
                DummyScaleClient(),
                AsyncMock(),
                None,
                MappingStore(f"{temp_dir}/slot_mappings.json"),
                ProfileRegistry(settings),
                SlotDetailStore(f"{temp_dir}/slot_detail_cache.json"),
            )
            overlay = parse_ssh_outputs(
                {
                    "sudo -n /usr/bin/sg_ses -p aes /dev/sg1": _md1280_aes_output("50050cc11ac013fc"),
                    "sudo -n /usr/bin/sg_ses -p aes /dev/sg76": _md1280_aes_output("50050cc11ac01479"),
                },
                84,
                None,
                None,
            )
            service._tag_ses_overlay(overlay, "192.0.2.51")
            service._fetch_scale_ses_overlay = AsyncMock(return_value=(overlay, []))

            snapshot = await service.get_snapshot()

        labels = [option.label for option in snapshot.enclosures]
        self.assertEqual(len(labels), 6)
        self.assertEqual(len(set(labels)), 6, labels)
        by_id = {option.id: option.label for option in snapshot.enclosures}
        self.assertEqual(by_id["50050cc11ac013fc"], "Dell MD1280 84 Bay [13fc]")
        self.assertEqual(by_id["50050cc11ac01479"], "Dell MD1280 84 Bay [1479]")
        self.assertEqual(
            by_id["50050cc11ac013fc::dell-md1280-drawer-top-42"],
            "Dell MD1280 Drawer 1-42 (Top) [13fc]",
        )
        self.assertEqual(
            by_id["50050cc11ac013fc::dell-md1280-drawer-bottom-42"],
            "Dell MD1280 Drawer 43-84 (Bottom) [13fc]",
        )
        self.assertEqual(
            by_id["50050cc11ac01479::dell-md1280-drawer-bottom-42"],
            "Dell MD1280 Drawer 43-84 (Bottom) [1479]",
        )
        # The page header follows the same label as the selector entry.
        self.assertEqual(snapshot.selected_enclosure_label, by_id[snapshot.selected_enclosure_id])


if __name__ == "__main__":
    unittest.main()
