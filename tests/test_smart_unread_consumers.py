"""SMART fields that were reported but not read, at the history and export consumers (#1221).

A field the SMART JSON reported in a shape that could not be read is "not
read": neither absent nor zero. History stores no sample for it, and the saved
HTML copy carries the list of unread fields so its page can say "not read".
These consumers import the POSIX-only history and export graph, so they live
apart from the portable parser tests in ``tests.test_smart_json_shapes``.
"""

import json
import unittest

from app.config import Settings
from app.models.domain import SlotView
from app.services.inventory import InventoryService
from app.services.snapshot_export import SnapshotExportService
from history_service.collector import SLOW_METRIC_FIELDS, HistoryCollector
from history_service.domain import SlotStateRecord
from tests.test_snapshot_export import FakeHistoryBackend, build_request, build_snapshot, templates

UNREAD_HOURS_PAYLOAD = {"temperature": {"current": 30}, "power_on_time": {"hours": True}}


def _summary(payload: dict) -> dict:
    slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0)
    return InventoryService._parse_smart_payload_for_slot(slot, json.dumps(payload)).model_dump(mode="json")


class HistoryUnreadTests(unittest.TestCase):
    SLOT = SlotStateRecord(
        system_id="synthetic", system_label=None, enclosure_key="synthetic", enclosure_id=None,
        enclosure_label=None, slot=0, slot_label="00", present=True, state=None, identify_active=False,
        device_name=None, serial=None, model=None, gptid=None, pool_name=None, vdev_name=None, health=None,
    )

    def _samples(self, payload: dict) -> list[tuple[str, int | None]]:
        samples = HistoryCollector._build_metric_samples(
            object.__new__(HistoryCollector), self.SLOT, _summary(payload),
            "2026-01-01T00:00:00+00:00", SLOW_METRIC_FIELDS,
        )
        return [(sample.metric_name, sample.value_integer) for sample in samples]

    def test_unread_power_on_hours_writes_no_sample(self) -> None:
        self.assertEqual(self._samples(UNREAD_HOURS_PAYLOAD), [])

    def test_zero_hours_control_writes_a_zero_sample(self) -> None:
        self.assertEqual(self._samples({"power_on_time": {"hours": 0}}), [("power_on_hours", 0)])


class SavedCopyUnreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_html_copy_carries_the_unread_list(self) -> None:
        summary = _summary(UNREAD_HOURS_PAYLOAD)
        self.assertEqual(summary["unread_fields"], ["power_on_days", "power_on_hours"])
        for redact in (False, True):
            with self.subTest(redact=redact):
                exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)
                rendered = await exporter.build_enclosure_snapshot_html(
                    request=build_request(), snapshot=build_snapshot(), smart_summary_cache={"0": summary},
                    selected_slot=0, history_window_hours=24, io_chart_mode="total", redact_sensitive=redact,
                )
                # assertTrue, not assertIn: a failure must not print the whole page.
                self.assertTrue('"unread_fields": ["power_on_days", "power_on_hours"]' in rendered.html)


if __name__ == "__main__":
    unittest.main()
