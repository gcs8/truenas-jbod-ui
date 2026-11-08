"use strict";

// Safety regressions for the UI coverage crawler after the DayBreak-Blue and
// Sol reviews of #1108 (#1117 identify cleanup, #1118 store checks, #1119
// fail-closed exit, #1120 route manifest and bound writes, scanner identity,
// axe failures, grouped findings). No browser, no network beyond 127.0.0.1.

const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const ORIGIN = "http://ui.example.test";

function tmpDir(label) {
  return fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), `uic-${label}-`));
}

// ---------------------------------------------------------------- #1120 ----

test("#1120 every backend route is classified in the reviewed manifest, with a current fingerprint", () => {
  const routes = req("qa/ui-crawl/routes");
  const manifest = routes.loadManifest();
  assert.deepEqual(routes.discoverRouteFiles(root), Object.keys(manifest.sources).sort(), "a Python file declares routes but is not in ui-routes.json sources");
  assert.deepEqual(routes.checkManifest(routes.scanRoutes(root, manifest), manifest), []);
});

test("#1120 a new route, a changed handler and a removed route all fail the manifest check", () => {
  const routes = req("qa/ui-crawl/routes");
  const source = [
    "def build():",
    "    @router.get(\"/api/things\")",
    "    async def things() -> dict:",
    "        return {}",
    "",
  ].join("\n");
  const scanned = { routes: routes.scanRouteSource("x.py", source, "ui").routes, problems: [] };
  const fp = scanned.routes[0].fingerprint;
  const manifest = { routes: [{ service: "ui", method: "GET", path: "/api/things", class: "read", why: "test", fingerprint: fp }] };
  assert.deepEqual(routes.checkManifest(scanned, manifest), []);
  // The same route now writes: its handler text changes, so its class must be re-reviewed.
  const changed = { routes: routes.scanRouteSource("x.py", source.replace("return {}", "store.save()\n        return {}"), "ui").routes, problems: [] };
  assert.match(routes.checkManifest(changed, manifest).join("\n"), /route ui GET \/api\/things changed/);
  const added = { routes: [...scanned.routes, ...routes.scanRouteSource("y.py", "@router.post(\"/api/things/delete\")\nasync def d():\n    pass\n", "ui").routes], problems: [] };
  assert.match(routes.checkManifest(added, manifest).join("\n"), /unclassified route ui POST \/api\/things\/delete/);
  assert.match(routes.checkManifest({ routes: [], problems: [] }, manifest).join("\n"), /stale manifest route/);
});

test("#1120 browser scripts only call /api paths that the manifest knows", () => {
  const routes = req("qa/ui-crawl/routes");
  const controls = req("qa/ui-crawl/controls");
  const manifest = routes.loadManifest();
  assert.deepEqual(routes.checkScriptApiPaths(root, controls.UI_SOURCES.scripts, manifest), []);
  const calls = routes.scanScriptApiPaths("app/static/app.js", "fetchJson(`/api/slots/${slot}/explode`, { method: \"POST\" });");
  assert.equal(calls[0].path, "/api/slots/x/explode");
  const table = new routes.RouteTable(manifest);
  assert.equal(table.match("ui", "POST", calls[0].path), null);
});

function manifestGuard(origins = { ui: ORIGIN }) {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  return new RequestGuard({ origins });
}

