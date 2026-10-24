from __future__ import annotations

import errno
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.storage_writability import (
    CONTAINER_BIND_SOURCES,
    StorageDirectoryUnwritable,
    describe_capacity_failure,
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


class HistoryCapacityFailureTests(unittest.TestCase):
    def test_capacity_failures_name_the_volume_and_space_or_inode_recovery(self) -> None:
        from history_service.startup import recorded_startup_failure

        for error, space in (
            (OSError(errno.ENOSPC, "No space left on device"), None),
            (sqlite3.OperationalError("database or disk is full"), None),
            (sqlite3.OperationalError("unable to open database file"),
             SimpleNamespace(f_bavail=1024, f_files=256, f_favail=0)),
            (sqlite3.OperationalError("unable to open database file"),
             SimpleNamespace(f_bavail=0, f_files=256, f_favail=128)),
        ):
            with self.subTest(error=str(error), space=space):
                def factory() -> object:
                    raise error

                with patch("app.services.storage_writability.os.statvfs", return_value=space, create=True):
                    with self.assertRaises((HistoryStartupError, OSError, sqlite3.OperationalError)) as raised:
                        open_history_store_with_retries(factory, directory="/app/history", attempts=1)
                self.assertIsInstance(raised.exception, HistoryStartupError)
                reason = raised.exception.reason
                self.assertIn("filesystem is full", reason)
                self.assertIn("free space or inodes", reason)
                self.assertIn("volume containing ./history", reason)
                self.assertNotIn("chown", reason)
                self.assertIs(raised.exception.__cause__, error)
                self.assertEqual(recorded_startup_failure(), reason)

    def test_explicit_permission_errors_keep_the_exact_ownership_remedy(self) -> None:
        for error in (
            PermissionError(errno.EACCES, "denied"),
            PermissionError(errno.EPERM, "denied"),
            OSError(errno.EROFS, "read-only filesystem"),
            sqlite3.OperationalError("attempt to write a readonly database"),
        ):
            with self.subTest(error=str(error)):
                def factory() -> object:
                    raise error

                with (
                    patch("app.services.storage_writability.os.statvfs", create=True) as statvfs,
                    patch("app.services.storage_writability._running_identity", return_value=(10001, 10001)),
                    patch("app.services.storage_writability.Path.stat", return_value=SimpleNamespace(st_uid=0)),
                ):
                    with self.assertRaises(HistoryStartupError) as raised:
                        open_history_store_with_retries(factory, directory="/app/history", attempts=1)
                self.assertEqual(raised.exception.reason,
                                 "Cannot write to /app/history (owned by uid 0, running as uid 10001). "
                                 "On the Docker host run: sudo chown -R 10001:10001 ./history")
                statvfs.assert_not_called()

    def test_sqlite_open_failure_needs_capacity_evidence_before_replacing_ownership(self) -> None:
        def factory() -> object:
            raise sqlite3.OperationalError("unable to open database file")

        for probe in (
            {"return_value": SimpleNamespace(f_bavail=1024, f_files=256, f_favail=128)},
            {"return_value": SimpleNamespace(f_bavail=1024, f_files=0, f_favail=0)},
            {"side_effect": OSError(errno.EACCES, "cannot inspect volume")},
            {"new": None},
        ):
            with self.subTest(probe=probe):
                with patch("app.services.storage_writability.os.statvfs", create=True, **probe):
                    with self.assertRaises(HistoryStartupError) as raised:
                        open_history_store_with_retries(factory, directory="/app/history", attempts=1)
                self.assertEqual(raised.exception.reason, describe_unwritable_directory("/app/history"))


class CapacityDiagnosisRegressionTests(unittest.TestCase):
    def test_missing_quota_errno_preserves_capacity_and_unrelated_errors(self) -> None:
        # errno constants are platform-dependent; quota support is optional.
        with patch.dict(vars(errno)):
            if hasattr(errno, "EDQUOT"):
                delattr(errno, "EDQUOT")
            for error in (PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "io"), OSError("unknown")):
                with self.subTest(error=type(error).__name__):
                    self.assertIsNone(describe_capacity_failure(error, "/app/data"))
            reason = describe_capacity_failure(OSError(errno.ENOSPC, "full"), "/app/data")
            self.assertIsNotNone(reason)
            assert reason is not None
            self.assertIn("filesystem is full", reason)

    def test_read_only_statvfs_never_proves_ambiguous_sqlite_capacity(self) -> None:
        error = sqlite3.OperationalError("unable to open database file")
        for blocks, inodes in ((0, 128), (1024, 0), (0, 0)):
            with self.subTest(blocks=blocks, inodes=inodes), patch(
                "app.services.storage_writability.os.statvfs", create=True,
                return_value=SimpleNamespace(
                    f_flag=getattr(os, "ST_RDONLY", 1), f_bavail=blocks,
                    f_files=256, f_favail=inodes,
                ),
            ):
                self.assertIsNone(describe_capacity_failure(error, "/app/history"))
                with self.assertRaises(HistoryStartupError) as raised:
                    open_history_store_with_retries(
                        lambda: (_ for _ in ()).throw(error),
                        directory="/app/history", attempts=1,
                    )
                self.assertEqual(raised.exception.reason, describe_unwritable_directory("/app/history"))

    def test_explicit_capacity_needs_no_statvfs_and_ui_remedy_has_no_history_restart(self) -> None:
        for number in (errno.ENOSPC, errno.EDQUOT):
            with self.subTest(errno=number), patch(
                "app.services.storage_writability.os.statvfs", create=True,
                side_effect=AssertionError("explicit errno needs no filesystem inference"),
            ):
                reason = describe_capacity_failure(OSError(number, "private failure"), "/app/data")
                self.assertIsNotNone(reason)
                assert reason is not None
                self.assertIn("free space or inodes", reason)
                self.assertIn("./data", reason)
                self.assertNotIn("chown", reason)
                self.assertNotIn("restart the history", reason)
                self.assertNotIn("private failure", reason)

    def test_real_history_store_open_failure_uses_capacity_or_read_only_fallback(self) -> None:
        from history_service.store import HistoryStore

        # Redirect the real store's SQLite open to a synthetic missing parent;
        # SQLite itself supplies the ambiguous open error, not a canned exception.
        real_connect = sqlite3.connect
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "history.sqlite"
            database.touch()
            before = database.read_bytes()
            missing = Path(raw) / "missing" / "history.sqlite"
            for readonly in (False, True):
                with self.subTest(readonly=readonly), patch(
                    "app.services.storage_writability.os.statvfs", create=True,
                    return_value=SimpleNamespace(
                        f_flag=getattr(os, "ST_RDONLY", 1) if readonly else 0,
                        f_bavail=0, f_files=256, f_favail=0,
                    ),
                ), patch(
                    "history_service.store.sqlite3.connect",
                    side_effect=lambda *args, **kwargs: real_connect(str(missing)),
                ) as connect:
                    with self.assertRaises(HistoryStartupError) as raised:
                        open_history_store_with_retries(
                            lambda: HistoryStore(str(database)), directory=database.parent, attempts=1,
                        )
                    self.assertTrue(connect.called)
                    self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)
                    if readonly:
                        self.assertEqual(raised.exception.reason, describe_unwritable_directory(database.parent))
                    else:
                        self.assertIn("free space or inodes", raised.exception.reason)
                        self.assertIn("restart the history service", raised.exception.reason)
                    self.assertEqual(database.read_bytes(), before)


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

    def test_capacity_failure_stays_up_and_reaches_health_with_its_recovery_remedy(self) -> None:
        import asyncio
        import json
        from history_service import main as history_main

        with patch.object(history_main, "get_history_settings", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertLogs("history_service.startup", level="WARNING"):
                settings, store, reason = history_main.load_history_runtime(
                    attempts=1, initial_backoff_seconds=0.0)
        self.assertIsNone(store)
        self.assertIsNotNone(settings)
        self.assertIn("filesystem is full", reason)
        self.assertIn("free space or inodes", reason)
        self.assertNotIn("chown", reason)
        with (
            patch.object(history_main, "startup_failure_reason", reason),
            patch.object(history_main, "collector", None),
            patch.object(history_main, "store", None),
        ):
            response = asyncio.run(history_main.healthz())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body), {
            "status": "down", "detail": history_main.HISTORY_UNAVAILABLE_DETAIL, "reason": reason,
        })


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
                side_effect=OSError(errno.EIO, "I/O error"),
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
