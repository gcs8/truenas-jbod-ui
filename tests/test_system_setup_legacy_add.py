"""Adding a system next to a legacy top-level system must not drop it (#1227).

A config without a nonempty ``systems`` list still monitors one system built
from the top-level ``truenas`` and ``ssh`` sections (and their environment
overrides, on the model's default host when none is set). Writing a
``systems`` list replaces that implicit system, so an add that starts the list
from empty silently stops monitoring it. System Setup and demo creation refuse
every add while that system exists, whatever id the request carries,
``default`` included. A real edit of ``default`` (``replace_existing``) starts
from the system in effect and keeps every field the form does not own; a key
or password that comes from the environment is only stored in config.yaml
when the operator types it in.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
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
LEGACY_SECRET_FROM_ENV_DETAIL = (
    "The API key or a password of the 'default' system comes from .env, and saving it "
    "here stores it in config.yaml. Type it in to save it there."
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


def _loaded_form_payload(serialized: dict, **changes) -> dict:
    """The body the System Setup form posts for a loaded system saved as is.

    Field for field what ``collectSetupPayload`` in admin.js builds from the
    serialized system: configured secrets as the keep-existing sentinel, the
    redacted command list as ``preserve`` from the loaded id, generated
    storage views left out, and ``replace_existing`` because the id is the
    loaded one.
    """

    def secret(name: str):
        return PRESERVE if serialized.get(f"{name}_configured") else None

    payload = {
        "system_id": serialized["id"],
        "label": serialized["label"],
        "platform": serialized["platform"],
        "truenas_host": serialized["truenas_host"],
        "api_key": secret("api_key"),
        "api_user": serialized["api_user"] or None,
        "api_password": secret("api_password"),
        "verify_ssl": serialized["verify_ssl"],
        "tls_ca_bundle_path": serialized["tls_ca_bundle_path"] or None,
        "tls_server_name": serialized["tls_server_name"] or None,
        "enclosure_filter": serialized["enclosure_filter"] or None,
        "ssh_enabled": serialized["ssh_enabled"],
        "ssh_host": serialized["ssh_host"] or None,
        "ha_enabled": False,
        "ha_nodes": [],
        "ssh_port": serialized["ssh_port"],
        "ssh_user": serialized["ssh_user"] or None,
        "ssh_key_path": serialized["ssh_key_path"] or None,
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
        "bmc_host": serialized["bmc_host"] or None,
        "bmc_username": serialized["bmc_username"] or None,
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

        status, response = self._post_setup(
            {"system_id": "new-host", "label": "New host", "truenas_host": "https://new.example.test"}
        )
        self.assertEqual(status, 200, response)
        self.assertEqual(list(self._effective_default()), ["default", "new-host"])
        self.assertEqual(self._effective_dumps(), before)

    def test_loaded_form_does_not_store_a_secret_from_env_without_it_being_typed(self) -> None:
        # A kept secret whose value comes from the environment would be
        # copied into config.yaml; that needs the operator to type it.
        secret_file = self.root / "synthetic-api-key"
        secret_file.write_text("synthetic-file-env-key\n", encoding="utf-8")
        secret_file.chmod(0o600)
        cases = (
            ("TRUENAS_API_KEY", "synthetic-env-key", "api_key", ("truenas", "api_key")),
            ("TRUENAS_API_KEY_FILE", str(secret_file), "api_key", ("truenas", "api_key")),
            ("SSH_PASSWORD", "synthetic-env-password", "ssh_password", ("ssh", "password")),
        )
        for env_name, env_value, field, (section, key) in cases:
            with self.subTest(env=env_name):
                os.environ[env_name] = env_value
                self.addCleanup(os.environ.pop, env_name, None)
                before_bytes = self._write(RICH_LEGACY_CONFIG)
                before = self._effective_dumps()
                in_effect = before[0 if section == "truenas" else 1][key]
                self.assertNotEqual(in_effect, RICH_LEGACY_CONFIG[section][key])
                serialized = serialize_systems(load_settings(create_directories=False))[0]

                status, response = self._post_setup(_loaded_form_payload(serialized))

                self.assertEqual(status, 400, response)
                self.assertEqual(response.get("detail"), LEGACY_SECRET_FROM_ENV_DETAIL)
                self.assertEqual(self.config_path.read_bytes(), before_bytes)

                # Control: typed in, the same value is saved and still in effect.
                status, response = self._post_setup(_loaded_form_payload(serialized, **{field: in_effect}))
                self.assertEqual(status, 200, response)
                self.assertEqual(self._effective_dumps(), before)
                os.environ.pop(env_name)
                self.assertEqual(self._effective_dumps(), before)

    def test_documented_manual_move_uses_the_values_in_effect(self) -> None:
        # The wiki step, literally: one systems entry with id 'default'
        # holding the top-level sections, and for each set variable the value
        # in effect. A blank variable is ignored (the file value stays); a
        # _FILE variable names a file whose content is the value and wins
        # over the direct one; exact-string values are copied exactly.
        secret_file = self.root / "synthetic-api-key"
        secret_file.write_text("synthetic-key-from-file\n", encoding="utf-8")
        secret_file.chmod(0o600)
        cases = {
            "blank direct value": (
                {"TRUENAS_API_KEY": "", "SSH_PASSWORD": "   "},
                {},
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
        section = text.split(WIKI_MIGRATION_HEADING, 1)[1].split("\n## ", 1)[0]
        for statement in (
            "A variable set to a blank value is ignored",
            "`_FILE`",
            "stored in `config.yaml`",
        ):
            self.assertTrue(statement in section, f"section does not say {statement!r}")


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

    def setUp(self) -> None:
        super().setUp()
        self.staging = self.config_path.with_suffix(".tmp")
        self.service = SystemSetupService(str(self.config_path))

    def _operations(self):
        return (
            ("save", lambda: self.service.save_system(self._request("new-host"))),
            ("delete", lambda: self.service.delete_system("lab")),
        )

    def test_a_failed_or_interrupted_rename_leaves_no_staging_copy(self) -> None:
        for name, operation in self._operations():
            for failure in (OSError(errno.EIO, "synthetic rename failure"), KeyboardInterrupt()):
                with self.subTest(operation=name, failure=type(failure).__name__):
                    before = self._write(self.POPULATED)
                    with mock.patch.object(secret_files.os, "replace", side_effect=failure):
                        with self.assertRaises(type(failure)):
                            operation()
                    self.assertEqual(self.config_path.read_bytes(), before)
                    self.assertFalse(self.staging.exists())

    def test_cleanup_removes_only_the_staging_file_this_write_created(self) -> None:
        # Control: if the staging name no longer holds this write's file,
        # the cleanup leaves whatever is there.
        before = self._write(self.POPULATED)

        def swap_then_fail(source, target):
            Path(source).unlink()
            Path(source).write_text("not this write's file", encoding="utf-8")
            raise OSError(errno.EIO, "synthetic rename failure")

        with mock.patch.object(secret_files.os, "replace", side_effect=swap_then_fail):
            with self.assertRaises(OSError):
                self.service.save_system(self._request("new-host"))
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.staging.read_text(encoding="utf-8"), "not this write's file")

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
        self.assertFalse(self.staging.exists())


if __name__ == "__main__":
    unittest.main()
