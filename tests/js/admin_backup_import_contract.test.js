"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const TEMPLATE_PATH = path.resolve(__dirname, "../../admin_service/templates/index.html");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");
const TEMPLATE = fs.readFileSync(TEMPLATE_PATH, "utf8");

function functionSource(name) {
  const start = SOURCE.indexOf(`async function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = SOURCE.indexOf("{", SOURCE.indexOf(")", start));
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < SOURCE.length; index += 1) {
    const character = SOURCE[index];
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    if (character === "}") {
      depth -= 1;
      if (depth === 0) return SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

test("backup import inspects, confirms observed mode, then imports with the single-use receipt", () => {
  const source = functionSource("runImportBackup");
  const inspect = source.indexOf("/api/admin/backup/inspect");
  const confirm = source.indexOf("window.confirm");
  const importRequest = source.indexOf("/api/admin/backup/import");

  assert.ok(inspect >= 0, "inspection request is required");
  assert.ok(confirm > inspect, "operator confirmation must follow inspection");
  assert.ok(importRequest > confirm, "import must follow confirmation");
  assert.match(source, /inspection\.encryption_mode/);
  assert.match(source, /inspection\.inspection_receipt/);
  assert.match(source, /if \(!confirmed\)\s*\{\s*return;/);
  assert.match(source, /"X-Backup-Expected-Encryption": inspection\.encryption_mode/);
  assert.match(source, /"X-Backup-Inspection-Receipt": inspection\.inspection_receipt/);
});


const { loadAdminFunctions, transportNames, restoreNames, restoreResult, jsonResponse } = require("./helpers/admin_transport");

function uploadFixture({ imported = () => jsonResponse(200, restoreResult()), confirm = true, offline = false, fileError = false, refreshError = false } = {}) {
  const calls = [];
  const banners = [];
  const timers = new Map();
  let timerId = 0;
  let refreshes = 0;
  const navigator = { onLine: true };
  const state = { admin: {}, systems: [{ id: "original" }], defaultSystemId: "original", setupDirty: true };
  const elements = {
    backupImportButton: {}, backupImportResult: { textContent: "", append(text) { this.textContent += text; } },
    backupImportPassphrase: { value: " synthetic phrase " },
    backupImportStopToggle: { checked: true }, backupImportRestartToggle: { checked: true },
  };
  const api = loadAdminFunctions([
    ...transportNames, ...restoreNames, "runImportBackup", "readOptionalSecretValue",
    "restartFailureKeys", "describeMaintenanceOutcome", "maintenanceKeys", "describeServices", "serviceName",
  ], {
    state, elements, navigator,
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms }); return id; },
    clearTimeout: (id) => timers.delete(id),
    readSelectedImportFile: () => ({ name: "synthetic.tar.zst", arrayBuffer: async () => {
      if (fileError) throw new Error("Cannot read the selected file");
      return new Uint8Array([1, 2, 3]);
    } }),
    encodeUtf8Base64: (text) => Buffer.from(text).toString("base64"),
    describeBackupRestoreConfirmation: () => "Restore synthetic backup?",
    window: { confirm: () => { navigator.onLine = !offline; return confirm; } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (url === "/api/admin/backup/inspect") return jsonResponse(200, {
        ok: true, encryption_mode: "plaintext", inspection_receipt: `receipt-${calls.length}`,
      });
      return imported(url, options);
    },
    renderMaintenanceResult: (node, lead, outcome) => { node.textContent = `${lead}${outcome.ok ? "." : `, but ${outcome.sentence}`}`; },
    setBanner: (message, tone) => banners.push({ message, tone }),
    refreshState: async () => { refreshes += 1; if (refreshError) throw new Error("Synthetic refresh failed"); },
  });
  return { api, calls, banners, state, elements, timers,
    get refreshes() { return refreshes; },
    expire() { for (const timer of [...timers.values()]) timer.callback(); },
  };
}

const settleRestore = () => new Promise((resolve) => setImmediate(resolve));

for (const [name, imported] of [
  ["transport drop", () => { throw new TypeError("Failed to fetch"); }],
  ["bad JSON", () => ({ ...jsonResponse(200), json: async () => { throw new SyntaxError("private raw body"); } })],
  ["wrong-shaped 2xx", () => jsonResponse(200, { unexpected: "value" })],
  ["ok-only 2xx", () => jsonResponse(200, { ok: true })],
  ["refused 2xx", () => jsonResponse(200, { ok: false, detail: "No decided restore result" })],
  ["undecided 5xx", () => jsonResponse(503, { detail: "Service unavailable" })],
  ["deadline with abort-ignoring body", () => ({ ...jsonResponse(200), json: () => new Promise(() => {}) })],
]) {
  test(`uploaded restore preserves unknown outcome after ${name}`, async () => {
    const fixture = uploadFixture({ imported });
    let done = false;
    const pending = fixture.api.runImportBackup().then(() => { done = true; });
    await settleRestore();
    if (name.startsWith("deadline")) fixture.expire();
    await settleRestore();
    assert.equal(done, true, "restore owner settles even when decoding ignores abort");
    await pending;
    assert.equal(fixture.calls.length, 2, "exactly one inspection and one apply, no automatic retry");
    assert.equal(fixture.banners.at(-1).tone, "error");
    assert.match(fixture.elements.backupImportResult.textContent, /unknown/i);
    assert.match(fixture.elements.backupImportResult.textContent, /check.*before.*restor/i);
    assert.doesNotMatch(fixture.elements.backupImportResult.textContent, /Import failed|private raw body/);
    assert.doesNotMatch(fixture.banners.at(-1).message, /import failed|imported from/i);
    assert.deepEqual(fixture.state.systems, [{ id: "original" }]);
    assert.equal(fixture.state.defaultSystemId, "original");
    assert.equal(fixture.state.setupDirty, true);
    assert.equal(fixture.elements.backupImportPassphrase.value, " synthetic phrase ");
    assert.equal(fixture.elements.backupImportButton.disabled, false);
    assert.equal(fixture.refreshes, 0);
    assert.equal(fixture.timers.size, 0);
    if (name === "transport drop") {
      await fixture.api.runImportBackup();
      assert.equal(fixture.calls[2].url, "/api/admin/backup/inspect", "manual retry must obtain a new receipt");
      assert.notEqual(fixture.calls[3].options.headers["X-Backup-Inspection-Receipt"], fixture.calls[1].options.headers["X-Backup-Inspection-Receipt"]);
    }
  });
}

for (const status of [400, 403, 409, 422]) {
  test(`uploaded restore preserves definite HTTP ${status} refusal`, async () => {
    const fixture = uploadFixture({ imported: () => jsonResponse(status, { detail: "Synthetic admission refused" }) });
    await fixture.api.runImportBackup();
    assert.match(fixture.elements.backupImportResult.textContent, /Import failed:.*Synthetic admission refused/);
    assert.doesNotMatch(fixture.banners.at(-1).message, /unknown|may or may not/);
    assert.equal(fixture.refreshes, 0);
    assert.equal(fixture.calls.length, 2);
  });
}

for (const options of [{ confirm: false }, { fileError: true }]) {
  test(`uploaded restore local refusal never dispatches apply: ${JSON.stringify(options)}`, async () => {
    const fixture = uploadFixture(options);
    await fixture.api.runImportBackup();
    assert.equal(fixture.calls.some(({ url }) => url.includes("/backup/import")), false);
    assert.equal(fixture.refreshes, 0);
    assert.equal(fixture.elements.backupImportButton.disabled, false);
    assert.doesNotMatch(fixture.elements.backupImportResult.textContent, /unknown/i);
    if (options.confirm === false) assert.equal(fixture.banners.length, 0);
    else assert.equal(fixture.banners.at(-1).tone, "error");
  });
}

test("uploaded restore reaches local service despite offline hint", async () => {
  const fixture = uploadFixture({ offline: true });
  await fixture.api.runImportBackup();
  assert.equal(fixture.calls.length, 2);
  assert.equal(fixture.refreshes, 1);
  assert.match(fixture.elements.backupImportResult.textContent, /^Imported synthetic/);
  assert.equal(fixture.timers.size, 0);
});

for (const failures of [{}, { ui: "Synthetic start failure" }]) {
  test(`uploaded restore accepts decided success with restart failures ${JSON.stringify(failures)}`, async () => {
    const payload = restoreResult({ stopped_containers: ["ui"], restarted_containers: Object.keys(failures).length ? [] : ["ui"], restart_failures: failures });
    const fixture = uploadFixture({ imported: () => jsonResponse(200, payload) });
    await fixture.api.runImportBackup();
    assert.equal(fixture.state.systems, payload.systems);
    assert.equal(fixture.state.defaultSystemId, "restored");
    assert.equal(fixture.refreshes, 1);
    assert.match(fixture.elements.backupImportResult.textContent, /^Imported synthetic/);
    assert.doesNotMatch(fixture.elements.backupImportResult.textContent, /unknown|Import failed/);
    assert.equal(fixture.banners.at(-1).tone, Object.keys(failures).length ? "error" : "success");
    assert.equal(fixture.calls.length, 2);
    assert.equal(fixture.timers.size, 0);
  });
}


test("an empty restored configuration clears the previous default system", async () => {
  const fixture = uploadFixture({ imported: () => jsonResponse(200, restoreResult({ systems: [], default_system_id: null })) });
  await fixture.api.runImportBackup();
  assert.equal(fixture.banners.at(-1).tone, "success");
  assert.deepEqual(fixture.state.systems, []);
  assert.equal(fixture.state.defaultSystemId, null);
});

test("a failed follow-up refresh cannot turn a decided restore into a failed apply", async () => {
  const fixture = uploadFixture({ refreshError: true });
  await fixture.api.runImportBackup();
  assert.match(fixture.elements.backupImportResult.textContent, /^Imported synthetic/);
  assert.doesNotMatch(fixture.banners.at(-1).message, /Import failed|unknown whether/i);
  assert.match(fixture.banners.at(-1).message, /imported.*refresh/i);
  assert.equal(fixture.calls.length, 2);
});

for (const [field, value] of [
  ["ok", "true"], ["systems", {}], ["systems", [{ id: "", label: "Bad" }]],
  ["default_system_id", 7], ["restored_paths", "bad"], ["restored_history_database", "false"],
  ["stopped_containers", null], ["restarted_containers", [null]], ["restart_failures", []],
]) {
  test(`restore result refuses malformed ${field}: ${JSON.stringify(value)}`, async () => {
    const fixture = uploadFixture({ imported: () => jsonResponse(200, restoreResult({ [field]: value })) });
    await fixture.api.runImportBackup();
    assert.equal(fixture.refreshes, 0);
    assert.equal(fixture.state.defaultSystemId, "original");
    assert.match(fixture.elements.backupImportResult.textContent, /unknown/i);
  });
}

test("restore form explains mandatory inspection and observed encryption confirmation", () => {
  assert.match(TEMPLATE, /inspect[^.]+observed encryption mode/i);
  assert.match(TEMPLATE, /confirm the archive before replacing/i);
  assert.doesNotMatch(TEMPLATE, /single-use inspection receipt|without pretending/i);
  assert.match(TEMPLATE, /<h1>Admin<\/h1>/);
  assert.match(TEMPLATE, /Support archive for offline inspection, not restorable/i);
  assert.match(TEMPLATE, /LAN.*auto-stops/i);
});
