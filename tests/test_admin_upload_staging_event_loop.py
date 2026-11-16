"""Upload staging must not block the admin event loop (#1230).

Every gate below releases from another thread or times out, so a regression
fails in about a second instead of hanging. The timeouts are failure bounds,
not storage-speed claims.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from starlette.requests import Request

# Must precede admin_service imports that read admin settings.
from tests.admin_test_env import ADMIN_TEST_PUBLIC_ORIGIN  # noqa: F401
import admin_service.routes as admin_routes
from admin_service.config import AdminSettings
from admin_service.route_support import stream_limited_request_body_to_file
from app.metrics import install_metrics
from app.router_inclusion import include_router_preserving_route_objects

GATE_TIMEOUT_SECONDS = 1.0


def make_request(chunks: list[bytes], path: str = "/api/admin/backup/inspect") -> tuple[Request, list[int]]:
    receive_calls: list[int] = []
    pending = list(chunks)

    async def receive() -> dict[str, Any]:
        receive_calls.append(len(receive_calls) + 1)
        if pending:
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"admin.example.test")],
            "client": ("127.0.0.1", 12345),
            "server": ("admin.example.test", 80),
        },
        receive,
    )
    return request, receive_calls


class GatedFsync:
    """Hold ``os.fsync`` until released, recording which thread called it."""

    def __init__(self, loop: asyncio.AbstractEventLoop, *, release_on_heartbeat: bool) -> None:
        self.loop = loop
        self.release_on_heartbeat = release_on_heartbeat
        self.entered = threading.Event()
        self.release = threading.Event()
        self.order: list[str] = []
        self.thread_id: int | None = None
        self.real_fsync = os.fsync

    def heartbeat(self) -> None:
        self.order.append("event-loop-heartbeat")
        if self.release_on_heartbeat:
            self.release.set()

    def __call__(self, descriptor: int) -> None:
        self.thread_id = threading.get_ident()
        self.order.append("fsync-enter")
        self.loop.call_soon_threadsafe(self.heartbeat)
        self.entered.set()
        self.release.wait(timeout=GATE_TIMEOUT_SECONDS)
        self.order.append("fsync-exit")
        self.real_fsync(descriptor)


class UploadStagingWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_fsync_runs_off_the_event_loop_while_the_loop_keeps_running(self) -> None:
        async def scenario() -> tuple[Path, GatedFsync, int]:
            gate = GatedFsync(asyncio.get_running_loop(), release_on_heartbeat=True)
            request, _ = make_request([b"synthetic ", b"archive"])
            with patch("os.fsync", gate):
                path = await stream_limited_request_body_to_file(
                    request, max_bytes=64, workspace_parent=self.root
                )
            return path, gate, threading.get_ident()

        path, gate, loop_thread = asyncio.run(scenario())
        self.assertEqual(path.read_bytes(), b"synthetic archive")
        self.assertNotEqual(gate.thread_id, loop_thread, "fsync ran on the event-loop thread")
        self.assertEqual(gate.order, ["fsync-enter", "event-loop-heartbeat", "fsync-exit"])

    def test_staging_writes_run_off_the_event_loop_one_chunk_at_a_time(self) -> None:
        real_write = os.write
        write_threads: list[int] = []
        receive_counts_during_write: list[int] = []

        async def scenario() -> tuple[Path, int]:
            loop_thread = threading.get_ident()
            request, receive_calls = make_request([b"aa", b"bb", b"cc"])

            def recording_write(descriptor: int, data: Any) -> int:
                if bytes(data) in {b"aa", b"bb", b"cc"}:
                    write_threads.append(threading.get_ident())
                    receive_counts_during_write.append(len(receive_calls))
                return real_write(descriptor, data)

            with patch("os.write", recording_write):
                path = await stream_limited_request_body_to_file(
                    request, max_bytes=64, workspace_parent=self.root
                )
            return path, loop_thread

        path, loop_thread = asyncio.run(scenario())
        self.assertEqual(path.read_bytes(), b"aabbcc")
        self.assertTrue(write_threads, "staging writes never reached a worker-owned descriptor write")
        self.assertNotIn(loop_thread, write_threads, "a staging write ran on the event-loop thread")
        # Backpressure: chunk N is written before chunk N+1 is read.
        self.assertEqual(receive_counts_during_write, [1, 2, 3])

    def test_cancellation_keeps_the_workspace_until_the_worker_settles(self) -> None:
        async def scenario() -> dict[str, Any]:
            loop = asyncio.get_running_loop()
            gate = GatedFsync(loop, release_on_heartbeat=False)
            workspace = self.root / "upload-workspace"
            workspace.mkdir()
            request, _ = make_request([b"synthetic archive"])
            observed: dict[str, Any] = {"gate": gate, "workspace": workspace}
            with patch("os.fsync", gate), patch(
                "admin_service.route_support.tempfile.mkdtemp", return_value=str(workspace)
            ):
                task = asyncio.create_task(
                    stream_limited_request_body_to_file(request, max_bytes=64)
                )
                entered = await asyncio.to_thread(gate.entered.wait, GATE_TIMEOUT_SECONDS * 2)
                observed["entered_before_task_done"] = entered and not task.done()
                task.cancel()
                await asyncio.sleep(0.05)
                task.cancel()
                await asyncio.sleep(0.05)
                observed["task_done_while_gated"] = task.done()
                observed["file_present_while_gated"] = (workspace / "bundle.archive").exists()
                gate.release.set()
                try:
                    await task
                except asyncio.CancelledError:
                    observed["outcome"] = "cancelled"
                else:
                    observed["outcome"] = "returned"
            return observed

        observed = asyncio.run(scenario())
        self.assertTrue(observed["entered_before_task_done"], "the request could not be cancelled while fsync was pending")
        self.assertFalse(observed["task_done_while_gated"], "cancellation finished before the worker settled")
        self.assertTrue(observed["file_present_while_gated"], "the staged file was removed while the worker used it")
        self.assertEqual(observed["outcome"], "cancelled")
        self.assertEqual(observed["gate"].order[-1], "fsync-exit")
        self.assertFalse(observed["workspace"].exists(), "cancelled staging left its workspace behind")

    def test_cancellation_while_creating_the_staging_file_removes_it(self) -> None:
        async def scenario() -> dict[str, Any]:
            workspace = self.root / "created-while-cancelled"
            entered = threading.Event()
            release = threading.Event()
            loop_thread = threading.get_ident()
            observed: dict[str, Any] = {"workspace": workspace}

            def gated_mkdtemp(*args: Any, **kwargs: Any) -> str:
                observed["mkdtemp_on_loop"] = threading.get_ident() == loop_thread
                entered.set()
                release.wait(timeout=GATE_TIMEOUT_SECONDS)
                workspace.mkdir()
                return str(workspace)

            request, receive_calls = make_request([b"synthetic archive"])
            with patch("admin_service.route_support.tempfile.mkdtemp", gated_mkdtemp):
                task = asyncio.create_task(
                    stream_limited_request_body_to_file(request, max_bytes=64)
                )
                await asyncio.to_thread(entered.wait, GATE_TIMEOUT_SECONDS * 2)
                observed["task_done_before_cancel"] = task.done()
                task.cancel()
                await asyncio.sleep(0.05)
                release.set()
                try:
                    await task
                except asyncio.CancelledError:
                    observed["outcome"] = "cancelled"
                else:
                    observed["outcome"] = "returned"
            observed["receive_calls"] = len(receive_calls)
            return observed

        observed = asyncio.run(scenario())
        self.assertFalse(observed["mkdtemp_on_loop"], "the staging workspace was created on the event-loop thread")
        self.assertFalse(observed["task_done_before_cancel"])
        self.assertEqual(observed["outcome"], "cancelled")
        self.assertEqual(observed["receive_calls"], 0)
        self.assertFalse(observed["workspace"].exists(), "a staging file created during cancellation was left behind")


async def asgi_call(app: FastAPI, method: str, path: str, body: bytes, media_type: str) -> tuple[int, dict[str, Any]]:
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
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"admin.example.test"),
                (b"content-type", media_type.encode()),
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


class InspectRouteUploadStagingTests(unittest.TestCase):
    def test_unrelated_admin_request_completes_while_inspect_upload_fsync_is_gated(self) -> None:
        class Receipts:
            def issue_digest(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return {"receipt": "synthetic-receipt", "expires_at": 1}

        class Backup:
            def inspect_bundle_file(self, path: Path, *, passphrase: Any, identity_callback: Any) -> dict[str, Any]:
                self.content = path.read_bytes()
                identity_callback("synthetic-digest", "plaintext")
                return {"encrypted": False}

        backup = Backup()

        async def scenario() -> dict[str, Any]:
            app = FastAPI()
            include_router_preserving_route_objects(app, admin_routes.build_router(AdminSettings()))
            install_metrics(app, service_name="synthetic-admin", version="synthetic")
            loop = asyncio.get_running_loop()
            gate = GatedFsync(loop, release_on_heartbeat=False)
            with (
                patch("os.fsync", gate),
                patch.object(admin_routes, "get_backup_service", lambda: backup),
                patch.object(admin_routes, "get_backup_receipt_store", lambda: Receipts()),
                patch.object(
                    admin_routes,
                    "build_runtime_payload",
                    new=AsyncMock(return_value={"available": False, "containers": []}),
                ),
            ):
                upload = asyncio.create_task(
                    asgi_call(app, "POST", "/api/admin/backup/inspect", b"synthetic archive", "application/octet-stream")
                )
                await asyncio.to_thread(gate.entered.wait, GATE_TIMEOUT_SECONDS * 2)
                unrelated_status, _ = await asgi_call(app, "GET", "/api/admin/runtime", b"", "application/json")
                unrelated_finished_while_gated = "fsync-exit" not in gate.order
                gate.release.set()
                upload_status, upload_payload = await upload
            return {
                "unrelated_status": unrelated_status,
                "unrelated_finished_while_gated": unrelated_finished_while_gated,
                "upload_status": upload_status,
                "upload_payload": upload_payload,
            }

        result = asyncio.run(scenario())
        self.assertEqual(result["unrelated_status"], 200)
        self.assertTrue(
            result["unrelated_finished_while_gated"],
            "an unrelated admin request waited for upload fsync to finish",
        )
        self.assertEqual(result["upload_status"], 200)
        self.assertEqual(result["upload_payload"]["inspection_receipt"], "synthetic-receipt")
        self.assertEqual(backup.content, b"synthetic archive")


if __name__ == "__main__":
    unittest.main()