test("#1120 the guard admits exact manifest reads only; unknown routes and destructive GETs are blocked", () => {
  const g = manifestGuard();
  const admit = (method, p, body) => g.admit({ method, url: `${ORIGIN}${p}`, body });
  assert.equal(admit("GET", "/api/inventory?system_id=a").allowed, true);
  assert.equal(admit("GET", "/static/app.js").allowed, true);
  assert.equal(admit("POST", "/api/slots/smart-batch", "{}").allowed, true, "read-post");
  assert.match(admit("GET", "/api/never-heard-of-it").reason, /unknown route/);
  assert.equal(admit("GET", "/api/admin/state").allowed, false, "an admin route on the main UI origin is unknown");
  // A GET that the manifest classes as an appliance poll or forbidden is not a read.
  assert.equal(admit("GET", "/api/inventory?force=true").allowed, false);
  assert.equal(admit("GET", "/api/slots/3/smart?fresh=1").allowed, false);
  assert.equal(admit("GET", "/metrics").allowed, false, "internal");
  const admin = manifestGuard({ admin: "http://admin.example.test" });
  assert.equal(admin.admit({ method: "GET", url: "http://admin.example.test/api/admin/backups/abc/download" }).allowed, false, "destructive/secret GET");
  assert.equal(admin.admit({ method: "GET", url: "http://admin.example.test/api/admin/storage-views/candidates" }).allowed, false, "appliance-load GET");
  assert.equal(admin.admit({ method: "GET", url: "http://admin.example.test/api/admin/state" }).allowed, true);
  // Writes never pass without a window.
  assert.equal(admit("POST", "/api/slots/3/led", '{"action":"IDENTIFY"}').allowed, false);
  assert.equal(admit("POST", "/api/systems/a/disk-inventory-sync", "{}").allowed, false);
});

test("#1120 write windows are bound to the write system, enclosure and captured bay", () => {
  const g = manifestGuard();
  const bind = { system_id: "sys-a", enclosure_id: "enc-1" };
  g.openWrites("main.led", [
    { method: "POST", path: "/api/slots/5/led", body_json: { action: "IDENTIFY" }, max: 1 },
    { method: "POST", path: "/api/slots/5/led", body_json: { action: "CLEAR" }, max: 3 },
  ], [], { bind, classes: ["led"] });
  const led = (slot, query, action) => g.admit({ method: "POST", url: `${ORIGIN}/api/slots/${slot}/led?${query}`, body: JSON.stringify({ action }) });
  assert.equal(led(6, "system_id=sys-a&enclosure_id=enc-1", "IDENTIFY").allowed, false, "another bay");
  assert.match(led(5, "system_id=sys-b&enclosure_id=enc-1", "IDENTIFY").reason, /bound system_id/, "another system");
  assert.match(led(5, "system_id=sys-a&enclosure_id=enc-2", "IDENTIFY").reason, /bound enclosure_id/, "another enclosure");
  assert.match(led(5, "enclosure_id=enc-1", "IDENTIFY").reason, /bound system_id \(missing\)/);
  assert.equal(led(5, "system_id=sys-a&enclosure_id=enc-1", "IDENTIFY").allowed, true);
  assert.equal(led(5, "system_id=sys-a&enclosure_id=enc-1", "IDENTIFY").allowed, false, "budget");
  assert.equal(led(5, "system_id=sys-a&enclosure_id=enc-1", "CLEAR").allowed, true);
  assert.equal(g.closeWrites().length, 2);
  // An alias write window does not admit another system's alias.
  g.openWrites("main.rename.save", [{ method: "POST", path: "/api/sas-fabric/aliases", body_json: { object_kind: "enclosure" }, max: 2 }], [], { bind, classes: ["app-write"] });
  const alias = (query) => g.admit({ method: "POST", url: `${ORIGIN}/api/sas-fabric/aliases?${query}`, body: '{"object_kind":"enclosure","label":"x"}' });
  assert.equal(alias("system_id=other&enclosure_id=enc-1").allowed, false);
  assert.equal(alias("system_id=sys-a&enclosure_id=enc-1").allowed, true);
  g.closeWrites();
  // Rules must name one object and one manifest route of an allowed class.
  assert.throws(() => g.openWrites("x", [{ method: "POST", path: "/api/slots/*/led", max: 1 }]), /not bound to one object/);
  assert.throws(() => g.openWrites("y", [{ method: "POST", path: "/api/slots/{slot}/led", max: 1 }]), /not bound to one object/);
  assert.throws(() => g.openWrites("z", [{ method: "POST", path: "/api/systems/a/disk-inventory-sync", max: 1 }], [], { classes: ["app-write"] }), /forbidden, not allowed here/);
  assert.throws(() => g.openWrites("w", [{ method: "POST", path: "/api/nope", max: 1 }]), /matches no ui route/);
});

