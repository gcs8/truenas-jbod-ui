from __future__ import annotations

import errno
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.storage_writability import (
    CONTAINER_BIND_SOURCES,
    StorageDirectoryUnwritable,
    describe_unwritable_directory,
    host_repair_path,
    is_unwritable_error,
    unwritable_directory_error,
)
from history_service.startup import (
    HistoryStartupError,
    open_history_store_with_retries,
)


class DescribeUnwritableDirectoryTests(unittest.TestCase):
    def test_the_line_names_the_path_the_owner_and_the_command_to_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "history"
            directory.mkdir()

            message = describe_unwritable_directory(directory)

        self.assertIn(str(directory), message)
        self.assertIn("Cannot write to", message)
        # The chown command needs the uid this process runs as; Windows has no
        # POSIX identity, so the line names the fix in words there instead.
        self.assertIn("chown" if hasattr(os, "geteuid") else "write access", message)
        self.assertNotIn("Traceback", message)

    def test_only_permission_shaped_errors_are_classified(self) -> None:
        directory = Path("/app/history")
        for number in (errno.EACCES, errno.EPERM, errno.EROFS):
            with self.subTest(errno=number):
                error = unwritable_directory_error(OSError(number, "denied"), directory)
                self.assertIsInstance(error, StorageDirectoryUnwritable)
                assert error is not None
                self.assertEqual(error.directory, directory)
        self.assertIsNone(
            unwritable_directory_error(OSError(errno.ENOSPC, "full"), directory)
        )
        self.assertIsNone(unwritable_directory_error(ValueError("nope"), directory))

    def test_the_detail_the_ui_shows_names_no_path_and_no_exception_text(self) -> None:
        detail = StorageDirectoryUnwritable(Path("/app/data")).public_detail
        self.assertEqual(
            detail,
            "Could not save: the data folder is not writable by the app. See Troubleshooting.",
        )


class HistoryStartupRetryTests(unittest.TestCase):
    def test_an_unwritable_history_directory_fails_with_the_path_and_the_fix(self) -> None:
        slept: list[float] = []
        attempts: list[int] = []

        def factory() -> object:
            attempts.append(1)
            raise PermissionError(errno.EACCES, "Permission denied")

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaises(HistoryStartupError) as raised:
                open_history_store_with_retries(
                    factory,
                    directory=directory,
                    attempts=3,
                    initial_backoff_seconds=1.0,
                    sleep=slept.append,
                )

        self.assertEqual(len(attempts), 3, "the retries must be bounded")
        self.assertEqual(slept, [1.0, 2.0], "each retry must back off further")
        reason = raised.exception.reason
        self.assertIn(str(directory), reason)
        self.assertIn("chown" if hasattr(os, "geteuid") else "write access", reason)
        self.assertEqual(str(raised.exception), reason)

    def test_a_directory_that_becomes_writable_starts_normally(self) -> None:
        calls: list[int] = []

        def factory() -> str:
            calls.append(1)
            if len(calls) < 2:
                raise PermissionError(errno.EACCES, "Permission denied")
            return "store"

        with tempfile.TemporaryDirectory() as raw:
            store = open_history_store_with_retries(
                factory,
                directory=Path(raw),
                attempts=3,
                initial_backoff_seconds=0.5,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(store, "store")
        self.assertEqual(len(calls), 2)

    def test_a_failure_that_is_not_about_permissions_is_not_retried(self) -> None:
        calls: list[int] = []

        def factory() -> str:
            calls.append(1)
            raise ValueError("Segmented history activation is pending.")

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValueError):
                open_history_store_with_retries(
                    factory,
                    directory=Path(raw),
                    attempts=3,
                    initial_backoff_seconds=0.5,
                    sleep=lambda _seconds: None,
                )

        self.assertEqual(len(calls), 1)

    def test_the_reason_is_recorded_for_later_inspection(self) -> None:
        import history_service.startup as startup_module

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaises(HistoryStartupError):
                open_history_store_with_retries(
                    lambda: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")),
                    directory=directory,
                    attempts=1,
                    initial_backoff_seconds=0.1,
                    sleep=lambda _seconds: None,
                )
            recorded = startup_module.recorded_startup_failure()

        self.assertIsNotNone(recorded)
        assert recorded is not None
        self.assertIn(str(directory), recorded)


