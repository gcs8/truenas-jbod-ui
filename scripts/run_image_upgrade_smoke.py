#!/usr/bin/env python3
"""Image-only upgrade and rollback smoke for CI (#399, #463).

Starts the previous release with its own published Compose file on root-owned
bind mounts, seeds configuration, bay mappings and history through that
release's own writers, then upgrades by changing only ``JBOD_UI_IMAGE`` and
running ``docker compose pull`` and ``docker compose up -d``. It checks health,
restart counts, image identity, version, revision, retained state and SQLite
integrity, writes with the new release, and rolls back by pin with the same two
commands.

``--scenario`` picks a run, each a separate CI step:

* ``base``: the root-owned base Compose file, as above.
* ``hardened``: the previous release's base file plus its
  ``docker-compose.nonroot.yml`` overlay, on bind mounts prepared with the
  ownership commands documented in ``wiki/Troubleshooting.md``. The same
  upgrade and rollback, with the services running as uid 10001 throughout.
* ``interrupted-migration``: the new release's history container is killed
  (``docker kill``) while its own startup is paused inside each schema-
  initialization step, reusing the per-step seams of
  ``tests/test_history_released_schema_upgrades.py``. The v0.22.2 database is
  already at the candidate schema version, so this proves interruption-safe,
  idempotent startup compatibility, not a schema transition. The next ordinary
  ``docker compose up -d`` must preserve the same end state as an uninterrupted
  start.
* ``segmented-catalog``: v0.22.2 creates and reads a real segmented catalog,
  then its exact generation, catalog bytes and referenced segment bytes must
  survive the image-only upgrade and rollback while both releases read the
  combined hot and segmented history.
* ``enospc-state``: seed v0.22.2 on an 8 MiB disposable tmpfs, then exhaust
  its inodes before candidate startup. Require ENOSPC, local-storage refusal
  with a disk-full diagnosis and space/inode recovery remedy, live process,
  unchanged bytes and no restarts over 20 seconds. Explicitly remove the filler
  and prove retained reads and new candidate writes. This is not a
  supported-storage or release-promotion decision.
* ``readonly-state``: after seeding and stopping v0.22.2, mount its required
  history directory literally read-only for candidate startup. Require the
  existing local-storage refusal contract, live process, unchanged bytes and
  no restarts over 20 seconds. Explicitly restore the writable mount and prove
  the candidate can read the retained records. This is not a supported-storage
  or release-promotion decision.
* ``clean-host-restore``: explicitly admitted, exclusive disposable host only.
  Export encrypted 7z through the predecessor Admin API, stop and remove its
  entire installation, then inspect/import into an independent current target
  through the receipt-bound Admin API. Verify bytes, logical identity, health,
  history reads, restart and resource absence. Not run by the existing CI jobs.
* ``unavailable-backup-storage``: after seeding and stopping v0.22.2, run the
  candidate's backup scheduler with one filesystem backup target. Prove the
  shipped copy once, make the target root not a directory (one injected
  storage condition), and require the scheduler's own visible refusal for the
  next backup — the status file names the target and the failure class, the
  local archive keeps the artifact, and the scheduler never restarts over 20
  seconds. Explicitly restore the target root and prove the next backup ships
  byte-identically by readback. This is not a supported-storage or
  release-promotion decision.

Synthetic data only; it needs a disposable Linux Docker host with ``sudo`` (the
bind mounts are root-owned on purpose). Every image it pulls is public. CI
publishes the candidate to a local registry so ``pull`` works unchanged.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import textwrap
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

UI_PORT = 18180
HISTORY_PORT = 18181
SERVICES = ("enclosure-ui", "enclosure-history")
SMOKE_SYSTEM = "upgrade-smoke"
SUCCESS_PREFIX = "image_upgrade_smoke=ok"
SCENARIOS = ("base", "hardened", "interrupted-migration", "segmented-catalog", "readonly-state", "enospc-state",
             "clean-host-restore", "unavailable-backup-storage")
# The released config/full artifact name shape (scheduled_backup.py _ARCHIVE_NAME).
ARCHIVE_ARTIFACT_NAME = re.compile(
    r"jbod-scheduled-backup-(?P<stamp>[0-9]{8}T[0-9]{6}Z)-[0-9a-f]{8}(?:\.7z|\.tar\.zst\.enc)$"
)
HARDENED_FILES = ("docker-compose.yml", "docker-compose.nonroot.yml")
READONLY_FILES = ("docker-compose.yml", "docker-compose.readonly.yml")
ENOSPC_FILES = ("docker-compose.yml", "docker-compose.enospc.yml")
UNAVAILABLE_BACKUP_FILES = ("docker-compose.yml", "docker-compose.unavailable-backup.yml")
BACKUP_TARGET_ID = "smoke-nas"
APP_UID = 10001

# The one-time ownership step for adopting docker-compose.nonroot.yml, exactly
# as wiki/Troubleshooting.md documents it ("A non-root container gets
# permission denied"); a contract test keeps the two identical. It runs once,
# before the hardened install first starts, never as part of an upgrade.
HARDENED_OWNERSHIP_PREP = (
    'app_uid="${APP_UID:-10001}"',
    'app_gid="${APP_GID:-10001}"',
    'backup_uid="${BACKUP_UID:-1000}"',
    'sudo find ./config -path ./config/backup-secrets -prune -o -exec chown "$app_uid:$app_gid" {} +',
    'sudo chown -R "$app_uid:$app_gid" ./data ./logs ./history',
    'sudo install -d -o "$backup_uid" -g "$app_gid" -m 2750 ./backup-status',
)

# HistoryStore schema-initialization steps, in order. These are migration-capable
# seams used by the released-schema kill tests (#603, KILL_PHASES there), but a
# v0.22.2 database is already stamped user_version 1, so the smoke reaches them
# only as idempotent startup steps. The new release never enters the batched
# identity backfill on it; `_backfill_disk_identity_batch` is the one #603 phase
# that cannot be reached from v0.22.2 state and stays unit-tested.
KILL_PHASES = (
    "_ensure_slot_state_columns",
    "_ensure_slot_event_columns",
    "_ensure_metric_sample_columns",
    "_backfill_disk_identity_keys_once",
    "_ensure_identity_indexes",
    "_ensure_chronological_indexes",
    "_synchronize_table_counts",
)
KILL_MARKER = "upgrade_smoke_kill_point_reached"
KILL_UNREACHED = "upgrade_smoke_kill_point_not_reached"


class SmokeError(RuntimeError):
    """A failed upgrade or rollback expectation."""


def run(command: list[str], *, cwd: Path | None = None, input_text: str | None = None, timeout: int = 600,
        env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise SmokeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
        )
    return result.stdout


class Deployment:
    def __init__(self, root: Path, files: tuple[str, ...] = ()) -> None:
        self.root = root
        # The ordered `-f` chain an operator uses; empty means Compose's default file.
        self.files = files
        self.image: str | None = None
        self.runtime_environment: dict[str, str] = {}
        self.environment: dict[str, str] | None = None

    def file_args(self) -> list[str]:
        return [arg for name in self.files for arg in ("-f", name)]

    def compose(self, *args: str, timeout: int = 600) -> str:
        return run(["docker", "compose", *self.file_args(), *args], cwd=self.root, timeout=timeout,
                   env=self.environment)

    def _write_environment(self) -> None:
        if self.image is None:
            raise SmokeError("deployment image must be set before its environment")
        env = "\n".join(
            (
                f"JBOD_UI_IMAGE={self.image}",
                "COMPOSE_PROFILES=history",
                f"APP_PORT={UI_PORT}",
                f"HISTORY_PORT={HISTORY_PORT}",
                *(f"{name}={value}" for name, value in sorted(self.runtime_environment.items())),
                "",
            )
        )
        path = self.root / ".env"
        path.write_text(env, encoding="utf-8")
        path.chmod(0o600)

    def set_image(self, image: str) -> None:
        self.image = image
        self._write_environment()

    def set_runtime_environment(self, name: str, value: str) -> None:
        if not name or "=" in name or "\n" in name or "\n" in value:
            raise SmokeError("invalid deployment environment value")
        self.runtime_environment[name] = value
        self._write_environment()

    def pull_and_up(self) -> None:
        # The literal published update procedure; --wait only blocks until the
        # healthchecks settle so the checks below read a converged stack.
        self.compose("pull")
        self.compose("up", "-d", "--wait", "--wait-timeout", "300", timeout=420)

    def exec_python(self, service: str, script: str) -> dict:
        output = run(
            ["docker", "compose", *self.file_args(), "exec", "-T", service, "python", "-"],
            cwd=self.root,
            input_text=textwrap.dedent(script),
            timeout=180,
            env=self.environment,
        )
        return json.loads(output.strip().splitlines()[-1])

    def run_python(self, service: str, script: str, extra: tuple[str, ...] = ()) -> dict:
        """Run a script in a one-off container of a stopped service (same image, mounts and user)."""
        output = run(
            ["docker", "compose", *self.file_args(), "run", "--rm", "--no-deps", "-T", *extra, service, "python", "-"],
            cwd=self.root,
            input_text=textwrap.dedent(script),
            timeout=180,
            env=self.environment,
        )
        return json.loads(output.strip().splitlines()[-1])


def prepare_root(root: Path, compose_fixture: Path, config_fixture: Path, nonroot_fixture: Path | None = None) -> None:
    if root.exists():
        run(["sudo", "rm", "-rf", str(root)])
    root.mkdir(parents=True)
    shutil.copyfile(compose_fixture, root / "docker-compose.yml")
    # Existing installs created these as root; the base Compose must accept
    # them without any ownership migration.
    for name in ("config", "config/ssh", "data", "logs", "history", "backup-status"):
        run(["sudo", "install", "-d", "-m", "0755", "-o", "0", "-g", "0", str(root / name)])
    run(["sudo", "install", "-m", "0644", "-o", "0", "-g", "0", str(config_fixture), str(root / "config" / "config.yaml")])
    if nonroot_fixture is not None:
        # Adopting the overlay is a separate, one-time change made before the
        # hardened install first starts: the documented ownership step, verbatim.
        shutil.copyfile(nonroot_fixture, root / "docker-compose.nonroot.yml")
        run(["bash", "-euo", "pipefail", "-c", "\n".join(HARDENED_OWNERSHIP_PREP)], cwd=root)


SEED_UI = f"""
import json
from app.config import get_settings
from app.models.domain import ManualMapping
from app.services.mapping_store import MappingStore
store = MappingStore(get_settings().paths.mapping_file)
for slot in SLOTS:
    store.save_mapping(ManualMapping(system_id="{SMOKE_SYSTEM}", enclosure_id="shelf-a", slot=slot,
                                     serial=f"SYNTH-{{slot:04d}}", notes="synthetic upgrade smoke"))
print(json.dumps({{"ok": True}}))
"""

READ_UI = f"""
import json
import os
from app import __version__
from app.config import get_settings
from app.services.mapping_store import MappingStore
store = MappingStore(get_settings().paths.mapping_file)
rows = sorted([m.enclosure_id, m.slot, m.serial, m.notes]
              for m in store.load_all().values() if m.system_id == "{SMOKE_SYSTEM}")
