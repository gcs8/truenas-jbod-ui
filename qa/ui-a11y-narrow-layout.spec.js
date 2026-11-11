"use strict";

// Accessibility and narrow-layout regressions found by the UI coverage crawl:
// - #1110 bay tiles and the Rename button: the accessible name contains the
//   visible text (axe label-content-name-mismatch), punctuation-only and
//   non-ASCII labels included (Chromium's own accessibility tree).
// - #1111 Disk Path path-member facts: valid dl > div > dt + dd markup (axe
//   definition-list, dlitem).
// - #1112 ellipsized Connections, focus-strip, topology and multipath-path
//   text carries its full text in a title; each control is a Tab stop with a
//   focus ring and unfolds while focused; path pills open and close with
//   Enter and Space.
// - #1124 a UniFi bay's state chip clears its heat value and text lines.
// - #1114, #1115, #1109, #1116 and the header part of #1102 at 390 px: no
//   overlapping controls, no page-wide sideways scroll, chassis faces scroll
//   inside their own shell, heat values stay on one line.
// The UI is a desktop application and 390 px is not a supported layout
// (CONTRIBUTING.md). The 390 px checks only keep the coverage crawl's
// best-effort findings fixed; desktop widths are checked for the same
// invariants so the narrow fixes cannot move them. Complete production
// templates and assets; only HTTP and the data are synthetic.
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const ORIGIN = "https://synthetic.invalid";
const AXE_SOURCE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const NARROW = { width: 390, height: 900 };
const DESKTOP = [{ width: 1600, height: 1000 }, { width: 1920, height: 1080 }, { width: 3840, height: 2160 }];
const SCREENSHOT_DIR = process.env.UI_A11Y_SCREENSHOT_DIR || "";
const STORAGE_VIEWS = ["boot-doms", "nvme-carrier-x4", "manual-group"];
// Plain UniFi plus every built-in UniFi model face, by face variant, with
// its bay count.
const UNIFI_FACES = [["unifi-plain", 8], ["unvr", 4], ["unvr-pro", 7], ["unvr-g2", 4], ["unvr-g2-pro", 8], ["envr", 16], ["envr-core", 16]];

