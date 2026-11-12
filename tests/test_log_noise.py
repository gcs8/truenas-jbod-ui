"""Repeated advisory log lines stay quiet; real failures still log (#1171)."""

from __future__ import annotations

import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import paramiko

from tests.test_inventory import build_inventory_service
from tests.test_ssh_probe import MemorySSHWire
from tests.test_ssh_session_reuse import _exec_ok, _fake_ssh_client

from app import config as config_module
from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig, get_settings
from app.log_limiter import RepeatedLogLimiter
from app.models.domain import InventorySnapshot, SlotView
from app.services import ssh_probe
from app.services.smartctl_status import (
    smartctl_device_path,
    smartctl_exit_failed,
    smartctl_exit_flags,
)
from app.services.ssh_probe import SSHCommandResult, SSHProbe

SMART_JSON = (
    "{"
    '"device":{"type":"sat","protocol":"ATA"},'
    '"model_name":"SYNTH-DISK",'
    '"serial_number":"SYNTH-SERIAL-0001",'
    '"smart_status":{"passed":true},'
    '"temperature":{"current":31}'
    "}"
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RepeatedLogLimiterTests(unittest.TestCase):
    def test_first_line_logs_and_identical_repeats_are_counted(self) -> None:
        limiter = RepeatedLogLimiter(repeat_interval_seconds=None)
        first = limiter.admit("key", 4)
        self.assertIsNotNone(first)
        self.assertEqual(first.count, 0)
        self.assertEqual(first.suffix(), "")
        self.assertIsNone(limiter.admit("key", 4))
        self.assertIsNone(limiter.admit("key", 4))
        changed = limiter.admit("key", 64)
        self.assertIsNotNone(changed)
        self.assertEqual(changed.count, 2)
        self.assertIn("2 identical", changed.suffix())

    def test_repeats_are_summarized_after_the_interval_with_first_and_last(self) -> None:
        clock = Clock()
        limiter = RepeatedLogLimiter(repeat_interval_seconds=60, clock=clock, wall_clock=clock)
        self.assertIsNotNone(limiter.admit("key"))
        clock.now += 10
        self.assertIsNone(limiter.admit("key"))
        clock.now += 20
        self.assertIsNone(limiter.admit("key"))
        clock.now += 40
        summary = limiter.admit("key")
        self.assertIsNotNone(summary)
        self.assertEqual(summary.count, 2)
        self.assertEqual((summary.first, summary.last), (1010.0, 1030.0))
        self.assertIn("first", summary.suffix())
        self.assertIn("last", summary.suffix())
        self.assertIsNone(limiter.admit("key"))

    def test_forgotten_key_logs_again(self) -> None:
        limiter = RepeatedLogLimiter(repeat_interval_seconds=None)
        self.assertIsNotNone(limiter.admit("key", 8))
        self.assertIsNone(limiter.admit("key", 8))
        limiter.forget("key")
        limiter.forget("never-seen")
        self.assertIsNotNone(limiter.admit("key", 8))

    def test_long_remote_identifiers_are_not_retained(self) -> None:
        limiter = RepeatedLogLimiter(repeat_interval_seconds=None, max_keys=8)
        for index in range(32):
            self.assertIsNotNone(limiter.admit(("host", f"/dev/{index}" + "x" * 65536), "s" * 65536))
        self.assertEqual(len(limiter), 8)
        for key, entry in limiter._entries.items():
            self.assertLessEqual(len(key), 32)
            self.assertLessEqual(len(entry.state), 32)
        self.assertIsNone(limiter.admit(("host", "/dev/31" + "x" * 65536), "s" * 65536))

    def test_tracked_keys_stay_bounded(self) -> None:
        limiter = RepeatedLogLimiter(repeat_interval_seconds=None, max_keys=3)
        for index in range(10):
            self.assertIsNotNone(limiter.admit(index))
        self.assertEqual(len(limiter), 3)
        # The oldest key was forgotten, so it logs again rather than staying silent.
        self.assertIsNotNone(limiter.admit(0))


class SmartctlExitBitTests(unittest.TestCase):
    def test_only_command_line_and_open_bits_are_failures(self) -> None:
        for exit_code in (1, 2, 3, 5, 66, 127, 255, None, -1, 256):
            with self.subTest(exit_code=exit_code):
                self.assertTrue(smartctl_exit_failed(exit_code))
        for exit_code in (0, 4, 8, 16, 32, 64, 128, 4 | 64, 252):
            with self.subTest(exit_code=exit_code):
                self.assertFalse(smartctl_exit_failed(exit_code))

    def test_health_bits_are_named(self) -> None:
        self.assertEqual(smartctl_exit_flags(0), [])
        self.assertEqual(smartctl_exit_flags(4), ["smart_command_failed"])
        self.assertEqual(
            smartctl_exit_flags(8 | 16 | 32 | 64 | 128),
            [
                "smart_status_failing",
                "prefail_attributes_at_threshold",
                "attributes_past_threshold",
                "error_log_has_errors",
                "self_test_log_has_errors",
            ],
        )

    def test_device_path_only_for_smartctl_commands(self) -> None:
        self.assertEqual(smartctl_device_path("sudo -n /usr/sbin/smartctl -x -j /dev/sda"), "/dev/sda")
        self.assertEqual(
            smartctl_device_path("sudo -n /usr/local/sbin/smartctl -d scsi -x /dev/da3"), "/dev/da3"
        )
        self.assertEqual(smartctl_device_path("smartctl -a /dev/nvme0"), "/dev/nvme0")
        self.assertIsNone(smartctl_device_path("sudo -n /usr/bin/sg_ses --join /dev/sg1"))
        self.assertIsNone(smartctl_device_path("echo smartctl /dev/sda"))
        self.assertIsNone(smartctl_device_path("smartctl --scan"))
        self.assertIsNone(smartctl_device_path("unbalanced 'quote"))


def _reset_ssh_log_limiters() -> None:
    for limiter in getattr(ssh_probe, "SSH_LOG_LIMITERS", ()):
        limiter.reset()


class SSHCommandExitLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_ssh_log_limiters()
        self.addCleanup(_reset_ssh_log_limiters)
        self.probe = SSHProbe(SSHConfig(enabled=True, host="synthetic.example.test"))

    def run_with_status(self, command: str, status: int) -> SSHCommandResult:
        wire = MemorySSHWire(lambda channel, _command: MemorySSHWire.feed(
            channel, SMART_JSON.encode(), b"", eof=True, status=status,
        ))
        try:
            return self.probe._run_single_command(wire.client, command, timeout_seconds=2)
        finally:
            wire.close()

    def test_smartctl_status_bits_are_not_logged_as_command_failures(self) -> None:
        command = "sudo -n /usr/sbin/smartctl -x -j /dev/sda"
        with self.assertLogs("app.services.ssh_probe", level="DEBUG") as logs:
            result = self.run_with_status(command, 4)
        self.assertEqual(result.exit_code, 4)
        self.assertEqual(result.stdout, SMART_JSON)
        self.assertFalse(any("SSH command failed" in line for line in logs.output), logs.output)
        status_lines = [line for line in logs.output if "smartctl reported" in line]
        self.assertEqual(len(status_lines), 1, logs.output)
        self.assertTrue(status_lines[0].startswith("INFO:"), status_lines)
        self.assertIn("/dev/sda", status_lines[0])
        self.assertIn("exit=4", status_lines[0])
        self.assertIn("smart_command_failed", status_lines[0])

    def test_smartctl_status_logs_once_per_device_until_it_changes(self) -> None:
        json_command = "sudo -n /usr/sbin/smartctl -x -j /dev/sda"
        text_command = "sudo -n /usr/sbin/smartctl -x /dev/sda"
        with self.assertLogs("app.services.ssh_probe", level="INFO") as logs:
            for _poll in range(5):
                self.run_with_status(json_command, 4)
                self.run_with_status(text_command, 4)
            self.run_with_status(json_command, 4 | 64)
            self.run_with_status("sudo -n /usr/sbin/smartctl -x -j /dev/sdb", 4)
        status_lines = [line for line in logs.output if "smartctl reported" in line]
        self.assertEqual(len(status_lines), 3, logs.output)
        self.assertIn("/dev/sda", status_lines[0])
        self.assertIn("error_log_has_errors", status_lines[1])
        self.assertIn("9 identical", status_lines[1])
        self.assertIn("/dev/sdb", status_lines[2])

    def test_failing_disk_status_is_a_warning(self) -> None:
        with self.assertLogs("app.services.ssh_probe", level="WARNING") as logs:
            self.run_with_status("sudo -n /usr/sbin/smartctl -x -j /dev/sdc", 8)
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn("smart_status_failing", logs.output[0])
        self.assertNotIn("SSH command failed", logs.output[0])

    def test_a_clean_read_resets_the_device_so_a_new_failure_logs(self) -> None:
        command = "sudo -n /usr/sbin/smartctl -x -j /dev/sdd"
        with self.assertLogs("app.services.ssh_probe", level="WARNING") as logs:
            self.run_with_status(command, 8)
            self.run_with_status(command, 8)
            self.run_with_status(command, 0)
            self.run_with_status(command, 8)
            self.run_with_status("sudo -n /usr/bin/sg_ses --join /dev/sg2", 1)
            self.run_with_status("sudo -n /usr/bin/sg_ses --join /dev/sg2", 0)
            self.run_with_status("sudo -n /usr/bin/sg_ses --join /dev/sg2", 1)
        self.assertEqual(
            [("smart_status_failing" in line, "SSH command failed" in line) for line in logs.output],
            [(True, False), (True, False), (False, True), (False, True)],
            logs.output,
        )

    def test_smartctl_command_line_and_open_failures_still_log(self) -> None:
        for status in (1, 2, 3 | 64):
            with self.subTest(status=status), self.assertLogs("app.services.ssh_probe", level="WARNING") as logs:
                self.run_with_status(f"sudo -n /usr/sbin/smartctl -x -j /dev/sd{status}", status)
            self.assertEqual(len(logs.output), 1, logs.output)
            self.assertIn("SSH command failed", logs.output[0])
            self.assertIn(f"exit={status}", logs.output[0])

    def test_identical_command_failures_are_rate_limited_with_a_count(self) -> None:
        clock = Clock()
        interval = getattr(ssh_probe, "SSH_COMMAND_FAILURE_REPEAT_SECONDS", 900.0)
        limiter = RepeatedLogLimiter(repeat_interval_seconds=interval, clock=clock, wall_clock=clock)
        command = "sudo -n /usr/bin/sg_ses --join --filter /dev/sg1"
        with (
            patch.object(ssh_probe, "SSH_COMMAND_FAILURE_LOG", limiter, create=True),
            self.assertLogs("app.services.ssh_probe", level="WARNING") as logs,
        ):
            for _poll in range(4):
                self.run_with_status(command, 1)
            self.run_with_status(command, 5)
            clock.now += interval + 1
            self.run_with_status(command, 5)
        self.assertEqual(len(logs.output), 3, logs.output)
        self.assertIn("exit=1", logs.output[0])
        self.assertIn("exit=5", logs.output[1])
        self.assertIn("3 identical", logs.output[1])
        self.assertIn("exit=5", logs.output[2])


class ChannelRefusalLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_ssh_log_limiters()
        self.addCleanup(_reset_ssh_log_limiters)

    @staticmethod
    def limited_client(server_limit: int) -> MagicMock:
        lock = threading.RLock()
        state = {"open": 0}
        ok_exec = _exec_ok(delay=0.01)

        def exec_command(command: str, timeout=None):
            with lock:
                if state["open"] >= server_limit:
                    raise paramiko.ChannelException(2, "Connect failed")
                state["open"] += 1
            streams = ok_exec(command, timeout)
            channel = streams[1].channel
            close = channel.close

            def close_counted():
                with lock:
                    was_closed = channel.closed
                    close()
                    if not was_closed and channel.closed:
                        state["open"] -= 1

            channel.close = close_counted
            return streams

        client = _fake_ssh_client()
        client.exec_command.side_effect = exec_command
        return client

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_channel_limit_logs_once_per_change_not_per_refusal(self, ssh_client_cls: MagicMock) -> None:
        ssh_client_cls.side_effect = lambda: self.limited_client(3)
        probe = SSHProbe(SSHConfig(enabled=True, host="synthetic.example.test", known_hosts_path=""))
        groups = [((lambda _results: []), [f"smartctl -j -a /dev/da{bay}"]) for bay in range(12)]
        with self.assertLogs("app.services.ssh_probe", level="INFO") as logs:
            for _refresh in range(3):
                outcomes = probe._run_planned_command_groups_sync(groups)
                self.assertTrue(all(result.ok for outcome in outcomes for result in outcome))
        refusal_lines = [line for line in logs.output if "refused" in line]
        self.assertEqual(len(refusal_lines), 1, logs.output)
        self.assertIn("at most 3", refusal_lines[0])

    @patch("app.services.ssh_probe.paramiko.SSHClient")
    def test_a_final_channel_refusal_still_logs_a_warning(self, ssh_client_cls: MagicMock) -> None:
        client = _fake_ssh_client()
        client.exec_command.side_effect = paramiko.ChannelException(2, "Connect failed")
        ssh_client_cls.return_value = client
        probe = SSHProbe(SSHConfig(enabled=True, host="synthetic.example.test", known_hosts_path=""))
        with self.assertLogs("app.services.ssh_probe", level="WARNING") as logs:
            outcomes = probe._run_planned_command_groups_sync([((lambda _results: []), ["a"])])
        self.assertEqual([[result.ok for result in outcome] for outcome in outcomes], [[False]])
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn("could not open an SSH channel", logs.output[0])
        self.assertIn("Connect failed", logs.output[0])

    def test_paramiko_per_channel_open_error_is_demoted(self) -> None:
        transport_logger = logging.getLogger("paramiko.transport")
        with self.assertNoLogs("paramiko.transport", level="INFO"):
            transport_logger.error("Secsh channel 7 open FAILED: open failed: Connect failed")
        with self.assertLogs("paramiko.transport", level="DEBUG") as logs:
            transport_logger.error("Secsh channel 8 open FAILED: open failed: Connect failed")
        self.assertEqual(logs.records[0].levelno, logging.DEBUG)
        # Other paramiko errors keep their level.
        with self.assertLogs("paramiko.transport", level="ERROR") as logs:
            transport_logger.error("Socket exception: synthetic")
        self.assertEqual(logs.records[0].levelno, logging.ERROR)


class LegacyConfigWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)

    def test_legacy_key_warnings_log_once_per_config_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config" / "config.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("app:\n  host: 0.0.0.0\n  port: 8080\n  verify_ssl: true\n", encoding="utf-8")
            with patch.dict("os.environ", {"APP_CONFIG_PATH": config_path.as_posix()}, clear=True):
                with self.assertLogs("app.config", level="WARNING") as logs:
                    for _reload in range(5):
                        get_settings.cache_clear()
                        get_settings()
                self.assertEqual(len(logs.output), 3, logs.output)
                self.assertTrue(any("app.verify_ssl" in line for line in logs.output))

                # A changed file is a new generation and warns again.
                config_path.write_text("app:\n  verify_ssl: false\n", encoding="utf-8")
                with self.assertLogs("app.config", level="WARNING") as logs:
                    for _reload in range(3):
                        get_settings.cache_clear()
                        get_settings()
                self.assertEqual(
                    logs.output, ["WARNING:app.config:config.yaml: unknown key `app.verify_ssl` is ignored."]
                )
                # The admin banner is unaffected by log de-duplication.
                warnings = config_module.build_unknown_config_key_warnings(get_settings())
                self.assertEqual([warning["key"] for warning in warnings], ["app.verify_ssl"])

    def test_config_generation_accepts_mixed_key_types(self) -> None:
        mixed = {1: "a", "app": {"verify_ssl": True}}
        self.assertEqual(config_module._config_generation(mixed), config_module._config_generation(dict(mixed)))
        self.assertNotEqual(config_module._config_generation(mixed), config_module._config_generation({1: "b"}))


