from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from history_service.segment_sealer import seal_history_segment
from history_service.store import SCHEMA


class SegmentSealerCliTests(unittest.TestCase):
    def test_sealer_partitions_mixed_offset_timestamps_by_absolute_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            with sqlite3.connect(source) as connection:
                connection.executescript(SCHEMA)
                for event_type, observed_at in (
                    ("chronologically-old", "2025-01-02T01:00:00+14:00"),
                    ("chronologically-new", "2025-01-01T20:00:00-12:00"),
                ):
                    connection.execute(
                        """
                        INSERT INTO slot_events (
                            observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (observed_at, "system-1", "enclosure-1", 1, "slot-1", event_type, "{}"),
                    )

            receipt = seal_history_segment(
                source=source,
                output_directory=output_directory,
                segment_id="segment-0001",
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
            )

            with sqlite3.connect(receipt["path"]) as connection:
                self.assertEqual(
                    connection.execute("SELECT event_type FROM slot_events").fetchall(),
                    [("chronologically-old",)],
                )
            self.assertEqual(receipt["coverage_start"], "2025-01-02T01:00:00+14:00")
            self.assertEqual(receipt["coverage_end"], "2025-01-02T01:00:00+14:00")

    def test_cli_rejects_source_with_sqlite_sidecar_without_publishing_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)
            (root / "history.db-wal").write_bytes(b"unexpected-sidecar")

            result = self._run_cli(source, output_directory)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sidecar", result.stderr)
            self.assertFalse((output_directory / "segment-0001.sqlite3").exists())

    def test_cli_seals_history_before_cutoff_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

            result = self._run_cli(source, output_directory)

            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            segment_path = output_directory / "segment-0001.sqlite3"
            self.assertEqual(receipt["segment_id"], "segment-0001")
            self.assertEqual(receipt["path"], str(segment_path))
            self.assertEqual(receipt["row_counts"], {
                "metric_rollups": 1,
                "metric_samples": 1,
                "slot_events": 1,
            })
            self.assertTrue(segment_path.is_file())
            source_metadata = source.stat()
            segment_metadata = segment_path.stat()
            directory_metadata = output_directory.stat()
            self.assertEqual(stat.S_IMODE(segment_metadata.st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(directory_metadata.st_mode), 0o750)
            self.assertEqual(
                (segment_metadata.st_uid, segment_metadata.st_gid),
                (source_metadata.st_uid, source_metadata.st_gid),
            )
            self.assertEqual(
                (directory_metadata.st_uid, directory_metadata.st_gid),
                (source_metadata.st_uid, source_metadata.st_gid),
            )
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_sha256)

            with sqlite3.connect(segment_path) as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM slot_state_current").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM metric_rollups").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT MAX(observed_at) FROM slot_events").fetchone()[0],
                    "2025-01-01T00:00:00+00:00",
                )

    def test_sealer_repairs_an_owned_existing_directory_to_shared_read_only_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)
            output_directory.mkdir(mode=0o700)
            output_directory.chmod(0o700)

            receipt = seal_history_segment(
                source=source,
                output_directory=output_directory,
                segment_id="segment-0001",
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
            )

            self.assertEqual(stat.S_IMODE(output_directory.stat().st_mode), 0o750)
            self.assertEqual(stat.S_IMODE(Path(receipt["path"]).stat().st_mode), 0o640)

    def test_sealer_explicitly_applies_source_ownership_to_directory_and_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)
            source_metadata = source.stat()

            with patch("history_service.segment_sealer.os.fchown", wraps=os.fchown) as fchown:
                seal_history_segment(
                    source=source,
                    output_directory=output_directory,
                    segment_id="segment-0001",
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                )

            applied_owners = {(item.args[1], item.args[2]) for item in fchown.call_args_list}
            self.assertIn((source_metadata.st_uid, source_metadata.st_gid), applied_owners)
            self.assertGreaterEqual(fchown.call_count, 2)

    def test_sealer_refuses_a_caller_that_does_not_own_the_hot_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)

            with (
                patch(
                    "history_service.segment_sealer.os.geteuid",
                    return_value=source.stat().st_uid + 1,
                ),
                self.assertRaisesRegex(ValueError, "must own the hot history database"),
            ):
                seal_history_segment(
                    source=source,
                    output_directory=output_directory,
                    segment_id="segment-0001",
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                )

            self.assertFalse(output_directory.exists())

    @staticmethod
    def _run_cli(source: Path, output_directory: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "scripts/seal_history_segment.py",
                "--source",
                str(source),
                "--output-dir",
                str(output_directory),
                "--segment-id",
                "segment-0001",
                "--cutoff",
                "2025-01-02T00:00:00+00:00",
                "--key-id",
                "test-key-1",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=False,
            text=True,
        )

    @staticmethod
    def _create_source_database(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                INSERT INTO slot_state_current (
                    system_id, enclosure_key, slot, slot_label, present, identify_active, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("system-1", "enclosure-1", 1, "slot-1", 1, 0, "2025-01-02T00:00:00+00:00"),
            )
            for observed_at in ("2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00"):
                connection.execute(
                    """
                    INSERT INTO slot_events (
                        observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (observed_at, "system-1", "enclosure-1", 1, "slot-1", "state_change", "{}"),
                )
                connection.execute(
                    """
                    INSERT INTO metric_samples (
                        observed_at, system_id, enclosure_key, slot, slot_label, metric_name, value_integer
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (observed_at, "system-1", "enclosure-1", 1, "slot-1", "temperature", 30),
                )
                connection.execute(
                    """
                    INSERT INTO metric_rollups (
                        bucket_start, bucket_seconds, system_id, enclosure_key, slot, slot_label,
                        metric_name, sample_count, value_sum, value_min, value_max, last_value, last_observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observed_at,
                        3600,
                        "system-1",
                        "enclosure-1",
                        1,
                        "slot-1",
                        "temperature",
                        1,
                        30.0,
                        30.0,
                        30.0,
                        30.0,
                        observed_at,
                    ),
                )
