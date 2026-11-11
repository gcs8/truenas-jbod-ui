"use strict";

// #1103: heat-map temperature colours come from absolute bands, not from the
// min-max of the bays in view, and every tile text line stays readable on
// its heat colour. Complete production assets and templates; only HTTP, the
// snapshot and the SMART summaries are synthetic.
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");

// Bay temperatures and the band each must show. Bays 0-1 and 2-3 are healthy
// pairs 1 C apart (the issue's 48 C / 49 C case); bay 4 is at the default
// critical edge; bay 5 is just as hot but its drive reports a 65 C rating.
const BAYS = [
  { temperature: 38, band: "normal" },
  { temperature: 39, band: "normal" },
  { temperature: 48, band: "warm" },
  { temperature: 49, band: "warm" },
  { temperature: 60, band: "critical" },
  { temperature: 60, band: "warning", warning: 55, critical: 65 },
];

// Bay shapes the value badge must share with the tile text (#1106): every
// face style, 2.5- and 3.5-inch, short and tall, each latch edge.
const SHAPES = [
  ["generic", "bottom", "3.5", 1, 6],
  ["generic", "bottom", "2.5", 1, 6],
  ["front-drive", "right", "3.5", 3, 4],
  ["front-drive", "top", "3.5", 2, 4],
  ["front-drive", "top", "2.5", 1, 24],
  ["rear-drive", "left", "2.5", 2, 12],
  ["rear-drive", "right", "2.5", 2, 12],
  ["top-loader", "bottom", "3.5", 3, 4],
  ["top-loader", "bottom", "2.5", 3, 4],
  ["drawer", "bottom", "3.5", 2, 6],
  ["unifi-drive", "bottom", "3.5", 2, 4],
].map(([face, latch, size, rows, columns]) => ({
  name: `${face} ${size}in ${latch} latch ${rows}x${columns}`,
  face_style: face,
  latch_edge: latch,
  bay_size: size,
  rows,
  columns,
  // The one-row top-latched 2.5-inch sled hides its labels under any heat
  // map (style.css): its badge owns the sled. Every other face keeps them.
  labelFree: face === "front-drive" && latch === "top" && size === "2.5" && rows === 1,
  bays: Array.from({ length: rows * columns }, (_, index) => ({ temperature: 30 + ((index * 7) % 32) })),
}));

// #1150: the SCALE 24-bay front (3.5-inch, right latch, 6 rows of 4).
const SCALE_FRONT = {
  name: "front-drive 3.5in right latch 6x4",
  face_style: "front-drive",
  latch_edge: "right",
  bay_size: "3.5",
  rows: 6,
  columns: 4,
  bays: Array.from({ length: 24 }, (_, index) => ({ temperature: 30 + ((index * 7) % 32) })),
};

function buildFixtures(layouts) {
  return JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json
import sys

import scripts.build_current_source_browser_fixture as builder
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload

base = builder.build_synthetic_live_snapshot().model_dump(mode="json")
runtime = StorageViewRuntimePayload(system_id="synthetic-system", views=[])
fixtures = []
for spec in json.loads(sys.argv[1]):
    data = json.loads(json.dumps(base))
    count, columns = len(spec["bays"]), spec["columns"]
    template = data["slots"][0]
    data["slots"] = [
        {
            **template,
            "slot": index,
            "slot_label": f"{index:02d}",
            "row_index": index // columns,
            "column_index": index % columns,
            "device_name": f"sd{index}",
            "serial": f"SANITIZED-HEAT-{index:02d}",
            "gptid": f"synthetic-gptid-{index}",
            "temperature_c": bay["temperature"],
            "notes": None,
            **({"present": False, "state": "empty", "device_name": None, "serial": None, "gptid": None, "model": None,
                "size_human": None, "pool_name": None, "vdev_name": None, "health": None, "temperature_c": None}
               if bay.get("empty") else {}),
        }
        for index, bay in enumerate(spec["bays"])
    ]
    layout = [list(range(start, min(count, start + columns))) for start in range(0, count, columns)]
    data["layout_rows"] = layout
    data["layout_slot_count"] = count
    data["layout_columns"] = columns
    data["selected_profile"] = {
        **data["selected_profile"],
        "id": "synthetic-heat-row",
        "label": "Synthetic heat row",
        "panel_title": "Synthetic heat row",
        **{key: spec[key] for key in ("face_style", "latch_edge", "bay_size") if key in spec},
        "rows": len(layout),
        "columns": columns,
        "slot_count": count,
        "slot_layout": layout,
    }
    data["enclosures"][0].update(profile_id="synthetic-heat-row", rows=len(layout), columns=columns, slot_count=count, slot_layout=layout)
    disks = sum(1 for bay in spec["bays"] if not bay.get("empty"))
    data["summary"].update(disk_count=disks, mapped_slot_count=disks, manual_mapping_count=0)
    snapshot = InventorySnapshot.model_validate(data)
    builder.build_synthetic_live_snapshot = lambda: snapshot
    fixtures.append(dict(html=builder.build_live_fixture_html(runtime), snapshot=snapshot.model_dump(mode="json")))
print(json.dumps(fixtures))
`, JSON.stringify(layouts)], { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }));
}

// #1123: storage-view cards draw the badge on their own text. The public
// demo's NVMe carrier and SATADOM views, plus the other card templates: the
// AOC carrier board and embedded boot media.
const CARD_VIEWS = ["nvme-carrier-x4", "synthetic-aoc-carrier", "boot-doms", "synthetic-boot-media"];

function buildCardFixture() {
  return JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json

import scripts.build_current_source_browser_fixture as builder
from app.config import Settings
from app.models.domain import StorageViewRuntimePayload
from app.services.public_demo_fixture import build_public_demo_snapshot_bundle, load_public_demo_fixture

bundle = build_public_demo_snapshot_bundle(fixture=load_public_demo_fixture(), settings=Settings())
runtime = bundle.storage_view_runtime.model_dump(mode="json")
views = {view["id"]: view for view in runtime["views"]}
aoc = json.loads(json.dumps(views["nvme-carrier-x4"]))
aoc.update(id="synthetic-aoc-carrier", label="Synthetic AOC Carrier", panel_title="Synthetic AOC Carrier",
           template_id="aoc-slg4-2h8m2-2", slot_layout=[[0, 1]], slot_count=2, slots=aoc["slots"][:2], order=40)
media = json.loads(json.dumps(views["boot-doms"]))
media.update(id="synthetic-boot-media", label="Synthetic Boot Media", panel_title="Synthetic Boot Media",
             template_id="embedded-boot-media-1", slot_layout=[[0]], slot_count=1, slots=media["slots"][:1], order=50)
runtime["views"] += [aoc, media]
for view in runtime["views"]:
    view["matched_count"] = sum(1 for slot in view["slots"] if slot.get("occupied"))
runtime = StorageViewRuntimePayload.model_validate(runtime)
snapshot = bundle.primary_snapshot
builder.build_synthetic_live_snapshot = lambda: snapshot
print(json.dumps(dict(html=builder.build_live_fixture_html(runtime), snapshot=snapshot.model_dump(mode="json"),
                      runtime=runtime.model_dump(mode="json"))))
`], { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }));
}

