from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import yaml

from admin_service.config import get_admin_settings
from app.config import ENV_OVERRIDES as APP_ENV_OVERRIDES
from app.config import AppConfig, build_unknown_config_key_warnings, get_settings
from app.config_errors import ConfigurationError
from app.read_ui_auth_config import load_read_ui_auth_settings
from history_service.config import get_history_settings

ROOT = Path(__file__).resolve().parents[1]




def _clear_loader_caches() -> None:
    get_settings.cache_clear()
    get_admin_settings.cache_clear()
    get_history_settings.cache_clear()


class _LoaderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _clear_loader_caches()

    def tearDown(self) -> None:
        _clear_loader_caches()

    @contextmanager
    def main_ui_environment(self, env: dict[str, str], yaml_text: str | None = None) -> Iterator[Path]:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config" / "config.yaml"
            config_path.parent.mkdir(parents=True)
            if yaml_text is not None:
                config_path.write_text(yaml_text, encoding="utf-8")
            with patch.dict(os.environ, {"APP_CONFIG_PATH": config_path.as_posix(), **env}, clear=True):
                _clear_loader_caches()
                yield config_path
            _clear_loader_caches()

    @contextmanager
    def history_environment(self, env: dict[str, str]) -> Iterator[Path]:
        with tempfile.TemporaryDirectory() as temp_dir:
            history_dir = Path(temp_dir) / "history"
            with patch.dict(
                os.environ,
                {"HISTORY_SQLITE_PATH": (history_dir / "history.db").as_posix(), **env},
                clear=True,
            ):
                _clear_loader_caches()
                yield history_dir
            _clear_loader_caches()


class BlankAndTextValueTests(_LoaderTestCase):
    def test_blank_main_ui_values_are_unset(self) -> None:
        env = {
            "APP_REFRESH_INTERVAL": "",
            "TRUENAS_TLS_CA_BUNDLE_PATH": "",
            "SSH_EXTRA_HOSTS_JSON": "   ",
            "APP_PUBLIC_ORIGIN": "",
        }
        with self.main_ui_environment(env):
            settings = get_settings()

        self.assertEqual(settings.app.refresh_interval_seconds, 30)
        self.assertIsNone(settings.truenas.tls_ca_bundle_path)
        self.assertEqual(settings.ssh.extra_hosts, [])
        self.assertIsNone(settings.app.public_origin)

    def test_text_values_stay_text(self) -> None:
        env = {"TRUENAS_HOST": "1234", "APP_LOG_LEVEL": "null", "TRUENAS_PLATFORM": " scale "}
        with self.main_ui_environment(env):
            settings = get_settings()

        self.assertEqual(settings.truenas.host, "1234")
        self.assertEqual(settings.app.log_level, "null")
        self.assertEqual(settings.truenas.platform, "scale")

    def test_blank_legacy_cache_ttl_does_not_claim_the_other_windows(self) -> None:
        with self.main_ui_environment({"APP_CACHE_TTL": ""}):
            settings = get_settings()

        self.assertEqual(settings.app.snapshot_cache_ttl_seconds, 10)
        self.assertEqual(settings.app.source_bundle_cache_ttl_seconds, 60)


