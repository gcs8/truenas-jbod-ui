"""Count file reads and writes, API logins, and SSH connections per SMART grid load.

Synthetic systems only: no hosts are contacted, the websocket and SSH clients
are in-memory fakes, and every serial or address is made up.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import DiskInventorySyncMode, SmartSummaryView, utcnow
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.ssh_probe import SSHProbe
from app.services.truenas_ws import TrueNASRawData, TrueNASWebsocketClient

BAY_COUNT = 84
SMART_JSON = json.dumps(
    {
        "device": {"name": "/dev/da0", "type": "scsi", "protocol": "SCSI"},
        "model_name": "SYNTHETIC-MODEL",
        "serial_number": "SN-0000",
        "firmware_version": "F001",
        "temperature": {"current": 31},
        "power_on_time": {"hours": 1000},
        "smart_status": {"passed": True},
        "logical_block_size": 512,
        "physical_block_size": 4096,
        "rotation_rate": 7200,
    }
)


def synthetic_raw_data(bay_count: int = BAY_COUNT) -> TrueNASRawData:
    return TrueNASRawData(
        enclosures=[
            {
                "id": "enc-synthetic",
                "name": "Synthetic Shelf",
                "label": "Synthetic Shelf",
                "elements": [
                    {"slot": index + 1, "dev": f"/dev/da{index}", "status": "OK", "descriptor": f"Slot{index:02d}"}
                    for index in range(bay_count)
                ],
            }
        ],
        disks=[
            {
                "name": f"da{index}",
                "devname": f"da{index}",
                "serial": f"SN-{index:04d}",
                "model": "SYNTHETIC-MODEL",
                "size": 4_000_000_000_000,
                "status": "ONLINE",
                "identifier": f"{{serial_lunid}}SN-{index:04d}_5000c500{index:08x}",
                "lunid": f"5000c500{index:08x}",
                "enclosure": {"id": "enc-synthetic", "slot": index + 1},
                "pool": "tank",
            }
            for index in range(bay_count)
        ],
        pools=[],
        disk_temperatures={f"da{index}": 30 for index in range(bay_count)},
        smart_test_results=[],
    )


class CountingSlotDetailStore(SlotDetailStore):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.counts: Counter[str] = Counter()

    def load_all(self):
        self.counts["load_all"] += 1
        return super().load_all()

    def _write(self, entries) -> None:
        self.counts["_write"] += 1
        super()._write(entries)


class FakeMiddlewareWebSocket:
    """Answers DDP method calls in memory, so the dispatcher and the plain call path both work."""

    def __init__(self, calls: Counter[str]) -> None:
        import asyncio

        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._calls = calls

    async def send(self, raw_message: str) -> None:
        message = json.loads(raw_message)
        if message.get("msg") != "method":
            return
        method = message["method"]
        self._calls[method] += 1
        if method == "disk.smartctl":
            result = "SMART text" if message["params"][1] == ["-x"] else SMART_JSON
        elif method == "auth.login_with_api_key":
            result = True
        else:
            result = []
        await self._queue.put(json.dumps({"msg": "result", "id": message["id"], "result": result}))

    async def recv(self) -> str:
        return await self._queue.get()


class CountingWebsocketClient(TrueNASWebsocketClient):
    def __init__(self, config: TrueNASConfig, raw_data: TrueNASRawData) -> None:
        super().__init__(config)
        self.raw_data = raw_data
        self.sessions = 0
        self.calls: Counter[str] = Counter()
        self.fail_next_session = False

    async def fetch_all(self) -> TrueNASRawData:
        return self.raw_data

    @asynccontextmanager
    async def _session(self):
        self.sessions += 1
        if self.fail_next_session:
            self.fail_next_session = False
            raise OSError("synthetic connection refused")
        yield FakeMiddlewareWebSocket(self.calls)


class _Stream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.channel = self

    def read(self, _size: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data

    def recv_exit_status(self) -> int:
        return 0

    def shutdown_write(self) -> None:
        pass

    def write(self, *_args) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeParamikoClient:
    inflight = 0
    max_inflight = 0
    lock = threading.Lock()
    polls = 0
    fail_next_exec = False
    closed = 0

    @classmethod
    def reset(cls) -> None:
        cls.inflight = 0
        cls.max_inflight = 0
        cls.polls = 0
        cls.fail_next_exec = False
        cls.closed = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def close(self) -> None:
        FakeParamikoClient.closed += 1

    def exec_command(self, command: str, timeout=None):
        with FakeParamikoClient.lock:
            if FakeParamikoClient.fail_next_exec:
                FakeParamikoClient.fail_next_exec = False
                raise OSError("synthetic channel failure")
            FakeParamikoClient.inflight += 1
            FakeParamikoClient.max_inflight = max(FakeParamikoClient.max_inflight, FakeParamikoClient.inflight)
        time.sleep(0.01)
        with FakeParamikoClient.lock:
            FakeParamikoClient.inflight -= 1
        output = b""
        if "smartctl" in command and "-j" in command:
            output = SMART_JSON.encode()
        elif "disk.sync_all" in command:
            output = b"4242"
        elif "core.get_jobs" in command:
            FakeParamikoClient.polls += 1
            state = "RUNNING" if FakeParamikoClient.polls < 4 else "SUCCESS"
            output = json.dumps([{"id": 4242, "state": state, "error": None}]).encode()
        return _Stream(b""), _Stream(output), _Stream(b"")


class CountingSSHProbe(SSHProbe):
    def __init__(self, config: SSHConfig) -> None:
        super().__init__(config)
        self.client_opens = 0
        self.fail_next_connect = False

    def _client(self):
        self.client_opens += 1
        if self.fail_next_connect:
            self.fail_next_connect = False
            raise OSError("synthetic connection refused")
        return FakeParamikoClient()


def build_service(
    platform: str,
    *,
    ssh_enabled: bool,
    temp_dir: str,
    smart_batch_max_concurrency: int | None = None,
):
    settings = Settings()
    settings.layout.slot_count = BAY_COUNT
    settings.layout.rows = 7
    settings.layout.columns = 12
    if smart_batch_max_concurrency is not None:
        settings.app.smart_batch_max_concurrency = smart_batch_max_concurrency
    ssh = (
        SSHConfig(enabled=True, host="192.0.2.10", user="synthetic", commands=[])
        if ssh_enabled
        else SSHConfig(enabled=False)
    )
    system = SystemConfig(
        id="synthetic",
        label="Synthetic",
        truenas=TrueNASConfig(platform=platform, host="https://nas.example.test", api_key="synthetic-key"),
        ssh=ssh,
    )
    settings.systems = [system]
    client = CountingWebsocketClient(system.truenas, synthetic_raw_data())
    probe = CountingSSHProbe(system.ssh)
    store = CountingSlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json"))
    service = InventoryService(
        settings,
        system,
        client,
        probe,
        None,
        MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
        ProfileRegistry(settings),
        store,
    )
    return service, client, probe, store


class SmartGridLoadCostTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_grid_load_logs_in_once_and_touches_the_slot_detail_file_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, client, _probe, store = build_service("core", ssh_enabled=False, temp_dir=temp_dir)
            snapshot = await service.get_snapshot(force_refresh=True)
            slots = [slot.slot for slot in snapshot.slots]
            self.assertGreaterEqual(len(slots), 60)
            client.sessions = 0
            client.calls.clear()
            store.counts.clear()

            items = await service.get_slot_smart_summaries(slots)

            self.assertEqual(len(items), len(slots))
            self.assertTrue(all(item.summary.available for item in items))
            self.assertEqual(client.sessions, 1)
            self.assertEqual(client.calls["disk.smartctl"], 2 * len(slots))
            # One read when the batch opens, one re-read under the lock when it saves.
            self.assertLessEqual(store.counts["load_all"], 2)
            self.assertEqual(store.counts["_write"], 1)
            entries = SlotDetailStore(store.file_path).load_all()
            self.assertEqual(sum(1 for entry in entries.values() if entry.smart_fields), len(slots))

            client.sessions = 0
            store.counts.clear()
            warm = await service.get_slot_smart_summaries(slots)
            self.assertTrue(all(item.summary.available for item in warm))
            self.assertEqual(client.sessions, 0)
            self.assertEqual(store.counts["_write"], 0)

            # A snapshot rebuild neither rewrites the file nor drops the SMART fields.
            service._cache.clear()
            store.counts.clear()
            await service.get_snapshot(force_refresh=True)
            self.assertEqual(store.counts["load_all"], 1)
            self.assertEqual(store.counts["_write"], 0)
            entries = SlotDetailStore(store.file_path).load_all()
            self.assertEqual(sum(1 for entry in entries.values() if entry.smart_fields), len(slots))

    async def test_core_grid_load_falls_back_to_one_login_per_call_when_the_shared_login_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, client, _probe, _store = build_service("core", ssh_enabled=False, temp_dir=temp_dir)
            snapshot = await service.get_snapshot(force_refresh=True)
            slots = [slot.slot for slot in snapshot.slots][:6]
            client.sessions = 0
            client.fail_next_session = True

            items = await service.get_slot_smart_summaries(slots)

            self.assertTrue(all(item.summary.available for item in items))
            self.assertEqual(client.sessions, 1 + 2 * len(slots))

    async def _scale_grid_load(self, temp_dir: str, *, smart_batch_max_concurrency: int | None = None):
        service, _client, probe, store = build_service(
            "scale",
            ssh_enabled=True,
            temp_dir=temp_dir,
            smart_batch_max_concurrency=smart_batch_max_concurrency,
        )
        snapshot = await service.get_snapshot(force_refresh=True)
        slots = [slot.slot for slot in snapshot.slots]
        self.assertEqual(len(slots), BAY_COUNT)
        probe.client_opens = 0
        store.counts.clear()
        FakeParamikoClient.reset()
        return service, probe, store, slots

    async def test_scale_grid_load_shares_one_ssh_connection_across_every_bay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, probe, store, slots = await self._scale_grid_load(temp_dir)

            items = await service.get_slot_smart_summaries(slots)

            self.assertEqual(sum(1 for item in items if item.summary.available), BAY_COUNT)
            self.assertEqual(probe.client_opens, 1)
            self.assertEqual(FakeParamikoClient.closed, 1)
            self.assertGreater(FakeParamikoClient.max_inflight, 1)
            self.assertLessEqual(FakeParamikoClient.max_inflight, 8)
            self.assertLessEqual(store.counts["load_all"], 2)
            self.assertEqual(store.counts["_write"], 1)

            probe.client_opens = 0
            warm = await service.get_slot_smart_summaries(slots)
            self.assertEqual(sum(1 for item in warm if item.summary.available), BAY_COUNT)
            self.assertEqual(probe.client_opens, 0)

    async def test_smart_batch_max_concurrency_bounds_commands_on_the_shared_connection(self) -> None:
        elapsed: dict[int, float] = {}
        peak: dict[int, int] = {}
        for limit in (1, 4):
            with tempfile.TemporaryDirectory() as temp_dir:
                service, probe, _store, slots = await self._scale_grid_load(
                    temp_dir, smart_batch_max_concurrency=limit
                )

                started = time.perf_counter()
                items = await service.get_slot_smart_summaries(slots)
                elapsed[limit] = time.perf_counter() - started
                peak[limit] = FakeParamikoClient.max_inflight

                self.assertEqual(sum(1 for item in items if item.summary.available), BAY_COUNT)
                self.assertEqual(probe.client_opens, 1)

        # Each fake command sleeps, so the setting must show up as overlap and wall time.
        self.assertEqual(peak[1], 1)
        self.assertGreater(peak[4], 1)
        self.assertLessEqual(peak[4], 4)
        self.assertLess(elapsed[4], elapsed[1] * 0.6, elapsed)

    async def test_scale_grid_load_falls_back_to_one_connection_per_bay_when_the_shared_one_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, probe, _store, slots = await self._scale_grid_load(temp_dir)
            probe.fail_next_connect = True

            items = await service.get_slot_smart_summaries(slots)

            self.assertEqual(sum(1 for item in items if item.summary.available), BAY_COUNT)
            self.assertEqual(probe.client_opens, 1 + BAY_COUNT)

    async def test_scale_grid_load_retries_a_bay_on_its_own_connection_after_a_channel_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, probe, _store, slots = await self._scale_grid_load(temp_dir, smart_batch_max_concurrency=1)
            FakeParamikoClient.fail_next_exec = True

            items = await service.get_slot_smart_summaries(slots)

            # The shared connection is dropped after the failure, so the rest of
            # the grid runs one connection per bay, and every bay still loads.
            self.assertEqual(sum(1 for item in items if item.summary.available), BAY_COUNT)
            self.assertEqual(probe.client_opens, 1 + BAY_COUNT)
            self.assertGreaterEqual(FakeParamikoClient.closed, 1)

    async def test_full_disk_sync_polls_over_one_ssh_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, _client, probe, _store = build_service("core", ssh_enabled=True, temp_dir=temp_dir)
            FakeParamikoClient.reset()
            probe.client_opens = 0

            async def no_sleep(_seconds: float) -> None:
                return None

            service._disk_inventory_sync_sleep = no_sleep
            result = await service.sync_disk_inventory(DiskInventorySyncMode.full)

            self.assertEqual(result.state, "SUCCESS")
            self.assertEqual(FakeParamikoClient.polls, 4)
            self.assertEqual(probe.client_opens, 1)


class SmartCacheEvictionCostTests(unittest.TestCase):
    def test_eviction_pops_only_entries_past_the_retention_horizon(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, _client, _probe, _store = build_service("core", ssh_enabled=False, temp_dir=temp_dir)
            summary = SmartSummaryView(available=True, temperature_c=30)
            fresh = utcnow() + timedelta(minutes=5)
            ancient = utcnow() - service._smart_cache_stale_retention() - timedelta(seconds=1)
            for index in range(2000):
                key = ("synthetic", "core", f"enc-{index % 20}", index, (f"da{index}",))
                service._smart_cache[key] = summary
                # Direct assignment, the way tests and older code seed the map,
                # must register with the eviction index too.
                service._smart_cache_until[key] = fresh if index % 100 else ancient
            orphan = ("synthetic", "core", "enc-orphan", 0, ("da-orphan",))
            service._smart_cache[orphan] = summary

            started = time.perf_counter()
            for _ in range(120):
                service._evict_expired_smart_cache_entries()
            elapsed = time.perf_counter() - started

            self.assertEqual(len(service._smart_cache), 1980)
            self.assertNotIn(orphan, service._smart_cache)
            self.assertTrue(all(until == fresh for until in service._smart_cache_until.values()))
            self.assertLess(elapsed, 1.0)

            # Re-setting a fresh key to an old expiry registers the new value too.
            replaced = ("synthetic", "core", "enc-1", 1, ("da1",))
            self.assertIn(replaced, service._smart_cache)
            service._smart_cache_until[replaced] = ancient
            service._evict_expired_smart_cache_entries()
            self.assertNotIn(replaced, service._smart_cache)
            self.assertEqual(len(service._smart_cache), 1979)


if __name__ == "__main__":
    unittest.main()