function smartSummary(bay, slot = 0) {
  return {
    available: true,
    temperature_c: bay.temperature,
    warning_temperature_c: bay.warning ?? null,
    critical_temperature_c: bay.critical ?? null,
    smart_health_status: "PASSED",
    rotation_rate_rpm: 7200,
    transport_protocol: "SAS",
    // #1145: wide values for the other metrics ("54,153 hr", "100% left",
    // "12.3 TB/yr", "1:2.50 R/W").
    ...(bay.wide ? {
      power_on_hours: 54153 + (slot * 1037),
      endurance_remaining_percent: 100 - (slot % 3),
      bytes_written: 1.23e14 + (slot * 7.7e12),
      annualized_bytes_written: 1.23e13 + (slot * 7.7e11),
      bytes_read: (slot % 2 ? 4.1e13 : 3.08e14) + (slot * 1e12),
    } : {}),
    // #1151: long Power-On Hours ("52,345 hr"); a bay without one has none.
    ...("hours" in bay ? { power_on_hours: bay.hours ?? null } : {}),
  };
}

let fixture;
let shapeFixtures;
let cardFixture;
let liveNvmeFixture;
let chipFixtures;
// A live enclosure drawn as an NVMe carrier board uses the same cards.
const LIVE_NVME = { face_style: "nvme-carrier", rows: 4, columns: 1, bays: [41, 40, 39, 38].map((temperature) => ({ temperature })) };
test.beforeAll(() => {
  const chipShapes = [SCALE_FRONT, ...SHAPES];
  let mixed;
  [fixture, liveNvmeFixture, ...mixed] = buildFixtures([
    { columns: BAYS.length, bays: BAYS }, LIVE_NVME, ...SHAPES,
    ...chipShapes.map((shape) => ({ ...shape, bays: mixedBays(shape, true) })),
  ]);
  shapeFixtures = mixed.slice(0, SHAPES.length);
  chipFixtures = mixed.slice(SHAPES.length);
  cardFixture = buildCardFixture();
});

test.use({ serviceWorkers: "block" });

async function openHeatMap(page, pageFixture = fixture, bays = BAYS, query = "", { empty = false } = {}) {
  const errors = [];
  const bayAt = (slot) => (bays ? bays[slot] : { temperature: 30 + ((slot * 7) % 32) });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== "https://synthetic.invalid") throw new Error(`Unexpected external request ${url.origin}`);
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: pageFixture.html });
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(root, "app", url.pathname);
      return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
    }
    let data = { ok: true, available: false };
    if (url.pathname === "/api/inventory") data = pageFixture.snapshot;
    if (url.pathname === "/api/storage-views") data = pageFixture.runtime || { system_id: "synthetic-system", views: [] };
    const viewSmart = /^\/api\/storage-views\/[^/]+\/slots\/(\d+)\/smart$/.exec(url.pathname);
    if (viewSmart) data = smartSummary(bayAt(Number(viewSmart[1])), Number(viewSmart[1]));
    if (url.pathname === "/api/slots/smart-batch") {
      const requested = JSON.parse(request.postData() || "{}").slots || [];
      data = { summaries: requested.map((slot) => ({ slot, summary: smartSummary(bayAt(slot), slot) })) };
    }
    const single = /^\/api\/slots\/(\d+)\/smart$/.exec(url.pathname);
    if (single) data = smartSummary(bayAt(Number(single[1])), Number(single[1]));
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto(`https://synthetic.invalid/${query}`);
  await page.locator("#auto-refresh-toggle").uncheck();
  if (bays) await expect(page.locator("#slot-grid [data-slot]")).toHaveCount(bays.length);
  else await expect(page.locator("#slot-grid [data-slot]").first()).toBeVisible();
  await page.locator("#heatmap-toggle-button").click();
  await page.locator("#heatmap-metric-select").selectOption("temperature_c");
  // The drive-rated bay can only reach "warning" once its SMART summary loads.
  if (bays === BAYS) await expect(page.locator('#slot-grid [data-slot="5"]')).toHaveAttribute("data-heatmap-band", "warning");
  if (bays && empty) await expect(page.locator("#slot-grid .slot-tile.heatmap-active")).toHaveCount(bays.filter((entry) => !entry.empty).length);
  else if (bays) await expect(page.locator("#slot-grid .slot-tile.heatmap-active")).toHaveCount(bays.length);
  else await expect(page.locator("#slot-grid .slot-tile.heatmap-active").first()).toBeVisible();
  return errors;
}