class SmartResultExitStatusTests(unittest.IsolatedAsyncioTestCase):
    async def summary_for_exit(self, exit_code: int):
        class DummyTrueNASClient:
            async def fetch_disk_smartctl(self, *_args, **_kwargs):
                raise AssertionError("This test uses the SSH smartctl path.")

        class DummySSHProbe:
            async def run_command(self, command: str) -> SSHCommandResult:
                if " -j " in command:
                    return SSHCommandResult(command=command, ok=False, stdout=SMART_JSON, exit_code=exit_code)
                return SSHCommandResult(command=command, ok=False, stdout="", exit_code=exit_code)

        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="synthetic",
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(Settings(), system, DummyTrueNASClient(), DummySSHProbe(), temp_dir)
            slot = SlotView(
                slot=0,
                slot_label="Bay 0",
                row_index=0,
                column_index=0,
                device_name="sda",
                smart_device_names=["sda"],
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(slots=[slot], refresh_interval_seconds=30)
            )
            return await service.get_slot_smart_summary(0)

    async def test_smart_status_bits_stay_visible_on_the_smart_result(self) -> None:
        summary = await self.summary_for_exit(4 | 8 | 64)
        self.assertTrue(summary.available)
        self.assertEqual(summary.smartctl_exit_status, 4 | 8 | 64)
        self.assertEqual(
            summary.smartctl_status_flags,
            ["smart_command_failed", "smart_status_failing", "error_log_has_errors"],
        )

    async def test_clean_exit_records_no_status_flags(self) -> None:
        summary = await self.summary_for_exit(0)
        self.assertTrue(summary.available)
        self.assertEqual(summary.smartctl_exit_status, 0)
        self.assertEqual(summary.smartctl_status_flags, [])


if __name__ == "__main__":
    unittest.main()