print(json.dumps({{"version": __version__, "mappings": rows, "uid": os.getuid()}}))
"""

SEED_HISTORY = f"""
import json
from datetime import datetime, timedelta, timezone
from history_service.config import get_history_settings
from history_service.domain import MetricSample, SlotEvent, SlotStateRecord
from history_service.store import HistoryStore, SlotStateUpdate
store = HistoryStore(get_history_settings().sqlite_path)
now = datetime.now(timezone.utc)
scope = dict(system_id="{SMOKE_SYSTEM}", system_label="Upgrade smoke", enclosure_key="shelf-a",
             enclosure_id="shelf-a", enclosure_label="Shelf A")
updates, samples = [], []
for slot in SLOTS:
    observed = (now - timedelta(minutes=10 + slot)).isoformat()
    serial = f"SYNTH-{{slot:04d}}"
    record = SlotStateRecord(**scope, slot=slot, slot_label=f"{{slot:02d}}", present=True, state="ok",
                             identify_active=False, device_name=f"da{{slot}}", serial=serial, model="SYNTH",
                             gptid=None, pool_name="tank", vdev_name="raidz2-0", health="ONLINE")
    event = SlotEvent(observed_at=observed, **scope, slot=slot, slot_label=f"{{slot:02d}}",
                      event_type="disk_inserted", previous_value=None, current_value=serial,
                      device_name=f"da{{slot}}", serial=serial, details_json="{{}}")
    updates.append(SlotStateUpdate(record=record, observed_at=observed, events=[event]))
    for minute in range(METRICS_PER_SLOT):
        samples.append(MetricSample(observed_at=(now - timedelta(minutes=minute + 1)).isoformat(), **scope,
                                    slot=slot, slot_label=f"{{slot:02d}}", metric_name="temperature_c",
                                    value_integer=30 + minute, value_real=None, device_name=f"da{{slot}}",
                                    serial=serial, model="SYNTH", state="ok"))
if updates:
    store.record_slot_updates(updates)
store.insert_metric_samples(samples)
print(json.dumps({{"ok": True}}))
"""

READ_HISTORY = f"""
import json
import os
import sqlite3
from history_service import store as store_module
from history_service.config import get_history_settings
path = get_history_settings().sqlite_path
connection = sqlite3.connect(f"file:{{path}}?mode=ro", uri=True)
try:
    counts = {{table: connection.execute(f"SELECT COUNT(*) FROM {{table}} WHERE system_id = ?",
                                        ("{SMOKE_SYSTEM}",)).fetchone()[0]
              for table in ("slot_state_current", "slot_events", "metric_samples")}}
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
finally:
    connection.close()
# v0.22.2 predates CURRENT_SCHEMA_VERSION; there it is the backfill user_version,
# which is what the newer name aliases.
current_schema = getattr(store_module, "CURRENT_SCHEMA_VERSION",
                         getattr(store_module, "DISK_IDENTITY_BACKFILL_USER_VERSION", None))
print(json.dumps({{"counts": counts, "integrity": integrity, "user_version": user_version,
                  "current_schema": current_schema,
                  "uid": os.getuid()}}))
"""

SEGMENT_CATALOG_PATH = "/app/history/segments/catalog.json"

# v0.22.2 did not package the CLI wrapper yet, but its released image contains
# the migration module. Running that module in a one-off container uses the
# released writer/schema and the deployment's real history mount.
CREATE_SEGMENTED_CATALOG = """
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from history_service.segment_migration import migrate_segmented_history
receipt = migrate_segmented_history(
    source=Path("/app/history/history.db"),
    segments_directory=Path("/app/history/segments"),
    # v0.23.0+ rounds the cutoff down to midnight UTC; two days ahead keeps
    # every freshly seeded row before it either way.
    cutoff=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    key_id="upgrade-smoke-key",
    apply=True,
)
print(json.dumps({"apply": receipt["apply"],
                  "segment_id": receipt["segment"]["segment_id"]}))
"""

# Logical identity plus exact catalog and referenced-segment bytes. This is
# intentionally stdlib-only so the same probe runs inside both image versions.
READ_SEGMENTED_IDENTITY = """
import hashlib
import json
from pathlib import Path
catalog_path = Path("/app/history/segments/catalog.json")
catalog_bytes = catalog_path.read_bytes()
catalog = json.loads(catalog_bytes)
segments = []
for entry in catalog["segments"]:
    segment_path = catalog_path.parent / entry["file_name"]
    segment_bytes = segment_path.read_bytes()
    segments.append({
        "segment_id": entry["segment_id"],
        "file_name": entry["file_name"],
        "catalog_sha256": entry["sha256"],
        "catalog_size_bytes": entry["size_bytes"],
        "actual_sha256": hashlib.sha256(segment_bytes).hexdigest(),
        "actual_size_bytes": len(segment_bytes),
    })
print(json.dumps({
    "catalog_version": catalog["catalog_version"],
    "generation_id": catalog["generation_id"],
    "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
    "segments": segments,
}, sort_keys=True))
"""

# Everything an interrupted schema-initialization start must preserve or
# produce, read-only and stdlib-only so it runs the same inside either release.
# PATH is replaced.
END_STATE = f"""
import hashlib
import json
import sqlite3
connection = sqlite3.connect("file:" + PATH + "?mode=ro", uri=True)
try:
    schema = sorted([str(t), str(n), str(q or "")] for t, n, q in
                    connection.execute("SELECT type, name, sql FROM sqlite_master"))
    rows = {{}}
    for table in ("slot_state_current", "slot_events", "metric_samples"):
        digest = hashlib.sha256()
        for row in connection.execute(f"SELECT * FROM {{table}} WHERE system_id = ? ORDER BY rowid",
                                      ("{SMOKE_SYSTEM}",)):
            digest.update(repr(tuple(row)).encode())
        rows[table] = digest.hexdigest()
    counters = dict(connection.execute("SELECT table_name, row_count FROM history_table_counts"))
    real = {{table: connection.execute(f"SELECT COUNT(*) FROM {{table}}").fetchone()[0]
            for table in ("slot_events", "metric_samples", "metric_rollups")}}
    state = {{"integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
              "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
              "schema": hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
              "rows": rows, "counters_match": counters == real}}
finally:
    connection.close()
print(json.dumps(state))
"""

# An uninterrupted upgrade of a copy of the same database, for comparison.
UNINTERRUPTED_REFERENCE = """
from history_service.store import HistoryStore
HistoryStore(PATH)
""" + END_STATE

# Runs as the new release's history container. It pauses the service's own
# startup (history_service.main opens and initializes the store at import) right
# after one schema-initialization step, announces it, and waits to be killed.
KILL_HOOK = """
import signal
from history_service.store import HistoryStore
name = PHASE
original = getattr(HistoryStore, name)
def paused(*args, **kwargs):
    result = original(*args, **kwargs)
    print("MARKER " + name, flush=True)
    signal.pause()
    return result
