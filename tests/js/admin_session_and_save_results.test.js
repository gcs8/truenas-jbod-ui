"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const REPO_ROOT = path.resolve(__dirname, "../..");
const SCRIPT_PATH = path.join(REPO_ROOT, "admin_service/static/admin.js");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");
const TEMPLATE = fs.readFileSync(path.join(REPO_ROOT, "admin_service/templates/index.html"), "utf8");
const ROUTES = fs.readFileSync(path.join(REPO_ROOT, "admin_service/routes.py"), "utf8");
const MAIN = fs.readFileSync(path.join(REPO_ROOT, "admin_service/main.py"), "utf8");

function functionSource(name) {
  const start = SOURCE.search(new RegExp(`^  (?:async )?function ${name}\\(`, "m"));
  assert.notEqual(start, -1, `function ${name} must exist`);
  const next = SOURCE.slice(start + 3).search(/^  (?:async )?function |^  const [A-Z_]+ = /m);
  return SOURCE.slice(start, next < 0 ? undefined : start + 3 + next);
}

function constantSource(name) {
  const start = SOURCE.search(new RegExp(`^  const ${name} = `, "m"));
  assert.notEqual(start, -1, `constant ${name} must exist`);
  const end = SOURCE.indexOf("\n", start);
  return SOURCE.slice(start, end);
}

