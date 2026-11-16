"""SMART fields that were reported but not read, at the history and export consumers (#1221).

A field the SMART JSON reported in a shape that could not be read is "not
read": neither absent nor zero. History stores no sample for it, and the saved
HTML copy carries the list of unread fields so its page can say "not read".
These consumers import the POSIX-only history and export graph, so they live
apart from the portable parser tests in ``tests.test_smart_json_shapes``.
"""

import json
import tempfile
import unittest
from pathlib import Path

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


class HistoryConversionTests(unittest.TestCase):
    """One bad value never stops the history pass: conversions fail locally to "no reading"."""

    SLOT = HistoryUnreadTests.SLOT

    def test_out_of_range_celsius_from_the_typed_parser_is_no_temperature(self) -> None:
        for value in (10**400, 2**63, 1001):
            with self.subTest(value=str(value)[:12]):
                summary = _summary({"temperature": {"current": value}, "smart_status": {"passed": True}})
                self.assertIn("temperature_c", summary["unread_fields"])
                self.assertIsNone(HistoryCollector._smart_summary_temperature(summary))
                self.assertTrue(HistoryCollector._smart_summary_has_alert_evidence(summary))

    def test_consumer_conversion_is_local_failure_safe(self) -> None:
        # Values that did not come through the typed parser (an older or
        # foreign summary) must not raise out of the per-disk loop either.
        for summary in ({"temperature_c": 10**400}, {"temperature_c": float("inf")}, {"temperature_c": "30"}):
            with self.subTest(summary=str(summary)[:40]):
                self.assertIsNone(HistoryCollector._smart_summary_temperature(summary))
        self.assertTrue(HistoryCollector._smart_summary_has_failure_health_evidence({"predictive_errors": 10**400}))
        self.assertFalse(HistoryCollector._smart_summary_has_failure_health_evidence({"predictive_errors": float("nan")}))

    def test_temperature_controls(self) -> None:
        for value, expected in ((30, 30.0), (-1, -1.0), (0, 0.0)):
            with self.subTest(value=value):
                summary = _summary({"temperature": {"current": value}})
                self.assertEqual(HistoryCollector._smart_summary_temperature(summary), expected)

    def test_integer_the_store_cannot_hold_writes_no_sample(self) -> None:
        from history_service.store import HistoryStore

        # Admitted readings at and past the store's signed 64-bit integer limit.
        too_large = _summary({"power_on_time": {"hours": 2**63},
                              "nvme_smart_health_information_log": {"data_units_written": 2**63 // 512_000 + 1}})
        self.assertEqual((too_large["power_on_hours"] is not None, too_large["bytes_written"] is not None), (True, True))
        largest = {"power_on_hours": 2**63 - 1, "bytes_read": 0, "temperature_c": -5, "annualized_bytes_written": None}
        samples = HistoryCollector._build_metric_samples(
            object.__new__(HistoryCollector), self.SLOT, {**too_large, **largest},
            "2026-01-01T00:00:00+00:00", (*SLOW_METRIC_FIELDS, "temperature_c"),
        )
        self.assertEqual(sorted((sample.metric_name, sample.value_integer) for sample in samples),
                         [("bytes_read", 0), ("power_on_hours", 2**63 - 1), ("temperature_c", -5)])
        rejected = HistoryCollector._build_metric_samples(
            object.__new__(HistoryCollector), self.SLOT, too_large, "2026-01-01T00:00:00+00:00", SLOW_METRIC_FIELDS,
        )
        self.assertEqual({sample.metric_name for sample in rejected} & {"power_on_hours", "bytes_written"}, set())
        with tempfile.TemporaryDirectory() as temp_dir:
            HistoryStore(str(Path(temp_dir) / "history.db")).insert_metric_samples(samples)


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
