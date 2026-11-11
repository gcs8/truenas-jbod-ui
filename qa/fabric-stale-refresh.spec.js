"use strict";

// #1158: Storage Fabric reads cached inventory stale-while-revalidate. A read
// just past the short cache TTL answers "stale-hit" while a background refresh
// runs; that must not paint the red STALE banner, and the page must refetch
// once to pick up the refreshed evidence. Old evidence keeps the STALE tone.
// Complete production template and assets; only HTTP, data and the clock are
// synthetic.
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const ORIGIN = "https://synthetic.invalid";
const NOW = Date.parse("2026-11-11T12:00:00Z");
const REFETCH_DELAY_MS = 15_000;
const SCREENSHOT_DIR = process.env.FABRIC_STALE_SCREENSHOT_DIR || "";
const fixture = {};

test.beforeAll(() => {
  Object.assign(fixture, JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json
from scripts.build_current_source_browser_fixture import build_synthetic_live_snapshot, build_fixture_request, TEMPLATES
snapshot = build_synthetic_live_snapshot().model_dump(mode="json")
trace = lambda index: dict(id=f"bay:{index}", kind="bay", label=f"Disk {index + 1}", slots=[index], node_ids=[f"bay:{index}"], link_ids=[], metrics={}, evidence=[])
node = lambda index: dict(id=f"bay:{index}", kind="bay", label=f"Disk {index + 1}", related_slots=[index], metrics={}, raw={})
fabric = dict(available=True, platform="linux", system_id=snapshot["selected_system_id"], selected_enclosure_id=snapshot["selected_enclosure_id"],
              nodes=[node(index) for index in range(3)], traces=[trace(index) for index in range(3)], links=[], controllers=[], paths=[],
              expanders=[], enclosures=[], warnings=[], sources=dict(api=dict(enabled=True, ok=True)), raw={},
              snapshot_cache_state="hit", source_cache_state="hit")
def page(bootstrap_fabric):
    return TEMPLATES.env.get_template("sas_fabric.html").render(
        request=build_fixture_request(), snapshot=snapshot, fabric=fabric, app_version="synthetic",
        bootstrap_json=json.dumps(dict(snapshot=snapshot, fabric=bootstrap_fabric)))
# The server normally renders the first read into the page bootstrap.
snapshot["last_updated"] = "2026-11-11T11:58:00Z"
stale_page = page(dict(fabric, snapshot_cache_state="stale-hit", source_cache_state="stale-hit"))
print(json.dumps(dict(snapshot=snapshot, fabric=fabric, pageWithoutFabric=page(None), pageWithStaleFabric=stale_page)))
`], { cwd: root, encoding: "utf8", maxBuffer: 16 * 1024 * 1024 })));
});

test.use({ serviceWorkers: "block", viewport: { width: 1600, height: 1000 } });

function isoAgo(seconds) {
  return new Date(NOW - seconds * 1000).toISOString();
}

// Each read pops the next scripted reply; the last one repeats.
async function openFabric(page, replies, { bootstrapped = false } = {}) {
  const http = { fabricReads: 0, inventoryReads: 0, errors: [] };
  page.on("pageerror", (error) => http.errors.push(error.message));
  await page.clock.install({ time: NOW });
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort();
    if (url.pathname === "/") {
      return route.fulfill({ contentType: "text/html; charset=utf-8", body: bootstrapped ? fixture.pageWithStaleFabric : fixture.pageWithoutFabric });
    }
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(root, "app", url.pathname);
      return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
    }
    if (url.pathname === "/api/inventory") {
      const reply = replies[Math.min(http.inventoryReads, replies.length - 1)];
      http.inventoryReads += 1;
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...fixture.snapshot, last_updated: isoAgo(reply.ageSeconds) }) });
    }
    if (url.pathname === "/api/sas-fabric") {
      const reply = replies[Math.min(http.fabricReads, replies.length - 1)];
      http.fabricReads += 1;
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ ...fixture.fabric, snapshot_cache_state: reply.cacheState, source_cache_state: reply.cacheState }),
      });
    }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true, available: false }) });
  });
  await page.goto(`${ORIGIN}/`);
  await expect.poll(() => http.fabricReads).toBe(bootstrapped ? 0 : 1);
  return http;
}

const chip = (page) => page.locator("#fabric-api-chip");
const statusText = (page) => page.locator("#fabric-status-text");

async function capture(page, name) {
  if (!SCREENSHOT_DIR) return;
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.locator("#fabric-status-strip").screenshot({ path: path.join(SCREENSHOT_DIR, `${name}.png`) });
}

test("a stale-hit just past the cache TTL reads as refreshing, then one refetch updates in place", async ({ page }) => {
  const http = await openFabric(page, [
    { cacheState: "stale-hit", ageSeconds: 180 },
    { cacheState: "hit", ageSeconds: 2 },
  ]);
  await expect(statusText(page)).toContainText("3 min ago");
  await expect(statusText(page)).toHaveAttribute("data-tone", "info");
  await expect(chip(page)).toHaveText(/OK$/);
  await expect(chip(page)).not.toHaveClass(/partial|error/);
  await expect(statusText(page)).not.toContainText(/retained|stale/i);
  await capture(page, "stale-hit-young-refreshing");

  await page.clock.runFor(REFETCH_DELAY_MS - 1000);
  expect(http.fabricReads).toBe(1);
  await page.clock.runFor(2000);
  await expect.poll(() => http.fabricReads).toBe(2);
  await expect(statusText(page)).toHaveText("Loaded 3 traces for current selection.");
  await expect(chip(page)).toHaveText(/OK$/);
  await capture(page, "after-refetch-hit");

  // One refetch only: no polling loop.
  await page.clock.runFor(10 * REFETCH_DELAY_MS);
  expect(http.fabricReads).toBe(2);
  expect(http.errors).toEqual([]);
});

test("old stale evidence keeps the STALE error tone with its age, and refetches only once", async ({ page }) => {
  const http = await openFabric(page, [{ cacheState: "stale-hit", ageSeconds: 20 * 60 }]);
  await expect(chip(page)).toHaveText(/STALE$/);
  await expect(statusText(page)).toHaveAttribute("data-tone", "error");
  await expect(statusText(page)).toContainText("20 min ago");
  await capture(page, "stale-hit-old-error");

  await page.clock.runFor(REFETCH_DELAY_MS + 1000);
  await expect.poll(() => http.fabricReads).toBe(2);
  await page.clock.runFor(10 * REFETCH_DELAY_MS);
  expect(http.fabricReads).toBe(2);
  await expect(chip(page)).toHaveText(/STALE$/);
  expect(http.errors).toEqual([]);
});

test("trusted-fallback evidence stays STALE even when young", async ({ page }) => {
  const http = await openFabric(page, [{ cacheState: "trusted-fallback", ageSeconds: 30 }]);
  await expect(chip(page)).toHaveText(/STALE$/);
  await expect(statusText(page)).toHaveAttribute("data-tone", "error");
  await capture(page, "trusted-fallback-error");
  expect(http.errors).toEqual([]);
});

test("a manual refresh cancels the pending stale refetch", async ({ page }) => {
  const http = await openFabric(page, [
    { cacheState: "stale-hit", ageSeconds: 60 },
    { cacheState: "hit", ageSeconds: 1 },
  ]);
  await expect(statusText(page)).toHaveAttribute("data-tone", "info");
  await page.locator("#fabric-refresh-button").click();
  await expect.poll(() => http.fabricReads).toBe(2);
  await expect(statusText(page)).toHaveText("Loaded 3 traces for current selection.");
  await page.clock.runFor(10 * REFETCH_DELAY_MS);
  expect(http.fabricReads).toBe(2);
  expect(http.errors).toEqual([]);
});

test("a server-rendered stale-hit bootstrap reads as refreshing and refetches once", async ({ page }) => {
  const http = await openFabric(page, [{ cacheState: "hit", ageSeconds: 1 }], { bootstrapped: true });
  await expect(statusText(page)).toContainText("2 min ago");
  await expect(statusText(page)).toHaveAttribute("data-tone", "info");
  await expect(chip(page)).toHaveText(/OK$/);
  await page.clock.runFor(REFETCH_DELAY_MS + 1000);
  await expect.poll(() => http.fabricReads).toBe(1);
  await expect(statusText(page)).toHaveText("Loaded 3 traces for current selection.");
  await page.clock.runFor(10 * REFETCH_DELAY_MS);
  expect(http.fabricReads).toBe(1);
  expect(http.errors).toEqual([]);
});