class SqliteUnwritableClassificationTests(unittest.TestCase):
    """SQLite reports a read-only directory as a message, not as an errno."""

    def test_sqlite_read_only_and_open_failures_are_unwritable(self) -> None:
        for message in (
            "attempt to write a readonly database",
            "unable to open database file",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_unwritable_error(sqlite3.OperationalError(message)))

    def test_locked_and_corrupt_databases_keep_their_own_failure(self) -> None:
        for error in (
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("no such table: samples"),
            sqlite3.DatabaseError("database disk image is malformed"),
        ):
            with self.subTest(error=str(error)):
                self.assertFalse(is_unwritable_error(error))


class HostRepairInstructionTests(unittest.TestCase):
    """The operator runs the repair on the host, so it must name a host path."""

    def test_a_container_mount_is_named_by_its_host_bind_source(self) -> None:
        message = describe_unwritable_directory(Path("/app/history"))
        remedy = message.split("On the Docker host", 1)[1]

        self.assertIn("./history", remedy)
        self.assertNotIn("/app/history", remedy)

    def test_a_path_inside_a_container_mount_keeps_its_relative_tail(self) -> None:
        message = describe_unwritable_directory(Path("/app/data/mappings"))
        remedy = message.split("On the Docker host", 1)[1]

        self.assertIn("./data/mappings", remedy)
        self.assertNotIn("/app/data", remedy)

    def test_a_path_outside_the_container_mounts_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            message = describe_unwritable_directory(Path(raw))

        self.assertIn(raw, message.split("On the Docker host", 1)[1])


class HistoryRuntimeStartupTests(unittest.TestCase):
    """Settings-time directory creation is part of the guarded startup."""

    def test_a_settings_time_permission_failure_is_retried_and_explained(self) -> None:
        from history_service import main as history_main

        calls: list[int] = []

        def failing_settings() -> object:
            calls.append(1)
            raise PermissionError(errno.EACCES, "Permission denied")

        with patch.object(history_main, "get_history_settings", failing_settings):
            with self.assertRaises(HistoryStartupError) as raised:
                history_main.open_history_runtime(
                    attempts=2,
                    initial_backoff_seconds=0.0,
                    sleep=lambda _seconds: None,
                )

        self.assertEqual(len(calls), 2, "the settings mkdir must be retried")
        self.assertIn("Cannot write to", raised.exception.reason)

    def test_a_terminal_failure_reports_unavailable_instead_of_crash_looping(self) -> None:
        from history_service import main as history_main

        def failing_settings() -> object:
            raise PermissionError(errno.EACCES, "Permission denied")

        with patch.object(history_main, "get_history_settings", failing_settings):
            settings, store, reason = history_main.load_history_runtime(
                attempts=1,
                initial_backoff_seconds=0.0,
                sleep=lambda _seconds: None,
            )

        self.assertIsNone(store, "the process must stay up with no store")
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("Cannot write to", reason)
        self.assertIsNotNone(settings, "a default settings object keeps the app importable")


class MappingStoreUnwritableTests(unittest.TestCase):
    def test_an_unwritable_data_directory_raises_the_named_error(self) -> None:
        from app.services.mapping_store import MappingStorageUnwritable, MappingStore

        with tempfile.TemporaryDirectory() as raw:
            store = MappingStore(Path(raw) / "mappings.json")
            with patch.object(
                MappingStore,
                "_create_temp_file",
                side_effect=PermissionError(errno.EACCES, "Permission denied"),
            ):
                with self.assertRaises(MappingStorageUnwritable) as raised:
                    store._commit_v2({})

        self.assertIsInstance(raised.exception, StorageDirectoryUnwritable)
        self.assertEqual(
            raised.exception.public_detail,
            "Could not save: the data folder is not writable by the app. See Troubleshooting.",
        )

    def test_other_write_failures_keep_their_own_error(self) -> None:
        from app.services.mapping_store import MappingStorageUnwritable, MappingStore

        with tempfile.TemporaryDirectory() as raw:
            store = MappingStore(Path(raw) / "mappings.json")
            with patch.object(
                MappingStore,
                "_create_temp_file",
                side_effect=OSError(errno.ENOSPC, "No space left on device"),
            ):
                with self.assertRaises(OSError) as raised:
                    store._commit_v2({})

        self.assertNotIsInstance(raised.exception, MappingStorageUnwritable)