const FIXTURE_SCRIPT = `
import asyncio
import json

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

import scripts.build_current_source_browser_fixture as builder
from app import __version__
from app.config import Settings
from app.models.domain import InventorySnapshot, MultipathView, StorageViewRuntimePayload
from app.services.public_demo_fixture import build_public_demo_snapshot_bundle, load_public_demo_fixture

WRITE_POLICY = {"enabled": True, "mode": "network", "reason": "", "public_origin": None}
dump = lambda model: model.model_dump(mode="json")
bundle = build_public_demo_snapshot_bundle(fixture=load_public_demo_fixture(), settings=Settings())
empty_runtime = StorageViewRuntimePayload(system_id="synthetic-system", views=[])


def main_page(snapshot, runtime):
    return builder.TEMPLATES.env.get_template("index.html").render({
        "request": builder.build_fixture_request(), "snapshot": snapshot, "storage_view_runtime": runtime,
        "settings": Settings(), "initial_snapshot_json": json.dumps(dump(snapshot)),
        "initial_storage_view_runtime_json": json.dumps(dump(runtime)), "history_configured": False,
        "app_version": __version__, "release_status": {}, "snapshot_mode": False, "sas_fabric_view_url": "/sas-fabric",
        "snapshot_export_meta": {}, "snapshot_export_meta_json": "null", "preloaded_history_json": "{}",
        "preloaded_smart_summary_json": "{}", "preloaded_snapshots_json": "{}",
        "preloaded_snapshot_smart_summary_json": "{}", "preloaded_storage_view_smart_summary_json": "{}",
        "preloaded_history_summary_json": '{"counts": {}, "collector": {}}', "initial_selected_slot_json": "null",
        "initial_selected_storage_view_id_json": "null", "initial_history_timeframe_hours_json": "24",
        "initial_history_panel_open_json": "false", "initial_history_io_chart_mode_json": '"total"',
        "admin_launch_url": None, "write_policy_json": json.dumps(WRITE_POLICY),
    })


# The 60-bay CORE demo: a flat-grouped top loader, a SATADOM boot view drawn
# as a row group of fixed cards, a four-card NVMe carrier canvas, and a manual
# four-bay group with one matched disk and three empty runtime cards.
runtime = dump(bundle.storage_view_runtime)
manual = json.loads(json.dumps(runtime["views"][0]))
manual.update(id="manual-group", label="Demo Manual Group", kind="manual", template_id="manual-4",
              panel_title="Demo Manual Group", slot_layout=[[0, 1, 2, 3]], slot_count=4, matched_count=1, order=30)
first = manual["slots"][0]
manual["slots"] = [{**first, "slot_index": 0, "slot_label": "M-1"}] + [
    {**first, "slot_index": index, "slot_label": f"M-{index + 1}", "occupied": False, "state": "empty",
     "device_name": None, "smart_device_names": [], "serial": None, "pool_name": None, "model": None,
     "size_bytes": None, "size_human": None, "gptid": None, "health": None, "temperature_c": None,
     "placement_key": None, "candidate_id": None}
    for index in range(1, 4)
]
runtime["views"].append(manual)
demo_runtime = StorageViewRuntimePayload.model_validate(runtime)
demo = bundle.primary_snapshot
demo = demo.model_copy(update={"slots": [slot.model_copy(update={"led_supported": True}) for slot in demo.slots]})
# Bay 03 has two paths; a long controller label makes its path pills cut off.
multipath = MultipathView(name="synthetic-mpath", device_name="multipath/synthetic-03", mode="ACTIVE/PASSIVE", state="OPTIMAL", members=[
    {"device_name": "demo-disk-03", "state": state, "controller_label": f"demo-hba-{index} CPU2 SLOT7 PCI-E 4.0 X16 (synthetic riser B) firmware 24.00.00.00-SYNTHETIC"}
    for index, state in enumerate(("ACTIVE", "PASSIVE"))
])
demo = demo.model_copy(update={"slots": [slot.model_copy(update={"multipath": multipath}) if slot.slot == 3 else slot for slot in demo.slots]})
# Linux mount labels that are punctuation only, or not ASCII (#1110).
MOUNTS = ["/--", "/", "/数据", "/var", "/._-"]
mounts = dump(demo)
mounts["selected_system_platform"] = "linux"
mounts["systems"][0]["platform"] = "linux"
for index, label in enumerate(MOUNTS):
    mounts["slots"][index]["pool_name"] = label
mounts = InventorySnapshot.model_validate(mounts)

# Long controller facts, so Connections and the focus strip must ellipsize.
fabric = dump(bundle.sas_fabric)
for node in fabric["nodes"]:
    if node["kind"] == "controller":
        node["metrics"].update(pcie_slot="CPU2 SLOT7 PCI-E 4.0 X16 (synthetic riser B)", firmware="24.00.00.00-SYNTHETIC-LONG-BUILD")
        node["raw"].update(board="SYNTHETIC 9500-16e TRI-MODE STORAGE ADAPTER")
for trace in fabric["traces"]:
    if trace["kind"] == "bay":
        slot = trace["slots"][0]
        trace["metrics"]["path_states"] = [
            {"controller": controller, "state": state, "device_name": f"demo-disk-{slot:02d}", "source": "ses",
             "path_type": "SAS", "ses_device": f"ses{index}", "sg_device": f"sg{slot + index}",
             "scsi_hctl": f"{index}:0:{slot}:0", "transport_protocol": "SAS"}
            for index, (controller, state) in enumerate((("demo-hba-0", "active"), ("demo-hba-1", "passive")))
        ]
fabric_html = builder.TEMPLATES.env.get_template("sas_fabric.html").render(
    request=builder.build_fixture_request(), snapshot=demo, fabric=fabric, settings=Settings(), app_version=__version__,
    bootstrap_json=json.dumps({"snapshot": dump(demo), "fabric": fabric, "systemId": demo.selected_system_id,
                               "enclosureId": demo.selected_enclosure_id, "appVersion": __version__,
                               "writePolicy": WRITE_POLICY}),
)


def variant(profile_id, title, face_style, rows, columns, row_groups=(), bay_size="3.5", layout=None, **profile):
    data = builder.build_synthetic_live_snapshot().model_dump(mode="json")
    layout = layout or [list(range(row * columns, row * columns + columns)) for row in range(rows)]
    count = sum(1 for row in layout for slot in row if slot is not None)
    template = data["slots"][0]
    data["slots"] = [
        {**template, "slot": index, "slot_label": f"{index:02d}", "row_index": next(number for number, row in enumerate(layout) if index in row),
         "column_index": next(row.index(index) for row in layout if index in row), "device_name": f"sd{index}", "serial": f"SANITIZED-NARROW-{index:02d}",
         "gptid": f"synthetic-gptid-{index}", "pool_name": "synthetic-pool", "notes": None}
        for index in range(count)
    ]
    data.update(layout_rows=layout, layout_slot_count=count, layout_columns=columns, selected_system_platform="core")
    data["systems"][0]["platform"] = "core"
    data["selected_profile"] = {
        **data["selected_profile"], "id": profile_id, "label": title, "panel_title": title, "face_style": face_style,
        "bay_size": bay_size, "rows": rows, "columns": columns, "slot_count": count, "slot_layout": layout,
        "row_groups": list(row_groups), **profile,
    }
    data["enclosures"][0].update(profile_id=profile_id, rows=rows, columns=columns, slot_count=count, slot_layout=layout)
    data["summary"].update(disk_count=count, mapped_slot_count=count, manual_mapping_count=0)
    return InventorySnapshot.model_validate(data)


env = Environment(loader=FileSystemLoader(str(builder.ROOT / "history_service" / "templates")), autoescape=True)
env.filters["script_json_text"] = lambda text: Markup(str(text).replace("<", "\\\\u003c"))
status = {"collector_running": True, "collection_running": False, "last_completed_at": "2026-01-01T00:00:00+00:00",
          "background_consecutive_failures": 0, "last_retention_rows_removed": 0, "last_collection_duration_seconds": 12.5}
history_html = env.get_template("dashboard.html").render(
    request=builder.build_fixture_request(), app_name="TrueNAS JBOD Enclosure UI", app_version=__version__,
    status=status, counts={"tracked_slots": 120, "metric_sample_count": 960000, "metric_rollup_count": 120, "event_count": 2400},
    scopes=[{"system_id": f"synthetic-system-{index}", "system_label": f"Synthetic Storage System {index}",
             "enclosure_id": f"synthetic-enclosure-{index}", "enclosure_label": f"Synthetic Front 24 Bay Shelf {index}",
             "tracked_slots": 60, "metric_sample_count": 480000, "event_count": 1200,
             "last_seen_at": "2026-01-01T00:00:00+00:00"} for index in range(2)],
    database_size_label="12.0 MiB", reclaimable_label="none", degraded_reason=None,
    backup_footprint_label="2 backups, 24.0 MiB", release_summary="Checking for updates...", latest_url=None,
    backoff_label="inactive", collector_state_label="idle", skipped_scopes_label="none", current_collection_label="no",
    collector_banner_text="", direct_refresh_enabled=True, last_collection_duration_label="12.5s",
    last_background_overrun_label="no", last_retention_duration_label="0.4s", last_inventory_mode="cached inventory",
    format_count=lambda value: f"{value:,}" if isinstance(value, int) else str(value),
    refresh={"full_refresh_cooldown_seconds_remaining": 0}, status_json=json.dumps(status),
    url_for=lambda name, path: "/static/" + path,
)

pages = {"demo": (demo, demo_runtime), "mounts": (mounts, empty_runtime)}
for name, snapshot in {
    "grouped": variant("synthetic-grouped-12", "Synthetic Grouped 12 Bay", "front-drive", 1, 12, (4, 4, 4)),
    # A plain UniFi face (no model print).
    "unifi-plain": variant("synthetic-unifi-8", "Synthetic UniFi 8 Bay", "unifi-drive", 2, 4),
}.items():
    pages[name] = (snapshot, empty_runtime)
# Every built-in UniFi model face (UNVR, UNVR Pro, UNVR-G2, UNVR-G2-Pro, ENVR,
# ENVR Core) with its shipped rows, bay layout and latch, keyed by variant.
from app.services.profile_registry import ProfileRegistry
for profile in ProfileRegistry(Settings()).list_profiles():
    if profile.face_style == "unifi-drive" and profile.face_variant:
        pages[profile.face_variant] = (variant(
            profile.id, profile.label, profile.face_style, profile.rows, profile.columns, bay_size=profile.bay_size,
            layout=profile.slot_layout, face_variant=profile.face_variant, latch_edge=profile.latch_edge,
            slot_number_base=profile.slot_number_base), empty_runtime)
# #1157: the other chassis face shapes (the SCALE 24-bay front, top- and
# side-latched sleds, top loader, generic and drawer) for the bay label fit
# check, and an ENVR Core face whose device and pool names are too long to fit.
for name, face_style, latch, size, rows, columns in [
    ("scale-front-24", "front-drive", "right", "3.5", 6, 4), ("front-top-3.5", "front-drive", "top", "3.5", 2, 4),
    ("front-top-2.5", "front-drive", "top", "2.5", 1, 24), ("rear-left-2.5", "rear-drive", "left", "2.5", 2, 12),
    ("rear-right-2.5", "rear-drive", "right", "2.5", 2, 12), ("top-loader-2.5", "top-loader", "bottom", "2.5", 3, 4),
    ("generic-3.5", "generic", "bottom", "3.5", 1, 6), ("drawer-3.5", "drawer", "bottom", "3.5", 2, 6),
]:
    pages[name] = (variant(f"synthetic-{name}", f"Synthetic {name}", face_style, rows, columns, bay_size=size, latch_edge=latch), empty_runtime)
long_labels = dump(pages["envr-core"][0])
for slot in long_labels["slots"]:
    slot.update(device_name=f"synthetic-long-device-name-{slot['slot']:02d}", pool_name="synthetic-pool-with-a-long-descriptive-name")
pages["envr-core-long"] = (InventorySnapshot.model_validate(long_labels), empty_runtime)
# The admin sidecar as the coverage crawl renders it (#1146), with the
# shipped layout catalog in the builder.
import scripts.build_ui_crawl_fixture as crawl_fixture
from admin_service.route_support import serialize_profiles
RUNTIME = [("enclosure-ui", "Main UI", "The main page you normally use.", "truenas-jbod-ui"),
           ("history", "History", "Records disk history and charts.", "truenas-jbod-history"),
           ("admin", "Admin", "This admin page.", "truenas-jbod-admin"),
           ("backup-scheduler", "Backup scheduler", "Runs scheduled backups.", "truenas-jbod-backup-scheduler")]
# The backup and debug path pills with their real labels (the debug ones carry
# a long description); paths shown as the container's /app layout.
from history_service.config import HistorySettings
from history_service.system_backup import default_backup_included_paths, default_debug_included_paths, describe_bundle_groups
path_groups = [{**group, "path": str(group["path"]).replace(str(builder.ROOT), "/app")} for group in describe_bundle_groups(Settings(), HistorySettings())]
admin_state = {**crawl_fixture.ADMIN_BOOTSTRAP, "profiles": serialize_profiles(Settings()), "backup_defaults": {
    "packaging": "tar.zst", "debug_packaging": "tar.zst", "included_paths": default_backup_included_paths(),
    "debug_included_paths": default_debug_included_paths(), "debug_scrub_secrets": True, "debug_scrub_disk_identifiers": True,
    "path_groups": path_groups}, "runtime": {
    "available": True, "version_detail": "Running containers agree on 0.0.0-synthetic.", "containers": [
        {"key": key, "label": label, "description": text, "name": name, "running": True, "status": "running",
         "lifecycle_label": "Normal", "health": "healthy", "status_text": "Up 3 hours (healthy)",
         "running_version": "0.0.0-synthetic.7", "latest_version": "v0.0.0", "can_stop": key != "admin", "can_restart": True,
         "release_status": {"summary": "Development build (newest release is v0.0.0)"},
         "version_sync_summary": "Matches other running containers."}
        for key, label, text, name in RUNTIME]}}
admin_api = {"/api/admin/state": admin_state, **{key.split(":", 1)[1]: value for key, value in asyncio.run(crawl_fixture.build())["api"].items()
                                                 if key.startswith("admin:") and key != "admin:/api/admin/state"}}
print(json.dumps({
    "main": {name: {"html": main_page(snapshot, runtime), "snapshot": dump(snapshot), "runtime": dump(runtime)}
             for name, (snapshot, runtime) in pages.items()},
    "fabric": {"html": fabric_html, "fabric": fabric},
    "history": history_html,
    "admin": {"html": crawl_fixture.render_admin(admin_state), "api": admin_api},
}))
`;

