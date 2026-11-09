"use strict";

// Regressions from the v0.24.0-beta.7 release QA crawl (synthetic data only).
//
//   #1143: a state whose control is present but disabled on a target is
//     `not-applicable` there, decided before any step runs (`requires_enabled`),
//     never a safety failure. An enabled control whose forbidden preview fails
//     still fails closed (preview-failed), and so does a control that turns
//     disabled after the precondition passed.
//   QuantaStor HA saved views: a storage-view target binds the enclosure the
//     page itself sends. Its URL has no enclosure_id, so app.js
//     (buildSelectionParams) sends the page's live enclosure, the server's
//     default snapshot enclosure for the system, on its inventory, storage-view
//     and SMART reads. The view's runtime backing_enclosure_id can name another
//     enclosure (an HA view bound to the partner node), and the page sends that
//     one only on the two storage-view history routes, and only for a
//     selected_enclosure_snapshot view (storageViewHistoryParams). Every other
//     enclosure id stays refused, and windows (writes, LED, appliance loads)
//     bind the page enclosure alone.
// No browser, no network.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const UI = "http://ui.example.test";
const HEAT = "window_hours=24&event_limit=0&metric_limit=24&metrics=temperature_c";

function tmpDir(label) {
  return fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), `uic12-${label}-`));
}

function inventoryStates() {
  const { loadInventory, expandStates } = req("qa/ui-crawl/inventory");
  const inventory = loadInventory();
  const states = expandStates(inventory);
  return { inventory, states, byId: new Map(states.map((state) => [state.id, state])) };
}

// ---------------------------------------------------------------- #1143 --

// A page whose locator answers for one selector: present (visible) and
// enabled or not. Every other selector is absent.
function fakePage({ selector, visible = true, enabled = true }) {
  const page = {
    gotos: 0,
    locator(css) {
      const hit = css === selector && visible;
      const first = () => ({ isEnabled: async () => enabled, isVisible: async () => hit });
      const self = { count: async () => (hit ? 1 : 0), first, filter: () => self };
      return self;
    },
    evaluate: async () => [],
  };
  return page;
}

// A Crawler with the page load stubbed out and a driver that records the
// steps it was asked to run; `dialogs` is how many native dialogs a click
// raises, `disableOnClick` makes the click itself find the control disabled.
function stubCrawler({ page, dialogs = 1, disableOnClick = false, outDir }) {
  const { Crawler } = req("qa/ui-crawl/crawler");
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { NotApplicable } = req("qa/ui-crawl/driver");
  const { inventory } = inventoryStates();
  const guard = new RequestGuard({ origins: { ui: UI } });
  const crawler = new Crawler({ context: null, cdpFactory: null, guard, inventory, outDir, widths: [1600], fixtureMode: true, confirmPreview: true, log: () => {} });
  crawler.page = page;
  crawler.load = async () => 0;
  const ran = [];
  const driver = {
    guard, vars: {}, bind: {}, service: "ui", dialogsSeen: 0, dialogTexts: [], downloads: [],
    resetState() {},
    settle: async () => {},
    runSteps: async (steps) => {
      for (const step of steps || []) {
        ran.push(step);
        if (step.click && disableOnClick) throw new NotApplicable(`${step.click} is disabled here`);
        if (step.click) driver.dialogsSeen += dialogs;
      }
    },
  };
  crawler.newDriver = () => driver;
  return { crawler, ran, guard };
}