setattr(HistoryStore, name, staticmethod(paused))
import history_service.main
print("UNREACHED " + name, flush=True)
"""


def end_state_script(path: str, template: str = END_STATE) -> str:
    return template.replace("PATH", repr(path))


def kill_hook_script(phase: str) -> str:
    if phase not in KILL_PHASES:
        raise ValueError(phase)
    return (KILL_HOOK.replace("PHASE", repr(phase))
            .replace("MARKER", KILL_MARKER).replace("UNREACHED", KILL_UNREACHED))


def seed_script(template: str, slots: tuple[int, ...], metrics_per_slot: int = 0) -> str:
    return template.replace("SLOTS", repr(slots)).replace("METRICS_PER_SLOT", str(metrics_per_slot))


def get_json(url: str, *, read_error_body: bool = False) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.load(exc) if read_error_body else {}


def history_api_view(slots: tuple[int, ...]) -> dict[str, list]:
    """Read seeded history through the running release's own history API.

    Raw counts prove rows exist; this proves the running release can still
    interpret them the way operators read them.
    """
    base = f"http://127.0.0.1:{HISTORY_PORT}/api/history/slots"
    query = f"system_id={SMOKE_SYSTEM}&enclosure_id=shelf-a"
    events: list = []
    samples: list = []
    for slot in slots:
        status, payload = get_json(f"{base}/{slot}/events?{query}&limit=1000")
        if status != 200:
            raise SmokeError(f"history events API for slot {slot} returned {status}")
        events.extend(
            [slot, row.get("event_type"), row.get("current_value"), row.get("serial")]
            for row in payload.get("events", [])
        )
        status, payload = get_json(f"{base}/{slot}/metrics?{query}&metric_name=temperature_c&limit=5000")
        if status != 200:
            raise SmokeError(f"history metrics API for slot {slot} returned {status}")
        samples.extend(
            [slot, row.get("value_integer"), row.get("serial")] for row in payload.get("samples", [])
        )
    return {"events": sorted(events), "samples": sorted(samples)}


def expected_history_view(seeded: dict[int, int]) -> dict[str, list]:
    """What SEED_HISTORY wrote: slot -> number of metric samples."""
    events = [[slot, "disk_inserted", f"SYNTH-{slot:04d}", f"SYNTH-{slot:04d}"] for slot in seeded]
    samples = [
        [slot, 30 + minute, f"SYNTH-{slot:04d}"] for slot, count in seeded.items() for minute in range(count)
    ]
    return {"events": sorted(events), "samples": sorted(samples)}


def check_runtime(deployment: Deployment, image: str, version: str, revision: str | None) -> dict:
    image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", image], env=deployment.environment).strip()
    rows = {}
    for service in SERVICES:
        container = deployment.compose("ps", "-q", service).strip()
        if not container or "\n" in container:
            raise SmokeError(f"{service} must resolve to exactly one container")
        state = json.loads(run(["docker", "inspect", "--format", "{{json .}}", container], env=deployment.environment))
        health = (state["State"].get("Health") or {}).get("Status", "")
        if state["State"]["Status"] != "running" or health != "healthy":
            raise SmokeError(f"{service} is {state['State']['Status']}/{health or 'no health'}")
        if state["Image"] != image_id:
            raise SmokeError(f"{service} runs {state['Image']}, expected {image_id}")
        if state["RestartCount"] != 0:
            raise SmokeError(f"{service} restarted {state['RestartCount']} times")
        rows[service] = {"health": health, "restarts": state["RestartCount"]}
    status, live = get_json(f"http://127.0.0.1:{UI_PORT}/livez")
    if status != 200 or live.get("version") != version:
        raise SmokeError(f"UI /livez reported {status} {live.get('version')!r}, expected {version}")
    for port in (UI_PORT, HISTORY_PORT):
        status, payload = get_json(f"http://127.0.0.1:{port}/healthz")
        if status != 200:
            raise SmokeError(f"/healthz on {port} returned {status}")
        # A quarantined or paused history store (#417) is never an accepted
        # upgrade, even though /healthz only grades it degraded.
        collector = payload.get("collector") if isinstance(payload.get("collector"), dict) else {}
        for flag in ("history_recovery_required", "history_collection_paused"):
            if collector.get(flag):
                raise SmokeError(f"/healthz on {port} reports {flag}")
    if revision is not None:
        label = run(
            ["docker", "image", "inspect", "--format",
             '{{index .Config.Labels "org.opencontainers.image.revision"}}', image], env=deployment.environment
        ).strip()
        if label != revision:
            raise SmokeError(f"image revision label is {label!r}, expected {revision}")
    return rows


def owner_uid(path: Path) -> int:
    # The history service may tighten its directory to 0770, so stat as root.
    return int(run(["sudo", "stat", "-c", "%u", str(path)]).strip())


def run_bytes(command: list[str], *, timeout: int = 600) -> bytes:
    """run() for binary output: identical failure discipline, no text decode."""
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise SmokeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout[-4000:]!r}\n{result.stderr[-4000:]!r}"
        )
    return result.stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(run_bytes(["sudo", "cat", str(path)])).hexdigest()


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def schema_evidence(predecessor: dict, candidate: dict) -> dict[str, str]:
    """Describe schema admission without inventing a migration transition."""
    for label, reading in (("predecessor", predecessor), ("candidate", candidate)):
        expect(
            reading["user_version"] == reading["current_schema"],
            f"{label} schema is not current for its image: {reading}",
        )
    before = predecessor["user_version"]
    after = candidate["user_version"]
    transition = "none" if before == after else f"{before}->{after}"
    return {
        "compatibility": "ok",
        "before": str(before),
        "after": str(after),
        "transition": transition,
    }


OWNED_PATHS = ("data", "history", "config", "data/slot_mappings.json", "history/history.db")


def seed_previous_release(deployment: Deployment, args: argparse.Namespace) -> tuple[dict, dict, dict[int, int]]:
    """Start the previous release as published and seed it with its own writers."""
    deployment.set_image(args.previous_image)
    deployment.pull_and_up()
    check_runtime(deployment, args.previous_image, args.previous_version, None)
    deployment.exec_python("enclosure-ui", seed_script(SEED_UI, (1, 2, 3)))
    deployment.exec_python("enclosure-history", seed_script(SEED_HISTORY, (1, 2, 3), metrics_per_slot=4))
    before_ui = deployment.exec_python("enclosure-ui", READ_UI)
    before_history = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(len(before_ui["mappings"]) == 3, f"seeded mappings not readable: {before_ui}")
    expect(before_history["counts"] == {"slot_state_current": 3, "slot_events": 3, "metric_samples": 12},
           f"seeded history not readable: {before_history}")
    seeded = {1: 4, 2: 4, 3: 4}
    expect(history_api_view((1, 2, 3)) == expected_history_view(seeded),
           "previous release history API does not return the seeded records")
    return before_ui, before_history, seeded


def upgrade_and_rollback(args: argparse.Namespace, *, hardened: bool) -> str:
    """Image-only upgrade and rollback on the base file, or the base file plus the nonroot overlay."""
    root = args.root.resolve()
    prepare_root(root, args.compose_fixture, args.config_fixture, args.nonroot_fixture if hardened else None)
    deployment = Deployment(root, HARDENED_FILES if hardened else ())
    # Base installs stay root-owned and run as root; the hardened install runs
    # as the app identity on the mounts its one-time preparation handed over.
    owner = APP_UID if hardened else 0
    config_path = root / "config" / "config.yaml"
    config_digest = sha256(config_path)

    # 1. The previous release, exactly as published.
    before_ui, before_history, seeded = seed_previous_release(deployment, args)
    for reading in (before_ui, before_history):
        expect(reading["uid"] == owner, f"previous release runs as uid {reading['uid']}, expected {owner}")

    # 2. Image-only upgrade: change JBOD_UI_IMAGE, pull, up -d. Nothing else.
    deployment.set_image(args.candidate_image)
    deployment.pull_and_up()
    upgraded = check_runtime(deployment, args.candidate_image, args.candidate_version, args.candidate_revision)
    after_ui = deployment.exec_python("enclosure-ui", READ_UI)
    after_history = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(after_ui["mappings"] == before_ui["mappings"], f"mappings changed across upgrade: {after_ui}")
    expect(after_history["counts"] == before_history["counts"], f"history rows changed across upgrade: {after_history}")
    expect(after_history["integrity"] == "ok", f"integrity_check after upgrade: {after_history['integrity']}")
    schema = schema_evidence(before_history, after_history)
    expect(history_api_view((1, 2, 3)) == expected_history_view(seeded),
           "upgraded history API does not return the predecessor's records")
    expect(sha256(config_path) == config_digest, "config.yaml changed during the upgrade")
    for reading in (after_ui, after_history):
        expect(reading["uid"] == owner, f"upgraded release runs as uid {reading['uid']}, expected {owner}")
    for name in OWNED_PATHS:
        expect(owner_uid(root / name) == owner, f"{name} ownership changed; an image-only upgrade must not migrate it")

    # 3. The new release writes, then roll back by pin with the same commands.
    deployment.exec_python("enclosure-ui", seed_script(SEED_UI, (4,)))
    deployment.exec_python("enclosure-history", seed_script(SEED_HISTORY, (4,), metrics_per_slot=1))
    deployment.set_image(args.previous_image)
    deployment.pull_and_up()
    check_runtime(deployment, args.previous_image, args.previous_version, None)
    rolled_ui = deployment.exec_python("enclosure-ui", READ_UI)
    rolled_history = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(len(rolled_ui["mappings"]) == 4, f"previous release cannot read successor mappings: {rolled_ui}")
    expect(rolled_history["counts"] == {"slot_state_current": 4, "slot_events": 4, "metric_samples": 13},
           f"previous release cannot read successor history: {rolled_history}")
    expect(rolled_history["integrity"] == "ok", f"integrity_check after rollback: {rolled_history['integrity']}")
    # The rolled-back release must serve both its own and the successor's
    # records through its normal history API, not just count them.
    seeded[4] = 1
    expect(history_api_view((1, 2, 3, 4)) == expected_history_view(seeded),
           "rolled-back history API does not return the successor-written records")
    successor = [row for row in rolled_ui["mappings"] if row[1] == 4]
    expect(successor == [["shelf-a", 4, "SYNTH-0004", "synthetic upgrade smoke"]],
           f"rolled-back release reads the successor mapping differently: {successor}")
    for name in OWNED_PATHS:
        expect(owner_uid(root / name) == owner, f"{name} ownership changed during the rollback")

    return (
        f"{SUCCESS_PREFIX} scenario={'hardened' if hardened else 'base'} "
        f"previous={args.previous_version} candidate={args.candidate_version} "
        f"revision={args.candidate_revision} services={','.join(sorted(upgraded))} restarts=0 uid={owner} "
        f"mappings={len(after_ui['mappings'])} history_rows={sum(after_history['counts'].values())} "
        f"schema_compatibility=ok schema_before={schema['before']} "
        f"schema_after={schema['after']} schema_transition={schema['transition']} "
        f"rollback_mappings={len(rolled_ui['mappings'])} "
        f"rollback_history_rows={sum(rolled_history['counts'].values())} history_api=ok"
    )


def seed_segmented_catalog(
    deployment: Deployment,
    args: argparse.Namespace,
    seeded: dict[int, int],
) -> dict:
    """Turn the running predecessor state into its own real segmented deployment."""
    deployment.compose("stop", "enclosure-history")
    receipt = deployment.run_python("enclosure-history", CREATE_SEGMENTED_CATALOG)
    expect(receipt == {"apply": True, "segment_id": "segment-0001"},
           f"predecessor did not create the expected segmented catalog: {receipt}")
    deployment.set_runtime_environment("HISTORY_SEGMENT_CATALOG_PATH", SEGMENT_CATALOG_PATH)
    deployment.pull_and_up()
    check_runtime(deployment, args.previous_image, args.previous_version, None)
    identity = deployment.exec_python("enclosure-history", READ_SEGMENTED_IDENTITY)
    expect(identity["catalog_version"] == 1, f"unexpected predecessor catalog version: {identity}")
    expect(identity["generation_id"] == "generation-0001", f"unexpected predecessor generation: {identity}")
    expect(len(identity["segments"]) == 1, f"unexpected predecessor segment count: {identity}")
    segment = identity["segments"][0]
    expect(segment["catalog_sha256"] == segment["actual_sha256"],
           f"predecessor segment digest does not match its catalog: {identity}")
    expect(segment["catalog_size_bytes"] == segment["actual_size_bytes"],
           f"predecessor segment size does not match its catalog: {identity}")
    expect(history_api_view((1, 2, 3)) == expected_history_view(seeded),
           "v0.22.2 cannot read its own segmented history")
    return identity


def segmented_catalog_upgrade(args: argparse.Namespace) -> str:
    """Preserve a v0.22.2 segmented catalog byte-for-byte across upgrade and rollback."""
    root = args.root.resolve()
    prepare_root(root, args.compose_fixture, args.config_fixture)
    deployment = Deployment(root)
    config_path = root / "config" / "config.yaml"
    config_digest = sha256(config_path)

    # Establish the segmented predecessor deployment before the upgrade starts.
    before_ui, _, seeded = seed_previous_release(deployment, args)
    before_identity = seed_segmented_catalog(deployment, args, seeded)

    # Image-only upgrade: set JBOD_UI_IMAGE, pull, up -d. The catalog setting and
    # every durable file remain untouched.
    deployment.set_image(args.candidate_image)
    deployment.pull_and_up()
    upgraded = check_runtime(
        deployment,
        args.candidate_image,
        args.candidate_version,
        args.candidate_revision,
    )
    after_identity = deployment.exec_python("enclosure-history", READ_SEGMENTED_IDENTITY)
    expect(after_identity == before_identity,
           f"segmented catalog identity changed across upgrade: {after_identity} != {before_identity}")
    after_ui = deployment.exec_python("enclosure-ui", READ_UI)
    after_history = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(after_ui["mappings"] == before_ui["mappings"], f"mappings changed across upgrade: {after_ui}")
    expect(after_history["integrity"] == "ok", f"hot history integrity after upgrade: {after_history}")
    expect(history_api_view((1, 2, 3)) == expected_history_view(seeded),
           "upgraded release cannot read the predecessor's segmented records")
    expect(sha256(config_path) == config_digest, "config.yaml changed during the segmented upgrade")

    # Successor writes land in the hot database. Rollback by image pin must keep
    # the same catalog identity and expose both archived and successor records.
    deployment.exec_python("enclosure-ui", seed_script(SEED_UI, (4,)))
    deployment.exec_python("enclosure-history", seed_script(SEED_HISTORY, (4,), metrics_per_slot=1))
    expect(deployment.exec_python("enclosure-history", READ_SEGMENTED_IDENTITY) == before_identity,
           "successor writes changed the immutable segmented catalog identity")
    seeded[4] = 1
    expect(history_api_view((1, 2, 3, 4)) == expected_history_view(seeded),
           "upgraded release cannot combine segmented and successor-written records")
    deployment.set_image(args.previous_image)
    deployment.pull_and_up()
    check_runtime(deployment, args.previous_image, args.previous_version, None)
    rolled_identity = deployment.exec_python("enclosure-history", READ_SEGMENTED_IDENTITY)
    expect(rolled_identity == before_identity,
           f"segmented catalog identity changed across rollback: {rolled_identity} != {before_identity}")
    expect(history_api_view((1, 2, 3, 4)) == expected_history_view(seeded),
           "rolled-back release cannot read segmented and successor-written records")

    return (
        f"{SUCCESS_PREFIX} scenario=segmented-catalog previous={args.previous_version} "
        f"candidate={args.candidate_version} revision={args.candidate_revision} "
        f"services={','.join(sorted(upgraded))} restarts=0 "
        f"catalog_generation={before_identity['generation_id']} "
        f"catalog_segments={len(before_identity['segments'])} catalog_identity=ok history_api=ok"
    )


HISTORY_DB = "/app/history/history.db"  # HISTORY_SQLITE_PATH in every published Compose file


def kill_during_startup(deployment: Deployment, phase: str, name: str, timeout: int = 180) -> None:
    """Start the history service's own startup in a one-off service container, kill it at `phase`.

    `docker compose run` gives the same image, mounts, user and environment as
    the service. The hook pauses the startup right after the named schema-
    initialization step returns (no commit, no close), and `docker kill` sends
    SIGKILL there, as a host crash or OOM kill would.
    """
    command = ["docker", "compose", *deployment.file_args(), "run", "--rm", "--no-deps", "-T",
               "--name", name, "enclosure-history", "python", "-"]
    process = subprocess.Popen(command, cwd=deployment.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    seen: list[str] = []
    try:
        assert process.stdin is not None
        process.stdin.write(textwrap.dedent(kill_hook_script(phase)))
        process.stdin.close()
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                raise SmokeError(f"history startup never reached {phase}: {''.join(seen)[-2000:]}") from None
            if line is None:
                raise SmokeError(f"history startup exited before {phase}: {''.join(seen)[-2000:]}")
            seen.append(line)
            if KILL_UNREACHED in line:
                raise SmokeError(f"history startup finished without calling {phase}")
            if f"{KILL_MARKER} {phase}" in line:
                break
        run(["docker", "kill", "--signal", "KILL", name])
        process.wait(timeout=60)
        expect(process.returncode != 0, f"killed history container for {phase} exited cleanly")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def interrupted_migration(args: argparse.Namespace) -> str:
    """Kill candidate startup at each schema-initialization seam, then restart normally."""
    root = args.root.resolve()
    prepare_root(root, args.compose_fixture, args.config_fixture)
    deployment = Deployment(root)

    # 1. v0.22.2 state written by v0.22.2 itself.
    _, before_history, seeded = seed_previous_release(deployment, args)

    # 2. Stop history cleanly and pin the new image; nothing else changes.
    deployment.compose("stop", "enclosure-history")
    deployment.set_image(args.candidate_image)
    deployment.compose("pull")

    # 3. The end state an uninterrupted upgrade produces, from a copy.
    reference_dir = root / "reference"
    run(["sudo", "install", "-d", "-m", "0700", "-o", "0", "-g", "0", str(reference_dir)])
    # After a clean stop the WAL is normally checkpointed away; copy it if not.
    run(["sudo", "sh", "-c", 'for f in history.db history.db-wal; do [ ! -e "$1/$f" ] || cp -p "$1/$f" "$2/"; done',
         "sh", str(root / "history"), str(reference_dir)])
    reference = deployment.run_python(
        "enclosure-history",
        end_state_script("/reference/history.db", UNINTERRUPTED_REFERENCE),
        extra=("-v", f"{reference_dir}:/reference"),
    )
    expect(reference["integrity"] == "ok", f"uninterrupted reference startup failed: {reference}")

    # 4. Kill the new release's own startup inside every schema-initialization
    #    step, one after another on the same database, as a crash loop would.
    for index, phase in enumerate(KILL_PHASES):
        kill_during_startup(deployment, phase, f"upgrade-smoke-kill-{os.getpid()}-{index}")

    # 5. The ordinary command finishes it.
    deployment.pull_and_up()
    check_runtime(deployment, args.candidate_image, args.candidate_version, args.candidate_revision)
    after = deployment.exec_python("enclosure-history", end_state_script(HISTORY_DB))
    after_history = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(after["integrity"] == "ok", f"integrity_check after the interrupted startup: {after['integrity']}")
    expect(after == reference, f"interrupted startup differs from an uninterrupted one: {after} != {reference}")
    schema = schema_evidence(before_history, after_history)
    expect(after_history["counts"] == before_history["counts"], f"history rows lost: {after_history}")
    expect(history_api_view((1, 2, 3)) == expected_history_view(seeded),
           "history API does not return the predecessor's records after the interruptions")
    quarantined = run(["sudo", "find", str(root / "history"), "-maxdepth", "1", "-name", "history.db.broken-*"]).strip()
    expect(not quarantined, f"the interrupted database was quarantined: {quarantined}")

    return (
        f"{SUCCESS_PREFIX} scenario=interrupted-migration previous={args.previous_version} "
        f"candidate={args.candidate_version} revision={args.candidate_revision} "
        f"startup_steps_interrupted={len(KILL_PHASES)} "
        f"restarts=0 history_rows={sum(after_history['counts'].values())} integrity=ok "
        f"schema_compatibility=ok schema_before={schema['before']} "
        f"schema_after={schema['after']} schema_transition={schema['transition']} "
        f"matches_uninterrupted=true quarantined=0 history_api=ok"
    )


# Stdlib-only probes do not import settings or initialize another store. Include
# every directory/file, including sidecars and recovery markers, not just row
# counts. Hash binary bytes, never the text-oriented host sha256 helper above.
READ_HISTORY_TREE = """
import hashlib
import json
from pathlib import Path
root = Path("/app/history")
entries = {}
for path in sorted(root.rglob("*")):
    if path.is_symlink() or not (path.is_file() or path.is_dir()):
        raise RuntimeError("unexpected history entry type")
    if path.is_file():
        with path.open("rb") as stream:
            entries[path.relative_to(root).as_posix()] = [path.stat().st_size, hashlib.file_digest(stream, "sha256").hexdigest()]
    else:
        entries[path.relative_to(root).as_posix()] = None