const fixture = {};
test.beforeAll(() => {
  Object.assign(fixture, JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", FIXTURE_SCRIPT], {
    cwd: root,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  })));
});

test.use({ serviceWorkers: "block" });

async function routeSynthetic(page, documentBody, data = {}) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN) return route.abort();
    if (url.pathname === "/" || url.pathname === "/sas-fabric") {
      return route.fulfill({ contentType: "text/html; charset=utf-8", body: documentBody });
    }
    if (url.pathname.startsWith("/static/")) {
      for (const base of ["app", "history_service", "admin_service"]) {
        const file = path.join(root, base, url.pathname);
        if (fs.existsSync(file)) return route.fulfill({ path: file });
      }
      return route.fulfill({ status: 404, body: "" });
    }
    let body = { ok: true, available: false };
    if (data.api && url.pathname in data.api) body = data.api[url.pathname];
    if (url.pathname === "/api/inventory" && data.snapshot) body = data.snapshot;
    if (url.pathname === "/api/storage-views" && data.runtime) body = data.runtime;
    if (url.pathname === "/api/sas-fabric") body = fixture.fabric.fabric;
    if (url.pathname === "/api/slots/smart-batch") {
      const slots = request.postDataJSON()?.slots || [];
      body = { summaries: slots.map((slot) => ({ slot, summary: { available: true, temperature_c: 30 + (slot % 9) } })) };
    }
    const smart = url.pathname.match(/^\/api\/slots\/(\d+)\/smart$/);
    if (smart) body = { available: true, temperature_c: 30 + (Number(smart[1]) % 9) };
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  return errors;
}

async function openMain(page, name, viewport, query = "") {
  await page.setViewportSize(viewport);
  const data = fixture.main[name];
  const errors = await routeSynthetic(page, data.html, data);
  await page.goto(`${ORIGIN}/${query}`);
  const autoRefresh = page.locator("#auto-refresh-toggle");
  if (await autoRefresh.count() && await autoRefresh.isVisible()) await autoRefresh.uncheck();
  await expect(page.locator("#slot-grid .slot-tile[data-slot]").first()).toBeVisible();
  expect(await page.evaluate(() => window.innerWidth), "actual viewport width").toBe(viewport.width);
  return errors;
}

async function enableHeatMap(page) {
  const toggle = page.locator("#heatmap-toggle-button");
  if ((await toggle.getAttribute("aria-pressed")) !== "true") await toggle.click();
  await page.locator("#heatmap-metric-select").selectOption("temperature_c");
  await expect(page.locator("#slot-grid .slot-tile.heatmap-active").first()).toBeVisible();
}

async function openConnections(page) {
  const toggle = page.locator("#sas-fabric-toggle-button");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await expect(page.locator("#sas-fabric-panel .sas-fabric-item").first()).toBeVisible();
}

async function axeViolations(page, rules) {
  if (!(await page.evaluate(() => Boolean(window.axe)))) await page.evaluate(AXE_SOURCE);
  const result = await page.evaluate((values) => window.axe.run(document, { runOnly: { type: "rule", values } }), rules);
  return result.violations.flatMap((violation) => violation.nodes.map((node) => `${violation.id}: ${node.target.join(" ")}`));
}

