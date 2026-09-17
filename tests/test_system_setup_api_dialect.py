from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from app.models.domain import SystemSetupRequest
from app.services.system_setup import SystemSetupService


class _DialectConfigMixin:
    """Shared temp-config helpers for the dialect carry-over suites."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "systems": [
                        {
                            "id": "saved-scale",
                            "label": "Saved Scale",
                            "truenas": {
                                "host": "https://nas.example.test",
                                "platform": "scale",
                                "api_dialect": "jsonrpc",
                                "api_version": "v25.10.0",
                            },
                        }
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.service = SystemSetupService(str(self.config_path))

    def _saved_truenas(self, system_id: str) -> dict:
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        system = next(item for item in payload["systems"] if item["id"] == system_id)
        return dict(system["truenas"])

    def _write_systems(self, systems: list[dict]) -> None:
        self.config_path.write_text(
            yaml.safe_dump({"systems": systems}, sort_keys=False),
            encoding="utf-8",
        )
        self.service = SystemSetupService(str(self.config_path))

    @staticmethod
    def _saved_entry(
        system_id: str,
        *,
        endpoint: str = "https://nas.example.test",
        dialect: str,
        version: str,
    ) -> dict:
        return {
            "id": system_id,
            "label": system_id,
            "truenas": {
                "host": endpoint,
                "platform": "scale",
                "api_dialect": dialect,
                "api_version": version,
            },
            "ssh": {"commands": [f"echo {system_id}"]},
        }

    def _request(
        self,
        *,
        system_id: str,
        endpoint: str,
        source_system_id: str | None = None,
        ssh_handling: str = "preserve",
    ) -> SystemSetupRequest:
        """A clone payload.

        `source_system_id` is the dedicated clone handle the admin UI sends
        whenever a loaded system is saved under a new id. `ssh_handling`
        models what the operator did to the SSH command box independently:
        `preserve` keeps the saved list (and is the only case that also
        carries `ssh_commands_source_system_id`), `replace` types a new list,
        and `default` leaves the box untouched on a fresh form.
        """

        extra: dict[str, object] = {}
        if source_system_id is not None:
            extra["clone_source_system_id"] = source_system_id
        if ssh_handling == "preserve" and source_system_id is not None:
            extra["ssh_commands_action"] = "preserve"
            extra["ssh_commands_source_system_id"] = source_system_id
        elif ssh_handling == "replace":
            extra["ssh_commands_action"] = "replace"
            extra["ssh_commands"] = ["echo replaced-by-the-operator"]
        return SystemSetupRequest(
            system_id=system_id,
            label="Cloned Scale",
            platform="scale",
            truenas_host=endpoint,
            api_key="cloned-api-key",
            replace_existing=False,
            **extra,
        )


class SystemSetupApiDialectTests(_DialectConfigMixin, unittest.TestCase):
    """The admin clone flow must not silently move a JSON-RPC host onto DDP."""

    def test_cloning_a_saved_system_keeps_the_jsonrpc_dialect(self) -> None:
        self.service.save_system(self._request(system_id="scale-clone", endpoint="https://nas.example.test"))

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "jsonrpc")
        self.assertEqual(saved.get("api_version"), "v25.10.0")

    def test_cloning_matches_the_host_regardless_of_url_spelling(self) -> None:
        self.service.save_system(self._request(system_id="scale-clone", endpoint="https://NAS.example.test/"))

        self.assertEqual(self._saved_truenas("scale-clone").get("api_dialect"), "jsonrpc")

    def test_a_new_unrelated_host_keeps_the_ddp_default(self) -> None:
        self.service.save_system(self._request(system_id="other-scale", endpoint="https://other.example.test"))

        saved = self._saved_truenas("other-scale")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")


class SystemSetupMixedDialectEndpointTests(_DialectConfigMixin, unittest.TestCase):
    """Two systems on one endpoint must not decide each other's dialect."""

    def test_clone_takes_the_dialect_of_the_system_it_was_cloned_from(self) -> None:
        self._write_systems(
            [
                self._saved_entry("saved-ddp", dialect="ddp", version="current"),
                self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
            ]
        )

        self.service.save_system(
            self._request(
                system_id="scale-clone",
                endpoint="https://nas.example.test",
                source_system_id="saved-jsonrpc",
            )
        )

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "jsonrpc")
        self.assertEqual(saved.get("api_version"), "v25.10.0")

    def test_clone_of_the_ddp_entry_does_not_inherit_the_jsonrpc_neighbour(self) -> None:
        self._write_systems(
            [
                self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
                self._saved_entry("saved-ddp", dialect="ddp", version="current"),
            ]
        )

        self.service.save_system(
            self._request(
                system_id="scale-clone",
                endpoint="https://nas.example.test",
                source_system_id="saved-ddp",
            )
        )

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")

    def test_mixed_dialect_endpoint_without_a_source_fails_closed(self) -> None:
        self._write_systems(
            [
                self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
                self._saved_entry("saved-ddp", dialect="ddp", version="current"),
            ]
        )

        self.service.save_system(
            self._request(system_id="scale-clone", endpoint="https://nas.example.test")
        )

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")

    def test_agreeing_entries_on_one_endpoint_still_carry_the_dialect(self) -> None:
        self._write_systems(
            [
                self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
                self._saved_entry("saved-jsonrpc-2", dialect="jsonrpc", version="v25.10.0"),
            ]
        )

        self.service.save_system(
            self._request(system_id="scale-clone", endpoint="https://nas.example.test")
        )

        self.assertEqual(self._saved_truenas("scale-clone").get("api_dialect"), "jsonrpc")

    def test_a_source_system_on_another_endpoint_does_not_set_the_dialect(self) -> None:
        self._write_systems(
            [
                self._saved_entry(
                    "saved-elsewhere",
                    endpoint="https://other.example.test",
                    dialect="jsonrpc",
                    version="v25.10.0",
                ),
                self._saved_entry("saved-ddp", dialect="ddp", version="current"),
            ]
        )

        self.service.save_system(
            self._request(
                system_id="scale-clone",
                endpoint="https://nas.example.test",
                source_system_id="saved-elsewhere",
            )
        )

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")


