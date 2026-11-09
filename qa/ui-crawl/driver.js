"use strict";

// Step executor for one Playwright page (#ui-coverage). The vocabulary is the
// `steps` language of qa/ui-states.json; every step is explicit and small.
// Write admission is not decided here: the RequestGuard sees every request.

const { substitute, stepKind } = require("./inventory");

class NotApplicable extends Error {
  constructor(message) {
    super(message);
    this.name = "NotApplicable";
  }
}

function pathGlob(pattern) {
  return new RegExp(`^${String(pattern).replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, "[^/]+")}$`);
}

// The bounded busy-retry policy, shared with live target discovery
// (scripts/ui_crawl.js, Codex PRRT_kwDOSAqK-M61Jv7a).
const BUSY_RETRY = Object.freeze({ attempts: 4, maxWaitMs: 10_000 });
function busyRetryWaitMs(retryAfterHeader, attempt, maxWaitMs = BUSY_RETRY.maxWaitMs) {
  const retryAfter = Number(retryAfterHeader) || 3;
  return Math.min(maxWaitMs, retryAfter * 1000 * attempt);
}

// Navigate or reload, retrying a document that answers 503 "busy" (the app
// sends Retry-After while a refresh holds the snapshot). Each retry is
// recorded by the crawler as an http-error finding, so busy pages stay visible.
async function navigate(page, action, { attempts = BUSY_RETRY.attempts, maxWaitMs = BUSY_RETRY.maxWaitMs } = {}) {
  let response = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    response = await action();
    if (!response || response.status() !== 503 || attempt === attempts) return response;
    await page.waitForTimeout(busyRetryWaitMs(response.headers()["retry-after"], attempt, maxWaitMs));
  }
  return response;
}

function stripKeys(value, ignore) {
  if (Array.isArray(value)) return value.map((item) => stripKeys(item, ignore));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).filter(([key]) => !ignore.has(key)).map(([key, item]) => [key, stripKeys(item, ignore)]));
  }
  return value === undefined ? null : value;
}

class CrawlDriver {
  constructor({ page, guard, selections = {}, ready, timeoutMs = 20_000, downloadDir = null, log = () => {} }) {
    this.page = page;
    this.guard = guard;
    this.selections = selections;
    this.ready = ready;
    this.timeoutMs = timeoutMs;
    this.downloadDir = downloadDir;
    this.log = log;
    this.vars = {};
    this.responses = [];
    this.mark = 0;
    this.dialogsSeen = 0;
    this.dialogTexts = [];
    this.acceptNextDialog = false;
    this.downloads = [];
    // Scope of the target page: writes are bound to it (#1120).
    this.bind = {};
    this.service = "ui";
    page.on("dialog", async (dialog) => {
      this.dialogsSeen += 1;
      this.dialogTexts.push(`${dialog.type()}: ${dialog.message().slice(0, 160)}`);
      const accept = this.acceptNextDialog && dialog.type() === "confirm";
      this.acceptNextDialog = false;
      try {
        if (accept) await dialog.accept();
        else await dialog.dismiss();
      } catch {
        // The page may already be gone.
      }
    });
    page.on("response", (response) => {
      const request = response.request();
      const entry = { method: request.method(), url: response.url(), status: response.status(), json: null };
      this.responses.push(entry);
      if (/\/led$/.test(new URL(entry.url).pathname) && entry.method === "POST") {
        response.json().then((json) => { entry.json = json; }).catch(() => {});
      }
    });
  }

  resetState() {
    this.vars = {};
    this.mark = this.responses.length;
    this.acceptNextDialog = false;
  }

  pause(ms) {
    return this.page.waitForTimeout(ms);
  }

  // Let a click's asynchronous handlers send whatever they will send.
  settle(ms = 600) {
    return this.page.waitForTimeout(ms);
  }