async function capture(page, name, locator = null) {
  if (!SCREENSHOT_DIR) return;
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  const file = path.join(SCREENSHOT_DIR, `${name}.png`);
  if (locator) await locator.screenshot({ path: file });
  else await page.screenshot({ path: file, fullPage: true });
}

// Visible controls whose boxes intersect (one inside the other excepted), the
// document's sideways overflow, and controls cut off by a clipping ancestor
// that does not scroll.
async function layoutReport(page, scope = "body") {
  return page.evaluate((selector) => {
    const visible = (element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const container = document.querySelector(selector);
    const controls = Array.from(container.querySelectorAll("button, a[href], select, input:not([type=hidden]), summary"))
      .filter((element) => visible(element) && !element.closest(".hidden"));
    const name = (element) => element.id ? `#${element.id}` : `${element.tagName.toLowerCase()}.${Array.from(element.classList).slice(0, 2).join(".")}${element.dataset.slot ? `[${element.dataset.slot}]` : ""}`;
    // #1146: against a control outside its scroll box (a long list), a
    // control counts only where that box shows it, as the crawler does.
    const visibleIn = (element, other) => {
      let { left, right, top, bottom } = element.getBoundingClientRect();
      for (let node = element.parentElement; node && node !== document.body; node = node.parentElement) {
        const style = getComputedStyle(node);
        if ((style.overflowX === "visible" && style.overflowY === "visible") || node.contains(other)) continue;
        const box = node.getBoundingClientRect();
        [left, right, top, bottom] = [Math.max(left, box.left), Math.min(right, box.right), Math.max(top, box.top), Math.min(bottom, box.bottom)];
      }
      return { left, right, top, bottom };
    };
    const overlaps = [];
    for (let i = 0; i < controls.length; i += 1) {
      for (let j = i + 1; j < controls.length; j += 1) {
        const a = controls[i];
        const b = controls[j];
        if (a.contains(b) || b.contains(a)) continue;
        if (a.closest("label") && a.closest("label") === b.closest("label")) continue;
        const ra = visibleIn(a, b);
        const rb = visibleIn(b, a);
        const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
        const h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
        if (w > 1 && h > 1) overlaps.push(`${name(a)} x ${name(b)} (${Math.round(w)}x${Math.round(h)})`);
      }
    }
    const clipped = [];
    for (const element of controls) {
      const rect = element.getBoundingClientRect();
      for (let node = element.parentElement; node && node !== document.body; node = node.parentElement) {
        const overflow = getComputedStyle(node).overflowX;
        if (!/(hidden|clip|auto|scroll)/.test(overflow)) continue;
        const box = node.getBoundingClientRect();
        const outside = rect.left < box.left - 1 || rect.right > box.right + 1;
        if (outside && /(hidden|clip)/.test(overflow)) clipped.push(`${name(element)} clipped by ${name(node)}`);
        break;
      }
    }
    return {
      viewport: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      overlaps,
      clipped,
    };
  }, scope);
}

function expectCleanLayout(report, label) {
  expect(report.documentWidth, `${label}: page scrolls sideways ${JSON.stringify(report)}`).toBeLessThanOrEqual(report.viewport);
  expect(report.overlaps, `${label}: overlapping controls`).toEqual([]);
  expect(report.clipped, `${label}: controls cut off by a non-scrolling ancestor`).toEqual([]);
}

// Text elements that wrap to one or two characters per line (#1109, #1114).
async function narrowWrappedText(page, selector) {
  return page.locator(selector).evaluateAll((elements) => elements.flatMap((element) => {
    const text = element.textContent.replace(/\s+/g, " ").trim();
    if (!text || element.getBoundingClientRect().width === 0) return [];
    const range = document.createRange();
    range.selectNodeContents(element);
    const tops = new Set(Array.from(range.getClientRects()).filter((rect) => rect.width > 0).map((rect) => Math.round(rect.top)));
    return tops.size >= 3 && text.length / tops.size < 4 ? [`${element.className} "${text}" ${tops.size} lines`] : [];
  }));
}

// ---- #1110 -----------------------------------------------------------------

test("bay tiles and the Rename button name what they show (#1110)", async ({ page }) => {
  const viewport = DESKTOP[0];
  const errors = await openMain(page, "demo", viewport);
  const rule = ["label-content-name-mismatch"];
  expect(await axeViolations(page, rule), "default").toEqual([]);
  await expect(page.locator("#enclosure-alias-edit-button")).toHaveAccessibleName(/^Rename\b/);

  await page.locator('#slot-grid .slot-tile[data-slot="3"]').click();
  await expect(page.locator("#slot-grid .slot-tile.peer-highlight").first()).toBeVisible();
  expect(await axeViolations(page, rule), "bay selected, vdev peers highlighted").toEqual([]);

  await openConnections(page);
  await page.locator("#sas-fabric-panel .sas-fabric-path-card").first().click();
  await expect(page.locator("#slot-grid .slot-tile.fabric-dimmed").first()).toBeVisible();
  expect(await axeViolations(page, rule), "Connections path selected").toEqual([]);

  await enableHeatMap(page);
  expect(await axeViolations(page, rule), "Heat Map").toEqual([]);
  // The heat value is text the bay shows, so it joins the name in place, and
  // leaves it again when the Heat Map is turned off.
  const tile = page.locator('#slot-grid .slot-tile[data-slot="4"]');
  const value = (await tile.locator(".slot-heatmap-value").textContent()).trim();
  expect(value).toMatch(/^\d+ C$/);
  await expect(tile).toHaveAccessibleName(`Slot 04, demo-disk-04, demo-capacity, ${value}, online`);
  await expect(tile).toHaveAttribute("aria-describedby", "slot-tooltip");
  await page.locator("#heatmap-toggle-button").click();
  await expect(tile).toHaveAccessibleName("Slot 04, demo-disk-04, demo-capacity, online");
  await enableHeatMap(page);

  for (const view of STORAGE_VIEWS) {
    await page.goto(`${ORIGIN}/?storage_view_id=${view}`);
    await expect(page.locator("#slot-grid .storage-view-slot").first()).toBeVisible();
    expect(await axeViolations(page, rule), `storage view ${view}`).toEqual([]);
    await enableHeatMap(page);
    expect(await axeViolations(page, rule), `storage view ${view} Heat Map`).toEqual([]);
  }
  expect(errors).toEqual([]);
});

// Chromium's own accessible name for each match, read from its accessibility
// tree over CDP rather than recomputed in the test.
async function chromiumNames(page, selector) {
  const cdp = await page.context().newCDPSession(page);
  try {
    const { root: doc } = await cdp.send("DOM.getDocument", { depth: 0 });
    const { nodeIds } = await cdp.send("DOM.querySelectorAll", { nodeId: doc.nodeId, selector });
    const names = [];
    for (const nodeId of nodeIds) {
      const { nodes } = await cdp.send("Accessibility.getPartialAXTree", { nodeId, fetchRelatives: false });
      names.push(nodes[0]?.name?.value ?? null);
    }
    return names;
  } finally {
    await cdp.detach();
  }
}

// The text a bay shows, in order. A missing heat value's "--" placeholder is
// the one shown text that is not a fact, so it stays out of the name.
async function shownBayText(page, selector) {
  return page.locator(selector).evaluateAll((tiles) => tiles.map((tile) => {
    const shown = [];
    const walk = document.createTreeWalker(tile, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) {
      const text = walk.currentNode.textContent.trim();
      const parent = walk.currentNode.parentElement;
      if (!text || !parent.checkVisibility({ visibilityProperty: true })) continue;
      if (parent.matches(".heatmap-missing .slot-heatmap-value") && text === "--") continue;
      shown.push(text);
    }
    return { slot: tile.dataset.slot, shown };
  }));
}

test("bay names keep punctuation-only and non-ASCII labels (#1110)", async ({ page }) => {
  const errors = await openMain(page, "mounts", DESKTOP[0]);
  const mounts = ["/--", "/", "/数据", "/var", "/._-"];
  const tiles = "#slot-grid .slot-tile[data-slot]";
  for (const heat of [false, true]) {
    if (heat) await enableHeatMap(page);
    const label = heat ? "Heat Map" : "default";
    for (const [index, mount] of mounts.entries()) {
      await expect(page.locator(`${tiles}[data-slot="${index}"] .slot-pool`), `${label}: bay ${index} shows its mount`).toHaveText(mount);
    }
    const shown = await shownBayText(page, tiles);
    const names = await chromiumNames(page, tiles);
    expect(names).toHaveLength(shown.length);
    for (const [index, bay] of shown.entries()) {
      // Every shown line, exactly and in order, then the health it does not show.
      const prefix = `Slot ${bay.shown.join(", ")}, `;
      expect(names[index].slice(0, prefix.length), `${label}: bay ${bay.slot} Chromium name`).toBe(prefix);
      expect(names[index].length, `${label}: bay ${bay.slot} name ends with its health`).toBeGreaterThan(prefix.length);
      await expect(page.locator(tiles).nth(index), `${label}: Playwright agrees`).toHaveAccessibleName(names[index]);
    }
    const bySlot = new Map(shown.map((bay, index) => [Number(bay.slot), names[index]]));
    for (const [slot, mount] of mounts.entries()) {
      expect(bySlot.get(slot), `${label}: bay ${slot} names mount ${mount}`).toContain(`, ${mount}, `);
    }
    await expect(page.locator(`${tiles}[data-slot="0"]`)).toMatchAriaSnapshot(`- button /, \\/--, /`);
    expect(await axeViolations(page, ["label-content-name-mismatch"]), label).toEqual([]);
  }
  expect(errors).toEqual([]);
});

// ---- #1111 -----------------------------------------------------------------

test("Disk Path path-member facts are dl > div > dt + dd pairs (#1111)", async ({ page }) => {
  await page.setViewportSize(DESKTOP[0]);
  const errors = await routeSynthetic(page, fixture.fabric.html);
  await page.goto(`${ORIGIN}/sas-fabric?mode=disk`);
  await page.locator('[data-fabric-trace^="bay:"]').first().click();
  const members = page.locator(".fabric-path-member-card dl");
  await expect(members.first()).toBeVisible();
  expect(await axeViolations(page, ["definition-list", "dlitem"])).toEqual([]);
  const shape = await members.evaluateAll((lists) => lists.map((list) => Array.from(list.children).map((child) => (
    `${child.tagName}>${Array.from(child.children).map((part) => part.tagName).join("+")}`
  ))));
  for (const children of shape) {
    expect(children.length).toBeGreaterThan(0);
    for (const child of children) expect(child).toBe("DIV>DT+DD");
  }
  expect(errors).toEqual([]);
});

// ---- #1112 -----------------------------------------------------------------

// Every ellipsized line names its full text in a title on itself or its
// control. Keyboard users reach each such control with Tab, see a focus
// ring, and read the whole text while it has focus.
async function truncatedLines(page, selector) {
  return page.locator(selector).evaluateAll((elements) => elements
    .filter((element) => element.getBoundingClientRect().width > 0 && element.scrollWidth > element.clientWidth + 1
      && getComputedStyle(element).textOverflow === "ellipsis")
    .map((element) => {
      const text = element.textContent.replace(/\s+/g, " ").trim();
      const titled = element.closest("[title]");
      return { text, title: titled ? titled.getAttribute("title") : null };
    }));
}

const cutOff = (control, lineSelector) => control.locator(lineSelector).evaluateAll((elements) => elements
  .filter((element) => element.scrollWidth > element.clientWidth + 1).map((element) => element.textContent.trim()));

// Tab into control `index` from the tab stop before it: the control itself
// is never focused by the test.
async function tabInto(page, selector, index, label) {
  const position = await page.evaluate(({ selector, index }) => {
    const target = document.querySelectorAll(selector)[index];
    const stops = Array.from(document.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea, summary, [tabindex]"))
      .filter((element) => element.tabIndex >= 0 && !element.disabled && element.checkVisibility({ visibilityProperty: true }));
    const at = stops.indexOf(target);
    if (at > 0) stops[at - 1].focus();
    return at;
  }, { selector, index });
  expect(position, `${label}: control ${index} is a tab stop`).toBeGreaterThan(0);
  await page.keyboard.press("Tab");
  const focus = await page.evaluate(({ selector, index }) => {
    const element = document.activeElement;
    const style = getComputedStyle(element);
    return {
      reached: element === document.querySelectorAll(selector)[index],
      focusVisible: element.matches(":focus-visible"),
      outline: `${style.outlineStyle} ${style.outlineWidth}`,
      outlineWidth: parseFloat(style.outlineWidth),
    };
  }, { selector, index });
  expect(focus.reached, `${label}: Tab reaches control ${index}`).toBe(true);
  expect(focus.focusVisible, `${label}: control ${index} shows keyboard focus`).toBe(true);
  // Chromium's own two-tone ring ("auto"), or a solid ring at least 2 px wide.
  expect(focus.outline, `${label}: control ${index} focus ring`).toMatch(/^(auto|solid) /);
  if (focus.outline.startsWith("solid")) expect(focus.outlineWidth, `${label}: control ${index} focus ring width`).toBeGreaterThanOrEqual(2);
}

async function expectKeyboardReveal(page, controlSelector, lineSelector, label, { truncates = true, disclosure = false } = {}) {
  const lines = await truncatedLines(page, `${controlSelector} ${lineSelector}`);
  if (truncates) expect(lines.length, `${label}: the fixture makes at least one line ellipsize`).toBeGreaterThan(0);
  for (const line of lines) {
    expect(line.title || "", `${label}: "${line.text}" has its full text in a title`).toContain(line.text);
  }
  const controls = page.locator(controlSelector);
  const count = await controls.count();
  expect(count, `${label}: controls rendered`).toBeGreaterThan(0);
  let checked = 0;
  for (let index = 0; index < count; index += 1) {
    const control = controls.nth(index);
    const before = await cutOff(control, lineSelector);
    // Controls with nothing cut off still need a tab stop and a ring; check the first.
    if (!before.length && (index > 0 || checked)) continue;
    await tabInto(page, controlSelector, index, label);
    expect(await cutOff(control, lineSelector), `${label}: focused control ${index} shows its whole text`).toEqual([]);
    if (disclosure && before.length) {
      // A disclosure keeps the text open after focus leaves, and closes again.
      await expect(control).toHaveAttribute("aria-expanded", "false");
      await page.keyboard.press("Enter");
      await expect(control).toHaveAttribute("aria-expanded", "true");
      await page.keyboard.press("Tab");
      expect(await cutOff(control, lineSelector), `${label}: opened control ${index} stays open after Tab`).toEqual([]);
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Space");
      await expect(control).toHaveAttribute("aria-expanded", "false");
      await page.keyboard.press("Tab");
      expect(await cutOff(control, lineSelector), `${label}: closed control ${index} is cut off again`).toEqual(before);
    }
    checked += 1;
  }
  return checked;
}

for (const viewport of [NARROW, DESKTOP[0]]) {
  test(`truncated Connections, focus-strip, topology and path text can be read in full from the keyboard at ${viewport.width} px (#1112)`, async ({ page }) => {
    const errors = await openMain(page, "demo", viewport);
    await openConnections(page);
    await expectKeyboardReveal(page, "#sas-fabric-panel .sas-fabric-item", ":is(.sas-fabric-item-label, .sas-fabric-item-meta)", "Connections");
    await page.locator('#slot-grid .slot-tile[data-slot="3"]').click();
    await expect(page.locator("#topology-context .topology-pill").first()).toBeVisible();
    await expectKeyboardReveal(page, "#topology-context .topology-pill", "small", "topology pills", { truncates: false });
    await expect(page.locator("#multipath-context .topology-pill")).toHaveCount(2);
    await expectKeyboardReveal(page, "#multipath-context .topology-pill", "small", "multipath pills", { disclosure: true });
    await capture(page, `multipath-${viewport.width}`, page.locator("#multipath-context"));

    const fabricErrors = await routeSynthetic(page, fixture.fabric.html);
    await page.goto(`${ORIGIN}/sas-fabric?mode=lanes`);
    await expect(page.locator("#fabric-focus-strip .fabric-focus-card").first()).toBeVisible();
    await expectKeyboardReveal(page, "#fabric-focus-strip .fabric-focus-card", ":is(span, small)", "focus strip", { truncates: false });
    expect([...errors, ...fabricErrors]).toEqual([]);
  });
}

// ---- 390 px layout -----------------------------------------------------------

// The UniFi matrix below covers every built-in model face: a new one fails
// here until it joins UNIFI_FACES.
test("the UniFi matrix covers every built-in UniFi model face", () => {
  const models = Object.entries(fixture.main)
    .filter(([name, page]) => page.snapshot.selected_profile?.face_variant && !name.endsWith("-long"))
    .map(([name, page]) => [name, page.snapshot.slots.length]);
  expect(Object.fromEntries(models)).toEqual(Object.fromEntries(UNIFI_FACES.filter(([face]) => face !== "unifi-plain")));
});

// #1124: each bay's state chip (its ::after) against the bay's text, heat
// value, LED and handle block. Text is measured by its rendered Range, so a
// short label beside the chip passes; the value badge, LED and handle by box.
// placeStateChips() runs on the next animation frame after a render, so
// measure once two frames pass with nothing moving.
async function stateChips(page) {
  const frames = () => page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
  let last = "";
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await frames();
    const chips = await measureStateChips(page);
    if (JSON.stringify(chips) === last) return chips;
    last = JSON.stringify(chips);
  }
  throw new Error(`state chips did not settle: ${last}`);
}

async function measureStateChips(page) {
  return page.locator("#slot-grid .slot-tile[data-slot]").evaluateAll((tiles) => tiles.map((tile) => {
    const style = getComputedStyle(tile, "::after");
    const px = (value) => parseFloat(value) || 0;
    const extra = style.boxSizing === "border-box" ? [0, 0] : [
      px(style.paddingLeft) + px(style.paddingRight) + px(style.borderLeftWidth) + px(style.borderRightWidth),
      px(style.paddingTop) + px(style.paddingBottom) + px(style.borderTopWidth) + px(style.borderBottomWidth),
    ];
    const width = px(style.width) + extra[0];
    const height = px(style.height) + extra[1];
    const shift = new DOMMatrixReadOnly(style.transform === "none" ? undefined : style.transform);
    const box = tile.getBoundingClientRect();
    const left = box.left + tile.clientLeft + (style.left !== "auto" ? px(style.left) : tile.clientWidth - px(style.right) - width) + shift.e;
    const top = box.top + tile.clientTop + (style.top !== "auto" ? px(style.top) : tile.clientHeight - px(style.bottom) - height) + shift.f;
    const chip = { left, top, right: left + width, bottom: top + height };
    const overlap = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
    const hits = [];
    for (const part of tile.querySelectorAll(".slot-heatmap-value, .slot-status-led, .slot-latch")) {
      if (part.checkVisibility({ visibilityProperty: true }) && overlap(chip, part.getBoundingClientRect())) hits.push(part.className);
    }
    for (const line of tile.querySelectorAll(".slot-number, .slot-device, .slot-pool, .slot-tertiary, .slot-heatmap-value")) {
      if (!line.textContent.trim() || !line.checkVisibility({ visibilityProperty: true })) continue;
      const range = document.createRange();
      range.selectNodeContents(line);
      if ([...range.getClientRects()].some((rect) => overlap(chip, rect))) hits.push(`${line.className} text`);
    }
    const inside = chip.left >= box.left - 0.5 && chip.right <= box.right + 0.5 && chip.top >= box.top - 0.5 && chip.bottom <= box.bottom + 0.5;
    return { slot: tile.dataset.slot, content: style.content, hits, inside, chip: [chip.left - box.left, chip.top - box.top, width, height].map(Math.round) };
  }));
}

// #1157: every chassis face the fixture draws, and long labels on ENVR Core.
const LABEL_FACES = ["demo", "grouped", ...UNIFI_FACES.map(([face]) => face), "envr-core-long", "scale-front-24", "front-top-3.5",
  "front-top-2.5", "rear-left-2.5", "rear-right-2.5", "top-loader-2.5", "generic-3.5", "drawer-3.5"];

// Each shown bay label line (device, pool, tertiary): its text's Range sits
// inside the line box top to bottom, the line's content fits its box
// (scrollHeight), and the line sits inside the bay's padding box. A line
// that is wider than its box must ellipsize on one line and the bay's
// accessible name must keep its full text.
async function bayLabelFit(page) {
  return page.locator("#slot-grid .slot-tile[data-slot]").evaluateAll((tiles) => {
    const failures = [];
    let lines = 0;
    let truncated = 0;
    for (const tile of tiles) {
      const bay = tile.getBoundingClientRect();
      const style = getComputedStyle(tile);
      const inner = {
        top: bay.top + parseFloat(style.borderTopWidth), bottom: bay.bottom - parseFloat(style.borderBottomWidth),
        left: bay.left + parseFloat(style.borderLeftWidth), right: bay.right - parseFloat(style.borderRightWidth),
      };
      for (const line of tile.querySelectorAll(":scope > :is(.slot-device, .slot-pool, .slot-tertiary)")) {
        const text = line.textContent.trim();
        if (!text || !line.checkVisibility({ visibilityProperty: true, opacityProperty: true })) continue;
        lines += 1;
        const where = `bay ${tile.dataset.slot} ${line.className} "${text}"`;
        const box = line.getBoundingClientRect();
        if (line.scrollHeight > line.clientHeight) failures.push(`${where} content ${line.scrollHeight}px in a ${line.clientHeight}px box`);
        const range = document.createRange();
        range.selectNodeContents(line);
        const rects = [...range.getClientRects()].filter((rect) => rect.width > 0);
        if (new Set(rects.map((rect) => Math.round(rect.top))).size > 1) failures.push(`${where} wraps`);
        for (const rect of rects) {
          if (rect.top < box.top - 0.5 || rect.bottom > box.bottom + 0.5) {
            failures.push(`${where} text ${rect.top.toFixed(1)}-${rect.bottom.toFixed(1)} outside its box ${box.top.toFixed(1)}-${box.bottom.toFixed(1)}`);
          }
        }
        if (box.top < inner.top - 0.5 || box.bottom > inner.bottom + 0.5 || box.left < inner.left - 0.5 || box.right > inner.right + 0.5) {
          failures.push(`${where} box ${[box.left, box.top, box.right, box.bottom].map((v) => v.toFixed(1))} outside the bay ${[inner.left, inner.top, inner.right, inner.bottom].map((v) => v.toFixed(1))}`);
        }
        if (line.scrollWidth > line.clientWidth) {
          truncated += 1;
          const lineStyle = getComputedStyle(line);
          if (lineStyle.textOverflow !== "ellipsis" || lineStyle.whiteSpace !== "nowrap") failures.push(`${where} is cut off without an ellipsis`);
          if (!(tile.getAttribute("aria-label") || "").includes(text)) failures.push(`${where} is cut off and missing from the bay's name`);
        }
      }
    }
    return { lines, truncated, failures };
  });
}

const MAIN_STATES = [
  { name: "default", setup: async () => {} },
  { name: "heat map", setup: enableHeatMap },
  { name: "rename editor", setup: async (page) => {
    await enableHeatMap(page);
    await page.locator("#enclosure-alias-edit-button").click();
    await expect(page.locator("#enclosure-alias-input")).toBeVisible();
  } },
];

for (const viewport of [NARROW, ...DESKTOP]) {
  test(`main page header, chassis faces and storage views keep controls apart at ${viewport.width} px (#1102, #1114, #1115)`, async ({ page }) => {
    const errors = await openMain(page, "demo", viewport);
    for (const state of MAIN_STATES) {
      await state.setup(page);
      expectCleanLayout(await layoutReport(page, ".enclosure-panel"), `demo ${state.name}`);
      await capture(page, `main-${state.name.replace(/\s+/g, "-")}-${viewport.width}`, page.locator(".enclosure-panel"));
      if (state.name === "rename editor") await page.locator("#enclosure-alias-cancel").click();
    }
    for (const view of STORAGE_VIEWS) {
      await page.goto(`${ORIGIN}/?storage_view_id=${view}`);
      await expect(page.locator("#slot-grid .storage-view-slot").first()).toBeVisible();
      expectCleanLayout(await layoutReport(page, ".enclosure-panel"), `storage view ${view}`);
      expect(await narrowWrappedText(page, "#slot-grid .storage-view-slot *"), `storage view ${view} text`).toEqual([]);
      await capture(page, `view-${view}-${viewport.width}`, page.locator(".enclosure-panel"));
    }
    expect(errors).toEqual([]);
  });

  test(`grouped chassis columns scroll inside the shell instead of overlapping at ${viewport.width} px (#1115)`, async ({ page }) => {
    const errors = await openMain(page, "grouped", viewport);
    await expect(page.locator("#slot-grid .row-group")).toHaveCount(3);
    for (const state of MAIN_STATES.slice(0, 2)) {
      await state.setup(page);
      expectCleanLayout(await layoutReport(page, ".enclosure-panel"), `grouped ${state.name}`);
      await capture(page, `grouped-${state.name.replace(/\s+/g, "-")}-${viewport.width}`, page.locator(".enclosure-panel"));
    }
    expect(errors).toEqual([]);
  });

  for (const [face, bays] of UNIFI_FACES) {
    test(`${face} state chips clear the labels, heat values, LED and handle, and values stay on one line at ${viewport.width} px (#1109, #1124)`, async ({ page }) => {
      const errors = await openMain(page, face, viewport);
      const expectClearChips = async (label) => {
        const chips = await stateChips(page);
        expect(chips, label).toHaveLength(bays);
        for (const chip of chips) {
          expect(chip.content, `${label}: bay ${chip.slot} draws a state chip`).not.toBe("none");
          expect(chip.hits, `${label}: bay ${chip.slot} chip ${JSON.stringify(chip.chip)} covers`).toEqual([]);
          expect(chip.inside, `${label}: bay ${chip.slot} chip ${JSON.stringify(chip.chip)} inside the bay`).toBe(true);
        }
        expectCleanLayout(await layoutReport(page, ".enclosure-panel"), `${face} ${label}`);
      };
      await expectClearChips("Heat Map off");
      await enableHeatMap(page);
      const values = await page.locator("#slot-grid .slot-heatmap-value").evaluateAll((badges) => badges.map((badge) => {
        const range = document.createRange();
        range.selectNodeContents(badge);
        return {
          text: badge.textContent.trim(),
          lines: new Set(Array.from(range.getClientRects()).map((rect) => Math.round(rect.top))).size,
          clipped: badge.scrollWidth > badge.clientWidth + 1,
        };
      }));
      expect(values).toHaveLength(bays);
      for (const value of values) {
        expect(value.lines, `"${value.text}" wraps`).toBe(1);
        expect(value.clipped, `"${value.text}" is cut off`).toBe(false);
      }
      await expectClearChips("Heat Map");
      await capture(page, `${face}-heatmap-${viewport.width}`, page.locator(".enclosure-panel"));
      expect(errors).toEqual([]);
    });
  }

  // #1157: a bay's device line and pool label are never cut off top or
  // bottom: each line box holds its text, the bay holds each line. A label
  // too long for the bay ends in an ellipsis on one line and the bay's
  // accessible name (and tooltip) keeps the whole text.
  for (const face of LABEL_FACES) {
    test(`${face} bay labels are not cut off vertically, with Heat Map off and on, at ${viewport.width} px (#1157)`, async ({ page }) => {
      const errors = await openMain(page, face, viewport);
      for (const mode of ["Heat Map off", "Heat Map"]) {
        if (mode === "Heat Map") await enableHeatMap(page);
        await stateChips(page);
        const report = await bayLabelFit(page);
        // The one-row top-latched 2.5-inch sled hides its labels under a heat
        // map by design (style.css): its value badge owns the sled.
        if (!(face === "front-top-2.5" && mode === "Heat Map")) expect(report.lines, `${face} ${mode}: label lines measured`).toBeGreaterThan(0);
        expect(report.failures, `${face} ${mode} at ${viewport.width} px`).toEqual([]);
        // At 390 px a UniFi face scrolls sideways at its content width, so a
        // long label can show in full; desktop bays must ellipsize it.
        if (face.endsWith("-long") && viewport.width !== NARROW.width) expect(report.truncated, `${face} ${mode}: long labels end in an ellipsis`).toBeGreaterThan(0);
        await capture(page, `labels-${face}-${mode.replace(/\s+/g, "-").toLowerCase()}-${viewport.width}`, page.locator(".enclosure-panel"));
      }
      expect(errors).toEqual([]);
    });
  }

  test(`history tracked-scopes table scrolls inside its own box at ${viewport.width} px (#1116)`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const errors = await routeSynthetic(page, fixture.history);
    await page.goto(`${ORIGIN}/`);
    const table = page.locator(".tracked-scopes-section table");
    await expect(table).toBeVisible();
    const report = await layoutReport(page);
    expect(report.viewport).toBe(viewport.width);
    expect(report.documentWidth, "history page scrolls sideways").toBeLessThanOrEqual(viewport.width);
    const box = await table.evaluate((element) => {
      const scroller = element.parentElement;
      const style = getComputedStyle(scroller);
      return { overflowX: style.overflowX, scrollWidth: scroller.scrollWidth, clientWidth: scroller.clientWidth, tableWidth: element.scrollWidth };
    });
    expect(box.overflowX, "the table's own box scrolls").toMatch(/auto|scroll/);
    if (viewport.width === NARROW.width) expect(box.scrollWidth, "the fixture table is wider than 390 px").toBeGreaterThan(box.clientWidth);
    // A scrolling region is keyboard reachable and named by its heading.
    const region = page.getByRole("region", { name: "Systems and enclosures" });
    await expect(region).toHaveAttribute("tabindex", "0");
    await capture(page, `history-${viewport.width}`);
    expect(errors).toEqual([]);
  });
}