function bay(page, index) {
  return page.locator(`#slot-grid [data-slot="${index}"]`);
}

const VIEWPORTS = [{ width: 1600, height: 1000 }, { width: 1920, height: 1080 }, { width: 3840, height: 2120 }];

for (const viewport of VIEWPORTS) {
  test(`temperature bands are absolute and tile text is readable at ${viewport.width} px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const errors = await openHeatMap(page);

    for (const [index, expected] of BAYS.entries()) {
      await expect(bay(page, index), `bay ${index} at ${expected.temperature} C`).toHaveAttribute("data-heatmap-band", expected.band);
      await expect(bay(page, index).locator(".slot-heatmap-value")).toHaveText(`${expected.temperature} C`);
    }
    const colour = (index) => bay(page, index).evaluate((tile) => tile.style.getPropertyValue("--heatmap-rgb"));
    expect(await colour(0), "38 C and 39 C share a colour").toBe(await colour(1));
    expect(await colour(2), "48 C and 49 C share a colour").toBe(await colour(3));

    await expect(page.locator("#heatmap-colour-field")).toBeVisible();
    await expect(page.locator("#heatmap-colour-select")).toHaveValue("bands");
    await expect(page.locator("#heatmap-legend-bands li")).toHaveCount(5);
    await expect(page.locator("#heatmap-legend-bands")).toContainText("45-49 C");
    await expect(page.locator("#heatmap-legend-bands")).toContainText("60+ C");
    await expect(page.locator("#heatmap-legend-source")).toContainText("HDD defaults (5 of 6 bays)");
    await expect(page.locator("#heatmap-legend-min")).toBeHidden();

    // Every text line sits on an opaque chip; its colour must meet WCAG AA.
    const ratios = await page.locator("#slot-grid [data-slot]").evaluateAll((tiles) => {
      const rgb = (value) => (value.match(/[\d.]+/g) || []).slice(0, 4).map(Number);
      const luminance = ([r, g, b]) => {
        const channel = (value) => {
          const scaled = value / 255;
          return scaled <= 0.04045 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
        };
        return (0.2126 * channel(r)) + (0.7152 * channel(g)) + (0.0722 * channel(b));
      };
      const ratio = (a, b) => {
        const [x, y] = [luminance(a), luminance(b)];
        return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
      };
      const results = [];
      for (const tile of tiles) {
        for (const line of tile.querySelectorAll(".slot-number, .slot-device, .slot-pool")) {
          const style = getComputedStyle(line);
          if (style.display === "none" || style.visibility === "hidden" || !line.textContent.trim()) continue;
          const background = rgb(style.backgroundColor);
          results.push({
            slot: tile.dataset.slot,
            line: line.className,
            opaque: background.length === 3 || background[3] === 1,
            opacity: Number(style.opacity),
            ratio: ratio(rgb(style.color), background),
          });
        }
      }
      return results;
    });
    expect(ratios.length).toBeGreaterThanOrEqual(BAYS.length * 2);
    for (const result of ratios) {
      expect(result.opaque, `${result.slot} ${result.line} chip`).toBe(true);
      expect(result.opacity, `${result.slot} ${result.line} opacity`).toBe(1);
      expect(result.ratio, `${result.slot} ${result.line} contrast`).toBeGreaterThanOrEqual(4.5);
    }

    if (process.env.HEATMAP_BANDS_SCREENSHOT_DIR) {
      await page.locator(".enclosure-face").screenshot({
        path: path.join(process.env.HEATMAP_BANDS_SCREENSHOT_DIR, `heatmap-bands-${viewport.width}.png`),
      });
    }

    // Relative min-max stays available as an explicit choice.
    await page.locator("#heatmap-colour-select").selectOption("relative");
    await expect(bay(page, 0)).not.toHaveAttribute("data-heatmap-band", /.+/);
    await expect(page.locator("#heatmap-legend-min")).toHaveText("38 C");
    await expect(page.locator("#heatmap-legend-max")).toHaveText("60 C");
    await expect(page.locator("#heatmap-legend-bands")).toBeHidden();
    expect(await colour(0)).not.toBe(await colour(4));
    expect(errors).toEqual([]);
  });
}

// #1106: an opaque value badge must not paint over a text line. Compare the
// text Ranges themselves with the badge box; computed contrast cannot see
// a sibling covering the text.
for (const [index, shape] of SHAPES.entries()) {
  test(`value badge leaves every tile text line visible on ${shape.name}`, async ({ page }) => {
    const errors = await openHeatMap(page, shapeFixtures[index], shape.bays);
    for (const viewport of VIEWPORTS) {
      await page.setViewportSize(viewport);
      const tiles = await page.locator("#slot-grid .slot-tile").evaluateAll((elements) => elements.map((tile) => {
        const box = (rect) => ({ left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom });
        const overlap = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
          && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
        const inside = (inner, outer) => inner.left >= outer.left - 1 && inner.right <= outer.right + 1
          && inner.top >= outer.top - 1 && inner.bottom <= outer.bottom + 1;
        const badgeElement = tile.querySelector(".slot-heatmap-value");
        const badge = box(badgeElement.getBoundingClientRect());
        const tileBox = box(tile.getBoundingClientRect());
        const lines = [];
        for (const line of tile.querySelectorAll(".slot-number, .slot-device, .slot-pool, .slot-tertiary")) {
          if (!line.textContent.trim() || !line.checkVisibility({ visibilityProperty: true })) continue;
          const range = document.createRange();
          range.selectNodeContents(line);
          // Clip each Range box to the line's own box: ellipsis hides the rest.
          const own = line.getBoundingClientRect();
          const rects = [...range.getClientRects()].map((rect) => ({
            left: Math.max(rect.left, own.left),
            right: Math.min(rect.right, own.right),
            top: rect.top,
            bottom: rect.bottom,
          }));
          lines.push({
            line: line.className,
            covered: rects.some((rect) => overlap(rect, badge)),
            inTile: rects.every((rect) => inside(rect, tileBox)),
          });
        }
        return {
          slot: tile.dataset.slot,
          badgeInTile: inside(badge, tileBox),
          badgeClipped: badgeElement.scrollHeight > badgeElement.clientHeight + 1,
          lines,
        };
      }));
      expect(tiles).toHaveLength(shape.bays.length);
      const where = (tile, line = "") => `${shape.name} at ${viewport.width} px, bay ${tile.slot} ${line}`;
      for (const tile of tiles) {
        expect(tile.badgeInTile, where(tile, "badge inside the bay")).toBe(true);
        expect(tile.badgeClipped, where(tile, "badge text clipped")).toBe(false);
        for (const line of tile.lines) {
          expect(line.covered, where(tile, `${line.line} covered by the badge`)).toBe(false);
          expect(line.inTile, where(tile, `${line.line} inside the bay`)).toBe(true);
        }
      }
      // Only single-row top-latched 2.5-inch sleds drop the labels on purpose.
      const hidesLabels = shape.face_style === "front-drive" && shape.latch_edge === "top" && shape.bay_size === "2.5" && shape.rows === 1;
      if (!hidesLabels) {
        for (const tile of tiles) {
          expect(tile.lines.map((line) => line.line), where(tile)).toEqual(expect.arrayContaining(["slot-device", "slot-pool"]));
        }
      }
    }
    expect(errors).toEqual([]);
  });
}

// #1145: a value never breaks inside a number or between a number and its
// unit ("+0.22 C" stacked one character per line on 24-bay 2.5-inch sleds),
// on every face style, at every width, for the widest metric values. It
// stays on one line, whole, inside its bay, off the bay's text, and the
// crawler's squeeze check would flag it if it did not.
const VALUE_METRICS = ["temperature_c", "temperature_delta_c", "power_on_hours", "endurance_remaining_percent",
  "annualized_bytes_written", "read_write_ratio"];
const ALL_WIDTHS = [{ width: 390, height: 900 }, ...VIEWPORTS];

async function measureValues(page) {
  // Two frames: fitting runs on the frame after a render or resize.
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
  return page.locator("#slot-grid .slot-tile.heatmap-active").evaluateAll((tiles) => tiles.map((tile) => {
    const box = (rect) => ({ left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom });
    const overlap = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
      && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
    const inside = (inner, outer) => inner.left >= outer.left - 1 && inner.right <= outer.right + 1
      && inner.top >= outer.top - 1 && inner.bottom <= outer.bottom + 1;
    const badge = tile.querySelector(".slot-heatmap-value");
    const range = document.createRange();
    range.selectNodeContents(badge);
    const fragments = [...range.getClientRects()].filter((rect) => rect.width > 0.5 && rect.height > 0.5);
    const badgeBox = box(badge.getBoundingClientRect());
    const tileBox = box(tile.getBoundingClientRect());
    // The bay's content box: its padding holds the latch, LED and state chip.
    const pad = getComputedStyle(tile);
    const inset = (side) => parseFloat(pad[`border-${side}-width`]) + parseFloat(pad[`padding-${side}`]) - 3;
    const content = { left: tileBox.left + inset("left"), top: tileBox.top + inset("top"), right: tileBox.right - inset("right"), bottom: tileBox.bottom - inset("bottom") };
    const outside = [];
    const covered = [];
    for (const line of tile.querySelectorAll(".slot-number, .slot-device, .slot-pool, .slot-tertiary")) {
      if (!line.textContent.trim() || !line.checkVisibility({ visibilityProperty: true })) continue;
      const lineRange = document.createRange();
      lineRange.selectNodeContents(line);
      const own = line.getBoundingClientRect();
      const rects = [...lineRange.getClientRects()].map((rect) => ({
        left: Math.max(rect.left, own.left), right: Math.min(rect.right, own.right), top: rect.top, bottom: rect.bottom,
      }));
      if (rects.some((rect) => overlap(rect, badgeBox))) covered.push(line.className);
      if (!rects.every((rect) => inside(rect, content))) outside.push(line.className);
    }
    // #1145: a one-line value grows sideways; it must not run over the
    // bay's latch handle or LED in the padding either.
    for (const part of tile.querySelectorAll(".slot-latch, .slot-status-led")) {
      if (part.checkVisibility({ visibilityProperty: true }) && overlap(part.getBoundingClientRect(), badgeBox)) covered.push(part.className);
    }
    // #1145 review: fitting a value must not hide the bay's device or serial
    // line; the checks above skip a line that is not drawn.
    const hidden = [...tile.querySelectorAll(".slot-device, .slot-pool")]
      .filter((line) => line.textContent.trim())
      .filter((line) => {
        const rect = line.getBoundingClientRect();
        return !line.checkVisibility({ visibilityProperty: true, opacityProperty: true }) || rect.width < 1 || rect.height < 1;
      })
      .map((line) => line.className);
    const text = badge.textContent.trim();
    return {
      slot: tile.dataset.slot,
      text,
      hidden,
      title: badge.title,
      fragments: fragments.length,
      clipped: badge.scrollWidth > badge.clientWidth + 1 || badge.scrollHeight > badge.clientHeight + 1,
      inTile: inside(badgeBox, content),
      covered,
      outside,
      // The crawler's text-narrow-wrap rule (qa/ui-crawl/checks.js).
      squeezed: fragments.length >= 3 && text.length >= 6 && text.length / fragments.length < 4,
      size: [Math.round(badgeBox.right - badgeBox.left), Math.round(badgeBox.bottom - badgeBox.top)],
    };
  }));
}

for (const [index, shape] of SHAPES.entries()) {
  test(`heat values stay on one line on ${shape.name} for every metric and width (#1145)`, async ({ page }) => {
    const bays = shape.bays.map((entry) => ({ ...entry, wide: true }));
    const errors = await openHeatMap(page, shapeFixtures[index], bays);
    for (const metric of VALUE_METRICS) {
      await page.locator("#heatmap-metric-select").selectOption(metric);
      await expect(page.locator("#slot-grid .slot-tile.heatmap-active")).toHaveCount(bays.length);
      for (const viewport of ALL_WIDTHS) {
        await page.setViewportSize(viewport);
        expect(await page.evaluate(() => window.innerWidth), "actual viewport width").toBe(viewport.width);
        const values = await measureValues(page);
        expect(values).toHaveLength(bays.length);
        if (process.env.HEATMAP_BANDS_SCREENSHOT_DIR && ["temperature_delta_c", "power_on_hours"].includes(metric)) {
          await page.mouse.move(0, 0);
          await page.locator(".enclosure-face").screenshot({
            path: path.join(process.env.HEATMAP_BANDS_SCREENSHOT_DIR, `values-${shape.name.replace(/\W+/g, "-")}-${metric}-${viewport.width}.png`),
          });
        }
        for (const value of values) {
          const where = `${shape.name} ${metric} at ${viewport.width} px, bay ${value.slot} "${value.text}" ${JSON.stringify(value.size)}`;
          expect(value.fragments, `${where} renders on one line`).toBe(1);
          expect(value.squeezed, `${where} squeezed`).toBe(false);
          expect(value.clipped, `${where} cut off`).toBe(false);
          expect(value.inTile, `${where} inside the bay's content box`).toBe(true);
          expect(value.outside, `${where} pushes bay text out of the content box`).toEqual([]);
          expect(value.covered, `${where} covers bay text, the latch or the LED`).toEqual([]);
          expect(value.title, `${where} full value in the title`).toContain(":");
          if (!shape.labelFree) expect(value.hidden, `${where} hides the device or serial line`).toEqual([]);
        }
        for (const chip of await stateChips(page)) {
          expect(chip.hits.filter((hit) => hit.startsWith("slot-heatmap-value")),
            `${shape.name} ${metric} at ${viewport.width} px, bay ${chip.slot} chip ${JSON.stringify(chip.chip)} covers the value`).toEqual([]);
        }
      }
    }
    expect(errors).toEqual([]);
  });
}

