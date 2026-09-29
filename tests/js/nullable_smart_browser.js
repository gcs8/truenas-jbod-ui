"use strict";

// Optional fixture-only Chromium regression. Run with the project PYTHON interpreter:
// PYTHON=/path/to/venv/bin/python node tests/js/nullable_smart_browser.js [baseline-revision]
// All browser requests are intercepted; no server or live appliance is used.
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { chromium, expect } = require("@playwright/test");
const ROOT = path.resolve(__dirname, "../..");
assert.ok(process.env.PYTHON, "Set PYTHON to the project interpreter explicitly");
const fixture = JSON.parse(execFileSync(process.env.PYTHON, ["-c", `
import asyncio, json
from scripts.build_current_source_browser_fixture import build_fixture_html
from app.models.domain import SmartSummaryView
cases = {
    "unknown": {},
    "cool": {"temperature_c": 30},
    "zero": {"temperature_c": 0, "read_error_count": 0, "write_error_count": 0,
             "uncorrected_read_errors": 12, "uncorrected_write_errors": 4},
    "errors": {"temperature_c": 30, "uncorrected_read_errors": 12, "uncorrected_write_errors": 4},
    "critical": {"temperature_c": 30, "critical_temperature_c": 30},
    "worn": {"temperature_c": 30, "endurance_remaining_percent": 0},
}
print(json.dumps({"html": asyncio.run(build_fixture_html()), "summaries": {
    name: json.loads(SmartSummaryView(available=True, **values).model_dump_json())
    for name, values in cases.items()
}}))
`], { cwd: ROOT, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 }));
assert.equal(fixture.summaries.cool.critical_temperature_c, null);
assert.equal(fixture.summaries.cool.endurance_remaining_percent, null);
assert.equal(fixture.summaries.errors.read_error_count, null);
let html = fixture.html;
if (process.argv[2]) {
  const current = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
  const baseline = execFileSync("git", ["show", `${process.argv[2]}:app/static/app.js`], { cwd: ROOT, encoding: "utf8", maxBuffer: 4 * 1024 * 1024 });
  assert.ok(html.includes(current), "Fixture must inline the exact current app source for baseline replay");
  html = html.replace(current, baseline);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const [name, summary] of Object.entries(fixture.summaries)) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: "block" });
      const errors = [];
      const unexpectedRequests = [];
      try {
        const page = await context.newPage();
        page.on("pageerror", error => errors.push(error.message));
        await context.route("**/*", route => {
          if (route.request().url() === "https://synthetic.invalid/") {
            return route.fulfill({ contentType: "text/html", body: html });
          }
          unexpectedRequests.push(route.request().url());
          return route.abort();
        });
        // Set only fixture data before the unmodified production application starts.
        await page.addInitScript(({ summary }) => {
          let bootstrap;
          Object.defineProperty(window, "APP_BOOTSTRAP", {
            get: () => bootstrap,
            set(value) {
              bootstrap = value;
              const snapshot = value.snapshot;
              snapshot.slots.forEach(slot => {
                if (slot.present) { slot.state = "healthy"; slot.identify_active = false; }
              });
              value.preloadedSnapshotsByEnclosure = {};
              value.preloadedSnapshotSmartSummaries = {};
              value.preloadedSmartSummariesBySlot = {};
              value.preloadedHistoryBySlot = {};
              value.initialSelectedStorageViewId = null;
              value.initialHistoryTimeframeHours = null;
              value.historyConfigured = true;
              for (const slot of snapshot.slots) {
                if (!slot.present) continue;
                value.preloadedSmartSummariesBySlot[slot.slot] = summary;
                const key = `${snapshot.selected_system_id}|${snapshot.selected_enclosure_id}|${slot.slot}`;
                value.preloadedHistoryBySlot[key] = { available: true, configured: true, metrics: {}, events: [], sample_counts: {} };
              }
            },
          });
        }, { summary });
        await page.goto("https://synthetic.invalid/");
        const tile = page.locator('.slot-tile[data-slot="0"]');
        await expect(tile).toBeVisible();
        await page.locator("#heatmap-toggle-button").click();
        const scores = { unknown: "0", cool: "0", zero: "0", errors: "26", critical: "40", worn: "35" };
        await expect(tile.locator(".slot-heatmap-value")).toHaveText(scores[name]);
        await tile.hover();
        const tooltip = page.locator("#slot-tooltip");
        if (["unknown", "cool", "zero"].includes(name)) {
          await expect(tooltip).not.toContainText(/at critical temp|endurance remaining|read errors|write errors/);
        } else if (name === "errors") {
          await expect(tooltip).toContainText("12 read errors");
          await expect(tooltip).toContainText("4 write errors");
        } else if (name === "critical") {
          await expect(tooltip).toContainText("at critical temp 30 C");
        } else {
          await expect(tooltip).toContainText("0% endurance remaining");
        }
        await page.locator("#heatmap-metric-select").selectOption("temperature_c");
        if (name === "unknown") {
          await expect(tile).toHaveClass(/heatmap-missing/);
          await expect(tile.locator(".slot-heatmap-value")).toHaveText("--");
          await expect(tile.locator(".slot-heatmap-value")).toHaveAttribute("title", "Temperature: unknown");
        } else {
          await expect(tile).toHaveClass(/heatmap-active/);
          await expect(tile.locator(".slot-heatmap-value")).toHaveText(name === "zero" ? "0 C" : "30 C");
        }
        await tile.click();
        await page.locator("#history-toggle-button").click();
        for (const label of ["Temperature", "Power On"]) {
          const card = page.locator(".history-metric-card").filter({ has: page.locator(".history-metric-label", { hasText: label }) });
          await expect(card.locator(".history-metric-value")).toHaveText("n/a");
        }
        assert.deepEqual(errors, []);
        assert.deepEqual(unexpectedRequests, []);
        results.push({ case: name, attentionScore: scores[name], temperature: summary.temperature_c, emptyHistory: "n/a", pageErrors: 0, networkRequestsOutsideFixture: 0 });
      } finally {
        await context.close();
      }
    }
    console.log(JSON.stringify({ status: "PASS", scope: "real SmartSummaryView JSON and complete current-source snapshot UI in Chromium; synthetic inputs only", results }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
