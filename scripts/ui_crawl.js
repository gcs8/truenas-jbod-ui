#!/usr/bin/env node
"use strict";

// UI coverage crawler CLI (#ui-coverage). See CONTRIBUTING.md, UI coverage.
//
// Synthetic fixture (no stack):
//   python scripts/build_ui_crawl_fixture.py --output "$TMP/fixture.json"
//   node scripts/ui_crawl.js --fixture "$TMP/fixture.json" --out "$TMP/crawl" --ci
// Live stack (read-only unless --allow names more classes):
//   node scripts/ui_crawl.js --ui http://127.0.0.1:8080 --history http://127.0.0.1:8081 --out "$TMP/crawl"
// A remote browser is used when the environment variable named by --cdp-env
// (default BROWSER_CDP_URL) is set; its value is never printed.
//
// Identify cleanup (#1117): a live run that may turn on a locate light needs
// --led-ledger FILE (keep it outside the run's --out, the same file for every
// run). Every IDENTIFY is a lease in that file (--led-lease-ms, default 20 s,
// at most 120 s), and an independent watchdog process (qa/ui-crawl/
// led-watchdog.js, started detached before the crawl) turns the bay off when
// the lease expires or this process disappears, SIGKILL included; no
// IDENTIFY leaves without a live, heartbeating watchdog. Lease expiry and
// heartbeat age use the monotonic clock, so a wall-clock step cannot extend
// identify time. A supervisor thread in this process (qa/ui-crawl/
// led-supervisor.js) watches the watchdog after arming: if it stops
// heartbeating, this process turns its lights off itself and the crawl fails
// (safety-failed). Lights are also cleared at
// startup, on SIGTERM/SIGINT/SIGHUP, after a browser disconnect, at exit, and by
//   node scripts/ui_crawl.js --led-sweep --led-ledger FILE --out DIR
// Exit status (#1119): 0 ok, 1 crawl errors, 2 safety gate failed, 3 a light
// could not be verified off; 128+n after a signal whose cleanup verified.

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { loadInventory, validateInventory, expandStates, selectStates } = require("../qa/ui-crawl/inventory");
const controls = require("../qa/ui-crawl/controls");
const routes = require("../qa/ui-crawl/routes");
const { LedLedger, sweepLedger } = require("../qa/ui-crawl/ledger");
const { startLedSupervisor } = require("../qa/ui-crawl/led-supervisor");
const { chromium } = require("@playwright/test");
const { EXIT } = require("../qa/ui-crawl/gate");
const { RequestGuard } = require("../qa/ui-crawl/guard");
const { installRoutes, fixtureHandler, ReadCache } = require("../qa/ui-crawl/transport");
const { reachProblems } = require("../qa/ui-crawl/reach");
const { Crawler, viewBoundEnclosure } = require("../qa/ui-crawl/crawler");
const { writeReport, mergeReports } = require("../qa/ui-crawl/report");

const FIXTURE_ORIGINS = {
  ui: "https://ui.synthetic.invalid",
  history: "https://history.synthetic.invalid",
  admin: "https://admin.synthetic.invalid",
  offline: "https://offline.synthetic.invalid",
  // build_current_source_browser_fixture renders static URLs on this host.
  assets: "https://synthetic.invalid",
  // Fixture variants for states that need another configuration (Sol #1108).
  "ui-write-sign-in": "https://ui-write-sign-in.synthetic.invalid",
  "ui-storage-views-error": "https://ui-storage-views-error.synthetic.invalid",
  // A QuantaStor system with LED-capable bays: the page sends a forced
  // inventory read at startup (Sol #1108), declared in pages.main.load_requests.
  "ui-quantastor": "https://ui-quantastor.synthetic.invalid",
};
const WATCHDOG_SCRIPT = path.join(__dirname, "..", "qa", "ui-crawl", "led-watchdog.js");
const REPO_ROOT = path.resolve(__dirname, "..");
// Live actuation stays off until these are fixed: a signal during a committed
// write can skip its revert (#1130), and a delayed IDENTIFY can relight an LED
// after cleanup drops its lease (#1131). Fixture crawls still exercise both.
const LIVE_ACTUATION_DISABLED = Object.freeze({ "app-write-revert": "#1130", led: "#1131" });

