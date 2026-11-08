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

function buildFixture() {
  return JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json
import sys

import scripts.build_current_source_browser_fixture as builder
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload

bays = json.loads(sys.argv[1])
count = len(bays)
data = builder.build_synthetic_live_snapshot().model_dump(mode="json")
template = data["slots"][0]
data["slots"] = [
    {
        **template,
        "slot": index,
        "slot_label": f"{index:02d}",
        "column_index": index,
        "device_name": f"sd{index}",
        "serial": f"SANITIZED-HEAT-{index:02d}",
        "gptid": f"synthetic-gptid-{index}",
        "temperature_c": bay["temperature"],
        "notes": None,
    }
    for index, bay in enumerate(bays)
]
layout = [list(range(count))]
data["layout_rows"] = layout
data["layout_slot_count"] = count
data["layout_columns"] = count
data["selected_profile"] = {
    **data["selected_profile"],
    "id": "synthetic-heat-row",
    "label": "Synthetic heat row",
    "panel_title": "Synthetic heat row",
    "rows": 1,
    "columns": count,
    "slot_count": count,
    "slot_layout": layout,
}
data["enclosures"][0].update(profile_id="synthetic-heat-row", rows=1, columns=count, slot_count=count, slot_layout=layout)
data["summary"].update(disk_count=count, mapped_slot_count=count, manual_mapping_count=0)
snapshot = InventorySnapshot.model_validate(data)
builder.build_synthetic_live_snapshot = lambda: snapshot
runtime = StorageViewRuntimePayload(system_id="synthetic-system", views=[])
print(json.dumps(dict(html=builder.build_live_fixture_html(runtime), snapshot=snapshot.model_dump(mode="json"))))
`, JSON.stringify(BAYS)], { cwd: root, encoding: "utf8", maxBuffer: 16 * 1024 * 1024 }));
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
test.beforeAll(() => {
  fixture = buildFixture();
});

test.use({ serviceWorkers: "block" });

async function openHeatMap(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== "https://synthetic.invalid") throw new Error(`Unexpected external request ${url.origin}`);
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: fixture.html });
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(root, "app", url.pathname);
      return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
    }
    let data = { ok: true, available: false };
    if (url.pathname === "/api/inventory") data = fixture.snapshot;
    if (url.pathname === "/api/storage-views") data = { system_id: "synthetic-system", views: [] };
    if (url.pathname === "/api/slots/smart-batch") {
      const requested = JSON.parse(request.postData() || "{}").slots || [];
      data = { summaries: requested.map((slot) => ({ slot, summary: smartSummary(BAYS[slot]) })) };
    }
    const single = /^\/api\/slots\/(\d+)\/smart$/.exec(url.pathname);
    if (single) data = smartSummary(BAYS[Number(single[1])]);
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto("https://synthetic.invalid/");
  await page.locator("#auto-refresh-toggle").uncheck();
  await expect(page.locator("#slot-grid [data-slot]")).toHaveCount(BAYS.length);
  await page.locator("#heatmap-toggle-button").click();
  await page.locator("#heatmap-metric-select").selectOption("temperature_c");
  // The drive-rated bay can only reach "warning" once its SMART summary loads.
  await expect(page.locator('#slot-grid [data-slot="5"]')).toHaveAttribute("data-heatmap-band", "warning");
  return errors;
}

function bay(page, index) {
  return page.locator(`#slot-grid [data-slot="${index}"]`);
}

for (const viewport of [{ width: 1600, height: 1000 }, { width: 1920, height: 1080 }]) {
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
