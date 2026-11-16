"""Nested API slot zero is a real coordinate (issue #1215).

A disk row's nested ``enclosure.slot`` of integer ``0`` is the first bay when
the API numbers bays from zero. It must bind the disk to bay 0 exactly as the
string ``"0"`` does, and must not be read as "missing". Synthetic data only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.config import EnclosureProfileConfig, LayoutConfig, PathConfig, Settings, SystemConfig, TrueNASConfig
from app.services.inventory import InventoryService
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry
from app.services.truenas_ws import TrueNASRawData


class _NoMappings:
    """Mapping store stand-in with no manual mappings, so nothing rescues a lost slot."""

    def load_lookup_snapshot(self) -> dict[str, Any]:
        return {}

    def get_mapping(self, *args: Any, **kwargs: Any) -> None:
        return None

    def has_legacy_only_mapping(self, *args: Any, **kwargs: Any) -> bool:
        return False


class NestedSlotZeroTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _service(self, *, base: int) -> InventoryService:
        root = Path(self._tmp.name)
        profile = EnclosureProfileConfig(
            id="synthetic-two-bay",
            label="Synthetic two bays",
            rows=1,
            columns=2,
            slot_layout=[[0, 1]],
        )
        system = SystemConfig(
            id="synthetic-system",
            label="Synthetic system",
            truenas=TrueNASConfig(platform="scale"),
            default_profile_id=profile.id,
        )
        settings = Settings(
            systems=[system],
            profiles=[profile],
            layout=LayoutConfig(slot_count=2, rows=1, columns=2, api_slot_number_base=base),
            config_file=str(root / "never-read.yaml"),
            paths=PathConfig(**{name: str(root / name) for name in PathConfig.model_fields}),
        )
        return InventoryService(settings, system, None, None, None, _NoMappings(), ProfileRegistry(settings))

    def _bays(self, service: InventoryService, disk: dict[str, Any], *, base: int) -> dict[int, str | None]:
        source = TrueNASRawData(
            enclosures=[
                {
                    "id": "shelf-a",
                    "label": "Synthetic shelf",
                    "elements": [{"slot": base, "status": "OK"}, {"slot": base + 1, "status": "OK"}],
                }
            ],
            disks=[disk],
            pools=[],
            disk_temperatures={},
            smart_test_results=[],
        )
        slots, *_ = service._correlate(source, ParsedSSHData(), [], selected_enclosure_id="shelf-a")
        return {view.slot: view.serial for view in slots}

    @staticmethod
    def _disk(**enclosure: Any) -> dict[str, Any]:
        return {"name": "sda", "serial": "SYNTHETIC-A", "enclosure": {"id": "shelf-a", **enclosure}}

    def test_integer_zero_extracts_bay_zero_with_zero_base(self) -> None:
        service = self._service(base=0)
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot=0)), ("shelf-a", 0))

    def test_integer_zero_binds_disk_to_bay_zero_with_zero_base(self) -> None:
        service = self._service(base=0)
        self.assertEqual(self._bays(service, self._disk(slot=0), base=0), {0: "SYNTHETIC-A", 1: None})

    def test_integer_zero_matches_string_zero(self) -> None:
        service = self._service(base=0)
        as_int = self._bays(service, self._disk(slot=0), base=0)
        as_str = self._bays(service, self._disk(slot="0"), base=0)
        self.assertEqual(as_int, as_str)
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot="0")), ("shelf-a", 0))

    def test_integer_zero_in_other_nested_keys_is_kept(self) -> None:
        service = self._service(base=0)
        self.assertEqual(service._extract_enclosure_slot(self._disk(number=0)), ("shelf-a", 0))
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot_number=0)), ("shelf-a", 0))

    def test_integer_zero_wins_over_a_later_nested_key(self) -> None:
        # The first present coordinate is the answer; zero must not let a later key take over.
        service = self._service(base=0)
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot=0, number=1)), ("shelf-a", 0))

    def test_control_integer_one_with_zero_base(self) -> None:
        service = self._service(base=0)
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot=1)), ("shelf-a", 1))
        self.assertEqual(self._bays(service, self._disk(slot=1), base=0), {0: None, 1: "SYNTHETIC-A"})

    def test_control_one_based_numbering_is_unchanged(self) -> None:
        service = self._service(base=1)
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot=1)), ("shelf-a", 0))
        self.assertEqual(self._bays(service, self._disk(slot=1), base=1), {0: "SYNTHETIC-A", 1: None})
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot=2)), ("shelf-a", 1))

    def test_null_nested_slot_falls_through_to_next_nested_key(self) -> None:
        service = self._service(base=0)
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot=None, number=1)), ("shelf-a", 1))
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot="", slot_number=0)), ("shelf-a", 0))

    def test_missing_nested_slot_falls_back_to_top_level_slot(self) -> None:
        service = self._service(base=0)
        disk = {"name": "sda", "enclosure": {"id": "shelf-a", "slot": None}, "slot": 0}
        self.assertEqual(service._extract_enclosure_slot(disk), ("shelf-a", 0))
        disk = {"name": "sda", "enclosure": {"id": "shelf-a"}, "enclosure_slot": 1}
        self.assertEqual(service._extract_enclosure_slot(disk), ("shelf-a", 1))

    def test_no_coordinate_anywhere_stays_unknown(self) -> None:
        service = self._service(base=0)
        self.assertEqual(service._extract_enclosure_slot(self._disk()), ("shelf-a", None))
        self.assertEqual(self._bays(service, self._disk(), base=0), {0: None, 1: None})

    def test_boolean_nested_slot_is_not_a_coordinate(self) -> None:
        # False must not become bay 0 now that zero is kept; a boolean is not a bay number.
        service = self._service(base=0)
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot=False)), ("shelf-a", None))
        self.assertEqual(service._extract_enclosure_slot(self._disk(slot=True)), ("shelf-a", None))


if __name__ == "__main__":
    unittest.main()