function parseArgs(argv) {
  const args = { pages: null, states: null, widths: null, systems: null, allow: [], paceMs: null, cdpEnv: "BROWSER_CDP_URL", axe: true };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    const next = () => argv[++i];
    if (flag === "--fixture") args.fixture = next();
    else if (flag === "--ui") args.ui = next().replace(/\/$/, "");
    else if (flag === "--history") args.history = next().replace(/\/$/, "");
    else if (flag === "--admin") args.admin = next().replace(/\/$/, "");
    else if (flag === "--out") args.out = next();
    else if (flag === "--pages") args.pages = next().split(",");
    else if (flag === "--states") args.states = next().split(",");
    else if (flag === "--widths") args.widths = next().split(",").map(Number);
    else if (flag === "--systems") args.systems = next().split(",");
    else if (flag === "--allow") args.allow = next().split(",").filter(Boolean);
    else if (flag === "--write-system") args.writeSystem = next();
    else if (flag === "--confirm-preview") args.confirmPreview = true;
    else if (flag === "--downloads") args.downloads = true;
    else if (flag === "--baseline") args.baseline = next();
    else if (flag === "--ci") args.ci = true;
    else if (flag === "--tier") args.tier = next();
    else if (flag === "--no-axe") args.axe = false;
    else if (flag === "--pace-ms") args.paceMs = Number(next());
    else if (flag === "--cdp-env") args.cdpEnv = next();
    else if (flag === "--label") args.label = next();
    else if (flag === "--appliance-load") args.applianceLoad = true;
    else if (flag === "--scopes") args.scopes = next();
    else if (flag === "--merge") args.merge = next().split(",");
    else if (flag === "--led-ledger") args.ledLedger = next();
    else if (flag === "--led-sweep") args.ledSweep = true;
    else if (flag === "--led-verify-ms") args.ledVerifyMs = Number(next());
    else if (flag === "--led-lease-ms") args.ledLeaseMs = Number(next());
    else throw new Error(`unknown argument ${flag}`);
  }
  if (!args.out) throw new Error("--out is required");
  if (args.merge) return args;
  if (args.ledSweep) {
    if (!args.ledLedger) throw new Error("--led-sweep needs --led-ledger");
    return args;
  }
  if (!args.fixture && !args.ui) throw new Error("give --fixture or --ui");
  if (args.allow.includes("forbidden")) throw new Error("forbidden states are never actuated");
  for (const cls of args.allow) if (!["app-write-revert", "led"].includes(cls)) throw new Error(`unknown class ${cls}`);
  if (args.allow.length && !args.fixture && !args.writeSystem) throw new Error("--allow on a live stack needs --write-system");
  if (args.allow.includes("led") && !args.fixture && !args.ledLedger) throw new Error("--allow led on a live stack needs --led-ledger (crash-safe identify cleanup)");
  const disabled = args.fixture ? [] : args.allow.filter((cls) => LIVE_ACTUATION_DISABLED[cls]);
  if (disabled.length) {
    throw new Error(`live ${disabled.join(" and ")} actuation is disabled until ${disabled.map((cls) => LIVE_ACTUATION_DISABLED[cls]).join(" and ")} ${disabled.length > 1 ? "are" : "is"} fixed; run the live crawl read-only (drop --allow) or use --fixture`);
  }
  return args;
}

async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(300_000) });
  if (!response.ok) throw new Error(`${response.status} for ${new URL(url).pathname}`);
  return response.json();
}

