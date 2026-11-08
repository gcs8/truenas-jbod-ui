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

const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("@playwright/test");
const { loadInventory, validateInventory, expandStates, selectStates } = require("../qa/ui-crawl/inventory");
const { RequestGuard } = require("../qa/ui-crawl/guard");
const { installRoutes, fixtureHandler, ReadCache } = require("../qa/ui-crawl/transport");
const { Crawler } = require("../qa/ui-crawl/crawler");
const { writeReport, mergeReports } = require("../qa/ui-crawl/report");

const FIXTURE_ORIGINS = {
  ui: "https://ui.synthetic.invalid",
  history: "https://history.synthetic.invalid",
  admin: "https://admin.synthetic.invalid",
  offline: "https://offline.synthetic.invalid",
  // build_current_source_browser_fixture renders static URLs on this host.
  assets: "https://synthetic.invalid",
};

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
    else throw new Error(`unknown argument ${flag}`);
  }
  if (!args.out) throw new Error("--out is required");
  if (args.merge) return args;
  if (!args.fixture && !args.ui) throw new Error("give --fixture or --ui");
  if (args.allow.includes("forbidden")) throw new Error("forbidden states are never actuated");
  for (const cls of args.allow) if (!["app-write-revert", "led"].includes(cls)) throw new Error(`unknown class ${cls}`);
  if (args.allow.length && !args.fixture && !args.writeSystem) throw new Error("--allow on a live stack needs --write-system");
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
    let views = { views: [] };
    try {
      views = await getJson(`${args.ui}/api/storage-views?system_id=${encodeURIComponent(system)}`);
    } catch {
      // no views
    }
    const scopes = [
      ...(snapshot.enclosures || []).map((e) => ({ kind: "enc", id: e.id, query: `enclosure_id=${encodeURIComponent(e.id)}` })),
      ...(views.views || []).filter((v) => v.enabled !== false && (v.render?.show_in_main_ui ?? true)).map((v) => ({ kind: "view", id: v.id, query: `storage_view_id=${encodeURIComponent(v.id)}` })),
    ];
    if (!scopes.length) scopes.push({ kind: "default", id: "default", query: "" });
    scopes.forEach((scope, index) => targets.push({
      page: "main", ready: "main", system, scopeIndex: index, label: `${system}/main/${scope.kind}-${index}`,
      url: `${args.ui}/?system_id=${encodeURIComponent(system)}${scope.query ? `&${scope.query}` : ""}`,
    }));
    (snapshot.enclosures || [{ id: null }]).forEach((enclosure, index) => targets.push({
      page: "fabric", ready: "fabric", system, scopeIndex: index, label: `${system}/fabric/enc-${index}`,
      url: `${args.ui}/sas-fabric?system_id=${encodeURIComponent(system)}${enclosure.id ? `&enclosure_id=${encodeURIComponent(enclosure.id)}` : ""}`,
    }));
  }
  if (args.history) targets.push({ page: "history", ready: "history", scopeIndex: 0, label: "history", url: `${args.history}/` });
  if (args.admin) targets.push({ page: "admin", ready: "admin", scopeIndex: 0, label: "admin", url: `${args.admin}/` });
  return targets;
}

function fixtureTargets() {
  return [
    { page: "main", ready: "main", scopeIndex: 0, system: "fixture", label: "fixture/main", url: `${FIXTURE_ORIGINS.ui}/` },
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
    if (state.heavy) {
      if (!args.downloads || used.heavy) return false;
      used.heavy = true;
    }
    if (["app-write-revert", "led"].includes(state.safety)) {
      if (!args.allow.includes(state.safety)) return false;
      if (!args.fixture && target.system !== args.writeSystem) return false;
      if (target.scopeIndex > 0) return false;
    }
    return true;
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.merge) {
    console.log(JSON.stringify(mergeReports(path.resolve(args.out), args.merge.map((dir) => path.resolve(dir))), null, 1));
    return 0;
  }
  const inventory = loadInventory();
  const problems = validateInventory(inventory);
  if (problems.length) throw new Error(`inventory invalid:\n${problems.join("\n")}`);
  const widths = args.widths || inventory.widths[args.ci ? "ci" : args.tier || "release"];
  let states = selectStates(expandStates(inventory), { pages: args.pages, ciOnly: args.ci, ids: args.states, includeFixtureOnly: false });
  const outDir = path.resolve(args.out);
  fs.mkdirSync(outDir, { recursive: true, mode: 0o700 });
  const fixture = args.fixture ? JSON.parse(fs.readFileSync(args.fixture, "utf8")) : null;
  const origins = fixture ? { ...FIXTURE_ORIGINS } : { ui: args.ui, history: args.history || null, admin: args.admin || null, offline: FIXTURE_ORIGINS.offline };
  const guard = new RequestGuard({ origins: Object.values(origins).filter(Boolean), readPosts: inventory.read_posts });
  const stats = { blocked: [], writes: [], routeErrors: [] };
  const cache = fixture ? null : new ReadCache();
  const offlinePages = {};
  const handler = fixture
    ? fixtureHandler(fixture)
    : null;
  const liveOffline = async (route, info) => {
    if (info.service === "offline" && offlinePages[info.url.pathname]) return route.fulfill({ contentType: "text/html; charset=utf-8", body: offlinePages[info.url.pathname] });
    if (info.service === "offline") return route.fulfill({ status: 404, body: "" });
    return null;
  };
  const cdpUrl = process.env[args.cdpEnv] || "";
  const started = new Date().toISOString();
  const browser = cdpUrl
    ? await chromium.connectOverCDP(cdpUrl, { timeout: 120_000 })
    : await chromium.launch({ channel: process.env.PLAYWRIGHT_BROWSER_CHANNEL || undefined });
  let context = null;
  let routes = null;
  let crawler = null;
  try {
    context = await browser.newContext({ viewport: null, acceptDownloads: true, serviceWorkers: "block" });
    routes = await installRoutes(context, {
      guard, origins, cache, stats,
      fixture: handler || (async (route, info) => {
        const answered = await liveOffline(route, info);
        if (answered === null) return route.continue();
        return answered;
      }),
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
      paceMs: args.paceMs ?? (fixture ? 0 : 1500),
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
  } finally {
    if (routes) await routes.drain();
    if (context) await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }
  const summary = writeReport(outDir, {
    meta: {
      mode: fixture ? "fixture" : "live", label: args.label || null, started, finished: new Date().toISOString(), widths,
      allow: args.allow, writeSystem: args.writeSystem || null, remoteBrowser: Boolean(cdpUrl),
      cache: cache ? { hits: cache.hits, misses: cache.misses } : null,
    },
    records: crawler ? crawler.records : [],
    actuations: crawler ? crawler.actuations : [],
    guardLog: guard.log,
    stats,
  });
  console.log(JSON.stringify(summary, null, 1));
  const revertFailed = (crawler ? crawler.actuations : []).some((a) => a.status === "revert-failed");
  return revertFailed ? 2 : 0;
}

if (require.main === module) {
  main().then((code) => process.exit(code), (error) => {
    console.error(String(error && error.message ? error.message : error).replace(/wss?:\/\/\S+/g, "<cdp-url>"));
    process.exit(1);
  });
}

module.exports = { parseArgs, statesFor, fixtureTargets, FIXTURE_ORIGINS };
