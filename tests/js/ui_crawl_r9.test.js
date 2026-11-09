"use strict";

// Round-9 regressions for the UI coverage crawler (#1108, owner decision
// D-TNJ-20261109-CRAWLER-SWEEP): plain reads (read, read-post) are admitted
// from a GENERATED reach, the /api requests each page's own scripts can send
// per scope kind (qa/ui-read-reach.json, scripts/generate_ui_read_reach.js),
// and only when bound to the target. Appliance loads, writes, LED, forbidden
// and internal routes stay explicit per-state declarations. Includes Codex
// PRRT_kwDOSAqK-M61IUDz: the storage-view heat-map history read. No browser,
// no network.

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const UI = "http://ui.example.test";
const ADMIN = "http://admin.example.test";
const HISTORY = "http://history.example.test";
const VIEW_HISTORY = "/api/storage-views/{view_id}/history";
const VIEW_SMART = "/api/storage-views/{view_id}/slots/{slot_index}/smart";
const GENERATOR = path.join(root, "scripts/generate_ui_read_reach.js");

function tmpDir(label) {
  return fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), `uic9-${label}-`));
}

function inventoryStates() {
  const { loadInventory, expandStates } = req("qa/ui-crawl/inventory");
  const inventory = loadInventory();
  const states = expandStates(inventory);
  return { inventory, states, byId: new Map(states.map((state) => [state.id, state])) };
}

// A guard for one state on one page scope kind, bound like a live target.
function stateGuard(stateId, scope, bind, origins = { ui: UI, admin: ADMIN, history: HISTORY }) {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { readsFor } = req("qa/ui-crawl/inventory");
  const { inventory, byId } = inventoryStates();
  const state = byId.get(stateId);
  assert.ok(state, `no state ${stateId}`);
  const g = new RequestGuard({ origins });
  g.beginState(stateId, { reads: readsFor(inventory, state, scope), bind });
  return g;
}

const get = (g, url) => g.admit({ method: "GET", url });

// ----------------------------------------------------------- the generator --

test("r9: the committed read reach is exactly what the generator produces from the page scripts (CI drift check)", () => {
  const out = spawnSync(process.execPath, [GENERATOR, "--check"], { cwd: root, encoding: "utf8" });
  assert.equal(out.status, 0, out.stderr);
  const { buildReach, renderReach, REACH_PATH } = req("qa/ui-crawl/reach");
  const { reach, problems } = buildReach(root);
  assert.deepEqual(problems, [], "every /api call site in the page scripts resolves to a manifest route");
  assert.equal(renderReach(reach), fs.readFileSync(REACH_PATH, "utf8"));
  // Deterministic: a second build is byte-identical.
  assert.equal(renderReach(buildReach(root).reach), renderReach(reach));
});

