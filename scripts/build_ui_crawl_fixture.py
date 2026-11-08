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
from app.services.public_demo_fixture import (  # noqa: E402
    PublicDemoHistoryBackend,
    build_public_demo_html,
    build_public_demo_snapshot_bundle,
    load_public_demo_fixture,
)
from scripts.build_current_source_browser_fixture import TEMPLATES, build_fixture_request  # noqa: E402

UI_ORIGIN = "https://ui.synthetic.invalid"


def _dump(model) -> object:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model


def render_main(bundle, *, history_configured: bool = True) -> str:
    snapshot = bundle.primary_snapshot
    runtime = bundle.storage_view_runtime
    context = {
        "request": build_fixture_request(),
        "snapshot": snapshot,
        "storage_view_runtime": runtime,
        "settings": Settings(),
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
    }
    return TEMPLATES.env.get_template("index.html").render(context)


def render_fabric(bundle) -> str:
    snapshot = bundle.primary_snapshot
    fabric = bundle.sas_fabric
    bootstrap = {
        "snapshot": _dump(snapshot),
        "fabric": _dump(fabric),
        "systemId": snapshot.selected_system_id,
        "enclosureId": snapshot.selected_enclosure_id,
        "appVersion": __version__,
        "writePolicy": None,
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
    "admin": {},
    "storage_view_templates": [
        {"id": "synthetic-manual", "label": "Synthetic manual", "kind": "manual", "slot_count": 2, "rows": 1, "columns": 2},
    ],
}


def render_admin() -> str:
    env = _script_json_env(ROOT / "admin_service" / "templates")
    return env.get_template("index.html").render(
        admin_bootstrap_json=json.dumps(ADMIN_BOOTSTRAP),
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
        "ui:/api/inventory": _dump(snapshot),
        "ui:/api/storage-views": _dump(bundle.storage_view_runtime),
        "ui:/api/sas-fabric": _dump(bundle.sas_fabric),
        "ui:/api/history/status": await history.get_status(),
        "ui:/api/release-status": {"status": "disabled"},
        "ui:/api/system-locator": {"available": False, "supported": False},
        "history:/healthz": {"status": "ok"},
        "admin:/api/admin/state": ADMIN_BOOTSTRAP,
        "admin:/api/admin/backups": {"available": True, "classes": {}, "targets": [], "artifacts": []},
        "admin:/api/admin/ssh-keys": {"keys": []},
        "admin:/api/admin/history/orphaned": {"orphaned_systems": []},
        "admin:/api/admin/storage-views/live-enclosures": {"enclosures": []},
    }
    smart = {}
    for key, summary in (bundle.smart_summary_cache or {}).items():
        smart[str(key)] = summary
    for slot in slots:
        api[f"ui:/api/slots/{slot}/history"] = scope_history.get(slot) or {}
        if str(slot) in smart:
            api[f"ui:/api/slots/{slot}/smart"] = smart[str(slot)]
    return {
        "origin": UI_ORIGIN,
        "pages": {
            "ui:/": render_main(bundle),
            "ui:/sas-fabric": render_fabric(bundle),
            "history:/": render_history(),
            "admin:/": render_admin(),
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
