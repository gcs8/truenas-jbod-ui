"use strict";

// Round-8 regressions for the UI coverage crawler (#1108, owner decision
// D-TNJ-20261109-CRAWLER-R8): the two Codex P2s at 97d12af9.
//   1. Selecting a bay can send GET /api/slots/{slot}/smart before the batch
//      SMART prefetch starts (renderLiveSlotDetail -> ensureSmartSummary, for
//      example with a long APP_SMART_PREFETCH_DELAY_MS). Every state that
//      selects a bay, derived from its steps, admits that read, bound to the
//      target's system and enclosure; no other state admits it.
//   2. Admin storage-view candidate reads carry target_system_id for a
//      QuantaStor HA binding (fetchStorageViewCandidates); every state that
//      loads an existing system declares that query key.
// No browser, no network.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const ORIGIN = "http://ui.example.test";
const ADMIN = "http://admin.example.test";
const SLOT_SMART = "/api/slots/{slot}/smart";

function inventoryStates() {
  const { loadInventory, expandStates } = req("qa/ui-crawl/inventory");
  const inventory = loadInventory();
  return { inventory, states: expandStates(inventory), byId: new Map(expandStates(inventory).map((state) => [state.id, state])) };
}

// The test's own scan of the steps: a bay is selected by the selectBay step,
// or by pressing a control whose click handler calls selectSlot().
const SELECTING_HOOKS = ["data-topology-slot", "data-sas-fabric-trace", "data-sas-fabric-slot", "#search-box"];
function stepsOf(state) {
  return ["capture", "steps", "revert", "verify"].flatMap((field) => state[field] || []);
}
function scanSelects(state) {
  return stepsOf(state).some((step) => step.selectBay !== undefined
    || ["click", "clickIfPresent", "fill"].some((kind) => typeof step[kind] === "string" && SELECTING_HOOKS.some((hook) => step[kind].includes(hook))));
}

// --------------------------------------------- 1. per-slot SMART on select --

test("Codex P2 1: app.js still sends the per-slot SMART read straight from the bay detail, scoped by system and enclosure", () => {
  const app = fs.readFileSync(path.join(root, "app/static/app.js"), "utf8");
  const detail = app.slice(app.indexOf("function renderLiveSlotDetail("), app.indexOf("function renderLiveSlotDetail(") + 8000);
  assert.match(detail, /void ensureSmartSummary\(slot\);/, "selection renders the detail, which asks for the slot's SMART summary directly");
  assert.match(app, /sendScopedRequest\(`\/api\/slots\/\$\{slot\.slot\}\/smart`\)/, "the read goes through buildScopedUrl (system_id, enclosure_id)");
  const scoped = app.slice(app.indexOf("function buildScopedUrl("), app.indexOf("function buildScopedUrl(") + 900);
  assert.match(scoped, /buildSelectionParams\(\)/);
});

// D-TNJ-20261109-CRAWLER-SWEEP: the per-slot SMART read is no longer a
// hand-declared selection read; it is generated from app.js for the main
// page (both scope kinds) and bound to the target's system and enclosure.
// selectsBay() still names the states the browser test crawls on the
// slow-prefetch page.
test("Codex P2 1: the per-slot SMART read is generated for the main page, bound to the target's system and enclosure, and selectsBay still derives the selecting states", () => {
  const { inventory, states } = inventoryStates();
  const { validateInventory, readsFor, selectsBay } = req("qa/ui-crawl/inventory");
  assert.deepEqual(validateInventory(inventory), []);
  assert.equal(typeof selectsBay, "function", "inventory.selectsBay derives bay selection from a state's steps");
  const main = inventory.pages.main;
  assert.equal((main.reads || []).some((rule) => rule.path === SLOT_SMART), false, "not hand-declared on the page");
  assert.equal((main.selection || {}).reads, undefined, "the selection read is generated now");
  const selecting = states.filter((state) => state.page === "main" && selectsBay(inventory, state)).map((state) => state.id);
  for (const id of ["main.select[healthy]", "main.select[empty]", "main.select[fault]", "main.select[unknown]", "main.select.smart-open", "main.select.topology",
    "main.heatmap.select", "main.history", "main.history.rate", "main.history.window", "main.mapping.form", "main.mapping.notes", "main.led", "main.quantastor"]) {
    assert.ok(selecting.includes(id), `${id} selects a bay`);
  }
  for (const state of states.filter((s) => s.page === "main")) {
    assert.equal(scanSelects(state), selecting.includes(state.id), `${state.id}: the steps and selectsBay agree`);
    for (const scope of ["enclosure", "storage-view"]) {
      const reads = readsFor(inventory, state, scope).filter((read) => read.path === SLOT_SMART);
      assert.equal(reads.length, 1, `${state.id}/${scope} admits the per-slot SMART read once`);
      assert.deepEqual(reads[0].bind, ["system_id", "enclosure_id"], `${state.id}: bound to the target`);
    }
  }
  for (const id of ["main.default", "main.heatmap", "main.export.dialog", "main.connections", "main.refresh-now"]) {
    assert.equal(selecting.includes(id), false, `${id} selects no bay`);
  }
  // No state declares it on its own (that would be a second source of truth).
  for (const state of inventory.states.filter((s) => s.page === "main")) {
    assert.equal((state.reads || []).some((read) => read.path === SLOT_SMART), false, `${state.id} declares ${SLOT_SMART} itself`);
  }
});

