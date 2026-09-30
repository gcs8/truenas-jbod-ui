"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../../admin_service/static/admin.js"), "utf8");

function functionSource(name, optional = false) {
  const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  if (start < 0 && optional) return "";
  assert.ok(start >= 0, name);
  const rest = source.slice(start);
  const end = rest.search(/\n  (?:async )?function /);
  return end < 0 ? rest : rest.slice(0, end);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const helpers = ["captureAdminEditorOperation", "retireAdminEditorControls", "recordAdminEditorOutcome",
  "validBootstrapResult", "validSystemDeleteResult", "validGeneratedKeyResult", "validHaNodesResult",
  "validProfileDeleteResult", "fetchBootstrapResult", "isNonEmptyString", "requireMutationResult",
  "describeMutationFailure", "adminRequestError", "validProfileSaveResult"];
const methods = ["saveCustomProfile", "bootstrapServiceAccount", "generateSshKey", "discoverQuantastorHaNodes", "deleteSelectedSystem", "deleteCustomProfile"];
const transportHelpers = ["fetchJson", "fetchOrReportStopped", "sessionRemainingMs", "fetchWithTimeout", "requestTimeoutError", "readJsonResponse", "describeApiError", "validatedRequestId", "describeRequestFailure", "isMutatingRequest", "browserIsOffline", "classifyTransportFailure", "describeTransportFailure", "classifyResponseFailure", "describeResponseFailure"];
function fixture(method, { realTransport = false } = {}) {
  const requests = [], banners = [], renders = [], refreshes = [];
  let resets = 0, builderLoads = 0;
  const field = (value = "") => ({ value, textContent: "", disabled: false, checked: false });
  const elements = new Proxy({
    setupSystemId: field("system-a"), setupTruenasHost: field("host-a.example.test"),
    setupPlatform: field("quantastor"), setupHaEnabled: { checked: true }, setupSshEnabled: { checked: true },
    setupSshHost: field("host-a.example.test"), setupSshUser: field("service-a"),
    setupSshPassword: field("SYNTHETIC-A"), setupSshSudoPassword: field("SYNTHETIC-A"),
    setupBootstrapHost: field("host-a.example.test"), setupBootstrapPassword: field("SYNTHETIC-A"),
    setupBootstrapSudoPassword: field("SYNTHETIC-A"), setupBootstrapButton: field(), setupResult: field(),
    setupBootstrapEnabled: { checked: true },
    setupProfile: field("profile-a"), setupSshKeyMode: field("generate"), setupSshExistingKey: field("key-a"),
    setupGenerateKeyName: field("key-a"), existingSystemSelect: field("system-a"),
    existingSystemDeleteButton: field(), existingSystemDeleteHistoryToggle: field(),
    profileBuilderSaveButton: field(), profileBuilderDeleteButton: field(), profileBuilderResult: field(),
    profileBuilderId: field("custom-a"), profileBuilderLabel: field("Custom A"),
  }, { get: (target, key) => target[key] ?? null });
  const state = { setupEditorGeneration: 1, setupDraftRevision: 0, loadedSystemId: "system-a", setupDirty: false,
    profileBuilderGeneration: 1, profileBuilderRevision: 0, loadedBuilderProfileId: "custom-a",
    selectedProfileId: "profile-a", selectedExistingSystemId: "system-a", defaultSystemId: "system-a",
    haNodes: [{ system_id: "node-a", host: "node-a.example.test", label: "Node A" }], haNodesLoading: false,
    systems: [{ id: "system-a", label: "System A" }, { id: "system-b", label: "System B" }], sshKeys: [], historyRowCounts: {} };
  const draft = () => ({ id: elements.profileBuilderId.value, label: elements.profileBuilderLabel.value,
    source_profile_id: "base", rows: 1, columns: 2, slot_count: 2 });
  const bindings = { state, elements, URLSearchParams, AbortController, setTimeout, clearTimeout,
    DEFAULT_REQUEST_TIMEOUT_MS: 60000, SERVER_REQUEST_ID_PATTERN: /^[0-9a-f]{32}$/,
    window: { confirm: () => true },
    setBanner: (message, tone) => banners.push({ message, tone }),
    fetchJson: (url, options) => { const d = deferred(); requests.push({ ...d, url, options }); return d.promise; },
    fetch: async (url, options) => { const d = deferred(); requests.push({ ...d, url, options }); const body = await d.promise;
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => body }; },
    refreshState: (options) => { const d = deferred(); refreshes.push({ ...d, options }); return d.promise; },
    collectSetupPayload: () => ({ system_id: elements.setupSystemId.value, truenas_host: elements.setupTruenasHost.value,
      platform: elements.setupPlatform.value, ssh_host: elements.setupSshHost.value, ssh_user: elements.setupSshUser.value }),
    collectBootstrapPayload: () => ({ host: elements.setupBootstrapHost.value, platform: "quantastor", service_user: elements.setupSshUser.value, install_sudo_rules: true }),
    bootstrapEnabledForSession: () => Boolean(elements.setupBootstrapEnabled.checked),
    setupDraftSnapshot: () => JSON.stringify([elements.setupProfile.value, elements.setupSshUser.value, state.haNodes]),
    recordSetupDraftChange: () => { state.setupDirty = true; state.setupDraftRevision++; },
    currentSetupPlatform: () => elements.setupPlatform.value, syncHaNodesFromInputs() {}, normalizeHaNodes: value => value,
    renderQuantastorHaSection: () => renders.push("ha"), renderStorageViews: () => renders.push("views"),
    normalizeKeyName: value => value, suggestedKeyName: () => "key-a", renderSshKeyOptions() {}, syncKeyMode() {},
    applySelectedKey: () => { elements.setupSshExistingKey.value = state.sshKeys[0].name; },
    syncSshFields() {}, maybeLoadRecommendedCommands() {}, scheduleSudoersPreviewRefresh() {},
    currentBuilderSourceProfile: () => ({ id: "base" }), readProfileBuilderDraft: draft,
    resolveBuilderDraftLayout: () => ({ slotLayoutForSave: [[0, 1]] }),
    getProfileById: id => ({ id, label: id, is_custom: true, reference_count: 0 }), profileReferenceCount: () => 0,
    getSystemById: id => state.systems.find(item => item.id === id), historyRowCountForSystem: () => 0,
    loadHistoryRowCounts: async () => {}, renderExistingSystems() {}, renderExistingSystemCatalog() {},
    renderProfileOptions() {}, renderProfilePreview() {}, renderProfileCatalog() {},
    loadProfileIntoBuilder: profile => { builderLoads++; elements.profileBuilderId.value = profile.id; state.profileBuilderGeneration++; },
    resetProfileBuilder: () => { builderLoads++; elements.profileBuilderId.value = ""; state.profileBuilderGeneration++; },
    renderProfileBuilder: () => renders.push("builder"), renderAll: () => renders.push("all"),
    renderSaveResult: (element, message) => { element.textContent = message; },
    resetSetupForm: () => { resets++; state.loadedSystemId = null; state.setupDirty = false; state.setupEditorGeneration++; },
  };
  const context = vm.createContext(bindings);
  vm.runInContext([...helpers.map(name => functionSource(name, true)),
    ...(realTransport ? transportHelpers.map(name => functionSource(name)) : []),
    functionSource(method), `globalThis.api = { ${method} };`].join("\n"), context);
  function change(mode) {
    if (mode === "later edit") state.setupDraftRevision++;
    else { state.setupEditorGeneration += mode === "A-B-A" ? 2 : 1; state.loadedSystemId = mode === "A-B-A" ? "system-a" : "system-b"; }
    if (mode === "builder edit") { state.profileBuilderRevision++; state.setupDraftRevision--; }
    state.setupDirty = true;
    elements.setupSshUser.value = "new-service";
    elements.setupSshPassword.value = "SYNTHETIC-NEW";
    elements.setupBootstrapPassword.value = "SYNTHETIC-NEW";
    elements.setupBootstrapHost.value = "new.example.test";
    elements.setupProfile.value = "profile-b"; state.selectedProfileId = "profile-b";
    elements.profileBuilderId.value = "custom-b";
    state.haNodes = [{ system_id: "node-b", host: "node-b.example.test", label: "Node B" }];
    elements.setupResult.textContent = "New draft"; elements.profileBuilderResult.textContent = "New builder";
    elements.setupBootstrapResult = field(); elements.setupBootstrapResult.textContent = "New bootstrap";
    elements.setupBootstrapButton.disabled = true; elements.profileBuilderSaveButton.disabled = true;
    elements.existingSystemDeleteButton.disabled = true; elements.existingSystemDeleteHistoryToggle.checked = true;
    state.haNodesLoading = true;
  }
  const snapshot = () => JSON.stringify({ loaded: state.loadedSystemId, dirty: state.setupDirty, generation: state.setupEditorGeneration,
    nodes: state.haNodes, loading: state.haNodesLoading, selected: state.selectedProfileId,
    fields: Object.fromEntries(Object.entries(elements).map(([key, value]) => [key, value])) });
  return { api: context.api, state, elements, requests, banners, refreshes, renders, change, snapshot,
    context, counts: () => ({ resets, builderLoads }) };
}
function valid(method) {
  if (method === "saveCustomProfile") return { ok: true, profile: { id: "custom-a", label: "Custom A" }, profiles: [] };
  if (method === "deleteCustomProfile") return { ok: true, profile_id: "custom-a", deleted_label: "Custom A", profiles: [] };
  if (method === "generateSshKey") return { ok: true, key: { name: "key-a", runtime_private_path: "/app/data/ssh/key-a" }, keys: [{ name: "key-a" }] };
  if (method === "discoverQuantastorHaNodes") return { ok: true, nodes: [{ system_id: "node-a", label: "Node A", host: "node-a.example.test" }], host_discovery: { attempted: true, ok: false, message: "SSH enrichment unavailable." } };
  if (method === "bootstrapServiceAccount") return { ok: true, host: "host-a.example.test", platform: "quantastor", service_user: "service-a", sudo_rules_installed: true, key_source: "synthetic", detail: "Provisioned service-a.", authorized_keys_path: "/home/service-a/.ssh/authorized_keys" };
  return { ok: true, system_id: "system-a", deleted_label: "System A", systems: [{ id: "system-b", label: "System B" }], default_system_id: "system-b", history_purge: { requested: false, ok: true, summary: null, detail: "Saved history left in place." } };
}
async function settle(probe, result) {
  probe.requests[0].resolve(result);
  // The real timeout/body reader crosses more microtasks than the stub. Drain
  // the same bounded sequence on baseline and candidate, including readbacks.
  for (let i = 0; i < 40; i++) {
    await Promise.resolve();
    for (const refresh of probe.refreshes) refresh.resolve();
  }
}
for (const method of methods) {
  for (const mode of ["A-B", "A-B-A", "later edit"]) {
    for (const failed of [false, true]) {
      test(`${method}: ${mode} fences stale ${failed ? "error and finalizer" : "success and finalizer"}`, async () => {
        const p = fixture(method), run = p.api[method]();
        p.change(mode); const before = p.snapshot(), renderCount = p.renders.length, bannerCount = p.banners.length;
        if (failed) p.requests[0].reject(new Error("Synthetic refusal")); else await settle(p, valid(method));
        await run;
        assert.equal(p.snapshot(), before, "successor draft/results/controls retained");
        assert.equal(p.banners.length, bannerCount, "no retired notification");
        assert.equal(p.counts().resets, 0); assert.equal(p.counts().builderLoads, 0);
        if (method === "discoverQuantastorHaNodes") assert.equal(p.renders.length, renderCount);
      });
    }
  }
  test(`${method}: unchanged originating draft accepts the server result`, async () => {
    const p = fixture(method), run = p.api[method](); await settle(p, valid(method)); await run;
    assert.equal(p.banners.at(-1).tone, method === "discoverQuantastorHaNodes" ? "info" : "success");
    if (method === "bootstrapServiceAccount") assert.equal(p.elements.setupSshPassword.value, "");
    if (method === "deleteSelectedSystem") assert.equal(p.counts().resets, 1);
    if (method === "saveCustomProfile") assert.equal(p.counts().builderLoads, 1);
  });
}
for (const phase of ["POST", "readback"]) {
  test(`profile save preserves independent builder edit during ${phase}`, async () => {
    const p = fixture("saveCustomProfile"), run = p.api.saveCustomProfile();
    if (phase === "readback") { p.requests[0].resolve(valid("saveCustomProfile")); for (let i = 0; i < 8; i++) await Promise.resolve(); }
    p.state.profileBuilderRevision++; p.elements.profileBuilderLabel.value = "New label";
    const before = p.elements.profileBuilderLabel.value;
    if (phase === "POST") await settle(p, valid("saveCustomProfile")); else p.refreshes[0].resolve();
    await run; assert.equal(p.elements.profileBuilderLabel.value, before); assert.equal(p.counts().builderLoads, 0);
    assert.equal(p.elements.profileBuilderSaveButton.disabled, true, "stale finalizer cannot release newer work");
  });
}
for (const method of ["bootstrapServiceAccount", "deleteSelectedSystem", "generateSshKey", "discoverQuantastorHaNodes", "deleteCustomProfile"]) {
  for (const label of ["unrelated", "ok only", "wrong target", "wrong types"]) {
    test(`${method}: malformed ${label} success retains draft and requires readback`, async () => {
      const p = fixture(method), body = valid(method);
      if (label === "wrong target") { body.system_id = "foreign"; body.host = "foreign.example.test"; body.service_user = "foreign"; if (body.key) body.key.name = "foreign"; body.profile_id = "foreign"; if (body.nodes) body.nodes = "foreign"; }
      if (label === "wrong types") { body.sudo_rules_installed = "yes"; body.systems = {}; body.keys = {}; body.nodes = {}; body.profiles = {}; }
      const run = p.api[method](), fields = [p.elements.setupSshPassword.value, p.elements.setupProfile.value], systems = p.state.systems;
      await settle(p, label === "unrelated" ? { unexpected: true } : label === "ok only" ? { ok: true } : body); await run;
      assert.deepEqual([p.elements.setupSshPassword.value, p.elements.setupProfile.value], fields);
      assert.equal(p.state.systems, systems); assert.equal(p.counts().resets, 0);
      assert.equal(p.requests.length, 1); assert.equal(p.refreshes.length, 0, "no automatic retry/readback mutation");
      assert.equal(p.banners.at(-1).tone, "error"); assert.match(p.banners.at(-1).message, /outcome is unknown/);
      assert.match(p.banners.at(-1).message, /re-check.*before retrying/);
    });
  }
}
test("system deletion accepts history purge failure without hiding config deletion", async () => {
  const p = fixture("deleteSelectedSystem"); p.elements.existingSystemDeleteHistoryToggle.checked = true;
  const body = valid("deleteSelectedSystem"); body.history_purge = { requested: true, ok: false, summary: null, detail: "Synthetic purge failure" };
  const run = p.api.deleteSelectedSystem(); await settle(p, body); await run;
  assert.equal(p.counts().resets, 1); assert.match(p.banners.at(-1).message, /Deleted.*history could not be deleted/);
});
test("deleting an unloaded system refreshes catalog without discarding the loaded draft", async () => {
  const p = fixture("deleteSelectedSystem"); p.state.loadedSystemId = "system-b";
  const run = p.api.deleteSelectedSystem(); await settle(p, valid("deleteSelectedSystem")); await run;
  assert.equal(p.counts().resets, 0); assert.equal(p.state.systems.length, 1);
});
test("HA discovery old finalizer cannot clear a newer discovery loading token", async () => {
  const p = fixture("discoverQuantastorHaNodes"), first = p.api.discoverQuantastorHaNodes(), second = p.api.discoverQuantastorHaNodes();
  p.requests[0].reject(new Error("Old failure")); await first;
  assert.equal(p.state.haNodesLoading, true); assert.equal(p.banners.length, 0);
  p.requests[1].resolve(valid("discoverQuantastorHaNodes")); await second; assert.equal(p.state.haNodesLoading, false);
});

