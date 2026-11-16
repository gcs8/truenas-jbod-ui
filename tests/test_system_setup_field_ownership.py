"""Re-saving a system from System Setup changes only the fields the form owns (#1192).

``save_system`` used to rebuild every nested model from the request and
replace the saved entry, so any saved field the request does not carry was
silently reset to its default. ``truenas.max_concurrent_calls`` (#1072) was
the concrete casualty. The classification table below names every field of
the saved system model; ``FieldClassificationTests`` fails when a model field
is added or removed until it is classified here, and the survival test then
proves that each field the form does not own keeps its saved value.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from pydantic import BaseModel

# Must precede admin_service.main, which builds its app at import time.
from tests.admin_test_env import ADMIN_TEST_PUBLIC_ORIGIN  # noqa: F401

from admin_service.main import app as admin_app
from app.config import (
    BMCConfig,
    HANodeConfig,
    SSHConfig,
    StorageViewConfig,
    SystemConfig,
    TrueNASConfig,
    effective_max_concurrent_calls,
    get_settings,
)
from app.models.domain import SystemSetupRequest
from app.services.system_setup import PRESERVE_SECRET_SENTINEL, SystemSetupService


# Every field of SystemConfig, with the nested TrueNAS, SSH and BMC blocks
# flattened to dotted paths. Lists of models (storage views, HA nodes) are
# owned or kept as a whole list.

# The request always carries these; a save writes what the form sent
# (saved secrets come back as the keep-existing sentinel).
FORM_REPLACES = frozenset({
    "id",
    "label",
    "default_profile_id",
    "truenas.host",
    "truenas.api_key",
    "truenas.api_user",
    "truenas.api_password",
    "truenas.platform",
    "truenas.verify_ssl",
    "truenas.enclosure_filter",
    "ssh.enabled",
    "ssh.host",
    "ssh.ha_enabled",
    "ssh.ha_nodes",
    "ssh.port",
    "ssh.user",
    "ssh.key_path",
    "ssh.password",
    "ssh.sudo_password",
    "ssh.strict_host_key_checking",
    "ssh.commands",
    "bmc.enabled",
    "bmc.host",
    "bmc.username",
    "bmc.password",
    "bmc.verify_ssl",
    "bmc.timeout_seconds",
})

# Form-owned, but an omitted (None) request value keeps the saved one. The
# sample is a non-default saved value; ``root`` is the test's temp directory.
FORM_KEEPS_WHEN_OMITTED: dict[str, Callable[[Path], Any]] = {
    "storage_views": lambda root: [
        StorageViewConfig(
            id="synthetic-view",
            label="Synthetic view",
            kind="manual",
            template_id="manual-4",
            order=30,
        )
    ],
    "auto_storage_views": lambda root: False,
    "view_order": lambda root: ["view:synthetic-view", "enclosure:synthetic-enclosure"],
    "truenas.tls_ca_bundle_path": lambda root: str(root / "synthetic-ca.pem"),
    "truenas.tls_server_name": lambda root: "nas-cert.example.test",
}

# Not owned by the setup form on an existing system: set only when a system
# is first created, or edited in config.yaml or elsewhere. A re-save must keep
# each of them.
NOT_FORM_OWNED: dict[str, Callable[[Path], Any]] = {
    "enclosure_profiles": lambda root: {"synthetic-enclosure": "synthetic-profile"},
    "truenas.api_dialect": lambda root: "jsonrpc",
    "truenas.api_version": lambda root: "v25.10.0",
    "truenas.timeout_seconds": lambda root: 41,
    "truenas.max_concurrent_calls": lambda root: 1,
    "ssh.extra_hosts": lambda root: ["extra.example.test"],
    "ssh.known_hosts_path": lambda root: str(root / "operator_known_hosts"),
    "ssh.timeout_seconds": lambda root: 37,
}

_NESTED = {"truenas": TrueNASConfig, "ssh": SSHConfig, "bmc": BMCConfig}


def _model_field_paths() -> set[str]:
    paths: set[str] = set()
    for name, field in SystemConfig.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            # Any new nested block must be added to _NESTED and classified.
            if _NESTED.get(name) is not annotation:
                paths.add(name)
                continue
            paths.update(f"{name}.{nested}" for nested in annotation.model_fields)
        else:
            paths.add(name)
    return paths


def _get(system: SystemConfig, path: str) -> Any:
    value: Any = system
    for part in path.split("."):
        value = getattr(value, part)
    return value


def _default_for(path: str) -> Any:
    head, _, tail = path.partition(".")
    model: type[BaseModel] = _NESTED[head] if tail else SystemConfig
    field = model.model_fields[tail or head]
    return field.get_default(call_default_factory=True)


def _with_values(system: SystemConfig, values: dict[str, Any]) -> SystemConfig:
    raw = system.model_dump(mode="python")
    for path, value in values.items():
        head, _, tail = path.partition(".")
        if isinstance(value, list):
            value = [item.model_dump(mode="python") if isinstance(item, BaseModel) else item for item in value]
        if tail:
            raw[head][tail] = value
        else:
            raw[head] = value
    return SystemConfig.model_validate(raw)


def _label_only_request(saved: SystemConfig, label: str) -> dict[str, Any]:
    """The JSON the setup form sends for a loaded system with only the label edited.

    Saved secrets and the redacted SSH command list come back as the
    keep-existing markers, as the admin page sends them. Fields the form keeps
    when omitted are left out.
    """

    def secret(value: str) -> str | None:
        return PRESERVE_SECRET_SENTINEL if value else None

    return {
        "system_id": saved.id,
        "label": label,
        "platform": saved.truenas.platform,
        "truenas_host": saved.truenas.host,
        "api_key": secret(saved.truenas.api_key),
        "api_user": saved.truenas.api_user or None,
        "api_password": secret(saved.truenas.api_password),
        "verify_ssl": saved.truenas.verify_ssl,
        "enclosure_filter": saved.truenas.enclosure_filter,
        "ssh_enabled": saved.ssh.enabled,
        "ssh_host": saved.ssh.host or None,
        "ha_enabled": saved.ssh.ha_enabled,
        "ha_nodes": [node.model_dump(mode="python") for node in saved.ssh.ha_nodes],
        "ssh_port": saved.ssh.port,
        "ssh_user": saved.ssh.user or None,
        "ssh_key_path": saved.ssh.key_path or None,
        "ssh_password": secret(saved.ssh.password),
        "ssh_sudo_password": secret(saved.ssh.sudo_password),
        "ssh_strict_host_key_checking": saved.ssh.strict_host_key_checking,
        "ssh_commands_action": "preserve",
        "ssh_commands_source_system_id": saved.id,
        "bmc_enabled": saved.bmc.enabled,
        "bmc_host": saved.bmc.host or None,
        "bmc_username": saved.bmc.username or None,
        "bmc_password": secret(saved.bmc.password),
        "bmc_verify_ssl": saved.bmc.verify_ssl,
        "bmc_timeout_seconds": saved.bmc.timeout_seconds,
        "default_profile_id": saved.default_profile_id,
        "replace_existing": True,
    }


class _ConfigMixin:
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.config_path = self.root / "config.yaml"

    def _write(self, *systems: SystemConfig) -> None:
        self.config_path.write_text(
            yaml.safe_dump(
                {"systems": [system.model_dump(mode="python", exclude_none=True) for system in systems]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def _raw_saved(self, system_id: str) -> dict[str, Any]:
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        return next(item for item in payload["systems"] if item["id"] == system_id)

    def _saved(self, system_id: str) -> SystemConfig:
        return SystemConfig.model_validate(self._raw_saved(system_id))

    def _save(self, payload: dict[str, Any]) -> SystemConfig:
        service = SystemSetupService(str(self.config_path))
        service.save_system(SystemSetupRequest.model_validate(payload))
        return self._saved(payload["system_id"])


def _limited_system(limit: int = 1) -> SystemConfig:
    return SystemConfig(
        id="lab",
        label="Before",
        truenas=TrueNASConfig(host="https://nas.example.test", api_key="synthetic-api-key", max_concurrent_calls=limit),
    )


class LabelOnlySaveKeepsCallLimitTests(_ConfigMixin, unittest.TestCase):
    def test_service_label_only_save_keeps_max_concurrent_calls(self) -> None:
        self._write(_limited_system(1))

        saved = self._save(_label_only_request(_limited_system(1), "Renamed"))

        self.assertEqual(saved.label, "Renamed")
        self.assertEqual(self._raw_saved("lab")["truenas"].get("max_concurrent_calls"), 1)
        self.assertEqual(effective_max_concurrent_calls(saved.truenas), 1)

    def test_registered_route_label_only_save_keeps_max_concurrent_calls(self) -> None:
        route = next(
            route for route in admin_app.routes
            if route.path == "/api/admin/system-setup" and "POST" in getattr(route, "methods", set())
        )
        self._write(_limited_system(1))
        body = json.loads(json.dumps(_label_only_request(_limited_system(1), "Renamed")))
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        with (
            patch.dict(os.environ, {"APP_CONFIG_PATH": str(self.config_path)}),
            patch("admin_service.routes.get_runtime_service", return_value=MagicMock()),
            patch("admin_service.routes.build_runtime_payload", new=AsyncMock(return_value={})),
            patch("admin_service.routes.record_config_change"),
        ):
            response = asyncio.run(route.endpoint(SystemSetupRequest.model_validate(body)))
            payload = json.loads(response.body)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["updated_existing"])
            get_settings.cache_clear()
            reloaded = next(system for system in get_settings().systems if system.id == "lab")

        self.assertEqual(reloaded.label, "Renamed")
        self.assertEqual(reloaded.truenas.max_concurrent_calls, 1)
        self.assertEqual(effective_max_concurrent_calls(reloaded.truenas), 1)


class FieldClassificationTests(unittest.TestCase):
    def test_every_saved_system_field_is_classified_once(self) -> None:
        groups = (FORM_REPLACES, set(FORM_KEEPS_WHEN_OMITTED), set(NOT_FORM_OWNED))
        classified: set[str] = set()
        for group in groups:
            overlap = classified & set(group)
            self.assertEqual(overlap, set(), f"classified twice: {sorted(overlap)}")
            classified |= set(group)
        actual = _model_field_paths()
        self.assertEqual(
            sorted(actual - classified), [],
            "new system model field(s): classify each in this module and make save_system keep or set it",
        )
        self.assertEqual(sorted(classified - actual), [], "classified field(s) no longer on the model")


class LabelOnlySaveKeepsUnownedFieldsTests(_ConfigMixin, unittest.TestCase):
    def _base_system(self) -> SystemConfig:
        return SystemConfig(
            id="lab",
            label="Before",
            default_profile_id="synthetic-profile",
            truenas=TrueNASConfig(
                host="https://nas.example.test",
                api_key="synthetic-api-key",
                api_user="synthetic-api-user",
                api_password="synthetic-api-password",
                platform="scale",
                verify_ssl=True,
                enclosure_filter="synthetic-enclosure",
            ),
            ssh=SSHConfig(
                enabled=True,
                host="ssh.example.test",
                ha_enabled=True,
                ha_nodes=[HANodeConfig(system_id="node-a", label="Node A", host="node-a.example.test")],
                port=2222,
                user="synthetic-ssh-user",
                key_path=str(self.root / "synthetic_key"),
                password="synthetic-ssh-password",
                sudo_password="synthetic-sudo-password",
                strict_host_key_checking=False,
                commands=["echo synthetic"],
            ),
            bmc=BMCConfig(
                enabled=True,
                host="bmc.example.test",
                username="synthetic-bmc-user",
                password="synthetic-bmc-password",
                verify_ssl=True,
                timeout_seconds=29,
            ),
        )

    def test_every_field_the_form_does_not_own_survives_a_label_only_save(self) -> None:
        samples = {
            path: factory(self.root)
            for path, factory in {**NOT_FORM_OWNED, **FORM_KEEPS_WHEN_OMITTED}.items()
        }
        for path, value in samples.items():
            self.assertNotEqual(value, _default_for(path), f"{path} sample must differ from the model default")
        original = _with_values(self._base_system(), samples)
        for path, value in samples.items():
            self.assertEqual(_get(original, path), value, f"{path} sample did not validate as written")
        self._write(original)

        saved = self._save(_label_only_request(original, "Renamed"))

        self.assertEqual(saved.label, "Renamed")
        for path in sorted(_model_field_paths() - {"label"}):
            with self.subTest(field=path):
                self.assertEqual(_get(saved, path), _get(original, path))


class FormOwnedChangesStillApplyTests(_ConfigMixin, unittest.TestCase):
    """Controls: keeping unowned fields must not stop the form changing or clearing its own."""

    def test_changed_form_fields_still_change(self) -> None:
        self._write(_limited_system(2))
        payload = _label_only_request(_limited_system(2), "Changed")
        payload.update(
            truenas_host="https://other-nas.example.test",
            api_key="replacement-api-key",
            verify_ssl=True,
            ssh_port=2200,
            bmc_timeout_seconds=31,
        )

        saved = self._save(payload)

        self.assertEqual(saved.label, "Changed")
        self.assertEqual(saved.truenas.host, "https://other-nas.example.test")
        self.assertEqual(saved.truenas.api_key, "replacement-api-key")
        self.assertTrue(saved.truenas.verify_ssl)
        self.assertEqual(saved.ssh.port, 2200)
        self.assertEqual(saved.bmc.timeout_seconds, 31)

    def test_fields_the_form_clears_are_still_cleared(self) -> None:
        existing = _limited_system(3).model_copy(
            update={
                "truenas": _limited_system(3).truenas.model_copy(update={"enclosure_filter": "synthetic-enclosure"}),
                "ssh": SSHConfig(
                    enabled=True, host="ssh.example.test", user="synthetic-ssh-user", commands=["echo synthetic"],
                ),
                "bmc": BMCConfig(
                    enabled=True, host="bmc.example.test", username="synthetic-bmc-user",
                    password="synthetic-bmc-password",
                ),
                "storage_views": [
                    StorageViewConfig(id="synthetic-view", label="Synthetic view", kind="manual", template_id="manual-4"),
                ],
            }
        )
        self._write(existing)
        payload = _label_only_request(existing, "Before")
        payload.update(
            enclosure_filter=None,
            ssh_commands_action="replace",
            ssh_commands=[],
            ssh_commands_source_system_id=None,
            bmc_enabled=False,
            bmc_host=None,
            bmc_username=None,
            bmc_password=None,
            storage_views=[],
        )

        saved = self._save(payload)

        self.assertIsNone(saved.truenas.enclosure_filter)
        self.assertNotIn("enclosure_filter", self._raw_saved("lab")["truenas"])
        self.assertEqual(saved.ssh.commands, [])
        self.assertFalse(saved.bmc.enabled)
        self.assertEqual((saved.bmc.host, saved.bmc.username, saved.bmc.password), ("", "", ""))
        self.assertEqual(saved.storage_views, [])


if __name__ == "__main__":
    unittest.main()