// #1145 review: each bay's state chip (its ::after, placed by app.js). It
// never covers the value badge, and on the rear 2.5-inch 2x12 faces, whose
// bays widen at 390 px, it also stays inside the bay and off the latch, LED
// and text. Text by its Range; hardware and the badge by box. Measured once
// two frames pass unchanged.
async function stateChips(page, strict = false) {
  let last = "";
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
    const chips = await page.locator("#slot-grid .slot-tile[data-slot]").evaluateAll((tiles, strict) => tiles.map((tile) => {
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
      // From the offsets the spot's rule sets (style.css): Chromium can
      // return a stale resolved left/top for a pseudo-element after a resize.
      const spot = tile.closest(".chassis-shell")?.dataset.chipSpot || "corner";
      const tileStyle = getComputedStyle(tile);
      const bayWidth = box.width - px(tileStyle.borderLeftWidth) - px(tileStyle.borderRightWidth);
      const bayHeight = box.height - px(tileStyle.borderTopWidth) - px(tileStyle.borderBottomWidth);
      const x = ["middle", "foot"].includes(spot) ? bayWidth / 2 : spot === "start" ? px(style.left) : bayWidth - px(style.right) - width;
      const y = spot === "middle" ? bayHeight / 2 : spot === "foot" ? bayHeight - px(style.bottom) - height : px(style.top);
      const left = box.left + tile.clientLeft + x + shift.e;
      const top = box.top + tile.clientTop + y + shift.f;
      const chip = { left, top, right: left + width, bottom: top + height };
      // #1150: strict means zero intersection, and the value by its text.
      const slack = strict ? 0 : 0.5;
      const overlap = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > slack && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > slack;
      const hits = [];
      for (const part of tile.querySelectorAll(strict ? ".slot-status-led, .slot-latch" : ".slot-heatmap-value, .slot-status-led, .slot-latch")) {
        if (part.checkVisibility({ visibilityProperty: true }) && overlap(chip, part.getBoundingClientRect())) hits.push(part.className);
      }
      for (const line of tile.querySelectorAll(strict ? ".slot-number, .slot-heatmap-value, .slot-device, .slot-pool, .slot-tertiary" : ".slot-number, .slot-device, .slot-pool, .slot-tertiary")) {
        if (!line.textContent.trim() || !line.checkVisibility({ visibilityProperty: true })) continue;
        const range = document.createRange();
        range.selectNodeContents(line);
        if ([...range.getClientRects()].some((rect) => overlap(chip, rect))) hits.push(`${line.className} text`);
      }
      const inside = chip.left >= box.left - 0.5 && chip.right <= box.right + 0.5 && chip.top >= box.top - 0.5 && chip.bottom <= box.bottom + 0.5;
      const value = tile.querySelector(".slot-heatmap-value")?.textContent.trim() || "";
      return { slot: tile.dataset.slot, content: style.content, value, hits, inside, chip: [chip.left - box.left, chip.top - box.top, width, height].map(Math.round) };
    }), strict);
    if (JSON.stringify(chips) === last) return chips;
    last = JSON.stringify(chips);
  }
  throw new Error(`state chips did not settle: ${last}`);
}

