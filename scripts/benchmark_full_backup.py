#!/usr/bin/env python3
"""Opt-in, synthetic-only FULL backup measurement precursor (#397).

Linux only (process groups, rlimits, wait4 resource accounting). No production
restore, network, service startup, existing database input, or format changes.
--self-test runs a 1 MiB sample. 2/4 GiB cells require --allow-large --output-root.
The 4 GiB cell targets the production member ceiling minus 1 MiB, not 4 GiB
as a minimum. SQLite pages are hard-capped, and actual bytes are reported.
JSON goes to stdout; all synthetic databases/archives are removed, even on failure.
Workers are forked by a single-threaded supervisor with in-memory scratch ownership.
Direct --worker-phase execution is refused; user-created marker files are not proof.
A nonzero exit and explicit blocked cells are expected when 7z is unavailable.

The phases are whole production API operations, not exclusive compressor spans:
create includes export validation; inspect includes extraction and validation;
verify is restore preflight, not activation; extract includes guarded archive
admission and byte/row verification. Never add their timings as pipeline latency.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import random
import resource
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MIB = 1024**2
GIB = 1024**3
CHUNK_BYTES = MIB
BATCH_ROWS = 64
MAX_TARGET_BYTES = 4 * GIB
PAGE_BYTES = 4096
OVERSHOOT_RESERVE_BYTES = MIB
MAX_FIXTURE_ROWS = 1_000_000  # per populated table; two rows per iteration
MAX_TIMEOUT = 600
PROFILE = {"format": "7z", "encrypted": True, "codec": "LZMA2", "level": 5, "workers": 1}
PHASES = ("fixture", "create", "inspect", "verify", "extract")


# This registry is inherited only by forked children. Nothing on stdin, argv,
# the filesystem or in the environment can register a caller-supplied root.
_OWNED_RUNS = {}


@contextmanager
def owned_run(parent: Path, target_bytes: int, *, allow_large: bool = False):
    preflight(parent, target_bytes)
    if target_bytes != MIB and (not allow_large or not shutil.which("7z")):
        raise ValueError("large fixture requires explicit admission and real codec")
    with tempfile.TemporaryDirectory(prefix="full-backup-benchmark-", dir=parent) as name:
        root = Path(name).resolve(strict=True)
        info = root.lstat()
        _OWNED_RUNS[str(root)] = (info.st_dev, info.st_ino, target_bytes)
        try:
            yield root
        finally:
            _OWNED_RUNS.pop(str(root), None)


def admit_run(root: Path, target_bytes=None):
    proof = _OWNED_RUNS.get(str(root))
    if proof is None:
        raise ValueError("scratch was not created by this operation")
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or (info.st_dev, info.st_ino) != proof[:2]
            or root.resolve(strict=True) != root
            or (target_bytes is not None and target_bytes != proof[2])):
        raise ValueError("scratch identity or admission changed")
    # No following aliases, even inside an owned root. This is a bounded
    # synthetic tree, not a recursive inspection of operator data.
    pending = [root]
    count = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 256:
                    raise ValueError("unexpected scratch cardinality")
                info = entry.stat(follow_symlinks=False)
                if info.st_uid != os.getuid():
                    raise ValueError("foreign scratch entry")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("aliased scratch entry")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def fixture_limits() -> dict:
    from history_service.system_backup import MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES

    member = min(MAX_TARGET_BYTES, MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES)
    member = member // PAGE_BYTES * PAGE_BYTES
    return {"member_limit_bytes": member,
            "maximum_target_bytes": member - OVERSHOOT_RESERVE_BYTES,
            "overshoot_reserve_bytes": OVERSHOOT_RESERVE_BYTES,
            "maximum_rows_per_table": MAX_FIXTURE_ROWS}


def validate_size(target_bytes: int) -> dict:
    limits = fixture_limits()
    if type(target_bytes) is not int or not MIB <= target_bytes <= limits["maximum_target_bytes"]:
        raise ValueError("fixture target exceeds supported range with SQLite overshoot reserve")
    return limits


def preflight(root: Path, target_bytes: int) -> int:
    limits = validate_size(target_bytes)
    # Source + snapshot + staged copy + archive + private import + extraction,
    # with additional fixed headroom. This is admission, not a disk quota.
    required = (target_bytes + limits["overshoot_reserve_bytes"]) * 8 + 256 * MIB
    if shutil.disk_usage(root).free < required:
        raise ValueError("insufficient free space for bounded synthetic scratch")
    return required


def generate_fixture(path: Path, target_bytes: int, *, seed: int = 397) -> dict:
    """Populate real production schema with mixed repeated/seeded event text.

    The target is a minimum SQLite file size, not an invented exact byte count.
    Report observed logical/allocated bytes and row counts, including overshoot.
    SQLite/Python versions are recorded because file bytes can vary by version.
    """
    preflight(path.parent, target_bytes)
    limits = validate_size(target_bytes)
    file_limit = min(limits["member_limit_bytes"], target_bytes + limits["overshoot_reserve_bytes"])
    from history_service.store import SCHEMA

    # Never overwrite an existing source, including symlinks.
    with path.open("xb"):
        pass
    rng = random.Random(seed)
    rows = 0
    try:
        with closing(sqlite3.connect(path)) as db:
            db.execute(f"PRAGMA page_size={PAGE_BYTES}")
            db.execute(f"PRAGMA max_page_count={file_limit // PAGE_BYTES}")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA cache_size=-4096")
            db.executescript(SCHEMA)
            while db.execute("PRAGMA page_count").fetchone()[0] * PAGE_BYTES < target_bytes:
                if rows >= MAX_FIXTURE_ROWS:
                    raise ValueError("synthetic fixture row ceiling reached before target")
                for _ in range(min(BATCH_ROWS, MAX_FIXTURE_ROWS - rows)):
                    n = rows
                    details = json.dumps({"sample": n, "repeated": "synthetic history " * 60,
                                          "entropy": rng.randbytes(512).hex()}, separators=(",", ":"))
                    stamp = f"2020-01-{1 + n % 28:02d}T{n % 24:02d}:{n % 60:02d}:00+00:00"
                    args = (stamp, "synthetic-system", "synthetic-enclosure", n % 60, f"Slot {n % 60}")
                    db.execute("INSERT INTO slot_events (observed_at,system_id,enclosure_key,slot,slot_label,event_type,details_json) VALUES (?,?,?,?,?,'synthetic',?)", (*args, details))
                    db.execute("INSERT INTO metric_samples (observed_at,system_id,enclosure_key,slot,slot_label,metric_name,value_integer) VALUES (?,?,?,?,?,'temperature_c',?)", (*args, 20 + n % 35))
                    rows += 1
                db.commit()
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("synthetic SQLite integrity failure")
            counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("slot_events", "metric_samples", "slot_state_current")}
        stat = path.stat()
        if not target_bytes <= stat.st_size <= file_limit:
            raise ValueError("synthetic fixture byte ceiling exceeded")
        return {**limits, "file_limit_bytes": file_limit, "requested_bytes": target_bytes, "logical_bytes": stat.st_size,
                "allocated_bytes": stat.st_blocks * 512, "sha256": file_digest(path),
                "rows": counts, "seed": seed, "batch_rows": BATCH_ROWS,
                "distribution": "synthetic mixed repeated text and seeded hex; not production representative"}
    except BaseException:
        path.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        raise


def prepare_config(root: Path, *, create: bool = True):
    from app.config import PathConfig, Settings

    admit_run(root)
    config = root / "config" / "config.yaml"
    if create:
        config.parent.mkdir()
    paths = PathConfig(**{key: str(root / "config" / (key + ".json"))
                         for key in PathConfig.model_fields})
    settings = Settings(config_file=str(config), paths=paths, systems=[])
    # JSON is valid YAML. Only synthetic settings, no environment loader.
    if create:
        with config.open("x", encoding="utf-8") as output:
            output.write(json.dumps({"systems": [], "paths": paths.model_dump()}))
    return settings


def synthetic_service(root: Path, *, create_config: bool = True):
    from history_service.config import HistorySettings
    from history_service.store import HistoryStore
    from history_service.system_backup import SystemBackupService

    settings = prepare_config(root, create=create_config)

    class SyntheticService(SystemBackupService):
        def _load_app_settings(self):
            return settings

    history = HistorySettings(sqlite_path=str(root / "history.db"),
                              backup_dir=str(root / "backups"), long_term_backup_dir=None,
                              release_check_enabled=False, segment_catalog_path=None)
    store = HistoryStore(history.sqlite_path, initialize=False, recover_unreadable_database=False,
                         shared_dir_mode=0o700, shared_file_mode=0o600)
    return SyntheticService(history, store)


def operation(phase: str, request: dict) -> dict:
    root = Path(request["root"])
    admit_run(root, request.get("target_bytes"))
    if phase not in PHASES:
        raise ValueError("unknown phase")
    if phase == "fixture":
        fixture = generate_fixture(root / "history.db", request["target_bytes"], seed=request["seed"])
        return {"input_bytes": 0, "output_bytes": fixture["logical_bytes"], "fixture": fixture}
    from history_service.system_backup import HISTORY_DB_KEY, default_backup_included_paths

    service = synthetic_service(root, create_config=phase == "create")
    archive = root / "bundle.7z"
    passphrase = request["passphrase"]
    if phase == "create":
        groups = default_backup_included_paths()
        if HISTORY_DB_KEY not in groups:
            raise ValueError("production FULL selection no longer includes history")
        artifact = service.export_bundle_to_file(encrypt=True, passphrase=passphrase,
                                                packaging="7z", included_paths=groups)
        try:
            if artifact.manifest["packaging"] != "7z":
                raise ValueError("baseline packaging changed")
            os.replace(artifact.path, archive)
        finally:
            artifact.cleanup()
        return {"input_bytes": sum(entry["size_bytes"] for entry in artifact.manifest["files"]),
                "output_bytes": archive.stat().st_size, "archive_sha256": file_digest(archive)}
    input_bytes = archive.stat().st_size
    if phase == "inspect":
        result = service.inspect_bundle_file(archive, passphrase=passphrase, expected_encrypted=True)
        if result.get("ok") is not True or result["packaging"] != "7z" or HISTORY_DB_KEY not in result["present_groups"]:
            raise ValueError("FULL inspection mismatch")
        return {"input_bytes": input_bytes, "output_bytes": result["total_uncompressed_bytes"]}
    if phase == "verify":
        service.preflight_import_bundle_file(archive, passphrase=passphrase, expected_encrypted=True)
        return {"input_bytes": input_bytes, "output_bytes": 0}
    if phase != "extract":
        raise ValueError("unknown phase")
    manifest, extracted, _, meta = service._read_archive_file(archive, passphrase=passphrase)
    try:
        service._enforce_expected_encryption(meta, True)
        service._validate_manifest_member_metadata(manifest, extracted)
        history = extracted[HISTORY_DB_KEY]
        if not isinstance(history, Path):
            raise ValueError("history extraction must remain file-backed")
        service._validate_history_member(history)
        # SQLite backup can change file metadata; compare actual rows as well as
        # production manifest digest validation, not source-file SHA equality.
        counts = {}
        with closing(sqlite3.connect(f"{history.as_uri()}?mode=ro", uri=True)) as db, closing(sqlite3.connect(f"{(root / 'history.db').as_uri()}?mode=ro", uri=True)) as original:
            for table in ("slot_events", "metric_samples", "slot_state_current"):
                count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if count != original.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                    raise ValueError("extracted row count mismatch")
                counts[table] = count
        return {"input_bytes": input_bytes, "output_bytes": sum(service._extracted_member_size(v) for v in extracted.values()), "rows": counts}
    finally:
        service._cleanup_extracted_archive(meta.get("_cleanup_root"))


def worker(phase: str) -> int:
    try:
        request = json.loads(sys.stdin.readline(8192))
        admit_run(Path(request["root"]), request["target_bytes"])
        timeout = request["timeout"]
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
            return 1
    except (ValueError, TypeError, KeyError, OSError):
        return 1
    resource.setrlimit(resource.RLIMIT_AS, (GIB, GIB))
    resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(timeout), math.ceil(timeout) + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (6 * GIB, 6 * GIB))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    started = time.monotonic()
    try:
        result = operation(phase, request)
    except Exception:
        # Never expose exception strings, paths, subprocess output or credentials.
        return 1
    own = resource.getrusage(resource.RUSAGE_SELF)
    descendants = resource.getrusage(resource.RUSAGE_CHILDREN)
    result.update({"state": "complete", "phase": phase,
                   "wall_seconds": time.monotonic() - started,
                   "cpu_seconds": own.ru_utime + own.ru_stime + descendants.ru_utime + descendants.ru_stime,
                   "peak_rss_bytes": max(own.ru_maxrss, descendants.ru_maxrss) * 1024})
    print(json.dumps(result, allow_nan=False))
    return 0


class OwnedWorkerProcess:
    """Popen-shaped fork child retaining the parent's in-memory scratch proof.

    Linux CLI only, fail closed if the supervisor has other Python threads.
    No serializable proof and no alternate executable worker entrypoint.
    """
    def __init__(self, phase, *, cwd, env):
        if threading.active_count() != 1:
            raise ValueError("owned workers require a single-threaded supervisor")
        read_in, write_in = os.pipe()
        read_out, write_out = os.pipe()

        def child():
            try:
                os.setsid()
                os.close(write_in)
                os.close(read_out)
                os.environ.clear()
                os.environ.update(env)
                tempfile.tempdir = env["TMPDIR"]
                os.chdir(cwd)
                os.dup2(read_in, 0)
                os.dup2(write_out, 1)
                with open(os.devnull, "w") as sink:
                    os.dup2(sink.fileno(), 2)
                sys.stdin = os.fdopen(os.dup(0), "r")
                sys.stdout = os.fdopen(os.dup(1), "w")
                code = worker(phase)
                sys.stdout.flush()
                os._exit(code)
            except BaseException:
                os._exit(1)

        self.process = multiprocessing.get_context("fork").Process(target=child)
        try:
            self.process.start()
        except BaseException:
            os.close(write_in)
            os.close(read_out)
            raise
        finally:
            os.close(read_in)
            os.close(write_out)
        self.pid = self.process.pid
        assert self.pid is not None
        self.stdin = os.fdopen(write_in, "wb")
        self.stdout = os.fdopen(read_out, "rb")

    @property
    def returncode(self):
        return self.process.exitcode

    def wait(self, timeout=None):
        self.process.join(timeout)
        if self.process.is_alive():
            raise subprocess.TimeoutExpired("owned worker", timeout)
        return self.returncode


def measure_process(argv: list[str], request: dict, scratch: Path, *, timeout: float, owned_phase=None) -> dict:
    """Run a phase with bounded pipe capture; never retain stderr or secrets."""
    scratch = scratch.resolve(strict=True)
    started = time.monotonic()
    result = {"state": "failed", "wall_seconds": None, "cpu_seconds": None,
              "peak_rss_bytes": None, "input_bytes": None, "output_bytes": None,
              "exit_code": None}
    wire = json.dumps(request).encode()
    if len(wire) > 8192:
        return result
    # No ambient APP_*, HISTORY_*, credential or service settings.
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(scratch),
           "TMPDIR": str(scratch), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "RELEASE_CHECK_ENABLED": "false"}
    if owned_phase is not None:
        admit_run(Path(request["root"]), request["target_bytes"])
        process = OwnedWorkerProcess(owned_phase, cwd=ROOT, env=env)
    else:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, cwd=ROOT, env=env, start_new_session=True)
    captured = bytearray()
    read_failed = threading.Event()

    def kill_group():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # A fork child may not have reached setsid yet. Kill that child
            # directly so timeout/cancellation cannot become a late write.
            if owned_phase is not None:
                try:
                    os.kill(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def read_bounded():
        try:
            while len(captured) <= 16384:
                chunk = process.stdout.read(min(4096, 16385 - len(captured)))
                if not chunk:
                    return
                captured.extend(chunk)
            kill_group()
        except OSError:
            read_failed.set()

    reader = threading.Thread(target=read_bounded, daemon=True)
    reader.start()
    try:
        process.stdin.write(wire)
        process.stdin.close()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        result["state"] = "timeout"
    except KeyboardInterrupt:
        result["state"] = "cancelled"
    except (BrokenPipeError, OSError):
        pass
    finally:
        # Include grandchildren (7z); killing only the worker leaks them.
        kill_group()
        process.wait()
        reader.join()
        process.stdin.close()
        process.stdout.close()
        result["supervisor_wall_seconds"] = time.monotonic() - started
        result["exit_code"] = process.returncode
        if isinstance(process, OwnedWorkerProcess):
            process.process.close()
    if result["state"] in ("timeout", "cancelled"):
        return result
    try:
        if result["exit_code"] == 0 and len(captured) <= 16384 and not read_failed.is_set():
            payload = json.loads(captured)
            required = ("wall_seconds", "cpu_seconds", "peak_rss_bytes", "input_bytes", "output_bytes")
            if payload.get("state") == "complete" and all(type(payload.get(k)) in (int, float) and math.isfinite(payload[k]) and payload[k] >= 0 for k in required):
                result.update({k: payload[k] for k in required})
                result["state"] = "complete"
                for key in ("fixture", "rows", "archive_sha256"):
                    if key in payload:
                        result[key] = payload[key]
    except (ValueError, TypeError, AttributeError):
        pass
    return result


def benchmark(root: Path, target_bytes: int, *, seed: int, timeout: float, allow_large: bool = False) -> dict:
    root = root.resolve(strict=True)
    preflight(root, target_bytes)
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    report = {"schema_version": 1, "synthetic_only": True, "release_acceptance": False,
              "source_sha": source_sha, "source_file_sha256": file_digest(ROOT / "history_service/system_backup.py"),
              "runner_sha256": file_digest(Path(__file__)), "python": sys.version.split()[0],
              "sqlite": sqlite3.sqlite_version, "profile": PROFILE, "worker_cap": 1,
              "fixture_limits": fixture_limits(),
              "per_process_address_space_limit_bytes": GIB, "phase_timeout_seconds": timeout,
              "process_model": "forked owned-scratch workers; RSS includes inherited supervisor memory",
              "rss_scope": "max Linux process high-water RSS across worker and reaped descendants; not concurrent aggregate",
              "timing_scope": "whole production operations; overlapping work, not additive; fixture excluded from backup timing",
              "candidate_profiles": [{"profile": "tar.zst+AES-256-GCM FULL", "state": "unsupported",
                                      "reason": "current production FULL encrypted export selects 7z; no supported alternate FULL export API; guards not bypassed"}],
              "large_matrix": [{"target_bytes": (size * GIB if size == 2 else fixture_limits()["maximum_target_bytes"]), "state": "not_run", "reason": "requires separate explicit opt-in and capacity/tool admission"} for size in (2, 4)],
              "phases": []}
    with owned_run(root, target_bytes, allow_large=allow_large) as run:
        name = str(run)
        secret = secrets.token_urlsafe(32)
        blocked = None
        for phase in PHASES:
            if phase == "create" and not shutil.which("7z"):
                blocked = "7z executable unavailable"
            if blocked:
                report["phases"].append({"phase": phase, "state": "blocked", "reason": blocked,
                                         "wall_seconds": None, "cpu_seconds": None, "peak_rss_bytes": None,
                                         "input_bytes": None, "output_bytes": None, "exit_code": None})
                continue
            with tempfile.TemporaryDirectory(prefix="phase-", dir=run) as scratch:
                request = {"root": str(run), "target_bytes": target_bytes, "seed": seed,
                           "timeout": timeout, "passphrase": secret}
                result = measure_process([], request, Path(scratch), timeout=timeout, owned_phase=phase)
            report["phases"].append({"phase": phase, **result})
            if result["state"] != "complete":
                blocked = "preceding phase did not complete"
        report["state"] = "complete" if all(p["state"] == "complete" for p in report["phases"]) else "incomplete"
        for cell in report["large_matrix"]:
            if cell["target_bytes"] == target_bytes:
                cell.update(state=report["state"], reason="see separately measured phases")
            elif not shutil.which("7z"):
                cell.update(state="blocked", reason="7z executable unavailable; no large allocation attempted")
    report["scratch_cleaned"] = not Path(name).exists()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--self-test", action="store_true", help="run a bounded 1 MiB synthetic sample")
    choice.add_argument("--size-gib", type=int, choices=(2, 4), help="2 GiB minimum or 4 GiB near-limit cell (ceiling minus 1 MiB); requires --allow-large")
    choice.add_argument("--worker-phase", choices=PHASES, help=argparse.SUPPRESS)
    parser.add_argument("--allow-large", action="store_true")
    parser.add_argument("--output-root", type=Path, help="existing private scratch parent; never a database input")
    parser.add_argument("--workers", type=int, choices=(1,), default=1, help="baseline fixed to one compressor worker")
    parser.add_argument("--timeout", type=float, default=120, help="per-phase wall/CPU budget, maximum 600 seconds")
    parser.add_argument("--seed", type=int, default=397)
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("Linux resource/process accounting required")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= MAX_TIMEOUT:
        parser.error("timeout must be positive and at most 600 seconds")
    if args.worker_phase:
        return worker(args.worker_phase)
    if args.size_gib and (not args.allow_large or args.output_root is None):
        parser.error("large fixture requires --allow-large and --output-root")
    if args.output_root is not None:
        root = args.output_root
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            parser.error("output-root must be an existing private owned directory (0700)")
    else:
        root = Path(tempfile.gettempdir())
    target = args.size_gib * GIB if args.size_gib else MIB
    if args.size_gib == 4:
        target = fixture_limits()["maximum_target_bytes"]
    try:
        validate_size(target)
    except ValueError as error:
        parser.error(str(error))
    if args.size_gib and not shutil.which("7z"):
        # Refuse expensive allocation without the only supported adapter.
        print(json.dumps({"state": "blocked", "synthetic_only": True, "release_acceptance": False,
                          "target_bytes": target, "fixture_limits": fixture_limits(),
                          "reason": "7z executable unavailable; no fixture allocated"}))
        return 2
    try:
        report = benchmark(root, target, seed=args.seed, timeout=args.timeout, allow_large=args.allow_large)
    except ValueError:
        print(json.dumps({"state": "blocked", "synthetic_only": True, "release_acceptance": False,
                          "reason": "scratch free-space admission failed"}))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["state"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