class PlainConfigurationErrorTests(_LoaderTestCase):
    def test_bad_admin_integer_names_the_variable_and_exits_non_zero(self) -> None:
        with (
            patch.dict(os.environ, {"ADMIN_AUTO_STOP_SECONDS": "3600.0"}, clear=True),
            self.assertRaises(ConfigurationError) as captured,
        ):
            get_admin_settings()

        # A ValueError, so every fail-closed caller that expects a rejected setting still sees one.
        self.assertIsInstance(captured.exception, ValueError)
        self.assertIsNone(captured.exception.__cause__)
        self.assertTrue(captured.exception.__suppress_context__)
        self.assertEqual(
            str(captured.exception),
            "Configuration error: ADMIN_AUTO_STOP_SECONDS in .env must be a whole number of seconds "
            "(0 disables auto-stop).",
        )
        self.assertNotIn("pydantic", str(captured.exception))

    def test_bad_admin_choice_lists_the_allowed_values(self) -> None:
        with (
            patch.dict(os.environ, {"ADMIN_AUTH_MODE": "token"}, clear=True),
            self.assertRaises(ConfigurationError) as captured,
        ):
            get_admin_settings()

        self.assertEqual(
            str(captured.exception),
            "Configuration error: ADMIN_AUTH_MODE in .env must be one of network, basic.",
        )

    def test_bad_main_ui_integer_names_the_variable(self) -> None:
        with (
            self.main_ui_environment({"APP_REFRESH_INTERVAL": "30s"}),
            self.assertRaises(ConfigurationError) as captured,
        ):
            get_settings()

        self.assertEqual(
            str(captured.exception),
            "Configuration error: APP_REFRESH_INTERVAL in .env must be a whole number.",
        )

    def test_bad_yaml_choice_names_the_key_path_and_file(self) -> None:
        yaml_text = "systems:\n  - id: primary\n    truenas:\n      platform: freenas\n"
        with (
            self.main_ui_environment({}, yaml_text) as config_path,
            self.assertRaises(ConfigurationError) as captured,
        ):
            get_settings()

        self.assertEqual(
            str(captured.exception),
            f"Configuration error: systems[0].truenas.platform in {Path(config_path)} must be one of "
            "core, scale, linux, quantastor, esxi, ipmi.",
        )
        self.assertNotIn("errors.pydantic.dev", str(captured.exception))

    def test_read_ui_auth_error_is_plain(self) -> None:
        with (
            patch.dict(os.environ, {"ADMIN_AUTH_MODE": "basic"}, clear=True),
            self.assertRaises(ConfigurationError) as captured,
        ):
            load_read_ui_auth_settings()

        self.assertEqual(
            str(captured.exception),
            "Configuration error: ADMIN_AUTH_MODE=basic requires non-empty ADMIN_AUTH_USERNAME and "
            "ADMIN_AUTH_PASSWORD.",
        )


class HistoryBindTests(_LoaderTestCase):
    EXPECTED = (
        "Configuration error: HISTORY_BIND_ADDRESS is not loopback. Set HISTORY_REFRESH_AUTH_MODE=token, "
        "HISTORY_REFRESH_TOKEN (or _FILE) and HISTORY_PUBLIC_ORIGIN, or set it back to 127.0.0.1."
    )

    def test_non_loopback_published_bind_names_the_fix(self) -> None:
        with (
            self.history_environment({"HISTORY_PUBLISHED_BIND_ADDRESS": "0.0.0.0"}),
            self.assertRaises(ConfigurationError) as captured,
        ):
            get_history_settings()

        self.assertEqual(str(captured.exception), self.EXPECTED)

