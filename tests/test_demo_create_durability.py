"""Demo creation keeps its profile when the config save was already published (#1229).

The config writer replaces ``config.yaml`` and then fsyncs the parent
directory. A failure in that last step means the new config is already
visible, so the demo profile it references must stay. Only a failure before
the replacement may roll the profile back.
"""
from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import yaml
from fastapi import HTTPException

# Must precede admin_service.main, which builds its app at import time.
import tests.admin_test_env  # noqa: F401
from admin_service.main import app as admin_app
from app.config import PathConfig, Settings
from app.models.domain import DemoSystemRequest
from app.secret_files import ReplacedDurabilityUncertainError
from app.services.demo_system_factory import DemoSystemFactory


def _directory_fsync_fault(directory: Path) -> tuple[list[int], object]:
    """Fail fsync only for ``directory``; every other fsync is real."""

    identity = (directory.stat().st_dev, directory.stat().st_ino)
    faults: list[int] = []
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        observed = os.fstat(descriptor)
        if stat.S_ISDIR(observed.st_mode) and (observed.st_dev, observed.st_ino) == identity:
            faults.append(descriptor)
            raise OSError(errno.EIO, "synthetic directory durability failure")
        return real_fsync(descriptor)

    return faults, fsync


def _referenced_profile_ids(config_path: Path) -> set[str]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    referenced: set[str] = set()
    for system in raw.get("systems") or []:
        if system.get("default_profile_id"):
            referenced.add(system["default_profile_id"])
        for view in system.get("storage_views") or []:
            if view.get("profile_id"):
                referenced.add(view["profile_id"])
    return referenced


def _saved_profile_ids(profile_path: Path) -> set[str]:
    if not profile_path.exists():
        return set()
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    return {profile["id"] for profile in raw.get("profiles") or []}


@unittest.skipIf(os.name != "posix", "the directory durability barrier is POSIX-only")
class DemoCreateDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config_dir = root / "config"
        self.profile_dir = root / "profiles"
        self.config_dir.mkdir()
        self.profile_dir.mkdir()
        self.config_path = self.config_dir / "config.yaml"
        self.profile_path = self.profile_dir / "profiles.yaml"
        self.config_path.write_text("systems: []\n", encoding="utf-8")
        self.settings = Settings(
            config_file=str(self.config_path),
            paths=PathConfig(profile_file=str(self.profile_path)),
        )

    def _create_with_config_directory_fault(self) -> tuple[list[int], BaseException]:
        factory = DemoSystemFactory(str(self.config_path), str(self.profile_path))
        faults, fsync = _directory_fsync_fault(self.config_dir)
        with patch("os.fsync", fsync):
            with self.assertRaises(OSError) as caught:
                factory.create_demo_system(DemoSystemRequest(system_id="demo-fault"), self.settings)
        return faults, caught.exception

    def test_published_config_keeps_a_new_demo_profile(self) -> None:
        faults, error = self._create_with_config_directory_fault()

        self.assertEqual(len(faults), 1)
        referenced = _referenced_profile_ids(self.config_path)
        self.assertEqual(referenced, {"demo-fault-chassis"})
        self.assertTrue(self.profile_path.exists(), "rollback removed a profile the visible config references")
        self.assertLessEqual(referenced, _saved_profile_ids(self.profile_path))
        self.assertIsInstance(error, ReplacedDurabilityUncertainError)

    def test_published_config_keeps_the_demo_profile_beside_existing_profiles(self) -> None:
        self.profile_path.write_text(
            yaml.safe_dump({"profiles": [{"id": "keep-me", "label": "Keep", "rows": 1, "columns": 1}]}),
            encoding="utf-8",
        )

        faults, error = self._create_with_config_directory_fault()

        self.assertEqual(len(faults), 1)
        referenced = _referenced_profile_ids(self.config_path)
        self.assertEqual(referenced, {"demo-fault-chassis"})
        self.assertEqual(_saved_profile_ids(self.profile_path), {"keep-me", "demo-fault-chassis"})
        self.assertIsInstance(error, ReplacedDurabilityUncertainError)

    def test_failure_before_config_replacement_still_rolls_the_profile_back(self) -> None:
        self.profile_path.write_text("# preserve bytes\nprofiles: []\n", encoding="utf-8")
        before = self.config_path.read_bytes(), self.profile_path.read_bytes()
        factory = DemoSystemFactory(str(self.config_path), str(self.profile_path))
        real_replace = os.replace

        def refuse_config_replace(source, destination, *args, **kwargs):
            if Path(destination) == self.config_path:
                raise OSError(errno.EIO, "synthetic replacement refusal")
            return real_replace(source, destination, *args, **kwargs)

        with patch("os.replace", refuse_config_replace):
            with self.assertRaisesRegex(OSError, "synthetic replacement refusal") as caught:
                factory.create_demo_system(DemoSystemRequest(system_id="demo-fault"), self.settings)

        self.assertNotIsInstance(caught.exception, ReplacedDurabilityUncertainError)
        self.assertEqual((self.config_path.read_bytes(), self.profile_path.read_bytes()), before)

    def test_failure_before_config_replacement_removes_a_new_profile_file(self) -> None:
        factory = DemoSystemFactory(str(self.config_path), str(self.profile_path))
        before = self.config_path.read_bytes()
        real_replace = os.replace

        def refuse_config_replace(source, destination, *args, **kwargs):
            if Path(destination) == self.config_path:
                raise OSError(errno.EIO, "synthetic replacement refusal")
            return real_replace(source, destination, *args, **kwargs)

        with patch("os.replace", refuse_config_replace):
            with self.assertRaisesRegex(OSError, "synthetic replacement refusal"):
                factory.create_demo_system(DemoSystemRequest(system_id="demo-fault"), self.settings)

        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.profile_path.exists())

    def test_route_reports_an_uncertain_save_without_claiming_nothing_was_saved(self) -> None:
        route = next(route for route in admin_app.routes if route.path == "/api/admin/system-setup/demo")
        faults, fsync = _directory_fsync_fault(self.config_dir)
        with patch("admin_service.routes.reload_app_settings", return_value=self.settings):
            with patch("os.fsync", fsync):
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(route.endpoint(DemoSystemRequest(system_id="demo-fault")))

        self.assertEqual(len(faults), 1)
        self.assertEqual(caught.exception.status_code, 503)
        detail = str(caught.exception.detail)
        self.assertIn("written", detail)
        self.assertIn("could not be confirmed", detail)
        self.assertIn("profile was kept", detail)
        self.assertLessEqual(_referenced_profile_ids(self.config_path), _saved_profile_ids(self.profile_path))


if __name__ == "__main__":
    unittest.main()
