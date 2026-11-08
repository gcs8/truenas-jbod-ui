"use strict";

// UI coverage crawler engine (#ui-coverage).
//
// For each target page (a URL for one page of one system/scope) and each
// inventory state: open a fresh document, run the state through the
// actuation runner, then for every width resize the same document, run the
// layout/heat/contrast checks, and capture a full-page PNG plus native
// resolution tiles. Results go to findings.json and index.html (report.js).

const fs = require("node:fs");
const path = require("node:path");
const { runState } = require("./actuation");
const { CrawlDriver, NotApplicable, navigate } = require("./driver");
const { pageChecks, axeFindings } = require("./checks");
const { readsFor, loadRequestsFor } = require("./inventory");
const png = require("./png");

const READY = {
  async main(page, timeout = 240_000) {
    await page.waitForFunction(() => {
      const grid = document.getElementById("slot-grid");
      return grid && grid.getAttribute("aria-busy") !== "true" && grid.querySelectorAll(".slot-tile[data-slot]").length > 0;
    }, null, { timeout, polling: 500 });
    const toggle = page.locator("#auto-refresh-toggle");
    if ((await toggle.count()) && (await toggle.isVisible()) && (await toggle.isChecked())) await toggle.uncheck();
  },
  async fabric(page, timeout = 240_000) {
    await page.waitForFunction(() => {
      const map = document.getElementById("fabric-map-panel");
      return map && map.children.length > 0;
    }, null, { timeout, polling: 500 });
  },
  async history(page) {
    await page.waitForLoadState("domcontentloaded");
  },
  async admin(page, timeout = 60_000) {
    await page.waitForFunction(() => document.querySelector("[data-admin-view-button]"), null, { timeout: 60_000 });
    await page.waitForTimeout(500);
  },
};

// Inventory control keys -> what to look for in the DOM (visibility audit).
function keySpec(key) {
  let m;
  if ((m = /^#([\w-]+)$/.exec(key))) return { key, css: `[id="${m[1]}"]` };
  if ((m = /^\[([\w-]+)=([^\]]+)\]$/.exec(key))) return { key, css: `[${m[1]}="${m[2]}"]` };
  if ((m = /^\[([\w-]+)\]$/.exec(key))) return { key, css: `[${m[1]}]` };
  if ((m = /^\.([\w-]+)$/.exec(key))) return { key, css: `.${m[1]}` };
  if ((m = /^(\w+)"([^"]*)"@(.*)$/.exec(key))) {
    const scope = m[3].startsWith("#") ? `[id="${m[3].slice(1)}"] ` : "";
    return { key, css: `${scope}${m[1]}`, text: m[2] };
  }
  return { key, css: null };
}

// Runs in the page: which keys have a visible element right now. A control
// hidden by design (a file input) counts when its label or an element that
// names it in aria-controls/for is visible.
function renderedKeys(specs) {
  const visible = (node) => {
    if (!node || !node.getClientRects || node.getClientRects().length === 0) return false;
    const style = getComputedStyle(node);
    return style.visibility !== "hidden" && style.display !== "none";
  };
  const textOf = (node) => [node.innerText, node.value, node.getAttribute("aria-label"), node.getAttribute("type"), node.textContent]
    .filter((v) => typeof v === "string").join(" ");
  const out = [];
  for (const spec of specs) {
    if (!spec.css) continue;
    let nodes = [];
    try {
      nodes = Array.from(document.querySelectorAll(spec.css));
    } catch {
      continue;
    }
    const hit = nodes.some((node) => {
      if (spec.text && !textOf(node).includes(spec.text)) return false;
      if (visible(node)) return true;
      if (node.tagName === "DIALOG" || node.tagName === "DETAILS") return false;
      return Boolean(node.id) && Array.from(document.querySelectorAll(`label[for="${node.id}"]`)).some(visible);
    });
    if (hit) out.push(spec.key);
  }
  return out;
}

function safeName(value) {
  return String(value).replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 120);
}