for (const [index, shape] of SHAPES.entries()) {
  if (shape.face_style !== "rear-drive") continue;
  test(`state chips clear the latch, LED, value and labels on ${shape.name} at 390 px (#1145)`, async ({ page }) => {
    const bays = shape.bays.map((entry) => ({ ...entry, wide: true }));
    const errors = await openHeatMap(page, shapeFixtures[index], bays);
    await page.setViewportSize({ width: 390, height: 900 });
    expect(await page.evaluate(() => window.innerWidth), "actual viewport width").toBe(390);
    for (const metric of VALUE_METRICS) {
      await page.locator("#heatmap-metric-select").selectOption(metric);
      const chips = await stateChips(page);
      expect(chips).toHaveLength(bays.length);
      for (const chip of chips) {
        const where = `${shape.name} ${metric} at 390 px, bay ${chip.slot} chip ${JSON.stringify(chip.chip)}`;
        expect(chip.content, `${where} is drawn`).not.toBe("none");
        expect(chip.hits, `${where} covers`).toEqual([]);
        expect(chip.inside, `${where} inside the bay`).toBe(true);
      }
    }
    expect(errors).toEqual([]);
  });
}

// #1150 / #1151: on every face shape at every width, with the heat map off,
// on temperature, and on long rotated Power-On Hours ("52,345 hr"), with
// occupied, empty and no-reading bays mixed, every bay's painted state chip
// (its ::after) stays inside the bay and has zero intersection with the
// slot-number text, the value text (by Range, so a rotated value counts as
// painted), the LED and the latch. Device and serial lines too. Covers the
// SCALE 24-bay front (3.5-inch right latch, 6x4) and dense mixed sleds.
const CHIP_SHAPES = [SCALE_FRONT, ...SHAPES].map((shape, index) => ({ shape, fixture: () => chipFixtures[index] }));
const CHIP_MODES = [
  { name: "heat map off" },
  { name: "temperature", metric: "temperature_c" },
  { name: "long power-on hours", metric: "power_on_hours", hours: true },
];

