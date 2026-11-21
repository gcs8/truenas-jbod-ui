"""QuantaStor enclosure choices are never dropped silently (#1307).

Synthetic fixtures only; no appliance payload or network request is used.

The enclosure options builder has an output-runaway bound. It sits far above
real hardware counts, so an inventory with more than 64 shelves lists every
shelf. When the bound is reached the read says so: it is marked incomplete
with the read and omitted counts, and the page carries a warning naming both
counts and the limit.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.config import Settings, SystemConfig, TrueNASConfig
from app.services import inventory as inventory_module
from app.services import ssh_probe as ssh_probe_module
from app.services.inventory import ParsedSSHData, TrueNASRawData
from tests.inventory_fixtures import build_inventory_service

PROFILE_ID = "supermicro-ssg-2028r-shared-front-24"


def _raw_data(systems: list[dict], hw_enclosures: list[dict]) -> TrueNASRawData:
    return TrueNASRawData(
        enclosures=[],
        systems=systems,
        disks=[],
        pools=[],
        pool_devices=[],
        ha_groups=[],
        hw_disks=[],
        hw_enclosures=hw_enclosures,
        disk_temperatures={},
        smart_test_results=[],
    )


def _shelves(owner: str, count: int) -> list[dict]:
    return [{"id": f"{owner}-enc-{index:04d}", "storageSystemId": owner} for index in range(count)]


class QuantaStorEnclosureOptionsLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        system = SystemConfig(
            id="options-limit-quantastor",
            default_profile_id=PROFILE_ID,
            truenas=TrueNASConfig(platform="quantastor"),
        )
        self.service = build_inventory_service(Settings(), system, MagicMock(), MagicMock(), self._temp.name)

    def _read(self, raw_data: TrueNASRawData):
        return self.service._read_quantastor_enclosure_options(raw_data)

    def test_bound_sits_far_above_real_shelf_counts(self) -> None:
        self.assertGreaterEqual(inventory_module.QUANTASTOR_ENCLOSURE_OPTIONS_MAX_ENTRIES, 1024)

    def test_sixty_five_shelves_are_all_listed(self) -> None:
        raw_data = _raw_data([{"id": "node-a", "name": "Node A"}], _shelves("node-a", 65))

        options = self.service._build_quantastor_enclosure_options(raw_data)

        self.assertEqual([option.id for option in options], [f"node-a-enc-{index:04d}" for index in range(65)])

    def test_read_at_the_bound_is_complete(self) -> None:
        raw_data = _raw_data([{"id": "node-a", "name": "Node A"}], _shelves("node-a", 8))

        with patch.object(inventory_module, "QUANTASTOR_ENCLOSURE_OPTIONS_MAX_ENTRIES", 8):
            result = self._read(raw_data)

        self.assertFalse(result.incomplete)
        self.assertEqual((result.read_count, result.omitted_count), (8, 0))
        self.assertEqual(len(result.options), 8)

    def test_read_one_over_the_bound_is_incomplete_with_counts(self) -> None:
        raw_data = _raw_data([{"id": "node-a", "name": "Node A"}], _shelves("node-a", 9))

        with patch.object(inventory_module, "QUANTASTOR_ENCLOSURE_OPTIONS_MAX_ENTRIES", 8):
            result = self._read(raw_data)

        self.assertTrue(result.incomplete)
        self.assertEqual((result.read_count, result.omitted_count, result.limit), (8, 1, 8))
        self.assertEqual([option.id for option in result.options], [f"node-a-enc-{index:04d}" for index in range(8)])

    def test_omitted_count_includes_later_storage_systems(self) -> None:
        # node-a has 6 shelves, node-b one shelf (one system-level choice),
        # node-c 3 shelves: 10 choices in all, 4 of them above a bound of 6.
        raw_data = _raw_data(
            [
                {"id": "node-a", "name": "Node A"},
                {"id": "node-b", "name": "Node B"},
                {"id": "node-c", "name": "Node C"},
            ],
            [*_shelves("node-a", 6), *_shelves("node-b", 1), *_shelves("node-c", 3)],
        )

        with patch.object(inventory_module, "QUANTASTOR_ENCLOSURE_OPTIONS_MAX_ENTRIES", 6):
            result = self._read(raw_data)

        self.assertTrue(result.incomplete)
        self.assertEqual((result.read_count, result.omitted_count), (6, 4))

    def test_page_warns_with_counts_when_the_bound_is_reached(self) -> None:
        raw_data = _raw_data([{"id": "node-a", "name": "Node A"}], _shelves("node-a", 11))
        warnings: list[str] = []

        with patch.object(inventory_module, "QUANTASTOR_ENCLOSURE_OPTIONS_MAX_ENTRIES", 8):
            _slots, options, *_rest = self.service._correlate_quantastor(
                raw_data, warnings, "node-a-enc-0000", ParsedSSHData()
            )

        self.assertEqual(len(options), 8)
        matching = [warning for warning in warnings if "enclosure list is incomplete" in warning]
        self.assertEqual(len(matching), 1, warnings)
        self.assertIn("8 enclosures listed", matching[0])
        self.assertIn("3 left out", matching[0])
        self.assertIn("limit of 8", matching[0])

    def test_page_has_no_limit_warning_at_the_bound(self) -> None:
        raw_data = _raw_data([{"id": "node-a", "name": "Node A"}], _shelves("node-a", 8))
        warnings: list[str] = []

        with patch.object(inventory_module, "QUANTASTOR_ENCLOSURE_OPTIONS_MAX_ENTRIES", 8):
            _slots, options, *_rest = self.service._correlate_quantastor(
                raw_data, warnings, "node-a-enc-0000", ParsedSSHData()
            )

        self.assertEqual(len(options), 8)
        self.assertFalse([warning for warning in warnings if "enclosure list is incomplete" in warning], warnings)

    def test_reaching_the_bound_is_logged_with_counts(self) -> None:
        raw_data = _raw_data([{"id": "node-a", "name": "Node A"}], _shelves("node-a", 10))
        log_key = ("quantastor_enclosure_options", self.service.system.id, "limit")
        ssh_probe_module.SSH_COMMAND_FAILURE_LOG.forget(log_key)
        self.addCleanup(ssh_probe_module.SSH_COMMAND_FAILURE_LOG.forget, log_key)

        with (
            patch.object(inventory_module, "QUANTASTOR_ENCLOSURE_OPTIONS_MAX_ENTRIES", 8),
            self.assertLogs(inventory_module.logger, level="WARNING") as captured,
        ):
            self.service._correlate_quantastor(raw_data, [], "node-a-enc-0000", ParsedSSHData())

        messages = [record.getMessage() for record in captured.records]
        self.assertTrue(
            any(
                "enclosure options" in message and "8 listed" in message and "2 left out" in message
                for message in messages
            ),
            messages,
        )


if __name__ == "__main__":
    unittest.main()
