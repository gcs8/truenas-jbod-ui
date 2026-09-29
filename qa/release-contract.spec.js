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
    process: { env: { ...process.env } },
    URL, console,
  });
  vm.runInContext(fs.readFileSync(absolute, "utf8"), context, { filename: absolute });
  return { cases, context };
}

// Explicit per-call budgets reach the host-required helper, unlike VM-local env.
const negativeTimeout = 1500;
const predicateTimeout = /Timeout 1500ms exceeded while waiting on the predicate/;
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


// Supported empty inventory uses an empty selector and omits both URL scope keys.
// Keep the real template/assets; change only synthetic bootstrap/HTTP data.
async function installEmptyInventory(page, timing) {
  const fixture = await installFixture(page, html, { timing, emptyInventory: true });
  await page.route("**/api/storage-views?**", route => route.fulfill({
    json: { system_id: "synthetic-system", views: [] },
  }));
  await page.route("**/api/inventory?**", async route => {
    fixture.diagnostics.inventory.push(route.request().url());
    await route.fulfill({ json: await page.evaluate(() => window.APP_BOOTSTRAP.snapshot) });
  });
  await page.goto(origin);
  await expect(page.locator("#slot-grid")).toHaveAttribute("aria-busy", "false");
  await switching.context.setAutoRefresh(page, false);
  await expect(page.locator("#enclosure-select")).toHaveValue("");
  expect(new URL(page.url()).searchParams.has("enclosure_id")).toBe(false);
  expect(new URL(page.url()).searchParams.has("storage_view_id")).toBe(false);
  return fixture;
}

for (const timing of [false, true]) {
  for (const operation of ["initial", "refresh"]) {
    test(`empty enclosure: timing ${timing}: ${operation} readiness`, async ({ page }, testInfo) => {
      const { diagnostics } = await installEmptyInventory(page, timing);
      const readiness = require("./release-readiness");
      try {
        if (operation === "initial") {
          await readiness.waitForSelectedScope(page);
        } else {
          await readiness.refreshSelectedScope(page, () => page.locator("#refresh-button").click(), {
            systemId: "synthetic-system", force: true,
          });
          expect(diagnostics.inventory).toHaveLength(1);
          expect(new URL(diagnostics.inventory[0]).searchParams.has("enclosure_id")).toBe(false);
        }
        await paintCheckpoint(page);
        await expect(page.locator("#enclosure-select")).toHaveValue("");
        await expect(page.locator("#slot-grid")).toHaveAttribute("aria-busy", "false");
        expect(await page.locator("#slot-grid").evaluate(grid => grid.inert)).toBe(false);
        expect(diagnostics.errors).toEqual([]);
        expect(diagnostics.warnings).toEqual([]);
        expect(diagnostics.unexpected).toEqual([]);
      } finally {
        await testInfo.attach("synthetic-diagnostics", { body: JSON.stringify(diagnostics), contentType: "application/json" });
      }
    });
  }
}

