"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const STYLE_PATH = path.resolve(__dirname, "../../admin_service/static/admin.css");
const TEMPLATE_PATH = path.resolve(__dirname, "../../admin_service/templates/base.html");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");
const STYLES = fs.readFileSync(STYLE_PATH, "utf8");
const TEMPLATE = fs.readFileSync(TEMPLATE_PATH, "utf8");

function sourceBetween(startMarker, endMarker) {
  const start = SOURCE.indexOf(startMarker);
  assert.notEqual(start, -1, `missing start marker: ${startMarker}`);
  const end = SOURCE.indexOf(endMarker, start);
  assert.notEqual(end, -1, `missing end marker: ${endMarker}`);
  return SOURCE.slice(start, end);
}

function loadFunctions(snippets, names, bindings = {}) {
  const context = vm.createContext({ console, ...bindings });
  vm.runInContext(
    `${snippets.join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.__tested;
}

function sparseElements(values = {}) {
  return new Proxy(values, { get: (target, property) => target[property] ?? null });
}

const collectSetupSource = sourceBetween(
  "  function collectSetupPayload",
  "\n  async function discoverQuantastorHaNodes"
);

function collectSetupPayload({ sshUser = "", bmcEnabled = false, bmcHost = "" } = {}) {
  const elements = sparseElements({
    setupPlatform: { value: "core" },
    setupSshEnabled: { checked: true },
    setupSshHost: { value: "host.example.test" },
    setupSshKeyMode: { value: "none" },
    setupSshUser: { value: sshUser },
    setupSystemId: { value: "system-a" },
    setupTruenasHost: { value: "https://api.example.test" },
    setupSshPort: { value: "22" },
    setupSshStrictHostKey: { checked: true },
    setupBmcEnabled: { checked: bmcEnabled },
    setupBmcHost: { value: bmcHost },
    setupBmcVerifySsl: { checked: true },
    setupBmcTimeoutSeconds: { value: "15" },
  });
  const state = { loadedSystemId: null, storageViews: [] };
  const { collectSetupPayload: collect } = loadFunctions(
    [collectSetupSource],
    ["collectSetupPayload"],
    {
      collectSecretField: () => null,
      collectSshCommandUpdate: () => ({
        ssh_commands: [],
        ssh_commands_action: "default",
        ssh_commands_source_system_id: null,
      }),
      collectTlsServerName: () => null,
      currentQuantastorHaNodes: () => [],
      currentSetupPlatform: () => "core",
      elements,
      getSystemById: () => null,
      isEditingLoadedSystem: () => false,
      normalizeConnectionHost: (value) => String(value || "").trim(),
      normalizeKeyMode: (value) => value,
      platformSupportsSavedSudo: () => true,
      recommendedSshUserForPlatform: () => "jbodmap",
      setupPlatformUsesBmcOnlyHost: () => false,
      setupPlatformUsesSshOnlyHost: () => false,
      state,
    }
  );
  return collect();
}

test("cleared SSH user remains intentionally blank in the setup payload", () => {
  const payload = collectSetupPayload({ sshUser: "" });
  assert.equal(payload.ssh_user, null);
});

test("setup payload does not expose a caller-controlled known-hosts path", () => {
  const payload = collectSetupPayload();

  assert.equal(Object.hasOwn(payload, "ssh_known_hosts_path"), false);
});

test("bootstrap rejects a deliberately blank service user", () => {
  const collectBootstrapSource = sourceBetween(
    "  function collectBootstrapPayload()",
    "\n  function collectEsxiHostPrepInstallPayload"
  );
  const elements = sparseElements({
    setupBootstrapHost: { value: "host.example.test" },
    setupBootstrapUser: { value: "root" },
    setupBootstrapInstallSudo: { checked: true },
  });
  const { collectBootstrapPayload } = loadFunctions(
    [collectBootstrapSource],
    ["collectBootstrapPayload"],
    {
      bootstrapEnabledForSession: () => true,
      collectBootstrapSudoCommands: () => [],
      collectSetupPayload: () => ({
        platform: "core",
        ssh_enabled: true,
        ssh_host: "host.example.test",
        ssh_port: 22,
        ssh_user: null,
        ssh_strict_host_key_checking: true,
      }),
      elements,
      normalizeConnectionHost: (value) => String(value || "").trim(),
      platformSupportsBootstrap: () => true,
      resolveBootstrapServiceKey: () => ({
        service_key_name: "id_truenas",
        service_key_path: null,
        service_public_key: null,
      }),
      suggestedConnectionHost: () => "host.example.test",
    }
  );

  assert.throws(() => collectBootstrapPayload(), /SSH user.*required/i);
});

test("disabled BMC access does not persist a stale host", () => {
  const payload = collectSetupPayload({ bmcEnabled: false, bmcHost: "https://api.example.test" });
  assert.equal(payload.bmc_host, null);
});

test("enabling BMC access does not copy the API host into an empty field", () => {
  const syncBmcSource = sourceBetween(
    "  function syncBmcFields()",
    "\n  function maybeLoadRecommendedCommands"
  );
  const elements = sparseElements({
    setupBmcEnabled: { checked: true, disabled: false },
    setupBmcHost: { value: "" },
    setupBmcHelp: { textContent: "" },
  });
  const { syncBmcFields } = loadFunctions([syncBmcSource], ["syncBmcFields"], {
    currentSetupPlatform: () => "core",
    document: { querySelectorAll: () => [] },
    elements,
    suggestedConnectionHost: () => "https://api.example.test",
  });

  syncBmcFields();

  assert.equal(elements.setupBmcHost.value, "");
});

test("manual SSH-user edits suppress automatic recommendation reloads", () => {
  const maybeLoadSource = sourceBetween(
    "  function maybeLoadRecommendedSshUser",
    "\n  function collectSetupCommands"
  );
  const elements = sparseElements({
    setupPlatform: { value: "core" },
    setupSshUser: { value: "" },
  });
  const state = { sshUserAutoPlatform: null, sshUserEdited: true };
  let recommendationLoads = 0;
  const { maybeLoadRecommendedSshUser } = loadFunctions(
    [maybeLoadSource],
    ["maybeLoadRecommendedSshUser"],
    {
      elements,
      recommendedSshUserForPlatform: () => "jbodmap",
      setRecommendedSshUser: () => {
        recommendationLoads += 1;
        elements.setupSshUser.value = "jbodmap";
      },
      state,
    }
  );

  maybeLoadRecommendedSshUser();

  assert.equal(recommendationLoads, 0);
  assert.equal(elements.setupSshUser.value, "");
});

test("profile catalog cards are native pressed buttons", () => {
  const renderSource = sourceBetween(
    "  function renderProfileCatalog()",
    "\n  function slugify"
  );
  const elements = sparseElements({
    profileCatalog: { innerHTML: "" },
    profileCatalogCount: { textContent: "" },
  });
  const profile = {
    id: "profile-a",
    label: "Profile A",
    summary: "Synthetic profile.",
    rows: 1,
    columns: 1,
    slot_count: 1,
    source: "built-in",
    reference_count: 0,
  };
  const state = { profiles: [profile] };
  const { renderProfileCatalog } = loadFunctions([renderSource], ["renderProfileCatalog"], {
    elements,
    escapeHtml: (value) => String(value),
    previewProfile: () => profile,
    state,
  });

  renderProfileCatalog();

  assert.match(elements.profileCatalog.innerHTML, /<button\b/);
  assert.match(elements.profileCatalog.innerHTML, /type="button"/);
  assert.match(elements.profileCatalog.innerHTML, /aria-pressed="true"/);
  assert.doesNotMatch(elements.profileCatalog.innerHTML, /<article\b/);
});

test("admin stylesheet has one hidden utility and visible keyboard focus", () => {
  assert.equal((STYLES.match(/\.hidden\s*\{/g) || []).length, 1);
  assert.match(STYLES, /:focus-visible/);
  assert.match(STYLES, /\.profile-card:focus-visible/);
  assert.doesNotMatch(STYLES, /\.runtime-card-status\.is-running/);
  assert.doesNotMatch(STYLES, /\.runtime-card-status\.is-stopped/);
});

test("admin page uses an inline favicon without a failing network request", () => {
  assert.match(TEMPLATE, /<link\s+rel="icon"\s+href="data:[^"]*">/);
});

test("countdown ticks reuse the existing release-note link", () => {
  const updateSource = sourceBetween(
    "  function updateAdminMeta()",
    "\n  function normalizeAdminView"
  );
  const releaseNote = {
    className: "",
    children: [],
    get firstElementChild() {
      return this.children[0] || null;
    },
    set textContent(value) {
      this.text = value;
      this.children = [];
    },
    replaceChildren(...children) {
      this.children = children;
    },
  };
  const elements = sparseElements({ releaseNote });
  const state = {
    releaseStatus: {
      latest_url: "https://example.test/releases/v1",
      status: "current",
      summary: "Current release",
    },
  };
  const document = {
    createElement: (tagName) => ({ tagName: tagName.toUpperCase() }),
  };
  const { updateAdminMeta } = loadFunctions([updateSource], ["updateAdminMeta"], {
    document,
    elements,
    formatCountdown: () => "1m 00s",
    formatLocalTimestamp: (value) => String(value),
    safeHttpUrl: (value) => value,
    state,
    URL,
    window: { location: { href: "http://127.0.0.1:8082/" } },
  });

  updateAdminMeta();
  const firstLink = releaseNote.firstElementChild;
  updateAdminMeta();

  assert.ok(firstLink);
  assert.equal(releaseNote.firstElementChild, firstLink);
});

test("missing admin expiry renders No auto-stop", () => {
  const formatSource = sourceBetween(
    "  function formatCountdown()",
    "\n  function startCountdownTimer"
  );
  const { formatCountdown } = loadFunctions([formatSource], ["formatCountdown"], {
    state: { admin: { expires_at: null } },
  });

  assert.equal(formatCountdown(), "No auto-stop");
});
