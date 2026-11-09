"use strict";

// Round-10 regressions for the UI coverage crawler (#1108, owner decision
// D-TNJ-20261109-CRAWLER-SVBIND; DayBreak-Blue round-9 BLOCK): a null target
// binding means the request may not carry that key, never "any value".
//
//   - A storage-view target binds the enclosure its view is bound to: the
//     view's backing_enclosure_id from the target's own storage-view runtime
//     (the backend builds it from the snapshot the page loads, and app.js
//     sends it as the page's live enclosure and as a snapshot view's history
//     enclosure). There, enclosure_id may be absent or that enclosure; with no
//     bound enclosure any enclosure_id is refused.
//   - Every other key a target kind leaves null (system_id, enclosure_id,
//     storage_view_id and the {view_id} path segment it binds) refuses the key.
//   - A slot is never a target binding: reads cannot bind it, and writes
//     substitute the captured bay.
//   - undefined (no live scope) stays the fixture-run case only.
// No browser, no network.

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const UI = "http://ui.example.test";
const ADMIN = "http://admin.example.test";
const HISTORY = "http://history.example.test";
const HEAT = "window_hours=24&event_limit=0&metric_limit=24&metrics=temperature_c";

function inventoryStates() {
  const { loadInventory, expandStates } = req("qa/ui-crawl/inventory");
  const inventory = loadInventory();
  const states = expandStates(inventory);
  return { inventory, states, byId: new Map(states.map((state) => [state.id, state])) };
}

function stateGuard(stateId, scope, bind) {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { readsFor } = req("qa/ui-crawl/inventory");
  const { inventory, byId } = inventoryStates();
  const state = byId.get(stateId);
  assert.ok(state, `no state ${stateId}`);
  const g = new RequestGuard({ origins: { ui: UI, admin: ADMIN, history: HISTORY } });
  g.beginState(stateId, { reads: readsFor(inventory, state, scope), bind });
  return g;
}

// The bind a live crawl gives a target (the real Crawler.bindFor).
function liveBind(target) {
  const { Crawler } = req("qa/ui-crawl/crawler");
  return Crawler.prototype.bindFor.call({ fixtureMode: false, writeSystem: null }, target);
}

const viewTarget = (viewEnclosure) => ({ page: "main", system: "sys-a", url: `${UI}/?system_id=sys-a&storage_view_id=view-a`, ...(viewEnclosure === undefined ? {} : { viewEnclosure }) });
const get = (g, url) => g.admit({ method: "GET", url });

// --------------------------------------------- DayBreak-Blue's exact probe --

test("DayBreak #1108 r9: a storage-view target whose view has no bound enclosure refuses a foreign enclosure_id on its history and SMART reads", () => {
  const bind = liveBind(viewTarget(null));
  for (const stateId of ["main.default", "main.heatmap.metric", "main.history"]) {
    const g = stateGuard(stateId, "storage-view", bind);
    const refused = {
      "view history": `${UI}/api/storage-views/view-a/history?system_id=sys-a&enclosure_id=enc-foreign&${HEAT}`,
      "view slot SMART": `${UI}/api/storage-views/view-a/slots/999/smart?system_id=sys-a&enclosure_id=enc-foreign`,
      "view slot history": `${UI}/api/storage-views/view-a/slots/3/history?system_id=sys-a&enclosure_id=enc-foreign&window_hours=24`,
      "an empty enclosure_id": `${UI}/api/storage-views/view-a/slots/0/smart?system_id=sys-a&enclosure_id=`,
    };
    for (const [why, url] of Object.entries(refused)) {
      const verdict = get(g, url);
      assert.equal(verdict.allowed, false, `${stateId}: ${why} must be refused (${verdict.reason})`);
    }
    // The view's own reads, without an enclosure, still pass.
    assert.equal(get(g, `${UI}/api/storage-views/view-a/history?system_id=sys-a&${HEAT}`).allowed, true, stateId);
    assert.equal(get(g, `${UI}/api/storage-views/view-a/slots/0/smart?system_id=sys-a`).allowed, true, stateId);
  }
});

