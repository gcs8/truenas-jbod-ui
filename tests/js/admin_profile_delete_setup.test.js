"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../../admin_service/static/admin.js"), "utf8");

// Load the real declarations, DOM bindings and event registration without the
// page-start timers/scans. No request, owner, renderer or save consumer is replaced.
const startup = source.indexOf("\n  if (elements.backupExportStopToggle)");
assert.ok(startup > 0, "real page-start boundary exists");
const declarations = `${source.slice(0, startup)}
  globalThis.api = { state, elements, bindEvents, renderProfileOptions,
    renderProfilePreview, renderProfileCatalog, renderStorageViews,
    flushStorageViewRender, loadProfileIntoBuilder, refreshState,
    collectSetupPayload, setupDraftSnapshot };
})();`;

class Field {
  constructor(value = "", kind = "input") {
    this.kind = kind;
    this._value = value;
    this._html = "";
    this.options = [];
    this.checked = false;
    this.disabled = false;
    this.textContent = "";
    this.dataset = {};
    this.listeners = new Map();
    this.classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => this.classes.add(name)),
      remove: (...names) => names.forEach(name => this.classes.delete(name)),
      toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
      contains: name => this.classes.has(name),
    };
    this.style = { setProperty(name, value) { this[name] = value; }, removeProperty(name) { delete this[name]; } };
  }
  get value() { return this._value; }
  set value(value) {
    this._value = this.kind === "select" && !this.options.some(option => option.value === String(value))
      ? "" : String(value);
  }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._html = value;
    if (this.kind === "select") {
      this.options = [...value.matchAll(/<option\b[^>]*value="([^"]*)"/g)].map(match => ({ value: match[1] }));
      this._value = this.options[0]?.value || "";
    }
  }
  addEventListener(name, listener) {
    this.listeners.set(name, [...(this.listeners.get(name) || []), listener]);
  }
  matches(selector) { return this.kind === "select" && selector.includes("select"); }
  closest(selector) { return this.builder && selector.includes(".setup-preview-column") ? this : null; }
  querySelectorAll() { return []; }
  setAttribute(name, value) { this[name] = value; }
  dispatch(name) {
    if (name === "click" && this.disabled) return;
    const event = { target: this };
    for (const listener of this.listeners.get(name) || []) listener(event);
    if (this.panel && ["input", "change"].includes(name)) {
      for (const listener of this.panel.listeners.get(name) || []) listener(event);
    }
  }
}

function profile(id, columns, label = id) {
  return { id, label, summary: `Synthetic ${label} layout`, is_custom: true,
    reference_count: 0, rows: 1, columns, slot_count: columns,
    slot_layout: [Array.from({ length: columns }, (_, i) => i)], face_style: "front-drive", row_groups: [] };
}
const originalProfiles = () => [profile("custom-a", 2, "Original A"), profile("custom-b", 3, "Original B")];
const remainingProfiles = () => [profile("custom-b", 4, "Readback B")];
const deleteResult = () => ({ ok: true, profile_id: "custom-a", deleted_label: "Original A", profiles: remainingProfiles() });
const selectIds = new Set(["setup-platform", "setup-profile", "setup-ssh-key-mode",
  "profile-builder-ordering", "setup-storage-view-template", "setup-storage-view-template-select",
  "setup-storage-view-profile", "setup-storage-view-binding-mode"]);

