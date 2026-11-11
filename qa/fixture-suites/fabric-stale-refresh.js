"use strict";

// Shared registration for the standalone spec and the existing CI entry point.
module.exports = function registerFixtureSuite({ test, expect }) {
// #1158: Storage Fabric reads cached inventory stale-while-revalidate. A read
// just past the short cache TTL answers "stale-hit" while a background refresh
// runs; that must not paint the red STALE banner, and the page must refetch
// once to pick up the refreshed evidence. Old evidence keeps the STALE tone.
// Complete production template and assets; only HTTP, data and the clock are
// synthetic.
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
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
  await page.clock.pauseAt(NOW);
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
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...fixture.snapshot, refresh_interval_seconds: 30, last_updated: reply.ageSeconds === null ? null : isoAgo(reply.ageSeconds) }) });
    }
    if (url.pathname === "/api/sas-fabric") {
      const reply = replies[Math.min(http.fabricReads, replies.length - 1)];
      http.fabricReads += 1;
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ ...fixture.fabric, snapshot_cache_state: reply.cacheState, source_cache_state: reply.cacheState,
          ...(reply.failedSource ? { sources: { ssh: { enabled: true, ok: false } } } : {}),
        }),
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
  for (const width of [1920, 3840]) {
    await page.setViewportSize({ width, height: 1080 });
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
    await page.mouse.move(0, 0);
    const strip = page.locator("#fabric-status-strip");
    const geometry = await strip.boundingBox();
    await strip.screenshot({ path: path.join(SCREENSHOT_DIR, `${name}-${width}.png`) });
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, `${name}-${width}-viewport.png`) });
    fs.writeFileSync(path.join(SCREENSHOT_DIR, `${name}-${width}.json`), JSON.stringify({
      width, geometry, text: await strip.innerText(),
    }, null, 2));
  }
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

test("a young stale-hit that is still stale after the refetch turns STALE when it ages out, without more reads", async ({ page }) => {
  // Both reads return the same evidence, read 3 min before NOW; with a 30 s
  // refresh interval the 5 min window ends at NOW + 120 s.
  const http = await openFabric(page, [{ cacheState: "stale-hit", ageSeconds: 180 }]);
  await expect(statusText(page)).toHaveAttribute("data-tone", "info");
  await page.clock.runFor(REFETCH_DELAY_MS + 1000);
  await expect.poll(() => http.fabricReads).toBe(2);
  await expect(statusText(page)).toHaveAttribute("data-tone", "info");
  await page.clock.runFor(104_000);
  await expect(chip(page)).toHaveText(/OK$/);
  await page.clock.runFor(1);
  await expect(chip(page)).toHaveText(/STALE$/);
  await expect(statusText(page)).toHaveAttribute("data-tone", "error");
  await expect(statusText(page)).toContainText("5 min ago");
  expect(http.fabricReads).toBe(2);
  expect(http.errors).toEqual([]);
});

test("persisted bfcache restore re-evaluates old evidence immediately and refetches once", async ({ page }) => {
  const http = await openFabric(page, [
    { cacheState: "stale-hit", ageSeconds: 180 },
    { cacheState: "hit", ageSeconds: 1 },
  ]);
  await expect(chip(page)).toHaveText(/OK$/);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })));
  await page.clock.runFor(180_000);
  expect(http.fabricReads).toBe(1);
  // This is an event-path regression, not a claim that routing fixtures enter
  // Chromium's native bfcache. The shipping listeners handle native events too.
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })));
  await expect(chip(page)).toHaveText(/STALE$/);
  await expect(statusText(page)).toHaveAttribute("data-tone", "error");
  await expect(statusText(page)).toContainText("6 min ago");
  await capture(page, "bfcache-restored-old-error");
  await page.clock.runFor(REFETCH_DELAY_MS + 1);
  await expect.poll(() => http.fabricReads).toBe(2);
  await expect(statusText(page)).toHaveText("Loaded 3 traces for current selection.");
  await page.clock.runFor(600_000);
  expect(http.fabricReads).toBe(2);
  expect(http.errors).toEqual([]);
});

test("young bfcache restore rearms the age check after its single stale refetch", async ({ page }) => {
  const http = await openFabric(page, [{ cacheState: "stale-hit", ageSeconds: 180 }]);
  await expect(chip(page)).toHaveText(/OK$/);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })));
  await page.clock.runFor(60_000);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })));
  await expect(chip(page)).toHaveText(/OK$/);
  await page.clock.runFor(REFETCH_DELAY_MS);
  await expect.poll(() => http.fabricReads).toBe(2);
  await expect(statusText(page)).toHaveAttribute("data-tone", "info");
  await page.clock.runFor(45_001);
  await expect(chip(page)).toHaveText(/STALE$/);
  expect(http.fabricReads).toBe(2);
  expect(http.errors).toEqual([]);
});

test("missing age and failed sources remain cautious during revalidation", async ({ page }) => {
  const http = await openFabric(page, [
    { cacheState: "stale-hit", ageSeconds: null },
    { cacheState: "stale-hit", ageSeconds: 30, failedSource: true },
  ]);
  await expect(chip(page)).toHaveText(/STALE$/);
  await expect(statusText(page)).toContainText("unknown age");
  await expect(statusText(page)).toHaveAttribute("data-tone", "error");
  await capture(page, "missing-age-error");
  await page.clock.runFor(REFETCH_DELAY_MS);
  await expect.poll(() => http.fabricReads).toBe(2);
  await expect(chip(page)).toHaveText(/PARTIAL$/);
  await expect(statusText(page)).toContainText("Failed sources: SSH");
  await expect(statusText(page)).toHaveAttribute("data-tone", "error");
  await capture(page, "failed-source-partial");
  await page.clock.runFor(600_000);
  expect(http.fabricReads).toBe(2);
  expect(http.errors).toEqual([]);
});
};
