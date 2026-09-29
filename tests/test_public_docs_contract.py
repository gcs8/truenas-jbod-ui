from __future__ import annotations

import ast
import builtins
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

import yaml

from scripts.check_public_docs import same_repository_main_path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_WIKI_PAGES = {
    "wiki/Admin-UI-and-System-Setup.md",
    "wiki/Advanced-Configuration.md",
    "wiki/Architecture-and-Services.md",
    "wiki/Backup-Restore-and-Debug-Bundles.md",
    "wiki/Demo-and-Offline-Workflows.md",
    "wiki/Docker-and-GHCR-Deployment.md",
    "wiki/Generic-Linux-Setup.md",
    "wiki/Heat-Map-Mode.md",
    "wiki/History-Maintenance-and-Recovery.md",
    "wiki/History-and-Snapshot-Export.md",
    "wiki/Home.md",
    "wiki/Live-Enclosures-and-Storage-Views.md",
    "wiki/Operations-Logging-and-Metrics.md",
    "wiki/Profiles-and-Custom-Layouts.md",
    "wiki/Public-Demo-Site.md",

    "wiki/Quantastor-Setup.md",
    "wiki/Quick-Start.md",
    "wiki/SSH-Setup-and-Sudo.md",
    "wiki/Storage-Fabric.md",
    "wiki/Troubleshooting.md",
    "wiki/Upgrading.md",
    "wiki/TrueNAS-CORE-Setup.md",
    "wiki/TrueNAS-SCALE-Setup.md",
    "wiki/Visual-Tour.md",
    "wiki/_Sidebar.md",
}
EXPECTED_HISTORICAL_READ_UI_CONTEXTS = {
    "docs/ESXI_PLATFORM_FEASIBILITY.md": (
        r"into\s+the\s+ignored\s+local\s+config,\s+restarted\s+the\s+read\s+UI,\s+and\s+confirmed:",
    ),
    "docs/M2_CARRIER_RENDERING_NOTES.md": (
        r"layouts\s+in\s+the\s+main\s+read\s+UI\s+and\s+admin\s+preview\s+flow,\s+so\s+we\s+can\s+reuse\s+the\s+same",
        r"The\s+read\s+UI\s+now\s+uses\s+a\s+real\s+board\s+image\s+instead\s+of\s+a\s+CSS-only\s+abstract",
    ),
    "docs/PRIVATE_QA_RESTORE.md": (
        r"The\s+read\s+UI\s+has\s+two\s+saved\s+operator\s+edits:",
    ),
}
STALE_ADMIN_SIDECAR_PATTERN = re.compile(r"\badmin\s+sidecars?\b", re.IGNORECASE)
STALE_READ_UI_PATTERN = re.compile(r"\bread\s+UI(?:s)?\b", re.IGNORECASE)


def run_checker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/check_public_docs.py", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def rotation_staging_code() -> str:
    """Extract the actual heredoc, never a rewritten staging implementation."""
    guide = (ROOT / "docs/SEGMENTED_HISTORY_V2.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", guide, re.DOTALL)
    blocks = [block for block in blocks if "STAGE_NAME =" in block]
    if len(blocks) != 1:
        raise AssertionError("expected one literal staging recipe")
    return blocks[0].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


class SyntheticStagingOS:
    """Real descriptor I/O; ownership labels only, NOT cross-UID access proof.

    The literal docs import this narrow os replacement. Every descriptor and
    path is confined to the fixture; fchown records labels without a host chown.
    """

    def __init__(self, root: Path, identities: dict[str, str], owners: dict,
                 *, change_status: bool = False, change_source: bool = False) -> None:
        self.root = root
        self.environ = identities
        self.owners = owners
        self.descriptors: set[int] = set()
        self.change_status = change_status
        self.status_opens = 0
        self.change_source = change_source
        self.source_seeks = 0
        self.source_path = None
        for name in ("O_RDONLY", "O_WRONLY", "O_CREAT", "O_EXCL", "O_NOFOLLOW",
                     "O_CLOEXEC", "O_DIRECTORY", "SEEK_SET"):
            setattr(self, name, getattr(os, name))

    def _fd(self, descriptor):
        if descriptor not in self.descriptors:
            raise AssertionError("descriptor escaped synthetic root")
        return descriptor

    def open(self, name, flags, mode=0o777, *, dir_fd=None):
        if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name:
            raise AssertionError("path escaped synthetic root")
        if dir_fd is None:
            if name not in {"backup-status", "backups", "history"}:
                raise AssertionError("unexpected staging root")
            path = self.root / name
        else:
            self._fd(dir_fd)
            path = name
        if name == "scheduled-backup.json":
            self.status_opens += 1
            if self.change_status and self.status_opens == 2:
                with (self.root / "backup-status" / name).open("ab") as stream:
                    stream.write(b" ")
        descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
        self.descriptors.add(descriptor)
        if name.startswith("jbod-scheduled-backup-") and not flags & os.O_CREAT:
            self.source_path = self.root / "backups/scheduled" / name
        return descriptor

    def fstat(self, descriptor):
        metadata = os.fstat(self._fd(descriptor))
        fields = {name: getattr(metadata, name) for name in dir(metadata) if name.startswith("st_")}
        fields["st_uid"], fields["st_gid"] = self.owners.get(
            (metadata.st_dev, metadata.st_ino), (metadata.st_uid, metadata.st_gid)
        )
        return SimpleNamespace(**fields)

    def fchown(self, descriptor, uid, gid):
        metadata = os.fstat(self._fd(descriptor))
        self.owners[metadata.st_dev, metadata.st_ino] = (uid, gid)

    def close(self, descriptor):
        os.close(self._fd(descriptor))
        self.descriptors.remove(descriptor)

    def lseek(self, descriptor, offset, whence):
        self._fd(descriptor)
        self.source_seeks += 1
        if self.change_source and self.source_seeks == 2:
            assert self.source_path is not None
            self.source_path.write_bytes(b"changed synthetic archive")
        return os.lseek(descriptor, offset, whence)

    def __getattr__(self, name):
        if name in {"read", "write", "fsync", "fchmod", "listdir"}:
            def descriptor_call(descriptor, *args):
                return getattr(os, name)(self._fd(descriptor), *args)
            return descriptor_call
        if name in {"mkdir", "rmdir", "unlink"}:
            def relative_call(path, *args, dir_fd):
                if not isinstance(path, str) or path in ("", ".", "..") or "/" in path or "\\" in path:
                    raise AssertionError("mutation escaped synthetic root")
                return getattr(os, name)(path, *args, dir_fd=self._fd(dir_fd))
            return relative_call
        raise AttributeError(name)