function fixture(selection = "custom-a", running = true) {
  const nodes = new Map(), requests = [], timers = new Map(), frames = new Map();
  let timerSequence = 0;
  const panel = new Field();
  const values = {
    "setup-system-id": "unsaved-system", "setup-system-label": "  Unsaved label  ",
    "setup-platform": "linux", "setup-truenas-host": "  host.example.test  ",
    "setup-api-key": "  SYNTHETIC-API  ", "setup-api-user": "  service-user  ",
    "setup-api-password": "  SYNTHETIC-API-PASSWORD  ", "setup-enclosure-filter": "  raw-filter  ",
    "setup-tls-ca-bundle-path": "  /synthetic/ca.pem  ", "setup-tls-server-name": "  tls.example.test  ",
    "setup-ssh-host": "  ssh.example.test  ", "setup-ssh-user": "  raw-user  ",
    "setup-ssh-port": "0022", "setup-ssh-key-mode": "none", "setup-ssh-key-path": "  /synthetic/key  ",
    "setup-ssh-password": "  SYNTHETIC-SSH  ", "setup-ssh-sudo-password": "  SYNTHETIC-SUDO  ",
    "setup-ssh-commands": "  command-one\ncommand-two  ",
    "setup-bootstrap-host": "  bootstrap.example.test  ", "setup-bootstrap-user": "  bootstrap-user  ",
    "setup-bootstrap-password": "  SYNTHETIC-BOOTSTRAP  ",
    "setup-bootstrap-sudo-password": "  SYNTHETIC-BOOTSTRAP-SUDO  ",
    "setup-bmc-host": "  bmc.example.test  ", "setup-bmc-username": "  bmc-user  ",
    "setup-bmc-password": "  SYNTHETIC-BMC  ", "setup-bmc-timeout-seconds": "0030",
    "setup-profile": "", "profile-builder-ordering": "source-layout",
  };
  const visual = ["profile-preview-badge", "profile-preview-summary", "profile-preview-grid", "profile-preview-meta",
    "profile-catalog", "profile-catalog-count", "profile-builder-badge", "profile-builder-load-button",
    "profile-builder-reset-button", "profile-builder-delete-button", "profile-builder-save-button",
    "profile-builder-id", "profile-builder-label", "profile-builder-rows", "profile-builder-columns",
    "profile-builder-slot-count", "profile-builder-row-groups", "profile-builder-layout-text",
    "profile-builder-layout-editor", "profile-builder-result", "profile-builder-preview-badge",
    "profile-builder-preview-summary", "profile-builder-preview-grid", "profile-builder-preview-meta",
    "setup-storage-view-template", "setup-storage-view-template-select", "setup-storage-view-profile",
    "setup-storage-view-list", "setup-storage-view-count", "setup-storage-view-editor", "setup-storage-view-empty",
    "setup-storage-view-template-badge", "setup-storage-view-kind-badge", "setup-storage-view-preview-summary",
    "setup-storage-view-preview-grid", "setup-storage-view-preview-meta", "setup-storage-view-label",
    "setup-storage-view-id", "setup-storage-view-serials", "setup-storage-view-slot-labels",
    "setup-create-button", "setup-result", "admin-status-banner"];
  for (const id of new Set([...Object.keys(values), ...visual,
    "setup-ssh-enabled", "setup-verify-ssl", "setup-ssh-strict-host-key", "setup-make-default", "setup-ha-enabled"])) {
    const field = new Field(values[id] || "", selectIds.has(id) ? "select" : "input");
    if (field.kind === "select" && values[id]) {
      field.innerHTML = `<option value="${values[id]}">${values[id]}</option>`;
    }
    field.builder = id.startsWith("profile-builder-");
    if (id.startsWith("setup-")) field.panel = panel;
    nodes.set(id, field);
  }
  nodes.get("setup-ssh-enabled").checked = true;
  nodes.get("setup-make-default").checked = true;
  const templates = [{ id: "ses-auto", kind: "ses_enclosure", label: "Saved chassis", summary: "Synthetic chassis" }];
  const statePayload = profiles => ({ systems: [], profiles, storage_view_templates: templates,
    runtime: { available: true, containers: [{ key: "ui", running }] } });
  const response = (status, body) => ({ ok: status >= 200 && status < 300, status,
    headers: { get: () => null }, json: async () => body });
  const context = vm.createContext({ URLSearchParams, AbortController, DOMException, console,
    document: { getElementById: id => nodes.get(id) || null,
      querySelector: selector => selector === ".setup-panel" ? panel : null,
      querySelectorAll: selector => selector.startsWith("button") ? [nodes.get("profile-builder-delete-button"), nodes.get("profile-builder-save-button")] : [] },
    window: { ADMIN_BOOTSTRAP: statePayload(originalProfiles()), location: { search: "" },
      addEventListener() {}, confirm: () => true },
    setTimeout: callback => { const id = ++timerSequence; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    requestAnimationFrame: callback => { const id = ++timerSequence; frames.set(id, callback); return id; },
    cancelAnimationFrame: id => frames.delete(id),
    fetch: (url, options = {}) => {
      if (url === "/api/admin/history/orphaned") return Promise.resolve(response(200, { orphaned_systems: [] }));
      assert.ok(url === "/api/admin/state" || url === "/api/admin/system-setup" || url === "/api/admin/profiles/custom-a" || url === "/api/admin/profiles",
        `unexpected synthetic transport path: ${url}`);
      return new Promise((resolve, reject) => requests.push({ url, options,
        reply: (body, status = 200) => resolve(response(status, body)), reject }));
    },
  });
  vm.runInContext(declarations, context, { filename: "admin.js" });
  const api = context.api, { state, elements } = api;
  api.bindEvents();
  api.renderProfileOptions();
  elements.setupProfile.value = selection;
  elements.setupProfile.dispatch("change");
  api.loadProfileIntoBuilder(state.profiles[0]);
  state.storageViews = [{ id: "draft-view", label: "Saved view", kind: "ses_enclosure", template_id: "ses-auto",
    profile_id: "", enabled: true, order: 10, render: {}, binding: {}, layout_overrides: null }];
  state.selectedStorageViewId = "draft-view";
  api.renderStorageViews(); api.flushStorageViewRender();
  // These text inputs have not emitted change, so their exact raw values are
  // newer than the normalized storage-view state and must survive catalog work.
  elements.setupStorageViewLabel.value = "  Dirty view label  ";
  elements.setupStorageViewId.value = "  Dirty-VIEW-ID  ";
  elements.setupStorageViewSerials.value = "SANITIZED-DRAFT,\n";
  elements.setupStorageViewSlotLabels.value = "0: Raw label  \n";
  elements.setupSystemLabel.dispatch("input");
  const dependentSelects = new Set(["setup-profile", "setup-storage-view-template", "setup-storage-view-profile"]);
  const rawSnapshot = () => JSON.stringify([...nodes].filter(([id]) => id.startsWith("setup-") && !dependentSelects.has(id))
    .map(([id, field]) => [id, field.value, field.checked]));
  const controlsSnapshot = () => JSON.stringify([...nodes].filter(([id]) => id.startsWith("setup-") || id.startsWith("profile-builder-"))
    .map(([id, field]) =>
    [id, field.value, field.checked, field.disabled, field.textContent, field.innerHTML, field.style]));
  return { api, state, elements, requests, nodes, panel, statePayload, rawSnapshot, controlsSnapshot,
    paint: () => api.flushStorageViewRender(),
    clickDelete: () => elements.profileBuilderDeleteButton.dispatch("click") };
}

async function drain() { for (let i = 0; i < 80; i++) await Promise.resolve(); }
async function completeDelete(p, readback, catalog = remainingProfiles()) {
  const offset = p.requests.length;
  p.clickDelete();
  assert.equal(p.requests[offset].options.method, "DELETE", "real registered delete callback dispatched");
  p.requests[offset].reply(deleteResult()); await drain();
  assert.equal(p.requests[offset + 1].url, "/api/admin/state", "real catalog-only readback dispatched");
  if (readback === "success") p.requests[offset + 1].reply(p.statePayload(catalog));
  else p.requests[offset + 1].reject(new Error("Synthetic readback failure"));
  await drain(); p.paint();
  assert.equal(p.state.adminEditorOutcomes["profile-delete"].outcome, "success");
  assert.match(p.elements.profileBuilderResult.textContent, readback === "success" ? /Deleted/ : /catalog refresh is unavailable/);
}
async function submittedSetup(p) {
  p.elements.setupCreateButton.dispatch("click"); await drain();
  const request = p.requests.find(item => item.url === "/api/admin/system-setup");
  assert.ok(request, "real registered save callback dispatched the actual payload");
  assert.equal(request.options.method, "POST");
  const payload = JSON.parse(request.options.body);
  // Refuse the synthetic save, avoiding unrelated successful-save refresh work.
  request.reply({ detail: "Synthetic save refusal" }, 400); await drain();
  return payload;
}

function pinStorageProfile(p, selection) {
  p.elements.setupStorageViewProfile.value = selection;
  p.elements.setupStorageViewProfile.dispatch("change");
  p.paint();
  assert.equal(p.state.storageViews[0].profile_id, selection,
    "real registered storage editor change recorded the explicit pin");
  // Keep raw inputs newer than the normalized model after the change's paint.
  p.elements.setupStorageViewLabel.value = "  New raw view label  ";
  p.elements.setupStorageViewSlotLabels.value = "0: New raw slot label  \n";
}

for (const readback of ["success", "fallback"]) {
  for (const selection of ["custom-a", "custom-b", ""]) {
    for (const running of [true, false]) {
      test(`profile delete storage: ${readback}, ${selection || "auto"}, runtime ${running ? "running" : "stopped"} actual POST agrees with model and preview`, async () => {
        const p = fixture("custom-b", running);
        pinStorageProfile(p, selection);
        const raw = p.rawSnapshot(), revision = p.state.setupDraftRevision;
        await completeDelete(p, readback);
        const expected = selection === "custom-a" ? "" : selection;
        const payload = await submittedSetup(p);
        assert.equal(payload.storage_views[0].profile_id, expected || null,
          "actual registered Create/Save POST cannot submit the deleted storage pin");
        assert.equal(p.state.storageViews[0].profile_id, expected, "submitted model agrees with selector");
        assert.equal(p.elements.setupStorageViewProfile.value, expected);
        assert.match(p.elements.setupStorageViewPreviewSummary.textContent,
          expected ? /saved chassis layout Readback B/ : /current live profile Readback B/);
        assert.match(p.elements.setupStorageViewPreviewMeta.innerHTML, /4 slots/);
        assert.equal(payload.default_profile_id, "custom-b");
        assert.equal(payload.ssh_password, "  SYNTHETIC-SSH  ");
        assert.equal(p.rawSnapshot(), raw, "catalog reconciliation preserves every unrelated raw draft field");
        assert.equal(p.state.setupDraftRevision, revision + (selection === "custom-a" ? 1 : 0),
          "storage-only submitted model changes advance ownership once");
        assert.equal(p.elements.profileBuilderDeleteButton.disabled, true, "originating finalizer remains admitted");
      });
    }
  }
  for (const selected of ["custom-a", "custom-b"]) {
    test(`profile delete storage: ${readback} clears nonselected pins while selected ${selected} and unrelated model fields survive`, async () => {
      const p = fixture("custom-b"); pinStorageProfile(p, selected);
      const view = p.state.storageViews[0];
      p.state.storageViews.push(...["custom-a", "custom-b", ""].map((profileId, index) => ({
        ...JSON.parse(JSON.stringify(view)), id: `other-view-${index}`, label: `Other view ${index}`,
        profile_id: profileId, order: (index + 2) * 10,
        layout_overrides: { slot_labels: { 0: "SANITIZED-SLOT" }, slot_sizes: {} },
      })));
      const before = JSON.parse(JSON.stringify(p.state.storageViews)), raw = p.rawSnapshot();
      const revision = p.state.setupDraftRevision;
      await completeDelete(p, readback);
      const payload = await submittedSetup(p);
      const expected = before.map(view => ({ ...view, profile_id: view.profile_id === "custom-a" ? "" : view.profile_id }));
      assert.deepEqual(payload.storage_views.map(view => view.profile_id), expected.map(view => view.profile_id || null),
        "actual POST reconciles all submitted views, not only the selected editor");
      assert.deepEqual(JSON.parse(JSON.stringify(p.state.storageViews)), expected,
        "only catalog-invalid model profile IDs change");
      assert.equal(p.elements.setupStorageViewProfile.value, selected === "custom-a" ? "" : selected);
      assert.equal(p.rawSnapshot(), raw);
      assert.equal(p.state.setupDraftRevision, revision + 1, "multiple invalid pins are one synchronous draft change");
    });
  }
  test(`profile delete storage: ${readback} later registered editor change cannot restore the deleted pin`, async () => {
    const p = fixture(); pinStorageProfile(p, "custom-a");
    await completeDelete(p, readback);
    p.elements.setupStorageViewLabel.value = "Later edited label";
    p.elements.setupStorageViewLabel.dispatch("change"); p.paint();
    assert.equal((await submittedSetup(p)).storage_views[0].profile_id, null,
      "normal editor save/normalization cannot resurrect a removed profile ID");
    assert.equal(p.state.storageViews[0].profile_id, "");
    assert.equal(p.elements.setupStorageViewProfile.value, "");
    assert.match(p.elements.setupStorageViewPreviewSummary.textContent, /current live profile Readback B/);
  });
  test(`profile delete storage: ${readback} model-only invalidation retires an older registered profile save`, async () => {
    const p = fixture("custom-b"); pinStorageProfile(p, "custom-a");
    p.elements.profileBuilderSaveButton.dispatch("click"); await drain();
    assert.equal(p.requests[0].url, "/api/admin/profiles");
    assert.equal(p.requests[0].options.method, "POST", "real registered older profile save is pending");
    await completeDelete(p, readback);
    const before = p.controlsSnapshot(), banner = p.elements.banner.textContent;
    p.requests[0].reply({ detail: "Synthetic older save refusal" }, 400); await drain(); p.paint();
    assert.equal(p.controlsSnapshot(), before, "model revision fences the older error/result/finalizer");
    assert.equal(p.elements.banner.textContent, banner, "older save cannot replace the deletion notification");
    assert.equal((await submittedSetup(p)).storage_views[0].profile_id, null);
  });
}

test("profile delete storage: model-only invalidation retires queued catalog read notification ownership", async () => {
  const p = fixture("custom-b"); pinStorageProfile(p, "custom-a");
  p.clickDelete(); p.requests[0].reply(deleteResult()); await drain();
  const queued = p.api.refreshState({ quiet: false, catalogOnly: true });
  p.requests[1].reject(new Error("Synthetic deletion readback failure")); await drain();
  assert.equal(p.requests[2].url, "/api/admin/state", "real queued read starts after the deletion readback");
  const banner = p.elements.banner.textContent;
  assert.match(banner, /Deleted.*catalog refresh is unavailable/);
  p.requests[2].reject(new Error("Synthetic queued read failure"));
  assert.equal(await queued, false); await drain(); p.paint();
  assert.equal(p.elements.banner.textContent, banner, "queued caller captured before the model revision cannot publish over it");
  assert.equal((await submittedSetup(p)).storage_views[0].profile_id, null);
});

test("profile delete storage: successful current same-ID recreation preserves the explicit pin", async () => {
  const p = fixture(); pinStorageProfile(p, "custom-a");
  const revision = p.state.setupDraftRevision, raw = p.rawSnapshot();
  await completeDelete(p, "success", [profile("custom-a", 5, "Recreated A"), ...remainingProfiles()]);
  assert.equal((await submittedSetup(p)).storage_views[0].profile_id, "custom-a");
  assert.equal(p.state.storageViews[0].profile_id, "custom-a");
  assert.equal(p.elements.setupStorageViewProfile.value, "custom-a");
  assert.match(p.elements.setupStorageViewPreviewSummary.textContent, /saved chassis layout Recreated A/);
  assert.match(p.elements.setupStorageViewPreviewMeta.innerHTML, /5 slots/);
  assert.equal(p.state.setupDraftRevision, revision);
  assert.equal(p.rawSnapshot(), raw);
});

for (const readback of ["success", "fallback"]) {
  test(`profile delete setup: ${readback} actual registered save never submits deleted profile`, async () => {
    const p = fixture();
    await completeDelete(p, readback);
    const payload = await submittedSetup(p);
    assert.equal(payload.default_profile_id, null, "actual outbound setup payload must not retain the deleted profile");
  });
  for (const selection of ["custom-a", "custom-b", ""]) {
    for (const running of [true, false]) {
      test(`profile delete setup: ${readback}, ${selection || "auto"}, runtime ${running ? "running" : "stopped"} synchronizes select and submission`, async () => {
        const p = fixture(selection, running), raw = p.rawSnapshot(), revision = p.state.setupDraftRevision;
        await completeDelete(p, readback);
        const expected = selection === "custom-a" ? "" : selection;
        assert.equal(p.elements.setupProfile.value, expected, "deleted selection cleared, unrelated/auto selection retained");
        assert.equal(p.state.selectedProfileId, expected, "state selection agrees with DOM");
        assert.ok(!p.elements.setupProfile.options.some(option => option.value === "custom-a"));
        assert.equal(p.rawSnapshot(), raw, "all unrelated raw setup, platform, SSH and storage editor fields retained");
        assert.equal(p.state.setupDirty, true);
        assert.equal(p.state.setupDraftRevision, revision + (selection === "custom-a" ? 1 : 0),
          "only catalog-driven submitted selection changes advance draft ownership");
        assert.equal(p.elements.profileBuilderDeleteButton.disabled, true);
        assert.equal(p.state.loadedBuilderProfileId, readback === "success" ? "" : "custom-a");
        const payload = await submittedSetup(p);
        assert.equal(payload.default_profile_id, expected || null, "actual save consumer cannot submit the deleted setup ID");
        assert.equal(payload.platform, "linux");
        assert.equal(payload.ssh_password, "  SYNTHETIC-SSH  ");
      });
    }
  }
  test(`profile delete setup: ${readback} refreshes preview geometry and dependent storage options without resetting raw editor`, async () => {
    const p = fixture(), raw = p.rawSnapshot();
    assert.equal(p.elements.profilePreviewSummary.textContent, "Synthetic Original A layout");
    assert.match(p.elements.setupStorageViewPreviewSummary.textContent, /Original A/);
    await completeDelete(p, readback);
    assert.equal(p.elements.profilePreviewSummary.textContent, "Synthetic Readback B layout");
    assert.equal(p.elements.profilePreviewBadge.textContent, "Auto Preview");
    assert.match(p.elements.profilePreviewMeta.innerHTML, /4 visible bays/);
    assert.equal([...p.elements.profilePreviewGrid.innerHTML.matchAll(/class="profile-preview-cell"/g)].length, 4);
    assert.match(p.elements.setupStorageViewPreviewSummary.textContent, /Readback B/);
    assert.match(p.elements.setupStorageViewPreviewMeta.innerHTML, /4 slots/);
    assert.equal([...p.elements.setupStorageViewPreviewGrid.innerHTML.matchAll(/class="profile-preview-cell"/g)].length, 4);
    assert.ok(!p.elements.setupStorageViewProfile.options.some(option => option.value === "custom-a"));
    assert.ok(!p.elements.setupStorageViewTemplate.options.some(option => option.value === "profile:custom-a"));
    assert.match(p.elements.setupStorageViewList.innerHTML, /Readback B/);
    assert.equal(p.rawSnapshot(), raw);
  });
  test(`profile delete setup: ${readback} empty catalog clears geometry and permits automatic submission`, async () => {
    const p = fixture(); pinStorageProfile(p, "custom-a");
    p.clickDelete(); p.requests[0].reply({ ...deleteResult(), profiles: [] }); await drain();
    if (readback === "success") p.requests[1].reply(p.statePayload([]));
    else p.requests[1].reject(new Error("Synthetic readback failure"));
    await drain(); p.paint();
    assert.equal(p.elements.setupProfile.value, "");
    assert.equal(p.elements.profilePreviewGrid.innerHTML, "");
    assert.equal(p.elements.profilePreviewBadge.textContent, "No profiles");
    const payload = await submittedSetup(p);
    assert.equal(payload.default_profile_id, null);
    assert.equal(payload.storage_views[0].profile_id, null);
    assert.equal(p.state.storageViews[0].profile_id, "");
  });
}

for (const replacement of ["different entries", "equal snapshot", "same-ID recreation"]) {
  test(`profile delete setup: newer ${replacement} admitted during DELETE survives fallback`, async () => {
    const p = fixture(); pinStorageProfile(p, "custom-a");
    const raw = p.rawSnapshot();
    p.clickDelete();
    const read = p.api.refreshState({ quiet: true, catalogOnly: true });
    const catalog = replacement === "equal snapshot" ? originalProfiles()
      : replacement === "same-ID recreation" ? [profile("custom-a", 5, "Recreated A"), ...remainingProfiles()]
        : remainingProfiles();
    p.requests[1].reply(p.statePayload(catalog)); assert.equal(await read, true);
    const admitted = p.state.profiles;
    p.requests[0].reply(deleteResult()); await drain();
    p.requests[2].reject(new Error("Synthetic follow-up failure")); await drain(); p.paint();
    assert.equal(p.state.profiles, admitted, "validated mutation fallback cannot undo newer server admission");
    const expected = replacement === "different entries" ? "" : "custom-a";
    assert.equal(p.elements.setupProfile.value, expected);
    assert.equal(p.state.selectedProfileId, expected);
    assert.equal(p.elements.profilePreviewSummary.textContent, catalog[0].summary);
    assert.equal(p.rawSnapshot(), raw);
    const payload = await submittedSetup(p);
    assert.equal(payload.default_profile_id, expected || null);
    assert.equal(payload.storage_views[0].profile_id, expected || null, "storage pin follows the current admitted catalog, not DELETE's older fallback");
    assert.equal(p.state.storageViews[0].profile_id, expected);
    assert.equal(p.elements.setupStorageViewProfile.value, expected);
  });
}

for (const [phase, completion] of ["DELETE", "readback"].flatMap(phase =>
  ["success", "fallback"].map(completion => [phase, completion]))) {
  for (const move of ["setup edit", "storage edit", "new builder", "same builder revisit", "successor delete"]) {
    test(`profile delete setup: retired ${phase} ${move} does not overwrite successor controls on ${completion}`, async () => {
      const p = fixture(); pinStorageProfile(p, "custom-a"); p.clickDelete();
      if (phase === "readback") { p.requests[0].reply(deleteResult()); await drain(); }
      if (move === "setup edit") {
        p.elements.setupSystemLabel.value = "  Successor draft  "; p.elements.setupSystemLabel.dispatch("input");
      } else if (move === "storage edit") {
        p.elements.setupStorageViewProfile.value = "custom-b"; p.elements.setupStorageViewProfile.dispatch("change");
      } else if (move === "new builder") {
        p.elements.setupProfile.value = "custom-b"; p.elements.setupProfile.dispatch("change");
        p.elements.profileBuilderLoadButton.dispatch("click");
      } else if (move === "same builder revisit") p.elements.profileBuilderLoadButton.dispatch("click");
      else {
        p.elements.setupSystemLabel.dispatch("input");
        p.clickDelete();
        assert.equal(p.elements.profileBuilderDeleteButton.disabled, true);
      }
      p.paint();
      const before = p.controlsSnapshot(), catalog = p.state.profiles;
      const model = JSON.stringify(p.state.storageViews), revision = p.state.setupDraftRevision;
      if (phase === "DELETE") { p.requests[0].reply(deleteResult()); await drain(); }
      const readback = p.requests.find(item => item.url === "/api/admin/state");
      assert.ok(readback);
      if (completion === "success") readback.reply(p.statePayload(remainingProfiles()));
      else readback.reject(new Error("Synthetic retired readback failure"));
      await drain(); p.paint();
      assert.equal(p.controlsSnapshot(), before, "stale completion does not render/reset successor controls");
      assert.equal(JSON.stringify(p.state.storageViews), model, "retired deletion cannot edit the successor storage model");
      assert.equal(p.state.setupDraftRevision, revision);
      if (completion === "fallback") assert.equal(p.state.profiles, catalog, "retired fallback cannot replace a successor catalog");
      else assert.deepEqual(JSON.parse(JSON.stringify(p.state.profiles)), remainingProfiles(),
        "new server-admitted catalog remains authoritative even when editor ownership retired");
      if (move === "successor delete") {
        const successor = p.requests.filter(item => item.options.method === "DELETE")[1];
        successor.reply({ detail: "Synthetic successor refusal" }, 400); await drain();
      }
    });
  }
}

for (const invalid of ["refusal", "ok only", "wrong target", "deleted ID remains"]) {
  test(`profile delete setup: ${invalid} cannot reconcile setup controls`, async () => {
    const p = fixture(); pinStorageProfile(p, "custom-a");
    const catalog = p.state.profiles, raw = p.rawSnapshot(); p.clickDelete();
    const body = invalid === "ok only" ? { ok: true } : invalid === "wrong target" ? { ...deleteResult(), profile_id: "custom-b" }
      : invalid === "deleted ID remains" ? { ...deleteResult(), profiles: originalProfiles() } : { detail: "Synthetic refusal" };
    p.requests[0].reply(body, invalid === "refusal" ? 400 : 200); await drain(); p.paint();
    assert.equal(p.requests.length, 1, "unvalidated deletion cannot read or reconcile catalogs");
    assert.equal(p.state.profiles, catalog);
    assert.equal(p.elements.setupProfile.value, "custom-a");
    assert.equal(p.state.storageViews[0].profile_id, "custom-a", "invalid mutation cannot invalidate storage pins");
    assert.equal(p.rawSnapshot(), raw);
    assert.equal(p.elements.profileBuilderDeleteButton.disabled, false);
  });
}

test("profile delete setup: invalid state read admits validated fallback and clears actual save payload", async () => {
  const p = fixture(), raw = p.rawSnapshot(); p.clickDelete();
  p.requests[0].reply(deleteResult()); await drain();
  p.requests[1].reply({ profiles: [], systems: null }); await drain(); p.paint();
  assert.equal(p.elements.setupProfile.value, "");
  assert.equal(p.rawSnapshot(), raw);
  assert.equal((await submittedSetup(p)).default_profile_id, null);
});

test("profile delete setup: referenced/built-in/absent profiles stay ineligible and stopped sessions lock controls", async () => {
  for (const capability of ["referenced", "built-in", "absent", "session stopped"]) {
    const p = fixture(); pinStorageProfile(p, "custom-a");
    if (capability === "referenced") p.state.profiles[0].reference_count = 1;
    if (capability === "built-in") p.state.profiles[0].is_custom = false;
    if (capability === "absent") p.state.profiles.shift();
    if (capability === "session stopped") p.state.sessionStopped = true;
    p.elements.profileBuilderLabel.dispatch("input");
    assert.equal(p.elements.profileBuilderDeleteButton.disabled, true, capability);
    p.clickDelete(); await drain(); assert.equal(p.requests.length, 0);
    assert.equal(p.state.storageViews[0].profile_id, "custom-a");
  }
});
