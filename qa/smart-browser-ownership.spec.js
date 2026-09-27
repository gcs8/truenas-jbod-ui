"use strict";

// Complete production assets and templates, synthetic HTTP only. The test export
// exposes closure state/entry points without replacing production orchestration.
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const revision = process.env.SMART_SOURCE_REVISION;
function source(file) {
  return revision ? execFileSync("git", ["show", `${revision}:${file}`], { cwd: root, encoding: "utf8" }) : fs.readFileSync(path.join(root, file), "utf8");
}
function exposed(file, names) {
  return source(file).replace(/\}\)\(\);\s*$/, `window.smartTest = { ${names} };\n})();`);
}
let fixture;
test.beforeAll(() => {
  fixture = JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json
from scripts.build_current_source_browser_fixture import build_synthetic_live_snapshot, build_live_fixture_html, build_fixture_request, TEMPLATES
from app.models.domain import StorageViewRuntimePayload
snapshot = build_synthetic_live_snapshot().model_dump(mode="json")
snapshot["slots"][0]["serial"] = "SANITIZED-OWNER-A"
snapshot["systems"].append(dict(id="synthetic-second", label="Second System", platform="linux"))
runtime = dict(system_id="synthetic-system", views=[dict(id="saved-chassis", label="Saved Chassis", kind="manual", template_id="synthetic-profile", slot_count=1, slot_layout=[[0]], slots=[dict(slot_index=0, slot_label="00", occupied=True, state="matched", source="inventory_candidate", device_name="sdx", serial="SANITIZED-OWNER-A")])])
fabric = dict(available=True, platform="linux", system_id="synthetic-system", selected_enclosure_id="enc-a", nodes=[dict(id="bay:0", kind="bay", label="Disk 1", related_slots=[0], metrics={}, raw={})], traces=[dict(id="bay:0", kind="bay", label="Disk 1", slots=[0], node_ids=["bay:0"], link_ids=[], metrics={}, evidence=[])], links=[], controllers=[], paths=[], expanders=[], enclosures=[], warnings=[], sources={}, raw={})
html = build_live_fixture_html(StorageViewRuntimePayload.model_validate(runtime))
fabric_html = TEMPLATES.env.get_template("sas_fabric.html").render(request=build_fixture_request(), snapshot=snapshot, fabric=fabric, app_version="synthetic", bootstrap_json=json.dumps(dict(snapshot=snapshot, fabric=fabric)))
print(json.dumps(dict(html=html, fabricHtml=fabric_html, snapshot=snapshot, runtime=runtime, fabric=fabric)))
`], { cwd: root, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 }));
});

test.use({ viewport: { width: 1280, height: 900 }, serviceWorkers: "block" });
async function open(page, { fabric = false, strategy = "single" } = {}) {
  const http = { pending: [], calls: [], errors: [], snapshot: structuredClone(fixture.snapshot), runtime: structuredClone(fixture.runtime) };
  page.on("pageerror", error => http.errors.push(error.message));
  await page.addInitScript(({ snapshot, runtime, strategy }) => {
    Object.defineProperty(window, "APP_BOOTSTRAP", { configurable: true, set(value) {
      value.snapshot = snapshot;
      value.storageViewsRuntime = runtime;
      value.initialSelectedSlot = null;
      value.smartPrefetchStrategy = strategy;
      value.smartPrefetchDelayMs = 600000;
      value.smartPrefetchChunkSize = 1;
      Object.defineProperty(window, "APP_BOOTSTRAP", { value, writable: true });
    } });
    const raf = window.requestAnimationFrame.bind(window);
    window.paintQueue = [];
    window.requestAnimationFrame = callback => window.holdPaint ? (window.paintQueue.push(callback), 1) : raf(callback);
    window.releasePaint = () => { window.holdPaint = false; window.paintQueue.splice(0).forEach(callback => raf(callback)); };
  }, { snapshot: http.snapshot, runtime: http.runtime, strategy });
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== "https://synthetic.invalid") throw new Error(`Unexpected external request ${url.origin}`);
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: fabric ? fixture.fabricHtml : fixture.html });
    if (url.pathname === "/static/app.js") return route.fulfill({ contentType: "text/javascript", body: exposed("app/static/app.js", "state, ensureSmartSummary, ensureStorageViewSmartSummary, ensureSmartSummaryOnInteraction, runSmartPrefetch, scheduleSmartPrefetch, refreshSnapshot, applySnapshot, applyStorageViewRuntime, getSmartCacheKey, getStorageViewSmartCacheKey, getSmartSummaryEntry, getStorageViewSmartSummaryEntry, renderAll") });
    if (url.pathname === "/static/sas_fabric_view.js") return route.fulfill({ contentType: "text/javascript", body: exposed("app/static/sas_fabric_view.js", "state, render, applySnapshot, refreshFabric, ensureSelectedSmartSummary, smartSummaryForSlot") });
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(root, url.pathname);
      return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
    }
    if (url.pathname.endsWith("/smart") || url.pathname.endsWith("/smart-batch")) {
      http.calls.push(url.pathname);
      http.pending.push(route);
      return;
    }
    let data = { available: false, ok: true };
    if (url.pathname === "/api/inventory") data = { ...http.snapshot, selected_system_id: url.searchParams.get("system_id") || http.snapshot.selected_system_id };
    if (url.pathname === "/api/storage-views") data = { ...http.runtime, system_id: url.searchParams.get("system_id") || http.runtime.system_id };
    if (url.pathname === "/api/sas-fabric") data = fixture.fabric;
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto(`https://synthetic.invalid/${fabric ? "?mode=disk" : ""}`);
  await expect.poll(() => page.evaluate(() => Boolean(window.smartTest))).toBe(true);
  return http;
}
async function settle(route, temperature = 71, available = true) {
  const summary = { available, temperature_c: temperature, serial: "SANITIZED-OWNER-A", message: available ? "" : "Temporarily unavailable" };
  const data = route.request().url().includes("smart-batch")
    ? { summaries: route.request().postDataJSON().slots.map(slot => ({ slot, summary })) } : summary;
  await route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
}
async function startBatch(page) {
  await page.evaluate(() => { const t = smartTest; t.scheduleSmartPrefetch(); void t.runSmartPrefetch(t.state.smartPrefetchToken, t.state.smartPrefetchScopeKey); });
}
async function startSlot(page, saved = false) {
  await page.evaluate(saved => { const t = smartTest; const view = t.state.storageViewsRuntime.views[0]; void (saved ? t.ensureStorageViewSmartSummary(view, view.slots[0]) : t.ensureSmartSummary(t.state.snapshot.slots[0])); }, saved);
}
async function cachedTemperatures(page) {
  return page.evaluate(() => Object.values(smartTest.state.smartSummaries).map(entry => entry.data?.temperature_c).filter(value => value != null));
}

// Saved candidates and live slots are produced independently. Exercise their
// compatibility through the real selector and tile, not just an exported key.
async function setSnapshotBackedIdentity(page, live, saved) {
  await page.evaluate(({ live, saved }) => {
    const t = smartTest;
    const snapshot = structuredClone(t.state.snapshot);
    Object.assign(snapshot.slots[0], { serial: null, logical_unit_id: null, gptid: null }, live);
    t.applySnapshot(snapshot);
    const runtime = structuredClone(t.state.storageViewsRuntime);
    runtime.views[0].backing_enclosure_id = "enc-a";
    Object.assign(runtime.views[0].slots[0], {
      snapshot_slot: 0, source: "inventory_candidate",
      serial: null, logical_unit_id: null, gptid: null,
    }, saved);
    t.applyStorageViewRuntime(runtime);
    t.renderAll();
  }, { live, saved });
}
async function selectSavedTile(page) {
  await page.locator("#enclosure-select").selectOption("view:saved-chassis");
  await page.locator('.slot-tile[data-slot="0"]').click();
  expect(await page.evaluate(() => smartTest.state.selectedStorageViewRuntimeId)).toBe("saved-chassis");
  expect(await page.evaluate(() => smartTest.state.selectedSlot)).toBe(0);
}
const knownLiveIdentity = {
  serial: "SANITIZED-OWNER-A", logical_unit_id: "SANITIZED-LUID", gptid: "synthetic-gptid-0",
};
const savedIdentityCases = [
  ["missing secondary LUID", knownLiveIdentity, { serial: "SANITIZED-OWNER-A", gptid: "synthetic-gptid-0" }],
  ["case normalization", knownLiveIdentity, { serial: "sanitized-owner-a", logical_unit_id: "sanitized-luid", gptid: "SYNTHETIC-GPTID-0" }],
  ["whitespace normalization", knownLiveIdentity, { serial: "  SANITIZED-OWNER-A\t", logical_unit_id: " SANITIZED-LUID " }],
  ["serial without secondaries", knownLiveIdentity, { serial: "SANITIZED-OWNER-A" }],
  ["LUID without secondary GPTID", { logical_unit_id: "SANITIZED-LUID", gptid: "synthetic-gptid-0" }, { logical_unit_id: "SANITIZED-LUID" }],
  ["LUID normalization", { logical_unit_id: "SANITIZED-LUID" }, { serial: " ", logical_unit_id: " sanitized-luid " }],
  ["GPTID normalization", { gptid: "synthetic-gptid-0" }, { gptid: " SYNTHETIC-GPTID-0 " }],
];
for (const [name, live, saved] of savedIdentityCases) {
  test(`saved/live compatibility admits public selection: ${name}`, async ({ page }) => {
    const http = await open(page);
    await setSnapshotBackedIdentity(page, live, saved);
    await selectSavedTile(page);
    expect(http.errors).toEqual([]);
    await expect.poll(() => http.pending.length, { timeout: 3000 }).toBe(1);
    expect(http.calls).toEqual(["/api/storage-views/saved-chassis/slots/0/smart"]);
    await settle(http.pending[0], 46);
    await expect.poll(() => cachedTemperatures(page)).toContain(46);
    await startSlot(page, true);
    expect(http.calls.length).toBe(1);
    expect(http.errors).toEqual([]);
  });
}
const refusedIdentityCases = [
  ["conflicting serial despite equal secondaries", knownLiveIdentity, { ...knownLiveIdentity, serial: "SANITIZED-OWNER-B" }],
  ["conflicting LUID despite equal serial", knownLiveIdentity, { ...knownLiveIdentity, logical_unit_id: "SANITIZED-OTHER" }],
  ["conflicting GPTID despite equal serial", knownLiveIdentity, { ...knownLiveIdentity, gptid: "synthetic-other" }],
  ["unknown live", {}, { serial: "SANITIZED-OWNER-A" }],
  ["unknown saved", knownLiveIdentity, {}],
  ["explicit unknown live", { ...knownLiveIdentity, identity_state: "unknown" }, knownLiveIdentity],
  ["cross-field collision", { serial: "SANITIZED-SAME" }, { logical_unit_id: "SANITIZED-SAME" }],
  ["LUID-only replacement", { logical_unit_id: "SANITIZED-LUID" }, { logical_unit_id: "SANITIZED-OTHER" }],
  ["GPTID-only replacement", { gptid: "synthetic-gptid-0" }, { gptid: "synthetic-other" }],
  ["different strongest field despite shared secondary", knownLiveIdentity, { logical_unit_id: "SANITIZED-LUID", gptid: "synthetic-gptid-0" }],
];
for (const [name, live, saved] of refusedIdentityCases) {
  test(`saved/live compatibility refuses public selection: ${name}`, async ({ page }) => {
    const http = await open(page);
    await setSnapshotBackedIdentity(page, live, saved);
    await selectSavedTile(page);
    await startSlot(page, true);
    await page.waitForTimeout(60);
    expect(http.calls).toEqual([]);
    expect(await cachedTemperatures(page)).toEqual([]);
    expect(http.errors).toEqual([]);
  });
}
for (const change of ["replacement", "unknown", "return"]) {
  test(`saved/live compatibility retains stale-owner fence: ${change}`, async ({ page }) => {
    const http = await open(page);
    await setSnapshotBackedIdentity(page, knownLiveIdentity, { serial: "sanitized-owner-a" });
    await selectSavedTile(page);
    expect(http.errors).toEqual([]);
    await expect.poll(() => http.pending.length, { timeout: 3000 }).toBe(1);
    await page.evaluate(change => {
      const t = smartTest;
      const original = structuredClone(t.state.snapshot);
      const next = structuredClone(original);
      Object.assign(next.slots[0], {
        serial: change === "unknown" ? null : "SANITIZED-OWNER-B",
        logical_unit_id: null, gptid: null,
      });
      t.applySnapshot(next);
      if (change === "return") t.applySnapshot(original);
    }, change);
    await settle(http.pending[0], 77);
    await page.waitForTimeout(60);
    expect(await cachedTemperatures(page)).not.toContain(77);
    // Recovery gets a fresh owner, including A-to-B-to-A, never the old result.
    await setSnapshotBackedIdentity(page, knownLiveIdentity, { serial: "sanitized-owner-a" });
    await startSlot(page, true);
    await expect.poll(() => http.pending.length).toBe(2);
    await settle(http.pending[1], 47);
    await expect.poll(() => cachedTemperatures(page)).toContain(47);
    expect(http.errors).toEqual([]);
  });
}

for (const strategy of ["single", "chunked"]) {
  for (const change of ["system", "replacement", "return"]) {
    test(`${strategy} batch rejects old completion during refresh paint: ${change}`, async ({ page }) => {
      const http = await open(page, { strategy });
      await startBatch(page);
      await expect.poll(() => http.pending.length).toBe(1);
      if (change === "replacement") http.snapshot.slots[0].serial = "SANITIZED-OWNER-B";
      await page.evaluate(() => { window.holdPaint = true; });
      if (change === "replacement") await page.evaluate(() => { void smartTest.refreshSnapshot(false); });
      else {
        await page.locator("#system-select").selectOption("synthetic-second");
        await expect.poll(() => page.evaluate(() => smartTest.state.snapshot.selected_system_id)).toBe("synthetic-second");
        if (change === "return") await page.locator("#system-select").selectOption("synthetic-system");
      }
      await expect.poll(() => page.evaluate(() => window.paintQueue.length)).toBeGreaterThan(0);
      await settle(http.pending[0]);
      await page.waitForTimeout(60);
      expect(await cachedTemperatures(page)).not.toContain(71);
      await page.evaluate(() => window.releasePaint());
      await startSlot(page);
      await expect.poll(() => http.pending.length).toBe(2);
      await settle(http.pending[1], 32);
      await expect.poll(() => cachedTemperatures(page)).toContain(32);
      expect(http.errors).toEqual([]);
    });
  }
}
for (const saved of [false, true]) {
  test(`${saved ? "saved" : "live"} cold repeated interactions coalesce and recover`, async ({ page }) => {
    const http = await open(page);
    if (saved) await page.locator("#enclosure-select").selectOption("view:saved-chassis");
    await startSlot(page, saved);
    await expect.poll(() => http.pending.length).toBe(1);
    for (let i = 0; i < 3; i += 1) await startSlot(page, saved);
    // The delegated handlers are exercised as well as detail's ensure entry point.
    await page.locator(".slot-tile[data-slot='0']").dispatchEvent("mouseover");
    await page.locator(".slot-tile[data-slot='0']").dispatchEvent("focusin");
    await page.waitForTimeout(50);
    expect(http.calls.length).toBe(1);
    await http.pending[0].abort();
    await page.waitForTimeout(50);
    // Use the existing freshness interval, not a new visual-expiry contract.
    await page.evaluate(() => { Object.values(smartTest.state.smartSummaries).forEach(entry => { entry.requestedAt = 1; }); });
    await startSlot(page, saved);
    await expect.poll(() => http.pending.length).toBe(2);
    await settle(http.pending[1], 32);
    await expect.poll(() => cachedTemperatures(page)).toContain(32);
    expect(http.errors).toEqual([]);
  });
  test(`${saved ? "saved" : "live"} replacement and unknown identity withhold cached data`, async ({ page }) => {
    const http = await open(page);
    await startSlot(page, saved);
    await expect.poll(() => http.pending.length).toBe(1);
    await settle(http.pending[0]);
    await expect.poll(() => cachedTemperatures(page)).toContain(71);
    const lookup = () => page.evaluate(saved => {
      const t = smartTest; const view = t.state.storageViewsRuntime.views[0];
      return (saved ? t.getStorageViewSmartSummaryEntry(view, view.slots[0]) : t.getSmartSummaryEntry(t.state.snapshot.slots[0]))?.data?.temperature_c ?? null;
    }, saved);
    expect(await lookup()).toBe(71);
    for (const serial of ["SANITIZED-OWNER-B", null, "SANITIZED-OWNER-A"]) {
      await page.evaluate(({ saved, serial }) => {
        const t = smartTest;
        if (saved) { const runtime = structuredClone(t.state.storageViewsRuntime); runtime.views[0].slots[0].serial = serial; runtime.views[0].slots[0].gptid = null; t.applyStorageViewRuntime(runtime); }
        else { const snapshot = structuredClone(t.state.snapshot); Object.assign(snapshot.slots[0], { serial, gptid: null, logical_unit_id: null }); t.applySnapshot(snapshot); }
      }, { saved, serial });
      expect(await lookup()).toBeNull();
    }
    expect(http.errors).toEqual([]);
  });
}
test("active batch suppresses slot calls but unowned queued entry can dispatch", async ({ page }) => {
  const http = await open(page);
  await startBatch(page);
  await expect.poll(() => http.pending.length).toBe(1);
  await page.evaluate(() => { for (let i = 0; i < 4; i += 1) void smartTest.ensureSmartSummaryOnInteraction(smartTest.state.snapshot.slots[0]); });
  await page.waitForTimeout(50);
  expect(http.calls).toEqual(["/api/slots/smart-batch"]);
  await settle(http.pending[0], 32);
  await expect.poll(() => cachedTemperatures(page)).toContain(32);
  await page.evaluate(() => { const t = smartTest; const key = t.getSmartCacheKey(t.state.snapshot.slots[0]); t.state.smartSummaries[key] = { queued: true, loading: true, requestedAt: Date.now() }; });
  await startSlot(page);
  await expect.poll(() => http.pending.length).toBe(2);
  expect(http.calls[1]).toBe("/api/slots/0/smart");
});
for (const change of ["replacement", "unknown", "return"]) {
  test(`Fabric rejects in-flight ${change} and unchanged cache remains warm`, async ({ page }) => {
    const http = await open(page, { fabric: true });
    await expect.poll(() => http.pending.length).toBe(1);
    await page.evaluate(change => {
      const t = smartTest; const original = structuredClone(t.state.snapshot);
      const next = structuredClone(original);
      Object.assign(next.slots[0], { serial: change === "unknown" ? null : "SANITIZED-OWNER-B", gptid: null, logical_unit_id: null });
      t.applySnapshot(next);
      if (change === "return") t.applySnapshot(original);
    }, change);
    await settle(http.pending[0]);
    await page.waitForTimeout(50);
    expect(await page.evaluate(() => smartTest.smartSummaryForSlot(0)?.temperature_c ?? null)).toBeNull();
    await page.evaluate(() => { smartTest.applySnapshot(window.SAS_FABRIC_BOOTSTRAP.snapshot); smartTest.render(); });
    await expect.poll(() => http.pending.length).toBe(2);
    await settle(http.pending[1], 32);
    await expect.poll(() => page.evaluate(() => smartTest.smartSummaryForSlot(0)?.temperature_c)).toBe(32);
    await page.evaluate(() => smartTest.render());
    expect(http.calls.length).toBe(2);
    expect(http.errors).toEqual([]);
  });
}
for (const failure of ["unavailable", "fetch-failure"]) {
test(`Fabric ${failure} retries on refresh and success revalidates after bounded age`, async ({ page }) => {
  const http = await open(page, { fabric: true });
  await expect.poll(() => http.pending.length).toBe(1);
  if (failure === "unavailable") await settle(http.pending[0], null, false);
  else await http.pending[0].abort();
  await expect.poll(() => page.evaluate(() => smartTest.smartSummaryForSlot(0)?.available)).toBe(false);
  await page.locator("#fabric-refresh-button").click();
  await expect.poll(() => http.pending.length).toBe(2);
  await settle(http.pending[1], 32);
  await expect.poll(() => page.evaluate(() => smartTest.smartSummaryForSlot(0)?.temperature_c)).toBe(32);
  await page.clock.install();
  await page.clock.fastForward(301000);
  await page.evaluate(() => smartTest.render());
  await expect.poll(() => http.pending.length).toBe(3);
  await settle(http.pending[2], 33);
  await expect.poll(() => page.evaluate(() => smartTest.smartSummaryForSlot(0)?.temperature_c)).toBe(33);
  expect(http.errors).toEqual([]);
});
}

for (const saved of [false, true]) {
  test(`${saved ? "saved" : "live"} single completion cannot survive replacement and return`, async ({ page }) => {
    const http = await open(page);
    await startSlot(page, saved);
    await expect.poll(() => http.pending.length).toBe(1);
    await page.evaluate(saved => {
      const t = smartTest;
      if (saved) {
        const original = structuredClone(t.state.storageViewsRuntime);
        const next = structuredClone(original); next.views[0].slots[0].serial = "SANITIZED-OWNER-B";
        t.applyStorageViewRuntime(next); t.applyStorageViewRuntime(original);
      } else {
        const original = structuredClone(t.state.snapshot);
        const next = structuredClone(original); next.slots[0].serial = "SANITIZED-OWNER-B";
        t.applySnapshot(next); t.applySnapshot(original);
      }
    }, saved);
    await settle(http.pending[0]);
    await page.waitForTimeout(60);
    expect(await cachedTemperatures(page)).not.toContain(71);
    await startSlot(page, saved);
    await expect.poll(() => http.pending.length).toBe(2);
    await settle(http.pending[1], 32);
    await expect.poll(() => cachedTemperatures(page)).toContain(32);
  });
}
test("a saved snapshot-backed disk with unknown live identity cannot reuse cached SMART", async ({ page }) => {
  const http = await open(page);
  await page.evaluate(() => {
    const t = smartTest; const runtime = structuredClone(t.state.storageViewsRuntime);
    runtime.views[0].backing_enclosure_id = "enc-a";
    Object.assign(runtime.views[0].slots[0], { snapshot_slot: 0, source: "snapshot_slot", gptid: "synthetic-gptid-0" });
    t.applyStorageViewRuntime(runtime);
  });
  await startSlot(page, true);
  await expect.poll(() => http.pending.length).toBe(1);
  await settle(http.pending[0]);
  await expect.poll(() => cachedTemperatures(page)).toContain(71);
  await page.evaluate(() => {
    const t = smartTest; const snapshot = structuredClone(t.state.snapshot);
    snapshot.slots[0].identity_state = "unknown";
    t.applySnapshot(snapshot);
  });
  expect(await page.evaluate(() => {
    const t = smartTest; const view = t.state.storageViewsRuntime.views[0];
    return t.getStorageViewSmartSummaryEntry(view, view.slots[0])?.data?.temperature_c ?? null;
  })).toBeNull();
  await startSlot(page, true);
  expect(http.calls.length).toBe(1);
});
test("selection invalidation precedes inventory response and forbids mismatched dispatch", async ({ page }) => {
  const http = await open(page);
  await startSlot(page);
  await expect.poll(() => http.pending.length).toBe(1);
  let inventory;
  await page.route("**/api/inventory?**", route => { inventory = route; });
  await page.locator("#system-select").selectOption("synthetic-second");
  await expect.poll(() => Boolean(inventory)).toBe(true);
  await settle(http.pending[0]);
  await page.waitForTimeout(50);
  expect(await cachedTemperatures(page)).not.toContain(71);
  await startSlot(page);
  await page.waitForTimeout(50);
  expect(http.calls.length).toBe(1);
});
for (const kind of ["live", "saved", "batch", "fabric"]) {
  test(`${kind} pending timeout releases ownership for a later request`, async ({ page }) => {
    test.setTimeout(25000);
    const http = await open(page, { fabric: kind === "fabric" });
    if (kind === "batch") await startBatch(page);
    else if (kind !== "fabric") await startSlot(page, kind === "saved");
    await expect.poll(() => http.pending.length).toBe(1);
    await expect.poll(() => page.evaluate(fabric => fabric
      ? Object.keys(smartTest.state.smartRequests).length === 0
      : Object.values(smartTest.state.smartSummaries).every(entry => !entry.loading && !entry.refreshing), kind === "fabric"), { timeout: 19000 }).toBe(true);
    await page.clock.install();
    await page.clock.fastForward(301000);
    if (kind === "fabric") await page.evaluate(() => smartTest.render());
    else if (kind === "batch") await startBatch(page);
    else await startSlot(page, kind === "saved");
    await expect.poll(() => http.pending.length).toBe(2);
    await settle(http.pending[1], 32);
    if (kind === "fabric") await expect.poll(() => page.evaluate(() => smartTest.smartSummaryForSlot(0)?.temperature_c)).toBe(32);
    else await expect.poll(() => cachedTemperatures(page)).toContain(32);
    expect(http.errors).toEqual([]);
  });
}

for (const kind of ["live", "saved", "chunked"]) {
  test(`${kind} pending ownership is not a wall-clock freshness decision`, async ({ page }) => {
    const http = await open(page, { strategy: "chunked" });
    if (kind === "chunked") {
      await page.evaluate(() => {
        const t = smartTest; const snapshot = structuredClone(t.state.snapshot);
        snapshot.slots = [0, 1, 2].map(slot => ({ ...snapshot.slots[0], slot, serial: `SANITIZED-CHUNK-${slot}` }));
        t.applySnapshot(snapshot);
      });
      await startBatch(page);
      await expect.poll(() => http.pending.length).toBe(2);
    } else {
      await startSlot(page, kind === "saved");
      await expect.poll(() => http.pending.length).toBe(1);
    }
    // Native AbortSignal owns the real timeout. A wall-clock step must not
    // abandon it, including a later chunk reserved by the active batch.
    await page.clock.install();
    await page.clock.setSystemTime(new Date(Date.now() + 20000));
    if (kind === "chunked") await page.evaluate(() => { void smartTest.ensureSmartSummaryOnInteraction(smartTest.state.snapshot.slots[2]); });
    else await startSlot(page, kind === "saved");
    await page.waitForTimeout(50);
    expect(http.calls.length).toBe(kind === "chunked" ? 2 : 1);
    if (kind === "chunked") {
      await settle(http.pending[0], 31);
      await expect.poll(() => http.pending.length).toBe(3);
      expect(http.pending[2].request().postDataJSON().slots).toEqual([2]);
      await settle(http.pending[1], 32);
      await settle(http.pending[2], 33);
      await expect.poll(() => cachedTemperatures(page)).toContain(33);
    } else {
      await settle(http.pending[0], 32);
      await expect.poll(() => cachedTemperatures(page)).toContain(32);
    }
    expect(http.errors).toEqual([]);
  });
}