class OperatorRecipeTests(unittest.TestCase):
    def _posix_bash(self) -> str:
        if os.name != "posix":
            self.skipTest("recipe stubs require POSIX executable semantics")
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("recipe execution requires Bash")
        return bash

    def test_core_standing_yaml_runs_two_read_only_probe_batches(self) -> None:
        from app.config import SSHConfig
        from app.services.ssh_probe import SSHProbe
        from tests.test_ssh_probe import MemorySSHWire

        guide = (ROOT / "docs/SSH_READ_ONLY_SETUP.md").read_text(encoding="utf-8")
        section = guide.split("For this system, the preferred SSH command list is:", 1)[1]
        section = section.split("`camcontrol devlist -v` labels", 1)[0]
        commands = []
        for block in re.findall(r"```yaml\n(.*?)\n```", section, re.DOTALL):
            parsed = yaml.safe_load(block)
            commands.extend(parsed["commands"] if isinstance(parsed, dict) else parsed)
        self.assertTrue(commands)
        batches = []

        def client_factory(*, _cancel=None):
            wire = MemorySSHWire(
                lambda channel, _command: MemorySSHWire.feed(
                    channel, eof=True, status=0,
                )
            )
            batches.append(wire.commands)
            return wire.client

        probe = SSHProbe(SSHConfig(enabled=True, host="core.example.test", user="jbodmap", commands=commands))
        with patch.object(probe, "_client", side_effect=client_factory):
            for _ in range(2):
                results = probe._run_planned_commands_sync(lambda _results: [])
                self.assertEqual(len(results), len(commands))
                self.assertTrue(all(result.ok for result in results))
        self.assertEqual(batches, [commands, commands])
        self.assertFalse(any(" locate " in command for batch in batches for command in batch), batches)
        self.assertIn("not a permission allowlist", guide)
        self.assertIn("verified target", guide)
        self.assertIn("explicit, capability-gated UI action", guide)

    def test_strict_reference_matches_rejection_and_separate_tofu_controls(self) -> None:
        import paramiko
        from app.config import SSHConfig
        from app.services.ssh_probe import AutoPinHostKeyPolicy, SSHProbe

        guide = (ROOT / "docs/SSH_READ_ONLY_SETUP.md").read_text(encoding="utf-8")
        self.assertNotIn("first successful SSH connection pins", guide)
        for text in ("RejectPolicy", "trusted channel", "../wiki/SSH-Setup-and-Sudo.md",
                     "~/.ssh/known_hosts", "SSH_STRICT_HOST_KEY_CHECKING=false", "TOFU"):
            self.assertIn(text, guide)
        with tempfile.TemporaryDirectory() as directory:
            for strict in (True, False):
                with self.subTest(strict=strict):
                    client = MagicMock()
                    path = str(Path(directory) / "known_hosts")
                    probe = SSHProbe(SSHConfig(host="core.example.test", strict_host_key_checking=strict,
                                               known_hosts_path=path))
                    with patch("app.services.ssh_probe.paramiko.SSHClient", return_value=client):
                        probe._client()
                    policy = client.set_missing_host_key_policy.call_args.args[0]
                    key = MagicMock()
                    key.get_fingerprint.return_value = b"synthetic"
                    key.get_name.return_value = "ssh-rsa"
                    if strict:
                        self.assertIsInstance(policy, paramiko.RejectPolicy)
                        with self.assertRaises(paramiko.SSHException):
                            policy.missing_host_key(client, "core.example.test", key)
                        client.save_host_keys.assert_not_called()
                    else:
                        self.assertIsInstance(policy, AutoPinHostKeyPolicy)
                        policy.missing_host_key(client, "core.example.test", key)
                        client.save_host_keys.assert_called_once_with(path)

        preload = (ROOT / "wiki/SSH-Setup-and-Sudo.md").read_text()
        block = next(block for block in re.findall(r"```bash\n(.*?)\n```", preload, re.DOTALL)
                     if "ssh-keyscan " in block)
        scan = next(line for line in block.splitlines() if line.startswith("ssh-keyscan "))
        self.assertTrue(scan)
        approval = re.search(r"^read -r -p .*\n.*verified.*exit 1.*$", block, re.MULTILINE)
        self.assertIsNotNone(approval, "installation must wait for operator fingerprint approval")

    def test_strict_preload_and_approval_with_posix_bash(self) -> None:
        bash = self._posix_bash()
        import paramiko
        from app.config import SSHConfig
        from app.services.ssh_probe import SSHProbe

        preload = (ROOT / "wiki/SSH-Setup-and-Sudo.md").read_text()
        block = next(block for block in re.findall(r"```bash\n(.*?)\n```", preload, re.DOTALL)
                     if "ssh-keyscan " in block)
        scan = next(line for line in block.splitlines() if line.startswith("ssh-keyscan "))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            fake = target / "ssh-keyscan"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\nfrom pathlib import Path\nimport paramiko\n"
                "args = sys.argv[1:]\n"
                "Path(os.environ['ARGV_PATH']).write_text(json.dumps(args))\n"
                "port = args[args.index('-p') + 1] if '-p' in args else '22'\n"
                "host = args[-1]\n"
                "identity = host if port == '22' else f'[{host}]:{port}'\n"
                "print(paramiko.HostKeys.hash_host(identity), os.environ['PUBLIC_KEY'])\n"
            )
            fake.chmod(0o700)
            # Only a generated public host key is serialized. No SSH transport runs.
            key = paramiko.RSAKey.generate(1024)
            for host in ("storage-host.example.test", "2001:db8::1"):
                for port in (22, 2222):
                    with self.subTest(preload_host=host, port=port):
                        known_hosts = target / "known_hosts"
                        argv_path = target / "argv.json"
                        result = subprocess.run(
                            [bash, "-c", scan], cwd=target, capture_output=True,
                            text=True, timeout=10, env={
                                "PATH": directory, "ssh_host": host, "ssh_port": str(port),
                                "known_hosts_scan": str(known_hosts), "ARGV_PATH": str(argv_path),
                                "PUBLIC_KEY": f"{key.get_name()} {key.get_base64()}",
                            },
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(argv_path.read_text()), ["-H", "-p", str(port), host])
                        identity = host if port == 22 else f"[{host}]:{port}"
                        probe = SSHProbe(SSHConfig(host=host, port=port, strict_host_key_checking=True,
                                                   known_hosts_path=str(known_hosts)))
                        with patch.object(paramiko.SSHClient, "connect") as connect:
                            client = probe._client()
                        try:
                            self.assertTrue(client.get_host_keys().check(identity, key))
                            self.assertEqual(connect.call_args.kwargs["hostname"], host)
                            self.assertEqual(connect.call_args.kwargs["port"], port)
                            wrong = f"[{host}]:2222" if port == 22 else host
                            self.assertIsNone(client.get_host_keys().lookup(wrong))
                        finally:
                            client.close()
        with self.subTest(preload="explicit approval"):
            approval = re.search(r"^read -r -p .*\n.*verified.*exit 1.*$", block, re.MULTILINE)
            self.assertIsNotNone(approval, "installation must wait for operator fingerprint approval")
            for answer, expected in (("no\n", 1), ("", 1), ("yes\n", 0)):
                result = subprocess.run([bash, "-c", approval.group()],
                                        input=answer, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected)

    def test_admin_claims_match_base_and_overlay_yaml_sources(self) -> None:
        base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
        overlay = yaml.safe_load((ROOT / "docker-compose.nonroot.yml").read_text())["services"]
        admin = base["enclosure-admin"]
        self.assertEqual(admin["user"], "0:0")
        for key in ("read_only", "cap_drop", "cap_add", "security_opt"):
            self.assertNotIn(key, admin)
        hardened = overlay["enclosure-admin"]
        self.assertEqual(hardened["user"], "0:${APP_GID:-10001}")
        self.assertIs(hardened["read_only"], True)
        self.assertEqual(hardened["cap_drop"], ["ALL"])
        self.assertEqual(hardened["cap_add"], ["CHOWN", "FOWNER"])
        self.assertEqual(hardened["security_opt"], ["no-new-privileges:true"])
        self.assertEqual(set(admin["volumes"]), {
            "./config:/app/config", "./data:/app/data", "./history:/app/history",
            "host-prep-staging:/app/host-prep", "./config/ssh:/run/ssh:ro",
            "/var/run/docker.sock:/var/run/docker.sock", "./backup-journal:/app/backup-journal",
            "./backup-api:/app/backup-api:ro",
        })
        for name, service in base.items():
            if name != "enclosure-admin":
                self.assertFalse(any("docker.sock" in mount for mount in service["volumes"]))
        self.assertEqual(admin["profiles"], ["admin"])
        # Source intent remains checked when the installed-Compose integration skips.
        module = ast.parse(Path(__file__).read_text())
        method = next(node for node in ast.walk(module) if isinstance(node, ast.FunctionDef)
                      and node.name == "test_admin_ordered_config_with_installed_compose_only")
        option_lists = [[item.value for item in node.elts if isinstance(item, ast.Constant)]
                        for node in ast.walk(method) if isinstance(node, ast.List)]
        self.assertTrue(any(any(values[index:index + 2] == ["--profile", "admin"]
                                for index in range(len(values) - 1)) for values in option_lists),
                        "Compose config must explicitly select the profiled admin service")
        guide = (ROOT / "docs/ADMIN_TRUST_BOUNDARY.md").read_text()
        for text in ("base `docker-compose.yml`", "`0:0`", "does not declare",
                     "-f docker-compose.yml -f docker-compose.nonroot.yml", "`0:APP_GID`",
                     "`./backup-journal`", "named volume", "`host-prep-staging`",
                     "`/app/host-prep`", "root-equivalent", "CHOWN", "FOWNER"):
            self.assertIn(text, guide)

    def test_admin_ordered_config_with_installed_compose_only(self) -> None:
        compose = shutil.which("docker-compose")
        if compose:
            command = [compose]
        elif shutil.which("docker"):
            command = [shutil.which("docker"), "compose"]
            if subprocess.run(command + ["version"], capture_output=True, timeout=10).returncode:
                self.skipTest("Compose plugin unavailable; YAML source contract only")
        else:
            self.skipTest("Compose unavailable; YAML source contract only, no merge/runtime proof")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name in ("docker-compose.yml", "docker-compose.nonroot.yml"):
                shutil.copyfile(ROOT / name, target / name)
            (target / ".env").write_text("")
            for app_gid in (None, "12345"):
                env = {"PATH": os.environ.get("PATH", ""), "HOME": directory}
                if app_gid is not None:
                    env["APP_UID"] = "12344"
                    env["APP_GID"] = app_gid
                for hardened in (False, True):
                    with self.subTest(app_gid=app_gid, hardened=hardened):
                        argv = command + ["--profile", "admin", "-f", "docker-compose.yml"]
                        if hardened:
                            argv += ["-f", "docker-compose.nonroot.yml"]
                        result = subprocess.run(argv + ["config", "--format", "json"], cwd=target,
                                                env=env, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        services = json.loads(result.stdout)["services"]
                        self.assertIn("enclosure-admin", services)
                        admin = services["enclosure-admin"]
                        self.assertEqual(admin["user"], f"0:{app_gid or '10001'}" if hardened else "0:0")
                        self.assertEqual(admin.get("read_only", False), hardened)
                        self.assertEqual(admin.get("cap_drop", []), ["ALL"] if hardened else [])
                        self.assertEqual(set(admin.get("cap_add", [])), {"CHOWN", "FOWNER"} if hardened else set())
                        self.assertEqual(admin.get("security_opt", []), ["no-new-privileges:true"] if hardened else [])

    def test_staging_numeric_helper_accepts_root_owners_but_not_root_status_group(self) -> None:
        tree = ast.parse(rotation_staging_code())
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "numeric_id")
        namespace: dict[str, Any] = {"os": SimpleNamespace(environ={"HISTORY_UID": "0", "APP_GID": "0"})}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "<documented numeric_id>", "exec"), namespace)
        try:
            actual = namespace["numeric_id"]("HISTORY_UID")
        except SystemExit as error:
            self.fail(f"documented helper rejects supported root owner: {error}")
        self.assertEqual(actual, 0)
        namespace["os"].environ["APP_GID"] = "10001"
        self.assertEqual(namespace["numeric_id"]("APP_GID"), 10001)
        namespace["os"].environ["APP_GID"] = "0"
        with self.assertRaises(SystemExit):
            namespace["numeric_id"]("APP_GID")
        for raw in ("-1", "", "1.5", " 1"):
            namespace["os"].environ["HISTORY_UID"] = raw
            with self.assertRaises(SystemExit):
                namespace["numeric_id"]("HISTORY_UID")
        guide = (ROOT / "docs/SEGMENTED_HISTORY_V2.md").read_text()
        shell = next(block for block in re.findall(r"```bash\n(.*?)\n```", guide, re.DOTALL)
                     if "STAGE_NAME =" in block).split("<<'PY'", 1)[0]
        inputs = ("HISTORY_UID", "HISTORY_GID", "APP_GID", "BACKUP_UID", "BACKUP_GID")
        with self.subTest(staging="observed owners, not service defaults"):
            self.assertEqual(dict(re.findall(r"^([A-Z_]+)=([0-9]+)$", shell, re.MULTILINE)), {})
            for name in inputs:
                self.assertIn(f'${{{name}:?', shell)
                self.assertIn(f'{name}="${name}"', shell)
            self.assertIn("stat -c", guide)
            self.assertIn("stored owner", guide)
            self.assertIn("--no-deps", guide)
            self.assertIn("HOLD", guide)
        commands = [block for block in re.findall(r"```bash\n(.*?)\n```", guide, re.DOTALL)
                    if "scripts/rotate_segmented_history.py" in block]
        self.assertEqual(len(commands), 3)
        base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
        overlay = yaml.safe_load((ROOT / "docker-compose.nonroot.yml").read_text())["services"]
        self.assertEqual(base["enclosure-history"]["user"], "0:0")
        self.assertEqual(base["enclosure-backup"]["user"], "${BACKUP_UID:-0}:${BACKUP_GID:-0}")
        self.assertEqual(overlay["enclosure-history"]["user"], "${APP_UID:-10001}:${APP_GID:-10001}")
        self.assertEqual(overlay["enclosure-backup"]["user"], "${BACKUP_UID:-1000}:${BACKUP_GID:-1000}")
        self.assertIn("do not recursively chown", guide)

    def test_rotation_commands_with_posix_bash(self) -> None:
        bash = self._posix_bash()
        guide = (ROOT / "docs/SEGMENTED_HISTORY_V2.md").read_text()
        commands = [block for block in re.findall(r"```bash\n(.*?)\n```", guide, re.DOTALL)
                    if "scripts/rotate_segmented_history.py" in block]
        self.assertEqual(len(commands), 3)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            fake = target / "docker"
            fake.write_text(f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
                            "Path(os.environ['ARGV_PATH']).write_text(json.dumps(sys.argv[1:]))\n")
            fake.chmod(0o700)
            for uid, gid in ((0, 0), (10001, 10001), (12001, 12002)):
                for command in commands:
                    with self.subTest(publisher=(uid, gid), command=command.splitlines()[-1]):
                        argv_path = target / "argv.json"
                        result = subprocess.run([bash, "-c", command], cwd=target,
                                                capture_output=True, text=True, timeout=10, env={
                                                    "PATH": directory, "ARGV_PATH": str(argv_path),
                                                    "HISTORY_UID": str(uid), "HISTORY_GID": str(gid),
                                                })
                        self.assertEqual(result.returncode, 0, result.stderr)
                        argv = json.loads(argv_path.read_text())
                        self.assertIn("--user", argv)
                        self.assertEqual(argv[argv.index("--user") + 1], f"{uid}:{gid}")
                        self.assertIn("--no-deps", argv)
                        self.assertIn("enclosure-history", argv)
                        self.assertIn("/app/history/history.db", argv)

    def test_staging_source_owner_with_posix_geteuid(self) -> None:
        if os.name != "posix" or not callable(getattr(os, "geteuid", None)):
            self.skipTest("source ownership requires POSIX os.geteuid")
        from history_service.segment_sealer import _require_source_owner

        # Root service defaults do not grant root permission to publish app-owned history.
        for owner in (0, 10001, 12001):
            with patch("history_service.segment_sealer.os.geteuid", return_value=owner):
                _require_source_owner(SimpleNamespace(st_uid=owner))
            with patch("history_service.segment_sealer.os.geteuid", return_value=owner + 1):
                with self.assertRaisesRegex(ValueError, "must own"):
                    _require_source_owner(SimpleNamespace(st_uid=owner))

    @unittest.skipUnless(os.name == "posix", "literal staging requires POSIX descriptor semantics")
    def test_literal_staging_root_and_nonroot_ownership_labels_and_refusals(self) -> None:
        from history_service.segment_rotation import _validate_backup_evidence

        code = rotation_staging_code()
        # Only these imports may execute. No shell, sudo, network, or host paths.
        allowed = {name: __import__(name) for name in ("hashlib", "json", "re", "stat")}
        self.assertEqual({alias.name for node in ast.walk(ast.parse(code)) if isinstance(node, ast.Import)
                          for alias in node.names}, set(allowed) | {"os"})
        archive_name = "jbod-scheduled-backup-20260101T000000Z-00000000.tar.zst.enc"
        payload = b"synthetic encrypted-archive-shaped bytes; staging does not decrypt"
        for history_ids, backup_ids in (((0, 0), (0, 0)), ((10001, 10001), (1000, 1000)),
                                         ((12001, 12002), (13001, 13002))):
            for fault in (None, "history_owner", "backup_owner", "status_owner", "archive_owner",
                          "history_group", "backup_group", "status_group", "archive_group",
                          "backup_mode", "status_mode", "archive_mode", "digest", "size",
                          "archive_symlink", "status_symlink", "backup_symlink", "hardlink",
                          "changed_status", "changed_source", "missing_history"):
                with self.subTest(history=history_ids, backup=backup_ids, fault=fault):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        for name in ("backup-status", "backups/scheduled", "history"):
                            (root / name).mkdir(parents=True, mode=0o700)
                        archive = root / "backups/scheduled" / archive_name
                        archive.write_bytes(payload)
                        archive.chmod(0o600)
                        status_path = root / "backup-status/scheduled-backup.json"
                        status_payload = {"schema_version": 1, "enabled": True, "success_count": 1,
                                          "failure_count": 0, "last_retention_removed": 0,
                                          "last_attempt_at": None, "last_failure_at": None,
                                          "last_success_at": datetime.now(timezone.utc).isoformat(),
                                          "last_error_code": None,
                                          "included_groups": ["history_db"], "last_absent_groups": [],
                                          "last_artifact_name": archive_name, "last_size_bytes": len(payload),
                                          "last_sha256": hashlib.sha256(payload).hexdigest()}
                        if fault == "digest":
                            status_payload["last_sha256"] = "0" * 64
                        if fault == "size":
                            status_payload["last_size_bytes"] += 1
                        if fault == "missing_history":
                            status_payload["last_absent_groups"] = ["history_db"]
                        status_path.write_text(json.dumps(status_payload))
                        status_path.chmod(0o640)
                        paths = {"history": root / "history", "backup": root / "backups/scheduled",
                                 "archive": archive, "status": status_path}
                        owners = {}
                        for name, path in paths.items():
                            ids = history_ids if name == "history" else backup_ids
                            if name == "status":
                                ids = (backup_ids[0], 10001)
                            if fault == name + "_owner":
                                ids = (99999, ids[1])
                            if fault == name + "_group":
                                ids = (ids[0], 99999)
                            metadata = path.stat()
                            owners[metadata.st_dev, metadata.st_ino] = ids
                            if fault == name + "_mode":
                                path.chmod(0o777)
                            if fault == name + "_symlink":
                                moved = path.with_name(path.name + ".real")
                                path.rename(moved)
                                path.symlink_to(moved)
                        if fault == "hardlink":
                            os.link(archive, archive.with_name("another-link"))
                        env: dict[str, str] = dict(zip(("HISTORY_UID", "HISTORY_GID", "BACKUP_UID", "BACKUP_GID"),
                                       map(str, (*history_ids, *backup_ids))))
                        env["APP_GID"] = "10001"
                        # APP_UID keeps the baseline executable; candidate must use HISTORY_UID.
                        env["APP_UID"] = str(history_ids[0])
                        fake_os = SyntheticStagingOS(root, env, owners, change_status=fault == "changed_status",
                                                     change_source=fault == "changed_source")

                        def safe_import(name, *args, **kwargs):
                            if name == "os":
                                return fake_os
                            if name not in allowed:
                                raise AssertionError("unexpected recipe import")
                            return allowed[name]

                        namespace = {"__builtins__": dict(vars(builtins), __import__=safe_import)}
                        stage = root / "history/.segment-rotation-backup"
                        output = io.StringIO()
                        try:
                            with redirect_stdout(output):
                                exec(compile(code, "<literal staging recipe>", "exec"), namespace)
                        except (SystemExit, OSError) as error:
                            if fault is None:
                                self.fail(f"supported ownership labels rejected: {error}")
                            if fault.endswith("_symlink"):
                                self.assertIsInstance(error, OSError)
                            else:
                                reason = {
                                    "history": "history directory ownership",
                                    "backup": "scheduled backup directory ownership or mode",
                                    "status": "scheduled backup status ownership or mode",
                                    "archive": "scheduled backup archive ownership or mode",
                                    "digest": "integrity verification",
                                    "size": "integrity verification",
                                    "hardlink": "scheduled backup archive ownership or mode",
                                    "changed": ("status changed" if fault == "changed_status"
                                                else "integrity verification"),
                                    "missing": "complete FULL backup evidence",
                                }[fault.split("_", 1)[0]]
                                self.assertIn(reason, str(error))
                            self.assertFalse(stage.exists(), "refused staging left publishable evidence")
                            self.assertNotIn("staged_backup=ok", output.getvalue())
                        else:
                            self.assertIsNone(fault, "unsafe evidence was staged")
                            self.assertEqual(output.getvalue(), "staged_backup=ok\n")
                            staged = stage / archive_name
                            self.assertEqual(staged.read_bytes(), payload)
                            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
                            self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
                            metadata = staged.stat()
                            self.assertEqual(owners[metadata.st_dev, metadata.st_ino], history_ids)
                            record = _validate_backup_evidence(stage, status_path)
                            self.assertEqual(record["sha256"], hashlib.sha256(payload).hexdigest())
                            # Staging is not rotation authorization. The real rotation gate
                            # must still reject stale status before catalog publication.
                            before = staged.read_bytes()
                            status_payload["last_success_at"] = (
                                datetime.now(timezone.utc) - timedelta(days=3)
                            ).isoformat()
                            status_path.write_text(json.dumps(status_payload))
                            with self.assertRaisesRegex(ValueError, "stale"):
                                _validate_backup_evidence(stage, status_path)
                            self.assertEqual(staged.read_bytes(), before)
                            self.assertFalse((root / "history/segments/catalog.json").exists())
                        finally:
                            leaked = set(fake_os.descriptors)
                            for descriptor in leaked:
                                fake_os.close(descriptor)
                        # An early SystemExit in this standalone heredoc relies on
                        # interpreter exit to close initial directory descriptors.
                        if fault is None:
                            self.assertEqual(leaked, set())


