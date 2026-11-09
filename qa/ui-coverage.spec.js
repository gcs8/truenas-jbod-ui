"use strict";

// UI coverage (#ui-coverage): every interactive control must be in the state
// inventory (qa/ui-states.json), every backend route in the reviewed route
// manifest (qa/ui-routes.json), and a representative subset of states is
// crawled on synthetic fixtures with the automatic layout, heat-map and
// contrast checks. The crawl also proves that each state only called routes
// its safety class allows, and that every inventoried control actually
// rendered somewhere. Complete production templates and assets; only HTTP and
// the data are synthetic. No stack, no appliance. See CONTRIBUTING.md, UI coverage.

const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const controls = require("./ui-crawl/controls");
const { loadInventory, validateInventory, expandStates, selectStates, checkObserved, readsFor } = require("./ui-crawl/inventory");
const routes = require("./ui-crawl/routes");
const { RequestGuard } = require("./ui-crawl/guard");
const { installRoutes, fixtureHandler } = require("./ui-crawl/transport");
const { Crawler } = require("./ui-crawl/crawler");
const { writeReport, allFindings, unexplainedErrors } = require("./ui-crawl/report");

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

test("every backend route has a reviewed safety class and every browser API call matches one", () => {
  const manifest = routes.loadManifest();
  expect(routes.discoverRouteFiles(root), "a Python file declares routes but is not in qa/ui-routes.json `sources`").toEqual(Object.keys(manifest.sources).sort());
  expect(routes.checkManifest(routes.scanRoutes(root, manifest), manifest), "Review the route's class in qa/ui-routes.json (see qa/ui-crawl/routes.js) and update its fingerprint").toEqual([]);
  expect(routes.checkScriptApiPaths(root, controls.UI_SOURCES.scripts, manifest)).toEqual([]);
});

// A synthetic page with one instance of each defect shape the checks exist
// for, modelled on #1102 and #1103. Every check must report its case.
const DEFECTS_HTML = `<!doctype html><html><head><meta charset="utf-8"><style>
  body { margin: 0; font: 14px sans-serif; background: #111; color: #ddd; }
  .head { display: flex; width: 600px; } .head h2 { flex: 0 1 24px; min-width: 0; margin: 0; font-size: 22px; overflow-wrap: anywhere; }
  .head .controls { flex: 1 0 540px; }
  .row { display: grid; grid-template-columns: 200px 90px 220px 200px; width: 900px; gap: 8px; }
  .card { border: 1px solid #555; } .card dl { display: grid; grid-template-columns: 38px 1fr; margin: 0; }
  .card dt { overflow-wrap: anywhere; } .empty-col { grid-column: 4; }
  .wide { width: 2400px; height: 4px; }
  .over { position: relative; height: 40px; } .over button { position: absolute; top: 0; width: 120px; }
  .over button + button { left: 60px; }
  .trunc { display: block; width: 80px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .slot-tile { display: inline-block; width: 90px; height: 60px; position: relative; background: #11161a; }
  .slot-tile span { display: block; color: rgb(149, 163, 179); }
</style></head><body>
  <div class="head"><h2 id="title">Demo Enclosure Title</h2><div class="controls"><button>Heat Map</button></div></div>
  <div class="row"><div class="card">HBA</div><div class="card"><dl><dt>Remote</dt><dd>01</dd><dt>Handle</dt><dd>0043</dd></dl></div><div class="card empty-col">EXPANDER</div></div>
  <div class="wide"></div>
  <div class="over"><button>First</button><button>Second</button></div>
  <span class="trunc">A long truncated label without a tooltip</span>
  <select id="heatmap-metric-select"><option selected>Temperature</option></select>
  <button class="slot-tile heatmap-active" style="--heatmap-rgb: 210, 74, 74; --heatmap-alpha: 0.75"><span class="slot-heatmap-value">37 C</span><span>pool-a</span></button>
  <button class="slot-tile heatmap-active" style="--heatmap-rgb: 47, 110, 187; --heatmap-alpha: 0.5"><span class="slot-heatmap-value">36 C</span><span>pool-a</span></button>
</body></html>`;