// ---- #1146 admin -------------------------------------------------------------

async function openAdmin(page, viewport) {
  await page.setViewportSize(viewport);
  const errors = await routeSynthetic(page, fixture.admin.html, { api: fixture.admin.api });
  await page.goto(`${ORIGIN}/`);
  await expect(page.locator("#existing-system-select option").first()).toBeAttached();
  expect(await page.evaluate(() => window.innerWidth), "actual viewport width").toBe(viewport.width);
  return errors;
}

// The crawler's own layout checks (qa/ui-crawl/checks.js) on the same page:
// the beta.7 crawl reported both #1146 defects through them.
async function crawlerLayoutFindings(page) {
  const { pageChecks } = require("./ui-crawl/checks");
  const result = await page.evaluate(pageChecks, {});
  return result.findings.filter((finding) => ["control-overlap", "page-horizontal-overflow", "control-clipped"].includes(finding.type))
    .map((finding) => `${finding.type} ${finding.selector} ${JSON.stringify(finding.detail)}`);
}

for (const viewport of [NARROW, ...DESKTOP]) {
  test(`admin Setup + Maintenance and the layout builder keep controls apart without page-wide sideways scroll at ${viewport.width} px (#1146)`, async ({ page }) => {
    const errors = await openAdmin(page, viewport);
    // The debug bundle's long path descriptions are what widened the page.
    await expect(page.locator("#debug-path-list .path-pill")).toHaveCount(12);
    await capture(page, `admin-operations-${viewport.width}`);
    expectCleanLayout(await layoutReport(page, "body"), "admin Setup + Maintenance");
    expect(await crawlerLayoutFindings(page), "admin Setup + Maintenance, crawler checks").toEqual([]);
    // A long path shrinks to the column and keeps its full text in a title.
    const pill = page.locator("#debug-path-list .path-pill").last();
    expect(await pill.evaluate((element) => element.getBoundingClientRect().right <= element.closest(".path-list").getBoundingClientRect().right + 1)).toBe(true);
    await expect(pill).toHaveAttribute("title", /Generated in a temporary export workspace/);

    await page.locator("[data-admin-view-button=builder]").click();
    await expect(page.locator("#profile-catalog .profile-card").first()).toBeVisible();
    await capture(page, `admin-builder-${viewport.width}`);
    expectCleanLayout(await layoutReport(page, "body"), "admin layout builder");
    expect(await crawlerLayoutFindings(page), "admin layout builder, crawler checks").toEqual([]);
    // Nothing paints over Load: the catalog clips the cards scrolled out of it.
    const load = page.locator("#profile-builder-load-button");
    await load.scrollIntoViewIfNeeded();
    expect(await load.evaluate((button) => {
      const box = button.getBoundingClientRect();
      return document.elementFromPoint(box.left + (box.width / 2), box.top + (box.height / 2)) === button;
    }), "Load selected is the topmost element at its centre").toBe(true);
    expect(errors).toEqual([]);
  });
}
