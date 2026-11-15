"use strict";

// Backup admin state ownership (#1198).
//
// The backup settings dialog keeps a model next to the controls it draws.
// These tests pin that an unsaved class draft survives a target change, on
// screen and in the Save payload.
// Synthetic data only: example.test names, no real paths.

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "../..");
const { createBackupLibrary } = require(path.join(ROOT, "admin_service/static/admin_backups.js"));

// ------------------------------------------------------------ fake DOM --
// The same small DOM the backup library tests use, kept in this module so the
// suite stands alone.

class FakeNode {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.tagName = tag ? tag.toUpperCase() : "#text";
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.className = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.open = false;
    this._text = "";
  }
  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.ownerDocument.body;
  }
  get id() {
    return this.attributes.id || "";
  }
  get textContent() {
    if (this.tagName === "#text") return this._text;
    return this.children.map((child) => child.textContent).join("");
  }
  set textContent(value) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    if (this.tagName === "#text") this._text = String(value);
    else if (value !== "") this.append(String(value));
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "open") this.open = true;
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name === "open") this.open = false;
  }
  get type() {
    return this.attributes.type || "";
  }
  append(...nodes) {
    nodes.forEach((node) => {
      const child = typeof node === "string" ? this.ownerDocument.createTextNode(node) : node;
      if (child.parentNode) child.parentNode.children = child.parentNode.children.filter((c) => c !== child);
      child.parentNode = this;
      this.children.push(child);
    });
  }
  replaceChildren(...nodes) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.append(...nodes.filter((node) => node !== "" && node !== null && node !== undefined));
  }
  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }
  dispatch(type) {
    let node = this;
    const event = { type, target: this, preventDefault() {} };
    while (node) {
      (node.listeners[type] || []).forEach((listener) => listener({ ...event, currentTarget: node }));
      node = node.parentNode;
    }
  }
  click() {
    this.dispatch("click");
  }
  focus() {
    this.ownerDocument.activeElement = this;
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
  }
  matches(selector) {
    return selector.split(",").some((part) => matchesSimple(this, part.trim()));
  }
  closest(selector) {
    let node = this;
    while (node && node.tagName !== "#text") {
      if (node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      node.children.forEach((child) => {
        if (child.tagName !== "#text" && child.matches(selector)) found.push(child);
        walk(child);
      });
    };
    walk(this);
    return found;
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function matchesSimple(node, selector) {
  const match = selector.match(/^([a-z0-9]*)?(#[\w-]+)?((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/i);
  if (!match) throw new Error(`unsupported selector ${selector}`);
  const [, tag, id, classes, attrs] = match;
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  if (id && node.id !== id.slice(1)) return false;
  const own = String(node.className || "").split(/\s+/);
  if (classes && !classes.split(".").filter(Boolean).every((cls) => own.includes(cls))) return false;
  for (const attr of attrs.match(/\[[^\]]+\]/g) || []) {
    const [, name, value] = attr.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
    let actual;
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_m, c) => c.toUpperCase());
      actual = node.dataset[key];
    } else {
      actual = node.getAttribute(name);
    }
    if (actual === undefined || actual === null) return false;
    if (value !== undefined && String(actual) !== value) return false;
  }
  return true;
}

function fakeDocument() {
  const doc = {
    activeElement: null,
    createElement: (tag) => new FakeNode(doc, tag),
    createTextNode: (text) => {
      const node = new FakeNode(doc, "");
      node._text = String(text);
      return node;
    },
  };
  doc.body = new FakeNode(doc, "body");
  return doc;
}

// ------------------------------------------------------------ fake API --

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function policyView(overrides = {}) {
  return {
    revision: "rev-1",
    classes: {
      config: { values: { enabled: true, local_keep: 30, remote_keep: null }, locked: {} },
      full: { values: { enabled: false, local_keep: 7, schedule: "0 3 * * *", archive_format: "tar.zst" }, locked: { schedule: "BACKUP_FULL_SCHEDULE" } },
    },
    archive_formats: ["tar.zst", "7z"],
    targets: [{
      values: { target_id: "alpha", label: "Alpha", enabled: true, provider: "filesystem", root: "/synthetic/alpha" },
      original_target_id: "alpha",
      secrets: {},
    }],
    targets_locked_by: null,
    providers: ["filesystem"],
    target_fields: ["target_id", "provider", "root"],
    secret_fields: [],
    problems: [],
    ...overrides,
  };
}

function artifact(overrides = {}) {
  return { id: "copy-a", backup_class: "config", location: "local", created_at: "2026-09-21T10:00:00Z", size: 2048, verified: true, restorable: true, state: "ok", preserved: false, ...overrides };
}

function listing(artifacts) {
  return { available: true, classes: {}, targets: [], storage: {}, artifacts, running: null };
}

// Each route answers with a value, a function of the request, or a queue of
// those; a deferred's promise holds the reply until the test releases it.
function fakeApi(routes) {
  const calls = [];
  const queues = {};
  async function fetchJson(url, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const [pathname] = url.split("?");
    const key = `${method} ${pathname}`;
    calls.push({ method, url, options });
    let handler = routes[key];
    if (Array.isArray(handler)) {
      queues[key] = queues[key] ?? 0;
      handler = handler[Math.min(queues[key]++, handler.length - 1)];
    }
    if (handler === undefined) throw Object.assign(new Error(`no route ${key}`), { status: 404 });
    const result = await (typeof handler === "function" ? handler({ url, options }) : handler);
    if (result instanceof Error) throw result;
    return result;
  }
  return { fetchJson, calls };
}