// Every third bay is empty; every fifth other bay has no hours reading.
function mixedBays(shape, hours) {
  return shape.bays.map((entry, slot) => ({ ...entry, empty: slot % 3 === 1, hours: hours && slot % 5 !== 4 ? 52345 + (slot * 1037) : undefined }));
}

for (const { shape, fixture: shapeFixture } of CHIP_SHAPES) {
  test(`state chips clear the number, value, LED and latch on ${shape.name} at every width (#1150, #1151)`, async ({ page }) => {
    test.setTimeout(120_000);
    const bays = mixedBays(shape, true);
    const errors = await openHeatMap(page, shapeFixture(), bays, "", { empty: true });
    const failures = [];
    for (const mode of CHIP_MODES) {
      if (mode.metric) {
        if (!(await page.locator("#heatmap-toggle-button").getAttribute("aria-pressed") === "true")) await page.locator("#heatmap-toggle-button").click();
        await page.locator("#heatmap-metric-select").selectOption(mode.metric);
      } else {
        await page.locator("#heatmap-toggle-button").click();
        await expect(page.locator("#slot-grid .slot-tile.heatmap-active")).toHaveCount(0);
      }
      for (const viewport of ALL_WIDTHS) {
        await page.setViewportSize(viewport);
        expect(await page.evaluate(() => window.innerWidth), "actual viewport width").toBe(viewport.width);
        const chips = await stateChips(page, true);
        // Fractional rear bays must agree with the actual border quads too;
        // rounded clientWidth otherwise invents a subpixel latch intersection.
        if (shape.face_style === "rear-drive" && shape.latch_edge === "right" && viewport.width === 1600) {
          const painted = await paintedStateChips(page);
          expect(painted).toHaveLength(bays.length);
          for (const chip of painted) {
            expect(chip.chip).not.toBeNull();
            expect(chip.hits, `${mode.name} actual border quad`).toEqual([]);
            expect(chip.inside).toBe(true);
          }
        }
        expect(chips).toHaveLength(bays.length);
        if (mode.hours) {
          const values = chips.filter((chip) => /^\d{2},\d{3}/.test(chip.value));
          expect(values.length, `${shape.name} long hours values drawn at ${viewport.width} px`).toBeGreaterThan(0);
        }
        if (process.env.HEATMAP_BANDS_SCREENSHOT_DIR) {
          await page.mouse.move(0, 0);
          await page.locator(".enclosure-face").screenshot({
            path: path.join(process.env.HEATMAP_BANDS_SCREENSHOT_DIR, `chips-${shape.name.replace(/\W+/g, "-")}-${mode.name.replace(/\W+/g, "-")}-${viewport.width}.png`),
          });
        }
        for (const chip of chips) {
          const where = `${shape.name} ${mode.name} at ${viewport.width} px, bay ${chip.slot} chip ${JSON.stringify(chip.chip)}`;
          if (chip.content === "none") failures.push(`${where} is not drawn`);
          if (chip.hits.length) failures.push(`${where} covers ${chip.hits.join(", ")}`);
          if (!chip.inside) failures.push(`${where} leaves the bay`);
        }
      }
    }
    expect(failures).toEqual([]);
    expect(errors).toEqual([]);
  });
}