test("SVBIND: a storage-view target bound to its view's enclosure admits that enclosure or none, and refuses every other", () => {
  const bind = liveBind(viewTarget("enc-9"));
  const g = stateGuard("main.default", "storage-view", bind);
  const routes = [
    (q) => `${UI}/api/storage-views/view-a/history?${q}&${HEAT}`,
    (q) => `${UI}/api/storage-views/view-a/slots/2/smart?${q}`,
    (q) => `${UI}/api/storage-views/view-a/slots/2/history?${q}&window_hours=24`,
    (q) => `${UI}/api/inventory?${q}`,
    (q) => `${UI}/api/slots/4/smart?${q}`,
    (q) => `${UI}/api/slots/4/history?${q}&window_hours=24`,
    (q) => `${UI}/api/history/scope?${q}&window_hours=24`,
  ];
  for (const url of routes) {
    for (const query of ["system_id=sys-a&enclosure_id=enc-9", "system_id=sys-a"]) {
      const ok = get(g, url(query));
      assert.equal(ok.allowed, true, `${url(query)}: ${ok.reason}`);
    }
    for (const query of ["system_id=sys-a&enclosure_id=enc-foreign", "system_id=sys-a&enclosure_id=", "system_id=sys-a&enclosure_id=enc-9&enclosure_id=enc-foreign",
      "system_id=sys-b&enclosure_id=enc-9", "enclosure_id=enc-9"]) {
      assert.equal(get(g, url(query)).allowed, false, `${url(query)} must be refused`);
    }
  }
  assert.equal(get(g, `${UI}/api/storage-views/view-b/slots/2/smart?system_id=sys-a&enclosure_id=enc-9`).allowed, false, "another view in the path");
});

// ------------------------------------------------- the target bind itself --

test("SVBIND: bindFor gives a storage-view target its view's enclosure (optional), and every live target a value or null for each key", () => {
  assert.deepEqual(liveBind(viewTarget(null)), { system_id: "sys-a", enclosure_id: null, storage_view_id: "view-a", optional: ["enclosure_id"] });
  assert.deepEqual(liveBind(viewTarget("enc-9")), { system_id: "sys-a", enclosure_id: "enc-9", storage_view_id: "view-a", optional: ["enclosure_id"] });
  assert.deepEqual(liveBind(viewTarget(undefined)), { system_id: "sys-a", enclosure_id: null, storage_view_id: "view-a", optional: ["enclosure_id"] }, "a view target the runner did not resolve binds no enclosure");
  // A URL enclosure_id on a view target does not widen it: the view decides.
  assert.deepEqual(liveBind({ ...viewTarget(null), url: `${UI}/?system_id=sys-a&storage_view_id=view-a&enclosure_id=enc-foreign` }).enclosure_id, null);
  assert.deepEqual(liveBind({ page: "main", system: "sys-a", url: `${UI}/?system_id=sys-a&enclosure_id=enc-1` }), { system_id: "sys-a", enclosure_id: "enc-1", storage_view_id: null });
  assert.deepEqual(liveBind({ page: "main", system: "sys-a", url: `${UI}/?system_id=sys-a` }), { system_id: "sys-a", enclosure_id: null, storage_view_id: null });
  assert.deepEqual(liveBind({ page: "admin", url: `${ADMIN}/` }), { system_id: null, enclosure_id: null, storage_view_id: null });
  const { Crawler } = req("qa/ui-crawl/crawler");
  assert.deepEqual(Crawler.prototype.bindFor.call({ fixtureMode: true }, viewTarget("enc-9")), {}, "fixture runs have no live scope");
});

test("SVBIND: the view's bound enclosure is its backing_enclosure_id from the target's own storage-view runtime", () => {
  const { viewBoundEnclosure } = req("qa/ui-crawl/crawler");
  assert.equal(viewBoundEnclosure({ id: "v", source: "selected_enclosure_snapshot", backing_enclosure_id: "enc-9" }), "enc-9");
  assert.equal(viewBoundEnclosure({ id: "v", source: "inventory_binding", backing_enclosure_id: "enc-9" }), "enc-9");
  assert.equal(viewBoundEnclosure({ id: "v", source: "inventory_binding", backing_enclosure_id: null }), null);
  assert.equal(viewBoundEnclosure({ id: "v", backing_enclosure_id: "" }), null);
  assert.equal(viewBoundEnclosure({ id: "v" }), null);
  assert.equal(viewBoundEnclosure(null), null);
});