function mount(api, deps = {}) {
  const doc = fakeDocument();
  const root = doc.createElement("section");
  doc.body.append(root);
  const elements = { root };
  ["heading", "status", "policies", "targets", "storage", "artifacts", "refreshButton", "cleanupButton", "editButton"].forEach((key) => {
    elements[key] = doc.createElement(key.endsWith("Button") ? "button" : "div");
    root.append(elements[key]);
  });
  elements.dialog = doc.createElement("dialog");
  root.append(elements.dialog);
  const banners = [];
  const library = createBackupLibrary({
    document: doc,
    elements,
    fetchJson: api.fetchJson,
    fetchBackupRestore: api.fetchJson,
    describeBackupRestoreFailure: (error) => String(error?.message || error),
    describeBackupRestoreConfirmation: () => "Synthetic restore confirmation.",
    describeMaintenanceOutcome: () => ({ ok: true, sentence: "", startKeys: [] }),
    renderMaintenanceResult: (node, lead) => { node.textContent = `${lead}.`; },
    refreshAdminState: async () => {},
    encodeUtf8Base64: (value) => Buffer.from(String(value), "utf8").toString("base64"),
    formatBytes: (value) => `${value}B`,
    formatLocalTimestamp: (value) => (value ? `T(${value})` : "-"),
    setBanner: (message, tone) => banners.push([message, tone]),
    confirm: () => true,
    isStopped: () => false,
    ...deps,
  });
  library.bind();
  return { elements, library, banners };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const field = (elements, id) => elements.dialog.querySelector(`#${id}`);
const visibleTargetIds = (elements) => elements.dialog.querySelectorAll("[data-target-key]")
  .filter((input) => input.dataset.targetKey === "target_id")
  .map((input) => input.value);
const puts = (api) => api.calls.filter((call) => call.method === "PUT").map((call) => JSON.parse(call.options.body));
const listGets = (api) => api.calls.filter((call) => call.method === "GET" && call.url === "/api/admin/backups");

// ------------------------------------------ #1198: class drafts survive --

function typeClassDraft(elements) {
  field(elements, "backup-edit-config-local_keep").value = "99";
  field(elements, "backup-edit-config-remote_keep").value = "";
  field(elements, "backup-edit-full-enabled").checked = true;
  field(elements, "backup-edit-full-archive_format").value = "7z";
}

function displayedClassDraft(elements) {
  return {
    local_keep: field(elements, "backup-edit-config-local_keep").value,
    remote_keep: field(elements, "backup-edit-config-remote_keep").value,
    full_enabled: field(elements, "backup-edit-full-enabled").checked,
    archive_format: field(elements, "backup-edit-full-archive_format").value,
  };
}

const TYPED = { local_keep: "99", remote_keep: "", full_enabled: true, archive_format: "7z" };

for (const change of ["add", "remove"]) {
  test(`#1198 ${change === "add" ? "adding" : "removing"} a target keeps unsaved class settings on screen and in Save`, async () => {
    const two = policyView();
    two.targets.push({ values: { target_id: "bravo", label: "Bravo", enabled: true, provider: "filesystem", root: "/synthetic/bravo" }, original_target_id: "bravo", secrets: {} });
    const api = fakeApi({
      "GET /api/admin/backups/policy": two,
      "PUT /api/admin/backups/policy": () => policyView({ revision: "rev-2" }),
    });
    const { elements, library } = mount(api);
    await library.actions.openPolicyEditor(elements.editButton);
    typeClassDraft(elements);
    // Control: the target draft was already preserved and must stay so.
    field(elements, "backup-edit-target-0-label").value = "Alpha typed";
    if (change === "add") library.actions.addPolicyTarget();
    else library.actions.removePolicyTarget(1);

    assert.deepEqual(displayedClassDraft(elements), TYPED, "the class draft is still on screen");
    assert.equal(field(elements, "backup-edit-target-0-label").value, "Alpha typed");
    assert.deepEqual(visibleTargetIds(elements), change === "add" ? ["alpha", "bravo", ""] : ["alpha"]);
    // The environment lock is still shown and still never sent.
    assert.equal(field(elements, "backup-edit-full-schedule").disabled, true);
    assert.match(elements.dialog.textContent, /Set by BACKUP_FULL_SCHEDULE in the environment/);

    if (change === "add") library.actions.removePolicyTarget(2);
    await library.actions.savePolicy();
    const [sent] = puts(api);
    assert.deepEqual(sent.classes.config, { enabled: true, local_keep: 99, remote_keep: null });
    assert.deepEqual(sent.classes.full, { enabled: true, local_keep: 7, archive_format: "7z" });
    assert.equal(sent.targets[0].values.label, "Alpha typed");
  });
}

test("#1198 control: a class field locked by the environment keeps its value through a target change", async () => {
  const locked = policyView();
  locked.classes.config.locked = { local_keep: "BACKUP_CONFIG_LOCAL_KEEP" };
  const api = fakeApi({ "GET /api/admin/backups/policy": locked, "PUT /api/admin/backups/policy": () => policyView() });
  const { elements, library } = mount(api);
  await library.actions.openPolicyEditor(elements.editButton);
  library.actions.addPolicyTarget();
  assert.equal(field(elements, "backup-edit-config-local_keep").value, "30");
  assert.equal(field(elements, "backup-edit-config-local_keep").disabled, true);
  library.actions.removePolicyTarget(1);
  await library.actions.savePolicy();
  assert.equal("local_keep" in puts(api)[0].classes.config, false, "a locked value is never sent");
});