test("r9: the drift check fails when the committed reach is edited or a page script gains a read", () => {
  const dir = tmpDir("drift");
  const { REACH_PATH } = req("qa/ui-crawl/reach");
  const committed = JSON.parse(fs.readFileSync(REACH_PATH, "utf8"));
  // 1. An edited reach file (a hand-widened allowlist) is stale.
  const edited = JSON.parse(JSON.stringify(committed));
  edited.pages.main.scopes.enclosure.push({ method: "GET", path: "/livez", class: "read", query: [], bind: [], sites: ["hand-edit"] });
  const editedFile = path.join(dir, "edited.json");
  fs.writeFileSync(editedFile, `${JSON.stringify(edited, null, 1)}\n`);
  const stale = spawnSync(process.execPath, [GENERATOR, "--check", "--out", editedFile], { cwd: root, encoding: "utf8" });
  assert.equal(stale.status, 1);
  assert.match(stale.stderr, /stale/);
  // 2. A page script that gains a read makes the committed reach stale.
  const copy = path.join(dir, "root");
  for (const rel of ["app/templates", "app/static", "admin_service/templates", "admin_service/static", "history_service/templates", "history_service/static"]) {
    fs.cpSync(path.join(root, rel), path.join(copy, rel), { recursive: true, filter: (src) => !/\/images(\/|$)/.test(src) });
  }
  const app = path.join(copy, "app/static/app.js");
  const text = fs.readFileSync(app, "utf8");
  const anchor = "  async function sendScopedRequest(url, options = {}, queryParams = null) {";
  assert.ok(text.includes(anchor));
  fs.writeFileSync(app, text.replace(anchor, `  async function readScopeBundleProbe() {\n    return sendScopedRequest("/api/history/scopes/bundle", { method: "POST", body: "{}" });\n  }\n\n${anchor}`));
  const drift = spawnSync(process.execPath, [GENERATOR, "--check", "--root", copy, "--out", REACH_PATH], { cwd: root, encoding: "utf8" });
  assert.equal(drift.status, 1, "the committed reach no longer matches the changed sources");
  const { buildReach } = req("qa/ui-crawl/reach");
  const rules = buildReach(copy).reach.pages.main.scopes.enclosure;
  const added = rules.find((rule) => rule.path === "/api/history/scopes/bundle");
  assert.ok(added, "the new read is generated");
  assert.equal(added.method, "POST");
  assert.deepEqual(added.bind, ["system_id", "enclosure_id"], "a buildScopedUrl read is bound to the target's system and enclosure");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("r9: the extractor reads fetch wrappers, template literals, query helpers, path builders and methods", () => {
  const { extractCalls } = req("qa/ui-crawl/reach");
  const { loadManifest } = req("qa/ui-crawl/routes");
  const manifest = loadManifest();
  const source = [
    "(function () {",
    "  const API_ROOT = \"/api/admin/backups\";",
    "  function selection() { const params = new URLSearchParams(); params.set(\"system_id\", \"a\"); return params; }",
    "  function itemUrl(id, suffix = \"\") { return `${API_ROOT}/${encodeURIComponent(id)}${suffix}`; }",
    "  async function list() { return fetchJson(API_ROOT); }",
    "  async function one(id) { return fetchJson(itemUrl(id)); }",
    "  async function verify(id) { return fetchJson(itemUrl(id, \"/verify\"), { method: \"POST\" }); }",
    "  async function act(key, action) { return fetchJson(`/api/admin/runtime/containers/${key}/${action}`, { method: \"POST\" }); }",
    "  async function state() { const params = selection(); params.set(\"view\", \"x\"); return fetchJson(`/api/admin/state?${params.toString()}`); }",
    "  // fetchJson(\"/api/admin/backups/run\", { method: \"POST\" }) in a comment is not a call",
    "})();",
  ].join("\n");
  const { calls, problems } = extractCalls("admin_service/static/probe.js", source, { service: "admin", manifest });
  assert.deepEqual(problems, []);
  const seen = calls.map((call) => `${call.method} ${call.path} ${call.class} [${call.query.join(",")}]`).sort();
  assert.deepEqual(seen, [
    "GET /api/admin/backups read []",
    "GET /api/admin/backups/{artifact_id} read []",
    "GET /api/admin/state read [system_id,view]",
    "POST /api/admin/backups/{artifact_id}/verify forbidden []",
    "POST /api/admin/runtime/containers/{container_key}/restart forbidden []",
    "POST /api/admin/runtime/containers/{container_key}/start forbidden []",
    "POST /api/admin/runtime/containers/{container_key}/stop forbidden []",
  ]);
  // An /api call no manifest route matches is a generator failure, never dropped.
  const unknown = extractCalls("admin_service/static/probe.js", "async function x() { return fetchJson(\"/api/admin/nowhere\"); }", { service: "admin", manifest });
  assert.match(unknown.problems.join("\n"), /matches no admin route/);
});

test("r9: the generated reach holds only read and read-post routes; every other class page JS reaches stays per-state", () => {
  const { loadReach } = req("qa/ui-crawl/reach");
  const { loadManifest } = req("qa/ui-crawl/routes");
  const reach = loadReach();
  const manifest = loadManifest();
  const classOf = (service, method, routePath) => [...manifest.routes, ...manifest.extra_routes].find((entry) => entry.service === service && entry.method === method && entry.path === routePath)?.class;
  assert.deepEqual(Object.keys(reach.pages).sort(), ["admin", "fabric", "history", "main", "offline"]);
  assert.deepEqual(Object.keys(reach.pages.main.scopes), ["enclosure", "storage-view"]);
  for (const [pageId, page] of Object.entries(reach.pages)) {
    for (const [scope, rules] of Object.entries(page.scopes)) {
      for (const rule of rules) {
        const cls = classOf(page.service, rule.method, rule.path);
        assert.ok(["read", "read-post"].includes(cls), `${pageId}/${scope}: ${rule.method} ${rule.path} is ${cls}`);
        assert.equal(rule.class, cls);
        assert.ok(rule.sites.length > 0, "every generated read names the call sites it came from");
      }
    }
    for (const entry of page.declared_per_state) assert.ok(!["read", "read-post"].includes(entry.class), `${pageId}: ${entry.path} is a plain read listed as per-state`);
  }
  const perState = (pageId) => reach.pages[pageId].declared_per_state.map((entry) => `${entry.class} ${entry.method} ${entry.path}`);
  for (const expected of ["app-write POST /api/slots/{slot}/mapping", "app-write DELETE /api/slots/{slot}/mapping", "app-write POST /api/sas-fabric/aliases",
    "led POST /api/slots/{slot}/led", "read-heavy POST /api/export/enclosure-snapshot", "forbidden POST /api/systems/{system_id}/disk-inventory-sync"]) {
    assert.ok(perState("main").includes(expected), `main: ${expected}`);
  }
  for (const expected of ["appliance-load GET /api/admin/storage-views/candidates", "appliance-load GET /api/admin/storage-views/live-enclosures",
    "appliance-load GET /api/admin/tls/saved-status", "appliance-load POST /api/admin/tls/inspect", "forbidden POST /api/admin/system-setup/bootstrap"]) {
    assert.ok(perState("admin").includes(expected), `admin: ${expected}`);
  }
  assert.deepEqual(reach.pages.offline.scopes.page, [], "the offline copy sends no /api request");
});

// ----------------------------------- Codex PRRT_kwDOSAqK-M61IUDz (r9 fix) --

const viewHistory = (view, query) => `${UI}/api/storage-views/${view}/history?${query}`;
const HEAT_QUERY = "window_hours=8760&event_limit=0&metric_limit=24&metrics=read_rate&metrics=write_rate";

test("Codex PRRT_kwDOSAqK-M61IUDz: app.js reads a selected storage view's heat-map history through /api/storage-views/{view_id}/history", () => {
  const app = fs.readFileSync(path.join(root, "app/static/app.js"), "utf8");
  const fn = app.slice(app.indexOf("function heatmapHistoryScopeRequest("), app.indexOf("function heatmapHistoryScopeRequest(") + 2000);
  assert.match(fn, /\/api\/storage-views\/\$\{encodeURIComponent\(selectedView\.id\)\}\/history\?\$\{params\.toString\(\)\}/);
  const { loadReach } = req("qa/ui-crawl/reach");
  const reach = loadReach();
  const rule = reach.pages.main.scopes["storage-view"].find((entry) => entry.path === VIEW_HISTORY);
  assert.ok(rule, "generated for the storage-view scope");
  assert.equal(rule.method, "GET");
  for (const key of ["system_id", "enclosure_id", "window_hours", "event_limit", "metric_limit", "metrics"]) assert.ok(rule.query.includes(key), key);
  assert.deepEqual(rule.bind_params, { view_id: "storage_view_id" }, "the path's view is bound to the target's storage view");
  assert.ok(rule.sites.some((site) => site.endsWith("#heatmapHistoryScopeRequest")));
  assert.equal(reach.pages.main.scopes.enclosure.some((entry) => entry.path === VIEW_HISTORY), false, "not reachable without a selected storage view");
});

test("Codex PRRT_kwDOSAqK-M61IUDz: main.heatmap.metric and main.heatmap.timeline on a storage-view target admit its heat-map history, bound to the target", () => {
  const bind = { system_id: "sys-a", storage_view_id: "view-a" };
  for (const stateId of ["main.heatmap.metric", "main.heatmap.timeline"]) {
    const g = stateGuard(stateId, "storage-view", bind);
    const ok = get(g, viewHistory("view-a", `system_id=sys-a&${HEAT_QUERY}`));
    assert.equal(ok.allowed, true, `${stateId}: ${ok.reason}`);
    assert.equal(ok.cls, "read");
    // A selected_enclosure_snapshot view also names its backing enclosure.
    assert.equal(get(g, viewHistory("view-a", `system_id=sys-a&enclosure_id=enc-9&${HEAT_QUERY}`)).allowed, true);
    const refused = {
      "another storage view": viewHistory("view-b", `system_id=sys-a&${HEAT_QUERY}`),
      "another system": viewHistory("view-a", `system_id=sys-b&${HEAT_QUERY}`),
      "no system": viewHistory("view-a", HEAT_QUERY),
      "a repeated system key": viewHistory("view-a", `system_id=sys-a&system_id=sys-b&${HEAT_QUERY}`),
      "another storage_view_id key": viewHistory("view-a", `system_id=sys-a&storage_view_id=view-b&${HEAT_QUERY}`),
      "an undeclared query key": viewHistory("view-a", `system_id=sys-a&purge=1&${HEAT_QUERY}`),
      "a forced refresh": viewHistory("view-a", `system_id=sys-a&force=true&${HEAT_QUERY}`),
    };
    for (const [why, url] of Object.entries(refused)) assert.equal(get(g, url).allowed, false, `${stateId}: ${why} must be refused`);
    // On an enclosure target the route is not reachable at all.
    const enclosure = stateGuard(stateId, "enclosure", { system_id: "sys-a", enclosure_id: "enc-1" });
    assert.equal(get(enclosure, viewHistory("view-a", `system_id=sys-a&enclosure_id=enc-1&${HEAT_QUERY}`)).allowed, false);
  }
});

// ------------------------------- generated admission, bound to the target --

test("r9: a read reachable from page JS but never hand-declared is admitted on its page and scope, bound to the target", () => {
  const bind = { system_id: "sys-a", enclosure_id: "enc-1" };
  // main: the mappings export was declared only by main.mapping.notes before.
  const main = stateGuard("main.default", "enclosure", bind);
  const ok = get(main, `${UI}/api/mappings/export?system_id=sys-a&enclosure_id=enc-1`);
  assert.equal(ok.allowed, true, ok.reason);
  assert.equal(get(main, `${UI}/api/mappings/export?system_id=sys-b&enclosure_id=enc-1`).allowed, false, "another system");
  assert.equal(get(main, `${UI}/api/mappings/export?system_id=sys-a&enclosure_id=enc-2`).allowed, false, "another enclosure");
  assert.equal(get(main, `${UI}/api/mappings/export?enclosure_id=enc-1`).allowed, false, "no system (the backend's default system)");
  // main: a bay's history is no longer tied to the history states.
  assert.equal(get(main, `${UI}/api/slots/4/history?system_id=sys-a&enclosure_id=enc-1&window_hours=24`).allowed, true);
  assert.equal(get(main, `${UI}/api/slots/4/history?system_id=sys-a&enclosure_id=enc-2&window_hours=24`).allowed, false);
  // fabric: the per-bay SMART read used to be page-wide and unbound.
  const fabric = stateGuard("fabric.lanes", "enclosure", bind);
  assert.equal(get(fabric, `${UI}/api/slots/4/smart?system_id=sys-a&enclosure_id=enc-1`).allowed, true);
  assert.equal(get(fabric, `${UI}/api/slots/4/smart?system_id=sys-a&enclosure_id=enc-2`).allowed, false, "another enclosure");
  assert.equal(get(fabric, `${UI}/api/slots/4/smart?system_id=sys-b&enclosure_id=enc-1`).allowed, false, "another system");
  // admin: the SSH key list, the runtime status and one backup's metadata are reachable from admin JS.
  const admin = stateGuard("admin.operations", "page", { system_id: "sys-a" });
  for (const url of [`${ADMIN}/api/admin/ssh-keys`, `${ADMIN}/api/admin/runtime`, `${ADMIN}/api/admin/backups/artifact-1`, `${ADMIN}/api/admin/history/systems`]) {
    const verdict = get(admin, url);
    assert.equal(verdict.allowed, true, `${url}: ${verdict.reason}`);
  }
  assert.equal(admin.admit({ method: "POST", url: `${ADMIN}/api/admin/system-setup/sudoers-preview`, body: "{}" }).allowed, true);
  // Storage-view SMART on a view target: bound to its view.
  const view = stateGuard("main.default", "storage-view", { system_id: "sys-a", storage_view_id: "view-a" });
  assert.equal(get(view, `${UI}/api/storage-views/view-a/slots/0/smart?system_id=sys-a`).allowed, true);
  assert.equal(get(view, `${UI}/api/storage-views/view-b/slots/0/smart?system_id=sys-a`).allowed, false);
});

test("r9: a route the page's JS cannot reach is refused, even when the manifest classes it a read", () => {
  const bind = { system_id: "sys-a", enclosure_id: "enc-1" };
  const main = stateGuard("main.default", "enclosure", bind);
  const refused = [
    ["POST", `${UI}/api/history/scopes/bundle?system_id=sys-a&enclosure_id=enc-1`],
    ["POST", `${UI}/api/storage-views/view-a/slots/smart-batch?system_id=sys-a&enclosure_id=enc-1`],
    ["GET", `${UI}/livez`],
    ["GET", `${UI}/healthz`],
    // A storage-view route on an enclosure target.
    ["GET", `${UI}/api/storage-views/view-a/slots/0/smart?system_id=sys-a&enclosure_id=enc-1`],
  ];
  for (const [method, url] of refused) assert.equal(main.admit({ method, url, body: "{}" }).allowed, false, `${method} ${url}`);
  const fabric = stateGuard("fabric.lanes", "enclosure", bind);
  assert.equal(get(fabric, `${UI}/api/history/scope?system_id=sys-a&enclosure_id=enc-1&window_hours=24`).allowed, false, "fabric JS reads no scope history");
  assert.equal(get(fabric, `${UI}/api/mappings/export?system_id=sys-a&enclosure_id=enc-1`).allowed, false);
  const history = stateGuard("history.default", "page", {});
  assert.equal(get(history, `${HISTORY}/api/history/overview`).allowed, true);
  assert.equal(get(history, `${HISTORY}/api/history/slots/1/events`).allowed, false, "a history read no dashboard script sends");
  const admin = stateGuard("admin.operations", "page", {});
  assert.equal(get(admin, `${ADMIN}/livez`).allowed, false);
});

test("r9: the generated reach never admits an appliance load, a write, an LED action or a forbidden route", () => {
  const bind = { system_id: "sys-a", enclosure_id: "enc-1" };
  const main = stateGuard("main.default", "enclosure", bind);
  const refused = [
    ["GET", `${UI}/api/inventory?system_id=sys-a&enclosure_id=enc-1&force=true`],
    ["GET", `${UI}/api/storage-views?system_id=sys-a&enclosure_id=enc-1&force=1`],
    ["GET", `${UI}/api/slots/4/smart?system_id=sys-a&enclosure_id=enc-1&fresh=t`],
    ["POST", `${UI}/api/slots/4/mapping?system_id=sys-a&enclosure_id=enc-1`],
    ["DELETE", `${UI}/api/slots/4/mapping?system_id=sys-a&enclosure_id=enc-1`],
    ["POST", `${UI}/api/sas-fabric/aliases?system_id=sys-a&enclosure_id=enc-1`],
    ["POST", `${UI}/api/slots/4/led?system_id=sys-a&enclosure_id=enc-1`],
    ["POST", `${UI}/api/export/enclosure-snapshot?system_id=sys-a&enclosure_id=enc-1`],
    ["POST", `${UI}/api/systems/sys-a/disk-inventory-sync`],
    ["GET", `${UI}/api/system-locator?system_id=sys-a`],
    ["GET", `${UI}/metrics`],
  ];
  for (const [method, url] of refused) {
    const verdict = main.admit({ method, url, body: "{\"action\":\"IDENTIFY\"}" });
    assert.equal(verdict.allowed, false, `${method} ${url}: ${verdict.reason}`);
  }
  const admin = stateGuard("admin.operations", "page", { system_id: "sys-a" });
  for (const url of [`${ADMIN}/api/admin/storage-views/candidates?system_id=sys-a`, `${ADMIN}/api/admin/storage-views/live-enclosures?system_id=sys-a`, `${ADMIN}/api/admin/tls/saved-status?system_id=sys-a`]) {
    assert.equal(get(admin, url).allowed, false, url);
  }
  // A widened read list cannot change that: the guard reads the class from
  // the manifest, so a write route named as a "read" is never a read.
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const g = new RequestGuard({ origins: { ui: UI } });
  g.beginState("tampered", { reads: [{ method: "POST", path: "/api/slots/{slot}/mapping", query: [] }, { method: "GET", path: "/api/system-locator", query: ["system_id"] }] });
  assert.equal(g.admit({ method: "POST", url: `${UI}/api/slots/4/mapping`, body: "{}" }).allowed, false);
  assert.equal(get(g, `${UI}/api/system-locator?system_id=a`).allowed, false);
});

test("r9: the inventory validates the reach and keeps plain /api reads out of hand declarations (one source of truth)", () => {
  const { loadInventory, validateInventory, readsFor, expandStates } = req("qa/ui-crawl/inventory");
  const { loadReach } = req("qa/ui-crawl/reach");
  const inventory = loadInventory();
  assert.deepEqual(validateInventory(inventory), []);
  for (const [pageId, page] of Object.entries(inventory.pages)) {
    for (const rule of page.reads || []) assert.ok(!rule.path.startsWith("/api/"), `pages.${pageId}.reads declares ${rule.path}; it is generated`);
    assert.equal(((page.selection || {}).reads || []).length, 0, `pages.${pageId}.selection.reads is generated now`);
  }
  for (const state of inventory.states) {
    for (const rule of state.reads || []) assert.ok(!rule.path.startsWith("/api/"), `${state.id} declares ${rule.path}; it is generated`);
  }
  const broken = (mutate, reach = loadReach()) => {
    const copy = loadInventory();
    mutate(copy, reach);
    return validateInventory(copy, { reach }).join("\n");
  };
  assert.match(broken((inv) => { inv.states.find((s) => s.id === "main.history").reads = [{ method: "GET", path: "/api/slots/{slot}/history", query: ["window_hours"] }]; }), /generated/);
  assert.match(broken((inv) => { inv.pages.fabric.reads.push({ method: "GET", path: "/api/inventory", query: [] }); }), /generated/);
  // A reach file that names a non-read route is refused outright.
  const tampered = JSON.parse(JSON.stringify(loadReach()));
  tampered.pages.admin.scopes.page.push({ method: "GET", path: "/api/admin/storage-views/candidates", class: "read", query: ["system_id"], bind: [], sites: ["x"] });
  assert.match(broken(() => {}, tampered), /candidates .*appliance-load/);
  // read-heavy keeps its per-state declaration and budget, and must be reachable from the page.
  assert.match(broken((inv) => { inv.states.find((s) => s.id === "main.export.download").read_requests = [{ method: "POST", path: "/api/export/enclosure-snapshot/estimate" }]; }), /read-post/);
  const states = expandStates(inventory);
  const download = states.find((s) => s.id === "main.export.download");
  assert.deepEqual(download.read_requests, [{ method: "POST", path: "/api/export/enclosure-snapshot" }]);
  // Every state's effective list is its page's documents plus the generated reach of its scope.
  const keys = (state, scope) => readsFor(inventory, state, scope).map((rule) => `${rule.method} ${rule.path}`);
  const heat = states.find((s) => s.id === "main.heatmap.metric");
  assert.ok(keys(heat, "storage-view").includes(`GET ${VIEW_HISTORY}`));
  assert.ok(!keys(heat, "enclosure").includes(`GET ${VIEW_HISTORY}`));
  assert.ok(keys(heat, "*").includes(`GET ${VIEW_HISTORY}`), "the any-scope list is the union (observed-route checks)");
  assert.ok(keys(heat, "enclosure").includes("GET /static/{path:path}"), "the page's own documents stay declared");
});

// ---------------------------------------------------- crawler and fixture --

test("r9: the crawler gives each state the reach of its target's scope kind and binds a storage-view target's view", () => {
  const crawler = fs.readFileSync(path.join(root, "qa/ui-crawl/crawler.js"), "utf8");
  const begins = [...crawler.matchAll(/this\.guard\.beginState\(([^;]+)\);/g)].map((m) => m[1]);
  assert.ok(begins.length >= 2);
  for (const call of begins) assert.match(call, /readsFor\(this\.inventory, state, this\.scopeFor\(target\)\)/, call);
  const { Crawler } = req("qa/ui-crawl/crawler");
  const scopeFor = Crawler.prototype.scopeFor;
  assert.equal(scopeFor.call({}, { page: "main", url: `${UI}/?system_id=sys-a&storage_view_id=view-a` }), "storage-view");
  assert.equal(scopeFor.call({}, { page: "main", url: `${UI}/?system_id=sys-a&enclosure_id=enc-1` }), "enclosure");
  assert.equal(scopeFor.call({}, { page: "main", url: `${UI}/` }), "enclosure");
  assert.equal(scopeFor.call({}, { page: "fabric", url: `${UI}/sas-fabric?storage_view_id=view-a` }), "enclosure");
  assert.equal(scopeFor.call({}, { page: "admin", url: `${ADMIN}/` }), "page");
  const bindFor = Crawler.prototype.bindFor;
  assert.deepEqual(bindFor.call({ fixtureMode: false, writeSystem: null }, { system: "sys-a", url: `${UI}/?system_id=sys-a&storage_view_id=view-a` }),
    { system_id: "sys-a", enclosure_id: null, storage_view_id: "view-a" });
});

test("r9: the CI fixture serves a storage view's heat-map history and crawls a target whose first scope is that view", () => {
  const builder = fs.readFileSync(path.join(root, "scripts/build_ui_crawl_fixture.py"), "utf8");
  assert.match(builder, /ui:\/api\/storage-views\/\{view_id\}\/history/);
  const cli = req("scripts/ui_crawl.js");
  const first = cli.fixtureTargets().find((target) => target.page === "main" && target.scopeIndex === 0 && /[?&]storage_view_id=/.test(target.url));
  assert.ok(first, "a fixture target whose first scope is a storage view");
  const { states } = inventoryStates();
  const ran = cli.statesFor(first, states, { fixture: true, allow: [] }, {}).map((state) => state.id);
  for (const id of ["main.heatmap.metric", "main.heatmap.timeline"]) assert.ok(ran.includes(id), `${id} runs on the view-first target`);
  const withWrites = cli.statesFor(first, states, { fixture: true, allow: ["app-write-revert", "led"] }, {}).map((state) => state.id);
  assert.equal(withWrites.some((id) => ["main.rename.save", "main.mapping.notes", "main.led"].includes(id)), false, "the view-first fixture target runs read states only");
  const spec = fs.readFileSync(path.join(root, "qa/ui-coverage.spec.js"), "utf8");
  assert.match(spec, /read GET \/api\/storage-views\/\{view_id\}\/history/);
  assert.match(spec, /storage_view_id: view \}/, "the browser test binds the view target like a live one");
});

test("r9: a live crawl refuses to start when the committed reach is stale", () => {
  const cli = req("scripts/ui_crawl.js");
  const source = fs.readFileSync(path.join(root, "scripts/ui_crawl.js"), "utf8");
  const preflight = source.slice(source.indexOf("function preflightProblems("), source.indexOf("function preflightProblems(") + 1400);
  assert.match(preflight, /reachProblems\(/);
  assert.equal(typeof cli.preflightProblems, "function");
  const { reachProblems } = req("qa/ui-crawl/reach");
  assert.deepEqual(reachProblems(root), []);
  const dir = tmpDir("stale");
  const file = path.join(dir, "reach.json");
  fs.writeFileSync(file, "{}\n");
  assert.match(reachProblems(root, { file }).join("\n"), /stale/);
  fs.rmSync(dir, { recursive: true, force: true });
});
