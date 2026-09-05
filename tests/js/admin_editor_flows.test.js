"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");
const PRESERVE_SECRET_SENTINEL = "__TRUENAS_JBOD_KEEP_EXISTING_VALUE__";

function sourceBetween(startMarker, endMarker) {
  const start = SOURCE.indexOf(startMarker);
  assert.notEqual(start, -1, `missing start marker: ${startMarker}`);
  const end = SOURCE.indexOf(endMarker, start);
  assert.notEqual(end, -1, `missing end marker: ${endMarker}`);
  return SOURCE.slice(start, end);
}

function loadFunctions(snippets, names, bindings = {}) {
  const context = vm.createContext({
    URLSearchParams,
    console,
    setTimeout,
    clearTimeout,
    ...bindings,
  });
  vm.runInContext(
    `${snippets.join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.__tested;
}

class FakeField {
  constructor({ kind = "input", value = "" } = {}) {
    this.kind = kind;
    this.value = value;
    this.checked = false;
    this.listeners = new Map();
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  matches(selector) {
    if (selector === "select") {
      return this.kind === "select";
    }
    if (selector === "input[type='checkbox'], select") {
      return this.kind === "checkbox" || this.kind === "select";
    }
    return false;
  }

  dispatch(name) {
    for (const listener of this.listeners.get(name) || []) {
      listener({ target: this });
    }
  }
}

function sparseElements(values = {}) {
  return new Proxy(
    { adminViewButtons: [], adminViewSwitches: [], ...values },
    { get: (target, property) => target[property] ?? null }
  );
}

const bindEventsSource = sourceBetween(
  "  function bindEvents() {",
  "\n  if (elements.backupExportStopToggle)"
);

test("storage-view text inputs keep the user's draft until change", () => {
  const viewId = new FakeField({ value: "Front-Bays" });
  const serials = new FakeField({ value: "disk-a,\n" });
  const slotLabels = new FakeField({ value: "0" });
  let saves = 0;
  const elements = sparseElements({
    setupStorageViewId: viewId,
    setupStorageViewSerials: serials,
    setupStorageViewSlotLabels: slotLabels,
  });
  const document = { querySelectorAll: () => [] };
  const window = { addEventListener: () => {}, location: { search: "" } };
  const saveStorageViewEditorToState = () => {
    saves += 1;
    viewId.value = viewId.value.toLowerCase();
    serials.value = "disk-a";
    slotLabels.value = "";
  };
  const { bindEvents } = loadFunctions([bindEventsSource], ["bindEvents"], {
    document,
    elements,
    saveStorageViewEditorToState,
    state: { storageViewTemplates: [], sshKeys: [] },
    window,
  });
  bindEvents();

  viewId.dispatch("input");
  serials.dispatch("input");
  slotLabels.dispatch("input");

  assert.equal(saves, 0);
  assert.equal(viewId.value, "Front-Bays");
  assert.equal(serials.value, "disk-a,\n");
  assert.equal(slotLabels.value, "0");

  serials.dispatch("change");
  assert.equal(saves, 1);
});

test("Quantastor HA label drafts are not trimmed or rerendered on input", () => {
  const label = new FakeField({ value: "Node " });
  let syncs = 0;
  let renders = 0;
  const elements = sparseElements();
  const document = { querySelectorAll: () => [label] };
  const window = { addEventListener: () => {}, location: { search: "" } };
  const syncHaNodesFromInputs = () => {
    syncs += 1;
    label.value = label.value.trim();
  };
  const renderQuantastorHaSection = () => {
    renders += 1;
  };
  const { bindEvents } = loadFunctions([bindEventsSource], ["bindEvents"], {
    document,
    elements,
    renderQuantastorHaSection,
    state: { storageViewTemplates: [], sshKeys: [] },
    syncHaNodesFromInputs,
    window,
  });
  bindEvents();

  label.dispatch("input");
  assert.equal(syncs, 0);
  assert.equal(renders, 0);
  assert.equal(label.value, "Node ");

  label.dispatch("change");
  assert.equal(syncs, 1);
  assert.equal(renders, 1);
});

test("Quantastor HA normalization preserves distinct label-only rows", () => {
  const normalizeHaSource = sourceBetween(
    "  function normalizeHaNode(rawNode) {",
    "\n  function haNodeFieldValue"
  );
  const { normalizeHaNodes } = loadFunctions([normalizeHaSource], ["normalizeHaNodes"]);

  const nodes = normalizeHaNodes([
    { label: "Node Alpha" },
    { label: "Node Beta" },
  ]);

  assert.deepEqual(JSON.parse(JSON.stringify(nodes)), [
    { system_id: "", label: "Node Alpha", host: "" },
    { system_id: "", label: "Node Beta", host: "" },
  ]);
});

test("storage-view normalization preserves an explicitly empty label", () => {
  const helpers = sourceBetween("  function slugify", "\n  function parseSlotLabelsText");
  const normalizeStorageViewSource = sourceBetween(
    "  function normalizeStorageView(rawView",
    "\n  function normalizeStorageViews"
  );
  const template = {
    id: "manual-4",
    kind: "manual",
    default_id: "manual-view",
    default_label: "Manual View",
  };
  const state = { storageViewTemplates: [template] };
  const getStorageViewTemplate = (templateId) =>
    state.storageViewTemplates.find((item) => item.id === templateId) || null;
  const { normalizeStorageView } = loadFunctions(
    [helpers, normalizeStorageViewSource],
    ["normalizeStorageView"],
    { getStorageViewTemplate, state }
  );

  const normalized = normalizeStorageView({
    id: "manual-view",
    label: "",
    template_id: "manual-4",
  });

  assert.equal(normalized.label, "");
});

test("setup collection preserves saved SSH secrets and configured timeout", () => {
  const collectSecretSource = sourceBetween(
    "  function collectSecretField",
    "\n  function renderHistoryMaintenance"
  );
  const collectSetupSource = sourceBetween(
    "  function collectSetupPayload",
    "\n  async function discoverQuantastorHaNodes"
  );
  const savedSystem = {
    id: "saved-esxi",
    ssh_password_configured: true,
    ssh_timeout_seconds: 240,
  };
  const elements = sparseElements({
    setupPlatform: { value: "esxi" },
    setupSshEnabled: { checked: true },
    setupSshHost: { value: "192.0.2.25" },
    setupSshKeyMode: { value: "none" },
    setupSshUser: { value: "root" },
    setupSystemId: { value: "saved-esxi" },
    setupTruenasHost: { value: "" },
    setupSshPort: { value: "22" },
    setupSshPassword: { value: "" },
    setupSshKnownHosts: { value: "/app/data/known_hosts" },
    setupSshStrictHostKey: { checked: true },
  });
  const state = { loadedSystemId: "saved-esxi", storageViews: [] };
  const bindings = {
    PRESERVE_SECRET_SENTINEL,
    collectSshCommandUpdate: () => ({
      ssh_commands: [],
      ssh_commands_action: "preserve",
      ssh_commands_source_system_id: "saved-esxi",
    }),
    collectTlsServerName: () => null,
    currentQuantastorHaNodes: () => [],
    currentSetupPlatform: () => "esxi",
    elements,
    getSystemById: () => savedSystem,
    isEditingLoadedSystem: () => true,
    normalizeConnectionHost: (value) => String(value || "").trim(),
    normalizeKeyMode: (value) => value,
    platformSupportsSavedSudo: () => false,
    recommendedSshUserForPlatform: () => "root",
    savedSecretConfigured: (system, configuredKey, legacyKey) =>
      Boolean(system && (system[configuredKey] || system[legacyKey])),
    setupPlatformUsesBmcOnlyHost: () => false,
    setupPlatformUsesSshOnlyHost: () => true,
    state,
  };
  const { collectSetupPayload } = loadFunctions(
    [collectSecretSource, collectSetupSource],
    ["collectSetupPayload"],
    bindings
  );

  const payload = collectSetupPayload({ preserveRedactedSecrets: true });

  assert.equal(payload.ssh_password, PRESERVE_SECRET_SENTINEL);
  assert.equal(payload.ssh_timeout_seconds, 240);
  assert.equal(payload.ssh_commands_action, "preserve");
  assert.equal(payload.ssh_commands_source_system_id, "saved-esxi");
});

test("Quantastor discovery uses canonical preserved secrets and SSH timeout", async () => {
  const discoverSource = sourceBetween(
    "  async function discoverQuantastorHaNodes",
    "\n  function resolveBootstrapServiceKey"
  );
  let collectOptions;
  let requestBody;
  const setupPayload = {
    system_id: "saved-quantastor",
    truenas_host: "https://192.0.2.30",
    api_user: "admin",
    api_password: PRESERVE_SECRET_SENTINEL,
    verify_ssl: true,
    tls_ca_bundle_path: null,
    tls_server_name: null,
    ssh_enabled: true,
    ssh_host: "192.0.2.31",
    ssh_port: 22,
    ssh_user: "svc",
    ssh_key_path: null,
    ssh_password: PRESERVE_SECRET_SENTINEL,
    ssh_known_hosts_path: "/app/data/known_hosts",
    ssh_strict_host_key_checking: true,
    ssh_timeout_seconds: 45,
    ha_nodes: [],
  };
  const elements = sparseElements({
    setupHaEnabled: { checked: true },
    setupTruenasHost: { value: setupPayload.truenas_host },
    setupApiUser: { value: setupPayload.api_user },
    setupApiPassword: { value: "" },
    setupVerifySsl: { checked: true },
  });
  const state = { haNodes: [], haNodesLoading: false };
  const { discoverQuantastorHaNodes } = loadFunctions(
    [discoverSource],
    ["discoverQuantastorHaNodes"],
    {
      collectSetupPayload: (options) => {
        collectOptions = options;
        return setupPayload;
      },
      collectTlsServerName: () => null,
      currentSetupPlatform: () => "quantastor",
      elements,
      fetchJson: async (_url, options) => {
        requestBody = JSON.parse(options.body);
        return { nodes: [], host_discovery: {} };
      },
      normalizeHaNodes: (nodes) => nodes,
      renderQuantastorHaSection: () => {},
      renderStorageViews: () => {},
      setBanner: () => {},
      state,
      syncHaNodesFromInputs: () => {},
    }
  );

  await discoverQuantastorHaNodes();

  assert.deepEqual(JSON.parse(JSON.stringify(collectOptions)), { preserveRedactedSecrets: true });
  assert.equal(requestBody.system_id, "saved-quantastor");
  assert.equal(requestBody.api_password, PRESERVE_SECRET_SENTINEL);
  assert.equal(requestBody.ssh_password, PRESERVE_SECRET_SENTINEL);
  assert.equal(requestBody.ssh_timeout_seconds, 45);
});

test("failed ESXi install refreshes packages before restoring controls", async () => {
  const installSource = sourceBetween(
    "  async function installEsxiHostPrepPackage() {",
    "\n  async function inspectTlsCertificate"
  );
  const installButton = { disabled: false };
  const state = {
    esxiHostPrep: {
      staged_packages: [{ token: "dead-token", filename: "vendor.vib" }],
    },
  };
  let refreshCalls = 0;
  const { installEsxiHostPrepPackage } = loadFunctions(
    [installSource],
    ["installEsxiHostPrepPackage"],
    {
      collectEsxiHostPrepInstallPayload: () => ({
        host: "192.0.2.25",
        upload_token: "dead-token",
      }),
      currentStagedEsxiHostPrepPackages: () => state.esxiHostPrep.staged_packages,
      elements: sparseElements({
        setupEsxiHostPrepInstallButton: installButton,
        setupEsxiHostPrepResult: { textContent: "" },
      }),
      fetchJson: async () => {
        throw new Error("synthetic install failure");
      },
      getSelectedEsxiHostPrepPackage: () => ({
        token: "dead-token",
        filename: "vendor.vib",
      }),
      refreshState: async () => {
        refreshCalls += 1;
        state.esxiHostPrep.staged_packages = [];
      },
      renderEsxiHostPrepPackages: () => {},
      setBanner: () => {},
      state,
      syncEsxiHostPrepFields: () => {
        installButton.disabled = state.esxiHostPrep.staged_packages.length === 0;
      },
    }
  );

  await installEsxiHostPrepPackage();

  assert.equal(refreshCalls, 1);
  assert.deepEqual(state.esxiHostPrep.staged_packages, []);
  assert.equal(installButton.disabled, true);
});

test("ESXi host prep uses canonical preserved secrets and configured timeout", () => {
  const collectEsxiSource = sourceBetween(
    "  function collectEsxiHostPrepInstallPayload",
    "\n  async function readJsonResponse"
  );
  let collectOptions;
  const setupPayload = {
    system_id: "saved-esxi",
    platform: "esxi",
    ssh_enabled: true,
    ssh_host: "192.0.2.25",
    ssh_port: 22,
    ssh_user: "root",
    ssh_key_path: null,
    ssh_password: PRESERVE_SECRET_SENTINEL,
    ssh_known_hosts_path: "/app/data/known_hosts",
    ssh_strict_host_key_checking: true,
    ssh_timeout_seconds: 240,
  };
  const { collectEsxiHostPrepInstallPayload } = loadFunctions(
    [collectEsxiSource],
    ["collectEsxiHostPrepInstallPayload"],
    {
      collectSetupPayload: (options) => {
        collectOptions = options;
        return setupPayload;
      },
      getSelectedEsxiHostPrepPackage: () => ({ token: "package-token" }),
      platformSupportsEsxiHostPrep: () => true,
    }
  );

  const payload = collectEsxiHostPrepInstallPayload();

  assert.deepEqual(JSON.parse(JSON.stringify(collectOptions)), { preserveRedactedSecrets: true });
  assert.equal(payload.system_id, "saved-esxi");
  assert.equal(payload.password, PRESERVE_SECRET_SENTINEL);
  assert.equal(payload.timeout_seconds, 240);
});
