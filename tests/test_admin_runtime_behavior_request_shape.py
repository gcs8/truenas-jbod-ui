"""The timing endpoint refuses a malformed ``values`` field before any save (#1231)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from fastapi import FastAPI

# Must precede admin_service imports that read admin settings.
from tests.admin_test_env import ADMIN_TEST_PUBLIC_ORIGIN  # noqa: F401
import admin_service.routes as admin_routes
from admin_service.config import AdminSettings
from app.config import Settings, save_runtime_behavior_overrides
from app.metrics import install_metrics
from app.router_inclusion import include_router_preserving_route_objects

ROUTE = "/api/admin/runtime-behavior"
VALUES_SHAPE_DETAIL = "values must be a mapping of timing setting names to values."


async def post_json(app: FastAPI, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"admin.example.test"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("admin.example.test", 80),
        },
        receive,
        send,
    )
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    payload = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(payload)


class RuntimeBehaviorRequestShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config_file = self.root / "config.yaml"
        overrides_file = self.root / "runtime-overrides.yaml"
        config_file.write_text("app:\n  refresh_interval_seconds: 30\n", encoding="utf-8")
        overrides_file.write_text(yaml.safe_dump({"app": {"refresh_interval_seconds": 45}}), encoding="utf-8")
        base = Settings(config_file=str(config_file))
        self.settings = base.model_copy(
            update={"paths": base.paths.model_copy(update={"runtime_overrides_file": str(overrides_file)})}
        )
        self.overrides_file = overrides_file

        self.app = FastAPI()
        include_router_preserving_route_objects(self.app, admin_routes.build_router(AdminSettings()))
        # The production exception middleware turns an unhandled error into 500.
        install_metrics(self.app, service_name="synthetic-admin", version="synthetic")

        self.reload = MagicMock(return_value=self.settings)
        self.save = MagicMock(side_effect=save_runtime_behavior_overrides)
        self.journal = MagicMock()
        runtime_service = MagicMock()
        for target in (
            patch.object(admin_routes, "reload_app_settings", self.reload),
            patch.object(admin_routes, "save_runtime_behavior_overrides", self.save),
            patch.object(admin_routes, "record_config_change", self.journal),
            patch.object(admin_routes, "get_runtime_service", return_value=runtime_service),
            patch.object(
                admin_routes,
                "build_runtime_payload",
                new=AsyncMock(return_value={"available": False, "containers": []}),
            ),
            patch.dict(os.environ, {}, clear=False),
        ):
            target.start()
            self.addCleanup(target.stop)
        os.environ.pop("APP_REFRESH_INTERVAL", None)

    def snapshot(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(self.root.iterdir())}

    def test_malformed_values_are_refused_before_settings_load_save_or_journal(self) -> None:
        cases = {
            "list": {"values": ["refresh_interval_seconds"]},
            "integer": {"values": 7},
            "null": {"values": None},
            "missing": {},
            "string": {"values": "refresh_interval_seconds"},
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.reload.reset_mock()
                self.save.reset_mock()
                self.journal.reset_mock()
                before = self.snapshot()

                status, payload = asyncio.run(post_json(self.app, ROUTE, json.dumps(body).encode()))

                self.assertEqual(status, 400, payload)
                self.assertEqual(payload.get("detail"), VALUES_SHAPE_DETAIL)
                self.reload.assert_not_called()
                self.save.assert_not_called()
                self.journal.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_valid_mapping_still_saves_and_journals_the_validated_keys(self) -> None:
        status, payload = asyncio.run(
            post_json(self.app, ROUTE, json.dumps({"values": {"refresh_interval_seconds": 60}}).encode())
        )

        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.save.assert_called_once_with(self.settings, {"refresh_interval_seconds": 60})
        self.journal.assert_called_once_with("runtime_overrides.save", "refresh_interval_seconds")
        saved = yaml.safe_load(self.overrides_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["app"]["refresh_interval_seconds"], 60)

    def test_empty_mapping_remains_an_accepted_no_op(self) -> None:
        before = self.snapshot()["runtime-overrides.yaml"]
        status, payload = asyncio.run(post_json(self.app, ROUTE, json.dumps({"values": {}}).encode()))

        self.assertEqual(status, 200, payload)
        self.save.assert_called_once_with(self.settings, {})
        self.journal.assert_called_once_with("runtime_overrides.save", "")
        self.assertEqual(
            yaml.safe_load(self.snapshot()["runtime-overrides.yaml"]),
            yaml.safe_load(before),
        )


if __name__ == "__main__":
    unittest.main()
