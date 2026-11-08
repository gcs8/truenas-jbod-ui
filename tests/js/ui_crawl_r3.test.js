"use strict";

// Round-3 safety regressions for the UI coverage crawler (#1108 after the
// DayBreak-Blue and Sol re-reviews): #1117 identify lease and independent
// watchdog, #1120 query-boolean normalisation, per-state read allowlists, BMC
// locator class and object-bound alias writes, and the QuantaStor startup
// refresh (Sol). No browser, no network beyond 127.0.0.1.

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const ORIGIN = "http://ui.example.test";

function tmpDir(label) {
  return fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), `uic3-${label}-`));
}

function newGuard(origins = { ui: ORIGIN }) {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  return new RequestGuard({ origins });
}

const INVENTORY_READ = { method: "GET", path: "/api/inventory", query: ["system_id", "enclosure_id", "force"] };

// ------------------------------------------------- #1120 (a) force= spellings

test("#1120a every spelling FastAPI reads as true raises a read to appliance-load", () => {
  const { forcesRefresh, parseQueryBool } = req("qa/ui-crawl/guard");
  // Pydantic 2 (FastAPI bool query fields): case-insensitive 1/on/t/true/y/yes.
  for (const value of ["t", "y", "T", "Y", "On", "yes", "1", "TRUE", "%74"]) {
    assert.equal(forcesRefresh(new URL(`${ORIGIN}/api/inventory?force=${value}`)), true, value);
  }
  // Anything that is not a clean false is treated as forced (fail closed),
  // including values FastAPI would refuse and repeated keys (FastAPI reads the last).
  for (const query of ["force=", "force=2", "force=%20true", "force=0&force=1", "force=1&force=0", "fresh=y", "refresh=t"]) {
    assert.equal(forcesRefresh(new URL(`${ORIGIN}/x?${query}`)), true, query);
  }
  for (const value of ["0", "f", "F", "n", "no", "off", "false", "FALSE"]) {
    assert.equal(forcesRefresh(new URL(`${ORIGIN}/api/inventory?force=${value}`)), false, value);
  }
  assert.equal(parseQueryBool("t"), true);
  assert.equal(parseQueryBool("off"), false);
  assert.equal(parseQueryBool(" true"), null);

  const g = newGuard();
  g.beginState("main.default", { reads: [INVENTORY_READ] });
  for (const value of ["t", "y", "On"]) {
    const verdict = g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=a&force=${value}` });
    assert.equal(verdict.allowed, false, value);
    assert.equal(verdict.cls, "appliance-load", value);
  }
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=a&force=false` }).allowed, true);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/slots/3/smart?fresh=y` }).allowed, false);
});

test("#1120a a declared forced read admits every truthy spelling through the same budget", () => {
  const g = newGuard();
  g.beginState("main.refresh-now", { reads: [INVENTORY_READ] });
  g.openReads("main.refresh-now", [{ method: "GET", path: "/api/inventory", force: true, max: 2 }], { classes: ["appliance-load"] });
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?force=t` }).allowed, true);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?force=Y` }).allowed, true);
  assert.match(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?force=true` }).reason, /budget exhausted/);
  assert.equal(g.closeWrites().length, 2);
});

test("#1120a forced reads are only declarable by states that send them", () => {
  const inventory = req("qa/ui-crawl/inventory");
  const check = (state, page = {}) => inventory.validateInventory({ pages: { main: { service: "ui", ...page } }, states: [{ page: "main", covers: [], ...state }] }).join("\n");
  assert.match(check({ id: "a", safety: "read", steps: [], read_requests: [{ method: "GET", path: "/api/inventory", force: true, max: 1 }] }), /appliance-load/);
  // A read allowlist entry can never carry the forced class.
  assert.match(check({ id: "b", safety: "read", steps: [], reads: [{ method: "GET", path: "/api/inventory", query: ["force"], force: true }] }), /reads cannot force/);
});

// ------------------------------------------- #1120 (b) per-state read lists

