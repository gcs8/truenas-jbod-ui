"use strict";

const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createRequire } = require("node:module");
const { buildFixture, installFixture, origin } = require("./fixtures/release-contract");

// Execute the actual release callbacks and helpers, not a second implementation.
// Their live opt-in hook is deliberately not registered here: every browser request
// is intercepted by installFixture, including document and static asset requests.
function loadReleaseSpec(filename) {
  const cases = new Map();
  const register = (name, callback) => cases.set(name, callback);
  register.describe = (_name, callback) => callback();
  register.beforeAll = () => {};
  register.skip = (condition, reason) => {
    if (condition) throw new Error(`Synthetic fixture did not satisfy release-test applicability: ${reason}`);
  };
  register.setTimeout = () => {}; // This suite owns a finite synthetic-only deadline.
  const absolute = path.join(__dirname, filename);
  const localRequire = createRequire(absolute);
  const context = vm.createContext({
    require: name => name === "@playwright/test" ? { test: register, expect } : localRequire(name),
    process: { env: { ...process.env, PLAYWRIGHT_SYSTEM_SETTLE_TIMEOUT_MS: "1500" } },
    URL, console,
  });
  vm.runInContext(fs.readFileSync(absolute, "utf8"), context, { filename: absolute });
  return { cases, context };
}

const switching = loadReleaseSpec("ui-switching.spec.js");
const esxi = loadReleaseSpec("esxi-smoke.spec.js");
const affectedCases = [
  "page loads and exposes the main switching chrome",
  "manual refresh restores focus to the same slot",
  "refresh timing strip shows cache TTLs and auto-refresh state",
  "system switches reuse the cached path and complete cleanly",
  "enclosure switches complete without stale-scope bleed",
  "slot detail clears when the operator switches systems",
  "configured systems and enclosure views complete a release sweep cleanly",
  "snapshot-backed saved chassis views reuse the live slot and detail chrome",
];
let html;
test.beforeAll(() => { html = buildFixture(); });
test.use({ baseURL: origin, viewport: { width: 1280, height: 900 }, serviceWorkers: "block" });

for (const timing of [false, true]) {
  for (const name of affectedCases) {
    test(`timing ${timing ? "on" : "off"}: ${name}`, async ({ page }, testInfo) => {
      test.setTimeout(12_000);
      const { diagnostics } = await installFixture(page, html, { timing });
      try {
        await switching.cases.get(name)({ page });
        expect(diagnostics.errors).toEqual([]);
        expect(diagnostics.warnings).toEqual([]);
        expect(diagnostics.unexpected).toEqual([]);
      } finally {
        await testInfo.attach("synthetic-diagnostics", { body: JSON.stringify(diagnostics, null, 2), contentType: "application/json" });
      }
    });
  }
  for (const hardware of ["fat-twin", "aoc"]) {
  test(`timing ${timing ? "on" : "off"}: applicable ESXi ${hardware} release callback`, async ({ page }, testInfo) => {
    test.setTimeout(12_000);
    const { diagnostics } = await installFixture(page, html, { timing, esxi: hardware });
    try {
      await esxi.cases.get("saved ESXi host renders a supported read-only hardware view")({ page });
      const target = hardware === "fat-twin" ? "esxi-ft-node-2" : "cryostorage-esxi";
      expect(diagnostics.inventory.some(url => new URL(url).searchParams.get("system_id") === target)).toBe(true);
      expect(diagnostics.warnings).toEqual([]);
      expect(diagnostics.errors).toEqual([]);
      expect(diagnostics.unexpected).toEqual([]);
    } finally {
      await testInfo.attach("synthetic-diagnostics", { body: JSON.stringify(diagnostics, null, 2), contentType: "application/json" });
    }
  });
  }
}

function gate() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
}