test("#1120 inventory validation rejects writes outside the state's class and unbound write rules", () => {
  const inventory = req("qa/ui-crawl/inventory");
  const base = { pages: { main: { service: "ui" } }, states: [] };
  const check = (state) => inventory.validateInventory({ ...base, states: [{ page: "main", covers: [], ...state }] }).join("\n");
  const shape = { steps: [{ click: "#x" }], revert: [{ click: "#y" }], verify: [{ wait: 1 }], semantic_reads: [{ path: "/api/mappings/export", pick: "mappings" }] };
  // A "reversible" state that declares a forbidden route is misclassified.
  assert.match(check({ id: "a", safety: "app-write-revert", ...shape, writes: [{ method: "POST", path: "/api/systems/{system}/disk-inventory-sync", max: 1 }] }), /forbidden/);
  assert.match(check({ id: "b", safety: "app-write-revert", ...shape, writes: [{ method: "POST", path: "/api/slots/*/mapping", max: 1 }] }), /glob/);
  assert.match(check({ id: "c", safety: "app-write-revert", ...shape, semantic_reads: [], writes: [{ method: "POST", path: "/api/sas-fabric/aliases", max: 1 }] }), /semantic_reads/);
  assert.match(check({ id: "d", safety: "read", steps: [], read_requests: [{ method: "POST", path: "/api/slots/{slot}/mapping" }] }), /app-write/);
  assert.match(check({ id: "e", safety: "read", fixture_only: true, steps: [] }), /never runs/);
});

test("#1120 a misclassified control is caught from the routes its state actually called", () => {
  const { deriveSafety, checkObserved } = req("qa/ui-crawl/inventory");
  assert.equal(deriveSafety(["read GET /api/inventory", "read-post POST /api/slots/smart-batch"]), "read");
  assert.equal(deriveSafety(["app-write POST /api/slots/{slot}/mapping"]), "app-write-revert");
  assert.equal(deriveSafety(["forbidden POST /api/systems/{system_id}/disk-inventory-sync"]), "forbidden");
  const problems = checkObserved({ id: "main.new-button", safety: "read" }, ["read GET /api/inventory", "app-write POST /api/sas-fabric/aliases"]);
  assert.match(problems.join(), /main.new-button is read but called app-write POST \/api\/sas-fabric\/aliases/);
  assert.deepEqual(checkObserved({ id: "ok", safety: "app-write-revert" }, ["app-write POST /api/sas-fabric/aliases"]), []);
});

function fakeDriver({ dialogs = 1, blockedOnPreview = false, semantic = null, failSemanticRead = false } = {}) {
  const g = manifestGuard();
  const driver = {
    guard: g, vars: {}, dialogsSeen: 0, acceptNextDialog: false, calls: [], bind: { system_id: "sys-a", enclosure_id: "enc-1" }, service: "ui",
    async pause() {},
    async runSteps(steps) {
      const label = steps[0] && steps[0].label;
      driver.calls.push(label);
      if (label === "capture") driver.vars.slot = "5";
      if (label === "steps") g.admit({ method: "POST", url: `${ORIGIN}/api/slots/5/mapping?system_id=sys-a&enclosure_id=enc-1`, body: '{"notes":"probe"}' });
      if (label === "revert") g.admit({ method: "DELETE", url: `${ORIGIN}/api/slots/5/mapping?system_id=sys-a&enclosure_id=enc-1&expected_revision=${"0".repeat(64)}` });
      if (label === "preview") {
        driver.dialogsSeen += dialogs;
        if (blockedOnPreview) g.admit({ method: "POST", url: `${ORIGIN}/api/systems/sys-a/disk-inventory-sync`, body: "{}" });
      }
    },
    snapshots: 0,
    async semanticSnapshot() {
      driver.snapshots += 1;
      if (failSemanticRead) throw new Error("503");
      return driver.snapshots === 1 ? { "/api/mappings/export": [] } : { "/api/mappings/export": semantic ?? [] };
    },
  };
  return driver;
}

const labelled = (label) => [{ wait: 1, label }];
const NOTES_STATE = {
  id: "main.mapping.notes", safety: "app-write-revert", capture: labelled("capture"), steps: labelled("steps"), revert: labelled("revert"), verify: labelled("verify"),
  semantic_reads: [{ path: "/api/mappings/export", pick: "mappings" }],
  writes: [
    { method: "POST", path: "/api/slots/{slot}/mapping", max: 1 },
    { method: "DELETE", path: "/api/slots/{slot}/mapping", max: 3 },
  ],
};

