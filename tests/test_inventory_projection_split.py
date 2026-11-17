"""Seam tests for the inventory projection helpers.

``InventoryService._attach_mapping_revisions``,
``InventoryService._build_platform_capabilities`` and
``InventoryService._resolve_layout_frame`` forward to stateless functions in
``app.services.inventory_projection``. These tests pin what callers and tests
of the service can observe: the method names, signatures and kinds, the order
and timing of every read the code makes on the service, the reads it does not
make on early paths, and that a replacement made on the instance, the class or
the ``inventory`` module still reaches the moved code.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import (
    EnclosureOption,
    EnclosureProfileView,
    InventorySummary,
    PlatformCapability,
    SlotView,
    SourceStatus,
)
from app.services import inventory as inventory_module
from app.services.inventory import InventoryService


REPO_ROOT = Path(__file__).resolve().parents[1]
SAVE_REVISION = "a" * 64
CLEAR_REVISION = "b" * 64
BINDING = "c" * 16

MOVED = {
    "_attach_mapping_revisions": (
        "attach_mapping_revisions",
        "(self, slots: 'list[SlotView]', *, loaded_entries=None) -> 'list[SlotView]'",
    ),
    "_build_platform_capabilities": (
        "build_platform_capabilities",
        "(self, *, slots: 'list[SlotView]', available_enclosures: 'list[EnclosureOption]', "
        "summary: 'InventorySummary', sources: 'dict[str, SourceStatus]', "
        "platform_context: 'dict[str, Any]', selected_profile: 'Any | None' = None) "
        "-> 'dict[str, PlatformCapability]'",
    ),
    "_resolve_layout_frame": (
        "resolve_layout_frame",
        "(self, available_enclosures: 'list[EnclosureOption]', selected_enclosure_id: 'str | None', "
        "warnings: 'list[str]', *, selected_meta: 'dict[str, Any] | None' = None, "
        "selection_warning: 'str | None' = None, profile_warning: 'str | None' = None, "
        "require_selected_option: 'bool' = True, require_profile: 'bool' = True) "
        "-> '_LayoutFrame | _CorrelationResult'",
    ),
}


class _RecordingService(InventoryService):
    """Logs every non-dunder attribute read made on the instance, in order."""

    def __getattribute__(self, name: str):
        if not name.startswith("__"):
            object.__getattribute__(self, "_reads").append(name)
        return object.__getattribute__(self, name)


def _recording_service() -> _RecordingService:
    service = object.__new__(_RecordingService)
    object.__setattr__(service, "_reads", [])
    return service


def _reads(service: _RecordingService) -> list[str]:
    return object.__getattribute__(service, "_reads")


def _slot(slot: int, enclosure_id: str = "enc-a") -> SlotView:
    return SlotView(slot=slot, slot_label=f"{slot:02d}", row_index=0, column_index=slot, enclosure_id=enclosure_id)


def _option() -> EnclosureOption:
    return EnclosureOption(
        id="enc-a",
        label="Enclosure A",
        name="Shelf A",
        rows=2,
        columns=2,
        slot_count=5,
        slot_layout=[[2, None], [0, 1]],
    )


def _profile() -> EnclosureProfileView:
    return EnclosureProfileView(
        id="profile-a",
        label="Profile A",
        rows=2,
        columns=2,
        slot_count=3,
        slot_layout=[[2, None], [0, 1]],
    )


def _layout_service(service: InventoryService | None = None) -> InventoryService:
    service = service if service is not None else object.__new__(InventoryService)
    settings = Settings(systems=[SystemConfig(id="system-a")])
    service.settings = settings
    service.system = settings.systems[0]
    service.profile_registry = MagicMock()
    service.profile_registry.resolve_for_enclosure.return_value = _profile()
    service._legacy_mapping_fallback_allowed = lambda available_enclosures: False
    service._resolve_selected_enclosure_option = lambda options, selected_id, meta: options[0] if options else None
    service._enclosure_option_meta = InventoryService._enclosure_option_meta
    service._correlation_mapping_entries = lambda: {}
    service._warn_unapplied_legacy_mappings = lambda *args, **kwargs: None
    return service


def _capability_kwargs() -> dict:
    return {
        "slots": [_slot(0)],
        "available_enclosures": [_option()],
        "summary": InventorySummary(disk_count=1, enclosure_count=1, mapped_slot_count=1),
        "sources": {"api": SourceStatus(enabled=True, ok=True), "ssh": SourceStatus(enabled=True, ok=False)},
        "platform_context": {},
    }


class ProjectionModuleImportTests(unittest.TestCase):
    def test_projection_module_imports_without_the_inventory_module(self) -> None:
        code = (
            "import sys\n"
            "import app.services.inventory_projection as projection\n"
            "assert 'app.services.inventory' not in sys.modules, 'inventory imported'\n"
            "print(sorted(name for name in ('attach_mapping_revisions', 'build_platform_capabilities',"
            " 'resolve_layout_frame') if callable(getattr(projection, name, None))))\n"
        )
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "['attach_mapping_revisions', 'build_platform_capabilities', 'resolve_layout_frame']",
        )


class ProjectionForwarderShapeTests(unittest.TestCase):
    def test_old_methods_keep_name_signature_kind_and_owner(self) -> None:
        for name, (_helper, signature) in MOVED.items():
            with self.subTest(method=name):
                function = InventoryService.__dict__[name]
                self.assertTrue(inspect.isfunction(function))
                self.assertEqual(function.__module__, "app.services.inventory")
                self.assertEqual(function.__qualname__, f"InventoryService.{name}")
                self.assertEqual(str(inspect.signature(function)), signature)
                self.assertFalse(inspect.iscoroutinefunction(function))

    def test_old_methods_forward_to_the_projection_module_at_call_time(self) -> None:
        from app.services import inventory_projection

        service = _layout_service()
        calls = (
            ("_attach_mapping_revisions", lambda: service._attach_mapping_revisions([_slot(0)])),
            ("_build_platform_capabilities", lambda: service._build_platform_capabilities(**_capability_kwargs())),
            ("_resolve_layout_frame", lambda: service._resolve_layout_frame([_option()], "enc-a", [])),
        )
        for name, call in calls:
            with self.subTest(method=name):
                sentinel = object()
                with patch.object(inventory_projection, MOVED[name][0], return_value=sentinel) as helper:
                    self.assertIs(call(), sentinel)
                helper.assert_called_once()

    def test_projection_helpers_match_the_service_methods(self) -> None:
        from app.services import inventory_projection

        system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="scale"))
        service = object.__new__(InventoryService)
        service.system = system
        direct = inventory_projection.build_platform_capabilities(
            **_capability_kwargs(),
            system=lambda: system,
            PlatformCapability=PlatformCapability,
        )
        self.assertEqual(
            {key: value.model_dump() for key, value in direct.items()},
            {key: value.model_dump() for key, value in service._build_platform_capabilities(**_capability_kwargs()).items()},
        )


class ProjectionServiceReadOrderTests(unittest.TestCase):
    """The exact sequence of service reads, identical before and after the move."""

    def test_attach_mapping_revisions_reads_in_original_order(self) -> None:
        service = _recording_service()
        service.mapping_store = MagicMock()
        service.mapping_store.save_revisions.return_value = defaultdict(lambda: SAVE_REVISION)
        service.mapping_store.clear_revisions.return_value = defaultdict(lambda: CLEAR_REVISION)
        service.system = SimpleNamespace(id="system-a")
        service._slot_disk_binding = InventoryService._slot_disk_binding
        _reads(service).clear()

        service._attach_mapping_revisions([_slot(0), _slot(1)], loaded_entries={})

        self.assertEqual(
            _reads(service),
            [
                "_attach_mapping_revisions",
                "mapping_store",
                "system",
                "mapping_store",
                "system",
                "_slot_disk_binding",
                "_slot_disk_binding",
                "_slot_disk_binding",
                "_slot_disk_binding",
            ],
        )

    def test_build_platform_capabilities_reads_in_original_order(self) -> None:
        service = _recording_service()
        service.system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="core"))

        service._build_platform_capabilities(**_capability_kwargs())

        self.assertEqual(_reads(service), ["_build_platform_capabilities", "system", "system", "system"])

    def test_resolve_layout_frame_reads_in_original_order(self) -> None:
        service = _layout_service(_recording_service())
        _reads(service).clear()

        frame = service._resolve_layout_frame([_option()], "enc-a", [])

        self.assertIsInstance(frame, inventory_module._LayoutFrame)
        self.assertEqual(
            _reads(service),
            [
                "_resolve_layout_frame",
                "_legacy_mapping_fallback_allowed",
                "_resolve_selected_enclosure_option",
                "_enclosure_option_meta",
                "profile_registry",
                "system",
                "_correlation_mapping_entries",
                "_warn_unapplied_legacy_mappings",
            ],
        )

    def test_resolve_layout_frame_fallback_reads_settings_where_it_did(self) -> None:
        service = _layout_service(_recording_service())
        service.profile_registry.resolve_for_enclosure.return_value = None
        service._resolve_selected_enclosure_option = lambda options, selected_id, meta: None
        _reads(service).clear()

        result = service._resolve_layout_frame(
            [],
            None,
            [],
            require_selected_option=False,
            require_profile=False,
        )

        self.assertIsInstance(result, inventory_module._LayoutFrame)
        self.assertEqual(
            _reads(service),
            [
                "_resolve_layout_frame",
                "_legacy_mapping_fallback_allowed",
                "_resolve_selected_enclosure_option",
                "settings",
                "profile_registry",
                "system",
                "settings",
                "settings",
                "settings",
                "settings",
                "_correlation_mapping_entries",
                "_warn_unapplied_legacy_mappings",
            ],
        )

    def test_early_returns_do_not_read_unset_service_state(self) -> None:
        service = object.__new__(InventoryService)
        service._legacy_mapping_fallback_allowed = lambda available_enclosures: False
        service._resolve_selected_enclosure_option = lambda options, selected_id, meta: None
        warnings: list[str] = []

        result = service._resolve_layout_frame([], None, warnings, selection_warning="no enclosure")

        self.assertEqual(result, ([], [], {"id": None, "label": None, "name": None}, [], 0, 0))
        self.assertEqual(warnings, ["no enclosure"])
        for name in ("settings", "system", "profile_registry", "mapping_store"):
            self.assertNotIn(name, vars(service))


class ProjectionReplacementTests(unittest.TestCase):
    """A replacement made the way existing tests make it reaches the moved code."""

    def test_instance_replacement_made_during_the_call_is_used(self) -> None:
        service = _layout_service()
        late = MagicMock(side_effect=lambda options, selected_id, meta: options[0])

        def allowed(available_enclosures):
            service._resolve_selected_enclosure_option = late
            return False

        service._legacy_mapping_fallback_allowed = allowed
        service._resolve_layout_frame([_option()], "enc-a", [])
        late.assert_called_once()

    def test_class_replacement_reaches_the_moved_code(self) -> None:
        service = _layout_service()
        del service._warn_unapplied_legacy_mappings
        warn = MagicMock()
        with patch.object(InventoryService, "_warn_unapplied_legacy_mappings", warn):
            service._resolve_layout_frame([_option()], "enc-a", [])
        warn.assert_called_once()
        self.assertEqual(warn.call_args.args[1], "enc-a")
        self.assertEqual(warn.call_args.kwargs, {"no_identified_physical_enclosure": False})

        slot_binding = MagicMock(return_value=BINDING)
        service.mapping_store = MagicMock()
        service.mapping_store.save_revisions.return_value = defaultdict(lambda: SAVE_REVISION)
        service.mapping_store.clear_revisions.return_value = defaultdict(lambda: CLEAR_REVISION)
        with patch.object(InventoryService, "_slot_disk_binding", slot_binding):
            service._attach_mapping_revisions([_slot(3)])
        self.assertEqual(slot_binding.call_count, 2)

    def test_module_replacement_reaches_the_moved_code(self) -> None:
        service = _layout_service()
        names = (
            "copy_layout_rows",
            "infer_slot_count_from_layout",
            "layout_slot_positions",
            "resolve_physical_mapping_scope",
        )
        replacements = {
            name: MagicMock(side_effect=getattr(inventory_module, name)) for name in names
        }
        with patch.multiple(inventory_module, **replacements):
            service._resolve_layout_frame([_option()], "enc-a", [])
        for name in names:
            with self.subTest(name=name):
                replacements[name].assert_called()

        frame_type = MagicMock(return_value="frame")
        with patch.object(inventory_module, "_LayoutFrame", frame_type):
            self.assertEqual(service._resolve_layout_frame([_option()], "enc-a", []), "frame")

        build_rows = MagicMock(side_effect=inventory_module.build_layout_rows)
        service._resolve_selected_enclosure_option = lambda options, selected_id, meta: None
        service.profile_registry.resolve_for_enclosure.return_value = None
        with patch.object(inventory_module, "build_layout_rows", build_rows):
            service._resolve_layout_frame([], None, [], require_selected_option=False, require_profile=False)
        layout = service.settings.layout
        build_rows.assert_called_once_with(layout.rows, layout.columns, layout.slot_count)

        service.mapping_store = MagicMock()
        service.mapping_store.save_revisions.return_value = defaultdict(lambda: SAVE_REVISION)
        service.mapping_store.clear_revisions.return_value = defaultdict(lambda: CLEAR_REVISION)
        service._slot_disk_binding = lambda slot: BINDING
        bind = MagicMock(side_effect=lambda revision, binding: f"{revision}:{binding}")
        scope = MagicMock(side_effect=lambda enclosure_id: f"scope-{enclosure_id}")
        with patch.multiple(inventory_module, bind_mapping_revision=bind, resolve_physical_mapping_scope=scope):
            revised = service._attach_mapping_revisions([_slot(4)])
        self.assertEqual(revised[0].mapping_revision, f"{SAVE_REVISION}:{BINDING}")
        self.assertEqual(revised[0].mapping_clear_revision, f"{CLEAR_REVISION}:{BINDING}")
        self.assertEqual(
            service.mapping_store.save_revisions.call_args.args[1],
            [("scope-enc-a", 4)],
        )

        capability = MagicMock(side_effect=PlatformCapability)
        service.system = SystemConfig(id="system-a")
        with patch.object(inventory_module, "PlatformCapability", capability):
            service._build_platform_capabilities(**_capability_kwargs())
        self.assertEqual(capability.call_count, 7)


if __name__ == "__main__":
    unittest.main()
