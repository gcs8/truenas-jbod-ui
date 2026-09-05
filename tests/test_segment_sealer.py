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
from typing import Any, Callable
from unittest.mock import patch

from history_service import segment_sealer
from history_service.segment_sealer import seal_history_segment
from history_service.store import SCHEMA


class SegmentSealerCliTests(unittest.TestCase):
    def test_sealer_allows_atime_only_source_metadata_change_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)
            receipt = self._seal_with_post_copy_stat(
                source,
                output_directory,
                lambda metadata: self._stat_result_with(
                    metadata,
                    st_atime_ns=metadata.st_atime_ns + 1_000_000_000,
                ),
            )

            self.assertTrue(Path(receipt["path"]).is_file())
            self.assertEqual(list(output_directory.glob(".segment-*")), [])

    def test_sealer_rejects_size_only_source_change_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)

            with self.assertRaisesRegex(
                ValueError,
                "History segment source changed while it was being sealed",
            ):
                self._seal_with_post_copy_stat(
                    source,
                    output_directory,
                    lambda metadata: self._stat_result_with(
                        metadata,
                        st_size=metadata.st_size + 1,
                    ),
                )

            self.assertFalse((output_directory / "segment-0001.sqlite3").exists())
            self.assertEqual(list(output_directory.glob(".segment-*")), [])

    def test_sealer_rejects_mtime_only_source_change_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)

            with self.assertRaisesRegex(
                ValueError,
                "History segment source changed while it was being sealed",
            ):
                self._seal_with_post_copy_stat(
                    source,
                    output_directory,
                    lambda metadata: self._stat_result_with(
                        metadata,
                        st_mtime_ns=metadata.st_mtime_ns + 1_000_000_000,
                    ),
                )

            self.assertFalse((output_directory / "segment-0001.sqlite3").exists())
            self.assertEqual(list(output_directory.glob(".segment-*")), [])

    def test_sealer_rejects_source_replacement_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)
            original_inode = source.stat().st_ino
            original_copy_and_prune = segment_sealer._copy_and_prune

            def copy_and_replace(copy_source: Path, destination: Path, cutoff: str) -> dict[str, int]:
                row_counts = original_copy_and_prune(copy_source, destination, cutoff)
                replacement = root / "replacement.db"
                replacement.write_bytes(copy_source.read_bytes())
                os.replace(replacement, copy_source)
                return row_counts

            with (
                patch(
                    "history_service.segment_sealer._copy_and_prune",
                    side_effect=copy_and_replace,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "History segment source changed while it was being sealed",
                ),
            ):
                seal_history_segment(
                    source=source,
                    output_directory=output_directory,
                    segment_id="segment-0001",
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                )

            self.assertNotEqual(source.stat().st_ino, original_inode)
            self.assertFalse((output_directory / "segment-0001.sqlite3").exists())
            self.assertEqual(list(output_directory.glob(".segment-*")), [])

    def test_sealer_rejects_ctime_only_source_metadata_change_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)

            with self.assertRaisesRegex(
                ValueError,
                "History segment source changed while it was being sealed",
            ):
                self._seal_with_post_copy_stat(
                    source,
                    output_directory,
                    lambda metadata: self._stat_result_with(
                        metadata,
                        st_ctime_ns=metadata.st_ctime_ns + 1_000_000_000,
                    ),
                )

            self.assertFalse((output_directory / "segment-0001.sqlite3").exists())
            self.assertEqual(list(output_directory.glob(".segment-*")), [])

    def test_sealer_snaps_cutoff_to_the_utc_day_bucket_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            output_directory = root / "segments"
            self._create_source_database(source)

            receipt = seal_history_segment(
                source=source,
                output_directory=output_directory,
                segment_id="segment-0001",
                cutoff="2025-01-02T12:34:56+00:00",
                key_id="test-key-1",
            )

            self.assertEqual(receipt["sealed_at"], "2025-01-02T00:00:00+00:00")
            with sqlite3.connect(receipt["path"]) as connection:
                self.assertEqual(
                    connection.execute("SELECT observed_at FROM slot_events ORDER BY observed_at").fetchall(),
                    [("2025-01-01T00:00:00+00:00",)],
                )

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

    def test_copy_and_prune_of_a_service_wal_hot_creates_no_source_sidecars(self) -> None:
        # The service leaves the hot database with a WAL header. A plain mode=ro
        # open of that quiesced file creates -wal/-shm that survive close, and the
        # next sidecar preflight refuses the source (issue #278).
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            self._create_source_database(source)
            connection = sqlite3.connect(source)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
            finally:
                connection.close()
            self.assertEqual(source.read_bytes()[18:20], b"\x02\x02")
            for suffix in ("-wal", "-shm"):
                self.assertFalse(Path(f"{source}{suffix}").exists(), suffix)

            counts = segment_sealer._copy_and_prune(
                source,
                root / "segment.sqlite3",
                "2025-01-02T00:00:00+00:00",
            )

            self.assertEqual(counts["slot_events"], 1)
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(f"{source}{suffix}").exists(), suffix)
            segment_sealer._require_regular_source(source)

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

    @staticmethod
    def _stat_result_with(
        metadata: os.stat_result,
        *,
        st_atime_ns: int | None = None,
        st_ino: int | None = None,
        st_size: int | None = None,
        st_mtime_ns: int | None = None,
        st_ctime_ns: int | None = None,
    ) -> os.stat_result:
        values = list(metadata)
        nanoseconds = {
            "st_atime_ns": metadata.st_atime_ns,
            "st_mtime_ns": metadata.st_mtime_ns,
            "st_ctime_ns": metadata.st_ctime_ns,
        }
        if st_ino is not None:
            values[1] = st_ino
        if st_size is not None:
            values[6] = st_size
        if st_atime_ns is not None:
            values[7] = st_atime_ns / 1_000_000_000
            nanoseconds["st_atime_ns"] = st_atime_ns
        if st_mtime_ns is not None:
            values[8] = st_mtime_ns / 1_000_000_000
            nanoseconds["st_mtime_ns"] = st_mtime_ns
        if st_ctime_ns is not None:
            values[9] = st_ctime_ns / 1_000_000_000
            nanoseconds["st_ctime_ns"] = st_ctime_ns
        return os.stat_result(values, nanoseconds)

    def _seal_with_post_copy_stat(
        self,
        source: Path,
        output_directory: Path,
        transform: Callable[[os.stat_result], os.stat_result],
    ) -> dict[str, Any]:
        original_copy_and_prune = segment_sealer._copy_and_prune
        original_stat = os.stat
        copy_finished = False

        def copy_and_prune(copy_source: Path, destination: Path, cutoff: str) -> dict[str, int]:
            nonlocal copy_finished
            row_counts = original_copy_and_prune(copy_source, destination, cutoff)
            copy_finished = True
            return row_counts

        def stat_after_copy(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            metadata = original_stat(path, *args, **kwargs)
            if copy_finished and Path(path) == source:
                return transform(metadata)
            return metadata

        with (
            patch(
                "history_service.segment_sealer._copy_and_prune",
                side_effect=copy_and_prune,
            ),
            patch("history_service.segment_sealer.os.stat", side_effect=stat_after_copy),
        ):
            return seal_history_segment(
                source=source,
                output_directory=output_directory,
                segment_id="segment-0001",
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
            )