test("#1118 a revert that leaves the store semantically different is a revert failure", async () => {
  const { runState } = req("qa/ui-crawl/actuation");
  const clean = await runState(fakeDriver(), NOTES_STATE, { allow: ["app-write-revert"] });
  assert.equal(clean.status, "done");
  assert.equal(clean.semanticChecked, true);
  const dirty = await runState(fakeDriver({ semantic: [{ slot: 5, notes: "probe" }] }), NOTES_STATE, { allow: ["app-write-revert"] });
  assert.equal(dirty.status, "revert-failed");
  assert.match(dirty.error, /store differs after the revert: \/api\/mappings\/export/);
  const unread = fakeDriver({ failSemanticRead: true });
  const refused = await runState(unread, NOTES_STATE, { allow: ["app-write-revert"] });
  assert.equal(refused.status, "precheck-failed");
  assert.deepEqual(unread.calls, ["capture"], "nothing is written when the store cannot be read first");
});

test("#1120 a forbidden preview needs a dismissed dialog and no attempted request", async () => {
  const { runState } = req("qa/ui-crawl/actuation");
  const state = { id: "main.disk-sync", safety: "forbidden", confirm_preview: true, reason: "x", steps: labelled("preview") };
  assert.equal((await runState(fakeDriver(), state, { confirmPreview: true })).status, "previewed");
  const silent = await runState(fakeDriver({ dialogs: 0 }), state, { confirmPreview: true });
  assert.equal(silent.status, "preview-failed");
  assert.match(silent.error, /no confirmation dialog/);
  const leaky = await runState(fakeDriver({ blockedOnPreview: true }), state, { confirmPreview: true });
  assert.equal(leaky.status, "preview-failed");
});

// ---------------------------------------------------------------- #1119 ----

test("#1119 the gate fails closed on every unsafe outcome", () => {
  const { evaluateGate } = req("qa/ui-crawl/gate");
  assert.equal(evaluateGate({}).exitCode, 0);
  const done = { state: "s", status: "done", admitted: [{ method: "POST", cls: "app-write" }] };
  assert.equal(evaluateGate({ actuations: [done], stats: { writes: ["POST /x"] } }).exitCode, 0);
  for (const status of ["revert-failed", "failed", "precheck-failed", "preview-failed", "weird-new-status"]) {
    assert.equal(evaluateGate({ actuations: [{ state: "s", status }] }).exitCode, 2, status);
  }
  assert.equal(evaluateGate({ stats: { blocked: ["GET /api/x (unknown route)"] } }).exitCode, 2, "blocked request");
  assert.equal(evaluateGate({ stats: { writes: ["POST /api/sas-fabric/aliases"] } }).exitCode, 2, "unexpected write");
  assert.equal(evaluateGate({ problems: ["report missing"] }).exitCode, 2);
  assert.equal(evaluateGate({ records: [{ status: "error" }] }).exitCode, 1);
  // A state that ran but whose capture or checker failed has no evidence (Codex #1108 P2).
  const captured = (type) => [{ target: "t", state: "s", status: "done", findings: [], widths: [{ width: 1600, findings: [{ type, severity: "error" }] }] }];
  assert.equal(evaluateGate({ records: captured("capture-error") }).exitCode, 1);
  assert.equal(evaluateGate({ records: captured("check-error") }).exitCode, 1);
  assert.equal(evaluateGate({ records: captured("contrast") }).exitCode, 0, "product findings are the report's business, not the exit status");
  assert.equal(evaluateGate({ ledCleanup: { ok: false } }).exitCode, 3);
});

