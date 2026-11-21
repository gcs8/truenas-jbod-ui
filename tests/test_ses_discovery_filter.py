"""SES discovery skips SG devices whose identity proves they are not enclosures (#1314).

Discovery used to run one elevated ``sg_ses -p aes`` read for every SCSI
generic node, disks included. The discovery script now reads each node's
unprivileged sysfs identity first and skips a node only when the identity is
complete and proves it is a storage device without enclosure services. A node
whose type or INQUIRY data is missing, unreadable or short is still read. The
script reports how many nodes it skipped, and discovery surfaces that count.

The shell tests run the shipped script with ``/bin/sh`` against a synthetic
device and sysfs tree and a stub in place of ``sudo -n /usr/bin/sg_ses``. No
appliance is contacted.
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.ssh_probe import SSHCommandResult

ELEVATED_READ = "sudo -n /usr/bin/sg_ses"
SYSFS_ROOT = "/sys/class/scsi_generic"

DISK, TAPE, OPTICAL, ENCLOSURE, PROCESSOR = "0", "1", "5", "13", "3"
NO_ENCSERV = bytes([0, 0, 6, 2, 31, 0, 0x00, 0])
WITH_ENCSERV = bytes([0, 0, 6, 2, 31, 0, 0x40, 0])
TRUNCATED = bytes([0, 0, 6, 2])
MISSING = object()


def _service(directory: str) -> InventoryService:
    settings = Settings()
    system = SystemConfig(
        id="synthetic-scale",
        truenas=TrueNASConfig(platform="scale"),
        ssh=SSHConfig(enabled=True, host="node.example.test", user="synthetic-operator", commands=[]),
    )
    return InventoryService(
        settings,
        system,
        AsyncMock(),
        AsyncMock(),
        None,
        MappingStore(str(Path(directory) / "slot_mappings.json")),
        ProfileRegistry(settings),
        SlotDetailStore(str(Path(directory) / "slot_detail_cache.json")),
    )


@unittest.skipUnless(os.path.exists("/bin/sh"), "needs a POSIX shell")
class DiscoveryScriptFilterTests(unittest.TestCase):
    """Run the shipped discovery script against a synthetic host."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.directory = str(self.root / "state")
        os.mkdir(self.directory)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _run(self, nodes: dict[int, tuple[object, object]], enclosures: set[int]) -> tuple[list[str], list[str]]:
        """Return (elevated reads in order, stdout lines) for the synthetic host.

        ``nodes`` maps an SG index to (type text or MISSING, INQUIRY bytes or MISSING).
        The stub answers like an enclosure for the indexes in ``enclosures``.
        """

        dev = self.root / "dev"
        sysfs = self.root / "sys"
        dev.mkdir()
        sysfs.mkdir()
        for index, (type_text, inquiry) in nodes.items():
            (dev / f"sg{index}").write_text("")
            device = sysfs / f"sg{index}" / "device"
            device.mkdir(parents=True)
            if type_text is not MISSING:
                (device / "type").write_text(str(type_text))
            if isinstance(inquiry, bytes):
                (device / "inquiry").write_bytes(inquiry)
        calls = self.root / "calls.log"
        stub = self.root / "sg_ses_stub"
        accepted = " ".join(str(dev / f"sg{index}") for index in sorted(enclosures))
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n'
            f'case {shlex.quote(" " + accepted + " ")} in *" $3 "*) exit 0 ;; esac\n'
            "exit 1\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

        command = _service(self.directory)._build_sg_ses_discovery_command()
        argv = shlex.split(command)
        self.assertEqual(argv[:2], ["/bin/sh", "-lc"])
        script = argv[2]
        self.assertIn("/dev/sg*", script)
        self.assertIn(ELEVATED_READ, script)
        script = script.replace(ELEVATED_READ, shlex.quote(str(stub)))
        script = script.replace("/dev/sg", f"{dev}/sg")
        script = script.replace(SYSFS_ROOT, str(sysfs))
        self.assertNotIn("/sys/class", script)
        completed = subprocess.run(
            ["/bin/sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        reads = calls.read_text().splitlines() if calls.exists() else []
        read_devices = [line.split()[-1].rsplit("/", 1)[-1] for line in reads]
        for line in reads:
            self.assertEqual(line.split()[:2], ["-p", "aes"])
        return read_devices, completed.stdout.splitlines()

    def test_known_non_enclosures_get_no_elevated_read(self) -> None:
        reads, stdout = self._run(
            {
                0: (DISK, NO_ENCSERV),
                1: (ENCLOSURE, NO_ENCSERV),
                2: (TAPE, NO_ENCSERV),
                3: (OPTICAL, NO_ENCSERV),
                4: (DISK, NO_ENCSERV),
                5: (ENCLOSURE, NO_ENCSERV),
            },
            enclosures={1, 5},
        )
        self.assertEqual(reads, ["sg1", "sg5"])
        self.assertEqual(
            [line for line in stdout if line.startswith(str(self.root))],
            [f"{self.root}/dev/sg1", f"{self.root}/dev/sg5"],
        )
        self.assertIn("skipped-not-enclosure 4", stdout)

    def test_unknown_or_incomplete_identity_is_still_read(self) -> None:
        cases = {
            "type_missing": (MISSING, NO_ENCSERV),
            "type_empty": ("", NO_ENCSERV),
            "type_garbage": ("0x", NO_ENCSERV),
            "type_unlisted": (PROCESSOR, NO_ENCSERV),
            "inquiry_missing": (DISK, MISSING),
            "inquiry_truncated": (DISK, TRUNCATED),
            "inquiry_empty": (DISK, b""),
            "disk_with_enclosure_services": (DISK, WITH_ENCSERV),
            "enclosure": (ENCLOSURE, MISSING),
        }
        for name, identity in cases.items():
            with self.subTest(case=name):
                self.tearDown()
                self.setUp()
                reads, stdout = self._run({7: identity}, enclosures={7})
                self.assertEqual(reads, ["sg7"])
                self.assertIn(f"{self.root}/dev/sg7", stdout)
                self.assertIn("skipped-not-enclosure 0", stdout)

    def test_mixed_list_keeps_order_and_counts(self) -> None:
        nodes = {
            0: (DISK, NO_ENCSERV),
            1: (ENCLOSURE, NO_ENCSERV),
            2: (DISK, TRUNCATED),
            3: (MISSING, MISSING),
            10: (TAPE, NO_ENCSERV),
            11: (DISK, WITH_ENCSERV),
            12: (OPTICAL, NO_ENCSERV),
            20: (ENCLOSURE, NO_ENCSERV),
        }
        reads, stdout = self._run(nodes, enclosures={1, 20})
        # The shell glob order, less the three proven non-enclosures.
        self.assertEqual(reads, ["sg1", "sg11", "sg2", "sg20", "sg3"])
        self.assertEqual(
            [line for line in stdout if line.startswith(str(self.root))],
            [f"{self.root}/dev/sg1", f"{self.root}/dev/sg20"],
        )
        self.assertEqual(stdout[-1], "skipped-not-enclosure 3")

    def test_all_enclosures_control(self) -> None:
        reads, stdout = self._run({0: (ENCLOSURE, NO_ENCSERV), 1: (ENCLOSURE, NO_ENCSERV)}, enclosures={0, 1})
        self.assertEqual(reads, ["sg0", "sg1"])
        self.assertEqual(stdout[-1], "skipped-not-enclosure 0")


class DiscoverySkipCountTests(unittest.IsolatedAsyncioTestCase):
    """The skipped count is reported, not mistaken for a stray line, and never admitted as a device."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.directory = self._temp.name

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _discovery_answer(self, service: InventoryService, stdout: str, planned: list[str]) -> None:
        async def run_planned(planner, *, initial_commands, host):
            del host
            discovery = SSHCommandResult(command=initial_commands[0], ok=True, stdout=stdout, exit_code=0)
            commands = planner([discovery])
            planned.extend(commands)
            return [
                discovery,
                *(SSHCommandResult(command=command, ok=False, stderr="denied", exit_code=1) for command in commands),
            ]

        service._run_ssh_planned_commands = run_planned

    async def test_skip_count_is_logged_and_not_counted_as_a_stray_line(self) -> None:
        service = _service(self.directory)
        planned: list[str] = []
        self._discovery_answer(service, "/dev/sg1\n/dev/sg5\nskipped-not-enclosure 4\n", planned)
        with self.assertLogs("app.services.inventory", level="INFO") as logs:
            devices, _overlay, _failures = await service._discover_and_fetch_sg_ses_host_overlay(
                "node.example.test", failure_prefix="TrueNAS SCALE SSH SES"
            )
        self.assertEqual(devices, ["/dev/sg1", "/dev/sg5"])
        self.assertEqual(
            sorted({shlex.split(command)[-1] for command in planned if "sg_ses" in command}),
            ["/dev/sg1", "/dev/sg5"],
        )
        skipped = [line for line in logs.output if "skipped 4 SG devices" in line]
        self.assertEqual(len(skipped), 1, logs.output)
        self.assertTrue(skipped[0].startswith("INFO:"), skipped[0])
        self.assertFalse([line for line in logs.output if "ignored" in line], logs.output)

    async def test_all_skipped_reports_the_count_with_the_empty_discovery(self) -> None:
        service = _service(self.directory)
        self._discovery_answer(service, "skipped-not-enclosure 12\n", [])
        with self.assertLogs("app.services.inventory", level=logging.INFO):
            devices, _overlay, failures = await service._discover_and_fetch_sg_ses_host_overlay(
                "node.example.test", failure_prefix="TrueNAS SCALE SSH SES"
            )
        self.assertEqual(devices, [])
        self.assertEqual(
            failures,
            [
                "TrueNAS SCALE SSH SES discovery found no usable sg_ses devices on node.example.test "
                "(12 SG devices skipped as not enclosures)."
            ],
        )

    async def test_no_skip_line_keeps_the_existing_messages(self) -> None:
        service = _service(self.directory)
        self._discovery_answer(service, "", [])
        devices, _overlay, failures = await service._discover_and_fetch_sg_ses_host_overlay(
            "node.example.test", failure_prefix="TrueNAS SCALE SSH SES"
        )
        self.assertEqual(devices, [])
        self.assertEqual(
            failures, ["TrueNAS SCALE SSH SES discovery found no usable sg_ses devices on node.example.test."]
        )

    async def test_malformed_skip_line_is_a_stray_line_not_a_count(self) -> None:
        service = _service(self.directory)
        self._discovery_answer(service, "/dev/sg1\nskipped-not-enclosure 04\nskipped-not-enclosure x\n", [])
        with self.assertLogs("app.services.inventory", level="WARNING") as logs:
            devices, _overlay, _failures = await service._discover_and_fetch_sg_ses_host_overlay(
                "node.example.test", failure_prefix="TrueNAS SCALE SSH SES"
            )
        self.assertEqual(devices, ["/dev/sg1"])
        self.assertTrue([line for line in logs.output if "ignored 2 lines" in line], logs.output)
        self.assertFalse([line for line in logs.output if "skipped" in line and "SG devices" in line], logs.output)


if __name__ == "__main__":
    unittest.main()