for (const timing of [false, true]) {
  for (const kind of ["system", "enclosure", "manual", "esxi"]) {
    test(`timing ${timing ? "on" : "off"}: ${kind} readiness rejects stale settled state and delayed busy completion`, async ({ page }, testInfo) => {
      test.setTimeout(12_000);
      const { controls, diagnostics } = await installFixture(page, html, { timing });
      const inventory = gate();
      const runtime = gate();
      let inventoryStarted = false;
      let runtimeStarted = false;
      let completed = false;
      let pending;
      try {
        await switching.context.gotoApp(page);
        await switching.context.setAutoRefresh(page, false);
        // Warm both scopes. The next switch can render a cached, non-busy grid
        // before its NEW network refresh completes, with an old done perf run.
        const target = kind === "esxi" ? "esxi-ft-node-2" : "synthetic-second";
        if (kind === "system" || kind === "esxi") {
          await switching.context.switchSystem(page, target);
          await switching.context.switchSystem(page, "synthetic-system");
        } else if (kind === "enclosure") {
          await switching.context.switchEnclosure(page, "enclosure:enc-b");
          await switching.context.switchEnclosure(page, "enclosure:enc-a");
        }
        const previousRun = await switching.context.latestUiPerfRun(page);
        controls.holdInventory = async () => { inventoryStarted = true; await inventory.promise; };
        controls.holdRuntime = async () => { runtimeStarted = true; await runtime.promise; };
        const readiness = require("./release-readiness");
        pending = (kind === "manual"
          ? readiness.refreshSelectedScope(page, () => page.locator("#refresh-button").click(), {
            systemId: "synthetic-system", enclosureId: "enc-a", force: true,
          })
          : kind === "enclosure"
            ? switching.context.switchEnclosure(page, "enclosure:enc-b")
            : readiness.switchSelectedScope(page, "#system-select", target)
        ).then(() => { completed = true; });
        await expect.poll(() => inventoryStarted).toBe(true);
        await expect(page.locator("#refresh-countdown-label")).toHaveText("Refreshing...");
        await page.waitForTimeout(150);
        expect(completed, "must not accept the cached grid or previous perf run").toBe(false);
        if (timing && previousRun) expect((await switching.context.latestUiPerfRun(page))?.id).toBe(previousRun.id);
        inventory.release();
        await expect.poll(() => runtimeStarted).toBe(true);
        await expect(page.locator("#storage-view-runtime-note")).toBeVisible();
        await page.waitForTimeout(150);
        expect(completed, "must wait for selected-scope runtime busy state to settle").toBe(false);
        runtime.release();
        await pending;
        await expect(page.locator("#slot-grid")).toHaveAttribute("aria-busy", "false");
        await expect(page.locator("#storage-view-runtime-note")).toBeHidden();
        expect(diagnostics.errors).toEqual([]);
        expect(diagnostics.warnings).toEqual([]);
        expect(diagnostics.unexpected).toEqual([]);
      } finally {
        inventory.release();
        runtime.release();
        if (pending) await pending.catch(() => {});
        await testInfo.attach("synthetic-diagnostics", { body: JSON.stringify(diagnostics, null, 2), contentType: "application/json" });
      }
    });
  }
}


