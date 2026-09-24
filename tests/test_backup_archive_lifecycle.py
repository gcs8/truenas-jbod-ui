from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from history_service.backup_archive.catalog import (
    CATALOG_SCHEMA_VERSION,
    ArtifactCatalog,
    ArtifactRecord,
    CatalogError,
)
from history_service.backup_archive.lifecycle import (
    GroomingPlan,
    LifecycleManager,
    LocalBackupDirectory,
    RetentionRule,
    local_target_resolver,
    reconcile,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class FakeObject:
    name: str
    size: int
    modified: datetime | None = None


class FakeTarget:
    """In-memory stand-in for an ArchiveTarget (lane A's transport lands separately)."""

    provider = "filesystem"

    def __init__(self, names: dict[str, int] | None = None) -> None:
        self.objects: dict[str, int] = dict(names or {})
        self.deleted: list[str] = []
        self.fail_on: set[str] = set()

    def list(self, prefix: str = "") -> list[FakeObject]:
        return [FakeObject(name, size) for name, size in sorted(self.objects.items()) if name.startswith(prefix)]

    def delete(self, name: str) -> None:
        if name in self.fail_on:
            raise PermissionError("synthetic permission denied")
        if name not in self.objects:
            raise FileNotFoundError(name)
        del self.objects[name]
        self.deleted.append(name)


def make_record(
    index: int,
    *,
    backup_class: str = "config",
    location: str = "local",
    age: timedelta | None = None,
    verified: bool = True,
    **extra,
) -> ArtifactRecord:
    created = NOW - (age if age is not None else timedelta(days=index))
    name = f"{backup_class}/{location}-{index:04d}.tar.zst"
    return ArtifactRecord(
        artifact_id=f"{backup_class}-{location}-{index:04d}",
        backup_class=backup_class,
        location=location,
        name=name,
        created_at=created,
        size=100 + index,
        sha256=hashlib.sha256(name.encode()).hexdigest(),
        verified=verified,
        **extra,
    )


class CatalogCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "private"
        self.db_path = self.dir / "catalog.sqlite3"
        self.catalog = ArtifactCatalog(self.db_path)
        self.addCleanup(self.catalog.close)

    def add_series(self, count: int, **kwargs) -> list[ArtifactRecord]:
        return [self.catalog.add(make_record(i, **kwargs), now=NOW) for i in range(count)]


class ArtifactCatalogTests(CatalogCase):
    def test_database_is_private_wal_and_versioned(self) -> None:
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode) & 0o077, 0)
        self.assertEqual(self.catalog.schema_version(), CATALOG_SCHEMA_VERSION)
        connection = sqlite3.connect(self.db_path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchall(), [(1,)])

    def test_refuses_newer_schema_and_open_directory(self) -> None:
        self.catalog.close()
        connection = sqlite3.connect(self.db_path)
        connection.execute("UPDATE schema_version SET version = 99")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(CatalogError, "newer"):
            ArtifactCatalog(self.db_path)
        open_dir = Path(self._tmp.name) / "open"
        open_dir.mkdir(mode=0o777)
        os.chmod(open_dir, 0o777)
        with self.assertRaisesRegex(CatalogError, "private directory"):
            ArtifactCatalog(open_dir / "catalog.sqlite3")

    def test_refuses_symlinked_database(self) -> None:
        link = self.dir / "link.sqlite3"
        link.symlink_to(self.db_path)
        with self.assertRaisesRegex(CatalogError, "private regular file"):
            ArtifactCatalog(link)

    def test_add_get_list_roundtrip_survives_reopen(self) -> None:
        record = make_record(1, change_ids=("chg-1", "chg-2"))
        self.catalog.add(record, now=NOW)
        self.catalog.add(make_record(2, backup_class="full"), now=NOW)
        self.catalog.add(make_record(3, location="nas-1"), now=NOW)
        self.catalog.close()
        reopened = ArtifactCatalog(self.db_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(record.artifact_id), record)
        self.assertEqual([r.artifact_id for r in reopened.list(backup_class="config", location="local")], [record.artifact_id])
        self.assertEqual(len(reopened.list(location="nas-1")), 1)
        self.assertEqual(len(reopened.list(backup_class="full")), 1)
        self.assertEqual(reopened.locations(), ["local", "nas-1"])
        self.assertIsNone(reopened.get("missing"))

    def test_list_is_oldest_first(self) -> None:
        self.add_series(5)
        created = [r.created_at for r in self.catalog.list()]
        self.assertEqual(created, sorted(created))

    def test_rejects_invalid_records(self) -> None:
        base = make_record(1)
        bad = [
            replace(base, name="../escape"),
            replace(base, name="/abs/path"),
            replace(base, name="a\\b"),
            replace(base, name="a//b"),
            replace(base, backup_class="metrics"),
            replace(base, location="has space"),
            replace(base, sha256="XYZ"),
            replace(base, size=-1),
            replace(base, created_at=datetime(2026, 1, 1)),
            replace(base, artifact_id="../x"),
            replace(base, preserved=True),  # no actor
        ]
        for record in bad:
            with self.subTest(record=record), self.assertRaises(CatalogError):
                self.catalog.add(record, now=NOW)
        self.assertEqual(self.catalog.list(), [])

    def test_duplicate_id_or_location_name_refused(self) -> None:
        record = self.catalog.add(make_record(1), now=NOW)
        with self.assertRaises(CatalogError):
            self.catalog.add(record, now=NOW)
        with self.assertRaises(CatalogError):
            self.catalog.add(replace(record, artifact_id="other"), now=NOW)
        self.catalog.record_deletion(record.artifact_id, reason="test", actor="op")
        with self.assertRaisesRegex(CatalogError, "deleted"):
            self.catalog.add(record, now=NOW)

    def test_preserve_and_clear_are_audited(self) -> None:
        record = self.catalog.add(make_record(1), now=NOW)
        pinned = self.catalog.set_preserve(record.artifact_id, reason="before upgrade to v0.24", actor="admin", now=NOW)
        self.assertTrue(pinned.preserved)
        self.assertEqual((pinned.preserve_reason, pinned.preserved_by), ("before upgrade to v0.24", "admin"))
        with self.assertRaises(CatalogError):
            self.catalog.set_preserve(record.artifact_id, reason=" ", actor="admin")
        cleared = self.catalog.clear_preserve(record.artifact_id, actor="admin2", reason="upgrade fine", now=NOW)
        self.assertFalse(cleared.preserved)
        self.assertEqual((cleared.preserve_reason, cleared.preserved_by), ("", ""))
        events = self.catalog.preserve_history(record.artifact_id)
        self.assertEqual([(e.action, e.actor, e.reason) for e in events], [
            ("preserve", "admin", "before upgrade to v0.24"),
            ("unpreserve", "admin2", "upgrade fine"),
        ])
        with self.assertRaises(CatalogError):
            self.catalog.set_preserve("unknown", reason="x", actor="y")

    def test_mark_verified_checks_readback(self) -> None:
        record = self.catalog.add(make_record(1, verified=False), now=NOW)
        with self.assertRaisesRegex(CatalogError, "sha256"):
            self.catalog.mark_verified(record.artifact_id, sha256="0" * 64)
        with self.assertRaisesRegex(CatalogError, "size"):
            self.catalog.mark_verified(record.artifact_id, size=record.size + 1)
        self.assertFalse(self.catalog.get(record.artifact_id).verified)
        verified = self.catalog.mark_verified(record.artifact_id, sha256=record.sha256, size=record.size)
        self.assertTrue(verified.verified)

    def test_record_deletion_tombstones_with_time_reason_actor(self) -> None:
        record = self.catalog.add(make_record(1, change_ids=("c1",)), now=NOW)
        tombstone = self.catalog.record_deletion(record.artifact_id, reason="manual", actor="op", now=NOW)
        self.assertIsNone(self.catalog.get(record.artifact_id))
        self.assertEqual(tombstone.record, record)
        [stored] = self.catalog.tombstones(location="local")
        self.assertEqual((stored.record, stored.deleted_at, stored.reason, stored.actor), (record, NOW, "manual", "op"))
        with self.assertRaises(CatalogError):
            self.catalog.record_deletion(record.artifact_id, reason="again", actor="op")