// Review regressions: use Chromium's actual pseudo-element border quad, not
// the chooser's offsets or a second implementation of the CSS cascade.
async function paintedStateChips(page) {
  const session = await page.context().newCDPSession(page);
  try {
    const { root: document } = await session.send("DOM.getDocument", { depth: -1, pierce: true });
    const { nodeIds } = await session.send("DOM.querySelectorAll", { nodeId: document.nodeId, selector: "#slot-grid .slot-tile[data-slot]" });
    const selected = new Set(nodeIds);
    const nodes = [];
    const visit = (node) => {
      if (selected.has(node.nodeId)) nodes.push(node);
      for (const child of node.children || []) visit(child);
    };
    visit(document);
    const chips = [];
    for (const node of nodes) {
      const after = node.pseudoElements?.find((pseudo) => pseudo.pseudoType === "after");
      let chip = null;
      if (after) {
        const { model } = await session.send("DOM.getBoxModel", { nodeId: after.nodeId });
        const xs = model.border.filter((_, index) => index % 2 === 0);
        const ys = model.border.filter((_, index) => index % 2 === 1);
        chip = { left: Math.min(...xs), top: Math.min(...ys), right: Math.max(...xs), bottom: Math.max(...ys) };
      }
      chips.push(await page.locator("#slot-grid .slot-tile[data-slot]").nth(chips.length).evaluate((tile, chip) => {
        const overlap = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
        const box = tile.getBoundingClientRect();
        const hits = [];
        for (const part of tile.querySelectorAll(".slot-status-led, .slot-latch, .slot-number, .slot-heatmap-value")) {
          if (!part.checkVisibility({ visibilityProperty: true })) continue;
          const rects = [part.getBoundingClientRect()];
          const range = document.createRange();
          range.selectNodeContents(part);
          rects.push(...range.getClientRects());
          if (chip && rects.some((rect) => overlap(chip, rect))) hits.push(part.className);
        }
        return {
          id: tile.id, chip, hits, content: getComputedStyle(tile, "::after").content,
          inside: !chip || (chip.left >= box.left && chip.top >= box.top && chip.right <= box.right && chip.bottom <= box.bottom),
        };
      }, chip));
    }
    return chips;
  } finally {
    await session.detach();
  }
}

for (const family of ["per-bay", "g2"]) {
  test(`fallback markers clear protected content with actual ::after boxes on ${family}`, async ({ page }) => {
    const errors = await openHeatMap(page);
    await page.locator("#heatmap-toggle-button").click();
    // Deliberately constrained geometry, using the shipping placement pipeline
    // and stylesheet. Opposite obstructions each leave room for a 12px marker,
    // but no shared position. G2 has a single bay requiring a free spot.
    await page.addStyleTag({ content: `
      #slot-grid { display: flex !important; flex: none; gap: 8px; min-height: 0; height: 150px; }
      #slot-grid .slot-tile { box-sizing: content-box; flex: none; width: 30px; height: 44px; min-height: 0; padding: 0; border: 1px solid #888; }
      #slot-grid .slot-latch { display: block; position: absolute; inset: 0 auto auto 0; box-sizing: border-box; width: 14px; height: 44px; margin: 0; transform: none; }
      #slot-grid .slot-tile:nth-child(2) .slot-latch { left: 16px; }
      #slot-grid .slot-tile::before { content: none; }
    ` });
    await page.locator("#slot-grid").evaluate((grid, family) => {
      grid.closest(".chassis-shell").dataset.faceFamily = family === "g2" ? "g2" : "plain";
      grid.innerHTML = Array.from({ length: family === "g2" ? 1 : 2 }, (_, index) =>
        `<div id="fallback-bay-${index}" class="slot-tile state-healthy" data-slot="${index}" role="button" aria-label="Synthetic bay ${index}, healthy"><span class="slot-latch"></span></div>`).join("");
    }, family);
    const failures = [];
    for (const viewport of ALL_WIDTHS) {
      await page.setViewportSize(viewport);
      expect(await page.evaluate(() => window.innerWidth)).toBe(viewport.width);
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      const chips = await paintedStateChips(page);
      expect(chips).toHaveLength(family === "g2" ? 1 : 2);
      if (process.env.HEATMAP_BANDS_SCREENSHOT_DIR) {
        await page.mouse.move(0, 0);
        await page.locator(".enclosure-face").screenshot({
          path: path.join(process.env.HEATMAP_BANDS_SCREENSHOT_DIR, `chips-fallback-${family}-${viewport.width}.png`),
        });
      }
      for (const chip of chips) {
        if (!chip.chip || chip.chip.right - chip.chip.left !== 12 || chip.hits.length || !chip.inside) {
          failures.push({ width: viewport.width, ...chip });
        }
      }
    }
    expect(failures).toEqual([]);
    // A genuinely full bay must not draw over its content. On resize back to
    // clear geometry, a shared full-size chip must return without stale attrs.
    if (family === "per-bay") {
      await page.locator(".slot-latch").evaluateAll((parts) => parts.forEach((part) => { part.style.width = "30px"; part.style.left = "0"; }));
      await page.locator("#slot-grid").evaluate((grid) => { grid.style.height = "151px"; });
      await expect.poll(() => page.locator("#fallback-bay-0").evaluate((tile) => getComputedStyle(tile, "::after").content)).toBe("none");
      expect((await paintedStateChips(page)).every((chip) => !chip.chip)).toBe(true);
      await page.locator(".slot-latch").evaluateAll((parts) => parts.forEach((part) => part.remove()));
      await page.locator("#slot-grid .slot-tile").evaluateAll((tiles) => tiles.forEach((tile) => { tile.style.width = "80px"; }));
      await page.locator("#slot-grid").evaluate((grid) => { grid.style.height = "152px"; });
      await expect(page.locator(".chassis-shell")).toHaveAttribute("data-chip-spot", "corner");
      for (const chip of await paintedStateChips(page)) {
        expect(chip.chip.right - chip.chip.left).toBe(24);
        expect(chip.hits).toEqual([]);
        expect(chip.inside).toBe(true);
      }
    }
    expect(errors).toEqual([]);
  });
}

