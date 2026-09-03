from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys

from starlette.datastructures import URLPath
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import templates  # noqa: E402
from app.models.domain import (  # noqa: E402
    EnclosureOption,
    EnclosureProfileView,
    InventorySnapshot,
    InventorySummary,
    SlotState,
    SlotView,
    SourceStatus,
    SystemOption,
)
from app.services.history_backend import HistoryBackendClient  # noqa: E402
from app.services.snapshot_export import SnapshotExportService  # noqa: E402


FIXTURE_GENERATED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def build_synthetic_snapshot() -> InventorySnapshot:
    profile = EnclosureProfileView(
        id="synthetic-browser-grid",
        label="Synthetic Browser Grid",
        eyebrow="Current-source browser fixture",
        summary="Deterministic synthetic slots for keyboard, hover, and rebuild QA.",
        panel_title="Synthetic Browser Grid",
        rows=2,
        columns=2,
        slot_layout=[[0, 1], [2, 3]],
    )
    slots = [
        SlotView(
            slot=slot,
            slot_label=f"{slot:02d}",
            row_index=slot // 2,
            column_index=slot % 2,
            enclosure_id="synthetic-enclosure",
            enclosure_label="Synthetic Enclosure",
            present=slot != 3,
            state=(SlotState.identify if slot == 1 else SlotState.empty if slot == 3 else SlotState.healthy),
            identify_active=slot == 1,
            device_name=None if slot == 3 else f"sd{chr(ord('a') + slot)}",
            serial=None if slot == 3 else f"SYNTHETIC-SLOT-{slot:04d}",
            model=None if slot == 3 else "Synthetic QA Disk",
            size_human=None if slot == 3 else "1 TB",
            pool_name=None if slot == 3 else "synthetic-pool",
            vdev_name=None if slot == 3 else "mirror-0",
            health=None if slot == 3 else "ONLINE",
            mapping_source="synthetic_fixture",
            search_text=f"synthetic slot {slot}",
        )
        for slot in range(4)
    ]
    return InventorySnapshot(
        slots=slots,
        layout_rows=profile.slot_layout,
        layout_slot_count=4,
        layout_columns=2,
        last_updated=FIXTURE_GENERATED_AT,
        generated_at=FIXTURE_GENERATED_AT,
        refresh_interval_seconds=30,
        selected_system_id="synthetic-system",
        selected_system_label="Synthetic System",
        selected_system_platform="linux",
        selected_enclosure_id="synthetic-enclosure",
        selected_enclosure_label="Synthetic Enclosure",
        selected_enclosure_name="Synthetic Browser Enclosure",
        selected_profile=profile,
        systems=[SystemOption(id="synthetic-system", label="Synthetic System", platform="linux")],
        enclosures=[
            EnclosureOption(
                id="synthetic-enclosure",
                label="Synthetic Enclosure",
                name="Synthetic Browser Enclosure",
                profile_id=profile.id,
                rows=2,
                columns=2,
                slot_count=4,
                slot_layout=profile.slot_layout,
            )
        ],
        platform_context={"platform": "Synthetic Linux", "fixture": "current-source-browser"},
        sources={
            "api": SourceStatus(enabled=False, ok=True, message="Synthetic browser fixture; no live API."),
            "ssh": SourceStatus(enabled=False, ok=True, message="Synthetic browser fixture; no SSH."),
        },
        summary=InventorySummary(
            disk_count=3,
            pool_count=1,
            enclosure_count=1,
            mapped_slot_count=3,
        ),
    )


def build_fixture_request() -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("synthetic.invalid", 443),
            "root_path": "",
            "app": None,
        }
    )
    request.scope["app"] = type(
        "SyntheticFixtureApp",
        (),
        {"url_path_for": lambda _, name, **params: URLPath(f"/static/{params['path']}")},
    )()
    return request


async def build_fixture_html() -> str:
    settings = Settings()
    exporter = SnapshotExportService(settings, HistoryBackendClient(settings.history), templates)
    rendered = await exporter.build_enclosure_snapshot_html(
        request=build_fixture_request(),
        snapshot=build_synthetic_snapshot(),
        smart_summary_cache={},
        selected_slot=None,
        history_window_hours=None,
        history_panel_open=False,
        io_chart_mode="total",
        generated_at=FIXTURE_GENERATED_AT,
        identifier_policy_label="Synthetic IDs",
        identifier_policy_note="Generated only from deterministic checked-in source and synthetic values.",
    )
    asset_hashes = {
        asset_name: hashlib.sha256((ROOT / "app" / "static" / asset_name).read_bytes()).hexdigest()
        for asset_name in ("app.js", "style.css")
    }
    source_manifest = (
        "<!-- current-source-browser-fixture "
        f"app.js_sha256={asset_hashes['app.js']} "
        f"style.css_sha256={asset_hashes['style.css']} -->\n"
    )
    return source_manifest + rendered.html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a deterministic browser fixture with current source assets inlined."
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="HTML output path. Use a temporary path; the generated artifact is disposable.",
    )
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    html = await build_fixture_html()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"Built current-source browser fixture {output_path} ({len(html.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
