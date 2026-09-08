from __future__ import annotations

from datetime import datetime
from pathlib import Path
import unittest

from app.services.public_demo_fixture import (
    _history_payload,
    _smart_summary,
    load_public_demo_fixture,
)


ROOT = Path(__file__).resolve().parents[1]


class PublicDemoHistoryConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_public_demo_fixture(ROOT / "tests/fixtures/public_demo/public_demo.json")
        self.slot = next(slot for slot in self.fixture.slots if slot.slot == 57)
        self.history = _history_payload(
            self.fixture,
            self.slot,
            slot=self.slot.slot,
            system_id=self.fixture.system.id,
            enclosure_id=self.fixture.enclosure.id,
        )

    def test_latest_history_values_match_slot_and_smart_summary(self) -> None:
        summary = _smart_summary(self.slot)
        latest = self.history["latest_values"]
        expected_annualized_read = int((self.slot.bytes_read or 0) * 8760 / (self.slot.power_on_hours or 1))
        expected_annualized_write = int((self.slot.bytes_written or 0) * 8760 / (self.slot.power_on_hours or 1))

        self.assertEqual(latest["temperature_c"], self.slot.temperature_c)
        self.assertEqual(latest["bytes_read"], self.slot.bytes_read)
        self.assertEqual(latest["bytes_written"], self.slot.bytes_written)
        self.assertEqual(summary.annualized_bytes_read, expected_annualized_read)
        self.assertEqual(summary.annualized_bytes_written, expected_annualized_write)
        self.assertEqual(latest["annualized_bytes_read"], summary.annualized_bytes_read)
        self.assertEqual(latest["annualized_bytes_written"], summary.annualized_bytes_written)

    def test_counter_history_is_monotonic_and_rate_matches_elapsed_time(self) -> None:
        for metric in ("bytes_read", "bytes_written"):
            with self.subTest(metric=metric):
                samples = self.history["metrics"][metric]
                values = [sample["value"] for sample in samples]
                self.assertEqual(values, sorted(values))
                first_at = datetime.fromisoformat(samples[0]["observed_at"])
                last_at = datetime.fromisoformat(samples[-1]["observed_at"])
                elapsed_hours = (last_at - first_at).total_seconds() / 3600
                self.assertEqual((values[-1] - values[0]) / elapsed_hours, 1_000_000)

    def test_annualized_history_is_present_and_stable(self) -> None:
        for metric in ("annualized_bytes_read", "annualized_bytes_written"):
            with self.subTest(metric=metric):
                samples = self.history["metrics"][metric]
                self.assertEqual(len(samples), len(self.fixture.history_sample_offsets_hours))
                self.assertEqual(len({sample["value"] for sample in samples}), 1)


if __name__ == "__main__":
    unittest.main()
