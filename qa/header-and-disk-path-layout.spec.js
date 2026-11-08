"use strict";

// #1102: two layout regressions on desktop widths.
// 1. Heat Map mode adds Metric/Mode/Scale controls and a legend to the
//    enclosure panel header. They must not squeeze the enclosure title into a
//    one-letter column; the title keeps its single-line width and the controls
//    move or wrap instead.
// 2. Storage Fabric > Disk Path: every card fills its grid cell (no empty
//    column beside a narrow card), and fact labels never break mid-word.
// Complete production templates and assets; only HTTP and the data are
// synthetic.
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const ORIGIN = "https://synthetic.invalid";
const BAYS = 24;
const TITLE = "Front 24 Bay";
const DESKTOP_WIDTHS = [1600, 1920, 3840];
const SCREENSHOT_DIR = process.env.LAYOUT_1102_SCREENSHOT_DIR || "";

function python(script, args = []) {
  return JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", script, ...args], {
    cwd: root,
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
  }));
}

// A synthetic 24-bay CORE shelf: 2 HBAs, an active and a passive path per bay,
// one expander per HBA and two backplane zones. Field values are shaped like
// the SAS facts that made the SAS LINK card narrow (short values, short title).
function buildFabric() {
  const slots = Array.from({ length: BAYS }, (_, index) => index);
  const zoneOf = (slot) => (slot < BAYS / 2 ? 0 : 1);
  const nodes = [{ id: "host", kind: "host", label: "Synthetic CORE", status: "online", metrics: {}, related_slots: slots, raw: {} }];
  const controllers = [];
  const paths = [];
  const traces = [];
  for (let c = 0; c < 2; c += 1) {
    const controllerId = `controller:mpr${c}`;
    nodes.push({
      id: controllerId, kind: "controller", label: `mpr${c}`, status: "online", related_slots: slots, raw_id: `mpr${c}`,
      metrics: { pcie_slot: `CPU1 SLOT${c + 1} PCI-E 3.0 X8`, firmware: "24.00.00.00", temperature: "51 C", linked_phy_count: 16, phy_count: 16 },
      raw: { board: "SYNTH 9400-8i8e", device: `/dev/mpr${c}`, pci_address: `0000:0${c}:00.0` },
    });
    controllers.push({ id: controllerId, name: `mpr${c}`, related_slots: slots });
    for (const state of ["active", "passive"]) {
      const pathId = `path:mpr${c}:${state}`;
      nodes.push({ id: pathId, kind: "path", label: `mpr${c} ${state}`, status: state, controller_id: controllerId, related_slots: slots, metrics: { state }, raw: {} });
      paths.push({ id: pathId, controller: `mpr${c}`, state, slots, count: slots.length });
    }
    nodes.push({
      id: `expander:mpr${c}`, kind: "expander", label: `50000000000000${c}f`, status: "online", controller_id: controllerId, related_slots: slots,
      raw_id: `50000000000000${c}f`,
      metrics: { linked_phys: 41, num_phys: 51 },
      raw: {
        device_counts: { "No Device": 10, "SAS Initiator": 8, "SAS Target": 24, "SEP Target": 1 },
        phys: slots.map((slot) => ({ dev_handle: `00${(0x1b + slot).toString(16)}`, phy: String(slot).padStart(2, "0"), remote_phy: "01", device: `da${slot + c * BAYS}`, min: "1.5", max: "12" })),
      },
    });
    nodes.push({ id: `mpr-enclosure:mpr${c}`, kind: "mpr-enclosure", label: `50000000000000${c}e`, status: "online", controller_id: controllerId, related_slots: slots, metrics: {}, raw: { enc_handle: "0003" } });
  }
  for (let z = 0; z < 2; z += 1) {
    nodes.push({ id: `backplane:${z}`, kind: "backplane", label: `Backplane Zone ${z + 1}`, status: "online", related_slots: slots.filter((slot) => zoneOf(slot) === z), metrics: {}, raw: {} });
  }
  for (const slot of slots) {
    const label = String(slot).padStart(2, "0");
    nodes.push({ id: `bay:${slot}`, kind: "bay", label: `Bay ${label}`, status: "online", related_slots: [slot], metrics: {}, raw: {} });
    const pathStates = [];
    const mprDevices = [];
    for (let c = 0; c < 2; c += 1) {
      const state = c === 0 ? "active" : "passive";
      const device = `da${slot + c * BAYS}`;
      pathStates.push({ controller: `mpr${c}`, state, device_name: device });
      mprDevices.push({
        controller: `mpr${c}`, state, member_device_name: device, speed: "12", handle: `00${(0x1b + slot).toString(16)}`, parent: "001a", mpr_slot: label,
        expander_ids: [`expander:mpr${c}`], enclosure_id: `mpr-enclosure:mpr${c}`, enclosure_handle: "0003", sas_address: `5000c500000000${label}`,
      });
    }
    traces.push({
      id: `bay:${slot}`, kind: "bay", label: `Bay ${label}`, slots: [slot],
      node_ids: ["host", "controller:mpr0", "path:mpr0:active", "expander:mpr0", "mpr-enclosure:mpr0", "controller:mpr1", "path:mpr1:passive", "expander:mpr1", "mpr-enclosure:mpr1", `backplane:${zoneOf(slot)}`, `bay:${slot}`],
      link_ids: [],
      metrics: { device_name: `multipath/disk${slot}`, physical_location_known: true, path_states: pathStates, mpr_devices: mprDevices },
      evidence: ["synthetic fixture"],
    });
  }
  return {
    available: true, platform: "core", system_id: "synthetic-system", system_label: "Synthetic CORE",
    selected_enclosure_id: "enc-a", selected_enclosure_label: "Synthetic 24 Bay Shelf",
    nodes, links: [], traces, controllers, paths, expanders: [], enclosures: [], aliases: [], warnings: [], sources: {},
    raw: { fabric_kind: "core_sas" },
  };
}