test("Codex P2 1: the inventory rejects a hand-declared selection read or a malformed selection", () => {
  const { loadInventory, validateInventory } = req("qa/ui-crawl/inventory");
  const broken = (mutate) => {
    const inventory = loadInventory();
    mutate(inventory);
    return validateInventory(inventory).join("\n");
  };
  assert.match(broken((inv) => { inv.pages.main.selection.reads = [{ method: "GET", path: SLOT_SMART, query: ["system_id", "enclosure_id"], bind: ["system_id", "enclosure_id"] }]; }), /selection reads are generated/);
  assert.match(broken((inv) => { inv.pages.main.selection.controls = []; }), /selection needs controls/);
  assert.match(broken((inv) => { delete inv.pages.main.selection.why; }), /selection needs a why/);
  assert.match(broken((inv) => { inv.states.find((s) => s.id === "main.history").reads = [{ method: "GET", path: SLOT_SMART, query: ["system_id", "enclosure_id"] }]; }), /generated from the page scripts/);
});

function selectionGuard(stateId, bind) {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { readsFor } = req("qa/ui-crawl/inventory");
  const { inventory, byId } = inventoryStates();
  const g = new RequestGuard({ origins: { ui: ORIGIN } });
  g.beginState(stateId, { reads: readsFor(inventory, byId.get(stateId), "enclosure"), bind });
  return g;
}

const smart = (query) => `${ORIGIN}/api/slots/7/smart${query === null ? "" : `?${query}`}`;

test("Codex P2 1: the guard admits the per-slot SMART read of the target's system and enclosure on the main page, and refuses every other scope", () => {
  const bind = { system_id: "sys-a", enclosure_id: "enc-1" };
  for (const stateId of ["main.select[healthy]", "main.history", "main.mapping.form", "main.heatmap.select", "main.select.smart-open", "main.default"]) {
    const g = selectionGuard(stateId, bind);
    const ok = g.admit({ method: "GET", url: smart("system_id=sys-a&enclosure_id=enc-1") });
    assert.equal(ok.allowed, true, `${stateId}: ${ok.reason}`);
    assert.equal(ok.cls, "read");
    const refused = {
      "another system": "system_id=sys-b&enclosure_id=enc-1",
      "another enclosure": "system_id=sys-a&enclosure_id=enc-2",
      "no enclosure": "system_id=sys-a",
      "no scope": null,
      "a repeated key with another system": "system_id=sys-a&enclosure_id=enc-1&system_id=sys-b",
      "an undeclared query key": "system_id=sys-a&enclosure_id=enc-1&force=false&view=x",
    };
    for (const [why, query] of Object.entries(refused)) {
      const verdict = g.admit({ method: "GET", url: smart(query) });
      assert.equal(verdict.allowed, false, `${stateId}: ${why} must be refused`);
    }
    // A forced read is an appliance load, never a plain read.
    assert.equal(g.admit({ method: "GET", url: smart("system_id=sys-a&enclosure_id=enc-1&force=true") }).allowed, false);
  }
  // A page whose scripts cannot send it refuses it even in scope.
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { readsFor } = req("qa/ui-crawl/inventory");
  const { inventory, byId } = inventoryStates();
  const history = new RequestGuard({ origins: { ui: ORIGIN } });
  history.beginState("history.default", { reads: readsFor(inventory, byId.get("history.default")), bind });
  assert.equal(history.admit({ method: "GET", url: smart("system_id=sys-a&enclosure_id=enc-1") }).allowed, false);
  // The binding covers every generated read of the page.
  const g = selectionGuard("main.select[healthy]", bind);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=sys-a&enclosure_id=enc-1` }).allowed, true);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=sys-b&enclosure_id=enc-1` }).allowed, false);
});