test("the layout and heat-map checks report each synthetic defect shape", async ({ page }) => {
  await page.route("**/*", (route) => route.fulfill({ contentType: "text/html; charset=utf-8", body: DEFECTS_HTML }));
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto("https://defects.synthetic.invalid/");
  const { pageChecks } = require("./ui-crawl/checks");
  const result = await page.evaluate(pageChecks, {});
  const types = new Set(result.findings.map((finding) => finding.type));
  for (const type of [
    "text-narrow-wrap", "text-mid-word-break", "empty-track-squeeze", "page-horizontal-overflow", "control-overlap",
    "text-truncated-no-tooltip", "heat-alarm-healthy-temp", "heat-small-range-spread", "heat-tile-contrast",
  ]) {
    expect(types.has(type), `${type} not reported: ${JSON.stringify(result.findings.map((f) => [f.type, f.selector]))}`).toBe(true);
  }
  const midWord = result.findings.find((finding) => finding.type === "text-mid-word-break");
  expect(midWord.detail.word).toMatch(/Remote|Handle/);
});

// The app's own heat-map colour and band functions, loaded from app.js so the
// heat fixtures below use exactly the colours, bands and badge chips a real
// tile gets (applyHeatmapToTile), not hand-copied numbers.
function appHeatApi() {
  const source = fs.readFileSync(path.join(root, "app/static/app.js"), "utf8");
  const functionSource = (name) => {
    const start = source.indexOf(`function ${name}(`);
    if (start < 0) throw new Error(`app.js has no function ${name}`);
    let depth = 0;
    let quote = null;
    for (let index = source.indexOf("{", source.indexOf(")", start)); index < source.length; index += 1) {
      const character = source[index];
      if (quote) {
        if (character === "\\") index += 1;
        else if (character === quote) quote = null;
      } else if (character === "'" || character === "\"" || character === "`") quote = character;
      else if (character === "{") depth += 1;
      else if (character === "}" && --depth === 0) return source.slice(start, index + 1);
    }
    throw new Error(`app.js function ${name} has no complete body`);
  };
  const names = [
    "nullableMetricNumber", "heatmapTemperatureBandDefinitions", "heatmapTemperatureDefaultProfiles", "heatmapTemperatureProfileId",
    "heatmapTemperatureThresholdValue", "resolveHeatmapTemperatureThresholds", "heatmapTemperatureBand", "heatmapRelativeLuminance",
    "heatmapContrastRatio", "heatmapSurfaceRgb", "heatmapReadableTextRgb", "heatmapBadgeRgb",
  ];
  const sandbox = require("node:vm").createContext({});
  require("node:vm").runInContext(`${names.map(functionSource).join("\n")}\nthis.api = { ${names.join(", ")} };`, sandbox);
  return sandbox.api;
}

const HEAT_FINDINGS = ["heat-small-range-spread", "heat-band-colour-mismatch", "heat-tile-contrast", "heat-alarm-healthy-temp"];

function heatLegendHtml(api) {
  return `<ul id="heatmap-legend-bands" class="heatmap-legend-bands" aria-label="Temperature bands">${api.heatmapTemperatureBandDefinitions()
    .map((band) => `<li data-band="${band.id}"><span aria-hidden="true" style="--heatmap-band-rgb: ${band.rgb.join(", ")}"></span>${band.label}</li>`).join("")}</ul>`;
}