const fixture = {};
test.beforeAll(() => {
  const fabric = buildFabric();
  Object.assign(fixture, python(`
import json
import sys

import scripts.build_current_source_browser_fixture as builder
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload

bays = int(sys.argv[1])
title = sys.argv[2]
fabric = json.loads(sys.argv[3])
data = builder.build_synthetic_live_snapshot().model_dump(mode="json")
template = data["slots"][0]
columns = 6
layout = [list(range(row * columns, row * columns + columns)) for row in range(bays // columns)]
data["slots"] = [
    {
        **template,
        "slot": index,
        "slot_label": f"{index:02d}",
        "row_index": index // columns,
        "column_index": index % columns,
        "device_name": f"multipath/disk{index}",
        "serial": f"SANITIZED-{index:04d}",
        "gptid": f"synthetic-gptid-{index}",
        "physical_location_known": True,
        "notes": None,
    }
    for index in range(bays)
]
data["layout_rows"] = layout
data["layout_slot_count"] = bays
data["layout_columns"] = columns
data["selected_system_platform"] = "core"
data["systems"][0]["platform"] = "core"
data["selected_profile"] = {
    **data["selected_profile"],
    "id": "synthetic-front-24",
    "label": title,
    "panel_title": title,
    "face_style": "front-drive",
    "rows": len(layout),
    "columns": columns,
    "slot_count": bays,
    "slot_layout": layout,
}
data["enclosures"][0].update(profile_id="synthetic-front-24", rows=len(layout), columns=columns, slot_count=bays, slot_layout=layout)
data["summary"].update(disk_count=bays, mapped_slot_count=bays, manual_mapping_count=0)
snapshot = InventorySnapshot.model_validate(data)
builder.build_synthetic_live_snapshot = lambda: snapshot
runtime = StorageViewRuntimePayload(system_id="synthetic-system", views=[])
snapshot_json = snapshot.model_dump(mode="json")
fabric_html = builder.TEMPLATES.env.get_template("sas_fabric.html").render(
    request=builder.build_fixture_request(),
    snapshot=snapshot_json,
    fabric=fabric,
    app_version="synthetic",
    bootstrap_json=json.dumps(dict(snapshot=snapshot_json, fabric=fabric)),
)
print(json.dumps(dict(html=builder.build_live_fixture_html(runtime), fabricHtml=fabric_html, snapshot=snapshot_json, fabric=fabric)))
`, [String(BAYS), TITLE, JSON.stringify(fabric)]));
  // The offline snapshot export inlines the same stylesheet and script; build
  // it through the shipping exporter so the header is checked there too.
  fixture.exportHtml = python(`
import asyncio
import json
import sys

import scripts.build_current_source_browser_fixture as builder
from app.config import Settings
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload
from app.services.history_backend import HistoryBackendClient
from app.services.snapshot_export import SnapshotExportService

snapshot = InventorySnapshot.model_validate(json.loads(sys.argv[1]))
settings = Settings()
exporter = SnapshotExportService(settings, HistoryBackendClient(settings.history), builder.TEMPLATES)
rendered = asyncio.run(exporter.build_enclosure_snapshot_html(
    request=builder.build_fixture_request(),
    snapshot=snapshot,
    smart_summary_cache={},
    selected_slot=None,
    history_window_hours=None,
    history_panel_open=False,
    io_chart_mode="total",
    storage_view_runtime=StorageViewRuntimePayload(system_id="synthetic-system", views=[]),
    generated_at=builder.FIXTURE_GENERATED_AT,
))
print(json.dumps(rendered.html))
`, [JSON.stringify(fixture.snapshot)]);
});