test("#1119 merge exits non-zero for a missing or malformed chunk report", () => {
  const dir = tmpDir("merge");
  const good = path.join(dir, "good");
  const bad = path.join(dir, "bad");
  const missing = path.join(dir, "missing");
  fs.mkdirSync(good);
  fs.mkdirSync(bad);
  fs.writeFileSync(path.join(good, "findings.json"), JSON.stringify({ meta: { started: "a", finished: "b", widths: [1600] }, records: [], actuations: [], guard: [], stats: { blocked: [], writes: [], routeErrors: [] } }));
  fs.writeFileSync(path.join(bad, "findings.json"), "{ not json");
  const run = (dirs) => spawnSync(process.execPath, [path.join(root, "scripts/ui_crawl.js"), "--merge", dirs.join(","), "--out", path.join(dir, "out")], { encoding: "utf8" });
  assert.equal(run([good]).status, 0);
  assert.equal(run([good, missing]).status, 2);
  assert.equal(run([good, bad]).status, 2);
  const receipt = JSON.parse(fs.readFileSync(path.join(dir, "out", "findings.json"), "utf8"));
  assert.equal(receipt.safety.status, "safety-failed");
  // A chunk whose own gate failed fails the merge too.
  fs.writeFileSync(path.join(bad, "findings.json"), JSON.stringify({ meta: {}, records: [], actuations: [{ state: "main.led", status: "revert-failed" }], guard: [], stats: {} }));
  assert.equal(run([good, bad]).status, 2);
});

// ---------------------------------------------------------------- #1117 ----

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

function fakeAppliance({ verifyOn = false, refuseClear = false } = {}) {
  const seen = [];
  const lit = new Set();
  const server = http.createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      const url = new URL(request.url, "http://127.0.0.1");
      seen.push(`${request.method} ${url.pathname}${url.search} ${body}`);
      const led = /^\/api\/slots\/(\d+)\/led$/.exec(url.pathname);
      response.setHeader("Content-Type", "application/json");
      if (led && request.method === "POST") {
        const action = JSON.parse(body || "{}").action;
        if (action === "CLEAR" && refuseClear) {
          response.statusCode = 503;
          return response.end("{}");
        }
        if (action === "IDENTIFY") lit.add(led[1]);
        else lit.delete(led[1]);
        return response.end(JSON.stringify({ ok: true, identify_active: action === "IDENTIFY" }));
      }
      if (url.pathname === "/api/inventory") {
        const slots = [5, 6].map((slot) => ({ slot, state: "healthy", identify_active: verifyOn || lit.has(String(slot)) }));
        return response.end(JSON.stringify({ slots }));
      }
      response.statusCode = 404;
      return response.end("{}");
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, seen, lit, origin: `http://127.0.0.1:${server.address().port}` })));
}

test("#1117 an interrupted crawl clears the exact armed bay on SIGTERM and records it", async () => {
  const appliance = await fakeAppliance();
  const dir = tmpDir("sigterm");
  const ledger = path.join(dir, "led-ledger.json");
  const child = spawn(process.execPath, [path.join(root, "tests/js/fixtures/ui_crawl_led_child.js"), appliance.origin, ledger, dir], { stdio: ["ignore", "pipe", "pipe"] });
  let out = "";
  child.stdout.on("data", (chunk) => { out += chunk; });
  child.stderr.on("data", (chunk) => { out += chunk; });
  await waitFor(() => out.includes("ARMED"), () => `child never armed: ${out}`);
  assert.equal(appliance.lit.has("5"), true, "the light was on when the crawl was interrupted");
  child.kill("SIGTERM");
  const code = await new Promise((resolve) => child.on("exit", (c) => resolve(c)));
  appliance.server.close();
  assert.equal(appliance.lit.has("5"), false, out);
  assert.ok(appliance.seen.some((line) => /^POST \/api\/slots\/5\/led\?system_id=sys-a&enclosure_id=enc-1 \{"action":"CLEAR"\}/.test(line)), appliance.seen.join("\n"));
  assert.equal(code, 143, out);
  const receipt = JSON.parse(fs.readFileSync(path.join(dir, "receipt.json"), "utf8"));
  assert.equal(receipt.ledCleanup.ok, true);
  assert.equal(receipt.signal, "SIGTERM");
  assert.deepEqual(JSON.parse(fs.readFileSync(ledger, "utf8")).armed, []);
});

