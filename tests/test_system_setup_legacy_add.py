"""Adding a system next to a legacy top-level system must not drop it (#1227).

A config without a nonempty ``systems`` list still monitors one system built
from the top-level ``truenas`` and ``ssh`` sections (and their environment
overrides, on the model's default host when none is set). Writing a
``systems`` list replaces that implicit system, so an add that starts the list
from empty silently stops monitoring it. System Setup and demo creation refuse
every add while that system exists, whatever id the request carries,
``default`` included. A real edit of ``default`` (``replace_existing``) starts
from the system in effect and keeps every field the form does not own; a key
or password the form keeps is saved with the value the configuration resolved,
including one that comes from the environment.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml
from fastapi import FastAPI

import admin_service.routes as admin_routes
import app.secret_files as secret_files
from admin_service.config import AdminSettings
from admin_service.route_support import serialize_systems
from app.config import ENV_OVERRIDES, TrueNASConfig, get_settings, load_settings
from app.models.domain import DemoSystemRequest, SystemSetupRequest
from app.router_inclusion import include_router_preserving_route_objects
from app.services.demo_system_factory import DemoSystemFactory
from app.services.system_setup import SystemSetupService

# The fixed refusals, pinned here so a reworded message is a reviewed change.
_LEGACY_SYSTEM_PREFIX = (
    "This config monitors one system ('default') from its legacy top-level truenas "
    "and ssh settings, and adding "
)
LEGACY_SYSTEM_MIGRATION_STEP = (
    "Save 'default' in System Setup first, or move it into a systems entry with id "
    "'default' in config.yaml with the values in effect; either way its API key and "
    "passwords are then stored in config.yaml. See \"Moving a single system into "
    "systems\" on the Advanced Configuration wiki page."
)
LEGACY_SYSTEM_MIGRATION_DETAIL = (
    _LEGACY_SYSTEM_PREFIX + "a system now would replace it. "
    + LEGACY_SYSTEM_MIGRATION_STEP + " Then add this one."
)
LEGACY_DEMO_DETAIL = (
    _LEGACY_SYSTEM_PREFIX + "a demo now would replace it. "
    + LEGACY_SYSTEM_MIGRATION_STEP + " Then add the demo."
)
SECRET_FILE_READABLE_NOTE = (
    "config.yaml now holds a key or password that came from the environment, and "
    "accounts other than the file's owner can read it. Keep the file readable only "
    "by its owner and the app's own services."
)
SECRET_FILE_READABLE_LOG = (
    "Saved a key or password from the environment into config.yaml, which group or "
    "other accounts can read."
)
LEGACY_SECTIONS_INVALID_DETAIL = (
    "The top-level truenas or ssh settings in config.yaml are not valid, so the "
    "'default' system cannot be saved from them."
)
WIKI_MIGRATION_HEADING = "## Moving a single system into systems"
PRESERVE = "__TRUENAS_JBOD_KEEP_EXISTING_VALUE__"

# Environment overrides that feed the implicit legacy system (every TRUENAS_
# and SSH_ override and its _FILE form), plus the config path. Each test starts
# with none of them set so the host shell cannot leak in.
_SYSTEM_ENV = (
    "APP_CONFIG_PATH",
    "SYSTEM_DEFAULT_ID",
    "BACKUP_CONFIG_ENABLED",
    *(name for name in ENV_OVERRIDES if name.startswith(("TRUENAS_", "SSH_"))),
    *(f"{name}_FILE" for name in ENV_OVERRIDES if name.startswith(("TRUENAS_", "SSH_"))),
)

LEGACY_CONFIG = {
    "truenas": {"host": "https://legacy.example.test", "platform": "core"},
    "ssh": {"enabled": True, "host": "legacy.example.test", "user": "jbodmap"},
}

# A working legacy system with settings the setup form does not show (API
# dialect, call limit, timeouts, extra hosts, trust file, command list) and
# file-held secrets. Every one of them must survive any save.
RICH_LEGACY_CONFIG = {
    "truenas": {
        "host": "https://legacy.example.test",
        "api_key": "synthetic-file-key",
        "platform": "scale",
        "api_dialect": "jsonrpc",
        "max_concurrent_calls": 3,
        "timeout_seconds": 91,
        "verify_ssl": True,
        "tls_server_name": "legacy.example.test",
    },
    "ssh": {
        "enabled": True,
        "host": "legacy.example.test",
        "extra_hosts": ["legacy-b.example.test"],
        "user": "jbodmap",
        "key_path": "/run/ssh/synthetic_key",
        "password": "synthetic-ssh-password",
        "known_hosts_path": "/srv/synthetic/known_hosts",
        "timeout_seconds": 33,
        "commands": ["zpool status -gP", "lsblk -OJ"],
    },
}


def _form_text(value) -> str | None:
    # admin.js sends `field.value.trim() || null` for its text inputs.
    return (value or "").strip() or None


def _loaded_form_payload(serialized: dict, **changes) -> dict:
    """The body the System Setup form posts for a loaded system saved as is.

    Field for field what ``collectSetupPayload`` in admin.js builds from the
    serialized system: text inputs trimmed, configured secrets as the
    keep-existing sentinel, the redacted command list as ``preserve`` from the
    loaded id, generated storage views left out, and ``replace_existing``
    because the id is the loaded one.
    """

    def secret(name: str):
        return PRESERVE if serialized.get(f"{name}_configured") else None

    payload = {
        "system_id": serialized["id"],
        "label": (serialized["label"] or "").strip(),
        "platform": serialized["platform"],
        "truenas_host": (serialized["truenas_host"] or "").strip(),
        "api_key": secret("api_key"),
        "api_user": _form_text(serialized["api_user"]),
        "api_password": secret("api_password"),
        "verify_ssl": serialized["verify_ssl"],
        "tls_ca_bundle_path": _form_text(serialized["tls_ca_bundle_path"]),
        "tls_server_name": _form_text(serialized["tls_server_name"]),
        "enclosure_filter": _form_text(serialized["enclosure_filter"]),
        "ssh_enabled": serialized["ssh_enabled"],
        "ssh_host": _form_text(serialized["ssh_host"]),
        "ha_enabled": False,
        "ha_nodes": [],
        "ssh_port": serialized["ssh_port"],
        "ssh_user": _form_text(serialized["ssh_user"]),
        "ssh_key_path": _form_text(serialized["ssh_key_path"]),
        "ssh_password": secret("ssh_password"),
        "ssh_sudo_password": secret("ssh_sudo_password"),
        "ssh_strict_host_key_checking": serialized["ssh_strict_host_key_checking"],
        "ssh_timeout_seconds": serialized["ssh_timeout_seconds"] or 15,
        **(
            {
                "ssh_commands": [],
                "ssh_commands_action": "preserve",
                "ssh_commands_source_system_id": serialized["id"],
            }
            if serialized["ssh_commands_redacted"]
            else {"ssh_commands": [], "ssh_commands_action": "replace"}
        ),
        "bmc_enabled": serialized["bmc_enabled"],
        "bmc_host": _form_text(serialized["bmc_host"]),
        "bmc_username": _form_text(serialized["bmc_username"]),
        "bmc_password": secret("bmc_password"),
        "bmc_verify_ssl": serialized["bmc_verify_ssl"],
        "bmc_timeout_seconds": serialized["bmc_timeout_seconds"] or 15,
        "default_profile_id": serialized["default_profile_id"] or None,
        "auto_storage_views": serialized["auto_storage_views"] is not False,
        "storage_views": [view for view in serialized["storage_views"] if not view.get("generated")],
        "view_order": list(serialized["view_order"]),
        "replace_existing": True,
        "clone_source_system_id": None,
        "make_default": serialized["is_default"],
    }
    payload.update(changes)
    return payload


async def _post(app: FastAPI, path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    messages: list[dict] = []
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()
        return {}

    async def send(message: dict) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"localhost"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 80),
        },
        receive,
        send,
    )
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(raw)


class _LegacyConfigMixin:
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config" / "config.yaml"
        self.config_path.parent.mkdir()
        environ = {key: value for key, value in os.environ.items() if key not in _SYSTEM_ENV}
        environ["APP_CONFIG_PATH"] = str(self.config_path)
        environ["BACKUP_CONFIG_ENABLED"] = "false"
        env_patch = mock.patch.dict(os.environ, environ, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)

    def _write(self, document: dict) -> bytes:
        self.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return self.config_path.read_bytes()

    def _effective_system_hosts(self) -> dict[str, str]:
        settings = load_settings(create_directories=False)
        return {system.id: system.truenas.host for system in settings.systems}

    def _raw_entry(self, system_id: str = "default") -> dict:
        document = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        return next(entry for entry in document["systems"] if entry["id"] == system_id)

    def _secret_file(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        path.chmod(0o600)
        return path

    def _set_env(self, environment: dict[str, str]) -> None:
        for env_name, env_value in environment.items():
            os.environ[env_name] = env_value
            self.addCleanup(os.environ.pop, env_name, None)

    def _capture_logs(self) -> list[logging.LogRecord]:
        records: list[logging.LogRecord] = []

        class _Collector(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collector(level=logging.DEBUG)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

        def restore() -> None:
            root.removeHandler(handler)
            root.setLevel(previous)

        self.addCleanup(restore)
        return records

    @staticmethod
    def _log_text(records: list[logging.LogRecord]) -> str:
        parts = []
        for record in records:
            parts.append(record.getMessage())
            if record.exc_info:
                parts.append(logging.Formatter().formatException(record.exc_info))
        return "\n".join(parts)

    @staticmethod
    def _request(system_id: str, host: str = "https://new.example.test") -> SystemSetupRequest:
        return SystemSetupRequest(system_id=system_id, label=system_id, truenas_host=host)


class LegacySystemAddRouteTests(_LegacyConfigMixin, unittest.TestCase):
    """The registered admin route, real config load and real config writer."""

    def setUp(self) -> None:
        super().setUp()
        self.app = FastAPI()
        include_router_preserving_route_objects(self.app, admin_routes.build_router(AdminSettings()))

        async def no_runtime(*args, **kwargs):
            return {"available": False, "containers": []}

        # Only runtime-control reporting is replaced, so no container socket is reached.
        for patcher in (
            mock.patch.object(
                admin_routes,
                "get_runtime_service",
                lambda: SimpleNamespace(mark_restart_required=lambda keys: None),
            ),
            mock.patch.object(admin_routes, "build_runtime_payload", no_runtime),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post_setup(self, payload: dict) -> tuple[int, dict]:
        return asyncio.run(_post(self.app, "/api/admin/system-setup", payload))

    def test_adding_a_system_to_a_legacy_config_is_refused_and_writes_nothing(self) -> None:
        before_bytes = self._write(LEGACY_CONFIG)
        self.assertEqual(self._effective_system_hosts(), {"default": "https://legacy.example.test"})

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )

        self.assertEqual(status, 400, response)
        self.assertEqual(response.get("detail"), LEGACY_SYSTEM_MIGRATION_DETAIL)
        self.assertEqual(self.config_path.read_bytes(), before_bytes)
        self.assertEqual(self._effective_system_hosts(), {"default": "https://legacy.example.test"})

    def test_saving_the_legacy_system_then_adding_keeps_both_monitored(self) -> None:
        # Control: an explicit edit of the legacy system under its own id
        # (replace_existing, as the form sends for a loaded system) is
        # accepted, and after it an add keeps both systems.
        self._write(LEGACY_CONFIG)

        status, response = self._post_setup(
            {
                "system_id": "default",
                "label": "Primary",
                "truenas_host": "https://legacy.example.test",
                "replace_existing": True,
            }
        )
        self.assertEqual(status, 200, response)
        self.assertTrue(response["ok"])
        self.assertTrue(response["updated_existing"])

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )
        self.assertEqual(status, 200, response)
        self.assertTrue(response["ok"])
        self.assertEqual(
            self._effective_system_hosts(),
            {"default": "https://legacy.example.test", "new-host": "https://new.example.test"},
        )

    def test_first_system_on_a_config_without_a_legacy_host_is_saved(self) -> None:
        # Control: a fresh install has no legacy host, so nothing is monitored
        # yet and the first add is the normal path.
        self._write({"app": {"log_level": "INFO"}})

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )

        self.assertEqual(status, 200, response)
        self.assertEqual(self._effective_system_hosts(), {"new-host": "https://new.example.test"})


    def _post_demo(self) -> tuple[int, dict]:
        return asyncio.run(_post(self.app, "/api/admin/system-setup/demo", {}))

    def _effective_default(self):
        settings = load_settings(create_directories=False)
        return {system.id: system for system in settings.systems}

    def test_credential_on_the_default_host_is_a_legacy_system(self) -> None:
        # The loader gives the implicit system the model's default host, so a
        # config that only sets an API key still monitors that host.
        before_bytes = self._write({"truenas": {"api_key": "synthetic-test-key", "platform": "core"}})
        systems = self._effective_default()
        self.assertEqual(list(systems), ["default"])
        self.assertEqual(systems["default"].truenas.host, TrueNASConfig().host)
        self.assertEqual(systems["default"].truenas.api_key, "synthetic-test-key")

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )

        self.assertEqual(status, 400, response)
        self.assertEqual(response.get("detail"), LEGACY_SYSTEM_MIGRATION_DETAIL)
        self.assertEqual(self.config_path.read_bytes(), before_bytes)
        self.assertEqual(list(self._effective_default()), ["default"])

    def test_demo_route_refuses_a_credential_on_the_default_host(self) -> None:
        before_bytes = self._write({"truenas": {"api_key": "synthetic-test-key", "platform": "core"}})
        profile_path = Path(load_settings(create_directories=False).paths.profile_file)
        profile_before = profile_path.read_bytes() if profile_path.exists() else None

        status, response = self._post_demo()

        self.assertEqual(status, 400, response)
        self.assertEqual(response.get("detail"), LEGACY_DEMO_DETAIL)
        self.assertEqual(self.config_path.read_bytes(), before_bytes)
        self.assertEqual(profile_path.read_bytes() if profile_path.exists() else None, profile_before)
        self.assertEqual(list(self._effective_default()), ["default"])

    def test_following_the_printed_migration_step_then_adding_keeps_the_legacy_system(self) -> None:
        # The refusal names a config.yaml edit. Do exactly that on a legacy
        # system whose API key lives only in .env and whose dialect, call
        # limit and command list are settings the setup form does not show,
        # then add through the same route: both are monitored and nothing of
        # the legacy system is lost.
        os.environ["TRUENAS_API_KEY"] = "synthetic-env-key"
        legacy = {
            "truenas": {
                "host": "https://legacy.example.test",
                "platform": "scale",
                "api_dialect": "jsonrpc",
                "max_concurrent_calls": 3,
            },
            "ssh": {
                "enabled": True,
                "host": "legacy.example.test",
                "user": "jbodmap",
                "commands": ["zpool status -gP", "lsblk -OJ"],
            },
        }
        self._write(legacy)
        before = self._effective_default()["default"]
        self.assertEqual(before.truenas.api_key, "synthetic-env-key")

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )
        self.assertEqual(status, 400, response)
        self.assertEqual(response.get("detail"), LEGACY_SYSTEM_MIGRATION_DETAIL)

        # The step, literally: a systems entry with id 'default' holding the
        # top-level truenas and ssh sections, plus the .env value it uses.
        entry = {
            "id": "default",
            "truenas": {**legacy["truenas"], "api_key": os.environ["TRUENAS_API_KEY"]},
            "ssh": dict(legacy["ssh"]),
        }
        self._write({**legacy, "systems": [entry]})

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )
        self.assertEqual(status, 200, response)
        self.assertTrue(response["ok"])
        after = self._effective_default()
        self.assertEqual(list(after), ["default", "new-host"])
        migrated = after["default"]
        self.assertEqual(migrated.truenas.model_dump(), before.truenas.model_dump())
        self.assertEqual(migrated.ssh.model_dump(), before.ssh.model_dump())


    def _effective_dumps(self, system_id: str = "default") -> tuple[dict, dict]:
        system = self._effective_default()[system_id]
        return system.truenas.model_dump(), system.ssh.model_dump()

    def test_adding_default_without_replace_is_refused_and_keeps_every_setting(self) -> None:
        # An add (replace_existing false) under the legacy id would replace
        # the implicit system with a new one; the id alone is not consent.
        for request in (
            {"system_id": "default"},
            {"system_id": "Default"},
            {"system_id": None, "label": "default"},
        ):
            with self.subTest(request=request):
                before_bytes = self._write(RICH_LEGACY_CONFIG)
                before = self._effective_dumps()

                status, response = self._post_setup(
                    {
                        "label": "Replacement",
                        "truenas_host": "https://other.example.test",
                        "platform": "core",
                        "replace_existing": False,
                        **request,
                    }
                )

                self.assertEqual(status, 400, response)
                self.assertEqual(response.get("detail"), LEGACY_SYSTEM_MIGRATION_DETAIL)
                self.assertEqual(self.config_path.read_bytes(), before_bytes)
                self.assertEqual(self._effective_dumps(), before)

    def test_saving_the_loaded_legacy_form_keeps_every_setting(self) -> None:
        # The migration the refusal names first: load 'default' in System
        # Setup and save it. The body is what the form posts for the loaded
        # system (kept secrets, preserved command list); only the label is
        # changed. Every truenas and ssh setting in effect stays, and an add
        # afterwards keeps both systems.
        self._write(RICH_LEGACY_CONFIG)
        before = self._effective_dumps()
        serialized = serialize_systems(load_settings(create_directories=False))[0]
        self.assertEqual(serialized["id"], "default")

        status, response = self._post_setup(_loaded_form_payload(serialized, label="Archive"))

        self.assertEqual(status, 200, response)
        self.assertTrue(response["updated_existing"])
        saved = self._effective_default()
        self.assertEqual(list(saved), ["default"])
        self.assertEqual(saved["default"].label, "Archive")
        self.assertEqual(self._effective_dumps(), before)
        # The operator's trust file is written into the entry itself, so it
        # stays when the obsolete top-level sections are removed.
        self.assertEqual(self._raw_entry()["ssh"]["known_hosts_path"], "/srv/synthetic/known_hosts")
        document = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        document.pop("truenas")
        document.pop("ssh")
        self._write(document)
        self.assertEqual(self._effective_dumps(), before)

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )
        self.assertEqual(status, 200, response)
        self.assertEqual(list(self._effective_default()), ["default", "new-host"])
        self.assertEqual(self._effective_dumps(), before)

    def _save_loaded_form(self, document: dict, *, mode: int = 0o600, **changes) -> tuple[int, dict, list]:
        self._write(document)
        self.config_path.chmod(mode)
        serialized = serialize_systems(load_settings(create_directories=False))[0]
        records = self._capture_logs()
        status, response = self._post_setup(_loaded_form_payload(serialized, **changes))
        return status, response, records

    def _assert_not_disclosed(self, value: str, response: dict, records: list) -> None:
        if not value.strip():
            return
        self.assertNotIn(value, json.dumps(response))
        self.assertNotIn(value, self._log_text(records))

    def test_loaded_form_copies_a_kept_secret_from_the_environment_exactly(self) -> None:
        # Saving the loaded form keeps every configured secret. One that the
        # running configuration takes from the environment (a direct variable
        # or a `_FILE` variable's content) is written into the entry exactly
        # as resolved, and is never shown in the answer or the log.
        lf_file = self._secret_file("api-key-lf", b"synthetic-file-env-key\n")
        crlf_file = self._secret_file("api-key-crlf", b" synthetic crlf key \r\n")
        spaces_file = self._secret_file("api-key-spaces", b" \t \n")
        sudo_file = self._secret_file("sudo-password", b"synthetic sudo password")
        cases = (
            ({"TRUENAS_API_KEY": "synthetic-env-key"}, ("truenas", "api_key"), "synthetic-env-key"),
            ({"TRUENAS_API_KEY_FILE": str(lf_file)}, ("truenas", "api_key"), "synthetic-file-env-key"),
            (
                {"TRUENAS_API_KEY": "synthetic-stale-direct", "TRUENAS_API_KEY_FILE": str(crlf_file)},
                ("truenas", "api_key"),
                " synthetic crlf key ",
            ),
            ({"TRUENAS_API_KEY_FILE": str(spaces_file)}, ("truenas", "api_key"), " \t "),
            ({"TRUENAS_API_PASSWORD": "  synthetic env password  "}, ("truenas", "api_password"), "  synthetic env password  "),
            ({"SSH_PASSWORD": "synthetic-env-ssh-password"}, ("ssh", "password"), "synthetic-env-ssh-password"),
            ({"SSH_SUDO_PASSWORD_FILE": str(sudo_file)}, ("ssh", "sudo_password"), "synthetic sudo password"),
        )
        for environment, (section, key), expected in cases:
            with self.subTest(env=sorted(environment)):
                self._set_env(environment)
                self._write(RICH_LEGACY_CONFIG)
                before = self._effective_dumps()
                self.assertEqual(before[0 if section == "truenas" else 1][key], expected)

                status, response, records = self._save_loaded_form(RICH_LEGACY_CONFIG)

                self.assertEqual(status, 200, response)
                self.assertTrue(response["updated_existing"])
                self.assertEqual(self._raw_entry()[section][key], expected)
                self.assertEqual(self._effective_dumps(), before)
                self._assert_not_disclosed(expected, response, records)
                # The saved entry wins from now on: a rotated value in the
                # environment, or none at all, changes nothing.
                for env_name in environment:
                    if env_name.endswith("_FILE"):
                        Path(os.environ[env_name]).write_bytes(b"synthetic-rotated-value")
                    else:
                        os.environ[env_name] = "synthetic-rotated-value"
                self.assertEqual(self._effective_dumps(), before)
                for env_name in environment:
                    os.environ.pop(env_name)
                self.assertEqual(self._effective_dumps(), before)

    def test_a_blank_direct_variable_never_overwrites_a_saved_key(self) -> None:
        # Control: a blank direct variable is ignored by the loader, so the
        # key and password in config.yaml stay in effect and are what is saved.
        self._set_env({"TRUENAS_API_KEY": "", "SSH_PASSWORD": "   "})
        status, response, _records = self._save_loaded_form(RICH_LEGACY_CONFIG)
        self.assertEqual(status, 200, response)
        entry = self._raw_entry()
        self.assertEqual(entry["truenas"]["api_key"], RICH_LEGACY_CONFIG["truenas"]["api_key"])
        self.assertEqual(entry["ssh"]["password"], RICH_LEGACY_CONFIG["ssh"]["password"])

    def test_empty_file_content_is_saved_as_the_empty_value_in_effect(self) -> None:
        # An empty file is not ignored: it is the key in effect (none), so the
        # key in config.yaml was not in use and the entry gets no key either.
        empty_file = self._secret_file("api-key-empty", b"")
        self._set_env({"TRUENAS_API_KEY_FILE": str(empty_file)})
        self._write(RICH_LEGACY_CONFIG)
        before = self._effective_dumps()
        self.assertEqual(before[0]["api_key"], "")

        status, response, _records = self._save_loaded_form(RICH_LEGACY_CONFIG)

        self.assertEqual(status, 200, response)
        self.assertEqual(self._raw_entry()["truenas"].get("api_key", ""), "")
        self.assertEqual(self._effective_dumps(), before)

    def test_after_the_copy_a_file_variable_must_name_a_readable_file_until_removed(self) -> None:
        secret_file = self._secret_file("api-key", b"synthetic-file-env-key\n")
        self._set_env({"TRUENAS_API_KEY_FILE": str(secret_file)})
        status, response, _records = self._save_loaded_form(RICH_LEGACY_CONFIG)
        self.assertEqual(status, 200, response)

        secret_file.unlink()
        with self.assertRaisesRegex(Exception, "TRUENAS_API_KEY_FILE must reference a readable regular file"):
            load_settings(create_directories=False)
        os.environ.pop("TRUENAS_API_KEY_FILE")
        self.assertEqual(self._effective_default()["default"].truenas.api_key, "synthetic-file-env-key")

    def test_loaded_form_keeps_exact_user_names(self) -> None:
        # The loader keeps user names exactly (QuantaStor sends the API user
        # verbatim in Basic authentication); the form trims its inputs. A
        # loaded user name saved unchanged keeps its exact form.
        for user in ("  synthetic admin  ", "'quoted-user'", '"double-quoted"', "true", "null", "007"):
            with self.subTest(user=user):
                document = {
                    "truenas": {
                        "host": "https://qs.example.test",
                        "platform": "quantastor",
                        "api_user": user,
                        "api_password": "synthetic-qs-password",
                        "verify_ssl": False,
                    },
                    "ssh": {"enabled": True, "host": "qs.example.test", "user": user},
                }
                self._write(document)
                before = self._effective_dumps()
                self.assertEqual(before[0]["api_user"], user)

                status, response, records = self._save_loaded_form(document)

                self.assertEqual(status, 200, response)
                entry = self._raw_entry()
                self.assertEqual(entry["truenas"]["api_user"], user)
                self.assertEqual(entry["ssh"]["user"], user)
                self.assertEqual(entry["truenas"]["api_password"], "synthetic-qs-password")
                self.assertEqual(self._effective_dumps(), before)
                self._assert_not_disclosed("synthetic-qs-password", response, records)

        # Control: a user name the operator changes is saved as sent.
        self._write({**document, "truenas": {**document["truenas"], "api_user": "  synthetic admin  "}})
        serialized = serialize_systems(load_settings(create_directories=False))[0]
        status, response = self._post_setup(
            _loaded_form_payload(serialized, api_user="synthetic-other", api_password="synthetic-typed")
        )
        self.assertEqual(status, 200, response)
        self.assertEqual(self._raw_entry()["truenas"]["api_user"], "synthetic-other")

    def _readable_warnings(self, records: list) -> list[str]:
        return [
            record.getMessage()
            for record in records
            if record.levelno == logging.WARNING and record.name == "app.services.system_setup"
        ]

    def test_copy_into_an_owner_only_config_keeps_its_mode_and_warns_nothing(self) -> None:
        self._set_env({"TRUENAS_API_KEY": "synthetic-env-key"})
        status, response, records = self._save_loaded_form(RICH_LEGACY_CONFIG, mode=0o600)
        self.assertEqual(status, 200, response)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(self._readable_warnings(records), [])
        self.assertNotIn(SECRET_FILE_READABLE_NOTE, response["detail"])

    def test_copy_into_a_shared_config_warns_once_and_keeps_its_mode(self) -> None:
        # The copy does not refuse, ask or change the file's mode (other
        # services may read it under another account); it says so once in the
        # log, without the value or a path, and on the page.
        for mode in (0o644, 0o640):
            with self.subTest(mode=oct(mode)):
                self._set_env({"TRUENAS_API_KEY": "synthetic-env-key"})
                status, response, records = self._save_loaded_form(RICH_LEGACY_CONFIG, mode=mode)
                self.assertEqual(status, 200, response)
                self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), mode)
                self.assertEqual(self._raw_entry()["truenas"]["api_key"], "synthetic-env-key")
                self.assertEqual(self._readable_warnings(records), [SECRET_FILE_READABLE_LOG])
                self.assertTrue(response["detail"].endswith(" " + SECRET_FILE_READABLE_NOTE), response["detail"])
                self._assert_not_disclosed("synthetic-env-key", response, records)
                self.assertNotIn(str(self.root), self._log_text(records))
                os.environ.pop("TRUENAS_API_KEY")

    def test_no_copy_into_a_shared_config_adds_no_warning(self) -> None:
        # Controls: a key already held in config.yaml, or a key typed in,
        # is not a copy from the environment.
        for changes in ({}, {"api_key": "synthetic-typed-key"}):
            with self.subTest(typed=bool(changes)):
                status, response, records = self._save_loaded_form(RICH_LEGACY_CONFIG, mode=0o644, **changes)
                self.assertEqual(status, 200, response)
                self.assertEqual(self._readable_warnings(records), [])
                self.assertNotIn(SECRET_FILE_READABLE_NOTE, response["detail"])

    def test_documented_manual_move_uses_the_values_in_effect(self) -> None:
        # The wiki step, literally: one systems entry with id 'default'
        # holding the top-level sections, and for each set variable the value
        # in effect. A blank variable is ignored (the file value stays); a
        # _FILE variable names a file whose content is the value and wins
        # over the direct one; exact-string values are copied exactly.
        secret_file = self.root / "synthetic-api-key"
        secret_file.write_text("synthetic-key-from-file\n", encoding="utf-8")
        secret_file.chmod(0o600)
        blank_file = self._secret_file("synthetic-blank-key", b"  \n")
        cases = {
            "blank direct value": (
                {"TRUENAS_API_KEY": "", "SSH_PASSWORD": "   "},
                {},
            ),
            "blank file content": (
                {"TRUENAS_API_KEY_FILE": str(blank_file)},
                {"truenas": {"api_key": "  "}},
            ),
            "direct and file value": (
                {
                    "TRUENAS_API_KEY": "synthetic-stale-direct-key",
                    "TRUENAS_API_KEY_FILE": str(secret_file),
                    "TRUENAS_API_USER": "  spaced-user ",
                },
                {"truenas": {"api_key": "synthetic-key-from-file", "api_user": "  spaced-user "}},
            ),
        }
        for name, (environment, values_in_effect) in cases.items():
            with self.subTest(case=name):
                for env_name, env_value in environment.items():
                    os.environ[env_name] = env_value
                    self.addCleanup(os.environ.pop, env_name, None)
                self._write(RICH_LEGACY_CONFIG)
                before = self._effective_dumps()

                status, response = self._post_setup(
                    {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
                )
                self.assertEqual(status, 400, response)
                self.assertEqual(response.get("detail"), LEGACY_SYSTEM_MIGRATION_DETAIL)

                entry = {
                    "id": "default",
                    "truenas": {**RICH_LEGACY_CONFIG["truenas"], **values_in_effect.get("truenas", {})},
                    "ssh": {**RICH_LEGACY_CONFIG["ssh"], **values_in_effect.get("ssh", {})},
                }
                self._write({**RICH_LEGACY_CONFIG, "systems": [entry]})
                self.assertEqual(self._effective_dumps(), before)

                status, response = self._post_setup(
                    {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
                )
                self.assertEqual(status, 200, response)
                self.assertEqual(list(self._effective_default()), ["default", "new-host"])
                self.assertEqual(self._effective_dumps(), before)
                for env_name in environment:
                    os.environ.pop(env_name)

    def test_refusal_names_a_wiki_section_that_exists(self) -> None:
        wiki = Path(__file__).resolve().parents[1] / "wiki" / "Advanced-Configuration.md"
        text = wiki.read_text(encoding="utf-8")
        self.assertTrue(WIKI_MIGRATION_HEADING + "\n" in text, f"missing {WIKI_MIGRATION_HEADING!r}")
        section = " ".join(text.split(WIKI_MIGRATION_HEADING, 1)[1].split("\n## ", 1)[0].split())
        for statement in (
            "A direct variable set to a blank value is ignored",
            "An empty or blank file is not ignored",
            "A `_FILE` variable set to a blank path is not ignored",
            "the saved entry wins",
            "must still name a readable file until you remove it",
            "copies the value in effect into `config.yaml`",
            "stored in `config.yaml`",
        ):
            self.assertTrue(statement in section, f"section does not say {statement!r}")
        for stale in ("has to be typed in", "no longer has any effect"):
            self.assertFalse(stale in section, f"section still says {stale!r}")


class LegacySystemAddServiceTests(_LegacyConfigMixin, unittest.TestCase):
    """Every source of the implicit legacy system is protected, not only truenas.host."""

    def _assert_refused_unchanged(self, before_bytes: bytes | None) -> None:
        service = SystemSetupService(str(self.config_path))
        with self.assertRaises(ValueError) as caught:
            service.save_system(self._request("new-host"))
        self.assertEqual(str(caught.exception), LEGACY_SYSTEM_MIGRATION_DETAIL)
        if before_bytes is None:
            self.assertFalse(self.config_path.exists())
        else:
            self.assertEqual(self.config_path.read_bytes(), before_bytes)

    def test_ssh_only_legacy_system_is_refused(self) -> None:
        before = self._write(
            {
                "truenas": {"platform": "linux"},
                "ssh": {"enabled": True, "host": "linux.example.test", "user": "jbodmap"},
            }
        )
        self._assert_refused_unchanged(before)

    def test_legacy_system_from_environment_is_refused(self) -> None:
        os.environ["TRUENAS_HOST"] = "https://env.example.test"
        self.assertEqual(self._effective_system_hosts(), {"default": "https://env.example.test"})
        self._assert_refused_unchanged(None)

    def test_credential_from_environment_alone_is_a_legacy_system(self) -> None:
        # No config file and no host anywhere: the key alone, through the
        # loader's environment rules, configures the default-host system.
        os.environ["TRUENAS_API_KEY"] = "synthetic-env-key"
        settings = load_settings(create_directories=False)
        self.assertEqual(settings.systems[0].truenas.api_key, "synthetic-env-key")
        self._assert_refused_unchanged(None)

    def test_ssh_host_from_environment_is_refused(self) -> None:
        os.environ["SSH_HOST"] = "env-ssh.example.test"
        self._assert_refused_unchanged(None)

    def test_demo_creation_refuses_an_environment_only_legacy_system(self) -> None:
        os.environ["TRUENAS_HOST"] = "https://env.example.test"
        profile_path = self.root / "config" / "profiles.yaml"
        factory = DemoSystemFactory(str(self.config_path), str(profile_path))
        with self.assertRaises(ValueError) as caught:
            factory.create_demo_system(DemoSystemRequest(), load_settings(create_directories=False))
        self.assertEqual(str(caught.exception), LEGACY_DEMO_DETAIL)
        self.assertFalse(self.config_path.exists())
        self.assertFalse(profile_path.exists())

    def test_blank_environment_host_is_not_a_legacy_system(self) -> None:
        # Control: load_settings treats a blank override as unset, and an
        # unset host, no credential and SSH off is the untouched default.
        os.environ["TRUENAS_HOST"] = "   "
        os.environ["TRUENAS_API_KEY"] = ""
        os.environ["SSH_ENABLED"] = "false"
        os.environ["TRUENAS_VERIFY_SSL"] = "false"
        SystemSetupService(str(self.config_path)).save_system(self._request("new-host"))
        self.assertEqual(self._effective_system_hosts(), {"new-host": "https://new.example.test"})

    def test_populated_systems_list_still_accepts_an_add(self) -> None:
        # Control: once systems exist the top-level sections are already
        # ignored, so an add changes nothing that was monitored.
        self._write(
            {
                **LEGACY_CONFIG,
                "systems": [{"id": "lab", "label": "Lab", "truenas": {"host": "https://lab.example.test"}}],
            }
        )
        SystemSetupService(str(self.config_path)).save_system(self._request("new-host"))
        self.assertEqual(
            self._effective_system_hosts(),
            {"lab": "https://lab.example.test", "new-host": "https://new.example.test"},
        )

    def test_demo_creation_uses_the_same_legacy_guard(self) -> None:
        before = self._write(
            {
                "truenas": {"platform": "linux"},
                "ssh": {"enabled": True, "host": "linux.example.test", "user": "jbodmap"},
            }
        )
        profile_path = self.root / "config" / "profiles.yaml"
        factory = DemoSystemFactory(str(self.config_path), str(profile_path))
        with self.assertRaises(ValueError) as caught:
            factory.create_demo_system(DemoSystemRequest(), load_settings(create_directories=False))
        self.assertEqual(str(caught.exception), LEGACY_DEMO_DETAIL)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(profile_path.exists())

    def test_editing_default_starts_from_the_legacy_settings_in_effect(self) -> None:
        # Even a legacy system that is not configured enough to block an add
        # keeps its settings when 'default' is edited: the edit starts from
        # the system in effect, not from the model defaults.
        self._write({"truenas": {"platform": "scale", "timeout_seconds": 91}, "ssh": {"timeout_seconds": 33}})
        SystemSetupService(str(self.config_path)).save_system(
            SystemSetupRequest(
                system_id="default",
                label="Primary",
                platform="scale",
                truenas_host="https://legacy.example.test",
                timeout_seconds=15,
                ssh_timeout_seconds=15,
                replace_existing=True,
            )
        )
        system = load_settings(create_directories=False).systems[0]
        self.assertEqual(system.id, "default")
        self.assertEqual(system.truenas.timeout_seconds, 91)
        self.assertEqual(system.ssh.timeout_seconds, 33)

    def _edit_default(self, **fields) -> None:
        SystemSetupService(str(self.config_path)).save_system(
            SystemSetupRequest(
                system_id="default",
                label="Primary",
                truenas_host="https://env.example.test",
                replace_existing=True,
                **fields,
            )
        )

    def test_copy_into_a_new_config_file_is_owner_only(self) -> None:
        # No config.yaml yet: the system comes from the environment alone.
        # The copy creates the file with mode 0600 and logs no warning.
        self._set_env({"TRUENAS_HOST": "https://env.example.test", "TRUENAS_API_KEY": "synthetic-env-key"})
        records = self._capture_logs()
        self._edit_default(api_key=PRESERVE)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(self._raw_entry()["truenas"]["api_key"], "synthetic-env-key")
        self.assertFalse(
            [record for record in records if record.levelno >= logging.WARNING],
            self._log_text(records),
        )

    def test_a_blank_file_variable_refuses_the_save_without_writing(self) -> None:
        # A `_FILE` variable set to a blank path is not ignored: it names no
        # readable file, so the configuration cannot be resolved.
        before = self._write(RICH_LEGACY_CONFIG)
        self._set_env({"TRUENAS_API_KEY_FILE": ""})
        with self.assertRaises(ValueError) as caught:
            self._edit_default(api_key=PRESERVE)
        self.assertEqual(str(caught.exception), "TRUENAS_API_KEY_FILE must reference a readable regular file.")
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_invalid_legacy_sections_refuse_the_edit_without_echoing_them(self) -> None:
        before = self._write({"truenas": {"api_key": "synthetic-test-key", "timeout_seconds": "not-a-number"}})
        with self.assertRaises(ValueError) as caught:
            SystemSetupService(str(self.config_path)).save_system(
                SystemSetupRequest(
                    system_id="default",
                    label="Primary",
                    truenas_host="https://legacy.example.test",
                    replace_existing=True,
                )
            )
        self.assertEqual(str(caught.exception), LEGACY_SECTIONS_INVALID_DETAIL)
        self.assertEqual(self.config_path.read_bytes(), before)


class ConfigStagingCleanupTests(_LegacyConfigMixin, unittest.TestCase):
    """A save that fails before publication leaves the old file and no staging copy."""

    POPULATED = {
        "systems": [
            {
                "id": "lab",
                "label": "Lab",
                "truenas": {"host": "https://lab.example.test", "api_key": "synthetic-lab-key"},
            }
        ]
    }

    STAGING_NAME = re.compile(r"^\.config\.yaml\.[0-9a-f]{32}\.tmp$")

    def setUp(self) -> None:
        super().setUp()
        self.service = SystemSetupService(str(self.config_path))

    def _directory(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.config_path.parent.iterdir()}

    def _operations(self):
        return (
            ("save", lambda: self.service.save_system(self._request("new-host"))),
            ("delete", lambda: self.service.delete_system("lab")),
        )

    def test_a_failed_or_interrupted_rename_leaves_no_staging_copy(self) -> None:
        for name, operation in self._operations():
            for failure in (OSError(errno.EIO, "synthetic rename failure"), KeyboardInterrupt()):
                with self.subTest(operation=name, failure=type(failure).__name__):
                    self._write(self.POPULATED)
                    before = self._directory()
                    with mock.patch.object(secret_files.os, "replace", side_effect=failure):
                        with self.assertRaises(type(failure)):
                            operation()
                    self.assertEqual(self._directory(), before)

    def test_each_write_stages_under_its_own_exclusive_owner_only_name(self) -> None:
        self._write(self.POPULATED)
        real_open, real_replace = os.open, os.replace
        created: list[tuple[str, int, int]] = []
        published: list[str] = []

        def recording_open(path, flags, mode=0o777, *args, **kwargs):
            if Path(path).parent == self.config_path.parent and flags & os.O_CREAT:
                created.append((Path(path).name, flags, mode))
            return real_open(path, flags, mode, *args, **kwargs)

        def recording_replace(source, target):
            published.append(Path(source).name)
            return real_replace(source, target)

        with (
            mock.patch.object(secret_files.os, "open", side_effect=recording_open),
            mock.patch.object(secret_files.os, "replace", side_effect=recording_replace),
        ):
            self.service.save_system(self._request("new-host"))
            self.service.save_system(self._request("other-host", "https://other.example.test"))

        self.assertEqual(len(created), 2, created)
        self.assertEqual([name for name, _flags, _mode in created], published)
        self.assertNotEqual(published[0], published[1])
        for name, flags, mode in created:
            self.assertRegex(name, self.STAGING_NAME)
            self.assertTrue(flags & os.O_EXCL)
            self.assertEqual(mode, 0o600)
        self.assertEqual(set(self._directory()), {"config.yaml"})

    def test_another_writers_staging_file_is_never_removed(self) -> None:
        # Another writer's staging file (or one left by an older version) is
        # not this write's to remove, whether the write succeeds or fails.
        self._write(self.POPULATED)
        foreign = {
            "config.tmp": b"older version staging",
            ".config.yaml." + "0" * 32 + ".tmp": b"another writer's staging",
        }
        for name, content in foreign.items():
            (self.config_path.parent / name).write_bytes(content)

        self.service.save_system(self._request("new-host"))
        with mock.patch.object(secret_files.os, "replace", side_effect=OSError(errno.EIO, "synthetic rename failure")):
            with self.assertRaises(OSError):
                self.service.save_system(self._request("other-host", "https://other.example.test"))

        directory = self._directory()
        for name, content in foreign.items():
            self.assertEqual(directory.get(name), content)
        self.assertEqual(set(directory), {"config.yaml", *foreign})

    def test_overlapping_writers_each_remove_only_their_own_staging_file(self) -> None:
        # Writer A fails after writer B ran to completion between A's staging
        # and A's rename: B's published file stays, A removes only its own.
        self._write(self.POPULATED)
        real_replace = os.replace
        calls: list[str] = []

        def interleave(source, target):
            calls.append(Path(source).name)
            if len(calls) == 1:
                secret_files.write_text_atomically(self.config_path, "written: by-b\n")
                raise OSError(errno.EIO, "synthetic rename failure")
            return real_replace(source, target)

        with mock.patch.object(secret_files.os, "replace", side_effect=interleave):
            with self.assertRaises(OSError):
                secret_files.write_text_atomically(self.config_path, "written: by-a\n")

        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual(self._directory(), {"config.yaml": b"written: by-b\n"})

    def test_staging_is_owner_only_until_its_content_is_complete(self) -> None:
        # The staging file is 0600 while it is written and takes the
        # destination's mode (0600 for a new file) only once it is complete.
        content = "written: complete\n"
        for existing_mode in (0o644, 0o640, 0o600, None):
            with self.subTest(existing=oct(existing_mode) if existing_mode else "new"):
                if existing_mode is None:
                    self.config_path.unlink(missing_ok=True)
                    expected_final = 0o600
                else:
                    self._write(self.POPULATED)
                    self.config_path.chmod(existing_mode)
                    expected_final = existing_mode
                real_fchmod = os.fchmod
                observed: list[tuple[int, int]] = []

                def recording_fchmod(descriptor, mode):
                    observed.append((os.fstat(descriptor).st_size, mode))
                    return real_fchmod(descriptor, mode)

                with mock.patch.object(secret_files.os, "fchmod", side_effect=recording_fchmod):
                    published_mode = secret_files.write_text_atomically(self.config_path, content)

                self.assertEqual(observed, [(0, 0o600), (len(content), expected_final)])
                self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), expected_final)
                self.assertEqual(published_mode, expected_final)

    def test_a_failed_write_closes_the_staging_file_before_removing_it(self) -> None:
        # Some platforms refuse to remove a file that is still open. The
        # cleanup must have closed the staging file first, or a failed save
        # leaves a copy that can hold credentials. The removal here refuses
        # while the file is open, as such a platform would.
        real_open, real_fdopen, real_fchmod, real_unlink = os.open, os.fdopen, os.fchmod, Path.unlink

        def failing_on_call(real, number, failure):
            calls = []

            def wrapper(*args, **kwargs):
                calls.append(args)
                if len(calls) == number:
                    raise failure
                return real(*args, **kwargs)

            return wrapper

        cases = {
            "opening the stream": ("fdopen", lambda failure: failing_on_call(real_fdopen, 1, failure)),
            "before the content": ("fchmod", lambda failure: failing_on_call(real_fchmod, 1, failure)),
            "after the content": ("fchmod", lambda failure: failing_on_call(real_fchmod, 2, failure)),
            "the sync": ("fsync", lambda failure: failing_on_call(os.fsync, 1, failure)),
        }
        for label, (name, build) in cases.items():
            for failure in (OSError(errno.EIO, "synthetic write failure"), KeyboardInterrupt()):
                with self.subTest(step=label, failure=type(failure).__name__):
                    self._write(self.POPULATED)
                    before = self._directory()
                    descriptors: list[int] = []

                    def recording_open(path, flags, mode=0o777, **kwargs):
                        descriptor = real_open(path, flags, mode, **kwargs)
                        if self.STAGING_NAME.match(Path(path).name):
                            descriptors.append(descriptor)
                        return descriptor

                    def unlink_refusing_an_open_file(path, *args, **kwargs):
                        for descriptor in descriptors:
                            try:
                                os.fstat(descriptor)
                            except OSError:
                                continue
                            raise PermissionError(errno.EACCES, "the file is in use")
                        return real_unlink(path, *args, **kwargs)

                    with (
                        mock.patch.object(secret_files.os, "open", side_effect=recording_open),
                        mock.patch.object(secret_files.os, name, side_effect=build(failure)),
                        mock.patch.object(Path, "unlink", unlink_refusing_an_open_file),
                    ):
                        with self.assertRaises(type(failure)):
                            self.service.save_system(self._request("new-host"))
                    self.assertEqual(len(descriptors), 1)
                    self.assertEqual(self._directory(), before)

    def test_the_staging_file_is_closed_before_it_is_renamed(self) -> None:
        # Control for the above: a save that succeeds has also closed the
        # staging file by the time it renames it, on every platform.
        self._write(self.POPULATED)
        real_open, real_replace = os.open, os.replace
        descriptors: list[int] = []
        open_at_rename: list[bool] = []

        def recording_open(path, flags, mode=0o777, **kwargs):
            descriptor = real_open(path, flags, mode, **kwargs)
            if self.STAGING_NAME.match(Path(path).name):
                descriptors.append(descriptor)
            return descriptor

        def recording_replace(source, target):
            for descriptor in descriptors:
                try:
                    os.fstat(descriptor)
                except OSError:
                    open_at_rename.append(False)
                else:
                    open_at_rename.append(True)
            return real_replace(source, target)

        with (
            mock.patch.object(secret_files.os, "open", side_effect=recording_open),
            mock.patch.object(secret_files.os, "replace", side_effect=recording_replace),
        ):
            self.service.save_system(self._request("new-host"))
        self.assertEqual(open_at_rename, [False])
        self.assertEqual(set(self._directory()), {"config.yaml"})

    def test_an_interrupt_after_the_rename_keeps_the_published_file(self) -> None:
        # Control: once the rename happened the new file is the config; the
        # cleanup must not remove it.
        self._write(self.POPULATED)
        real_replace = os.replace

        def replace_then_interrupt(source, target):
            real_replace(source, target)
            raise KeyboardInterrupt()

        with mock.patch.object(secret_files.os, "replace", side_effect=replace_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.save_system(self._request("new-host"))
        self.assertEqual(self._effective_system_hosts(), {"lab": "https://lab.example.test", "new-host": "https://new.example.test"})
        self.assertEqual(set(self._directory()), {"config.yaml"})


if __name__ == "__main__":
    unittest.main()