test("#1120b reads are admitted per state, by exact route and query keys", () => {
  const g = newGuard();
  const admit = (method, p, body) => g.admit({ method, url: `${ORIGIN}${p}`, body });
  // No state: nothing is a read yet.
  assert.equal(admit("GET", "/api/inventory?system_id=a").allowed, false);
  g.beginState("main.default", { reads: [INVENTORY_READ, { method: "GET", path: "/static/{path:path}", query: ["v"] }] });
  assert.equal(admit("GET", "/api/inventory?system_id=a&enclosure_id=e").allowed, true);
  assert.equal(admit("GET", "/static/app.js?v=1").allowed, true);
  // DayBreak's examples: routes main.default never declared.
  assert.match(admit("GET", "/api/mappings/export?system_id=a").reason, /not in the read allowlist of main.default/);
  assert.match(admit("POST", "/api/history/scopes/bundle", "{}").reason, /not in the read allowlist/);
  assert.equal(admit("GET", "/api/system-locator?system_id=a").allowed, false);
  // A declared route with an undeclared query key is not the same read.
  assert.match(admit("GET", "/api/inventory?system_id=a&retention_scope=system").reason, /query key retention_scope/);
  g.endState();
  // Between states the last state's list stays (a timer firing before the next load).
  assert.equal(admit("GET", "/api/inventory?system_id=a").allowed, true);
  g.beginState("main.export.dialog", { reads: [{ method: "POST", path: "/api/export/enclosure-snapshot/estimate", query: ["system_id", "enclosure_id"] }] });
  assert.equal(admit("GET", "/api/inventory?system_id=a").allowed, false, "the next state's list replaces it");
  assert.equal(admit("POST", "/api/export/enclosure-snapshot/estimate?system_id=a", "{}").allowed, true);
});

test("#1120b inventory read lists name exact read routes; every state's effective list is page + state", () => {
  const inventory = req("qa/ui-crawl/inventory");
  const check = (state, page = {}) => inventory.validateInventory({ pages: { main: { service: "ui", ...page } }, states: [{ page: "main", covers: [], ...state }] }).join("\n");
  assert.match(check({ id: "a", safety: "read", steps: [], reads: [{ method: "POST", path: "/api/slots/{slot}/mapping", query: [] }] }), /app-write route; reads may only name read or read-post routes/);
  assert.match(check({ id: "b", safety: "read", steps: [], reads: [{ method: "GET", path: "/api/nope", query: [] }] }), /matches no ui route/);
  assert.match(check({ id: "c", safety: "read", steps: [], reads: [{ method: "GET", path: "/api/inventory" }] }), /query must list/);
  assert.match(check({ id: "d", safety: "read", steps: [], reads: [{ method: "GET", path: "/api/slots/5/history", query: [] }] }), /exact manifest template/);
  const inv = inventory.loadInventory();
  const states = inventory.expandStates(inv);
  const byId = new Map(states.map((state) => [state.id, state]));
  const reads = (id) => inventory.readsFor(inv, byId.get(id)).map((rule) => `${rule.method} ${rule.path}`);
  assert.ok(reads("main.default").includes("GET /api/inventory"));
  assert.ok(!reads("main.default").includes("GET /api/mappings/export"), "export is a control read, not a page read");
  assert.ok(!reads("main.default").includes("POST /api/history/scopes/bundle") || reads("main.history").includes("POST /api/history/scopes/bundle"));
  assert.ok(reads("main.mapping.notes").includes("GET /api/mappings/export"), "the notes probe's semantic read is its own");
  for (const state of states) {
    assert.ok(inventory.readsFor(inv, state).length > 0, `${state.id} has no read allowlist`);
    for (const rule of inventory.readsFor(inv, state)) assert.ok(!rule.path.includes("system-locator"), `${state.id} may read the BMC locator`);
  }
});

test("#1120b a state that read a route outside its list is reported from the observed requests", () => {
  const { checkObserved } = req("qa/ui-crawl/inventory");
  const state = { id: "main.default", safety: "read" };
  const reads = [INVENTORY_READ];
  assert.deepEqual(checkObserved(state, ["read GET /api/inventory"], reads), []);
  assert.match(checkObserved(state, ["read GET /api/mappings/export"], reads).join(), /main.default read GET \/api\/mappings\/export outside its read allowlist/);
});

// ------------------------------------------------- #1120 (c) BMC locator

