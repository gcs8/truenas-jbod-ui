"""Backup integration (#398/#573): policy, cron, journal hooks, scheduler, API, health, Compose.

No network: remote targets are filesystem targets from the real transport
module, and the archive builder is a fake runner that writes a file.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from app.config_errors import ConfigurationError
from history_service.backup_archive.cron import CronError, CronSchedule
from history_service.backup_archive.journal import ChangeJournal
from history_service.backup_archive.policy import load_backup_policy
from history_service.backup_archive.transport import LocalDirectoryTarget

REPO_ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def asgi_call(app: Any, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None):
    """Minimal ASGI client (httpx is not a dependency)."""

    payload = b"" if body is None else json.dumps(body).encode()
    raw_headers = [(b"content-type", b"application/json"), (b"host", b"test")]
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": raw_headers, "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    }
    messages = [{"type": "http.request", "body": payload, "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive():
        if messages:
            return messages.pop(0)
        await asyncio.sleep(0.01)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    content = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, content


class CronTests(unittest.TestCase):
    def test_daily_and_steps(self) -> None:
        schedule = CronSchedule.parse("30 3 * * *")
        self.assertEqual(schedule.next_after(datetime(2026, 9, 24, 3, 29, tzinfo=UTC)), datetime(2026, 9, 24, 3, 30, tzinfo=UTC))
        self.assertEqual(schedule.next_after(datetime(2026, 9, 24, 3, 30, tzinfo=UTC)), datetime(2026, 9, 25, 3, 30, tzinfo=UTC))
        every = CronSchedule.parse("*/15 * * * *")
        self.assertEqual(every.next_after(datetime(2026, 1, 1, 0, 14, 59, tzinfo=UTC)), datetime(2026, 1, 1, 0, 15, tzinfo=UTC))

    def test_weekday_and_shortcuts(self) -> None:
        sunday = CronSchedule.parse("@weekly")  # Sunday 00:00
        self.assertEqual(sunday.next_after(datetime(2026, 9, 24, tzinfo=UTC)), datetime(2026, 9, 27, tzinfo=UTC))
        seven = CronSchedule.parse("0 0 * * 7")
        self.assertEqual(seven.weekdays, frozenset({0}))
        either = CronSchedule.parse("0 0 1 * 1")  # 1st of month OR Monday
        self.assertEqual(either.next_after(datetime(2026, 9, 24, tzinfo=UTC)), datetime(2026, 9, 28, tzinfo=UTC))
        self.assertEqual(CronSchedule.parse("0 12 29 2 *").next_after(datetime(2026, 3, 1, tzinfo=UTC)).year, 2028)

    def test_invalid(self) -> None:
        for text in ("", "* * * *", "60 * * * *", "* * 0 * *", "a * * * *", "*/0 * * * *", "5-1 * * * *", "0 0 31 2 *"):
            with self.subTest(text=text), self.assertRaises(CronError):
                CronSchedule.parse(text).next_after(datetime(2026, 1, 1, tzinfo=UTC))


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.yaml"

    def write(self, backups: Any) -> None:
        self.config.write_text(yaml.safe_dump({"backups": backups}))

    def test_defaults_are_disabled(self) -> None:
        policy = load_backup_policy(self.config, {})
        self.assertFalse(policy.config.enabled)
        self.assertFalse(policy.full.enabled)
        self.assertEqual(policy.targets, ())

    def test_yaml_and_env_overrides(self) -> None:
        self.write({
            "config": {"enabled": True, "debounce_seconds": 5, "max_delay_seconds": 60, "local_keep": 3},
            "full": {"schedule": "@daily", "remote_max_age_days": 30},
            "targets": [{"target_id": "nas", "label": "NAS", "provider": "filesystem", "root": "/srv/b"}],
        })
        policy = load_backup_policy(self.config, {"BACKUP_FULL_ENABLED": "true", "BACKUP_CONFIG_REMOTE_KEEP": "9"})
        self.assertTrue(policy.config.enabled and policy.full.enabled)
        self.assertEqual(policy.config.remote_keep, 9)
        self.assertEqual(policy.full.schedule, "@daily")
        self.assertEqual(policy.max_age("full"), timedelta(days=30))
        self.assertEqual(policy.targets[0].label, "NAS")

    def test_full_archive_format_defaults_to_7z_and_accepts_tar_zst(self) -> None:
        # #397: the fast format is opt-in so older app versions can still restore by default.
        self.write({"full": {"enabled": True}})
        self.assertEqual(load_backup_policy(self.config, {}).full.archive_format, "7z")
        policy = load_backup_policy(self.config, {"BACKUP_FULL_ARCHIVE_FORMAT": "tar.zst"})
        self.assertEqual(policy.full.archive_format, "tar.zst")
        self.write({"full": {"archive_format": "zip"}})
        with self.assertRaises(ConfigurationError) as caught:
            load_backup_policy(self.config, {})
        self.assertTrue(any("backups.full.archive_format" in p for p in caught.exception.problems))

    def test_errors_are_plain_sentences_naming_the_source(self) -> None:
        self.write({"config": {"debounce_seconds": "soon"}, "full": {"schedule": "bad"}})
        with self.assertRaises(ConfigurationError) as caught:
            load_backup_policy(self.config, {"BACKUP_CONFIG_LOCAL_KEEP": "0"})
        problems = caught.exception.problems
        self.assertTrue(any(p.startswith("backups.config.debounce_seconds in ") and "whole number" in p for p in problems), problems)
        self.assertTrue(any(p.startswith("BACKUP_CONFIG_LOCAL_KEEP in the environment must be 1 or more") for p in problems), problems)
        self.assertTrue(any("backups.full.schedule" in p and "cron" in p for p in problems), problems)
        for problem in problems:
            self.assertNotIn("\n", problem)

    def test_unknown_keys_and_delay_rule(self) -> None:
        self.write({"config": {"enabled": True, "debounce_seconds": 100, "max_delay_seconds": 10}})
        with self.assertRaises(ConfigurationError) as caught:
            load_backup_policy(self.config, {})
        self.assertIn("max_delay_seconds must be at least", " ".join(caught.exception.problems))
        self.write({"surprise": 1})
        with self.assertRaises(ConfigurationError):
            load_backup_policy(self.config, {})

    def test_targets_reject_inline_secrets_and_duplicates(self) -> None:
        env = {"BACKUP_TARGETS_JSON": json.dumps([
            {"target_id": "s3", "provider": "s3", "root": "b", "bucket": "bk", "secret_access_key": "x"},
            {"target_id": "a", "provider": "filesystem", "root": "/a"},
            {"target_id": "a", "provider": "filesystem", "root": "/b"},
            {"target_id": "local", "provider": "filesystem", "root": "/c"},
        ])}
        with self.assertRaises(ConfigurationError) as caught:
            load_backup_policy(self.config, env)
        text = " ".join(caught.exception.problems)
        self.assertIn("BACKUP_TARGETS_JSON[0] in the environment", text)
        self.assertIn("*_file", text)
        self.assertNotIn("\"x\"", text)
        self.assertEqual(sum("must be unique" in p for p in caught.exception.problems), 2)
        with self.assertRaises(ConfigurationError):
            load_backup_policy(self.config, {"BACKUP_TARGETS_JSON": "{not json"})


class JournalHookTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.services import config_change_journal

        self.module = config_change_journal
        self.module.reset_for_tests()
        self.addCleanup(self.module.reset_for_tests)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal_dir = Path(self.temp.name) / "journal"
        self.journal_dir.mkdir(mode=0o2770)
        self.path = self.journal_dir / "config-changes.jsonl"

    def env(self, **extra: str):
        return patch.dict(os.environ, {"BACKUP_JOURNAL_PATH": str(self.path), **extra})

    def test_disabled_by_default_writes_nothing(self) -> None:
        with self.env(BACKUP_CONFIG_ENABLED="false"):
            self.assertFalse(self.module.record_config_change("mapping.save", "a:b:1"))
        self.assertFalse(self.path.exists())

    def test_enabled_appends_shared_mode_entry(self) -> None:
        with self.env(BACKUP_CONFIG_ENABLED="true"):
            self.assertTrue(self.module.record_config_change("mapping.save", "sys:enc:3"))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o660)
        entries = ChangeJournal(self.path, file_mode=0o660).pending()
        self.assertEqual([(e.action, e.subject) for e in entries], [("mapping.save", "sys:enc:3")])

    def test_yaml_section_enables_and_is_not_an_unknown_key(self) -> None:
        from app.config import Settings, collect_unknown_config_keys

        config = Path(self.temp.name) / "config.yaml"
        config.write_text(yaml.safe_dump({"backups": {"config": {"enabled": True}}, "bogus": 1}))
        with self.env(APP_CONFIG_PATH=str(config), BACKUP_CONFIG_ENABLED=""):
            self.assertTrue(self.module.config_backups_enabled())
            self.assertTrue(self.module.record_config_change("profile.save", "p"))
        self.assertEqual(collect_unknown_config_keys(yaml.safe_load(config.read_text()), Settings), ["bogus"])

    def test_errors_never_propagate(self) -> None:
        with self.env(BACKUP_CONFIG_ENABLED="true"):
            self.assertFalse(self.module.record_config_change("Bad Action!", "x"))
            with patch.object(self.module, "_journal_for", side_effect=OSError("disk full")):
                self.assertFalse(self.module.record_config_change("mapping.save", "x"))
        with patch.dict(os.environ, {"BACKUP_JOURNAL_PATH": "/nonexistent-dir/j.jsonl", "BACKUP_CONFIG_ENABLED": "1"}):
            self.assertFalse(self.module.record_config_change("mapping.save", "x"))

    def test_config_mutation_points_record_changes(self) -> None:
        from app.models.domain import ManualMapping
        from app.services.mapping_store import MappingStore
        from app.services.sas_fabric_alias_store import SasFabricAliasStore
        from app.services.system_setup import SystemSetupService

        root = Path(self.temp.name)
        with self.env(BACKUP_CONFIG_ENABLED="true"):
            store = MappingStore(root / "slot_mappings.json")
            store.save_mapping(ManualMapping(system_id="s", enclosure_id="e", slot=1, serial="X"))
            store.clear_mapping("s", "e", 1)
            aliases = SasFabricAliasStore(root / "aliases.json")
            from app.models.domain import SasFabricAlias

            aliases.save_alias(SasFabricAlias(system_id="s", object_id="o", label="A"))
            aliases.clear_alias("s", None, "o")
            config_path = root / "config.yaml"
            config_path.write_text(yaml.safe_dump({"systems": [{"id": "keep"}, {"id": "gone"}]}))
            SystemSetupService(str(config_path)).delete_system("gone")
        actions = [e.action for e in ChangeJournal(self.path, file_mode=0o660).pending()]
        self.assertEqual(actions, ["mapping.save", "mapping.clear", "sas_alias.save", "sas_alias.clear", "system.delete"])

    def test_hooks_are_wired_at_every_writer(self) -> None:
        expectations = {
            "app/services/mapping_store.py": ["mapping.save", "mapping.clear", "mapping.replace", "mapping.import"],
            "app/services/sas_fabric_alias_store.py": ["sas_alias.save", "sas_alias.clear"],
            "app/services/system_setup.py": ["system.save", "system.delete"],
            "app/services/profile_builder.py": ["profile.save", "profile.delete"],
            "admin_service/routes.py": ["runtime_overrides.save", "backup.restore"],
        }
        for relative, actions in expectations.items():
            source = (REPO_ROOT / relative).read_text()
            for action in actions:
                with self.subTest(file=relative, action=action):
                    self.assertIn(f'"{action}"', source)


class FakeRunner:
    """Stands in for ScheduledBackupRunner: writes a deterministic archive."""

    calls: list[dict[str, Any]] = []
    fail = False

    def __init__(self, backup_service, *, destination_dir, included_groups, clock, **kwargs) -> None:
        self.destination = Path(destination_dir)
        self.groups = list(included_groups)
        self.clock = clock
        self.kwargs = kwargs
        self.last_manifest = {"app_version": "9.9.9", "schema_version": 1, "packaging": "tar.zst"}
        FakeRunner.calls.append({"groups": self.groups, **kwargs})

    def run_once(self) -> dict[str, Any]:
        if FakeRunner.fail:
            raise RuntimeError("export failed")
        self.destination.mkdir(parents=True, exist_ok=True)
        name = f"jbod-backup-{len(FakeRunner.calls):04d}.tar.zst.enc"
        content = f"archive {len(FakeRunner.calls)} {self.groups}".encode()
        (self.destination / name).write_bytes(content)
        return {"last_artifact_name": name, "last_size_bytes": len(content), "last_sha256": hashlib.sha256(content).hexdigest()}


class SchedulerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        from history_service.backup_scheduler.service import BackupScheduler, SchedulerPaths

        FakeRunner.calls = []
        FakeRunner.fail = False
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status_dir = self.root / "status"
        self.status_dir.mkdir()
        os.chmod(self.status_dir, 0o2750)
        (self.root / "journal").mkdir(mode=0o2770)
        self.remote_root = self.root / "remote"
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        self.mono = [1000.0]
        self.snapshot = {"config": {"a": 1}}
        self.broken_targets: set[str] = set()
        self._BackupScheduler = BackupScheduler
        self._paths = SchedulerPaths(
            local_dir=self.root / "local",
            state_dir=self.root / "state",
            journal_path=self.root / "journal" / "config-changes.jsonl",
            status_file=self.status_dir / "backup-archive.json",
            passphrase_file=self.root / "passphrase",
            runner_status_dir=self.status_dir,
        )

    def open_target(self, settings):
        @contextlib.contextmanager
        def opened():
            if settings.target_id in self.broken_targets:
                raise ConnectionRefusedError("connection refused by nas.example.test")
            yield LocalDirectoryTarget(self.remote_root / settings.target_id)

        return opened()

    def make(self, backups: dict[str, Any]):
        config = self.root / "config.yaml"
        config.write_text(yaml.safe_dump({"backups": backups}))
        policy = load_backup_policy(config, {})
        scheduler = self._BackupScheduler(
            policy,
            object(),
            self._paths,
            app_gid=os.getegid(),
            config_groups=["config_file", "mapping_file"],
            full_groups=["config_file", "mapping_file", "history_db"],
            snapshot_config=lambda: self.snapshot,
            open_target_fn=self.open_target,
            runner_factory=FakeRunner,
            clock=lambda: self.now,
            monotonic=lambda: self.mono[0],
            local_tz=UTC,
        )
        self.addCleanup(scheduler.close)
        return scheduler


TARGET = {"target_id": "nas", "label": "Office NAS", "provider": "filesystem", "root": "/unused"}


class SchedulerTests(SchedulerTestBase):
    def test_config_changes_coalesce_into_one_backup_shipped_and_catalogued(self) -> None:
        scheduler = self.make({"config": {"enabled": True, "debounce_seconds": 10, "max_delay_seconds": 60}, "targets": [TARGET]})
        journal = ChangeJournal(self._paths.journal_path, file_mode=0o660)
        journal.append("mapping.save", "s:e:1")
        journal.append("mapping.save", "s:e:2")
        scheduler.tick()
        self.assertEqual(FakeRunner.calls, [])  # debounce
        self.mono[0] += 11
        scheduler.tick()
        self.assertEqual(len(FakeRunner.calls), 1)
        self.assertNotIn("history_db", FakeRunner.calls[0]["groups"])
        self.assertIs(FakeRunner.calls[0]["apply_retention"], False)
        records = scheduler.catalog.list()
        self.assertEqual(sorted(r.location for r in records), ["local", "nas"])
        self.assertTrue(all(len(r.change_ids) == 2 for r in records))
        remote = next(r for r in records if r.location == "nas")
        self.assertTrue((self.remote_root / "nas" / remote.name).is_file())
        self.assertEqual(journal.pending(), [])
        detail = scheduler.detail(remote.artifact_id)
        self.assertEqual([c["action"] for c in detail["changes"]], ["mapping.save", "mapping.save"])
        self.assertEqual(detail["inspect"]["app_version"], "9.9.9")
        # Same config again: a no-op, not another backup.
        journal.append("profile.save", "p")
        self.mono[0] += 11
        scheduler.tick()
        self.mono[0] += 11
        scheduler.tick()
        self.assertEqual(len(FakeRunner.calls), 1)

    def test_full_runs_use_the_policy_archive_format_and_config_runs_stay_7z(self) -> None:
        FakeRunner.calls.clear()
        scheduler = self.make({
            "config": {"enabled": True},
            "full": {"enabled": True, "archive_format": "tar.zst"},
        })
        scheduler.run_now("full")
        scheduler.run_now("config")
        formats = [call.get("archive_format") for call in FakeRunner.calls]
        self.assertEqual(formats, ["tar.zst", "7z"])

    def test_full_backup_runs_on_cron_and_grooms_local_keep(self) -> None:
        scheduler = self.make({"full": {"enabled": True, "schedule": "0 * * * *", "local_keep": 2}})
        self.assertEqual(scheduler.next_full_at, datetime(2026, 9, 24, 13, 0, tzinfo=UTC))
        for hour in (13, 14, 15):
            self.now = datetime(2026, 9, 24, hour, 0, tzinfo=UTC)
            scheduler.tick()
        self.assertEqual(len(FakeRunner.calls), 3)
        self.assertIn("history_db", FakeRunner.calls[0]["groups"])
        local = scheduler.catalog.list(location="local")
        self.assertEqual(len(local), 2)
        self.assertEqual(len(list((self._paths.local_dir / "full").iterdir())), 2)
        self.assertEqual(len(scheduler.catalog.tombstones()), 1)

    def test_target_failure_is_degraded_status_and_other_targets_still_ship(self) -> None:
        second = {**TARGET, "target_id": "cloud", "label": "Cloud"}
        scheduler = self.make({"full": {"enabled": True}, "targets": [TARGET, second]})
        self.broken_targets = {"nas"}
        scheduler.run_now("full")
        self.assertEqual(sorted(r.location for r in scheduler.catalog.list()), ["cloud", "local"])
        status = json.loads(self._paths.status_file.read_text())
        self.assertFalse(status["targets"]["nas"]["ok"])
        self.assertIn("connection refused", status["targets"]["nas"]["detail"])
        self.assertTrue(status["targets"]["cloud"]["ok"])
        self.assertEqual(stat.S_IMODE(self._paths.status_file.stat().st_mode), 0o640)

        from app.services.backup_health import backup_archive_problems

        problems = backup_archive_problems(self._paths.status_file)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("Backup target Office NAS degraded:"))

    def test_failed_backup_recorded_and_single_flight(self) -> None:
        from history_service.backup_scheduler.service import SchedulerBusyError

        scheduler = self.make({"full": {"enabled": True}})
        FakeRunner.fail = True
        with self.assertRaises(RuntimeError):
            scheduler.run_now("full")
        from app.services.backup_health import backup_archive_problems

        self.assertEqual(backup_archive_problems(self._paths.status_file), ["Full backup failed: RuntimeError: export failed"])
        FakeRunner.fail = False
        with scheduler._job("full"), self.assertRaises(SchedulerBusyError):
            scheduler.run_now("full")
        with self.assertRaises(ValueError):
            scheduler.run_now("everything")

    def test_plan_token_preserve_verify_and_remote_materialize(self) -> None:
        scheduler = self.make({"full": {"enabled": True, "local_keep": 1, "remote_keep": 1}, "targets": [TARGET]})
        first = scheduler.run_now("full")
        self.now += timedelta(hours=1)
        scheduler.preserve(first.artifact_id, reason="before upgrade", actor="admin")
        scheduler.run_now("full")
        # The preserved local copy survived grooming; the older remote copy was groomed.
        self.assertIsNotNone(scheduler.catalog.get(first.artifact_id))
        remote = scheduler.catalog.list(location="nas")
        self.assertEqual(len(remote), 1)
        verified = scheduler.verify(remote[0].artifact_id)
        self.assertTrue(verified["ok"], verified)
        with scheduler.materialize(remote[0].artifact_id) as (_record, path):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), remote[0].sha256)
        self.assertEqual(list(self._paths.state_dir.glob("backup-fetch-*")), [])
        # Tamper with the remote copy: verify reports, never raises, and the copy
        # stops counting as verified (not restorable, not the protected newest).
        (self.remote_root / "nas" / remote[0].name).write_bytes(b"tampered")
        failed = scheduler.verify(remote[0].artifact_id)
        self.assertFalse(failed["ok"])
        self.assertFalse(failed["artifact"]["verified"])
        self.assertFalse(failed["artifact"]["restorable"])
        from history_service.backup_scheduler.service import ArchiveIntegrityError

        with self.assertRaises(ArchiveIntegrityError), scheduler.materialize(remote[0].artifact_id):
            pass
        self.assertEqual(list(self._paths.state_dir.glob("backup-fetch-*")), [])
        scheduler.unpreserve(first.artifact_id, actor="admin")
        token, _expires, plan = scheduler.plan()
        self.assertEqual([item.record.artifact_id for item in plan.items], [first.artifact_id])
        result = scheduler.apply(token)
        self.assertTrue(result.complete)
        with self.assertRaises(LookupError):
            scheduler.apply(token)
        _token2, _e, _p = scheduler.plan()
        self.mono[0] += 10_000
        with self.assertRaises(LookupError):
            scheduler.apply(_token2)

    def test_substituted_remote_backup_is_never_served(self) -> None:
        from history_service.backup_scheduler.service import ArchiveIntegrityError

        scheduler = self.make({"full": {"enabled": True}, "targets": [TARGET]})
        first = scheduler.run_now("full")
        self.now += timedelta(hours=1)
        scheduler.run_now("full")
        remote_first, remote_second = scheduler.catalog.list(location="nas")
        # Swap in another *valid* backup under the first name.
        (self.remote_root / "nas" / remote_first.name).write_bytes(
            (self.remote_root / "nas" / remote_second.name).read_bytes()
        )
        with self.assertRaises(ArchiveIntegrityError), scheduler.materialize(remote_first.artifact_id):
            pass
        (self._paths.local_dir / first.name).write_bytes(b"x" * first.size)
        with self.assertRaises(ArchiveIntegrityError), scheduler.materialize(first.artifact_id):
            pass

    def test_start_run_reserves_before_returning(self) -> None:
        import threading

        from history_service.backup_scheduler.service import SchedulerBusyError

        scheduler = self.make({"full": {"enabled": True}})
        gate = threading.Event()
        original = FakeRunner.run_once

        def slow(runner):
            gate.wait(5)
            return original(runner)

        with patch.object(FakeRunner, "run_once", slow):
            thread = scheduler.start_run("full")
            # Reserved synchronously: a tick or second request cannot slip in.
            self.assertEqual(scheduler.running["backup_class"], "full")
            with self.assertRaises(SchedulerBusyError):
                scheduler.start_run("config")
            with self.assertRaises(SchedulerBusyError):
                scheduler.run_now("full")
            gate.set()
            thread.join(5)
        self.assertIsNone(scheduler.running)
        self.assertEqual(len(scheduler.catalog.list()), 1)

    def test_idle_scheduler_needs_no_passphrase(self) -> None:
        from history_service.backup_scheduler import main as scheduler_main

        config = self.root / "idle.yaml"
        config.write_text("{}\n")
        with patch.dict(os.environ, {"APP_CONFIG_PATH": str(config), "APP_GID": str(os.getegid()),
                                     "BACKUP_ARCHIVE_PASSPHRASE_FILE": "", "SCHEDULED_BACKUP_PASSPHRASE_FILE": "",
                                     "BACKUP_ARCHIVE_DIR": str(self.root / "a"),
                                     "BACKUP_ARCHIVE_STATE_DIR": str(self.root / "s")}):
            policy = load_backup_policy(config, {})
            with patch("history_service.system_backup.SystemBackupService"), patch("history_service.store.HistoryStore"):
                scheduler = scheduler_main.build_scheduler(policy)
            self.addCleanup(scheduler.close)
            self.assertFalse(scheduler.library()["classes"]["full"]["enabled"])
            enabled = load_backup_policy(config, {"BACKUP_FULL_ENABLED": "true"})
            with self.assertRaises(ConfigurationError):
                scheduler_main.build_scheduler(enabled)

    def test_library_shape_matches_contract(self) -> None:
        scheduler = self.make({"config": {"enabled": True}, "full": {"enabled": True}, "targets": [TARGET]})
        scheduler.run_now("full")
        library = scheduler.library()
        self.assertTrue(library["available"])
        self.assertEqual(set(library["classes"]["config"]), {
            "enabled", "debounce_seconds", "max_delay_seconds", "local_keep", "remote_keep",
            "remote_max_age_days", "pending_changes", "last_run"})
        self.assertEqual(set(library["classes"]["full"]), {
            "enabled", "schedule", "next_run_at", "local_keep", "remote_keep", "remote_max_age_days", "last_run"})
        self.assertEqual(set(library["targets"][0]), {"id", "label", "provider", "transport_encrypted", "enabled", "last_run"})
        artifact_keys = {"id", "backup_class", "location", "created_at", "size", "sha256", "verified", "restorable",
                         "state", "preserved", "preserve_reason", "preserved_by", "change_count", "app_version"}
        for artifact in library["artifacts"]:
            self.assertEqual(set(artifact), artifact_keys)
            self.assertNotIn("/", artifact["id"])
        self.assertEqual(library["storage"]["local"]["count"], 1)
        serialized = json.dumps(library)
        self.assertNotIn(str(self.root), serialized)

    def test_missing_local_file_is_reported(self) -> None:
        scheduler = self.make({"full": {"enabled": True}})
        record = scheduler.run_now("full")
        (self._paths.local_dir / record.name).unlink()
        self.assertEqual(scheduler.serialize(record)["state"], "missing")


class SchedulerApiTests(SchedulerTestBase):
    def test_internal_routes(self) -> None:
        from history_service.backup_scheduler.api import build_app

        scheduler = self.make({"full": {"enabled": True}, "targets": [TARGET]})
        record = scheduler.run_now("full")
        app = build_app(scheduler)
        status, body = asgi_call(app, "GET", "/internal/backups")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["artifacts"]), 2)
        status, body = asgi_call(app, "GET", f"/internal/backups/{record.artifact_id}/download")
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(body).hexdigest(), record.sha256)
        self.assertEqual(asgi_call(app, "GET", "/internal/backups/nope")[0], 404)
        status, body = asgi_call(app, "POST", f"/internal/backups/{record.artifact_id}/preserve", {"reason": "keep"}, {"X-Backup-Actor": "alice"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["artifact"]["preserved_by"], "alice")
        self.assertEqual(asgi_call(app, "POST", f"/internal/backups/{record.artifact_id}/preserve", {"reason": ""})[0], 400)
        status, body = asgi_call(app, "GET", "/internal/backups/lifecycle/plan")
        plan = json.loads(body)
        self.assertEqual(set(plan), {"plan_token", "expires_at", "items", "guarded"})
        status, body = asgi_call(app, "POST", "/internal/backups/lifecycle/apply", {"plan_token": plan["plan_token"]})
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(body)), {"ok", "deleted", "already_missing", "failed", "not_attempted"})
        self.assertEqual(asgi_call(app, "POST", "/internal/backups/lifecycle/apply", {"plan_token": plan["plan_token"]})[0], 409)
        status, body = asgi_call(app, "POST", "/internal/backups/targets/nas/test")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(asgi_call(app, "POST", "/internal/backups/run", {"backup_class": "nope"})[0], 400)
        with scheduler._job("full"):
            self.assertEqual(asgi_call(app, "POST", "/internal/backups/run", {"backup_class": "full"})[0], 409)


class AdminProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        from admin_service import main as admin_main

        self.admin_main = admin_main
        admin_main.get_backup_scheduler_client.cache_clear()
        self.addCleanup(admin_main.get_backup_scheduler_client.cache_clear)

    def call(self, method: str, path: str, body: Any = None):
        # Origin and auth policy have their own suites; other tests may change the
        # module-level admin settings, so let these requests through explicitly.
        with patch.object(self.admin_main, "_request_origin_allowed", return_value=True), \
                patch.object(self.admin_main, "_basic_auth_matches", return_value=True):
            return asgi_call(self.admin_main.app, method, path, body, {"origin": "http://test"})

    def test_library_reports_unavailable_without_scheduler(self) -> None:
        with patch.dict(os.environ, {"BACKUP_SCHEDULER_SOCKET": "/nonexistent/scheduler.sock"}):
            status, body = self.call("GET", "/api/admin/backups")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertFalse(payload["available"])
            self.assertEqual(payload["artifacts"], [])
            self.assertEqual(self.call("POST", "/api/admin/backups/run", {"backup_class": "full"})[0], 503)

    def test_routes_forward_and_validate(self) -> None:
        from admin_service.services.backup_scheduler_client import SchedulerResponse

        calls: list[tuple[str, str, Any]] = []

        class FakeClient:
            def request(self, method, path, *, body=None, actor=None):
                calls.append((method, path, body))
                if path.endswith("/run"):
                    return SchedulerResponse(409, {"detail": "A backup or grooming run is already in progress."})
                return SchedulerResponse(200, {"ok": True})

        with patch.object(self.admin_main, "get_backup_scheduler_client", return_value=FakeClient()):
            self.assertEqual(self.call("POST", "/api/admin/backups/abc123/verify")[0], 200)
            self.assertEqual(self.call("POST", "/api/admin/backups/abc123/preserve", {"reason": "x"})[0], 200)
            self.assertEqual(self.call("DELETE", "/api/admin/backups/abc123/preserve")[0], 200)
            self.assertEqual(self.call("POST", "/api/admin/backups/lifecycle/apply", {"plan_token": "t"})[0], 200)
            self.assertEqual(self.call("POST", "/api/admin/backups/run", {"backup_class": "full"})[0], 409)
            self.assertEqual(self.call("POST", "/api/admin/backups/run", {"backup_class": "bogus"})[0], 400)
            self.assertEqual(self.call("GET", "/api/admin/backups/..%2Fetc")[0], 404)
        self.assertIn(("POST", "/internal/backups/abc123/verify", None), calls)
        self.assertIn(("POST", "/internal/backups/abc123/preserve", {"reason": "x"}), calls)

    def test_cross_origin_mutation_is_rejected(self) -> None:
        status, _ = asgi_call(self.admin_main.app, "POST", "/api/admin/backups/run", {"backup_class": "full"},
                              {"origin": "http://evil.example.test"})
        self.assertEqual(status, 403)


class PolicyEditorTests(unittest.TestCase):
    """#573: edit backup policy and targets; secrets stay file-only."""

    def setUp(self) -> None:
        import threading

        from history_service.backup_archive import editor

        self.editor = editor
        self.lock = threading.RLock()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "config.yaml"
        secrets_dir = self.root / "backup-secrets"
        secrets_dir.mkdir()
        key = secrets_dir / "archive_sftp_key"
        key.write_text("synthetic-key\n")
        key.chmod(0o600)
        self.config.write_text(yaml.safe_dump({
            "app": {"title": "keep me"},
            "backups": {
                "config": {"enabled": True, "local_keep": 30},
                "targets": [{
                    "target_id": "office-nas", "label": "Office NAS", "provider": "sftp",
                    "hostname": "nas.example.test", "username": "backup", "root": "/srv/backups/jbod",
                    "known_hosts_path": "/run/backup-secrets/archive_known_hosts",
                    "private_key_file": "/run/backup-secrets/archive_sftp_key",
                    "password_file": "/run/backup-secrets/missing_password",
                }],
            },
        }, sort_keys=False))
        self.journal: list[tuple[str, str]] = []

    def view(self, env: dict[str, str] | None = None) -> dict[str, Any]:
        return self.editor.load_editor_view(self.config, env or {})

    def save(self, payload: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, Any]:
        return self.editor.apply_editor_change(
            self.config, payload, env or {}, write_lock=self.lock,
            record_change=lambda action, subject: self.journal.append((action, subject)),
        )

    def test_view_never_returns_secret_paths_only_present_or_missing(self) -> None:
        view = self.view()
        text = json.dumps(view)
        self.assertNotIn("archive_sftp_key", text)
        self.assertNotIn("missing_password", text)
        secrets = view["targets"][0]["secrets"]
        self.assertEqual(secrets["private_key_file"], {"configured": True, "present": True})
        self.assertEqual(secrets["password_file"], {"configured": True, "present": False})
        self.assertEqual(secrets["access_key_id_file"], {"configured": False, "present": None})
        self.assertEqual(view["classes"]["config"]["values"]["local_keep"], 30)
        self.assertEqual(view["problems"], [])

    def test_group_readable_secret_file_is_reported_missing(self) -> None:
        (self.root / "backup-secrets" / "archive_sftp_key").chmod(0o640)
        self.assertFalse(self.view()["targets"][0]["secrets"]["private_key_file"]["present"])

    def test_save_edits_policy_keeps_omitted_secrets_other_sections_and_journals(self) -> None:
        view = self.view()
        target = view["targets"][0]
        target["values"]["label"] = "Office NAS 2"
        payload = {
            "revision": view["revision"],
            "classes": {"full": {"enabled": True, "schedule": "30 2 * * *", "local_keep": 3}},
            "targets": [{"values": target["values"], "original_target_id": target["original_target_id"],
                         "secrets": {"password_file": None}}],
        }
        saved = self.save(payload)
        stored = yaml.safe_load(self.config.read_text())
        self.assertEqual(stored["app"], {"title": "keep me"})
        self.assertEqual(stored["backups"]["full"]["schedule"], "30 2 * * *")
        stored_target = stored["backups"]["targets"][0]
        self.assertEqual(stored_target["label"], "Office NAS 2")
        self.assertEqual(stored_target["private_key_file"], "/run/backup-secrets/archive_sftp_key")
        self.assertNotIn("password_file", stored_target)
        self.assertEqual(self.journal, [("backups.policy.save", "config=on full=on targets=1")])
        self.assertNotEqual(saved["revision"], view["revision"])
        self.assertEqual(list(self.root.glob(".config.yaml*")), [])

    def test_save_replaces_a_secret_by_file_path_only(self) -> None:
        view = self.view()
        values = view["targets"][0]["values"]
        payload = {"revision": view["revision"], "targets": [{"values": values, "secrets": {"password_file": "/run/backup-secrets/new_password"}}]}
        self.save(payload)
        self.assertEqual(
            yaml.safe_load(self.config.read_text())["backups"]["targets"][0]["password_file"],
            "/run/backup-secrets/new_password",
        )
        for bad in ("relative/path", "/run/backup-secrets/../etc/shadow", ""):
            with self.subTest(bad=bad):
                view = self.view()
                with self.assertRaises(self.editor.PolicyEditError):
                    self.save({"revision": view["revision"], "targets": [{"values": values, "secrets": {"password_file": bad}}]})

    def _target_payload(self, view: dict[str, Any], values: dict[str, Any], secrets: dict[str, Any] | None = None,
                        original: str | None = "office-nas") -> dict[str, Any]:
        entry: dict[str, Any] = {"values": values, "secrets": secrets or {}}
        if original is not None:
            entry["original_target_id"] = original
        return {"revision": view["revision"], "targets": [entry]}

    def test_renaming_a_target_keeps_its_secret_files(self) -> None:
        view = self.view()
        self.assertEqual(view["targets"][0]["original_target_id"], "office-nas")
        values = dict(view["targets"][0]["values"], target_id="office-nas-2")
        self.save(self._target_payload(view, values))
        stored = yaml.safe_load(self.config.read_text())["backups"]["targets"][0]
        self.assertEqual(stored["target_id"], "office-nas-2")
        self.assertEqual(stored["private_key_file"], "/run/backup-secrets/archive_sftp_key")

    def test_new_row_reusing_an_old_id_inherits_no_secrets(self) -> None:
        view = self.view()
        before = self.config.read_bytes()
        values = dict(view["targets"][0]["values"])
        with self.assertRaises(self.editor.PolicyEditError):
            # sftp needs a key or password; a fresh row gets neither from the old one.
            self.save(self._target_payload(view, values, original=None))
        self.assertEqual(self.config.read_bytes(), before)

    def test_changing_where_a_target_points_requires_choosing_secrets_again(self) -> None:
        for field, value in (("hostname", "attacker.example.test"), ("username", "other"),
                             ("port", 2222), ("provider", "ftp")):
            with self.subTest(field=field):
                view = self.view()
                before = self.config.read_bytes()
                values = dict(view["targets"][0]["values"], **{field: value})
                with self.assertRaisesRegex(self.editor.PolicyEditError, "choose its .* again"):
                    self.save(self._target_payload(view, values))
                self.assertEqual(self.config.read_bytes(), before)
        view = self.view()
        values = dict(view["targets"][0]["values"], hostname="nas2.example.test")
        self.save(self._target_payload(view, values, {
            "private_key_file": "/run/backup-secrets/archive_sftp_key", "password_file": None,
        }))
        stored = yaml.safe_load(self.config.read_text())["backups"]["targets"][0]
        self.assertEqual(stored["hostname"], "nas2.example.test")
        self.assertNotIn("password_file", stored)

    def test_secret_files_must_be_target_credentials_in_the_secrets_folder(self) -> None:
        env = {"BACKUP_ARCHIVE_PASSPHRASE_FILE": "/run/backup-secrets/archive-pass"}
        for bad, message in (
            ("/run/backup-secrets/scheduled-backup-passphrase", "passphrase file"),
            ("/run/backup-secrets/archive-pass", "passphrase file"),
            ("/run/backup-secrets//archive-pass", "passphrase file"),
            ("/etc/shadow", "must be a file in /run/backup-secrets"),
            ("/run/backup-secrets", "must be a file in /run/backup-secrets"),
        ):
            with self.subTest(bad=bad):
                view = self.view(env)
                before = self.config.read_bytes()
                values = dict(view["targets"][0]["values"])
                with self.assertRaisesRegex(self.editor.PolicyEditError, message):
                    self.save(self._target_payload(view, values, {"password_file": bad}), env)
                self.assertEqual(self.config.read_bytes(), before)

    def test_locked_fields_show_the_effective_environment_value(self) -> None:
        env = {"BACKUP_FULL_SCHEDULE": "0 4 * * *", "BACKUP_CONFIG_LOCAL_KEEP": "9"}
        view = self.view(env)
        self.assertEqual(view["classes"]["full"]["values"]["schedule"], "0 4 * * *")
        self.assertEqual(view["classes"]["full"]["locked"]["schedule"], "BACKUP_FULL_SCHEDULE")
        self.assertEqual(view["classes"]["config"]["values"]["local_keep"], 9)
        # Posting the displayed value back for a locked field is not an edit.
        self.save({"revision": view["revision"], "classes": {"full": {"schedule": "0 4 * * *"}}}, env)

    def test_inline_credentials_and_file_paths_in_values_are_refused(self) -> None:
        for key in ("password", "secret_access_key", "private_key_file"):
            with self.subTest(key=key):
                view = self.view()
                values = dict(view["targets"][0]["values"], **{key: "synthetic"})
                before = self.config.read_bytes()
                with self.assertRaisesRegex(self.editor.PolicyEditError, "cannot be set here"):
                    self.save({"revision": view["revision"], "targets": [{"values": values, "secrets": {}}]})
                self.assertEqual(self.config.read_bytes(), before)

    def test_invalid_policy_is_refused_with_plain_problems_and_file_untouched(self) -> None:
        view = self.view()
        before = self.config.read_bytes()
        with self.assertRaises(self.editor.PolicyEditError) as raised:
            self.save({"revision": view["revision"], "classes": {"full": {"schedule": "not cron"}}})
        self.assertTrue(any("schedule" in problem for problem in raised.exception.problems))
        with self.assertRaises(self.editor.PolicyEditError):
            self.save({"revision": view["revision"], "classes": {"config": {"debounce_seconds": 900, "max_delay_seconds": 60}}})
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.journal, [])

    def test_stale_revision_is_a_conflict(self) -> None:
        view = self.view()
        self.config.write_text(self.config.read_text() + "\n# edited by hand\n")
        with self.assertRaises(self.editor.PolicyEditError) as raised:
            self.save({"revision": view["revision"], "classes": {}})
        self.assertTrue(raised.exception.conflict)

    def test_environment_values_are_locked(self) -> None:
        env = {"BACKUP_FULL_SCHEDULE": "0 4 * * *", "BACKUP_TARGETS_JSON": "[]"}
        view = self.view(env)
        self.assertEqual(view["classes"]["full"]["locked"], {"schedule": "BACKUP_FULL_SCHEDULE"})
        self.assertEqual(view["targets_locked_by"], "BACKUP_TARGETS_JSON")
        with self.assertRaisesRegex(self.editor.PolicyEditError, "BACKUP_FULL_SCHEDULE"):
            self.save({"revision": view["revision"], "classes": {"full": {"schedule": "0 5 * * *"}}}, env)
        with self.assertRaisesRegex(self.editor.PolicyEditError, "BACKUP_TARGETS_JSON"):
            self.save({"revision": view["revision"], "targets": []}, env)
        self.save({"revision": view["revision"], "classes": {"full": {"local_keep": 4}}}, env)

    def test_admin_routes_read_and_save_the_policy(self) -> None:
        from admin_service import main as admin_main

        def call(method: str, path: str, body: Any = None):
            with patch.object(admin_main, "_request_origin_allowed", return_value=True), \
                    patch.object(admin_main, "_basic_auth_matches", return_value=True), \
                    patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config)}):
                return asgi_call(admin_main.app, method, path, body, {"origin": "http://test"})

        status, body = call("GET", "/api/admin/backups/policy")
        self.assertEqual(status, 200, body)
        view = json.loads(body)
        self.assertNotIn(b"archive_sftp_key", body)
        status, body = call("PUT", "/api/admin/backups/policy", {"revision": view["revision"], "classes": {"config": {"local_keep": 12}}})
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["restart_required"])
        status, body = call("PUT", "/api/admin/backups/policy", {"revision": view["revision"], "classes": {}})
        self.assertEqual(status, 409, body)
        status, _ = asgi_call(admin_main.app, "PUT", "/api/admin/backups/policy", {"revision": "x"},
                              {"origin": "http://evil.example.test"})
        self.assertEqual(status, 403)