test("Codex P2 1: with no captured scope (fixture runs, a storage-view target) only the query keys are checked; a bound key that is set still binds", () => {
  const free = selectionGuard("main.select[healthy]", {});
  assert.equal(free.admit({ method: "GET", url: smart("system_id=demo-system&enclosure_id=demo-enclosure") }).allowed, true);
  const systemOnly = selectionGuard("main.select[healthy]", { system_id: "sys-a", enclosure_id: null });
  assert.equal(systemOnly.admit({ method: "GET", url: smart("system_id=sys-a&enclosure_id=any") }).allowed, true);
  assert.equal(systemOnly.admit({ method: "GET", url: smart("system_id=sys-b&enclosure_id=any") }).allowed, false);
});

test("Codex P2 1: the crawler gives every state its target's scope, and a live target binds system and enclosure", () => {
  const crawler = fs.readFileSync(path.join(root, "qa/ui-crawl/crawler.js"), "utf8");
  const begins = [...crawler.matchAll(/this\.guard\.beginState\(([^;]+)\);/g)].map((m) => m[1]);
  assert.ok(begins.length >= 2);
  for (const call of begins) assert.match(call, /bind: this\.bindFor\(target\)/, call);
  const { Crawler } = req("qa/ui-crawl/crawler");
  const bindFor = Crawler.prototype.bindFor;
  assert.deepEqual(bindFor.call({ fixtureMode: false, writeSystem: null }, { system: "sys-a", url: `${ORIGIN}/?system_id=sys-a&enclosure_id=enc-1` }), { system_id: "sys-a", enclosure_id: "enc-1" });
});

test("Codex P2 1: the CI fixture has a long SMART prefetch delay page and crawls every selecting state on it", () => {
  const builder = fs.readFileSync(path.join(root, "scripts/build_ui_crawl_fixture.py"), "utf8");
  assert.match(builder, /SLOW_SMART_PREFETCH_DELAY_MS = \d{6,}/);
  assert.match(builder, /"ui-slow-prefetch:\/": render_main\(bundle, settings=slow_prefetch_settings\(\)\)/);
  const spec = fs.readFileSync(path.join(root, "qa/ui-coverage.spec.js"), "utf8");
  assert.match(spec, /"ui-slow-prefetch": "https:\/\/ui-slow-prefetch\.synthetic\.invalid"/);
  assert.match(spec, /selectsBay\(inventory, state\)/);
  assert.match(spec, /read GET \/api\/slots\/\{slot\}\/smart/);
});

// ------------------------------------- 2. HA target on candidate reads ----

const CANDIDATES = "/api/admin/storage-views/candidates";

function loadsExistingSystem(state) {
  return (state.steps || []).some((step) => step.click === "[data-existing-system-id]" || step.clickIfPresent === "[data-existing-system-id]");
}