test("SVBIND: liveTargets carries each storage view's bound enclosure into its target", async () => {
  const cli = req("scripts/ui_crawl.js");
  assert.equal(typeof cli.liveTargets, "function");
  const pages = {
    "/": `<select id="system-select"><option value="sys-a">A</option></select>`,
    "/api/inventory?system_id=sys-a": JSON.stringify({ snapshot: { selected_system_platform: "core", selected_enclosure_id: "enc-1", enclosures: [{ id: "enc-1" }, { id: "enc-2" }] } }),
    "/api/storage-views?system_id=sys-a": JSON.stringify({ views: [
      { id: "view-a", source: "inventory_binding", backing_enclosure_id: "enc-1" },
      { id: "view-b", source: "selected_enclosure_snapshot", backing_enclosure_id: "enc-1" },
      { id: "view-c", source: "inventory_binding", backing_enclosure_id: null },
      { id: "view-hidden", enabled: false, backing_enclosure_id: "enc-2" },
    ] }),
  };
  const realFetch = global.fetch;
  global.fetch = async (url) => {
    const parsed = new URL(url);
    const body = pages[`${parsed.pathname}${parsed.search}`];
    return new Response(body === undefined ? "not found" : body, { status: body === undefined ? 404 : 200 });
  };
  let targets;
  try {
    targets = await cli.liveTargets({ ui: UI, systems: null, history: null, admin: null });
  } finally {
    global.fetch = realFetch;
  }
  const main = targets.filter((target) => target.page === "main");
  const byView = Object.fromEntries(main.filter((target) => /storage_view_id=/.test(target.url)).map((target) => [new URL(target.url).searchParams.get("storage_view_id"), target]));
  assert.deepEqual(Object.keys(byView).sort(), ["view-a", "view-b", "view-c"]);
  assert.equal(byView["view-a"].viewEnclosure, "enc-1");
  assert.equal(byView["view-b"].viewEnclosure, "enc-1");
  assert.equal(byView["view-c"].viewEnclosure, null);
  for (const target of main.filter((t) => !/storage_view_id=/.test(t.url))) assert.equal("viewEnclosure" in target, false, target.label);
  assert.equal(liveBind(byView["view-c"]).enclosure_id, null);
  assert.equal(liveBind(byView["view-a"]).enclosure_id, "enc-1");
});

// ------------------------------------ every other key a target leaves null --

test("SVBIND: an enclosure target (storage_view_id null) refuses any storage_view_id a read carries", () => {
  const bind = liveBind({ page: "main", system: "sys-a", url: `${UI}/?system_id=sys-a&enclosure_id=enc-1` });
  const g = stateGuard("main.default", "enclosure", bind);
  assert.equal(get(g, `${UI}/api/inventory?system_id=sys-a&enclosure_id=enc-1`).allowed, true);
  for (const value of ["view-x", ""]) {
    assert.equal(get(g, `${UI}/api/inventory?system_id=sys-a&enclosure_id=enc-1&storage_view_id=${value}`).allowed, false, `storage_view_id=${value}`);
    assert.equal(get(g, `${UI}/api/storage-views?system_id=sys-a&enclosure_id=enc-1&storage_view_id=${value}`).allowed, false, `storage_view_id=${value}`);
  }
});

test("SVBIND: a target with no enclosure (enclosure_id null) refuses any enclosure_id; one with no system refuses any system_id", () => {
  const systemOnly = stateGuard("main.select[healthy]", "enclosure", liveBind({ page: "main", system: "sys-a", url: `${UI}/?system_id=sys-a` }));
  assert.equal(get(systemOnly, `${UI}/api/slots/4/smart?system_id=sys-a`).allowed, true);
  for (const query of ["system_id=sys-a&enclosure_id=any", "system_id=sys-a&enclosure_id=", "system_id=sys-b"]) {
    assert.equal(get(systemOnly, `${UI}/api/slots/4/smart?${query}`).allowed, false, query);
  }
  const fabric = stateGuard("fabric.lanes", "enclosure", liveBind({ page: "fabric", system: "sys-a", url: `${UI}/sas-fabric?system_id=sys-a` }));
  assert.equal(get(fabric, `${UI}/api/sas-fabric?system_id=sys-a`).allowed, true);
  assert.equal(get(fabric, `${UI}/api/sas-fabric?system_id=sys-a&enclosure_id=enc-foreign`).allowed, false);
  const noSystem = stateGuard("main.default", "enclosure", { system_id: null, enclosure_id: "enc-1", storage_view_id: null });
  assert.equal(get(noSystem, `${UI}/api/inventory?enclosure_id=enc-1`).allowed, true);
  for (const query of ["system_id=sys-a&enclosure_id=enc-1", "system_id=&enclosure_id=enc-1"]) {
    assert.equal(get(noSystem, `${UI}/api/inventory?${query}`).allowed, false, query);
  }
});

test("SVBIND: a storage-view path segment ({view_id}) is refused when the target binds no view", () => {
  const g = stateGuard("main.default", "storage-view", { system_id: "sys-a", enclosure_id: null, storage_view_id: null });
  assert.equal(get(g, `${UI}/api/storage-views/view-a/slots/0/smart?system_id=sys-a`).allowed, false);
  assert.equal(get(g, `${UI}/api/storage-views/view-a/history?system_id=sys-a&${HEAT}`).allowed, false);
});

