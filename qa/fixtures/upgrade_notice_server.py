"""Loopback-only synthetic notice QA. No app lifespan or real collectors."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import uvicorn
from starlette.responses import JSONResponse

from app import main as app_main
from app.config import AppConfig, PathConfig, Settings
from app.models.domain import (
    EnclosureOption, InventorySnapshot, SlotView, StorageViewRuntimePayload, SystemOption,
)
from app.services import upgrade_notice
from tests.test_read_ui_auth import build_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--mode", choices=("network", "basic"), default="basic")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    directory = args.directory.resolve(strict=True)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))
    port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    settings = Settings(
        app=AppConfig(public_origin=origin, release_check_enabled=False),
        paths=PathConfig(mapping_file=str(directory / "slot_mappings.json")),
    )
    snapshot = InventorySnapshot(
        slots=[SlotView(slot=0, slot_label="00", row_index=0, column_index=0,
                        present=True, device_name="synthetic-disk", enclosure_id="synthetic-shelf")],
        layout_rows=[[0]], layout_slot_count=1, layout_columns=1,
        refresh_interval_seconds=3600,
        selected_system_id="synthetic-system", selected_system_label="Synthetic system",
        selected_enclosure_id="synthetic-shelf", selected_enclosure_label="Synthetic shelf",
        systems=[SystemOption(id="synthetic-system", label="Synthetic system", platform="linux")],
        enclosures=[EnclosureOption(id="synthetic-shelf", label="Synthetic shelf", rows=1,
                                    columns=1, slot_count=1, slot_layout=[[0]])],
    )
    runtime = StorageViewRuntimePayload(system_id="synthetic-system", views=[])

    class Service:
        system = SimpleNamespace(id="synthetic-system", truenas=SimpleNamespace(platform="linux"))

        async def get_snapshot(self, **_kwargs):
            return snapshot

        async def get_storage_view_runtime(self, **_kwargs):
            return runtime

    app = build_app(auth_mode=args.mode, public_origin=origin)
    app_main.get_settings = lambda: settings
    app_main.get_inventory_registry = lambda: SimpleNamespace(get_service=lambda _id: Service())
    app_main.resolve_admin_launch_url = lambda *_args: None
    app_main.get_release_status_service = lambda: SimpleNamespace(snapshot=lambda: {})
    write_state = upgrade_notice._write_state
    upgrade_notice._write_state = lambda path, state: (
        False if (directory / "fail-write").exists() else write_state(path, state)
    )

    @app.middleware("http")
    async def synthetic_boundary(request, call_next):
        path = request.url.path
        if path == "/__ready":
            return JSONResponse({"ready": True})
        if path == "/api/inventory":
            return JSONResponse(snapshot.model_dump(mode="json"))
        if path == "/api/storage-views":
            return JSONResponse(runtime.model_dump(mode="json"))
        if path.startswith("/api/") and path not in {
            "/api/upgrade-notice/dismiss", "/api/read-ui/auth/verify",
        }:
            return JSONResponse({"configured": False, "available": False, "views": [], "slots": []})
        if path != "/" and not path.startswith("/static/") and not path.startswith("/api/"):
            return JSONResponse({"detail": "Outside synthetic QA scope"}, status_code=404)
        return await call_next(request)

    print(json.dumps({"origin": origin}), flush=True)
    try:
        uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error", access_log=False)).run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    main()
