"use strict";

// Round-4 safety regressions for the UI coverage crawler (#1108, owner
// decision D-TNJ-20261108-CRAWLER-R5): #1117 monotonic identify lease and
// watchdog liveness supervision, #1127 storage-view heat-map SMART reads, and
// #1128 the declared follow-up window of a forced refresh. (#1119's Archive
// timestamp window lives in the restricted runner and its own tests.) No
// browser, no network beyond 127.0.0.1.

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
const CLOCK_SHIFT = path.join(root, "tests/js/fixtures/ui_crawl_clock_shift.js");

function tmpDir(label) {
  return fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), `uic4-${label}-`));
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Whatever a failing test leaves behind (servers, children, watchdogs) is
// closed at the end, so a red run reports instead of hanging.
const leftovers = [];
test.after(() => {
  for (const close of leftovers) {
    try {
      close();
    } catch {
      // already gone
    }
  }
});

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
    }, 10);
  });
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

function fakeAppliance() {
  const seen = [];
  const lit = new Set();
  const server = http.createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      const url = new URL(request.url, "http://127.0.0.1");
      seen.push({ at: performance.now(), line: `${request.method} ${url.pathname}${url.search} ${body}` });
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
  leftovers.push(() => { server.closeAllConnections(); server.close(); });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, seen, lit, origin: `http://127.0.0.1:${server.address().port}` })));
}

function startChild(appliance, dir, args = [], env = {}) {
  const ledger = path.join(dir, "led-ledger.json");
  const child = spawn(process.execPath, [path.join(root, "tests/js/fixtures/ui_crawl_led_child.js"), appliance.origin, ledger, dir, ...args], {
    stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, ...env },
  });
  const state = { out: "", child, ledger, code: undefined };
  child.stdout.on("data", (chunk) => { state.out += chunk; });
  child.stderr.on("data", (chunk) => { state.out += chunk; });
  leftovers.push(() => {
    const watchdog = /WATCHDOG (\d+)/.exec(state.out);
    if (watchdog) process.kill(Number(watchdog[1]), "SIGKILL");
  });
  leftovers.push(() => child.kill("SIGKILL"));
  state.exited = new Promise((resolve) => child.on("exit", (code, signal) => { state.code = code ?? signal; resolve(state.code); }));
  return state;
}

// The wall-clock fields of every lease move one hour ahead: what an ISO
// deadline looks like after the wall clock stepped back (DayBreak #1117).
function shiftLeaseWallFields(file, ms) {
  const doc = JSON.parse(fs.readFileSync(file, "utf8"));
  doc.armed = doc.armed.map((entry) => ({
    ...entry,
    armedAt: new Date(Date.parse(entry.armedAt) + ms).toISOString(),
    expiresAt: new Date(Date.parse(entry.expiresAt) + ms).toISOString(),
  }));
  fs.writeFileSync(file, JSON.stringify(doc));
}

const CLEAR_LINE = /^POST \/api\/slots\/5\/led\?system_id=sys-a&enclosure_id=enc-1 \{"action":"CLEAR"\}/;

// ------------------------------------------------ #1117 monotonic lease ----

test("#1117 lease expiry follows the monotonic clock; wall-clock fields can only shorten it", () => {
  const { leaseExpired, monoNow, bootId, MAX_LEASE_MS } = req("qa/ui-crawl/ledger");
  const now = monoNow();
  const hour = 3_600_000;
  const fresh = { armedMono: now - 100, leaseMs: 1500, bootId: bootId(), armedAt: new Date().toISOString(), expiresAt: new Date(Date.now() + 1400).toISOString() };
  assert.equal(leaseExpired(fresh), false);
  // A backward wall-clock step (or an edited ISO deadline) cannot extend it.
  const stepped = { ...fresh, armedMono: now - 1600, armedAt: new Date(Date.now() + hour).toISOString(), expiresAt: new Date(Date.now() + 2 * hour).toISOString() };
  assert.equal(leaseExpired(stepped), true);
  assert.equal(leaseExpired(stepped, { wall: Date.now() - hour }), true);
  // A lease longer than the cap is held to the cap.
  assert.equal(leaseExpired({ ...fresh, armedMono: now - MAX_LEASE_MS - 1, leaseMs: 10 * MAX_LEASE_MS }), true);
  // A forward wall step only ends it early (clearing early is safe).
  assert.equal(leaseExpired({ ...fresh, armedAt: new Date(Date.now() - 60_000).toISOString() }), true);
  // No monotonic stamp, another boot, or a stamp from the future: expired (clear it).
  assert.equal(leaseExpired({ ...fresh, armedMono: undefined }), true);
  assert.equal(leaseExpired({ ...fresh, bootId: "another-boot" }), true);
  assert.equal(leaseExpired({ ...fresh, armedMono: now + 60_000 }), true);
});