test("#1120c the BMC locator status read is an appliance query and its switch is never admitted", () => {
  const routes = req("qa/ui-crawl/routes");
  const manifest = routes.loadManifest();
  const find = (method, p) => manifest.routes.find((route) => route.service === "ui" && route.method === method && route.path === p);
  assert.equal(find("GET", "/api/system-locator").class, "appliance-load");
  assert.equal(find("POST", "/api/system-locator").class, "forbidden");
  const inventory = req("qa/ui-crawl/inventory");
  const check = (state) => inventory.validateInventory({ pages: { main: { service: "ui" } }, states: [{ page: "main", covers: [], ...state }] }).join("\n");
  assert.match(check({ id: "a", safety: "read", steps: [], reads: [{ method: "GET", path: "/api/system-locator", query: ["system_id"] }] }), /appliance-load route; reads may only name/);
  assert.match(check({ id: "b", safety: "led", steps: [{ click: "#x" }], revert: [{ click: "#y" }], verify: [{ ledOff: true }],
    writes: [{ method: "POST", path: "/api/system-locator", body_json: { active: true }, max: 1 }] }), /forbidden/);
  const g = newGuard();
  g.beginState("main.default", { reads: [{ method: "GET", path: "/api/system-locator", query: ["system_id"] }] });
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/system-locator?system_id=a` }).allowed, false, "a read list cannot admit an appliance query");
});

// --------------------------------------------- #1120 (d) object-bound alias

const ALIAS_RULE = { method: "POST", path: "/api/sas-fabric/aliases", body_json: { object_kind: "enclosure", scope: "system", object_id: "enc-1" }, max: 2 };

test("#1120d an alias write is bound to the captured object and enclosure; any other object is blocked", () => {
  const g = newGuard();
  g.beginState("main.rename.save", { reads: [] });
  g.openWrites("main.rename.save", [ALIAS_RULE], [], { bind: { system_id: "sys-a", enclosure_id: "enc-1" }, classes: ["app-write"] });
  const alias = (objectId, query = "system_id=sys-a&enclosure_id=enc-1") => g.admit({
    method: "POST", url: `${ORIGIN}/api/sas-fabric/aliases?${query}`,
    body: JSON.stringify({ object_id: objectId, object_kind: "enclosure", label: "probe", scope: "system" }),
  });
  assert.equal(alias("enc-other").allowed, false, "another object in the right scope");
  assert.equal(alias("enc-1", "system_id=sys-a&enclosure_id=enc-2").allowed, false, "the right object in another enclosure");
  assert.equal(alias("enc-1").allowed, true);
  g.closeWrites();
  // A rule for a route that names an object must bind that object.
  assert.throws(() => g.openWrites("x", [{ method: "POST", path: "/api/sas-fabric/aliases", body_json: { object_kind: "enclosure" }, max: 1 }], [], { classes: ["app-write"] }), /must bind body object_id/);
  assert.throws(() => g.openWrites("y", [{ ...ALIAS_RULE, body_json: { ...ALIAS_RULE.body_json, object_id: "{object_id}" } }], [], { classes: ["app-write"] }), /not bound to one object/);
});

test("#1120d the inventory binds the rename probe to the captured enclosure object", () => {
  const inventory = req("qa/ui-crawl/inventory");
  const inv = inventory.loadInventory();
  const rename = inv.states.find((state) => state.id === "main.rename.save");
  const aliasRules = rename.writes.filter((rule) => rule.path === "/api/sas-fabric/aliases");
  assert.ok(aliasRules.length > 0);
  for (const rule of aliasRules) assert.equal(rule.body_json.object_id, "{object_id}");
  assert.ok(rename.capture.some((step) => step.as === "object_id"), "capture reads the enclosure object id");
  const check = (state) => inventory.validateInventory({ pages: { main: { service: "ui" } }, states: [{ page: "main", covers: [], ...state }] }).join("\n");
  assert.match(check({ ...rename, id: "r2", writes: [{ ...aliasRules[0], body_json: { object_kind: "enclosure", scope: "system" } }] }), /must bind body object_id/);
});

test("#1120d a write state with an unbound object or no enclosure scope refuses before writing", async () => {
  const { runState } = req("qa/ui-crawl/actuation");
  const calls = [];
  const driver = {
    guard: newGuard(), vars: {}, bind: { system_id: "sys-a", enclosure_id: "enc-1" }, service: "ui",
    async runSteps(steps) { calls.push(steps[0].label); },
    async semanticSnapshot() { return { "/api/sas-fabric": [] }; },
  };
  const state = {
    id: "main.rename.save", safety: "app-write-revert", capture: [{ wait: 1, label: "capture" }], steps: [{ wait: 1, label: "steps" }],
    revert: [{ wait: 1, label: "revert" }], verify: [{ wait: 1, label: "verify" }], semantic_reads: [{ path: "/api/sas-fabric", pick: "aliases" }],
    writes: [{ ...ALIAS_RULE, body_json: { ...ALIAS_RULE.body_json, object_id: "{object_id}" } }],
  };
  const unbound = await runState(driver, state, { allow: ["app-write-revert"] });
  assert.equal(unbound.status, "precheck-failed");
  assert.match(unbound.error, /not bound to one object/);
  assert.deepEqual(calls, ["capture"], "nothing ran after the refusal");
  const noScope = { ...driver, guard: newGuard(), bind: { system_id: "sys-a", enclosure_id: null } };
  calls.length = 0;
  const refused = await runState(noScope, { ...state, writes: [ALIAS_RULE] }, { allow: ["app-write-revert"] });
  assert.equal(refused.status, "precheck-failed");
  assert.match(refused.error, /no enclosure scope/);
});

test("#1120a a write's trailing forced refresh is admitted during verify with its remaining budget, and nothing else is", async () => {
  const { runState } = req("qa/ui-crawl/actuation");
  const g = newGuard();
  g.beginState("main.rename.save", { reads: [INVENTORY_READ, { method: "GET", path: "/api/storage-views", query: ["system_id", "enclosure_id", "force"] }] });
  const seen = {};
  const driver = {
    guard: g, vars: {}, bind: { system_id: "sys-a", enclosure_id: "enc-1" }, service: "ui",
    async runSteps(steps) {
      const label = steps[0].label;
      const admit = (method, p, body) => g.admit({ method, url: `${ORIGIN}${p}`, body }).allowed;
      if (label === "capture") driver.vars.object_id = "enc-1";
      if (label === "steps" || label === "revert") admit("POST", "/api/sas-fabric/aliases?system_id=sys-a&enclosure_id=enc-1", JSON.stringify({ object_id: "enc-1", object_kind: "enclosure", scope: "system", label: "x" }));
      if (label === "verify") {
        seen.storage = admit("GET", "/api/storage-views?system_id=sys-a&enclosure_id=enc-1&force=true");
        seen.again = admit("GET", "/api/storage-views?system_id=sys-a&enclosure_id=enc-1&force=true");
        seen.write = admit("POST", "/api/sas-fabric/aliases?system_id=sys-a&enclosure_id=enc-1", JSON.stringify({ object_id: "enc-1", object_kind: "enclosure", scope: "system", label: "y" }));
      }
    },
    async semanticSnapshot() { return { "/api/sas-fabric": [] }; },
  };
  const state = {
    id: "main.rename.save", safety: "app-write-revert", capture: [{ wait: 1, label: "capture" }], steps: [{ wait: 1, label: "steps" }],
    revert: [{ wait: 1, label: "revert" }], verify: [{ wait: 1, label: "verify" }], semantic_reads: [{ path: "/api/sas-fabric", pick: "aliases" }],
    writes: [{ ...ALIAS_RULE, body_json: { ...ALIAS_RULE.body_json, object_id: "{object_id}" } }, { method: "GET", path: "/api/storage-views", force: true, max: 1 }],
  };
  const outcome = await runState(driver, state, { allow: ["app-write-revert"] });
  assert.equal(outcome.status, "done", JSON.stringify(outcome));
  assert.deepEqual(seen, { storage: true, again: false, write: false });
  assert.equal(g.window, null);
});

// ------------------------------------------------ Sol: QuantaStor startup

test("Sol: the QuantaStor startup refresh is declared by the page load, bounded and bound", () => {
  const g = newGuard();
  g.beginState("main.default", { reads: [INVENTORY_READ] });
  const forced = (query = "system_id=sys-q&enclosure_id=enc-1") => g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?${query}&force=true` });
  assert.equal(forced().allowed, false, "no load window: the startup refresh is not a read");
  g.openLoad("main.default", [{ method: "GET", path: "/api/inventory", force: true, max: 1, query: ["system_id", "enclosure_id", "force"] }], { bind: { system_id: "sys-q", enclosure_id: "enc-1" } });
  assert.equal(forced("system_id=other&enclosure_id=enc-1").allowed, false, "another system");
  const ok = forced();
  assert.equal(ok.allowed, true);
  assert.equal(ok.cls, "appliance-load");
  assert.match(forced().reason, /budget exhausted/);
  assert.equal(g.closeLoad().length, 1);
  assert.equal(forced().allowed, false, "closed after the page settled");
  // Observed as the page's own load request, which the state's class check accepts.
  const seen = [...g.observed.get("main.default").keys()];
  assert.ok(seen.includes("page-load GET /api/inventory"), seen.join());
  const { checkObserved } = req("qa/ui-crawl/inventory");
  assert.deepEqual(checkObserved({ id: "main.default", safety: "read" }, ["page-load GET /api/inventory"], [INVENTORY_READ]), []);
  assert.match(checkObserved({ id: "main.default", safety: "read" }, ["appliance-load GET /api/inventory"], [INVENTORY_READ]).join(), /not appliance_load/);
  assert.throws(() => g.openLoad("x", [{ method: "POST", path: "/api/slots/1/led", max: 1 }]), /appliance-load/);
});

