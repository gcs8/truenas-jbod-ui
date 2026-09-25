#!/usr/bin/env python3
"""Image-only upgrade and rollback smoke for CI (#399, #463).

Starts the previous release with its own published Compose file on root-owned
bind mounts, seeds configuration, bay mappings and history through that
release's own writers, then upgrades by changing only ``JBOD_UI_IMAGE`` and
running ``docker compose pull`` and ``docker compose up -d``. It checks health,
restart counts, image identity, version, revision, retained state and SQLite
integrity, writes with the new release, and rolls back by pin with the same two
commands.

Synthetic data only; it needs a disposable Linux Docker host with ``sudo`` (the
bind mounts are root-owned on purpose). Every image it pulls is public. CI
publishes the candidate to a local registry so ``pull`` works unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

UI_PORT = 18180
HISTORY_PORT = 18181
SERVICES = ("enclosure-ui", "enclosure-history")
SMOKE_SYSTEM = "upgrade-smoke"
SUCCESS_PREFIX = "image_upgrade_smoke=ok"


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
    def __init__(self, root: Path) -> None:
        self.root = root

    def compose(self, *args: str, timeout: int = 600) -> str:
        return run(["docker", "compose", *args], cwd=self.root, timeout=timeout)

    def set_image(self, image: str) -> None:
        env = "\n".join(
            (
                f"JBOD_UI_IMAGE={image}",
                "COMPOSE_PROFILES=history",
                f"APP_PORT={UI_PORT}",
                f"HISTORY_PORT={HISTORY_PORT}",
                "",
            )
        )
        path = self.root / ".env"
        path.write_text(env, encoding="utf-8")
        path.chmod(0o600)

    def pull_and_up(self) -> None:
        # The literal published update procedure; --wait only blocks until the
        # healthchecks settle so the checks below read a converged stack.
        self.compose("pull")
        self.compose("up", "-d", "--wait", "--wait-timeout", "300", timeout=420)

    def exec_python(self, service: str, script: str) -> dict:
        output = run(
            ["docker", "compose", "exec", "-T", service, "python", "-"],
            cwd=self.root,
            input_text=textwrap.dedent(script),
            timeout=180,
        )
        return json.loads(output.strip().splitlines()[-1])


def prepare_root(root: Path, compose_fixture: Path, config_fixture: Path) -> None:
    if root.exists():
        run(["sudo", "rm", "-rf", str(root)])
    root.mkdir(parents=True)
    shutil.copyfile(compose_fixture, root / "docker-compose.yml")
    # Existing installs created these as root; the base Compose must accept
    # them without any ownership migration.
    for name in ("config", "config/ssh", "data", "logs", "history", "backup-status"):
        run(["sudo", "install", "-d", "-m", "0755", "-o", "0", "-g", "0", str(root / name)])
    run(["sudo", "install", "-m", "0644", "-o", "0", "-g", "0", str(config_fixture), str(root / "config" / "config.yaml")])


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
from app import __version__
from app.config import get_settings
from app.services.mapping_store import MappingStore
store = MappingStore(get_settings().paths.mapping_file)
rows = sorted([m.enclosure_id, m.slot, m.serial, m.notes]
              for m in store.load_all().values() if m.system_id == "{SMOKE_SYSTEM}")
print(json.dumps({{"version": __version__, "mappings": rows}}))
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
print(json.dumps({{"counts": counts, "integrity": integrity, "user_version": user_version,
                  "current_schema": getattr(store_module, "CURRENT_SCHEMA_VERSION", None)}}))
"""


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
        status, _ = get_json(f"http://127.0.0.1:{port}/healthz")
        if status != 200:
            raise SmokeError(f"/healthz on {port} returned {status}")
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


def smoke(args: argparse.Namespace) -> str:
    root = args.root.resolve()
    prepare_root(root, args.compose_fixture, args.config_fixture)
    deployment = Deployment(root)
    config_path = root / "config" / "config.yaml"
    config_digest = sha256(config_path)

    # 1. The previous release, exactly as published, on root-owned mounts.
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

    # 2. Image-only upgrade: change JBOD_UI_IMAGE, pull, up -d. Nothing else.
    deployment.set_image(args.candidate_image)
    deployment.pull_and_up()
    upgraded = check_runtime(deployment, args.candidate_image, args.candidate_version, args.candidate_revision)
    after_ui = deployment.exec_python("enclosure-ui", READ_UI)
    after_history = deployment.exec_python("enclosure-history", READ_HISTORY)
    expect(after_ui["mappings"] == before_ui["mappings"], f"mappings changed across upgrade: {after_ui}")
    expect(after_history["counts"] == before_history["counts"], f"history rows changed across upgrade: {after_history}")
    expect(after_history["integrity"] == "ok", f"integrity_check after upgrade: {after_history['integrity']}")
    expect(after_history["user_version"] == after_history["current_schema"],
           f"history schema not migrated on startup: {after_history}")
    expect(history_api_view((1, 2, 3)) == expected_history_view(seeded),
           "upgraded history API does not return the predecessor's records")
    expect(sha256(config_path) == config_digest, "config.yaml changed during the upgrade")
    for name in ("data", "history", "config", "data/slot_mappings.json", "history/history.db"):
        expect(owner_uid(root / name) == 0, f"{name} ownership changed; the base upgrade must not migrate it")

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

    return (
        f"{SUCCESS_PREFIX} previous={args.previous_version} candidate={args.candidate_version} "
        f"revision={args.candidate_revision} services={','.join(sorted(upgraded))} restarts=0 "
        f"mappings={len(after_ui['mappings'])} history_rows={sum(after_history['counts'].values())} "
        f"schema={after_history['user_version']} rollback_mappings={len(rolled_ui['mappings'])} "
        f"rollback_history_rows={sum(rolled_history['counts'].values())} history_api=ok"
    )


def cleanup(root: Path) -> None:
    if (root / "docker-compose.yml").exists():
        subprocess.run(["docker", "compose", "down", "--volumes", "--remove-orphans"], cwd=root, check=False)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not sys.platform.startswith("linux") or not hasattr(os, "geteuid"):
        print("run_image_upgrade_smoke.py needs a disposable Linux Docker host.", file=sys.stderr)
        return 2
    started = time.monotonic()
    try:
        print(smoke(args) + f" seconds={int(time.monotonic() - started)}")
    except SmokeError as exc:
        print(f"image upgrade smoke failed: {exc}", file=sys.stderr)
        subprocess.run(["docker", "compose", "ps", "-a"], cwd=args.root, check=False)
        subprocess.run(["docker", "compose", "logs", "--tail", "80"], cwd=args.root, check=False)
        return 1
    finally:
        cleanup(args.root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
