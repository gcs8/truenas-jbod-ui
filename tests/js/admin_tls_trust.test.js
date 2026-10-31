"use strict";

// #954: TLS trust with an expired certificate. Synthetic hosts and dates only.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const TEMPLATE_PATH = path.resolve(__dirname, "../../admin_service/templates/index.html");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");
const TEMPLATE = fs.readFileSync(TEMPLATE_PATH, "utf8");

function functionSource(name) {
  const match = new RegExp(`^  (?:async )?function ${name}\\(`, "m").exec(SOURCE);
  assert.ok(match, `function ${name} must exist`);
  const start = match.index;
  const bodyStart = SOURCE.indexOf(") {", start) + 2;
  let depth = 0;
  for (let index = bodyStart; index < SOURCE.length; index += 1) {
    if (SOURCE[index] === "{") depth += 1;
    if (SOURCE[index] === "}") {
      depth -= 1;
      if (depth === 0) return SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`unterminated ${name}`);
}

function load(names, bindings = {}) {
  const context = vm.createContext({ console, encodeURIComponent, Date, ...bindings });
  vm.runInContext(
    `${names.map(functionSource).join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.__tested;
}

class FakeElement {
  constructor({ value = "", checked = false, placeholder = "" } = {}) {
    this.value = value;
    this.checked = checked;
    this.placeholder = placeholder;
    this.disabled = false;
    this.textContent = "";
    this.innerHTML = "";
    this.className = "";
    this.attributes = new Map();
    this.classes = new Set();
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !this.classes.has(name) : Boolean(force);
        if (on) this.classes.add(name);
        else this.classes.delete(name);
        return on;
      },
    };
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const SYNTHETIC_HOST = "https://truenas.example.test";

function leaf(overrides = {}) {
  return {
    subject: "CN=truenas.example.test",
    issuer: "CN=truenas.example.test",
    not_valid_before: "2025-01-01T00:00:00+00:00",
    not_valid_after: "2025-06-01T12:30:00+00:00",
    expired: false,
    not_yet_valid: false,
    san_dns: ["truenas.example.test"],
    san_ip: [],
    ...overrides,
  };
}

function renderInspection(inspectedLeaf) {
  const elements = {
    setupTlsInspectionMeta: new FakeElement(),
    setupTlsLeafDetails: new FakeElement(),
    setupTlsInspectionResult: new FakeElement(),
  };
  const state = { tlsInspection: { host: "https://truenas.example.test", certificate_count: 1, leaf: inspectedLeaf } };
  const { renderTlsInspection } = load(["formatTlsValidityDate", "tlsLeafValidityWarning", "renderTlsInspection"], {
    elements,
    escapeHtml,
    state,
  });
  renderTlsInspection();
  return elements.setupTlsLeafDetails.innerHTML;
}

test("inspection warns that an expired leaf cannot be pinned, with its date", () => {
  const html = renderInspection(leaf({ expired: true }));

  assert.match(html, /This certificate expired on 2025-06-01 12:30 UTC\./);
  assert.match(html, /Pinning it won(?:'|&#39;)t work\. Renew it on the server, then trust the new one\./);
  assert.match(html, /role="alert"/);
});

test("inspection warns about a not-yet-valid leaf and stays quiet for a current one", () => {
  const future = renderInspection(leaf({ not_yet_valid: true, not_valid_before: "2030-02-03T04:05:00+00:00" }));
  assert.match(future, /not valid until 2030-02-03 04:05 UTC/);

  const current = renderInspection(leaf());
  assert.doesNotMatch(current, /expired on|not valid until|role="alert"/);
});

function trustHarness({ validation, endpoint = "trust" }) {
  const elements = {
    setupVerifySsl: new FakeElement({ checked: false }),
    setupTlsCaBundlePath: new FakeElement({ value: "" }),
    setupTlsImportResult: new FakeElement(),
    setupTrustRemoteTlsButton: new FakeElement(),
    setupTlsImportCaButton: new FakeElement(),
    setupSystemId: new FakeElement({ value: "synthetic-core" }),
    setupTlsCaFile: { files: [{ name: "synthetic.pem" }] },
  };
  const state = { sessionStopped: false };
  const banners = [];
  const synced = [];
  const bindings = {
    elements,
    state,
    collectTlsTargetHost: () => "https://truenas.example.test",
    collectTlsServerName: () => "",
    captureTlsEditorOperation: () => ({ owns: () => true, latest: () => true, serverName: "", advance: () => {} }),
    suggestedTlsBundleName: () => "synthetic-core",
    readTextFile: async () => "synthetic pem text",
    fetchJson: async () => ({
      ok: true,
      bundle_path: "/synthetic/tls/synthetic-core.pem",
      certificate_count: 1,
      inspection: null,
      validation,
    }),
    setupDraftSnapshot: () => JSON.stringify([elements.setupVerifySsl.checked, elements.setupTlsCaBundlePath.value]),
    recordSetupDraftChange: () => {},
    renderTlsInspection: () => {},
    syncVerifySslHelp: () => {},
    syncTlsServerNameHelp: () => {},
    renderTlsServerNameSuggestions: () => {},
    syncApiSecretReentry: () => {},
    syncTlsTrustStatus: (value) => {
      synced.push(value);
      return Boolean(value && value.validated);
    },
    buildTlsValidationSuggestion: (value) => String(value?.detail || ""),
    defaultTlsTrustDetail: () => "synthetic default detail",
    setBanner: (message, tone) => banners.push({ message, tone }),
  };
  const name = endpoint === "trust" ? "trustRemoteTlsCertificate" : "importTlsBundle";
  const fn = load([name], bindings)[name];
  return { run: fn, elements, banners, synced };
}

for (const endpoint of ["trust", "import"]) {
  test(`${endpoint} leaves certificate checks off when the saved bundle does not validate`, async () => {
    const harness = trustHarness({
      endpoint,
      validation: {
        validated: false, host: SYNTHETIC_HOST,
        bundle_path: "/synthetic/tls/synthetic-core.pem",
        detail: "TLS verification failed with the saved bundle: certificate verify failed: Hostname mismatch",
      },
    });
    await harness.run();

    assert.equal(harness.elements.setupVerifySsl.checked, false, "a bundle that cannot pass must not switch verification on");
    assert.equal(harness.elements.setupTlsCaBundlePath.value, "/synthetic/tls/synthetic-core.pem");
    assert.match(harness.elements.setupTlsImportResult.textContent, /Certificate checks were left off/);
  });

  test(`${endpoint} turns certificate checks on once the saved bundle validates`, async () => {
    const harness = trustHarness({
      endpoint,
      validation: {
        validated: true, host: SYNTHETIC_HOST,
        bundle_path: "/synthetic/tls/synthetic-core.pem",
        detail: "Verified.",
      },
    });
    await harness.run();

    assert.equal(harness.elements.setupVerifySsl.checked, true);
  });
}

// #737: TLS inspect and trust-remote answers belong to the editor and target
// that asked. Synthetic hosts, paths and fingerprints only.
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function tlsEditorHarness() {
  const elements = {
    setupSystemId: new FakeElement({ value: "synthetic-core" }),
    setupPlatform: new FakeElement({ value: "core" }),
    setupTruenasHost: new FakeElement({ value: SYNTHETIC_HOST }),
    setupTlsServerName: new FakeElement({ value: "" }),
    setupVerifySsl: new FakeElement({ checked: false }),
    setupTlsCaBundlePath: new FakeElement({ value: "" }),
    setupInspectTlsButton: new FakeElement(),
    setupTrustRemoteTlsButton: new FakeElement(),
    setupTlsInspectionResult: new FakeElement(),
    setupTlsImportResult: new FakeElement(),
  };
  const state = {
    sessionStopped: false, setupEditorGeneration: 1, setupDraftRevision: 0, loadedSystemId: "synthetic-core",
    profileBuilderGeneration: 0, profileBuilderRevision: 0, tlsInspection: null,
  };
  const requests = [];
  const banners = [];
  const synced = [];
  const tested = load(
    ["collectTlsTargetHost", "collectTlsServerName", "captureAdminEditorOperation", "captureTlsEditorOperation",
      "inspectTlsCertificate", "trustRemoteTlsCertificate"],
    {
      elements,
      state,
      JSON,
      fetchJson: (url, options) => {
        const pending = deferred();
        requests.push({ ...pending, url, body: JSON.parse(options.body) });
        return pending.promise;
      },
      tlsLeafValidityWarning: () => "",
      renderTlsInspection: () => {},
      syncTlsServerNameHelp: () => {},
      renderTlsServerNameSuggestions: () => {},
      syncVerifySslHelp: () => {},
      syncApiSecretReentry: () => {},
      suggestedTlsBundleName: () => "synthetic-core",
      setupDraftSnapshot: () => JSON.stringify([elements.setupVerifySsl.checked, elements.setupTlsCaBundlePath.value]),
      recordSetupDraftChange: (before) => {
        if (before !== JSON.stringify([elements.setupVerifySsl.checked, elements.setupTlsCaBundlePath.value])) {
          state.setupDraftRevision += 1;
        }
      },
      syncTlsTrustStatus: (value) => {
        synced.push(value);
        return Boolean(value && value.validated);
      },
      buildTlsValidationSuggestion: () => "",
      setBanner: (message, tone) => banners.push({ message, tone }),
    }
  );
  return { ...tested, elements, state, requests, banners, synced };
}

const INSPECTION_A = { host: SYNTHETIC_HOST, certificate_count: 1, leaf: { sha256: "AA:SYNTHETIC" } };
const TRUSTED_A = {
  ok: true,
  bundle_path: "/synthetic/tls/synthetic-core.pem",
  inspection: INSPECTION_A,
  validation: { validated: true, host: SYNTHETIC_HOST },
};

// Each edit mirrors what the page does: field input bumps the draft revision,
// loading another system bumps the editor generation.
const TLS_EDITS = {
  "the HTTPS host changed": (h) => {
    h.elements.setupTruenasHost.value = "https://other-nas.example.test";
    h.state.setupDraftRevision += 1;
    h.state.tlsInspection = null;
  },
  "another system was loaded": (h) => {
    h.state.setupEditorGeneration += 1;
    h.state.loadedSystemId = "synthetic-other";
    h.elements.setupSystemId.value = "synthetic-other";
  },
  "only the certificate name changed": (h) => {
    h.elements.setupTlsServerName.value = "other-name.example.test";
  },
};

for (const [label, edit] of Object.entries(TLS_EDITS)) {
  test(`a late TLS inspection is dropped after ${label}`, async () => {
    const harness = tlsEditorHarness();
    const pending = harness.inspectTlsCertificate();
    assert.equal(harness.requests.length, 1);
    edit(harness);
    const shownBefore = harness.elements.setupTlsInspectionResult.textContent;
    harness.requests[0].resolve({ inspection: INSPECTION_A });
    await pending;

    assert.equal(harness.state.tlsInspection, null, "an older target's certificate must not be shown");
    assert.equal(harness.elements.setupTlsInspectionResult.textContent, shownBefore);
    assert.deepEqual(harness.banners, []);
  });

  test(`a late trust-remote answer does not change the form after ${label}`, async () => {
    const harness = tlsEditorHarness();
    const pending = harness.trustRemoteTlsCertificate();
    edit(harness);
    const revision = harness.state.setupDraftRevision;
    harness.requests[0].resolve(TRUSTED_A);
    await pending;

    assert.equal(harness.elements.setupTlsCaBundlePath.value, "", "an older target's bundle must not be put in this form");
    assert.equal(harness.elements.setupVerifySsl.checked, false, "certificate checks must not be turned on for another target");
    assert.equal(harness.state.tlsInspection, null);
    assert.equal(harness.state.setupDraftRevision, revision);
    assert.deepEqual(harness.synced, []);
    assert.deepEqual(harness.banners, []);
  });
}

test("a late TLS failure for an older target does not overwrite the current result", async () => {
  const harness = tlsEditorHarness();
  const pending = harness.trustRemoteTlsCertificate();
  TLS_EDITS["the HTTPS host changed"](harness);
  harness.requests[0].reject(new Error("synthetic handshake failure"));
  await pending;

  assert.doesNotMatch(harness.elements.setupTlsImportResult.textContent, /synthetic handshake failure/);
  assert.deepEqual(harness.synced, []);
  assert.deepEqual(harness.banners, []);
});

test("the newer of two TLS inspections wins whichever answer arrives last", async () => {
  const harness = tlsEditorHarness();
  const first = harness.inspectTlsCertificate();
  const second = harness.inspectTlsCertificate();
  const inspectionB = { ...INSPECTION_A, leaf: { sha256: "BB:SYNTHETIC" } };
  harness.requests[1].resolve({ inspection: inspectionB });
  await second;
  harness.requests[0].resolve({ inspection: INSPECTION_A });
  await first;

  assert.equal(harness.state.tlsInspection, inspectionB);
  assert.equal(harness.banners.length, 1);
});

test("a current TLS answer still applies and is sent with the target it belongs to", async () => {
  const harness = tlsEditorHarness();
  harness.elements.setupTlsServerName.value = "truenas.example.test";
  const inspect = harness.inspectTlsCertificate();
  assert.deepEqual(
    [harness.requests[0].body.host, harness.requests[0].body.tls_server_name],
    [SYNTHETIC_HOST, "truenas.example.test"]
  );
  harness.requests[0].resolve({ inspection: INSPECTION_A });
  await inspect;
  assert.equal(harness.state.tlsInspection, INSPECTION_A);
  assert.equal(harness.elements.setupInspectTlsButton.disabled, false);

  const trust = harness.trustRemoteTlsCertificate();
  harness.requests[1].resolve(TRUSTED_A);
  await trust;
  assert.equal(harness.elements.setupTlsCaBundlePath.value, TRUSTED_A.bundle_path);
  assert.equal(harness.elements.setupVerifySsl.checked, true);
  assert.equal(harness.elements.setupTrustRemoteTlsButton.disabled, false);
  assert.equal(harness.banners.at(-1).tone, "success");
});

// #737 review: some draft changes (storage-view delete, duplicate, order
// reset) bump the revision without releasing editor controls.
const UNRELEASED_DRAFT_CHANGE = (h) => {
  h.state.setupDraftRevision += 1;
};

test("a dropped late TLS answer still releases its button when nothing else released it", async () => {
  const harness = tlsEditorHarness();
  const inspect = harness.inspectTlsCertificate();
  assert.equal(harness.elements.setupInspectTlsButton.disabled, true);
  UNRELEASED_DRAFT_CHANGE(harness);
  harness.requests[0].resolve({ inspection: INSPECTION_A });
  await inspect;
  assert.equal(harness.state.tlsInspection, null, "the stale answer is still dropped");
  assert.equal(harness.elements.setupInspectTlsButton.disabled, false);

  const trust = harness.trustRemoteTlsCertificate();
  UNRELEASED_DRAFT_CHANGE(harness);
  harness.requests[1].reject(new Error("synthetic handshake failure"));
  await trust;
  assert.equal(harness.elements.setupTlsCaBundlePath.value, "");
  assert.equal(harness.elements.setupTrustRemoteTlsButton.disabled, false);
});

test("an older TLS request never releases the button a newer request holds", async () => {
  const harness = tlsEditorHarness();
  const first = harness.inspectTlsCertificate();
  const second = harness.inspectTlsCertificate();
  harness.requests[0].resolve({ inspection: INSPECTION_A });
  await first;
  assert.equal(harness.elements.setupInspectTlsButton.disabled, true, "the newer inspection still holds it");
  harness.requests[1].resolve({ inspection: INSPECTION_A });
  await second;
  assert.equal(harness.elements.setupInspectTlsButton.disabled, false);
});

test("editing or leaving the form releases the TLS buttons a retired request held", () => {
  const elements = {
    setupInspectTlsButton: new FakeElement(),
    setupTrustRemoteTlsButton: new FakeElement(),
  };
  elements.setupInspectTlsButton.disabled = true;
  elements.setupTrustRemoteTlsButton.disabled = true;
  const { retireAdminEditorControls } = load(["retireAdminEditorControls"], {
    elements,
    state: { sessionStopped: false, systems: [] },
    currentSetupPlatform: () => "core",
    bootstrapEnabledForSession: () => false,
    getProfileById: () => null,
    profileReferenceCount: () => 0,
  });
  retireAdminEditorControls();
  assert.equal(elements.setupInspectTlsButton.disabled, false);
  assert.equal(elements.setupTrustRemoteTlsButton.disabled, false);
});

function savedStatusHarness({ system, response, fields = null }) {
  const elements = {
    setupTruenasHost: new FakeElement({ value: system.truenas_host || "" }),
    setupVerifySsl: new FakeElement({ checked: Boolean(system.verify_ssl) }),
    setupTlsCaBundlePath: new FakeElement({ value: system.tls_ca_bundle_path || "" }),
    setupTlsServerName: new FakeElement({ value: system.tls_server_name || "" }),
    setupTlsTrustPill: new FakeElement(),
    setupTlsTrustDetail: new FakeElement(),
  };
  const state = { loadedSystemId: system.id, tlsTrustStatus: {}, tlsInspection: null };
  const requests = [];
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const tested = load(
    [
      "currentTlsSettingsKey",
      "savedSystemTlsSettingsKey",
      "refreshSavedTlsTrustStatus",
      "defaultTlsTrustDetail",
      "buildTlsValidationSuggestion",
      "setTlsTrustStatus",
      "syncTlsTrustStatus",
      "renderTlsTrustStatus",
    ],
    {
      elements,
      state,
      collectTlsTargetHost: () => elements.setupTruenasHost.value.trim(),
      collectTlsServerName: () => elements.setupTlsServerName.value.trim(),
      suggestedTlsServerNames: () => [],
      fetchJson: async (url) => {
        requests.push(url);
        await gate;
        if (fields) fields(elements, state);
        if (response instanceof Error) throw response;
        return response;
      },
    }
  );
  return { ...tested, elements, state, requests, release };
}

const SAVED_CORE = {
  id: "synthetic-core",
  truenas_host: "https://truenas.example.test",
  verify_ssl: true,
  tls_ca_bundle_path: "/synthetic/tls/synthetic-core.pem",
  tls_server_name: "",
};

test("loading a saved system re-checks its bundle and shows the real failure reason", async () => {
  const detail = "TLS verification failed with the saved bundle: certificate verify failed: certificate has expired";
  const harness = savedStatusHarness({
    system: SAVED_CORE,
    response: { ok: true, system_id: "synthetic-core", validation: { validated: false, detail, bundle_path: SAVED_CORE.tls_ca_bundle_path } },
  });

  const pending = harness.refreshSavedTlsTrustStatus(SAVED_CORE);
  assert.match(harness.elements.setupTlsTrustDetail.textContent, /Checking/);
  harness.release();
  await pending;

  assert.deepEqual(harness.requests, ["/api/admin/tls/saved-status?system_id=synthetic-core"]);
  assert.equal(harness.elements.setupTlsTrustDetail.textContent, detail);
  assert.doesNotMatch(harness.elements.setupTlsTrustDetail.textContent, /has not been validated/);

  // An unrelated re-sync keeps the known failure while the TLS fields are unchanged...
  harness.syncTlsTrustStatus();
  assert.equal(harness.elements.setupTlsTrustDetail.textContent, detail);
  // ...and drops it once a TLS field changes.
  harness.elements.setupTlsServerName.value = "other.example.test";
  harness.syncTlsTrustStatus();
  assert.notEqual(harness.elements.setupTlsTrustDetail.textContent, detail);
});

test("a remembered passing check does not read as Trusted once verification is off", async () => {
  const harness = savedStatusHarness({
    system: SAVED_CORE,
    response: { ok: true, validation: { validated: true, detail: "Verified synthetic bundle." } },
  });
  const pending = harness.refreshSavedTlsTrustStatus(SAVED_CORE);
  harness.release();
  await pending;
  assert.equal(harness.elements.setupTlsTrustPill.textContent, "Trusted");

  harness.elements.setupVerifySsl.checked = false;
  harness.syncTlsTrustStatus();
  assert.equal(harness.elements.setupTlsTrustPill.textContent, "Untrusted");
  assert.match(harness.elements.setupTlsTrustDetail.textContent, /verification is off/);

  harness.elements.setupVerifySsl.checked = true;
  harness.syncTlsTrustStatus();
  assert.equal(harness.elements.setupTlsTrustPill.textContent, "Trusted");
});

test("no re-check runs when verification is off or no bundle is saved", async () => {
  for (const system of [{ ...SAVED_CORE, verify_ssl: false }, { ...SAVED_CORE, tls_ca_bundle_path: "" }]) {
    const harness = savedStatusHarness({ system, response: { ok: true, validation: null } });
    harness.release();
    await harness.refreshSavedTlsTrustStatus(system);
    assert.deepEqual(harness.requests, []);
  }
});

test("a late re-check result for a system that is no longer loaded is ignored", async () => {
  const harness = savedStatusHarness({
    system: SAVED_CORE,
    response: { ok: true, validation: { validated: false, detail: "synthetic stale failure" } },
    fields: (_elements, state) => {
      state.loadedSystemId = "another-system";
    },
  });
  const pending = harness.refreshSavedTlsTrustStatus(SAVED_CORE);
  harness.release();
  await pending;

  assert.notEqual(harness.elements.setupTlsTrustDetail.textContent, "synthetic stale failure");
  assert.equal(harness.state.lastTlsValidation, null);
});

test("loading a system starts the saved-bundle re-check", () => {
  const loadSource = functionSource("loadSystemIntoForm");
  assert.match(loadSource, /state\.lastTlsValidation = null;/);
  assert.match(loadSource, /void refreshSavedTlsTrustStatus\(system\);/);
  assert.match(functionSource("resetSetupForm"), /state\.lastTlsValidation = null;/);
});

function reentryHarness(system, { apiKey = "", apiPassword = "" } = {}) {
  const elements = {
    setupSystemId: new FakeElement({ value: system.id }),
    setupPlatform: new FakeElement({ value: system.platform }),
    setupTruenasHost: new FakeElement({ value: system.truenas_host }),
    setupApiUser: new FakeElement({ value: system.api_user || "" }),
    setupVerifySsl: new FakeElement({ checked: Boolean(system.verify_ssl) }),
    setupTlsCaBundlePath: new FakeElement({ value: system.tls_ca_bundle_path || "" }),
    setupTlsServerName: new FakeElement({ value: system.tls_server_name || "" }),
    setupApiKey: new FakeElement({ value: apiKey, placeholder: "Saved value retained unless replaced" }),
    setupApiPassword: new FakeElement({ value: apiPassword }),
    setupApiSecretReentryNote: new FakeElement(),
  };
  elements.setupApiSecretReentryNote.classes.add("hidden");
  const state = { loadedSystemId: system.id, systems: [system] };
  const tested = load(
    [
      "savedSecretConfigured",
      "isEditingLoadedSystem",
      "getSystemById",
      "apiEndpointIdentity",
      "apiCredentialSettingsChanged",
      "syncApiSecretReentry",
    ],
    { elements, state, collectTlsServerName: () => elements.setupTlsServerName.value.trim() }
  );
  return { ...tested, elements };
}

const SAVED_WITH_KEY = {
  id: "synthetic-core",
  platform: "core",
  truenas_host: "https://truenas.example.test",
  api_user: "",
  verify_ssl: true,
  tls_ca_bundle_path: "/synthetic/tls/synthetic-core.pem",
  tls_server_name: "",
  api_key_configured: true,
};

test("changing a TLS field on a saved system marks the stored API key for re-entry", () => {
  for (const change of [
    (elements) => { elements.setupPlatform.value = "scale"; },
    (elements) => { elements.setupVerifySsl.checked = false; },
    (elements) => { elements.setupTlsCaBundlePath.value = "/synthetic/tls/other.pem"; },
    (elements) => { elements.setupTlsServerName.value = "truenas.example.test"; },
  ]) {
    const harness = reentryHarness(SAVED_WITH_KEY);
    harness.syncApiSecretReentry();
    assert.equal(harness.elements.setupApiKey.classes.has("needs-reentry"), false, "unchanged settings keep the saved key");
    assert.equal(harness.elements.setupApiSecretReentryNote.classes.has("hidden"), true);

    change(harness.elements);
    harness.syncApiSecretReentry();
    const key = harness.elements.setupApiKey;
    assert.equal(key.classes.has("needs-reentry"), true);
    assert.equal(key.getAttribute("aria-invalid"), "true");
    assert.match(key.placeholder, /Enter the API key again/);
    const note = harness.elements.setupApiSecretReentryNote;
    assert.equal(note.classes.has("hidden"), false);
    assert.match(note.textContent, /saved API key can't be reused/);
    assert.equal(harness.elements.setupApiPassword.classes.has("needs-reentry"), false, "no saved password, nothing to re-enter");

    key.value = "synthetic-replacement-value";
    harness.syncApiSecretReentry();
    assert.equal(key.classes.has("needs-reentry"), false);
    assert.equal(key.getAttribute("aria-invalid"), null);
    assert.equal(note.classes.has("hidden"), true);
  }
});

test("host spelling the server treats as the same does not ask for the key again", () => {
  for (const host of ["https://truenas.example.test/", "HTTPS://TrueNAS.Example.Test", "  https://truenas.example.test  "]) {
    const harness = reentryHarness(SAVED_WITH_KEY);
    harness.elements.setupTruenasHost.value = host;
    harness.syncApiSecretReentry();
    assert.equal(harness.elements.setupApiKey.classes.has("needs-reentry"), false, host);
  }
  const moved = reentryHarness(SAVED_WITH_KEY);
  moved.elements.setupTruenasHost.value = "https://other.example.test";
  moved.syncApiSecretReentry();
  assert.equal(moved.elements.setupApiKey.classes.has("needs-reentry"), true);
});

test("reverting the TLS change restores the saved-key placeholder", () => {
  const harness = reentryHarness(SAVED_WITH_KEY);
  harness.elements.setupVerifySsl.checked = false;
  harness.syncApiSecretReentry();
  harness.elements.setupVerifySsl.checked = true;
  harness.syncApiSecretReentry();

  assert.equal(harness.elements.setupApiKey.classes.has("needs-reentry"), false);
  assert.equal(harness.elements.setupApiKey.placeholder, "Saved value retained unless replaced");
});

test("TLS, host, platform, and API credential edits refresh the re-entry marker", () => {
  const bind = functionSource("bindEvents");
  for (const field of ["setupVerifySsl", "setupTlsCaBundlePath", "setupTlsServerName", "setupTruenasHost"]) {
    const start = bind.indexOf(`elements.${field}?.addEventListener(`);
    assert.notEqual(start, -1, `${field} must have a listener`);
    const end = bind.indexOf("\n    });", start);
    assert.match(bind.slice(start, end), /syncApiSecretReentry\(\)/, `${field} listener must refresh the re-entry marker`);
  }
  // Platform is part of the server's credential authority (Codex review on #965).
  const platformListeners = bind.split('elements.setupPlatform?.addEventListener("change"').slice(1)
    .map((rest) => rest.slice(0, rest.indexOf("\n    });")));
  assert.ok(
    platformListeners.some((body) => /syncApiSecretReentry\(\)/.test(body)),
    "a platform change must refresh the re-entry marker"
  );
  assert.match(
    bind,
    /\[elements\.setupApiKey, elements\.setupApiPassword, elements\.setupApiUser\]\.forEach\(\(field\) => \{\s*field\?\.addEventListener\("input", \(\) => \{\s*syncApiSecretReentry\(\);/,
    "typing a replacement API key, password, or user must refresh the re-entry marker"
  );
  assert.match(functionSource("loadSystemIntoForm"), /syncApiSecretReentry\(\);/);
  assert.match(TEMPLATE, /id="setup-api-secret-reentry-note"[^>]*class="[^"]*hidden/);
  assert.match(SOURCE, /setupApiSecretReentryNote: document\.getElementById\("setup-api-secret-reentry-note"\)/);
});