class Crawler {
  constructor({
    context, cdpFactory, guard, inventory, outDir, widths, allow = [], confirmPreview = false, axeSource = null,
    axeWidths = null, tileSize = [1280, 720], baselineDir = null, budgets = {}, checkOptions = {}, fixtureMode = false,
    downloads = false, cache = null, log = console.log, settleMs = 700, paceMs = 0, ledger = null, writeSystem = null,
    renderAudit = false,
  }) {
    Object.assign(this, { context, cdpFactory, guard, inventory, outDir, widths, allow, confirmPreview, axeSource, tileSize, baselineDir, checkOptions, fixtureMode, cache, log, settleMs, paceMs, ledger, writeSystem });
    this.aborted = null;
    const keys = new Set();
    for (const state of inventory.states || []) for (const key of state.covers || []) keys.add(key);
    this.auditSpecs = renderAudit ? [...keys].sort().map(keySpec) : null;
    this.lastLoad = 0;
    this.readyTimeoutMs = fixtureMode ? 30_000 : 240_000;
    this.axeWidths = axeWidths || [widths[0]];
    this.budgets = { readyMs: 15_000, requestMs: 20_000, ...budgets };
    this.downloadDir = downloads ? path.join(outDir, "downloads") : null;
    this.records = [];
    this.actuations = [];
    this.bucket = null;
    fs.mkdirSync(outDir, { recursive: true });
    if (this.downloadDir) fs.mkdirSync(this.downloadDir, { recursive: true });
  }

  debug(message) {
    if (process.env.UI_CRAWL_DEBUG) this.log(`  [debug] ${message}`);
  }