async function liveTargets(args) {
  const html = await (await fetch(`${args.ui}/`, { signal: AbortSignal.timeout(300_000) })).text();
  const select = /<select[^>]*id="system-select"[^>]*>([\s\S]*?)<\/select>/.exec(html);
  const systems = select ? [...select[1].matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]) : [];
  const chosen = args.systems ? systems.filter((id) => args.systems.includes(id)) : systems;
  const targets = [];
  for (const system of chosen) {
    const inventory = await getJson(`${args.ui}/api/inventory?system_id=${encodeURIComponent(system)}`);
    const snapshot = inventory.snapshot || inventory;
    // The page's load-time requests depend on the platform (Sol #1108).
    const platform = String(snapshot.selected_system_platform || "core").toLowerCase();
    let views = { views: [] };
    try {
      views = await getJson(`${args.ui}/api/storage-views?system_id=${encodeURIComponent(system)}`);
    } catch {
      // no views
    }
    const scopes = [
      ...(snapshot.enclosures || []).map((e) => ({ kind: "enc", id: e.id, query: `enclosure_id=${encodeURIComponent(e.id)}` })),
      // SVBIND: a view target binds the enclosure its view is bound to (null: none).
      ...(views.views || []).filter((v) => v.enabled !== false && (v.render?.show_in_main_ui ?? true)).map((v) => ({ kind: "view", id: v.id, query: `storage_view_id=${encodeURIComponent(v.id)}`, viewEnclosure: viewBoundEnclosure(v) })),
    ];
    if (!scopes.length) scopes.push({ kind: "default", id: "default", query: "" });
    scopes.forEach((scope, index) => targets.push({
      page: "main", ready: "main", system, platform, scopeIndex: index, label: `${system}/main/${scope.kind}-${index}`,
      url: `${args.ui}/?system_id=${encodeURIComponent(system)}${scope.query ? `&${scope.query}` : ""}`,
      ...(scope.kind === "view" ? { viewEnclosure: scope.viewEnclosure } : {}),
    }));
    (snapshot.enclosures || [{ id: null }]).forEach((enclosure, index) => targets.push({
      page: "fabric", ready: "fabric", system, platform, scopeIndex: index, label: `${system}/fabric/enc-${index}`,
      url: `${args.ui}/sas-fabric?system_id=${encodeURIComponent(system)}${enclosure.id ? `&enclosure_id=${encodeURIComponent(enclosure.id)}` : ""}`,
    }));
  }
  if (args.history) targets.push({ page: "history", ready: "history", scopeIndex: 0, label: "history", url: `${args.history}/` });
  if (args.admin) targets.push({ page: "admin", ready: "admin", scopeIndex: 0, label: "admin", url: `${args.admin}/` });
  return targets;
}

function fixtureTargets() {
  return [
    { page: "main", ready: "main", scopeIndex: 0, system: "fixture", variant: "write-sign-in", label: "fixture/main-write-sign-in", url: `${FIXTURE_ORIGINS["ui-write-sign-in"]}/` },
    { page: "fabric", ready: "fabric", scopeIndex: 0, system: "fixture", variant: "write-sign-in", label: "fixture/fabric-write-sign-in", url: `${FIXTURE_ORIGINS["ui-write-sign-in"]}/sas-fabric` },
    { page: "main", ready: "main", scopeIndex: 0, system: "fixture", variant: "storage-views-error", label: "fixture/main-storage-views-error", url: `${FIXTURE_ORIGINS["ui-storage-views-error"]}/` },
    { page: "main", ready: "main", scopeIndex: 0, system: "fixture", platform: "quantastor", variant: "quantastor", label: "fixture/main-quantastor", url: `${FIXTURE_ORIGINS["ui-quantastor"]}/` },
    { page: "main", ready: "main", scopeIndex: 0, system: "fixture", label: "fixture/main", url: `${FIXTURE_ORIGINS.ui}/` },
    // #1127: a saved storage view selected through the target URL, as
    // liveTargets() emits it (a later scope, so the `scope: "all"` states run).
    { page: "main", ready: "main", scopeIndex: 1, system: "fixture", label: "fixture/main-view-boot-doms", url: `${FIXTURE_ORIGINS.ui}/?system_id=demo-system&storage_view_id=boot-doms` },
    // Codex PRRT_kwDOSAqK-M61IUDz: a system whose FIRST scope is a storage
    // view with history (no enclosure options), so every main read state,
    // the heat-map rate metrics and timeline included, runs on the view.
    // Read states only: the write and identify states target fixture/main.
    { page: "main", ready: "main", scopeIndex: 0, system: "fixture", readOnly: true, label: "fixture/main-view-first-nvme", url: `${FIXTURE_ORIGINS.ui}/?system_id=demo-system&storage_view_id=nvme-carrier-x4` },
    { page: "fabric", ready: "fabric", scopeIndex: 0, system: "fixture", label: "fixture/fabric", url: `${FIXTURE_ORIGINS.ui}/sas-fabric` },
    { page: "history", ready: "history", scopeIndex: 0, label: "fixture/history", url: `${FIXTURE_ORIGINS.history}/` },
    { page: "admin", ready: "admin", scopeIndex: 0, label: "fixture/admin", url: `${FIXTURE_ORIGINS.admin}/` },
    { page: "offline", ready: "main", scopeIndex: 0, label: "fixture/offline", url: `${FIXTURE_ORIGINS.offline}/` },
  ];
}

