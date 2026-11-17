"use strict";

// UI coverage primary crawl (#ui-coverage): every `ci: true` state of
// qa/ui-states.json is crawled on every synthetic fixture target at the `ci`
// widths with the automatic layout, heat-map, contrast and accessibility
// checks, and must call only the routes its safety class allows. Split out of
// qa/ui-coverage.spec.js so CI can run this long crawl on its own browser leg
// (#1191); the test itself is unchanged. Complete production templates and
// assets; only HTTP and the data are synthetic. No stack, no appliance. See
// CONTRIBUTING.md, UI coverage.

const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { loadInventory, expandStates, selectStates, checkObserved, readsFor } = require("./ui-crawl/inventory");
const { RequestGuard } = require("./ui-crawl/guard");
const { installRoutes, fixtureHandler } = require("./ui-crawl/transport");
const { Crawler } = require("./ui-crawl/crawler");
const { writeReport, allFindings, unexplainedErrors } = require("./ui-crawl/report");

const root = path.resolve(__dirname, "..");
const inventory = loadInventory();

test.use({ serviceWorkers: "block" });

test("the crawler's layout, heat-map and contrast checks pass on the synthetic fixtures", async ({ browser }, testInfo) => {
  test.setTimeout(16 * 60_000);
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-ui-coverage-"));
  const fixturePath = path.join(tempDir, "fixture.json");
  execFileSync(process.env.PYTHON || "python3", ["scripts/build_ui_crawl_fixture.py", "--output", fixturePath], { cwd: root, stdio: "inherit" });
  const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
  const origins = {
    ui: "https://ui.synthetic.invalid",
    history: "https://history.synthetic.invalid",
    admin: "https://admin.synthetic.invalid",
    offline: "https://offline.synthetic.invalid",
    assets: "https://synthetic.invalid",
    "ui-write-sign-in": "https://ui-write-sign-in.synthetic.invalid",
    "ui-storage-views-error": "https://ui-storage-views-error.synthetic.invalid",
    "ui-quantastor": "https://ui-quantastor.synthetic.invalid",
    "ui-dense-sleds": "https://ui-dense-sleds.synthetic.invalid",
    "ui-fabric-topology": "https://ui-fabric-topology.synthetic.invalid",
  };
  const guard = new RequestGuard({ origins });
  const stats = { blocked: [], writes: [], routeErrors: [] };
  const outDir = testInfo.outputPath("ui-crawl");
  const context = await browser.newContext({ viewport: null, serviceWorkers: "block" });
  const routes = await installRoutes(context, { guard, origins, stats, fixture: fixtureHandler(fixture) });
  const states = selectStates(expandStates(inventory), { ciOnly: true, includeFixtureOnly: true });
  const crawler = new Crawler({
    context, cdpFactory: (page) => context.newCDPSession(page), guard, inventory, outDir, widths: inventory.widths.ci,
    axeSource: fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8"), fixtureMode: true, settleMs: 400, renderAudit: true,
    log: (line) => process.stdout.write(`${line}\n`),
  });
  const targets = [
    { page: "main", ready: "main", label: "fixture/main", url: `${origins.ui}/` },
    { page: "fabric", ready: "fabric", label: "fixture/fabric", url: `${origins.ui}/sas-fabric` },
    { page: "history", ready: "history", label: "fixture/history", url: `${origins.history}/` },
    { page: "admin", ready: "admin", label: "fixture/admin", url: `${origins.admin}/` },
    { page: "offline", ready: "main", label: "fixture/offline", url: `${origins.offline}/` },
    { page: "main", ready: "main", variant: "write-sign-in", label: "fixture/main-write-sign-in", url: `${origins["ui-write-sign-in"]}/` },
    { page: "fabric", ready: "fabric", variant: "write-sign-in", label: "fixture/fabric-write-sign-in", url: `${origins["ui-write-sign-in"]}/sas-fabric` },
    // #937: Topology drawn from the real builder's synthetic cascade (bays, lines, link labels).
    { page: "fabric", ready: "fabric", variant: "fabric-topology", label: "fixture/fabric-topology", url: `${origins["ui-fabric-topology"]}/sas-fabric` },
    { page: "main", ready: "main", variant: "storage-views-error", label: "fixture/main-storage-views-error", url: `${origins["ui-storage-views-error"]}/` },
    // Sol #1108: a QuantaStor page sends a forced startup read; only its declared page-load window admits it.
    { page: "main", ready: "main", variant: "quantastor", platform: "quantastor", label: "fixture/main-quantastor", url: `${origins["ui-quantastor"]}/` },
    // #1145: 24 narrow 2.5-inch sleds, where heat values once wrapped mid-number.
    { page: "main", ready: "main", variant: "dense-sleds", label: "fixture/main-dense-sleds", url: `${origins["ui-dense-sleds"]}/` },
    // #1127: a saved storage view selected through the target URL, as a live
    // crawl reaches it (a later scope: only the `scope: "all"` states run).
    { page: "main", ready: "main", scopeIndex: 1, label: "fixture/main-view-boot-doms", url: `${origins.ui}/?system_id=demo-system&storage_view_id=boot-doms` },
  ];
  try {
    for (const target of targets) await crawler.crawlTarget(target, states.filter((state) => state.page === target.page && (!target.scopeIndex || state.scope === "all")));
  } finally {
    await routes.drain();
    await context.close();
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
  const summary = writeReport(outDir, {
    meta: { mode: "fixture", label: "ci", started: "", finished: "", widths: inventory.widths.ci },
    records: crawler.records, actuations: crawler.actuations, guardLog: guard.log, stats,
  });
  await testInfo.attach("ui-crawl-summary", { body: JSON.stringify(summary, null, 1), contentType: "application/json" });

  const broken = crawler.records.filter((record) => !["done", "not-applicable"].includes(record.status));
  expect(broken.map((record) => `${record.target} ${record.state}: ${record.status} ${JSON.stringify(record.findings)}`)).toEqual([]);
  expect(stats.blocked, "the fixture pages made requests the guard refused (unknown route, or a class the state may not use)").toEqual([]);
  expect(crawler.records.filter((record) => record.status === "done").length).toBeGreaterThan(20);
  // Every selected state ran on a page that has it (a variant state needs its variant target).
  const ran = new Set(crawler.records.map((record) => record.state));
  expect(states.filter((state) => !ran.has(state.id)).map((state) => state.id), "selected states that no target ran").toEqual([]);

  // #1120: the safety class each state really has, from the routes it called.
  const byId = new Map(states.map((state) => [state.id, state]));
  const misclassified = [];
  for (const [stateId, seen] of guard.observed) {
    const state = byId.get(stateId);
    if (state) misclassified.push(...checkObserved(state, [...seen.keys()], readsFor(inventory, state, "*")));
  }
  expect(misclassified, "a control called a route its state's safety class or read allowlist does not allow; fix the class, the reads or the control").toEqual([]);

  // #1127: on the saved-view target the heat map read each occupied slot's
  // SMART summary through the storage-view route, admitted by the heat-map
  // states' own read lists (a blocked one would have failed above).
  const viewSmart = "read GET /api/storage-views/{view_id}/slots/{slot_index}/smart";
  expect(["main.heatmap", "main.heatmap.temperature"].filter((id) => !(guard.observed.get(id) || new Map()).has(viewSmart)),
    "the saved-view fixture target no longer drives the storage-view heat-map SMART read").toEqual([]);

  // Codex #1108: loading the synthetic system (verify_ssl + saved CA) into
  // the admin form re-checks its saved bundle, admitted by the loading
  // state's own declaration (a blocked one would have failed above).
  const savedStatus = "appliance-load GET /api/admin/tls/saved-status";
  expect((guard.observed.get("admin.system-form") || new Map()).get(savedStatus),
    "the admin fixture no longer sends the saved-certificate check when a system loads").toBe(1);

  // Codex #1108: fabric.diagnostics switches to Disk Path and the fixture
  // carries diagnostics, so the state runs instead of being skipped.
  const diagnostics = crawler.records.filter((record) => record.state === "fabric.diagnostics" && record.target === "fixture/fabric");
  expect(diagnostics.map((record) => record.status), "fabric.diagnostics did not render the Disk Path evidence").toEqual(["done"]);

  // Sol #1108: the QuantaStor page's startup refresh went out, inside the
  // declared page-load window only (it would be a blocked request otherwise).
  const startupLoads = (crawler.loadAdmitted || []).filter((entry) => entry.target === "fixture/main-quantastor");
  expect(startupLoads.length, "the QuantaStor fixture page sent no startup refresh; the fixture no longer covers the load window").toBeGreaterThan(0);
  expect((crawler.loadAdmitted || []).filter((entry) => entry.target !== "fixture/main-quantastor"), "a page-load window admitted a request on a non-QuantaStor target").toEqual([]);

  // Sol #1108: `covers` must be backed by a rendering. Every inventoried
  // control is visible in at least one executed state, unless it is a
  // reviewed render gap.
  const rendered = new Set(crawler.records.flatMap((record) => record.rendered || []));
  const gaps = new Map((inventory.render_gaps || []).map((gap) => [gap.key, gap]));
  const claimed = [...new Set(inventory.states.flatMap((state) => state.covers || []))].sort();
  const unrendered = claimed.filter((key) => !rendered.has(key) && !gaps.has(key));
  expect(unrendered, "controls named in `covers` that no CI state rendered: add a state (or fixture variant) that shows them, or a reviewed `render_gaps` entry").toEqual([]);
  const staleGaps = [...gaps.keys()].filter((key) => rendered.has(key) || !claimed.includes(key));
  expect(staleGaps, "render_gaps entries that now render, or that no state covers; remove them").toEqual([]);

  const unexplained = unexplainedErrors(crawler.records);
  const lines = unexplained.map((f) => `${f.state} @${f.width}px ${f.type} ${f.selector || ""} ${f.text || ""} ${JSON.stringify(f.detail || {})}`);
  expect(lines, `Layout/contrast/console findings. Open ${outDir}/index.html for tiles. Fix the defect, or if it is a known `
    + "issue add it to known_findings in qa/ui-states.json with the issue number.").toEqual([]);

  // A known finding that no longer reproduces should be removed from the inventory.
  const seen = new Set(allFindings(crawler.records).filter((f) => f.known).map((f) => `${f.known}:${f.type}`));
  const stale = (inventory.known_findings || []).filter((entry) => !seen.has(`${entry.issue}:${entry.type}`));
  if (stale.length) {
    testInfo.annotations.push({ type: "stale-known-finding", description: stale.map((e) => `#${e.issue} ${e.type} ${e.state}`).join("; ") });
  }
});