test("#1117 the watchdog counts as alive only with a fresh monotonic heartbeat from this boot, and IDENTIFY also needs the in-process supervisor", () => {
  const { LedLedger, armedEntry, monoNow, bootId, writeDurable } = req("qa/ui-crawl/ledger");
  const dir = tmpDir("beat");
  const ledger = new LedLedger(path.join(dir, "ledger.json"), { leaseMs: 1500 });
  const beat = (fields) => writeDurable(ledger.watchdogFile, JSON.stringify({ pid: process.pid, ownerPid: process.pid, bootId: bootId(), heartbeatMono: monoNow(), heartbeatAt: new Date().toISOString(), ...fields }));
  beat({});
  assert.equal(ledger.watchdogAlive(), true);
  // The wall-clock stamp says nothing: a backward step makes an old beat look new.
  beat({ heartbeatMono: monoNow() - 10_000, heartbeatAt: new Date(Date.now() + 3_600_000).toISOString() });
  assert.equal(ledger.watchdogAlive(), false, "a stale monotonic heartbeat is not alive");
  beat({ heartbeatAt: new Date(Date.now() - 3_600_000).toISOString() });
  assert.equal(ledger.watchdogAlive(), true, "a fresh monotonic heartbeat is alive whatever the wall clock says");
  beat({ heartbeatMono: monoNow() + 60_000 });
  assert.equal(ledger.watchdogAlive(), false, "a heartbeat from the future is not evidence");
  beat({ bootId: "another-boot" });
  assert.equal(ledger.watchdogAlive(), false);
  beat({ heartbeatMono: undefined });
  assert.equal(ledger.watchdogAlive(), false, "a heartbeat without a monotonic stamp is not evidence");
  // A live watchdog is not enough: the crawler's own supervisor must be running.
  beat({});
  const entry = armedEntry(`${ORIGIN}/api/slots/5/led?system_id=a&enclosure_id=e`, "main.led");
  assert.throws(() => ledger.arm(entry), /supervisor/);
  ledger.supervisor = { alive: () => false };
  assert.throws(() => ledger.arm(entry), /supervisor/);
  ledger.supervisor = { alive: () => true };
  const lease = ledger.arm(entry);
  assert.equal(typeof lease.armedMono, "number");
  assert.equal(lease.bootId, bootId());
  assert.equal(lease.leaseMs, 1500);
});

test("#1117 a watchdog that dies while the crawler is stuck and the wall clock steps back: the crawler's supervisor turns the exact bay off within the cap, then the crawl fails", async () => {
  const appliance = await fakeAppliance();
  const dir = tmpDir("wdkill");
  const flag = path.join(dir, "clock-back");
  const run = startChild(appliance, dir, ["block", "6000"], { NODE_OPTIONS: `--require ${CLOCK_SHIFT}`, UI_CRAWL_TEST_CLOCK_FLAG: flag });
  await waitFor(() => run.out.includes("ARMED"), () => `child never armed: ${run.out}`);
  const armedAt = performance.now();
  const watchdogPid = Number(/WATCHDOG (\d+)/.exec(run.out)[1]);
  assert.equal(appliance.lit.has("5"), true);
  // The wall clock steps back an hour (crawler and watchdog) and the lease's ISO deadline now reads an hour ahead.
  fs.writeFileSync(flag, "");
  shiftLeaseWallFields(run.ledger, 3_600_000);
  await sleep(150); // the crawler's main thread is blocked by now
  process.kill(watchdogPid, "SIGKILL");
  // Lease 1500 ms: off within the cap plus one supervisor tick and its grace.
  await waitFor(() => !appliance.lit.has("5"), () => `still lit: ${run.out}\n${appliance.seen.map((s) => s.line).join("\n")}`, 4000);
  const offAfter = performance.now() - armedAt;
  assert.ok(offAfter < 1500 + 1000, `off ${Math.round(offAfter)} ms after IDENTIFY`);
  assert.equal(pidAlive(run.child.pid), true, "the crawler is still alive");
  assert.equal(run.out.includes("UNBLOCKED"), false, "the crawler's main thread was still stuck when the light went off");
  assert.ok(appliance.seen.some((s) => CLEAR_LINE.test(s.line)), "the exact system/enclosure/bay was cleared");
  // Once it can run again, the crawler fails (it does not carry on without a watchdog).
  const code = await run.exited;
  assert.equal(code, 2, run.out);
  assert.match(run.out, /LOST .*watchdog/);
  assert.equal(appliance.lit.has("5"), false);
  assert.deepEqual(JSON.parse(fs.readFileSync(run.ledger, "utf8")).armed, []);
  appliance.server.close();
});