async function paintCheckpoint(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

for (const timing of [false, true]) {
  for (const stalePayload of [false, true]) {
    test(`F1: timing ${timing}: overlapping refresh owns new request, stale payload ${stalePayload}`, async ({ page }, testInfo) => {
      const { controls, diagnostics } = await installFixture(page, html, { timing });
      await switching.context.gotoApp(page);
      await switching.context.setAutoRefresh(page, false);
      const snapshot = await page.evaluate(() => window.APP_BOOTSTRAP.snapshot);
      const old = gate(), fresh = gate(), runtime = gate();
      let calls = 0, runtimeStarted = false, completed = false, error;
      let pending;
      // Both are real button-dispatched requests with identical URLs. Making
      // only the old payload invalid separately proves response ownership.
      await page.route("**/api/inventory?**", async route => {
        const n = ++calls;
        await (n === 1 ? old : fresh).promise;
        await route.fulfill({ json: { ...snapshot,
          selected_system_id: n === 1 && stalePayload ? "wrong-synthetic-system" : "synthetic-system",
        } });
      });
      controls.holdRuntime = async () => { runtimeStarted = true; await runtime.promise; };
      try {
        await page.locator("#refresh-button").click();
        await expect.poll(() => calls).toBe(1);
        const previous = (await switching.context.latestUiPerfRun(page))?.id || null;
        pending = require("./release-readiness").refreshSelectedScope(page,
          () => page.locator("#refresh-button").click(), {
            systemId: "synthetic-system", enclosureId: "enc-a", force: true,
          }).then(() => switching.context.waitForRefreshToSettle(page, "manual-refresh", previous))
          .then(() => { completed = true; }, caught => { error = caught.message; });
        await expect.poll(() => calls).toBe(2);
        const oldFinished = page.waitForEvent("requestfinished", request => new URL(request.url()).pathname === "/api/inventory");
        old.release();
        await oldFinished;
        await paintCheckpoint(page);
        expect(runtimeStarted).toBe(false);
        expect(completed).toBe(false);
        expect(error).toBeUndefined();
        fresh.release();
        await expect.poll(() => runtimeStarted).toBe(true);
        await expect(page.locator("#storage-view-runtime-note")).toBeVisible();
        await expect(page.locator("#refresh-countdown-label")).toHaveText("Auto refresh off");
        await paintCheckpoint(page);
        expect(completed, "current runtime is still held at a real paint boundary").toBe(false);
        expect(error).toBeUndefined();
        runtime.release();
        await pending;
        expect(error).toBeUndefined();
        expect(completed).toBe(true);
        await expect(page.locator("#storage-view-runtime-note")).toBeHidden();
        expect(diagnostics.errors).toEqual([]);
        expect(diagnostics.warnings).toEqual([]);
        expect(diagnostics.unexpected).toEqual([]);
      } finally {
        old.release(); fresh.release(); runtime.release();
        if (pending) await pending;
        await testInfo.attach("synthetic-diagnostics", { body: JSON.stringify(diagnostics), contentType: "application/json" });
      }
    });
  }

  test(`F1: timing ${timing}: superseded helper cannot claim a later same-scope refresh`, async ({ page }) => {
    const { controls } = await installFixture(page, html, { timing });
    await switching.context.gotoApp(page);
    await switching.context.setAutoRefresh(page, false);
    const first = gate(), second = gate();
    let calls = 0, error, completed = false;
    controls.holdInventory = async () => { await (++calls === 1 ? first : second).promise; };
    const pending = require("./release-readiness").refreshSelectedScope(page,
      () => page.locator("#refresh-button").click(), {
        systemId: "synthetic-system", enclosureId: "enc-a", force: true,
      }).then(() => { completed = true; }, caught => { error = caught.message; });
    try {
      await expect.poll(() => calls).toBe(1);
      await page.locator("#refresh-button").click();
      await expect.poll(() => calls).toBe(2);
      first.release(); second.release();
      await pending;
      expect(completed).toBe(false);
      expect(error).toContain("readiness request was superseded");
    } finally {
      first.release(); second.release(); await pending;
    }
  });
}

for (const control of ["busy", "inert", "inventory-note", "runtime-note", "error", "system", "enclosure", "location"]) {
  test(`F1: settled-scope rejects ${control} at the completion boundary`, async ({ page }) => {
    await installFixture(page, html);
    await switching.context.gotoApp(page);
    await switching.context.setAutoRefresh(page, false);
    // Test-only negative controls alter one visible predicate on an otherwise
    // settled real page. They do not replace application functions or strings.
    await page.evaluate(kind => {
      const node = id => document.getElementById(id);
      if (kind === "busy") node("slot-grid").setAttribute("aria-busy", "true");
      if (kind === "inert") node("slot-grid").inert = true;
      if (kind === "inventory-note" || kind === "runtime-note") {
        const note = node(kind === "inventory-note" ? "inventory-scope-note" : "storage-view-runtime-note");
        note.classList.remove("hidden");
        note.textContent = "Synthetic blocked scope";
      }
      if (kind === "error") node("status-text").setAttribute("data-tone", "error");
      if (kind === "system") node("system-select").value = "synthetic-second";
      if (kind === "enclosure") node("enclosure-select").value = "enclosure:enc-b";
      if (kind === "location") history.replaceState({}, "", "?system_id=synthetic-second&enclosure_id=enc-a");
    }, control);
    await expect(require("./release-readiness").waitForSelectedScope(page, {
      systemId: "synthetic-system", enclosureValue: "enclosure:enc-a",
    })).rejects.toThrow();
  });
}

test("F1: real in-flight countdown rejects settled-scope readiness", async ({ page }) => {
  const { controls } = await installFixture(page, html);
  await switching.context.gotoApp(page);
  await switching.context.setAutoRefresh(page, false);
  const inventory = gate();
  let started = false;
  controls.holdInventory = async () => { started = true; await inventory.promise; };
  try {
    await page.locator("#refresh-button").click();
    await expect.poll(() => started).toBe(true);
    await expect(page.locator("#refresh-countdown-label")).toHaveText("Refreshing...");
    await expect(require("./release-readiness").waitForSelectedScope(page)).rejects.toThrow();
  } finally {
    inventory.release();
    await require("./release-readiness").waitForSelectedScope(page);
  }
});

test("F1: an action without a new request times out instead of borrowing settled state", async ({ page }) => {
  await installFixture(page, html);
  await switching.context.gotoApp(page);
  await expect(require("./release-readiness").refreshSelectedScope(page, async () => {}, {
    systemId: "synthetic-system", enclosureId: "enc-a", force: true,
  })).rejects.toThrow(/Timeout/);
});
