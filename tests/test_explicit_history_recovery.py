"""Synthetic offline admission, journaled apply and selected paused replay."""

from contextlib import closing
import hashlib
import itertools
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from history_service import recovery_state
from history_service.migration_lock import history_write_lock
from tests.history_schema_fixtures import RELEASE_SCHEMAS


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            digest(path) if stat.S_ISREG(info.st_mode) else None,
        )
    return result


def pause_fixture(parent, locations=("retained",) * 4):
    database = parent / "history.db"
    for role, suffix in recovery_state.ARTIFACTS.items():
        Path(str(database) + suffix).write_bytes(("synthetic-corrupt-" + role).encode())
    with history_write_lock(database, blocking=False):
        recovery_state.pause_and_quarantine(database)
    root = recovery_state.recovery_path(database)
    for (role, suffix), location in zip(recovery_state.ARTIFACTS.items(), locations):
        retained = root / role
        original = Path(str(database) + suffix)
        if location == "original":
            retained.rename(original)
        elif location == "both":
            os.link(retained, original)
    return database, json.loads((root / "intent.json").read_bytes())["id"], digest(root / "intent.json")


def bundle_fixture(parent, kind="supported", change=None, extra=None):
    from history_service.system_backup import BUNDLE_FORMAT, HISTORY_DB_KEY, BACKUP_GROUP_METADATA

    candidate = parent / "selected.sqlite3"
    with closing(sqlite3.connect(candidate)) as connection:
        if kind not in ("empty", "corrupt"):
            connection.executescript(RELEASE_SCHEMAS[-1][2])
        if kind == "unsupported":
            connection.execute("CREATE TABLE alien(value TEXT)")
        connection.execute("PRAGMA user_version=" + ("0" if kind == "marker0" else "1"))
        connection.commit()
    if kind == "corrupt":
        candidate.write_bytes(b"synthetic-not-sqlite")
    payload = candidate.read_bytes()
    member = {
        "key": HISTORY_DB_KEY,
        "group_key": HISTORY_DB_KEY,
        "archive_path": "history/history.sqlite3",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    manifest = {
        "schema_version": 1,
        "format": BUNDLE_FORMAT,
        "packaging": "zip",
        "groups": [
            {
                "key": HISTORY_DB_KEY,
                "selected": True,
                "present": True,
                "restore_mode": BACKUP_GROUP_METADATA[HISTORY_DB_KEY]["restore_mode"],
                "archive_root": "history/history.sqlite3",
            }
        ],
        "files": [member],
    }
    if change:
        change(manifest)
    bundle = parent / "selected.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("history/history.sqlite3", payload)
        if extra:
            archive.writestr(extra, b"synthetic-extra")
    return bundle



class JournaledApplyTests(unittest.TestCase):
    """Bounded apply retains pause; replay requires an authenticated selection."""

    def fixture(self, temporary, locations=("retained",) * 4):
        base = Path(temporary)
        target = base / "target"
        target.mkdir()
        scratch = base / "scratch"
        scratch.mkdir(mode=0o700)
        source = base / "source"
        source.mkdir()
        database, rid, selected = pause_fixture(target, locations)
        bundle = bundle_fixture(source)
        payload_path = source / "selected.sqlite3"
        with closing(sqlite3.connect(payload_path)) as connection:
            connection.execute(
                "INSERT INTO metric_samples(observed_at,system_id,enclosure_key,slot,slot_label,metric_name,value_integer) "
                "VALUES ('2026-01-01T00:00:00Z','synthetic-selected','synthetic-enclosure',1,'slot-1','temperature',37)"
            )
            connection.commit()
        with zipfile.ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        payload = payload_path.read_bytes()
        manifest["files"][0].update(size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("history/history.sqlite3", payload)
        args = ["restore-bundle", "--database", str(database), "--recovery-id", rid,
                "--intent-sha256", selected, "--bundle", str(bundle), "--bundle-sha256", digest(bundle),
                "--history-only", "--apply", "--offline", "--accept-backup-history",
                "--resolved-topology", "unsegmented", "--scratch-parent", str(scratch),
                "--publication-uid", str(os.geteuid()), "--publication-gid", str(os.getegid()),
                "--publication-mode", "0600"]
        return database, args, payload

    def test_actual_apply_restores_selected_bytes_but_keeps_pause(self):
        from history_service import explicit_recovery as recovery
        for location in ("original", "retained", "both"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temporary:
                database, args, payload = self.fixture(temporary, (location,) * 4)
                root = recovery_state.recovery_path(database)
                intent = (root / "intent.json").read_bytes()
                recorded = json.loads(intent)
                try:
                    code = recovery.main(args)
                except SystemExit as exc:
                    code = exc.code
                self.assertEqual(code, 5, "apply must actually publish, but never advertise completed recovery")
                self.assertEqual(database.read_bytes(), payload)
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    self.assertEqual(connection.execute("SELECT system_id,value_integer FROM metric_samples").fetchall(),
                                     [("synthetic-selected", 37)])
                self.assertEqual((root / "intent.json").read_bytes(), intent)
                for role, facts in recorded["artifacts"].items():
                    self.assertEqual(digest(root / role), facts["sha256"])
                    self.assertEqual((root / role).stat().st_ino, facts["ino"])
                    self.assertEqual((root / role).stat().st_nlink, 1)
                self.assertTrue((root / "recovery-operation.json").is_file())
                self.assertTrue((root / "completed.json").is_file())
                self.assertFalse((root / "candidate.sqlite3").exists())
                before = inventory(database.parent)
                resume = ["resume", "--database", str(database), "--recovery-id", recorded["id"],
                          "--intent-sha256", digest(root / "intent.json"), "--offline", "--apply"]
                self.assertEqual(recovery.main(resume), 4)
                self.assertEqual(recovery.main(args), 4)
                self.assertEqual(inventory(database.parent), before)
                self.assertEqual(recovery_state.inspect_recovery(database), "required")


    def test_process_crashes_preserve_pause_and_refuse_unsafe_replay(self):
        script = r'''
import json, os, sys
from history_service import recovery_apply, explicit_recovery
trace = []
cut = int(sys.argv[1])
def boundary(name):
    trace.append(name)
    if len(trace) == cut:
        os._exit(71)
recovery_apply._boundary = boundary
code = explicit_recovery.main(json.loads(sys.argv[2]))
assert code == 5
print('TRACE:' + json.dumps(trace))
'''
        restart = r'''
import sqlite3, sys
from unittest.mock import patch
from history_service.store import HistoryStore
with patch.object(sqlite3, 'connect', side_effect=AssertionError('unexpected SQLite open')):
    store = HistoryStore(sys.argv[1])
    assert store.recovery_status()['recovery_required']
'''
        from history_service import explicit_recovery as recovery
        crash_cases = 0
        replay_outcomes = []
        traces = {}
        for location in ("original", "retained", "both"):
            with tempfile.TemporaryDirectory() as temporary:
                _, args, _ = self.fixture(temporary, (location,) * 4)
                result = subprocess.run([sys.executable, "-B", "-c", script, "0", json.dumps(args)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                trace = json.loads(next(line[6:] for line in result.stdout.splitlines() if line.startswith("TRACE:")))
                self.assertIn("before:candidate:publish-link", trace)
                self.assertIn("after:completed.json:directory-fsync", trace)
                self.assertEqual(sum(n.startswith("before:") for n in trace),
                                 sum(n.startswith("after:") for n in trace))
                traces[location] = trace
            for cut, event in enumerate(trace, 1):
                with self.subTest(location=location, cut=cut, event=event), tempfile.TemporaryDirectory() as temporary:
                    database, args, payload = self.fixture(temporary, (location,) * 4)
                    root = recovery_state.recovery_path(database)
                    intent = (root / "intent.json").read_bytes()
                    record = json.loads(intent)
                    result = subprocess.run([sys.executable, "-B", "-c", script, str(cut), json.dumps(args)],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 71, result.stderr)
                    crash_cases += 1
                    self.assertEqual((root / "intent.json").read_bytes(), intent)
                    for role, facts in record["artifacts"].items():
                        names = [root / role, Path(str(database) + recovery_state.ARTIFACTS[role])]
                        authentic = [p for p in names if p.exists() and p.stat().st_ino == facts["ino"]]
                        self.assertTrue(authentic)
                        for path in authentic:
                            self.assertEqual(digest(path), facts["sha256"])
                            self.assertEqual(path.stat().st_nlink, len(authentic))
                    if database.exists() and database.stat().st_ino != record["artifacts"]["main"]["ino"]:
                        self.assertEqual(database.read_bytes(), payload)
                        operation = json.loads((root / "recovery-operation.json").read_bytes())
                        self.assertEqual(database.stat().st_ino, operation["candidate"]["ino"])
                    before = inventory(database.parent)
                    for _ in range(2):
                        fresh = subprocess.run([sys.executable, "-B", "-c", restart, str(database)],
                                               capture_output=True, text=True, timeout=20)
                        self.assertEqual(fresh.returncode, 0, fresh.stderr)
                    resume = ["resume", "--database", str(database), "--recovery-id", record["id"],
                              "--intent-sha256", digest(root / "intent.json"), "--offline", "--apply"]
                    self.assertEqual(recovery.main(resume), 4)
                    self.assertEqual(inventory(database.parent), before)
                    staged = any((root / name).exists() for name in
                                 ("candidate.sqlite3", "recovery-operation.json", "completed.json"))
                    if staged:
                        self.assertEqual(recovery.main(args), 4)
                        self.assertEqual(inventory(database.parent), before)
                    expected = JournaledResumeTests.expected_replay(root)
                    selected_resume = JournaledResumeTests.resume_args(database)
                    replay = subprocess.run([sys.executable, "-B", "scripts/recover_paused_history.py", *selected_resume],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(replay.returncode, expected, replay.stdout + replay.stderr)
                    replay_outcomes.append(dict(location=location, cut=cut, event=event, exit=expected))
                    if expected == 4:
                        self.assertEqual(inventory(database.parent), before)
                    else:
                        self.assertEqual(json.loads(replay.stdout)["state"], "applied-paused")
                        self.assertEqual(database.read_bytes(), payload)
                        self.assertFalse((root / "candidate.sqlite3").exists())
                        for role, facts in record["artifacts"].items():
                            self.assertEqual((root / role).stat().st_ino, facts["ino"])
                            self.assertEqual(digest(root / role), facts["sha256"])
                            self.assertEqual((root / role).stat().st_nlink, 1)
                    after_replay = inventory(database.parent)
                    for _ in range(2):
                        fresh = subprocess.run([sys.executable, "-B", "-c", restart, str(database)],
                                               capture_output=True, text=True, timeout=20)
                        self.assertEqual(fresh.returncode, 0, fresh.stderr)
                    self.assertEqual(inventory(database.parent), after_replay)
                    if not staged:
                        self.assertEqual(recovery.main(args), 5)
                        self.assertEqual(database.read_bytes(), payload)
        print("APPLY_FAULT_MATRIX=" + json.dumps(dict(crash_cases=crash_cases, fresh_starts=crash_cases * 4,
                                                     traces=traces, replay_outcomes=replay_outcomes), sort_keys=True))

    def test_short_target_writes_preserve_partial_files_and_pause(self):
        from history_service import explicit_recovery as recovery
        from history_service import recovery_apply
        for name in ("candidate.sqlite3", "recovery-operation.json", "completed.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                database, args, payload = self.fixture(temporary, ("original",) * 4)
                root = recovery_state.recovery_path(database)
                native = os.write
                def short(fd, value):
                    target = root / name
                    if target.exists() and os.fstat(fd).st_ino == target.stat().st_ino:
                        return native(fd, value[:1])
                    return native(fd, value)
                with patch.object(recovery_apply.os, "write", side_effect=short):
                    self.assertEqual(recovery.main(args), 5)
                self.assertEqual((root / name).stat().st_size, 1)
                self.assertTrue(root.is_dir())
                record = json.loads((root / "intent.json").read_bytes())
                for role, facts in record["artifacts"].items():
                    original = Path(str(database) + recovery_state.ARTIFACTS[role])
                    retained = root / role
                    evidence = retained if retained.exists() else original
                    self.assertEqual(digest(evidence), facts["sha256"])
                    self.assertEqual(evidence.stat().st_ino, facts["ino"])
                if name == "completed.json":
                    self.assertEqual(database.read_bytes(), payload)
                before = inventory(database.parent)
                self.assertEqual(recovery.main(args), 4)
                self.assertEqual(inventory(database.parent), before)

    def test_continuous_exclusive_lock_through_apply_boundaries(self):
        from history_service import explicit_recovery as recovery
        from history_service import recovery_apply
        probe = r'''
import sqlite3, sys
from history_service.migration_lock import _history_lifecycle_lock
from pathlib import Path
try:
    with _history_lifecycle_lock(Path(sys.argv[1]), blocking=False):
        sys.exit(9)
except sqlite3.OperationalError:
    sys.exit(0)
'''
        with tempfile.TemporaryDirectory() as temporary:
            database, args, _ = self.fixture(temporary, ("both",) * 4)
            alias = Path(temporary) / "alias"
            alias.symlink_to(database.parent, target_is_directory=True)
            observed = []
            def boundary(event):
                if event.startswith("before:"):
                    for target in (database, alias / database.name):
                        child = subprocess.run([sys.executable, "-B", "-c", probe, str(target)],
                                               capture_output=True, text=True, timeout=5)
                        self.assertEqual(child.returncode, 0, child.stderr)
                        observed.append(event)
            with patch.object(recovery_apply, "_boundary", side_effect=boundary):
                self.assertEqual(recovery.main(args), 5)
            self.assertIn("before:final:evidence-readback", observed)
            self.assertGreater(len(observed), 20)
            child = subprocess.run([sys.executable, "-B", "-c", probe, str(database)],
                                   capture_output=True, text=True, timeout=5)
            self.assertEqual(child.returncode, 9, child.stderr)
            print("APPLY_LOCK_PROBES=" + str(len(observed)))

    def test_produced_evidence_divergence_never_clears_pause(self):
        from history_service import explicit_recovery as recovery
        from history_service import recovery_apply
        for name, mutation in itertools.product(("intent.json", "candidate.sqlite3", "recovery-operation.json"),
                                                ("missing", "replacement", "changed")):
            with self.subTest(name=name, mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                database, args, _ = self.fixture(temporary, ("original",) * 4)
                root = recovery_state.recovery_path(database)
                original = {role: (Path(str(database) + suffix).read_bytes(),
                                   Path(str(database) + suffix).stat().st_ino)
                            for role, suffix in recovery_state.ARTIFACTS.items()}
                fired = []
                def boundary(event):
                    if event != "after:recovery-operation.json:readback" or fired:
                        return
                    fired.append(event)
                    path = root / name
                    if mutation == "changed":
                        path.write_bytes(path.read_bytes() + b"synthetic-change")
                    else:
                        parked = Path(temporary) / "parked"
                        path.rename(parked)
                        if mutation == "replacement":
                            path.write_bytes(parked.read_bytes())
                            path.chmod(0o600)
                with patch.object(recovery_apply, "_boundary", side_effect=boundary):
                    self.assertEqual(recovery.main(args), 5)
                self.assertEqual(len(fired), 1)
                self.assertFalse((root / "completed.json").exists())
                self.assertTrue(root.is_dir())
                for role, suffix in recovery_state.ARTIFACTS.items():
                    path = Path(str(database) + suffix)
                    self.assertEqual((path.read_bytes(), path.stat().st_ino), original[role])

    def test_admission_refusal_is_before_any_target_write(self):
        from history_service import explicit_recovery as recovery
        from history_service import recovery_apply
        for case in ("consent", "digest", "mode", "uid", "unknown", "divergence", "stale"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                database, args, _ = self.fixture(temporary)
                if case == "consent":
                    args.remove("--offline")
                elif case == "digest":
                    args[args.index("--bundle-sha256") + 1] = "0" * 64
                elif case == "mode":
                    args.remove("--publication-mode")
                    args.remove("0600")
                elif case == "uid":
                    args[args.index("--publication-uid") + 1] = str(os.geteuid() + 1)
                elif case == "unknown":
                    (database.parent / "unknown").write_bytes(b"synthetic")
                elif case == "divergence":
                    database.write_bytes(b"synthetic-replacement")
                elif case == "stale":
                    args[args.index("--intent-sha256") + 1] = "0" * 64
                before = inventory(database.parent)
                with patch.object(recovery_apply, "_boundary", side_effect=AssertionError("unexpected mutation")):
                    self.assertEqual(recovery.main(args), 4)
                self.assertEqual(inventory(database.parent), before)


class JournaledResumeTests(unittest.TestCase):
    fixture = JournaledApplyTests.fixture

    @staticmethod
    def expected_replay(root):
        # Fault campaign oracle, not production parsing: actual apply only writes
        # a complete JSON object or an empty/partial file at these process cuts.
        try:
            json.loads((root / "recovery-operation.json").read_bytes())
            if (root / "completed.json").exists():
                json.loads((root / "completed.json").read_bytes())
        except (OSError, ValueError):
            return 4
        return 5

    @staticmethod
    def resume_args(database):
        root = recovery_state.recovery_path(database)
        record = json.loads((root / "intent.json").read_bytes())
        args = ["resume", "--database", str(database), "--recovery-id", record["id"],
                "--intent-sha256", digest(root / "intent.json"), "--offline", "--apply",
                "--resolved-topology", "unsegmented"]
        for name, flag in (("recovery-operation.json", "operation"), ("completed.json", "receipt")):
            path = root / name
            if path.exists():
                info = path.stat()
                args += ["--" + flag + "-sha256", digest(path),
                         "--" + flag + "-identity", str(info.st_dev) + ":" + str(info.st_ino)]
        return args

    def prepare(self, database, args, event="after:recovery-operation.json:readback"):
        from history_service import explicit_recovery as api, recovery_apply
        def boundary(name):
            if name == event:
                raise OSError("synthetic interruption")
        with patch.object(recovery_apply, "_boundary", side_effect=boundary):
            self.assertEqual(api.main(args), 5)

    def test_authenticated_resume_cli_replays_prepared_to_applied_paused(self):
        from history_service import explicit_recovery as api
        for location in ("original", "retained", "both"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temporary:
                database, args, payload = self.fixture(temporary, (location,) * 4)
                self.prepare(database, args)
                root = recovery_state.recovery_path(database)
                original = {p.name: (p.read_bytes(), p.stat().st_ino)
                            for p in root.iterdir() if p.is_file() and p.name != "candidate.sqlite3"}
                resume = self.resume_args(database)
                child = subprocess.run([sys.executable, "-B", "scripts/recover_paused_history.py", *resume],
                                       capture_output=True, text=True, timeout=30)
                self.assertEqual(child.returncode, 5, child.stdout + child.stderr)
                result = json.loads(child.stdout)
                self.assertEqual(result["state"], "applied-paused")
                self.assertFalse(result["recovery_completed"])
                self.assertEqual(database.read_bytes(), payload)
                for name, facts in original.items():
                    self.assertEqual(((root / name).read_bytes(), (root / name).stat().st_ino), facts)
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    self.assertEqual(connection.execute("SELECT system_id,value_integer FROM metric_samples").fetchall(),
                                     [("synthetic-selected", 37)])
                before = inventory(database.parent)
                self.assertEqual(api.main(self.resume_args(database)), 5)
                self.assertEqual(inventory(database.parent), before)
                self.assertEqual(recovery_state.inspect_recovery(database), "required")


    def test_apply_reports_anchors_for_later_selected_resume(self):
        import io
        from contextlib import redirect_stdout
        from history_service import explicit_recovery as api
        with tempfile.TemporaryDirectory() as temporary:
            database, args, _ = self.fixture(temporary)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(api.main(args), 5)
            result = json.loads(output.getvalue())
            self.assertTrue(result["resume_available"])
            root = recovery_state.recovery_path(database)
            for key, name in (("operation", "recovery-operation.json"), ("receipt", "completed.json")):
                info = (root / name).stat()
                self.assertEqual(result[key]["ino"], info.st_ino)
                self.assertEqual(result[key]["dev"], info.st_dev)
                self.assertEqual(result[key]["sha256"], digest(root / name))

    def test_resume_process_cuts_replay_or_preserve_partial_receipt(self):
        script = r'''
import json, os, sys
from history_service import recovery_apply, explicit_recovery
trace = []
def boundary(event):
    trace.append(event)
    if len(trace) == int(sys.argv[1]):
        os._exit(71)
recovery_apply._boundary = boundary
code = explicit_recovery.main(json.loads(sys.argv[2]))
assert code == 5
print('TRACE:' + json.dumps(trace))
'''
        restart = r'''
import sqlite3, sys
from unittest.mock import patch
from history_service.store import HistoryStore
with patch.object(sqlite3, 'connect', side_effect=AssertionError('unexpected SQLite open')):
    store = HistoryStore(sys.argv[1])
    assert store.recovery_status()['recovery_required']
'''
        starts = (("prepared-original", "after:recovery-operation.json:readback"),
                  ("active-two", "after:candidate:publish-link"),
                  ("receipt", "after:completed.json:readback"))
        outcomes = []
        traces = {}
        for label, event in starts:
            with tempfile.TemporaryDirectory() as temporary:
                database, args, _ = self.fixture(temporary, ("original",) * 4)
                self.prepare(database, args, event)
                result = subprocess.run([sys.executable, "-B", "-c", script, "0",
                                         json.dumps(self.resume_args(database))], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                trace = json.loads(next(line[6:] for line in result.stdout.splitlines() if line.startswith("TRACE:")))
                traces[label] = trace
            for cut, boundary in enumerate(trace, 1):
                with self.subTest(start=label, cut=cut, boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                    database, args, payload = self.fixture(temporary, ("original",) * 4)
                    self.prepare(database, args, event)
                    root = recovery_state.recovery_path(database)
                    intent = (root / "intent.json").read_bytes()
                    operation = (root / "recovery-operation.json").read_bytes()
                    record = json.loads(intent)
                    selected = self.resume_args(database)
                    result = subprocess.run([sys.executable, "-B", "-c", script, str(cut), json.dumps(selected)],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 71, result.stdout + result.stderr)
                    before = inventory(database.parent)
                    for _ in range(2):
                        child = subprocess.run([sys.executable, "-B", "-c", restart, str(database)],
                                               capture_output=True, text=True, timeout=20)
                        self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(inventory(database.parent), before)
                    expected = self.expected_replay(root)
                    replay = subprocess.run([sys.executable, "-B", "scripts/recover_paused_history.py",
                                             *self.resume_args(database)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(replay.returncode, expected, replay.stdout + replay.stderr)
                    if expected == 4:
                        self.assertEqual(inventory(database.parent), before)
                    else:
                        self.assertEqual(json.loads(replay.stdout)["state"], "applied-paused")
                        self.assertEqual(database.read_bytes(), payload)
                    self.assertEqual((root / "intent.json").read_bytes(), intent)
                    self.assertEqual((root / "recovery-operation.json").read_bytes(), operation)
                    for role, facts in record["artifacts"].items():
                        names = [root / role, Path(str(database) + recovery_state.ARTIFACTS[role])]
                        authentic = [p for p in names if p.exists() and p.stat().st_ino == facts["ino"]]
                        self.assertTrue(authentic)
                        for path in authentic:
                            self.assertEqual(digest(path), facts["sha256"])
                            self.assertEqual(path.stat().st_nlink, len(authentic))
                    after = inventory(database.parent)
                    for _ in range(2):
                        child = subprocess.run([sys.executable, "-B", "-c", restart, str(database)],
                                               capture_output=True, text=True, timeout=20)
                        self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(inventory(database.parent), after)
                    outcomes.append(dict(start=label, cut=cut, event=boundary, exit=expected))
        print("RESUME_FAULT_MATRIX=" + json.dumps(dict(traces=traces, outcomes=outcomes,
                                                       crash_cases=len(outcomes), fresh_starts=len(outcomes) * 4), sort_keys=True))

    def test_resume_continuous_lock_and_existing_store_latch(self):
        from history_service import explicit_recovery as api, recovery_apply
        from history_service.store import HistoryStore
        probe = r'''
import sqlite3, sys
from history_service.migration_lock import _history_lifecycle_lock
from pathlib import Path
try:
    with _history_lifecycle_lock(Path(sys.argv[1]), blocking=False): sys.exit(9)
except sqlite3.OperationalError: sys.exit(0)
'''
        with tempfile.TemporaryDirectory() as temporary:
            database, args, _ = self.fixture(temporary, ("both",) * 4)
            self.prepare(database, args)
            with patch.object(sqlite3, "connect", side_effect=AssertionError("paused SQLite open")):
                store = HistoryStore(str(database))
            alias = Path(temporary) / "alias"
            alias.symlink_to(database.parent, target_is_directory=True)
            events = []
            def boundary(event):
                if event.startswith("before:"):
                    for target in (database, alias / database.name):
                        result = subprocess.run([sys.executable, "-B", "-c", probe, str(target)],
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        events.append(event)
            with patch.object(recovery_apply, "_boundary", side_effect=boundary):
                self.assertEqual(api.main(self.resume_args(database)), 5)
            with patch.object(sqlite3, "connect", side_effect=AssertionError("latched SQLite open")):
                self.assertTrue(store.recovery_status()["recovery_required"])
            self.assertIn("before:final:evidence-readback", events)
            result = subprocess.run([sys.executable, "-B", "-c", probe, str(database)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 9, result.stderr)
            print("RESUME_LOCK_PROBES=" + str(len(events)))

    def test_resume_strict_operation_decode_refuses_before_sqlite_or_mutation(self):
        from history_service import explicit_recovery as api
        cases = ("unknown", "duplicate", "oversize", "partial", "version-bool", "candidate-bool",
                 "source-bool", "identity-bool", "artifact-bool", "observed-bool", "path", "source-digest",
                 "candidate-owner", "source-unknown", "observed-unknown", "observed-links", "missing-role")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                database, args, _ = self.fixture(temporary)
                self.prepare(database, args)
                path = recovery_state.recovery_path(database) / "recovery-operation.json"
                value = json.loads(path.read_bytes())
                if case == "unknown":
                    value["extra"] = 0
                if case == "version-bool":
                    value["version"] = True
                if case == "candidate-bool":
                    value["candidate"]["uid"] = True
                if case == "source-bool":
                    value["source"]["backfill_marker"] = True
                if case == "identity-bool":
                    value["intent_identity"][8] = True
                if case == "artifact-bool":
                    value["artifacts"]["main"]["dev"] = True
                if case == "observed-bool":
                    value["observed"]["main"]["retained"]["links"] = True
                if case == "path":
                    value["candidate_name"] = "../candidate.sqlite3"
                if case == "source-digest":
                    value["source"]["history_sha256"] = "0" * 64
                if case == "candidate-owner":
                    value["candidate"]["uid"] += 1
                if case == "source-unknown":
                    value["source"]["extra"] = 0
                if case == "observed-unknown":
                    value["observed"]["main"]["retained"]["extra"] = 0
                if case == "observed-links":
                    value["observed"]["main"]["retained"]["links"] = 2
                if case == "missing-role":
                    del value["observed"]["main"]
                raw = json.dumps(value).encode()
                if case == "duplicate":
                    raw = raw.replace(b'"version": 1', b'"version": 1, "version": 1')
                if case == "oversize":
                    raw += b" " * 16385
                if case == "partial":
                    raw = b"{"
                path.write_bytes(raw)
                resume = self.resume_args(database)
                before = inventory(database.parent)
                with patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite before authentication")):
                    self.assertEqual(api.main(resume), 4)
                self.assertEqual(inventory(database.parent), before)

    def test_resume_all_location_permutations(self):
        from history_service import explicit_recovery as api
        count = 0
        for locations in itertools.product(("original", "retained", "both"), repeat=4):
            with self.subTest(locations=locations), tempfile.TemporaryDirectory() as temporary:
                database, args, payload = self.fixture(temporary, locations)
                self.prepare(database, args)
                root = recovery_state.recovery_path(database)
                record = json.loads((root / "intent.json").read_bytes())
                self.assertEqual(api.main(self.resume_args(database)), 5)
                self.assertEqual(database.read_bytes(), payload)
                for role, facts in record["artifacts"].items():
                    self.assertEqual((root / role).stat().st_ino, facts["ino"])
                    self.assertEqual(digest(root / role), facts["sha256"])
                    self.assertEqual((root / role).stat().st_nlink, 1)
                count += 1
        print("RESUME_LOCATION_PERMUTATIONS=" + str(count))

    def test_resume_receipt_schema_and_partial_write_fail_closed(self):
        from history_service import explicit_recovery as api, recovery_apply
        cases = ("partial", "oversize", "unknown", "duplicate", "version-bool", "marker-bool", "completed-int",
                 "candidate", "operation", "mode", "fifo", "symlink", "missing", "short-write", "zero-write")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                database, args, payload = self.fixture(temporary)
                root = recovery_state.recovery_path(database)
                path = root / "completed.json"
                if case.endswith("write"):
                    self.prepare(database, args)
                    selected = self.resume_args(database)
                    original = os.write
                    def short(fd, raw):
                        if path.exists() and os.fstat(fd).st_ino == path.stat().st_ino:
                            return original(fd, raw[:0 if case == "zero-write" else 1])
                        return original(fd, raw)
                    with patch.object(recovery_apply.os, "write", side_effect=short):
                        self.assertEqual(api.main(selected), 5)
                    self.assertEqual(path.stat().st_size, 0 if case == "zero-write" else 1)
                    self.assertEqual(database.read_bytes(), payload)
                else:
                    self.assertEqual(api.main(args), 5)
                    selected = self.resume_args(database)
                    value = json.loads(path.read_bytes())
                    if case == "unknown":
                        value["extra"] = 1
                    if case == "version-bool":
                        value["version"] = True
                    if case == "marker-bool":
                        value["backfill_marker"] = True
                    if case == "completed-int":
                        value["recovery_completed"] = 0
                    if case in ("candidate", "operation"):
                        value[case]["ino"] += 1
                    raw = json.dumps(value).encode()
                    if case == "partial":
                        raw = b"{"
                    if case == "oversize":
                        raw += b" " * 16385
                    if case == "duplicate":
                        raw = raw.replace(b'"version": 1', b'"version": 1,"version": 1')
                    path.write_bytes(raw)
                    if case == "mode":
                        path.chmod(0o644)
                    if case in ("fifo", "symlink", "missing"):
                        parked = Path(temporary) / "parked-receipt"
                        path.rename(parked)
                        if case == "fifo":
                            os.mkfifo(path, 0o600)
                        if case == "symlink":
                            path.symlink_to(parked)
                if case not in ("fifo", "symlink", "missing"):
                    selected = self.resume_args(database)
                before = inventory(database.parent)
                with patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite before receipt authentication")):
                    self.assertEqual(api.main(selected), 4)
                self.assertEqual(inventory(database.parent), before)
                self.assertTrue(root.is_dir())

    def test_invalid_journal_tokens_are_evidence_refusals_not_argument_errors(self):
        from history_service import explicit_recovery as api
        for key in ("bundle_sha256", "manifest_sha256", "history_sha256"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                database, args, _ = self.fixture(temporary)
                self.prepare(database, args)
                path = recovery_state.recovery_path(database) / "recovery-operation.json"
                value = json.loads(path.read_bytes())
                value["source"][key] = "MALFORMED"
                path.write_text(json.dumps(value))
                before = inventory(database.parent)
                self.assertEqual(api.main(self.resume_args(database)), 4)
                self.assertEqual(inventory(database.parent), before)

    def test_resume_schema_io_error_is_evidence_refusal_not_lock_busy(self):
        from history_service import explicit_recovery as api
        with tempfile.TemporaryDirectory() as temporary:
            database, args, _ = self.fixture(temporary)
            self.prepare(database, args)
            selected = self.resume_args(database)
            before = inventory(database.parent)
            with patch.object(api, "_validate_candidate", side_effect=sqlite3.OperationalError("synthetic IO")):
                self.assertEqual(api.main(selected), 4)
            self.assertEqual(inventory(database.parent), before)

    def test_resume_stale_anchors_and_evidence_preserve_inventory(self):
        from history_service import explicit_recovery as api
        cases = ("operation-replacement", "receipt-replacement", "intent-replacement", "candidate-replacement",
                 "operation-change", "receipt-change", "new-sidecar", "extra-link", "missing-candidate",
                 "wrong-topology", "wrong-id", "wrong-digest", "missing-anchor", "missing-receipt-anchor")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                database, args, _ = self.fixture(temporary)
                if case.startswith("receipt") or case == "missing-receipt-anchor":
                    self.assertEqual(api.main(args), 5)
                else:
                    self.prepare(database, args)
                root = recovery_state.recovery_path(database)
                resume = self.resume_args(database)
                names = {"operation": "recovery-operation.json", "receipt": "completed.json",
                         "intent": "intent.json", "candidate": "candidate.sqlite3"}
                if case.endswith("replacement"):
                    path = root / names[case.split("-")[0]]
                    parked = Path(temporary) / "parked"
                    path.rename(parked)
                    path.write_bytes(parked.read_bytes())
                    path.chmod(0o600)
                elif case.endswith("change"):
                    path = root / names[case.split("-")[0]]
                    path.write_bytes(path.read_bytes() + b" ")
                elif case == "new-sidecar":
                    Path(str(database) + "-wal").write_bytes(b"synthetic")
                elif case == "extra-link":
                    os.link(root / "main", Path(temporary) / "extra")
                elif case == "missing-candidate":
                    (root / "candidate.sqlite3").rename(Path(temporary) / "parked")
                elif case == "wrong-topology":
                    resume.remove("--resolved-topology")
                    resume.remove("unsegmented")
                elif case == "wrong-id":
                    resume[resume.index("--recovery-id") + 1] = "0" * 32
                elif case == "wrong-digest":
                    resume[resume.index("--intent-sha256") + 1] = "0" * 64
                elif case in ("missing-anchor", "missing-receipt-anchor"):
                    flag = "--operation-identity" if case == "missing-anchor" else "--receipt-identity"
                    index = resume.index(flag)
                    del resume[index:index + 2]
                before = inventory(database.parent)
                with patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite before authentication")):
                    self.assertEqual(api.main(resume), 4)
                self.assertEqual(inventory(database.parent), before)


class FinalizationTests(unittest.TestCase):
    fixture = JournaledApplyTests.fixture

    @staticmethod
    def selections(database, rid):
        pending = recovery_state.recovery_path(database)
        root = pending if pending.exists() else database.with_name(database.name + '.recovery-archive-' + rid)
        args = ['finalize', '--database', str(database), '--recovery-id', rid,
                '--intent-sha256', digest(root / 'intent.json'), '--offline', '--apply',
                '--resolved-topology', 'unsegmented']
        for name, flag in [('recovery-operation.json', 'operation'), ('completed.json', 'receipt'),
                           ('finalized.json', 'completion')]:
            path = root / name
            if path.exists():
                info = path.stat()
                args += ['--' + flag + '-sha256', digest(path), '--' + flag + '-identity',
                         f'{info.st_dev}:{info.st_ino}']
        gate = database.with_name(database.name + '.recovery-finalizing')
        if not gate.exists():
            gate = root / 'finalization-gate.json'
        if gate.exists():
            info = gate.stat()
            args += ['--gate-sha256', digest(gate), '--gate-identity', f'{info.st_dev}:{info.st_ino}']
        return args

    def test_real_cli_finalizes_and_replays_archive(self):
        from history_service import explicit_recovery as api
        with tempfile.TemporaryDirectory() as temporary:
            database, args, payload = self.fixture(temporary)
            self.assertEqual(api.main(args), 5)
            root = recovery_state.recovery_path(database)
            originals = {p.name: (p.read_bytes(), p.stat().st_ino) for p in root.iterdir()}
            rid = json.loads((root / 'intent.json').read_bytes())['id']
            child = subprocess.run([sys.executable, '-B', 'scripts/recover_paused_history.py',
                                    *self.selections(database, rid)], capture_output=True, text=True)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertTrue(json.loads(child.stdout)['recovery_completed'])
            archive = database.with_name(database.name + '.recovery-archive-' + rid)
            self.assertFalse(root.exists())
            self.assertEqual(recovery_state.inspect_recovery(database), 'none')
            self.assertEqual(database.read_bytes(), payload)
            for name, facts in originals.items():
                self.assertEqual(((archive / name).read_bytes(), (archive / name).stat().st_ino), facts)
            self.assertTrue(json.loads((archive / 'finalized.json').read_bytes())['recovery_completed'])
            before = inventory(database.parent)
            self.assertEqual(api.main(self.selections(database, rid)), 0)
            self.assertEqual(inventory(database.parent), before)

    def test_finalization_process_crashes_preserve_gate_and_replay(self):
        from history_service import explicit_recovery as api
        script = r'''
import json, os, sys
from history_service import recovery_apply, explicit_recovery
trace = []
def boundary(event):
    trace.append(event)
    if len(trace) == int(sys.argv[1]): os._exit(71)
recovery_apply._boundary = boundary
assert explicit_recovery.main(json.loads(sys.argv[2])) == 0
print('TRACE:' + json.dumps(trace))
'''
        restart = r'''
import sqlite3, sys
from unittest.mock import patch
from history_service.store import HistoryStore
paused = sys.argv[2] == '1'
with patch.object(sqlite3, 'connect', side_effect=AssertionError('unexpected SQLite open')):
    store = HistoryStore(sys.argv[1], initialize=paused)
    assert store.recovery_status()['recovery_required'] == paused
'''
        outcomes = []
        traces = {}
        for start in ('pending', 'archived-gated', 'released'):
            def setup(temporary):
                database, args, payload = self.fixture(temporary)
                self.assertEqual(api.main(args), 5)
                root = recovery_state.recovery_path(database)
                rid = json.loads((root / 'intent.json').read_bytes())['id']
                if start != 'pending':
                    from history_service import recovery_apply
                    def stop(event):
                        if event == 'after:finalize:archive-rename':
                            raise OSError('synthetic crash')
                    if start == 'archived-gated':
                        with patch.object(recovery_apply, '_boundary', side_effect=stop):
                            self.assertEqual(api.main(self.selections(database, rid)), 5)
                    else:
                        self.assertEqual(api.main(self.selections(database, rid)), 0)
                return database, rid, payload
            with tempfile.TemporaryDirectory() as temporary:
                database, rid, _ = setup(temporary)
                child = subprocess.run([sys.executable, '-B', '-c', script, '0',
                                        json.dumps(self.selections(database, rid))], capture_output=True, text=True, timeout=30)
                self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                trace = json.loads(next(line[6:] for line in child.stdout.splitlines() if line.startswith('TRACE:')))
                traces[start] = trace
            for cut, event in enumerate(trace, 1):
                with self.subTest(start=start, cut=cut, event=event), tempfile.TemporaryDirectory() as temporary:
                    database, rid, payload = setup(temporary)
                    pending = recovery_state.recovery_path(database)
                    archive = database.with_name(database.name + '.recovery-archive-' + rid)
                    root = pending if pending.exists() else archive
                    original = {p.name: (p.read_bytes(), p.stat().st_ino) for p in root.iterdir() if p.is_file()}
                    child = subprocess.run([sys.executable, '-B', '-c', script, str(cut),
                                            json.dumps(self.selections(database, rid))], capture_output=True, text=True, timeout=30)
                    self.assertEqual(child.returncode, 71, child.stdout + child.stderr)
                    root = pending if pending.exists() else archive
                    gate = recovery_state.finalization_path(database)
                    paused = pending.exists() or gate.exists() or not any(archive.glob('committed-*'))
                    if not paused:
                        self.assertTrue((archive / 'finalized.json').exists())
                        self.assertTrue((archive / 'finalization-gate.json').exists())
                        self.assertTrue(json.loads((archive / 'finalized.json').read_bytes())['recovery_completed'])
                    before = inventory(database.parent)
                    for _ in range(2):
                        child = subprocess.run([sys.executable, '-B', '-c', restart, str(database), str(int(paused))],
                                                capture_output=True, text=True, timeout=20)
                        self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(inventory(database.parent), before)
                    partial = any(p.exists() and p.stat().st_size == 0 for p in (gate, root / 'finalized.json'))
                    child = subprocess.run([sys.executable, '-B', 'scripts/recover_paused_history.py',
                                            *self.selections(database, rid)], capture_output=True, text=True, timeout=30)
                    expected = 4 if partial else 0
                    self.assertEqual(child.returncode, expected, child.stdout + child.stderr)
                    if partial:
                        self.assertEqual(inventory(database.parent), before)
                    self.assertEqual(database.read_bytes(), payload)
                    root = pending if pending.exists() else archive
                    for name, facts in original.items():
                        self.assertEqual(((root / name).read_bytes(), (root / name).stat().st_ino), facts)
                        self.assertEqual((root / name).stat().st_nlink, 1)
                    paused = recovery_state.inspect_recovery(database) != 'none'
                    for _ in range(2):
                        child = subprocess.run([sys.executable, '-B', '-c', restart, str(database), str(int(paused))],
                                                capture_output=True, text=True, timeout=20)
                        self.assertEqual(child.returncode, 0, child.stderr)
                    outcomes.append(dict(start=start, cut=cut, event=event, exit=expected))
        print('FINALIZATION_FAULT_MATRIX=' + json.dumps(dict(traces=traces, outcomes=outcomes,
                                                            crash_cases=len(outcomes), fresh_starts=4 * len(outcomes))))

    def test_selected_record_replacement_missing_and_collisions_refuse(self):
        from history_service import explicit_recovery as api
        cases = ('operation', 'receipt', 'intent', 'main', 'wal', 'gate', 'completion', 'archive', 'sidecar')
        for case in cases:
            for mutation in ('missing', 'same-bytes-replacement'):
                with self.subTest(case=case, mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                    database, args, _ = self.fixture(temporary)
                    self.assertEqual(api.main(args), 5)
                    root = recovery_state.recovery_path(database)
                    rid = json.loads((root / 'intent.json').read_bytes())['id']
                    if case in ('gate', 'completion'):
                        self.assertEqual(api.main(self.selections(database, rid)), 0)
                        root = database.with_name(database.name + '.recovery-archive-' + rid)
                    selected = self.selections(database, rid)
                    names = dict(operation='recovery-operation.json', receipt='completed.json', intent='intent.json',
                                 main='main', wal='wal', gate='finalization-gate.json', completion='finalized.json')
                    if case == 'archive':
                        database.with_name(database.name + '.recovery-archive-' + rid).mkdir()
                    elif case == 'sidecar':
                        Path(str(database) + '-wal').write_bytes(b'synthetic-collision')
                    else:
                        path = root / names[case]
                        # Keep the old inode allocated so replacement cannot reuse it.
                        moved = Path(temporary) / 'old-record'
                        path.rename(moved)
                        if mutation != 'missing':
                            path.write_bytes(moved.read_bytes())
                            path.chmod(moved.stat().st_mode & 0o777)
                    before = inventory(database.parent)
                    with patch.object(sqlite3, 'connect', side_effect=AssertionError('unexpected SQLite open')):
                        self.assertEqual(api.main(selected), 4)
                    self.assertEqual(inventory(database.parent), before)

    def test_finalization_io_errors_and_late_collisions_preserve_evidence(self):
        from history_service import explicit_recovery as api, recovery_apply
        events = ('before:finalize:archive-parent-fsync', 'before:finalize:schema-readback',
                  'before:finalized.json:file-fsync', 'before:finalized.json:readback',
                  'before:finalize:gate-archive-rename', 'before:finalize:clear-parent-fsync',
                  'archive-collision', 'gate-collision')
        for event in events:
            with self.subTest(event=event), tempfile.TemporaryDirectory() as temporary:
                database, args, payload = self.fixture(temporary)
                self.assertEqual(api.main(args), 5)
                pending = recovery_state.recovery_path(database)
                originals = {p.name: (p.read_bytes(), p.stat().st_ino) for p in pending.iterdir()}
                rid = json.loads((pending / 'intent.json').read_bytes())['id']
                archive = database.with_name(database.name + '.recovery-archive-' + rid)
                selected = self.selections(database, rid)
                hit = []
                def boundary(name):
                    if event == 'archive-collision' and name == 'before:finalize:archive-rename':
                        archive.mkdir()
                        hit.append(name)
                    elif event == 'gate-collision' and name == 'before:finalize:gate-archive-rename':
                        (archive / 'finalization-gate.json').write_bytes(b'synthetic-collision')
                        hit.append(name)
                    elif name == event and not hit:
                        hit.append(name)
                        raise OSError('synthetic I/O error')
                with patch.object(recovery_apply, '_boundary', side_effect=boundary):
                    self.assertEqual(api.main(selected), 4 if event == 'before:finalize:schema-readback' else 5)
                self.assertTrue(hit)
                self.assertEqual(database.read_bytes(), payload)
                root = pending if pending.exists() else archive
                for name, facts in originals.items():
                    self.assertEqual(((root / name).read_bytes(), (root / name).stat().st_ino), facts)
                self.assertNotEqual(recovery_state.inspect_recovery(database), 'none')
                if event == 'gate-collision':
                    self.assertEqual((archive / 'finalization-gate.json').read_bytes(), b'synthetic-collision')
                if event not in ('archive-collision', 'gate-collision'):
                    self.assertEqual(api.main(self.selections(database, rid)), 0)

    def test_gate_observation_cli_never_reports_clear(self):
        from history_service import explicit_recovery as api
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'history.db'
            recovery_state.finalization_path(database).write_bytes(b'partial')
            self.assertNotEqual(api.inspect(database)['state'], 'none')

    def test_finalization_lock_latch_and_fresh_usable_store(self):
        from history_service import explicit_recovery as api, recovery_apply
        from history_service.store import HistoryStore
        probe = r'''
import sqlite3, sys
from pathlib import Path
from history_service.migration_lock import _history_lifecycle_lock
try:
    with _history_lifecycle_lock(Path(sys.argv[1]), blocking=False): sys.exit(9)
except sqlite3.OperationalError: sys.exit(0)
'''
        with tempfile.TemporaryDirectory() as temporary:
            database, args, payload = self.fixture(temporary)
            with patch.object(sqlite3, 'connect', side_effect=AssertionError('paused SQLite open')):
                old_store = HistoryStore(str(database))
            self.assertEqual(api.main(args), 5)
            root = recovery_state.recovery_path(database)
            rid = json.loads((root / 'intent.json').read_bytes())['id']
            alias = Path(temporary) / 'alias'
            alias.symlink_to(database.parent, target_is_directory=True)
            events = []
            def boundary(event):
                if event.startswith('before:'):
                    for target in (database, alias / database.name):
                        child = subprocess.run([sys.executable, '-B', '-c', probe, str(target)],
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(child.returncode, 0, child.stderr)
                        events.append(event)
            with patch.object(recovery_apply, '_boundary', side_effect=boundary):
                self.assertEqual(api.main(self.selections(database, rid)), 0)
            self.assertEqual(database.read_bytes(), payload)
            with patch.object(sqlite3, 'connect', side_effect=AssertionError('latched SQLite open')):
                self.assertTrue(old_store.recovery_status()['recovery_required'])
            fresh = HistoryStore(str(database))
            self.assertFalse(fresh.recovery_status()['recovery_required'])
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
                self.assertEqual(connection.execute('SELECT system_id,value_integer FROM metric_samples').fetchall(),
                                 [('synthetic-selected', 37)])
            print('FINALIZATION_LOCK_PROBES=' + str(len(events)))

    def test_terminal_observation_preserves_parent_aliases(self):
        from history_service import explicit_recovery as api
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / 'empty'
            parent.mkdir()
            alias = Path(tmp) / 'empty-alias'
            alias.symlink_to(parent, target_is_directory=True)
            self.assertEqual(recovery_state.inspect_recovery(alias / 'history.db'), 'none')
            db, args, _ = self.fixture(tmp)
            alias = Path(tmp) / 'target-alias'
            alias.symlink_to(db.parent, target_is_directory=True)
            target = alias / db.name
            with patch.object(sqlite3, 'connect', side_effect=AssertionError('paused alias SQLite open')):
                self.assertTrue(HistoryStore(str(target)).recovery_status()['recovery_required'])
            self.assertEqual(api.main(args), 5)
            rid = json.loads((recovery_state.recovery_path(db) / 'intent.json').read_bytes())['id']
            self.assertEqual(api.main(self.selections(db, rid)), 0)
            self.assertFalse(HistoryStore(str(target)).recovery_status()['recovery_required'])

    def test_terminal_creation_readback_prevents_false_success(self):
        from history_service import explicit_recovery as api
        from history_service.store import HistoryStore
        with tempfile.TemporaryDirectory() as tmp:
            db, args, _ = self.fixture(tmp)
            self.assertEqual(api.main(args), 5)
            rid = json.loads((recovery_state.recovery_path(db) / 'intent.json').read_bytes())['id']
            mkdir = os.mkdir
            hits = []
            def unsafe(name, mode=0o777, *, dir_fd=None):
                result = mkdir(name, mode=mode, dir_fd=dir_fd)
                if str(name).startswith('committed-'):
                    hits.append(name)
                    os.chmod(name, 0o755, dir_fd=dir_fd)
                return result
            with patch.object(os, 'mkdir', side_effect=unsafe):
                self.assertEqual(api.main(self.selections(db, rid)), 5)
            self.assertEqual(len(hits), 1)
            with patch.object(sqlite3, 'connect', side_effect=AssertionError('unsafe terminal SQLite open')):
                self.assertTrue(HistoryStore(str(db)).recovery_status()['recovery_required'])

    def test_terminal_startup_validates_retained_protocol(self):
        from history_service import explicit_recovery as api
        from history_service.store import HistoryStore
        cases = ('missing-terminal', 'unsafe-terminal', 'nonempty-terminal', 'symlink-terminal',
                 'malformed-terminal', 'duplicate-terminal', 'missing-completion', 'replacement-completion',
                 'duplicate-completion-key', 'false-completion', 'unknown-completion-field',
                 'missing-gate', 'replacement-gate', 'missing-operation', 'missing-receipt',
                 'missing-original', 'modified-original', 'wrong-root', 'archive-symlink')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                db, args, _ = self.fixture(tmp)
                self.assertEqual(api.main(args), 5)
                rid = json.loads((recovery_state.recovery_path(db) / 'intent.json').read_bytes())['id']
                self.assertEqual(api.main(self.selections(db, rid)), 0)
                archive = db.with_name(db.name + '.recovery-archive-' + rid)
                terminal = next(archive.glob('committed-*'))
                parked = Path(tmp) / 'parked-evidence'
                if case == 'unsafe-terminal':
                    terminal.chmod(0o755)
                elif case == 'nonempty-terminal':
                    (terminal / 'unexpected').write_bytes(b'synthetic')
                elif case == 'duplicate-terminal':
                    (archive / 'committed-unknown').mkdir(mode=0o700)
                elif case in ('missing-terminal', 'symlink-terminal', 'malformed-terminal'):
                    terminal.rename(parked)
                    if case == 'symlink-terminal':
                        terminal.symlink_to(parked, target_is_directory=True)
                    elif case == 'malformed-terminal':
                        (archive / 'committed-malformed').mkdir(mode=0o700)
                elif case in ('wrong-root', 'archive-symlink'):
                    archive.rename(parked)
                    if case == 'archive-symlink':
                        archive.symlink_to(parked, target_is_directory=True)
                    else:
                        import shutil
                        shutil.copytree(parked, archive)
                else:
                    name = {'completion': 'finalized.json', 'gate': 'finalization-gate.json',
                            'operation': 'recovery-operation.json', 'receipt': 'completed.json',
                            'original': 'main'}[case.split('-')[-1] if case.startswith(('missing-', 'replacement-', 'modified-'))
                                              else 'completion']
                    path = archive / name
                    raw = path.read_bytes()
                    path.rename(parked)
                    if not case.startswith('missing-'):
                        if case == 'duplicate-completion-key':
                            raw = raw.replace(b'{', b'{"version":1,', 1)
                        elif case == 'false-completion':
                            raw = raw.replace(b'"recovery_completed":true', b'"recovery_completed":false')
                        elif case == 'unknown-completion-field':
                            raw = raw.replace(b'{', b'{"unknown":1,', 1)
                        elif case == 'modified-original':
                            raw += b'synthetic-corruption'
                        path.write_bytes(raw)
                        path.chmod(0o600)
                before = inventory(db.parent)
                with patch.object(sqlite3, 'connect', side_effect=AssertionError('invalid protocol SQLite open')):
                    for _ in range(2):
                        self.assertTrue(HistoryStore(str(db)).recovery_status()['recovery_required'])
                self.assertEqual(inventory(db.parent), before)

    def test_committed_replay_failure_keeps_terminal_outcome(self):
        import io
        from contextlib import redirect_stdout
        from history_service import explicit_recovery as api, recovery_apply
        with tempfile.TemporaryDirectory() as tmp:
            db, args, _ = self.fixture(tmp)
            self.assertEqual(api.main(args), 5)
            rid = json.loads((recovery_state.recovery_path(db) / 'intent.json').read_bytes())['id']
            self.assertEqual(api.main(self.selections(db, rid)), 0)
            before = inventory(db.parent)
            def fail(event):
                if event == 'before:finalize:schema-readback':
                    raise OSError('synthetic committed replay response failure')
            output = io.StringIO()
            with patch.object(recovery_apply, '_boundary', side_effect=fail), redirect_stdout(output):
                self.assertEqual(api.main(self.selections(db, rid)), 5)
            result = json.loads(output.getvalue())
            self.assertEqual(result['state'], 'completed-replay-failed')
            self.assertTrue(result['recovery_completed'])
            self.assertEqual(recovery_state.inspect_recovery(db), 'none')
            self.assertEqual(inventory(db.parent), before)

    def test_terminal_commit_failure_is_not_incomplete(self):
        import io
        from contextlib import redirect_stdout
        from history_service import explicit_recovery as api, recovery_apply
        from history_service.store import HistoryStore
        for event, paused in (('before:finalize:terminal-commit', True),
                              ('after:finalize:terminal-commit', False),
                              ('before:finalize:terminal-fsync', False),
                              ('after:finalize:terminal-fsync', False)):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as tmp:
                db, args, _ = self.fixture(tmp)
                self.assertEqual(api.main(args), 5)
                rid = json.loads((recovery_state.recovery_path(db) / 'intent.json').read_bytes())['id']
                hit = []
                def fail(name):
                    if name == event:
                        hit.append(name)
                        raise OSError('synthetic terminal failure')
                output = io.StringIO()
                with patch.object(recovery_apply, '_boundary', side_effect=fail), redirect_stdout(output):
                    self.assertEqual(api.main(self.selections(db, rid)), 5)
                self.assertEqual(hit, [event])
                value = json.loads(output.getvalue())
                self.assertEqual(value['state'], 'refused' if paused else 'outcome-unknown')
                self.assertEqual(recovery_state.inspect_recovery(db) != 'none', paused)
                # Probe without initializing a committed generation before explicit replay.
                self.assertEqual(HistoryStore(str(db), initialize=paused).recovery_status()['recovery_required'], paused)
                self.assertEqual(api.main(self.selections(db, rid)), 0)
                self.assertFalse(HistoryStore(str(db)).recovery_status()['recovery_required'])

    def test_late_failures_persist_startup_refusal(self):
        from history_service import explicit_recovery as api
        from history_service.store import HistoryStore
        for fault in ('final-schema-readback', 'gate-archive-fsync', 'clear-parent-fsync'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                db, args, payload = self.fixture(tmp)
                self.assertEqual(api.main(args), 5)
                root = recovery_state.recovery_path(db)
                rid = json.loads((root / 'intent.json').read_bytes())['id']
                originals = {p.name: (p.read_bytes(), p.stat().st_ino) for p in root.iterdir()}
                hits = []
                if fault == 'final-schema-readback':
                    real = api._validate_candidate
                    def validate(*a, **kw):
                        hits.append(1)
                        if len(hits) == 5:
                            raise OSError('synthetic final readback failure')
                        return real(*a, **kw)
                    context = patch.object(api, '_validate_candidate', side_effect=validate)
                else:
                    real = os.fsync
                    def sync(fd):
                        target = os.readlink('/proc/self/fd/' + str(fd))
                        released = not recovery_state.finalization_path(db).exists() and not root.exists()
                        if released and ((fault == 'gate-archive-fsync' and '.recovery-archive-' in target)
                                         or (fault == 'clear-parent-fsync' and target == str(db.parent))):
                            hits.append(1)
                            raise OSError('synthetic post-clear fsync failure')
                        return real(fd)
                    context = patch.object(os, 'fsync', side_effect=sync)
                with context:
                    self.assertEqual(api.main(self.selections(db, rid)), 5)
                self.assertTrue(hits)
                archive = db.with_name(db.name + '.recovery-archive-' + rid)
                for name, facts in originals.items():
                    self.assertEqual(((archive / name).read_bytes(), (archive / name).stat().st_ino), facts)
                self.assertEqual(db.read_bytes(), payload)
                self.assertNotEqual(recovery_state.inspect_recovery(db), 'none')
                for _ in range(2):
                    with patch.object(sqlite3, 'connect', side_effect=AssertionError('incomplete SQLite open')):
                        self.assertTrue(HistoryStore(str(db)).recovery_status()['recovery_required'])
                self.assertEqual(api.main(self.selections(db, rid)), 0)
                self.assertFalse(HistoryStore(str(db)).recovery_status()['recovery_required'])

    def test_finalization_gate_alone_blocks_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'history.db'
            database.with_name(database.name + '.recovery-finalizing').write_bytes(b'partial')
            self.assertNotEqual(recovery_state.inspect_recovery(database), 'none')


class PausedPublisherGateTests(unittest.TestCase):
    """Real public entry/lock, with only the mutation body replaced by a sentinel."""

    @staticmethod
    def cases():
        from history_service import segment_migration, segment_rotation

        return (
            (segment_migration, "migrate_segmented_history", {"cutoff": "2026-01-01T00:00:00Z", "key_id": "synthetic"}),
            (segment_migration, "rollback_segmented_history", {}),
            (segment_migration, "recover_pending_migration", {}),
            (segment_rotation, "rotate_segmented_history", {"cutoff": "2026-01-01T00:00:00Z", "key_id": "synthetic"}),
            (segment_rotation, "recover_pending_rotation", {}),
        )

    @staticmethod
    def kwargs(root, public, extra, apply):
        result = dict(source=root / "history.db", segments_directory=root / "segments", apply=apply, **extra)
        if public == "rotate_segmented_history":
            result.update(scheduled_backup_directory=root / "backup",
                          scheduled_backup_status_path=root / "backup-status.json")
        return result

    @staticmethod
    def marker(root, kind, *, locked=False):
        database = root / "history.db"
        if kind in ("retained", "both"):
            if locked:
                recovery_state.pause_and_quarantine(database)
            else:
                pause_fixture(root, (kind,) * 4)
        else:
            marker = recovery_state.recovery_path(database)
            if kind == "file":
                marker.write_bytes(b"synthetic-invalid")
            elif kind == "symlink":
                marker.symlink_to(root / "missing")
            else:
                marker.mkdir(mode=0o700)
                if kind == "malformed":
                    (marker / "intent.json").write_bytes(b"{")
                    (marker / "intent.json").chmod(0o600)

    def test_paused_publishers_refuse_before_lock_and_body(self):
        for module, public, extra in self.cases():
            for kind, apply in itertools.product(("retained", "both", "partial", "malformed", "file", "symlink"), (False, True)):
                with self.subTest(entry=public, kind=kind, apply=apply), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    self.marker(root, kind)
                    before = inventory(root)
                    with patch.object(module, "history_write_lock", wraps=history_write_lock) as lock, patch.object(
                        module, "_" + public + "_locked", return_value={"unexpected": "entered"}
                    ) as body:
                        with self.assertRaises(recovery_state.HistoryRecoveryRequired):
                            getattr(module, public)(**self.kwargs(root, public, extra, apply))
                        lock.assert_not_called()
                        body.assert_not_called()
                    self.assertEqual(inventory(root), before)

    def test_paused_publishers_recheck_after_real_lock_acquisition(self):
        from contextlib import contextmanager

        for module, public, extra in self.cases():
            for kind, apply in itertools.product(("retained", "partial", "malformed", "file", "symlink"), (False, True)):
                with self.subTest(entry=public, kind=kind, apply=apply), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / "history.db").write_bytes(b"synthetic-corrupt-main")
                    snapshots = []

                    @contextmanager
                    def racing_lock(database, *, blocking):
                        with history_write_lock(database, blocking=blocking):
                            self.marker(root, kind, locked=True)
                            snapshots.append(inventory(root))
                            yield

                    with patch.object(module, "history_write_lock", side_effect=racing_lock) as lock, patch.object(
                        module, "_" + public + "_locked", return_value={"unexpected": "entered"}
                    ) as body:
                        with self.assertRaises(recovery_state.HistoryRecoveryRequired):
                            getattr(module, public)(**self.kwargs(root, public, extra, apply))
                        lock.assert_called_once()
                        body.assert_not_called()
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(inventory(root), snapshots[0])
                    # The refusal must release the real lifecycle ownership.
                    with history_write_lock(root / "history.db", blocking=False):
                        pass

    def test_clear_publishers_keep_arguments_and_lock_ownership(self):
        for module, public, extra in self.cases():
            for apply in (False, True):
                with self.subTest(entry=public, apply=apply), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / "history.db").write_bytes(b"synthetic-main")
                    kwargs = self.kwargs(root, public, extra, apply)
                    before = inventory(root)

                    def body(**received):
                        self.assertEqual(received, kwargs)
                        with self.assertRaises(sqlite3.OperationalError):
                            with history_write_lock(kwargs["source"], blocking=False):
                                self.fail("publisher lost lifecycle ownership")
                        return {"sentinel": "clear"}

                    with patch.object(module, "_" + public + "_locked", side_effect=body) as locked_body:
                        self.assertEqual(getattr(module, public)(**kwargs), {"sentinel": "clear"})
                        locked_body.assert_called_once_with(**kwargs)
                    self.assertEqual(inventory(root), before)


class ExplicitHistoryRecoveryTests(unittest.TestCase):
    def api(self):
        import importlib.util

        self.assertIsNotNone(
            importlib.util.find_spec("history_service.explicit_recovery"), "offline recovery admission is missing"
        )
        from history_service import explicit_recovery

        return explicit_recovery

    def test_inspect_is_bounded_and_never_reads_evidence_or_sqlite(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as directory:
            database, selected, intent = pause_fixture(Path(directory))
            before = inventory(Path(directory))
            with patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite forbidden")):
                result = api.inspect(database)
            self.assertEqual(
                result,
                {"state": "required", "recovery_id": selected, "intent_sha256": intent, "restore_available": False},
            )
            self.assertEqual(inventory(Path(directory)), before)

    def test_all_role_locations_authenticate_without_mutation(self):
        api = self.api()
        for locations in itertools.product(("original", "retained", "both"), repeat=4):
            with self.subTest(locations=locations), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)
                database, selected, intent = pause_fixture(parent, locations)
                before = inventory(parent)
                with api.admit_evidence(
                    database, recovery_id=selected, intent_sha256=intent, resolved_topology="unsegmented"
                ) as admitted:
                    self.assertEqual(set(admitted["artifacts"]), set(recovery_state.ARTIFACTS))
                self.assertEqual(inventory(parent), before)
                if locations[0] == "both":
                    with self.assertRaisesRegex(ValueError, "hard-link"):
                        with history_write_lock(database, blocking=False):
                            self.fail("normal admission bypassed")

    def test_literal_tokens_refuse_before_any_lookup(self):
        api = self.api()
        for selected, intent in [
            ("A" * 32, "a" * 64),
            ("a" * 31, "a" * 64),
            ("a" * 32, "A" * 64),
            ("a" * 32, "a" * 64 + " "),
            (True, "a" * 64),
        ]:
            with (
                self.subTest(selected=selected, intent=intent),
                patch.object(os, "open", side_effect=AssertionError("lookup")),
            ):
                with self.assertRaises(api.RecoveryRefusal):
                    with api.admit_evidence(
                        Path("/absent/history.db"),
                        recovery_id=selected,
                        intent_sha256=intent,
                        resolved_topology="unsegmented",
                    ):
                        self.fail("accepted")

    def test_evidence_conflicts_preserve_inventory_and_never_open_sqlite(self):
        api = self.api()
        for kind in (
            "missing",
            "replacement",
            "newmain",
            "changed",
            "extra-link",
            "symlink",
            "fifo",
            "unknown-root",
            "operation",
            "new-sidecar",
            "stale-id",
            "stale-digest",
            "topology",
            "archive",
            "unknown-parent",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)
                database, selected, intent = pause_fixture(parent)
                root = recovery_state.recovery_path(database)
                retained = root / "main"
                if kind == "missing":
                    retained.unlink()
                if kind == "replacement":
                    data = retained.read_bytes()
                    retained.rename(parent / "old")
                    retained.write_bytes(data)
                if kind == "newmain":
                    database.write_bytes(b"readable-divergence")
                if kind == "changed":
                    retained.write_bytes(b"changed")
                if kind == "extra-link":
                    os.link(retained, parent / "alias")
                if kind in ("symlink", "fifo"):
                    retained.unlink()
                    if kind == "symlink":
                        retained.symlink_to(root / "wal")
                    else:
                        os.mkfifo(retained)
                if kind == "unknown-root":
                    (root / "unexpected").write_bytes(b"preserve")
                if kind == "operation":
                    (root / "recovery-operation.json").write_bytes(b"{}")
                if kind == "new-sidecar":
                    Path(str(database) + "-wal").write_bytes(b"new-wal")
                if kind == "stale-id":
                    selected = "0" * 32
                if kind == "stale-digest":
                    intent = "0" * 64
                if kind == "archive":
                    (parent / (database.name + ".recovery-archive-" + selected)).mkdir()
                if kind == "unknown-parent":
                    (parent / "segments").mkdir()
                before = inventory(parent)
                with patch.object(sqlite3, "connect", side_effect=AssertionError("SQLite forbidden")):
                    with self.assertRaises(api.RecoveryRefusal):
                        with api.admit_evidence(
                            database,
                            recovery_id=selected,
                            intent_sha256=intent,
                            resolved_topology="unknown" if kind == "topology" else "unsegmented",
                        ):
                            self.fail("accepted")
                self.assertEqual(inventory(parent), before)

    def test_unsafe_intent_never_returns_selection(self):
        api = self.api()
        for kind in ("oversize", "duplicate", "unknown", "mode", "link", "fifo", "root-mode", "version"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                database, _, _ = pause_fixture(Path(directory))
                root = recovery_state.recovery_path(database)
                record = root / "intent.json"
                if kind == "oversize":
                    record.write_bytes(b" " * 8193)
                if kind == "duplicate":
                    record.write_bytes(b'{"version":1,"version":1}')
                if kind in ("unknown", "version"):
                    value = json.loads(record.read_bytes())
                    value["extra" if kind == "unknown" else "version"] = True
                    record.write_text(json.dumps(value))
                if kind == "mode":
                    record.chmod(0o644)
                if kind == "root-mode":
                    root.chmod(0o755)
                if kind == "link":
                    os.link(record, root / "alias")
                if kind == "fifo":
                    record.unlink()
                    os.mkfifo(record, 0o600)
                before = inventory(Path(directory))
                self.assertEqual(api.inspect(database), {"state": "invalid", "restore_available": False})
                self.assertEqual(inventory(Path(directory)), before)

    def test_supported_bundle_plan_never_mutates_target(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as scratch:
            parent = Path(directory)
            database, selected, intent = pause_fixture(parent, ("both",) * 4)
            bundle = bundle_fixture(Path(scratch))
            before = inventory(parent)
            result = api.plan_bundle(
                database,
                recovery_id=selected,
                intent_sha256=intent,
                bundle=bundle,
                bundle_sha256=digest(bundle),
                resolved_topology="unsegmented",
                scratch_parent=Path(scratch),
            )
            self.assertEqual(result["state"], "admitted-plan-only")
            self.assertFalse(result["restore_available"])
            self.assertEqual(result["schema"], "supported")
            self.assertEqual(result["backfill_marker"], 1)
            self.assertEqual(result["bundle_sha256"], digest(bundle))
            self.assertEqual(inventory(parent), before)

    def test_bundle_and_schema_refusals_do_not_change_target(self):
        api = self.api()
        cases = [
            "empty",
            "corrupt",
            "unsupported",
            "marker0",
            "digest",
            "member-digest",
            "missing-digest",
            "upper-digest",
            "wrong-generation",
            "traversal",
            "sidecar",
            "scratch-target",
        ]
        for kind in cases:
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as directory,
                tempfile.TemporaryDirectory() as scratch,
            ):
                parent = Path(directory)
                database, selected, intent = pause_fixture(parent)

                def change(manifest):
                    if kind == "member-digest":
                        manifest["files"][0]["sha256"] = "0" * 64
                    if kind == "missing-digest":
                        manifest["files"][0].pop("sha256")
                    if kind == "upper-digest":
                        manifest["files"][0]["sha256"] = manifest["files"][0]["sha256"].upper()
                    if kind == "wrong-generation":
                        manifest["schema_version"] = 2

                bundle = bundle_fixture(
                    Path(scratch),
                    kind,
                    change,
                    "../escape"
                    if kind == "traversal"
                    else "history/history.sqlite3-wal"
                    if kind == "sidecar"
                    else None,
                )
                before = inventory(parent)
                with self.assertRaises(api.RecoveryRefusal):
                    api.plan_bundle(
                        database,
                        recovery_id=selected,
                        intent_sha256=intent,
                        bundle=bundle,
                        bundle_sha256="0" * 64 if kind == "digest" else digest(bundle),
                        resolved_topology="unsegmented",
                        scratch_parent=parent if kind == "scratch-target" else Path(scratch),
                    )
                self.assertEqual(inventory(parent), before)

    def test_process_contention_and_parent_alias_refuse(self):
        api = self.api()
        script = """
import sys
from pathlib import Path
from history_service.explicit_recovery import admit_evidence, RecoveryRefusal
try:
    with admit_evidence(Path(sys.argv[1]), recovery_id=sys.argv[2], intent_sha256=sys.argv[3], resolved_topology='unsegmented'): pass
except RecoveryRefusal as exc:
    sys.exit(exc.exit_code)
sys.exit(99)
"""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as aliases:
            database, selected, intent = pause_fixture(Path(directory))
            alias = Path(aliases) / "alias"
            alias.symlink_to(database.parent, target_is_directory=True)
            with api.admit_evidence(
                database, recovery_id=selected, intent_sha256=intent, resolved_topology="unsegmented"
            ):
                for target in (database, alias / database.name, alias / "other.db"):
                    result = subprocess.run(
                        [sys.executable, "-B", "-c", script, str(target), selected, intent],
                        capture_output=True,
                        timeout=20,
                    )
                    self.assertEqual(result.returncode, 3, result.stderr)

    def test_process_crashes_at_counted_scratch_and_readback_boundaries(self):
        api = self.api()
        script = r"""
import json, os, sys
from pathlib import Path
from history_service import explicit_recovery as api
# Import validation dependencies before instrumenting operation boundaries.
from history_service import system_backup
counts = {}
selected_operation, selected_index, timing = sys.argv[6:]
def wrapped(name, original):
    def call(*args, **kwargs):
        counts[name] = counts.get(name, 0) + 1
        selected = name == selected_operation and counts[name] == int(selected_index)
        if selected and timing == 'before': os._exit(71)
        result = original(*args, **kwargs)
        if selected and timing == 'after': os._exit(71)
        return result
    return call
for name in ('fsync', 'mkdir', 'unlink', 'rmdir', 'chmod', 'link', 'rename'):
    setattr(os, name, wrapped(name, getattr(os, name)))
for name in ('_hash', '_record', '_bundle_plan'):
    setattr(api, name, wrapped(name, getattr(api, name)))
original_open = Path.open
class Output:
    def __init__(self, output): self.output = output
    def __enter__(self): return self
    def __exit__(self, *args): return wrapped('close-output', self.output.__exit__)(*args)
    def __getattr__(self, name): return getattr(self.output, name)
    def write(self, data): return wrapped('write', self.output.write)(data)
    def flush(self): return wrapped('flush', self.output.flush)()
def opened(path, mode='r', *args, **kwargs):
    output = original_open(path, mode, *args, **kwargs)
    return Output(output) if 'x' in mode or 'w' in mode else output
Path.open = opened
result = api.plan_bundle(Path(sys.argv[1]), recovery_id=sys.argv[2], intent_sha256=sys.argv[3],
    bundle=Path(sys.argv[4]), bundle_sha256=sys.argv[5], resolved_topology='unsegmented',
    scratch_parent=Path(sys.argv[4]).parent)
assert result['state'] == 'admitted-plan-only' and result['restore_available'] is False
print(json.dumps(counts, sort_keys=True))
"""
        restart = r"""
import sys, sqlite3
from unittest.mock import patch
from history_service.store import HistoryStore
with patch.object(sqlite3, 'connect', side_effect=AssertionError('unexpected SQLite open')):
    store = HistoryStore(sys.argv[1])
    assert store.recovery_status()['recovery_required']
"""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as scratch:
            database, selected, intent = pause_fixture(Path(directory), ("both",) * 4)
            bundle = bundle_fixture(Path(scratch))
            before = inventory(Path(directory))
            args = [sys.executable, "-B", "-c", script, str(database), selected, intent, str(bundle), digest(bundle)]
            result = subprocess.run([*args, "none", "0", "before"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            counts = json.loads(result.stdout)
            self.assertGreater(counts["write"], 0)
            self.assertGreater(counts["fsync"], 0)
            self.assertNotIn("link", counts)
            self.assertNotIn("rename", counts)
            crash_cases = 0
            restarts = 0
            for operation, count in counts.items():
                for index in range(1, count + 1):
                    for timing in ("before", "after"):
                        with self.subTest(operation=operation, index=index, timing=timing):
                            result = subprocess.run(
                                [*args, operation, str(index), timing], capture_output=True, text=True, timeout=30
                            )
                            self.assertEqual(result.returncode, 71, result.stderr)
                            crash_cases += 1
                            self.assertEqual(inventory(Path(directory)), before)
                            for _ in range(2):
                                fresh = subprocess.run(
                                    [sys.executable, "-B", "-c", restart, str(database)],
                                    capture_output=True,
                                    text=True,
                                    timeout=20,
                                )
                                self.assertEqual(fresh.returncode, 0, fresh.stderr)
                                restarts += 1
                            self.assertEqual(inventory(Path(directory)), before)
                            # A fresh explicit admission remains possible; orphan
                            # scratch is outside target, never scanned or adopted.
                            with api.admit_evidence(
                                database, recovery_id=selected, intent_sha256=intent, resolved_topology="unsegmented"
                            ):
                                pass
            print(
                "ADMISSION_FAULT_COUNTS="
                + json.dumps(
                    {"boundaries": counts, "crash_cases": crash_cases, "fresh_starts": restarts}, sort_keys=True
                )
            )

    def test_same_byte_intent_replacement_during_plan_refuses(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as scratch:
            database, selected, intent = pause_fixture(Path(directory))
            bundle = bundle_fixture(Path(scratch))
            root = recovery_state.recovery_path(database)
            record = root / "intent.json"
            original = api._bundle_plan

            def swapped(*args):
                result = original(*args)
                raw = record.read_bytes()
                record.rename(root / "replaced-intent")
                record.write_bytes(raw)
                record.chmod(0o600)
                return result

            with patch.object(api, "_bundle_plan", swapped), self.assertRaises(api.RecoveryRefusal):
                api.plan_bundle(
                    database,
                    recovery_id=selected,
                    intent_sha256=intent,
                    bundle=bundle,
                    bundle_sha256=digest(bundle),
                    resolved_topology="unsegmented",
                    scratch_parent=Path(scratch),
                )
            self.assertTrue((root / "replaced-intent").exists())
            self.assertTrue((root / "main").exists())

    def test_candidate_short_write_result_refuses_even_when_bytes_landed(self):
        api = self.api()
        original_open = Path.open

        class ShortWriter:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)

            def __getattr__(self, key):
                return getattr(self.wrapped, key)

            def write(self, data):
                self.wrapped.write(data)
                return len(data) - 1

        def opened(path, mode="r", *args, **kwargs):
            result = original_open(path, mode, *args, **kwargs)
            return ShortWriter(result) if path.name == "history.sqlite3" and mode == "xb" else result

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as scratch:
            database, selected, intent = pause_fixture(Path(directory))
            bundle = bundle_fixture(Path(scratch))
            before = inventory(Path(directory))
            with patch.object(Path, "open", opened), self.assertRaises(api.RecoveryRefusal):
                api.plan_bundle(
                    database,
                    recovery_id=selected,
                    intent_sha256=intent,
                    bundle=bundle,
                    bundle_sha256=digest(bundle),
                    resolved_topology="unsegmented",
                    scratch_parent=Path(scratch),
                )
            self.assertEqual(inventory(Path(directory)), before)

    def test_cli_synthetic_inspect_and_plan_do_not_start_services(self):
        script = """
import sys
from unittest.mock import patch
from history_service.store import HistoryStore
from app.config import get_settings
from history_service.explicit_recovery import main
with patch.object(HistoryStore, '__init__', side_effect=AssertionError('store initialization forbidden')), patch('app.config.get_settings', side_effect=AssertionError('ambient config forbidden')):
    code = main(sys.argv[1:])
assert 'history_service.main' not in sys.modules
assert 'admin_service.main' not in sys.modules
assert 'app.main' not in sys.modules
raise SystemExit(code)
"""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as scratch:
            database, selected, intent = pause_fixture(Path(directory))
            bundle = bundle_fixture(Path(scratch))
            before = inventory(Path(directory))
            for argv, state in [
                (["inspect", "--database", str(database)], "required"),
                (
                    [
                        "restore-bundle",
                        "--database",
                        str(database),
                        "--recovery-id",
                        selected,
                        "--intent-sha256",
                        intent,
                        "--bundle",
                        str(bundle),
                        "--bundle-sha256",
                        digest(bundle),
                        "--history-only",
                        "--dry-run",
                        "--resolved-topology",
                        "unsegmented",
                        "--scratch-parent",
                        scratch,
                    ],
                    "admitted-plan-only",
                ),
            ]:
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script, *argv], capture_output=True, text=True, timeout=30
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["state"], state)
                self.assertFalse(payload["restore_available"])
                self.assertEqual(inventory(Path(directory)), before)

    def test_cli_is_explicit_and_apply_resume_are_unavailable(self):
        api = self.api()
        for argv in (
            [],
            ["inspect"],
            ["restore-bundle", "--apply"],
            ["resume"],
            ["inspect", "--database", "relative.db"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as caught:
                    api.main(argv)
                self.assertEqual(caught.exception.code, 2)


class RecoveryStatusCompositionTests(unittest.TestCase):
    """Real offline protocol evidence through history, main and admin observation."""

    fixture = JournaledApplyTests.fixture
    selections = staticmethod(FinalizationTests.selections)

    @staticmethod
    def _cli(args):
        import contextlib
        import io
        from history_service import explicit_recovery

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = explicit_recovery.main(args)
        return code, json.loads(output.getvalue())

    @staticmethod
    async def _request(application, path):
        import asyncio

        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await application({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"host", b"admin.example.test")],
            "client": ("synthetic.test", 1), "server": ("admin.example.test", 8082),
        }, receive, send)
        return (next(m["status"] for m in messages if m["type"] == "http.response.start"),
                b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))

    def _observe(self, database, store, *, paused):
        import asyncio
        import io
        import urllib.error
        from types import SimpleNamespace
        # Import the clean-room settings before admin's import-time app factory.
        from tests.admin_test_env import ADMIN_TEST_PUBLIC_ORIGIN
        from admin_service import main as admin
        from admin_service.config import AdminSettings
        from admin_service.services.runtime_control import DockerRuntimeService
        from app import main as frontend
        from app.config import HistoryConfig
        from app.services.history_backend import HistoryBackendClient
        from history_service import main as producer
        from history_service.collector import HistoryCollector
        from history_service.config import HistorySettings

        collector = HistoryCollector(HistorySettings(sqlite_path=str(database)), store)
        before = inventory(database.parent)
        with patch.object(producer, "store", store), patch.object(producer, "collector", collector):
            live_code, live_body = asyncio.run(self._request(producer.app, "/livez"))
            health_code, health_body = asyncio.run(self._request(producer.app, "/healthz"))
        self.assertEqual(live_code, 200)
        self.assertEqual(health_code, 503 if paused else 200)
        if paused:
            self.assertIs(json.loads(health_body)["ready"], False)
            asyncio.run(collector.start())
            self.assertIsNone(collector._task, "Observation must not start a paused collector")
        else:
            self.assertEqual(json.loads(health_body)["status"], "ok")

        streams = []

        def transport(request, *, timeout):
            self.assertGreater(timeout, 0)
            self.assertTrue(request.full_url.endswith("/healthz"))
            stream = io.BytesIO(health_body)
            streams.append(stream)
            if health_code == 503:
                raise urllib.error.HTTPError(request.full_url, 503, "synthetic pause", {}, stream)
            stream.headers = {}
            return stream

        backend = HistoryBackendClient(HistoryConfig(service_url="http://synthetic.test"))
        settings = AdminSettings(auth_mode="network", public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
                                 auto_stop_seconds=0, container_version_probe_timeout_seconds=0.25)
        runtime = DockerRuntimeService(settings)
        container = runtime._build_status_payload("history", {"State": "running", "Status": "Up (healthy)"})
        with patch("app.services.history_backend.urllib.request.urlopen", side_effect=transport):
            with patch.object(frontend, "get_history_backend", return_value=backend):
                main_code, main_body = asyncio.run(self._request(frontend.app, "/api/history/status"))
            with patch.object(runtime, "_probe_running_version", return_value="0.23.0"):
                runtime._annotate_versions([container])
        self.assertTrue(streams and all(stream.closed for stream in streams))
        self.assertEqual(main_code, 200)
        main_status = json.loads(main_body)
        self.assertIs(main_status["available"], not paused)
        if paused:
            self.assertIs(main_status["ready"], False)
            self.assertEqual(main_status["counts"], {})
            self.assertEqual(main_status["scopes"], [])
        else:
            self.assertNotIn("recovery_required", main_status)
        # Only Docker inventory is replaced; readiness came from actual history ASGI bytes.
        with (patch.object(runtime, "status_payload", return_value={"available": True, "containers": [container]}),
              patch.object(admin, "get_admin_settings", return_value=settings),
              patch.object(admin, "get_runtime_service", return_value=runtime),
              patch.object(admin, "get_release_status_service", return_value=SimpleNamespace(snapshot=lambda: {}))):
            admin_code, admin_body = asyncio.run(self._request(admin.create_app(), "/api/admin/runtime"))
        self.assertEqual(admin_code, 200)
        public_container = json.loads(admin_body)["runtime"]["containers"][0]
        self.assertIs(public_container["running"], True)
        self.assertIs(public_container["ready"], not paused)
        if paused:
            self.assertEqual(public_container["lifecycle_label"], "Recovery Required")
        else:
            self.assertNotIn("recovery_required", public_container)
        for raw in (live_body, health_body, main_body, admin_body):
            for forbidden in (str(database).encode(), b"composition-private-sentinel", b"recovery-operation.json",
                              b"intent.json", b"committed-", b"sha256"):
                self.assertNotIn(forbidden, raw)
        self.assertEqual(inventory(database.parent), before, "Status must not mutate recovery inventory")

    def test_actual_terminal_completion_keeps_old_store_latched_and_fresh_status_ready(self):
        from history_service.store import HistoryStore

        with tempfile.TemporaryDirectory() as temporary:
            db, args, payload = self.fixture(temporary)
            old = HistoryStore(str(db))
            self._observe(db, old, paused=True)
            self.assertEqual(self._cli(args)[0], 5)
            root = recovery_state.recovery_path(db)
            rid = json.loads((root / "intent.json").read_bytes())["id"]
            originals = {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_nlink) for p in root.iterdir()}
            self.assertEqual(self._cli(self.selections(db, rid))[0], 0)
            for _ in range(2):
                self._observe(db, old, paused=True)
            fresh = HistoryStore(str(db))
            self._observe(db, fresh, paused=False)
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute("SELECT system_id,value_integer FROM metric_samples").fetchall(),
                                 [("synthetic-selected", 37)])
            archive = db.with_name(db.name + ".recovery-archive-" + rid)
            for name, facts in originals.items():
                retained = archive / name
                self.assertEqual((retained.read_bytes(), retained.stat().st_ino, retained.stat().st_nlink), facts)

    def test_real_late_failure_states_project_persisted_decision_not_cli_exit(self):
        from history_service import explicit_recovery as api
        from history_service.store import HistoryStore

        for fault in ("schema-five", "fsync-17", "fsync-18", "fsync-19", "fsync-20"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                db, args, payload = self.fixture(temporary)
                self.assertEqual(self._cli(args)[0], 5)
                root = recovery_state.recovery_path(db)
                rid = json.loads((root / "intent.json").read_bytes())["id"]
                original = {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_nlink) for p in root.iterdir()}
                count = 0
                real = api._validate_candidate if fault == "schema-five" else os.fsync
                cut = 5 if fault == "schema-five" else int(fault.split("-")[1])

                def fail(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == cut:
                        raise OSError("composition-private-sentinel")
                    return real(*args, **kwargs)

                with patch.object(api if fault == "schema-five" else os,
                                  "_validate_candidate" if fault == "schema-five" else "fsync", side_effect=fail):
                    code, result = self._cli(self.selections(db, rid))
                self.assertEqual(code, 5)
                self.assertEqual(count, cut)
                paused = fault not in ("fsync-19", "fsync-20")
                self.assertEqual(result["state"], "refused" if paused else "outcome-unknown")
                if paused:
                    with patch.object(sqlite3, "connect", side_effect=AssertionError("Original evidence SQLite open")):
                        fresh = HistoryStore(str(db))
                        self._observe(db, fresh, paused=True)
                else:
                    self._observe(db, HistoryStore(str(db)), paused=False)
                archive = db.with_name(db.name + ".recovery-archive-" + rid)
                for name, facts in original.items():
                    retained = archive / name
                    self.assertEqual((retained.read_bytes(), retained.stat().st_ino, retained.stat().st_nlink), facts)

    def test_corrupted_terminal_receipts_refuse_without_http_evidence_leakage(self):
        from history_service.store import HistoryStore

        for fault in ("missing-terminal", "unsafe-terminal", "malformed-completion", "same-byte-inode",
                      "unknown-archive", "multiple-archive", "parent-alias"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                db, args, payload = self.fixture(temporary)
                self.assertEqual(self._cli(args)[0], 5)
                root = recovery_state.recovery_path(db)
                rid = json.loads((root / "intent.json").read_bytes())["id"]
                self.assertEqual(self._cli(self.selections(db, rid))[0], 0)
                archive = db.with_name(db.name + ".recovery-archive-" + rid)
                terminal = next(archive.glob("committed-*"))
                if fault == "missing-terminal":
                    terminal.rmdir()
                elif fault == "unsafe-terminal":
                    terminal.chmod(0o755)
                elif fault == "malformed-completion":
                    completion = archive / "finalized.json"
                    value = json.loads(completion.read_bytes())
                    value["composition-private-sentinel"] = "credential-like-synthetic-marker"
                    completion.write_text(json.dumps(value))
                    # Bind the malformed bytes to the terminal, so validation must reject structure.
                    terminal.rename(archive / f"committed-{digest(completion)}-{completion.stat().st_dev}-{completion.stat().st_ino}")
                elif fault == "same-byte-inode":
                    completion = archive / "finalized.json"
                    raw = completion.read_bytes()
                    completion.rename(Path(temporary) / "retained-completion")
                    completion.write_bytes(raw)
                    completion.chmod(0o600)
                elif fault == "unknown-archive":
                    (archive / "composition-private-sentinel").write_bytes(b"synthetic")
                elif fault == "multiple-archive":
                    db.with_name(db.name + ".recovery-archive-" + "0" * 32).mkdir(mode=0o700)
                else:
                    alias = Path(temporary) / "alias"
                    alias.symlink_to(db.parent, target_is_directory=True)
                    db = alias / db.name
                paused = fault != "parent-alias"
                if paused:
                    with patch.object(sqlite3, "connect", side_effect=AssertionError("Original evidence SQLite open")):
                        self._observe(db, HistoryStore(str(db)), paused=True)
                else:
                    self._observe(db, HistoryStore(str(db)), paused=False)


if __name__ == "__main__":
    unittest.main()