for (const method of ["saveCustomProfile", "deleteCustomProfile"]) {
  for (const mode of ["A-B", "A-B-A", "later edit"]) {
    for (const failed of [false, true]) {
      test(`${method}: ${mode} retires delayed readback ${failed ? "error" : "success"}`, async () => {
        const p = fixture(method), run = p.api[method]();
        p.requests[0].resolve(valid(method)); for (let i = 0; i < 8; i++) await Promise.resolve();
        assert.equal(p.refreshes.length, 1); p.change(mode); const before = p.snapshot(), banners = p.banners.length;
        if (failed) p.refreshes[0].reject(new Error("Synthetic readback unavailable")); else p.refreshes[0].resolve();
        await run; assert.equal(p.snapshot(), before); assert.equal(p.banners.length, banners);
        assert.equal(p.counts().builderLoads, 0);
      });
    }
  }
}
for (const method of ["bootstrapServiceAccount", "deleteSelectedSystem", "generateSshKey", "discoverQuantastorHaNodes", "deleteCustomProfile"]) {
  for (const body of [{}, { unexpected: true }, { ok: true }, { ok: false, detail: "No decided route result" }]) {
    test(`${method}: real HTTP 200 parser rejects ${JSON.stringify(body)} before applying state`, async () => {
      const p = fixture(method, { realTransport: true }), run = p.api[method]();
      await settle(p, body); await run;
      assert.equal(p.requests.length, 1); assert.equal(p.counts().resets, 0);
      assert.equal(p.elements.setupSshPassword.value, "SYNTHETIC-A"); assert.equal(p.state.systems.length, 2);
      assert.match(p.banners.at(-1).message, /outcome is unknown/); assert.notEqual(p.banners.at(-1).tone, "success");
      assert.doesNotMatch(p.banners.at(-1).message, /Retry or check/, "an ambiguous write must not recommend blind retry");
      assert.equal(p.requests[0].options.acceptPartialResult, undefined, "client-only policy is not sent to fetch");
    });
  }
}
test("bootstrap real HTTP 200 preserves partial account setup with unverified sudo policy", async () => {
  const p = fixture("bootstrapServiceAccount", { realTransport: true }), body = valid("bootstrapServiceAccount");
  body.ok = false; body.sudo_rules_installed = false; body.authorized_keys_path = null;
  body.detail = "The requested sudo policy installation could not be verified. Account setup may have completed.";
  const run = p.api.bootstrapServiceAccount(); await settle(p, body); await run;
  assert.match(p.banners.at(-1).message, /partially completed.*Check the host before retrying/);
  assert.doesNotMatch(p.elements.setupResult.textContent, /is ready/);
  assert.equal(p.elements.setupSshPassword.value, "SYNTHETIC-A"); assert.equal(p.requests.length, 1);
});
test("HA discovery retains a legitimate node without a REST or SSH host", async () => {
  const p = fixture("discoverQuantastorHaNodes"), body = valid("discoverQuantastorHaNodes"); body.nodes[0].host = null;
  const run = p.api.discoverQuantastorHaNodes(); await settle(p, body); await run;
  assert.equal(p.state.haNodes[0].host, null); assert.equal(p.banners.at(-1).tone, "info");
});
test("system deletion accepts successful history purge row counts", async () => {
  const p = fixture("deleteSelectedSystem"); p.elements.existingSystemDeleteHistoryToggle.checked = true;
  const body = valid("deleteSelectedSystem"); body.history_purge = { requested: true, ok: true, summary: { total_rows: 3 }, detail: "Purged 3 rows." };
  const run = p.api.deleteSelectedSystem(); await settle(p, body); await run;
  assert.equal(p.counts().resets, 1); assert.match(p.banners.at(-1).message, /history \(3 rows\)/);
});
test("catalog-only readback does not run form renderers or successor discovery", async () => {
  const p = fixture("saveCustomProfile");
  const methods = ["refreshState", "startRefreshState", "runRefreshState"];
  p.state.selectedBackupPaths = []; p.state.selectedDebugPaths = []; p.state.backupDefaults = {};
  p.context.currentStagedEsxiHostPrepPackages = () => []; p.context.renderProfileCatalog = () => {};
  p.context.renderRuntimeCards = () => {}; p.context.loadOrphanedHistory = () => {};
  p.context.fetchLiveEnclosures = () => assert.fail("foreign enclosure discovery");
  p.context.fetchStorageViewCandidates = () => assert.fail("foreign candidate discovery");
  vm.runInContext(methods.map(name => functionSource(name)).join("\n"), p.context);
  const run = p.api.saveCustomProfile(); p.requests[0].resolve(valid("saveCustomProfile"));
  for (let i = 0; i < 8; i++) await Promise.resolve();
  p.change("A-B-A"); const before = p.snapshot();
  p.requests[1].resolve({ profiles: [valid("saveCustomProfile").profile], systems: [{ id: "system-a", label: "System A" }] });
  await run; assert.equal(p.snapshot(), before); assert.ok(!p.renders.includes("all"));
});
test("setup and builder input listeners advance revisions even when edits return to the original value", () => {
  const p = fixture("saveCustomProfile"), setupHandlers = {}, builderHandlers = {};
  p.elements.adminViewButtons = []; p.elements.adminViewSwitches = [];
  p.elements.setupPanel = { addEventListener: (name, fn) => { setupHandlers[name] = fn; } };
  p.elements.profileBuilderLabel.matches = () => false;
  p.elements.profileBuilderLabel.addEventListener = (name, fn) => { builderHandlers[name] = fn; };
  for (const element of Object.values(p.elements)) {
    if (element && !element.addEventListener) element.addEventListener = () => {};
    if (element && !element.matches) element.matches = () => false;
  }
  p.context.document = { querySelectorAll: () => [] }; p.context.window.addEventListener = () => {};
  p.context.syncKeyHelp = () => {};
  p.state.storageViewTemplates = [];
  const bindSource = source.slice(source.indexOf("  function bindEvents() {"), source.indexOf("\n  if (elements.backupExportStopToggle)"));
  vm.runInContext(`${bindSource}\nbindEvents();`, p.context);
  setupHandlers.input({ target: {} }); setupHandlers.input({ target: {} });
  assert.equal(p.state.setupDraftRevision, 2);
  builderHandlers.input(); builderHandlers.input(); assert.equal(p.state.profileBuilderRevision, 2);
  p.elements.setupBootstrapEnabled.checked = false; p.elements.setupBootstrapButton.disabled = true;
  p.elements.setupHaEnabled.checked = false; p.elements.setupDiscoverHaNodesButton = { disabled: true };
  setupHandlers.input({ target: {} });
  assert.equal(p.elements.setupBootstrapButton.disabled, true, "retirement must preserve bootstrap capability gating");
  assert.equal(p.elements.setupDiscoverHaNodesButton.disabled, true, "retirement must preserve HA capability gating");
});

