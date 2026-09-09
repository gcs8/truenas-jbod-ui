from __future__ import annotations

import os
import re
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import yaml

from admin_service.config import ENV_OVERRIDES as ADMIN_ENV_OVERRIDES
from admin_service.config import get_admin_settings
from app.config import ENV_OVERRIDES as APP_ENV_OVERRIDES
from app.config import AppConfig, build_unknown_config_key_warnings, get_settings
from app.config_errors import ConfigurationError
from app.read_ui_auth_settings import ENV_OVERRIDES as READ_UI_ENV_OVERRIDES
from app.read_ui_auth_settings import get_read_ui_auth_settings
from history_service.config import ENV_OVERRIDES as HISTORY_ENV_OVERRIDES
from history_service.config import get_history_settings

ROOT = Path(__file__).resolve().parents[1]
ENV_LINE = re.compile(r"^#?\s*(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")

# Read by a service but wired by the Compose files or only meaningful to developers,
# so .env.example does not list them on purpose.
INTERNAL_ENV_KEYS = frozenset(
    {
        "APP_HOST",
        "APP_DISK_INVENTORY_SYNC_POLL_INTERVAL_SECONDS",
        "HISTORY_BACKEND_URL",
        "HISTORY_BACKEND_FALLBACK_CONCURRENCY",
        "ADMIN_SERVICE_URL",
        "PATH_SAS_FABRIC_ALIAS_FILE",
        "ADMIN_APP_NAME",
        "ADMIN_HOST",
        "ADMIN_CONTAINER_UI_NAME",
        "ADMIN_CONTAINER_HISTORY_NAME",
        "ADMIN_CONTAINER_ADMIN_NAME",
        "ADMIN_CONTAINER_CONTROL_TIMEOUT_SECONDS",
        "HISTORY_HOST",
        "HISTORY_SOURCE_BASE_URL",
        "HISTORY_SQLITE_PATH",
    }
)


def _env_example_text() -> str:
    return (ROOT / ".env.example").read_text(encoding="utf-8")


def _env_example_lines() -> list[tuple[str, str, bool]]:
    """Return (key, value, active) for every assignment line, commented ones included."""
    lines: list[tuple[str, str, bool]] = []
    for raw_line in _env_example_text().splitlines():
        match = ENV_LINE.match(raw_line.strip())
        if match is None:
            continue
        lines.append((match.group("key"), match.group("value").strip(), not raw_line.lstrip().startswith("#")))
    return lines


def _env_example_active_values() -> dict[str, str]:
    return {key: value for key, value, active in _env_example_lines() if active}


def _clear_loader_caches() -> None:
    get_settings.cache_clear()
    get_admin_settings.cache_clear()
    get_history_settings.cache_clear()
    get_read_ui_auth_settings.cache_clear()


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

    def test_blank_admin_and_history_values_are_unset(self) -> None:
        with patch.dict(
            os.environ,
            {"ADMIN_HOST_PREP_MAX_PACKAGES": "", "ADMIN_AUTO_STOP_SECONDS": ""},
            clear=True,
        ):
            admin_settings = get_admin_settings()
        self.assertEqual(admin_settings.host_prep_max_packages, 8)
        self.assertEqual(admin_settings.auto_stop_seconds, 0)

        with self.history_environment({"HISTORY_POLL_INTERVAL_SECONDS": "", "HISTORY_PUBLIC_ORIGIN": ""}):
            history_settings = get_history_settings()
        self.assertEqual(history_settings.poll_interval_seconds, 300)
        self.assertIsNone(history_settings.public_origin)

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

        self.assertIsInstance(captured.exception, SystemExit)
        self.assertEqual(
            str(captured.exception),
            "Configuration error: ADMIN_AUTO_STOP_SECONDS in .env must be a whole number of seconds "
            "(0 disables auto-stop).",
        )
        self.assertNotIn("pydantic", str(captured.exception))
        self.assertTrue(str(captured.exception.code).startswith("Configuration error:"))

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
            get_read_ui_auth_settings()

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

    def test_bind_address_is_used_when_the_published_address_is_unset(self) -> None:
        with (
            self.history_environment({"HISTORY_BIND_ADDRESS": "0.0.0.0", "HISTORY_PUBLISHED_BIND_ADDRESS": ""}),
            self.assertRaises(ConfigurationError) as captured,
        ):
            get_history_settings()
        self.assertEqual(str(captured.exception), self.EXPECTED)

        with self.history_environment({"HISTORY_BIND_ADDRESS": "localhost"}):
            self.assertEqual(get_history_settings().published_bind_address, "localhost")

    def test_fast_interval_follows_the_poll_interval_unless_set(self) -> None:
        with self.history_environment({"HISTORY_POLL_INTERVAL_SECONDS": "60"}):
            self.assertEqual(get_history_settings().fast_interval_seconds, 60)

        with self.history_environment({"HISTORY_POLL_INTERVAL_SECONDS": "60", "HISTORY_FAST_INTERVAL_SECONDS": "120"}):
            self.assertEqual(get_history_settings().fast_interval_seconds, 120)


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
                "WARNING:app.config:config.yaml: unknown key `systems[0].truenas.verfiy_ssl` is ignored.",
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