class OperatorRecipePortabilityTests(unittest.TestCase):
    PORTABLE_METHODS = (
        "test_core_standing_yaml_runs_two_read_only_probe_batches",
        "test_strict_reference_matches_rejection_and_separate_tofu_controls",
        "test_admin_claims_match_base_and_overlay_yaml_sources",
        "test_staging_numeric_helper_accepts_root_owners_but_not_root_status_group",
    )

    def _run_portable(self) -> None:
        result = unittest.TestResult()
        for name in self.PORTABLE_METHODS:
            test = OperatorRecipeTests(name)
            with patch.object(test, "assertIn", wraps=test.assertIn) as assertions:
                test.run(result)
            self.assertGreater(assertions.call_count, 0, f"{name} lost its portable assertions")
        self.assertEqual(result.testsRun, len(self.PORTABLE_METHODS))
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertEqual(result.skipped, [], "portable assertions must execute, not skip")

    def test_portable_assertions_run_without_bash_or_posix_identity(self) -> None:
        # Replace only this module's os binding, never global os.name/Path behavior.
        for platform in ("posix", "nt"):
            with self.subTest(platform=platform), \
                    patch.object(sys.modules[__name__], "os", SimpleNamespace(name=platform)), \
                    patch.object(shutil, "which", return_value=None), \
                    patch.object(subprocess, "run", side_effect=AssertionError("portable shell execution")):
                self._run_portable()

    SHELL_METHODS = (
        "test_strict_preload_and_approval_with_posix_bash",
        "test_rotation_commands_with_posix_bash",
    )
    OWNER_METHOD = "test_staging_source_owner_with_posix_geteuid"

    def _assert_execution_skips(self, methods: tuple[str, ...], reason: str) -> None:
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "history_service.segment_sealer":
                raise AssertionError("protected ownership block reached")
            return original_import(name, *args, **kwargs)

        result = unittest.TestResult()
        with patch.object(builtins, "__import__", side_effect=guarded_import), \
                patch.object(tempfile, "TemporaryDirectory", side_effect=AssertionError("protected fixture reached")), \
                patch.object(subprocess, "run", side_effect=AssertionError("protected shell reached")):
            unittest.TestSuite(OperatorRecipeTests(name) for name in methods).run(result)
        self.assertEqual(result.testsRun, len(methods))
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertEqual([(test._testMethodName, message) for test, message in result.skipped],
                         [(name, reason) for name in methods])

    def test_missing_bash_skips_only_shell_methods(self) -> None:
        with patch.object(sys.modules[__name__], "os", SimpleNamespace(name="posix")), \
                patch.object(shutil, "which", return_value=None) as lookup:
            self._run_portable()
            self._assert_execution_skips(self.SHELL_METHODS, "recipe execution requires Bash")
            self.assertEqual(lookup.call_count, len(self.SHELL_METHODS))
            for call in lookup.call_args_list:
                self.assertEqual(call.args, ("bash",))

    def test_nonposix_skips_executable_stubs_even_when_bash_is_available(self) -> None:
        with patch.object(sys.modules[__name__], "os", SimpleNamespace(name="nt")), \
                patch.object(shutil, "which", return_value="bash") as lookup:
            self._run_portable()
            self._assert_execution_skips(self.SHELL_METHODS, "recipe stubs require POSIX executable semantics")
            self._assert_execution_skips((self.OWNER_METHOD,), "source ownership requires POSIX os.geteuid")
            lookup.assert_not_called()

    def test_missing_geteuid_skips_protected_ownership_not_portable_assertions(self) -> None:
        for capabilities in (SimpleNamespace(name="posix"), SimpleNamespace(name="posix", geteuid=None)):
            with self.subTest(capabilities=capabilities), \
                    patch.object(sys.modules[__name__], "os", capabilities):
                self._run_portable()
                self._assert_execution_skips((self.OWNER_METHOD,), "source ownership requires POSIX os.geteuid")


