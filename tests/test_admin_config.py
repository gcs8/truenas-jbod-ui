from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from admin_service.config import get_admin_settings


class AdminSettingsHostPrepTempDirTests(unittest.TestCase):
    def setUp(self) -> None:
        get_admin_settings.cache_clear()

    def tearDown(self) -> None:
        get_admin_settings.cache_clear()

    def test_host_prep_temp_dir_defaults_under_tmpdir(self) -> None:
        with (
            patch.dict(os.environ, {"TMPDIR": "/app/history"}, clear=True),
            patch("admin_service.config.Path.mkdir"),
        ):
            settings = get_admin_settings()

        self.assertEqual(
            settings.host_prep_temp_dir,
            str(Path("/app/history") / "truenas-jbod-ui-host-prep"),
        )

    def test_explicit_host_prep_temp_dir_overrides_tmpdir_default(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "TMPDIR": "/app/history",
                    "ADMIN_HOST_PREP_TEMP_DIR": "/srv/host-prep",
                },
                clear=True,
            ),
            patch("admin_service.config.Path.mkdir"),
        ):
            settings = get_admin_settings()

        self.assertEqual(settings.host_prep_temp_dir, "/srv/host-prep")

    def test_host_prep_stale_ttl_defaults_to_24_hours(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("admin_service.config.Path.mkdir"),
        ):
            settings = get_admin_settings()

        self.assertEqual(settings.host_prep_stale_ttl_seconds, 24 * 60 * 60)

    def test_host_prep_stale_ttl_accepts_zero_to_disable_pruning(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"ADMIN_HOST_PREP_STALE_TTL_SECONDS": "0"},
                clear=True,
            ),
            patch("admin_service.config.Path.mkdir"),
        ):
            settings = get_admin_settings()

        self.assertEqual(settings.host_prep_stale_ttl_seconds, 0)


if __name__ == "__main__":
    unittest.main()