for (const method of ["saveCustomProfile", "deleteCustomProfile"]) {
  test(`${method}: failed real readback preserves draft and confirmed mutation outcome`, async () => {
    const p = fixture(method);
    p.state.selectedBackupPaths = []; p.state.selectedDebugPaths = []; p.state.backupDefaults = {};
    p.context.currentStagedEsxiHostPrepPackages = () => []; p.context.renderRuntimeCards = () => {};
    p.context.loadOrphanedHistory = () => {};
    vm.runInContext(["refreshState", "startRefreshState", "runRefreshState"].map(name => functionSource(name)).join("\n"), p.context);
    const run = p.api[method](); p.requests[0].resolve(valid(method));
    for (let i = 0; i < 8; i++) await Promise.resolve();
    p.requests[1].reject(new Error("Synthetic state read failed")); await run;
    assert.equal(p.counts().builderLoads, 0); assert.equal(p.elements.setupProfile.value, "profile-a");
    assert.equal(p.state.adminEditorOutcomes[method === "saveCustomProfile" ? "profile-save" : "profile-delete"].outcome, "success");
    assert.match(p.banners.at(-1).message, /(?:Saved|Deleted).*catalog refresh.*unavailable/);
    assert.doesNotMatch(p.banners.at(-1).message, /save failed|delete failed|retry/i);
  });
}