class AliasStoreUnwritableTests(unittest.TestCase):
    def test_an_unwritable_data_directory_raises_the_named_error(self) -> None:
        from app.services.sas_fabric_alias_store import (
            SasFabricAliasStorageUnwritable,
            SasFabricAliasStore,
        )

        with tempfile.TemporaryDirectory() as raw:
            store = SasFabricAliasStore(Path(raw) / "aliases.json")
            with patch(
                "pathlib.Path.open",
                side_effect=PermissionError(errno.EACCES, "Permission denied"),
            ):
                with self.assertRaises(SasFabricAliasStorageUnwritable):
                    store._write({})


class TroubleshootingHostPathTests(unittest.TestCase):
    """The wiki must quote host paths; a container path there is an unrunnable command."""

    ROOT = Path(__file__).resolve().parents[1]
    COMPOSE_FILES = (
        "docker-compose.yml",
        "docker-compose.dev.yml",
        "docker-compose.nonroot.yml",
        "docker-compose.secrets.yml",
    )
    CHOWN = re.compile(r"chown\s+(?:-R\s+)?\d+:\d+\s+([^\n`]+)")
    BIND = re.compile(r"-\s+(\./[^:\s]+):(/[^:\s]+)")

    def _bind_sources(self) -> set[str]:
        """Host sides of every `./host:/app/container` bind in the compose files."""
        sources: set[str] = set()
        for name in self.COMPOSE_FILES:
            path = self.ROOT / name
            if not path.is_file():
                continue
            for host, _container in self.BIND.findall(path.read_text(encoding="utf-8")):
                sources.add(host)
        self.assertTrue(sources, "no compose bind mounts found")
        return sources

    def _chown_targets(self, text: str) -> list[str]:
        targets: list[str] = []
        for match in self.CHOWN.finditer(text):
            targets.extend(match.group(1).split())
        return targets

    def test_no_documented_chown_names_a_container_path(self) -> None:
        offenders: list[str] = []
        for page in sorted((self.ROOT / "wiki").glob("*.md")):
            for target in self._chown_targets(page.read_text(encoding="utf-8")):
                if target.startswith("/app/"):
                    offenders.append(f"{page.name}: {target}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_quoted_log_line_is_the_line_the_code_emits(self) -> None:
        quoted = " ".join(
            (self.ROOT / "wiki" / "Troubleshooting.md").read_text(encoding="utf-8").split()
        )
        checked = 0
        for container, host in CONTAINER_BIND_SOURCES:
            self.assertEqual(host_repair_path(container), host)
            if f"Cannot write to {container} " not in quoted:
                continue
            checked += 1
            self.assertIn(
                f"sudo chown -R 10001:10001 {host}",
                quoted,
                f"the quoted line for {container} does not name the host path {host}",
            )
        self.assertTrue(checked, "Troubleshooting.md no longer quotes the unwritable-directory line")

    def test_every_documented_chown_target_is_a_compose_bind_source(self) -> None:
        sources = self._bind_sources()
        text = (self.ROOT / "wiki" / "Troubleshooting.md").read_text(encoding="utf-8")
        unknown = [
            target
            for target in self._chown_targets(text)
            if not target.startswith("$") and "./" + target.lstrip("./") not in sources
        ]
        self.assertEqual(unknown, [], "\n".join(unknown))


if __name__ == "__main__":
    unittest.main()
