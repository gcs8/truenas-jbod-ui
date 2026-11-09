"use strict";

// Round-5 regressions for the UI coverage crawler (#1108, owner decision
// D-TNJ-20261108-CRAWLER-CODEX3): the three Codex P2s at 37a5312d.
//   1. Loading an existing system with verify_ssl and a saved CA in the admin
//      form re-checks the bundle (GET /api/admin/tls/saved-status); the admin
//      states that load a system declare exactly that request.
//   2. A request that matches an exhausted page-load rule falls through to the
//      running state's declared requests and the follow-up window instead of
//      being refused outright; every budget and binding still holds.
//   3. fabric.diagnostics switches to Disk Path before it checks that the
//      diagnostic evidence is shown, and the CI fixture renders it.
// No browser, no network.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const ORIGIN = "http://ui.example.test";
const ADMIN = "http://admin.example.test";

function inventoryStates() {
  const { loadInventory, expandStates } = req("qa/ui-crawl/inventory");
  const inventory = loadInventory();
  return { inventory, states: expandStates(inventory), byId: new Map(inventory.states.map((state) => [state.id, state])) };
}

// ------------------------------------- 1. admin saved-certificate check ----

const SAVED_STATUS = "/api/admin/tls/saved-status";

function loadsExistingSystem(state) {
  return (state.steps || []).some((step) => step.click === "[data-existing-system-id]" || step.clickIfPresent === "[data-existing-system-id]");
}