class CloneSourceWithoutSshPreservationTests(_DialectConfigMixin, unittest.TestCase):
    """The clone handle is independent of what the operator did to SSH commands.

    `ssh_commands_source_system_id` only exists while the redacted command
    list is being preserved. Replacing or defaulting the commands used to
    erase the clone's source identity, which silently persisted DDP on a
    mixed-dialect endpoint instead of inheriting the loaded system.
    """

    MIXED = ("saved-jsonrpc", "saved-ddp")

    def _write_mixed(self, first: str) -> None:
        entries = {
            "saved-jsonrpc": self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
            "saved-ddp": self._saved_entry("saved-ddp", dialect="ddp", version="current"),
        }
        order = [first] + [name for name in self.MIXED if name != first]
        self._write_systems([entries[name] for name in order])

    def test_a_clone_that_replaces_the_ssh_commands_still_inherits_its_source(self) -> None:
        for stored_first in self.MIXED:
            for source, dialect, version in (
                ("saved-jsonrpc", "jsonrpc", "v25.10.0"),
                ("saved-ddp", "ddp", "current"),
            ):
                with self.subTest(stored_first=stored_first, source=source):
                    self._write_mixed(stored_first)
                    self.service.save_system(
                        self._request(
                            system_id=f"clone-{stored_first}-{source}",
                            endpoint="https://nas.example.test",
                            source_system_id=source,
                            ssh_handling="replace",
                        )
                    )
                    saved = self._saved_truenas(f"clone-{stored_first}-{source}")
                    self.assertEqual(saved.get("api_dialect"), dialect)
                    self.assertEqual(saved.get("api_version"), version)

    def test_a_clone_that_defaults_the_ssh_commands_still_inherits_its_source(self) -> None:
        for stored_first in self.MIXED:
            for source, dialect, version in (
                ("saved-jsonrpc", "jsonrpc", "v25.10.0"),
                ("saved-ddp", "ddp", "current"),
            ):
                with self.subTest(stored_first=stored_first, source=source):
                    self._write_mixed(stored_first)
                    self.service.save_system(
                        self._request(
                            system_id=f"default-{stored_first}-{source}",
                            endpoint="https://nas.example.test",
                            source_system_id=source,
                            ssh_handling="default",
                        )
                    )
                    saved = self._saved_truenas(f"default-{stored_first}-{source}")
                    self.assertEqual(saved.get("api_dialect"), dialect)
                    self.assertEqual(saved.get("api_version"), version)

    def test_preserving_ssh_commands_no_longer_decides_the_dialect(self) -> None:
        """Only the clone handle may speak; the SSH handle must not.

        The payload preserves `saved-jsonrpc`'s command list but names
        `saved-ddp` as the system it was cloned from. The dialect follows the
        clone handle.
        """

        self._write_mixed("saved-jsonrpc")
        request = SystemSetupRequest(
            system_id="split-handles",
            label="Cloned Scale",
            platform="scale",
            truenas_host="https://nas.example.test",
            api_key="cloned-api-key",
            replace_existing=False,
            clone_source_system_id="saved-ddp",
            ssh_commands_action="preserve",
            ssh_commands_source_system_id="saved-jsonrpc",
        )
        self.service.save_system(request)

        saved = self._saved_truenas("split-handles")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")

    def test_an_unknown_clone_source_fails_closed_on_a_mixed_endpoint(self) -> None:
        self._write_mixed("saved-jsonrpc")

        self.service.save_system(
            self._request(
                system_id="ghost-source",
                endpoint="https://nas.example.test",
                source_system_id="no-such-system",
                ssh_handling="default",
            )
        )

        saved = self._saved_truenas("ghost-source")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")

    def test_a_foreign_clone_source_fails_closed_on_a_mixed_endpoint(self) -> None:
        self._write_systems(
            [
                self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
                self._saved_entry("saved-ddp", dialect="ddp", version="current"),
                self._saved_entry(
                    "saved-elsewhere",
                    endpoint="https://other.example.test",
                    dialect="jsonrpc",
                    version="v25.10.0",
                ),
            ]
        )

        self.service.save_system(
            self._request(
                system_id="foreign-source",
                endpoint="https://nas.example.test",
                source_system_id="saved-elsewhere",
                ssh_handling="default",
            )
        )

        saved = self._saved_truenas("foreign-source")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")

    def test_a_re_save_under_the_same_id_ignores_a_stale_clone_handle(self) -> None:
        self._write_mixed("saved-jsonrpc")

        request = SystemSetupRequest(
            system_id="saved-jsonrpc",
            label="Saved Scale",
            platform="scale",
            truenas_host="https://nas.example.test",
            api_key="rotated-api-key",
            replace_existing=True,
            clone_source_system_id="saved-ddp",
        )
        self.service.save_system(request)

        saved = self._saved_truenas("saved-jsonrpc")
        self.assertEqual(saved.get("api_dialect"), "jsonrpc")
        self.assertEqual(saved.get("api_version"), "v25.10.0")


if __name__ == "__main__":
    unittest.main()
