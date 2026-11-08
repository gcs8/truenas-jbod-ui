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
  bays: Array.from({ length: rows * columns }, (_, index) => ({ temperature: 30 + ((index * 7) % 32) })),
}));

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
    data["summary"].update(disk_count=count, mapped_slot_count=count, manual_mapping_count=0)
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

function smartSummary(bay) {
  return {
    available: true,
    temperature_c: bay.temperature,
    warning_temperature_c: bay.warning ?? null,
    critical_temperature_c: bay.critical ?? null,
    smart_health_status: "PASSED",
    rotation_rate_rpm: 7200,
    transport_protocol: "SAS",
  };
}

let fixture;
let shapeFixtures;
let cardFixture;
let liveNvmeFixture;
// A live enclosure drawn as an NVMe carrier board uses the same cards.
const LIVE_NVME = { face_style: "nvme-carrier", rows: 4, columns: 1, bays: [41, 40, 39, 38].map((temperature) => ({ temperature })) };
test.beforeAll(() => {
  [fixture, liveNvmeFixture, ...shapeFixtures] = buildFixtures([{ columns: BAYS.length, bays: BAYS }, LIVE_NVME, ...SHAPES]);
  cardFixture = buildCardFixture();
});

test.use({ serviceWorkers: "block" });

async function openHeatMap(page, pageFixture = fixture, bays = BAYS, query = "") {
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
    if (viewSmart) data = smartSummary(bayAt(Number(viewSmart[1])));
    if (url.pathname === "/api/slots/smart-batch") {
      const requested = JSON.parse(request.postData() || "{}").slots || [];
      data = { summaries: requested.map((slot) => ({ slot, summary: smartSummary(bayAt(slot)) })) };
    }
    const single = /^\/api\/slots\/(\d+)\/smart$/.exec(url.pathname);
    if (single) data = smartSummary(bayAt(Number(single[1])));
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
  if (bays) await expect(page.locator("#slot-grid .slot-tile.heatmap-active")).toHaveCount(bays.length);
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