test.use({ serviceWorkers: "block" });

function temperatureFor(slot) {
  return 30 + (slot % 6);
}

async function routeSynthetic(page, documentBody) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN) return route.abort();
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html; charset=utf-8", body: documentBody });
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(root, "app", url.pathname);
      return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
    }
    let data = { ok: true, available: false };
    if (url.pathname === "/api/inventory") data = fixture.snapshot;
    if (url.pathname === "/api/storage-views") data = { system_id: "synthetic-system", views: [] };
    if (url.pathname === "/api/sas-fabric") data = fixture.fabric;
    if (url.pathname === "/api/slots/smart-batch") {
      const slots = request.postDataJSON()?.slots || [];
      data = { summaries: slots.map((slot) => ({ slot, summary: { available: true, temperature_c: temperatureFor(slot), serial: `SANITIZED-${String(slot).padStart(4, "0")}` } })) };
    }
    const smart = url.pathname.match(/^\/api\/slots\/(\d+)\/smart$/);
    if (smart) data = { available: true, temperature_c: temperatureFor(Number(smart[1])) };
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  return errors;
}

async function openMainPage(page, body = fixture.html) {
  const errors = await routeSynthetic(page, body);
  await page.goto(`${ORIGIN}/`);
  const autoRefresh = page.locator("#auto-refresh-toggle");
  if (await autoRefresh.count() && await autoRefresh.isVisible()) await autoRefresh.uncheck();
  await expect(page.locator("#slot-grid [data-slot]")).toHaveCount(BAYS);
  return errors;
}

async function enableHeatMap(page) {
  const toggle = page.locator("#heatmap-toggle-button");
  if ((await toggle.getAttribute("aria-pressed")) !== "true") await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#heatmap-controls")).toBeVisible();
  await expect(page.locator("#heatmap-legend")).toBeVisible();
}

// The title's rendered width against the width of the same text laid out on
// one line in the same font, and the number of line boxes it occupies.
async function titleGeometry(page) {
  return page.locator("#enclosure-panel-title").evaluate((title) => {
    const text = title.textContent.trim();
    const style = getComputedStyle(title);
    const probe = document.createElement("span");
    probe.textContent = text;
    for (const property of ["font-family", "font-size", "font-weight", "font-style", "letter-spacing", "text-transform", "word-spacing"]) {
      probe.style.setProperty(property, style.getPropertyValue(property));
    }
    Object.assign(probe.style, { position: "absolute", visibility: "hidden", whiteSpace: "nowrap", left: "0", top: "0" });
    document.body.append(probe);
    const natural = probe.getBoundingClientRect().width;
    probe.remove();
    const range = document.createRange();
    range.selectNodeContents(title);
    const lines = new Set(Array.from(range.getClientRects()).filter((rect) => rect.width > 0).map((rect) => Math.round(rect.top))).size;
    const rect = title.getBoundingClientRect();
    const controls = document.getElementById("heatmap-controls").getBoundingClientRect();
    const legend = document.getElementById("heatmap-legend").getBoundingClientRect();
    const overlaps = (other) => other.width > 0 && other.height > 0
      && rect.left < other.right && other.left < rect.right && rect.top < other.bottom && other.top < rect.bottom;
    return {
      text,
      natural,
      width: rect.width,
      lines,
      overlapsControls: overlaps(controls) || overlaps(legend),
      viewport: { width: window.innerWidth, height: window.innerHeight },
    };
  });
}

async function expectTitleOnOneLine(page, width) {
  const geometry = await titleGeometry(page);
  expect(geometry.viewport.width, "actual viewport width").toBe(width);
  expect(geometry.text).toBe(TITLE);
  expect(geometry.width, `title width ${JSON.stringify(geometry)}`).toBeGreaterThanOrEqual(geometry.natural - 1);
  expect(geometry.lines, `title line boxes ${JSON.stringify(geometry)}`).toBe(1);
  expect(geometry.overlapsControls, "title overlaps heat-map controls").toBe(false);
}