// #1123: the same guarantee for storage-view cards (NVMe carrier boards,
// SATADOM and embedded boot media), in both colour modes. The primary
// device line is checked by name: a visible serial line below it is a
// different field.
const CARD_LINES = ["slot", "size-chip", "device", "summary", "tertiary"].map((part) => `.storage-view-runtime-card-${part}`).join(", ");

const CARD_CASES = [
  ...CARD_VIEWS.map((view) => ({ view: `storage view ${view}`, open: (page) => openHeatMap(page, cardFixture, null, `?storage_view_id=${view}`) })),
  { view: "live nvme-carrier face", open: (page) => openHeatMap(page, liveNvmeFixture, LIVE_NVME.bays) },
];

for (const { view, open } of CARD_CASES) {
  test(`value badge leaves every card text line visible on ${view}`, async ({ page }) => {
    const errors = await open(page);
    for (const colours of ["bands", "relative"]) {
      await page.locator("#heatmap-colour-select").selectOption(colours);
      for (const viewport of VIEWPORTS) {
        await page.setViewportSize(viewport);
        expect(await page.evaluate(() => window.innerWidth), "actual viewport width").toBe(viewport.width);
        const tiles = await page.locator("#slot-grid .slot-tile.heatmap-active").evaluateAll((elements, selector) => elements.map((tile) => {
          const box = (rect) => ({ left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom });
          const overlap = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
            && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
          const inside = (inner, outer) => inner.left >= outer.left - 1 && inner.right <= outer.right + 1
            && inner.top >= outer.top - 1 && inner.bottom <= outer.bottom + 1;
          const badgeElement = tile.querySelector(".slot-heatmap-value");
          const badge = box(badgeElement.getBoundingClientRect());
          const tileBox = box(tile.getBoundingClientRect());
          const lines = [];
          for (const line of tile.querySelectorAll(selector)) {
            if (!line.textContent.trim() || !line.checkVisibility({ visibilityProperty: true })) continue;
            const range = document.createRange();
            range.selectNodeContents(line);
            const rects = [...range.getClientRects()].filter((rect) => rect.width > 0).map(box);
            lines.push({
              line: line.className.replace("storage-view-runtime-card-", ""),
              text: line.textContent.trim(),
              covered: rects.some((rect) => overlap(rect, badge)),
              inTile: rects.every((rect) => inside(rect, tileBox)),
            });
          }
          return {
            slot: tile.dataset.slot,
            badge,
            badgeInTile: inside(badge, tileBox),
            badgeClipped: badgeElement.scrollHeight > badgeElement.clientHeight + 1 || badgeElement.scrollWidth > badgeElement.clientWidth + 1,
            lines,
          };
        }), CARD_LINES);
        expect(tiles.length, `${view} has heat-map cards`).toBeGreaterThan(0);
        const where = (tile, line = "") => `${view} ${colours} at ${viewport.width} px, card ${tile.slot} ${line}`;
        for (const tile of tiles) {
          expect(tile.badgeInTile, where(tile, "badge inside the card")).toBe(true);
          expect(tile.badgeClipped, where(tile, "badge text clipped")).toBe(false);
          expect(tile.lines.map((line) => line.line), where(tile, "primary device line shown")).toContain("device");
          for (const line of tile.lines) {
            expect(line.covered, where(tile, `${line.line} "${line.text}" covered by the badge ${JSON.stringify(tile.badge)}`)).toBe(false);
            expect(line.inTile, where(tile, `${line.line} inside the card`)).toBe(true);
          }
        }
      }
    }
    expect(errors).toEqual([]);
  });
}