test("#1117 with crawler and watchdog alive, a backward wall-clock step and an hour-ahead ISO deadline do not extend identify past the cap", async () => {
  const appliance = await fakeAppliance();
  const dir = tmpDir("clock");
  const flag = path.join(dir, "clock-back");
  const run = startChild(appliance, dir, ["hang"], { NODE_OPTIONS: `--require ${CLOCK_SHIFT}`, UI_CRAWL_TEST_CLOCK_FLAG: flag });
  await waitFor(() => run.out.includes("ARMED"), () => `child never armed: ${run.out}`);
  const armedAt = performance.now();
  fs.writeFileSync(flag, "");
  shiftLeaseWallFields(run.ledger, 3_600_000);
  await waitFor(() => !appliance.lit.has("5"), () => `lease did not cap identify: ${run.out}`, 4000);
  assert.ok(performance.now() - armedAt < 1500 + 1000);
  assert.equal(pidAlive(run.child.pid), true, "the crawler is still alive");
  const watchdogPid = Number(/WATCHDOG (\d+)/.exec(run.out)[1]);
  assert.equal(pidAlive(watchdogPid), true, "the watchdog is still alive");
  // The watchdog heartbeats with monotonic stamps from this boot.
  const beat = JSON.parse(fs.readFileSync(`${run.ledger}.watchdog`, "utf8"));
  assert.equal(typeof beat.heartbeatMono, "number");
  assert.equal(beat.bootId, req("qa/ui-crawl/ledger").bootId());
  await waitFor(() => fs.existsSync(path.join(dir, "led-watchdog.json")), () => "no watchdog receipt", 3000);
  const receipt = JSON.parse(fs.readFileSync(path.join(dir, "led-watchdog.json"), "utf8"));
  assert.ok(receipt.cleared.some((item) => item.reason === "lease-expired" && item.verified), JSON.stringify(receipt));
  run.child.kill("SIGTERM");
  await run.exited;
  appliance.server.close();
});