test("#1117 after SIGKILL the startup sweep clears the light before anything else runs, and fails closed when it cannot", async () => {
  const appliance = await fakeAppliance();
  const dir = tmpDir("sigkill");
  const ledger = path.join(dir, "led-ledger.json");
  const child = spawn(process.execPath, [path.join(root, "tests/js/fixtures/ui_crawl_led_child.js"), appliance.origin, ledger, dir], { stdio: ["ignore", "pipe", "pipe"] });
  let out = "";
  child.stdout.on("data", (chunk) => { out += chunk; });
  child.stderr.on("data", (chunk) => { out += chunk; });
  await waitFor(() => out.includes("ARMED"), () => `child never armed: ${out}`);
  child.kill("SIGKILL");
  await new Promise((resolve) => child.on("exit", resolve));
  assert.equal(appliance.lit.has("5"), true, "SIGKILL skips in-process cleanup");
  assert.equal(JSON.parse(fs.readFileSync(ledger, "utf8")).armed.length, 1);
  const sweep = (args) => new Promise((resolve) => {
    const proc = spawn(process.execPath, [path.join(root, "scripts/ui_crawl.js"), "--led-sweep", "--led-ledger", ledger, "--out", path.join(dir, "sweep"), ...args], { stdio: ["ignore", "pipe", "pipe"] });
    let text = "";
    proc.stdout.on("data", (chunk) => { text += chunk; });
    proc.stderr.on("data", (chunk) => { text += chunk; });
    proc.on("exit", (status) => resolve({ status, text }));
  });
  const ok = await sweep(["--led-verify-ms", "2000"]);
  assert.equal(ok.status, 0, ok.text);
  assert.equal(appliance.lit.has("5"), false);
  assert.deepEqual(JSON.parse(fs.readFileSync(ledger, "utf8")).armed, []);
  appliance.server.close();
  // A light that will not read back off keeps its ledger entry and exits 3.
  const stuck = await fakeAppliance({ verifyOn: true });
  const { LedLedger } = req("qa/ui-crawl/ledger");
  new LedLedger(ledger).arm({ origin: stuck.origin, path: "/api/slots/6/led", slot: "6", system_id: "sys-a", enclosure_id: null });
  const failed = await sweep(["--led-verify-ms", "1500"]);
  assert.equal(failed.status, 3, failed.text);
  assert.equal(JSON.parse(fs.readFileSync(ledger, "utf8")).armed.length, 1);
  // A normal run refuses to crawl while the previous light is unverified.
  const crawl = await new Promise((resolve) => {
    const proc = spawn(process.execPath, [path.join(root, "scripts/ui_crawl.js"), "--ui", "http://127.0.0.1:9", "--out", path.join(dir, "crawl"), "--led-ledger", ledger, "--led-verify-ms", "1500"], { stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, BROWSER_CDP_URL: "" } });
    let text = "";
    proc.stderr.on("data", (chunk) => { text += chunk; });
    proc.on("exit", (status) => resolve({ status, text }));
  });
  stuck.server.close();
  assert.equal(crawl.status, 3, crawl.text);
  const receipt = JSON.parse(fs.readFileSync(path.join(dir, "crawl", "receipt.json"), "utf8"));
  assert.equal(receipt.stage, "startup sweep");
});

// ------------------------------------------------------------------ Sol ----

test("Sol 1: a new tab that reuses an existing data hook still needs its own inventory entry", () => {
  const controls = req("qa/ui-crawl/controls");
  const before = `<nav><button data-admin-view-button="setup">Setup</button><button data-admin-view-button="backups">Backups</button></nav>`;
  const after = `${before}<button data-admin-view-button="review-new-tab">New tab</button>`;
  const inventory = { states: [{ id: "admin", covers: ["[data-admin-view-button=setup]", "[data-admin-view-button=backups]"] }] };
  assert.deepEqual(controls.coverageReport(controls.scanTemplate("t.html", before), inventory).uncovered, []);
  const report = controls.coverageReport(controls.scanTemplate("t.html", after), inventory);
  assert.deepEqual(report.uncovered.map((c) => c.key), ["[data-admin-view-button=review-new-tab]"]);
  // Script-rendered finite values are keyed the same way; dynamic ones stay shared.
  const script = "x.innerHTML = `<button data-mode=\"lanes\">L</button><button data-mode=\"disk\">D</button><button data-slot=\"${n}\">B</button>`;";
  assert.deepEqual(controls.scanScript("s.js", script).map((c) => c.key), ["[data-mode=lanes]", "[data-mode=disk]", "[data-slot]"]);
  // Semantic controls without a native control tag are discovered.
  const roles = controls.scanTemplate("r.html", `<div id="review-dummy" role="button" tabindex="0">Do</div><span role="tab" data-tab="x">X</span><div tabindex="0" class="card">focusable</div>`);
  assert.deepEqual(roles.map((c) => c.key), ["#review-dummy", "[data-tab=x]", ".card"]);
});