for (const method of methods) {
  for (const ownership of ["identical A-B-A target", "same-visit revision only"]) {
    test(`${method}: ${ownership} is not admitted by value equality`, async () => {
      const p = fixture(method), run = p.api[method]();
      if (ownership === "identical A-B-A target") p.state.setupEditorGeneration += 2;
      else p.state.setupDraftRevision += 2;
      // Only secret input changes. The non-secret target and builder remain
      // byte-identical, so these cases must use generation/revision ownership.
      p.elements.setupSshPassword.value = "SYNTHETIC-NEW";
      p.elements.setupBootstrapPassword.value = "SYNTHETIC-NEW";
      const before = p.snapshot(), bannerCount = p.banners.length;
      await settle(p, valid(method)); await run;
      assert.equal(p.snapshot(), before); assert.equal(p.banners.length, bannerCount);
    });
  }
}
test("bootstrap finalizer does not release a newer same-visit bootstrap", async () => {
  const p = fixture("bootstrapServiceAccount"), first = p.api.bootstrapServiceAccount(), second = p.api.bootstrapServiceAccount();
  p.requests[0].reject(new Error("Retired refusal")); await first;
  assert.equal(p.elements.setupBootstrapButton.disabled, true); assert.equal(p.banners.length, 0);
  p.requests[1].resolve(valid("bootstrapServiceAccount")); await second;
  assert.equal(p.elements.setupBootstrapButton.disabled, false);
});
test("saved custom profile options exist before auto-selection", async () => {
  const p = fixture("saveCustomProfile"); let value = "profile-a", refreshedOptions = false;
  p.context.renderProfileOptions = () => { refreshedOptions = true; };
  Object.defineProperty(p.elements.setupProfile, "value", { enumerable: true, get: () => value, set: next => {
    assert.ok(refreshedOptions || next !== "custom-a", "a new ID is not yet a selectable DOM option"); value = next;
  } });
  const run = p.api.saveCustomProfile(); await settle(p, valid("saveCustomProfile")); await run;
  assert.equal(value, "custom-a"); assert.equal(p.banners.at(-1).tone, "success");
});
