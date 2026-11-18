"use strict";

// #1135: on a live admin crawl, a state that selects an existing system binds
// its declared appliance reads to the system it selects.
//
// liveTargets() gives the admin page a target with no system, so its live
// binding is system_id: null, and a null binding means a request may not carry
// the key (SVBIND). The admin states that click a saved system (the system
// form, its matrix rows and the storage-view editor) then send that system's
// live-enclosure, storage-view candidate and saved-certificate reads, which
// were refused, so a live `--appliance-load` admin crawl ended safety-failed.
// The fixture crawl binds nothing and never saw it.
//
// Now the crawler reads the system the state will select (the first listed,
// or the `--write-system` one when the run names it) before the state's read
// window opens, pins the click to that system, and binds the window to it.
// Reads for any other system stay refused. Live-shaped: fixtureMode false and
// the target exactly as liveTargets() builds it. No browser, no network.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const ADMIN = "http://admin.example.test";
const SELECT = "[data-existing-system-id]";
const LIVE_ENCLOSURES = "/api/admin/storage-views/live-enclosures";
const CANDIDATES = "/api/admin/storage-views/candidates";
const SAVED_STATUS = "/api/admin/tls/saved-status";

// The admin target exactly as scripts/ui_crawl.js liveTargets() emits it.
const ADMIN_TARGET = Object.freeze({ page: "admin", ready: "admin", scopeIndex: 0, label: "admin", url: `${ADMIN}/` });

function tmpDir(label) {
  return fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), `uic1135-${label}-`));
}

function inventoryStates() {
  const { loadInventory, expandStates } = req("qa/ui-crawl/inventory");
  const inventory = loadInventory();
  const states = expandStates(inventory);
  return { inventory, states, byId: new Map(states.map((state) => [state.id, state])) };
}

function clicksSystem(state) {
  return (state.steps || []).some((step) => step && (step.click === SELECT || step.clickIfPresent === SELECT));
}

// The saved-system buttons of the admin page, in page order, matched the way
// a CSS attribute selector does: `[data-existing-system-id]` lists every
// button, `[data-existing-system-id="X"]` only X's.
function matching(systems, css) {
  if (css === SELECT) return systems;
  const hit = /^\[data-existing-system-id="((?:[^"\\]|\\.)*)"\]$/.exec(css);
  if (!hit) return [];
  const want = hit[1].replace(/\\(.)/g, "$1");
  return systems.filter((id) => id === want);
}

function fakeAdminPage(systems) {
  return {
    url: () => "about:blank",
    locator(css) {
      const ids = matching(systems, css);
      const nodes = ids.map((id) => ({ getAttribute: (name) => (name === "data-existing-system-id" ? id : null) }));
      const self = {
        count: async () => nodes.length,
        filter: () => self,
        first: () => ({ isEnabled: async () => true, isVisible: async () => nodes.length > 0 }),
        evaluateAll: async (fn, arg) => fn(nodes, arg),
      };
      return self;
    },
    evaluate: async () => [],
  };
}

// A live (non-fixture) Crawler with the page load stubbed out. Its driver
// gets the target binding the real newDriver() gives it, and its steps act
// like the admin page: a click on a saved-system button loads the FIRST
// button the selector matches, and the page then sends that system's live
// enclosures, storage-view candidates and saved-certificate check. After the
// steps the test sends one read for another system, which must be refused.
function liveAdminCrawler({ systems, writeSystem = null, fixtureMode = false, other = "sys-other", outDir }) {
  const { Crawler } = req("qa/ui-crawl/crawler");
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { inventory } = inventoryStates();
  const guard = new RequestGuard({ origins: { admin: ADMIN } });
  const crawler = new Crawler({ context: null, cdpFactory: null, guard, inventory, outDir, widths: [1600], fixtureMode, writeSystem, log: () => {} });
  const page = fakeAdminPage(systems);
  crawler.page = page;
  crawler.load = async () => 0;
  const seen = { clicked: [], sent: [], other: null, bindAtRun: null };
  const driver = {
    guard, vars: {}, bind: {}, service: "admin", dialogsSeen: 0, dialogTexts: [], downloads: [],
    resetState() {},
    settle: async () => {},
    runSteps: async (steps) => {
      seen.bindAtRun = { ...driver.bind };
      for (const step of steps || []) {
        const css = step.clickIfPresent || step.click;
        if (!css || !css.startsWith("[data-existing-system-id")) continue;
        const [system] = matching(systems, css);
        if (!system) continue;
        seen.clicked.push(system);
        const q = encodeURIComponent(system);
        for (const url of [`${ADMIN}${LIVE_ENCLOSURES}?system_id=${q}`, `${ADMIN}${CANDIDATES}?system_id=${q}`, `${ADMIN}${SAVED_STATUS}?system_id=${q}`]) {
          seen.sent.push(guard.admit({ method: "GET", url }));
        }
      }
      seen.other = guard.admit({ method: "GET", url: `${ADMIN}${LIVE_ENCLOSURES}?system_id=${encodeURIComponent(other)}` });
    },
  };
  crawler.newDriver = (target) => {
    driver.bind = crawler.bindFor(target);
    return driver;
  };
  return { crawler, guard, seen };
}