test("Codex P2 1: every admin state that loads an existing system declares the saved-certificate check, bounded, and no other state does", () => {
  const { inventory } = inventoryStates();
  const { validateInventory } = req("qa/ui-crawl/inventory");
  assert.deepEqual(validateInventory(inventory), []);
  const admin = inventory.states.filter((state) => state.page === "admin");
  const loaders = admin.filter(loadsExistingSystem);
  assert.ok(loaders.length >= 6, `admin states that load a system: ${loaders.map((s) => s.id).join(", ")}`);
  for (const state of loaders) {
    assert.equal(state.appliance_load, true, `${state.id} is appliance_load (run only with --appliance-load)`);
    const rules = (state.read_requests || []).filter((rule) => rule.path === SAVED_STATUS);
    assert.equal(rules.length, 1, `${state.id} declares ${SAVED_STATUS} once`);
    const [rule] = rules;
    assert.equal(rule.method, "GET");
    assert.deepEqual(rule.query, ["system_id"], `${state.id}: exactly the query key admin.js sends`);
    assert.equal(rule.max, 1, `${state.id}: one load of a system, one check`);
    assert.equal(rule.force, undefined);
  }
  for (const state of inventory.states.filter((s) => !loadsExistingSystem(s))) {
    assert.equal((state.read_requests || []).some((rule) => rule.path === SAVED_STATUS), false, `${state.id} loads no system and must not declare ${SAVED_STATUS}`);
  }
  // The manifest still classes it as an appliance load, and admin.js still sends only system_id.
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "qa/ui-routes.json"), "utf8"));
  const entry = manifest.routes.find((route) => route.service === "admin" && route.path === SAVED_STATUS);
  assert.equal(entry.class, "appliance-load");
  const adminJs = fs.readFileSync(path.join(root, "admin_service/static/admin.js"), "utf8");
  const sends = [...adminJs.matchAll(/\/api\/admin\/tls\/saved-status\?([^`"']*)/g)].map((m) => m[1]);
  assert.deepEqual(sends, ["system_id=${encodeURIComponent(system.id)}"]);
});

test("Codex P2 1: the guard admits one saved-certificate check per loading state, with only system_id, and refuses it elsewhere", async () => {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { runState } = req("qa/ui-crawl/actuation");
  const { readsFor } = req("qa/ui-crawl/inventory");
  const { inventory, byId } = inventoryStates();
  const url = `${ADMIN}${SAVED_STATUS}?system_id=synthetic-system`;
  const g = new RequestGuard({ origins: { admin: ADMIN } });
  const state = byId.get("admin.system-form");
  g.beginState(state.id, { reads: readsFor(inventory, state) });
  const seen = [];
  const driver = {
    guard: g, vars: {}, bind: {}, service: "admin",
    runSteps: async () => {
      seen.push(g.admit({ method: "GET", url }));
      seen.push(g.admit({ method: "GET", url }));
      seen.push(g.admit({ method: "GET", url: `${url}&force=true` }));
      seen.push(g.admit({ method: "GET", url: `${url}&host=other.example.test` }));
    },
  };
  const outcome = await runState(driver, state, {});
  assert.equal(outcome.status, "done");
  assert.equal(seen[0].allowed, true, seen[0].reason);
  assert.equal(seen[0].cls, "appliance-load");
  assert.equal(seen[1].allowed, false, "a second check in the same state is over budget");
  assert.equal(seen[2].allowed, false, "an undeclared force key");
  assert.equal(seen[3].allowed, false, "an undeclared query key");
  // A state that loads no system has no window for it.
  g.endState();
  const operations = byId.get("admin.operations");
  g.beginState(operations.id, { reads: readsFor(inventory, operations) });
  assert.equal(g.admit({ method: "GET", url }).allowed, false);
  // Bound live: another system than the target's is refused.
  const bound = new RequestGuard({ origins: { admin: ADMIN } });
  bound.beginState(state.id, { reads: readsFor(inventory, state) });
  let other;
  await runState({ ...driver, guard: bound, bind: { system_id: "synthetic-system" }, runSteps: async () => { other = bound.admit({ method: "GET", url: `${ADMIN}${SAVED_STATUS}?system_id=another-system` }); } }, state, {});
  assert.equal(other.allowed, false);
});

test("Codex P2 1: live crawls admit the check only with --appliance-load; the CI fixture loads a system with verify_ssl and a saved CA", () => {
  const cli = req("scripts/ui_crawl.js");
  const { states } = inventoryStates();
  const target = { page: "admin", ready: "admin", scopeIndex: 0, label: "admin", url: `${ADMIN}/` };
  const ids = (args) => cli.statesFor(target, states, { allow: [], ...args }, {}).map((state) => state.id);
  assert.equal(ids({}).includes("admin.system-form"), false, "not without --appliance-load");
  assert.equal(ids({ applianceLoad: true }).includes("admin.system-form"), true);
  const builder = fs.readFileSync(path.join(root, "scripts/build_ui_crawl_fixture.py"), "utf8");
  const bootstrap = /ADMIN_BOOTSTRAP = \{[\s\S]*?\n\}\n/.exec(builder)[0];
  assert.match(bootstrap, /"verify_ssl": True/);
  assert.match(bootstrap, /"tls_ca_bundle_path": "[^"]+"/);
  assert.match(builder, /"admin:\/api\/admin\/tls\/saved-status":/);
  // The browser spec requires the check to have been sent (and admitted) in admin.system-form.
  const spec = fs.readFileSync(path.join(root, "qa/ui-coverage.spec.js"), "utf8");
  assert.match(spec, /appliance-load GET \/api\/admin\/tls\/saved-status/);
});

// ----------------------- 2. exhausted page-load rule falls through to state --

const QS_BIND = { system_id: "sys-a", enclosure_id: "enc-1" };
const forced = (route, extra = "") => `${ORIGIN}${route}?system_id=sys-a&enclosure_id=enc-1&force=true${extra}`;

function quantastorGuard(stateId, { now = () => 0 } = {}) {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { readsFor, loadRequestsFor } = req("qa/ui-crawl/inventory");
  const { inventory, byId } = inventoryStates();
  const g = new RequestGuard({ origins: { ui: ORIGIN }, now });
  const state = byId.get(stateId);
  g.beginState(stateId, { reads: readsFor(inventory, state) });
  const rules = loadRequestsFor(inventory, "main", "quantastor");
  assert.ok(rules.length >= 2, "QuantaStor main page declares its startup refresh");
  g.openLoad(stateId, rules, { bind: QS_BIND, service: "ui" });
  // The startup refresh spends the page-load allowance.
  const startup = [g.admit({ method: "GET", url: forced("/api/inventory") }), g.admit({ method: "GET", url: forced("/api/storage-views") })];
  for (const entry of startup) {
    assert.equal(entry.allowed, true, entry.reason);
    assert.match(entry.reason, /page-load/);
  }
  return { g, state, inventory };
}

test("Codex P2 2: after the QuantaStor startup refresh spends the load allowance, main.refresh-now's declared Refresh is admitted by the state", async () => {
  const { runState } = req("qa/ui-crawl/actuation");
  const { g, state } = quantastorGuard("main.refresh-now");
  const seen = [];
  const driver = {
    guard: g, vars: {}, bind: QS_BIND, service: "ui",
    runSteps: async () => {
      // Refresh: forced inventory, then forced storage views, up to the declared max of 2 each.
      for (let i = 0; i < 2; i += 1) {
        seen.push(g.admit({ method: "GET", url: forced("/api/inventory") }));
        seen.push(g.admit({ method: "GET", url: forced("/api/storage-views") }));
      }
      // An extra forced read beyond the declared allowance.
      seen.push(g.admit({ method: "GET", url: forced("/api/inventory") }));
    },
  };
  const outcome = await runState(driver, state, {});
  assert.equal(outcome.status, "done");
  for (const entry of seen.slice(0, 4)) {
    assert.equal(entry.allowed, true, entry.reason);
    assert.match(entry.reason, /declared appliance-load for main\.refresh-now/);
  }
  const extra = seen[4];
  assert.equal(extra.allowed, false, "beyond both the page-load and the state's budget");
  assert.match(extra.reason, /budget exhausted/);
  assert.equal(outcome.admitted.length, 4, "the state's window counts only its own four");
  assert.equal(g.load.admitted.length, 2, "the load window counts only the startup refresh");
});

test("Codex P2 2: the fall-through keeps every other bound: no declaring state, another target, an undeclared route, a write", async () => {
  const { runState } = req("qa/ui-crawl/actuation");
  // A state that declares nothing: the exhausted load rule is still a refusal.
  let { g } = quantastorGuard("main.default");
  const plain = g.admit({ method: "GET", url: forced("/api/inventory") });
  assert.equal(plain.allowed, false);
  assert.match(plain.reason, /budget exhausted/);
  // Binding: an exhausted load rule never opens another target's request, even with a declaring state.
  ({ g } = quantastorGuard("main.refresh-now"));
  const { byId } = inventoryStates();
  const seen = [];
  await runState({
    guard: g, vars: {}, bind: QS_BIND, service: "ui",
    runSteps: async () => {
      seen.push(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=sys-b&enclosure_id=enc-1&force=true` }));
      seen.push(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=sys-a&enclosure_id=enc-2&force=true` }));
      seen.push(g.admit({ method: "GET", url: forced("/api/inventory", "&unexpected=1") }));
      seen.push(g.admit({ method: "POST", url: `${ORIGIN}/api/slots/5/led?system_id=sys-a&enclosure_id=enc-1`, body: '{"action":"IDENTIFY"}' }));
    },
  }, byId.get("main.refresh-now"), {});
  assert.deepEqual(seen.map((entry) => entry.allowed), [false, false, false, false], JSON.stringify(seen.map((e) => e.reason)));
  assert.match(seen[0].reason, /bound system_id/);
  assert.match(seen[1].reason, /bound enclosure_id/);
});

test("Codex P2 2: a delayed follow-up that matches an exhausted load rule is admitted by the declared follow-up window, within its budget and time", async () => {
  const { runState } = req("qa/ui-crawl/actuation");
  const box = { clock: 0 };
  const { g, state } = quantastorGuard("main.refresh-now", { now: () => box.clock });
  // Refresh sends one forced inventory read; the storage-view follow-up comes after the steps.
  await runState({
    guard: g, vars: {}, bind: QS_BIND, service: "ui",
    runSteps: async () => { assert.equal(g.admit({ method: "GET", url: forced("/api/inventory") }).allowed, true); },
  }, state, {});
  assert.ok(g.load, "the page-load window is still open after the steps");
  assert.ok(g.followUp, "the declared follow-up window is open");
  box.clock = 1000;
  const late = [1, 2, 3].map(() => g.admit({ method: "GET", url: forced("/api/storage-views") }));
  assert.equal(late[0].allowed, true, late[0].reason);
  assert.match(late[0].reason, /follow-up/);
  assert.equal(late[1].allowed, true, late[1].reason);
  assert.equal(late[2].allowed, false, "only the two declared storage-view reads");
  // Expired: refused even though the follow-up route matches.
  box.clock = state.follow_up_ms + 1;
  assert.equal(g.admit({ method: "GET", url: forced("/api/inventory") }).allowed, false);
});

// ---------------------------------- 3. fabric.diagnostics on Disk Path ----

const DIAGNOSTIC_KEYS = ["[data-fabric-evidence-key]", "[data-fabric-diagnostic-table-key]"];

test("Codex P2 3: fabric.diagnostics switches to Disk Path, then requires the evidence, and runs in CI", () => {
  const { inventory, byId } = inventoryStates();
  const state = byId.get("fabric.diagnostics");
  assert.equal(state.requires, undefined, "availability is checked after the mode switch, not before the steps");
  assert.equal(state.ci, true, "the CI fixture renders diagnostics");
  const kinds = state.steps.map((step) => Object.keys(step)[0]);
  const modeClick = state.steps.findIndex((step) => step.click === "#fabric-mode-disk");
  const required = state.steps.findIndex((step) => step.requireVisible === "[data-fabric-evidence-key]");
  const firstOpen = kinds.indexOf("openAll");
  assert.equal(modeClick, 0, "Disk Path first");
  assert.ok(required > modeClick, "evidence required after the switch");
  assert.ok(firstOpen > required, "panels opened only once the evidence is there");
  // No reviewed render gap for what the CI fixture now renders.
  const gaps = new Set((inventory.render_gaps || []).map((gap) => gap.key));
  for (const key of DIAGNOSTIC_KEYS) assert.equal(gaps.has(key), false, `${key} renders in CI now`);
  // The fixture builder attaches synthetic kernel diagnostics, decoded by the app's own parser.
  const builder = fs.readFileSync(path.join(root, "scripts/build_ui_crawl_fixture.py"), "utf8");
  assert.match(builder, /parse_mpr_dmesg_events/);
  assert.match(builder, /kernel_diagnostics/);
  // The browser spec requires the state to have run, not been skipped.
  const spec = fs.readFileSync(path.join(root, "qa/ui-coverage.spec.js"), "utf8");
  assert.match(spec, /fabric\.diagnostics/);
});

test("Codex P2 3: a missing-evidence fabric target still skips fabric.diagnostics as not applicable, after the switch", async () => {
  const { CrawlDriver, NotApplicable } = req("qa/ui-crawl/driver");
  const { runState } = req("qa/ui-crawl/actuation");
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { byId } = inventoryStates();
  const state = byId.get("fabric.diagnostics");
  // A minimal page double: evidence appears only after Disk Path is clicked.
  const run = async (hasData) => {
    const clicked = [];
    const visibleCount = (selector) => {
      if (selector === "[data-fabric-evidence-key]" || /data-fabric-evidence-key/.test(selector)) return hasData && clicked.includes("#fabric-mode-disk") ? 1 : 0;
      if (selector === "#fabric-mode-disk") return 1;
      return 0;
    };
    const locator = (selector) => {
      const self = {
        first: () => self, filter: () => self, nth: () => self,
        count: async () => visibleCount(selector),
        isVisible: async () => visibleCount(selector) > 0,
        isEnabled: async () => true,
        scrollIntoViewIfNeeded: async () => {},
        click: async () => { clicked.push(selector); },
        evaluateAll: async () => {},
        waitFor: async () => { if (!visibleCount(selector)) throw new Error(`timeout waiting for ${selector}`); },
      };
      return self;
    };
    const page = { locator, waitForTimeout: async () => {}, on: () => {}, off: () => {}, evaluate: async () => {}, url: () => `${ORIGIN}/sas-fabric` };
    const guard = new RequestGuard({ origins: { ui: ORIGIN } });
    const driver = new CrawlDriver({ page, guard, selections: {}, log: () => {} });
    driver.resetState();
    try {
      return { outcome: await runState(driver, state, {}), clicked };
    } catch (error) {
      return { error, clicked };
    }
  };
  const withData = await run(true);
  assert.equal(withData.error, undefined, withData.error && withData.error.message);
  assert.equal(withData.outcome.status, "done");
  assert.equal(withData.clicked[0], "#fabric-mode-disk");
  const without = await run(false);
  assert.ok(without.error instanceof NotApplicable, String(without.error));
  assert.deepEqual(without.clicked, ["#fabric-mode-disk"], "switched before deciding");
});