test("#1117 with the watchdog heartbeating but not clearing, the supervisor clears a lease that outlived its cap and the crawl fails; a lease the watchdog is clearing is left to it", async () => {
  const appliance = await fakeAppliance();
  const { LedLedger, armedEntry, monoNow, bootId, writeDurable } = req("qa/ui-crawl/ledger");
  const { startLedSupervisor } = req("qa/ui-crawl/led-supervisor");
  const dir = tmpDir("overrun");
  const ledger = new LedLedger(path.join(dir, "ledger.json"), { leaseMs: 500 });
  // A heartbeating "watchdog" (this process) that never clears anything.
  const beat = () => writeDurable(ledger.watchdogFile, JSON.stringify({ pid: process.pid, ownerPid: process.pid, bootId: bootId(), heartbeatMono: monoNow(), heartbeatAt: new Date().toISOString() }));
  beat();
  const beating = setInterval(beat, 100);
  const events = [];
  const supervisor = await startLedSupervisor({ ledger, intervalMs: 50, graceMs: 200, verifyMs: 1000, onLost: (event) => events.push(event) });
  try {
    // Bay 6 is being cleared by the watchdog (slowly): not an overrun.
    const six = ledger.arm(armedEntry(`${appliance.origin}/api/slots/6/led?system_id=sys-a&enclosure_id=enc-1`, "main.led"));
    ledger.update({ origin: six.origin, path: six.path, system_id: six.system_id, enclosure_id: six.enclosure_id }, { watchdogClearingAt: new Date().toISOString() });
    appliance.lit.add("6");
    ledger.arm(armedEntry(`${appliance.origin}/api/slots/5/led?system_id=sys-a&enclosure_id=enc-1`, "main.led"));
    appliance.lit.add("5");
    const armedAt = performance.now();
    await waitFor(() => !appliance.lit.has("5"), () => `overrun not cleared: ${JSON.stringify(events)}`, 3000);
    assert.ok(performance.now() - armedAt >= 500, "not before the cap");
    await waitFor(() => events.length > 0, () => "no event", 2000);
    assert.equal(events[0].type, "lease-overrun");
    assert.ok(appliance.seen.some((s) => CLEAR_LINE.test(s.line)));
    assert.equal(appliance.lit.has("6"), true, "the lease the watchdog is clearing was left to it");
  } finally {
    clearInterval(beating);
    await supervisor.stop();
    appliance.server.close();
  }
});

// ------------------------------------------ #1127 storage-view SMART reads ----

const VIEW_SMART = "/api/storage-views/{view_id}/slots/{slot_index}/smart";

