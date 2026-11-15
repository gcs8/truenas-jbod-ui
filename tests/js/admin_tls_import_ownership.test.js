"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");

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
  const context = vm.createContext({ console, encodeURIComponent, JSON, Object, ...bindings });
  vm.runInContext(
    `${names.map(functionSource).join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.__tested;
}

class FakeElement {
  constructor({ value = "", checked = false } = {}) {
    this.value = value;
    this.checked = checked;
    this.disabled = false;
    this.textContent = "";
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const ALPHA_HOST = "https://alpha.example.test";
const BRAVO_HOST = "https://bravo.example.test";
const ALPHA_REPLY = {
  bundle_path: "/synthetic/tls/alpha.pem",
  certificate_count: 1,
  validation: {
    validated: true, host: ALPHA_HOST,
    bundle_path: "/synthetic/tls/alpha.pem",
    detail: "Verified synthetic alpha bundle.",
  },
};

function importHarness({ deferredRead = false } = {}) {
  const read = deferred();
  const elements = {
    setupSystemId: new FakeElement({ value: "alpha" }),
    setupSystemLabel: new FakeElement({ value: "Alpha" }),
    setupPlatform: new FakeElement({ value: "core" }),
    setupTruenasHost: new FakeElement({ value: ALPHA_HOST }),
    setupSshHost: new FakeElement(),
    setupSshUser: new FakeElement(),
    setupBootstrapHost: new FakeElement(),
    setupProfile: new FakeElement(),
    setupTlsServerName: new FakeElement({ value: "alpha.example.test" }),
    setupVerifySsl: new FakeElement({ checked: false }),
    setupTlsCaBundlePath: new FakeElement(),
    setupTlsImportResult: new FakeElement(),
    setupTlsImportCaButton: new FakeElement(),
    setupTlsCaFile: { files: [{ name: "synthetic-alpha.pem" }] },
  };
  const state = {
    sessionStopped: false,
    setupEditorGeneration: 1,
    setupDraftRevision: 0,
    loadedSystemId: "alpha",
    profileBuilderGeneration: 0,
    profileBuilderRevision: 0,
    tlsTrustStatus: { level: "untrusted", label: "Untrusted", detail: "Alpha not checked." },
    lastTlsValidation: null,
  };
  const requests = [];
  const banners = [];
  const tested = load(
    [
      "suggestedTlsBundleName",
      "collectTlsServerName",
      "collectTlsTargetHost",
      "captureAdminEditorOperation",
      "captureTlsEditorOperation",
      "currentTlsSettingsKey",
      "setTlsTrustStatus",
      "syncTlsTrustStatus",
      "importTlsBundle",
    ],
    {
      elements,
      state,
      suggestedConnectionHost: () => "alpha.example.test",
      readTextFile: () => deferredRead ? read.promise : Promise.resolve("SYNTHETIC ALPHA CERTIFICATE"),
      fetchJson: (url, options) => {
        const pending = deferred();
        requests.push({ ...pending, url, body: JSON.parse(options.body) });
        return pending.promise;
      },
      setupDraftSnapshot: () => JSON.stringify([
        elements.setupTlsCaBundlePath.value,
        elements.setupVerifySsl.checked,
      ]),
      recordSetupDraftChange: (before) => {
        const after = JSON.stringify([
          elements.setupTlsCaBundlePath.value,
          elements.setupVerifySsl.checked,
        ]);
        if (before !== after) state.setupDraftRevision += 1;
      },
      renderTlsTrustStatus: () => {},
      syncVerifySslHelp: () => {},
      syncApiSecretReentry: () => {},
      buildTlsValidationSuggestion: (validation) => String(validation?.detail || ""),
      defaultTlsTrustDetail: () => "Synthetic bundle not validated.",
      setBanner: (message, tone) => banners.push({ message, tone }),
    }
  );
  return { ...tested, elements, state, requests, banners, read };
}

function loadBravo(harness) {
  harness.state.setupEditorGeneration += 1;
  harness.state.loadedSystemId = "bravo";
  harness.elements.setupSystemId.value = "bravo";
  harness.elements.setupSystemLabel.value = "Bravo";
  harness.elements.setupTruenasHost.value = BRAVO_HOST;
  harness.elements.setupTlsServerName.value = "bravo.example.test";
  harness.elements.setupTlsCaBundlePath.value = "/synthetic/tls/bravo-existing.pem";
  harness.elements.setupVerifySsl.checked = false;
  harness.elements.setupTlsImportResult.textContent = "Bravo has no pending import.";
  harness.elements.setupTlsImportCaButton.disabled = false;
  harness.state.tlsTrustStatus = { level: "untrusted", label: "Untrusted", detail: "Bravo not checked." };
  harness.state.lastTlsValidation = {
    key: JSON.stringify([BRAVO_HOST, "/synthetic/tls/bravo-existing.pem", "bravo.example.test"]),
    validation: { validated: false, host: BRAVO_HOST },
  };
}

async function requestStarted(harness) {
  for (let turn = 0; turn < 5 && harness.requests.length === 0; turn += 1) {
    await Promise.resolve();
  }
  assert.equal(harness.requests.length, 1, "the synthetic import request should reach the deferred response");
}

test("switching editors during file reading retires the import before dispatch", async () => {
  const harness = importHarness({ deferredRead: true });
  const pending = harness.importTlsBundle();
  loadBravo(harness);
  const bravoTrust = harness.state.tlsTrustStatus;
  const bravoValidation = harness.state.lastTlsValidation;

  harness.read.resolve("SYNTHETIC ALPHA CERTIFICATE");
  for (let turn = 0; turn < 5 && harness.requests.length === 0; turn += 1) {
    await Promise.resolve();
  }
  // The unfixed implementation dispatches after the stale file read. Resolve
  // that synthetic request so the regression fails at the ownership assertion,
  // rather than leaving the test pending.
  if (harness.requests[0]) harness.requests[0].resolve(ALPHA_REPLY);
  await pending;

  assert.equal(harness.requests.length, 0, "a file read owned by the previous editor must not dispatch");
  assert.equal(harness.elements.setupTlsCaBundlePath.value, "/synthetic/tls/bravo-existing.pem");
  assert.equal(harness.elements.setupVerifySsl.checked, false);
  assert.equal(harness.elements.setupTlsImportResult.textContent, "Bravo has no pending import.");
  assert.strictEqual(harness.state.tlsTrustStatus, bravoTrust);
  assert.strictEqual(harness.state.lastTlsValidation, bravoValidation);
  assert.deepEqual(harness.banners, []);
});

test("a stale import success cannot update another editor or its trust cache", async () => {
  const harness = importHarness();
  const pending = harness.importTlsBundle();
  await requestStarted(harness);
  assert.deepEqual(
    [harness.requests[0].body.host, harness.requests[0].body.system_id,
      harness.requests[0].body.bundle_name, harness.requests[0].body.tls_server_name],
    [ALPHA_HOST, "alpha", "alpha", "alpha.example.test"]
  );

  loadBravo(harness);
  const bravoTrust = harness.state.tlsTrustStatus;
  const bravoValidation = harness.state.lastTlsValidation;
  harness.requests[0].resolve(ALPHA_REPLY);
  await pending;

  assert.equal(harness.elements.setupTlsCaBundlePath.value, "/synthetic/tls/bravo-existing.pem");
  assert.equal(harness.elements.setupVerifySsl.checked, false);
  assert.equal(harness.elements.setupTlsImportResult.textContent, "Bravo has no pending import.");
  assert.strictEqual(harness.state.tlsTrustStatus, bravoTrust);
  assert.strictEqual(harness.state.lastTlsValidation, bravoValidation);
  assert.deepEqual(harness.banners, []);
});

test("a completed import for an earlier draft tells the current editor to reload", async () => {
  const harness = importHarness();
  const pending = harness.importTlsBundle();
  await requestStarted(harness);
  const trustBefore = harness.state.tlsTrustStatus;
  harness.state.setupDraftRevision += 1;

  harness.requests[0].resolve(ALPHA_REPLY);
  await pending;

  assert.equal(harness.elements.setupTlsCaBundlePath.value, "");
  assert.equal(harness.elements.setupVerifySsl.checked, false);
  assert.match(harness.elements.setupTlsImportResult.textContent, /finished for an earlier draft/i);
  assert.match(harness.elements.setupTlsImportResult.textContent, /reload/i);
  assert.strictEqual(harness.state.tlsTrustStatus, trustBefore);
  assert.equal(harness.state.lastTlsValidation, null);
  assert.deepEqual(harness.banners, [{
    message: "Certificate import finished for an earlier draft. Reload this system before using the imported bundle.",
    tone: "info",
  }]);
});

test("a stale import failure cannot replace another editor's status", async () => {
  const harness = importHarness();
  const pending = harness.importTlsBundle();
  await requestStarted(harness);
  loadBravo(harness);
  const bravoTrust = harness.state.tlsTrustStatus;
  const bravoValidation = harness.state.lastTlsValidation;

  harness.requests[0].reject(new Error("synthetic alpha import failure"));
  await pending;

  assert.equal(harness.elements.setupTlsImportResult.textContent, "Bravo has no pending import.");
  assert.strictEqual(harness.state.tlsTrustStatus, bravoTrust);
  assert.strictEqual(harness.state.lastTlsValidation, bravoValidation);
  assert.deepEqual(harness.banners, []);
});

test("changing only the TLS server name retires a pending import", async () => {
  const harness = importHarness();
  const pending = harness.importTlsBundle();
  await requestStarted(harness);
  const trustBefore = harness.state.tlsTrustStatus;
  harness.elements.setupTlsServerName.value = "other-name.example.test";
  harness.elements.setupTlsImportResult.textContent = "Current server-name draft.";

  harness.requests[0].resolve(ALPHA_REPLY);
  await pending;

  assert.equal(harness.elements.setupTlsCaBundlePath.value, "");
  assert.equal(harness.elements.setupVerifySsl.checked, false);
  assert.equal(harness.elements.setupTlsImportResult.textContent, "Current server-name draft.");
  assert.strictEqual(harness.state.tlsTrustStatus, trustBefore);
  assert.equal(harness.state.lastTlsValidation, null);
  assert.deepEqual(harness.banners, []);
});

test("returning to the original target does not revive an earlier import", async () => {
  const harness = importHarness();
  const pending = harness.importTlsBundle();
  await requestStarted(harness);
  loadBravo(harness);
  harness.state.setupEditorGeneration += 1;
  harness.state.loadedSystemId = "alpha";
  harness.elements.setupSystemId.value = "alpha";
  harness.elements.setupSystemLabel.value = "Alpha";
  harness.elements.setupTruenasHost.value = ALPHA_HOST;
  harness.elements.setupTlsServerName.value = "alpha.example.test";
  harness.elements.setupTlsCaBundlePath.value = "/synthetic/tls/alpha-current.pem";
  harness.elements.setupTlsImportResult.textContent = "Current alpha editor.";

  harness.requests[0].resolve(ALPHA_REPLY);
  await pending;

  assert.equal(harness.elements.setupTlsCaBundlePath.value, "/synthetic/tls/alpha-current.pem");
  assert.equal(harness.elements.setupVerifySsl.checked, false);
  assert.equal(harness.elements.setupTlsImportResult.textContent, "Current alpha editor.");
  assert.deepEqual(harness.banners, []);
});

test("an older import cannot release the button held by a newer import", async () => {
  const harness = importHarness();
  const first = harness.importTlsBundle();
  await requestStarted(harness);
  loadBravo(harness);
  const second = harness.importTlsBundle();
  for (let turn = 0; turn < 5 && harness.requests.length < 2; turn += 1) await Promise.resolve();
  assert.equal(harness.requests.length, 2);
  assert.equal(harness.elements.setupTlsImportCaButton.disabled, true);

  harness.requests[0].resolve(ALPHA_REPLY);
  await first;
  assert.match(harness.elements.setupTlsImportResult.textContent, /^Importing /);
  assert.equal(harness.elements.setupTlsImportCaButton.disabled, true, "the newer import still owns the button");

  harness.requests[1].resolve({
    ...ALPHA_REPLY,
    bundle_path: "/synthetic/tls/bravo.pem",
    validation: { ...ALPHA_REPLY.validation, host: BRAVO_HOST, bundle_path: "/synthetic/tls/bravo.pem" },
  });
  await second;
  assert.equal(harness.elements.setupTlsImportCaButton.disabled, false);
});

test("the current import still applies and records trust for its captured target", async () => {
  const harness = importHarness();
  const pending = harness.importTlsBundle();
  await requestStarted(harness);
  harness.requests[0].resolve(ALPHA_REPLY);
  await pending;

  assert.equal(harness.elements.setupTlsCaBundlePath.value, ALPHA_REPLY.bundle_path);
  assert.equal(harness.elements.setupVerifySsl.checked, true);
  assert.equal(harness.state.tlsTrustStatus.level, "trusted");
  assert.deepEqual(
    JSON.parse(harness.state.lastTlsValidation.key),
    [ALPHA_HOST, ALPHA_REPLY.bundle_path, "alpha.example.test"]
  );
  assert.equal(harness.state.lastTlsValidation.validation, ALPHA_REPLY.validation);
  assert.equal(harness.banners.at(-1).tone, "success");
  assert.equal(harness.elements.setupTlsImportCaButton.disabled, false);
});

test("retiring editor controls releases an import held by the previous editor", () => {
  const elements = { setupTlsImportCaButton: new FakeElement() };
  elements.setupTlsImportCaButton.disabled = true;
  const { retireAdminEditorControls } = load(["retireAdminEditorControls"], {
    elements,
    state: { sessionStopped: false, systems: [] },
    currentSetupPlatform: () => "core",
    bootstrapEnabledForSession: () => false,
    getProfileById: () => null,
    profileReferenceCount: () => 0,
  });

  retireAdminEditorControls();

  assert.equal(elements.setupTlsImportCaButton.disabled, false);
});