async function crawlAdmin(stateId, options) {
  const { byId } = inventoryStates();
  const state = byId.get(stateId);
  assert.ok(state, `${stateId} is in the inventory`);
  const outDir = tmpDir("admin");
  try {
    const harness = liveAdminCrawler({ ...options, outDir });
    const record = await harness.crawler.crawlState({ ...ADMIN_TARGET }, state);
    return { ...harness, record };
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
}

test("#1135: the live admin target binds no system, so without a selected system any system_id stays refused", () => {
  const { Crawler } = req("qa/ui-crawl/crawler");
  const crawler = Object.create(Crawler.prototype);
  Object.assign(crawler, { fixtureMode: false, writeSystem: null });
  assert.deepEqual(crawler.bindFor({ ...ADMIN_TARGET }), { system_id: null, enclosure_id: null, storage_view_id: null });
});

test("#1135: a live read-only admin crawl binds each system-selecting state to the system it selects", async () => {
  const { states } = inventoryStates();
  const selecting = states.filter((state) => state.page === "admin" && clicksSystem(state));
  assert.ok(selecting.length >= 6, selecting.map((state) => state.id).join(", "));
  for (const state of selecting) {
    const { record, seen, guard } = await crawlAdmin(state.id, { systems: ["sys-a", "sys-b"] });
    assert.equal(record.status, "done", `${state.id}: ${JSON.stringify(record)}`);
    assert.deepEqual(seen.clicked, ["sys-a"], `${state.id} loads the first saved system`);
    for (const entry of seen.sent) {
      assert.equal(entry.allowed, true, `${state.id}: ${entry.method} ${entry.path} refused: ${entry.reason}`);
      assert.equal(entry.cls, "appliance-load");
    }
    assert.equal(seen.bindAtRun.system_id, "sys-a", `${state.id}: its window is bound to the selected system before any step runs`);
    assert.equal(seen.sent.length, 3);
    // Wrong-system control: a read for another system stays refused.
    assert.equal(seen.other.allowed, false, `${state.id}: a read for another system was admitted`);
    assert.match(seen.other.reason, /outside the bound system_id \(other\)/);
    assert.deepEqual(guard.blocked().map((entry) => entry.path), [`${LIVE_ENCLOSURES}?system_id=sys-other`], `${state.id}: only the other system's read was refused`);
  }
});

test("#1135: with --write-system the state selects and binds that system, even when it is not the first listed", async () => {
  const { record, seen, guard } = await crawlAdmin("admin.system-form", { systems: ["sys-a", "sys-b"], writeSystem: "sys-b", other: "sys-a" });
  assert.equal(record.status, "done", JSON.stringify(record));
  assert.deepEqual(seen.clicked, ["sys-b"], "the click is pinned to the write system");
  assert.equal(seen.bindAtRun.system_id, "sys-b");
  for (const entry of seen.sent) assert.equal(entry.allowed, true, `${entry.path}: ${entry.reason}`);
  assert.equal(seen.other.allowed, false, "the first listed system is another system here");
  assert.match(seen.other.reason, /outside the bound system_id \(other\)/);
  assert.equal(guard.blocked().length, 1);
});

test("#1135: a --write-system the admin page does not list is not-applicable, and nothing is clicked or admitted", async () => {
  const { record, seen } = await crawlAdmin("admin.system-form", { systems: ["sys-a", "sys-b"], writeSystem: "sys-z" });
  assert.equal(record.status, "not-applicable", JSON.stringify(record));
  assert.match(record.reason, /sys-z is not listed/);
  assert.deepEqual(seen.clicked, []);
  assert.equal(seen.bindAtRun, null, "no step ran");
});

test("#1135: an admin page with no saved system selects nothing and keeps refusing every system_id", async () => {
  const { record, seen } = await crawlAdmin("admin.system-form", { systems: [] });
  assert.equal(record.status, "done", JSON.stringify(record));
  assert.deepEqual(seen.clicked, []);
  assert.equal(seen.bindAtRun.system_id, null, "the window keeps the target's null system");
  assert.equal(seen.other.allowed, false);
  assert.match(seen.other.reason, /outside the bound system_id \(target has none\)/);
});

test("#1135: a fixture crawl still binds nothing and selects the first listed system", async () => {
  const { record, seen } = await crawlAdmin("admin.system-form", { systems: ["sys-a", "sys-b"], fixtureMode: true });
  assert.equal(record.status, "done", JSON.stringify(record));
  assert.deepEqual(seen.clicked, ["sys-a"]);
  assert.deepEqual(seen.bindAtRun, {}, "fixture runs have no live scope");
});

test("#1135: every state that clicks a saved system declares selects_system, and the inventory rejects one that does not", () => {
  const { loadInventory, validateInventory } = req("qa/ui-crawl/inventory");
  const inventory = loadInventory();
  assert.deepEqual(validateInventory(inventory), []);
  const selecting = inventory.states.filter(clicksSystem);
  assert.ok(selecting.length >= 6);
  for (const state of selecting) assert.equal(state.selects_system, SELECT, `${state.id} declares the system it selects`);
  const undeclared = JSON.parse(JSON.stringify(inventory));
  const target = undeclared.states.find((state) => state.id === "admin.system-form");
  delete target.selects_system;
  assert.ok(validateInventory(undeclared).some((error) => /admin\.system-form: .*selects_system/.test(error)), "a system click without selects_system is rejected");
  const malformed = JSON.parse(JSON.stringify(inventory));
  malformed.states.find((state) => state.id === "admin.system-form").selects_system = "button.pill";
  assert.ok(validateInventory(malformed).some((error) => /admin\.system-form: selects_system/.test(error)), "a selects_system that is not one attribute selector is rejected");
  const unclicked = JSON.parse(JSON.stringify(inventory));
  unclicked.states.find((state) => state.id === "admin.backups" || (state.page === "admin" && !clicksSystem(state))).selects_system = SELECT;
  assert.ok(validateInventory(unclicked).some((error) => /selects_system .* no step clicks it/.test(error)), "selects_system without a step that clicks it is rejected");
});