test("#1127 the storage-view SMART read is declared for the heat-map states that send it, with its exact query keys, and nowhere else", () => {
  const { loadInventory, readsFor, validateInventory, expandStates } = req("qa/ui-crawl/inventory");
  const inventory = loadInventory();
  assert.deepEqual(validateInventory(inventory), []);
  const states = new Map(expandStates(inventory).map((state) => [state.id, state]));
  // Learned with a scratch learning guard on the synthetic saved view
  // (boot-doms): every heat-map state sends it there with exactly these keys
  // (main.heatmap and main.heatmap.temperature on a later view scope; all of
  // them when a view is a system's first scope).
  const senders = ["main.heatmap", "main.heatmap.temperature", "main.heatmap.relative", "main.heatmap.metric", "main.heatmap.timeline", "main.heatmap.scale-max", "main.heatmap.select"];
  for (const id of senders) {
    const rule = readsFor(inventory, states.get(id)).find((read) => read.path === VIEW_SMART);
    assert.ok(rule, `${id} declares ${VIEW_SMART}`);
    assert.equal(rule.method, "GET");
    assert.deepEqual([...rule.query].sort(), ["enclosure_id", "system_id"]);
  }
  // Not on the page, and not on a state that does not send it.
  assert.equal((inventory.pages.main.reads || []).some((read) => read.path === VIEW_SMART), false);
  assert.equal(readsFor(inventory, states.get("main.default")).some((read) => read.path === VIEW_SMART), false);

  const { RequestGuard } = req("qa/ui-crawl/guard");
  const g = new RequestGuard({ origins: { ui: ORIGIN } });
  const url = `${ORIGIN}/api/storage-views/boot-doms/slots/0/smart?system_id=demo-system&enclosure_id=enc-1`;
  g.beginState("main.heatmap", { reads: readsFor(inventory, states.get("main.heatmap")) });
  const ok = g.admit({ method: "GET", url });
  assert.equal(ok.allowed, true, ok.reason);
  assert.equal(ok.cls, "read");
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/storage-views/boot-doms/slots/0/smart?system_id=demo-system` }).allowed, true);
  // fresh=true stays an appliance load, never admitted as a read.
  const fresh = g.admit({ method: "GET", url: `${url}&fresh=true` });
  assert.equal(fresh.allowed, false);
  assert.equal(fresh.cls, "appliance-load");
  assert.equal(g.admit({ method: "GET", url: `${url}&storage_view_id=boot-doms` }).allowed, false, "an undeclared query key");
  // Another state cannot reuse the heat-map state's list.
  g.endState();
  g.beginState("main.default", { reads: readsFor(inventory, states.get("main.default")) });
  assert.equal(g.admit({ method: "GET", url }).allowed, false);
});

test("#1127 the CI fixture crawls a saved storage view through the real target URL, like a live view target", () => {
  const cli = req("scripts/ui_crawl.js");
  const view = cli.fixtureTargets().find((target) => /[?&]storage_view_id=/.test(target.url));
  assert.ok(view, "a fixture target selects a saved storage view");
  assert.equal(view.page, "main");
  assert.ok(view.scopeIndex > 0, "like a live view target, it runs the scope: all states");
  const ran = cli.statesFor(view, req("qa/ui-crawl/inventory").expandStates(req("qa/ui-crawl/inventory").loadInventory()), { fixture: "x", allow: [] }, {}).map((state) => state.id);
  assert.ok(ran.includes("main.heatmap") && ran.includes("main.heatmap.temperature"), ran.join(","));
  // The browser spec crawls the same target and requires the storage-view SMART read to be admitted there.
  const spec = fs.readFileSync(path.join(root, "qa/ui-coverage.spec.js"), "utf8");
  assert.match(spec, /storage_view_id=boot-doms/);
  assert.match(spec, /\/api\/storage-views\/\{view_id\}\/slots\/\{slot_index\}\/smart/);
  // The fixture builder answers the view slots' SMART reads with synthetic data.
  const builder = fs.readFileSync(path.join(root, "scripts/build_ui_crawl_fixture.py"), "utf8");
  assert.match(builder, /storage_view_smart_summary_cache/);
});

// ------------------------------------------- #1128 follow-up window ----

function followState(extra = {}) {
  return {
    id: "main.refresh-now", page: "main", safety: "read", appliance_load: true, follow_up_ms: 5000,
    read_requests: [{ method: "GET", path: "/api/inventory", force: true, max: 1 }, { method: "GET", path: "/api/storage-views", force: true, max: 1 }],
    steps: [{ click: "#refresh-button" }],
    ...extra,
  };
}

test("#1128 a declared forced refresh's delayed follow-up is admitted only inside its declared follow-up window, only for its exact routes and budget", async () => {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { runState } = req("qa/ui-crawl/actuation");
  let clock = 1000;
  const g = new RequestGuard({ origins: { ui: ORIGIN }, now: () => clock });
  const state = followState();
  g.beginState(state.id, { reads: [] });
  const sent = [];
  const driver = {
    guard: g, vars: {}, bind: { system_id: "sys-a", enclosure_id: null }, service: "ui",
    // The click sends the forced inventory read; its answer is slow, so the
    // storage-view follow-up is sent after the steps have returned.
    runSteps: async () => { sent.push(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=sys-a&force=true` })); },
  };
  const outcome = await runState(driver, state, {});
  assert.equal(outcome.status, "done");
  assert.equal(sent[0].allowed, true, sent[0].reason);
  assert.equal(g.window, null, "the state's own request window closed with its steps");
  const follow = `${ORIGIN}/api/storage-views?system_id=sys-a&force=true`;
  clock += 4000;
  const late = g.admit({ method: "GET", url: follow });
  assert.equal(late.allowed, true, late.reason);
  assert.equal(late.cls, "appliance-load");
  // Budget: the declared one storage-view read only; nothing else rides on the window.
  assert.equal(g.admit({ method: "GET", url: follow }).allowed, false, "a second follow-up");
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/inventory?system_id=sys-a&force=true` }).allowed, false, "the inventory budget was spent in the state");
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/sas-fabric?system_id=sys-a&force=true` }).allowed, false, "an undeclared route");
  assert.equal(g.admit({ method: "POST", url: `${ORIGIN}/api/slots/5/led?system_id=sys-a`, body: '{"action":"IDENTIFY"}' }).allowed, false, "a write");
  assert.deepEqual(g.closeFollowUp().map((entry) => entry.path), ["/api/storage-views?system_id=sys-a&force=true"]);
});