class HealthTests(unittest.TestCase):
    def test_absent_or_unsafe_status_is_not_a_problem(self) -> None:
        from app.services.backup_health import backup_archive_problems

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "status.json"
            self.assertEqual(backup_archive_problems(path), [])
            path.write_text(json.dumps({"schema_version": 1, "classes": {"full": {"ok": False}}}))
            os.chmod(path, 0o666)
            self.assertEqual(backup_archive_problems(path), [])
            os.chmod(path, 0o640)
            self.assertEqual(backup_archive_problems(path), ["Full backup failed: no details recorded"])

    def test_backup_problem_degrades_but_never_downs(self) -> None:
        from app.main import build_health_payload, health_status_code

        payload = build_health_payload(None, remote_problems=["Backup target NAS degraded: refused"])
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(health_status_code(payload), 200)


class ComposeContractTests(unittest.TestCase):
    def services(self, name: str) -> dict[str, Any]:
        return yaml.safe_load((REPO_ROOT / name).read_text())["services"]

    def test_only_the_backup_scheduler_gains_sys_admin(self) -> None:
        overlay = self.services("docker-compose.backup-nfs.yml")
        self.assertEqual(set(overlay), {"enclosure-backup-scheduler"})
        self.assertEqual(overlay["enclosure-backup-scheduler"]["cap_add"], ["SYS_ADMIN"])
        for compose in sorted(REPO_ROOT.glob("docker-compose*.yml")):
            for service_name, service in self.services(compose.name).items():
                caps = service.get("cap_add") or []
                if "SYS_ADMIN" in caps:
                    with self.subTest(compose=compose.name, service=service_name):
                        self.assertEqual((compose.name, service_name),
                                         ("docker-compose.backup-nfs.yml", "enclosure-backup-scheduler"))

    def test_scheduler_service_is_opt_in_and_hardened(self) -> None:
        for name in ("docker-compose.yml", "docker-compose.dev.yml"):
            scheduler = self.services(name)["enclosure-backup-scheduler"]
            with self.subTest(compose=name):
                self.assertEqual(scheduler["profiles"], ["backup-scheduler"])
                self.assertEqual(scheduler["cap_drop"], ["ALL"])
                self.assertNotIn("cap_add", scheduler)
                self.assertIn("./config:/app/config:ro", scheduler["volumes"])
                self.assertIn("./backup-api:/app/backup-api", scheduler["volumes"])
                admin = self.services(name)["enclosure-admin"]
                self.assertIn("./backup-api:/app/backup-api:ro", admin["volumes"])
                ui = self.services(name)["enclosure-ui"]
                self.assertNotIn("./backup-api:/app/backup-api:ro", ui["volumes"])
                self.assertIn("./backup-journal:/app/backup-journal", ui["volumes"])

    def test_dependencies_are_pinned(self) -> None:
        requirements = (REPO_ROOT / "requirements.txt").read_text().splitlines()
        for package in ("smbprotocol", "boto3"):
            self.assertEqual(sum(line.startswith(f"{package}==") for line in requirements), 1, package)
        self.assertIn("nfs-common", (REPO_ROOT / "Dockerfile").read_text())


if __name__ == "__main__":
    unittest.main()
