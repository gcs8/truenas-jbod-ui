from __future__ import annotations

import json
import re
import unittest
from typing import Any

from markupsafe import Markup
from starlette.datastructures import URLPath
from starlette.requests import Request

from app.config import Settings
from app.main import build_index_context, templates
from app.models.domain import (
    EnclosureOption,
    InventorySnapshot,
    InventorySummary,
    SasFabricSnapshot,
    SlotState,
    SlotView,
    SourceStatus,
    StorageViewRuntimePayload,
    SystemOption,
)
from app.script_json import (
    SCRIPT_JSON_FILTER_NAME,
    register_script_json_filters,
    script_safe_json,
    script_safe_json_text,
)
from app.services.snapshot_export import (
    EXPORT_HISTORY_CACHE,
    EXPORT_RENDER_CACHE,
    EXPORT_ZIP_CACHE,
    SnapshotExportService,
)

# Every string here is hostile to a raw <script> JSON embed: script-closing text, HTML
# comment openers, ampersands, quotes, and the two JavaScript line separators.
HOSTILE_TEXT = "</script><script>alert(1)</script><!-- & ' \"" + chr(0x2028) + chr(0x2029) + " tail"

SCRIPT_BLOCK_RE = re.compile(r"<script>(.*?)</script>", re.DOTALL)


class FakeHistoryBackend:
    configured = False

    async def fetch_slot_history(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"scope": {}, "metrics": [], "events": [], "counts": {}}

    async def fetch_history_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"counts": {}, "collector": {}}


def build_request(path: str = "/") -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "root_path": "",
            "app": None,
        }
    )
    request.scope["app"] = type(
        "FakeApp",
        (),
        {"url_path_for": lambda _, name, **params: URLPath(f"/static/{params['path']}")},
    )()
    return request


def build_hostile_snapshot() -> InventorySnapshot:
    return InventorySnapshot(
        slots=[
            SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id="front",
                enclosure_label=HOSTILE_TEXT,
                present=True,
                state=SlotState.healthy,
                device_name="da0",
                serial="SER0001",
                model=HOSTILE_TEXT,
                size_human="1 TB",
                pool_name="tank",
                vdev_name="raidz2-0",
                health="ONLINE",
            )
        ],
        layout_rows=[[0]],
        layout_slot_count=1,
        layout_columns=1,
        refresh_interval_seconds=30,
        selected_system_id="system-a",
        selected_system_label=HOSTILE_TEXT,
        selected_enclosure_id="front",
        selected_enclosure_label=HOSTILE_TEXT,
        systems=[SystemOption(id="system-a", label=HOSTILE_TEXT, platform="core")],
        enclosures=[
            EnclosureOption(id="front", label=HOSTILE_TEXT, rows=1, columns=1, slot_count=1, slot_layout=[[0]])
        ],
        sources={"api": SourceStatus(enabled=True, ok=True, message=HOSTILE_TEXT)},
        summary=InventorySummary(
            disk_count=1,
            pool_count=1,
            enclosure_count=1,
            mapped_slot_count=1,
            manual_mapping_count=0,
            ssh_slot_hint_count=0,
        ),
        warnings=[HOSTILE_TEXT],
    )


def extract_bootstrap_object(html: str, marker: str) -> dict[str, Any]:
    """Locate the inline bootstrap script and parse it as JSON.

    The bootstrap blocks are JavaScript object literals; every value is JSON, and the
    keys are unquoted identifiers, so quoting the keys yields a JSON document. That
    parse doubles as the assertion that no `</script>` inside the payload split the
    element early.
    """

    for block in SCRIPT_BLOCK_RE.findall(html):
        if marker not in block:
            continue
        body = block.strip()
        assert body.startswith(marker), body[:80]
        literal = body[len(marker):].strip().rstrip(";")
        literal = re.sub(r"(?m)^(\s*)([A-Za-z_][A-Za-z0-9_]*):", r'\1"\2":', literal)
        literal = re.sub(r",(\s*[}\]])", r"\1", literal)
        return json.loads(literal)
    raise AssertionError(f"bootstrap marker {marker!r} not found in a single <script> block")


