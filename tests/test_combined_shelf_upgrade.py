"""Saved state keyed by a combined SES shelf id from before #1000 (#1006).

The fixtures under tests/fixtures/pre_1000_combined_shelf are a synthetic
mapping store and history rows written the way a pre-#1000 install wrote them:
two same-model 24-bay shelves joined as `A+B`. Every id and serial is made up.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import tests.admin_test_env  # noqa: F401  (must precede admin_service imports)
from admin_service import route_support as admin_route_support
from app import main as app_main
from app import routes as app_routes
from app.config import (
    Settings,
    SSHConfig,
    StorageViewBindingConfig,
    StorageViewConfig,
    SystemConfig,
    TrueNASConfig,
)
from app.models.domain import SourceStatus, StorageViewRuntimePayload
from app.services import inventory as inventory_module
from app.services.combined_shelf_ids import (
    combined_enclosure_parts,
    combined_history_scope_warnings,
    combined_reference_warnings,
    locate_member_slot,
)
from app.services.mapping_store import MappingStore
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.truenas_ws import TrueNASRawData
from history_service.config import HistorySettings
from history_service.domain import SlotStateRecord
from history_service.store import HistoryStore
from tests import core_ses_fixtures as fx

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pre_1000_combined_shelf"
FIRST, SECOND = "500cccc00000010", "500cccc00000020"
COMBINED = f"{FIRST}+{SECOND}"
SSH_OUTPUTS = {"sesutil map": fx.sesutil_map(
    ("ses0", "ExampleCo Shelf 0100", FIRST, 24, {0: "da0"}),
    ("ses1", "ExampleCo Shelf 0100", SECOND, 24, {0: "da24"}),
)}


def _service(temp_dir: str, platform: str = "scale", **system_overrides) -> inventory_module.InventoryService:
    mapping_path = Path(temp_dir) / "slot_mappings.json"
    if not mapping_path.exists():
        shutil.copyfile(FIXTURES / "slot_mappings.json", mapping_path)
    system = SystemConfig(
        id="scale-ssh",
        label="Synthetic SCALE",
        truenas=TrueNASConfig(host="https://nas.example.test", api_key="token", platform=platform),
        ssh=SSHConfig(enabled=True, host="nas.example.test", user="jbodmap"),
        **system_overrides,
    )
    settings = Settings()
    service = inventory_module.InventoryService(
        settings,
        system,
        AsyncMock(),
        AsyncMock(),
        None,
        MappingStore(str(mapping_path)),
        ProfileRegistry(settings),
        SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
    )
    service._get_inventory_source_bundle = AsyncMock(
        return_value=inventory_module.InventorySourceBundle(
            raw_data=TrueNASRawData(enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[]),
            ssh_outputs=SSH_OUTPUTS,
            ssh_collected=True,
            warnings=[],
            sources={
                "api": SourceStatus(enabled=True, ok=False, message="API unreachable"),
                "ssh": SourceStatus(enabled=True, ok=True),
            },
            scale_ses_data=ParsedSSHData(),
            quantastor_ses_data=ParsedSSHData(),
        )
    )
    return service


def _combined_warnings(snapshot) -> list[str]:
    return [warning for warning in snapshot.warnings if COMBINED in warning]


class CombinedShelfIdHelperTests(unittest.TestCase):
    def test_only_the_exact_parser_shape_counts_as_combined(self) -> None:
        self.assertEqual(combined_enclosure_parts(COMBINED), (FIRST, SECOND))
        for value in (None, FIRST, f"{FIRST}+", f"+{FIRST}", f"{FIRST}++{SECOND}", f"{FIRST}+{FIRST}",
                      f"{FIRST}+ {SECOND}", f"{FIRST}::profile+{SECOND}", 7):
            with self.subTest(value=value):
                self.assertIsNone(combined_enclosure_parts(value))

    def test_member_slot_follows_stacking_order_and_never_guesses(self) -> None:
        members = [(FIRST, 24), (SECOND, 24)]
        self.assertEqual(locate_member_slot(0, members), (FIRST, 0))
        self.assertEqual(locate_member_slot(23, members), (FIRST, 23))
        self.assertEqual(locate_member_slot(24, members), (SECOND, 0))
        self.assertEqual(locate_member_slot(47, members), (SECOND, 23))
        for slot in (48, -1, True):
            with self.subTest(slot=slot):
                self.assertIsNone(locate_member_slot(slot, members))
        # An unknown count makes every later offset unknown.
        self.assertEqual(locate_member_slot(3, [(FIRST, 24), (SECOND, None)]), (FIRST, 3))
        self.assertIsNone(locate_member_slot(30, [(FIRST, 24), (SECOND, None)]))
        self.assertIsNone(locate_member_slot(30, [(FIRST, None), (SECOND, 24)]))

    def test_reference_warnings_need_at_least_one_offered_member(self) -> None:
        for offered in ([], ["500dddd00000001"], [FIRST], [SECOND], [FIRST, SECOND], [COMBINED]):
            with self.subTest(offered=offered):
                expected = int(FIRST in offered or SECOND in offered)
                order = combined_reference_warnings(
                    offered_ids=offered, view_order=[f"enclosure:{COMBINED}"], storage_view_bindings=[],
                )
                bindings = combined_reference_warnings(
                    offered_ids=offered, view_order=[], storage_view_bindings=[("pinned-shelf", [COMBINED])],
                )
                self.assertEqual(len(order), expected, order)
                self.assertEqual(len(bindings), expected, bindings)

    def test_reference_warnings_ignore_noncombined_and_malformed_ids(self) -> None:
        for value in (FIRST, f"{FIRST}+", f"+{FIRST}", f"{FIRST}++{SECOND}", f"{FIRST}+{FIRST}",
                      f"{FIRST}+ {SECOND}", f"{FIRST}::profile+{SECOND}"):
            with self.subTest(value=value):
                self.assertEqual(combined_reference_warnings(
                    offered_ids=[FIRST, SECOND], view_order=[f"enclosure:{value}"],
                    storage_view_bindings=[("pinned-shelf", [value])],
                ), [])


class NonCoreCombinedSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_core_combined_selection_names_the_split_shelves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            await service.get_snapshot(force_refresh=True)
            with self.assertRaises(inventory_module.SplitEnclosureError) as raised:
                await service.get_snapshot(selected_enclosure_id=COMBINED)
        # Still an unknown-enclosure error for every caller that tolerates one.
        self.assertIsInstance(raised.exception, inventory_module.UnknownEnclosureError)
        self.assertEqual(raised.exception.member_ids, (FIRST, SECOND))
        self.assertIn("now shown separately", str(raised.exception))

    async def test_combined_id_with_a_retired_shelf_stays_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            await service.get_snapshot(force_refresh=True)
            for selected in (f"{FIRST}+500cccc00000099", f"{FIRST}+{FIRST}", f"{FIRST}++{SECOND}"):
                with self.subTest(selected=selected):
                    with self.assertRaises(inventory_module.UnknownEnclosureError) as raised:
                        await service.get_snapshot(selected_enclosure_id=selected)
                    self.assertNotIsInstance(raised.exception, inventory_module.SplitEnclosureError)

    async def test_core_explicit_combined_selection_still_opens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir, platform="core")
            snapshot = await service.get_snapshot(selected_enclosure_id=COMBINED, force_refresh=True)
        self.assertEqual(snapshot.selected_enclosure_id, COMBINED)
        self.assertEqual(len(snapshot.slots), 48)

    def test_split_error_maps_to_a_404_that_names_the_shelves(self) -> None:
        error = inventory_module.SplitEnclosureError(COMBINED, (FIRST, SECOND))
        response = asyncio.run(app_main.mapped_exception_handler(MagicMock(), error))
        self.assertEqual(response.status_code, 404)
        payload = json.loads(response.body)
        self.assertEqual(payload["error"], "enclosure_split")
        self.assertEqual(payload["enclosure_id"], COMBINED)
        self.assertEqual(payload["member_enclosure_ids"], [FIRST, SECOND])
        self.assertIn(FIRST, payload["detail"])
        self.assertIn(SECOND, payload["detail"])

    def test_inventory_api_returns_the_split_404_not_a_500(self) -> None:
        service = MagicMock()
        service.system = SimpleNamespace(id="scale-ssh", truenas=SimpleNamespace(platform="scale"))
        service.get_snapshot = AsyncMock(side_effect=inventory_module.SplitEnclosureError(COMBINED, (FIRST, SECOND)))
        registry = MagicMock()
        registry.get_service.return_value = service
        application = app_main.create_app()
        messages: list[dict[str, object]] = []

        async def invoke() -> None:
            sent = False

            async def receive() -> dict[str, object]:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message: dict[str, object]) -> None:
                messages.append(message)

            await application(
                {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                    "scheme": "http", "path": "/api/inventory", "raw_path": b"/api/inventory",
                    "query_string": f"enclosure_id={COMBINED}".encode(), "headers": [(b"host", b"testserver")],
                    "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
                },
                receive,
                send,
            )

        with patch.object(app_routes, "get_inventory_registry", return_value=registry):
            asyncio.run(invoke())
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        self.assertEqual(start["status"], 404)
        self.assertEqual(json.loads(body)["member_enclosure_ids"], [FIRST, SECOND])

    def test_index_page_opens_the_first_shelf_with_a_notice(self) -> None:
        settings = Settings(
            systems=[SystemConfig(id="scale-ssh", label="Synthetic SCALE", truenas=TrueNASConfig(platform="scale"))],
            default_system_id="scale-ssh",
        )
        first_snapshot = inventory_module.InventorySnapshot(
            slots=[], selected_system_id="scale-ssh", selected_system_label="Synthetic SCALE",
            selected_system_platform="scale", selected_enclosure_id=FIRST, refresh_interval_seconds=30,
        )
        requested: list[str | None] = []

        async def get_snapshot(*, selected_enclosure_id=None, **_kwargs):
            requested.append(selected_enclosure_id)
            if selected_enclosure_id == COMBINED:
                raise inventory_module.SplitEnclosureError(COMBINED, (FIRST, SECOND))
            return first_snapshot

        service = Mock()
        service.system = SimpleNamespace(id="scale-ssh", truenas=SimpleNamespace(platform="scale"))
        service.get_snapshot = AsyncMock(side_effect=get_snapshot)
        service.get_storage_view_runtime = AsyncMock(
            return_value=StorageViewRuntimePayload(system_id="scale-ssh", views=[])
        )
        registry = Mock()
        registry.get_service.return_value = service
        release_service = Mock()
        release_service.snapshot.return_value = {}
        route = next(route for route in app_main.app.routes if getattr(route, "path", None) == "/")
        request = MagicMock()
        request.query_params = {}
        request.app.state.operator_auth_settings.auth_mode = "network"

        with (
            patch.object(app_routes, "get_settings", return_value=settings),
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "get_release_status_service", return_value=release_service),
            patch.object(app_routes, "resolve_admin_launch_url", return_value=None),
            patch.object(app_routes, "runtime_warnings_for", return_value=[]),
            patch.object(app_routes, "resolve_read_ui_write_policy", return_value={"mode": "network", "enabled": True}),
            patch.object(app_routes, "live_write_policy", return_value={"mode": "network", "enabled": True}),
            patch.object(app_routes.upgrade_notice, "current_notice", return_value=None),
            patch.object(app_routes, "build_index_context", side_effect=lambda **kwargs: kwargs) as build_context,
            patch.object(app_routes.templates, "TemplateResponse", side_effect=lambda _req, _name, ctx: ctx),
        ):
            context = asyncio.run(route.endpoint(request=request, system_id=None, enclosure_id=COMBINED))

        self.assertEqual(requested, [COMBINED, FIRST])
        self.assertEqual(build_context.call_args.kwargs["snapshot"].selected_enclosure_id, FIRST)
        notice = context["system_notice"]
        self.assertIn(COMBINED, notice)
        self.assertIn(f"Showing {FIRST} instead", notice)


class Pre1000MappingFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_mapping_scope_is_reported_with_unambiguous_placements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            before = (Path(temp_dir) / "slot_mappings.json").read_bytes()
            snapshot = await service.get_snapshot(force_refresh=True)
            second = await service.get_snapshot(selected_enclosure_id=SECOND)
            after = (Path(temp_dir) / "slot_mappings.json").read_bytes()

        # Detection never writes the store.
        self.assertEqual(before, after)
        for view in (snapshot, second):
            with self.subTest(view=view.selected_enclosure_id):
                warnings = _combined_warnings(view)
                self.assertEqual(len(warnings), 1, view.warnings)
                warning = warnings[0]
                self.assertIn("3 saved bay assignments", warning)
                self.assertIn("Nothing was moved", warning)
                self.assertIn(f"bay 00 -> {FIRST} bay 00", warning)
                self.assertIn(f"bay 30 -> {SECOND} bay 06", warning)
                # Presence is known; the warning does not compare disk contents.
                self.assertIn(f"bay 23 -> {FIRST} bay 23 (that bay already has a saved assignment)", warning)
        # The combined rows are not applied to either separate shelf.
        first_bay = next(slot for slot in snapshot.slots if slot.slot == 0)
        self.assertNotEqual(first_bay.serial, "SYNTH-SN-0000")
        self.assertNotEqual(first_bay.notes, "Synthetic front-left bay")

    async def test_copied_mapping_warns_of_saved_assignment_without_claiming_a_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            mapping_path = Path(temp_dir) / "slot_mappings.json"
            payload = json.loads(mapping_path.read_text())
            combined_key = MappingStore._encode_v2_key("scale-ssh", COMBINED, 0)
            member_key = MappingStore._encode_v2_key("scale-ssh", FIRST, 0)
            original = payload["slot_mappings"][combined_key]
            copied = {**original, "enclosure_id": FIRST}
            payload["slot_mappings"][member_key] = copied
            mapping_path.write_text(json.dumps(payload))
            before = mapping_path.read_bytes()
            for field in ("serial", "device_name", "notes", "gptid", "source"):
                self.assertEqual(copied[field], original[field])
            snapshot = await service.get_snapshot(force_refresh=True)
            self.assertEqual(mapping_path.read_bytes(), before)

        warning = _combined_warnings(snapshot)[0]
        self.assertIn(f"bay 00 -> {FIRST} bay 00 (that bay already has a saved assignment)", warning)
        self.assertNotIn("different assignment", warning)
        first_bay = next(slot for slot in snapshot.slots if slot.slot == 0)
        self.assertEqual(first_bay.serial, original["serial"])
        self.assertEqual(first_bay.notes, original["notes"])

    async def test_combined_mapping_scope_with_an_unknown_shelf_count_does_not_guess(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            service._get_inventory_source_bundle.return_value.ssh_outputs = {"sesutil map": fx.sesutil_map(
                ("ses0", "ExampleCo Shelf 0100", FIRST, 24, {0: "da0"}),
            )}
            snapshot = await service.get_snapshot(force_refresh=True)

        warning = _combined_warnings(snapshot)[0]
        self.assertIn(f"Not every shelf it joined is reported now ({SECOND})", warning)
        self.assertNotIn("->", warning)

    async def test_no_warning_when_neither_shelf_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            service._get_inventory_source_bundle.return_value.ssh_outputs = {"sesutil map": fx.sesutil_map(
                ("ses0", "ExampleCo Shelf 0200", "500dddd00000001", 12, {}),
            )}
            snapshot = await service.get_snapshot(force_refresh=True)

        self.assertEqual(_combined_warnings(snapshot), [])

    async def test_saved_order_does_not_report_offline_members_as_split(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir, view_order=[f"enclosure:{COMBINED}"])
            bundle = await service._get_inventory_source_bundle()
            bundle.ssh_outputs = {"sesutil map": fx.sesutil_map(
                ("ses0", "ExampleCo Shelf 0200", "500dddd00000001", 12, {}),
            )}
            before = (Path(temp_dir) / "slot_mappings.json").read_bytes()
            snapshot = await service.get_snapshot(force_refresh=True)
            self.assertEqual((Path(temp_dir) / "slot_mappings.json").read_bytes(), before)

        self.assertEqual(_combined_warnings(snapshot), [])
        self.assertEqual(service.system.view_order, [f"enclosure:{COMBINED}"])

    async def test_storage_binding_does_not_report_offline_members_as_split(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir, storage_views=[StorageViewConfig(
                id="pinned-shelf", label="Pinned shelf", kind="ses_enclosure", template_id="ses-auto",
                binding=StorageViewBindingConfig(enclosure_ids=[COMBINED]),
            )])
            bundle = await service._get_inventory_source_bundle()
            bundle.ssh_outputs = {"sesutil map": fx.sesutil_map(
                ("ses0", "ExampleCo Shelf 0200", "500dddd00000001", 12, {}),
            )}
            before = (Path(temp_dir) / "slot_mappings.json").read_bytes()
            snapshot = await service.get_snapshot(force_refresh=True)
            self.assertEqual((Path(temp_dir) / "slot_mappings.json").read_bytes(), before)

        self.assertEqual(_combined_warnings(snapshot), [])
        self.assertEqual(service.system.storage_views[0].binding.enclosure_ids, [COMBINED])

    async def test_core_names_the_combined_view_that_still_opens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir, platform="core")
            default = await service.get_snapshot(force_refresh=True)
            combined = await service.get_snapshot(selected_enclosure_id=COMBINED)

        self.assertIn("That combined view still opens", _combined_warnings(default)[0])
        # The explicit combined view applies its own saved rows and does not
        # report them as orphaned.
        self.assertEqual(_combined_warnings(combined), [])
        self.assertEqual(next(slot for slot in combined.slots if slot.slot == 0).serial, "SYNTH-SN-0000")

    async def test_saved_order_and_storage_view_binding_naming_the_combined_id_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(
                temp_dir,
                view_order=[f"enclosure:{COMBINED}", f"enclosure:{FIRST}"],
                storage_views=[
                    StorageViewConfig(
                        id="pinned-shelf", label="Pinned shelf", kind="ses_enclosure", template_id="ses-auto",
                        binding=StorageViewBindingConfig(enclosure_ids=[COMBINED]),
                    ),
                ],
            )
            snapshot = await service.get_snapshot(force_refresh=True)

        self.assertTrue(any(
            warning.startswith(f"The saved main-page order names the combined enclosure {COMBINED}")
            for warning in snapshot.warnings
        ), snapshot.warnings)
        self.assertTrue(any(
            warning.startswith(f"Storage view pinned-shelf is bound to the combined enclosure {COMBINED}")
            for warning in snapshot.warnings
        ), snapshot.warnings)


class Pre1000HistoryFixtureTests(unittest.TestCase):
    def _store_from_fixture(self, temp_dir: str) -> HistoryStore:
        store = HistoryStore(str(Path(temp_dir) / "history.db"))
        rows = json.loads((FIXTURES / "history_slot_states.json").read_text())["rows"]
        for row in rows:
            store.upsert_slot_state(
                SlotStateRecord(
                    system_id=row["system_id"], system_label=row["system_label"],
                    enclosure_key=row["enclosure_id"], enclosure_id=row["enclosure_id"],
                    enclosure_label=row["enclosure_label"], slot=row["slot"], slot_label=f"{row['slot']:02d}",
                    present=True, state="healthy", identify_active=False, device_name=None,
                    serial=row["serial"], model=None, gptid=None, pool_name=None, vdev_name=None, health=None,
                ),
                row["observed_at"],
            )
        return store

    def _record_member(self, store: HistoryStore, enclosure_id: str) -> None:
        store.upsert_slot_state(
            SlotStateRecord(
                system_id="scale-ssh", system_label="Synthetic SCALE", enclosure_key=enclosure_id,
                enclosure_id=enclosure_id, enclosure_label="ExampleCo Shelf 0100", slot=0, slot_label="00",
                present=True, state="healthy", identify_active=False, device_name=None, serial="SYNTH-SN-NEW",
                model=None, gptid=None, pool_name=None, vdev_name=None, health=None,
            ),
            "2026-10-17T12:00:00+00:00",
        )

    def test_history_scope_is_reported_once_every_shelf_records_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store_from_fixture(temp_dir)
            # Before the upgraded collector records the separate shelves the
            # combined scope may still be a live stack: say nothing.
            self.assertEqual(combined_history_scope_warnings(store.list_scopes()), [])
            self._record_member(store, FIRST)
            self.assertEqual(combined_history_scope_warnings(store.list_scopes()), [])
            self._record_member(store, SECOND)
            warnings = combined_history_scope_warnings(store.list_scopes(include_activity_counts=False))

        self.assertEqual([warning["enclosure_id"] for warning in warnings], [COMBINED])
        self.assertEqual(warnings[0]["code"], "combined_shelf_history_scope")
        self.assertIn("keeps 2 tracked bays", warnings[0]["message"])
        self.assertIn("Nothing was moved", warnings[0]["message"])
        # The Front/Rear stack in the fixture never recorded its halves, so
        # it is a combined view still in use, not an orphan.
        self.assertNotIn("500bbbb00000001", json.dumps(warnings))

    def test_admin_state_warning_reads_the_history_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store_from_fixture(temp_dir)
            self._record_member(store, FIRST)
            self._record_member(store, SECOND)
            history_settings = HistorySettings(sqlite_path=str(Path(temp_dir) / "history.db"))
            with patch("admin_service.route_support.get_history_settings", return_value=history_settings):
                warnings = admin_route_support.build_combined_shelf_history_warnings(history_settings)
            missing = admin_route_support.build_combined_shelf_history_warnings(
                HistorySettings(sqlite_path=str(Path(temp_dir) / "absent.db"))
            )

        self.assertEqual([warning["enclosure_id"] for warning in warnings], [COMBINED])
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