test("Sol: page load requests are platform-scoped in the inventory and the CI fixtures include a QuantaStor target", () => {
  const inventory = req("qa/ui-crawl/inventory");
  const inv = inventory.loadInventory();
  const load = inv.pages.main.load_requests || [];
  const startup = load.find((rule) => rule.path === "/api/inventory" && rule.force === true);
  assert.ok(startup, "main declares its startup forced inventory read");
  assert.deepEqual(startup.platforms, ["quantastor"]);
  assert.equal(startup.max, 1);
  const follow = load.find((rule) => rule.path === "/api/storage-views" && rule.force === true);
  assert.ok(follow, "and the forced storage-view read its refresh sends after the inventory answers");
  assert.deepEqual(inventory.loadRequestsFor(inv, "main", "quantastor").length, 2);
  assert.deepEqual(inventory.loadRequestsFor(inv, "main", "core"), []);
  const check = (page) => inventory.validateInventory({ pages: { main: { service: "ui", ...page } }, states: [] }).join("\n");
  assert.match(check({ load_requests: [{ method: "GET", path: "/api/inventory", force: true, max: 1 }] }), /load_requests need platforms/);
  assert.match(check({ load_requests: [{ method: "POST", path: "/api/slots/{slot}/led", max: 1, platforms: ["x"] }] }), /load_requests may only declare appliance-load/);
  const cli = req("scripts/ui_crawl");
  const q = cli.fixtureTargets().find((target) => target.variant === "quantastor");
  assert.ok(q, "a QuantaStor fixture target");
  assert.equal(q.platform, "quantastor");
  assert.ok(cli.FIXTURE_ORIGINS["ui-quantastor"]);
  assert.ok(inventory.expandStates(inv).some((state) => state.variant === "quantastor" && state.ci), "a CI state runs on it");
});