// A settled live selector must not accept a leftover saved-view URL key, even
// when its value is empty. Mutate only browser location after real app settlement.
for (const timing of [false, true]) {
  for (const value of ["stale", ""]) {
    test(`live enclosure: timing ${timing}: rejects storage_view_id=${JSON.stringify(value)}`, async ({ page }, testInfo) => {
      const { diagnostics } = await installFixture(page, html, { timing });
      await switching.context.gotoApp(page);
      await switching.context.setAutoRefresh(page, false);
      const readiness = require("./release-readiness");
      const scope = { systemId: "synthetic-system", enclosureValue: "enclosure:enc-a", timeout: negativeTimeout };
      try {
        await readiness.waitForSelectedScope(page, scope);
        expect(new URL(page.url()).searchParams.get("enclosure_id")).toBe("enc-a");
        expect(new URL(page.url()).searchParams.has("storage_view_id")).toBe(false);
        await page.evaluate(value => {
          const url = new URL(location.href);
          url.searchParams.set("storage_view_id", value);
          history.replaceState({}, "", url);
        }, value);
        await expect(readiness.waitForSelectedScope(page, scope)).rejects.toThrow(predicateTimeout);
        await page.evaluate(() => {
          const url = new URL(location.href);
          url.searchParams.delete("storage_view_id");
          history.replaceState({}, "", url);
        });
        await readiness.waitForSelectedScope(page, scope);
        expect(diagnostics.errors).toEqual([]);
        expect(diagnostics.warnings).toEqual([]);
        expect(diagnostics.unexpected).toEqual([]);
      } finally {
        await testInfo.attach("synthetic-diagnostics", { body: JSON.stringify(diagnostics), contentType: "application/json" });
      }
    });
  }

  test(`live enclosure: timing ${timing}: wrong enclosure URL remains rejected`, async ({ page }) => {
    await installFixture(page, html, { timing });
    await switching.context.gotoApp(page);
    await switching.context.setAutoRefresh(page, false);
    await page.evaluate(() => {
      const url = new URL(location.href);
      url.searchParams.set("enclosure_id", "enc-b");
      history.replaceState({}, "", url);
    });
    await expect(require("./release-readiness").waitForSelectedScope(page, {
      systemId: "synthetic-system", enclosureValue: "enclosure:enc-a", timeout: negativeTimeout,
    })).rejects.toThrow(predicateTimeout);
  });

  test(`saved view: timing ${timing}: backing enclosure URL and return to live remain supported`, async ({ page }, testInfo) => {
    const { diagnostics } = await installFixture(page, html, { timing });
    const readiness = require("./release-readiness");
    await switching.context.gotoApp(page);
    await switching.context.setAutoRefresh(page, false);
    try {
      const inventoryCount = diagnostics.inventory.length;
      await readiness.switchSelectedScope(page, "#enclosure-select", "view:saved-chassis");
      await readiness.waitForSelectedScope(page, {
        enclosureValue: "view:saved-chassis", backingEnclosureId: "enc-a", timeout: negativeTimeout,
      });
      expect(diagnostics.inventory).toHaveLength(inventoryCount);
      const params = new URL(page.url()).searchParams;
      expect(params.get("system_id")).toBe("synthetic-system");
      expect(params.get("storage_view_id")).toBe("saved-chassis");
      // buildSelectionParams retains currentLiveEnclosureId for this saved view.
      expect(params.get("enclosure_id")).toBe("enc-a");
      await page.evaluate(() => {
        const url = new URL(location.href);
        url.searchParams.set("storage_view_id", "stale");
        history.replaceState({}, "", url);
      });
      await expect(readiness.waitForSelectedScope(page, {
        enclosureValue: "view:saved-chassis", backingEnclosureId: "enc-a", timeout: negativeTimeout,
      })).rejects.toThrow(predicateTimeout);
      await page.evaluate(() => {
        const url = new URL(location.href);
        url.searchParams.set("storage_view_id", "saved-chassis");
        url.searchParams.set("enclosure_id", "enc-b");
        history.replaceState({}, "", url);
      });
      await expect(readiness.waitForSelectedScope(page, {
        enclosureValue: "view:saved-chassis", backingEnclosureId: "enc-a", timeout: negativeTimeout,
      })).rejects.toThrow(predicateTimeout);
      await page.evaluate(() => {
        const url = new URL(location.href);
        url.searchParams.set("enclosure_id", "enc-a");
        history.replaceState({}, "", url);
      });
      await readiness.switchSelectedScope(page, "#enclosure-select", "enclosure:enc-a");
      expect(new URL(page.url()).searchParams.get("enclosure_id")).toBe("enc-a");
      expect(new URL(page.url()).searchParams.has("storage_view_id")).toBe(false);
      expect(diagnostics.errors).toEqual([]);
      expect(diagnostics.warnings).toEqual([]);
      expect(diagnostics.unexpected).toEqual([]);
    } finally {
      await testInfo.attach("synthetic-diagnostics", { body: JSON.stringify(diagnostics), contentType: "application/json" });
    }
  });

  test(`helper timeout: timing ${timing}: explicit budget reaches final runtime settlement`, async ({ page }) => {
    const { controls } = await installFixture(page, html, { timing });
    await switching.context.gotoApp(page);
    await switching.context.setAutoRefresh(page, false);
    const runtime = gate();
    let started = false;
    controls.holdRuntime = async () => { started = true; await runtime.promise; };
    const readiness = require("./release-readiness");
    const listeners = page.listenerCount("request");
    try {
      await expect(readiness.refreshSelectedScope(page, () => page.locator("#refresh-button").click(), {
        systemId: "synthetic-system", enclosureId: "enc-a", force: true, timeout: negativeTimeout,
      })).rejects.toThrow(predicateTimeout);
      expect(started).toBe(true);
      expect(page.listenerCount("request")).toBe(listeners);
    } finally {
      runtime.release();
      await readiness.waitForSelectedScope(page, { timeout: negativeTimeout });
    }
  });
}