test("SVBIND: a slot is never a target binding: a read rule cannot bind it, and a write rule must name the captured bay", () => {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const g = new RequestGuard({ origins: { ui: UI } });
  assert.throws(() => g.beginState("s", { reads: [{ method: "GET", path: "/api/slots/{slot}/smart", query: ["system_id", "slot"], bind: ["slot"] }] }), /cannot bind slot/);
  assert.throws(() => g.beginState("s", { reads: [{ method: "GET", path: "/api/slots/{slot}/smart", query: ["system_id"], bind_params: { slot: "slot" } }] }), /cannot bind path slot/);
  assert.throws(() => g.openWrites("s", [{ method: "POST", path: "/api/slots/{slot}/led", body_json: { action: "IDENTIFY" } }], [], { bind: { system_id: "sys-a", enclosure_id: "enc-1" } }), /not bound to one object/);
});

test("SVBIND: an `optional` entry can only relax enclosure_id; system_id stays required", () => {
  const g = stateGuard("main.default", "storage-view", { system_id: "sys-a", enclosure_id: null, storage_view_id: "view-a", optional: ["system_id", "enclosure_id"] });
  assert.equal(get(g, `${UI}/api/storage-views/view-a/slots/0/smart?system_id=sys-a`).allowed, true);
  assert.equal(get(g, `${UI}/api/storage-views/view-a/slots/0/smart`).allowed, false, "a read without system_id reads the backend's default system");
});

// ------------------------------------------ windows (load, follow-up, writes) --

test("SVBIND: bound windows apply the same rule: a null key may not be carried, a view's enclosure may be absent", () => {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const forcedInventory = [{ method: "GET", path: "/api/inventory", force: true, max: 5 }];
  const load = (bind) => {
    const g = new RequestGuard({ origins: { ui: UI, admin: ADMIN } });
    g.openLoad("main.default", forcedInventory, { bind });
    return g;
  };
  const inv = (query) => `${UI}/api/inventory?force=true&${query}`;
  // A view with no bound enclosure.
  let g = load(liveBind(viewTarget(null)));
  assert.equal(get(g, inv("system_id=sys-a")).allowed, true);
  assert.equal(get(g, inv("system_id=sys-a&enclosure_id=enc-foreign")).allowed, false);
  assert.equal(get(g, inv("system_id=sys-a&enclosure_id=")).allowed, false);
  // A view bound to enc-9: that enclosure, or none.
  g = load(liveBind(viewTarget("enc-9")));
  assert.equal(get(g, inv("system_id=sys-a&enclosure_id=enc-9")).allowed, true);
  assert.equal(get(g, inv("system_id=sys-a")).allowed, true);
  assert.equal(get(g, inv("system_id=sys-a&enclosure_id=enc-foreign")).allowed, false);
  // An enclosure target still requires its enclosure.
  g = load(liveBind({ page: "main", system: "sys-a", url: `${UI}/?system_id=sys-a&enclosure_id=enc-1` }));
  assert.equal(get(g, inv("system_id=sys-a&enclosure_id=enc-1")).allowed, true);
  assert.equal(get(g, inv("system_id=sys-a")).allowed, false, "an enclosure target's enclosure is required");
  // A system-only target refuses any enclosure.
  g = load(liveBind({ page: "main", system: "sys-a", url: `${UI}/?system_id=sys-a` }));
  assert.equal(get(g, inv("system_id=sys-a")).allowed, true);
  assert.equal(get(g, inv("system_id=sys-a&enclosure_id=any")).allowed, false);
  // A target with no system (the admin page on a read-only run) refuses any system_id on a bound appliance load.
  const admin = new RequestGuard({ origins: { ui: UI, admin: ADMIN } });
  admin.beginState("admin.system-form", { reads: [] });
  admin.openReads("admin.system-form", [{ method: "GET", path: "/api/admin/tls/saved-status", query: ["system_id"], max: 3 }], { bind: liveBind({ page: "admin", url: `${ADMIN}/` }), service: "admin" });
  assert.equal(get(admin, `${ADMIN}/api/admin/tls/saved-status`).allowed, true);
  assert.equal(get(admin, `${ADMIN}/api/admin/tls/saved-status?system_id=sys-any`).allowed, false);
});

test("SVBIND: a write state on a storage-view target is still refused before anything is written, even when its view has an enclosure", async () => {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { runState } = req("qa/ui-crawl/actuation");
  const calls = [];
  const driver = {
    guard: new RequestGuard({ origins: { ui: UI } }), vars: { slot: "4" }, bind: liveBind(viewTarget("enc-9")), service: "ui",
    runSteps: async (steps) => { calls.push(steps); },
  };
  const state = {
    id: "main.led", safety: "led", capture: [], steps: [{ wait: 1 }], revert: [{ wait: 1 }], verify: [],
    writes: [{ method: "POST", path: "/api/slots/{slot}/led", body_json: { action: "IDENTIFY" } }],
  };
  const outcome = await runState(driver, state, { allow: ["led"] });
  assert.equal(outcome.status, "precheck-failed");
  assert.match(outcome.error, /storage-view target/);
  assert.equal(calls.length, 1, "only the (empty) capture ran; no step, no write");
});
