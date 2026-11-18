"""Synthetic-only benchmark contracts; never use a maintainer database."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/benchmark_full_backup.py"


class FullBackupBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "synthetic FULL benchmark runner is missing")
        from scripts import benchmark_full_backup
        self.b = benchmark_full_backup
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.enterContext(self.b.owned_run(Path(self.tmp.name), self.b.MIB))

    def test_direct_workers_refuse_unowned_roots_without_mutation(self):
        for phase in self.b.PHASES:
            for alias in ("plain", "root-link", "config-link", "hardlink"):
                with self.subTest(phase=phase, alias=alias), tempfile.TemporaryDirectory() as name:
                    root = Path(name)
                    protected = root / "protected"
                    protected.write_bytes(b"synthetic sentinel\x00unchanged")
                    config = root / "config"
                    config.mkdir()
                    target = config / "config.yaml"
                    if alias == "config-link":
                        target.symlink_to(protected)
                    elif alias == "hardlink":
                        os.link(protected, target)
                    else:
                        target.write_bytes(protected.read_bytes())
                    supplied = root
                    if alias == "root-link":
                        supplied = root / "alias"
                        supplied.symlink_to(root, target_is_directory=True)
                    before = {p.relative_to(root): p.read_bytes() for p in (protected, target)}
                    request = {"root": str(supplied), "target_bytes": self.b.MIB,
                               "timeout": 5, "seed": 397, "passphrase": "unit-only",
                               "supervisor_token": "user-created-is-not-authority"}
                    proc = subprocess.run([sys.executable, "-B", str(SCRIPT),
                                           "--worker-phase", phase], input=json.dumps(request),
                                          capture_output=True, text=True, timeout=15)
                    self.assertEqual({p: (root / p).read_bytes() for p in before}, before)
                    self.assertFalse((root / "history.db").exists())
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(proc.stdout, "")

    def test_worker_large_request_cannot_reach_operation(self):
        request = {"root": str(self.root), "target_bytes": 2 * self.b.GIB,
                   "timeout": 5, "seed": 397, "passphrase": "unit-only"}
        with patch("sys.stdin", io.StringIO(json.dumps(request))), \
                patch.object(self.b.resource, "setrlimit"), \
                patch.object(self.b, "operation", return_value={}) as operation:
            self.assertEqual(self.b.worker("fixture"), 1)
        operation.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_owned_run_rejects_aliases_before_any_phase(self):
        protected = Path(self.tmp.name) / "sentinel"
        protected.write_bytes(b"synthetic protected bytes")
        for alias in ("file-symlink", "dir-symlink", "hardlink"):
            target = self.root / "alias"
            if alias == "dir-symlink":
                target.symlink_to(protected.parent, target_is_directory=True)
            elif alias == "file-symlink":
                target.symlink_to(protected)
            else:
                os.link(protected, target)
            try:
                for phase in self.b.PHASES:
                    with self.subTest(alias=alias, phase=phase):
                        with self.assertRaises(ValueError):
                            self.b.operation(phase, {"root": str(self.root),
                                                    "target_bytes": self.b.MIB})
                        self.assertEqual(protected.read_bytes(), b"synthetic protected bytes")
                        self.assertFalse((self.root / "config").exists())
                        self.assertFalse((self.root / "history.db").exists())
            finally:
                target.unlink()

    def test_owned_run_proof_is_revoked_on_exit(self):
        with self.b.owned_run(Path(self.tmp.name), self.b.MIB) as run:
            self.b.admit_run(run)
        with self.assertRaises(ValueError):
            self.b.admit_run(run)

    def test_owned_worker_checks_timeout_and_binding_before_operation(self):
        for timeout, size in ((0, self.b.MIB), (float("nan"), self.b.MIB),
                              (601, self.b.MIB), (5, 2 * self.b.GIB)):
            request = {"root": str(self.root), "target_bytes": size, "timeout": timeout}
            with patch("sys.stdin", io.StringIO(json.dumps(request))), \
                    patch.object(self.b, "operation") as operation:
                self.assertEqual(self.b.worker("fixture"), 1)
                operation.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_owned_large_run_requires_opt_in_and_codec_before_scratch(self):
        for allowed in (False, True):
            with patch.object(shutil, "which", return_value=None), \
                    patch.object(tempfile, "TemporaryDirectory") as allocation:
                with self.assertRaises(ValueError):
                    with self.b.owned_run(self.root, 2 * self.b.GIB, allow_large=allowed):
                        self.fail("large admission bypass")
                allocation.assert_not_called()

    def test_config_helper_never_overwrites_existing_sentinel(self):
        (self.root / "config").mkdir()
        target = self.root / "config/config.yaml"
        target.write_bytes(b"synthetic config sentinel")
        with self.assertRaises((ValueError, FileExistsError)):
            self.b.prepare_config(self.root)
        self.assertEqual(target.read_bytes(), b"synthetic config sentinel")

    def test_fixture_reproducible_real_sqlite_with_bounded_rows(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            first = self.b.generate_fixture(self.root / "a.db", 1024 * 1024, seed=397)
            second = self.b.generate_fixture(self.root / "b.db", 1024 * 1024, seed=397)
        self.assertEqual(first, second)
        self.assertGreater(first["rows"]["slot_events"], 0)
        self.assertEqual(first["rows"]["slot_events"], first["rows"]["metric_samples"])
        self.assertGreaterEqual(first["logical_bytes"], 1024 * 1024)
        self.assertLess(first["logical_bytes"], 2 * 1024 * 1024)
        self.assertGreater(first["allocated_bytes"], 0)
        with sqlite3.connect(self.root / "a.db") as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone(), ("ok",))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0], first["rows"]["slot_events"])
            self.assertLessEqual(db.execute("SELECT MAX(length(details_json)) FROM slot_events").fetchone()[0], 4096)

    def run_cli(self, argv, *, cwd, timeout):
        # The path-only CLI probe stays in this session. Kill its descendants
        # before the caller removes case scratch, including on timeout/assertion.
        with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()

    def test_cli_timeout_reaps_process_group_and_cleans_case_scratch(self):
        import time

        marker = self.root / "late-cli-write"
        processes = []
        original_popen = subprocess.Popen
        original_communicate = subprocess.Popen.communicate
        with tempfile.TemporaryDirectory(dir=self.root) as name:
            case_root = Path(name)
            ready = case_root / "ready"
            child = (f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); "
                     f"time.sleep(1); Path({str(marker)!r}).touch()")
            code = ("import subprocess,sys,time; "
                    f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)")

            def observed(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                processes.append(process)
                return process

            def after_ready(process, *args, **kwargs):
                if kwargs.get("timeout") is not None:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), "synthetic descendant did not start")
                    kwargs["timeout"] = 0.05
                return original_communicate(process, *args, **kwargs)

            with patch.object(subprocess, "Popen", side_effect=observed), \
                    patch.object(original_popen, "communicate", after_ready):
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.run_cli([sys.executable, "-c", code], cwd=case_root, timeout=10)
            self.assertIsNotNone(processes[0].returncode, "CLI supervisor was not reaped")
        self.assertFalse(case_root.exists())
        time.sleep(1.1)
        self.assertFalse(marker.exists(), "CLI descendant survived timeout cleanup")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_relative_output_cli_outside_repository(self):
        # Exercise the real CLI, root resolution, owned_run and phase-directory
        # cleanup. Only phase measurement is replaced: full production codec
        # coverage lives in the dedicated self-test contracts below.
        probe = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from scripts import benchmark_full_backup as b
expected_parent = Path(sys.argv[sys.argv.index("--output-root") + 1]).resolve(strict=True)

def path_phase(argv, request, scratch, *, timeout, owned_phase):
    root = Path(request["root"])
    assert root.is_absolute() and scratch.is_absolute()
    assert root.parent == expected_parent
    assert scratch.parent == root
    b.admit_run(root, request["target_bytes"])
    (scratch / "synthetic-phase-sentinel").write_text(owned_phase)
    (root / (owned_phase + ".synthetic")).touch()
    return {{"state": "complete", "input_bytes": 0, "output_bytes": 0}}

b.measure_process = path_phase
raise SystemExit(b.main())
"""
        for relative in (".", "child space/..", "child space"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(dir=self.root) as name:
                case_root = Path(name)
                child = case_root / "child space"
                child.mkdir(mode=0o700)
                proc = self.run_cli([sys.executable, "-B", "-c", probe, "--self-test",
                                     "--format", "tar.zst-stream", "--output-root", relative],
                                    cwd=case_root, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                report = json.loads(proc.stdout)
                self.assertEqual([p["phase"] for p in report["phases"]], list(self.b.PHASES))
                self.assertTrue(all(p["state"] == "complete" for p in report["phases"]))
                self.assertTrue(report["scratch_cleaned"])
                self.assertEqual(list(child.iterdir()), [])
                self.assertEqual(list(case_root.iterdir()), [child])
            self.assertFalse(case_root.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_scaled_production_ceiling_export_and_size_prevalidation(self):
        from history_service import system_backup as sb
        from tests.test_system_backup import SystemBackupServiceTests
        codec = SystemBackupServiceTests("test_fake_7z_archive_does_not_store_cleartext_passphrase")
        # Real generator and exporter, only ceiling scaled and test codec used.
        # No compression timings or multi-GiB allocation.
        with patch.object(sb, "MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES", 2 * self.b.MIB):
            target = self.b.MIB
            for offset in (1, 4095, 4096, self.b.MIB):
                with self.subTest(offset=offset), patch.object(sqlite3, "connect", side_effect=AssertionError("allocated before rejection")):
                    with self.assertRaises(ValueError):
                        self.b.generate_fixture(self.root / "unsupported.db", target + offset)
                    self.assertFalse((self.root / "unsupported.db").exists())
            fixture = self.b.generate_fixture(self.root / "history.db", target)
            self.assertGreaterEqual(fixture["logical_bytes"], target)
            self.assertLessEqual(fixture["logical_bytes"], 2 * self.b.MIB)
            self.assertEqual(fixture["member_limit_bytes"], 2 * self.b.MIB)
            with patch.object(sb.SystemBackupService, "_run_7z_command", side_effect=codec._fake_7z_command):
                result = self.b.operation("create", {"root": str(self.root), "passphrase": "unit-only"})
                self.assertGreater(result["output_bytes"], 0)

    def test_production_maximum_metadata_and_preallocation(self):
        from history_service import system_backup as sb
        maximum = sb.MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES - self.b.MIB
        self.assertEqual(self.b.fixture_limits()["maximum_target_bytes"], maximum)
        for offset in (1, 4095, 4096, self.b.MIB):
            with self.subTest(offset=offset), patch.object(sqlite3, "connect", side_effect=AssertionError("large allocation")):
                with self.assertRaises(ValueError):
                    self.b.generate_fixture(self.root / "large.db", maximum + offset)
        for size in (maximum, sb.MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES):
            sb.SystemBackupService._validate_7z_listed_entries(
                [{"Path": "history/history.db", "Size": str(size), "Packed Size": str(size)}],
                archive_size=size, member_limit=sb.MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
                expanded_limit=sb.MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES)
        size = sb.MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES + 4096
        with self.assertRaises(ValueError):
            sb.SystemBackupService._validate_7z_listed_entries(
                [{"Path": "history/history.db", "Size": str(size), "Packed Size": str(size)}],
                archive_size=size, member_limit=sb.MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
                expanded_limit=sb.MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES)

    def test_row_ceiling_and_page_ceiling_clean_failed_fixture(self):
        with patch.object(self.b, "MAX_FIXTURE_ROWS", 65, create=True):
            with self.assertRaisesRegex(ValueError, "row"):
                self.b.generate_fixture(self.root / "rows.db", self.b.MIB)
        self.assertFalse((self.root / "rows.db").exists())
        from history_service import system_backup as sb
        with patch.object(sb, "MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES", self.b.MIB), patch.object(self.b, "OVERSHOOT_RESERVE_BYTES", 0, create=True):
            with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                self.b.generate_fixture(self.root / "pages.db", self.b.MIB)
        self.assertFalse((self.root / "pages.db").exists())

    def test_output_root_privacy_admission_unchanged(self):
        public = self.root / "public"
        public.mkdir(mode=0o755)
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        for path in (public, link, self.root / "missing"):
            with self.subTest(path=path.name), patch.object(self.b, "benchmark", side_effect=AssertionError("allocation")), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    self.b.main(["--self-test", "--output-root", str(path)])
                self.assertEqual(error.exception.code, 2)

    def test_large_cli_maps_near_limit_and_prevalidates_before_codec(self):
        from history_service import system_backup as sb
        with patch.object(shutil, "which", return_value=None), patch("sys.stdout", new_callable=io.StringIO) as out:
            code = self.b.main(["--size-gib", "4", "--allow-large", "--output-root", str(self.root)])
        self.assertEqual(code, 2)
        report = json.loads(out.getvalue())
        self.assertEqual(report["target_bytes"], sb.MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES - self.b.MIB)
        self.assertEqual(report["state"], "blocked")
        with patch.object(sb, "MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES", 2 * self.b.MIB), patch.object(shutil, "which", side_effect=AssertionError("codec lookup before validation")), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                self.b.main(["--size-gib", "2", "--allow-large", "--output-root", str(self.root)])
            self.assertEqual(error.exception.code, 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_fixture_entropy_allocations_are_bounded(self):
        import random
        original = random.Random.randbytes
        sizes = []

        def bounded(rng, size):
            sizes.append(size)
            self.assertLessEqual(size, 4096)
            return original(rng, size)

        with patch.object(random.Random, "randbytes", bounded):
            self.b.generate_fixture(self.root / "bounded.db", 1024 * 1024)
        self.assertTrue(sizes)

    def test_timeout_kills_descendants(self):
        marker = self.root / "late-write"
        child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
        code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(5)"
        result = self.b.measure_process([sys.executable, "-c", code], {}, self.root, timeout=0.3)
        self.assertEqual(result["state"], "timeout")
        import time
        time.sleep(1.1)
        self.assertFalse(marker.exists())

    def test_owned_worker_timeout_kills_descendants(self):
        import time
        marker = self.root / "late-owned-write"
        child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"

        # The worker hides every exception by design, so an early exit only
        # shows up as "failed". These notes say how far it got. They go
        # through one descriptor opened before the fork and hold fixed words
        # and exception type names only.
        notes = tempfile.TemporaryFile()
        self.addCleanup(notes.close)
        real_worker = self.b.worker

        def note(text):
            try:
                os.write(notes.fileno(), text + b"\n")
            except BaseException:
                pass

        def noted_worker(phase):
            note(b"worker-start")
            try:
                code = real_worker(phase)
            except BaseException as error:
                note(b"worker-raised " + type(error).__name__.encode())
                raise
            note(b"worker-returned %d" % code)
            return code

        def blocked_operation(*args):
            note(b"operation-start")
            try:
                subprocess.Popen([sys.executable, "-c", child])
            except BaseException as error:
                note(b"operation-raised " + type(error).__name__.encode())
                raise
            note(b"operation-spawned")
            time.sleep(5)

        def diagnosis(result):
            notes.seek(0)
            with open("/proc/self/status", encoding="ascii") as status:
                memory = [line.split()[1] for line in status if line.startswith(("VmSize:", "VmRSS:"))]
            return (
                f"exit_code={result['exit_code']} "
                f"supervisor_wall_seconds={result['supervisor_wall_seconds']:.3f} "
                f"runner_vm_size_kib={memory[0]} runner_vm_rss_kib={memory[1]} "
                f"worker_notes={notes.read().decode('ascii', 'replace').splitlines()}"
            )

        request = {"root": str(self.root), "target_bytes": self.b.MIB, "timeout": 5}
        with patch.object(self.b, "operation", side_effect=blocked_operation), \
                patch.object(self.b, "worker", noted_worker):
            result = self.b.measure_process([], request, self.root, timeout=0.3, owned_phase="fixture")
        self.assertEqual(result["state"], "timeout", diagnosis(result))
        time.sleep(1.1)
        self.assertFalse(marker.exists())

    def test_owned_worker_timeout_before_session_creation_is_bounded(self):
        import time
        original = os.setsid

        def delayed_session():
            time.sleep(0.6)
            original()

        request = {"root": str(self.root), "target_bytes": self.b.MIB, "timeout": 5, "seed": 397}
        with patch.object(os, "setsid", side_effect=delayed_session):
            result = self.b.measure_process([], request, self.root, timeout=0.05, owned_phase="fixture")
        self.assertEqual(result["state"], "timeout")
        self.assertFalse((self.root / "history.db").exists(), "worker wrote after timeout")

    def test_cancelled_child_is_reaped(self):
        original = subprocess.Popen.wait
        calls = []

        def interrupt_once(process, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return original(process, *args, **kwargs)

        with patch.object(subprocess.Popen, "wait", interrupt_once):
            result = self.b.measure_process([sys.executable, "-c", "import time; time.sleep(5)"], {}, self.root, timeout=5)
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNotNone(result["exit_code"])

    def test_seed_changes_content(self):
        a = self.b.generate_fixture(self.root / "a.db", 1024 * 1024, seed=1)
        b = self.b.generate_fixture(self.root / "b.db", 1024 * 1024, seed=2)
        self.assertNotEqual(a["sha256"], b["sha256"])

    def test_digest_never_uses_unbounded_reads(self):
        class Bounded(io.BytesIO):
            def read(inner, size=-1):
                self.assertGreater(size, 0)
                self.assertLessEqual(size, 1024 * 1024)
                return super().read(size)
        with patch.object(Path, "open", return_value=Bounded(b"abc")):
            self.assertEqual(self.b.file_digest(Path("unused")), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_fixture_refuses_existing_target_and_invalid_size(self):
        target = self.root / "existing.db"
        target.write_text("sentinel")
        with self.assertRaises(FileExistsError):
            self.b.generate_fixture(target, 1024 * 1024)
        self.assertEqual(target.read_text(), "sentinel")
        for size in (0, -1, 5 * 1024**3):
            with self.assertRaises(ValueError):
                self.b.generate_fixture(self.root / "new.db", size)
        self.assertFalse((self.root / "new.db").exists())

    def test_cli_invalid_options_never_reach_fixture_allocation(self):
        for argv in ([], ["--size-gib", "2"], ["--self-test", "--workers", "2"]):
            with self.subTest(argv=argv), patch.object(self.b, "benchmark", side_effect=AssertionError("implicit allocation")), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    self.b.main(argv)
                self.assertEqual(error.exception.code, 2)

    def test_large_and_worker_admission_before_allocation(self):
        for argv in ([], ["--size-gib", "2"], ["--self-test", "--workers", "2"],
                     ["--self-test", "--timeout", "0"], ["--self-test", "--size-gib", "4"]):
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), *argv], capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0, argv)
        with patch.object(shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            with self.assertRaisesRegex(ValueError, "space"):
                self.b.preflight(self.root, 1024 * 1024)

    def test_phase_failure_timeout_and_false_success_are_not_complete(self):
        for code, expected in [
            ("raise SystemExit(9)", "failed"),
            ("import time; time.sleep(5)", "timeout"),
            ('print(\'{"state":"complete"}\')', "failed"),
        ]:
            result = self.b.measure_process([sys.executable, "-c", code], {}, self.root, timeout=0.2)
            self.assertEqual(result["state"], expected)
            self.assertIsNone(result["output_bytes"])
            self.assertNotIn("passphrase", json.dumps(result))

    def test_supervisor_capture_has_a_fixed_memory_budget(self):
        from tests import heap_probe
        heap_probe.start()
        try:
            result = self.b.measure_process(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * (8 * 1024**2))"],
                {}, self.root, timeout=5,
            )
            _, peak = heap_probe.get_traced_memory()
        finally:
            heap_probe.stop()
        self.assertEqual(result["state"], "failed")
        self.assertLess(peak, 2 * 1024**2)

    def test_passphrase_only_in_stdin_and_errors_redacted(self):
        secret = "synthetic-test-canary-397"
        code = "import sys; value=sys.stdin.read(); print(value); raise SystemExit(3)"
        original = subprocess.Popen
        commands = []

        def observed(argv, **kwargs):
            commands.append(argv)
            self.assertNotIn(secret, json.dumps(argv))
            self.assertNotIn(secret, json.dumps(kwargs.get("env", {})))
            return original(argv, **kwargs)

        with patch.object(subprocess, "Popen", observed):
            result = self.b.measure_process([sys.executable, "-c", code], {"passphrase": secret}, self.root, timeout=5)
        self.assertTrue(commands)
        self.assertEqual(result["state"], "failed")
        self.assertNotIn(secret, json.dumps(result))

    def test_self_test_reports_blocked_or_real_phases_and_cleans_scratch(self):
        proc = subprocess.run([sys.executable, "-B", str(SCRIPT), "--self-test", "--output-root", str(self.root)], capture_output=True, text=True, timeout=120)
        self.assertIn(proc.returncode, (0, 2), proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["synthetic_only"])
        self.assertFalse(report["release_acceptance"])
        self.assertEqual(report["worker_cap"], 1)
        self.assertEqual(
            {c["profile"]: c["state"] for c in report["candidate_profiles"]},
            {"7z": "measured", "tar.zst-stream": "not_run"},
        )
        self.assertEqual([p["phase"] for p in report["phases"]], ["fixture", "create", "inspect", "verify", "extract"])
        self.assertEqual(report["phases"][0]["state"], "complete")
        self.assertEqual(list(self.root.iterdir()), [])
        for phase in report["phases"]:
            if phase["state"] == "complete":
                self.assertGreaterEqual(phase["wall_seconds"], 0)
                self.assertGreaterEqual(phase["cpu_seconds"], 0)
                self.assertGreater(phase["peak_rss_bytes"], 0)
        if shutil.which("7z"):
            self.assertEqual(proc.returncode, 0, report)
            self.assertTrue(all(p["state"] == "complete" for p in report["phases"]))
        else:
            self.assertTrue(all(p["state"] == "blocked" for p in report["phases"][1:]))

    def test_stream_format_self_test_runs_without_7z_and_cleans_scratch(self):
        # #397: the fast format needs no external codec, so its phases always run.
        proc = subprocess.run([sys.executable, "-B", str(SCRIPT), "--self-test", "--format", "tar.zst-stream",
                               "--output-root", str(self.root)], capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["profile"]["codec"], "zstd")
        self.assertEqual(report["worker_cap"], 2)
        self.assertTrue(all(p["state"] == "complete" for p in report["phases"]), report["phases"])
        create = next(p for p in report["phases"] if p["phase"] == "create")
        self.assertGreater(create["output_bytes"], 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_production_api_wiring_with_existing_test_codec_not_a_benchmark(self):
        # The repository's test codec is only a unit-test seam. It is never
        # available to the runner and supplies no compression/performance evidence.
        from history_service.system_backup import SystemBackupService
        from tests.test_system_backup import SystemBackupServiceTests
        codec = SystemBackupServiceTests("test_fake_7z_archive_does_not_store_cleartext_passphrase")
        fixture = self.b.generate_fixture(self.root / "history.db", 1024 * 1024)
        request = {"root": str(self.root), "passphrase": "ephemeral-unit-only"}
        old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(self.root)
        self.addCleanup(setattr, tempfile, "tempdir", old_tempdir)
        with patch.object(SystemBackupService, "_run_7z_command", side_effect=codec._fake_7z_command):
            created = self.b.operation("create", request)
            self.assertGreater(created["output_bytes"], 0)
            self.assertGreater(created["input_bytes"], fixture["logical_bytes"])
            config_before = (self.root / "config/config.yaml").read_bytes()
            for phase in ("inspect", "verify", "extract"):
                result = self.b.operation(phase, request)
                self.assertEqual((self.root / "config/config.yaml").read_bytes(), config_before)
                self.assertEqual(result["input_bytes"], created["output_bytes"])
                if phase == "extract":
                    self.assertEqual(result["rows"], fixture["rows"])
                with self.assertRaises(ValueError):
                    self.b.operation(phase, {**request, "passphrase": "wrong"})
                self.assertFalse(any(p.name.startswith("truenas-jbod-ui-") for p in self.root.iterdir()))

    @unittest.skipUnless(shutil.which("7z"), "real 7z executable unavailable")
    def test_real_export_wrong_passphrase_corruption_and_cleanup(self):
        self.b.generate_fixture(self.root / "history.db", 1024 * 1024)
        with patch.dict(os.environ, {"TMPDIR": str(self.root)}):
            old_tempdir = tempfile.tempdir
            tempfile.tempdir = str(self.root)
            self.addCleanup(setattr, tempfile, "tempdir", old_tempdir)
            request = {"root": str(self.root), "passphrase": "  ephemeral 雪 test-only   "}
            self.b.operation("create", request)
            for operation in ("inspect", "verify", "extract"):
                result = self.b.operation(operation, request)
                if operation == "extract":
                    self.assertGreater(result["rows"]["slot_events"], 0)
                self.assertFalse(any(p.name.startswith("truenas-jbod-ui-") for p in self.root.iterdir()))
                with self.assertRaises(ValueError):
                    self.b.operation(operation, {**request, "passphrase": "wrong"})
                self.assertFalse(any(p.name.startswith("truenas-jbod-ui-") for p in self.root.iterdir()))
            with (self.root / "bundle.7z").open("r+b") as out:
                out.seek(40)
                out.write(b"corrupt")
            with self.assertRaises(ValueError):
                self.b.operation("verify", request)


class BenchmarkSourceProvenanceTests(unittest.TestCase):
    """The report fingerprints every module that defines the benchmarked export."""

    MODULES = (
        "history_service/system_backup.py",
        "history_service/backup_7z.py",
        "history_service/backup_json.py",
        "history_service/backup_archive/stream_envelope.py",
    )

    def setUp(self):
        from scripts import benchmark_full_backup
        self.b = benchmark_full_backup
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tree = Path(tmp.name) / "tree"
        for name in self.MODULES:
            target = self.tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        self.scratch = Path(tmp.name) / "scratch"
        self.scratch.mkdir(mode=0o700)

    def report_provenance(self) -> dict:
        # Synthetic copy of the source tree; no phase runs and no git read.
        with patch.object(self.b, "ROOT", self.tree), patch.object(self.b, "PHASES", ()), \
                patch.object(self.b.subprocess, "check_output", return_value="0" * 40 + "\n"):
            report = self.b.benchmark(self.scratch, self.b.MIB, seed=1, timeout=5)
        self.assertEqual(report["phases"], [])
        return {key: value for key, value in report.items() if key.startswith("source_")}

    def flip_one_byte(self, name: str) -> None:
        path = self.tree / name
        data = bytearray(path.read_bytes())
        data[-1] ^= 0x01
        path.write_bytes(bytes(data))

    def test_one_byte_change_in_backup_7z_changes_the_report_fingerprint(self):
        before = self.report_provenance()
        self.flip_one_byte("history_service/backup_7z.py")
        after = self.report_provenance()
        self.assertNotEqual(before, after, "a backup_7z.py change left every report source digest unchanged")
        # The original field keeps its meaning: system_backup.py only.
        self.assertEqual(before["source_file_sha256"], after["source_file_sha256"])
        changed = {k for k in after["source_files_sha256"] if before["source_files_sha256"][k] != after["source_files_sha256"][k]}
        self.assertEqual(changed, {"history_service/backup_7z.py"})

    def test_every_module_change_is_visible_and_old_field_is_unchanged_in_meaning(self):
        for name in self.MODULES:
            with self.subTest(module=name):
                before = self.report_provenance()
                self.assertEqual(set(before["source_files_sha256"]), set(self.MODULES))
                self.assertEqual(before["source_file_sha256"],
                                 before["source_files_sha256"]["history_service/system_backup.py"])
                self.flip_one_byte(name)
                after = self.report_provenance()
                changed = {k for k in after["source_files_sha256"]
                           if before["source_files_sha256"][k] != after["source_files_sha256"][k]}
                self.assertEqual(changed, {name})
                self.assertEqual(before["source_file_sha256"] != after["source_file_sha256"],
                                 name == "history_service/system_backup.py")

    def test_fingerprinted_modules_cover_backup_modules_imported_by_system_backup(self):
        import ast
        tree = ast.parse((ROOT / "history_service/system_backup.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("history_service"):
                for alias in node.names:
                    for dotted in (f"{node.module}.{alias.name}", node.module):
                        path = Path(*dotted.split("."))
                        if path.name.startswith("backup") or path.parent.name.startswith("backup"):
                            if (ROOT / path.with_suffix(".py")).is_file():
                                imported.add(path.with_suffix(".py").as_posix())
                                break
        self.assertTrue(imported)
        self.assertLessEqual(imported, set(self.b.SOURCE_MODULES))
        self.assertEqual(set(self.b.SOURCE_MODULES), set(self.MODULES))


if __name__ == "__main__":
    unittest.main()