print(json.dumps(entries, sort_keys=True))
"""

READONLY_WRITE_PROBE = """
import errno
import json
import os
from pathlib import Path
path = Path("/app/history/.upgrade-smoke-readonly-probe")
try:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except OSError as exc:
    if exc.errno != errno.EROFS:
        raise
    print(json.dumps({"errno": exc.errno}))
else:
    os.close(descriptor)
    path.unlink()
    raise RuntimeError("required history mount is writable")
"""


ENOSPC_WRITE_PROBE = """
import errno
import json
import os
from pathlib import Path
path = Path("/app/history/.upgrade-smoke-enospc-probe")
try:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except OSError as exc:
    if exc.errno != errno.ENOSPC:
        raise
    print(json.dumps({"errno": exc.errno}))
else:
    os.close(descriptor)
    path.unlink()
    raise RuntimeError("required history mount has free space")
"""


# Exhaust only the fixture's finite inode budget. File creation itself must
# fail with ENOSPC; unused data blocks do not make a full inode table writable.
FILL_ENOSPC_HISTORY = """
import errno
import json
import os
from pathlib import Path
root = Path("/app/history")
space = os.statvfs(root)
if not (0 < space.f_frsize * space.f_blocks <= 8 * 1024 * 1024 and 0 < space.f_files <= 256):
    raise RuntimeError("history is not the bounded fixture filesystem")
