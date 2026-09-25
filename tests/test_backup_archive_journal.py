"""Change journal and config-backup coalescing (#573)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from history_service.backup_archive.journal import (
    ChangeJournal,
    ConfigBackupCoalescer,
    JournalError,
    canonical_config_hash,
    content_sha256,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True)
class FakeArtifact:
    artifact_id: str
    change_ids: tuple[str, ...]


class ConfigFixture:
    """A synthetic config set whose snapshot the coalescer hashes."""

    def __init__(self) -> None:
        self.docs = {
            "config.yaml": {"systems": [{"id": "lab-a", "host": "192.0.2.10"}], "updated_at": "t0"},
            "slot_mappings.json": {"version": 2, "updated_at": "t0", "slot_mappings": {}},
            "profiles.yaml": {"profiles": []},
        }

    def snapshot(self):
        return json.loads(json.dumps(self.docs))


class JournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "journal" / "config-changes.jsonl"
        self.clock = FakeClock()
        self.config = ConfigFixture()
        self.backups: list[FakeArtifact] = []

    def make_backup(self, change_ids):
        artifact = FakeArtifact(artifact_id=f"cfg-{len(self.backups) + 1}", change_ids=tuple(change_ids))
        self.backups.append(artifact)
        return artifact

    def coalescer(self, journal: ChangeJournal | None = None, **kwargs) -> ConfigBackupCoalescer:
        kwargs.setdefault("make_backup", self.make_backup)
        return ConfigBackupCoalescer(
            journal or ChangeJournal(self.path),
            snapshot_config=self.config.snapshot,
            clock=self.clock,
            quiet_period=30,
            max_delay=600,
            **kwargs,
        )

    def edit(self, co: ConfigBackupCoalescer, slot: int, serial: str, action: str = "mapping.save"):
        before = canonical_config_hash(self.config.docs)
        self.config.docs["slot_mappings.json"]["slot_mappings"][str(slot)] = {"serial": serial}
        self.config.docs["slot_mappings.json"]["updated_at"] = f"t{self.clock.now}"
        return co.record_change(
            action, f"lab-a/slot-{slot}", before_hash=before, after_hash=canonical_config_hash(self.config.docs)
        )


class CanonicalHashTests(unittest.TestCase):
    def test_key_order_and_volatile_fields_do_not_change_the_hash(self) -> None:
        a = {"config.yaml": {"b": 1, "a": {"y": 2, "x": 1}, "updated_at": "2026-01-01"}}
        b = {"config.yaml": {"a": {"x": 1, "y": 2, "updated_at": "2026-09-24"}, "b": 1}}
        self.assertEqual(canonical_config_hash(a), canonical_config_hash(b))

    def test_list_order_value_and_missing_document_do_change_the_hash(self) -> None:
        base = canonical_config_hash({"c": {"systems": ["a", "b"]}})
        self.assertNotEqual(base, canonical_config_hash({"c": {"systems": ["b", "a"]}}))
        self.assertNotEqual(base, canonical_config_hash({"c": {"systems": ["a", "c"]}}))
        self.assertNotEqual(base, canonical_config_hash({"c": {"systems": ["a", "b"]}, "d": None}))

    def test_content_sha256(self) -> None:
        self.assertIsNone(content_sha256(None))
        self.assertEqual(content_sha256("x"), content_sha256(b"x"))


class ChangeJournalTests(JournalTestCase):
    def test_append_is_durable_json_lines_with_private_mode(self) -> None:
        journal = ChangeJournal(self.path)
        entry = journal.append("system.rename", "lab-a", before_hash="a" * 64, after_hash="b" * 64)
        lines = self.path.read_bytes().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["change_id"], entry.change_id)
        self.assertEqual(record["action"], "system.rename")
        self.assertTrue(record["at"].endswith("Z"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([e.change_id for e in ChangeJournal(self.path).pending()], [entry.change_id])

    def test_rejects_bad_input_and_symlinks(self) -> None:
        journal = ChangeJournal(self.path)
        with self.assertRaises(ValueError):
            journal.append("Bad Action")
        with self.assertRaises(ValueError):
            journal.append("mapping.save", before_hash="not-a-hash")
        with self.assertRaises(ValueError):
            journal.commit(["x"], outcome="backup", config_hash="a" * 64)
        target = self.root / "elsewhere.jsonl"
        target.write_text("")
        link = self.root / "journal" / "link.jsonl"
        link.symlink_to(target)
        with self.assertRaises(JournalError):
            ChangeJournal(link).append("mapping.save")

    def test_torn_last_line_stays_pending_as_recovered_change_across_repair(self) -> None:
        journal = ChangeJournal(self.path)
        first = journal.append("mapping.save", "slot-1")
        journal.commit([first.change_id], outcome="backup", config_hash="c" * 64, backup_id="cfg-1")
        with self.path.open("ab") as handle:
            handle.write(b'{"v":1,"type":"change","change_id":"chg_')  # crash mid-write
        size_before = self.path.stat().st_size
        reopened = ChangeJournal(self.path)
        pending = reopened.pending()
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].recovered)
        self.assertEqual(reopened.last_backup(), ("c" * 64, "cfg-1"))
        self.assertGreater(reopened.stats().torn_tail_bytes, 0)
        second = reopened.append("mapping.save", "slot-2")
        data = self.path.read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        self.assertGreater(len(data), size_before)  # the torn bytes were never truncated away
        ids = [e.change_id for e in reopened.pending()]
        # The terminated line keeps the placeholder id readers already saw.
        self.assertEqual(ids, [pending[0].change_id, second.change_id])
        self.assertEqual(reopened.stats().torn_tail_bytes, 0)
        self.assertEqual(reopened.stats().corrupt_lines, 1)
        reopened.commit(ids, outcome="noop", config_hash="c" * 64)
        self.assertEqual(ChangeJournal(self.path).pending(), [])
        reopened.compact()
        self.assertNotIn(b'"change_id":"chg_\n', self.path.read_bytes())
        self.assertEqual(ChangeJournal(self.path).pending(), [])

    def test_complete_record_missing_only_its_newline_is_kept(self) -> None:
        journal = ChangeJournal(self.path)
        entry = journal.append("mapping.save", "slot-1")
        self.path.write_bytes(self.path.read_bytes().rstrip(b"\n"))
        self.assertEqual([e.change_id for e in journal.pending()], [entry.change_id])
        journal.append("mapping.save", "slot-2")
        self.assertEqual(len(journal.pending()), 2)
        self.assertEqual(journal.stats().corrupt_lines, 0)

    def test_first_append_fsyncs_the_directory(self) -> None:
        from history_service.backup_archive import journal as journal_module

        calls = []
        original = journal_module._fsync_directory
        journal_module._fsync_directory = lambda path: calls.append(path)
        try:
            journal = ChangeJournal(self.path)
            journal.append("mapping.save")
            journal.append("mapping.save")
        finally:
            journal_module._fsync_directory = original
        # Directory creation fsyncs root; the first append fsyncs the journal dir once.
        self.assertEqual(calls, [self.root, self.path.parent])

    def test_directory_fsync_failure_is_not_reported_as_durable(self) -> None:
        from history_service.backup_archive import journal as journal_module

        def failing(path):
            raise PermissionError("cannot open directory")

        original = journal_module._fsync_directory
        journal_module._fsync_directory = failing
        try:
            with self.assertRaises(PermissionError):
                ChangeJournal(self.root / "fresh" / "journal.jsonl")
        finally:
            journal_module._fsync_directory = original
        journal = ChangeJournal(self.path)
        journal_module._fsync_directory = failing
        try:
            with self.assertRaises(PermissionError):
                journal.append("mapping.save")  # first creation needs the directory fsync
        finally:
            journal_module._fsync_directory = original

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_real_unreadable_directory_raises(self) -> None:
        directory = self.root / "write-only"
        directory.mkdir(mode=0o700)
        directory.chmod(0o300)
        self.addCleanup(directory.chmod, 0o700)
        with self.assertRaises(PermissionError):
            ChangeJournal(directory / "journal.jsonl").append("mapping.save")

    def test_large_commit_is_split_below_the_line_limit(self) -> None:
        journal = ChangeJournal(self.path, max_bytes=16 * 1024 * 1024)
        ids = [journal.append("mapping.save", f"s{n}").change_id for n in range(1700)]
        journal.commit(ids, outcome="backup", config_hash="e" * 64, backup_id="cfg-big")
        self.assertTrue(all(len(line) < 64 * 1024 for line in self.path.read_bytes().splitlines()))
        reread = ChangeJournal(self.path)
        self.assertEqual(reread.pending(), [])
        self.assertEqual(reread.last_backup(), ("e" * 64, "cfg-big"))
        self.assertEqual(reread.stats().corrupt_lines, 0)

    def test_corrupt_complete_line_keeps_a_backup_pending(self) -> None:
        journal = ChangeJournal(self.path)
        with self.path.open("ab") as handle:
            handle.write(b"\x00\xffnot json\n")
        pending = journal.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].action, "journal.recovered")
        self.assertEqual(journal.stats().corrupt_lines, 1)

    def test_compaction_keeps_pending_and_last_backup_and_bounds_size(self) -> None:
        journal = ChangeJournal(self.path, max_bytes=8192, retain_committed_entries=5)
        for batch in range(20):
            ids = [journal.append("mapping.save", f"slot-{batch}-{i}").change_id for i in range(5)]
            journal.commit(ids, outcome="backup", config_hash=f"{batch:064x}", backup_id=f"cfg-{batch}")
        waiting = journal.append("profile.save", "profile-x")
        journal.compact()
        self.assertLessEqual(self.path.stat().st_size, 8192)
        reread = ChangeJournal(self.path)
        self.assertEqual([e.change_id for e in reread.pending()], [waiting.change_id])
        self.assertEqual(reread.last_backup(), (f"{19:064x}", "cfg-19"))
        committed = [e for e in reread.entries() if e.status == "backup"]
        self.assertEqual(len(committed), 5)
        self.assertTrue(all(e.backup_id == "cfg-19" for e in committed))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_small_budget_bounds_retained_commits_by_bytes(self) -> None:
        journal = ChangeJournal(self.path, max_bytes=4096)  # default count retention (256)
        sizes = []
        for n in range(100):
            entry = journal.append("mapping.save", f"slot-{n}")
            journal.commit([entry.change_id], outcome="backup", config_hash=f"{n:064x}", backup_id=f"cfg-{n}")
            sizes.append(self.path.stat().st_size)
        self.assertLessEqual(max(sizes), 4096)
        reread = ChangeJournal(self.path)
        self.assertEqual(reread.last_backup(), (f"{99:064x}", "cfg-99"))
        self.assertGreater(len(reread.entries()), 0)
        self.assertEqual(reread.pending(), [])

    def test_new_journal_directories_are_fsynced_into_their_parents(self) -> None:
        from history_service.backup_archive import journal as journal_module

        calls = []
        original = journal_module._fsync_directory
        journal_module._fsync_directory = lambda path: calls.append(path)
        try:
            ChangeJournal(self.root / "a" / "b" / "journal.jsonl")
        finally:
            journal_module._fsync_directory = original
        self.assertEqual(calls, [self.root, self.root / "a"])
        self.assertEqual((self.root / "a" / "b").stat().st_mode & 0o777, 0o700)

    def test_commit_triggers_compaction_past_the_budget(self) -> None:
        journal = ChangeJournal(self.path, max_bytes=4096, retain_committed_entries=2)
        for n in range(40):
            entry = journal.append("setting.save", f"s{n}")
            journal.commit([entry.change_id], outcome="noop", config_hash="d" * 64)
        self.assertLessEqual(self.path.stat().st_size, 4096)
        self.assertEqual(journal.pending(), [])

    def test_pending_entries_are_merged_not_dropped_when_backups_keep_failing(self) -> None:
        journal = ChangeJournal(self.path, max_bytes=4096)
        for n in range(200):
            journal.append("mapping.save", f"slot-{n}", after_hash=f"{n:064x}")
        self.assertLessEqual(self.path.stat().st_size, 4096)
        pending = journal.pending()
        self.assertEqual(pending[0].action, "journal.overflow")
        self.assertEqual(pending[-1].subject, "slot-199")
        self.assertEqual(sum(entry.merged for entry in pending), 200)
        self.assertEqual(pending[0].subject, f"{pending[0].merged} older uncommitted changes merged")

    def test_commit_write_failure_keeps_entries_pending(self) -> None:
        co = ConfigBackupCoalescer(
            ChangeJournal(self.path), snapshot_config=dict, make_backup=self.make_backup, clock=self.clock,
            quiet_period=30, max_delay=600,
        )
        co.record_change("system.save", "lab-a")
        self.clock.advance(30)
        original = co.journal.commit

        def broken_commit(*args, **kwargs):
            raise OSError("read-only file system")

        co.journal.commit = broken_commit  # type: ignore[method-assign]
        result = co.tick()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.backup_id, "cfg-1")
        co.journal.commit = original  # type: ignore[method-assign]
        self.assertEqual(len(co.journal.pending()), 1)


class CoalescerTests(JournalTestCase):
    def test_burst_of_twenty_edits_makes_one_backup(self) -> None:
        co = self.coalescer()
        ids = []
        for n in range(20):
            ids.append(self.edit(co, n, f"SER{n:03d}").change_id)
            self.clock.advance(2)
            self.assertEqual(co.tick().status, "not_due")
        self.clock.advance(27)  # 29 s after the last edit
        self.assertEqual(co.tick().status, "not_due")
        self.clock.advance(1)
        result = co.tick()
        self.assertEqual(result.status, "backup")
        self.assertEqual(len(self.backups), 1)
        self.assertEqual(result.change_ids, tuple(ids))
        self.assertEqual(self.backups[0].change_ids, tuple(ids))
        self.assertEqual(co.tick().status, "idle")
        journal = ChangeJournal(self.path)
        self.assertEqual(journal.pending(), [])
        self.assertTrue(all(e.backup_id == "cfg-1" for e in journal.entries()))

    def test_edit_then_revert_makes_no_backup(self) -> None:
        co = self.coalescer()
        self.edit(co, 1, "SER1")
        self.clock.advance(31)
        self.assertEqual(co.tick().status, "backup")
        self.edit(co, 2, "SER2")
        del self.config.docs["slot_mappings.json"]["slot_mappings"]["2"]
        co.record_change("mapping.clear", "lab-a/slot-2")
        self.clock.advance(31)
        result = co.tick()
        self.assertEqual(result.status, "noop")
        self.assertEqual(len(self.backups), 1)
        entries = {e.change_id: e for e in ChangeJournal(self.path).entries()}
        self.assertTrue(all(entries[cid].status == "noop" and entries[cid].backup_id is None for cid in result.change_ids))

    def test_volatile_field_only_change_makes_no_backup(self) -> None:
        co = self.coalescer()
        self.edit(co, 1, "SER1")
        self.clock.advance(31)
        co.tick()
        self.config.docs["slot_mappings.json"]["updated_at"] = "much later"
        self.config.docs["config.yaml"]["updated_at"] = "much later"
        co.record_change("mapping.save", "lab-a/slot-1")
        self.clock.advance(31)
        self.assertEqual(co.tick().status, "noop")
        self.assertEqual(len(self.backups), 1)

    def test_first_ever_change_backs_up_even_without_a_prior_hash(self) -> None:
        co = self.coalescer()
        co.record_change("system.save", "lab-a")
        self.clock.advance(30)
        self.assertEqual(co.tick().status, "backup")

    def test_max_delay_caps_a_constant_stream_of_edits(self) -> None:
        co = self.coalescer()
        made_at = []
        for n in range(200):  # one edit every 10 s never leaves a 30 s quiet gap
            self.edit(co, n, f"S{n}")
            self.clock.advance(10)
            if co.tick().status == "backup":
                made_at.append(n)
        self.assertEqual(len(made_at), 3)  # 2000 s of edits / 600 s cap
        self.assertTrue(all(b - a == 60 for a, b in zip(made_at, made_at[1:])))
        backed_up = [cid for artifact in self.backups for cid in artifact.change_ids]
        self.assertEqual(len(backed_up), len(set(backed_up)))

    def test_restart_with_pending_entries_schedules_a_backup(self) -> None:
        co = self.coalescer()
        first = self.edit(co, 1, "SER1")
        del co  # process dies before the quiet period ends
        self.clock.advance(3600)
        restarted = self.coalescer()
        self.assertEqual(restarted.seconds_until_due(), 30)
        self.clock.advance(30)
        result = restarted.tick()
        self.assertEqual(result.status, "backup")
        self.assertEqual(result.change_ids, (first.change_id,))

    def test_crash_with_torn_line_still_backs_up(self) -> None:
        co = self.coalescer()
        self.config.docs["profiles.yaml"]["profiles"].append({"id": "p1"})
        with self.path.open("ab") as handle:
            handle.write(b'{"v":1,"type":"change","act')  # mutation landed, journal line did not
        restarted = self.coalescer()
        self.clock.advance(30)
        result = restarted.tick()
        self.assertEqual(result.status, "backup")
        self.assertEqual(len(result.change_ids), 1)
        self.assertTrue(result.change_ids[0].startswith("chg_recovered_"))
        self.assertEqual(ChangeJournal(self.path).pending(), [])
        del co

    def test_changes_from_another_process_are_picked_up_by_poll(self) -> None:
        co = self.coalescer()
        ChangeJournal(self.path).append("system.delete", "lab-b")
        self.assertEqual(co.tick().status, "not_due")
        self.clock.advance(30)
        self.assertEqual(co.tick().status, "backup")

    def test_failed_backup_keeps_entries_pending_and_retries_with_backoff(self) -> None:
        calls = []

        def flaky(change_ids):
            calls.append(change_ids)
            if len(calls) == 1:
                raise OSError("disk full")
            return {"artifact_id": "cfg-ok"}

        co = self.coalescer(make_backup=flaky)
        co.record_change("mapping.save", "slot-1")
        self.clock.advance(30)
        failed = co.tick()
        self.assertEqual(failed.status, "failed")
        self.assertIn("OSError", co.last_error or "")
        self.assertEqual(len(ChangeJournal(self.path).pending()), 1)
        self.assertEqual(co.tick().status, "not_due")
        self.clock.advance(30)
        ok = co.tick()
        self.assertEqual(ok.status, "backup")
        self.assertEqual(ok.backup_id, "cfg-ok")
        self.assertIsNone(co.last_error)

    def test_retry_delay_does_not_overflow_after_many_failures(self) -> None:
        def always_fail(change_ids):
            raise OSError("target down")

        co = self.coalescer(make_backup=always_fail)
        co.record_change("mapping.save", "slot-1")
        co._failures = 5000  # a week-plus of failures
        self.clock.advance(30)
        self.assertEqual(co.run().status, "failed")
        self.assertEqual(co.seconds_until_due(), 600)

    def test_backup_without_artifact_id_is_a_failure(self) -> None:
        co = self.coalescer(make_backup=lambda ids: {"name": "x"})
        co.record_change("mapping.save", "slot-1")
        self.clock.advance(30)
        self.assertEqual(co.tick().status, "failed")
        self.assertEqual(ChangeJournal(self.path).last_backup(), (None, None))

    def test_oversized_or_unsafe_artifact_id_is_a_failure_not_a_poisoned_commit(self) -> None:
        for bad in ("x" * 70000, "a" * 129, "../cfg", "cfg\nid"):
            with self.subTest(length=len(bad)):
                co = self.coalescer(make_backup=lambda ids, bad=bad: {"artifact_id": bad})
                co.record_change("mapping.save", "slot-1")
                self.clock.advance(30)
                self.assertEqual(co.run().status, "failed")
                self.assertTrue(all(len(line) < 64 * 1024 for line in self.path.read_bytes().splitlines()))
                self.assertEqual(ChangeJournal(self.path).last_backup(), (None, None))
        with self.assertRaises(ValueError):
            ChangeJournal(self.path).commit(["chg_x"], outcome="backup", config_hash="a" * 64, backup_id="b" * 200)

    def test_concurrent_triggers_make_a_single_backup(self) -> None:
        started = threading.Event()
        release = threading.Event()
        made = []

        def slow_backup(change_ids):
            made.append(change_ids)
            started.set()
            release.wait(5)
            return FakeArtifact(artifact_id=f"cfg-{len(made)}", change_ids=change_ids)

        co = self.coalescer(make_backup=slow_backup)
        other = self.coalescer(make_backup=slow_backup)  # a second instance on the same journal
        co.record_change("system.save", "lab-a")
        self.clock.advance(30)
        results = []
        worker = threading.Thread(target=lambda: results.append(co.run()))
        worker.start()
        self.assertTrue(started.wait(5))
        self.assertEqual(co.run().status, "busy")
        self.assertEqual(other.run().status, "busy")
        release.set()
        worker.join(5)
        self.assertEqual(results[0].status, "backup")
        self.assertEqual(len(made), 1)
        self.assertEqual(other.run().status, "idle")

    def test_many_threads_racing_tick(self) -> None:
        co = self.coalescer()
        for n in range(5):
            self.edit(co, n, f"S{n}")
        self.clock.advance(30)
        barrier = threading.Barrier(8)
        statuses = []

        def race():
            barrier.wait()
            statuses.append(co.tick().status)

        threads = [threading.Thread(target=race) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(statuses.count("backup"), 1)
        self.assertEqual(len(self.backups), 1)

    def test_change_during_a_run_is_not_lost(self) -> None:
        def backup_that_sees_a_new_edit(change_ids):
            if not self.backups:  # another writer edits while the first backup runs
                ChangeJournal(self.path).append("mapping.save", "slot-late")
                self.config.docs["profiles.yaml"]["profiles"].append({"id": "late"})
            return self.make_backup(change_ids)

        co = self.coalescer(make_backup=backup_that_sees_a_new_edit)
        co.record_change("mapping.save", "slot-1")
        self.clock.advance(30)
        first = co.tick()
        self.assertEqual(len(first.change_ids), 1)
        pending = ChangeJournal(self.path).pending()
        self.assertEqual([e.subject for e in pending], ["slot-late"])
        self.clock.advance(30)
        self.assertEqual(co.tick().status, "backup")
        self.assertEqual(ChangeJournal(self.path).pending(), [])

    def test_invalid_timing(self) -> None:
        with self.assertRaises(ValueError):
            self.coalescer_with(quiet_period=700, max_delay=600)

    def coalescer_with(self, **kwargs):
        return ConfigBackupCoalescer(
            ChangeJournal(self.path), snapshot_config=dict, make_backup=self.make_backup, clock=self.clock, **kwargs
        )


class JournalTimestampTests(JournalTestCase):
    def test_injected_utc_clock_is_recorded(self) -> None:
        fixed = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        journal = ChangeJournal(self.path, utcnow=lambda: fixed)
        entry = journal.append("setting.save")
        self.assertEqual(ChangeJournal(self.path).pending()[0].at, fixed)
        self.assertEqual(entry.at, fixed)
        self.assertIn("2026-09-24T12:00:00Z", self.path.read_text())
        self.assertEqual(os.stat(self.path).st_size, len(self.path.read_bytes()))


if __name__ == "__main__":
    unittest.main()
