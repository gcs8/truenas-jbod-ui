"use strict";

// UI coverage (#ui-coverage): every interactive control must be in the state
// inventory (qa/ui-states.json), and a representative subset of states is
// crawled on synthetic fixtures with the automatic layout, heat-map and
// contrast checks. Complete production templates and assets; only HTTP and the
// data are synthetic. No stack, no appliance. See CONTRIBUTING.md, UI coverage.

const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const controls = require("./ui-crawl/controls");
const { loadInventory, validateInventory, expandStates, selectStates } = require("./ui-crawl/inventory");
const { RequestGuard } = require("./ui-crawl/guard");
const { installRoutes, fixtureHandler } = require("./ui-crawl/transport");
const { Crawler } = require("./ui-crawl/crawler");
const { writeReport, allFindings } = require("./ui-crawl/report");

const root = path.resolve(__dirname, "..");
const inventory = loadInventory();

test.use({ serviceWorkers: "block" });

test("every interactive control in the UI sources has an inventory entry", () => {
  expect(validateInventory(inventory)).toEqual([]);
  expect(controls.discoverUiSourceFiles(root), "a UI template or script is not scanned: add it to UI_SOURCES in qa/ui-crawl/controls.js")
    .toEqual([...controls.UI_SOURCES.templates, ...controls.UI_SOURCES.scripts].sort());
  const report = controls.coverageReport(controls.scanSources(root), inventory);
  const message = report.uncovered.length
    ? `These controls are not covered by qa/ui-states.json. Add each key to the \`covers\` list of the state that renders it `
      + `(or add a new state with its selector steps and safety class), or to \`ignored_controls\` with a reason. `
      + `See CONTRIBUTING.md, UI coverage.\n${controls.formatUncovered(report.uncovered)}`
    : "";
  expect(report.uncovered.length, message).toBe(0);
  expect(report.stale, "qa/ui-states.json names controls that no longer exist in the sources; remove them").toEqual([]);
});

test("the crawler's layout, heat-map and contrast checks pass on the synthetic fixtures", async ({ browser }, testInfo) => {
  test.setTimeout(12 * 60_000);
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
  };
  const guard = new RequestGuard({ origins: Object.values(origins), readPosts: inventory.read_posts });
  const stats = { blocked: [], writes: [], routeErrors: [] };
  const outDir = testInfo.outputPath("ui-crawl");
  const context = await browser.newContext({ viewport: null, serviceWorkers: "block" });
  const routes = await installRoutes(context, { guard, origins, stats, fixture: fixtureHandler(fixture) });
  const states = selectStates(expandStates(inventory), { ciOnly: true });
  const crawler = new Crawler({
    context, cdpFactory: (page) => context.newCDPSession(page), guard, inventory, outDir, widths: inventory.widths.ci,
    axeSource: fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8"), fixtureMode: true, settleMs: 400,
    log: (line) => process.stdout.write(`${line}\n`),
  });
  const targets = [
    { page: "main", ready: "main", label: "fixture/main", url: `${origins.ui}/` },
    { page: "fabric", ready: "fabric", label: "fixture/fabric", url: `${origins.ui}/sas-fabric` },
    { page: "history", ready: "history", label: "fixture/history", url: `${origins.history}/` },
    { page: "admin", ready: "admin", label: "fixture/admin", url: `${origins.admin}/` },
    { page: "offline", ready: "main", label: "fixture/offline", url: `${origins.offline}/` },
  ];
  try {
    for (const target of targets) await crawler.crawlTarget(target, states.filter((state) => state.page === target.page));
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
  expect(stats.blocked, "the fixture pages made requests outside the synthetic origins").toEqual([]);
  expect(crawler.records.filter((record) => record.status === "done").length).toBeGreaterThan(20);

  const unexplained = allFindings(crawler.records).filter((finding) => finding.severity === "error" && !finding.known);
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