for (const key of ["enclosure_id", "storage_view_id"]) {
  test(`empty enclosure rejects empty-valued ${key} location`, async ({ page }) => {
    await installEmptyInventory(page, false);
    await page.evaluate(key => history.replaceState({}, "", `?system_id=synthetic-system&${key}=`), key);
    await expect(require("./release-readiness").waitForSelectedScope(page, {
      timeout: negativeTimeout,
    })).rejects.toThrow(predicateTimeout);
  });
}

for (const selector of ["system-select", "enclosure-select"]) {
  test(`empty enclosure rejects missing ${selector}`, async ({ page }) => {
    await installEmptyInventory(page, false);
    await page.locator(`#${selector}`).evaluate(node => node.remove());
    await expect(require("./release-readiness").waitForSelectedScope(page, {
      systemId: "synthetic-system", enclosureValue: "", timeout: negativeTimeout,
    })).rejects.toThrow(predicateTimeout);
  });
}

for (const key of ["enclosure_id", "storage_view_id"]) {
  test(`empty enclosure rejects stale ${key} location`, async ({ page }) => {
    await installEmptyInventory(page, false);
    await page.evaluate(key => history.replaceState({}, "", `?system_id=synthetic-system&${key}=stale`), key);
    await expect(require("./release-readiness").waitForSelectedScope(page, {
      timeout: negativeTimeout,
    })).rejects.toThrow(predicateTimeout);
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
      systemId: "synthetic-system", enclosureValue: "enclosure:enc-a", timeout: negativeTimeout,
    })).rejects.toThrow(predicateTimeout);
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
    await expect(require("./release-readiness").waitForSelectedScope(page, {
      timeout: negativeTimeout,
    })).rejects.toThrow(predicateTimeout);
  } finally {
    inventory.release();
    await require("./release-readiness").waitForSelectedScope(page);
  }
});

test("F1: an action without a new request times out instead of borrowing settled state", async ({ page }) => {
  await installFixture(page, html);
  await switching.context.gotoApp(page);
  await expect(require("./release-readiness").refreshSelectedScope(page, async () => {}, {
    systemId: "synthetic-system", enclosureId: "enc-a", force: true, timeout: negativeTimeout,
  })).rejects.toThrow(/page\.waitForResponse: Timeout 1500ms exceeded while waiting for event "response"/);
});