class UnknownConfigKeyTests(_LoaderTestCase):
    YAML = (
        "unknown_top: 1\n"
        "truenas:\n"
        "  host: https://truenas.example.test\n"
        "  bogus_field: 1\n"
        "systems:\n"
        "  - id: primary\n"
        "    truenas:\n"
        "      verfiy_ssl: true\n"
    )

    def test_unknown_keys_warn_at_load_and_reach_the_admin_banner(self) -> None:
        with self.main_ui_environment({}, self.YAML), self.assertLogs("app.config", level="WARNING") as logs:
            settings = get_settings()
            warnings = build_unknown_config_key_warnings(settings)

        self.assertEqual(
            logs.output,
            [
                "WARNING:app.config:config.yaml: unknown key `unknown_top` is ignored.",
                "WARNING:app.config:config.yaml: unknown key `truenas.bogus_field` is ignored.",
                "WARNING:app.config:config.yaml: unknown key `systems[0].truenas.verfiy_ssl` is ignored; "
                "did you mean `systems[0].truenas.verify_ssl`?",
            ],
        )
        self.assertEqual(
            [warning["key"] for warning in warnings],
            ["unknown_top", "truenas.bogus_field", "systems[0].truenas.verfiy_ssl"],
        )
        self.assertEqual(warnings[0]["code"], "unknown_config_key")
        self.assertEqual(warnings[1]["message"], "config.yaml: unknown key `truenas.bogus_field` is ignored.")

    def test_app_verify_ssl_is_no_longer_a_setting(self) -> None:
        self.assertNotIn("verify_ssl", AppConfig.model_fields)
        self.assertNotIn("APP_VERIFY_SSL", APP_ENV_OVERRIDES)
        example = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("verify_ssl", example["app"])

        with self.main_ui_environment({}, "app:\n  verify_ssl: true\n"), self.assertLogs("app.config") as logs:
            get_settings()
        self.assertEqual(logs.output, ["WARNING:app.config:config.yaml: unknown key `app.verify_ssl` is ignored."])

    def test_unknown_key_names_the_closest_valid_key(self) -> None:
        yaml_text = "backup:\n  full:\n    enabled: true\nhistroy:\n  timeout_seconds: 5\n"
        with self.main_ui_environment({}, yaml_text), self.assertLogs("app.config", level="WARNING") as logs:
            settings = get_settings()
            warnings = build_unknown_config_key_warnings(settings)

        self.assertEqual(
            logs.output,
            [
                "WARNING:app.config:config.yaml: unknown key `backup` is ignored; did you mean `backups`?",
                "WARNING:app.config:config.yaml: unknown key `histroy` is ignored; did you mean `history`?",
            ],
        )
        self.assertEqual([warning["key"] for warning in warnings], ["backup", "histroy"])

    def test_backups_section_keys_are_checked_against_the_scheduler_policy(self) -> None:
        yaml_text = (
            "backups:\n"
            "  config:\n"
            "    enabled: true\n"
            "    debounce_seconds: 30\n"
            "  full:\n"
            "    enabled: true\n"
            "    shedule: '0 3 * * *'\n"
            "  targets:\n"
            "    - target_id: office-nas\n"
            "      label: Office NAS\n"
            "      provider: sftp\n"
            "      hostnme: nas.example.test\n"
            "      root: /srv/backups/jbod\n"
            "  retention: 5\n"
        )
        with self.main_ui_environment({}, yaml_text), self.assertLogs("app.config", level="WARNING") as logs:
            settings = get_settings()

        self.assertEqual(
            logs.output,
            [
                "WARNING:app.config:config.yaml: unknown key `backups.retention` is ignored.",
                "WARNING:app.config:config.yaml: unknown key `backups.full.shedule` is ignored; "
                "did you mean `backups.full.schedule`?",
                "WARNING:app.config:config.yaml: unknown key `backups.targets[0].hostnme` is ignored; "
                "did you mean `backups.targets[0].hostname`?",
            ],
        )
        # Warn only: the main UI still starts and a valid key elsewhere still applies.
        self.assertEqual(settings.app.port, 8080)

    def test_backups_key_lists_match_the_scheduler_models(self) -> None:
        from dataclasses import fields

        from app.config import BACKUPS_SECTION_KEYS
        from history_service.backup_archive.policy import ConfigClassPolicy, FullClassPolicy, _PolicyDocument
        from history_service.backup_archive.settings import ArchiveTargetSettings

        self.assertEqual(BACKUPS_SECTION_KEYS[""], set(_PolicyDocument.model_fields))
        self.assertEqual(BACKUPS_SECTION_KEYS["config"], set(ConfigClassPolicy.model_fields))
        self.assertEqual(BACKUPS_SECTION_KEYS["full"], set(FullClassPolicy.model_fields))
        self.assertEqual(
            BACKUPS_SECTION_KEYS["targets"],
            {field.name for field in fields(ArchiveTargetSettings)} | {"label", "enabled"},
        )

    def test_every_documented_backups_key_is_known(self) -> None:
        from app.config import collect_unknown_config_keys
        from history_service.backup_archive import policy

        example = policy.__doc__.split("::", 1)[1].split("\n\n")[1]
        documented = yaml.safe_load(example)
        self.assertIn("targets", documented["backups"])
        self.assertEqual(collect_unknown_config_keys(documented), [])


if __name__ == "__main__":
    unittest.main()