  // #1118: the app's own read API for the stores a state writes, read fresh
  // (never from the harness cache) in the target's scope. The values must be
  // equal before the write and after the revert.
  async semanticSnapshot(reads) {
    const out = {};
    for (const read of reads) {
      const answer = await this.page.evaluate(async ({ path, bind }) => {
        const params = new URLSearchParams(location.search);
        const query = new URLSearchParams();
        for (const key of ["system_id", "enclosure_id"]) {
          const value = bind[key] || params.get(key);
          if (value) query.set(key, value);
        }
        try {
          const response = await fetch(`${path}${query.toString() ? `?${query}` : ""}`, { headers: { Accept: "application/json", "X-UI-Crawl-Fresh": "1" }, cache: "no-store" });
          if (!response.ok) return { error: `${response.status}` };
          return { json: await response.json() };
        } catch (error) {
          return { error: String(error) };
        }
      }, { path: read.path, bind: this.bind || {} });
      if (!answer || answer.error) throw new Error(`${read.path} answered ${answer ? answer.error : "nothing"}`);
      out[read.path] = stripKeys(read.pick ? answer.json[read.pick] : answer.json, new Set(read.ignore || []));
    }
    return out;
  }

  visible(selector) {
    return this.page.locator(selector).filter({ visible: true });
  }

  async present(selector) {
    return (await this.visible(selector).count()) > 0;
  }

  // Fail closed: the notes probe runs only on a bay with no stored mapping of
  // any kind (saved or imported) and no notes, because its revert deletes the
  // mapping it created.
  async checkPrecondition(precondition, vars) {
    if (!precondition.noManualMapping) return null;
    if (/assignment/i.test(String(vars.detailText || ""))) return "the bay shows a stored assignment";
    const slot = await this.page.evaluate(async (slotNumber) => {
      try {
        const params = new URLSearchParams(location.search);
        const query = new URLSearchParams();
        for (const key of ["system_id", "enclosure_id"]) if (params.get(key)) query.set(key, params.get(key));
        const response = await fetch(`/api/inventory?${query}`, { headers: { Accept: "application/json", "X-UI-Crawl-Fresh": "1" }, cache: "no-store" });
        if (!response.ok) return { error: response.status };
        const data = await response.json();
        const match = ((data.snapshot || data).slots || []).find((item) => Number(item.slot) === slotNumber);
        return match ? { mapping_source: match.mapping_source || null, notes: match.notes || null } : { error: "slot not found" };
      } catch (error) {
        return { error: String(error) };
      }
    }, Number(vars.slot));
    if (!slot || slot.error) return `could not read the bay's mapping (${slot ? slot.error : "no answer"})`;
    if (["manual", "manual_mapping", "import"].includes(String(slot.mapping_source))) return `the bay has a stored mapping (${slot.mapping_source})`;
    if (slot.notes) return "the bay already has notes";
    return null;
  }

  async runSteps(steps, vars = this.vars) {
    for (const raw of steps || []) {
      const step = substitute(raw, vars);
      const kind = stepKind(step);
      if (!kind) throw new Error(`unknown step ${JSON.stringify(raw)}`);
      await this[`step_${kind}`](step, vars);
    }
  }

  async actOn(locator, action) {
    this.mark = this.responses.length;
    await action(locator.first());
  }

  async step_click(step) {
    const locator = this.visible(step.click);
    if (!(await locator.count())) throw new Error(`no visible ${step.click}`);
    if (!(await locator.first().isEnabled())) throw new NotApplicable(`${step.click} is disabled here`);
    await this.actOn(locator, (el) => el.click({ timeout: this.timeoutMs }));
  }

  async step_clickIfPresent(step) {
    const locator = this.visible(step.clickIfPresent);
    if (await locator.count()) await this.actOn(locator, (el) => el.click({ timeout: this.timeoutMs }));
  }

  async step_select(step) {
    const locator = this.visible(step.select);
    const option = step.value !== undefined ? { value: String(step.value) } : { index: step.index || 0 };
    await this.actOn(locator, (el) => el.selectOption(option, { timeout: this.timeoutMs }));
  }

  async step_selectIfVisible(step) {
    const locator = this.visible(step.selectIfVisible);
    if (await locator.count()) await this.actOn(locator, (el) => el.selectOption({ value: String(step.value) }, { timeout: this.timeoutMs }));
  }