class ScriptSafeJsonHelperTests(unittest.TestCase):
    def test_hardens_every_script_boundary_character(self) -> None:
        text = script_safe_json({"value": HOSTILE_TEXT})

        for forbidden in ("<", ">", "&", "'", chr(0x2028), chr(0x2029)):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("</script", text.lower())
        self.assertIn("\\u003c/script\\u003e", text)
        self.assertIn("\\u2028", text)
        self.assertIn("\\u2029", text)

    def test_round_trips_to_the_original_value(self) -> None:
        payload = {"value": HOSTILE_TEXT, "nested": [HOSTILE_TEXT, {"k": HOSTILE_TEXT}], "n": 1, "b": False}

        self.assertEqual(json.loads(script_safe_json(payload)), payload)

    def test_hardens_non_ascii_json_text_too(self) -> None:
        raw = json.dumps({"value": HOSTILE_TEXT}, ensure_ascii=False)
        self.assertIn(chr(0x2028), raw)

        hardened = script_safe_json_text(raw)

        self.assertNotIn(chr(0x2028), hardened)
        self.assertNotIn(chr(0x2029), hardened)
        self.assertNotIn("<", hardened)
        self.assertEqual(json.loads(hardened), {"value": HOSTILE_TEXT})

    def test_is_idempotent(self) -> None:
        once = script_safe_json({"value": HOSTILE_TEXT})

        self.assertEqual(script_safe_json_text(once), once)

    def test_leaves_structural_json_untouched(self) -> None:
        payload = {"a": [1, 2.5, None, True], "b": {"c": "plain"}}
        text = json.dumps(payload)

        self.assertEqual(script_safe_json_text(text), text)

    def test_filter_returns_markup_and_treats_none_as_null(self) -> None:
        env = type("Env", (), {"filters": {}})()
        register_script_json_filters(env)
        script_filter = env.filters[SCRIPT_JSON_FILTER_NAME]

        rendered = script_filter(json.dumps({"value": HOSTILE_TEXT}))
        self.assertIsInstance(rendered, Markup)
        self.assertNotIn("<", str(rendered))
        self.assertEqual(json.loads(str(rendered)), {"value": HOSTILE_TEXT})
        self.assertEqual(str(script_filter(None)), "null")

    def test_app_template_environment_registers_the_filter(self) -> None:
        self.assertIn(SCRIPT_JSON_FILTER_NAME, templates.env.filters)

    def test_bootstrap_templates_do_not_pass_json_through_safe(self) -> None:
        for name in ("index.html", "sas_fabric.html"):
            source, _, _ = templates.env.loader.get_source(templates.env, name)
            self.assertNotRegex(source, r"_json\s*\|\s*safe", f"{name} still passes pre-serialized JSON through |safe")


