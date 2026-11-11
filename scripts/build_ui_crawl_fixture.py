#!/usr/bin/env python3
"""Build the synthetic pages and API answers the UI coverage crawler uses in CI.

Writes one JSON document: rendered HTML for the main page, the Storage Fabric
page, the history dashboard, the admin page and the offline snapshot (public
demo), plus canned JSON for the read endpoints those pages call. Every value
comes from the checked-in public demo fixture or is synthetic. No config,
credentials, network or appliance access; nothing is written except --output.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader  # noqa: E402
from markupsafe import Markup  # noqa: E402

from app import __version__  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models.domain import InventorySnapshot  # noqa: E402
from app.services.public_demo_fixture import (  # noqa: E402
    PublicDemoHistoryBackend,
    build_public_demo_html,
    build_public_demo_snapshot_bundle,
    load_public_demo_fixture,
)
from app.services.sas_fabric import _diagnostic_metric_summary  # noqa: E402
from app.services.sas_fabric_parsers import parse_mpr_dmesg_events  # noqa: E402
from scripts.build_current_source_browser_fixture import TEMPLATES, build_fixture_request  # noqa: E402

UI_ORIGIN = "https://ui.synthetic.invalid"


def _dump(model) -> object:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model


WRITE_POLICY = {"enabled": True, "mode": "network", "reason": "", "public_origin": None}
# Variant: write sign-in configured (Basic), so the sign-in panels render.
WRITE_POLICY_BASIC = {
    "enabled": False,
    "mode": "basic",
    "reason": "Sign in to make changes.",
    "public_origin": None,
}


def live_snapshot(bundle, *, platform: str | None = None, ssh: bool = False):
    """The demo snapshot as a live system: locate lights work (synthetic answers).

    ``platform`` overrides the selected system's platform (the QuantaStor
    variant: its page queues a forced startup read when bays are LED-capable).
    ``ssh`` marks the SSH source configured (#1143: the CORE disk-sync buttons
    are enabled only then, so the default fixture renders them disabled).
    """
    snapshot = bundle.primary_snapshot
    slots = [
        slot.model_copy(update={"led_supported": True, "mapping_revision": f"synthetic-{slot.slot}", "mapping_clear_revision": f"synthetic-{slot.slot}"})
        for slot in snapshot.slots
    ]
    update = {"slots": slots}
    if platform:
        update["selected_system_platform"] = platform
    if ssh:
        sources = dict(snapshot.sources)
        sources["ssh"] = sources["ssh"].model_copy(update={"enabled": True, "message": "Synthetic fixture: SSH marked configured; nothing is contacted."})
        update["sources"] = sources
    return snapshot.model_copy(update=update)


# Codex #1108: a main page whose batch SMART prefetch waits far longer than
# any crawl state (as with a large APP_SMART_PREFETCH_DELAY_MS), so selecting
# a bay sends the bay's own GET /api/slots/{slot}/smart first.
SLOW_SMART_PREFETCH_DELAY_MS = 3600000


def slow_prefetch_settings() -> Settings:
    settings = Settings()
    return settings.model_copy(update={"app": settings.app.model_copy(update={"smart_prefetch_delay_ms": SLOW_SMART_PREFETCH_DELAY_MS})})


# #1145: the demo's first 24 disks as one row of 2.5-inch top-latched sleds
# (a 24-bay 2U front), whose narrow bays once stacked a heat value such as
# "+0.22 C" one character per line. #1151: every third sled is empty, as on
# a part-filled shelf; there the state chip once covered a rotated hours value.
DENSE_SLED_BAYS = 24
DENSE_SLED_EMPTY_EVERY = 3

_EMPTY_BAY = {
    "present": False, "state": "empty", "device_name": None, "serial": None, "gptid": None, "model": None,
    "size_bytes": None, "size_human": None, "pool_name": None, "vdev_name": None, "vdev_class": None,
    "topology_label": None, "health": None, "temperature_c": None, "smart_device_names": [],
    "persistent_id_label": None, "logical_unit_id": None, "sas_address": None, "notes": None,
}


def dense_sled_snapshot(bundle):
    data = _dump(live_snapshot(bundle))
    layout = [list(range(DENSE_SLED_BAYS))]
    data["slots"] = [
        {**slot, "row_index": 0, "column_index": index, **(_EMPTY_BAY if index % DENSE_SLED_EMPTY_EVERY == 1 else {})}
        for index, slot in enumerate(data["slots"][:DENSE_SLED_BAYS])
    ]
    data.update(layout_rows=layout, layout_slot_count=DENSE_SLED_BAYS, layout_columns=DENSE_SLED_BAYS)
    data["selected_profile"] = {
        **data["selected_profile"],
        "id": "synthetic-dense-sleds-24",
        "label": "Synthetic 24-bay 2.5in front",
        "panel_title": "Synthetic 24-bay 2.5in front",
        "face_style": "front-drive",
        "latch_edge": "top",
        "bay_size": "2.5",
        "rows": 1,
        "columns": DENSE_SLED_BAYS,
        "slot_count": DENSE_SLED_BAYS,
        "slot_layout": layout,
        "row_groups": [],
    }
    data["enclosures"][0].update(
        profile_id="synthetic-dense-sleds-24", rows=1, columns=DENSE_SLED_BAYS, slot_count=DENSE_SLED_BAYS, slot_layout=layout
    )
    disks = sum(1 for slot in data["slots"] if slot["state"] != "empty")
    data["summary"].update(disk_count=disks, mapped_slot_count=disks)
    return InventorySnapshot.model_validate(data)


# beta.7 QA: saved views whose runtime backing enclosure is not the page's
# own, as on a QuantaStor HA pair where a view's binding.target_system_id
# names the partner node (inventory.py builds backing_enclosure_id from that
# node's snapshot). The page itself still loads the system's default
# enclosure. Synthetic id only.
SYNTHETIC_PARTNER_ENCLOSURE_ID = "synthetic-partner-node-enclosure"


def partner_backed_runtime(runtime):
    """The demo storage-view runtime with every inventory view backed by the partner."""
    views = [
        view.model_copy(update={
            "backing_enclosure_id": SYNTHETIC_PARTNER_ENCLOSURE_ID,
            "backing_enclosure_label": "Synthetic partner node",
        })
        if view.source == "inventory_binding" else view
        for view in runtime.views
    ]
    return runtime.model_copy(update={"views": views})


def render_main(
    bundle,
    *,
    history_configured: bool = True,
    write_policy: dict = WRITE_POLICY,
    platform: str | None = None,
    settings: Settings | None = None,
    runtime=None,
    ssh: bool = False,
    snapshot: InventorySnapshot | None = None,
) -> str:
    snapshot = snapshot or live_snapshot(bundle, platform=platform, ssh=ssh)
    runtime = runtime or bundle.storage_view_runtime
    context = {
        "request": build_fixture_request(),
        "snapshot": snapshot,
        "storage_view_runtime": runtime,
        "settings": settings or Settings(),
        "initial_snapshot_json": json.dumps(_dump(snapshot)),
        "initial_storage_view_runtime_json": json.dumps(_dump(runtime)),
        "history_configured": history_configured,
        "app_version": __version__,
        "release_status": {},
        "snapshot_mode": False,
        "sas_fabric_view_url": "/sas-fabric",
        "snapshot_export_meta": {},
        "snapshot_export_meta_json": "null",
        "preloaded_history_json": "{}",
        "preloaded_smart_summary_json": "{}",
        "preloaded_snapshots_json": "{}",
        "preloaded_snapshot_smart_summary_json": "{}",
        "preloaded_storage_view_smart_summary_json": "{}",
        "preloaded_history_summary_json": '{"counts": {}, "collector": {}}',
        "initial_selected_slot_json": "null",
        "initial_selected_storage_view_id_json": "null",
        "initial_history_timeframe_hours_json": "24",
        "initial_history_panel_open_json": "false",
        "initial_history_io_chart_mode_json": '"total"',
        "admin_launch_url": None,
        "write_policy_json": json.dumps(write_policy),
    }
    return TEMPLATES.env.get_template("index.html").render(context)


# Codex #1108: synthetic mpr kernel events, decoded by the app's own parser,
# so the Disk Path view renders its fault-evidence panels and event tables
# (fabric.diagnostics) in CI. Invented device/target numbers only.
SYNTHETIC_MPR_DMESG = "\n".join(
    line
    for index in range(4)
    for line in (
        f"mpr0: Controller reported scsi ioc terminated tgt {180 + index} SMID {140 + index} loginfo 31120302",
        f"(da{60 + index}:mpr0:0:{180 + index}:0): WRITE(16). CDB: 8a 00 00 00 00 01 75 5d d9 60 00 00 00 f8 00 00",
        f"(da{60 + index}:mpr0:0:{180 + index}:0): CAM status: CCB request completed with an error",
        f"(da{60 + index}:mpr0:0:{180 + index}:0): SCSI sense: ABORTED COMMAND asc:4b,4 (NAK received)",
        f"(da{60 + index}:mpr0:0:{180 + index}:0): Retrying command (per sense data)",
    )
)


def fabric_with_diagnostics(fabric) -> dict:
    """The demo fabric with synthetic kernel diagnostics on each controller."""
    data = _dump(fabric)
    events = parse_mpr_dmesg_events(SYNTHETIC_MPR_DMESG)
    summary = _diagnostic_metric_summary(events["by_controller"]["mpr0"])
    for node in data.get("nodes") or []:
        if node.get("kind") == "controller":
            node["metrics"] = {**(node.get("metrics") or {}), "kernel_diagnostics": summary}
    return data


def render_fabric(bundle, *, write_policy: dict = WRITE_POLICY) -> str:
    snapshot = bundle.primary_snapshot
    fabric = fabric_with_diagnostics(bundle.sas_fabric)
    bootstrap = {
        "snapshot": _dump(snapshot),
        "fabric": _dump(fabric),
        "systemId": snapshot.selected_system_id,
        "enclosureId": snapshot.selected_enclosure_id,
        "appVersion": __version__,
        "writePolicy": write_policy,
    }
    context = {
        "request": build_fixture_request(),
        "snapshot": snapshot,
        "fabric": fabric,
        "settings": Settings(),
        "app_version": __version__,
        "bootstrap_json": json.dumps(bootstrap),
    }
    return TEMPLATES.env.get_template("sas_fabric.html").render(context)


def _script_json_env(directory: Path) -> Environment:
    env = Environment(loader=FileSystemLoader(str(directory)), autoescape=True)
    env.filters["script_json_text"] = lambda text: Markup(str(text).replace("<", "\\u003c"))
    return env


ADMIN_BOOTSTRAP = {
    "systems": [
        {
            "id": "synthetic-system",
            "label": "Synthetic System",
            "platform": "core",
            "truenas_host": "nas.example.test",
            # Codex #1108: verification on with a saved CA bundle, so loading
            # this system into the form re-checks the bundle
            # (GET /api/admin/tls/saved-status), as a live system can.
            "verify_ssl": True,
            "tls_ca_bundle_path": "/app/data/tls/synthetic-ca.pem",
            "api_user": "synthetic",
            "api_password_configured": True,
            "ssh_enabled": True,
            "ssh_host": "nas.example.test",
            "storage_views": [
                {
                    "id": "synthetic-view",
                    "label": "Synthetic View",
                    "kind": "manual",
                    "template_id": "synthetic-manual",
                    "enabled": True,
                    "order": 10,
                    "render": {"show_in_main_ui": True, "show_in_admin_ui": True},
                    "binding": {"mode": "manual", "device_names": []},
                }
            ],
        }
    ],
    "profiles": [
        {"id": "synthetic-profile", "label": "Synthetic profile", "rows": 2, "columns": 4, "slot_count": 8,
         "slot_layout": [[0, 1, 2, 3], [4, 5, 6, 7]]},
    ],
    "setup_platform_defaults": {"core": {"ssh_commands": ["synthetic-read-only-command"]}},
    "default_system_id": "synthetic-system",
    # A public origin other than the fixture's own shows the "open at the
    # configured address" link (rendered only, never followed).
    "admin": {"public_origin": "https://admin-public.synthetic.invalid"},
    "storage_view_templates": [
        {"id": "synthetic-manual", "label": "Synthetic manual", "kind": "manual", "slot_count": 2, "rows": 1, "columns": 2},
    ],
}


# Codex #1108: a QuantaStor HA system whose saved storage view binds an HA
# node (binding.target_system_id). Loading it sends the candidate read with
# target_system_id. Synthetic names only.
ADMIN_BOOTSTRAP_QUANTASTOR_HA = {
    **ADMIN_BOOTSTRAP,
    "systems": [
        {
            "id": "synthetic-qs-ha",
            "label": "Synthetic QuantaStor HA",
            "platform": "quantastor",
            "truenas_host": "qs.example.test",
            "verify_ssl": False,
            "api_user": "synthetic",
            "api_password_configured": True,
            "ssh_enabled": True,
            "ssh_host": "qs.example.test",
            "ha_enabled": True,
            "ha_nodes": [
                {"system_id": "synthetic-qs-node-a", "label": "Synthetic node A", "host": "qs-a.example.test"},
                {"system_id": "synthetic-qs-node-b", "label": "Synthetic node B", "host": "qs-b.example.test"},
            ],
            "storage_views": [
                {
                    "id": "synthetic-ha-view",
                    "label": "Synthetic HA View",
                    "kind": "manual",
                    "template_id": "synthetic-manual",
                    "enabled": True,
                    "order": 10,
                    "render": {"show_in_main_ui": True, "show_in_admin_ui": True},
                    "binding": {"mode": "manual", "device_names": [], "target_system_id": "synthetic-qs-node-b"},
                }
            ],
        }
    ],
    "default_system_id": "synthetic-qs-ha",
}


def render_admin(bootstrap: dict = ADMIN_BOOTSTRAP) -> str:
    env = _script_json_env(ROOT / "admin_service" / "templates")
    return env.get_template("index.html").render(
        admin_bootstrap_json=json.dumps(bootstrap),
        url_for=lambda name, path: "/static/" + path,
    )


def render_history() -> str:
    env = _script_json_env(ROOT / "history_service" / "templates")
    status = {
        "collector_running": True,
        "collection_running": False,
        "last_completed_at": "2026-01-01T00:00:00+00:00",
        "background_consecutive_failures": 0,
        "last_retention_rows_removed": 0,
        "last_collection_duration_seconds": 12.5,
    }
    scopes = [
        {"system_id": "demo-system", "system_label": "Demo System", "enclosure_id": "demo-enclosure",
         "enclosure_label": "Demo Enclosure", "tracked_slots": 60, "metric_sample_count": 4800,
         "event_count": 12, "last_seen_at": "2026-01-01T00:00:00+00:00"},
    ]
    return env.get_template("dashboard.html").render(
        request=build_fixture_request(),
        app_name="TrueNAS JBOD Enclosure UI",
        app_version=__version__,
        status=status,
        counts={"tracked_slots": 60, "metric_sample_count": 4800, "metric_rollup_count": 120, "event_count": 12},
        scopes=scopes,
        database_size_label="12.0 MiB",
        reclaimable_label="none",
        degraded_reason=None,
        backup_footprint_label="2 backups, 24.0 MiB",
        release_summary="Checking for updates...",
        latest_url=None,
        backoff_label="inactive",
        collector_state_label="idle",
        skipped_scopes_label="none",
        current_collection_label="no",
        collector_banner_text="",
        direct_refresh_enabled=True,
        last_collection_duration_label="12.5s",
        last_background_overrun_label="no",
        last_retention_duration_label="0.4s",
        last_inventory_mode="cached inventory",
        format_count=lambda value: f"{value:,}" if isinstance(value, int) else str(value),
        refresh={"full_refresh_cooldown_seconds_remaining": 0},
        status_json=json.dumps(status),
        url_for=lambda name, path: "/static/" + path,
    )


async def build() -> dict:
    settings = Settings()
    fixture = load_public_demo_fixture()
    bundle = build_public_demo_snapshot_bundle(fixture=fixture, settings=settings)
    history = PublicDemoHistoryBackend(fixture)
    snapshot = bundle.primary_snapshot
    slots = [slot.slot for slot in snapshot.slots]
    scope_history = await history.get_scope_history(
        system_id=snapshot.selected_system_id,
        enclosure_id=snapshot.selected_enclosure_id,
        slots=slots,
        window_hours=fixture.history_window_hours,
    )
    api = {
        "ui:/api/inventory": _dump(live_snapshot(bundle)),
        # The QuantaStor variant keeps its platform after its startup refresh.
        "ui-quantastor:/api/inventory": _dump(live_snapshot(bundle, platform="quantastor")),
        "ui-dense-sleds:/api/inventory": _dump(dense_sled_snapshot(bundle)),
        "ui:/api/storage-views": _dump(bundle.storage_view_runtime),
        "ui-ha-view:/api/storage-views": _dump(partner_backed_runtime(bundle.storage_view_runtime)),
        "ui-ssh:/api/inventory": _dump(live_snapshot(bundle, ssh=True)),
        "ui:/api/sas-fabric": fabric_with_diagnostics(bundle.sas_fabric),
        "ui:/api/history/status": await history.get_status(),
        "ui:/api/release-status": {"status": "disabled"},
        "ui:/api/system-locator": {"available": False, "supported": False},
        "ui:/api/mappings/export": {"schema_version": 1, "mappings": []},
        "history:/healthz": {"status": "ok"},
        "admin:/api/admin/state": ADMIN_BOOTSTRAP,
        "admin-quantastor-ha:/api/admin/state": ADMIN_BOOTSTRAP_QUANTASTOR_HA,
        "admin:/api/admin/backups": {"available": True, "classes": {}, "targets": [], "artifacts": []},
        "admin:/api/admin/backups/policy": {"policies": {}, "targets": []},
        "admin:/api/admin/ssh-keys": {"keys": []},
        "admin:/api/admin/history/orphaned": {"orphaned_systems": []},
        "admin:/api/admin/storage-views/live-enclosures": {"enclosures": []},
        "admin:/api/admin/storage-views/candidates": {"candidates": []},
        "admin:/api/admin/tls/saved-status": {
            "ok": True,
            "system_id": "synthetic-system",
            "validation": {
                "validated": True,
                "host": "nas.example.test",
                "bundle_path": "/app/data/tls/synthetic-ca.pem",
                "detail": "Synthetic fixture: the saved bundle verifies this host.",
            },
        },
    }
    smart = {}
    for key, summary in (bundle.smart_summary_cache or {}).items():
        smart[str(key)] = summary
    for slot in slots:
        api[f"ui:/api/slots/{slot}/history"] = scope_history.get(slot) or {}
        if str(slot) in smart:
            api[f"ui:/api/slots/{slot}/smart"] = smart[str(slot)]
    # Saved storage views (#1127): the heat map of a selected non-enclosure view
    # reads each occupied slot's SMART summary through the storage-view route.
    for view_id, summaries in (bundle.storage_view_smart_summary_cache or {}).items():
        for slot_index, summary in (summaries or {}).items():
            api[f"ui:/api/storage-views/{view_id}/slots/{slot_index}/smart"] = summary
    # Codex PRRT_kwDOSAqK-M61IUDz: with a storage view selected, the heat
    # map's rate metrics and timeline read the view's own history
    # (heatmapHistoryScopeRequest -> ui:/api/storage-views/{view_id}/history),
    # shaped like the route's answer (histories keyed by display slot).
    for view in fixture.storage_views:
        indices = [slot.slot_index for slot in view.slots]
        view_history = await history.get_scope_history(
            system_id=snapshot.selected_system_id,
            enclosure_id=f"storage-view:{view.id}",
            slots=indices,
            window_hours=fixture.history_window_hours,
        )
        api[f"ui:/api/storage-views/{view.id}/history"] = {
            "configured": True,
            "system_id": snapshot.selected_system_id,
            "storage_view_id": view.id,
            "histories": {str(index): payload for index, payload in view_history.items()},
        }
    return {
        "origin": UI_ORIGIN,
        "pages": {
            "ui:/": render_main(bundle),
            "ui:/sas-fabric": render_fabric(bundle),
            # Fixture variants (qa/ui-states.json `variant`): the write
            # sign-in panels; the storage-view outage variant reuses ui:/ and
            # the crawler's fixture handler fails its /api/storage-views.
            "ui-write-sign-in:/": render_main(bundle, write_policy=WRITE_POLICY_BASIC),
            "ui-write-sign-in:/sas-fabric": render_fabric(bundle, write_policy=WRITE_POLICY_BASIC),
            "ui-quantastor:/": render_main(bundle, platform="quantastor"),
            "ui-slow-prefetch:/": render_main(bundle, settings=slow_prefetch_settings()),
            "ui-dense-sleds:/": render_main(bundle, snapshot=dense_sled_snapshot(bundle)),
            "ui-ha-view:/": render_main(bundle, runtime=partner_backed_runtime(bundle.storage_view_runtime)),
            # #1143: SSH configured, so the CORE disk-sync buttons are enabled.
            "ui-ssh:/": render_main(bundle, ssh=True),
            "history:/": render_history(),
            "admin:/": render_admin(),
            "admin-quantastor-ha:/": render_admin(ADMIN_BOOTSTRAP_QUANTASTOR_HA),
            "offline:/": await build_public_demo_html(settings=settings),
        },
        "api": api,
        "smart_batch": smart,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="JSON output path (use a temporary path).")
    args = parser.parse_args(argv)
    payload = asyncio.run(build())
    args.output.write_text(json.dumps(payload, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
