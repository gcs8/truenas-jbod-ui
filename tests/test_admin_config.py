from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from admin_service.config import get_admin_settings
from app.config_errors import ConfigurationError


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

    def test_host_prep_aggregate_quota_defaults_are_bounded(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("admin_service.config.Path.mkdir"),
        ):
            settings = get_admin_settings()

        self.assertEqual(settings.host_prep_max_packages, 8)
        self.assertEqual(settings.host_prep_max_bytes, 2 * 1024 * 1024 * 1024)

    def test_host_prep_aggregate_quota_accepts_explicit_positive_limits(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "ADMIN_HOST_PREP_MAX_PACKAGES": "3",
                    "ADMIN_HOST_PREP_MAX_BYTES": "1073741824",
                },
                clear=True,
            ),
            patch("admin_service.config.Path.mkdir"),
        ):
            settings = get_admin_settings()

        self.assertEqual(settings.host_prep_max_packages, 3)
        self.assertEqual(settings.host_prep_max_bytes, 1073741824)

    def test_blank_non_text_env_values_keep_defaults(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "ADMIN_CONTAINER_VERSION_PROBE_TIMEOUT_SECONDS": "",
                    "ADMIN_PORT": "  ",
                    "ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT": "",
                },
                clear=True,
            ),
            patch("admin_service.config.Path.mkdir"),
        ):
            settings = get_admin_settings()

        self.assertEqual(settings.container_version_probe_timeout_seconds, 1.5)
        self.assertEqual(settings.port, 8002)
        self.assertFalse(settings.allow_plaintext_backup_export)

    def test_non_numeric_env_value_still_raises(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"ADMIN_CONTAINER_VERSION_PROBE_TIMEOUT_SECONDS": "soon"},
                clear=True,
            ),
            patch("admin_service.config.Path.mkdir"),
        ):
            with self.assertRaises(ConfigurationError) as caught:
                get_admin_settings()

        self.assertIn("ADMIN_CONTAINER_VERSION_PROBE_TIMEOUT_SECONDS", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