test("#1128 outside the follow-up window (expired, another target, or after the next navigation) the follow-up stays a hard failure", async () => {
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const { runState } = req("qa/ui-crawl/actuation");
  const follow = (system = "sys-a") => `${ORIGIN}/api/storage-views?system_id=${system}&force=true`;
  const setup = async () => {
    const box = { clock: 0 };
    const g = new RequestGuard({ origins: { ui: ORIGIN }, now: () => box.clock });
    g.beginState("main.refresh-now", { reads: [] });
    const driver = { guard: g, vars: {}, bind: { system_id: "sys-a", enclosure_id: null }, service: "ui", runSteps: async () => {} };
    await runState(driver, followState(), {});
    return { g, box };
  };
  // Expired: 1 ms past the declared 5000 ms.
  let { g, box } = await setup();
  box.clock = 5001;
  const expired = g.admit({ method: "GET", url: follow() });
  assert.equal(expired.allowed, false);
  assert.match(expired.reason, /follow-up window/);
  // Inside the window, but another system.
  ({ g, box } = await setup());
  box.clock = 10;
  assert.equal(g.admit({ method: "GET", url: follow("sys-b") }).allowed, false);
  // Closed by the next navigation (the crawler closes it when the next page load starts).
  ({ g, box } = await setup());
  g.closeFollowUp();
  assert.equal(g.admit({ method: "GET", url: follow() }).allowed, false);
  // A state that declares no follow-up window gets none.
  const g2 = new RequestGuard({ origins: { ui: ORIGIN }, now: () => 0 });
  g2.beginState("main.refresh-now", { reads: [] });
  await runState({ guard: g2, vars: {}, bind: { system_id: "sys-a", enclosure_id: null }, service: "ui", runSteps: async () => {} }, followState({ follow_up_ms: undefined }), {});
  assert.equal(g2.admit({ method: "GET", url: follow() }).allowed, false);
});

test("#1128 the follow-up window is declared, short and only on forced-refresh read states; the crawler closes it at the next navigation", async () => {
  const { loadInventory, validateInventory, MAX_FOLLOW_UP_MS } = req("qa/ui-crawl/inventory");
  const inventory = loadInventory();
  const refresh = inventory.states.find((state) => state.id === "main.refresh-now");
  assert.ok(Number.isInteger(refresh.follow_up_ms) && refresh.follow_up_ms > 0 && refresh.follow_up_ms <= MAX_FOLLOW_UP_MS, String(refresh.follow_up_ms));
  assert.ok(MAX_FOLLOW_UP_MS <= 60_000, "short");
  const variant = (patch) => {
    const copy = JSON.parse(JSON.stringify(inventory));
    Object.assign(copy.states.find((state) => state.id === "main.refresh-now"), patch);
    return validateInventory(copy).join("\n");
  };
  assert.match(variant({ follow_up_ms: MAX_FOLLOW_UP_MS + 1 }), /follow_up_ms/);
  assert.match(variant({ follow_up_ms: 0 }), /follow_up_ms/);
  assert.match(variant({ follow_up_ms: "30000" }), /follow_up_ms/);
  assert.match(variant({ read_requests: [{ method: "POST", path: "/api/export/enclosure-snapshot" }], appliance_load: false }), /follow_up_ms/);
  const copy = JSON.parse(JSON.stringify(inventory));
  copy.states.find((state) => state.id === "main.default").follow_up_ms = 1000;
  assert.match(validateInventory(copy).join("\n"), /main\.default.*follow_up_ms/);

  // The crawler ends a follow-up window when the next page load starts.
  const { Crawler } = req("qa/ui-crawl/crawler");
  const { RequestGuard } = req("qa/ui-crawl/guard");
  const g = new RequestGuard({ origins: { ui: ORIGIN }, now: () => 0 });
  g.openFollowUp("main.refresh-now", [{ method: "GET", path: "/api/storage-views", force: true, max: 1 }], { bind: { system_id: "sys-a" }, ms: 5000 });
  assert.equal(g.admit({ method: "GET", url: `${ORIGIN}/api/storage-views?system_id=sys-a&force=true` }).allowed, true);
  const crawler = Object.create(Crawler.prototype);
  Object.assign(crawler, { guard: g, inventory, fixtureMode: false, writeSystem: null });
  await crawler.withLoadWindow({ page: "main", label: "t", url: `${ORIGIN}/?system_id=sys-a`, system: "sys-a", platform: "core" }, async () => {
    assert.equal(g.followUp, null, "closed before the next navigation");
  });
  assert.equal(crawler.followUpAdmitted.length, 1);
  assert.equal(crawler.followUpAdmitted[0].state, "main.refresh-now");
});