// A temperature-band tile exactly as applyHeatmapToTile() paints it.
function bandTileHtml(api, slot, value, extra = "", data = {}) {
  const band = api.heatmapTemperatureBand(value, api.resolveHeatmapTemperatureThresholds(data, {}));
  const surface = api.heatmapSurfaceRgb(band.rgb, band.alpha);
  const vars = {
    rgb: band.rgb, alpha: [band.alpha], "border-alpha": [0.9], "surface-rgb": surface,
    "text-rgb": api.heatmapReadableTextRgb(surface), "badge-rgb": api.heatmapBadgeRgb(surface),
  };
  const style = Object.entries(vars).map(([name, list]) => `--heatmap-${name}: ${Array.from(list).join(", ")}`).join("; ");
  return `<button class="slot-tile heatmap-active" data-slot="${slot}" data-heatmap-band="${band.id}" style="${style}">`
    + `<span class="slot-number">${String(slot).padStart(2, "0")}</span><span class="slot-device">sd${"abcdefgh"[slot]}</span>`
    + `<span class="slot-pool">pool-a</span>${extra}<span class="slot-heatmap-value">${Math.round(value)} C</span></button>`;
}

async function runChecksOn(page, html) {
  await page.route("**/*", (route) => route.fulfill({ contentType: "text/html; charset=utf-8", body: html }));
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto("https://heat.synthetic.invalid/");
  const { pageChecks } = require("./ui-crawl/checks");
  return page.evaluate(pageChecks, {});
}

