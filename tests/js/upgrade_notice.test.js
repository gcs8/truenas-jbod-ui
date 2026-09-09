"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

function functionSource(name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = APP_SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
    if (lineComment) {
      lineComment = character !== "\n";
      continue;
    }
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "/" && APP_SOURCE[index + 1] === "/") {
      lineComment = true;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    if (character === "}") {
      depth -= 1;
      if (depth === 0) return APP_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

const FUNCTIONS = [
  "upgradeNoticeVersion",
  "upgradeNoticeDismissedLocally",
  "renderUpgradeNotice",
  "dismissUpgradeNotice",
];

function loadFunctions(context = {}) {
  const sandbox = vm.createContext({ ...context });
  const source = FUNCTIONS.map(functionSource).join("\n");
  const exports = FUNCTIONS.map((name) => `this.__${name} = ${name};`).join("\n");
  vm.runInContext(
    `const UPGRADE_NOTICE_STORAGE_KEY = "truenas-jbod-ui.upgrade-notice-dismissed";\n${source}\n${exports}`,
    sandbox,
    { filename: "upgrade-notice.behavior.js" },
  );
  return Object.fromEntries(FUNCTIONS.map((name) => [name, sandbox[`__${name}`]]));
}

function element(dataset = {}) {
  const classes = new Set();
  return {
    dataset,
    disabled: false,
    classList: {
      add(name) { classes.add(name); },
      toggle(name, force) {
        if (force === undefined ? !classes.has(name) : force) classes.add(name);
        else classes.delete(name);
      },
      contains(name) { return classes.has(name); },
    },
  };
}

function harness({ stored = null, fetchResult = () => Promise.resolve({ ok: true }) } = {}) {
  const upgradeNotice = element({ noticeVersion: "0.23.0" });
  const upgradeNoticeDismiss = element();
  const writes = [];
  const requests = [];
  const functions = loadFunctions({
    upgradeNotice,
    upgradeNoticeDismiss,
    state: { snapshotMode: false },
    loadStoredJson: () => stored,
    storeJson: (key, payload) => writes.push([key, payload]),
    fetchJson: (url, options) => {
      requests.push([url, options]);
      return fetchResult();
    },
  });
  return { ...functions, upgradeNotice, upgradeNoticeDismiss, writes, requests };
}

test("a pending notice stays visible until it is dismissed", () => {
  const h = harness();

  h.renderUpgradeNotice();

  assert.equal(h.upgradeNotice.classList.contains("hidden"), false);
});

test("a notice this browser already dismissed for the same version stays hidden", () => {
  const h = harness({ stored: { version: "0.23.0" } });

  h.renderUpgradeNotice();

  assert.equal(h.upgradeNotice.classList.contains("hidden"), true);
});

test("a dismissal remembered for an older version does not hide a new notice", () => {
  const h = harness({ stored: { version: "0.22.2" } });

  h.renderUpgradeNotice();

  assert.equal(h.upgradeNotice.classList.contains("hidden"), false);
});

test("dismissing hides the notice, remembers it locally and records it on the server", async () => {
  const h = harness();

  await h.dismissUpgradeNotice();

  assert.equal(h.upgradeNotice.classList.contains("hidden"), true);
  assert.equal(h.upgradeNoticeDismiss.disabled, true);
  assert.equal(h.writes.length, 1);
  assert.equal(h.writes[0][0], "truenas-jbod-ui.upgrade-notice-dismissed");
  assert.equal(h.writes[0][1].version, "0.23.0");
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0][0], "/api/upgrade-notice/dismiss");
  assert.equal(h.requests[0][1].method, "POST");
  assert.equal(h.requests[0][1].readUiAuth, true);
});

test("a refused server write still leaves the notice dismissed in this browser", async () => {
  const failure = new Error("Sign in to enable this write.");
  failure.status = 401;
  const h = harness({ fetchResult: () => Promise.reject(failure) });

  await assert.doesNotReject(() => h.dismissUpgradeNotice());

  assert.equal(h.upgradeNotice.classList.contains("hidden"), true);
  assert.equal(h.writes.length, 1);
});

test("the template renders one notice above the status strip only when one is pending", () => {
  const noticeStart = TEMPLATE.indexOf('id="upgrade-notice"');
  const stripStart = TEMPLATE.indexOf('id="status-strip"');
  assert.notEqual(noticeStart, -1);
  assert.ok(noticeStart < stripStart);
  const guard = TEMPLATE.lastIndexOf("{% if not snapshot_mode and upgrade_notice %}", noticeStart);
  assert.notEqual(guard, -1);
  assert.match(TEMPLATE, /id="upgrade-notice-dismiss"[^>]*type="button"/);
  assert.equal((TEMPLATE.match(/id="upgrade-notice"/g) || []).length, 1);
});
