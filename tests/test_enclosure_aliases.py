from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import Mock, patch

from starlette.requests import Request

from app import main as app_main
from app.config import Settings
from app.main import build_index_context, templates
from app.models.domain import (
    EnclosureOption,
    EnclosureProfileView,
    InventorySnapshot,

    SasFabricAliasRequest,
    StorageViewRuntimePayload,
)



def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "app": app_main.app,
        }
    )


class EnclosureAliasRouteTests(unittest.TestCase):
    def test_alias_route_preserves_shared_route_and_invalidates_physical_snapshot_views(self) -> None:
        route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", "") == "/api/sas-fabric/aliases"
            and "POST" in (getattr(route, "methods", None) or set())
        )
        service = Mock()
        service.system.id = "system-a"
        service.system.truenas.platform = "scale"
        service.save_sas_fabric_alias.return_value = {
            "ok": True,
            "cleared": False,
            "alias": {"object_id": "enc-a", "label": "Archive East"},
        }
        registry = Mock()
        registry.get_service.return_value = service
        payload = SasFabricAliasRequest(
            object_id="enc-a::drawer-top",
            object_kind="enclosure",
            label="Archive East",
            scope="system",
        )

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "add_perf_metadata"),
        ):
            response = asyncio.run(
                route.endpoint(
                    payload=payload,
                    system_id="system-a",
                    enclosure_id="enc-a::drawer-top",
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["ok"])
        service.save_sas_fabric_alias.assert_called_once_with(
            object_id="enc-a::drawer-top",
            object_kind="enclosure",
            label="Archive East",
            selected_enclosure_id="enc-a::drawer-top",
            scope="system",
        )
        service.invalidate_physical_enclosure_snapshot_cache.assert_not_called()


class EnclosureAliasServerRenderTests(unittest.TestCase):
    def _html(self, *, snapshot_mode: bool) -> str:
        snapshot = InventorySnapshot(
            slots=[],
            refresh_interval_seconds=30,
            selected_system_id="system-a",
            selected_system_label="System A",
            selected_enclosure_id="enc-b",
            selected_enclosure_label="Archive East",
            selected_profile=EnclosureProfileView(
                id="profile-a",
                label="Raw Profile",
                panel_title="Raw Profile Title",
                rows=1,
                columns=1,
                slot_layout=[[0]],
            ),
            enclosures=[
                EnclosureOption(id="enc-a", label="Shelf A", raw_label="Shelf A"),
                EnclosureOption(id="enc-b", label="Archive East", raw_label="Shelf B", alias="Archive East"),
            ],
        )
        context = build_index_context(
            request=_request(),
            snapshot=snapshot,
            storage_view_runtime=StorageViewRuntimePayload(system_id="system-a", views=[]),
            settings=Settings(),
            history_configured=False,
            snapshot_mode=snapshot_mode,
        )
        return templates.get_template("index.html").render(context)

    def test_multiple_live_enclosures_server_render_selected_option_label(self) -> None:
        html = self._html(snapshot_mode=False)

        title = html.split('id="enclosure-panel-title"', 1)[1].split("</h2>", 1)[0]
        self.assertIn("Archive East", title)
        self.assertNotIn("Raw Profile Title", title)
        self.assertIn('id="enclosure-alias-edit-button"', html)
        self.assertIn("Raw: Shelf B", html)

    def test_snapshot_render_omits_enclosure_alias_editor(self) -> None:
        html = self._html(snapshot_mode=True)

        self.assertNotIn('id="enclosure-alias-edit-button"', html)
        self.assertNotIn('id="enclosure-alias-form"', html)


if __name__ == "__main__":
    unittest.main()
