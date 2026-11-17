"""The portable 7z operations live in `history_service.backup_7z` behind `SystemBackupService`.

These tests pin what the move must keep: every method name, kind and signature
on the service, the released string literals, a module that imports without the
system-backup module, and that a replacement made on the system-backup module,
class or instance still reaches the moved code at call time. One test runs the
installed 7z binary, when there is one, with a synthetic passphrase.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from history_service import system_backup
from history_service.system_backup import BundleMember, SystemBackupService

ROOT = Path(__file__).resolve().parents[1]
MOVED = (
    "_build_7z_archive_to_path",
    "_read_7z_archive",
    "_validate_7z_listed_entries",
    "_validate_7z_listing_against_manifest",
    "_seven_zip_encryption_mode",
    "_is_7z_directory_entry",
    "_validate_extracted_7z_tree",
    "_seven_zip_listed_entries",
    "_run_7z_command",
    "_run_7z_prompt_command",
    "_raise_for_7z_failure",
)
# Recorded on the commit before the move.
RELEASED_SIGNATURES = {
    "_build_7z_archive_to_path": (
        "function",
        "(self, bundle_members: 'list[BundleMember]', manifest_bytes: 'bytes', archive_path: 'Path', *, "
        "passphrase: 'str | None' = None) -> 'None'",
    ),
    "_read_7z_archive": (
        "function",
        "(self, *, archive_path: 'Path', passphrase: 'str | None' = None, workspace: 'Path | None' = None) -> "
        "'tuple[dict[str, Any], dict[str, ExtractedMember], ArchivePackaging, dict[str, Any]]'",
    ),
    "_validate_7z_listed_entries": (
        "classmethod",
        "(entries: 'list[dict[str, str]]', *, archive_size: 'int', member_limit: 'int' = 1610612736, "
        "expanded_limit: 'int' = 2147483648) -> 'None'",
    ),
    "_validate_7z_listing_against_manifest": (
        "classmethod",
        "(entries: 'list[dict[str, str]]', manifest: 'dict[str, Any]') -> 'None'",
    ),
    "_seven_zip_encryption_mode": ("classmethod", "(entries: 'list[dict[str, str]]') -> 'bool'"),
    "_is_7z_directory_entry": ("staticmethod", "(entry: 'dict[str, str]') -> 'bool'"),
    "_validate_extracted_7z_tree": (
        "classmethod",
        "(extract_dir: 'Path', listed_entries: 'list[dict[str, str]]') -> 'None'",
    ),
    "_seven_zip_listed_entries": ("staticmethod", "(output: 'str', archive_path: 'Path') -> 'list[dict[str, str]]'"),
    "_run_7z_command": (
        "function",
        "(self, args: 'list[str]', *, cwd: 'Path | None' = None, passphrase: 'str | None' = None, "
        "timeout_seconds: 'float | None' = None) -> 'subprocess.CompletedProcess[str]'",
    ),
    "_run_7z_prompt_command": (
        "staticmethod",
        "(command: 'list[str]', *, cwd: 'Path | None', passphrase: 'str', timeout_seconds: 'float | None' = None) "
        "-> 'subprocess.CompletedProcess[str]'",
    ),
    "_raise_for_7z_failure": (
        "staticmethod",
        "(result: 'subprocess.CompletedProcess[str]', message: 'str', *, passphrase: 'str | None', "
        "reading_archive: 'bool' = False) -> 'None'",
    ),
}
# sha256 of the JSON list of every str/bytes literal repr in the eleven methods, in this
# order and in `ast.walk` order, recorded on the commit before the move.
RELEASED_LITERALS_SHA256 = "f6f9ee20dcd7c6f86db8f97e481944ef4ee832ba5047ec72126d85d4a35f3be1"
SYNTHETIC_PASSPHRASE = "synthetic split passphrase 7"
OK = subprocess.CompletedProcess(["7z"], 0, stdout="Everything is Ok", stderr="")


def _service() -> SystemBackupService:
    # The moved operations read no service state; they only call sibling methods.
    return SystemBackupService.__new__(SystemBackupService)


def _listing(*entries: dict[str, str]) -> str:
    blocks = ["Path = bundle.7z\nType = 7z"]
    blocks.extend("\n".join(f"{key} = {value}" for key, value in entry.items()) for entry in entries)
    return "\n\n".join(blocks) + "\n"


class Backup7zModuleShapeTests(unittest.TestCase):
    def test_backup_7z_imports_without_the_system_backup_module(self) -> None:
        probe = (
            "import sys\n"
            "import history_service.backup_7z as moved\n"
            "loaded = sorted(name for name in sys.modules if name == 'app' or name.startswith('app.')"
            " or name == 'history_service.system_backup')\n"
            "print(loaded)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

        tree = ast.parse((ROOT / "history_service/backup_7z.py").read_text(encoding="utf-8"))
        runtime_imports: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.If):
                # Only the typing-only block may name the system-backup module.
                self.assertEqual(ast.unparse(node.test), "TYPE_CHECKING")
                continue
            if isinstance(node, ast.Import):
                runtime_imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                runtime_imports.add((node.module or "").split(".")[0])
        self.assertLessEqual(runtime_imports, set(sys.stdlib_module_names) | {"__future__"})

    def test_service_keeps_every_moved_name_with_its_kind_and_signature(self) -> None:
        from history_service import backup_7z

        self.assertEqual(
            tuple(name for name, value in vars(backup_7z).items() if inspect.isfunction(value)
                  and value.__module__ == backup_7z.__name__),
            MOVED,
        )
        for name, (kind, signature) in RELEASED_SIGNATURES.items():
            with self.subTest(name):
                raw = inspect.getattr_static(SystemBackupService, name)
                self.assertEqual(type(raw).__name__, kind)
                self.assertEqual(str(inspect.signature(getattr(SystemBackupService, name))), signature)
        self.assertIs(system_backup._SYSTEM_BACKUP_MODULE, system_backup)

    def test_moved_string_literals_are_the_released_text(self) -> None:
        tree = ast.parse((ROOT / "history_service/backup_7z.py").read_text(encoding="utf-8"))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        literals = [
            repr(node.value)
            for name in MOVED
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))
        ]
        self.assertEqual(len(literals), 152)
        self.assertEqual(hashlib.sha256(json.dumps(literals).encode()).hexdigest(), RELEASED_LITERALS_SHA256)


class Backup7zLateBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="backup-7z-split-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def test_a_replaced_member_count_on_the_module_reaches_listing_validation(self) -> None:
        # One listed file with two implicit directories: only the moved count of
        # materialized paths (3) can cross a limit of 2; the shared declared-entry
        # check still sees one entry.
        entries = [{"Path": "config/sub/config.yaml", "Size": "10", "Packed Size": "10", "Encrypted": "-"}]
        SystemBackupService._validate_7z_listed_entries(entries, archive_size=1024)
        with patch.object(system_backup, "MAX_ARCHIVE_MEMBER_COUNT", 2):
            with self.assertRaisesRegex(ValueError, "too many members"):
                SystemBackupService._validate_7z_listed_entries(entries, archive_size=1024)

    def test_listing_limits_default_to_the_values_bound_when_the_class_was_defined(self) -> None:
        entries = [{"Path": "manifest.json", "Size": "10", "Packed Size": "10", "Encrypted": "-"}]
        # As before the move, a later module patch does not change the method's defaults.
        with (
            patch.object(system_backup, "MAX_ARCHIVE_MEMBER_BYTES", 1),
            patch.object(system_backup, "MAX_ARCHIVE_EXPANDED_BYTES", 1),
        ):
            SystemBackupService._validate_7z_listed_entries(entries, archive_size=1024)
            with self.assertRaisesRegex(ValueError, "expanded byte limit"):
                SystemBackupService._validate_7z_listed_entries(entries, archive_size=1024, member_limit=1)

    def test_a_replaced_binary_on_the_module_reaches_the_command_runner(self) -> None:
        missing = self.temp_dir / "no-such-7z"
        with patch.object(system_backup, "SEVEN_ZIP_BINARY", str(missing)):
            with self.assertRaisesRegex(ValueError, "requires the '7z' command inside the container image"):
                _service()._run_7z_command(["i"])

    def test_a_replaced_timeout_on_the_module_and_prompt_on_the_class_reach_the_runner(self) -> None:
        prompt = MagicMock(return_value=OK)
        with (
            patch.object(system_backup, "SEVEN_ZIP_TIMEOUT_SECONDS", 7),
            patch.object(system_backup, "SEVEN_ZIP_BINARY", "synthetic-7z"),
            patch.object(SystemBackupService, "_run_7z_prompt_command", prompt),
        ):
            result = _service()._run_7z_command(["l", "bundle.7z"], passphrase=SYNTHETIC_PASSPHRASE)
        self.assertIs(result, OK)
        prompt.assert_called_once_with(
            ["synthetic-7z", "l", "bundle.7z"],
            cwd=None,
            passphrase=SYNTHETIC_PASSPHRASE,
            timeout_seconds=7,
        )

    def test_replaced_messages_on_the_module_reach_the_failure_mapping(self) -> None:
        wrong = subprocess.CompletedProcess(["7z"], 2, stdout="Wrong password?", stderr="")
        with (
            patch.object(system_backup, "WRONG_PASSPHRASE_MESSAGE", "synthetic wrong"),
            patch.object(system_backup, "PASSPHRASE_REQUIRED_MESSAGE", "synthetic required"),
        ):
            with self.assertRaisesRegex(ValueError, "^synthetic wrong$"):
                SystemBackupService._raise_for_7z_failure(wrong, "m", passphrase=SYNTHETIC_PASSPHRASE)
            with self.assertRaisesRegex(ValueError, "^synthetic required$"):
                SystemBackupService._raise_for_7z_failure(wrong, "m", passphrase=None)

    def test_module_policy_hooks_and_instance_runner_reach_the_create_step(self) -> None:
        service = _service()
        service._run_7z_command = MagicMock(return_value=OK)
        service._raise_for_7z_failure = MagicMock()
        member = BundleMember(
            key="config_file",
            group_key="config_file",
            archive_path="config/config.yaml",
            source_path=None,
            present=True,
            content=b"synthetic: true\n",
        )
        logger = MagicMock()
        observe = MagicMock()
        with (
            patch.object(system_backup, "seven_zip_create_timeout_seconds", return_value=4321) as deadline,
            patch.object(system_backup, "observe_backup_7z_deadline", observe),
            patch.object(system_backup, "logger", logger),
        ):
            service._build_7z_archive_to_path(
                [member], b"{}", self.temp_dir / "bundle.7z", passphrase=SYNTHETIC_PASSPHRASE
            )
        input_bytes = len(b"{}") + len(member.content or b"")
        deadline.assert_called_once_with(input_bytes)
        observe.assert_called_once_with(operation="create", input_bytes=input_bytes, deadline_seconds=4321)
        self.assertEqual(logger.info.call_args.args[1:], (input_bytes, 4321, 512 * 1024, 4 * 60 * 60))
        command = service._run_7z_command.call_args.args[0]
        self.assertEqual(command[:6], ["a", "-t7z", "-y", "-bd", "-p", "-mhe=on"])
        self.assertNotIn(SYNTHETIC_PASSPHRASE, command)
        self.assertEqual(service._run_7z_command.call_args.kwargs["timeout_seconds"], 4321)
        service._raise_for_7z_failure.assert_called_once_with(
            OK, "Portable 7z backup export failed.", passphrase=SYNTHETIC_PASSPHRASE
        )

    def test_a_replaced_manifest_limit_and_instance_runner_reach_the_read_step(self) -> None:
        archive = self.temp_dir / "bundle.7z"
        archive.write_bytes(b"synthetic")
        service = _service()
        listing = _listing({"Path": "manifest.json", "Size": "64", "Packed Size": "64", "Encrypted": "-"})
        service._run_7z_command = MagicMock(
            return_value=subprocess.CompletedProcess(["7z"], 0, stdout=listing, stderr="")
        )
        workspace = self.temp_dir / "workspace"
        workspace.mkdir()
        with patch.object(system_backup, "MAX_MANIFEST_BYTES", 63):
            with self.assertRaisesRegex(ValueError, "manifest exceeds its size limit"):
                service._read_7z_archive(archive_path=archive, workspace=workspace)
        self.assertEqual(service._run_7z_command.call_count, 1)
        self.assertEqual(service._run_7z_command.call_args.args[0], ["l", "-slt", str(archive.absolute()), "-p"])
        self.assertFalse(workspace.exists())

    def test_a_replaced_class_helper_reaches_the_class_method_callers(self) -> None:
        entries = [{"Path": "manifest.json", "Size": "10", "Encrypted": "+"}]
        self.assertTrue(SystemBackupService._seven_zip_encryption_mode(entries))
        with patch.object(SystemBackupService, "_is_7z_directory_entry", staticmethod(lambda entry: True)):
            with self.assertRaisesRegex(ValueError, "contains no regular members"):
                SystemBackupService._seven_zip_encryption_mode(entries)
            # A directory-only listing must extract no files.
            (self.temp_dir / "manifest.json").write_bytes(b"0123456789")
            with self.assertRaisesRegex(ValueError, "extracted members do not match its listing"):
                SystemBackupService._validate_extracted_7z_tree(self.temp_dir, entries)

    def test_the_failure_log_keeps_its_logger_and_masks_the_passphrase(self) -> None:
        failed = subprocess.CompletedProcess(
            ["7z"], 2, stdout=f"synthetic detail {SYNTHETIC_PASSPHRASE}", stderr="synthetic stderr"
        )
        with (
            self.assertLogs("history_service.system_backup", level="WARNING") as logs,
            self.assertRaises(ValueError) as raised,
        ):
            SystemBackupService._raise_for_7z_failure(failed, "Synthetic step.", passphrase=SYNTHETIC_PASSPHRASE)
        self.assertEqual(str(raised.exception), "Synthetic step. The 7-Zip step failed (exit 2).")
        self.assertEqual(logs.records[0].name, "history_service.system_backup")
        self.assertEqual(logs.records[0].levelno, logging.WARNING)
        self.assertEqual(logs.records[0].getMessage(), "7-Zip exited with 2: synthetic detail ***\nsynthetic stderr")


@unittest.skipUnless(shutil.which("7z"), "real 7z executable unavailable")
class Backup7zNativeTests(unittest.TestCase):
    def test_encrypted_create_list_and_wrong_passphrase_through_the_service(self) -> None:
        with tempfile.TemporaryDirectory(prefix="backup-7z-native-") as directory:
            root = Path(directory)
            archive = root / "bundle.7z"
            member = BundleMember(
                key="config_file",
                group_key="config_file",
                archive_path="config/config.yaml",
                source_path=None,
                present=True,
                content=b"synthetic: true\n",
            )
            argv: list[str] = []
            original_popen = subprocess.Popen

            def popen(command, **kwargs):
                argv.append(json.dumps(command))
                self.assertNotIn(SYNTHETIC_PASSPHRASE, json.dumps(command))
                self.assertNotIn("env", kwargs)
                return original_popen(command, **kwargs)

            service = _service()
            with (
                patch("history_service.system_backup.subprocess.Popen", side_effect=popen),
                patch.object(system_backup, "SEVEN_ZIP_TIMEOUT_SECONDS", 60),
            ):
                service._build_7z_archive_to_path(
                    [member], b"{}", archive, passphrase=SYNTHETIC_PASSPHRASE
                )
                listed = service._run_7z_command(["l", "-slt", str(archive)], passphrase=SYNTHETIC_PASSPHRASE)
                service._raise_for_7z_failure(listed, "listing", passphrase=SYNTHETIC_PASSPHRASE)
                entries = service._seven_zip_listed_entries(listed.stdout, archive)
                wrong = service._run_7z_command(
                    ["x", str(archive), f"-o{root / 'out'}", "-y", "-bd"], passphrase="synthetic wrong passphrase"
                )
                with self.assertRaises(ValueError) as raised:
                    service._raise_for_7z_failure(
                        wrong, "extract", passphrase="synthetic wrong passphrase", reading_archive=True
                    )
            self.assertEqual(len(argv), 3)
            self.assertEqual(
                sorted(entry["Path"] for entry in entries if not service._is_7z_directory_entry(entry)),
                ["config/config.yaml", "manifest.json"],
            )
            self.assertTrue(service._seven_zip_encryption_mode(entries))
            service._validate_7z_listed_entries(entries, archive_size=archive.stat().st_size)
            self.assertEqual(str(raised.exception), system_backup.WRONG_PASSPHRASE_MESSAGE)
            self.assertNotIn(SYNTHETIC_PASSPHRASE, listed.stdout + wrong.stdout)


if __name__ == "__main__":
    unittest.main()
