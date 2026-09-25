"""Count SSH connections and wall time for a modeled SSH SMART grid load (#448).

A fake paramiko client stands in for the NAS: each connection costs a
simulated handshake (key exchange, host-key check, auth) and each command a
simulated round trip. N bays each ask for one planned SMART session through
``InventoryService._run_ssh_planned_commands``. As in a real grid load, at most
``smart_batch_max_concurrency`` bays are in flight at once. No network is used.

Run it against two checkouts and compare::

    python scripts/benchmark_ssh_smart_sessions.py --bays 24 60 --concurrency 1 4 12
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig  # noqa: E402
from app.services.inventory import InventoryService  # noqa: E402
from app.services.mapping_store import MappingStore  # noqa: E402
from app.services.profile_registry import ProfileRegistry  # noqa: E402
from app.services.slot_detail_store import SlotDetailStore  # noqa: E402
from app.services.ssh_probe import SSHProbe  # noqa: E402

HOST = "192.0.2.10"


class _Stream:
    def __init__(self, data: bytes = b"", channel: Any = None) -> None:
        self._data = data
        self.channel = channel

    def read(self, _size: int = -1) -> bytes:
        return self._data

    def write(self, _data: str) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Channel:
    def recv_exit_status(self) -> int:
        return 0

    def shutdown_write(self) -> None:
        return None


class _Transport:
    def is_active(self) -> bool:
        return True


class FakeNAS:
    """Counts logins and open channels; sleeps to model latency."""

    def __init__(self, handshake_seconds: float, command_seconds: float) -> None:
        self.handshake_seconds = handshake_seconds
        self.command_seconds = command_seconds
        self.lock = threading.Lock()
        self.connections = 0
        self.commands = 0
        self.open_channels = 0
        self.peak_channels = 0

    def client(self) -> Any:
        nas = self
        time.sleep(self.handshake_seconds)
        with self.lock:
            self.connections += 1

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.close()

            def close(self) -> None:
                return None

            def get_transport(self):
                return _Transport()

            def exec_command(self, _command: str, timeout: float | None = None):
                with nas.lock:
                    nas.commands += 1
                    nas.open_channels += 1
                    nas.peak_channels = max(nas.peak_channels, nas.open_channels)
                try:
                    time.sleep(nas.command_seconds)
                finally:
                    with nas.lock:
                        nas.open_channels -= 1
                channel = _Channel()
                return _Stream(channel=channel), _Stream(b'{"smart_status": {"passed": true}}', channel), _Stream(b"", channel)

        return Client()


async def run_case(bays: int, concurrency: int, handshake: float, command: float, commands_per_bay: int) -> dict[str, Any]:
    nas = FakeNAS(handshake, command)
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = Settings()
        settings.app.smart_batch_max_concurrency = concurrency
        system = SystemConfig(
            id="bench",
            truenas=TrueNASConfig(platform="scale"),
            ssh=SSHConfig(enabled=True, host=HOST, user="operator"),
        )
        probe = SSHProbe(system.ssh)
        probe._client = nas.client  # type: ignore[method-assign]
        service = InventoryService(
            settings, system, None, probe, None,
            MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
        )
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def bay(index: int) -> int:
            commands = [f"smartctl -j -a /dev/da{index} #{step}" for step in range(commands_per_bay)]

            def planner(results: list[Any]) -> list[str]:
                return commands[len(results):len(results) + 1]

            async with semaphore:
                results = await service._run_ssh_planned_commands(
                    planner, initial_commands=commands[:1], host=HOST,
                )
            return sum(1 for result in results if result.ok)

        started = time.perf_counter()
        ok = await asyncio.gather(*(bay(index) for index in range(bays)))
        elapsed = time.perf_counter() - started
    if sum(ok) != bays * commands_per_bay:
        raise RuntimeError(f"expected {bays * commands_per_bay} ok results, got {sum(ok)}")
    return {
        "bays": bays,
        "concurrency": concurrency,
        "connections": nas.connections,
        "commands": nas.commands,
        "peak_channels": nas.peak_channels,
        "wall_s": round(elapsed, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bays", nargs="+", type=int, default=[24, 60])
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 4, 12])
    parser.add_argument("--handshake-ms", type=float, default=150.0)
    parser.add_argument("--command-ms", type=float, default=40.0)
    parser.add_argument("--commands-per-bay", type=int, default=2)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = []
    for bays in args.bays:
        for concurrency in args.concurrency:
            samples = [
                asyncio.run(run_case(bays, concurrency, args.handshake_ms / 1000, args.command_ms / 1000, args.commands_per_bay))
                for _ in range(args.repeat)
            ]
            row = dict(samples[0])
            row["wall_s"] = round(statistics.median(sample["wall_s"] for sample in samples), 3)
            row["connections"] = max(sample["connections"] for sample in samples)
            rows.append(row)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print("| bays | smart_batch_max_concurrency | connections | commands | peak channels | wall s |")
        print("|---|---|---|---|---|---|")
        for row in rows:
            print(f"| {row['bays']} | {row['concurrency']} | {row['connections']} | {row['commands']} | {row['peak_channels']} | {row['wall_s']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