class LifecyclePlanTests(CatalogCase):
    def manager(self, *rules: RetentionRule, **kwargs) -> LifecycleManager:
        return LifecycleManager(self.catalog, rules, **kwargs)

    def planned_ids(self, plan: GroomingPlan) -> list[str]:
        return [item.record.artifact_id for item in plan.items]

    def test_keep_30_local_and_90_remote(self) -> None:
        local = self.add_series(40, location="local")
        remote = self.add_series(100, location="nas-1")
        manager = self.manager(
            RetentionRule("config", "local", keep_count=30),
            RetentionRule("config", "nas-1", keep_count=90),
        )
        plan = manager.plan(NOW)
        # make_record(i) is i days old, so the oldest are the highest indices.
        expected = {r.artifact_id for r in local[30:]} | {r.artifact_id for r in remote[90:]}
        self.assertEqual(set(self.planned_ids(plan)), expected)
        self.assertEqual(len(plan.items), 20)
        created = [item.record.created_at for item in plan.items]
        self.assertEqual(created, sorted(created), "plan must be oldest first")
        self.assertTrue(all(item.kind == "retention" and "keep_count" in item.reason for item in plan.items))
        self.assertEqual(plan.guarded, ())

    def test_preserved_never_deleted_and_not_counted(self) -> None:
        records = self.add_series(10)
        oldest = records[-1]
        self.catalog.set_preserve(oldest.artifact_id, reason="known good", actor="admin")
        self.catalog.set_preserve(records[0].artifact_id, reason="newest pinned", actor="admin")
        plan = self.manager(RetentionRule("config", "local", keep_count=3, max_age=timedelta(days=2))).plan(NOW)
        ids = self.planned_ids(plan)
        self.assertNotIn(oldest.artifact_id, ids)
        self.assertNotIn(records[0].artifact_id, ids)
        # Pinned copies do not use keep_count slots: records 1..3 are kept, 4..8 go.
        # Record 3 is 3 days old, past max_age 2d, so it goes too; 1 and 2 are within age.
        self.assertEqual(ids, [r.artifact_id for r in reversed(records[3:9])])

    def test_unpreserve_returns_to_grooming(self) -> None:
        records = self.add_series(5)
        self.catalog.set_preserve(records[4].artifact_id, reason="hold", actor="admin")
        manager = self.manager(RetentionRule("config", "local", keep_count=2))
        self.assertNotIn(records[4].artifact_id, self.planned_ids(manager.plan(NOW)))
        self.catalog.clear_preserve(records[4].artifact_id, actor="admin")
        self.assertIn(records[4].artifact_id, self.planned_ids(manager.plan(NOW)))

    def test_age_and_count_together(self) -> None:
        records = self.add_series(10, location="nas-1")
        manager = self.manager(RetentionRule("config", "nas-1", keep_count=7, max_age=timedelta(days=4, hours=12)))
        plan = manager.plan(NOW)
        # Age removes 5..9 (> 4.5 days); count alone would remove 7..9.
        self.assertEqual(set(self.planned_ids(plan)), {r.artifact_id for r in records[5:]})
        by_id = {item.record.artifact_id: item.reason for item in plan.items}
        self.assertIn("keep_count", by_id[records[9].artifact_id])
        self.assertIn("max_age", by_id[records[9].artifact_id])
        self.assertNotIn("keep_count", by_id[records[5].artifact_id])
        count_only = self.manager(RetentionRule("config", "nas-1", keep_count=7)).plan(NOW)
        self.assertEqual(set(self.planned_ids(count_only)), {r.artifact_id for r in records[7:]})
        age_only = self.manager(RetentionRule("config", "nas-1", max_age=timedelta(days=30))).plan(NOW)
        self.assertEqual(age_only.items, ())

    def test_newest_verified_copy_is_guarded(self) -> None:
        records = self.add_series(3, age=None)
        plan = self.manager(RetentionRule("config", "local", max_age=timedelta(hours=1))).plan(NOW + timedelta(days=30))
        self.assertEqual(self.planned_ids(plan), [records[2].artifact_id, records[1].artifact_id])
        self.assertEqual([item.record.artifact_id for item in plan.guarded], [records[0].artifact_id])
        zero = self.manager(RetentionRule("config", "local", keep_count=0)).plan(NOW)
        self.assertNotIn(records[0].artifact_id, self.planned_ids(zero))

    def test_guard_skips_unverified_newer_copies(self) -> None:
        verified = self.catalog.add(make_record(5), now=NOW)
        newer_unverified = self.catalog.add(make_record(1, verified=False), now=NOW)
        plan = self.manager(RetentionRule("config", "local", keep_count=0), unverified_grace=timedelta(hours=1)).plan(NOW)
        self.assertEqual([i.record.artifact_id for i in plan.guarded], [verified.artifact_id])
        self.assertEqual([(i.record.artifact_id, i.kind) for i in plan.items], [(newer_unverified.artifact_id, "unverified")])

    def test_guard_is_per_class_and_location(self) -> None:
        self.catalog.add(make_record(1, location="local"), now=NOW)
        self.catalog.add(make_record(2, location="nas-1"), now=NOW)
        self.catalog.add(make_record(3, backup_class="full", location="nas-1"), now=NOW)
        manager = self.manager(
            RetentionRule("config", "local", keep_count=0),
            RetentionRule("config", "nas-1", keep_count=0),
            RetentionRule("full", "nas-1", keep_count=0),
        )
        plan = manager.plan(NOW)
        self.assertEqual(plan.items, ())
        self.assertEqual(len(plan.guarded), 3)

    def test_unverified_candidates_flagged_separately_after_grace(self) -> None:
        verified = self.add_series(3)
        young = self.catalog.add(make_record(10, verified=False, age=timedelta(hours=2)), now=NOW)
        old = self.catalog.add(make_record(11, verified=False, age=timedelta(days=2)), now=NOW)
        pinned = self.catalog.add(
            make_record(12, verified=False, age=timedelta(days=3), preserved=True, preserve_reason="debug", preserved_by="admin"),
            now=NOW,
        )
        plan = self.manager(RetentionRule("config", "local", keep_count=3), unverified_grace=timedelta(days=1)).plan(NOW)
        self.assertEqual([i.record.artifact_id for i in plan.unverified_items], [old.artifact_id])
        self.assertEqual(plan.retention_items, ())
        ids = self.planned_ids(plan)
        self.assertNotIn(young.artifact_id, ids)
        self.assertNotIn(pinned.artifact_id, ids)
        self.assertTrue(all(r.artifact_id not in ids for r in verified), "unverified copies do not use keep slots")

    def test_no_rule_means_no_grooming(self) -> None:
        self.add_series(50)
        self.add_series(50, backup_class="full")
        plan = self.manager(RetentionRule("full", "local", keep_count=45)).plan(NOW)
        self.assertEqual(len(plan.items), 5)
        self.assertTrue(all(item.record.backup_class == "full" for item in plan.items))

    def test_rule_validation(self) -> None:
        for kwargs in ({"keep_count": -1}, {"keep_count": 1.5}, {"max_age": timedelta(0)}, {"max_age": 5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RetentionRule("config", "local", **kwargs)
        with self.assertRaises(CatalogError):
            RetentionRule("metrics", "local")
        with self.assertRaises(ValueError):
            self.manager(RetentionRule("config", "local", keep_count=1), RetentionRule("config", "local", keep_count=2))

    def test_plan_is_dry_run(self) -> None:
        self.add_series(10)
        before = self.catalog.list()
        self.manager(RetentionRule("config", "local", keep_count=1)).plan(NOW)
        self.assertEqual(self.catalog.list(), before)
        self.assertEqual(self.catalog.tombstones(), [])


class LifecycleApplyTests(CatalogCase):
    def setUp(self) -> None:
        super().setUp()
        self.local_records = self.add_series(8, location="local")
        self.remote_records = self.add_series(8, location="nas-1")
        self.remote = FakeTarget({r.name: r.size for r in self.remote_records})
        self.backup_dir = Path(self._tmp.name) / "backups"
        for record in self.local_records:
            path = self.backup_dir / record.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * record.size)
        self.manager = LifecycleManager(
            self.catalog,
            (RetentionRule("config", "local", keep_count=3), RetentionRule("config", "nas-1", keep_count=5)),
        )
        self.resolver = local_target_resolver(self.backup_dir, remote={"nas-1": self.remote}.__getitem__)

    def test_apply_deletes_exactly_the_plan(self) -> None:
        plan = self.manager.plan(NOW)
        result = self.manager.apply(plan, self.resolver, actor="scheduler", now=lambda: NOW)
        self.assertTrue(result.complete)
        self.assertEqual(result.deleted, plan.items)
        planned = {item.record.artifact_id for item in plan.items}
        self.assertEqual({t.record.artifact_id for t in self.catalog.tombstones()}, planned)
        self.assertEqual(sorted(self.remote.deleted), sorted(r.name for r in self.remote_records[5:]))
        self.assertEqual(sorted(p.name for p in (self.backup_dir / "config").iterdir()),
                         sorted(Path(r.name).name for r in self.local_records[:3]))
        tomb = self.catalog.tombstones(location="nas-1")[0]
        self.assertEqual((tomb.actor, tomb.deleted_at), ("scheduler", NOW))
        self.assertIn("keep_count", tomb.reason)
        self.assertEqual(self.manager.plan(NOW).items, ())

    def test_apply_tolerates_already_missing(self) -> None:
        plan = self.manager.plan(NOW)
        gone = self.remote_records[7]
        del self.remote.objects[gone.name]
        (self.backup_dir / self.local_records[7].name).unlink()
        result = self.manager.apply(plan, self.resolver)
        self.assertTrue(result.complete)
        self.assertEqual({i.record.artifact_id for i in result.already_missing},
                         {gone.artifact_id, self.local_records[7].artifact_id})
        tombstones = {t.record.artifact_id: t.reason for t in self.catalog.tombstones()}
        self.assertIn("already missing", tombstones[gone.artifact_id])

    def test_apply_stops_on_first_unexpected_error(self) -> None:
        plan = self.manager.plan(NOW)
        failing = self.remote_records[6]
        self.remote.fail_on.add(failing.name)
        result = self.manager.apply(plan, self.resolver)
        self.assertFalse(result.complete)
        self.assertEqual(result.failed.record.artifact_id, failing.artifact_id)
        self.assertIn("PermissionError", result.error)
        index = [i.record.artifact_id for i in plan.items].index(failing.artifact_id)
        self.assertEqual(result.deleted, plan.items[:index])
        self.assertEqual(result.not_attempted, plan.items[index + 1:])
        self.assertIsNotNone(self.catalog.get(failing.artifact_id))
        for item in result.not_attempted:
            self.assertIsNotNone(self.catalog.get(item.record.artifact_id))

    def test_apply_refuses_item_pinned_after_planning(self) -> None:
        plan = self.manager.plan(NOW)
        first = plan.items[0].record
        self.catalog.set_preserve(first.artifact_id, reason="late pin", actor="admin")
        result = self.manager.apply(plan, self.resolver)
        self.assertEqual(result.failed.record.artifact_id, first.artifact_id)
        self.assertIn("changed", result.error)
        self.assertEqual(result.deleted, ())
        self.assertEqual(self.catalog.tombstones(), [])

    def test_apply_refuses_to_delete_newest_verified(self) -> None:
        self.catalog.add(make_record(1, location="solo"), now=NOW)
        record = self.catalog.add(make_record(2, location="solo"), now=NOW)
        manager = LifecycleManager(self.catalog, (RetentionRule("config", "solo", keep_count=1),))
        plan = manager.plan(NOW)
        self.assertEqual([i.record.artifact_id for i in plan.items], [record.artifact_id])
        self.catalog.record_deletion("config-solo-0001", reason="manual", actor="op")
        result = manager.apply(plan, lambda _location: FakeTarget({record.name: record.size}))
        self.assertIn("newest verified", result.error)
        self.assertIsNotNone(self.catalog.get(record.artifact_id))

    def test_apply_unknown_location_stops(self) -> None:
        self.catalog.add(make_record(20, location="gone-target"), now=NOW)
        self.catalog.add(make_record(21, location="gone-target"), now=NOW)
        manager = LifecycleManager(self.catalog, (RetentionRule("config", "gone-target", keep_count=1),))
        result = manager.apply(manager.plan(NOW), local_target_resolver(self.backup_dir))
        self.assertIn("LookupError", str(result.error))
        self.assertEqual(result.deleted, ())


class LocalBackupDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "backups"
        (self.root / "config").mkdir(parents=True)
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "victim.txt").write_text("keep")
        (self.root / "config" / "a.tar").write_bytes(b"abc")
        self.target = LocalBackupDirectory(self.root)

    def test_path_confinement(self) -> None:
        for name in ("../outside/victim.txt", "/etc/passwd", "config\\a.tar", "", "config/../../outside/victim.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.target.delete(name)
        (self.root / "escape").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.target.delete("escape/victim.txt")
        (self.root / "config" / "link.tar").symlink_to(self.outside / "victim.txt")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.target.delete("config/link.tar")
        self.assertEqual((self.outside / "victim.txt").read_text(), "keep")
        self.assertTrue((self.root / "config" / "link.tar").is_symlink())

    def test_delete_and_missing(self) -> None:
        self.target.delete("config/a.tar")
        self.assertFalse((self.root / "config" / "a.tar").exists())
        with self.assertRaises(FileNotFoundError):
            self.target.delete("config/a.tar")

    def test_list_skips_symlinks(self) -> None:
        (self.root / "escape").symlink_to(self.outside, target_is_directory=True)
        (self.root / "config" / "link.tar").symlink_to(self.outside / "victim.txt")
        self.assertEqual([(o.name, o.size) for o in self.target.list()], [("config/a.tar", 3)])
        self.assertEqual(self.target.list(prefix="full/"), [])


class ReconcileTests(CatalogCase):
    def test_reports_missing_and_uncatalogued_without_deleting(self) -> None:
        records = self.add_series(4, location="nas-1")
        target = FakeTarget({r.name: r.size for r in records[1:]})
        target.objects["config/look-alike-config.tar.zst"] = 5
        target.objects[records[3].name] = 1  # size drift
        report = reconcile(self.catalog, target, "nas-1")
        self.assertEqual([r.artifact_id for r in report.missing], [records[0].artifact_id])
        self.assertEqual([o.name for o in report.uncatalogued], ["config/look-alike-config.tar.zst"])
        self.assertEqual([r.artifact_id for r, _ in report.size_mismatch], [records[3].artifact_id])
        self.assertFalse(report.clean)
        self.assertEqual(target.deleted, [])
        self.assertIn("config/look-alike-config.tar.zst", target.objects)
        self.assertEqual(len(self.catalog.list(location="nas-1")), 4)

    def test_uncatalogued_objects_never_groomed(self) -> None:
        records = self.add_series(3, location="nas-1")
        target = FakeTarget({r.name: r.size for r in records})
        for i in range(10):
            target.objects[f"config/stranger-{i}.tar.zst"] = 1
        manager = LifecycleManager(self.catalog, (RetentionRule("config", "nas-1", keep_count=1),))
        result = manager.apply(manager.plan(NOW), lambda _loc: target)
        self.assertTrue(result.complete)
        self.assertEqual(sorted(target.deleted), sorted(r.name for r in records[1:]))
        self.assertEqual(len([n for n in target.objects if "stranger" in n]), 10)

    def test_tombstoned_but_present_and_clean(self) -> None:
        records = self.add_series(2, location="nas-1")
        target = FakeTarget({r.name: r.size for r in records})
        self.assertTrue(reconcile(self.catalog, target, "nas-1").clean)
        self.catalog.record_deletion(records[1].artifact_id, reason="manual", actor="op")
        report = reconcile(self.catalog, target, "nas-1")
        self.assertEqual([o.name for o in report.tombstoned_but_present], [records[1].name])
        self.assertEqual(report.uncatalogued, ())


if __name__ == "__main__":
    unittest.main()