test("valid band rendering passes: 49 C warm next to 50 C warning, and labels on their own painted chips", async ({ page }) => {
  const api = appHeatApi();
  // The fixture models Codex's two cases on the real HDD defaults.
  const thresholds = api.resolveHeatmapTemperatureThresholds({}, {});
  expect([49, 50].map((value) => api.heatmapTemperatureBand(value, thresholds).id)).toEqual(["warm", "warning"]);
  const warm = api.heatmapTemperatureBandDefinitions().find((band) => band.id === "warm");
  const warmSurface = api.heatmapSurfaceRgb(warm.rgb, warm.alpha);
  const badgeText = [244, 247, 251]; // .slot-heatmap-value colour #f4f7fb in style.css
  expect(api.heatmapContrastRatio(badgeText, warmSurface), "the badge text would fail on the bare warm surface").toBeLessThan(4.5);
  expect(api.heatmapContrastRatio(badgeText, api.heatmapBadgeRgb(warmSurface))).toBeGreaterThan(10.5);

  // The production stylesheet paints the chips (badge, number, device, pool).
  const css = fs.readFileSync(path.join(root, "app/static/style.css"), "utf8");
  // A semi-transparent chip: composited over the warm surface it is the badge colour.
  const translucent = `<span class="translucent-note" style="position: relative; z-index: 6; background: rgba(5, 8, 12, 0.62); color: #f4f7fb">note</span>`
    // A gradient chip like the boot-media cards: measured at each colour stop.
    + `<span class="gradient-note" style="position: relative; z-index: 6; background: radial-gradient(circle, rgba(76, 216, 164, 0.12), transparent 28%), linear-gradient(180deg, rgba(12, 36, 30, 0.96), rgba(9, 21, 19, 0.96)); color: #f4f7fb">boot</span>`;
  // Room for every label (bay height is the chassis view's business, not this check's).
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>${css}</style><style>.slot-tile { height: auto; min-height: 220px; }</style></head><body>
    <select id="heatmap-metric-select"><option selected>Temperature</option></select>${heatLegendHtml(api)}
    <div class="slot-grid">${bandTileHtml(api, 1, 49, translucent)}${bandTileHtml(api, 2, 50)}</div></body></html>`;
  const result = await runChecksOn(page, html);
  const heatFindings = result.findings.filter((finding) => HEAT_FINDINGS.includes(finding.type));
  expect(heatFindings, JSON.stringify(heatFindings)).toEqual([]);
  expect(result.heat).toMatchObject({ active: 2, temperature: true, bands: { warm: 1, warning: 1 } });
  // Every label measured (number, device, pool, badge; plus two notes on bay 1), none skipped.
  expect(result.heat).toMatchObject({ contrastChecked: 10, contrastUnmeasured: 0 });
  expect(result.heat.minContrast).toBeGreaterThanOrEqual(4.5);
});

test("heat checks still flag same-band alarm spreads, band colour mismatches and low-contrast tile text", async ({ page }) => {
  const api = appHeatApi();
  const bands = Object.fromEntries(api.heatmapTemperatureBandDefinitions().map((band) => [band.id, band]));
  const red = [210, 74, 74];
  const redSurface = api.heatmapSurfaceRgb(red, 0.75);
  const translucentOverRed = api.heatmapSurfaceRgb([255, 255, 255], 0.1, redSurface);
  const tile = (slot, value, rgb, alpha, inner) => `<button class="slot-tile heatmap-active" data-slot="${slot}" data-heatmap-band="normal" `
    + `style="--heatmap-rgb: ${rgb.join(", ")}; --heatmap-alpha: ${alpha}"><span class="slot-heatmap-value">${value} C</span>${inner}</button>`;
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>
    body { margin: 0; font: 14px sans-serif; background: #111; color: #ddd; }
    .slot-tile { display: inline-block; width: 120px; height: 90px; position: relative; background: #11161a; color: #f4f7fb; }
    .slot-tile span { display: block; }
    .slot-heatmap-value { background: rgb(23, 26, 30); color: #f4f7fb; }
  </style></head><body><select id="heatmap-metric-select"><option selected>Temperature</option></select>${heatLegendHtml(api)}
    ${tile(1, 36, red, 0.75, `<span class="bare" style="color: rgb(149, 163, 179)">pool-a</span><span class="veil" style="background: rgba(255, 255, 255, 0.1); color: rgb(149, 163, 179)">veil</span>`)}
    ${tile(2, 37, bands.normal.rgb, bands.normal.alpha, `<span class="own-chip" style="background: rgb(200, 200, 200); color: #f4f7fb">pool-b</span>`
      + `<span class="gradient-chip" style="background: linear-gradient(90deg, rgb(20, 20, 20), rgb(230, 230, 230)); color: #f4f7fb">grad</span>`
      + `<span class="image-chip" style="background: url(data:image/gif;base64,R0lGODlhAQABAAAAACw=) rgb(0, 0, 0); color: #f4f7fb">img</span>`)}
  </body></html>`;
  const result = await runChecksOn(page, html);
  const of = (type) => result.findings.filter((finding) => finding.type === type);
  const summary = JSON.stringify(result.findings.map((finding) => [finding.type, finding.selector, finding.detail]));
  // Two healthy disks 1 C apart, both in the normal band, one painted an alarm colour.
  expect(of("heat-small-range-spread").length, summary).toBe(1);
  expect(of("heat-small-range-spread")[0].detail).toMatchObject({ band: "normal", min: 36, max: 37 });
  expect(of("heat-band-colour-mismatch").map((finding) => finding.detail), summary).toEqual([
    { band: "normal", value: 36, rgb: red.join(","), expected: bands.normal.rgb.join(",") },
  ]);
  const contrast = Object.fromEntries(of("heat-tile-contrast").map((finding) => [finding.selector.split(" > ").pop(), finding.detail.background]));
  expect(contrast, summary).toEqual({
    "span.bare": redSurface.join(","), // no own background: the tile's heat surface
    "span.veil": translucentOverRed.join(","), // a semi-transparent layer composited over that surface
    "span.own-chip": "200,200,200", // its own opaque background
    "span.gradient-chip": "230,230,230", // a gradient's worst colour stop
  });
  // An image has no single colour: counted as unmeasured, never guessed.
  expect(result.heat.contrastUnmeasured).toBe(1);
});

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
    { page: "main", ready: "main", variant: "storage-views-error", label: "fixture/main-storage-views-error", url: `${origins["ui-storage-views-error"]}/` },
    // Sol #1108: a QuantaStor page sends a forced startup read; only its declared page-load window admits it.
    { page: "main", ready: "main", variant: "quantastor", platform: "quantastor", label: "fixture/main-quantastor", url: `${origins["ui-quantastor"]}/` },
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
    if (state) misclassified.push(...checkObserved(state, [...seen.keys()], readsFor(inventory, state)));
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