  async openPage() {
    const page = await this.context.newPage();
    page.setDefaultTimeout(60_000);
    const push = (type, severity, text, detail) => this.bucket && this.bucket.push({ type, severity, text: String(text).slice(0, 300), detail });
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      // In fixture mode a missing synthetic answer is a fixture gap, not a product error.
      if (this.fixtureMode && /Failed to load resource: the server responded with a status of 404/.test(message.text())) return;
      // Requests the guard aborted are recorded by the guard, not as console errors.
      if (/net::ERR_BLOCKED_BY_CLIENT/.test(message.text())) return;
      push("console-error", "error", message.text());
    });
    page.on("pageerror", (error) => push("page-error", "error", error.message));
    page.on("requestfailed", (request) => {
      const failure = request.failure()?.errorText || "failed";
      if (/ERR_BLOCKED_BY_CLIENT|blockedbyclient/i.test(failure)) return; // recorded by the guard
      if (/ERR_ABORTED/.test(failure) && request.resourceType() === "document") return;
      push("request-failed", "error", `${request.method()} ${new URL(request.url()).pathname} ${failure}`);
    });
    page.on("requestfinished", async (request) => {
      const timing = request.timing();
      const elapsed = timing.responseEnd > 0 ? timing.responseEnd : null;
      if (elapsed && elapsed > this.budgets.requestMs) {
        push("slow-request", "warn", `${request.method()} ${new URL(request.url()).pathname}`, { ms: Math.round(elapsed), budgetMs: this.budgets.requestMs });
      }
    });
    page.on("response", (response) => {
      if (response.status() < 400) return;
      const url = new URL(response.url());
      const type = this.fixtureMode && response.status() === 404 && url.pathname.startsWith("/api/") ? "fixture-gap" : "http-error";
      push(type, type === "fixture-gap" ? "info" : "error", `${response.status()} ${response.request().method()} ${url.pathname}`);
    });
    this.page = page;
    this.cdp = await this.cdpFactory(page);
    return page;
  }

  async setWidth(width, height = 1000) {
    await this.cdp.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
    await this.page.waitForFunction(([w, h]) => window.innerWidth === w && window.innerHeight === h, [width, height], { timeout: 20_000, polling: 200 });
    await this.page.waitForTimeout(this.settleMs);
  }

  async capture(dir) {
    const metrics = await this.cdp.send("Page.getLayoutMetrics");
    const size = metrics.cssContentSize || metrics.contentSize;
    const clip = { x: 0, y: 0, width: Math.ceil(size.width), height: Math.min(Math.ceil(size.height), 16_000), scale: 1 };
    const shot = await this.cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, clip });
    const buffer = Buffer.from(shot.data, "base64");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "full.png"), buffer);
    const image = png.decode(buffer);
    const tiles = png.tiles(image, this.tileSize[0], this.tileSize[1]).map((tile, index) => {
      const name = `tile-${String(index).padStart(2, "0")}-x${tile.x}-y${tile.y}.png`;
      fs.writeFileSync(path.join(dir, name), tile.png);
      return { name, x: tile.x, y: tile.y, width: tile.width, height: tile.height };
    });
    return { image, size: [image.width, image.height], tiles };
  }

  baselineDiff(relDir, image) {
    if (!this.baselineDir) return null;
    const file = path.join(this.baselineDir, relDir, "full.png");
    if (!fs.existsSync(file)) return { missing: true };
    try {
      const result = png.diff(image, png.decode(fs.readFileSync(file)));
      return { ...result, changedFraction: Number(result.changedFraction.toFixed(4)) };
    } catch (error) {
      return { error: error.message };
    }
  }

  async runChecks(width, state) {
    const result = await this.page.evaluate(pageChecks, this.checkOptions);
    const findings = result.findings;
    if (this.axeSource && this.axeWidths.includes(width)) {
      try {
        const hasAxe = await this.page.evaluate(() => typeof window.axe !== "undefined");
        if (!hasAxe) await this.page.evaluate(this.axeSource);
        const axe = await this.page.evaluate(async () => window.axe.run(document, {
          runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
          resultTypes: ["violations"],
        }));
        findings.push(...axeFindings(axe));
      } catch (error) {
        // A required checker that did not run is not a clean result (Sol #1108).
        findings.push({ type: "check-error", severity: "error", text: `axe: ${String(error.message).split("\n")[0].slice(0, 200)}` });
      }
    }
    if (!(state.checks || []).includes("heatmap")) {
      for (const finding of findings) if (finding.type.startsWith("heat-")) finding.severity = "warn";
    }
    return { findings, heat: result.heat, document: result.document, viewport: result.viewport, scanned: result.scanned };
  }

  markKnown(record, state = {}) {
    // A fixture variant's deliberate failure (for example the storage-view
    // outage) is expected there, not a defect.
    const expected = state.expected_findings || [];
    for (const finding of [...record.findings, ...record.widths.flatMap((w) => w.findings)]) {
      if (expected.some((e) => e.type === finding.type && (!e.text || String(finding.text || "").includes(e.text)))) finding.expected = true;
    }
    for (const width of record.widths) {
      for (const finding of width.findings) {
        const stateMatches = (pattern) => (pattern.endsWith("*") ? record.state.startsWith(pattern.slice(0, -1)) : pattern === record.state || pattern === record.baseId);
        const known = (this.inventory.known_findings || []).find((entry) => stateMatches(entry.state)
          && entry.type === finding.type && (!entry.selector || String(finding.selector || "").includes(entry.selector)));
        if (known) finding.known = known.issue;
      }
    }
  }

  // Expand forEachOption steps: one sub-state per option of the select.
  async expandOptions(target, state) {
    const index = (state.steps || []).findIndex((step) => step.forEachOption);
    if (index < 0) return [state];
    const step = state.steps[index];
    this.guard.beginState(state.id, { reads: readsFor(this.inventory, state) });
    await this.load(target);
    const driver = this.newDriver(target);
    await driver.runSteps(state.steps.slice(0, index));
    const values = await this.page.locator(step.forEachOption).first().evaluate((select) => Array.from(select.options).filter((o) => !o.disabled).map((o) => o.value));
    return values.slice(0, step.max || 12).map((value) => ({
      ...state,
      id: `${state.id}{${value}}`,
      steps: [...state.steps.slice(0, index), { forEachOption: step.forEachOption, value }, ...state.steps.slice(index + 1)],
    }));
  }

  // One driver per page: it owns the page's dialog and response listeners,
  // which must not accumulate across states.
  newDriver(target) {
    if (!this.driver || this.driver.page !== this.page) {
      this.driver = new CrawlDriver({ page: this.page, guard: this.guard, selections: this.inventory.selections, downloadDir: this.downloadDir, log: this.log });
    }
    this.driver.ready = READY[target.ready] && ((page) => READY[target.ready](page, this.readyTimeoutMs));
    this.driver.withLoadWindow = (fn) => this.withLoadWindow(target, fn);
    this.driver.bind = this.bindFor(target);
    this.driver.service = (this.inventory.pages[target.page] || {}).service || "ui";
    this.driver.resetState();
    this.driver.downloads = [];
    this.driver.dialogTexts = [];
    return this.driver;
  }

  // #1120: bound requests are admitted only for the target's system (the
  // runner only gives write states the --write-system target) and the
  // target's own enclosure (from its URL). Fixture runs have no live scope.
  bindFor(target) {
    if (this.fixtureMode) return {};
    let enclosure = null;
    try {
      enclosure = new URL(target.url).searchParams.get("enclosure_id");
    } catch {
      enclosure = null;
    }
    return { system_id: target.system || this.writeSystem || null, enclosure_id: enclosure };
  }

  // Sol #1108: a page load may send requests of its own before any state
  // window opens (QuantaStor queues a forced inventory read at startup when its
  // bays are LED-capable, then a forced storage-view read once that answers,
  // which on a real appliance can be long after the page is ready). They are
  // admitted only when the page declares them for the target's platform
  // (qa/ui-states.json load_requests), once per load, bound to the target. The
  // window opens with each navigation or reload and stays open until the next
  // one or the end of the state; it admits nothing but those reads.
  async withLoadWindow(target, fn) {
    this.endLoadWindow();
    const rules = loadRequestsFor(this.inventory, target.page, target.platform);
    if (rules.length) {
      const service = ((this.inventory.pages || {})[target.page] || {}).service || "ui";
      this.guard.openLoad(this.guard.state || target.label, rules, { bind: this.bindFor(target), service });
      this.loadTarget = target.label;
    }
    return fn();
  }

  endLoadWindow() {
    if (!this.guard.load) return;
    const admitted = this.guard.closeLoad();
    if (admitted.length) (this.loadAdmitted = this.loadAdmitted || []).push(...admitted.map((entry) => ({ target: this.loadTarget, state: this.guard.state, ...entry })));
  }

  async load(target) {
    // Serialize and space page loads on a live stack (Archive load rules).
    const wait = this.lastLoad + this.paceMs - Date.now();
    if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
    this.lastLoad = Date.now();
    const started = Date.now();
    await this.withLoadWindow(target, async () => {
      this.debug(`goto ${target.label}`);
      await navigate(this.page, () => this.page.goto(target.url, { waitUntil: "domcontentloaded", timeout: 300_000 }));
      this.debug("width");
      await this.setWidth(this.widths[0]);
      this.debug("ready");
      if (READY[target.ready]) await READY[target.ready](this.page, this.readyTimeoutMs);
    });
    this.debug("loaded");
    return Date.now() - started;
  }

  async crawlTarget(target, states) {
    if (this.aborted) return;
    if (!this.page) await this.openPage();
    // A fixture variant page runs only its own states, and a default page none of them.
    const own = states.filter((state) => (state.variant || null) === (target.variant || null));
    for (const base of own) {
      if (this.aborted) return;
      let expanded;
      try {
        expanded = await this.expandOptions(target, base);
      } catch (error) {
        expanded = [base];
      }
      for (const state of expanded) {
        if (this.aborted) return;
        await this.crawlState(target, state);
      }
    }
  }

  async crawlState(target, state) {
    const relDir = path.join(safeName(target.label), safeName(state.id));
    const record = {
      target: target.label, page: target.page, state: state.id, baseId: state.baseId || state.id, safety: state.safety,
      status: null, widths: [], findings: [], startedAt: new Date().toISOString(),
    };
    this.bucket = record.findings;
    this.log(`${target.label} ${state.id}: start`);
    let driver = null;
    this.guard.beginState(state.id, { reads: readsFor(this.inventory, state) });
    try {
      const readyMs = await this.load(target);
      if (readyMs > this.budgets.readyMs) record.findings.push({ type: "slow-ready", severity: "warn", text: `${target.label} ready in ${readyMs} ms`, detail: { ms: readyMs, budgetMs: this.budgets.readyMs } });
      record.readyMs = readyMs;
      if (state.requires && !(await this.page.locator(state.requires).filter({ visible: true }).count())) {
        record.status = "not-applicable";
        record.reason = `requires ${state.requires}`;
      } else {
        driver = this.newDriver(target);
        driver.resetState();
        const outcome = await runState(driver, state, { allow: this.allow, confirmPreview: this.confirmPreview });
        record.status = outcome.status;
        Object.assign(record, { outcome });
        if (state.safety !== "read" && state.safety !== "forbidden" || outcome.status === "previewed" || outcome.status === "preview-failed") this.actuations.push({ target: target.label, ...outcome, dialogs: driver.dialogTexts });
        // #1117: a light that read back off leaves the ledger; anything else stays for the out-of-band sweep.
        if (state.safety === "led" && outcome.verified && this.ledger) this.ledger.disarm({ state: state.id });
        if (this.auditSpecs && outcome.status === "done") record.rendered = await this.page.evaluate(renderedKeys, this.auditSpecs).catch(() => []);
        if (outcome.status === "revert-failed") record.findings.push({ type: "revert-failed", severity: "error", text: outcome.error });
        if (driver.downloads.length) record.downloads = driver.downloads.map((file) => path.relative(this.outDir, file));
      }
    } catch (error) {
      if (error instanceof NotApplicable) {
        record.status = "not-applicable";
        record.reason = error.message;
      } else {
        record.status = "error";
        record.findings.push({ type: "state-error", severity: "error", text: String(error.message || error).split("\n")[0].slice(0, 300) });
      }
    }
    const capture = ["done", "previewed"].includes(record.status);
    if (capture) {
      for (const width of this.widths) {
        const entry = { width, findings: [] };
        try {
          await this.setWidth(width);
          const checks = await this.runChecks(width, state);
          Object.assign(entry, { heat: checks.heat, document: checks.document, scanned: checks.scanned });
          entry.findings = checks.findings;
          const dir = path.join(this.outDir, "states", relDir, String(width));
          const shot = await this.capture(dir);
          entry.full = path.relative(this.outDir, path.join(dir, "full.png"));
          entry.size = shot.size;
          entry.tiles = shot.tiles.map((tile) => ({ ...tile, file: path.relative(this.outDir, path.join(dir, tile.name)) }));
          entry.diff = this.baselineDiff(path.join("states", relDir, String(width)), shot.image);
          if (entry.diff && entry.diff.changedFraction > 0.02) entry.findings.push({ type: "visual-diff", severity: "info", text: `changed ${Math.round(entry.diff.changedFraction * 100)}% vs baseline`, detail: entry.diff });
        } catch (error) {
          entry.findings.push({ type: "capture-error", severity: "error", text: String(error.message || error).split("\n")[0].slice(0, 300) });
        }
        record.widths.push(entry);
      }
      try {
        await this.setWidth(this.widths[0]);
      } catch {
        // next state reloads anyway
      }
    }
    this.endLoadWindow();
    this.guard.endState();
    this.bucket = null;
    this.markKnown(record, state);
    record.finishedAt = new Date().toISOString();
    this.records.push(record);
    const counts = record.widths.reduce((sum, w) => sum + w.findings.filter((f) => f.severity === "error" && !f.known && !f.expected).length, 0) + record.findings.filter((f) => f.severity === "error" && !f.expected).length;
    this.log(`${target.label} ${state.id}: ${record.status}${record.reason ? ` (${record.reason})` : ""}, ${counts} error findings`);
    return record;
  }
}

module.exports = { Crawler, READY, safeName, keySpec, renderedKeys };
