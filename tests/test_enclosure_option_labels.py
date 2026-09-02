from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import EnclosureOption
from app.services.inventory import InventoryService, disambiguate_enclosure_option_labels
from app.services.mapping_store import MappingStore
from app.services.parsers import parse_ssh_outputs
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
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