test("Codex P2 2: admin.js sends target_system_id on candidate reads for a QuantaStor HA binding", () => {
  const admin = fs.readFileSync(path.join(root, "admin_service/static/admin.js"), "utf8");
  const fetcher = admin.slice(admin.indexOf("async function fetchStorageViewCandidates("), admin.indexOf("async function fetchStorageViewCandidates(") + 1400);
  assert.match(fetcher, /params\.set\("target_system_id", targetSystemId\)/);
  const keys = [...fetcher.matchAll(/params\.set\("(\w+)"/g)].map((m) => m[1]);
  assert.deepEqual(keys.sort(), ["force", "target_system_id"]);
  assert.match(fetcher, /new URLSearchParams\(\{ system_id: systemId \}\)/);
});

test("Codex P2 2: every admin state that loads an existing system declares target_system_id on its candidate reads", () => {
  const { inventory } = inventoryStates();
  const loaders = inventory.states.filter((state) => state.page === "admin" && loadsExistingSystem(state));
  assert.ok(loaders.length >= 6, loaders.map((s) => s.id).join(", "));
  for (const state of loaders) {
    const rules = (state.read_requests || []).filter((rule) => rule.path === CANDIDATES);
    assert.equal(rules.length, 1, `${state.id} declares the candidate read once`);
    assert.deepEqual(rules[0].query, ["target_system_id"], `${state.id}: the HA target key (system_id is bound)`);
    assert.equal(rules[0].force, undefined, `${state.id}: never a forced candidate refresh`);
  }
  for (const state of inventory.states.filter((s) => !loadsExistingSystem(s))) {
    assert.equal((state.read_requests || []).some((rule) => rule.path === CANDIDATES), false, `${state.id} loads no system`);
  }
});

test("Codex P2 2: the guard admits a candidate read with the HA target in a loading state; another system, a force or an unknown key stays refused", async () => {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { runState } = req("qa/ui-crawl/actuation");
  const { readsFor } = req("qa/ui-crawl/inventory");
  const { byId } = inventoryStates();
  const { inventory } = inventoryStates();
  for (const stateId of ["admin.storage-views", "admin.storage-views.binding[auto]", "admin.system-form", "admin.system-form.quantastor-ha"]) {
    const state = byId.get(stateId);
    const g = new RequestGuard({ origins: { admin: ADMIN } });
    g.beginState(state.id, { reads: readsFor(inventory, state) });
    const seen = {};
    const driver = {
      guard: g, vars: {}, bind: { system_id: "qs-ha" }, service: "admin",
      runSteps: async () => {
        seen.ha = g.admit({ method: "GET", url: `${ADMIN}${CANDIDATES}?system_id=qs-ha&target_system_id=qs-node-b` });
        seen.plain = g.admit({ method: "GET", url: `${ADMIN}${CANDIDATES}?system_id=qs-ha` });
        seen.other = g.admit({ method: "GET", url: `${ADMIN}${CANDIDATES}?system_id=qs-other&target_system_id=qs-node-b` });
        seen.forced = g.admit({ method: "GET", url: `${ADMIN}${CANDIDATES}?system_id=qs-ha&target_system_id=qs-node-b&force=true` });
        seen.unknown = g.admit({ method: "GET", url: `${ADMIN}${CANDIDATES}?system_id=qs-ha&target_system_id=qs-node-b&pool=x` });
      },
    };
    const outcome = await runState(driver, state, { applianceLoad: true });
    assert.equal(outcome.status, "done", stateId);
    assert.equal(seen.ha.allowed, true, `${stateId}: ${seen.ha.reason}`);
    assert.equal(seen.ha.cls, "appliance-load");
    assert.equal(seen.plain.allowed, true, `${stateId}: ${seen.plain.reason}`);
    assert.equal(seen.other.allowed, false, `${stateId}: another system`);
    assert.equal(seen.forced.allowed, false, `${stateId}: forced refresh`);
    assert.equal(seen.unknown.allowed, false, `${stateId}: undeclared key`);
  }
});

test("Codex P2 2: the CI fixture has a QuantaStor HA system whose storage view binds target_system_id, crawled by the loading states", () => {
  const builder = fs.readFileSync(path.join(root, "scripts/build_ui_crawl_fixture.py"), "utf8");
  const block = /ADMIN_BOOTSTRAP_QUANTASTOR_HA = \{[\s\S]*?\n\}\n/.exec(builder);
  assert.ok(block, "a QuantaStor HA admin bootstrap");
  assert.match(block[0], /"platform": "quantastor"/);
  assert.match(block[0], /"ha_enabled": True/);
  assert.match(block[0], /"target_system_id": "[\w-]+"/);
  assert.match(builder, /"admin-quantastor-ha:\/":/);
  assert.match(builder, /"admin-quantastor-ha:\/api\/admin\/state": ADMIN_BOOTSTRAP_QUANTASTOR_HA/);
  const spec = fs.readFileSync(path.join(root, "qa/ui-coverage.spec.js"), "utf8");
  assert.match(spec, /"admin-quantastor-ha": "https:\/\/admin-quantastor-ha\.synthetic\.invalid"/);
  assert.match(spec, /target_system_id=/);
});