class EnvExampleTests(_LoaderTestCase):
    def test_env_example_loads_everywhere_with_an_unwritable_host_prep_folder(self) -> None:
        text = _env_example_text()
        self.assertIn("Copy only the lines you change; blank values are ignored", text)
        values = _env_example_active_values()
        self.assertNotIn("ADMIN_HOST_PREP_TEMP_DIR", values)
        self.assertNotIn("HISTORY_PUBLISHED_BIND_ADDRESS", values)
        self.assertNotIn("LOG_SYSLOG_ADDRESS", values)
        self.assertNotIn("HISTORY_BACKEND_FALLBACK_CONCURRENCY", text)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blocker = root / "blocker.txt"
            blocker.write_text("not a folder\n", encoding="utf-8")
            unwritable_host_prep = blocker / "host-prep"
            env = {
                **values,
                "APP_CONFIG_PATH": (root / "config" / "config.yaml").as_posix(),
                "HISTORY_SQLITE_PATH": (root / "history" / "history.db").as_posix(),
                "HISTORY_BACKUP_DIR": (root / "history" / "backups").as_posix(),
                "HISTORY_LONG_TERM_BACKUP_DIR": (root / "history" / "backups" / "long-term").as_posix(),
                "ADMIN_HOST_PREP_TEMP_DIR": unwritable_host_prep.as_posix(),
            }
            with patch.dict(os.environ, env, clear=True):
                _clear_loader_caches()
                settings = get_settings()
                admin_settings = get_admin_settings()
                history_settings = get_history_settings()
                read_ui_settings = get_read_ui_auth_settings()
            _clear_loader_caches()

            self.assertEqual(admin_settings.host_prep_temp_dir, unwritable_host_prep.as_posix())
            self.assertFalse(unwritable_host_prep.exists())

        self.assertEqual(settings.app.refresh_interval_seconds, 30)
        self.assertEqual(settings.truenas.platform, "core")
        self.assertEqual(read_ui_settings.auth_mode, "network")
        self.assertIsNone(read_ui_settings.public_origin)
        self.assertEqual(history_settings.published_bind_address, "127.0.0.1")
        self.assertEqual(history_settings.refresh_auth_mode, "network")

    def test_every_env_override_is_documented_or_listed_as_internal(self) -> None:
        documented = {key for key, _value, _active in _env_example_lines()}
        overrides = (
            set(APP_ENV_OVERRIDES)
            | set(ADMIN_ENV_OVERRIDES)
            | set(HISTORY_ENV_OVERRIDES)
            | set(READ_UI_ENV_OVERRIDES)
        )
        self.assertEqual(sorted(overrides - documented - INTERNAL_ENV_KEYS), [])
        self.assertEqual(sorted(INTERNAL_ENV_KEYS & documented), [])
        self.assertEqual(sorted(INTERNAL_ENV_KEYS - overrides), [])

    def test_main_ui_code_never_imports_the_admin_service(self) -> None:
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "app").rglob("*.py")
            if "admin_service" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_compose_forwards_the_release_check_to_the_history_service(self) -> None:
        expected = {
            "RELEASE_CHECK_ENABLED": "${RELEASE_CHECK_ENABLED:-true}",
            "RELEASE_CHECK_REPO": "${RELEASE_CHECK_REPO:-gcs8/truenas-jbod-ui}",
            "RELEASE_CHECK_INTERVAL_SECONDS": "${RELEASE_CHECK_INTERVAL_SECONDS:-86400}",
            "RELEASE_CHECK_TIMEOUT_SECONDS": "${RELEASE_CHECK_TIMEOUT_SECONDS:-5}",
        }
        for filename in ("docker-compose.yml", "docker-compose.dev.yml"):
            document = yaml.safe_load((ROOT / filename).read_text(encoding="utf-8"))
            environment = document["services"]["enclosure-history"]["environment"]
            with self.subTest(compose=filename):
                for key, value in expected.items():
                    self.assertEqual(environment.get(key), value, key)


if __name__ == "__main__":
    unittest.main()