  async step_selectBay(step, vars) {
    let selector;
    if (step.selectBay === "same") {
      if (!vars.slotSelector) throw new Error("selectBay same before any bay was selected");
      selector = vars.slotSelector;
    } else {
      selector = this.selections[step.selectBay];
      if (!selector) throw new Error(`unknown selection ${step.selectBay}`);
    }
    const locator = this.visible(selector);
    if (!(await locator.count())) throw new NotApplicable(`no ${step.selectBay} bay in this scope`);
    const slot = await locator.first().getAttribute("data-slot");
    vars.slot = slot;
    vars.slotSelector = `#slot-grid [data-slot="${slot}"]`;
    const selected = await locator.first().evaluate((el) => el.classList.contains("selected") || el.getAttribute("aria-pressed") === "true");
    if (!selected) await this.actOn(locator, (el) => el.click({ timeout: this.timeoutMs }));
  }

  // Applicability that only shows after earlier steps (for example the
  // history toggle appears once a bay is selected).
  async step_requireVisible(step) {
    const locator = this.visible(step.requireVisible);
    try {
      await locator.first().waitFor({ state: "visible", timeout: step.timeoutMs || 5000 });
    } catch {
      throw new NotApplicable(`${step.requireVisible} is not shown here`);
    }
  }

  async step_open(step) {
    await this.page.locator(step.open).first().evaluate((el) => { el.open = true; });
  }

  async step_openAll(step) {
    await this.page.locator(step.openAll).evaluateAll((nodes) => nodes.forEach((node) => { node.open = true; }));
  }

  async step_fill(step) {
    await this.actOn(this.visible(step.fill), (el) => el.fill(String(step.text), { timeout: this.timeoutMs }));
  }

  async step_press(step) {
    this.mark = this.responses.length;
    await this.page.keyboard.press(step.press);
  }

  async step_wait(step) {
    await this.page.waitForTimeout(Number(step.wait));
  }

  async step_waitFor(step) {
    await this.page.locator(step.waitFor).first().waitFor({ state: "visible", timeout: this.timeoutMs });
  }

  async step_waitText(step) {
    const locator = this.page.locator(step.waitText).first();
    const deadline = Date.now() + this.timeoutMs;
    while (Date.now() < deadline) {
      const text = (await locator.textContent().catch(() => "")) || "";
      if (text.includes(step.contains)) return;
      await this.page.waitForTimeout(250);
    }
    throw new Error(`${step.waitText} never contained "${step.contains}"`);
  }

  async step_waitResponse(step) {
    const want = step.waitResponse;
    const re = pathGlob(want.path);
    const deadline = Date.now() + Math.max(this.timeoutMs, 60_000);
    while (Date.now() < deadline) {
      const hit = this.responses.slice(this.mark).find((r) => r.method === String(want.method).toUpperCase() && re.test(new URL(r.url).pathname));
      if (hit) {
        if (hit.status >= 400) throw new Error(`${want.method} ${want.path} answered ${hit.status}`);
        return;
      }
      await this.page.waitForTimeout(250);
    }
    throw new Error(`no response for ${want.method} ${want.path}`);
  }

  async step_check(step) {
    const locator = this.visible(step.check);
    // A disabled toggle (for example a single-enclosure export scope) stays as it is.
    if (!(await locator.count()) || !(await locator.first().isEnabled())) return;
    await this.actOn(locator, (el) => el.check({ timeout: this.timeoutMs }));
  }

  async step_uncheck(step) {
    const locator = this.page.locator(step.uncheck).first();
    if (await locator.isChecked()) {
      this.mark = this.responses.length;
      await locator.uncheck({ timeout: this.timeoutMs });
    }
  }

  async step_hover(step) {
    const locator = this.visible(step.hover);
    if (!(await locator.count())) throw new NotApplicable(`nothing to hover: ${step.hover}`);
    await locator.first().hover({ timeout: this.timeoutMs });
  }

  async step_setRange(step) {
    await this.actOn(this.visible(step.setRange), (el) => el.evaluate((node, value) => {
      node.value = value;
      node.dispatchEvent(new Event("input", { bubbles: true }));
      node.dispatchEvent(new Event("change", { bubbles: true }));
    }, String(step.value)));
  }

