#!/usr/bin/env python3
"""Image-only upgrade and rollback smoke for CI (#399, #463).

Starts the previous release with its own published Compose file on root-owned
bind mounts, seeds configuration, bay mappings and history through that
release's own writers, then upgrades by changing only ``JBOD_UI_IMAGE`` and
running ``docker compose pull`` and ``docker compose up -d``. It checks health,
restart counts, image identity, version, revision, retained state and SQLite
integrity, writes with the new release, and rolls back by pin with the same two
commands.

``--scenario`` picks one of four runs, each a separate CI step:

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

Synthetic data only; it needs a disposable Linux Docker host with ``sudo`` (the
bind mounts are root-owned on purpose). Every image it pulls is public. CI
publishes the candidate to a local registry so ``pull`` works unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

UI_PORT = 18180
HISTORY_PORT = 18181
SERVICES = ("enclosure-ui", "enclosure-history")
SMOKE_SYSTEM = "upgrade-smoke"
SUCCESS_PREFIX = "image_upgrade_smoke=ok"
SCENARIOS = ("base", "hardened", "interrupted-migration", "segmented-catalog")
HARDENED_FILES = ("docker-compose.yml", "docker-compose.nonroot.yml")
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
    "_synchronize_table_counts",
)
KILL_MARKER = "upgrade_smoke_kill_point_reached"
KILL_UNREACHED = "upgrade_smoke_kill_point_not_reached"


class SmokeError(RuntimeError):
    """A failed upgrade or rollback expectation."""


def run(command: list[str], *, cwd: Path | None = None, input_text: str | None = None, timeout: int = 600) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
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

    def file_args(self) -> list[str]:
        return [arg for name in self.files for arg in ("-f", name)]

    def compose(self, *args: str, timeout: int = 600) -> str:
        return run(["docker", "compose", *self.file_args(), *args], cwd=self.root, timeout=timeout)

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
        )
        return json.loads(output.strip().splitlines()[-1])

    def run_python(self, service: str, script: str, extra: tuple[str, ...] = ()) -> dict:
        """Run a script in a one-off container of a stopped service (same image, mounts and user)."""
        output = run(
            ["docker", "compose", *self.file_args(), "run", "--rm", "--no-deps", "-T", *extra, service, "python", "-"],
            cwd=self.root,
            input_text=textwrap.dedent(script),
            timeout=180,
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
    cutoff=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
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


def get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, {}


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
    image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip()
    rows = {}
    for service in SERVICES:
        container = deployment.compose("ps", "-q", service).strip()
        if not container or "\n" in container:
            raise SmokeError(f"{service} must resolve to exactly one container")
        state = json.loads(run(["docker", "inspect", "--format", "{{json .}}", container]))
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
             '{{index .Config.Labels "org.opencontainers.image.revision"}}', image]
        ).strip()
        if label != revision:
            raise SmokeError(f"image revision label is {label!r}, expected {revision}")
    return rows


def owner_uid(path: Path) -> int:
    # The history service may tighten its directory to 0770, so stat as root.
    return int(run(["sudo", "stat", "-c", "%u", str(path)]).strip())


def sha256(path: Path) -> str:
    return hashlib.sha256(run(["sudo", "cat", str(path)]).encode("utf-8")).hexdigest()


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


def smoke(args: argparse.Namespace) -> str:
    if args.scenario == "interrupted-migration":
        return interrupted_migration(args)
    if args.scenario == "segmented-catalog":
        return segmented_catalog_upgrade(args)
    return upgrade_and_rollback(args, hardened=args.scenario == "hardened")


def compose_chain(root: Path) -> list[str]:
    if (root / "docker-compose.nonroot.yml").exists():
        return [arg for name in HARDENED_FILES for arg in ("-f", name)]
    return []


def cleanup(root: Path) -> None:
    if (root / "docker-compose.yml").exists():
        subprocess.run(["docker", "compose", *compose_chain(root), "down", "--volumes", "--remove-orphans"],
                       cwd=root, check=False)
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