test("#1143: the disk-sync preview on a target whose button is disabled is not-applicable, before any step runs", async () => {
  const { byId } = inventoryStates();
  const state = byId.get("main.disk-sync");
  assert.ok(state, "main.disk-sync is in the inventory");
  assert.equal(state.requires_enabled, true, "main.disk-sync requires its control to be enabled");
  const outDir = tmpDir("disabled");
  try {
    const { crawler, ran, guard } = stubCrawler({ page: fakePage({ selector: state.requires, enabled: false }), outDir });
    const record = await crawler.crawlState({ page: "main", ready: "main", label: "synthetic/core" }, state);
    assert.equal(record.status, "not-applicable", JSON.stringify(record));
    assert.match(record.reason, /disabled/);
    assert.deepEqual(ran, [], "no step ran: the control was never pressed");
    assert.deepEqual(crawler.actuations, [], "a not-applicable preview is no actuation");
    assert.deepEqual(guard.log, [], "nothing was sent");
    assert.equal(record.findings.filter((finding) => finding.severity === "error").length, 0);
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

test("#1143: an enabled control still previews, and a failed preview of an enabled control still fails closed", async () => {
  const { byId } = inventoryStates();
  const state = byId.get("main.disk-sync");
  const outDir = tmpDir("enabled");
  try {
    let { crawler, ran } = stubCrawler({ page: fakePage({ selector: state.requires, enabled: true }), outDir });
    let record = await crawler.crawlState({ page: "main", ready: "main", label: "synthetic/core" }, state);
    assert.equal(record.status, "previewed", JSON.stringify(record));
    assert.equal(ran.length, 1);
    // No confirmation dialog: preview-failed, recorded as an actuation.
    ({ crawler } = stubCrawler({ page: fakePage({ selector: state.requires, enabled: true }), dialogs: 0, outDir }));
    record = await crawler.crawlState({ page: "main", ready: "main", label: "synthetic/core" }, state);
    assert.equal(record.status, "preview-failed");
    assert.equal(crawler.actuations.length, 1);
    // Enabled at the precondition, disabled when clicked: still preview-failed, never not-applicable.
    ({ crawler } = stubCrawler({ page: fakePage({ selector: state.requires, enabled: true }), disableOnClick: true, outDir }));
    record = await crawler.crawlState({ page: "main", ready: "main", label: "synthetic/core" }, state);
    assert.equal(record.status, "preview-failed", JSON.stringify(record));
    assert.match(record.outcome.error, /disabled/);
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

test("#1143: a control that is absent stays not-applicable through `requires`, and a disabled one without requires_enabled keeps the old fail-closed path", async () => {
  const { byId } = inventoryStates();
  const state = byId.get("main.disk-sync");
  const outDir = tmpDir("absent");
  try {
    let { crawler, ran } = stubCrawler({ page: fakePage({ selector: state.requires, visible: false }), outDir });
    let record = await crawler.crawlState({ page: "main", ready: "main", label: "synthetic/scale" }, state);
    assert.equal(record.status, "not-applicable");
    assert.match(record.reason, /requires/);
    assert.deepEqual(ran, []);
    const { requires_enabled: _drop, ...legacy } = state;
    ({ crawler, ran } = stubCrawler({ page: fakePage({ selector: state.requires, enabled: false }), disableOnClick: true, outDir }));
    record = await crawler.crawlState({ page: "main", ready: "main", label: "synthetic/core" }, legacy);
    assert.equal(record.status, "preview-failed", "without the precondition a disabled control is not quietly skipped");
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

test("#1143: requires_enabled is validated, and every confirm-preview state that clicks its required control declares it", () => {
  const { validateInventory, loadInventory } = req("qa/ui-crawl/inventory");
  const base = loadInventory();
  assert.deepEqual(validateInventory(base), []);
  const withState = (patch) => ({ ...base, states: base.states.map((state) => (state.id === "main.disk-sync" ? patch(state) : state)) });
  const missing = validateInventory(withState(({ requires_enabled: _drop, ...state }) => state));
  assert.ok(missing.some((error) => /main\.disk-sync: .*requires_enabled/.test(error)), missing.join("\n"));
  const noRequires = validateInventory(withState(({ requires: _drop, ...state }) => state));
  assert.ok(noRequires.some((error) => /main\.disk-sync: requires_enabled needs requires/.test(error)), noRequires.join("\n"));
  const notBoolean = validateInventory(withState((state) => ({ ...state, requires_enabled: "yes" })));
  assert.ok(notBoolean.some((error) => /main\.disk-sync: requires_enabled must be true or absent/.test(error)), notBoolean.join("\n"));
});

// ------------------------------------------------- QuantaStor HA views --

const PAGE_ENC = "enc-node-a"; // the page's live enclosure (default snapshot)
const PARTNER_ENC = "enc-node-b"; // the HA partner node's enclosure

function liveBind(target) {
  const { Crawler } = req("qa/ui-crawl/crawler");
  return Crawler.prototype.bindFor.call({ fixtureMode: false, writeSystem: null }, target);
}

function stateGuard(stateId, scope, bind) {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { readsFor } = req("qa/ui-crawl/inventory");
  const { inventory, byId } = inventoryStates();
  const state = byId.get(stateId);
  assert.ok(state, `no state ${stateId}`);
  const g = new RequestGuard({ origins: { ui: UI } });
  g.beginState(stateId, { reads: readsFor(inventory, state, scope), bind });
  return g;
}

const haView = (extra = {}) => ({
  page: "main", system: "qs-ha", url: `${UI}/?system_id=qs-ha&storage_view_id=view-ha`,
  pageEnclosure: PAGE_ENC, viewEnclosure: PARTNER_ENC, viewSource: "inventory_binding", ...extra,
});
const sesView = (extra = {}) => ({
  page: "main", system: "qs-ha", url: `${UI}/?system_id=qs-ha&storage_view_id=view-ses`,
  pageEnclosure: PAGE_ENC, viewEnclosure: PARTNER_ENC, viewSource: "selected_enclosure_snapshot", ...extra,
});
const send = (g, method, url) => g.admit({ method, url, body: method === "POST" ? JSON.stringify({ slots: [0, 1], max_concurrency: 2 }) : null });

test("QuantaStor HA view: liveTargets gives each view target the page's live enclosure, its view's enclosure and source", async () => {
  const cli = req("scripts/ui_crawl.js");
  const pages = {
    "/": `<select id="system-select"><option value="qs-ha">QS</option></select>`,
    "/api/inventory?system_id=qs-ha": JSON.stringify({ selected_system_platform: "quantastor", selected_enclosure_id: PAGE_ENC, enclosures: [{ id: PAGE_ENC }, { id: PARTNER_ENC }] }),
    "/api/storage-views?system_id=qs-ha": JSON.stringify({ views: [
      { id: "view-local", source: "inventory_binding", backing_enclosure_id: PAGE_ENC },
      { id: "view-ha", source: "inventory_binding", backing_enclosure_id: PARTNER_ENC },
      { id: "view-ses", source: "selected_enclosure_snapshot", backing_enclosure_id: PARTNER_ENC },
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
  const byView = Object.fromEntries(targets.filter((t) => /storage_view_id=/.test(t.url)).map((t) => [new URL(t.url).searchParams.get("storage_view_id"), t]));
  assert.deepEqual(Object.keys(byView).sort(), ["view-ha", "view-local", "view-ses"]);
  for (const id of Object.keys(byView)) assert.equal(byView[id].pageEnclosure, PAGE_ENC, id);
  assert.equal(byView["view-ha"].viewEnclosure, PARTNER_ENC);
  assert.equal(byView["view-ha"].viewSource, "inventory_binding");
  assert.equal(byView["view-ses"].viewSource, "selected_enclosure_snapshot");
  // The HA view binds the page's enclosure, the one its page sends.
  assert.equal(liveBind(byView["view-ha"]).enclosure_id, PAGE_ENC);
  // Enclosure targets are unchanged.
  for (const target of targets.filter((t) => t.page === "main" && !/storage_view_id=/.test(t.url))) {
    assert.equal("pageEnclosure" in target, false, target.label);
    assert.equal(liveBind(target).enclosure_id, new URL(target.url).searchParams.get("enclosure_id"));
  }
});

test("QuantaStor HA view: an HA view target admits the page's own reads (the beta.7 blocked requests) and refuses every other enclosure", () => {
  const bind = liveBind(haView());
  assert.deepEqual(bind, { system_id: "qs-ha", enclosure_id: PAGE_ENC, storage_view_id: "view-ha", optional: ["enclosure_id"] });
  for (const stateId of ["main.default", "main.heatmap", "main.heatmap.temperature"]) {
    const g = stateGuard(stateId, "storage-view", bind);
    const q = (enc) => `system_id=qs-ha&enclosure_id=${enc}`;
    const admitted = [
      ["GET", `${UI}/api/inventory?${q(PAGE_ENC)}&force=false`],
      ["GET", `${UI}/api/storage-views?${q(PAGE_ENC)}&force=false`],
      ["POST", `${UI}/api/slots/smart-batch?${q(PAGE_ENC)}`],
    ];
    for (const [method, url] of admitted) {
      const verdict = send(g, method, url);
      assert.equal(verdict.allowed, true, `${stateId} ${method} ${url}: ${verdict.reason}`);
    }
    // Negative: a truly foreign enclosure, an empty one, a mixed repeat, the
    // partner enclosure on a non-history read (the page never sends it there),
    // and another system.
    for (const query of [q("enc-foreign"), "system_id=qs-ha&enclosure_id=", `${q(PAGE_ENC)}&enclosure_id=enc-foreign`, q(PARTNER_ENC), `system_id=qs-other&enclosure_id=${PAGE_ENC}`]) {
      for (const [method, pathPart] of [["GET", "/api/inventory"], ["GET", "/api/storage-views"], ["POST", "/api/slots/smart-batch"]]) {
        const verdict = send(g, method, `${UI}${pathPart}?${query}`);
        assert.equal(verdict.allowed, false, `${stateId} ${method} ${pathPart}?${query} must be refused`);
      }
    }
    // An inventory_binding view's history reads carry the page enclosure too, never the partner's.
    assert.equal(send(g, "GET", `${UI}/api/storage-views/view-ha/history?${q(PAGE_ENC)}&${HEAT}`).allowed, true);
    assert.equal(send(g, "GET", `${UI}/api/storage-views/view-ha/history?${q(PARTNER_ENC)}&${HEAT}`).allowed, false);
  }
});

test("QuantaStor HA view: a snapshot (SES) view's history reads may carry the view's own enclosure, and only those two routes", () => {
  const bind = liveBind(sesView());
  assert.equal(bind.enclosure_id, PAGE_ENC);
  const g = stateGuard("main.heatmap.metric", "storage-view", bind);
  const q = (enc) => `system_id=qs-ha&enclosure_id=${enc}`;
  for (const enc of [PAGE_ENC, PARTNER_ENC]) {
    assert.equal(send(g, "GET", `${UI}/api/storage-views/view-ses/history?${q(enc)}&${HEAT}`).allowed, true, enc);
  }
  const history = stateGuard("main.history", "storage-view", bind);
  assert.equal(send(history, "GET", `${UI}/api/storage-views/view-ses/slots/3/history?${q(PARTNER_ENC)}&window_hours=24`).allowed, true);
  // Not on any other read, not a foreign enclosure, not another view.
  assert.equal(send(g, "GET", `${UI}/api/inventory?${q(PARTNER_ENC)}`).allowed, false);
  assert.equal(send(history, "GET", `${UI}/api/storage-views/view-ses/slots/3/smart?${q(PARTNER_ENC)}`).allowed, false);
  assert.equal(send(g, "GET", `${UI}/api/storage-views/view-ses/history?${q("enc-foreign")}&${HEAT}`).allowed, false);
  assert.equal(send(g, "GET", `${UI}/api/storage-views/view-ses/history?${q(PARTNER_ENC)}&enclosure_id=enc-foreign&${HEAT}`).allowed, false);
  assert.equal(send(g, "GET", `${UI}/api/storage-views/view-other/history?${q(PARTNER_ENC)}&${HEAT}`).allowed, false);
  // An inventory_binding view gets no such alternate.
  assert.equal("read_alternates" in liveBind(haView()), false);
  // Nor does a snapshot view whose enclosure is the page's.
  assert.equal("read_alternates" in liveBind(sesView({ viewEnclosure: PAGE_ENC })), false);
});

test("QuantaStor HA view: windows on a view target bind the page enclosure only; writes stay refused there", async () => {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { runState } = req("qa/ui-crawl/actuation");
  const forced = [{ method: "GET", path: "/api/inventory", force: true, max: 5 }, { method: "GET", path: "/api/storage-views", force: true, max: 5 }];
  for (const target of [haView(), sesView()]) {
    const g = new RequestGuard({ origins: { ui: UI } });
    g.openLoad("main.default", forced, { bind: liveBind(target) });
    assert.equal(g.admit({ method: "GET", url: `${UI}/api/inventory?system_id=qs-ha&enclosure_id=${PAGE_ENC}&force=true` }).allowed, true);
    assert.equal(g.admit({ method: "GET", url: `${UI}/api/storage-views?system_id=qs-ha&enclosure_id=${PAGE_ENC}&force=true` }).allowed, true);
    for (const enc of [PARTNER_ENC, "enc-foreign", ""]) {
      assert.equal(g.admit({ method: "GET", url: `${UI}/api/inventory?system_id=qs-ha&enclosure_id=${enc}&force=true` }).allowed, false, `forced read with enclosure ${enc}`);
    }
  }
  const driver = {
    guard: new RequestGuard({ origins: { ui: UI } }), vars: { slot: "4" }, bind: liveBind(sesView()), service: "ui",
    runSteps: async () => {},
  };
  const state = {
    id: "main.led", safety: "led", capture: [], steps: [{ wait: 1 }], revert: [{ wait: 1 }], verify: [],
    writes: [{ method: "POST", path: "/api/slots/{slot}/led", body_json: { action: "IDENTIFY" } }],
  };
  const outcome = await runState(driver, state, { allow: ["led"] });
  assert.equal(outcome.status, "precheck-failed");
});

test("QuantaStor HA view: a view target with no known page enclosure refuses every enclosure_id; a malformed alternate is refused at state start", () => {
  const bind = liveBind(haView({ pageEnclosure: undefined }));
  assert.equal(bind.enclosure_id, null);
  const g = stateGuard("main.default", "storage-view", bind);
  for (const enc of [PAGE_ENC, PARTNER_ENC, ""]) {
    assert.equal(send(g, "GET", `${UI}/api/inventory?system_id=qs-ha&enclosure_id=${enc}`).allowed, false, enc);
  }
  assert.equal(send(g, "GET", `${UI}/api/inventory?system_id=qs-ha`).allowed, true);
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const bad = [
    [{ key: "system_id", value: "x", routes: ["GET /api/storage-views/{view_id}/history"] }],
    [{ key: "enclosure_id", value: "", routes: ["GET /api/storage-views/{view_id}/history"] }],
    [{ key: "enclosure_id", value: "enc-x", routes: [] }],
    [{ key: "enclosure_id", value: "enc-x", routes: ["GET /api/inventory*"] }],
    { key: "enclosure_id" },
  ];
  for (const alternates of bad) {
    const guard = new RequestGuard({ origins: { ui: UI } });
    assert.throws(() => guard.beginState("s", { reads: [], bind: { system_id: "qs-ha", enclosure_id: PAGE_ENC, read_alternates: alternates } }), /read_alternates/, JSON.stringify(alternates));
  }
});
