"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");
const test = require("node:test");
const source = fs.readFileSync(path.resolve(__dirname, "../../admin_service/static/admin.js"), "utf8");
function extract(name) {
  const start = source.search(new RegExp(`^  (?:async )?function ${name}\\(`, "m"));
  assert.ok(start >= 0, name);
  const next = source.slice(start + 3).search(/^  (?:async )?function /m);
  return source.slice(start, next < 0 ? undefined : start + 3 + next);
}
function load(names, bindings = {}) {
  const context = vm.createContext({console, URLSearchParams, setTimeout, ...bindings});
  vm.runInContext(names.map(extract).join("\n") + `\nglobalThis.tested = {${names.join(",")}}`, context);
  return context.tested;
}
test("failed history source scan preserves last known rows and marks them unavailable", async () => {
  const state = {orphanedHistory: [{system_id: "removed", total_rows: 4}]};
  await load(["loadOrphanedHistory"], {state, elements: {}, fetchJson: async () => {throw new Error("offline");}, renderHistoryMaintenance() {}, setBanner() {}}).loadOrphanedHistory();
  assert.equal(state.orphanedHistory.length, 1);
  assert.equal(state.orphanedHistoryError, true);
});
test("empty purge preview disables deletion without asking for confirmation", async () => {
  const elements = {historyPurgeOrphanedButton: {}, historyPurgeOrphanedResult: {}};
  let confirms = 0;
  await load(["purgeOrphanedHistory"], {state: {}, elements, fetchJson: async () => ({orphaned_systems: [], purge_preview_token: "proof"}), window: {confirm() {confirms++;}}, setBanner() {}}).purgeOrphanedHistory();
  assert.equal(confirms, 0); assert.equal(elements.historyPurgeOrphanedButton.disabled, true);
});
test("incomplete state object cannot clear saved lists", async () => {
  const state = {systems: [{id: "keep"}], profiles: [{id: "keep-profile"}]};
  let message;
  await load(["runRefreshState"], {state, elements: {}, fetchJson: async () => ({}), setBanner(text) {message = text;}}).runRefreshState();
  assert.equal(state.systems[0]?.id, "keep"); assert.equal(state.profiles[0]?.id, "keep-profile");
  assert.match(message, /Invalid admin state response/);
});
test("incomplete timing save cannot report success", async () => {
  const state = {runtimeBehavior: {fields: [{key: "interval", value: 15}]}};
  const banners = [];
  await load(["saveRuntimeBehaviorSettings"], {state, elements: {runtimeBehaviorSaveButton: {}}, collectRuntimeBehaviorValues: () => ({interval: "99"}), fetchJson: async () => ({}), renderRuntimeBehaviorSettings() {}, renderRuntimeCards() {}, setBanner(text, kind) {banners.push(kind);}}).saveRuntimeBehaviorSettings();
  assert.deepEqual(banners, ["error"]);
});
// These integer keys and boolean ownership flags are emitted by
// app.config.runtime_behavior_settings_payload, including env-owned fields.
const timingKeys = ["refresh_interval_seconds", "snapshot_cache_ttl_seconds", "source_bundle_cache_ttl_seconds", "smart_cache_ttl_seconds", "sg_ses_device_cache_ttl_seconds"];
function timingFields() {
  return timingKeys.map((key, index) => ({key, value: index ? 0 : 30, writable: index !== 4}));
}
async function timingSaveProbe(response) {
  const original = {fields: timingFields()};
  const runtime = {containers: ["keep"]};
  const state = {runtimeBehavior: original, runtime, runtimeBehaviorBaseline: original.fields};
  const input = {value: "99", dataset: {runtimeBehaviorKey: timingKeys[0]}};
  let renders = 0, posts = 0;
  const holder = {querySelectorAll: () => [input], set innerHTML(_value) {renders++;}};
  const elements = {runtimeBehaviorFields: holder, runtimeBehaviorDetail: {}, runtimeBehaviorSaveButton: {}, runtimeBehaviorResult: {}};
  const banners = [];
  const api = load(["saveRuntimeBehaviorSettings", "collectRuntimeBehaviorValues", "renderRuntimeBehaviorSettings", "fetchJson", "readJsonResponse", "describeApiError"], {
    state, elements, fetch: async () => {posts++; if (response instanceof Error) throw response; return {ok: true, status: 200, ...response};},
    setBanner(text, kind) {banners.push({text, kind});}, renderRuntimeCards() {}, escapeHtml: String, runtimeBehaviorOwnerLabel: () => "",
  });
  await api.saveRuntimeBehaviorSettings();
  return {state, original, runtime, input, elements, banners, posts, renders};
}
function assertUnknownTimingSave(probe) {
  assert.equal(probe.state.runtimeBehavior, probe.original);
  assert.equal(probe.state.runtimeBehaviorBaseline, probe.original.fields);
  assert.equal(probe.state.runtime, probe.runtime);
  assert.equal(probe.input.value, "99");
  assert.equal(probe.renders, 0);
  assert.equal(probe.posts, 1);
  assert.equal(probe.elements.runtimeBehaviorSaveButton.disabled, false);
  assert.equal(probe.banners[0].kind, "error");
  assert.match(probe.banners[0].text, /outcome is unknown/i);
  assert.match(probe.banners[0].text, /may already have been saved/i);
  assert.match(probe.banners[0].text, /check.*saved.*before/i);
  assert.doesNotMatch(probe.banners[0].text, /retry|save failed/i);
  assert.equal(probe.elements.runtimeBehaviorResult.textContent, probe.banners[0].text);
}
for (const [name, fields] of [
  ["empty", []], ["null entry", [null]], ["scalar entry", [5]],
  ["missing key", timingFields().map(({key, ...field}) => field)],
  ["missing field", timingFields().slice(1)],
  ["unknown key", timingFields().map(field => ({...field, key: "unknown"}))],
  ["duplicate key", timingFields().map(field => ({...field, key: timingKeys[0]}))],
  ["missing value", timingFields().map(({value, ...field}) => field)],
  ...[null, "30", false, {}, 1.5].map(value => [`invalid value ${JSON.stringify(value)}`, timingFields().map(field => ({...field, value}))]),
  ["invalid writable", timingFields().map(field => ({...field, writable: "false"}))],
]) {
  test(`timing save rejects ${name} fields without replacing state`, async () => {
    assertUnknownTimingSave(await timingSaveProbe({json: async () => ({ok: true, runtime_behavior: {fields}, runtime: {}})}));
  });
}
for (const [name, response] of [
  ["lost response", new Error("Failed to fetch")],
  ["HTML response", {headers: {get: () => "synthetic-request"}, json: async () => {throw new SyntaxError("private raw body");}}],
  ["empty object", {json: async () => ({})}],
  ["missing behavior", {json: async () => ({ok: true})}],
  ["server error after possible write", {ok: false, status: 500, json: async () => ({detail: "internal failure"})}],
]) {
  test(`timing save reports unknown outcome for ${name}`, async () => {
    const probe = await timingSaveProbe(response);
    assertUnknownTimingSave(probe);
    assert.doesNotMatch(probe.banners[0].text, /private raw body/);
    if (name === "HTML response") assert.match(probe.banners[0].text, /synthetic-request/);
  });
}
for (const status of [400, 422]) {
  test(`timing save distinguishes definite HTTP ${status} validation refusal`, async () => {
    const probe = await timingSaveProbe({ok: false, status, json: async () => ({detail: "Must be a whole number"})});
    assert.equal(probe.state.runtimeBehavior, probe.original);
    assert.equal(probe.input.value, "99");
    assert.equal(probe.renders, 0);
    assert.equal(probe.banners[0].kind, "error");
    assert.match(probe.banners[0].text, /rejected.*Must be a whole number/);
    assert.doesNotMatch(probe.banners[0].text, /unknown/);
  });
}
test("timing save accepts complete reordered integer fields including zero and env ownership", async () => {
  const fields = timingFields().reverse();
  // Effective config/env settings are ints, not bounded by the admin write limits.
  fields[0].value = -1;
  const behavior = {fields};
  const probe = await timingSaveProbe({json: async () => ({ok: true, runtime_behavior: behavior})});
  assert.equal(probe.state.runtimeBehavior, behavior);
  assert.equal(probe.banners[0].kind, "success");
});
function controls() {
  const elements = {backupExportButton: {}, debugExportButton: {}, backupImportButton: {}};
  for (const prefix of ["backupExport", "backupImport", "debugExport"]) {
    elements[prefix + "StopToggle"] = {checked: false};
    elements[prefix + "RestartToggle"] = {checked: true};
  }
  return {elements, state: {selectedDebugPaths: ["history"], selectedBackupPaths: ["config"], backupDefaults: {}, operationPromises: {}}, syncSingleBundleControls() {}, getDebugExportPolicy: () => ({allowed: true}), getBackupExportPolicy: () => ({allowed: true})};
}
test("restart preference survives stop toggles, including explicit opt-out", () => {
  const bindings = controls();
  const {syncBackupControls} = load(["syncBackupControls"], bindings);
  for (const prefix of ["backupExport", "backupImport", "debugExport"]) {
    const stop = bindings.elements[prefix + "StopToggle"], restart = bindings.elements[prefix + "RestartToggle"];
    syncBackupControls(); assert.equal(restart.checked, true); assert.equal(restart.disabled, true);
    stop.checked = true; syncBackupControls(); assert.equal(restart.checked, true);
    restart.checked = false; stop.checked = false; syncBackupControls(); stop.checked = true; syncBackupControls();
    assert.equal(restart.checked, false);
  }
});
test("JSON responses reject HTML, empty, null, array and scalar without exposing body", async () => {
  for (const value of [undefined, null, [], {}, "secret html", 5, false]) {
    const {fetchJson} = load(["fetchJson", "readJsonResponse", "describeApiError"], {fetch: async () => ({ok: true, status: 200, headers: {get: () => "synthetic-request"}, json: async () => {if (value === undefined) throw new SyntaxError("secret html"); return value;}})});
    await assert.rejects(fetchJson("/synthetic"), error => /Invalid JSON response/.test(error.message) && !/secret html/.test(error.message) && /synthetic-request/.test(error.message));
  }
  const {fetchJson} = load(["fetchJson", "readJsonResponse", "describeApiError"], {fetch: async () => ({ok: true, json: async () => ({ok: true})})});
  assert.equal((await fetchJson("/synthetic")).ok, true);
});
test("demo creation ignores loaded editor ID and avoids both ID namespaces", async () => {
  let body;
  const state = {systems: [{id: "demo-builder-lab"}, {id: "real-system"}], profiles: [{id: "demo-builder-lab-2-chassis"}]};
  await load(["createDemoSystem"], {state, elements: {setupSystemId: {value: "real-system"}, setupSystemLabel: {value: "Keep original"}}, fetchJson: async (_url, options) => {body = JSON.parse(options.body); return {system: {id: body.system_id}};}, refreshState: async () => {}, getSystemById: () => null, renderAll() {}, setBanner() {}}).createDemoSystem();
  assert.equal(body.system_id, "demo-builder-lab-3"); assert.equal(body.replace_existing, false); assert.notEqual(body.label, "Keep original");
});
test("purge cancel sends no delete and confirmation names exact preview", async () => {
  let posts = 0, message = "";
  const elements = {historyPurgeOrphanedButton: {}, historyPurgeOrphanedResult: {}};
  await load(["purgeOrphanedHistory"], {state: {}, elements, window: {confirm(text) {message = text; return false;}}, fetchJson: async (_url, options) => {if (options?.method === "POST") posts++; return {orphaned_systems: [{system_id: "removed-one", total_rows: 4}], purge_preview_token: "proof"};}, loadOrphanedHistory: async () => {}, setBanner() {}}).purgeOrphanedHistory();
  assert.equal(posts, 0); assert.match(message, /removed-one/); assert.match(message, /4/); assert.match(message, /irreversible/i);
});
test("timing drafts and existing input survive failed save and refresh", async () => {
  const field = {value: "99", dataset: {runtimeBehaviorKey: "interval"}};
  let renders = 0;
  const holder = {querySelectorAll: () => [field], set innerHTML(value) {renders++; field.value = value.match(/value="([^"]*)"/)[1];}};
  const state = {runtimeBehavior: {fields: [{key: "interval", value: 15, writable: true}]}};
  let fail = true; const sent = [];
  const api = load(["saveRuntimeBehaviorSettings", "collectRuntimeBehaviorValues", "renderRuntimeBehaviorSettings"], {state, elements: {runtimeBehaviorFields: holder, runtimeBehaviorDetail: {}, runtimeBehaviorSaveButton: {}}, fetchJson: async (_url, options) => {sent.push(JSON.parse(options.body)); if (fail) throw new Error("temporary network failure"); return {runtime_behavior: {fields: [{key: "interval", value: 99, writable: true}]}};}, setBanner() {}, escapeHtml: String, runtimeBehaviorOwnerLabel: () => "", renderRuntimeCards() {}});
  await api.saveRuntimeBehaviorSettings(); api.renderRuntimeBehaviorSettings();
  assert.equal(field.value, "99"); assert.equal(renders, 0);
  fail = false; await api.saveRuntimeBehaviorSettings(); assert.deepEqual(sent[0], sent[1]); assert.equal(state.runtimeBehavior.fields[0].value, 99);
});
test("duplicate restore calls share one file read and pending operation", async () => {
  let reads = 0, reject;
  const bindings = controls();
  Object.assign(bindings, {readSelectedImportFile: () => ({name: "synthetic.zip", arrayBuffer() {reads++; return new Promise((_resolve, fail) => {reject = fail;});}}), readOptionalSecretValue: () => null, setBanner() {}});
  const names = ["importBackup", "syncBackupControls"];
  if (source.includes("function runImportBackup(")) names.push("runImportBackup", "runBackupOperation");
  const api = load(names, bindings);
  const first = api.importBackup(), second = api.importBackup();
  await Promise.resolve();
  assert.equal(reads, 1); assert.equal(first, second);
  api.syncBackupControls(); assert.equal(bindings.elements.backupImportButton.disabled, true);
  reject(new Error("synthetic read failure")); await first;
  assert.equal(bindings.elements.backupImportButton.disabled, false);
});
test("busy backup and debug exports coalesce and stay disabled through control sync", async () => {
  for (const operation of ["exportBackup", "exportDebugBundle"]) {
    let reject, posts = 0;
    const bindings = controls();
    Object.assign(bindings, {fetch: () => {posts++; return new Promise((_resolve, fail) => {reject = fail;});}, readOptionalSecretValue: () => null, setBanner() {}, refreshState: async () => {}, readJsonResponse: async () => null});
    const api = load([operation, "syncBackupControls", ...(source.includes(`function run${operation[0].toUpperCase() + operation.slice(1)}(`) ? [`run${operation[0].toUpperCase() + operation.slice(1)}`, "runBackupOperation"] : [])], bindings);
    const first = api[operation](); const second = api[operation]();
    await Promise.resolve();
    api.syncBackupControls();
    assert.equal(posts, 1); assert.equal(first, second);
    const button = bindings.elements[operation === "exportBackup" ? "backupExportButton" : "debugExportButton"];
    assert.equal(button.disabled, true);
    reject(new Error("synthetic failure")); await first; api.syncBackupControls(); assert.equal(button.disabled, false);
  }
});