function statesFor(target, states, args, used) {
  if (args.scopes === "first" && target.scopeIndex > 0) return [];
  if (args.scopes === "rest" && target.scopeIndex === 0) return [];
  return states.filter((state) => {
    if (state.page !== target.page) return false;
    if (target.scopeIndex > 0 && state.scope !== "all") return false;
    if (state.appliance_load && !args.fixture && !args.applianceLoad) return false;
    if ((state.variant || null) !== (target.variant || null)) return false;
    if (state.heavy) {
      if (!args.downloads || used.heavy) return false;
      used.heavy = true;
    }
    if (["app-write-revert", "led"].includes(state.safety)) {
      if (!args.allow.includes(state.safety) || target.readOnly) return false;
      if (!args.fixture && target.system !== args.writeSystem) return false;
      if (target.scopeIndex > 0) return false;
    }
    return true;
  });
}

function writeReceipt(outDir, receipt) {
  try {
    fs.mkdirSync(outDir, { recursive: true, mode: 0o700 });
    fs.writeFileSync(path.join(outDir, "receipt.json"), JSON.stringify({ ...receipt, at: new Date().toISOString() }, null, 1));
  } catch {
    // The exit status still carries the result.
  }
}

// #1117: identify cleanup on every exit path the process itself sees. The
// startup sweep of the next run covers the ones it cannot (SIGKILL, crash).
function createExitGuards({ ledger, outDir, sweepOptions = {}, log = (line) => process.stderr.write(`${line}\n`) }) {
  // Only an in-progress sweep is shared (a signal during the final cleanup);
  // every later call reads the ledger again, so a light armed after the
  // startup sweep is still cleared.
  let inflight = null;
  const sweep = () => {
    if (!inflight) {
      inflight = (async () => {
        if (!ledger) return null;
        try {
          if (!ledger.entries().length) return { ok: true, armed: 0, results: [] };
          log("identify cleanup: clearing armed locate lights out of band");
          return await sweepLedger(ledger, sweepOptions);
        } catch (error) {
          return { ok: false, error: String(error && error.message ? error.message : error).slice(0, 200) };
        }
      })().finally(() => {
        inflight = null;
      });
    }
    return inflight;
  };
  const onSignal = (signal, number) => {
    let handled = false;
    return async () => {
      if (handled) return;
      handled = true;
      const ledCleanup = await sweep();
      const code = ledCleanup && ledCleanup.ok === false ? EXIT["cleanup-uncertain"] : 128 + number;
      writeReceipt(outDir, { status: "interrupted", signal, exitCode: code, ledCleanup });
      process.exit(code);
    };
  };
  const onFatal = async (error) => {
    const ledCleanup = await sweep();
    const code = ledCleanup && ledCleanup.ok === false ? EXIT["cleanup-uncertain"] : 1;
    writeReceipt(outDir, { status: "crashed", error: String(error && error.message ? error.message : error).replace(/wss?:\/\/\S+/g, "<cdp-url>").slice(0, 300), exitCode: code, ledCleanup });
    process.exit(code);
  };
  return {
    sweep,
    install() {
      process.on("SIGTERM", onSignal("SIGTERM", 15));
      process.on("SIGINT", onSignal("SIGINT", 2));
      process.on("SIGHUP", onSignal("SIGHUP", 1));
      process.on("uncaughtException", onFatal);
      process.on("unhandledRejection", onFatal);
      return this;
    },
  };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// #1117: start the independent identify watchdog for this process, detached
// in its own session (it outlives SIGKILL of this process and of its process
// group), and wait for its first heartbeat. Throws when it does not start; no
// IDENTIFY can be armed without it.
async function startLedWatchdog({ ledger, outDir, pollMs = 1000, settleMs = 15_000, recheckMs = 5000, verifyMs = 45_000, maxLifeMs = 30 * 60_000 }) {
  fs.mkdirSync(outDir, { recursive: true, mode: 0o700 });
  const log = fs.openSync(path.join(outDir, "led-watchdog.log"), "a", 0o600);
  const child = spawn(process.execPath, [
    WATCHDOG_SCRIPT, "--ledger", ledger.file, "--owner", String(process.pid), "--out", outDir,
    "--poll-ms", String(pollMs), "--settle-ms", String(settleMs), "--recheck-ms", String(recheckMs),
    "--verify-ms", String(verifyMs), "--max-life-ms", String(maxLifeMs),
  ], { detached: true, stdio: ["ignore", log, log] });
  fs.closeSync(log);
  child.unref();
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    const beat = ledger.watchdog();
    if (beat && beat.pid === child.pid && ledger.watchdogAlive()) return { pid: child.pid };
    if (child.exitCode !== null) break;
    await sleep(50);
  }
  try {
    process.kill(child.pid, "SIGKILL");
  } catch {
    // already gone
  }
  throw new Error("the identify watchdog did not start; no IDENTIFY will be sent");
}

