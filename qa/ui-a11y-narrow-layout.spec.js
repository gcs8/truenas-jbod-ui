"use strict";

// Accessibility and narrow-layout regressions found by the UI coverage crawl:
// - #1110 bay tiles and the Rename button: the accessible name contains the
//   visible text (axe label-content-name-mismatch).
// - #1111 Disk Path path-member facts: valid dl > div > dt + dd markup (axe
//   definition-list, dlitem).
// - #1112 ellipsized Connections, focus-strip and topology-pill text carries
//   its full text in a title and unfolds when the control has keyboard focus.
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

const FIXTURE_SCRIPT = `
import json

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

import scripts.build_current_source_browser_fixture as builder
from app import __version__
from app.config import Settings
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload
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

pages = {"demo": (demo, demo_runtime)}
for name, snapshot in {
    "grouped": variant("synthetic-grouped-12", "Synthetic Grouped 12 Bay", "front-drive", 1, 12, (4, 4, 4)),
    # A plain UniFi face (no model print) and a UNVR Pro, whose bays 1-3 share
    # the top row with the touchscreen and ports.
    "unifi-plain": variant("synthetic-unifi-8", "Synthetic UniFi 8 Bay", "unifi-drive", 2, 4),
    "unifi": variant("synthetic-unvr-pro", "Synthetic UNVR Pro", "unifi-drive", 2, 4,
                     layout=[[0, 1, 2, None], [3, 4, 5, 6]], face_variant="unvr-pro", slot_number_base=1),
}.items():
    pages[name] = (snapshot, empty_runtime)
print(json.dumps({
    "main": {name: {"html": main_page(snapshot, runtime), "snapshot": dump(snapshot), "runtime": dump(runtime)}
             for name, (snapshot, runtime) in pages.items()},
    "fabric": {"html": fabric_html, "fabric": fabric},
    "history": history_html,
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
      for (const base of ["app", "history_service"]) {
        const file = path.join(root, base, url.pathname);
        if (fs.existsSync(file)) return route.fulfill({ path: file });
      }
      return route.fulfill({ status: 404, body: "" });
    }
    let body = { ok: true, available: false };
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
    const overlaps = [];
    for (let i = 0; i < controls.length; i += 1) {
      for (let j = i + 1; j < controls.length; j += 1) {
        const a = controls[i];
        const b = controls[j];
        if (a.contains(b) || b.contains(a)) continue;
        if (a.closest("label") && a.closest("label") === b.closest("label")) continue;
        const ra = a.getBoundingClientRect();
        const rb = b.getBoundingClientRect();
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
// control, and shows the whole text while that control has keyboard focus.
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

async function expectRevealedOnFocus(page, controlSelector, lineSelector, label) {
  const lines = await truncatedLines(page, `${controlSelector} ${lineSelector}`);
  expect(lines.length, `${label}: the fixture makes at least one line ellipsize`).toBeGreaterThan(0);
  for (const line of lines) {
    expect(line.title || "", `${label}: "${line.text}" has its full text in a title`).toContain(line.text);
  }
  const controls = page.locator(controlSelector);
  const count = await controls.count();
  for (let index = 0; index < count; index += 1) {
    const control = controls.nth(index);
    const before = await control.locator(lineSelector).evaluateAll((elements) => elements.some((element) => element.scrollWidth > element.clientWidth + 1));
    if (!before) continue;
    await control.focus();
    await page.keyboard.press("Shift");
    const still = await control.locator(lineSelector).evaluateAll((elements) => elements
      .filter((element) => element.scrollWidth > element.clientWidth + 1).map((element) => element.textContent.trim()));
    expect(still, `${label}: focused control ${index} shows its whole text`).toEqual([]);
  }
}

for (const viewport of [NARROW, DESKTOP[0]]) {
  test(`truncated Connections, focus-strip and topology text can be read in full at ${viewport.width} px (#1112)`, async ({ page }) => {
    const errors = await openMain(page, "demo", viewport);
    await openConnections(page);
    await expectRevealedOnFocus(page, "#sas-fabric-panel .sas-fabric-item", ":is(.sas-fabric-item-label, .sas-fabric-item-meta)", "Connections");
    await page.locator('#slot-grid .slot-tile[data-slot="3"]').click();
    await expect(page.locator(".topology-pill").first()).toBeVisible();
    for (const line of await truncatedLines(page, ".topology-pill small")) {
      expect(line.title || "", `topology pill "${line.text}" has its full text in a title`).toContain(line.text);
    }

    const fabricErrors = await routeSynthetic(page, fixture.fabric.html);
    await page.goto(`${ORIGIN}/sas-fabric?mode=lanes`);
    await expect(page.locator("#fabric-focus-strip .fabric-focus-card").first()).toBeVisible();
    for (const line of await truncatedLines(page, "#fabric-focus-strip .fabric-focus-card small")) {
      expect(line.title || "", `focus card "${line.text}" has its full text in a title`).toContain(line.text);
    }
    expect([...errors, ...fabricErrors]).toEqual([]);
  });
}

// ---- 390 px layout -----------------------------------------------------------

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

  for (const [face, bays] of [["unifi-plain", 8], ["unifi", 7]]) {
    test(`${face} heat-map temperatures stay on one line at ${viewport.width} px (#1109)`, async ({ page }) => {
      const errors = await openMain(page, face, viewport);
      await enableHeatMap(page);
      const values = await page.locator("#slot-grid .slot-heatmap-value").evaluateAll((badges) => badges.map((badge) => {
        const range = document.createRange();
        range.selectNodeContents(badge);
        return { text: badge.textContent.trim(), lines: new Set(Array.from(range.getClientRects()).map((rect) => Math.round(rect.top))).size };
      }));
      expect(values).toHaveLength(bays);
      for (const value of values) expect(value.lines, `"${value.text}" wraps`).toBe(1);
      expectCleanLayout(await layoutReport(page, ".enclosure-panel"), "UniFi heat map");
      await capture(page, `${face}-heatmap-${viewport.width}`, page.locator(".enclosure-panel"));
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