filler = root / ".upgrade-smoke-full"
filler.mkdir()
for number in range(257):
    try:
        descriptor = os.open(filler / str(number), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        if exc.errno != errno.ENOSPC:
            raise
        print(json.dumps({"errno": exc.errno}))
        break
    else:
        os.close(descriptor)
else:
    raise RuntimeError("bounded fixture did not exhaust its inodes")
"""

CLEAR_ENOSPC_HISTORY = """
import json
import shutil
shutil.rmtree("/app/history/.upgrade-smoke-full")
print(json.dumps({"ok": True}))
"""


def install_enospc_history_mount(deployment: Deployment) -> None:
    # Create the cleanup marker before mounting, including on partial setup.
    (deployment.root / ENOSPC_FILES[-1]).write_text(json.dumps({
        "services": {"enclosure-history": {"volumes": ["./history:/app/history:rw"]}},
    }) + "\n", encoding="utf-8")
    deployment.files = ENOSPC_FILES
    run(["sudo", "mount", "-t", "tmpfs", "-o", "size=8m,nr_inodes=256", "tmpfs",
         str(deployment.root / "history")])


def install_readonly_history_mount(deployment: Deployment) -> None:
    # Compose merges volumes by container target. Only the history bind changes;
    # retain the published command, restart policy, healthcheck and UI mounts.
    (deployment.root / READONLY_FILES[-1]).write_text(json.dumps({
        "services": {"enclosure-history": {"volumes": ["./history:/app/history:ro"]}},
    }) + "\n", encoding="utf-8")
    deployment.files = READONLY_FILES


def wait_for_history_liveness(version: str, timeout: int = 60, *, fault: str = "read-only") -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            status, payload = get_json(f"http://127.0.0.1:{HISTORY_PORT}/livez")
        except OSError:
            if time.monotonic() >= deadline:
                raise SmokeError(f"{fault} history startup did not become live within 60 seconds") from None
            time.sleep(1)
            continue
        expect(status == 200 and payload.get("status") == "ok" and payload.get("version") == version,
               f"{fault} history /livez does not identify the live candidate")
        return


def check_storage_refusal_runtime(
    deployment: Deployment, args: argparse.Namespace, *, fault: str, readonly: bool,
) -> dict:
    """Local startup refusal is 503/down, not remote-source 200/degraded (#429)."""
    image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", args.candidate_image]).strip()
    revision = run(["docker", "image", "inspect", "--format",
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}', args.candidate_image]).strip()
    expect(revision == args.candidate_revision, f"{fault} candidate revision does not match")
    identity = {}
    for service in SERVICES:
        container = deployment.compose("ps", "-q", service).strip()
        expect(bool(container) and "\n" not in container, f"{service} must resolve to exactly one container")
        state = json.loads(run(["docker", "inspect", "--format", "{{json .}}", container]))
        expect(state["State"]["Status"] == "running" and state["RestartCount"] == 0,
               f"{service} exited or restarted during {fault} refusal")
        expect(state["Image"] == image_id, f"{service} does not run the {fault} candidate")
        identity[service] = [container, state["State"]["StartedAt"]]
        if service == "enclosure-history":
            mounts = [mount for mount in state["Mounts"] if mount["Destination"] == "/app/history"]
            expect(len(mounts) == 1 and mounts[0]["Type"] == "bind" and mounts[0]["RW"] is (not readonly)
                   and mounts[0]["Source"] == str(deployment.root / "history"),
                   f"required history mount is not the seeded {fault} bind")
    for port in (UI_PORT, HISTORY_PORT):
        status, live = get_json(f"http://127.0.0.1:{port}/livez")
        expect(status == 200 and live.get("status") == "ok" and live.get("version") == args.candidate_version,
               f"/livez on {port} does not identify the live candidate")
    status, ui = get_json(f"http://127.0.0.1:{UI_PORT}/healthz")
    expect(status == 200 and ui.get("status") in {"ok", "degraded"}, "safe UI access is unavailable")
    status, health = get_json(f"http://127.0.0.1:{HISTORY_PORT}/healthz", read_error_body=True)
    unavailable = "History storage is unavailable; see the service logs."
    expect(status == 503 and health.get("status") == "down" and health.get("detail") == unavailable
           and str(health.get("reason", "")).startswith("Cannot write to /app/history "),
           f"{fault} history must visibly refuse unavailable local storage, not report healthy recovery")
    if fault == "ENOSPC":
        reason = str(health.get("reason", "")).lower()
        expect("filesystem is full" in reason and "free space or inodes" in reason
               and "./history" in reason and "chown" not in reason,
               "ENOSPC history must report a disk-full diagnosis and space/inode recovery remedy")
    status, overview = get_json(f"http://127.0.0.1:{HISTORY_PORT}/api/history/overview", read_error_body=True)
    expect(status == 503 and overview == {"detail": unavailable},
           f"{fault} history API must refuse rather than serve fresh active history")
    return identity


def check_readonly_runtime(deployment: Deployment, args: argparse.Namespace) -> dict:
    return check_storage_refusal_runtime(deployment, args, fault="read-only", readonly=True)


def readonly_state(args: argparse.Namespace) -> str:
    """Exercise one literal read-only required-state mount on synthetic released state."""
    root = args.root.resolve()
    prepare_root(root, args.compose_fixture, args.config_fixture)
    deployment = Deployment(root)
    config_digest = sha256(root / "config/config.yaml")
    before_ui, before_history, seeded = seed_previous_release(deployment, args)
    deployment.compose("stop", *SERVICES)
    # Snapshot after the released writer closes and checkpoints. Inject no
    # permission repair, fault hook, database edit or alternate storage path.
    install_readonly_history_mount(deployment)
    deployment.set_image(args.candidate_image)
    deployment.compose("pull")
    before_bytes = deployment.run_python("enclosure-history", READ_HISTORY_TREE)
    expect("history.db" in before_bytes and before_bytes["history.db"][0] > 0, "seeded history bytes are missing")
    # --wait would demand healthy storage and hide the intentional 503 behind a
    # Compose timeout. Keep the ordinary up -d and bound the probe separately.
    deployment.compose("up", "-d")
    wait_for_history_liveness(args.candidate_version)
    expect(deployment.exec_python("enclosure-history", READONLY_WRITE_PROBE) == {"errno": errno.EROFS},
           "required history mount did not reject writes with EROFS")
    identity = check_readonly_runtime(deployment, args)
    for _ in range(10):
        time.sleep(2)
        expect(check_readonly_runtime(deployment, args) == identity, "container identity changed during refusal")
    expect(deployment.exec_python("enclosure-history", READ_HISTORY_TREE) == before_bytes,
           "history bytes or entries changed during read-only refusal")
    expect(sha256(root / "config/config.yaml") == config_digest, "config changed during read-only refusal")
    expect(deployment.exec_python("enclosure-ui", READ_UI)["mappings"] == before_ui["mappings"],
           "mappings changed during read-only refusal")

    # Operator removes the injected fault, not an automatic repair. The same
    # candidate must then open the same seeded store through normal startup.
    deployment.compose("stop", *SERVICES)
    deployment.files = ()
    deployment.pull_and_up()
    check_runtime(deployment, args.candidate_image, args.candidate_version, args.candidate_revision)
    after = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(after["integrity"] == "ok" and after["counts"] == before_history["counts"],
           "writable recovery lost predecessor history")
    expect(history_api_view((1, 2, 3)) == expected_history_view(seeded),
           "writable candidate cannot read retained predecessor history")
    return (f"{SUCCESS_PREFIX} scenario=readonly-state previous={args.previous_version} "
            f"candidate={args.candidate_version} revision={args.candidate_revision} "
            "mount=ro write_errno=EROFS livez=ok history_health=down history_http=503 "
            "history_api=refused restarts=0 observation_seconds=20 history_bytes=unchanged "
            "writable_recovery=ok")


def check_enospc_runtime(deployment: Deployment, args: argparse.Namespace) -> dict:
    return check_storage_refusal_runtime(deployment, args, fault="ENOSPC", readonly=False)


def enospc_state(args: argparse.Namespace) -> str:
    """Exhaust a disposable required-state filesystem, then explicitly free it."""
    root = args.root.resolve()
    prepare_root(root, args.compose_fixture, args.config_fixture)
    deployment = Deployment(root)
    config_digest = sha256(root / "config/config.yaml")
    install_enospc_history_mount(deployment)
    before_ui, before_history, seeded = seed_previous_release(deployment, args)
    deployment.compose("stop", *SERVICES)
    # Exhaust only the fixture after the released writer closes and checkpoints.
    expect(deployment.run_python("enclosure-history", FILL_ENOSPC_HISTORY) == {"errno": errno.ENOSPC},
           "required history fixture did not exhaust with ENOSPC")
    deployment.set_image(args.candidate_image)
    deployment.compose("pull")
    before_bytes = deployment.run_python("enclosure-history", READ_HISTORY_TREE)
    expect("history.db" in before_bytes and before_bytes["history.db"][0] > 0, "seeded history bytes are missing")
    # --wait would demand healthy storage and hide the intentional 503 behind a
    # Compose timeout. Keep the ordinary up -d and bound the probe separately.
    deployment.compose("up", "-d")
    wait_for_history_liveness(args.candidate_version, fault="ENOSPC")
    expect(deployment.exec_python("enclosure-history", ENOSPC_WRITE_PROBE) == {"errno": errno.ENOSPC},
           "required history mount did not reject writes with ENOSPC")
    identity = check_enospc_runtime(deployment, args)
    for _ in range(10):
        time.sleep(2)
        expect(check_enospc_runtime(deployment, args) == identity, "container identity changed during refusal")
    expect(deployment.exec_python("enclosure-history", READ_HISTORY_TREE) == before_bytes,
           "history bytes or entries changed during ENOSPC refusal")
    expect(sha256(root / "config/config.yaml") == config_digest, "config changed during ENOSPC refusal")
    expect(deployment.exec_python("enclosure-ui", READ_UI)["mappings"] == before_ui["mappings"],
           "mappings changed during ENOSPC refusal")

    # Operator removes the injected fault, not an automatic repair. The same
    # candidate must then open the same seeded store through normal startup.
    deployment.compose("stop", *SERVICES)
    expect(deployment.run_python("enclosure-history", CLEAR_ENOSPC_HISTORY) == {"ok": True},
           "required history fixture could not be freed")
    deployment.files = ()
    # The bind stays writable; discard the refused processes so normal startup
    # opens the same retained store again after the operator frees space.
    deployment.compose("rm", "-f", *SERVICES)
    deployment.pull_and_up()
    check_runtime(deployment, args.candidate_image, args.candidate_version, args.candidate_revision)
    after = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(after["integrity"] == "ok" and after["counts"] == before_history["counts"],
           "writable recovery lost predecessor history")
    expect(history_api_view((1, 2, 3)) == expected_history_view(seeded),
           "writable candidate cannot read retained predecessor history")
    deployment.exec_python("enclosure-history", seed_script(SEED_HISTORY, (4,), metrics_per_slot=1))
    seeded[4] = 1
    expect(history_api_view((1, 2, 3, 4)) == expected_history_view(seeded),
           "writable candidate did not retain its new history write")
    written = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(written["integrity"] == "ok" and written["counts"] == {
        "slot_state_current": 4, "slot_events": 4, "metric_samples": 13,
    }, "writable candidate history write failed integrity or count checks")
    return (f"{SUCCESS_PREFIX} scenario=enospc-state previous={args.previous_version} "
            f"candidate={args.candidate_version} revision={args.candidate_revision} "
            "mount=rw fixture=tmpfs inode_budget=256 write_errno=ENOSPC livez=ok history_health=down history_http=503 "
            "history_api=refused restarts=0 observation_seconds=20 history_bytes=unchanged "
            "writable_recovery=ok")


CLEAN_HOST_GROUPS = ("config_file", "mapping_file", "history_db")
CLEAN_HOST_ADMIN_PORT = 18182
CLEAN_HOST_AUTH = "Basic " + base64.b64encode(b"operator:synthetic-clean-host-auth").decode("ascii")

# Read-only probes. History identity is logical, not a promise that SQLite file
# bytes survive supported snapshot/restore/startup. Config and mappings are exact.
READ_CLEAN_FILES = """
import hashlib, json
from pathlib import Path
print(json.dumps({name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in
    (("config_sha256", "/app/config/config.yaml"), ("mapping_sha256", "/app/data/slot_mappings.json"))}))
"""
READ_CLEAN_HISTORY = f"""
import hashlib, json, sqlite3
connection = sqlite3.connect("file:/app/history/history.db?mode=ro", uri=True)
try:
    rows = {{}}
    for table, columns in (("slot_state_current", "system_id,enclosure_key,slot,serial,device_name"),
                           ("slot_events", "system_id,enclosure_key,slot,serial,event_type,current_value,observed_at"),
                           ("metric_samples", "system_id,enclosure_key,slot,serial,metric_name,value_integer,observed_at")):
        values = sorted(connection.execute(f"SELECT {{columns}} FROM {{table}} WHERE system_id=?",
                                           ("{SMOKE_SYSTEM}",)).fetchall())
        rows[table] = [len(values), hashlib.sha256(json.dumps(values).encode()).hexdigest()]
    print(json.dumps({{"history_rows": rows}}))
finally:
    connection.close()
"""


def admit_clean_host_root(root: Path, *, consent: bool) -> None:
    expect(consent, "clean-host restore needs --disposable-clean-host-restore on an exclusive disposable host")
    expect(root.is_absolute() and root == root.resolve() and root.name.startswith("jbod-clean-host-")
           and root.parent.is_dir() and root.is_relative_to(Path(tempfile.gettempdir()).resolve()),
           "clean-host root must be an absent nonsymlink jbod-clean-host-* directory below TMPDIR")
    expect(not root.exists(), "clean-host root already exists; refusing to replace recovery files")
    root.mkdir(mode=0o700)


def clean_host_absent(project: str, env: dict[str, str]) -> None:
    commands = [
        ["docker", "container", "ls", "--all", "--format", "{{.Names}}"],
        ["docker", "container", "ls", "--all", "--filter", f"label=com.docker.compose.project={project}",
         "--format", "{{.Names}}"],
        *(["docker", kind, "ls", "--filter", f"label=com.docker.compose.project={project}",
           "--format", "{{.Name}}"] for kind in ("network", "volume")),
    ]
    unsafe = False
    for index, command in enumerate(commands):
        try:
            output = run(command, env=env, timeout=30).splitlines()
        except (SmokeError, OSError, subprocess.SubprocessError):
            unsafe = True
            continue
        unsafe |= any(name in {"truenas-jbod-ui", "truenas-jbod-history", "truenas-jbod-admin"}
                      for name in output) if index == 0 else bool(output)
    expect(not unsafe, "clean-host resources remain or census is unknown; retain recovery files")


def clean_host_down(deployment: Deployment, project: str, env: dict[str, str]) -> None:
    failed = False
    try:
        deployment.compose("--profile", "history", "--profile", "admin", "down", "--volumes")
    except (SmokeError, OSError, subprocess.SubprocessError):
        failed = True
    clean_host_absent(project, env)  # independent readback even after failed down
    expect(not failed, "clean-host teardown failed; retain recovery files")


def clean_host_post(path: str, body: bytes, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(f"http://127.0.0.1:{CLEAN_HOST_ADMIN_PORT}" + path, data=body,
                                     headers={"Authorization": CLEAN_HOST_AUTH, **headers}, method="POST")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=180) as response:
            return json.load(response)
    except (OSError, ValueError) as exc:
        raise SmokeError("clean-host admin request failed") from exc


def clean_host_import(archive: Path, passphrase: str) -> None:
    expect(0 < archive.stat().st_size <= 16 * 1024 * 1024, "clean-host archive exceeds its 16 MiB bound")
    body = archive.read_bytes()  # bounded synthetic fixture only, never production scale
    headers = {"Content-Type": "application/octet-stream",
               "X-Backup-Passphrase-Base64": base64.b64encode(passphrase.encode()).decode("ascii")}
    inspection = clean_host_post("/api/admin/backup/inspect", body, headers)
    expect(inspection.get("ok") is True and inspection.get("encrypted") is True
           and inspection.get("encryption_mode") == "encrypted" and bool(inspection.get("inspection_receipt"))
           and set(inspection.get("selected_groups", [])) == set(CLEAN_HOST_GROUPS)
           and set(inspection.get("present_groups", [])) == set(CLEAN_HOST_GROUPS)
           and inspection.get("absent_groups") == [], "clean-host encrypted inspection is incomplete")
    result = clean_host_post("/api/admin/backup/import?stop_services=true&restart_services=false", body, {
        **headers, "X-Backup-Expected-Encryption": "encrypted",
        "X-Backup-Inspection-Receipt": inspection["inspection_receipt"],
    })
    expect(result.get("ok") is True and set(result.get("included_groups", [])) == set(CLEAN_HOST_GROUPS),
           "clean-host import did not restore all required groups")


def check_clean_host_identity(before: dict, after: dict) -> None:
    expect(before == after, "clean-host restore changed config/mapping bytes or history counts/identity")


class CleanHostDeployment(Deployment):
    def __init__(self, root: Path, project: str, env: dict[str, str]) -> None:
        super().__init__(root)
        self.project = project
        self.environment = env

    def file_args(self) -> list[str]:
        return ["--project-name", self.project, "-f", "docker-compose.yml"]


def clean_host_restore(args: argparse.Namespace) -> str:
    """One archive-only synthetic recovery cell, not support or human acceptance."""
    repository = Path(__file__).resolve().parents[1]
    for supplied, expected in ((args.compose_fixture, repository / "tests/fixtures/compose/v0.22.2.yml"),
                               (args.config_fixture, repository / "tests/fixtures/ci-smoke-config.yaml")):
        expect(supplied.read_bytes() == expected.read_bytes(), "clean-host requires the maintained synthetic fixtures")
    root = args.root.absolute()
    admit_clean_host_root(root, consent=args.disposable_clean_host_restore)
    client = root / "docker-client"
    client.mkdir(mode=0o700)
    env = {"PATH": os.environ.get("PATH", os.defpath), "DOCKER_HOST": "unix:///var/run/docker.sock",
           "DOCKER_CONFIG": str(client), "APP_BIND_ADDRESS": "127.0.0.1"}
    project = root.name
    clean_host_absent(project, env)  # before daemon mutation or teardown ownership
    recovery = root / "recovery"
    recovery.mkdir(mode=0o700)
    passphrase = os.urandom(32).hex()  # synthetic key input, never a real credential
    key = recovery / "operator-key-input"
    key.write_text(passphrase, encoding="utf-8")
    key.chmod(0o600)
    archive = recovery / "backup.7z"
    deployment = CleanHostDeployment(root / "source", project, env)
    completed = False
    try:
        prepare_root(deployment.root, args.compose_fixture, args.config_fixture)
        deployment.image = args.previous_image
        deployment.runtime_environment.update({"ADMIN_PORT": str(CLEAN_HOST_ADMIN_PORT),
            "ADMIN_BIND_ADDRESS": "127.0.0.1", "ADMIN_AUTH_MODE": "basic", "ADMIN_AUTH_USERNAME": "operator",
            "ADMIN_AUTH_PASSWORD": "synthetic-clean-host-auth"})
        seed_previous_release(deployment, args)
        deployment.compose("stop", *SERVICES)
        before = {**deployment.run_python("enclosure-ui", READ_CLEAN_FILES),
                  **deployment.run_python("enclosure-history", READ_CLEAN_HISTORY)}
        deployment.compose("--profile", "admin", "up", "-d", "--wait", "enclosure-admin")
        payload = json.dumps({"encrypt": True, "passphrase": passphrase, "packaging": "7z",
                              "included_paths": list(CLEAN_HOST_GROUPS)}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{CLEAN_HOST_ADMIN_PORT}/api/admin/backup/export"
                                         "?stop_services=true&restart_services=false", data=payload,
                                         headers={"Authorization": CLEAN_HOST_AUTH, "Content-Type": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=180) as response:
            content = response.read(16 * 1024 * 1024 + 1)
        expect(content.startswith(b"7z\xbc\xaf\x27\x1c") and len(content) <= 16 * 1024 * 1024,
               "predecessor did not export the bounded supported 7z archive")
        archive.write_bytes(content)
        archive.chmod(0o600)
        del payload, content, request
        clean_host_down(deployment, project, env)
        run(["sudo", "rm", "-rf", "--", str(deployment.root)], env=env)
        expect(not deployment.root.exists(), "original installation was not removed")
        # New current Compose and bootstrap fixture, never copied source state.
        deployment = CleanHostDeployment(root / "target", project, env)
        bootstrap = root / "fresh-bootstrap.yaml"
        bootstrap.write_bytes(args.config_fixture.read_bytes().replace(b"WARNING", b"ERROR"))
        expect(bootstrap.read_bytes() != args.config_fixture.read_bytes(), "fresh bootstrap must differ from source config")
        prepare_root(deployment.root, repository / "docker-compose.yml", bootstrap)
        bootstrap.unlink()
        deployment.image = args.candidate_image
        deployment.runtime_environment.update({"ADMIN_PORT": str(CLEAN_HOST_ADMIN_PORT),
            "ADMIN_BIND_ADDRESS": "127.0.0.1", "ADMIN_AUTH_MODE": "basic", "ADMIN_AUTH_USERNAME": "operator",
            "ADMIN_AUTH_PASSWORD": "synthetic-clean-host-auth"})
        deployment.set_image(args.candidate_image)
        deployment.compose("--profile", "admin", "pull", "enclosure-admin")
        deployment.compose("--profile", "admin", "up", "-d", "--wait", "enclosure-admin")
        clean_host_import(archive, key.read_text(encoding="utf-8"))
        after = {**deployment.run_python("enclosure-ui", READ_CLEAN_FILES),
                 **deployment.run_python("enclosure-history", READ_CLEAN_HISTORY)}
        check_clean_host_identity(before, after)
        deployment.pull_and_up()
        check_runtime(deployment, args.candidate_image, args.candidate_version, args.candidate_revision)
        expect(history_api_view((1, 2, 3)) == expected_history_view({1: 4, 2: 4, 3: 4}),
               "clean-host history API lost predecessor identity")
        after_history = deployment.exec_python("enclosure-history", READ_HISTORY)
        expect(after_history["integrity"] == "ok" and after_history["counts"] == {
            "slot_state_current": 3, "slot_events": 3, "metric_samples": 12}, "clean-host counts/integrity failed")
        deployment.compose("restart", *SERVICES)
        deployment.compose("up", "-d", "--wait", "--wait-timeout", "300")
        check_runtime(deployment, args.candidate_image, args.candidate_version, args.candidate_revision)
        check_clean_host_identity(before, {**deployment.exec_python("enclosure-ui", READ_CLEAN_FILES),
                                          **deployment.exec_python("enclosure-history", READ_CLEAN_HISTORY)})
        completed = True
    finally:
        # Partial startup owns resources too. Never erase recovery material after
        # failure, failed teardown, failed list queries, or residual resources.
        clean_host_down(deployment, project, env)
        if completed:
            run(["sudo", "rm", "-rf", "--", str(root)], env=env)
            expect(not root.exists(), "clean-host fixture cleanup failed")
    return (f"{SUCCESS_PREFIX} scenario=clean-host-restore previous={args.previous_version} "
            f"candidate={args.candidate_version} revision={args.candidate_revision} "
            "archive=7z encrypted=true original_installation=removed mappings=3 history_rows=18 "
            "state_identity=ok integrity=ok history_api=ok restart=ok cleanup=verified human_acceptance=unverified")


# Synthetic config change the released MappingStore writer journals for the
# scheduler; each qualification phase changes the notes so the canonical config
# hash differs and the coalescer takes a real backup instead of a noop.
CONFIG_CHANGE_PROBE = f"""
import json
from app.config import get_settings
from app.models.domain import ManualMapping
from app.services.mapping_store import MappingStore
store = MappingStore(get_settings().paths.mapping_file)
store.save_mapping(ManualMapping(system_id="{SMOKE_SYSTEM}", enclosure_id="shelf-a", slot=4,
                                 serial="SYNTH-0004", notes=NOTES))
print(json.dumps({{"ok": True}}))
"""

# Appends one pending change through the released journal writer, inside the
# scheduler container (the only container bound to ./backup-journal).
BACKUP_JOURNAL_APPEND = """
import json
from pathlib import Path
from history_service.backup_archive.journal import ChangeJournal
journal = ChangeJournal(Path("/app/backup-journal/config-changes.jsonl"), file_mode=0o660)
entry = journal.append("mapping.save", SUBJECT)
print(json.dumps({"change_id": entry.change_id}))
"""


def config_change_script(notes: str) -> str:
    return CONFIG_CHANGE_PROBE.replace("NOTES", repr(notes))


def journal_append_script(notes: str) -> str:
    return BACKUP_JOURNAL_APPEND.replace("SUBJECT", repr(notes))


# The candidate scheduler service as the overlay defines it. The scheduler is
# absent from v0.22.2, so this is the whole service definition: the published
# shape (read-only rootfs, dropped capabilities, socket API healthcheck) with a
# bounded config-backup policy and one root-owned filesystem target whose root
# the scenario can make unavailable through the existing bind mount.
BACKUP_SCHEDULER_OVERLAY = {
    "services": {
        "enclosure-backup-scheduler": {
            "image": "${JBOD_UI_IMAGE:-ghcr.io/gcs8/truenas-jbod-ui:latest}",
            "command": ["python", "-m", "history_service.backup_scheduler.main"],
            "user": "0:0",
            "read_only": True,
            "tmpfs": ["/tmp"],
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "group_add": ["10001"],
            "environment": {
                "TMPDIR": "/app/backups",
                "APP_LOG_LEVEL": "INFO",
                "APP_GID": "10001",
                "LOG_FORMAT": "text",
                "HISTORY_SQLITE_PATH": "/app/history/history.db",
                "BACKUP_ARCHIVE_DIR": "/app/backups/archive",
                "BACKUP_ARCHIVE_STATE_DIR": "/app/backups/archive-state",
                "BACKUP_JOURNAL_PATH": "/app/backup-journal/config-changes.jsonl",
                "BACKUP_ARCHIVE_STATUS_FILE": "/app/backup-status/backup-archive.json",
                "BACKUP_SCHEDULER_SOCKET": "/app/backup-api/scheduler.sock",
                "BACKUP_CONFIG_ENABLED": "true",
                "BACKUP_CONFIG_DEBOUNCE_SECONDS": "2",
                "BACKUP_CONFIG_MAX_DELAY_SECONDS": "10",
                "SCHEDULED_BACKUP_PASSPHRASE_FILE": "/app/config/backup-secrets/backup-passphrase",
                "BACKUP_TARGETS_JSON": '[{"target_id":"smoke-nas","provider":"filesystem","root":"/app/backups/targets/remote"}]',
            },
            "volumes": [
                "./config:/app/config:ro",
                "./data:/app/data:ro",
                "./history:/app/history",
                "./backups:/app/backups",
                "./backup-status:/app/backup-status",
                "./backup-journal:/app/backup-journal",
                "./backup-api:/app/backup-api",
                "./backup-targets:/app/backups/targets",
                "./config/ssh:/run/ssh:ro",
            ],
            "restart": "unless-stopped",
            "mem_limit": "1g",
            "healthcheck": {
                "test": ["CMD", "python", "-c",
                         "import os, stat, sys; sys.exit(0 if stat.S_ISSOCK(os.stat('/app/backup-api/scheduler.sock').st_mode) else 1)"],
                "interval": "30s",
                "timeout": "5s",
                "retries": 3,
                "start_period": "30s",
            },
        },
    },
}


def install_backup_secrets(root: Path) -> None:
    """Create the scheduler's private passphrase before the released writers run.

    prepare_root leaves ./config root-owned, and the seeded v0.22.2 containers
    harden it further while they run, so this must happen before seeding, as
    root. The value is synthetic and exists only at run time.
    """
    secrets = root / "config" / "backup-secrets"
    run(["sudo", "install", "-d", "-o", "0", "-g", "0", "-m", "0700", str(secrets)])
    temporary = root / "backup-passphrase.tmp"
    # Private from creation: no umask-dependent window before a later chmod.
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="ascii") as handle:
        handle.write(uuid.uuid4().hex + "\n")
    run(["sudo", "install", "-m", "0600", "-o", "0", "-g", "0",
         str(temporary), str(secrets / "backup-passphrase")])
    temporary.unlink()


def install_backup_scheduler_with_target(deployment: Deployment) -> None:
    """Add the candidate's backup scheduler service with one available target."""
    root = deployment.root
    (root / UNAVAILABLE_BACKUP_FILES[-1]).write_text(json.dumps(BACKUP_SCHEDULER_OVERLAY) + "\n", encoding="utf-8")
    # write_shared_status demands root:APP_GID mode 2750 (chown first: chown
    # clears the setgid bit); _prepare_socket demands no access for others.
    run(["sudo", "chown", "0:10001", str(root / "backup-status")])
    run(["sudo", "chmod", "2750", str(root / "backup-status")])
    for name, mode in (("backups", "0755"), ("backup-journal", "0755"),
                       ("backup-api", "2750"), ("backup-targets", "0755")):
        run(["sudo", "install", "-d", "-o", "0", "-g", "0", "-m", mode, str(root / name)])
    run(["sudo", "install", "-d", "-o", "0", "-g", "0", "-m", "0755", str(root / "backup-targets" / "remote")])
    deployment.files = UNAVAILABLE_BACKUP_FILES


def make_backup_target_unavailable(root: Path) -> float:
    """The one injected condition: the target root is not a directory.

    Host-side rename plus file swap inside the existing bind, so the scheduler
    sees the same condition an operator's storage failure leaves behind.
    Returns the host injection timestamp: the readback window starts there,
    because the refused archive's build predates its own refusal record.
    """
    run(["sudo", "mv", str(root / "backup-targets" / "remote"), str(root / "backup-targets" / "remote.held")])
    run(["sudo", "install", "-m", "0644", "-o", "0", "-g", "0", "/dev/null", str(root / "backup-targets" / "remote")])
    return time.time()


def make_backup_target_available(root: Path) -> float:
    """The operator's explicit fix: the real target root directory back.

    Returns the host restore timestamp: the readback window ends there, and
    every archive built at/after it must ship (the recovery proof).
    """
    run(["sudo", "rm", str(root / "backup-targets" / "remote")])
    run(["sudo", "mv", str(root / "backup-targets" / "remote.held"), str(root / "backup-targets" / "remote")])
    return time.time()


def read_backup_archive_status(root: Path) -> dict:
    """Read the scheduler's secret-free status file the way /healthz reads it."""
    text = run(["sudo", "cat", str(root / "backup-status" / "backup-archive.json")])
    status = json.loads(text)
    expect(isinstance(status, dict) and status.get("schema_version") == 1
           and isinstance(status.get("classes"), dict) and isinstance(status.get("targets"), dict),
           f"backup archive status has an unexpected shape: {status!r}")
    return status


# The class-record detail shape `_run_class` writes after `_ship` isolates the
# remote failures; shared by the refusal validator and the wait's coherence pin.
REMOTE_COPY_FAILURE_DETAIL = re.compile(r"[0-9]+ remote copy failure\(s\)")


def _coherent_refusal_status(status: dict, previous_class_at: str) -> bool:
    """Whether `_run_class`'s class record for this failure has landed.

    Between `_ship`'s status write (fresh target failure, stale class record)
    and `_record_class`'s write, the file names the refusal without the
    local-success/remote-failure isolation; a verdict read there would reject
    the scheduler's own bookkeeping. The coherent class record is the refused
    run's own record, so its `at` is the archive BUILD time and predates the
    target record's ship-time `at` — never compare the two. Coherence is the
    isolation detail shape plus a class record that does not regress behind
    what earlier polls of this wait already saw.
    """
    class_record = (status.get("classes") or {}).get("config") or {}
    return (class_record.get("ok") is True
            and str(class_record.get("at") or "") >= previous_class_at
            and REMOTE_COPY_FAILURE_DETAIL.fullmatch(str(class_record.get("detail") or "")) is not None)


def wait_for_backup_target_status(root: Path, *, want_ok: bool, after: str = "", timeout: int = 90) -> dict:
    """Poll for a target record newer than `after` whose `ok` equals `want_ok`.

    A refusal is returned only with its matching class record (coherent
    snapshot); otherwise the poll keeps going, still deadline-bounded.
    """
    deadline = time.monotonic() + timeout
    previous_class_at = ""
    while True:
        try:
            status = read_backup_archive_status(root)
        except (SmokeError, ValueError):
            # A torn or malformed status file is no fresh status yet (JSON
            # decode errors are ValueErrors), never a failed qualification.
            status = {}
        record = status.get("targets", {}).get(BACKUP_TARGET_ID) or {}
        at = str(record.get("at") or "")
        if at > after and bool(record.get("ok")) is want_ok:
            if want_ok or _coherent_refusal_status(status, previous_class_at):
                return status
        previous_class_at = (str((status.get("classes") or {}).get("config", {}).get("at") or "")
                             or previous_class_at)
        if time.monotonic() >= deadline:
            raise SmokeError(
                f"backup target {BACKUP_TARGET_ID!r} never reported "
                f"{'a shipped copy' if want_ok else 'a refusal'} within {timeout} seconds: {record}"
            )
        time.sleep(2)


def check_backup_scheduler_runtime(deployment: Deployment) -> dict:
    """The scheduler stays up through a refused target: running, never restarted."""
    container = deployment.compose("ps", "-q", "enclosure-backup-scheduler").strip()
    expect(bool(container) and "\n" not in container, "scheduler must resolve to exactly one container")
    state = json.loads(run(["docker", "inspect", "--format", "{{json .}}", container]))
    expect(state["State"]["Status"] == "running", "the backup scheduler container is not running")
    expect(state["RestartCount"] == 0,
           f"the backup scheduler restarted {state['RestartCount']} times during the refusal")
    return {"enclosure-backup-scheduler": [container, state["State"]["StartedAt"]]}


def check_backup_target_refusal(status: dict, *, target_id: str = BACKUP_TARGET_ID) -> None:
    """Pin the scheduler's own failure classification for an unusable target root.

    The per-target record must name the target and carry the archives module's
    own diagnosis, never a sibling fault class, while the class record proves
    the local archive copy succeeded and only the remote copy failed.
    """
    record = (status.get("targets") or {}).get(target_id) or {}
    expect(record.get("ok") is False, f"backup target {target_id!r} must be refused, not reported ok: {record}")
    detail = str(record.get("detail") or "")
    expect(detail.startswith("ConfigurationError: "),
           f"the refusal detail must name the failing check type, got: {detail!r}")
    expect(f"Filesystem archive target {target_id!r}: " in detail,
           f"the refusal detail must attribute the failure to the target, got: {detail!r}")
    expect("must resolve through directories only" in detail,
           "the refusal detail must carry the scheduler's own diagnosis for a target root "
           f"that is not a directory, got: {detail!r}")
    expect("filesystem is full" not in detail and "Cannot write to /app/history" not in detail,
           f"the refusal must not borrow a sibling fault class: {detail!r}")
    class_record = (status.get("classes") or {}).get("config") or {}
    expect(class_record.get("ok") is True and bool(class_record.get("artifact_id")),
           f"the local config backup must still succeed with a refused target: {class_record}")
    # The count of refused ship attempts inside one run is the scheduler's own
    # bookkeeping; the operator contract is the shape: a local archive kept and
    # the remote failure isolated into the per-class detail.
    expect(REMOTE_COPY_FAILURE_DETAIL.fullmatch(str(class_record.get("detail"))),
           f"the class record must isolate the remote copy failures: {class_record}")


def check_backup_target_recovery(status: dict, *, target_id: str = BACKUP_TARGET_ID) -> dict:
    """The success surface after the explicit fix: shipped, verified, attributed."""
    record = (status.get("targets") or {}).get(target_id) or {}
    expect(record.get("ok") is True, f"backup target {target_id!r} must ship after the fix, got: {record}")
    expect(bool(record.get("artifact_id")), f"the recovered copy must name its artifact: {record}")
    expect(not record.get("detail"), f"the recovered copy must carry no failure detail: {record}")
    return record


def _window_bound(bound: float | str) -> datetime:
    """A window bound is the host injection epoch or an ISO status instant."""
    if isinstance(bound, (int, float)):
        return datetime.fromtimestamp(bound, tz=timezone.utc)
    return datetime.fromisoformat(str(bound)).astimezone(timezone.utc)


def check_backup_target_readback(root: Path, *, window_start: float | str, window_end: float | str) -> dict:
    """The target holds exactly the local archive minus the refusal-window artifacts.

    The runner keeps its own bookkeeping (``.scheduled-backup.lock``) beside the
    artifacts, so both sides are filtered to the released artifact-name shape
    first. The window starts at the raw host injection instant (the refused
    archive's build predates its own refusal record) and ends at the observed
    recovery record; artifact name stamps are whole-second truncations of the
    same host kernel clock.
    """
    remote_root = root / "backup-targets" / "remote"
    local_root = root / "backups" / "archive"
    start = _window_bound(window_start)
    end = _window_bound(window_end)

    def artifact_stamp(relative_name: str) -> datetime | None:
        match = ARCHIVE_ARTIFACT_NAME.search(Path(relative_name).name)
        if match is None:
            return None
        return datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

    def digest_tree(base: Path) -> dict[str, str]:
        listing = run(["sudo", "find", str(base), "-type", "f", "-printf", "%P\n"])
        return {name: sha256(base / name) for name in listing.splitlines() if name}

    remote_files = digest_tree(remote_root)
    expect(bool(remote_files), "the recovered target holds no shipped backup bytes")
    remote_artifacts = {}
    for name, digest in remote_files.items():
        stamp = artifact_stamp(name)
        expect(stamp is not None,
               f"the target must hold only shipped archive artifacts, saw {name!r}")
        remote_artifacts[name] = stamp
    local_files = digest_tree(local_root)
    local_artifacts = {name: artifact_stamp(name) for name in local_files}
    local_artifacts = {name: stamp for name, stamp in local_artifacts.items() if stamp is not None}
    missing = set(local_artifacts) - set(remote_artifacts)
    allowed_missing = {name for name in missing
                       if local_artifacts[name] is not None and start <= local_artifacts[name] < end}
    expect(bool(allowed_missing), "the refusal window produced no unshipped artifact")
    for name in sorted(missing):
        stamp = local_artifacts[name]
        expect(stamp is not None and stamp >= start,
               f"missing artifact {name!r} is not attributable to the refusal window: "
               f"{stamp} not within [{start}, {end})")
    # The recovery proof: everything built at/after the restore instant must be
    # on the target (the recovery build runs at least a debounce interval after
    # the restore, so its second-resolution stamp cannot predate the restore).
    for name, stamp in sorted(local_artifacts.items()):
        if stamp is not None and stamp >= end:
            expect(name in remote_files,
                   f"the post-restore artifact {name!r} must be shipped byte-identically")
    for name, digest in remote_files.items():
        expect(name in local_files and local_files[name] == digest,
               f"target copy {name!r} does not byte-match the local archive copy")
    return remote_files


def record_backup_phase_change(deployment: Deployment, notes: str) -> None:
    """One real config change plus its journal entry, through the released writers."""
    deployment.exec_python("enclosure-ui", config_change_script(notes))
    deployment.exec_python("enclosure-backup-scheduler", journal_append_script(notes))


def unavailable_backup_storage(args: argparse.Namespace) -> str:
    """Exercise one unavailable filesystem backup target on synthetic released state."""
    root = args.root.resolve()
    prepare_root(root, args.compose_fixture, args.config_fixture)
    deployment = Deployment(root)
    config_digest = sha256(root / "config/config.yaml")
    # The scheduler's config-side state exists before the released writers
    # harden ./config; the seed phase runs without the scheduler overlay.
    install_backup_secrets(root)
    before_ui, before_history, seeded = seed_previous_release(deployment, args)
    deployment.compose("stop", *SERVICES)

    # The scheduler joins only through the overlay; v0.22.2 has no such service.
    install_backup_scheduler_with_target(deployment)
    deployment.set_image(args.candidate_image)
    deployment.compose("pull")
    # Nothing is faulted yet, so the literal update procedure converges every
    # service, including the scheduler's socket healthcheck.
    deployment.pull_and_up()
    upgraded = check_runtime(deployment, args.candidate_image, args.candidate_version, args.candidate_revision)

    # Success surface before the condition: one config backup ships to the target.
    record_backup_phase_change(deployment, "upgrade-smoke-backup-before")
    shipped = wait_for_backup_target_status(root, want_ok=True)
    check_backup_target_recovery(shipped)
    shipped_record = shipped["targets"][BACKUP_TARGET_ID]

    # Like readonly-state and enospc-state, snapshot with the history service
    # stopped: its collector and retention passes write on their own clock, so
    # the guard holds only while the scheduler is the one live writer of
    # ./history. Inject no history edit and no alternate storage path.
    deployment.compose("stop", "enclosure-history")
    before_bytes = deployment.run_python("enclosure-history", READ_HISTORY_TREE)
    expect("history.db" in before_bytes and before_bytes["history.db"][0] > 0, "seeded history bytes are missing")

    # The condition: the target root is not a directory for the next backup.
    fault_injected_at = make_backup_target_unavailable(root)
    record_backup_phase_change(deployment, "upgrade-smoke-backup-during")
    refused = wait_for_backup_target_status(root, want_ok=False, after=shipped_record["at"])
    check_backup_target_refusal(refused)
    refused_record = refused["targets"][BACKUP_TARGET_ID]

    identity = check_backup_scheduler_runtime(deployment)
    for _ in range(10):
        time.sleep(2)
        expect(check_backup_scheduler_runtime(deployment) == identity, "scheduler identity changed during the refusal")

    expect(deployment.run_python("enclosure-history", READ_HISTORY_TREE) == before_bytes,
           "history bytes or entries changed during the backup-target refusal")
    deployment.compose("up", "-d", "--wait", "--wait-timeout", "300", "enclosure-history", timeout=420)
    expect(sha256(root / "config/config.yaml") == config_digest, "config changed during the backup-target refusal")
    during_ui = deployment.exec_python("enclosure-ui", READ_UI)
    expect(during_ui["mappings"] == before_ui["mappings"] + [["shelf-a", 4, "SYNTH-0004", "upgrade-smoke-backup-during"]],
           "mappings changed beyond the qualification's own phase change during the refusal")

    # The operator's explicit fix, not an automatic repair. The same candidate
    # must then ship to the restored target and read back what it wrote.
    restored_at = make_backup_target_available(root)
    record_backup_phase_change(deployment, "upgrade-smoke-backup-after")
    recovered = wait_for_backup_target_status(root, want_ok=True, after=refused_record["at"])
    check_backup_target_recovery(recovered)
    check_backup_target_readback(root,
                                 window_start=fault_injected_at,
                                 window_end=restored_at)

    check_runtime(deployment, args.candidate_image, args.candidate_version, args.candidate_revision)
    after = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(after["integrity"] == "ok" and after["counts"] == before_history["counts"],
           "the candidate lost predecessor history across the backup-target qualification")
    expect(history_api_view((1, 2, 3)) == expected_history_view(seeded),
           "the candidate cannot read retained predecessor history after the qualification")
    after_ui = deployment.exec_python("enclosure-ui", READ_UI)
    expect(after_ui["mappings"] == before_ui["mappings"] + [["shelf-a", 4, "SYNTH-0004", "upgrade-smoke-backup-after"]],
           "mappings after recovery are not the seeded three plus the phase change")

    return (f"{SUCCESS_PREFIX} scenario=unavailable-backup-storage previous={args.previous_version} "
            f"candidate={args.candidate_version} revision={args.candidate_revision} "
            f"services={','.join(sorted(upgraded))} restarts=0 "
            "target_refusals=1 target_refusal=ConfigurationError target_recovery=ok "
            "backup_local_kept=ok target_readback=ok history_bytes=unchanged config_bytes=unchanged")


def smoke(args: argparse.Namespace) -> str:
    if args.scenario == "enospc-state":
        return enospc_state(args)
    if args.scenario == "readonly-state":
        return readonly_state(args)
    if args.scenario == "unavailable-backup-storage":
        return unavailable_backup_storage(args)
    if args.scenario == "interrupted-migration":
        return interrupted_migration(args)
    if args.scenario == "segmented-catalog":
        return segmented_catalog_upgrade(args)
    return upgrade_and_rollback(args, hardened=args.scenario == "hardened")


def compose_chain(root: Path) -> list[str]:
    if (root / ENOSPC_FILES[-1]).exists():
        return [arg for name in ENOSPC_FILES for arg in ("-f", name)]
    if (root / UNAVAILABLE_BACKUP_FILES[-1]).exists():
        return [arg for name in UNAVAILABLE_BACKUP_FILES for arg in ("-f", name)]
    if (root / READONLY_FILES[-1]).exists():
        return [arg for name in READONLY_FILES for arg in ("-f", name)]
    if (root / "docker-compose.nonroot.yml").exists():
        return [arg for name in HARDENED_FILES for arg in ("-f", name)]
    return []


def cleanup(root: Path) -> None:
    if (root / "docker-compose.yml").exists():
        subprocess.run(["docker", "compose", *compose_chain(root), "down", "--volumes", "--remove-orphans"],
                       cwd=root, check=False)
    if (root / ENOSPC_FILES[-1]).exists() and os.path.ismount(root / "history"):
        result = subprocess.run(["sudo", "umount", str(root / "history")], check=False)
        expect(result.returncode == 0, "ENOSPC fixture unmount failed; leaving the deployment root intact")
    subprocess.run(["sudo", "rm", "-rf", str(root)], check=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="Disposable deployment directory.")
    parser.add_argument("--compose-fixture", type=Path, required=True, help="The previous release's Compose file.")
    parser.add_argument("--config-fixture", type=Path, required=True)
    parser.add_argument("--previous-image", required=True)
    parser.add_argument("--previous-version", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--candidate-version", required=True)
    parser.add_argument("--candidate-revision", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="base")
    parser.add_argument("--disposable-clean-host-restore", action="store_true",
                        help="Admit fixture destruction on an exclusive disposable local Docker host.")
    parser.add_argument("--nonroot-fixture", type=Path,
                        help="The previous release's docker-compose.nonroot.yml (hardened scenario).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.scenario == "hardened" and args.nonroot_fixture is None:
        parser.error("--scenario hardened needs --nonroot-fixture")
    if not sys.platform.startswith("linux") or not hasattr(os, "geteuid"):
        print("run_image_upgrade_smoke.py needs a disposable Linux Docker host.", file=sys.stderr)
        return 2
    started = time.monotonic()
    if args.scenario == "clean-host-restore":
        try:
            print(clean_host_restore(args))
            return 0
        except (SmokeError, OSError, subprocess.SubprocessError) as exc:
            print(f"clean-host restore failed; recovery files retained: {type(exc).__name__}", file=sys.stderr)
            return 1
    try:
        print(smoke(args) + f" seconds={int(time.monotonic() - started)}")
    except SmokeError as exc:
        print(f"image upgrade smoke failed: {exc}", file=sys.stderr)
        chain = compose_chain(args.root.resolve())
        subprocess.run(["docker", "compose", *chain, "ps", "-a"], cwd=args.root, check=False)
        subprocess.run(["docker", "compose", *chain, "logs", "--tail", "80"], cwd=args.root, check=False)
        return 1
    finally:
        cleanup(args.root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
