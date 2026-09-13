"""Synthetic public-entry-point I/O acceptance for #448; no hardware benchmark.

The batching/off-loop assertions intentionally expose remaining production work.
Only collection/transport is fake: snapshot, SMART parsing, caches and disk stores
are real. Snapshot and grid phases must never be combined in reported budgets.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from app.config import EnclosureProfileConfig, Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import SmartSummaryView
from app.services.inventory import InventoryService, SmartDetailBatch, SnapshotStateBusyError, _smart_detail_batch
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore
from app.services.truenas_ws import TrueNASAPIError, TrueNASRawData


class StoreTrace:
    """Observe real file operations, not dispatch calls or mocked store results."""

    def __init__(self, store):
        self.store = store
        self.paths = {store.file_path, store.file_path.with_suffix(".tmp")}
        self.loop_thread = threading.get_ident()
        self.operations = Counter()
        self.threads = Counter()
        self.methods = Counter()
        self.lock = threading.Lock()

    def record(self, operation):
        with self.lock:
            self.operations[operation] += 1
            category = "loop" if threading.get_ident() == self.loop_thread else "worker"
            self.threads[category] += 1

    @contextmanager
    def capture(self):
        real_open, real_replace = Path.open, Path.replace
        real_load, real_dump = json.load, json.dump

        def opened(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if path in self.paths:
                self.record("write" if "w" in mode else "read")
            return handle

        def replaced(path, target):
            result = real_replace(path, target)
            if path in self.paths:
                self.record("replace")
            return result

        def loaded(handle, *args, **kwargs):
            result = real_load(handle, *args, **kwargs)
            if Path(handle.name) in self.paths:
                self.record("json_load")
            return result

        def dumped(payload, handle, *args, **kwargs):
            result = real_dump(payload, handle, *args, **kwargs)
            if Path(handle.name) in self.paths:
                self.record("json_dump")
            return result

        def observed(name, original):
            def call(*args, **kwargs):
                with self.lock:
                    self.methods[name] += 1
                return original(*args, **kwargs)
            return call

        with ExitStack() as stack:
            for owner, name, replacement in (
                (Path, "open", opened), (Path, "replace", replaced),
                (json, "load", loaded), (json, "dump", dumped),
            ):
                stack.enter_context(patch.object(owner, name, replacement))
            for name in ("load_all", "get_entry", "save_entries", "_write"):
                stack.enter_context(patch.object(self.store, name, observed(name, getattr(self.store, name))))
            yield self

    def report(self, phase, count):
        print(json.dumps({"phase": phase, "slots": count, "operations": dict(self.operations),
                          "store_calls": dict(self.methods), "thread_categories": dict(self.threads)}, sort_keys=True))


class SyntheticAPI:
    def __init__(self, count):
        self.count = count
        self.available = True
        self.calls = 0
        self.hours = 321

    async def fetch_all(self):
        disks = [{"name": f"da{i}", "serial": f"INVENTED-{i:03d}", "model": "Synthetic disk",
                  "enclosure": {"id": "synthetic-enclosure", "slot": i}, "status": "ONLINE"}
                 for i in range(self.count)]
        return TrueNASRawData(
            enclosures=[{"id": "synthetic-enclosure", "label": "Synthetic enclosure",
                         "elements": [{"slot": i, "dev": f"/dev/da{i}", "status": "OK"}
                                      for i in range(self.count)]}],
            disks=disks, pools=[], disk_temperatures={}, smart_test_results=[],
        )

    async def fetch_disk_smartctl(self, device, args):
        self.calls += 1
        if not self.available:
            raise TrueNASAPIError("Synthetic transport unavailable")
        return json.dumps({"smart_status": {"passed": True}, "power_on_time": {"hours": self.hours},
                "temperature": {"current": 31}, "device": {"protocol": "ATA"},
                "model_name": "Synthetic disk"})


class CoreGridPeer:
    """Invented DDP server with independent sockets and aggregate counters."""

    def __init__(self, raw, text=False):
        self.raw, self.text = raw, text
        self.connections = self.logins = self.closes = 0
        self.active = self.peak = 0
        self.methods = Counter()
        self.fail_phase: str | None = None
        self.gate: asyncio.Event | None = None
        self.sent = []
        self.replied = []
        self.started = asyncio.Event()
        self.gate_phase: str | None = None
        self.hours = 321
        self.json_extra = {}

    def connect(self, url, **kwargs):
        from contextlib import asynccontextmanager
        peer = self

        @asynccontextmanager
        async def connection():
            peer.connections += 1
            queue = asyncio.Queue()
            pending = []

            class Socket:
                async def send(self, raw):
                    msg = json.loads(raw)
                    if msg['msg'] == 'connect':
                        queue.put_nowait({'msg': 'connected'})
                        return
                    if msg['msg'] == 'pong':
                        return
                    method = msg['method']
                    reply = {'msg': 'result', 'id': msg['id']}
                    if method == 'auth.login_with_api_key':
                        peer.logins += 1
                        reply['result'] = True
                    elif method == 'disk.smartctl':
                        device, args = msg['params']
                        phase = 'json' if '-j' in args else 'text'
                        peer.methods[phase] += 1
                        peer.sent.append((device, phase))
                        peer.active += 1
                        peer.peak = max(peer.peak, peer.active)
                        hours = peer.hours
                        if peer.gate_phase is None or peer.gate_phase == phase:
                            peer.started.set()
                        async def respond():
                            if peer.gate is not None and (peer.gate_phase is None or peer.gate_phase == phase):
                                await peer.gate.wait()
                            await asyncio.sleep(0)
                            index = int(device.removeprefix('/dev/').removeprefix('da'))
                            for _ in range(index % 3):
                                await asyncio.sleep(0)
                            if peer.fail_phase == 'invalid':
                                reply['result'] = {'invalid': 'SMART payload'}
                            elif peer.fail_phase == phase:
                                reply['error'] = {'reason': 'synthetic unavailable'}
                            elif phase == 'json':
                                reply['result'] = json.dumps({'smart_status': {'passed': True},
                                    'power_on_time': {'hours': hours + index},
                                    'temperature': {'current': 31},
                                    'device': {'protocol': 'ATA' if peer.text else 'NVMe'}, **peer.json_extra})
                            else:
                                reply['result'] = 'Read look-ahead is: Enabled\nWrite cache is: Enabled\n'
                            peer.active -= 1
                            peer.replied.append((device, phase))
                            queue.put_nowait(reply)
                        pending.append(asyncio.create_task(respond()))
                        return
                    else:
                        reply['result'] = {'enclosure.query': peer.raw.enclosures,
                            'disk.query': peer.raw.disks, 'pool.query': [],
                            'disk.temperatures': {}, 'smart.test.results': []}[method]
                    queue.put_nowait(reply)

                async def recv(self):
                    return json.dumps(await queue.get())

            try:
                yield Socket()
            finally:
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                peer.closes += 1
        return connection()


class CoreGridWebsocketIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        SmartGridIOTests.setUp(self)

    def fixture(self, count):
        return SmartGridIOTests.fixture(self, count)

    @contextmanager
    def wire(self, service, peer):
        from app.services.truenas_ws import TrueNASWebsocketClient
        service.system.truenas.api_key = 'synthetic-test-token'
        service.truenas_client = TrueNASWebsocketClient(service.system.truenas)
        with patch('app.services.truenas_ws.connect', peer.connect):
            yield

    async def test_cold_core_grid_counts_real_sessions(self):
        for count in (60, 84):
            for text in (False, True):
                with self.subTest(count=count, text=text), self.fixture(count) as (s, api, store, other):
                    peer = CoreGridPeer(await api.fetch_all(), text)
                    with self.wire(s, peer):
                        snapshot = await s.get_snapshot()
                        snapshot_sessions = peer.connections
                        trace = StoreTrace(store)
                        slots = [v.slot for v in reversed(snapshot.slots)]
                        with trace.capture():
                            result = await s.get_slot_smart_summaries(slots)
                        counts = {'slots': count, 'text': text, 'snapshot_sessions': snapshot_sessions,
                                  'grid_sessions': peer.connections - snapshot_sessions,
                                  'combined_sessions': peer.connections, 'logins': peer.logins,
                                  'closes': peer.closes, 'methods': dict(peer.methods), 'peak': peer.peak}
                        print(json.dumps(counts, sort_keys=True))
                        self.assertEqual([r.slot for r in result], slots)
                        self.assertEqual([r.summary.power_on_hours for r in result], [321 + slot for slot in slots])
                        self.assertEqual(peer.connections - snapshot_sessions, 2 if text else 1)
                        self.assertNotEqual(peer.sent, peer.replied)
                        self.assertEqual(peer.logins, peer.connections)
                        self.assertEqual(peer.closes, peer.connections)
                        self.assertEqual(peer.methods, Counter(json=count, **({'text': count} if text else {})))
                        self.assertLessEqual(trace.operations['read'], 2)
                        self.assertEqual(trace.operations['write'], 1)
                        self.assertEqual(trace.threads['loop'], 0)

    async def test_grid_request_and_service_budgets(self):
        for budget in (1, 2, 12):
            with self.subTest(budget=budget), self.fixture(84) as (s, api, store, other):
                s._smart_operation_limit = budget
                s._smart_operation_semaphore = asyncio.Semaphore(budget)
                peer = CoreGridPeer(await api.fetch_all())
                with self.wire(s, peer):
                    await s.get_snapshot()
                    await asyncio.wait_for(s.get_slot_smart_summaries(list(range(84)), max_concurrency=budget), 15)
                    self.assertLessEqual(peer.peak, budget)
                    self.assertEqual(peer.connections, 2)
        for budget, cap in ((2, 1), (12, 2), (12, 12)):
            with self.subTest(service=budget, request=cap), self.fixture(84) as (s, api, store, other):
                s._smart_operation_limit = budget
                s._smart_operation_semaphore = asyncio.Semaphore(budget)
                peer = CoreGridPeer(await api.fetch_all())
                peer.gate = asyncio.Event()
                with self.wire(s, peer):
                    await s.get_snapshot()
                    owner = asyncio.create_task(s.get_slot_smart_summaries(list(range(40)), max_concurrency=cap))
                    try:
                        await asyncio.wait_for(peer.started.wait(), 3)
                        for _ in range(100):
                            if peer.active == cap:
                                break
                            await asyncio.sleep(0)
                        self.assertEqual(peer.active, cap)
                        overlap = asyncio.create_task(s.get_slot_smart_summaries(list(range(20, 80)), max_concurrency=cap))
                        drawer = asyncio.create_task(s.get_slot_smart_summary(83))
                        for _ in range(20):
                            await asyncio.sleep(0)
                        self.assertLessEqual(peer.peak, budget)
                    finally:
                        peer.gate.set()
                    await asyncio.wait_for(asyncio.gather(owner, overlap, drawer), 10)
                    self.assertLessEqual(peer.peak, budget)
                    self.assertEqual(peer.methods['json'], 81)
                    self.assertFalse(s._smart_load_tasks)

    async def test_warm_stale_negative_and_partial_grid_admission(self):
        with self.fixture(4) as (s, api, store, other):
            peer = CoreGridPeer(await api.fetch_all())
            with self.wire(s, peer):
                await s.get_snapshot()
                await s.get_slot_smart_summaries([0, 1])
                before = peer.connections
                trace = StoreTrace(store)
                with trace.capture():
                    result = await s.get_slot_smart_summaries([1, 0, 1, -1, 999])
                self.assertEqual([r.slot for r in result], [1, 0])
                self.assertEqual(peer.connections, before)
                self.assertFalse(trace.operations)
                await s.get_slot_smart_summaries([0, 1, 2, 3])
                self.assertEqual(peer.connections, before + 1)
                self.assertEqual(peer.methods['json'], 4)


    async def test_phase_failure_preserves_slot_fallbacks(self):
        for phase in ('json', 'text', 'invalid'):
            with self.subTest(phase=phase), self.fixture(4) as (s, api, store, other):
                peer = CoreGridPeer(await api.fetch_all(), text=True)
                peer.fail_phase = phase
                with self.wire(s, peer):
                    await s.get_snapshot()
                    result = await s.get_slot_smart_summaries([0, 1, 2, 3], max_concurrency=2)
                    self.assertEqual([r.summary.available for r in result], [phase == 'text'] * 4)
                    self.assertLessEqual(peer.peak, 2)
                    self.assertEqual(peer.closes, peer.connections)
                    print('FAILURE_SESSIONS', phase, peer.connections - 1, dict(peer.methods))
                    if phase == 'text':
                        self.assertEqual(peer.connections - 1, 6)
                        self.assertEqual([r.summary.power_on_hours for r in result], [321, 322, 323, 324])
                    else:
                        before = peer.connections
                        await s.get_slot_smart_summaries([0, 1, 2, 3])
                        self.assertEqual(peer.connections, before)
                        await s.get_slot_smart_summaries([0], bypass_negative_cache=True)
                        self.assertGreater(peer.connections, before)
                    self.assertFalse(s._smart_load_tasks)

    async def test_cancelled_grid_retains_transport_and_save_owner(self):
        for fail_save in (False, True):
            with self.subTest(fail_save=fail_save), self.fixture(4) as (s, api, store, other):
                peer = CoreGridPeer(await api.fetch_all())
                peer.gate = asyncio.Event()
                contexts = []
                loop = asyncio.get_running_loop()
                previous = loop.get_exception_handler()
                loop.set_exception_handler(lambda _loop, context: contexts.append(context))
                try:
                    with self.wire(s, peer):
                        await s.get_snapshot()
                        real_save = store.save_entries
                        def save(*args, **kwargs):
                            if fail_save:
                                raise OSError('invented save failure')
                            return real_save(*args, **kwargs)
                        with patch.object(store, 'save_entries', save):
                            owner = asyncio.create_task(s.get_slot_smart_summaries([0, 1, 2, 3]))
                            await asyncio.wait_for(peer.started.wait(), 3)
                            joiner = asyncio.create_task(s.get_slot_smart_summaries([3, 2, 1, 0]))
                            drawer = asyncio.create_task(s.get_slot_smart_summary(0))
                            await asyncio.sleep(0)
                            owner.cancel()
                            await asyncio.sleep(0)
                            owner.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await owner
                            peer.gate.set()
                            results = await asyncio.wait_for(asyncio.gather(joiner, drawer, return_exceptions=True), 5)
                            if fail_save:
                                self.assertTrue(all(isinstance(r, OSError) for r in results))
                            else:
                                self.assertEqual([r.slot for r in results[0]], [3, 2, 1, 0])
                            self.assertEqual(peer.connections, 2)
                            self.assertFalse(s._smart_load_tasks)
                            for _ in range(5):
                                await asyncio.sleep(0)
                            self.assertFalse(contexts)
                finally:
                    peer.gate.set()
                    loop.set_exception_handler(previous)

    async def test_invalidation_fences_batched_transport_results(self):
        with self.fixture(4) as (s, api, store, other):
            peer = CoreGridPeer(await api.fetch_all())
            peer.gate = asyncio.Event()
            with self.wire(s, peer):
                await s.get_snapshot()
                owner = asyncio.create_task(s.get_slot_smart_summaries([0, 1, 2, 3]))
                try:
                    await asyncio.wait_for(peer.started.wait(), 3)
                    s.invalidate_snapshot_cache(reason='invented generation change')
                finally:
                    peer.gate.set()
                await asyncio.wait_for(owner, 5)
                self.assertFalse(s._smart_cache)
                self.assertFalse(s._smart_negative_cache)
                for slot in range(4):
                    self.assertFalse(store.get_entry(s.system.id, 'synthetic-enclosure', slot).smart_fields)
                self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), other)

    async def test_replacement_identity_while_transport_awaits(self):
        """A refreshed disk at reused da0 must not inherit the old load."""
        with self.fixture(2) as (s, api, store, other):
            peer = CoreGridPeer(await api.fetch_all())
            peer.gate = asyncio.Event()
            with self.wire(s, peer):
                original = await s.get_snapshot()
                original_slot = next(slot for slot in original.slots if slot.slot == 0)
                self.assertEqual(original_slot.serial, 'INVENTED-000')
                owner = asyncio.create_task(s.get_slot_smart_summaries([0]))
                try:
                    await asyncio.wait_for(peer.started.wait(), 3)
                    self.assertEqual(peer.sent, [('da0', 'json')])
                    # Replace only the peer's inventory identity, not the captured
                    # SlotView, loader, generation, or transport implementation.
                    peer.raw.disks[0]['serial'] = 'INVENTED-REPLACEMENT'
                    replacement = await s.get_snapshot(force_refresh=True)
                    replacement_slot = next(slot for slot in replacement.slots if slot.slot == 0)
                    self.assertEqual(replacement_slot.serial, 'INVENTED-REPLACEMENT')
                    self.assertEqual(replacement_slot.device_name, original_slot.device_name)
                    replacement_entry = store.get_entry(s.system.id, 'synthetic-enclosure', 0)
                    self.assertEqual(replacement_entry.slot_fields['serial'], 'INVENTED-REPLACEMENT')
                    self.assertFalse(replacement_entry.smart_fields)
                finally:
                    peer.gate.set()
                result = await asyncio.wait_for(owner, 5)
                entry = store.get_entry(s.system.id, 'synthetic-enclosure', 0)
                cached = s._smart_cache.get(s._smart_cache_key(replacement_slot))
                print('REPLACEMENT_IDENTITY', json.dumps({
                    'original_serial': original_slot.serial,
                    'replacement_serial': replacement_slot.serial,
                    'returned': result[0].model_dump(mode='json'),
                    'replacement_entry_preserved': entry == replacement_entry,
                    'cached_old_hours': cached.power_on_hours if cached else None,
                    'connections': peer.connections,
                    'logins': peer.logins, 'closes': peer.closes,
                }, sort_keys=True))
                self.assertEqual(entry, replacement_entry, 'Old load overwrote replacement disk persistence')
                self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), other)
                self.assertEqual(peer.connections, peer.closes)
                self.assertFalse(s._smart_load_tasks)
                self.assertIsNone(cached, 'Old disk SMART cached under replacement disk key')
                self.assertFalse(s._smart_negative_cache)
                self.assertFalse(result[0].summary.available)
                self.assertIsNone(result[0].summary.power_on_hours)

    async def test_serial_loss_return_and_replacement_public_matrix(self):
        for single in (False, True):
            for mode in ('inflight', 'warm', 'negative'):
                for returned_serial in ('INVENTED-000', 'INVENTED-REPLACEMENT'):
                    with self.subTest(single=single, mode=mode, returned_serial=returned_serial), self.fixture(2) as (s, api, store, other):
                        peer = CoreGridPeer(await api.fetch_all())
                        with self.wire(s, peer):
                            await s.get_snapshot()
                            async def request():
                                if single:
                                    return await s.get_slot_smart_summary(0)
                                return (await s.get_slot_smart_summaries([0]))[0].summary
                            owner = None
                            if mode == 'warm':
                                self.assertEqual((await request()).power_on_hours, 321)
                            else:
                                peer.gate = asyncio.Event()
                                if mode == 'negative':
                                    peer.fail_phase = 'invalid'
                                owner = asyncio.create_task(request())
                                await asyncio.wait_for(peer.started.wait(), 3)
                            peer.raw.disks[0]['serial'] = None
                            try:
                                await s.get_snapshot(force_refresh=True)
                            finally:
                                if peer.gate is not None:
                                    peer.gate.set()
                            if owner is not None:
                                stale = await asyncio.wait_for(owner, 5)
                                self.assertFalse(stale.available)
                                self.assertIsNone(stale.power_on_hours)
                            peer.fail_phase = None
                            peer.hours = 777
                            calls = peer.methods['json']
                            # Repeat the forced snapshot before fresh admission:
                            # historical serial restoration must not block retry.
                            lost = await s.get_snapshot(force_refresh=True)
                            fresh = await request()
                            self.assertTrue(fresh.available, 'Serial loss must admit current-device SMART')
                            self.assertEqual(fresh.power_on_hours, 777)
                            self.assertGreater(peer.methods['json'], calls)
                            self.assertIsNone(lost.slots[0].serial, 'Device alias cannot prove historical serial')
                            self.assertEqual(s._smart_cache_key(lost.slots[0])[-1], ('device', 'da0'))
                            entry = store.get_entry(s.system.id, 'synthetic-enclosure', 0)
                            self.assertNotIn('serial', entry.slot_fields)
                            self.assertEqual(entry.smart_fields['power_on_hours'], 777)
                            calls = peer.methods['json']
                            self.assertEqual((await request()).power_on_hours, 777)
                            self.assertEqual(peer.methods['json'], calls)
                            peer.raw.disks[0]['serial'] = returned_serial
                            peer.hours = 888
                            await s.get_snapshot(force_refresh=True)
                            self.assertFalse(s._smart_cache)
                            self.assertFalse(s._smart_negative_cache)
                            self.assertEqual((await request()).power_on_hours, 888)
                            self.assertGreater(peer.methods['json'], calls)
                            calls = peer.methods['json']
                            self.assertEqual((await request()).power_on_hours, 888)
                            self.assertEqual(peer.methods['json'], calls)
                            entry = store.get_entry(s.system.id, 'synthetic-enclosure', 0)
                            self.assertEqual(entry.slot_fields['serial'], returned_serial)
                            self.assertEqual(entry.smart_fields['power_on_hours'], 888)
                            self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), other)
                            self.assertFalse(s._smart_load_tasks)
                            self.assertFalse(s._smart_negative_cache)
                            self.assertEqual(peer.connections, peer.closes)
                            print('SERIAL_LOSS_PASS', single, mode, returned_serial)

    async def test_identity_replacement_public_paths(self):
        for single in (False, True):
            for mode in ('inflight', 'negative', 'text', 'warm', 'background', 'aba'):
                with self.subTest(single=single, mode=mode), self.fixture(2) as (s, api, store, other):
                    peer = CoreGridPeer(await api.fetch_all(), text=mode == 'text')
                    with self.wire(s, peer):
                        snapshot = await s.get_snapshot()
                        async def request(stale=False):
                            if single:
                                return await s.get_slot_smart_summary(0, allow_stale_cache=stale)
                            return (await s.get_slot_smart_summaries([0], allow_stale_cache=stale))[0].summary
                        owner = None
                        refreshes = []
                        if mode in ('warm', 'background'):
                            self.assertEqual((await request()).power_on_hours, 321)
                        if mode != 'warm':
                            peer.started.clear()
                            peer.gate = asyncio.Event()
                            peer.gate_phase = 'text' if mode == 'text' else None
                            if mode == 'negative':
                                peer.fail_phase = 'invalid'
                            if mode == 'background':
                                key = s._smart_cache_key(snapshot.slots[0])
                                s._smart_cache_until[key] = datetime.now(timezone.utc) - timedelta(seconds=1)
                                self.assertEqual((await request(True)).power_on_hours, 321)
                                refreshes = list(s._smart_refresh_tasks.values())
                                owner = None
                            else:
                                owner = asyncio.create_task(request())
                            await asyncio.wait_for(peer.started.wait(), 3)
                        peer.raw.disks[0]['serial'] = 'INVENTED-REPLACEMENT'
                        try:
                            replacement = await s.get_snapshot(force_refresh=True)
                            if mode == 'aba':
                                peer.raw.disks[0]['serial'] = 'INVENTED-000'
                                replacement = await s.get_snapshot(force_refresh=True)
                        finally:
                            if peer.gate is not None:
                                peer.gate.set()
                        if mode != 'warm':
                            if owner is not None:
                                old = await asyncio.wait_for(owner, 5)
                                self.assertFalse(old.available, 'Old disk result returned after replacement')
                                self.assertIsNone(old.power_on_hours)
                            await asyncio.gather(*refreshes)
                        key = s._smart_cache_key(replacement.slots[0])
                        self.assertNotIn(key, s._smart_cache)
                        self.assertNotIn(key, s._smart_negative_cache)
                        entry = store.get_entry(s.system.id, 'synthetic-enclosure', 0)
                        assert entry is not None
                        self.assertFalse(entry.smart_fields, 'Replacement retained old disk SMART')
                        peer.fail_phase = None
                        peer.hours = 777
                        before = peer.methods['json']
                        fresh = await request()
                        self.assertTrue(fresh.available)
                        self.assertEqual(fresh.power_on_hours, 777)
                        self.assertGreater(peer.methods['json'], before, 'Reused device must admit a new lookup')
                        before = peer.connections
                        self.assertEqual((await request()).power_on_hours, fresh.power_on_hours)
                        self.assertEqual(peer.connections, before, 'Replacement warm lookup must use cache')
                        self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), other)
                        self.assertEqual(peer.connections, peer.closes)

    async def test_optional_ssh_source_precedence_matrix(self):
        from app.services.ssh_probe import SSHProbe, SSHCommandResult
        scenarios = ('disabled', 'nvme', 'complete', 'sparse', 'advisory', 'ssh-failure',
                     'api-failure', 'both-fail', 'alternate-device', 'alternate-binary')
        for scenario in scenarios:
            with self.subTest(scenario=scenario), self.fixture(2) as (s, api, store, other):
                peer = CoreGridPeer(await api.fetch_all(), text=scenario != 'nvme')
                if scenario == 'complete':
                    peer.json_extra = {
                        'rotation_rate': 7200, 'form_factor': {'name': '2.5 inches'},
                        'sata_version': {'string': 'SATA 3.3'},
                        'interface_speed': {'current': {'string': '6 Gb/s'}},
                        'read_lookahead': {'enabled': True}, 'write_cache': {'enabled': True},
                        'ata_device_statistics': {'pages': [{'table': [
                            {'name': name, 'value': 5} for name in ('Logical Sectors Read',
                            'Logical Sectors Written', 'Number of Read Commands', 'Number of Write Commands')]}]},
                    }
                if scenario in ('api-failure', 'both-fail'):
                    peer.fail_phase = 'json'
                calls = []
                async def run_planned(probe, planner, *, initial_commands):
                    results = []
                    commands = list(initial_commands)
                    for _ in range(12):
                        if not commands:
                            return results
                        for command in commands:
                            calls.append((probe.config.host, command))
                            failure = scenario in ('ssh-failure', 'both-fail')
                            failure |= scenario == 'alternate-device' and command.endswith('/dev/da0')
                            binary_missing = scenario == 'alternate-binary' and command.startswith('sudo -n smartctl ')
                            payload = json.dumps({'smart_status': {'passed': True},
                                'device': {'protocol': 'ATA'}, 'power_on_time': {'hours': 888}})
                            if '-j' not in command:
                                payload = 'Read look-ahead is: Enabled\nWrite cache is: Enabled\n'
                            results.append(SSHCommandResult(command=command, ok=not (failure or binary_missing or scenario == 'advisory'),
                                stdout='' if failure or binary_missing else payload,
                                stderr='command not found' if binary_missing else ('synthetic disk unavailable' if failure else ''),
                                exit_code=127 if binary_missing else (4 if failure else (8 if scenario == 'advisory' else 0))))
                        commands = list(planner(results))
                    self.fail('Synthetic SSH planner did not terminate')
                with self.wire(s, peer):
                    snapshot = await s.get_snapshot()
                    s.system.ssh.enabled = scenario != 'disabled'
                    s.ssh_probe = SSHProbe(s.system.ssh)
                    snapshot.slots[0].smart_device_type = 'sat'
                    if scenario == 'alternate-device':
                        snapshot.slots[0].smart_device_names = ['da0', 'da9']
                    # CORE uses its configured host, never QuantaStor preferred hosts.
                    with patch.object(SSHProbe, 'run_planned_commands', run_planned):
                        result = await s.get_slot_smart_summaries([0, 1], max_concurrency=1)
                    expected_ssh = scenario not in ('disabled', 'nvme', 'complete')
                    self.assertEqual(bool(calls), expected_ssh)
                    self.assertEqual([item.slot for item in result], [0, 1])
                    expected = [None, None] if scenario == 'both-fail' else (
                        [321, 322] if scenario in ('disabled', 'nvme', 'complete', 'ssh-failure') else [888, 888])
                    self.assertEqual([item.summary.power_on_hours for item in result], expected)
                    if calls:
                        self.assertTrue(all(host == 'ssh.invalid' for host, _command in calls))
                        self.assertTrue(all('sudo -n ' in command for _host, command in calls))
                        self.assertTrue(all('-d sat ' in command for _host, command in calls if command.endswith('/dev/da0')))
                        self.assertTrue(all('-d sat ' not in command for _host, command in calls if command.endswith('/dev/da1')))
                    if scenario == 'alternate-device':
                        self.assertTrue(any(command.endswith('/dev/da9') for _host, command in calls))
                    if scenario == 'alternate-binary':
                        self.assertTrue(any('/usr/local/sbin/smartctl' in command for _host, command in calls))
                    self.assertEqual(peer.connections, peer.closes)
                    self.assertFalse(s._smart_load_tasks)
                    self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), other)

    async def test_freshness_survives_batched_transport(self):
        with self.fixture(4) as (s, api, store, other):
            peer = CoreGridPeer(await api.fetch_all())
            with self.wire(s, peer):
                snapshot = await s.get_snapshot()
                SmartGridIOTests.seed_history(self, s, snapshot)
                first = await s.get_slot_smart_summaries([0, 1, 2, 3])
                self.assertEqual([r.summary.power_on_hours for r in first], [321, 322, 323, 324])
                before = store.file_path.read_bytes()
                for key in s._smart_cache_until:
                    s._smart_cache_until[key] = datetime.now(timezone.utc) - timedelta(seconds=1)
                trace = StoreTrace(store)
                with trace.capture():
                    await s.get_slot_smart_summaries([0, 1, 2, 3])
                self.assertNotEqual(store.file_path.read_bytes(), before)
                self.assertLessEqual(trace.operations['read'], 2)
                self.assertEqual(trace.operations['write'], 1)
                self.assertEqual(trace.operations['replace'], 1)
                self.assertEqual(trace.threads['loop'], 0)
                self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), other)

    async def test_drawer_and_non_core_paths_unchanged(self):
        with self.fixture(4) as (s, api, store, other):
            peer = CoreGridPeer(await api.fetch_all(), text=True)
            with self.wire(s, peer):
                await s.get_snapshot()
                await s.get_slot_smart_summary(0)
                self.assertEqual(peer.connections, 3)
                for platform in ('scale', 'linux', 'quantastor'):
                    s.system.truenas.platform = platform
                    s._smart_cache.clear()
                    s._smart_negative_cache.clear()
                    before = peer.connections
                    await s.get_slot_smart_summaries([1, 2, 3])
                    self.assertEqual(peer.connections, before)


class SmartGridIOTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Fail closed before any fixture can accidentally reach a real connection.
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                       "paramiko.SSHClient.connect"):
            self.guards.enter_context(patch(target, side_effect=AssertionError("Unexpected network connection")))

    @contextmanager
    def fixture(self, count):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(config_file=str(root / "unused.yaml"))
            for name in ("runtime_overrides_file", "mapping_file", "sas_fabric_alias_file", "log_file",
                         "profile_file", "slot_detail_cache_file"):
                setattr(settings.paths, name, str(root / name))
            settings.layout.api_slot_number_base = 0
            settings.layout.rows = 1
            settings.layout.columns = count
            settings.layout.slot_count = count
            settings.profiles = [EnclosureProfileConfig(id="synthetic-grid", label="Synthetic grid",
                                                       rows=1, columns=count, slot_count=count,
                                                       slot_layout=[list(range(count))])]
            settings.app.snapshot_cache_ttl_seconds = 3600
            settings.app.smart_cache_ttl_seconds = 3600
            system = SystemConfig(id="synthetic-system", default_profile_id="synthetic-grid", truenas=TrueNASConfig(host="nas.invalid", platform="core"),
                                  ssh=SSHConfig(enabled=False, host="ssh.invalid", key_path=str(root / "unused-key"),
                                                known_hosts_path=str(root / "unused-hosts")))
            api = SyntheticAPI(count)
            store = SlotDetailStore(str(root / "slot-details.json"))
            other = SlotDetailCacheEntry(system_id="other-synthetic-system", enclosure_id="synthetic-enclosure",
                                         slot=0, identifiers=["other-invented-disk"],
                                         slot_fields={"model": "Untouched synthetic model"},
                                         updated_at="2001-01-01T00:00:00+00:00")
            store.save_entries([other])
            ssh = AsyncMock()
            ssh.run_commands.side_effect = AssertionError("Unexpected SSH collection")
            service = InventoryService(settings, system, api, ssh, None,
                                       MappingStore(str(root / "mappings.json")), ProfileRegistry(settings), store)
            yield service, api, store, other

    async def snapshot(self, service, count):
        trace = StoreTrace(service.slot_detail_store)
        with trace.capture():
            snapshot = await service.get_snapshot()
        trace.report("snapshot", count)
        self.assertEqual(len(snapshot.slots), count)
        self.assertEqual(len({s.device_name for s in snapshot.slots}), count)
        self.assertTrue(all(s.present for s in snapshot.slots))
        return snapshot

    async def grid(self, service, slots, phase="grid", **kwargs):
        trace = StoreTrace(service.slot_detail_store)
        with trace.capture():
            result = await service.get_slot_smart_summaries(slots, **kwargs)
        trace.report(phase, len(result))
        self.assertEqual(trace.operations["read"], trace.operations["json_load"])
        self.assertEqual(trace.operations["write"], trace.operations["json_dump"])
        self.assertEqual(trace.operations["write"], trace.operations["replace"])
        return result, trace

    def assert_other(self, store, other):
        self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), other)

    def seed_history(self, service, snapshot):
        entries = [service._build_slot_detail_entry(s, smart_summary=SmartSummaryView(available=True, power_on_hours=17))
                   for s in snapshot.slots]
        service.slot_detail_store.save_entries(entries)

    async def test_cold_grid_has_constant_store_io(self):
        for count in (1, 60, 84):
            with self.subTest(slots=count), self.fixture(count) as (service, api, store, other):
                snapshot = await self.snapshot(service, count)
                slots = [s.slot for s in snapshot.slots]
                result, trace = await self.grid(service, slots)
                self.assertEqual([r.slot for r in result], slots)
                self.assertTrue(all(r.summary.power_on_hours == api.hours for r in result))
                self.assert_other(store, other)
                self.assertLessEqual(trace.operations["read"], 2)
                self.assertEqual(trace.operations["write"], 1)
                self.assertEqual(trace.operations["replace"], 1)

    async def test_persisted_fallback_loads_once_per_grid(self):
        for count in (1, 60, 84):
            with self.subTest(slots=count), self.fixture(count) as (service, api, store, other):
                snapshot = await self.snapshot(service, count)
                self.seed_history(service, snapshot)
                before = store.file_path.read_bytes()
                api.available = False
                result, trace = await self.grid(service, [s.slot for s in snapshot.slots], "fallback")
                self.assertEqual([r.slot for r in result], [s.slot for s in snapshot.slots])
                self.assertTrue(all(r.summary.power_on_hours == 17 for r in result))
                self.assertGreater(api.calls, 0)
                self.assertEqual(before, store.file_path.read_bytes())
                self.assert_other(store, other)
                self.assertEqual(trace.operations["write"], 0)
                self.assertEqual(trace.methods["save_entries"], 0)
                self.assertEqual(trace.operations["read"], 1)

    async def test_store_io_runs_off_event_loop(self):
        for count in (1, 60, 84):
            with self.subTest(slots=count), self.fixture(count) as (service, api, store, other):
                snapshot = await self.snapshot(service, count)
                _, trace = await self.grid(service, [s.slot for s in snapshot.slots])
                self.assertGreater(sum(trace.operations.values()), 0)
                self.assertEqual(trace.threads["loop"], 0)

    async def test_warm_cache_order_dedup_and_invalid_requests(self):
        for count in (1, 60, 84):
            with self.subTest(slots=count), self.fixture(count) as (service, api, store, other):
                snapshot = await self.snapshot(service, count)
                slots = [s.slot for s in reversed(snapshot.slots)]
                await self.grid(service, slots)
                calls = api.calls
                before = store.file_path.read_bytes()
                result, trace = await self.grid(service, slots + slots + [-100, 99999], "warm")
                self.assertEqual([r.slot for r in result], slots)
                self.assertTrue(all(r.summary.power_on_hours == api.hours for r in result))
                self.assertEqual(api.calls, calls)
                self.assertEqual(sum(trace.operations.values()), 0)
                self.assertEqual(before, store.file_path.read_bytes())
                empty, trace = await self.grid(service, [-100, 99999], "invalid")
                self.assertEqual(empty, [])
                self.assertEqual(sum(trace.operations.values()), 0)
                self.assert_other(store, other)

    async def test_fallback_rejects_wrong_disk_identity(self):
        with self.fixture(1) as (service, api, store, other):
            snapshot = await self.snapshot(service, 1)
            self.seed_history(service, snapshot)
            entry = store.get_entry(service.system.id, snapshot.slots[0].enclosure_id, snapshot.slots[0].slot)
            assert entry is not None
            entry.identifiers = ["unrelated-invented-disk"]
            store.save_entries([entry])
            api.available = False
            result, trace = await self.grid(service, [snapshot.slots[0].slot], "identity-rejection")
            self.assertIsNone(result[0].summary.power_on_hours)
            self.assertFalse(result[0].summary.available)
            self.assertEqual(trace.operations["write"], 0)
            self.assert_other(store, other)

    async def test_default_refreshes_history_and_timestamp_only_change_is_written(self):
        with self.fixture(1) as (service, api, store, other):
            snapshot = await self.snapshot(service, 1)
            self.seed_history(service, snapshot)
            slots = [s.slot for s in snapshot.slots]
            result, _ = await self.grid(service, slots, "history-default")
            self.assertEqual(result[0].summary.power_on_hours, 321)
            before = store.load_all()
            # Expire positive entries rather than disabling the cache under test.
            for key in service._smart_cache_until:
                service._smart_cache_until[key] = datetime.now(timezone.utc) - timedelta(seconds=1)
            future = datetime.now(timezone.utc) + timedelta(seconds=10)
            with patch("app.services.slot_detail_store.utcnow", return_value=future):
                result, trace = await self.grid(service, slots, "timestamp-only")
            after = store.load_all()
            key = store._slot_key(service.system.id, snapshot.slots[0].enclosure_id, slots[0])
            self.assertEqual(before[key].smart_fields, after[key].smart_fields)
            self.assertEqual(before[key].slot_fields, after[key].slot_fields)
            self.assertEqual(before[key].identifiers, after[key].identifiers)
            self.assertNotEqual(before[key].updated_at, after[key].updated_at)
            self.assertEqual(trace.operations["write"], 1)
            self.assert_other(store, other)

    async def test_allow_stale_returns_history_then_default_request_refreshes(self):
        with self.fixture(1) as (service, api, store, other):
            snapshot = await self.snapshot(service, 1)
            self.seed_history(service, snapshot)
            # Keep actual scheduling intact. Completion of a scheduled task is not
            # proof it fetched fresh data (the inherited loader can coalesce it).
            with patch.object(service, "_schedule_background_smart_refresh",
                              wraps=service._schedule_background_smart_refresh) as scheduled:
                result, _ = await self.grid(service, [snapshot.slots[0].slot], "history-stale", allow_stale_cache=True)
                self.assertEqual(result[0].summary.power_on_hours, 17)
                scheduled.assert_called_once()
                tasks = list(service._smart_refresh_tasks.values())
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks), 5)
            result, _ = await self.grid(service, [snapshot.slots[0].slot], "history-default-after-stale")
            self.assertEqual(result[0].summary.power_on_hours, 321)
            self.assertGreater(api.calls, 0)
            self.assert_other(store, other)

    async def test_forced_snapshot_preserves_normal_freshness_writes(self):
        with self.fixture(1) as (service, api, store, other):
            await self.snapshot(service, 1)
            before = store.load_all()
            future = datetime.now(timezone.utc) + timedelta(seconds=10)
            trace = StoreTrace(store)
            with patch("app.services.slot_detail_store.utcnow", return_value=future), trace.capture():
                await service.get_snapshot(force_refresh=True)
            trace.report("snapshot-forced", 1)
            self.assertEqual(trace.operations["write"], 1)
            self.assertNotEqual(before, store.load_all())
            self.assert_other(store, other)

    async def test_negative_cache_bypass_performs_fresh_work(self):
        with self.fixture(1) as (service, api, store, other):
            snapshot = await self.snapshot(service, 1)
            slots = [snapshot.slots[0].slot]
            api.available = False
            result, _ = await self.grid(service, slots, "negative-cold")
            self.assertFalse(result[0].summary.available)
            calls = api.calls
            api.available = True
            result, trace = await self.grid(service, slots, "negative-warm")
            self.assertFalse(result[0].summary.available)
            self.assertEqual(api.calls, calls)
            self.assertEqual(sum(trace.operations.values()), 0)
            result, trace = await self.grid(service, slots, "negative-bypass", bypass_negative_cache=True)
            self.assertEqual(result[0].summary.power_on_hours, 321)
            self.assertGreater(api.calls, calls)
            self.assertEqual(trace.operations["write"], 1)
            self.assert_other(store, other)

    async def test_trace_control_exact_repeat_and_timestamp_mutation(self):
        with self.fixture(1) as (service, api, store, other):
            repeat = StoreTrace(store)
            with repeat.capture():
                await asyncio.to_thread(store.save_entries, [other])
            self.assertEqual(repeat.operations, Counter(read=1, json_load=1))
            self.assertEqual(repeat.threads["loop"], 0)
            changed = other.model_copy(update={"updated_at": "2002-01-01T00:00:00+00:00"})
            trace = StoreTrace(store)
            with trace.capture():
                await asyncio.to_thread(store.save_entries, [changed])
            self.assertEqual(trace.operations, Counter(read=1, json_load=1, write=1, json_dump=1, replace=1))
            self.assertEqual(trace.threads["worker"], 5)
            self.assertEqual(trace.threads["loop"], 0)
            self.assert_other(store, changed)


class SnapshotIOTests(unittest.IsolatedAsyncioTestCase):
    fixture = SmartGridIOTests.fixture
    setUp = SmartGridIOTests.setUp

    async def until(self, predicate):
        async def spin():
            while not predicate():
                await asyncio.sleep(.001)
        await asyncio.wait_for(spin(), 3)

    async def test_snapshot_real_io_off_loop_and_exact_freshness(self):
        for count in (1, 60, 84):
            with self.subTest(slots=count), self.fixture(count) as (s, api, store, other):
                for phase in ('cold', 'forced', 'warm'):
                    trace = StoreTrace(store)
                    stamp = datetime(2030, 1, 1 if phase == 'cold' else 2, tzinfo=timezone.utc)
                    with patch('app.services.slot_detail_store.utcnow', return_value=stamp), trace.capture():
                        snapshot = await s.get_snapshot(force_refresh=phase == 'forced')
                    trace.report('snapshot-' + phase, count)
                    self.assertEqual(len(snapshot.slots), count)
                    expected = Counter() if phase == 'warm' else Counter(read=2, json_load=2, write=1, json_dump=1, replace=1)
                    self.assertEqual(trace.operations, expected)
                    self.assertEqual(trace.threads['loop'], 0)
                    entries = store.load_all()
                    self.assertEqual(entries[store._slot_key(other.system_id, other.enclosure_id, other.slot)], other)
                    for slot in snapshot.slots:
                        entry = store.get_entry(s.system.id, slot.enclosure_id, slot.slot, loaded_entries=entries)
                        self.assertEqual(entry.updated_at, stamp.isoformat())

    async def test_cancel_twice_retains_snapshot_owner_until_real_io_drains(self):
        for phase in ('load_all', 'save_entries'):
            for fail in (False, True):
                with self.subTest(phase=phase, fail=fail), self.fixture(1) as (s, api, store, other):
                    await s.get_snapshot()
                    entered, release = threading.Event(), threading.Event()
                    original = getattr(store, phase)
                    loop_thread = threading.get_ident()
                    def blocked(*args, **kwargs):
                        if not entered.is_set():
                            entered.set()
                            if threading.get_ident() == loop_thread:
                                raise AssertionError('snapshot I/O on loop')
                            if not release.wait(3):
                                raise AssertionError('worker release timeout')
                            if fail:
                                raise RuntimeError('synthetic late I/O failure')
                        return original(*args, **kwargs)
                    errors = []
                    loop = asyncio.get_running_loop()
                    previous = loop.get_exception_handler()
                    loop.set_exception_handler(lambda loop, context: errors.append(context))
                    tasks = []
                    try:
                        with patch.object(store, phase, blocked):
                            owner = asyncio.create_task(s.get_snapshot(force_refresh=True))
                            tasks.append(owner)
                            await self.until(entered.is_set)
                            for _ in range(2):
                                owner.cancel()
                                await asyncio.sleep(0)
                                self.assertFalse(owner.done())
                            successor = asyncio.create_task(s.get_snapshot(force_refresh=True))
                            tasks.append(successor)
                            await asyncio.sleep(0)
                            self.assertFalse(successor.done())
                            release.set()
                            outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
                            self.assertIsInstance(outcomes[0], asyncio.CancelledError)
                            self.assertEqual(len(outcomes[1].slots), 1)
                        # Drain completion callbacks before checking every loop
                        # error context, including Python 3.14 shield diagnostics.
                        await asyncio.sleep(0)
                        await asyncio.sleep(0)
                        self.assertFalse(errors)
                        self.assertFalse(s._snapshot_activity)
                    finally:
                        release.set()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        loop.set_exception_handler(previous)

    async def test_snapshot_uncancelled_io_failure_propagates(self):
        for phase in ('load_all', 'save_entries'):
            with self.subTest(phase=phase), self.fixture(1) as (s, api, store, other):
                await s.get_snapshot()
                failure = RuntimeError('synthetic snapshot I/O failure')
                with patch.object(store, phase, side_effect=failure):
                    with self.assertRaises(RuntimeError) as caught:
                        await s.get_snapshot(force_refresh=True)
                self.assertIs(caught.exception, failure)
                self.assertFalse(s._snapshot_activity)
                self.assertEqual(len((await s.get_snapshot(force_refresh=True)).slots), 1)

    async def test_snapshot_commit_fenced_after_serialization(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), self.fixture(1) as (s, api, store, other):
                await s.get_snapshot()
                before = store.file_path.read_bytes()
                entered, release = threading.Event(), threading.Event()
                original = json.dump
                loop_thread = threading.get_ident()
                def blocked(payload, handle, *args, **kwargs):
                    result = original(payload, handle, *args, **kwargs)
                    if Path(handle.name) == store.file_path.with_suffix('.tmp'):
                        entered.set()
                        if threading.get_ident() == loop_thread:
                            raise AssertionError('snapshot serialization on loop')
                        if not release.wait(3):
                            raise AssertionError('worker release timeout')
                    return result
                owner = None
                try:
                    with patch.object(json, 'dump', blocked):
                        owner = asyncio.create_task(s.get_snapshot(force_refresh=True))
                        await self.until(entered.is_set)
                        if cancel:
                            owner.cancel()
                            await asyncio.sleep(0)
                        s.invalidate_snapshot_cache(reason='synthetic change', cache_keys=['synthetic-enclosure'])
                        release.set()
                        outcomes = await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 3)
                        self.assertIsInstance(outcomes[0], asyncio.CancelledError if cancel else SnapshotStateBusyError)
                    self.assertEqual(store.file_path.read_bytes(), before)
                    self.assertFalse(store.file_path.with_suffix('.tmp').exists())
                finally:
                    release.set()
                    if owner is not None:
                        await asyncio.gather(owner, return_exceptions=True)

    async def test_concurrent_grid_replacement_survives_snapshot_read(self):
        with self.fixture(1) as (s, api, store, other):
            snapshot = await s.get_snapshot()
            entered, release = threading.Event(), threading.Event()
            original = store.load_all
            loop_thread = threading.get_ident()
            def blocked():
                result = original()
                if not entered.is_set():
                    entered.set()
                    if threading.get_ident() == loop_thread:
                        raise AssertionError('snapshot read on loop')
                    if not release.wait(3):
                        raise AssertionError('worker release timeout')
                return result
            owner = None
            try:
                with patch.object(store, 'load_all', blocked):
                    owner = asyncio.create_task(s.get_snapshot(force_refresh=True))
                    await self.until(entered.is_set)
                    result = await asyncio.wait_for(s.get_slot_smart_summaries([snapshot.slots[0].slot]), 3)
                    self.assertEqual(result[0].summary.power_on_hours, 321)
                    committed = store.file_path.read_bytes()
                    release.set()
                    await asyncio.wait_for(owner, 3)
                self.assertEqual(store.file_path.read_bytes(), committed)
                self.assertEqual(store.get_entry(s.system.id, 'synthetic-enclosure', 0).smart_fields['power_on_hours'], 321)
            finally:
                release.set()
                if owner is not None:
                    await asyncio.gather(owner, return_exceptions=True)


# Reviewed concurrency regressions; original acceptance budgets above unchanged.

class SmartGridConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    fixture = SmartGridIOTests.fixture

    async def asyncSetUp(self):
        self.loop_errors = []
        loop = asyncio.get_running_loop()
        self.previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: self.loop_errors.append(context))

    async def asyncTearDown(self):
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(self.loop_errors, [])
        finally:
            asyncio.get_running_loop().set_exception_handler(self.previous_handler)

    def setUp(self):
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex', 'paramiko.SSHClient.connect'):
            self.guards.enter_context(patch(target, side_effect=AssertionError('network forbidden')))

    async def until(self, predicate):
        async def spin():
            while not predicate():
                await asyncio.sleep(.001)
        await asyncio.wait_for(spin(), 3)

    async def drain(self, baseline):
        def remaining():
            return [t for t in asyncio.all_tasks() if t not in baseline and t is not asyncio.current_task() and not t.done()]
        await self.until(lambda: not remaining())
        self.assertEqual(remaining(), [])
        # Task completion can precede shield's late-failure callback on 3.14.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_cancelled_retained_waits_observe_late_failure(self):
        # Exercise each other inventory shield call site through its real caller.
        for path in ('entries', 'dependencies', 'source_refresh', 'slot_loader', 'slot_saved'):
            with self.subTest(path=path), self.fixture(1) as (s, api, store, other):
                snapshot = await s.get_snapshot()
                baseline = asyncio.all_tasks()
                loop = asyncio.get_running_loop()
                errors = []
                previous = loop.get_exception_handler()
                loop.set_exception_handler(lambda loop, context: errors.append(context))
                entered, release = threading.Event(), threading.Event()
                loop_thread = threading.get_ident()
                failure = RuntimeError('synthetic retained failure')
                def blocked():
                    self.assertNotEqual(threading.get_ident(), loop_thread)
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('worker release timeout')
                    raise failure
                worker = asyncio.create_task(asyncio.to_thread(blocked))
                batch = SmartDetailBatch(store)
                try:
                    with ExitStack() as patches:
                        if path == 'entries':
                            batch.loaded = worker
                            call = batch.entries()
                        elif path == 'dependencies':
                            batch.dependencies.add(worker)
                            call = batch.wait_dependencies()
                        elif path == 'source_refresh':
                            patches.enter_context(patch.object(s, '_schedule_background_source_bundle_refresh', return_value=worker))
                            call = s._background_snapshot_refresh('synthetic-enclosure')
                        else:
                            slot = snapshot.slots[0]
                            key = s._smart_cache_key(slot)
                            loader = worker
                            if path == 'slot_saved':
                                batch.saved = worker
                                loader = loop.create_future()
                                loader.set_result(SmartSummaryView())
                                s._smart_load_batches[loader] = batch
                            s._smart_load_tasks[key] = loader
                            call = s._get_slot_smart_summary_for_slot_view(slot)
                        caller = asyncio.create_task(call)
                        await self.until(entered.is_set)
                        # Let nested gather/dependency waiters reach suspension.
                        for _ in range(5):
                            await asyncio.sleep(0)
                        for _ in range(2):
                            caller.cancel()
                            await asyncio.sleep(0)
                        with self.assertRaises(asyncio.CancelledError):
                            await caller
                        self.assertFalse(worker.done())
                        release.set()
                        outcome = await asyncio.gather(worker, return_exceptions=True)
                        self.assertIs(outcome[0], failure)
                        await self.drain(baseline)
                        self.assertEqual(errors, [], path)
                finally:
                    release.set()
                    await asyncio.gather(worker, return_exceptions=True)
                    await self.drain(baseline)
                    loop.set_exception_handler(previous)
                    s._smart_load_tasks.clear()
                    s._smart_load_batches.clear()

    async def test_concurrent_same_and_different_grids(self):
        for second in ([0, 1], [1, 2], [2, 3]):
            with self.subTest(second=second), self.fixture(4) as (s, api, store, other):
                await s.get_snapshot()
                baseline = asyncio.all_tasks()
                entered, release = asyncio.Event(), asyncio.Event()
                real = api.fetch_disk_smartctl
                async def delayed(device, args):
                    entered.set()
                    await asyncio.wait_for(release.wait(), 3)
                    return await real(device, args)
                with patch.object(api, 'fetch_disk_smartctl', delayed):
                    a = asyncio.create_task(s.get_slot_smart_summaries([0, 1]))
                    await asyncio.wait_for(entered.wait(), 3)
                    b = asyncio.create_task(s.get_slot_smart_summaries(second))
                    await asyncio.sleep(.01)
                    release.set()
                    ra, rb = await asyncio.wait_for(asyncio.gather(a, b), 3)
                self.assertEqual([x.slot for x in rb], second)
                self.assertTrue(all(x.summary.power_on_hours == 321 for x in ra + rb))
                self.assertEqual(api.calls, 2 * len(set([0, 1] + second)))
                for slot in set([0, 1] + second):
                    self.assertEqual(store.get_entry(s.system.id, 'synthetic-enclosure', slot).smart_fields['power_on_hours'], 321)
                self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), other)
                await self.drain(baseline)
                self.assertIsNone(_smart_detail_batch.get())

    async def test_cancel_during_load_and_save_success_and_failure(self):
        for phase in ('load_all', 'save_entries'):
            for fail in (False, True):
                with self.subTest(phase=phase, fail=fail), self.fixture(2) as (s, api, store, other):
                    await s.get_snapshot()
                    baseline = asyncio.all_tasks()
                    before = store.file_path.read_bytes()
                    entered, release = threading.Event(), threading.Event()
                    real = getattr(store, phase)
                    loop_thread = threading.get_ident()
                    def gated(*args, **kwargs):
                        self.assertNotEqual(threading.get_ident(), loop_thread)
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError('worker release timeout')
                        if fail:
                            raise RuntimeError('synthetic store failure')
                        return real(*args, **kwargs)
                    errors = []
                    loop = asyncio.get_running_loop()
                    previous = loop.get_exception_handler()
                    loop.set_exception_handler(lambda loop, context: errors.append(context))
                    try:
                        with patch.object(store, phase, gated):
                            a = asyncio.create_task(s.get_slot_smart_summaries([0, 1]))
                            await self.until(entered.is_set)
                            for _ in range(2):
                                a.cancel()
                                await asyncio.sleep(0)
                            with self.assertRaises(asyncio.CancelledError):
                                await a
                            self.assertTrue(any(t not in baseline for t in asyncio.all_tasks()))
                            self.assertEqual(store.file_path.read_bytes(), before)
                            release.set()
                            await self.drain(baseline)
                        self.assertEqual(errors, [], 'late retained batch failure reached loop handler')
                    finally:
                        release.set()
                        await self.drain(baseline)
                        loop.set_exception_handler(previous)
                    self.assertFalse(s._smart_load_tasks)
                    self.assertFalse(s._smart_refresh_tasks)
                    self.assertIsNone(_smart_detail_batch.get())
                    if fail:
                        self.assertEqual(store.file_path.read_bytes(), before)
                    else:
                        self.assertEqual(store.get_entry(s.system.id, 'synthetic-enclosure', 0).smart_fields['power_on_hours'], 321)

    async def test_cancel_remote_then_join_and_cleanup(self):
        with self.fixture(2) as (s, api, store, other):
            await s.get_snapshot()
            baseline = asyncio.all_tasks()
            entered, release = asyncio.Event(), asyncio.Event()
            real = api.fetch_disk_smartctl
            async def gated(device, args):
                entered.set()
                await asyncio.wait_for(release.wait(), 3)
                return await real(device, args)
            with patch.object(api, 'fetch_disk_smartctl', gated):
                a = asyncio.create_task(s.get_slot_smart_summaries([0, 1]))
                await asyncio.wait_for(entered.wait(), 3)
                a.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await a
                b = asyncio.create_task(s.get_slot_smart_summaries([0, 1]))
                release.set()
                result = await asyncio.wait_for(b, 3)
                self.assertEqual(len(result), 2)
                await self.drain(baseline)
            self.assertEqual(api.calls, 4)
            self.assertEqual(store.get_entry(s.system.id, 'synthetic-enclosure', 0).smart_fields['power_on_hours'], 321)

    async def test_background_context_reset_real_cached_refresh(self):
        with self.fixture(1) as (s, api, store, other):
            snapshot = await s.get_snapshot()
            await s.get_slot_smart_summaries([0])
            for key in s._smart_cache_until:
                s._smart_cache_until[key] = datetime.now(timezone.utc) - timedelta(seconds=1)
            api.hours = 654
            parent = SmartDetailBatch(store)
            baseline = asyncio.all_tasks()
            token = _smart_detail_batch.set(parent)
            try:
                await s.get_slot_smart_summaries([0], allow_stale_cache=True)
                await self.drain(baseline)
                self.assertIs(_smart_detail_batch.get(), parent)
            finally:
                _smart_detail_batch.reset(token)
            self.assertIsNone(parent.loaded)
            self.assertEqual(parent.pending, [])
            self.assertEqual(store.get_entry(s.system.id, snapshot.slots[0].enclosure_id, 0).smart_fields['power_on_hours'], 654)

    async def test_real_store_concurrent_conflicts_exact_values(self):
        with self.fixture(3) as (s, api, store, other):
            await s.get_snapshot()
            expected = store.load_all()
            first = store.get_entry(s.system.id, 'synthetic-enclosure', 0)
            second = store.get_entry(s.system.id, 'synthetic-enclosure', 1)
            third = store.get_entry(s.system.id, 'synthetic-enclosure', 2)
            # Exact JSON numeric distinction must fence a conflict.
            first.slot_fields['probe'] = True
            store.save_entries([first])
            expected = store.load_all()
            concurrent = first.model_copy(deep=True)
            concurrent.slot_fields['probe'] = 1
            changed_other = other.model_copy(update={'updated_at': '2040-01-01T00:00:00+00:00'})
            await asyncio.to_thread(store.save_entries, [concurrent, changed_other])
            newer_second = second.model_copy(update={'updated_at': '2041-01-01T00:00:00+00:00'})
            await asyncio.to_thread(store.save_entries, [first, newer_second, third], expected_entries=expected)
            self.assertIs(type(store.get_entry(s.system.id, first.enclosure_id, 0).slot_fields['probe']), int)
            self.assertEqual(store.get_entry(s.system.id, second.enclosure_id, 1).updated_at, newer_second.updated_at)
            self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), changed_other)
            # A removed system entry must not be resurrected by the old read snapshot.
            baseline = store.load_all()
            await asyncio.to_thread(store.prune_unknown_systems, {other.system_id})
            await asyncio.to_thread(store.save_entries, [first, second], expected_entries=baseline)
            self.assertIsNone(store.get_entry(s.system.id, first.enclosure_id, 0))

    async def test_generation_invalidation_fences_already_queued_entry(self):
        with self.fixture(2) as (s, api, store, other):
            await s.get_snapshot()
            baseline = asyncio.all_tasks()
            release = asyncio.Event()
            real = api.fetch_disk_smartctl
            async def delayed(device, args):
                if device.endswith('da1'):
                    await asyncio.wait_for(release.wait(), 3)
                return await real(device, args)
            with patch.object(api, 'fetch_disk_smartctl', delayed):
                a = asyncio.create_task(s.get_slot_smart_summaries([0, 1]))
                await self.until(lambda: bool(s._smart_cache))
                self.assertFalse(store.get_entry(s.system.id, 'synthetic-enclosure', 0).smart_fields)
                s.invalidate_snapshot_cache(reason='synthetic generation fence')
                release.set()
                await asyncio.wait_for(a, 3)
                await self.drain(baseline)
            entry = store.get_entry(s.system.id, 'synthetic-enclosure', 0)
            print('GENERATION_PROBE', json.dumps({'smart_fields_after_invalidation': entry.smart_fields, 'in_memory_cache_empty': not s._smart_cache}))
            self.assertFalse(entry.smart_fields, 'Old generation entry was flushed after invalidation')

    async def test_inflight_join_surfaces_save_failure(self):
        for cancel in (False, True):
            for batch_join in (False, True):
                with self.subTest(cancel=cancel, batch_join=batch_join):
                    await self._inflight_join_failure(cancel=cancel, batch_join=batch_join)

    async def _inflight_join_failure(self, *, cancel, batch_join):
        with self.fixture(1) as (s, api, store, other):
            await s.get_snapshot()
            baseline = asyncio.all_tasks()
            remote_entered, remote_release = asyncio.Event(), asyncio.Event()
            save_entered, save_release = threading.Event(), threading.Event()
            real = api.fetch_disk_smartctl
            async def remote(device, args):
                remote_entered.set()
                await asyncio.wait_for(remote_release.wait(), 3)
                return await real(device, args)
            def save(*args, **kwargs):
                save_entered.set()
                if not save_release.wait(3):
                    raise AssertionError('save release timed out')
                raise OSError('synthetic disk-full failure')
            try:
                with patch.object(api, 'fetch_disk_smartctl', remote), patch.object(store, 'save_entries', save):
                    a = asyncio.create_task(s.get_slot_smart_summaries([0]))
                    await asyncio.wait_for(remote_entered.wait(), 3)
                    b = asyncio.create_task(
                        s.get_slot_smart_summaries([0]) if batch_join else s.get_slot_smart_summary(0)
                    )
                    # The API remains blocked, so the second request cannot get a positive cache hit.
                    await asyncio.sleep(.02)
                    self.assertFalse(s._smart_cache)
                    self.assertEqual(len(s._smart_load_tasks), 1)
                    remote_release.set()
                    await self.until(save_entered.is_set)
                    await asyncio.sleep(.02)
                    premature = b.done()
                    if cancel:
                        a.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await a
                    save_release.set()
                    results = await asyncio.wait_for(asyncio.gather(a, b, return_exceptions=True), 3)
                    await self.drain(baseline)
            finally:
                save_release.set()
                remote_release.set()
            print('INFLIGHT_JOIN_FAILURE', {'owner': type(results[0]).__name__, 'joiner': type(results[1]).__name__, 'join_returned_before_save': premature, 'persisted': bool(store.get_entry(s.system.id, 'synthetic-enclosure', 0).smart_fields)})
            self.assertIsInstance(results[0], asyncio.CancelledError if cancel else OSError)
            self.assertIsInstance(results[1], OSError, 'The joined loader success hid its owning batch persistence failure')
            self.assertFalse(premature)
            if not cancel:
                self.assertIs(results[0], results[1])
            self.assertFalse(s._smart_load_batches)

    async def test_cancelled_batch_does_not_write_invalidated_generation(self):
        with self.fixture(2) as (s, api, store, other):
            await s.get_snapshot()
            baseline = asyncio.all_tasks()
            release = asyncio.Event()
            real = api.fetch_disk_smartctl
            async def remote(device, args):
                if device.endswith('da1'):
                    await asyncio.wait_for(release.wait(), 3)
                return await real(device, args)
            with patch.object(api, 'fetch_disk_smartctl', remote):
                a = asyncio.create_task(s.get_slot_smart_summaries([0, 1]))
                await self.until(lambda: bool(s._smart_cache))
                a.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await a
                s.invalidate_snapshot_cache(reason='synthetic cancelled batch fence')
                release.set()
                await self.drain(baseline)
            fields = store.get_entry(s.system.id, 'synthetic-enclosure', 0).smart_fields
            print('CANCELLED_GENERATION', {'persisted_old_fields': fields, 'load_tasks': len(s._smart_load_tasks)})
            self.assertFalse(fields, 'Cancelled owner later flushed an invalidated entry')


    async def test_generation_fenced_at_worker_and_commit_boundary(self):
        for phase in ('save_entries', 'json_dump'):
            for cancel in (False, True):
                for scoped in (False, True):
                    with self.subTest(phase=phase, cancel=cancel, scoped=scoped), self.fixture(1) as (s, api, store, other):
                        await s.get_snapshot()
                        baseline = asyncio.all_tasks()
                        before = store.file_path.read_bytes()
                        entered, release = threading.Event(), threading.Event()
                        real = store.save_entries if phase == 'save_entries' else json.dump
                        def gated(*args, **kwargs):
                            if phase == 'json_dump':
                                result = real(*args, **kwargs)
                            entered.set()
                            if not release.wait(3):
                                raise AssertionError('worker release timeout')
                            return real(*args, **kwargs) if phase == 'save_entries' else result
                        target = patch.object(store, 'save_entries', gated) if phase == 'save_entries' else patch('app.services.slot_detail_store.json.dump', gated)
                        try:
                            with target:
                                owner = asyncio.create_task(s.get_slot_smart_summaries([0]))
                                await self.until(entered.is_set)
                                if cancel:
                                    owner.cancel()
                                    with self.assertRaises(asyncio.CancelledError):
                                        await owner
                                s.invalidate_snapshot_cache(reason='synthetic commit fence', cache_keys=['synthetic-enclosure'] if scoped else None)
                                release.set()
                                if not cancel:
                                    await asyncio.wait_for(owner, 3)
                                await self.drain(baseline)
                        finally:
                            release.set()
                        self.assertEqual(store.file_path.read_bytes(), before)
                        self.assertFalse(store.file_path.with_suffix('.tmp').exists())
                        self.assertFalse(s._smart_load_tasks)

    async def test_cross_owned_grids_publish_before_waiting_dependencies(self):
        for fail in (False, True):
            for cancel in (False, True):
                with self.subTest(fail=fail, cancel=cancel), self.fixture(2) as (s, api, store, other):
                    await s.get_snapshot()
                    baseline = asyncio.all_tasks()
                    before = store.file_path.read_bytes()
                    entered = [asyncio.Event(), asyncio.Event()]
                    release = asyncio.Event()
                    batches = []
                    real_helper = s._get_slot_smart_summary_for_slot_view
                    real_remote = api.fetch_disk_smartctl
                    async def helper(slot_view, **kwargs):
                        batch = _smart_detail_batch.get()
                        if batch not in batches:
                            batches.append(batch)
                        index = batches.index(batch)
                        # A owns slot 0, B owns slot 1; their other tasks really join.
                        if slot_view.slot != index:
                            await asyncio.wait_for(entered[slot_view.slot].wait(), 3)
                        return await real_helper(slot_view, **kwargs)
                    async def remote(device, args):
                        index = 1 if device.endswith('da1') else 0
                        entered[index].set()
                        await asyncio.wait_for(release.wait(), 3)
                        return await real_remote(device, args)
                    real_save = store.save_entries
                    def save(*args, **kwargs):
                        if fail:
                            raise OSError('synthetic shared persistence failure')
                        return real_save(*args, **kwargs)
                    try:
                        with patch.object(s, '_get_slot_smart_summary_for_slot_view', helper), patch.object(api, 'fetch_disk_smartctl', remote), patch.object(store, 'save_entries', save):
                            a = asyncio.create_task(s.get_slot_smart_summaries([0, 1]))
                            await asyncio.wait_for(entered[0].wait(), 3)
                            b = asyncio.create_task(s.get_slot_smart_summaries([1, 0]))
                            await asyncio.wait_for(entered[1].wait(), 3)
                            await asyncio.sleep(.02)
                            self.assertFalse(s._smart_cache)
                            self.assertEqual(len(s._smart_load_tasks), 2)
                            if cancel:
                                a.cancel()
                                with self.assertRaises(asyncio.CancelledError):
                                    await a
                            release.set()
                            results = await asyncio.wait_for(asyncio.gather(a, b, return_exceptions=True), 3)
                            await self.drain(baseline)
                    finally:
                        release.set()
                    self.assertEqual(api.calls, 4)
                    self.assertFalse(s._smart_load_tasks)
                    if fail:
                        self.assertIsInstance(results[1], OSError)
                        if not cancel:
                            self.assertIsInstance(results[0], OSError)
                        self.assertEqual(store.file_path.read_bytes(), before)
                    else:
                        self.assertEqual([item.slot for item in results[1]], [1, 0])
                        for slot in (0, 1):
                            self.assertEqual(store.get_entry(s.system.id, 'synthetic-enclosure', slot).smart_fields['power_on_hours'], 321)
                    self.assertEqual(store.get_entry(other.system_id, other.enclosure_id, other.slot), other)
