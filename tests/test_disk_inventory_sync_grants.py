"""Sudo grant contract for the TrueNAS disk inventory sync action (issue #357).

The expected command set is derived by running ``InventoryService.sync_disk_inventory``
against a recording SSH runner, so a new sudo-run midclt shape fails here until the
bootstrap grants it. The matching helpers mirror the per-argument matching sudoers
applies to a ``Cmnd_Spec``. PR #333 introduces the same helpers for the Linux probes;
fold the two copies together once both have merged.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from admin_service.services.account_bootstrap import (
    CORE_MIDCLT_DISK_SYNC_SUDO_COMMANDS,
    SCALE_MIDCLT_DISK_SYNC_SUDO_COMMANDS,
    SUDO_COMMANDS_BY_PLATFORM,
    ServiceAccountBootstrapService,
)
from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import DiskInventorySyncMode
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.ssh_probe import SSHCommandResult


def sudoers_grant_matches(grant: str, command: str) -> bool:
    grant_tokens = shlex.split(grant)
    command_tokens = shlex.split(command)
    if not grant_tokens or not command_tokens or grant_tokens[0] != command_tokens[0]:
        return False
    grant_arguments = grant.split(maxsplit=1)[1] if len(grant_tokens) > 1 else ""
    command_arguments = " ".join(command_tokens[1:])
    if grant_arguments.startswith("^") and grant_arguments.endswith("$"):
        python_pattern = grant_arguments.replace("[[]", r"\[").replace("[]]", r"\]")
        return re.fullmatch(python_pattern, command_arguments) is not None
    return fnmatch.fnmatchcase(command_arguments, grant_arguments)


def strip_sudo_prefix(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] != "sudo":
        return None
    remainder = tokens[1:]
    while remainder and remainder[0].startswith("-"):
        remainder.pop(0)
    return shlex.join(remainder) if remainder else None


class DiskInventorySyncSudoGrantContractTests(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    def build_service(self, temp_dir: str, platform: str) -> InventoryService:
        system = SystemConfig(
            id=f"{platform}-sync-contract",
            label="Synthetic TrueNAS",
            truenas=TrueNASConfig(platform=platform),
            ssh=SSHConfig(enabled=True, host="truenas.invalid", user="jbodmap", commands=[]),
        )
        settings = Settings()
        return InventoryService(
            settings,
            system,
            AsyncMock(),
            AsyncMock(),
            None,
            MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
            ProfileRegistry(settings),
            SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
        )

    async def collect_sudo_commands(self, platform: str, modes: list[DiskInventorySyncMode]) -> set[str]:
        recorded: list[str] = []
        job_id = 268071

        async def record(
            command: str,
            *_args: Any,
            **_kwargs: Any,
        ) -> SSHCommandResult:
            recorded.append(command)
            stdout = ""
            if command.endswith("call disk.sync_all"):
                stdout = f"{job_id}\n"
            elif "call core.get_jobs" in command:
                stdout = json.dumps([{"id": job_id, "state": "SUCCESS", "error": None}])
            elif command.endswith("call disk.multipath_sync"):
                stdout = "null\n"
            return SSHCommandResult(command=command, ok=True, stdout=stdout, stderr="", exit_code=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.build_service(temp_dir, platform)
            service._run_ssh_command = record
            for mode in modes:
                await service.sync_disk_inventory(mode)

        sudo_commands = {
            stripped
            for command in recorded
            if (stripped := strip_sudo_prefix(command)) is not None
        }
        self.assertTrue(sudo_commands, f"no sudo-run {platform} sync commands were captured")
        return sudo_commands

    def assert_grants_cover(self, grants: tuple[str, ...], commands: set[str], label: str) -> None:
        ungranted = sorted(
            command
            for command in commands
            if not any(sudoers_grant_matches(grant, command) for grant in grants)
        )
        self.assertEqual(ungranted, [], f"{label} sudo grants do not cover: " + ", ".join(ungranted))

    async def test_core_grants_cover_both_sync_modes(self) -> None:
        commands = await self.collect_sudo_commands(
            "core", [DiskInventorySyncMode.multipath, DiskInventorySyncMode.full]
        )
        self.assertEqual(
            commands,
            {
                "/usr/local/bin/midclt call disk.multipath_sync",
                "/usr/local/bin/midclt call disk.sync_all",
                "/usr/local/bin/midclt call core.get_jobs '[[\"id\",\"=\",268071]]'",
            },
        )
        self.assert_grants_cover(SUDO_COMMANDS_BY_PLATFORM["core"], commands, "CORE bootstrap")
        self.assert_grants_cover(
            ServiceAccountBootstrapService._resolve_sudo_commands("core", ["sudo -n /usr/sbin/sesutil map"]),
            commands,
            "CORE supplemental",
        )

    async def test_scale_grants_cover_the_full_sync_only(self) -> None:
        commands = await self.collect_sudo_commands("scale", [DiskInventorySyncMode.full])
        self.assertEqual(
            commands,
            {
                "/usr/bin/midclt call disk.sync_all",
                "/usr/bin/midclt call core.get_jobs '[[\"id\",\"=\",268071]]'",
            },
        )
        self.assert_grants_cover(SUDO_COMMANDS_BY_PLATFORM["scale"], commands, "SCALE bootstrap")
        self.assert_grants_cover(
            ServiceAccountBootstrapService._resolve_sudo_commands(
                "scale", ["sudo -n /usr/bin/sg_ses -p aes /dev/sg3"]
            ),
            commands,
            "SCALE supplemental",
        )
        # SCALE must not be handed the FreeBSD-only multipath call or the CORE midclt path.
        self.assertFalse(any("multipath_sync" in grant for grant in SUDO_COMMANDS_BY_PLATFORM["scale"]))
        self.assertFalse(any(grant.startswith("/usr/local/bin/midclt") for grant in SUDO_COMMANDS_BY_PLATFORM["scale"]))

    def test_grants_are_exact_argument_entries(self) -> None:
        for grant in (*CORE_MIDCLT_DISK_SYNC_SUDO_COMMANDS, *SCALE_MIDCLT_DISK_SYNC_SUDO_COMMANDS):
            tokens = shlex.split(grant)
            if tokens[1].startswith("^call"):
                self.assertIn('^call core[.]get_jobs [[][[]"id","=",[0-9]+[]][]]', grant)
                self.assertTrue(grant.endswith("$"), grant)
                self.assertNotIn(" *", grant)
                self.assertNotIn("\\", grant, "TrueNAS doubles backslashes when rendering sudoers")
            else:
                self.assertEqual(tokens[1], "call", grant)
                self.assertIn(tokens[2], {"disk.multipath_sync", "disk.sync_all"}, grant)
                self.assertEqual(len(tokens), 3, f"{grant} must not accept extra arguments")
        # A wildcard that would admit other midclt methods must never appear.
        for grants in SUDO_COMMANDS_BY_PLATFORM.values():
            for grant in grants:
                if "midclt" in grant:
                    self.assertNotIn("call *", grant)
                    self.assertFalse(grant.endswith("midclt *"), grant)

    def test_job_poll_grants_reject_arbitrary_filters_and_trailing_arguments(self) -> None:
        for grants, midclt in (
            (CORE_MIDCLT_DISK_SYNC_SUDO_COMMANDS, "/usr/local/bin/midclt"),
            (SCALE_MIDCLT_DISK_SYNC_SUDO_COMMANDS, "/usr/bin/midclt"),
        ):
            with self.subTest(midclt=midclt):
                expected = f'{midclt} call core.get_jobs \'[["id","=",268071]]\''
                arbitrary_filter = f'{midclt} call core.get_jobs \'[["method","=","pool.export"]]\''
                trailing_argument = expected + " synthetic-extra-argument"
                self.assertTrue(any(sudoers_grant_matches(grant, expected) for grant in grants))
                self.assertFalse(any(sudoers_grant_matches(grant, arbitrary_filter) for grant in grants))
                self.assertFalse(any(sudoers_grant_matches(grant, trailing_argument) for grant in grants))

    def test_sudoers_files_render_the_midclt_grants(self) -> None:
        core = ServiceAccountBootstrapService._build_sudoers_content("jbodmap", "core")
        for grant in CORE_MIDCLT_DISK_SYNC_SUDO_COMMANDS:
            self.assertIn(grant, core)
        scale = ServiceAccountBootstrapService._build_sudoers_content("jbodmap", "scale")
        for grant in SCALE_MIDCLT_DISK_SYNC_SUDO_COMMANDS:
            self.assertIn(grant, scale)
        self.assertNotIn("/usr/local/bin/midclt", scale)


if __name__ == "__main__":
    unittest.main()