async function capture(page, locator, name) {
  if (!SCREENSHOT_DIR) return;
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await locator.screenshot({ path: path.join(SCREENSHOT_DIR, `${name}.png`) });
}

for (const width of [390, ...DESKTOP_WIDTHS]) {
  test(`enclosure title stays on one line in Heat Map mode at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1200 });
    const errors = await openMainPage(page);
    await expectTitleOnOneLine(page, width);
    await enableHeatMap(page);
    await expectTitleOnOneLine(page, width);
    await capture(page, page.locator(".enclosure-panel"), `main-heatmap-${width}`);
    expect(errors).toEqual([]);
  });
}

for (const width of DESKTOP_WIDTHS) {
  test(`offline snapshot export keeps the enclosure title on one line in Heat Map mode at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1200 });
    const errors = await openMainPage(page, fixture.exportHtml);
    await enableHeatMap(page);
    await expectTitleOnOneLine(page, width);
    await capture(page, page.locator(".enclosure-panel"), `snapshot-heatmap-${width}`);
    expect(errors).toEqual([]);
  });
}

async function openDiskPath(page) {
  const errors = await routeSynthetic(page, fixture.fabricHtml);
  await page.goto(`${ORIGIN}/?mode=disk`);
  await expect(page.locator(".disk-path-branch").first()).toBeVisible();
  await expect(page.locator(".disk-path-kind", { hasText: "SAS Link" }).first()).toBeVisible();
  return errors;
}

async function diskPathGeometry(page) {
  return page.locator(".disk-path-board").evaluate((board) => {
    const lineTops = (range) => new Set(Array.from(range.getClientRects()).filter((rect) => rect.width > 0).map((rect) => Math.round(rect.top))).size;
    const flows = Array.from(board.querySelectorAll(".disk-path-flow")).map((flow) => {
      const cells = Array.from(flow.querySelectorAll(":scope > .disk-path-card-cell"));
      const tracks = getComputedStyle(flow).gridTemplateColumns.split(" ").map(parseFloat).filter((value) => value > 0);
      const cards = cells.map((cell) => {
        const card = cell.querySelector(".disk-path-card");
        const kind = card.querySelector(".disk-path-kind").textContent.trim();
        const labels = Array.from(card.querySelectorAll(".disk-path-facts em")).map((label) => {
          const range = document.createRange();
          range.selectNodeContents(label);
          const brokenWords = [];
          const node = label.firstChild;
          for (const match of label.textContent.matchAll(/\S+/g)) {
            const word = document.createRange();
            word.setStart(node, match.index);
            word.setEnd(node, match.index + match[0].length);
            if (lineTops(word) > 1) brokenWords.push(match[0]);
          }
          return { text: label.textContent, lines: lineTops(range), brokenWords };
        });
        return { kind, cellWidth: cell.getBoundingClientRect().width, cardWidth: card.getBoundingClientRect().width, labels };
      });
      return { tracks: tracks.length, cells: cells.length, cards };
    });
    return { viewport: window.innerWidth, flows };
  });
}

for (const width of DESKTOP_WIDTHS) {
  test(`Disk Path cards fill their columns and labels never break mid-word at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1400 });
    const errors = await openDiskPath(page);
    const geometry = await diskPathGeometry(page);
    expect(geometry.viewport, "actual viewport width").toBe(width);
    expect(geometry.flows.length).toBeGreaterThanOrEqual(2);
    for (const [flowIndex, flow] of geometry.flows.entries()) {
      expect(flow.tracks, `branch ${flowIndex}: no empty grid column`).toBeLessThanOrEqual(flow.cells);
      for (const card of flow.cards) {
        expect(card.cardWidth, `branch ${flowIndex} ${card.kind} card fills its column`).toBeGreaterThanOrEqual(card.cellWidth - 1);
        for (const label of card.labels) {
          expect(label.brokenWords, `branch ${flowIndex} ${card.kind} label "${label.text}" breaks mid-word`).toEqual([]);
        }
      }
      const sasLink = flow.cards.find((card) => card.kind === "SAS Link");
      expect(sasLink, `branch ${flowIndex} has a SAS Link card`).toBeTruthy();
      expect(sasLink.labels.map((label) => label.text)).toEqual(expect.arrayContaining(["Remote", "Handle", "Parent"]));
      for (const label of sasLink.labels) {
        expect(label.lines, `branch ${flowIndex} SAS Link label "${label.text}" is one line`).toBe(1);
      }
    }
    await capture(page, page.locator(".disk-path-board"), `disk-path-${width}`);
    expect(errors).toEqual([]);
  });
}