function loadFunctions(names, bindings = {}, constants = []) {
  const context = vm.createContext({
    console,
    URL,
    URLSearchParams,
    Date,
    setTimeout,
    clearTimeout,
    ...bindings,
  });
  const snippets = [...constants.map(constantSource), ...names.map(functionSource)];
  vm.runInContext(
    `${snippets.join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.__tested;
}

class FakeClassList {
  constructor(initial = []) {
    this.names = new Set(initial);
  }

  add(...names) {
    names.forEach((name) => this.names.add(name));
  }

  remove(...names) {
    names.forEach((name) => this.names.delete(name));
  }

  toggle(name, force) {
    const enabled = force === undefined ? !this.names.has(name) : Boolean(force);
    if (enabled) {
      this.names.add(name);
    } else {
      this.names.delete(name);
    }
    return enabled;
  }

  contains(name) {
    return this.names.has(name);
  }
}

class FakeElement {
  constructor(props = {}) {
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.textContent = "";
    this.title = "";
    this.href = "";
    this.classList = new FakeClassList(props.classes || []);
    this.children = [];
    this.listeners = new Map();
    Object.assign(this, props);
    delete this.classes;
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  click() {
    (this.listeners.get("click") || []).forEach((listener) => listener({ target: this }));
  }

  append(...nodes) {
    nodes.forEach((node) => {
      if (typeof node === "string") {
        this.textContent += node;
      } else {
        this.children.push(node);
      }
    });
  }

  replaceChildren(...nodes) {
    this.children = nodes.filter((node) => typeof node !== "string");
    this.textContent = nodes.map((node) => (typeof node === "string" ? node : node.textContent)).join("");
  }

  get text() {
    return `${this.textContent}${this.children.map((child) => `[${child.textContent}]`).join("")}`;
  }
}

function fakeDocument(buttons = []) {
  return {
    createElement: (tagName) => new FakeElement({ tagName: tagName.toUpperCase() }),
    querySelectorAll: (selector) => (selector === "button:not(.admin-view-button)" ? buttons : []),
  };
}

function sparseElements(values = {}) {
  return new Proxy(values, { get: (target, property) => target[property] ?? null });
}

const MAINTENANCE_FUNCTIONS = [
  "maintenanceKeys",
  "serviceName",
  "describeServices",
  "createServiceActionButton",
  "describeMaintenanceOutcome",
  "renderMaintenanceResult",
  "capitalize",
  "renderSaveResult",
];

const SESSION_FUNCTIONS = [
  "sessionRemainingMs",
  "formatClockTime",
  "formatCountdown",
  "describeAutoStopDuration",
  "markAdminStopped",
  "syncSessionBanner",
  "fetchOrReportStopped",
];

const SESSION_CONSTANTS = ["SESSION_WARNING_MS", "ADMIN_START_COMMAND"];

// Save results offer the restart in place.

test("a save response that needs a restart renders a Restart main UI button and updates the runtime card state", () => {
  const runtimeActions = [];
  let runtimeRenders = 0;
  const state = { runtime: { containers: [] } };
  const { renderSaveResult } = loadFunctions(MAINTENANCE_FUNCTIONS, {
    state,
    document: fakeDocument(),
    renderRuntimeCards: () => { runtimeRenders += 1; },
    runRuntimeAction: (key, action) => {
      runtimeActions.push([key, action]);
      return Promise.resolve(true);
    },
  });
  const result = new FakeElement();
  const runtime = { containers: [{ key: "ui", label: "Main UI", restart_required: true }] };

  renderSaveResult(result, "Saved. Restart the main UI to show the new system.", {
    detail: "Saved. Restart the main UI to show the new system.",
    restart_required: ["ui"],
    runtime,
  });

  assert.equal(result.textContent, "Saved. Restart the main UI to show the new system. ");
  assert.equal(result.children.length, 1);
  assert.equal(result.children[0].textContent, "Restart main UI now");
  assert.equal(state.runtime, runtime);
  assert.equal(runtimeRenders, 1);
  result.children[0].click();
  assert.deepEqual(runtimeActions, [["ui", "restart"]]);
});

test("a save response without restart_required renders plain text and leaves runtime state alone", () => {
  const state = { runtime: { containers: [{ key: "ui" }] } };
  const { renderSaveResult } = loadFunctions(MAINTENANCE_FUNCTIONS, {
    state,
    document: fakeDocument(),
    renderRuntimeCards: () => assert.fail("runtime cards must not re-render without a runtime payload"),
    runRuntimeAction: () => assert.fail("no restart should be offered"),
  });
  const result = new FakeElement();

  renderSaveResult(result, "Deleted custom profile Lab.", { detail: "Deleted custom profile Lab." });

  assert.equal(result.text, "Deleted custom profile Lab.");
  assert.equal(state.runtime.containers.length, 1);
});

test("every save, delete, and timing result goes through renderSaveResult and nothing user-facing says read UI", () => {
  for (const name of ["createSystem", "saveRuntimeBehaviorSettings", "deleteCustomProfile"]) {
    assert.match(functionSource(name), /renderSaveResult\(/, `${name} should render its result through renderSaveResult`);
  }
  assert.ok((SOURCE.match(/renderSaveResult\(/g) || []).length >= 6);
  for (const [label, text] of [["admin.js", SOURCE], ["index.html", TEMPLATE], ["routes.py", ROUTES]]) {
    assert.doesNotMatch(text, /read ui/i, `${label} must say main UI`);
  }
  assert.match(TEMPLATE, /Saved systems appear in the main UI after a restart\./);
  assert.match(ROUTES, /"restart_required": \["ui"\],\n\s+"detail": "Runtime behavior overrides saved\. Restart the main UI to apply them\."/);
});

// Auto-stop is announced before and after it happens.

test("countdown reads as one auto-stop line and Stays running without an expiry", () => {
  const expiresAt = new Date(Date.now() + 42 * 60 * 1000 + 10 * 1000).toISOString();
  const { formatCountdown } = loadFunctions(
    SESSION_FUNCTIONS,
    { state: { admin: { expires_at: expiresAt } }, elements: sparseElements(), document: fakeDocument() },
    SESSION_CONSTANTS
  );
  assert.match(formatCountdown(), /^Auto-stops in 42m (09|10)s \(.+\)$/);

  const { formatCountdown: noExpiry } = loadFunctions(
    SESSION_FUNCTIONS,
    { state: { admin: { expires_at: null } }, elements: sparseElements(), document: fakeDocument() },
    SESSION_CONSTANTS
  );
  assert.equal(noExpiry(), "Stays running");
  assert.equal((TEMPLATE.match(/class="hero-stat"/g) || []).length, 4);
  assert.doesNotMatch(TEMPLATE, /admin-started-at|admin-expires-at/);
  assert.match(TEMPLATE, /id="admin-session-banner"/);
});

test("five minutes before auto-stop a sticky warning banner asks the user to save", () => {
  const banner = new FakeElement({ classes: ["hidden"] });
  const state = { admin: { expires_at: new Date(Date.now() + 5 * 60 * 1000 - 1000).toISOString() }, sessionStopped: false };
  const { syncSessionBanner } = loadFunctions(
    SESSION_FUNCTIONS,
    { state, elements: sparseElements({ sessionBanner: banner }), document: fakeDocument() },
    SESSION_CONSTANTS
  );

  syncSessionBanner();

  assert.equal(banner.textContent, "This admin session stops in 5 minutes. Save your work.");
  assert.ok(banner.classList.contains("is-warning"));
  assert.ok(!banner.classList.contains("hidden"));
  assert.equal(state.sessionStopped, false);
});

test("at auto-stop the banner explains how to start admin again and every action button is disabled", () => {
  const banner = new FakeElement({ classes: ["hidden"] });
  const countdown = new FakeElement();
  const buttons = [new FakeElement(), new FakeElement()];
  const state = {
    admin: { expires_at: new Date(Date.now() - 1000).toISOString(), auto_stop_seconds: 3600 },
    sessionStopped: false,
    countdownTimerId: 7,
  };
  let cleared = null;
  const { syncSessionBanner } = loadFunctions(
    SESSION_FUNCTIONS,
    {
      state,
      elements: sparseElements({ sessionBanner: banner, countdown }),
      document: fakeDocument(buttons),
      window: { clearInterval: (id) => { cleared = id; } },
    },
    SESSION_CONSTANTS
  );

  syncSessionBanner();

  assert.equal(
    banner.textContent,
    "Admin has stopped (it stops itself after 1 hour). To start it again run docker compose --profile admin up -d enclosure-admin, then reload this page."
  );
  assert.equal(banner.children[0].tagName, "CODE");
  assert.ok(banner.classList.contains("is-error"));
  assert.ok(!banner.classList.contains("hidden"));
  assert.equal(countdown.textContent, "Stopped");
  assert.equal(state.sessionStopped, true);
  assert.equal(state.countdownTimerId, null);
  assert.equal(cleared, 7);
  assert.ok(buttons.every((button) => button.disabled));
});

test("a network failure after the expiry marks admin stopped; before the expiry it is just an error", async () => {
  const build = (expiresAt) => {
    const banner = new FakeElement({ classes: ["hidden"] });
    const state = { admin: { expires_at: expiresAt }, sessionStopped: false, countdownTimerId: null };
    const { fetchOrReportStopped } = loadFunctions(
      SESSION_FUNCTIONS,
      {
        state,
        elements: sparseElements({ sessionBanner: banner }),
        document: fakeDocument(),
        fetch: () => Promise.reject(new TypeError("Failed to fetch")),
      },
      SESSION_CONSTANTS
    );
    return { state, banner, fetchOrReportStopped };
  };

  const expired = build(new Date(Date.now() - 1000).toISOString());
  await assert.rejects(() => expired.fetchOrReportStopped("/api/admin/state"), TypeError);
  assert.equal(expired.state.sessionStopped, true);
  assert.match(expired.banner.textContent, /^Admin has stopped/);

  const live = build(new Date(Date.now() + 30 * 60 * 1000).toISOString());
  await assert.rejects(() => live.fetchOrReportStopped("/api/admin/state"), TypeError);
  assert.equal(live.state.sessionStopped, false);
  assert.ok(live.banner.classList.contains("hidden"));
  assert.match(functionSource("fetchJson"), /fetchOrReportStopped\(/);
});

// Debug bundle exports never pause production by default.

test("debug bundle export defaults to not pausing services in the template, the state payload, and the route", () => {
  assert.match(TEMPLATE, /<input id="debug-export-stop-toggle" type="checkbox">/);
  assert.doesNotMatch(TEMPLATE, /debug-export-stop-toggle" type="checkbox" checked/);
  assert.equal((TEMPLATE.match(/Pause the main UI and history while exporting \(usually not needed\)/g) || []).length, 2);
  assert.equal((TEMPLATE.match(/Start them again afterwards/g) || []).length, 3);
  assert.match(MAIN, /"debug_stop_services": False/);
  assert.match(ROUTES, /export_debug_bundle\([\s\S]*?stop_services: bool = Query\(default=False\)/);
  assert.match(SOURCE, /elements\.debugExportStopToggle\.checked = Boolean\(state\.backupDefaults\.debug_stop_services\)/);
});

test("export and import results name services by label and only mention pauses that happened", () => {
  const runtimeActions = [];
  const { describeMaintenanceOutcome, renderMaintenanceResult } = loadFunctions(MAINTENANCE_FUNCTIONS, {
    state: { runtime: { containers: [{ key: "ui", label: "Main UI" }, { key: "history", label: "History" }] } },
    document: fakeDocument(),
    renderRuntimeCards: () => {},
    runRuntimeAction: (key, action) => {
      runtimeActions.push([key, action]);
      return Promise.resolve(true);
    },
  });

  const untouched = new FakeElement();
  renderMaintenanceResult(untouched, "Backup saved as tar.zst", describeMaintenanceOutcome({ stopped: "none", restarted: "none", failures: "" }));
  assert.equal(untouched.text, "Backup saved as tar.zst.");

  const recovered = new FakeElement();
  renderMaintenanceResult(recovered, "Backup saved as tar.zst", describeMaintenanceOutcome({ stopped: "ui,history", restarted: "ui,history", failures: "" }));
  assert.equal(recovered.text, "Backup saved as tar.zst. The main UI and the history collector were paused and started again.");

  const failed = new FakeElement();
  renderMaintenanceResult(failed, "Backup saved as tar.zst", describeMaintenanceOutcome({ stopped: "ui,history", restarted: "history", failures: "ui" }));
  assert.equal(failed.text, "Backup saved as tar.zst, but the main UI did not start again. [Start main UI]");
  failed.children[0].click();
  assert.deepEqual(runtimeActions, [["ui", "start"]]);

  const stillPaused = new FakeElement();
  renderMaintenanceResult(stillPaused, "Imported backup.tar.zst", describeMaintenanceOutcome({ stopped: ["ui"], restarted: [], failures: "" }));
  assert.equal(stillPaused.text, "Imported backup.tar.zst, but the main UI is still paused. [Start main UI]");
  assert.doesNotMatch(SOURCE, /Stopped: \$\{stopped\}/);
});

// IPMI / BMC-only systems get their own wording.

const BOOTSTRAP_FUNCTIONS = [
  "syncBootstrapFields",
  "bootstrapEnabledForSession",
  "platformSupportsBootstrap",
  "setupPlatformUsesBmcOnlyHost",
  "currentSetupPlatform",
  "refreshSudoersPreview",
  "renderSudoersPreview",
  "collectSudoersPreviewPayload",
  "recommendedSshUserForPlatform",
  "collectBootstrapSudoCommandPayload",
  "collectBootstrapSudoCommands",
  "collectSetupCommands",
  "unchangedRedactedSshCommands",
  "normalizeCommandText",
];

function bootstrapElements(platform) {
  return {
    setupPlatform: new FakeElement({ value: platform }),
    setupSshEnabled: new FakeElement({ checked: true }),
    setupBootstrapEnabled: new FakeElement({ checked: true }),
    setupBootstrapResult: new FakeElement(),
    setupBootstrapInstallSudo: new FakeElement({ checked: true }),
    setupBootstrapFields: new FakeElement(),
    setupBootstrapSudoersPanel: new FakeElement(),
    setupBootstrapSudoersPreview: new FakeElement(),
    setupBootstrapSudoersName: new FakeElement(),
    setupBootstrapSudoersDetail: new FakeElement(),
    setupSshUser: new FakeElement({ value: "jbodmap" }),
    setupSshCommands: new FakeElement(),
  };
}

test("an IPMI / BMC-only system says no host login is needed and hides the permission preview", async () => {
  const elements = bootstrapElements("ipmi");
  const tested = loadFunctions(
    BOOTSTRAP_FUNCTIONS,
    { elements, document: { querySelectorAll: () => [] }, state: {}, fetchJson: async () => assert.fail("no preview request for BMC-only") },
    ["BMC_ONLY_BOOTSTRAP_NOTE"]
  );

  tested.syncBootstrapFields();
  await tested.refreshSudoersPreview();

  const note = "This system is managed through its BMC. No host login is needed.";
  assert.equal(elements.setupBootstrapResult.textContent, note);
  assert.equal(elements.setupBootstrapSudoersDetail.textContent, note);
  assert.doesNotMatch(elements.setupBootstrapSudoersPreview.textContent, /ESXi/);
  assert.ok(elements.setupBootstrapSudoersPanel.classList.contains("hidden"));
});

test("an ESXi system keeps the ESXi wording and its permission preview", async () => {
  const elements = bootstrapElements("esxi");
  const tested = loadFunctions(
    BOOTSTRAP_FUNCTIONS,
    { elements, document: { querySelectorAll: () => [] }, state: {}, fetchJson: async () => ({}) },
    ["BMC_ONLY_BOOTSTRAP_NOTE"]
  );

  tested.syncBootstrapFields();
  await tested.refreshSudoersPreview();

  assert.match(elements.setupBootstrapResult.textContent, /VMware ESXi/);
  assert.match(elements.setupBootstrapSudoersDetail.textContent, /VMware ESXi/);
  assert.ok(!elements.setupBootstrapSudoersPanel.classList.contains("hidden"));
});

test("collecting a bootstrap payload names the BMC for ipmi and ESXi for esxi", () => {
  const load = (platform) => loadFunctions(
    ["collectBootstrapPayload", "platformSupportsBootstrap", "setupPlatformUsesBmcOnlyHost", "currentSetupPlatform"],
    {
      elements: sparseElements({ setupPlatform: new FakeElement({ value: platform }) }),
      bootstrapEnabledForSession: () => true,
      collectSetupPayload: () => ({ platform, ssh_enabled: true }),
    },
    ["BMC_ONLY_BOOTSTRAP_NOTE"]
  ).collectBootstrapPayload;

  assert.throws(() => load("ipmi")(), /This system is managed through its BMC\. No host login is needed\./);
  assert.throws(() => load("esxi")(), /VMware ESXi does not use/);
});

// SSH key mode never points at a key that does not exist.

test("with no SSH keys the form defaults to creating a key and leaves the path blank", () => {
  const { defaultKeyMode } = loadFunctions(["defaultKeyMode"], { state: { sshKeys: [] } });
  assert.equal(defaultKeyMode(), "generate");
  const { defaultKeyMode: withKeys } = loadFunctions(["defaultKeyMode"], { state: { sshKeys: [{ name: "id_truenas" }] } });
  assert.equal(withKeys(), "reuse");

  const keyPath = new FakeElement({ value: "/run/ssh/id_truenas" });
  const { applySelectedKey } = loadFunctions(["applySelectedKey", "normalizeKeyMode", "getSshKeyByName"], {
    state: { sshKeys: [] },
    elements: sparseElements({
      setupSshKeyPath: keyPath,
      setupSshKeyMode: new FakeElement({ value: "reuse" }),
      setupSshExistingKey: new FakeElement({ value: "" }),
    }),
  });
  applySelectedKey();
  assert.equal(keyPath.value, "");

  const resetSource = functionSource("resetSetupForm");
  assert.match(resetSource, /elements\.setupSshKeyMode\.value = defaultKeyMode\(\)/);
  assert.match(resetSource, /elements\.setupSshKeyPath\.value = state\.sshKeys\.length \? "\/run\/ssh\/id_truenas" : ""/);
  assert.equal((SOURCE.match(/setupSshKeyMode\.value = defaultKeyMode\(\)/g) || []).length, 2);
});

test("saving with SSH enabled, reuse mode, and no key is refused before any request", async () => {
  const banners = [];
  let requests = 0;
  const createButton = new FakeElement();
  const { createSystem } = loadFunctions(["createSystem"], {
    state: { sshKeys: [] },
    elements: sparseElements({
      setupSshKeyMode: new FakeElement({ value: "reuse" }),
      setupSshExistingKey: new FakeElement({ value: "" }),
      setupCreateButton: createButton,
      setupResult: new FakeElement(),
    }),
    collectSetupPayload: () => ({ label: "Box", truenas_host: "https://nas.example.test", ssh_enabled: true }),
    normalizeKeyMode: (value) => value,
    getSshKeyByName: () => null,
    setBanner: (message, tone) => banners.push([message, tone]),
    fetchJson: async () => { requests += 1; return {}; },
  });

  await createSystem();

  assert.deepEqual(banners, [["Choose or create an SSH key first.", "error"]]);
  assert.equal(requests, 0);
  assert.equal(createButton.disabled, false);
});

// A rejected save keeps the draft.

test("a cross-origin rejection keeps the form draft and says so", async () => {
  const banners = [];
  const label = new FakeElement({ value: "Box" });
  const result = new FakeElement();
  const detail = "This page was opened at http://192.0.2.10:8082, but the admin service only accepts changes from http://nas.example.test:8082. Open the admin UI at http://nas.example.test:8082, or set ADMIN_PUBLIC_ORIGIN in .env to http://192.0.2.10:8082 and recreate the admin container.";
  const { createSystem } = loadFunctions(["createSystem"], {
    state: {},
    elements: sparseElements({
      setupSystemLabel: label,
      setupResult: result,
      setupCreateButton: new FakeElement(),
      setupSshKeyMode: new FakeElement({ value: "none" }),
    }),
    collectSetupPayload: () => ({ label: label.value, truenas_host: "https://nas.example.test", ssh_enabled: false }),
    normalizeKeyMode: (value) => value,
    getSshKeyByName: () => null,
    setBanner: (message, tone) => banners.push([message, tone]),
    fetchJson: async () => {
      const error = new Error(detail);
      error.status = 403;
      throw error;
    },
    resetSetupForm: () => assert.fail("the draft must survive a rejected save"),
  });

  await createSystem();

  assert.equal(result.textContent, `System setup failed: ${detail} Your entries are still in the form.`);
  assert.equal(banners.at(-1)[1], "error");
  assert.equal(label.value, "Box");
  assert.doesNotMatch(functionSource("createSystem"), /resetSetupForm/);
});

// Profile delete knows about references up front.

test("a profile still used by systems disables Delete with a Used-by title and skips the confirm", async () => {
  const { describeProfileReferences } = loadFunctions(["describeProfileReferences"]);
  assert.equal(describeProfileReferences(1), "Used by 1 system");
  assert.equal(describeProfileReferences(3), "Used by 3 systems");
  assert.match(SOURCE, /profileBuilderDeleteButton\.disabled = !\(loadedProfile && loadedProfile\.is_custom\) \|\| referenceCount > 0/);
  assert.match(SOURCE, /profileBuilderDeleteButton\.title = referenceCount > 0 \? describeProfileReferences\(referenceCount\) : ""/);

  const banners = [];
  let confirms = 0;
  let requests = 0;
  const result = new FakeElement();
  const { deleteCustomProfile } = loadFunctions(
    ["deleteCustomProfile", "profileReferenceCount", "describeProfileReferences"],
    {
      state: { loadedBuilderProfileId: "lab-4x4" },
      elements: sparseElements({ profileBuilderResult: result }),
      getProfileById: () => ({ id: "lab-4x4", label: "Lab 4x4", is_custom: true, reference_count: 2 }),
      setBanner: (message, tone) => banners.push([message, tone]),
      window: { confirm: () => { confirms += 1; return true; } },
      fetchJson: async () => { requests += 1; return {}; },
    }
  );

  await deleteCustomProfile();

  assert.equal(result.textContent, "Lab 4x4 is used by 2 systems. Move them to another profile before deleting it.");
  assert.equal(banners.at(-1)[1], "error");
  assert.equal(confirms, 0);
  assert.equal(requests, 0);
});

// The origin link only appears when it goes somewhere else.

test("the admin origin link is hidden unless a configured address differs from the current one", () => {
  const run = (publicOrigin) => {
    const link = new FakeElement({ classes: ["hidden"] });
    const { updateAdminMeta } = loadFunctions(["updateAdminMeta"], {
      state: { admin: { public_origin: publicOrigin }, systems: [], profiles: [], currentAdminView: "builder" },
      elements: sparseElements({ adminOriginLink: link }),
      window: { location: { origin: "http://192.0.2.10:8082", pathname: "/" } },
      document: fakeDocument(),
      formatCountdown: () => "",
      syncSessionBanner: () => {},
      safeHttpUrl: () => "",
    });
    updateAdminMeta();
    return link;
  };

  assert.ok(run(null).classList.contains("hidden"));
  assert.ok(run("http://192.0.2.10:8082").classList.contains("hidden"));
  assert.ok(run("not an origin").classList.contains("hidden"));

  const shown = run("http://nas.example.test:8082");
  assert.ok(!shown.classList.contains("hidden"));
  assert.equal(shown.textContent, "Open at http://nas.example.test:8082");
  assert.equal(shown.href, "http://nas.example.test:8082/?view=builder");
  assert.doesNotMatch(TEMPLATE, /Open Another Admin Tab/);
  assert.match(TEMPLATE, /id="admin-origin-link" class="button ghost hidden"/);
  assert.match(MAIN, /def resolve_public_origin\(settings: AdminSettings, request: Request\) -> str \| None:/);
});