class PublicDocsContractTests(unittest.TestCase):
    def test_platform_api_first_examples_keep_tls_verification_opt_in(self) -> None:
        for relative_path in (
            "wiki/TrueNAS-CORE-Setup.md",
            "wiki/TrueNAS-SCALE-Setup.md",
            "wiki/Quantastor-Setup.md",
        ):
            guide = (ROOT / relative_path).read_text(encoding="utf-8")
            initial_setup = guide.split("## 2.", maxsplit=1)[0]
            with self.subTest(guide=relative_path):
                self.assertIn("verify_ssl: false", initial_setup)
                self.assertNotIn("verify_ssl: true", initial_setup)

    def test_admin_guide_does_not_describe_the_main_ui_as_read_only(self) -> None:
        guide = (ROOT / "wiki/Admin-UI-and-System-Setup.md").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("read-only enclosure UI", guide)
        self.assertIn("main enclosure UI", guide)

    def test_public_docs_use_current_service_names(self) -> None:
        current_reference_docs = tuple(
            path.relative_to(ROOT).as_posix()
            for path in sorted((ROOT / "docs").glob("*.md"))
        )
        for relative_path in (
            "README.md",
            *sorted(EXPECTED_WIKI_PAGES),
            *current_reference_docs,
        ):
            text = (ROOT / relative_path).read_text(encoding="utf-8")
            allowed_read_ui_contexts = EXPECTED_HISTORICAL_READ_UI_CONTEXTS.get(
                relative_path, ()
            )
            text_without_allowed_contexts = text
            for allowed_context in allowed_read_ui_contexts:
                text_without_allowed_contexts, replacements = re.subn(
                    allowed_context,
                    "",
                    text_without_allowed_contexts,
                    count=1,
                    flags=re.IGNORECASE,
                )
                self.assertEqual(
                    replacements,
                    1,
                    f"missing historical read UI context in {relative_path}",
                )
            with self.subTest(document=relative_path):
                self.assertNotRegex(text, STALE_ADMIN_SIDECAR_PATTERN)
                self.assertNotRegex(
                    text_without_allowed_contexts,
                    STALE_READ_UI_PATTERN,
                )

    def test_stale_service_name_patterns_cover_plural_mutations(self) -> None:
        for stale_text, pattern in (
            ("admin sidecar", STALE_ADMIN_SIDECAR_PATTERN),
            ("admin sidecars", STALE_ADMIN_SIDECAR_PATTERN),
            ("read UI", STALE_READ_UI_PATTERN),
            ("read UIs", STALE_READ_UI_PATTERN),
            ("read\nUI", STALE_READ_UI_PATTERN),
            ("read\nUIs", STALE_READ_UI_PATTERN),
        ):
            with self.subTest(stale_text=stale_text):
                self.assertRegex(stale_text, pattern)

    def test_architecture_guide_states_the_reachability_boundary_plainly(self) -> None:
        guide = (ROOT / "wiki/Architecture-and-Services.md").read_text(
            encoding="utf-8"
        )
        normalized = " ".join(guide.split())

        self.assertNotRegex(guide, r"(?i)trusted (?:network|LAN)")
        self.assertIn("Anyone who can reach an enabled service", normalized)
        self.assertIn("Basic authentication", normalized)

    def test_architecture_and_backup_guides_bound_both_backup_workers(self) -> None:
        architecture = (ROOT / "wiki/Architecture-and-Services.md").read_text(
            encoding="utf-8"
        )
        backup = (ROOT / "wiki/Backup-Restore-and-Debug-Bundles.md").read_text(
            encoding="utf-8"
        )
        normalized_backup = " ".join(backup.split())

        for marker in (
            "One-shot backup",
            "Backup scheduler",
            "`backup`",
            "`backup-scheduler`",
            "no network",
            "Unix socket",
            "no published TCP port",
            "Docker socket",
            "./backup-journal",
            "./backup-api",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, architecture)
        self.assertIn("NFS overlay", architecture)
        self.assertIn("current-source", normalized_backup)
        self.assertIn("v0.22.2", backup)
        self.assertIn("v0.23.0", backup)
        self.assertIn("image-only update cannot add", normalized_backup)
        self.assertIn("source checkout's `docker-compose.yml`", normalized_backup)
        self.assertIn("beginner install's `compose.yaml`", normalized_backup)

    def test_readme_is_a_short_human_facing_entry_point(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertLessEqual(len(readme.splitlines()), 150)
        for required in (
            "https://gcs8.github.io/truenas-jbod-ui/",
            "public-demo-overview.png",
            "public-demo-history.png",
            "TRUENAS_HOST=https://truenas.example.test",
            "TRUENAS_API_KEY=replace-with-your-api-key",
            "TRUENAS_PLATFORM=core",
            "TRUENAS_VERIFY_SSL=false",
            "docker compose up -d",
            "wiki/Quick-Start.md",
        ):
            with self.subTest(required=required):
                self.assertIn(required, readme)

        for internal_note in (
            "Desktop support contract",
            "documentation inventory",
            "review baseline",
            "source revision",
            "release gate",
            "current `main` source build",
            "next release",
            "ADMIN_PUBLIC_ORIGIN",
            "APP_PUBLIC_ORIGIN",
            "ADMIN_AUTH_MODE",
            "TRUENAS_TLS_CA_BUNDLE_PATH",
        ):
            with self.subTest(internal_note=internal_note):
                self.assertNotIn(internal_note, readme)

    def test_readme_discovers_backup_scheduler_and_admin_backup_workflows(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        normalized = " ".join(readme.split())

        for required in (
            "--profile backup-scheduler",
            "The backup scheduler and Backups page are not in a released deployment yet",
            "current-source checkout using its matching image and Compose file",
            "only after you enable those two classes in the backup policy",
            "[Backups page](wiki/Backup-Restore-and-Debug-Bundles.md#editing-from-the-admin-backups-page)",
            "[`docker-compose.backup-nfs.yml`](wiki/Backup-Restore-and-Debug-Bundles.md#nfs-targets)",
            "[`docker-compose.history-bind.yml`](wiki/Upgrading.md#history)",
            "filesystem, FTP/FTPS, SFTP, SMB, NFS, or S3",
            "encrypted `tar.zst`",
            "existing `.7z` backups remain readable",
            "inspects the exact archive and asks for confirmation before import",
            "Local and remote retention are configured independently",
            "[Backup, restore, and debug bundles](wiki/Backup-Restore-and-Debug-Bundles.md)",
            "[Upgrading](wiki/Upgrading.md)",
        ):
            with self.subTest(required=required):
                self.assertIn(required, normalized)

        backup_guide = (ROOT / "wiki/Backup-Restore-and-Debug-Bundles.md").read_text(encoding="utf-8")
        self.assertIn("no with an `http://` custom endpoint", backup_guide)
        self.assertIn("Artifact and library routes use opaque backup ids", backup_guide)
        self.assertIn("secret-file paths and credential contents are never returned", backup_guide)
        self.assertNotIn("No route accepts or returns a file path or a credential", backup_guide)

    def test_repository_has_exact_readme_and_wiki_document_set(self) -> None:
        actual = {path.relative_to(ROOT).as_posix() for path in (ROOT / "wiki").glob("*.md")}

        self.assertEqual(actual, EXPECTED_WIKI_PAGES)
        self.assertTrue((ROOT / "README.md").is_file())
        self.assertEqual(len(actual), 25)
        self.assertEqual(len(actual) + 1, 26)
        self.assertFalse((ROOT / "wiki/Publishing-the-Wiki.md").exists())
        self.assertTrue((ROOT / "docs/PUBLISHING_THE_WIKI.md").is_file())

    def test_clean_checkout_docs_checker_passes(self) -> None:
        result = run_checker()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("26 documents", result.stdout)
        self.assertIn("local links", result.stdout)
        self.assertIn("YAML examples", result.stdout)
        self.assertIn("command paths", result.stdout)
        self.assertIn("configuration keys", result.stdout)
        self.assertIn("prose patterns", result.stdout)

    def test_documentation_inventory_is_archived_and_publishing_guide_names_the_gate(self) -> None:
        self.assertFalse((ROOT / "docs/DOCUMENTATION_INVENTORY.md").exists())
        self.assertTrue((ROOT / "docs/archive/DOCUMENTATION_INVENTORY.md").is_file())

        publishing = (ROOT / "docs/PUBLISHING_THE_WIKI.md").read_text(encoding="utf-8")
        self.assertIn("scripts/check_public_docs.py", publishing)

    def test_every_live_reference_document_is_linked_from_the_wiki_or_contributing(self) -> None:
        linkable = (
            (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
            + "".join((ROOT / page).read_text(encoding="utf-8") for page in sorted(EXPECTED_WIKI_PAGES))
            + (ROOT / "README.md").read_text(encoding="utf-8")
        )
        current_release_files = {"RELEASE_NOTES_0.23.0.md", "RELEASE_WRAP_0.23.0.md"}
        unlinked = [
            path.name
            for path in sorted((ROOT / "docs").glob("*.md"))
            if path.name not in current_release_files and f"docs/{path.name}" not in linkable
        ]
        self.assertEqual(unlinked, [])

    def test_quick_start_is_ca_optional_and_advanced_docs_cover_verification(self) -> None:
        quick_start = (ROOT / "wiki/Quick-Start.md").read_text(encoding="utf-8")
        advanced = (ROOT / "wiki/Advanced-Configuration.md").read_text(encoding="utf-8")
        operations = (ROOT / "wiki/Operations-Logging-and-Metrics.md").read_text(encoding="utf-8")

        self.assertIn("TRUENAS_VERIFY_SSL=false", quick_start)
        self.assertNotIn("TRUENAS_TLS_CA_BUNDLE_PATH", quick_start)
        self.assertIn("TRUENAS_VERIFY_SSL=true", advanced)
        self.assertIn("TRUENAS_TLS_CA_BUNDLE_PATH=/app/config/tls/truenas-ca.pem", advanced)
        self.assertIn("TRUENAS_TLS_SERVER_NAME=truenas.example.test", advanced)
        self.assertIn("trusted, isolated logging network", operations)
        self.assertIn("authenticated and encrypted", operations)

    def test_retired_app_bind_keys_are_not_described_as_restart_only(self) -> None:
        advanced = (ROOT / "wiki/Advanced-Configuration.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        browser_qa = (ROOT / "qa/admin-operations.spec.js").read_text(encoding="utf-8")

        restart_table = advanced.split("A few settings are only read", 1)[1].split("When you change one", 1)[0]
        reload_entry = changelog.split("- The main UI applies edits", 1)[1].split("(#614)", 1)[0]

        self.assertNotRegex(restart_table, r"\bapp\.(?:host|port)\b")
        self.assertNotRegex(reload_entry.lower(), r"\b(?:bind address|port)\b")
        self.assertNotRegex(
            browser_qa,
            r'restart_settings:\s*\[[^\]]*"app\.(?:host|port)"',
        )
        self.assertIn("`app.public_origin`", restart_table)
        self.assertIn('restart_settings: ["app.public_origin"]', browser_qa)

    def test_all_yaml_examples_parse(self) -> None:
        count = 0
        for relative_path in ("README.md", *sorted(EXPECTED_WIKI_PAGES)):
            text = (ROOT / relative_path).read_text(encoding="utf-8")
            parts = text.split("```yaml\n")
            for part in parts[1:]:
                block, separator, _rest = part.partition("\n```")
                self.assertTrue(separator, relative_path)
                yaml.safe_load(block)
                count += 1
        self.assertGreaterEqual(count, 20)

    def test_external_link_check_has_a_bounded_online_mode(self) -> None:
        help_result = run_checker("--help")

        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--check-external", help_result.stdout)
        self.assertIn("--external-timeout-seconds", help_result.stdout)
        self.assertIn("--external-max-attempts", help_result.stdout)

    def test_same_repository_main_links_validate_candidate_paths_without_traversal(self) -> None:
        self.assertEqual(
            same_repository_main_path(
                "https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/PUBLIC_DEMO_PRODUCT_BRIEF.md"
            ),
            Path("docs/PUBLIC_DEMO_PRODUCT_BRIEF.md"),
        )
        self.assertIsNone(
            same_repository_main_path(
                "https://github.com/another-owner/truenas-jbod-ui/blob/main/docs/PUBLIC_DEMO_PRODUCT_BRIEF.md"
            )
        )
        self.assertIsNone(
            same_repository_main_path(
                "https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/../private.txt"
            )
        )

    def test_public_demo_brief_records_product_browser_and_rollback_contracts(self) -> None:
        brief = (ROOT / "docs/PUBLIC_DEMO_PRODUCT_BRIEF.md").read_text(encoding="utf-8")
        checklist = (ROOT / "docs/RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

        for phrase in (
            "## Audience",
            "## Included interactions",
            "## Deliberate omissions",
            "## Fixture strategy",
            "## Source and build identity",
            "## Screenshot provenance",
            "## Supported desktop browser and layout matrix",
            "## Publication and readback",
            "## Revert and republish",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, brief)
        self.assertIn("Mobile and tablet layouts are unsupported", brief)
        self.assertIn("git worktree add --detach", checklist)
        self.assertIn("python3 scripts/check_public_demo_artifact.py public-demo", checklist)
        self.assertIn("byte-readback and browser jobs", checklist)

    def test_public_demo_pages_are_for_visitors(self) -> None:
        public_demo = (ROOT / "wiki/Public-Demo-Site.md").read_text(encoding="utf-8")
        visual_tour = (ROOT / "wiki/Visual-Tour.md").read_text(encoding="utf-8")
        sidebar = (ROOT / "wiki/_Sidebar.md").read_text(encoding="utf-8")
        publishing_guide = (ROOT / "docs/PUBLISHING_THE_WIKI.md").read_text(encoding="utf-8")

        for page in (public_demo, visual_tour):
            for maintainer_term in (
                "manifest",
                "pixel review",
                "source revision",
                "source-revision",
                "publication",
                "publish-public-demo.yml",
                "workflow_dispatch",
                "contract",
            ):
                with self.subTest(page=page.splitlines()[0], maintainer_term=maintainer_term):
                    self.assertNotIn(maintainer_term, page.lower())

        self.assertNotIn("Publishing-the-Wiki", sidebar)
        self.assertIn("python scripts/verify_wiki_drift.py", publishing_guide)
        self.assertIn("complete `images/` tree byte for byte", publishing_guide)
        self.assertIn("must pass before push", publishing_guide)
        self.assertIn("public Git URL and the pushed commit", publishing_guide)


if __name__ == "__main__":
    unittest.main()
