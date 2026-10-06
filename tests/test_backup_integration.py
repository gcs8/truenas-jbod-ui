"""Backup integration (#398/#573): policy, cron, journal hooks, scheduler, API, health, Compose.

No network: remote targets are filesystem targets from the real transport
module. Scheduler unit tests use a fake runner; captured-generation regressions
use the real encrypted archive builder over synthetic config files.
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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from app.config_errors import ConfigurationError
from history_service.backup_archive.cron import CronError, CronSchedule
from history_service.backup_archive.journal import ChangeJournal
from history_service.backup_archive.policy import load_backup_policy
from history_service.backup_archive.transport import LocalDirectoryTarget
from admin_service import route_support as admin_route_support
from admin_service import routes as admin_routes

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

    def test_star_step_day_field_is_not_restricted(self) -> None:
        # Classic cron: a day field that starts with "*" does not count as restricted,
        # so "*/2" day-of-month with a weekday means both must match.
        odd_mondays = CronSchedule.parse("0 3 */2 * 1")
        self.assertEqual(odd_mondays.next_after(datetime(2026, 9, 24, tzinfo=UTC)), datetime(2026, 10, 5, 3, 0, tzinfo=UTC))
        self.assertEqual(odd_mondays.next_after(datetime(2026, 10, 5, 3, 0, tzinfo=UTC)), datetime(2026, 10, 19, 3, 0, tzinfo=UTC))
        either = CronSchedule.parse("0 3 1,15 * 1")  # 1st, 15th, or any Monday
        start = datetime(2026, 9, 24, tzinfo=UTC)
        runs = []
        for _ in range(5):
            start = either.next_after(start)
            runs.append(start.day)
        self.assertEqual(runs, [28, 1, 5, 12, 15])

    def test_sparse_leap_sunday_beyond_five_years(self) -> None:
        from zoneinfo import ZoneInfo

        schedule = CronSchedule.parse("0 0 29 2 */7")
        for zone in (UTC, ZoneInfo("America/New_York")):
            with self.subTest(zone=zone):
                first = schedule.next_after(datetime(2026, 9, 28, tzinfo=zone))
                self.assertEqual(first, datetime(2032, 2, 29, tzinfo=zone))
                self.assertIs(first.tzinfo, zone)
                self.assertEqual(schedule.next_after(first), datetime(2060, 2, 29, tzinfo=zone))
                self.assertEqual(
                    schedule.next_after(datetime(2096, 3, 1, tzinfo=zone)),
                    datetime(2128, 2, 29, tzinfo=zone),
                )

    def test_impossible_intersection_has_bounded_calendar_search(self) -> None:
        schedule = CronSchedule.parse("0 0 31 2 */7")
        original = CronSchedule._day_matches
        with patch.object(CronSchedule, "_day_matches", autospec=True, side_effect=original) as matches:
            with self.assertRaises(CronError):
                schedule.next_after(datetime(2026, 1, 1, tzinfo=UTC))
        self.assertLessEqual(matches.call_count, 146098)

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
        self.assertEqual(policy.full.local_keep, 14)
        self.assertEqual(policy.targets, ())

    def test_operator_guide_keeps_the_same_fourteen_copy_full_default(self) -> None:
        guide = (REPO_ROOT / "wiki" / "Backup-Restore-and-Debug-Bundles.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '  full:\n    enabled: true\n    schedule: "0 3 * * *"\n'
            "    archive_format: tar.zst  # default; 7z for older app versions, see "
            '"Full backup archive format"\n    local_keep: 14',
            guide,
        )

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

    def test_full_archive_format_defaults_to_tar_zst_and_accepts_7z(self) -> None:
        # #397 (owner decision): tar.zst is the FULL default; 7z stays selectable.
        self.write({"full": {"enabled": True}})
        self.assertEqual(load_backup_policy(self.config, {}).full.archive_format, "tar.zst")
        policy = load_backup_policy(self.config, {"BACKUP_FULL_ARCHIVE_FORMAT": "7z"})
        self.assertEqual(policy.full.archive_format, "7z")
        self.write({"full": {"enabled": True, "archive_format": "7z"}})
        self.assertEqual(load_backup_policy(self.config, {}).full.archive_format, "7z")
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

    def test_schedule_that_never_matches_is_refused(self) -> None:
        # A parseable schedule with no real date used to pass validation and
        # then stop the scheduler on start (restart loop).
        for text in ("0 0 30 2 *", "0 0 31 4 *"):
            with self.subTest(text=text):
                self.write({"full": {"schedule": text}})
                with self.assertRaises(ConfigurationError) as caught:
                    load_backup_policy(self.config, {})
                problems = caught.exception.problems
                self.assertTrue(
                    any("backups.full.schedule" in p and "never matches a real date" in p for p in problems),
                    problems,
                )
        self.write({"full": {"schedule": "0 0 29 2 *"}})
        self.assertEqual(load_backup_policy(self.config, {}).full.schedule, "0 0 29 2 *")

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

    def test_filesystem_targets_reject_local_archive_overlap_and_aliases(self) -> None:
        local = Path(self.temp.name) / "archive"
        local.mkdir()
        alias = Path(self.temp.name) / "archive-alias"
        alias.symlink_to(local, target_is_directory=True)
        unrelated = Path(self.temp.name) / "remote"
        self.write({
            "targets": [{"target_id": "nas", "provider": "filesystem", "root": str(unrelated)}],
        })
        self.assertEqual(
            load_backup_policy(self.config, {"BACKUP_ARCHIVE_DIR": str(local)}).targets[0].settings.root,
            str(unrelated),
        )

        for target_root in (local, local.parent, local / "future", alias, alias / "future"):
            with self.subTest(target_root=target_root):
                self.write({
                    "targets": [{"target_id": "nas", "provider": "filesystem", "root": str(target_root)}],
                })
                with self.assertRaisesRegex(ConfigurationError, "must not overlap the local archive root"):
                    load_backup_policy(self.config, {"BACKUP_ARCHIVE_DIR": str(local)})
        self.assertFalse((local / "future").exists(), "validation must not create a missing target")


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
    absent_groups: list[str] = []

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
        return {
            "included_groups": list(self.groups),
            "last_absent_groups": list(FakeRunner.absent_groups),
            "last_artifact_name": name,
            "last_size_bytes": len(content),
            "last_sha256": hashlib.sha256(content).hexdigest(),
        }


class SchedulerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        from history_service.backup_scheduler.service import BackupScheduler, SchedulerPaths

        FakeRunner.calls = []
        FakeRunner.fail = False
        FakeRunner.absent_groups = []
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
            history_backup_dir=self.root / "history-backups",
            history_long_term_backup_dir=self.root / "history-backups" / "long-term",
            history_database_stem="history",
        )

    def open_target(self, settings):
        @contextlib.contextmanager
        def opened():
            if settings.target_id in self.broken_targets:
                raise ConnectionRefusedError("connection refused by nas.example.test")
            yield LocalDirectoryTarget(self.remote_root / settings.target_id)

        return opened()

    def make(self, backups: dict[str, Any], *, local_tz: Any = UTC):
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
            local_tz=local_tz,
        )
        self.addCleanup(scheduler.close)
        return scheduler


TARGET = {"target_id": "nas", "label": "Office NAS", "provider": "filesystem", "root": "/unused"}


class SchedulerPreservationTests(SchedulerTestBase):
    """Synthetic files through the public scheduler and real publication transport."""

    def real_scheduler(self, targets=()):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from history_service.backup_archive.policy import policy_from_section
        from history_service.system_backup import FileBackupArtifact

        self._paths.passphrase_file.write_text("synthetic-test-passphrase\n")
        self._paths.passphrase_file.chmod(0o600)
        producer = Mock()

        def export(**kwargs):
            workspace = Path(tempfile.mkdtemp(dir=self.root))
            source = workspace / "synthetic.tar.zst.enc"
            source.write_bytes(b"synthetic archive payload")
            return FileBackupArtifact(source.name, source, "application/octet-stream",
                                      {"schema_version": 1}, workspace)

        producer.export_scheduled_bundle_to_file.side_effect = export
        producer.preflight_scheduled_bundle_file.return_value = {"absent_groups": []}
        policy = policy_from_section({"full": {"enabled": True, "local_keep": 1, "remote_keep": 1},
                                      "targets": list(targets)}, source="synthetic", environ={})
        scheduler = self._BackupScheduler(
            policy, producer, self._paths, app_gid=os.getegid(),
            config_groups=["config_file"], full_groups=["config_file", "history_db"],
            snapshot_config=lambda: {}, clock=lambda: self.now,
        )
        self.addCleanup(scheduler.close)
        return SimpleNamespace(service=scheduler, producer=producer)

    def remote_pair(self):
        first, second = self.root / "first", self.root / "second"
        first.mkdir()
        second.mkdir()
        alias = self.root / "second-alias"
        alias.symlink_to(second, target_is_directory=True)
        targets = [{**TARGET, "target_id": "first", "root": str(first)},
                   {**TARGET, "target_id": "second", "root": str(alias)}]
        return first, second, alias, targets

    def test_remote_same_root_policy_refuses_enabled_plain_and_symlink_aliases(self):
        from history_service.backup_archive.policy import policy_from_section

        first, _, alias, targets = self.remote_pair()
        alias.unlink()
        alias.symlink_to(first, target_is_directory=True)
        for root in (first, alias):
            with self.subTest(root=root):
                duplicate = {**targets[1], "root": str(root)}
                with self.assertRaisesRegex(ConfigurationError, "same physical root"):
                    policy_from_section({"targets": [targets[0], duplicate]}, source="synthetic", environ={})

    def test_remote_alias_rechecked_before_shipping_and_both_preserved_copies_survive(self):
        first, second, alias, targets = self.remote_pair()
        scheduler = self.real_scheduler(targets).service
        old = scheduler.run_now("full")
        for record in scheduler.catalog.list():
            if record.location != "local":
                scheduler.preserve(record.artifact_id, reason="retain old bytes", actor="test")
        old_bytes = (first / old.name).read_bytes()
        alias.unlink()
        alias.symlink_to(first, target_is_directory=True)
        # Filesystem admission must not impose a cross-protocol alias policy.
        # The independent provider is a synthetic local target, never a network.
        from dataclasses import replace
        from history_service.backup_archive.policy import ArchiveTarget
        from history_service.backup_archive.settings import ArchiveTargetSettings

        independent = ArchiveTarget(ArchiveTargetSettings(
            target_id="independent", provider="s3", root="synthetic", bucket="synthetic",
            access_key_id_file=str(self.root / "unused-access-key"),
            secret_access_key_file=str(self.root / "unused-secret-key"),
        ), label="Synthetic independent provider")
        scheduler.policy = replace(scheduler.policy, targets=(*scheduler.policy.targets, independent))
        original_open = scheduler._open_target

        @contextlib.contextmanager
        def opened(settings, **kwargs):
            if settings.target_id == "independent":
                yield LocalDirectoryTarget(self.root / "independent", provider="s3")
            else:
                with original_open(settings, **kwargs) as remote:
                    yield remote

        scheduler._open_target = opened
        self.now += timedelta(hours=1)
        new = scheduler.run_now("full")
        self.assertEqual([r.name for r in scheduler.catalog.list(location="first")], [old.name])
        self.assertEqual([r.name for r in scheduler.catalog.list(location="second")], [old.name])
        self.assertFalse((first / new.name).exists())
        self.assertEqual((first / old.name).read_bytes(), old_bytes)
        self.assertEqual((second / old.name).read_bytes(), old_bytes)
        targets_by_id = {t["id"]: t for t in scheduler.library()["targets"]}
        self.assertFalse(targets_by_id["first"]["last_run"]["ok"])
        self.assertFalse(targets_by_id["second"]["last_run"]["ok"])
        self.assertTrue(targets_by_id["independent"]["last_run"]["ok"])

    def test_unavailable_filesystem_target_does_not_block_healthy_peer(self):
        from history_service.backup_archive import policy as policy_module
        from history_service.backup_archive.settings import ArchiveRootUnavailableError

        first, second, alias, targets = self.remote_pair()
        scheduler = self.real_scheduler(targets).service
        original = policy_module.filesystem_roots_overlap

        def inspected(local_root, remote_root):
            if Path(remote_root) == alias:
                raise ArchiveRootUnavailableError("synthetic target unavailable")
            return original(local_root, remote_root)

        with patch.object(policy_module, "filesystem_roots_overlap", side_effect=inspected):
            record = scheduler.run_now("full")

        self.assertTrue((first / record.name).is_file())
        self.assertFalse((second / record.name).exists())
        self.assertEqual(len(scheduler.catalog.list(location="first")), 1)
        self.assertEqual(scheduler.catalog.list(location="second"), [])
        targets_by_id = {item["id"]: item for item in scheduler.library()["targets"]}
        self.assertTrue(targets_by_id["first"]["last_run"]["ok"])
        self.assertFalse(targets_by_id["second"]["last_run"]["ok"])

    def test_remote_alias_rechecked_at_each_delete_in_cached_target(self):
        from dataclasses import replace

        first, second, alias, targets = self.remote_pair()
        scheduler = self.real_scheduler(targets).service
        scheduler.policy = replace(scheduler.policy, full=scheduler.policy.full.model_copy(update={"remote_keep": 3}))
        records = []
        for _ in range(3):
            records.append(scheduler.run_now("full"))
            self.now += timedelta(hours=1)
        for record in scheduler.catalog.list(location="second"):
            scheduler.preserve(record.artifact_id, reason="retain physical copy", actor="test")
        scheduler.policy = replace(scheduler.policy, full=scheduler.policy.full.model_copy(update={"remote_keep": 1}))
        token, _, _ = scheduler.plan()
        original = scheduler.catalog.record_deletion

        def tombstone(*args, **kwargs):
            result = original(*args, **kwargs)
            # A second target becomes an alias after the first deletion, while
            # LifecycleManager and the scheduler both cache the opened target.
            alias.unlink()
            alias.symlink_to(first, target_is_directory=True)
            return result

        with patch.object(scheduler.catalog, "record_deletion", side_effect=tombstone):
            result = scheduler.apply(token)
        self.assertFalse(result.complete)
        self.assertIn("same physical root", result.error)
        self.assertTrue((first / records[1].name).exists())
        self.assertTrue((second / records[1].name).exists())
        self.assertEqual(len(result.deleted), 1)

    def test_first_class_ancestor_eio_preserves_uncertain_copy_without_success_credit(self):
        import errno

        remote = self.root / "first-use-remote"
        remote.mkdir()
        scheduler = self.real_scheduler([{**TARGET, "root": str(remote)}]).service
        original = os.fsync
        ancestor_attempts = []

        def synced(fd):
            info, parent = os.fstat(fd), remote.stat()
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
                ancestor_attempts.append((info.st_dev, info.st_ino))
                raise OSError(errno.EIO, "synthetic ancestor barrier failure")
            return original(fd)

        locals_kept = []
        # The real shipping path opens a fresh target each job. Keep EIO active
        # throughout both runs; directory existence cannot grant durability.
        with patch.object(os, "fsync", side_effect=synced):
            for run in range(2):
                locals_kept.append(scheduler.run_now("full"))
                remote_records = scheduler.catalog.list(location="nas")
                self.assertFalse(scheduler.library()["targets"][0]["last_run"]["ok"])
                self.assertEqual(len(ancestor_attempts), run + 1)
                self.assertEqual(len(remote_records), run + 1)
                self.assertFalse(any(record.verified for record in remote_records))
                for uncertain in remote_records:
                    self.assertTrue(uncertain.preserved)
                    self.assertIn("directory durability", uncertain.preserve_reason)
                    self.assertEqual((remote / uncertain.name).read_bytes(), b"synthetic archive payload")
                self.assertTrue((self._paths.local_dir / locals_kept[-1].name).exists())
                self.now += timedelta(hours=1)
        # Only a later healthy barrier can earn new remote retention credit.
        scheduler.run_now("full")
        self.assertTrue(scheduler.library()["targets"][0]["last_run"]["ok"])
        remote_records = scheduler.catalog.list(location="nas")
        self.assertEqual(sum(record.verified for record in remote_records), 1)
        self.assertEqual(sum(record.preserved for record in remote_records), 2)
        for local in locals_kept:
            self.assertTrue((remote / local.name).exists())

    def test_oversized_upload_readback_releases_job_and_refuses_remote_credit(self):
        self._check_upload_readback_job("sftp")

    def test_oversized_smb_upload_readback_releases_job_and_refuses_remote_credit(self):
        self._check_upload_readback_job("smb")

    def _check_upload_readback_job(self, provider):
        from history_service.backup_archive import transport
        from tests.test_backup_archive_transport import FakeSFTP, FakeSmbClient, _UploadReadback

        scheduler = self.real_scheduler([{**TARGET, "root": str(self.root / provider)}]).service
        body = _UploadReadback(self, len(b"synthetic archive payload"), "excess")
        if provider == "sftp":
            peer = FakeSFTP()
            target = transport.SftpTarget(peer, "/synthetic")
            original = peer.open

            def opened_sftp(path, mode):
                return body if mode == "rb" else original(path, mode)

            patcher = patch.object(peer, "open", side_effect=opened_sftp)
        else:
            peer = FakeSmbClient()
            target = transport.SmbTarget(peer, "\\\\peer.example.test\\backups", encrypted=True)
            original = peer.open_file

            @contextlib.contextmanager
            def opened_smb(path, mode="rb"):
                if mode == "rb":
                    with body:
                        yield body
                else:
                    with original(path, mode=mode) as stream:
                        yield stream

            patcher = patch.object(peer, "open_file", side_effect=opened_smb)
        with patch.object(scheduler, "_open_configured_target", return_value=contextlib.nullcontext(target)):
            with patcher:
                local = scheduler.run_now("full")
            self.assertEqual(scheduler.catalog.list(location="nas"), [])
            self.assertFalse(scheduler.library()["targets"][0]["last_run"]["ok"])
            self.assertEqual(peer.files, {})
            self.assertTrue(body.closed)
            self.assertLessEqual(body.consumed, local.size + transport.CHUNK_SIZE)
            self.now += timedelta(hours=1)
            scheduler.run_now("full")
            self.assertTrue(scheduler.library()["targets"][0]["last_run"]["ok"])

    def test_directory_eio_does_not_replace_remote_retention_copy(self):
        import errno

        remote = self.root / "remote-real"
        target = {**TARGET, "root": str(remote)}
        scheduler = self.real_scheduler([target]).service
        old = scheduler.run_now("full")
        self.now += timedelta(hours=1)
        original = os.fsync

        def synced(fd):
            metadata = os.fstat(fd)
            parent = remote
            if stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == (parent.stat().st_dev, parent.stat().st_ino):
                raise OSError(errno.EIO, "synthetic ancestor EIO")
            return original(fd)

        # Even a previously verified hierarchy is re-admitted for each job.
        with patch.object(os, "fsync", side_effect=synced):
            for run in range(2):
                local = scheduler.run_now("full")
                remote_records = scheduler.catalog.list(location="nas")
                self.assertEqual(len(remote_records), run + 2)
                self.assertEqual(sum(record.verified for record in remote_records), 1)
                for uncertain in (record for record in remote_records if record.name != old.name):
                    self.assertTrue(uncertain.preserved)
                    self.assertFalse(uncertain.verified)
                    self.assertIn("directory durability", uncertain.preserve_reason)
                    self.assertTrue((remote / uncertain.name).exists())
                self.assertTrue((remote / old.name).exists())
                self.assertTrue((self._paths.local_dir / local.name).exists())
                self.assertFalse(scheduler.library()["targets"][0]["last_run"]["ok"])
                self.now += timedelta(hours=1)
        scheduler.run_now("full")
        self.assertTrue(scheduler.library()["targets"][0]["last_run"]["ok"])
        self.assertFalse((remote / old.name).exists())

    def test_remote_alias_introduced_during_copy_refuses_final_publication(self):
        from history_service.backup_archive import transport

        first, _, alias, targets = self.remote_pair()
        scheduler = self.real_scheduler(targets).service
        original = transport._check_readback
        changed = False

        def readback(*args):
            nonlocal changed
            original(*args)
            if not changed:
                alias.unlink()
                alias.symlink_to(first, target_is_directory=True)
                changed = True

        with patch.object(transport, "_check_readback", side_effect=readback):
            record = scheduler.run_now("full")
        self.assertTrue(changed)
        self.assertFalse((first / record.name).exists())
        self.assertEqual(scheduler.catalog.list(location="first"), [])
        self.assertEqual(scheduler.catalog.list(location="second"), [])

    def test_real_catalog_insert_failure_is_recovered_without_enumerating_files(self):
        import sqlite3
        from history_service.backup_archive.catalog import CatalogError

        scheduler = self.real_scheduler().service
        database = self._paths.state_dir / "catalog.sqlite3"
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON artifacts "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic insertion failure'); END")
        with self.assertRaises(CatalogError):
            scheduler.run_now("full")
        self.assertEqual(scheduler.catalog.list(), [])
        scheduler.close()
        still_failed = self.real_scheduler().service
        self.assertEqual(still_failed.library()["storage"]["local"]["count"], 1)
        self.assertTrue(still_failed.library()["detail"])
        still_failed.close()
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TRIGGER synthetic_failure")
        with patch.object(Path, "glob", side_effect=AssertionError("no discovery")), \
                patch.object(Path, "iterdir", side_effect=AssertionError("no discovery")):
            recovered = self.real_scheduler().service
        self.assertEqual(len(recovered.catalog.list()), 1)
        self.assertTrue(recovered.catalog.list()[0].preserved)

    def test_sqlite_operational_error_during_recovery_keeps_library_accounting(self):
        import sqlite3
        from history_service.backup_archive.catalog import ArtifactCatalog

        scheduler = self.real_scheduler().service
        with patch.object(scheduler.catalog, "add", side_effect=sqlite3.OperationalError("synthetic disk I/O")):
            with self.assertRaises(sqlite3.OperationalError):
                scheduler.run_now("full")
        scheduler.close()
        with patch.object(ArtifactCatalog, "add", side_effect=sqlite3.OperationalError("synthetic disk I/O")):
            reopened = self.real_scheduler().service
        self.assertEqual(reopened.library()["storage"]["local"]["count"], 1)
        self.assertTrue(reopened.library()["detail"])

    @contextlib.contextmanager
    def metadata_save_fault(self, scheduler, seam):
        """Fail real metadata I/O, not the ownership or publication validators."""
        import errno

        hits = []
        meta_path = scheduler._meta_path
        real_open, real_fdopen = os.open, os.fdopen
        real_dump, real_fsync = json.dump, os.fsync
        real_replace, real_unlink, real_lstat = os.replace, Path.unlink, Path.lstat
        real_save = scheduler._save_meta
        temporary_fds = set()

        def fail():
            hits.append(seam)
            raise OSError(errno.EIO, "synthetic metadata failure")

        def is_temporary(path):
            return (isinstance(path, (str, bytes, os.PathLike))
                    and Path(os.fsdecode(path)).parent == meta_path.parent
                    and Path(os.fsdecode(path)).name.startswith(".artifact-meta.json."))

        def opened(path, flags, *args, **kwargs):
            if not hits:
                if seam == "temp_open" and is_temporary(path):
                    fail()
                if seam == "directory_open" and Path(path) == meta_path.parent and flags & os.O_DIRECTORY:
                    fail()
            descriptor = real_open(path, flags, *args, **kwargs)
            temporary_fds.discard(descriptor)
            if is_temporary(path):
                temporary_fds.add(descriptor)
            return descriptor

        class Stream:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def write(self, data):
                if seam == "write" and not hits:
                    fail()
                return self.handle.write(data)

            def flush(self):
                if seam == "flush" and not hits:
                    fail()
                return self.handle.flush()

        def fdopened(fd, *args, **kwargs):
            handle = real_fdopen(fd, *args, **kwargs)
            return Stream(handle) if fd in temporary_fds else handle

        def dumped(obj, handle, *args, **kwargs):
            if seam == "json_partial" and isinstance(handle, Stream) and not hits:
                handle.write("{")
                fail()
            return real_dump(obj, handle, *args, **kwargs)

        def synced(fd):
            if not hits:
                if seam == "file_fsync" and fd in temporary_fds:
                    fail()
                metadata = os.fstat(fd)
                parent = meta_path.parent.stat()
                if (seam == "directory_fsync"
                        and (metadata.st_dev, metadata.st_ino) == (parent.st_dev, parent.st_ino)):
                    fail()
            return real_fsync(fd)

        def replaced(source, destination, *args, **kwargs):
            targeted = Path(destination) == meta_path and not hits
            if targeted and seam == "replace":
                fail()
            result = real_replace(source, destination, *args, **kwargs)
            if targeted and seam in {"replace_ack_lost", "replace_ack_uninspectable"}:
                fail()
            return result

        def inspected(path, *args, **kwargs):
            if seam == "replace_ack_uninspectable" and hits and path == meta_path:
                raise OSError(errno.EIO, "synthetic metadata inspection failure")
            return real_lstat(path, *args, **kwargs)

        def unlinked(path, *args, **kwargs):
            if seam == "cleanup" and is_temporary(path) and not hits:
                fail()
            return real_unlink(path, *args, **kwargs)

        def saved(*args, **kwargs):
            if seam == "before_save" and not hits:
                fail()
            result = real_save(*args, **kwargs)
            if seam == "after_save" and not hits:
                fail()
            return result

        with contextlib.ExitStack() as stack:
            for obj, name, side_effect in (
                (os, "open", opened), (os, "fdopen", fdopened), (json, "dump", dumped),
                (os, "fsync", synced), (os, "replace", replaced), (Path, "unlink", unlinked),
                (scheduler, "_save_meta", saved), (Path, "lstat", inspected),
            ):
                stack.enter_context(patch.object(obj, name, side_effect=side_effect, autospec=True))
            yield hits
        self.assertEqual(hits, [seam], "the named real I/O seam must fail exactly once")

    def check_failed_intent_save(self, seams, *, immediate_retained, recovered_retained):
        from dataclasses import replace

        original_paths = self._paths
        for seam in seams:
            for later_verify in (False, True):
                with self.subTest(seam=seam, later_verify=later_verify), \
                        tempfile.TemporaryDirectory(dir=self.root) as directory:
                    case = Path(directory)
                    self._paths = replace(original_paths, local_dir=case / "local", state_dir=case / "state")
                    scheduler = self.real_scheduler().service
                    old = scheduler.run_now("full")
                    scheduler.preserve(old.artifact_id, reason="synthetic preserved copy", actor="test")
                    old_path = scheduler.paths.local_dir / old.name
                    old_bytes = old_path.read_bytes()
                    before = scheduler._meta_path.read_bytes()
                    self.now += timedelta(hours=1)
                    with self.metadata_save_fault(scheduler, seam):
                        with self.assertRaisesRegex(OSError, "synthetic metadata failure"):
                            scheduler.run_now("full")
                    immediate = scheduler.library()
                    self.assertFalse(immediate["classes"]["full"]["last_run"]["ok"])
                    self.assertEqual(len(scheduler.catalog.list()), 1)
                    self.assertEqual(
                        immediate["storage"]["local"]["count"],
                        1 + int(immediate_retained),
                    )
                    if not immediate_retained:
                        self.assertEqual(scheduler._meta_path.read_bytes(), before)
                    if later_verify:
                        self.assertTrue(scheduler.verify(old.artifact_id)["ok"])
                    scheduler.close()
                    reopened = self.real_scheduler().service
                    library = reopened.library()
                    self.assertEqual(
                        library["storage"]["local"]["count"],
                        1 + int(recovered_retained),
                    )
                    self.assertEqual(
                        library["storage"]["local"]["full_bytes"],
                        len(old_bytes) * (1 + int(recovered_retained)),
                    )
                    self.assertEqual(bool(library["detail"]), recovered_retained)
                    extra = [item for item in library["artifacts"] if item["id"] != old.artifact_id]
                    for item in extra:
                        self.assertEqual(item["state"], "missing")
                        self.assertTrue(item["preserved"])
                        self.assertFalse(item["restorable"])
                        self.assertFalse(item["verified"])
                    self.assertEqual(old_path.read_bytes(), old_bytes)
                    self.assertEqual(reopened._verified_local_full_count(), 1)
                    self.assertEqual(reopened.plan()[2].items, ())
                    reopened.close()
        self._paths = original_paths

    def test_pre_replace_metadata_failures_do_not_leave_phantom_intents(self):
        self.check_failed_intent_save(
            ("before_save", "temp_open", "write", "json_partial", "flush", "file_fsync", "replace"),
            immediate_retained=False,
            recovered_retained=False,
        )

    def test_post_replace_metadata_failures_clear_intents_after_absence_is_proven(self):
        self.check_failed_intent_save(
            ("directory_open", "directory_fsync", "cleanup", "replace_ack_lost",
             "replace_ack_uninspectable", "after_save"),
            immediate_retained=True,
            recovered_retained=False,
        )

    def test_first_intent_save_failure_does_not_leak_into_successful_retry(self):
        from dataclasses import replace

        original_paths = self._paths
        for seam in ("before_save", "temp_open", "write", "json_partial", "flush", "file_fsync", "replace"):
            with self.subTest(seam=seam), tempfile.TemporaryDirectory(dir=self.root) as directory:
                case = Path(directory)
                self._paths = replace(original_paths, local_dir=case / "local", state_dir=case / "state")
                scheduler = self.real_scheduler().service
                with self.metadata_save_fault(scheduler, seam):
                    with self.assertRaisesRegex(OSError, "synthetic metadata failure"):
                        scheduler.run_now("full")
                self.assertEqual(scheduler.library()["artifacts"], [])
                self.assertFalse(scheduler._meta_path.exists())
                self.now += timedelta(hours=1)
                record = scheduler.run_now("full")
                scheduler.close()
                reopened = self.real_scheduler().service
                self.assertEqual([item["id"] for item in reopened.library()["artifacts"]], [record.artifact_id])
                self.assertFalse(reopened.library()["detail"])
                reopened.close()
        self._paths = original_paths

    def test_metadata_directory_eio_prevents_final_publication(self):
        import errno

        scheduler = self.real_scheduler().service
        original = os.fsync
        state = self._paths.state_dir.stat()

        def synced(fd):
            metadata = os.fstat(fd)
            if (metadata.st_dev, metadata.st_ino) == (state.st_dev, state.st_ino):
                raise OSError(errno.EIO, "synthetic ownership barrier failure")
            return original(fd)

        with patch.object(os, "fsync", side_effect=synced):
            with self.assertRaisesRegex(OSError, "ownership barrier"):
                scheduler.run_now("full")
        self.assertEqual(scheduler.catalog.list(), [])
        self.assertEqual([p.name for p in (self._paths.local_dir / "full").iterdir()
                          if p.name != ".scheduled-backup.lock"], [])

    def test_concurrent_verify_cannot_overwrite_durable_publication_intent(self):
        import threading

        scheduler = self.real_scheduler().service
        old = scheduler.run_now("full")
        old_save_ready = threading.Event()
        backup_done = threading.Event()
        errors = []
        original = os.replace

        def replaced(source, destination, *args, **kwargs):
            if Path(destination) == scheduler._meta_path and threading.current_thread().name == "synthetic-verify":
                old_save_ready.set()
                # Without serialization the newer intent is published while this
                # older snapshot waits, then the older replacement erases it.
                # With serialization, releasing this writer lets the intent save
                # proceed next. Both waits and worker joins are bounded.
                backup_done.wait(1)
            return original(source, destination, *args, **kwargs)

        def verify():
            try:
                scheduler.verify(old.artifact_id)
            except BaseException as exc:
                errors.append(exc)

        with patch.object(os, "replace", side_effect=replaced):
            worker = threading.Thread(target=verify, name="synthetic-verify")
            worker.start()
            try:
                self.assertTrue(old_save_ready.wait(5))
                with patch.object(scheduler.catalog, "add", side_effect=OSError("catalog failure")):
                    with self.assertRaisesRegex(OSError, "catalog failure"):
                        scheduler.run_now("full")
            finally:
                backup_done.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        scheduler.close()
        recovered = self.real_scheduler().service
        self.assertEqual(recovered.library()["storage"]["local"]["count"], 2)
        self.assertEqual(sum(record.preserved for record in recovered.catalog.list()), 1)

    def test_library_snapshot_survives_concurrent_failed_publication(self):
        import sqlite3
        import threading
        from history_service.backup_archive.catalog import CatalogError
        from history_service.backup_scheduler import service as module

        scheduler = self.real_scheduler().service
        with contextlib.closing(sqlite3.connect(self._paths.state_dir / "catalog.sqlite3")) as db, db:
            db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON artifacts "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic insertion failure'); END")
        with self.assertRaises(CatalogError):
            scheduler.run_now("full")
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        results, errors, writer_errors = [], [], []
        original = module.validate_record

        def validated(record):
            value = original(record)
            if threading.current_thread().name == "synthetic-library":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("library validation barrier timed out")
            return value

        def reader():
            try:
                results.append(scheduler.library())
            except BaseException as exc:
                errors.append(exc)

        def writer():
            try:
                scheduler.run_now("full")
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                finished.set()

        self.now += timedelta(hours=1)
        with patch.object(module, "validate_record", side_effect=validated):
            reading = threading.Thread(target=reader, name="synthetic-library")
            writing = threading.Thread(target=writer, name="synthetic-publication")
            reading.start()
            try:
                self.assertTrue(entered.wait(5))
                writing.start()
                # Validation uses detached metadata, not a held writer lock.
                self.assertTrue(finished.wait(5))
            finally:
                release.set()
                reading.join(5)
                if writing.ident is not None:
                    writing.join(5)
        self.assertFalse(reading.is_alive())
        self.assertFalse(writing.is_alive())
        self.assertEqual(len(writer_errors), 1)
        self.assertIsInstance(writer_errors[0], CatalogError)
        self.assertEqual(errors, [], "library must remain available during publication")
        self.assertEqual(results[0]["storage"]["local"]["count"], 1)
        current = scheduler.library()
        self.assertEqual(current["storage"]["local"], {
            "count": 2, "config_bytes": 0, "full_bytes": 2 * len(b"synthetic archive payload"),
        })
        self.assertEqual(len(json.loads(scheduler._meta_path.read_text())), 2)
        self.assertEqual(scheduler.plan()[2].items, ())

    def test_detail_uses_one_metadata_snapshot_during_verify(self):
        import threading

        scheduler = self.real_scheduler().service
        record = scheduler.run_now("full")
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        results, errors = [], []
        original = scheduler._local_path

        def local_path(value):
            path = original(value)
            if threading.current_thread().name == "synthetic-detail":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("detail stat barrier timed out")
            return path

        def reader():
            try:
                results.append(scheduler.detail(record.artifact_id))
            except BaseException as exc:
                errors.append(exc)

        def writer():
            try:
                self.assertTrue(scheduler.verify(record.artifact_id)["ok"])
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        with patch.object(scheduler, "_local_path", side_effect=local_path):
            reading = threading.Thread(target=reader, name="synthetic-detail")
            writing = threading.Thread(target=writer, name="synthetic-verify")
            reading.start()
            try:
                self.assertTrue(entered.wait(5))
                writing.start()
                self.assertTrue(finished.wait(5), "file inspection must not hold the metadata lock")
            finally:
                release.set()
                reading.join(5)
                if writing.ident is not None:
                    writing.join(5)
        self.assertFalse(reading.is_alive())
        self.assertFalse(writing.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(results[0]["last_verify"])
        self.assertTrue(scheduler.detail(record.artifact_id)["last_verify"]["ok"])

    def test_detail_nested_metadata_is_detached_from_scheduler(self):
        import copy

        scheduler = self.real_scheduler().service
        record = scheduler.run_now("full")
        self.assertTrue(scheduler.verify(record.artifact_id)["ok"])
        before = copy.deepcopy(scheduler.detail(record.artifact_id))
        changed = scheduler.detail(record.artifact_id)
        changed["inspect"]["groups"].append("synthetic-not-a-group")
        changed["last_verify"]["ok"] = False
        self.assertEqual(scheduler.detail(record.artifact_id), before)

    def test_all_metadata_access_uses_state_lock(self):
        import copy

        target = self.root / "remote-metadata"
        scheduler = self.real_scheduler([{**TARGET, "root": str(target)}]).service
        violations = []

        class AuditedDict(dict):
            # Record accesses rather than raising inside fault-handling paths.
            def check(self, operation):
                if not scheduler._state_lock._is_owned():
                    violations.append(operation)

            def __getitem__(self, key):
                self.check("getitem")
                return super().__getitem__(key)

            def __setitem__(self, key, value):
                self.check("setitem")
                super().__setitem__(key, wrap(value))

            def get(self, key, default=None):
                self.check("get")
                return super().get(key, default)

            def items(self):
                self.check("items")
                return super().items()

            def setdefault(self, key, default=None):
                self.check("setdefault")
                return super().setdefault(key, wrap(default))

            def pop(self, key, *args):
                self.check("pop")
                return super().pop(key, *args)

            def __deepcopy__(self, memo):
                self.check("snapshot")
                return {key: copy.deepcopy(value, memo) for key, value in self.items()}

        def wrap(value):
            if isinstance(value, dict):
                return AuditedDict({key: wrap(child) for key, child in value.items()})
            return value

        scheduler._meta = AuditedDict()
        with patch.object(scheduler.catalog, "add", side_effect=OSError("synthetic pending")):
            with self.assertRaises(OSError):
                scheduler.run_now("full")
        self.assertEqual(scheduler.library()["storage"]["local"]["count"], 1)
        scheduler._recover_publications()
        recovered = scheduler.catalog.list()[0]
        self.assertTrue(scheduler.verify(recovered.artifact_id)["ok"])
        scheduler.unpreserve(recovered.artifact_id, actor="test")
        self.now += timedelta(hours=1)
        record = scheduler.run_now("full")
        self.assertTrue(scheduler.verify(record.artifact_id)["ok"])
        self.assertEqual(scheduler.detail(record.artifact_id)["state"], "ok")
        scheduler.library()
        scheduler._verified_local_full_count()
        self.assertEqual(violations, [], "all live metadata access must share the snapshot/write lock")

    def test_stale_intent_cannot_resurrect_tombstoned_generation(self):
        from history_service.backup_scheduler.service import BackupScheduler

        scheduler = self.real_scheduler().service
        with patch.object(scheduler.catalog, "add", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                scheduler.run_now("full")
        scheduler.close()
        with patch.object(BackupScheduler, "_save_meta", side_effect=OSError("intent clear failed")):
            recovered = self.real_scheduler().service
        old = recovered.catalog.list()[0]
        self.assertTrue(recovered.verify(old.artifact_id)["ok"])
        self.now += timedelta(hours=1)
        recovered.run_now("full")
        recovered.unpreserve(old.artifact_id, actor="test")
        token, _, _ = recovered.plan()
        self.assertTrue(recovered.apply(token).complete)
        self.assertEqual(recovered.library()["storage"]["local"]["count"], 1)
        recovered.close()
        reopened = self.real_scheduler().service
        self.assertEqual(reopened.library()["storage"]["local"]["count"], 1)
        self.assertIsNone(reopened.catalog.get(old.artifact_id))
        self.assertFalse((self._paths.local_dir / old.name).exists())

    def test_ownership_intent_failure_prevents_final_publication(self):
        scheduler = self.real_scheduler().service
        with patch.object(scheduler, "_save_meta", side_effect=OSError("intent unavailable")):
            with self.assertRaisesRegex(OSError, "intent unavailable"):
                scheduler.run_now("full")
        destination = self._paths.local_dir / "full"
        self.assertEqual([p.name for p in destination.iterdir() if p.name != ".scheduled-backup.lock"], [])
        self.assertEqual(scheduler.catalog.list(), [])

    def test_recovery_clears_intent_when_final_publication_is_absent(self):
        scheduler = self.real_scheduler().service
        with patch.object(scheduler.catalog, "add", side_effect=OSError("synthetic catalog failure")):
            with self.assertRaisesRegex(OSError, "catalog failure"):
                scheduler.run_now("full")
        pending = scheduler._pending_publications()
        self.assertEqual(len(pending), 1)
        (scheduler.paths.local_dir / pending[0].name).unlink()
        scheduler.close()

        reopened = self.real_scheduler().service
        self.assertEqual(reopened.catalog.list(), [])
        self.assertEqual(reopened._pending_publications(), [])
        self.assertEqual(reopened.library()["artifacts"], [])

    def test_recovery_removes_recorded_temporary_link_before_cataloguing(self):
        scheduler = self.real_scheduler().service
        with patch.object(scheduler.catalog, "add", side_effect=OSError("synthetic catalog failure")):
            with self.assertRaisesRegex(OSError, "catalog failure"):
                scheduler.run_now("full")
        pending = scheduler._pending_publications()
        self.assertEqual(len(pending), 1)
        record = pending[0]
        intent = scheduler._metadata_snapshot(record.artifact_id)["publication"]
        target = scheduler.paths.local_dir / record.name
        temporary = target.parent / intent["temporary_name"]
        os.link(target, temporary)
        self.assertEqual(target.stat().st_nlink, 2)
        scheduler.close()

        reopened = self.real_scheduler().service
        recovered = reopened.catalog.get(record.artifact_id)
        self.assertIsNotNone(recovered)
        self.assertTrue(recovered.preserved)
        self.assertFalse(temporary.exists())
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(reopened._pending_publications(), [])

    def test_ownership_recovery_never_adopts_substituted_file(self):
        scheduler = self.real_scheduler().service
        names = []

        def failed(record, **kwargs):
            names.append(record.name)
            raise OSError("catalog unavailable")

        with patch.object(scheduler.catalog, "add", side_effect=failed):
            with self.assertRaises(OSError):
                scheduler.run_now("full")
        path = self._paths.local_dir / names[0]
        # Keep the owned inode alive to rule out immediate inode reuse.
        owned = path.with_suffix(".held")
        path.rename(owned)
        path.write_bytes(owned.read_bytes())
        path.chmod(0o600)
        scheduler.close()
        reopened = self.real_scheduler().service
        self.assertEqual(reopened.catalog.list(), [])
        self.assertTrue(reopened.library()["detail"])
        self.assertEqual(reopened.plan()[2].items, ())
        self.assertEqual(path.read_bytes(), b"synthetic archive payload")
        self.assertTrue(owned.exists())

    def test_corrupt_ownership_metadata_is_not_silently_discarded(self):
        scheduler = self.real_scheduler().service
        scheduler.run_now("full")
        scheduler.close()
        (self._paths.state_dir / "artifact-meta.json").write_text("{broken")
        with self.assertRaises(ValueError):
            self.real_scheduler()

    def test_completed_uncatalogued_generations_recover_after_restart_and_retry(self):
        from history_service.backup_archive.catalog import ArtifactCatalog

        scheduler = self.real_scheduler().service
        foreign = self._paths.local_dir / "full" / "jbod-scheduled-backup-20260924T000000Z-deadbeef.tar.zst.enc"
        foreign.parent.mkdir(mode=0o700)
        foreign.write_bytes(b"foreign backup-looking bytes")
        observed = []

        def fail_after_publication(record, **kwargs):
            path = self._paths.local_dir / record.name
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), b"synthetic archive payload")
            observed.append((record.name, path.stat().st_ino))
            raise OSError("synthetic catalog failure")

        with patch.object(scheduler.catalog, "add", side_effect=fail_after_publication):
            for _ in range(2):
                with self.assertRaisesRegex(OSError, "synthetic catalog failure"):
                    scheduler.run_now("full")
                self.now += timedelta(hours=1)
        self.assertEqual(len(observed), 2)
        scheduler.close()
        # Persisting the catalog remains unavailable at first restart. Library
        # reads must still account for both exact, owned completed generations.
        with patch.object(ArtifactCatalog, "add", side_effect=OSError("still unavailable")):
            held = self.real_scheduler().service
            library = held.library()
            self.assertEqual(library["storage"].get("local", {}).get("count", 0), 2)
            self.assertEqual(library["storage"]["local"]["full_bytes"], 2 * len(b"synthetic archive payload"))
            self.assertTrue(library["detail"])
        held.close()
        recovered = self.real_scheduler().service
        records = recovered.catalog.list()
        self.assertEqual({r.name for r in records}, {name for name, _ in observed})
        self.assertTrue(all(r.preserved and not r.verified for r in records))
        self.assertEqual(recovered.library()["storage"]["local"]["count"], 2)
        self.assertEqual(recovered.plan()[2].items, ())
        recovered.run_now("full")
        self.assertEqual(recovered.library()["storage"]["local"]["count"], 3)
        for name, inode in observed:
            self.assertEqual((self._paths.local_dir / name).stat().st_ino, inode)
        self.assertEqual(foreign.read_bytes(), b"foreign backup-looking bytes")
        # Explicit verification/unpreservation uses the existing lifecycle.
        for record in records:
            self.assertTrue(recovered.verify(record.artifact_id)["ok"])
            recovered.unpreserve(record.artifact_id, actor="test")
        token, _, _ = recovered.plan()
        self.assertTrue(recovered.apply(token).complete)
        recovered.close()
        final = self.real_scheduler().service
        self.assertEqual(final.library()["storage"]["local"]["count"], 1)
        self.assertEqual(foreign.read_bytes(), b"foreign backup-looking bytes")


class CapturedConfigSchedulerTests(SchedulerTestBase):
    """Automatic ticks with real encrypted archives and disposable config files."""

    def setUp(self) -> None:
        super().setUp()
        from app.config import get_settings
        from history_service.backup_scheduler.service import BackupScheduler, snapshot_config_files
        from history_service.config import HistorySettings
        from history_service.system_backup import SystemBackupService
        from types import SimpleNamespace

        self.config_path = self.root / "config.yaml"
        self.config_path.write_text("systems: []\n")
        self.mapping_path = self.root / "slot_mappings.json"
        self.mapping_path.write_text('{"version": 1, "slot_mappings": {}}')
        self.slot_path = self.root / "slot_detail_cache.json"
        self.slot_path.write_text('{"version": 1, "slot_details": {}}')
        self.hashed = {"config": self.config_path, "mappings": self.mapping_path}
        self.passphrase = "synthetic-capture-test"
        self._paths.passphrase_file.write_text(self.passphrase)
        self._paths.passphrase_file.chmod(0o600)
        env = patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)})
        env.start()
        self.addCleanup(env.stop)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        self.service = SystemBackupService(
            HistorySettings(sqlite_path=str(self.root / "unused.db")),
            SimpleNamespace(file_path=self.root / "unused.db", segment_catalog_path=None),
        )
        self.after_snapshot = lambda: None

        def snapshot():
            captured = snapshot_config_files(self.hashed)
            self.after_snapshot()
            return captured

        policy_path = self.root / "policy.yaml"
        policy_path.write_text(yaml.safe_dump({"backups": {"config": {
            "enabled": True, "debounce_seconds": 10, "max_delay_seconds": 60,
            "local_keep": 20,
        }}}))
        self.scheduler = BackupScheduler(
            load_backup_policy(policy_path, {}), self.service, self._paths,
            app_gid=os.getegid(),
            config_groups=["config_file", "mapping_file", "slot_detail_file"],
            full_groups=["config_file", "mapping_file", "slot_detail_file", "history_db"],
            snapshot_config=snapshot, clock=lambda: self.now,
            monotonic=lambda: self.mono[0],
        )
        self.addCleanup(self.scheduler.close)
        self.journal = self.scheduler.journal

    def due_tick(self) -> None:
        self.scheduler.tick()  # observe the externally journalled change
        self.mono[0] += 11
        self.now += timedelta(seconds=11)
        self.scheduler.tick()

    def archive_bytes(self, record) -> dict[str, bytes]:
        _manifest, members, _packaging, metadata = self.service._read_archive_file(
            self._paths.local_dir / record.name, passphrase=self.passphrase,
        )
        try:
            self.assertTrue(metadata["encrypted"])
            return {key: self.service._extracted_member_bytes(member) for key, member in members.items()}
        finally:
            self.service._cleanup_extracted_archive(metadata.get("_cleanup_root"))

    def archive_documents(self, record) -> dict[str, Any]:
        members = self.archive_bytes(record)
        documents = {}
        for logical, group in (("config", "config_file"), ("mappings", "mapping_file")):
            content = members.get(group)
            if content is None:
                documents[logical] = None
            else:
                text = content.decode("utf-8")
                documents[logical] = json.loads(text) if logical == "mappings" else yaml.safe_load(text)
        return documents

    def test_captured_bytes_preserve_formatting_volatile_fields_and_deleted_document(self) -> None:
        from history_service.backup_archive.journal import canonical_config_hash
        from history_service.backup_scheduler.service import snapshot_config_files

        config_bytes = b"# Preserve this comment\r\nsystems: []\r\nupdated_at: captured\r\n"
        mapping_bytes = b'{ "version": 1, "slot_mappings": {}, "updated_at": "captured" }\n'
        self.config_path.write_bytes(config_bytes)
        self.mapping_path.write_bytes(mapping_bytes)
        expected_hash = canonical_config_hash(snapshot_config_files(self.hashed))

        def replace_after_snapshot():
            self.config_path.write_text("systems: []\nupdated_at: replaced\n")
            self.mapping_path.unlink()
            self.slot_path.write_text('{"version": 1, "slot_details": {}, "updated_at": "live-cache"}')
            self.journal.append("mapping.save", "deleted")
            self.after_snapshot = lambda: None

        self.after_snapshot = replace_after_snapshot
        self.journal.append("mapping.save", "initial")
        self.due_tick()
        members = self.archive_bytes(self.scheduler.catalog.list()[0])
        self.assertEqual(members["config_file"], config_bytes)
        self.assertEqual(members["mapping_file"], mapping_bytes)
        self.assertEqual(members["slot_detail_file"], self.slot_path.read_bytes())
        self.assertEqual(self.journal.last_backup()[0], expected_hash)
        self.assertEqual([entry.subject for entry in self.journal.pending()], ["deleted"])
        self.mono[0] += 11
        self.now += timedelta(seconds=11)
        self.scheduler.tick()
        records = self.scheduler.catalog.list()
        self.assertEqual(len(records), 2)
        self.assertNotIn("mapping_file", self.archive_bytes(records[-1]))
        self.assertEqual(self.journal.pending(), [])

    def test_automatic_hash_a_capture_b_revert_a_never_consumes_unbacked_a(self) -> None:
        generation_a = self.config_path.read_bytes()
        first = self.journal.append("system.save", "generation-a")
        late = []

        def change_after_snapshot():
            self.config_path.write_text("systems: []\napp: {source_bundle_cache_ttl_seconds: 123}\n")
            late.append(self.journal.append("system.save", "generation-b"))
            self.after_snapshot = lambda: None

        self.after_snapshot = change_after_snapshot
        self.scheduler.tick()
        self.assertEqual(self.scheduler.catalog.list(), [])  # debounce still applies
        self.mono[0] += 11
        self.scheduler.tick()
        initial = self.scheduler.catalog.list()
        self.assertEqual(len(initial), 1)
        self.assertEqual(initial[0].change_ids, (first.change_id,))
        self.assertEqual([entry.change_id for entry in self.journal.pending()], [late[0].change_id])
        self.scheduler.tick()
        self.assertEqual(len(self.scheduler.catalog.list()), 1)  # late edit has its own debounce

        self.config_path.write_bytes(generation_a)
        self.journal.append("system.save", "revert-a")
        self.due_tick()
        self.assertEqual(self.journal.pending(), [])
        latest = self.scheduler.catalog.list()[-1]
        self.assertEqual(self.archive_documents(latest)["config"], yaml.safe_load(generation_a))
        from history_service.backup_archive.journal import canonical_config_hash
        self.assertEqual(self.journal.last_backup()[0], canonical_config_hash(self.archive_documents(latest)))

    def test_late_generation_gets_its_own_automatic_archive(self) -> None:
        from history_service.backup_archive.journal import canonical_config_hash

        def change_after_snapshot():
            self.config_path.write_text("systems: []\napp: {source_bundle_cache_ttl_seconds: 123}\n")
            self.journal.append("system.save", "generation-b")
            self.after_snapshot = lambda: None

        self.after_snapshot = change_after_snapshot
        self.journal.append("system.save", "generation-a")
        self.due_tick()
        first = self.scheduler.catalog.list()[0]
        first_docs = self.archive_documents(first)
        self.assertEqual(first_docs["config"], {"systems": []})
        self.assertEqual(self.journal.last_backup()[0], canonical_config_hash(first_docs))
        self.mono[0] += 11
        self.now += timedelta(seconds=11)
        self.scheduler.tick()
        records = self.scheduler.catalog.list()
        self.assertEqual(len(records), 2)
        last_docs = self.archive_documents(records[-1])
        self.assertEqual(last_docs["config"]["app"]["source_bundle_cache_ttl_seconds"], 123)
        self.assertEqual(self.journal.last_backup()[0], canonical_config_hash(last_docs))
        self.assertEqual(self.journal.pending(), [])

    def test_missing_captured_file_stays_absent_when_created_before_export(self) -> None:
        self.mapping_path.unlink()

        def create_after_snapshot():
            self.mapping_path.write_text('{"version": 1, "slot_mappings": {}}')
            self.journal.append("mapping.save", "created")
            self.after_snapshot = lambda: None

        self.after_snapshot = create_after_snapshot
        self.journal.append("mapping.save", "removed")
        self.due_tick()
        self.assertIsNone(self.archive_documents(self.scheduler.catalog.list()[0])["mappings"])
        self.mono[0] += 11
        self.now += timedelta(seconds=11)
        self.scheduler.tick()
        self.assertIsNotNone(self.archive_documents(self.scheduler.catalog.list()[-1])["mappings"])
        self.assertEqual(self.journal.pending(), [])

    def test_volatile_fields_and_unhashed_cache_do_not_trigger_automatic_archive(self) -> None:
        self.journal.append("system.save", "initial")
        self.due_tick()
        self.config_path.write_text("systems: []\nupdated_at: later\n")
        self.slot_path.write_text('{"version": 1, "slot_details": {}, "updated_at": "later"}')
        self.journal.append("system.save", "volatile")
        self.due_tick()
        self.assertEqual(len(self.scheduler.catalog.list()), 1)
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(self.journal.entries()[0].status, "noop")

    def test_failed_capture_export_retries_current_generation(self) -> None:
        from history_service.system_backup import SystemBackupService

        entry = self.journal.append("system.save", "generation-a")
        with patch.object(SystemBackupService, "_build_archive_to_path", side_effect=OSError("synthetic failure")):
            self.due_tick()
        self.assertEqual(self.scheduler.catalog.list(), [])
        self.assertEqual(self.journal.last_backup(), (None, None))
        self.assertEqual([item.change_id for item in self.journal.pending()], [entry.change_id])
        self.config_path.write_text("systems: []\napp: {source_bundle_cache_ttl_seconds: 456}\n")
        self.scheduler.tick()
        self.assertEqual(self.scheduler.catalog.list(), [])  # retry backoff
        self.mono[0] += 11
        self.scheduler.tick()
        record = self.scheduler.catalog.list()[0]
        self.assertEqual(self.archive_documents(record)["config"]["app"]["source_bundle_cache_ttl_seconds"], 456)
        self.assertEqual(self.journal.pending(), [])

    def test_manual_run_now_still_archives_after_noop_and_idle(self) -> None:
        self.journal.append("system.save", "initial")
        self.due_tick()
        self.journal.append("system.save", "unchanged")
        after_noop = self.scheduler.run_now("config")
        self.config_path.write_text("systems: []\napp: {source_bundle_cache_ttl_seconds: 789}\n")
        after_idle = self.scheduler.run_now("config")
        self.assertIsNotNone(after_noop)
        self.assertIsNotNone(after_idle)
        self.assertNotEqual(after_noop.artifact_id, after_idle.artifact_id)
        self.assertEqual(len(self.scheduler.catalog.list()), 3)
        self.assertEqual(self.archive_documents(after_noop)["config"], {"systems": []})
        self.assertEqual(self.archive_documents(after_idle)["config"]["app"]["source_bundle_cache_ttl_seconds"], 789)


class ProductionPathCaptureTests(unittest.TestCase):
    """Production constructor, runner and encrypted members; only the unused history store is stubbed."""

    @contextlib.contextmanager
    def built(self, *, nested=True, initial=None, mutate=None):
        from types import SimpleNamespace
        from app.config import _derive_runtime_layout_paths, get_settings
        from history_service.backup_scheduler import service as scheduler_service
        from history_service.backup_scheduler.main import build_scheduler
        from history_service.config import HistorySettings

        with tempfile.TemporaryDirectory(prefix="capture-paths-") as name:
            root = Path(name)
            config = root / "config" / "config.yaml" if nested else root / "config.yaml"
            config.parent.mkdir(parents=True, exist_ok=True)
            layout = {k: Path(v) for k, v in _derive_runtime_layout_paths(config).items()}
            raw = {
                "config_file": b"# synthetic UTF-8 caf\xc3\xa9\r\nsystems: []\r\n",
                "runtime_overrides_file": b"{}\n",
                "profile_file": b"profiles: []\n",
                "mapping_file": b'{"version":1,"slot_mappings":{}}\n',
                "sas_fabric_alias_file": b'{"version":1,"sas_fabric_aliases":{}}\n',
                "slot_detail_cache_file": b'{"version":1,"slot_details":{}}\n',
            }
            for key, content in raw.items():
                layout[key].parent.mkdir(parents=True, exist_ok=True)
                layout[key].write_bytes(content)
            if initial is not None:
                initial(layout, raw)
            phrase = root / "synthetic-passphrase"
            phrase.write_text("synthetic-capture-only")
            phrase.chmod(0o600)
            status = root / "status"
            status.mkdir(mode=0o2750)
            status.chmod(0o2750)
            journal_dir = root / "journal"
            journal_dir.mkdir(mode=0o2770)
            journal_dir.chmod(0o2770)
            settings = HistorySettings(sqlite_path=str(root / "history.db"),
                                       backup_dir=str(root / "history-backups"),
                                       long_term_backup_dir=str(root / "history-longterm"))
            environment = {
                "APP_CONFIG_PATH": str(config), "APP_GID": str(os.getegid()),
                "BACKUP_ARCHIVE_DIR": str(root / "archives"),
                "BACKUP_ARCHIVE_STATE_DIR": str(root / "state"),
                "BACKUP_JOURNAL_PATH": str(journal_dir / "changes.jsonl"),
                "BACKUP_ARCHIVE_STATUS_FILE": str(status / "archive.json"),
                "BACKUP_ARCHIVE_PASSPHRASE_FILE": str(phrase),
                "SCHEDULED_BACKUP_STATUS_FILE": "",
            }
            original_capture = scheduler_service.snapshot_config_files
            state: dict[str, Any] = {"armed": True}

            def capture(paths):
                result = original_capture(paths)
                state["capture"] = result
                self.assertEqual(set(result), {"config", "runtime_overrides", "profiles", "mappings", "sas_fabric_aliases"})
                if state["armed"]:
                    state["armed"] = False
                    if mutate is not None:
                        mutate(layout, raw, state)
                    state["late"] = state["scheduler"].journal.append("system.save", "synthetic-late-b")
                return result

            with patch.dict(os.environ, environment), patch("history_service.config.get_history_settings", return_value=settings), patch("history_service.store.HistoryStore", return_value=SimpleNamespace(file_path=root / "history.db", segment_catalog_path=None)), patch.object(scheduler_service, "snapshot_config_files", capture):
                get_settings.cache_clear()
                policy = load_backup_policy(config, {"BACKUP_CONFIG_ENABLED": "true"})
                scheduler = build_scheduler(policy)
                state["scheduler"] = scheduler
                clock = [1000.0]
                scheduler.coalescer._clock = lambda: clock[0]
                try:
                    yield scheduler, state, layout, raw, clock
                finally:
                    scheduler.close()
                    get_settings.cache_clear()

    def decrypt(self, scheduler, record):
        service = scheduler.backup_service
        manifest, members, packaging, metadata = service._read_archive_file(
            scheduler.paths.local_dir / record.name, passphrase="synthetic-capture-only")
        try:
            self.assertTrue(metadata["encrypted"])
            self.assertEqual(packaging, "tar.zst")
            return {key: service._extracted_member_bytes(value) for key, value in members.items()}
        finally:
            service._cleanup_extracted_archive(metadata.get("_cleanup_root"))

    def due(self, scheduler, clock):
        scheduler.tick()
        clock[0] += scheduler.coalescer.quiet_period + 1
        scheduler.tick()

    def projection(self, members):
        docs = {}
        for logical, group in {"config": "config_file", "runtime_overrides": "runtime_overrides_file", "profiles": "profile_file", "mappings": "mapping_file", "sas_fabric_aliases": "sas_fabric_alias_file"}.items():
            content = members.get(group)
            docs[logical] = None if content is None else (json.loads(content) if logical in {"mappings", "sas_fabric_aliases"} else yaml.safe_load(content))
        return docs

    def assert_capture_archive(self, scheduler, state, raw, *, omitted=()):
        from history_service.backup_archive.journal import canonical_config_hash

        records = scheduler.catalog.list()
        self.assertEqual(len(records), 1, scheduler._status)
        members = self.decrypt(scheduler, records[0])
        for key in ("config_file", "runtime_overrides_file", "profile_file",
                    "mapping_file", "sas_fabric_alias_file"):
            if key in omitted:
                self.assertNotIn(key, members)
            else:
                self.assertEqual(members[key], raw[key], key)
        self.assertEqual(scheduler.journal.last_backup()[0], canonical_config_hash(state["capture"]))
        self.assertEqual(scheduler.journal.last_backup()[0], canonical_config_hash(self.projection(members)))
        self.assertEqual([x.change_id for x in scheduler.journal.pending()], [state["late"].change_id])
        self.assertEqual(dict(scheduler.backup_service._captured_config_files), {})
        return members

    @staticmethod
    def redirect_paths(layout, raw, state):
        paths = {}
        for key in ("runtime_overrides_file", "profile_file", "mapping_file", "sas_fabric_alias_file"):
            target = layout[key].with_name("alternate-" + layout[key].name)
            # Valid, semantically distinct documents exercise real archive validation.
            redirected = {
                "runtime_overrides_file": {"app": {"source_bundle_cache_ttl_seconds": 97}},
                "profile_file": {"profiles": [{"id": "synthetic-b", "label": "Synthetic B", "rows": 1, "columns": 1}]},
                "mapping_file": {"version": 1, "slot_mappings": {"synthetic-s:synthetic-e:1": {
                    "system_id": "synthetic-s", "enclosure_id": "synthetic-e", "slot": 1, "serial": "SANITIZED-CAPTURE-B",
                }}},
                "sas_fabric_alias_file": {"version": 1, "sas_fabric_aliases": {"synthetic-port": {
                    "object_id": "synthetic-port", "label": "Synthetic B",
                }}},
            }[key]
            text = json.dumps(redirected) if key.endswith("mapping_file") or key == "sas_fabric_alias_file" else yaml.safe_dump(redirected)
            target.write_text(text)
            paths[key] = str(target)
        layout["config_file"].write_text(yaml.safe_dump({"systems": [], "paths": paths}))
        state["redirected"] = paths

    def test_config_redirects_cannot_bypass_capture_and_revert_noop(self):
        for nested in (True, False):
            with self.subTest(nested=nested), self.built(nested=nested, mutate=self.redirect_paths) as (
                scheduler, state, layout, raw, clock
            ):
                first = scheduler.journal.append("system.save", "synthetic-a")
                self.due(scheduler, clock)
                self.assert_capture_archive(scheduler, state, raw)
                self.assertEqual(scheduler.catalog.list()[0].change_ids, (first.change_id,))
                layout["config_file"].write_bytes(raw["config_file"])
                scheduler.journal.append("system.save", "synthetic-revert-a")
                self.due(scheduler, clock)
                self.assertEqual(len(scheduler.catalog.list()), 1)
                self.assertEqual(scheduler.journal.pending(), [])

    def test_runtime_overrides_remain_filtered_and_captured(self):
        def initial(layout, raw):
            raw["runtime_overrides_file"] = yaml.safe_dump({
                "app": {"source_bundle_cache_ttl_seconds": 41},
                "paths": {"mapping_file": str(layout["mapping_file"].with_name("ignored.json"))},
            }).encode()
            layout["runtime_overrides_file"].write_bytes(raw["runtime_overrides_file"])
            # The list form is an existing profile-loader contract.
            raw["profile_file"] = b"- {id: synthetic-a, label: Synthetic A, rows: 1, columns: 1}\n"
            layout["profile_file"].write_bytes(raw["profile_file"])

        def mutate(layout, raw, state):
            layout["runtime_overrides_file"].write_text(yaml.safe_dump({
                "app": {"source_bundle_cache_ttl_seconds": 97},
                "paths": {"profile_file": str(layout["profile_file"].with_name("ignored.yaml"))},
            }))
            layout["profile_file"].unlink()  # retained profile still supplies settings

        with self.built(initial=initial, mutate=mutate) as (scheduler, state, layout, raw, clock):
            scheduler.journal.append("system.save", "synthetic-a")
            self.due(scheduler, clock)
            self.assert_capture_archive(scheduler, state, raw)
            from app.config import get_settings
            cached = get_settings()
            live = scheduler.backup_service._load_app_settings()
            captured_service = scheduler.backup_service.with_captured_config_files(state["capture"].files)
            settings = captured_service._load_app_settings()
            self.assertIsNot(live, cached)
            self.assertIs(get_settings(), cached)
            self.assertEqual(settings.app.source_bundle_cache_ttl_seconds, 41)
            self.assertEqual([profile.id for profile in settings.profiles], ["synthetic-a"])
            self.assertEqual(Path(settings.paths.mapping_file), layout["mapping_file"])
            self.assertEqual(Path(settings.paths.profile_file), layout["profile_file"])
            self.assertEqual(scheduler.backup_service._load_app_settings().app.source_bundle_cache_ttl_seconds, 97)

    def test_legacy_path_defaults_are_normalized_before_captured_selection(self):
        def initial(layout, raw):
            from app.config import _legacy_container_layout_paths
            legacy = _legacy_container_layout_paths()
            raw["config_file"] = yaml.safe_dump({"systems": [], "paths": {
                key: legacy[key] for key in ("mapping_file", "profile_file", "runtime_overrides_file",
                                             "sas_fabric_alias_file")
            }}).encode()
            layout["config_file"].write_bytes(raw["config_file"])

        with self.built(initial=initial, mutate=self.redirect_paths) as (scheduler, state, _, raw, clock):
            scheduler.journal.append("system.save", "synthetic-a")
            self.due(scheduler, clock)
            self.assert_capture_archive(scheduler, state, raw)

    def test_absent_captured_settings_do_not_read_late_path_redirects(self):
        def initial(layout, raw):
            for key in ("config_file", "runtime_overrides_file", "profile_file"):
                layout[key].unlink()

        def mutate(layout, raw, state):
            self.redirect_paths(layout, raw, state)
            layout["runtime_overrides_file"].write_text("app: {source_bundle_cache_ttl_seconds: 97}\n")
            layout["profile_file"].write_text("profiles: invalid\n")

        with self.built(initial=initial, mutate=mutate) as (scheduler, state, _, raw, clock):
            scheduler.journal.append("system.save", "synthetic-a")
            self.due(scheduler, clock)
            self.assert_capture_archive(scheduler, state, raw,
                                        omitted=("config_file", "runtime_overrides_file", "profile_file"))

    def test_custom_path_scope_and_selected_groups_are_not_expanded(self):
        def initial(layout, raw):
            custom = layout["mapping_file"].with_name("custom-mapping.json")
            custom.write_bytes(raw["mapping_file"])
            raw["config_file"] = yaml.safe_dump({"systems": [], "paths": {"mapping_file": str(custom)}}).encode()
            layout["config_file"].write_bytes(raw["config_file"])

        def mutate(layout, raw, state):
            custom = layout["mapping_file"].with_name("custom-mapping.json")
            custom.write_bytes(raw["mapping_file"] + b" \n")
            state["custom"] = custom

        with self.built(initial=initial, mutate=mutate) as (scheduler, state, layout, raw, clock):
            scheduler.groups["config"].remove("profile_file")
            scheduler.journal.append("system.save", "synthetic-a")
            self.due(scheduler, clock)
            record, = scheduler.catalog.list()
            members = self.decrypt(scheduler, record)
            self.assertNotIn("profile_file", members)
            self.assertEqual(members["mapping_file"], state["custom"].read_bytes())
            self.assertNotIn(state["custom"], state["capture"].files)
            self.assertIn(layout["mapping_file"], state["capture"].files)

    def test_environment_path_override_keeps_existing_precedence(self):
        with self.built(mutate=self.redirect_paths) as (scheduler, state, layout, raw, clock):
            custom = layout["mapping_file"].with_name("environment-mapping.json")
            custom_bytes = raw["mapping_file"] + b" \n"
            custom.write_bytes(custom_bytes)
            with patch.dict(os.environ, {"PATH_MAPPING_FILE": str(custom)}):
                scheduler.journal.append("system.save", "synthetic-a")
                self.due(scheduler, clock)
                record, = scheduler.catalog.list()
                members = self.decrypt(scheduler, record)
                self.assertEqual(members["config_file"], raw["config_file"])
                self.assertEqual(members["mapping_file"], custom_bytes)
                self.assertNotIn(custom, state["capture"].files)
                captured = scheduler.backup_service.with_captured_config_files(state["capture"].files)
                self.assertEqual(Path(captured._load_app_settings().paths.mapping_file), custom)

    def test_manual_noop_and_idle_read_current_path_selection(self):
        with self.built() as (scheduler, state, layout, raw, clock):
            scheduler.journal.append("system.save", "synthetic-a")
            self.due(scheduler, clock)
            # The late unchanged entry makes run_now take its live noop branch.
            after_noop = scheduler.run_now("config")
            self.assertEqual(self.decrypt(scheduler, after_noop)["mapping_file"], raw["mapping_file"])
            self.redirect_paths(layout, raw, state)
            after_idle = scheduler.run_now("config")
            members = self.decrypt(scheduler, after_idle)
            self.assertEqual(members["config_file"], layout["config_file"].read_bytes())
            self.assertEqual(members["mapping_file"], Path(state["redirected"]["mapping_file"]).read_bytes())
            self.assertEqual(dict(scheduler.backup_service._captured_config_files), {})

    def test_invalid_captured_settings_preserve_journal_and_retry_live_generation(self):
        def initial(layout, raw):
            raw["config_file"] = b"systems: []\npaths: {mapping_file: null}\n"
            layout["config_file"].write_bytes(raw["config_file"])

        def mutate(layout, raw, state):
            layout["config_file"].write_text("systems: []\n")

        with self.built(initial=initial, mutate=mutate) as (scheduler, state, layout, raw, clock):
            first = scheduler.journal.append("system.save", "synthetic-invalid-a")
            self.due(scheduler, clock)
            self.assertEqual(scheduler.catalog.list(), [])
            self.assertEqual(scheduler.journal.last_backup(), (None, None))
            self.assertEqual({x.change_id for x in scheduler.journal.pending()},
                             {first.change_id, state["late"].change_id})
            clock[0] += 3600
            scheduler.tick()
            record, = scheduler.catalog.list()
            self.assertEqual(self.decrypt(scheduler, record)["config_file"], layout["config_file"].read_bytes())
            self.assertEqual(scheduler.journal.pending(), [])


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

    def test_full_schedule_keeps_local_wall_clock_across_dst(self) -> None:
        try:
            new_york = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            self.skipTest("America/New_York time zone data is not available")
        self.now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
        scheduler = self.make({"full": {"enabled": True, "schedule": "30 1 * * *"}}, local_tz=new_york)
        self.assertEqual(scheduler.next_full_at, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))  # 01:30 EDT
        self.now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        scheduler.tick()
        self.assertEqual(len(FakeRunner.calls), 1)
        # Fall back: 01:30 repeats; the next run is 01:30 EST on 11-02, not a second run on 11-01.
        self.assertEqual(scheduler.next_full_at, datetime(2026, 11, 2, 6, 30, tzinfo=UTC))
        # A start inside the repeated hour must not pick the already-past first 01:30.
        self.assertEqual(
            scheduler._compute_next_full(datetime(2026, 11, 1, 6, 10, tzinfo=UTC)),
            datetime(2026, 11, 2, 6, 30, tzinfo=UTC),
        )
        # Spring forward: 01:30 EST on 03-08, then 01:30 EDT on 03-09 (not an hour late).
        self.assertEqual(
            scheduler._compute_next_full(datetime(2026, 3, 8, 6, 30, tzinfo=UTC)),
            datetime(2026, 3, 9, 5, 30, tzinfo=UTC),
        )
        # Spring forward: 02:30 does not exist on 03-08, so do not run at
        # 03:30 EDT; wait for 02:30 EDT on 03-09.
        gap_scheduler = self.make(
            {"full": {"enabled": True, "schedule": "30 2 * * *"}},
            local_tz=new_york,
        )
        self.assertEqual(
            gap_scheduler._compute_next_full(datetime(2026, 3, 8, 6, 0, tzinfo=UTC)),
            datetime(2026, 3, 9, 6, 30, tzinfo=UTC),
        )

    def test_scheduler_main_uses_the_tz_zone(self) -> None:
        from history_service.backup_scheduler import main as scheduler_main

        try:
            new_york = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            self.skipTest("America/New_York time zone data is not available")
        config = self.root / "idle.yaml"
        config.write_text("{}\n")
        env = {"APP_CONFIG_PATH": str(config), "APP_GID": str(os.getegid()),
               "BACKUP_ARCHIVE_PASSPHRASE_FILE": "", "SCHEDULED_BACKUP_PASSPHRASE_FILE": "",
               "BACKUP_ARCHIVE_DIR": str(self.root / "a"),
               "BACKUP_ARCHIVE_STATE_DIR": str(self.root / "s"), "TZ": "America/New_York"}
        with patch.dict(os.environ, env):
            with patch("history_service.system_backup.SystemBackupService"), patch("history_service.store.HistoryStore"):
                scheduler = scheduler_main.build_scheduler(load_backup_policy(config, {}))
            self.addCleanup(scheduler.close)
            self.assertEqual(scheduler._local_tz, new_york)
        for value in ("", "  "):
            with self.subTest(tz=value), patch.dict(os.environ, {"TZ": value}):
                self.assertIsNone(scheduler_main._local_tz())
        with patch.dict(os.environ, {"TZ": "Not/AZone"}), self.assertLogs(scheduler_main.logger, "WARNING"):
            self.assertIsNone(scheduler_main._local_tz())

    def test_verified_full_replaces_only_older_history_sidecar_copies(self) -> None:
        scheduler = self.make({"full": {"enabled": True}})
        for _ in range(13):
            scheduler.run_now("full")
            self.now += timedelta(days=1)
        daily = self._paths.history_backup_dir
        long_term = self._paths.history_long_term_backup_dir
        old_paths = [
            daily / "history-20260922T030000Z.sqlite3",
            long_term / "weekly" / "history-weekly-2026-W37.sqlite3",
            long_term / "monthly" / "history-monthly-2026-08.sqlite3",
        ]
        newer = daily / "history-20260925T030000Z.sqlite3"
        unrelated = daily / "operator-note.txt"
        hardlink_source = daily / "held-source.sqlite3"
        hardlink_copy = daily / "history-20260921T030000Z.sqlite3"
        symlink_copy = daily / "history-20260920T030000Z.sqlite3"
        for path in [*old_paths, newer, unrelated, hardlink_source]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"sidecar snapshot")
        os.link(hardlink_source, hardlink_copy)
        symlink_copy.symlink_to(unrelated.name)
        older_stamp = (self.now - timedelta(days=2)).timestamp()
        newer_stamp = (self.now + timedelta(hours=1)).timestamp()
        for path in [*old_paths, hardlink_source]:
            os.utime(path, (older_stamp, older_stamp))
        os.utime(newer, (newer_stamp, newer_stamp))

        record = scheduler.run_now("full")

        self.assertIsNotNone(record)
        self.assertTrue(all(not path.exists() for path in old_paths))
        self.assertTrue(newer.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(hardlink_source.exists())
        self.assertTrue(hardlink_copy.exists())
        self.assertTrue(symlink_copy.is_symlink())
        status = json.loads(self._paths.status_file.read_text(encoding="utf-8"))
        self.assertEqual(
            status["verified_full"],
            {
                "artifact_id": record.artifact_id,
                "created_at": self.now.isoformat(),
                "included_groups": ["config_file", "mapping_file", "history_db"],
            },
        )

    def test_first_verified_full_does_not_collapse_fourteen_sidecars_to_one_copy(self) -> None:
        scheduler = self.make({"full": {"enabled": True}})
        backup_dir = self._paths.history_backup_dir
        sidecars: list[Path] = []
        for age in range(14, 0, -1):
            created = self.now - timedelta(days=age)
            path = backup_dir / f"history-{created:%Y%m%dT%H%M%S}Z.sqlite3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"sidecar {age}".encode())
            stamp = created.timestamp()
            os.utime(path, (stamp, stamp))
            sidecars.append(path)

        scheduler.run_now("full")

        remaining = [path for path in sidecars if path.exists()]
        self.assertEqual(len(remaining), 13)
        self.assertFalse(sidecars[0].exists())
        self.assertTrue(all(path.exists() for path in sidecars[1:]))
        self.assertEqual(
            len(scheduler.catalog.list(backup_class="full", location="local")) + len(remaining),
            14,
        )

    def test_fulls_missing_history_do_not_count_toward_cutover_coverage(self) -> None:
        scheduler = self.make({"full": {"enabled": True, "local_keep": 14}})
        backup_dir = self._paths.history_backup_dir
        sidecars: list[Path] = []
        for age in range(14, 0, -1):
            created = self.now - timedelta(days=age)
            path = backup_dir / f"history-{created:%Y%m%dT%H%M%S}Z.sqlite3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"sidecar {age}".encode())
            stamp = created.timestamp()
            os.utime(path, (stamp, stamp))
            sidecars.append(path)

        FakeRunner.absent_groups = ["history_db"]
        for _ in range(13):
            scheduler.run_now("full")
            self.now += timedelta(hours=1)
        FakeRunner.absent_groups = []
        scheduler.run_now("full")

        self.assertEqual(sum(path.exists() for path in sidecars), 13)

    def test_sidecar_copies_survive_until_a_catalogued_full_contains_history(self) -> None:
        scheduler = self.make({"full": {"enabled": True}})
        old = self._paths.history_backup_dir / "history-20260922T030000Z.sqlite3"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"sidecar snapshot")
        older_stamp = (self.now - timedelta(days=2)).timestamp()
        os.utime(old, (older_stamp, older_stamp))

        FakeRunner.absent_groups = ["history_db"]
        scheduler.run_now("full")
        self.assertTrue(old.exists())
        status = json.loads(self._paths.status_file.read_text(encoding="utf-8"))
        self.assertNotIn("verified_full", status)

        FakeRunner.absent_groups = []
        with patch.object(scheduler.catalog, "add", side_effect=RuntimeError("catalog unavailable")):
            with self.assertRaisesRegex(RuntimeError, "catalog unavailable"):
                scheduler.run_now("full")
        self.assertTrue(old.exists())

    def test_restart_rewrites_a_stale_verified_full_receipt(self) -> None:
        scheduler = self.make({"full": {"enabled": True}})
        record = scheduler.run_now("full")
        status_before = json.loads(self._paths.status_file.read_text(encoding="utf-8"))
        self.assertEqual(status_before["verified_full"]["artifact_id"], record.artifact_id)

        scheduler.catalog.mark_unverified(record.artifact_id)
        restarted = self.make({"full": {"enabled": True}})

        status_after = json.loads(self._paths.status_file.read_text(encoding="utf-8"))
        self.assertNotIn("verified_full", status_after)
        self.assertNotIn("verified_full", restarted._status)

    def test_target_failure_is_degraded_status_and_other_targets_still_ship(self) -> None:
        second = {**TARGET, "target_id": "cloud", "label": "Cloud", "root": "/unused-cloud"}
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

    def test_stalled_sftp_target_times_out_and_later_targets_still_ship(self) -> None:
        # #723: a real loopback SFTP server stalls after authentication. The
        # shipping job must give up within the target timeout, release its job
        # reservation, leave no client connection running, and still ship to
        # the next target.
        import time as time_module

        from history_service.backup_archive import transport
        from history_service.backup_archive.settings import ArchiveTargetSettings
        from tests import sftp_stall_server

        stalled = {**TARGET, "target_id": "stalled", "label": "Stalled SFTP", "root": "/unused-stalled"}
        second = {**TARGET, "target_id": "cloud", "label": "Cloud", "root": "/unused-cloud"}
        secret = self.root / "sftp-password"
        secret.write_text(sftp_stall_server.PASSWORD + "\n", encoding="utf-8")
        secret.chmod(0o600)
        known_hosts = self.root / "known_hosts"
        before = sftp_stall_server.live_client_transports()
        default_open = self.open_target
        with sftp_stall_server.StallingSftpServer(stall="request") as server:
            server.write_known_hosts(known_hosts)

            def open_target(settings):
                if settings.target_id != "stalled":
                    return default_open(settings)
                return transport.open_target(ArchiveTargetSettings(
                    target_id="stalled", provider="sftp", root="/srv/backups/jbod-ui",
                    hostname="127.0.0.1", port=server.port, username=sftp_stall_server.USERNAME,
                    password_file=str(secret), known_hosts_path=str(known_hosts), timeout_seconds=0.5,
                ))

            self.open_target = open_target  # type: ignore[method-assign]
            scheduler = self.make({"full": {"enabled": True}, "targets": [stalled, second]})
            started = time_module.monotonic()
            scheduler.run_now("full")
            elapsed = time_module.monotonic() - started
            self.assertTrue(server.reached.is_set())
        self.assertLess(elapsed, 4.0)
        self.assertEqual(sorted(r.location for r in scheduler.catalog.list()), ["cloud", "local"])
        status = json.loads(self._paths.status_file.read_text())
        self.assertFalse(status["targets"]["stalled"]["ok"])
        self.assertIn("TimeoutError", status["targets"]["stalled"]["detail"])
        self.assertTrue(status["targets"]["cloud"]["ok"])
        self.assertIsNone(scheduler.running)
        scheduler.run_now("config")  # the job reservation was released
        deadline = time_module.monotonic() + 5
        while sftp_stall_server.live_client_transports() - before and time_module.monotonic() < deadline:
            time_module.sleep(0.05)
        self.assertEqual(sftp_stall_server.live_client_transports() - before, set())

    def test_disabled_target_gets_no_retention_rule(self) -> None:
        disabled = {**TARGET, "target_id": "cloud", "label": "Cloud", "enabled": False}
        scheduler = self.make({"full": {"enabled": True, "remote_keep": 3}, "targets": [TARGET, disabled]})
        self.assertEqual({rule.location for rule in scheduler.retention_rules()}, {"local", "nas"})

    def test_unreachable_target_still_grooms_local_and_reports_grooming(self) -> None:
        scheduler = self.make({"full": {"enabled": True, "local_keep": 3, "remote_keep": 3}, "targets": [TARGET]})
        for hour in (13, 14, 15):
            self.now = datetime(2026, 9, 24, hour, 0, tzinfo=UTC)
            scheduler.run_now("full")
        scheduler.close()
        self.broken_targets = {"nas"}
        scheduler = self.make({"full": {"enabled": True, "local_keep": 1, "remote_keep": 1}, "targets": [TARGET]})
        self.now = datetime(2026, 9, 24, 16, 0, tzinfo=UTC)
        backup_dir = self._paths.history_backup_dir
        sidecars: list[Path] = []
        for age in range(14, 0, -1):
            created = self.now - timedelta(days=age)
            path = backup_dir / f"history-{created:%Y%m%dT%H%M%S}Z.sqlite3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"sidecar {age}".encode())
            stamp = created.timestamp()
            os.utime(path, (stamp, stamp))
            sidecars.append(path)
        scheduler.run_now("full")
        # The remote outage must not block the local sidecar replacement cleanup.
        self.assertFalse(sidecars[0].exists())
        self.assertTrue(all(path.exists() for path in sidecars[1:]))
        self.assertEqual(len(scheduler.catalog.list(location="local")), 1)
        self.assertEqual(len(scheduler.catalog.list(location="nas")), 3)
        grooming = json.loads(self._paths.status_file.read_text())["grooming"]
        self.assertFalse(grooming["ok"])
        self.assertEqual(grooming["deleted"], 3)
        self.assertEqual(sorted(grooming["failed_locations"]), ["nas"])
        self.assertIn("connection refused", grooming["failed_locations"]["nas"])

        from app.services.backup_health import backup_archive_problems

        problems = backup_archive_problems(self._paths.status_file)
        self.assertTrue(any(p.startswith("Backup grooming stopped: ConnectionRefusedError") for p in problems))

    def test_unverified_remote_copy_is_a_failed_run_and_never_catalogued(self) -> None:
        import dataclasses

        class UnverifiedTarget(LocalDirectoryTarget):
            def put(self, local_path, name):
                return dataclasses.replace(super().put(local_path, name), verified=False)

        remote_dir = self.remote_root / "nas"

        @contextlib.contextmanager
        def opened(_settings):
            yield UnverifiedTarget(remote_dir)

        scheduler = self.make({"full": {"enabled": True}, "targets": [TARGET]})
        scheduler._open_target = opened
        record = scheduler.run_now("full")
        self.assertEqual([r.location for r in scheduler.catalog.list()], ["local"])
        status = json.loads(self._paths.status_file.read_text())
        self.assertFalse(status["targets"]["nas"]["ok"])
        self.assertIn("remote copy could not be verified", status["targets"]["nas"]["detail"])
        self.assertFalse((remote_dir / record.name).exists())

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

    def test_deeply_nested_journal_line_is_a_pending_change_and_full_still_runs(self) -> None:
        scheduler = self.make({
            "config": {"enabled": True},
            "full": {"enabled": True, "schedule": "0 * * * *"},
        })
        with open(self._paths.journal_path, "ab") as handle:
            handle.write(b"[" * 60000 + b"\n")
        self.now = datetime(2026, 9, 24, 13, 0, tzinfo=UTC)
        scheduler.tick()
        self.assertEqual([call["groups"] for call in FakeRunner.calls], [["config_file", "mapping_file", "history_db"]])
        self.assertEqual(scheduler.library()["classes"]["config"]["pending_changes"], 1)

    def test_unreadable_journal_is_recorded_once_and_never_blocks_start_or_full(self) -> None:
        from history_service.backup_archive.journal import JournalError

        with patch.object(ChangeJournal, "pending", side_effect=JournalError("journal is not a regular file")):
            scheduler = self.make({
                "config": {"enabled": True},
                "full": {"enabled": True, "schedule": "0 * * * *"},
            })
            self.now = datetime(2026, 9, 24, 13, 0, tzinfo=UTC)
            with self.assertLogs("history_service.backup_scheduler.service", "WARNING") as logs:
                scheduler.tick()
                scheduler.tick()
        self.assertEqual(len(FakeRunner.calls), 1)
        self.assertIn("history_db", FakeRunner.calls[0]["groups"])
        self.assertEqual(len([line for line in logs.output if "journal is not a regular file" in line]), 1)
        config_run = scheduler.library()["classes"]["config"]["last_run"]
        self.assertIs(config_run["ok"], False)
        self.assertIn("journal is not a regular file", config_run["detail"])
        scheduler.tick()  # readable again: the check failure is withdrawn
        self.assertIsNone(scheduler.library()["classes"]["config"]["last_run"])

    def test_journal_check_failure_persisted_across_restart_is_withdrawn_once_readable(self) -> None:
        from history_service.backup_archive.journal import JournalError

        policy = {"config": {"enabled": True}, "full": {"enabled": True, "schedule": "0 * * * *"}}
        self.now = datetime(2026, 9, 24, 13, 0, tzinfo=UTC)
        with patch.object(ChangeJournal, "pending", side_effect=JournalError("journal is not a regular file")):
            scheduler = self.make(policy)
            scheduler.tick()
            scheduler.close()
            # Still unreadable after a restart: the stored check failure is not
            # taken for the last real run.
            scheduler = self.make(policy)
            scheduler.tick()
        self.assertIn("journal is not a regular file", scheduler.library()["classes"]["config"]["last_run"]["detail"])
        scheduler.close()
        scheduler = self.make(policy)
        scheduler.tick()  # readable again after a restart
        self.assertIsNone(scheduler.library()["classes"]["config"]["last_run"])
        self.assertNotIn("config", json.loads(self._paths.status_file.read_text())["classes"])

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

    def test_oversized_remote_download_stops_before_catalog_check(self) -> None:
        from history_service.backup_archive.transport import ArchiveVerificationError

        scheduler = self.make({"full": {"enabled": True}, "targets": [TARGET]})
        scheduler.run_now("full")
        (remote,) = scheduler.catalog.list(location="nas")
        (self.remote_root / "nas" / remote.name).write_bytes(b"x" * (remote.size + 2))
        with self.assertRaisesRegex(ArchiveVerificationError, "larger than expected"), scheduler.materialize(remote.artifact_id):
            pass
        self.assertEqual(list(self._paths.state_dir.glob("backup-fetch-*")), [])
        # A copy that grew past the cap no longer matches the catalogue, so verify
        # must stop counting it as verified, like any other integrity mismatch.
        self.assertTrue(scheduler.catalog.get(remote.artifact_id).verified)
        failed = scheduler.verify(remote.artifact_id)
        self.assertFalse(failed["ok"])
        self.assertFalse(failed["artifact"]["verified"])
        self.assertFalse(failed["artifact"]["restorable"])

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

    def test_runtime_rejects_alias_before_remote_retention_can_share_a_local_copy(self) -> None:
        local_copy = self._paths.local_dir / "full" / "preserved.tar.zst.enc"
        local_copy.parent.mkdir(parents=True)
        local_copy.write_bytes(b"preserved-local-copy")
        alias = self.root / "local-alias"
        alias.symlink_to(self._paths.local_dir, target_is_directory=True)
        target = {**TARGET, "root": str(alias)}

        with self.assertRaisesRegex(ConfigurationError, "must not overlap the local archive root"):
            self.make({"full": {"enabled": True, "local_keep": 2, "remote_keep": 1}, "targets": [target]})

        self.assertEqual(local_copy.read_bytes(), b"preserved-local-copy")
        self.assertFalse((self._paths.state_dir / "catalog.sqlite3").exists())

    def test_runtime_rechecks_alias_before_shipping_or_remote_catalogue_insert(self) -> None:
        physical_remote = self.root / "physical-remote"
        physical_remote.mkdir()
        alias = self.root / "mutable-target"
        alias.symlink_to(physical_remote, target_is_directory=True)
        scheduler = self.make({
            "full": {"enabled": True, "local_keep": 2, "remote_keep": 1},
            "targets": [{**TARGET, "root": str(alias)}],
        })

        alias.unlink()
        alias.symlink_to(self._paths.local_dir, target_is_directory=True)
        local_record = scheduler.run_now("full")
        self.assertIsNotNone(local_record)
        assert local_record is not None

        self.assertEqual([record.location for record in scheduler.catalog.list()], ["local"])
        self.assertTrue((self._paths.local_dir / local_record.name).is_file())
        status_file = self._paths.status_file
        self.assertIsNotNone(status_file)
        assert status_file is not None
        status = json.loads(status_file.read_text())
        self.assertFalse(status["targets"]["nas"]["ok"])
        self.assertIn("must not overlap the local archive root", status["targets"]["nas"]["detail"])

    def test_uninspectable_target_refuses_only_that_target_and_keeps_local_backups(self) -> None:
        from unittest import mock

        from history_service.backup_archive import settings as archive_settings

        unreadable = self.root / "stale-usb"
        unreadable.mkdir()
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if Path(path) == unreadable:
                raise PermissionError(13, "Permission denied")
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(archive_settings.os, "lstat", side_effect=lstat):
            with self.assertLogs("history_service.backup_archive.policy", "WARNING") as logs:
                scheduler = self.make({
                    "full": {"enabled": True, "local_keep": 2, "remote_keep": 1},
                    "targets": [{**TARGET, "root": str(unreadable)}],
                })
            self.assertIn("could not be checked for local archive overlap", "\n".join(logs.output))
            local_record = scheduler.run_now("full")

        self.assertIsNotNone(local_record)
        assert local_record is not None
        self.assertTrue((self._paths.local_dir / local_record.name).is_file())
        self.assertEqual([record.location for record in scheduler.catalog.list()], ["local"])
        status_file = self._paths.status_file
        assert status_file is not None
        status = json.loads(status_file.read_text())
        self.assertFalse(status["targets"]["nas"]["ok"])
        self.assertIn("could not be inspected safely", status["targets"]["nas"]["detail"])

    def test_runtime_rechecks_alias_before_remote_retention_deletes_preserved_local_copy(self) -> None:
        from dataclasses import replace

        from history_service.backup_archive.transport import open_target

        physical_remote = self.root / "physical-remote"
        physical_remote.mkdir()
        alias = self.root / "mutable-target"
        alias.symlink_to(physical_remote, target_is_directory=True)
        scheduler = self.make({
            "full": {"enabled": True, "local_keep": 2, "remote_keep": 2},
            "targets": [{**TARGET, "root": str(alias)}],
        })
        first = scheduler.run_now("full")
        self.assertIsNotNone(first)
        assert first is not None
        scheduler.preserve(first.artifact_id, reason="sole known-good copy", actor="admin")
        self.now += timedelta(hours=1)
        scheduler.run_now("full")
        self.assertEqual(len(scheduler.catalog.list(location="nas")), 2)

        scheduler.policy = replace(
            scheduler.policy,
            full=scheduler.policy.full.model_copy(update={"remote_keep": 1}),
        )
        alias.unlink()
        alias.symlink_to(self._paths.local_dir, target_is_directory=True)
        scheduler._open_target = open_target
        result = scheduler._groom_locked()

        self.assertFalse(result.complete)
        self.assertIn("must not overlap the local archive root", result.error)
        self.assertTrue((self._paths.local_dir / first.name).is_file())
        preserved = scheduler.catalog.get(first.artifact_id)
        self.assertIsNotNone(preserved)
        assert preserved is not None
        self.assertTrue(preserved.preserved)
        self.assertEqual(len(scheduler.catalog.list(location="nas")), 2)


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

    def test_library_carries_the_last_grooming_outcome(self) -> None:
        from history_service.backup_scheduler.api import build_app

        scheduler = self.make({"full": {"enabled": True}, "targets": [TARGET]})
        app = build_app(scheduler)
        self.assertIsNone(json.loads(asgi_call(app, "GET", "/internal/backups")[1])["grooming"])
        scheduler._record_grooming(
            ok=False, deleted=2, detail="ConnectionRefusedError: refused", failed_locations={"nas": "refused"}
        )
        status, body = asgi_call(app, "GET", "/internal/backups")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["grooming"], {
            "at": "2026-09-24T12:00:00+00:00", "ok": False, "deleted": 2,
            "detail": "ConnectionRefusedError: refused", "failed_locations": {"nas": "refused"}})

        # A location that opened but then failed a deletion is listed too, so
        # the UI does not hide the error that stopped the run behind "nas".
        from types import SimpleNamespace

        from history_service.backup_archive.lifecycle import ApplyResult

        stopped = SimpleNamespace(record=SimpleNamespace(location="local"))
        manager = SimpleNamespace(
            plan=lambda now: SimpleNamespace(items=(stopped,)),
            apply=lambda plan, resolver, actor, now: ApplyResult(
                deleted=(), already_missing=(), failed=stopped, error="PermissionError: denied",
                failed_locations={"nas": "refused"},
            ),
        )
        with patch.object(scheduler, "_manager", return_value=manager):
            scheduler._groom_locked()
        grooming = json.loads(asgi_call(app, "GET", "/internal/backups")[1])["grooming"]
        self.assertEqual(grooming["detail"], "PermissionError: denied")
        self.assertEqual(grooming["failed_locations"], {"nas": "refused", "local": "PermissionError: denied"})

    def test_manual_lifecycle_apply_records_the_latest_grooming_outcome(self) -> None:
        from types import SimpleNamespace

        from history_service.backup_archive.lifecycle import ApplyResult
        from history_service.backup_scheduler.api import build_app

        scheduler = self.make({"full": {"enabled": True}, "targets": [TARGET]})
        app = build_app(scheduler)
        scheduler._record_grooming(ok=True, deleted=4, detail=None, failed_locations={})
        failed: Any = SimpleNamespace(record=SimpleNamespace(location="local"))
        result = ApplyResult(
            deleted=(),
            already_missing=(),
            failed=failed,
            error="PermissionError: denied",
            failed_locations={"nas": "ConnectionError: unavailable"},
        )
        manager = SimpleNamespace(
            plan=lambda now: SimpleNamespace(items=(failed,), guarded=()),
            apply=lambda plan, resolver, actor, now: result,
        )
        record_grooming_result = scheduler._record_grooming_result

        def assert_single_flight(applied_result: ApplyResult) -> None:
            self.assertTrue(scheduler._job_lock.locked())
            record_grooming_result(applied_result)

        with (
            patch.object(scheduler, "_manager", return_value=manager),
            patch.object(scheduler, "_record_grooming_result", side_effect=assert_single_flight),
        ):
            token, _, _ = scheduler.plan()
            self.assertIs(scheduler.apply(token), result)

        status, body = asgi_call(app, "GET", "/internal/backups")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["grooming"], {
            "at": "2026-09-24T12:00:00+00:00",
            "ok": False,
            "deleted": 0,
            "detail": "PermissionError: denied",
            "failed_locations": {
                "nas": "ConnectionError: unavailable",
                "local": "PermissionError: denied",
            },
        })


class AdminProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        from admin_service import main as admin_main

        self.admin_main = admin_main
        admin_route_support.get_backup_scheduler_client.cache_clear()
        self.addCleanup(admin_route_support.get_backup_scheduler_client.cache_clear)

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
            self.assertEqual(payload["detail"], "The backup scheduler service is not available.")
            self.assertEqual(self.call("POST", "/api/admin/backups/run", {"backup_class": "full"})[0], 503)

    def test_scheduler_exception_detail_is_not_returned(self) -> None:
        from admin_service.services.backup_scheduler_client import SchedulerUnavailableError

        class FailingClient:
            def request(self, *_args, **_kwargs):
                raise SchedulerUnavailableError("private socket path /run/secrets/backup-scheduler.sock")

        with patch.object(admin_routes, "get_backup_scheduler_client", return_value=FailingClient()):
            status, body = self.call("GET", "/api/admin/backups")
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body)["detail"],
                "The backup scheduler service is not available.",
            )
            status, body = self.call("POST", "/api/admin/backups/run", {"backup_class": "full"})
            self.assertEqual(status, 503)
            self.assertEqual(
                json.loads(body)["detail"],
                "The backup scheduler service is not available.",
            )
            self.assertNotIn(b"/run/secrets", body)

    def test_routes_forward_and_validate(self) -> None:
        from admin_service.services.backup_scheduler_client import SchedulerResponse

        calls: list[tuple[str, str, Any]] = []

        class FakeClient:
            def request(self, method, path, *, body=None, actor=None):
                calls.append((method, path, body))
                if path.endswith("/run"):
                    return SchedulerResponse(409, {"detail": "A backup or grooming run is already in progress."})
                return SchedulerResponse(200, {"ok": True})

        with patch.object(admin_routes, "get_backup_scheduler_client", return_value=FakeClient()):
            self.assertEqual(self.call("POST", "/api/admin/backups/abc123/verify")[0], 200)
            self.assertEqual(self.call("POST", "/api/admin/backups/abc123/preserve", {"reason": "x"})[0], 200)
            self.assertEqual(self.call("DELETE", "/api/admin/backups/abc123/preserve")[0], 200)
            self.assertEqual(self.call("POST", "/api/admin/backups/lifecycle/apply", {"plan_token": "t"})[0], 200)
            self.assertEqual(self.call("POST", "/api/admin/backups/run", {"backup_class": "full"})[0], 409)
            self.assertEqual(self.call("POST", "/api/admin/backups/run", {"backup_class": "bogus"})[0], 400)
            self.assertEqual(self.call("GET", "/api/admin/backups/..%2Fetc")[0], 404)
        self.assertIn(("POST", "/internal/backups/abc123/verify", None), calls)
        self.assertIn(("POST", "/internal/backups/abc123/preserve", {"reason": "x"}), calls)

    def test_download_to_rejects_short_body_and_wrong_digest(self) -> None:
        import http.client
        import io

        from admin_service.services import backup_scheduler_client as client_module

        archive = b"archive-bytes-" * 100
        digest = hashlib.sha256(archive).hexdigest()

        class FakeSocket:
            def __init__(self, raw: bytes) -> None:
                self.raw = raw

            def makefile(self, *_args, **_kwargs):
                return io.BytesIO(self.raw)

        class FakeConnection:
            def __init__(self, raw: bytes) -> None:
                self.raw = raw

            def request(self, *_args, **_kwargs) -> None:
                return None

            def getresponse(self):
                response = http.client.HTTPResponse(FakeSocket(self.raw))
                response.begin()
                return response

            def close(self) -> None:
                return None

        def raw_response(body: bytes, length: int, sha256: str) -> bytes:
            head = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Length: {length}\r\n"
                'Content-Disposition: attachment; filename="full.archive"\r\n'
                f"X-Backup-Sha256: {sha256}\r\n\r\n"
            )
            return head.encode("ascii") + body

        def download(raw: bytes):
            client = client_module.BackupSchedulerClient("/nonexistent/scheduler.sock")
            sink = io.BytesIO()
            with patch.object(client, "_connection", return_value=FakeConnection(raw)):
                return client.download_to("abc123", sink)

        short = download(raw_response(archive[:-10], len(archive), digest))
        self.assertNotEqual(short.status, 200)
        self.assertIn("did not match", short.payload["detail"])
        wrong = download(raw_response(archive, len(archive), "0" * 64))
        self.assertNotEqual(wrong.status, 200)
        self.assertIn("did not match", wrong.payload["detail"])
        good = download(raw_response(archive, len(archive), digest))
        self.assertEqual(good.status, 200)
        self.assertEqual(good.payload, {"filename": "full.archive", "sha256": digest})

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

    def test_view_lists_the_allowed_archive_formats_for_the_editor_choice(self) -> None:
        view = self.view()
        self.assertEqual(view["archive_formats"], ["7z", "tar.zst"])
        self.assertIn(view["classes"]["full"]["values"]["archive_format"], view["archive_formats"])

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

    def _rewrite_target(self, **changes: Any) -> None:
        document = yaml.safe_load(self.config.read_text())
        document["backups"]["targets"][0].update(changes)
        self.config.write_text(yaml.safe_dump(document, sort_keys=False))

    def test_quoted_port_saved_back_as_a_number_keeps_secret_files(self) -> None:
        self._rewrite_target(port="2222")
        view = self.view()
        values = dict(view["targets"][0]["values"], port=2222)  # the UI's number field
        self.save(self._target_payload(view, values))
        stored = yaml.safe_load(self.config.read_text())["backups"]["targets"][0]
        self.assertEqual(stored["port"], 2222)
        self.assertEqual(stored["private_key_file"], "/run/backup-secrets/archive_sftp_key")
        for field, value in (("hostname", "attacker.example.test"), ("port", 2223)):
            with self.subTest(field=field):
                view = self.view()
                before = self.config.read_bytes()
                values = dict(view["targets"][0]["values"], **{field: value})
                with self.assertRaisesRegex(self.editor.PolicyEditError, "choose its .* again"):
                    self.save(self._target_payload(view, values))
                self.assertEqual(self.config.read_bytes(), before)

    def test_username_whitespace_change_is_a_different_endpoint(self) -> None:
        self._rewrite_target(username="backup ")
        view = self.view()
        before = self.config.read_bytes()
        values = dict(view["targets"][0]["values"], username="backup")  # the UI trims its text fields
        with self.assertRaisesRegex(self.editor.PolicyEditError, "choose its .* again"):
            self.save(self._target_payload(view, values))
        self.assertEqual(self.config.read_bytes(), before)

    def test_numeric_bucket_saved_back_as_text_keeps_secret_files(self) -> None:
        document = yaml.safe_load(self.config.read_text())
        document["backups"]["targets"] = [{
            "target_id": "office-nas", "provider": "s3", "root": "jbod", "bucket": 2024, "region": "us-east-1",
            "access_key_id_file": "/run/backup-secrets/archive_s3_key_id",
            "secret_access_key_file": "/run/backup-secrets/archive_s3_secret",
        }]
        self.config.write_text(yaml.safe_dump(document, sort_keys=False))
        view = self.view()
        values = dict(view["targets"][0]["values"], bucket="2024")  # the UI's text field
        self.save(self._target_payload(view, values))
        stored = yaml.safe_load(self.config.read_text())["backups"]["targets"][0]
        self.assertEqual(stored["bucket"], "2024")
        self.assertEqual(stored["access_key_id_file"], "/run/backup-secrets/archive_s3_key_id")
        self.assertEqual(stored["secret_access_key_file"], "/run/backup-secrets/archive_s3_secret")

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

    def test_editor_refuses_filesystem_target_overlapping_local_archive(self) -> None:
        local = self.root / "archive"
        local.mkdir()
        view = self.view({"BACKUP_ARCHIVE_DIR": str(local)})
        before = self.config.read_bytes()
        target = {
            "values": {
                "target_id": "same-files",
                "label": "Unsafe local target",
                "provider": "filesystem",
                "root": str(local / "remote"),
                "enabled": True,
            },
            "secrets": {},
        }
        with self.assertRaisesRegex(self.editor.PolicyEditError, "must not overlap the local archive root"):
            self.save(
                {"revision": view["revision"], "targets": [target]},
                {"BACKUP_ARCHIVE_DIR": str(local)},
            )
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((local / "remote").exists())
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
        from app.route_support import build_health_payload, health_status_code

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