class MainViewBootstrapTests(unittest.TestCase):
    def test_index_bootstrap_survives_hostile_values(self) -> None:
        snapshot = build_hostile_snapshot()
        runtime = StorageViewRuntimePayload(system_id="system-a", system_label=HOSTILE_TEXT, views=[])
        context = build_index_context(
            request=build_request("/"),
            snapshot=snapshot,
            storage_view_runtime=runtime,
            settings=Settings(),
            history_configured=False,
            snapshot_export_meta_json=json.dumps({"label": HOSTILE_TEXT}),
            preloaded_history_json=json.dumps({"0": {"note": HOSTILE_TEXT}}),
            initial_selected_slot_json=json.dumps(0),
            initial_history_io_chart_mode_json=json.dumps("total"),
        )

        html = templates.get_template("index.html").render(context)

        bootstrap = extract_bootstrap_object(html, "window.APP_BOOTSTRAP =")
        self.assertEqual(bootstrap["snapshot"]["selected_system_label"], HOSTILE_TEXT)
        self.assertEqual(bootstrap["snapshot"]["slots"][0]["model"], HOSTILE_TEXT)
        self.assertEqual(bootstrap["snapshot"]["warnings"], [HOSTILE_TEXT])
        self.assertEqual(bootstrap["storageViewsRuntime"]["system_label"], HOSTILE_TEXT)
        self.assertEqual(bootstrap["snapshotExportMeta"], {"label": HOSTILE_TEXT})
        self.assertEqual(bootstrap["preloadedHistoryBySlot"], {"0": {"note": HOSTILE_TEXT}})
        self.assertEqual(bootstrap["initialSelectedSlot"], 0)
        self.assertEqual(bootstrap["initialHistoryIoChartMode"], "total")
        self.assertEqual(bootstrap["preloadedHistorySummary"], {"counts": {}, "collector": {}})

        script_body = next(block for block in SCRIPT_BLOCK_RE.findall(html) if "window.APP_BOOTSTRAP" in block)
        self.assertNotIn("<", script_body)
        self.assertNotIn(chr(0x2028), script_body)
        self.assertNotIn(chr(0x2029), script_body)


class StorageFabricBootstrapTests(unittest.TestCase):
    def test_fabric_bootstrap_survives_hostile_values(self) -> None:
        snapshot = build_hostile_snapshot()
        fabric = SasFabricSnapshot(available=False, system_id="system-a", system_label=HOSTILE_TEXT)
        bootstrap = {
            "snapshot": snapshot.model_dump(mode="json"),
            "fabric": fabric.model_dump(mode="json"),
            "systemId": "system-a",
            "enclosureId": "front",
            "appVersion": "0.0.0-test",
        }

        html = templates.get_template("sas_fabric.html").render(
            {
                "request": build_request("/sas-fabric"),
                "snapshot": snapshot,
                "fabric": fabric,
                "settings": Settings(),
                "app_version": "0.0.0-test",
                "bootstrap_json": json.dumps(bootstrap),
            }
        )

        parsed = extract_bootstrap_object(html, "window.SAS_FABRIC_BOOTSTRAP =")
        self.assertEqual(parsed["fabric"]["system_label"], HOSTILE_TEXT)
        self.assertEqual(parsed["snapshot"]["selected_enclosure_label"], HOSTILE_TEXT)
        script_body = next(block for block in SCRIPT_BLOCK_RE.findall(html) if "SAS_FABRIC_BOOTSTRAP" in block)
        self.assertNotIn("<", script_body)
        self.assertNotIn(chr(0x2028), script_body)


class SnapshotExportBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        EXPORT_HISTORY_CACHE.clear()
        EXPORT_RENDER_CACHE.clear()
        EXPORT_ZIP_CACHE.clear()

    async def test_offline_export_bootstrap_survives_hostile_values(self) -> None:
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)  # type: ignore[arg-type]

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request("/"),
            snapshot=build_hostile_snapshot(),
            smart_summary_cache={"0": {"available": True, "model": HOSTILE_TEXT}},
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
        )

        bootstrap = extract_bootstrap_object(rendered.html, "window.APP_BOOTSTRAP =")
        self.assertTrue(bootstrap["snapshotMode"])
        self.assertEqual(bootstrap["snapshot"]["selected_system_label"], HOSTILE_TEXT)
        self.assertEqual(bootstrap["snapshot"]["slots"][0]["model"], HOSTILE_TEXT)
        self.assertEqual(bootstrap["preloadedSmartSummariesBySlot"]["0"]["model"], HOSTILE_TEXT)
        script_body = next(block for block in SCRIPT_BLOCK_RE.findall(rendered.html) if "window.APP_BOOTSTRAP" in block)
        self.assertNotIn("<", script_body)
        self.assertNotIn(chr(0x2028), script_body)


if __name__ == "__main__":
    unittest.main()