test("Sol: the crawler opens the load window around navigation and reloads only for the target platform", async () => {
  const { Crawler } = req("qa/ui-crawl/crawler");
  const inventory = req("qa/ui-crawl/inventory");
  const inv = inventory.loadInventory();
  const g = newGuard();
  g.beginState("main.default", { reads: [INVENTORY_READ] });
  const results = [];
  const page = {
    goto: async (url) => {
      results.push(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=sys-q&force=true` }));
      return { status: () => 200, headers: () => ({}) };
    },
    waitForTimeout: async () => {},
    waitForFunction: async () => {},
    locator: () => ({ count: async () => 0 }),
  };
  const crawler = Object.create(Crawler.prototype);
  Object.assign(crawler, { guard: g, inventory: inv, page, widths: [1600], lastLoad: 0, paceMs: 0, settleMs: 0, readyTimeoutMs: 1, fixtureMode: false, writeSystem: null, log() {}, debug() {}, setWidth: async () => {} });
  await crawler.load({ page: "main", ready: null, url: `${ORIGIN}/?system_id=sys-q`, system: "sys-q", platform: "quantastor" });
  assert.equal(results[0].allowed, true, JSON.stringify(results[0]));
  // The follow-up forced storage-view read may come long after ready; the
  // window stays open (for those declared reads only) until the state ends.
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/storage-views?system_id=sys-q&force=true` }).allowed, true);
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/storage-views?system_id=sys-q&force=true` }).allowed, false, "once per load");
  crawler.endLoadWindow();
  assert.equal(g.load, null, "closed at the end of the state");
  assert.equal(crawler.loadAdmitted.length, 2);
  await crawler.load({ page: "main", ready: null, url: `${ORIGIN}/?system_id=sys-c`, system: "sys-c", platform: "core" });
  assert.equal(results[1].allowed, false, "no load window for another platform");
});

// ------------------------------------------------ #1117 lease and watchdog

function fakeAppliance() {
  const seen = [];
  const lit = new Set();
  const server = http.createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      const url = new URL(request.url, "http://127.0.0.1");
      seen.push({ at: Date.now(), line: `${request.method} ${url.pathname}${url.search} ${body}` });
      const led = /^\/api\/slots\/(\d+)\/led$/.exec(url.pathname);
      response.setHeader("Content-Type", "application/json");
      if (led && request.method === "POST") {
        const action = JSON.parse(body || "{}").action;
        if (action === "IDENTIFY") lit.add(led[1]);
        else lit.delete(led[1]);
        return response.end(JSON.stringify({ ok: true, identify_active: action === "IDENTIFY" }));
      }
      if (url.pathname === "/api/inventory") {
        return response.end(JSON.stringify({ slots: [5, 6].map((slot) => ({ slot, state: "healthy", identify_active: lit.has(String(slot)) })) }));
      }
      response.statusCode = 404;
      return response.end("{}");
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, seen, lit, origin: `http://127.0.0.1:${server.address().port}` })));
}

function waitFor(predicate, message, ms = 15_000) {
  const deadline = Date.now() + ms;
  return new Promise((resolve, reject) => {
    const timer = setInterval(() => {
      if (predicate()) {
        clearInterval(timer);
        resolve();
      } else if (Date.now() > deadline) {
        clearInterval(timer);
        reject(new Error(message()));
      }
    }, 20);
  });
}

function startChild(appliance, dir, mode) {
  const ledger = path.join(dir, "led-ledger.json");
  const child = spawn(process.execPath, [path.join(root, "tests/js/fixtures/ui_crawl_led_child.js"), appliance.origin, ledger, dir, mode], { stdio: ["ignore", "pipe", "pipe"] });
  const state = { out: "", child, ledger };
  child.stdout.on("data", (chunk) => { state.out += chunk; });
  child.stderr.on("data", (chunk) => { state.out += chunk; });
  return state;
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

test("#1117 SIGKILL mid-identify: the independent watchdog turns the exact bay off within the lease cap", async () => {
  const appliance = await fakeAppliance();
  const dir = tmpDir("kill");
  const run = startChild(appliance, dir, "hang");
  await waitFor(() => run.out.includes("ARMED"), () => `child never armed: ${run.out}`);
  const watchdogPid = Number(/WATCHDOG (\d+)/.exec(run.out)[1]);
  assert.equal(appliance.lit.has("5"), true);
  const killedAt = Date.now();
  run.child.kill("SIGKILL");
  await new Promise((resolve) => run.child.on("exit", resolve));
  // The lease in the child is 1500 ms; off must follow within the cap plus a poll.
  await waitFor(() => !appliance.lit.has("5"), () => `still lit after SIGKILL: ${run.out}\n${appliance.seen.map((s) => s.line).join("\n")}`, 6000);
  assert.ok(Date.now() - killedAt < 6000);
  const clears = appliance.seen.filter((s) => /^POST \/api\/slots\/5\/led\?system_id=sys-a&enclosure_id=enc-1 \{"action":"CLEAR"\}/.test(s.line));
  assert.ok(clears.length >= 1, "the watchdog cleared the exact system/enclosure/bay");
  await waitFor(() => !pidAlive(watchdogPid), () => "the watchdog did not exit after the owner died and the ledger emptied", 10_000);
  assert.deepEqual(JSON.parse(fs.readFileSync(run.ledger, "utf8")).armed, []);
  const receipt = JSON.parse(fs.readFileSync(path.join(dir, "led-watchdog.json"), "utf8"));
  assert.equal(receipt.ok, true);
  assert.ok(receipt.cleared.some((entry) => entry.slot === "5" && entry.reason === "owner-gone" || entry.reason === "lease-expired"));
  appliance.server.close();
});

test("#1117 a live but stuck crawler cannot hold identify past its lease", async () => {
  const appliance = await fakeAppliance();
  const dir = tmpDir("stuck");
  const run = startChild(appliance, dir, "hang");
  await waitFor(() => run.out.includes("ARMED"), () => `child never armed: ${run.out}`);
  const armedAt = Date.now();
  await waitFor(() => !appliance.lit.has("5"), () => `lease did not cap identify: ${run.out}`, 6000);
  assert.ok(Date.now() - armedAt < 6000);
  assert.equal(pidAlive(run.child.pid), true, "the crawler is still alive");
  run.child.kill("SIGTERM");
  await new Promise((resolve) => run.child.on("exit", resolve));
  appliance.server.close();
});

test("#1117 IDENTIFY never leaves without a live watchdog, and the ledger commit includes its directory entry", async () => {
  const { LedLedger, armedEntry } = req("qa/ui-crawl/ledger");
  const dir = tmpDir("nowd");
  const ledger = new LedLedger(path.join(dir, "ledger.json"), { leaseMs: 1500 });
  assert.throws(() => ledger.arm(armedEntry(`${ORIGIN}/api/slots/5/led?system_id=a&enclosure_id=e`, "main.led")), /no live identify watchdog/);
  assert.deepEqual(ledger.entries(), []);
  // Directory fsync after the atomic rename (host-loss durability).
  const opened = [];
  const synced = [];
  const realOpen = fs.openSync;
  const realSync = fs.fsyncSync;
  fs.openSync = function patchedOpen(file, ...rest) {
    const fd = realOpen.call(fs, file, ...rest);
    opened.push([fd, String(file)]);
    return fd;
  };
  fs.fsyncSync = function patchedSync(fd) {
    // Descriptor numbers are reused after close: the latest open owns it.
    synced.push(opened.findLast(([candidate]) => candidate === fd)?.[1]);
    return realSync.call(fs, fd);
  };
  try {
    ledger.write([]);
  } finally {
    fs.openSync = realOpen;
    fs.fsyncSync = realSync;
  }
  assert.ok(synced.includes(dir), `parent directory fsynced: ${JSON.stringify(synced)}`);
  // The transport refuses (and records) an IDENTIFY when the ledger cannot arm.
  const { installRoutes } = req("qa/ui-crawl/transport");
  let handler = null;
  const g = newGuard();
  g.beginState("main.led", { reads: [] });
  g.openWrites("main.led", [{ method: "POST", path: "/api/slots/5/led", body_json: { action: "IDENTIFY" }, max: 1 }], [], { classes: ["led"] });
  const stats = { blocked: [], writes: [], routeErrors: [] };
  await installRoutes({ route: async (_p, cb) => { handler = cb; } }, { guard: g, origins: { ui: ORIGIN }, stats, ledger });
  const calls = [];
  await handler({
    request: () => ({ method: () => "POST", url: () => `${ORIGIN}/api/slots/5/led?system_id=a&enclosure_id=e`, postData: () => '{"action":"IDENTIFY"}', headers: () => ({}) }),
    continue: async () => calls.push("continue"), abort: async () => calls.push("abort"), fulfill: async () => calls.push("fulfill"),
  });
  assert.deepEqual(calls, ["abort"]);
  assert.match(stats.routeErrors.join() + stats.blocked.join(), /watchdog/);
});

test("#1117 the startup sweep also clears an expired lease left by a dead owner", async () => {
  const appliance = await fakeAppliance();
  const { LedLedger, sweepLedger } = req("qa/ui-crawl/ledger");
  const dir = tmpDir("expired");
  const file = path.join(dir, "ledger.json");
  appliance.lit.add("6");
  // An entry written by a long-gone run (no watchdog any more).
  fs.writeFileSync(file, JSON.stringify({ armed: [{ origin: appliance.origin, path: "/api/slots/6/led", slot: "6", system_id: "sys-a", enclosure_id: null, ownerPid: 999999, armedAt: "2026-01-01T00:00:00Z", expiresAt: "2026-01-01T00:00:15Z" }] }));
  const out = await sweepLedger(new LedLedger(file), { verifyMs: 1500, pollMs: 100 });
  assert.equal(out.ok, true);
  assert.equal(appliance.lit.has("6"), false);
  assert.deepEqual(new LedLedger(file).entries(), []);
  appliance.server.close();
});