  async step_forEachOption(step) {
    // Expanded by the crawler before the state runs; a bare one selects the
    // value the crawler substituted.
    if (step.value === undefined) throw new Error("forEachOption must be expanded by the crawler");
    await this.actOn(this.visible(step.forEachOption), (el) => el.selectOption({ value: String(step.value) }));
  }

  async step_read(step, vars) {
    const locator = this.page.locator(step.read).first();
    let value = await locator.evaluate((el, property) => {
      if (property) return el[property];
      return "value" in el && el.tagName !== "BUTTON" ? el.value : el.innerText;
    }, step.property || null);
    // `prefix` strips a value prefix ("enclosure:<id>"; any other value, a
    // storage view, stays and cannot match the target), `before` keeps the
    // part before a separator (the persistent "<id>::<path>" base).
    if (step.prefix && String(value).startsWith(step.prefix)) value = String(value).slice(step.prefix.length);
    if (step.before) value = String(value).split(step.before, 1)[0];
    vars[step.as] = value;
  }

  async step_reload() {
    // A reload is a page load: its load-time requests (Sol #1108) are judged
    // in the same page-load window as the first navigation.
    const reload = async () => {
      await navigate(this.page, () => this.page.reload({ waitUntil: "domcontentloaded", timeout: 300_000 }));
      if (this.ready) await this.ready(this.page);
    };
    await (this.withLoadWindow ? this.withLoadWindow(reload) : reload());
  }

  async step_expectValue(step) {
    const value = await this.page.locator(step.expectValue).first().inputValue();
    if (value !== String(step.equals)) throw new Error(`${step.expectValue} is "${value}", expected "${step.equals}"`);
  }

  async step_acceptNextDialog() {
    this.acceptNextDialog = true;
  }

  async step_download(step, vars) {
    if (!this.downloadDir) throw new NotApplicable("downloads are off in this run");
    const locator = this.visible(step.download);
    this.mark = this.responses.length;
    const [download] = await Promise.all([
      this.page.waitForEvent("download", { timeout: 300_000 }),
      locator.first().click({ timeout: this.timeoutMs }),
    ]);
    const path = require("node:path");
    const target = path.join(this.downloadDir, `${step.as || "download"}-${Date.now()}.html`);
    await download.saveAs(target);
    this.downloads.push(target);
    vars[step.as || "download"] = target;
  }

  // Verify the locate light reads back off: the CLEAR reply says so, and a
  // fresh inventory read (cached, not forced) no longer shows identify.
  async step_ledOff(step, vars) {
    const replies = this.responses.filter((r) => r.method === "POST" && /\/led$/.test(new URL(r.url).pathname));
    const last = replies[replies.length - 1];
    const deadline = Date.now() + 10_000;
    while (last && last.json === null && Date.now() < deadline) await this.page.waitForTimeout(200);
    // Some backends (TrueNAS API, BMC) give no immediate readback
    // (identify_active null); then the inventory re-read below decides.
    if (!last || !last.json || last.json.identify_active === true || last.status >= 400) {
      throw new Error(`locate light not confirmed off (reply ${last ? JSON.stringify(last.json) : "missing"})`);
    }
    const slot = Number(vars.slot);
    const until = Date.now() + 45_000;
    let seen = null;
    while (Date.now() < until) {
      seen = await this.page.evaluate(async (slotNumber) => {
        const params = new URLSearchParams(location.search);
        const query = new URLSearchParams();
        for (const key of ["system_id", "enclosure_id"]) if (params.get(key)) query.set(key, params.get(key));
        const response = await fetch(`/api/inventory?${query}`, { headers: { Accept: "application/json", "X-UI-Crawl-Fresh": "1" }, cache: "no-store" });
        const data = await response.json();
        const snapshot = data.snapshot || data;
        const match = (snapshot.slots || []).find((item) => Number(item.slot) === slotNumber);
        return match ? { state: match.state, identify_active: match.identify_active } : null;
      }, slot);
      if (seen && seen.identify_active !== true && seen.state !== "identify") return;
      await this.page.waitForTimeout(3000);
    }
    throw new Error(`inventory still shows identify for bay ${slot}: ${JSON.stringify(seen)}`);
  }
}

module.exports = { CrawlDriver, NotApplicable, navigate, stripKeys, BUSY_RETRY, busyRetryWaitMs };