test("Sol 2: every inventory state is selected by at least one runner", () => {
  const inventory = req("qa/ui-crawl/inventory");
  const inv = inventory.loadInventory();
  const states = inventory.expandStates(inv);
  const ci = new Set(inventory.selectStates(states, { ciOnly: true, includeFixtureOnly: true }).map((s) => s.id));
  const live = new Set(inventory.selectStates(states, { includeFixtureOnly: false }).map((s) => s.id));
  const never = states.filter((s) => !ci.has(s.id) && !live.has(s.id)).map((s) => s.id);
  assert.deepEqual(never, []);
  for (const id of ["main.storage-views-error", "main.write-sign-in", "fabric.write-sign-in"]) assert.ok(ci.has(id), id);
});

test("Sol 3: an axe execution failure is an error the CI gate refuses", async () => {
  const { Crawler } = req("qa/ui-crawl/crawler");
  const { unexplainedErrors } = req("qa/ui-crawl/report");
  const crawler = Object.create(Crawler.prototype);
  Object.assign(crawler, {
    axeSource: "/* axe */", axeWidths: [1600], checkOptions: {},
    page: {
      evaluate: async (fn) => {
        const text = String(fn);
        if (text.includes("pageChecks") || text.includes("findings")) return { findings: [], heat: null, document: null, viewport: null, scanned: 0 };
        if (text.includes("typeof window.axe")) return true;
        if (text.includes("axe.run")) throw new Error("axe.run rejected");
        return { findings: [], heat: null, document: null, viewport: null, scanned: 0 };
      },
    },
  });
  const checks = await crawler.runChecks(1600, { id: "s" });
  const failure = checks.findings.find((f) => f.type === "check-error");
  assert.equal(failure.severity, "error");
  const records = [{ target: "t", state: "s", findings: [], widths: [{ width: 1600, findings: checks.findings }] }];
  assert.equal(unexplainedErrors(records).length, 1);
});

test("Sol 4: the index groups repeated findings with counts, examples and filters", () => {
  const { groupFindings, renderHtml, summarize } = req("qa/ui-crawl/report");
  const records = [];
  for (let i = 0; i < 300; i += 1) {
    records.push({
      target: `sys/main/enc-${i % 3}`, state: `main.select[healthy]`, safety: "read", status: "done", findings: [],
      widths: [{ width: 1600, findings: [
        { type: "axe-label-content-name-mismatch", severity: "error", known: 1110, selector: `#slot-grid > button:nth-child(${i + 1})`, rect: { x: 0, y: 0, w: 10, h: 10 } },
        { type: "contrast", severity: "error", selector: "#title" },
      ], tiles: [{ file: `t${i}.png`, x: 0, y: 0, width: 1280, height: 720 }] }],
    });
  }
  const groups = groupFindings(records);
  const axe = groups.filter((g) => g.type === "axe-label-content-name-mismatch");
  assert.equal(axe.length, 1, JSON.stringify(axe.map((g) => g.pattern)));
  assert.equal(axe[0].count, 300);
  assert.ok(axe[0].examples.length >= 1 && axe[0].examples.length <= 3);
  assert.equal(groups.find((g) => g.type === "contrast").count, 300);
  const html = renderHtml({ meta: { mode: "live" }, summary: summarize(records), records, actuations: [], safety: { status: "ok", failures: [] } });
  assert.match(html, /id="filter-severity"/);
  assert.match(html, /id="filter-known"/);
  assert.ok((html.match(/axe-label-content-name-mismatch/g) || []).length < 20, "per-instance lists are not repeated 300 times");
});
