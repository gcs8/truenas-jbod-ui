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
const FETCH_JSON_HELPERS = ["fetchJson", "fetchWithTimeout", "requestTimeoutError", "readJsonResponse", "describeApiError", "validatedRequestId", "describeRequestFailure", "isMutatingRequest", "browserIsOffline", "adminRequestError", "classifyTransportFailure", "describeTransportFailure", "classifyResponseFailure", "describeResponseFailure"];
const MUTATION_RESULT_HELPERS = ["requireMutationResult", "isNonEmptyString", "validSystemSaveResult", "validDemoSystemResult", "validProfileSaveResult", "describeMutationFailure", "adminRequestError"];
const SYNTHETIC_REQUEST_ID = "0123456789abcdef0123456789abcdef";
function load(names, bindings = {}) {
  const context = vm.createContext({console, URLSearchParams, setTimeout, clearTimeout, AbortController, DEFAULT_REQUEST_TIMEOUT_MS: 60000, ...bindings});
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
  const api = load(["saveRuntimeBehaviorSettings", "collectRuntimeBehaviorValues", "renderRuntimeBehaviorSettings", ...FETCH_JSON_HELPERS], {
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
  ["HTML response", {headers: {get: () => SYNTHETIC_REQUEST_ID}, json: async () => {throw new SyntaxError("private raw body");}}],
  ["empty object", {json: async () => ({})}],
  ["missing behavior", {json: async () => ({ok: true})}],
  ["server error after possible write", {ok: false, status: 500, json: async () => ({detail: "internal failure"})}],
]) {
  test(`timing save reports unknown outcome for ${name}`, async () => {
    const probe = await timingSaveProbe(response);
    assertUnknownTimingSave(probe);
    assert.doesNotMatch(probe.banners[0].text, /private raw body/);
    if (name === "HTML response") assert.ok(probe.banners[0].text.includes(SYNTHETIC_REQUEST_ID));
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
    const {fetchJson} = load([...FETCH_JSON_HELPERS], {fetch: async () => ({ok: true, status: 200, headers: {get: () => SYNTHETIC_REQUEST_ID}, json: async () => {if (value === undefined) throw new SyntaxError("secret html"); return value;}})});
    await assert.rejects(fetchJson("/synthetic"), error => /Invalid JSON response/.test(error.message) && !/secret html/.test(error.message) && error.message.includes(SYNTHETIC_REQUEST_ID));
  }
  const {fetchJson} = load([...FETCH_JSON_HELPERS], {fetch: async () => ({ok: true, json: async () => ({ok: true})})});
  assert.equal((await fetchJson("/synthetic")).ok, true);
});
test("demo creation ignores loaded editor ID and avoids both ID namespaces", async () => {
  let body;
  const state = {systems: [{id: "demo-builder-lab"}, {id: "real-system"}], profiles: [{id: "demo-builder-lab-2-chassis"}]};
  await load(["createDemoSystem", ...MUTATION_RESULT_HELPERS], {state, elements: {setupSystemId: {value: "real-system"}, setupSystemLabel: {value: "Keep original"}}, fetchJson: async (_url, options) => {body = JSON.parse(options.body); return {ok: true, system: {id: body.system_id, label: "Demo Builder Lab"}, systems: [], profile: {id: `${body.system_id}-chassis`}, profiles: []};}, refreshState: async () => {}, getSystemById: () => null, renderAll() {}, setBanner() {}}).createDemoSystem();
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

// #411 caller-level contract: a 2xx body that is a nonempty object but not the
// route's result must never show success, apply result state, or claim a
// definite failure; a confirmed refusal still says "failed".
function mutationProbe(name, fetchResult, extra = {}) {
  const banners = [];
  const elements = {setupResult: {}, setupCreateButton: {}, setupCreateDemoButton: {}, profileBuilderResult: {}, profileBuilderSaveButton: {}, setupProfile: {value: "keep-profile"}};
  const state = {systems: [{id: "existing"}], profiles: [{id: "source"}], loadedSystemId: "existing", selectedExistingSystemId: "existing", defaultSystemId: "existing", loadedBuilderProfileId: "source", selectedProfileId: "source"};
  let refreshes = 0, fetches = 0;
  const bindings = {
    state, elements,
    fetchJson: async () => {fetches++; if (fetchResult instanceof Error) throw fetchResult; return fetchResult;},
    refreshState: async () => {refreshes++;},
    setBanner(text, kind) {banners.push({text, kind});},
    collectSetupPayload: () => ({label: "Synthetic", truenas_host: "https://nas.example.test"}),
    updateCreateButton() {}, fetchStorageViewCandidates: async () => {}, getSystemById: () => null, renderAll() {},
    currentBuilderSourceProfile: () => ({id: "source"}),
    readProfileBuilderDraft: () => ({id: "custom-draft", label: "Custom Draft", source_profile_id: "source", rows: 1, columns: 2, slot_count: 2}),
    resolveBuilderDraftLayout: () => ({slotLayoutForSave: [[0, 1]]}),
    getProfileById: () => null, loadProfileIntoBuilder() {}, renderProfileBuilder() {},
    ...extra,
  };
  const api = load([name, ...MUTATION_RESULT_HELPERS], bindings);
  return {api, state, elements, banners, counts: () => ({refreshes, fetches})};
}
function outcomeError(message, outcome, status) {
  const error = new Error(message); error.adminOutcome = outcome; error.outcomeUnknown = outcome === "unknown"; error.status = status; return error;
}
const MUTATIONS = [
  ["createSystem", "setupResult", {ok: true, system: {id: "new-system", label: "New"}, systems: []}],
  ["createDemoSystem", "setupResult", {ok: true, system: {id: "demo-builder-lab", label: "Demo"}, systems: [], profile: {id: "demo-builder-lab-chassis"}, profiles: []}],
  ["saveCustomProfile", "profileBuilderResult", {ok: true, profile: {id: "custom-draft", label: "Custom Draft"}, profiles: []}],
];
for (const [name, resultKey, valid] of MUTATIONS) {
  for (const [label, body] of [
    ["unexpected object", {unexpected: true}],
    ["ok without result", {ok: true}],
    ["non-boolean ok", {...valid, ok: "yes"}],
    ["result with blank id", JSON.parse(JSON.stringify(valid).replace(/"id":"[^"]+"/g, '"id":""'))],
  ]) {
    test(`${name} treats a 2xx ${label} as an unknown outcome, not success`, async () => {
      const probe = mutationProbe(name, body);
      const before = JSON.stringify(probe.state);
      await probe.api[name]();
      assert.equal(JSON.stringify(probe.state), before, "no result state applied");
      assert.equal(probe.counts().refreshes, 0);
      assert.equal(probe.counts().fetches, 1, "no automatic retry");
      assert.ok(probe.banners.every((banner) => banner.kind !== "success"));
      const last = probe.banners.at(-1);
      assert.equal(last.kind, "error");
      assert.match(last.text, /outcome is unknown/);
      assert.match(last.text, /may or may not have been applied/);
      assert.doesNotMatch(last.text, / failed:/);
      assert.equal(probe.elements[resultKey].textContent, last.text);
    });
  }
  test(`${name} reports an ambiguous fetch outcome as unknown and keeps the draft`, async () => {
    const probe = mutationProbe(name, outcomeError("Invalid JSON response (200). The change may or may not have been applied.", "unknown", 200));
    await probe.api[name]();
    assert.match(probe.banners.at(-1).text, /outcome is unknown.*Draft retained/);
    assert.doesNotMatch(probe.banners.at(-1).text, / failed:/);
    assert.equal(probe.counts().fetches, 1);
  });
  test(`${name} reports a confirmed refusal as a definite failure`, async () => {
    const probe = mutationProbe(name, outcomeError("Label already used.", "error", 409));
    await probe.api[name]();
    assert.match(probe.banners.at(-1).text, / failed: Label already used\./);
    assert.doesNotMatch(probe.banners.at(-1).text, /unknown/);
  });
  test(`${name} accepts the route's valid result`, async () => {
    const probe = mutationProbe(name, valid);
    await probe.api[name]();
    assert.equal(probe.banners.at(-1).kind, "success");
    assert.equal(probe.counts().refreshes, 1);
  });
}
test("fetchJson turns a malformed 2xx mutation body into an unknown outcome and a GET into an error", async () => {
  for (const [method, outcome] of [["POST", "unknown"], ["GET", "error"]]) {
    const {fetchJson} = load([...FETCH_JSON_HELPERS], {fetch: async () => ({ok: true, status: 200, headers: {get: () => ""}, json: async () => {throw new SyntaxError("<html>proxy</html>");}})});
    await assert.rejects(fetchJson("/synthetic", {method}), (error) => error.adminOutcome === outcome && error.protocolError === true && !/proxy/.test(error.message));
  }
});

test("explicit discard drops the timing draft; failure does not", async () => {
  const field = {value: "99", dataset: {runtimeBehaviorKey: "interval"}};
  const holder = {querySelectorAll: () => [field], set innerHTML(value) {field.value = value.match(/value="([^"]*)"/)[1];}};
  const state = {runtimeBehavior: {fields: [{key: "interval", value: 15, writable: true}]}};
  const elements = {runtimeBehaviorFields: holder, runtimeBehaviorDetail: {}, runtimeBehaviorSaveButton: {}, runtimeBehaviorResult: {}};
  const api = load(["saveRuntimeBehaviorSettings", "collectRuntimeBehaviorValues", "renderRuntimeBehaviorSettings", "discardRuntimeBehaviorDraft"], {state, elements, fetchJson: async () => {throw new Error("temporary network failure");}, setBanner() {}, escapeHtml: String, runtimeBehaviorOwnerLabel: () => "", renderRuntimeCards() {}});
  await api.saveRuntimeBehaviorSettings();
  assert.equal(field.value, "99");
  assert.doesNotMatch(elements.runtimeBehaviorResult.textContent, /discarded/);
  api.discardRuntimeBehaviorDraft();
  assert.equal(field.value, "15");
  assert.match(elements.runtimeBehaviorResult.textContent, /discarded/);
});