// Refuse a live crawl whose inventory or route manifest is not current: an
// unclassified control or route is a hard failure (#1120).
function preflightProblems(inventory) {
  const problems = [];
  const report = controls.coverageReport(controls.scanSources(REPO_ROOT), inventory);
  for (const control of report.uncovered) problems.push(`unclassified control ${control.key} (${control.file}:${control.line})`);
  for (const key of report.stale) problems.push(`stale inventory key ${key}`);
  const manifest = routes.loadManifest();
  problems.push(...routes.checkManifest(routes.scanRoutes(REPO_ROOT, manifest), manifest));
  problems.push(...routes.checkScriptApiPaths(REPO_ROOT, controls.UI_SOURCES.scripts, manifest));
  // D-TNJ-20261109-CRAWLER-SWEEP: the generated read reach must match the page scripts.
  problems.push(...reachProblems(REPO_ROOT, { manifest }));
  return problems;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const outDir = path.resolve(args.out);
  if (args.merge) {
    const { summary, problems } = mergeReports(outDir, args.merge.map((dir) => path.resolve(dir)));
    console.log(JSON.stringify({ ...summary, problems }, null, 1));
    return EXIT[summary.safety] ?? EXIT["safety-failed"];
  }
  const sweepOptions = { verifyMs: args.ledVerifyMs || 45_000, pollMs: Math.min(3000, Math.max(200, Math.floor((args.ledVerifyMs || 45_000) / 10))) };
  const ledger = args.ledLedger ? new LedLedger(args.ledLedger, { leaseMs: args.ledLeaseMs }) : null;
  if (args.ledSweep) {
    const ledCleanup = await sweepLedger(ledger, sweepOptions);
    const code = ledCleanup.ok ? EXIT.ok : EXIT["cleanup-uncertain"];
    writeReceipt(outDir, { status: ledCleanup.ok ? "ok" : "cleanup-uncertain", mode: "led-sweep", exitCode: code, ledCleanup });
    console.log(JSON.stringify(ledCleanup, null, 1));
    return code;
  }
  const guards = createExitGuards({ ledger, outDir, sweepOptions }).install();
  // Startup sweep: no crawl starts while a previous run's light may be on.
  if (ledger) {
    const startup = await guards.sweep();
    if (startup && startup.ok === false) {
      writeReceipt(outDir, { status: "cleanup-uncertain", stage: "startup sweep", exitCode: EXIT["cleanup-uncertain"], ledCleanup: startup });
      console.error("identify cleanup from a previous run could not be verified; nothing was crawled");
      return EXIT["cleanup-uncertain"];
    }
  }
  // #1117: the watchdog must be watching, and this process's supervisor
  // watching the watchdog, before any IDENTIFY can be armed.
  let watchdog = null;
  let crawler = null;
  const ledEvents = [];
  if (ledger && args.allow.includes("led")) {
    try {
      watchdog = await startLedWatchdog({ ledger, outDir, verifyMs: sweepOptions.verifyMs });
      await startLedSupervisor({
        ledger, verifyMs: sweepOptions.verifyMs,
        // The watchdog stopped heartbeating (or a lease overran with it alive):
        // the supervisor has turned this run's lights off; the crawl stops and fails.
        onLost: (event) => {
          ledEvents.push(event);
          if (crawler && !crawler.finished) crawler.aborted = `identify ${event.type}`;
        },
      });
    } catch (error) {
      writeReceipt(outDir, { status: "safety-failed", stage: "led watchdog", exitCode: EXIT["safety-failed"], error: String(error.message).slice(0, 200) });
      console.error(String(error.message));
      return EXIT["safety-failed"];
    }
  }
  const inventory = loadInventory();
  const problems = validateInventory(inventory);
  if (problems.length) throw new Error(`inventory invalid:\n${problems.join("\n")}`);
  if (!args.fixture) {
    const preflight = preflightProblems(inventory);
    if (preflight.length) {
      writeReceipt(outDir, { status: "safety-failed", stage: "preflight", exitCode: EXIT["safety-failed"], failures: preflight });
      console.error(`refusing a live crawl:\n${preflight.join("\n")}`);
      return EXIT["safety-failed"];
    }
  }
  const widths = args.widths || inventory.widths[args.ci ? "ci" : args.tier || "release"];
  const states = selectStates(expandStates(inventory), { pages: args.pages, ciOnly: args.ci, ids: args.states, includeFixtureOnly: Boolean(args.fixture) });
  fs.mkdirSync(outDir, { recursive: true, mode: 0o700 });
  const fixture = args.fixture ? JSON.parse(fs.readFileSync(args.fixture, "utf8")) : null;
  const origins = fixture ? { ...FIXTURE_ORIGINS } : { ui: args.ui, history: args.history || null, admin: args.admin || null, offline: FIXTURE_ORIGINS.offline };
  const guard = new RequestGuard({ origins });
  const stats = { blocked: [], writes: [], routeErrors: [] };
  const cache = fixture ? null : new ReadCache();
  const offlinePages = {};
  const handler = fixture
    ? fixtureHandler(fixture)
    : null;
  // Live mode answers only the synthetic offline origin locally (downloaded
  // copies); everything else continues to the stack through the read cache.
  const liveOffline = async (route, info) => {
    if (info.service !== "offline") return false;
    const body = offlinePages[info.url.pathname];
    await (body ? route.fulfill({ contentType: "text/html; charset=utf-8", body }) : route.fulfill({ status: 404, body: "" }));
    return true;
  };
  const cdpUrl = process.env[args.cdpEnv] || "";
  const started = new Date().toISOString();
  const browser = cdpUrl
    ? await chromium.connectOverCDP(cdpUrl, { timeout: 120_000 })
    : await chromium.launch({ channel: process.env.PLAYWRIGHT_BROWSER_CHANNEL || undefined, handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false });
  const runProblems = [];
  let context = null;
  let routesHandle = null;
  browser.on("disconnected", () => {
    if (crawler && !crawler.finished) {
      crawler.aborted = "browser disconnected";
      runProblems.push("the browser disconnected during the crawl");
    }
  });
  try {
    context = await browser.newContext({ viewport: null, acceptDownloads: true, serviceWorkers: "block" });
    routesHandle = await installRoutes(context, {
      guard, origins, cache, stats, ledger,
      fixture: handler,
      local: handler ? null : liveOffline,
    });
    const axePath = (() => {
      try {
        return require.resolve("axe-core/axe.min.js");
      } catch {
        return null;
      }
    })();
    crawler = new Crawler({
      context, cdpFactory: (page) => context.newCDPSession(page), guard, inventory, outDir, widths,
      allow: args.allow, confirmPreview: Boolean(args.confirmPreview), downloads: Boolean(args.downloads),
      axeSource: args.axe && axePath ? fs.readFileSync(axePath, "utf8") : null,
      baselineDir: args.baseline ? path.resolve(args.baseline) : null, fixtureMode: Boolean(fixture), cache,
      paceMs: args.paceMs ?? (fixture ? 0 : 1500), ledger, writeSystem: args.writeSystem || null,
    });
    const targets = fixture ? fixtureTargets() : await liveTargets(args);
    const used = {};
    for (const target of targets) {
      const chosen = statesFor(target, states, args, used);
      if (chosen.length) await crawler.crawlTarget(target, chosen);
    }
    // Offline copies the crawl downloaded are crawled like the public demo.
    const downloads = crawler.records.flatMap((record) => record.downloads || []);
    downloads.forEach((file, index) => { offlinePages[`/copy-${index}`] = fs.readFileSync(path.join(outDir, file), "utf8"); });
    if (!fixture) {
      for (const [index] of downloads.entries()) {
        const target = { page: "offline", ready: "main", scopeIndex: 0, label: `offline/copy-${index}`, url: `${FIXTURE_ORIGINS.offline}/copy-${index}` };
        await crawler.crawlTarget(target, statesFor(target, states, args, used));
      }
    }
    if (crawler.aborted) runProblems.push(`crawl aborted: ${crawler.aborted}`);
  } catch (error) {
    runProblems.push(`crawl stopped: ${String(error && error.message ? error.message : error).replace(/wss?:\/\/\S+/g, "<cdp-url>").split("\n")[0].slice(0, 200)}`);
  } finally {
    if (crawler) crawler.finished = true;
    if (routesHandle) await routesHandle.drain();
    if (context) await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }
  if (crawler) crawler.endFollowUp();
  // Whatever the browser did, anything still armed is cleared out of band now.
  const ledCleanup = ledger ? await guards.sweep() : null;
  // #1117: the supervisor found the watchdog gone (or a lease overrun): a failed crawl.
  if (ledger && ledger.supervisor) ledEvents.push(...ledger.supervisor.events.filter((event) => !ledEvents.includes(event)));
  for (const event of ledEvents) runProblems.push(`identify ${event.type}: ${event.detail || event.error || ""}`.trim());
  // #1128: delayed follow-ups admitted inside a declared follow-up window.
  stats.followUps = crawler ? crawler.followUpAdmitted || [] : [];
  // Sol #1108: every request a page-load window admitted (a platform's startup refresh).
  stats.pageLoads = crawler ? crawler.loadAdmitted || [] : [];
  const summary = writeReport(outDir, {
    meta: {
      mode: fixture ? "fixture" : "live", label: args.label || null, started, finished: new Date().toISOString(), widths,
      allow: args.allow, writeSystem: args.writeSystem || null, remoteBrowser: Boolean(cdpUrl),
      ledWatchdog: watchdog ? { pid: watchdog.pid, leaseMs: ledger.leaseMs, supervisor: Boolean(ledger.supervisor) } : null,
      cache: cache ? { hits: cache.hits, misses: cache.misses } : null,
    },
    records: crawler ? crawler.records : [],
    actuations: crawler ? crawler.actuations : [],
    guardLog: guard.log,
    stats,
    ledCleanup,
    problems: runProblems,
  });
  const code = EXIT[summary.safety];
  writeReceipt(outDir, { status: summary.safety, exitCode: code, ledCleanup, problems: runProblems });
  console.log(JSON.stringify(summary, null, 1));
  return code;
}

if (require.main === module) {
  main().then((code) => process.exit(code), (error) => {
    console.error(String(error && error.message ? error.message : error).replace(/wss?:\/\/\S+/g, "<cdp-url>"));
    process.exit(1);
  });
}

module.exports = { parseArgs, statesFor, fixtureTargets, liveTargets, createExitGuards, startLedWatchdog, preflightProblems, FIXTURE_ORIGINS };