// Real application refreshes share one global token, not a scope/force token.
// Hold only HTTP responses. Never replace refreshSnapshot or mutate its token.
test.describe("F2: global inventory supersession", () => {
  const readiness = require("./release-readiness");
  const scope = { systemId: "synthetic-system", enclosureId: "enc-a", force: true, timeout: 3000 };
  const markers = ["SANITIZED-OWNED-A", "SANITIZED-LATER-B", "SANITIZED-LATER-C"];
  const click = page => page.locator("#refresh-button").click();

  async function setup(page, timing) {
    const fixture = await installFixture(page, html, { timing });
    await switching.context.gotoApp(page);
    await switching.context.setAutoRefresh(page, false);
    const snapshot = await page.evaluate(() => structuredClone(window.APP_BOOTSTRAP.snapshot));
    await page.evaluate(() => {
      window.__refreshObservations = { consumed: [], renders: [] };
      // Observation only: native fetch and JSON decoding still run unchanged.
      const nativeFetch = window.fetch;
      window.fetch = async function(...args) {
        const response = await nativeFetch.apply(this, args);
        if (new URL(response.url).pathname === "/api/inventory") {
          const nativeJson = response.json.bind(response);
          response.json = async function() {
            const value = await nativeJson();
            if (value.slots?.[0]) window.__refreshObservations.consumed.push(value.slots[0].device_name);
            return value;
          };
        }
        return response;
      };
      new MutationObserver(() => {
        window.__refreshObservations.renders.push(document.getElementById("slot-grid").textContent);
      }).observe(document.getElementById("slot-grid"), { subtree: true, childList: true, characterData: true });
    });
    const requests = [], gates = [gate(), gate(), gate()];
    await page.route("**/api/inventory?**", async route => {
      const url = new URL(route.request().url());
      const index = requests.length;
      requests.push({ url: url.href, force: url.searchParams.get("force"), system: url.searchParams.get("system_id") });
      await gates[index].promise;
      const payload = structuredClone(snapshot);
      payload.selected_system_id = url.searchParams.get("system_id");
      payload.selected_enclosure_id = url.searchParams.get("enclosure_id") || "enc-a";
      payload.slots.forEach(slot => {
        slot.serial = markers[index];
        slot.device_name = markers[index];
        slot.enclosure_id = payload.selected_enclosure_id;
      });
      await route.fulfill({ json: payload });
    });
    return { ...fixture, requests, gates };
  }

  async function nonforcedSameScope(page) {
    // Saved view is local; returning to its backing live enclosure dispatches B.
    await page.locator("#enclosure-select").selectOption("view:saved-chassis");
    await page.locator("#enclosure-select").selectOption("enclosure:enc-a");
  }

  function start(page, action, options = scope) {
    const outcome = { status: "pending" };
    const pending = readiness.refreshSelectedScope(page, action, options).then(
      () => { outcome.status = "resolved"; },
      error => { outcome.status = "rejected"; outcome.error = error.message; });
    return { pending, outcome };
  }
  const consumed = (page, marker) => expect.poll(() => page.evaluate(
    marker => window.__refreshObservations.consumed.includes(marker), marker)).toBe(true);
  const rendered = (page, marker) => expect(page.locator("#slot-grid")).toContainText(marker);
  async function observations(page, fixture, operation, info) {
    const browser = await page.evaluate(() => window.__refreshObservations);
    await info.attach("refresh-ownership", { body: JSON.stringify({
      browser, requests: fixture.requests, outcome: operation.outcome, diagnostics: fixture.diagnostics,
    }), contentType: "application/json" });
    expect(fixture.diagnostics.errors).toEqual([]);
    expect(fixture.diagnostics.warnings).toEqual([]);
    expect(fixture.diagnostics.unexpected).toEqual([]);
    return browser;
  }

  for (const timing of [false, true]) {
    for (const order of ["B-first", "B-last"]) {
      test(`timing ${timing}: non-forced same-scope ${order} rejects discarded A`, async ({ page }, info) => {
        const fixture = await setup(page, timing), listeners = page.listenerCount("request");
        const operation = start(page, () => click(page));
        try {
          await expect.poll(() => fixture.requests.length).toBe(1);
          await nonforcedSameScope(page);
          await expect.poll(() => fixture.requests.length).toBe(2);
          expect(fixture.requests.map(request => request.force)).toEqual(["true", "false"]);
          const first = order === "B-first" ? 1 : 0;
          fixture.gates[first].release();
          await consumed(page, markers[first]);
          if (first === 1) await rendered(page, markers[1]);
          await paintCheckpoint(page);
          expect(operation.outcome.status).toBe("pending");
          expect(await page.locator("#slot-grid").textContent()).not.toContain(markers[0]);
          fixture.gates[1 - first].release();
          await operation.pending;
          await consumed(page, markers[0]);
          await rendered(page, markers[1]);
          await paintCheckpoint(page);
          const browser = await observations(page, fixture, operation, info);
          expect(browser.renders.some(text => text.includes(markers[0]))).toBe(false);
          expect(page.listenerCount("request")).toBe(listeners);
          expect(operation.outcome.status, "discarded owned refresh must not report success").toBe("rejected");
          expect(operation.outcome.error).toContain("readiness request was superseded");
        } finally {
          fixture.gates.forEach(gate => gate.release());
          await operation.pending;
        }
      });
    }

    for (const kind of ["system", "enclosure"]) {
      test(`timing ${timing}: ${kind} away-and-back cannot borrow C readiness`, async ({ page }, info) => {
        const fixture = await setup(page, timing), listeners = page.listenerCount("request");
        const operation = start(page, () => click(page));
        try {
          await expect.poll(() => fixture.requests.length).toBe(1);
          const selector = page.locator(kind === "system" ? "#system-select" : "#enclosure-select");
          await selector.selectOption(kind === "system" ? "synthetic-second" : "enclosure:enc-b");
          await expect.poll(() => fixture.requests.length).toBe(2);
          await selector.selectOption(kind === "system" ? "synthetic-system" : "enclosure:enc-a");
          await expect.poll(() => fixture.requests.length).toBe(3);
          fixture.gates[2].release();
          await rendered(page, markers[2]);
          fixture.gates[1].release(); fixture.gates[0].release();
          await operation.pending;
          await consumed(page, markers[0]); await consumed(page, markers[1]);
          await paintCheckpoint(page);
          const browser = await observations(page, fixture, operation, info);
          expect(browser.renders.some(text => text.includes(markers[0]) || text.includes(markers[1]))).toBe(false);
          expect(page.listenerCount("request")).toBe(listeners);
          expect(operation.outcome.status, "scope roundtrip must not restore discarded ownership").toBe("rejected");
          expect(operation.outcome.error).toContain("readiness request was superseded");
        } finally {
          fixture.gates.forEach(gate => gate.release());
          await operation.pending;
        }
      });
    }

    test(`timing ${timing}: wrong-force dispatch before owned A is not selected`, async ({ page }, info) => {
      const fixture = await setup(page, timing), listeners = page.listenerCount("request");
      const operation = start(page, async () => {
        await nonforcedSameScope(page);
        await expect.poll(() => fixture.requests.length).toBe(1);
        await click(page);
      });
      try {
        await expect.poll(() => fixture.requests.length).toBe(2);
        expect(fixture.requests.map(request => request.force)).toEqual(["false", "true"]);
        fixture.gates[0].release();
        await consumed(page, markers[0]); await paintCheckpoint(page);
        expect(operation.outcome.status).toBe("pending");
        fixture.gates[1].release(); await operation.pending;
        await rendered(page, markers[1]);
        await observations(page, fixture, operation, info);
        expect(operation.outcome.status).toBe("resolved");
        expect(page.listenerCount("request")).toBe(listeners);
      } finally {
        fixture.gates.forEach(gate => gate.release());
        await operation.pending;
      }
    });

    for (const traffic of ["none", "POST", "non-inventory", "foreign-origin"]) {
      test(`timing ${timing}: A succeeds with ${traffic} unrelated traffic`, async ({ page }, info) => {
        const fixture = await setup(page, timing), listeners = page.listenerCount("request");
        const operation = start(page, () => click(page));
        try {
          await expect.poll(() => fixture.requests.length).toBe(1);
          if (traffic !== "none") {
            const url = traffic === "foreign-origin"
              ? "https://foreign.example.test/api/inventory?system_id=synthetic-system&enclosure_id=enc-a&force=true"
              : traffic === "non-inventory" ? `${origin}/api/release-status`
                : `${origin}/api/inventory?system_id=synthetic-system&enclosure_id=enc-a&force=true`;
            const method = traffic === "POST" ? "POST" : "GET";
            let observed = 0;
            // Explicitly fulfilled synthetic traffic, never a production refresh.
            await page.route(url, async route => {
              expect(route.request().method()).toBe(method);
              observed += 1;
              await route.fulfill({ json: { ok: true }, headers: { "access-control-allow-origin": "*" } });
            });
            await page.evaluate(async ({ url, method }) => {
              const response = await fetch(url, { method });
              if (!response.ok) throw new Error("Synthetic unrelated request failed");
              await response.json();
            }, { url, method });
            expect(observed).toBe(1);
          }
          fixture.gates[0].release(); await operation.pending;
          await rendered(page, markers[0]);
          await observations(page, fixture, operation, info);
          expect(operation.outcome.status).toBe("resolved");
          expect(page.listenerCount("request")).toBe(listeners);
        } finally {
          fixture.gates.forEach(gate => gate.release());
          await operation.pending;
        }
      });
    }
  }

  for (const kind of ["rejected-action", "no-request", "wrong-force-only"]) {
    test(`${kind} retains exact failure and restores request listener`, async ({ page }, info) => {
      const fixture = await setup(page, false), listeners = page.listenerCount("request");
      fixture.gates.forEach(gate => gate.release());
      const action = kind === "rejected-action" ? async () => { throw new Error("synthetic action refusal"); }
        : kind === "no-request" ? async () => {} : () => nonforcedSameScope(page);
      const operation = start(page, action, { ...scope, timeout: negativeTimeout });
      await operation.pending;
      await observations(page, fixture, operation, info);
      expect(operation.outcome.status).toBe("rejected");
      expect(operation.outcome.error).toMatch(kind === "rejected-action" ? /^synthetic action refusal$/
        : /page\.waitForResponse: Timeout 1500ms exceeded while waiting for event "response"/);
      expect(page.listenerCount("request")).toBe(listeners);
    });
  }
});
