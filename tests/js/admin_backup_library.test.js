"use strict";

// Backups library page (#398) against a fake of the /api/admin/backups API.
// Synthetic data only: example.test names, no real paths or credentials.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { loadAdminFunctions, transportNames, restoreNames, restoreResult, jsonResponse } = require("./helpers/admin_transport");

const ROOT = path.resolve(__dirname, "../..");
const { createBackupLibrary, model } = require(path.join(ROOT, "admin_service/static/admin_backups.js"));
const ADMIN_SOURCE = fs.readFileSync(path.join(ROOT, "admin_service/static/admin.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "admin_service/templates/index.html"), "utf8");
const BASE = fs.readFileSync(path.join(ROOT, "admin_service/templates/base.html"), "utf8");
const STYLES = fs.readFileSync(path.join(ROOT, "admin_service/static/admin.css"), "utf8");
const LIBRARY_SOURCE = fs.readFileSync(path.join(ROOT, "admin_service/static/admin_backups.js"), "utf8");

// ------------------------------------------------------------ fake DOM --

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
  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name);
  }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name === "open") this.open = false;
  }
  get href() {
    return this.attributes.href || "";
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
  appendChild(node) {
    this.append(node);
    return node;
  }
  replaceChildren(...nodes) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.append(...nodes.filter((node) => node !== "" && node !== null && node !== undefined));
  }
  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }
  dispatch(type, extra = {}) {
    let node = this;
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra };
    while (node) {
      (node.listeners[type] || []).forEach((listener) => listener({ ...event, currentTarget: node }));
      node = node.parentNode;
    }
    return event;
  }
  click() {
    return this.dispatch("click");
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

const SHA = "a".repeat(64);

function syntheticLibrary() {
  return {
    classes: {
      config: { enabled: true, debounce_seconds: 60, max_delay_seconds: 900, local_keep: 10, remote_keep: 20, remote_max_age_days: null, pending_changes: 0, last_run: { at: "2026-09-21T10:00:00Z", ok: true, detail: "", artifact_id: "cfg-new" }, passphrase_file: "/run/secrets/backup-passphrase" },
      full: { enabled: true, schedule: "0 3 * * *", next_run_at: "2026-09-25T03:00:00Z", local_keep: 3, remote_keep: 5, remote_max_age_days: 90, last_run: null, retention: { "offsite-sftp": { keep_count: 5, max_age_days: 90 } }, output_dir: "/data/backups" },
    },
    targets: [
      { id: "offsite-sftp", label: "Offsite SFTP", provider: "sftp", transport_encrypted: true, enabled: true, last_run: { at: "2026-09-20T03:00:00Z", ok: true, detail: "" } },
      { id: "legacy-ftp", label: "Old FTP box", provider: "ftp", transport_encrypted: false, enabled: true, last_run: { at: "2026-09-20T03:00:00Z", ok: false, detail: "login failed for ftp://backup:hunter2@ftp.example.test/srv/backups/jbod" } },
    ],
    artifacts: [
      { id: "cfg-new", backup_class: "config", location: "local", created_at: "2026-09-21T10:00:00Z", size: 2048, sha256: SHA, verified: true, restorable: true, state: "ok", preserved: false, preserve_reason: null, preserved_by: null, change_count: 2, app_version: "0.23.0" },
      { id: "cfg-old", backup_class: "config", location: "offsite-sftp", created_at: "2026-09-01T10:00:00Z", size: 1024, sha256: SHA, verified: true, restorable: true, state: "ok", preserved: true, preserve_reason: "before upgrade", preserved_by: "admin", change_count: 1, app_version: "0.22.0" },
      { id: "full-part", backup_class: "full", location: "local", created_at: "2026-09-22T03:00:00Z", size: 4096, sha256: null, verified: false, restorable: false, state: "incomplete", preserved: false, change_count: 0, app_version: null },
      { id: "full-new", backup_class: "full", location: "local", created_at: "2026-09-21T03:00:00Z", size: 1048576, sha256: SHA, verified: true, restorable: true, state: "ok", preserved: false, change_count: 0, app_version: "0.23.0" },
      { id: "full-odd", backup_class: "full", location: "legacy-ftp", created_at: "2026-08-01T03:00:00Z", size: 8192, sha256: SHA, verified: true, restorable: false, state: "unsupported", preserved: false, change_count: 0, app_version: "0.9.0" },
    ],
    storage: {
      local: { config_bytes: 2048, full_bytes: 1052672, count: 3 },
      "offsite-sftp": { config_bytes: 1024, full_bytes: 0, count: 1 },
    },
    available: true,
    detail: null,
    running: null,
  };
}

function fakeApi(overrides = {}) {
  const calls = [];
  const data = syntheticLibrary();
  const routes = {
    "GET /api/admin/backups": () => data,
    "GET /api/admin/backups/cfg-new": () => ({ ...data.artifacts[0], changes: [
      { change_id: "c1", at: "2026-09-21T09:58:00Z", action: "system saved", subject: "nas-a.example.test" },
      { change_id: "c2", at: "2026-09-21T09:59:00Z", action: "key file changed", subject: "/srv/app/keys/id_ed25519" },
      { change_id: "c3", at: null, action: null, subject: null },
    ], inspect: { encrypted: true, schema_version: 3, app_version: "0.23.0", packaging: "7z", groups: ["config", "profiles"] }, last_verify: { at: "2026-09-21T10:05:00Z", ok: true, detail: "" } }),
    "POST /api/admin/backups/cfg-new/verify": () => ({ ok: true, artifact: { ...data.artifacts[0], verified: true } }),
    "POST /api/admin/backups/cfg-new/preserve": () => ({ ok: true }),
    "DELETE /api/admin/backups/cfg-old/preserve": () => ({ ok: true }),
    "POST /api/admin/backups/run": () => ({ ok: true, backup_class: "config", state: "started" }),
    "POST /api/admin/backups/targets/legacy-ftp/test": () => ({ ok: true, detail: "ftp archive destination is writable", duration_ms: 42, provider: "ftp", transport_encrypted: false }),
    "POST /api/admin/backups/targets/offsite-sftp/test": () => ({ ok: false, detail: "connect to sftp://svc:pw@sftp.example.test/srv/x failed" }),
    "POST /api/admin/backups/full-new/restore/inspect": () => ({ ok: true, encryption_mode: "plaintext", inspection_receipt: "receipt-1", aggregate_counts: { systems: 2, profiles: 1 }, exported_at: "2026-09-21T03:00:00Z" }),
    "POST /api/admin/backups/full-new/restore/import": () => restoreResult({ stopped_containers: ["ui"], restarted_containers: ["ui"] }),
    "GET /api/admin/backups/lifecycle/plan": () => ({ plan_token: "plan-token-1", expires_at: "2026-09-24T12:00:00Z", items: [
      { id: "cfg-old", location: "offsite-sftp", backup_class: "config", reason: "beyond keep_count 1 (newest #2)", kind: "retention" },
      { id: "full-part", location: "local", backup_class: "full", reason: "unverified for longer than grace 1d (age 2d)", kind: "unverified" },
    ], guarded: [{ id: "full-new", location: "local", backup_class: "full", reason: "beyond keep_count 0 (newest #1)" }] }),
    "POST /api/admin/backups/lifecycle/apply": () => ({ ok: true, deleted: ["cfg-old"], already_missing: ["full-part"], failed: null, not_attempted: [] }),
    ...overrides,
  };
  async function fetchJson(url, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const [pathname] = url.split("?");
    calls.push({ method, url, options });
    const handler = routes[`${method} ${pathname}`];
    if (!handler) {
      const error = new Error(`no route ${method} ${pathname}`);
      error.status = 404;
      throw error;
    }
    const result = await handler({ url, options });
    if (result instanceof Error) throw result;
    return result;
  }
  return { fetchJson, calls, data };
}

function mount({ api = fakeApi(), confirm = () => true, stopped = false, deps = {} } = {}) {
  const doc = fakeDocument();
  const ids = ["root", "heading", "status", "policies", "targets", "storage", "artifacts", "refreshButton", "cleanupButton", "editButton"];
  const elements = {};
  const root = doc.createElement("section");
  doc.body.append(root);
  ids.forEach((key) => {
    if (key === "root") return;
    elements[key] = doc.createElement(key.endsWith("Button") ? "button" : "div");
    root.append(elements[key]);
  });
  elements.root = root;
  elements.dialog = doc.createElement("dialog");
  root.append(elements.dialog); // nested in the section, as the template does
  const banners = [];
  const refreshes = [];
  const restoreTransport = loadAdminFunctions(restoreNames, { fetchJson: api.fetchJson });
  const library = createBackupLibrary({
    document: doc,
    elements,
    fetchJson: api.fetchJson,
    fetchBackupRestore: restoreTransport.fetchBackupRestore,
    describeBackupRestoreFailure: restoreTransport.describeBackupRestoreFailure,
    formatBytes: (value) => `${value}B`,
    formatLocalTimestamp: (value) => (value ? `T(${value})` : "-"),
    setBanner: (message, tone) => banners.push([message, tone]),
    confirm,
    encodeUtf8Base64: (value) => Buffer.from(String(value), "utf8").toString("base64"),
    describeBackupRestoreConfirmation: (inspection) => `This backup contains stuff (${inspection.encryption_mode}).\n\nRestoring replaces all current settings, mappings and history with this backup. Continue?`,
    describeMaintenanceOutcome: () => ({ ok: true, sentence: "", startKeys: [] }),
    renderMaintenanceResult: (node, lead) => { node.textContent = `${lead}.`; },
    refreshAdminState: async () => { refreshes.push(true); },
    isStopped: () => stopped,
    ...deps,
  });
  library.bind();
  return { doc, elements, library, api, banners, refreshes };
}

function rowFor(elements, id) {
  return elements.artifacts.querySelector(`tr[data-artifact-id="${id}"]`);
}

function action(scope, name, id) {
  return scope.querySelectorAll(`[data-backup-action="${name}"]`).find((node) => !id || node.dataset.backupId === id);
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

// ---------------------------------------------------------------- tests --

test("page lists policies, targets, storage and artifacts grouped by class, newest first", async () => {
  const { elements, library } = mount();
  await library.load();

  const policyText = elements.policies.textContent;
  assert.match(policyText, /Settings backups/);
  assert.match(policyText, /Full backups/);
  assert.match(policyText, /Copies kept here10/);
  assert.match(policyText, /Copies kept on targets20/);
  assert.match(policyText, /Oldest copy kept on targets \(days\)No limit/);
  assert.match(policyText, /Last backupWorked \(T\(2026-09-21T10:00:00Z\)\)/);
  assert.match(policyText, /Last backupNot used yet/);
  assert.match(policyText, /Next runT\(2026-09-25T03:00:00Z\)/);
  assert.match(policyText, /Keeps: Offsite SFTPcopies kept 5, oldest copy kept \(days\) 90/i);
  assert.equal(action(elements.policies, "run").textContent, "Back up now");

  const configRows = elements.artifacts.querySelector('section[data-backup-class="config"]').querySelectorAll("tr[data-artifact-id]");
  assert.deepEqual(configRows.map((row) => row.dataset.artifactId), ["cfg-new", "cfg-old"]);
  const fullRows = elements.artifacts.querySelector('section[data-backup-class="full"]').querySelectorAll("tr[data-artifact-id]");
  assert.deepEqual(fullRows.map((row) => row.dataset.artifactId), ["full-part", "full-new", "full-odd"]);

  assert.match(elements.storage.textContent, /This server2048B1052672B3/);
  assert.match(elements.storage.textContent, /Offsite SFTP1024B0B1/);
  assert.match(elements.status.textContent, /5 backup copies/);
});

test("incomplete and unsupported copies are shown but cannot be restored", async () => {
  const { elements, library } = mount();
  await library.load();
  for (const id of ["full-part", "full-odd"]) {
    const row = rowFor(elements, id);
    assert.ok(row, `${id} must be listed, not hidden`);
    assert.equal(row.dataset.restorable, "false");
    assert.equal(action(row, "restore").disabled, true);
    assert.match(row.textContent, /can't restore/i);
  }
  assert.match(rowFor(elements, "full-part").textContent, /Incomplete, can't restore/);
  assert.match(rowFor(elements, "full-odd").textContent, /Unsupported, can't restore/);
  assert.equal(action(rowFor(elements, "full-new"), "restore").disabled, false);
});

test("kept copies show a badge with the reason and a Stop keeping action", async () => {
  const { elements, library } = mount();
  await library.load();
  const row = rowFor(elements, "cfg-old");
  assert.match(row.textContent, /Kept before upgrade/);
  assert.ok(action(row, "unpreserve"));
  assert.equal(action(row, "preserve"), undefined);
  assert.ok(action(rowFor(elements, "cfg-new"), "preserve"));
});

test("plain FTP is labelled unencrypted without warning styling", async () => {
  const { elements, library } = mount();
  await library.load();
  const ftp = elements.targets.querySelector('tr[data-target-id="legacy-ftp"]');
  const sftp = elements.targets.querySelector('tr[data-target-id="offsite-sftp"]');
  const badge = ftp.querySelector(".backup-plain-badge");
  assert.equal(badge.textContent, "Unencrypted");
  assert.doesNotMatch(badge.className, /warn|danger|error/);
  assert.equal(sftp.querySelector(".backup-plain-badge"), null);
  assert.match(sftp.textContent, /SFTP/);
  assert.match(sftp.textContent, /Worked/);
  assert.match(ftp.textContent, /Failed: login failed/);
  const block = STYLES.slice(STYLES.indexOf(".backup-plain-badge {"), STYLES.indexOf("}", STYLES.indexOf(".backup-plain-badge {")));
  assert.doesNotMatch(block, /--warning|--danger/);
});

test("no filesystem path or credential reaches the page", async () => {
  const { elements, library, doc } = mount();
  await library.load();
  await library.actions.testTarget("offsite-sftp");
  await library.actions.showDetails("cfg-new");
  await settle();
  const everything = [elements.root.textContent, elements.dialog.textContent].join("\n");
  assert.doesNotMatch(everything, /hunter2|svc:pw|\/srv\/|\/run\/secrets|\/data\/backups|passphrase_file|output_dir/);
  assert.match(everything, /login failed for ftp:\/\/ftp\.example\.test \(/);
  assert.ok(doc);
});

test("scrubText removes userinfo and absolute paths but keeps plain words", () => {
  assert.equal(model.scrubText("ok"), "ok");
  assert.equal(model.scrubText("sftp://u:p@h.example.test/x"), "sftp://h.example.test");
  assert.equal(model.scrubText("see https://docs.example.test/a/b."), "see https://docs.example.test");
  assert.equal(model.scrubText("cannot open /var/lib/app/backups/x.7z"), "cannot open [path]");
  assert.equal(model.scrubText(["D", ":\\", "backups\\a.zip missing"].join("")), "[path] missing");
  assert.equal(model.scrubText("1/2 done"), "1/2 done");
});

test("artifact ids are validated before any URL is built", () => {
  assert.equal(model.artifactUrl("abc123", "/verify"), "/api/admin/backups/abc123/verify");
  for (const bad of ["../etc", "a/b", "", "%2e%2e", " x"]) {
    assert.throws(() => model.artifactUrl(bad), /not valid/);
  }
  assert.throws(() => model.targetUrl("../x"), /not valid/);
});

test("details shows the changes captured in a settings backup", async () => {
  const { elements, library } = mount();
  await library.load();
  const trigger = action(rowFor(elements, "cfg-new"), "details");
  trigger.click();
  await settle();
  assert.equal(elements.dialog.open, true);
  const text = elements.dialog.textContent;
  assert.match(text, /Changes in this backup/);
  assert.match(text, /system saved: nas-a\.example\.test/);
  assert.match(text, /key file changed: \[path\]/);
  assert.match(text, /EncryptionEncrypted/);
  assert.match(text, /File format7z/);
  assert.match(text, /Containsconfig, profiles/);
  assert.match(text, /Last checkWorked \(T\(2026-09-21T10:05:00Z\)\)/);
  assert.match(text, /A change whose details are no longer kept/);
  action(elements.dialog, "dialog-cancel").click();
  assert.equal(elements.dialog.open, false);
});

test("a slow details response never lands in a dialog opened after it", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const base = fakeApi();
  const slow = fakeApi({ "GET /api/admin/backups/cfg-new": async () => { await gate; return base.data.artifacts[0]; } });
  const { elements, library } = mount({ api: slow });
  await library.load();
  const pending = library.actions.showDetails("cfg-new");
  library.actions.closeDialog();
  library.actions.openPreserve("cfg-old");
  release();
  await pending;
  await settle();
  assert.match(elements.dialog.textContent, /Keep this backup/);
  assert.ok(elements.dialog.querySelector("#backup-preserve-reason"), "Keep keeps its reason field");
  assert.doesNotMatch(elements.dialog.textContent, /Checksum|Changes in this backup/);
});

test("a clean-up plan that arrives after the dialog closed is dropped", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const base = fakeApi();
  const slow = fakeApi({ "GET /api/admin/backups/lifecycle/plan": async () => { await gate; return base.fetchJson("/api/admin/backups/lifecycle/plan"); } });
  const { elements, library, api } = mount({ api: slow });
  await library.load();
  const pending = library.actions.openCleanup();
  library.actions.closeDialog();
  release();
  await pending;
  assert.equal(elements.dialog.open, false);
  await library.actions.applyCleanup();
  assert.equal(api.calls.some((call) => call.url.endsWith("/lifecycle/apply")), false);
});

test("closing a dialog returns focus to the button that opened it, including Escape", async () => {
  const { elements, library, doc } = mount();
  await library.load();
  const trigger = action(rowFor(elements, "cfg-new"), "preserve");
  trigger.click();
  assert.equal(elements.dialog.open, true);
  assert.equal(doc.activeElement.id, "backup-preserve-reason");
  elements.dialog.dispatch("cancel");
  assert.equal(elements.dialog.open, false);
  assert.equal(doc.activeElement, trigger);
});

test("keep asks for a reason and sends it", async () => {
  const { elements, library, api, banners } = mount();
  await library.load();
  action(rowFor(elements, "cfg-new"), "preserve").click();
  action(elements.dialog, "preserve-confirm").click();
  await settle();
  assert.match(elements.dialog.textContent, /Add a short reason first/);
  assert.equal(api.calls.filter((call) => call.method === "POST").length, 0);

  elements.dialog.querySelector("#backup-preserve-reason").value = "  known good  ";
  await library.actions.submitPreserve();
  const post = api.calls.find((call) => call.method === "POST");
  assert.equal(post.url, "/api/admin/backups/cfg-new/preserve");
  assert.deepEqual(JSON.parse(post.options.body), { reason: "known good" });
  assert.equal(elements.dialog.open, false);
  assert.deepEqual(banners.at(-1), ["Backup kept.", "success"]);
});

test("stop keeping confirms first and a cancel sends nothing", async () => {
  let answer = false;
  const { elements, library, api } = mount({ confirm: () => answer });
  await library.load();
  await library.actions.unpreserve("cfg-old");
  assert.equal(api.calls.filter((call) => call.method === "DELETE").length, 0);
  answer = true;
  await library.actions.unpreserve("cfg-old");
  assert.equal(api.calls.filter((call) => call.method === "DELETE")[0].url, "/api/admin/backups/cfg-old/preserve");
  assert.ok(elements);
});

test("verify posts once even when clicked twice and reloads the list", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const api = fakeApi({ "POST /api/admin/backups/cfg-new/verify": async () => { await gate; return { ok: true, verified: true }; } });
  const { elements, library, banners } = mount({ api });
  await library.load();
  action(rowFor(elements, "cfg-new"), "verify").click();
  await settle();
  const busy = action(rowFor(elements, "cfg-new"), "verify");
  assert.equal(busy.disabled, true);
  assert.equal(busy.textContent, "Verifying...");
  const again = library.actions.verify("cfg-new");
  release();
  await again;
  assert.equal(api.calls.filter((call) => call.url.endsWith("/verify")).length, 1);
  assert.deepEqual(banners.at(-1), ["Backup verified.", "success"]);
  assert.equal(api.calls.filter((call) => call.url === "/api/admin/backups").length, 2);
});

test("download is a plain link to the streaming route, absent for missing copies", async () => {
  const api = fakeApi();
  api.data.artifacts.push({ id: "gone", backup_class: "full", location: "offsite-sftp", created_at: "2026-07-01T00:00:00Z", size: 1, verified: true, restorable: false, state: "missing", preserved: false });
  const { elements, library } = mount({ api });
  await library.load();
  const link = action(rowFor(elements, "full-new"), "download");
  assert.equal(link.tagName, "A");
  assert.equal(link.href, "/api/admin/backups/full-new/download");
  assert.equal(action(rowFor(elements, "gone"), "download"), undefined);
  assert.equal(action(rowFor(elements, "gone"), "verify").disabled, true);
});

test("back up now starts a run, shows it running, and polls until it finishes", async () => {
  const api = fakeApi();
  const timers = [];
  let running = { backup_class: "full", started_at: "2026-09-24T10:00:00Z" };
  api.data.running = running;
  const origin = api.fetchJson;
  const doc = mount({ api });
  // Reach in: the mount helper has no timer hook, so rebuild with one.
  const { createBackupLibrary: create } = require(path.join(ROOT, "admin_service/static/admin_backups.js"));
  const library = create({
    document: doc.doc, elements: doc.elements, fetchJson: origin, formatBytes: String, formatLocalTimestamp: String,
    setBanner: () => {}, setTimeout: (callback) => timers.push(callback), isVisible: () => true, isStopped: () => false,
  });
  await library.load();
  assert.match(doc.elements.status.textContent, /A full backup is running/);
  const runButtons = doc.elements.policies.querySelectorAll('[data-backup-action="run"]');
  assert.ok(runButtons.every((node) => node.disabled), "no second run while one is going");
  assert.equal(runButtons[1].textContent, "Backing up...");
  assert.equal(timers.length, 1);
  api.data.running = null;
  running = null;
  timers.shift()();
  await settle();
  await settle();
  assert.equal(timers.length, 0, "polling stops once nothing runs");
  assert.ok(doc.elements.policies.querySelectorAll('[data-backup-action="run"]').every((node) => !node.disabled));
});

function pollingLibrary(api, { visible = () => true } = {}) {
  const timers = [];
  const doc = mount({ api });
  const library = createBackupLibrary({
    document: doc.doc, elements: doc.elements, fetchJson: api.fetchJson, formatBytes: String, formatLocalTimestamp: String,
    setBanner: () => {}, setTimeout: (callback) => timers.push(callback), isVisible: visible, isStopped: () => false,
  });
  return { timers, library, elements: doc.elements };
}

test("a poll tick while the tab is hidden keeps polling so the run result shows on return", async () => {
  const api = fakeApi();
  api.data.running = { backup_class: "full", started_at: "2026-09-24T10:00:00Z" };
  let visible = true;
  const { timers, library, elements } = pollingLibrary(api, { visible: () => visible });
  await library.load();
  assert.equal(timers.length, 1);

  visible = false;
  timers.shift()();
  await settle();
  assert.equal(timers.length, 1, "a hidden tick reschedules instead of dropping the poll");

  visible = true;
  api.data.running = null;
  timers.shift()();
  await settle();
  await settle();
  assert.equal(timers.length, 0);
  assert.doesNotMatch(elements.status.textContent, /is running/);
  assert.ok(elements.policies.querySelectorAll('[data-backup-action="run"]').every((node) => !node.disabled));
});

test("one failed poll during a run keeps the error visible and polls again", async () => {
  let fail = false;
  const api = fakeApi({
    "GET /api/admin/backups": () => {
      if (fail) {
        fail = false;
        return Object.assign(new Error("HTTP 502"), { status: 502 });
      }
      return api.data;
    },
  });
  api.data.running = { backup_class: "full", started_at: "2026-09-24T10:00:00Z" };
  const { timers, library, elements } = pollingLibrary(api);
  await library.load();

  fail = true;
  timers.shift()();
  await settle();
  await settle();
  assert.match(elements.status.textContent, /^Couldn't load backups/);
  assert.equal(timers.length, 1, "a failed poll schedules another one");

  api.data.running = null;
  timers.shift()();
  await settle();
  await settle();
  assert.equal(timers.length, 0);
  assert.equal(library.state.data.running, null);
  assert.ok(elements.policies.querySelectorAll('[data-backup-action="run"]').every((node) => !node.disabled));
});

test("an unavailable backup service says so and offers nothing to run", async () => {
  const api = fakeApi({ "GET /api/admin/backups": () => ({ available: false, detail: "The backup scheduler is not running.", classes: { config: { enabled: false }, full: { enabled: false } }, targets: [], artifacts: [], storage: {}, running: null }) });
  const { elements, library } = mount({ api });
  await library.load();
  assert.equal(elements.status.textContent, "Backups aren't available: The backup scheduler is not running.");
  assert.ok(elements.policies.querySelectorAll('[data-backup-action="run"]').every((node) => node.disabled));
  assert.equal(elements.cleanupButton.disabled, true);
});

test("the status line shows the last clean-up, and a warning naming each place it failed", async () => {
  const okApi = fakeApi();
  okApi.data.grooming = { at: "2026-09-24T12:00:00Z", ok: true, deleted: 3, detail: null, failed_locations: {} };
  const ok = mount({ api: okApi });
  await ok.library.load();
  assert.match(ok.elements.status.textContent, /5 backup copies\. Last clean-up: T\(2026-09-24T12:00:00Z\), 3 removed\.$/);
  assert.equal(ok.elements.status.querySelector("span.backup-run-status.is-ok").textContent, "Last clean-up: T(2026-09-24T12:00:00Z), 3 removed.");

  const badApi = fakeApi();
  badApi.data.grooming = { at: "2026-09-24T12:00:00Z", ok: false, deleted: 1, detail: "ConnectionRefusedError: refused",
    failed_locations: { "offsite-sftp": "connect to sftp://svc:pw@sftp.example.test/srv/x failed", local: "PermissionError: denied" } };
  const bad = mount({ api: badApi });
  await bad.library.load();
  const warning = bad.elements.status.querySelector("span.backup-run-status.is-bad");
  assert.ok(warning, "a failed clean-up renders a warning span");
  assert.equal(warning.textContent,
    "Last clean-up T(2026-09-24T12:00:00Z) didn't finish: Offsite SFTP: connect to sftp://sftp.example.test failed; This server: PermissionError: denied.");
  assert.equal(bad.elements.status.querySelector("span.backup-run-status.is-ok"), null);

  const none = mount();
  await none.library.load();
  assert.equal(none.elements.status.textContent, "5 backup copies.");
});

test("back up now reports that the run started", async () => {
  const { library, banners } = mount();
  await library.load();
  await library.actions.runNow("config");
  assert.deepEqual(banners.at(-1), ["Settings backup started. This list updates when it finishes.", "success"]);
});

test("back up now posts the class and explains a 409 plainly", async () => {
  const busy = Object.assign(new Error("already running"), { status: 409 });
  const api = fakeApi({ "POST /api/admin/backups/run": () => busy });
  const { elements, library, banners } = mount({ api });
  await library.load();
  const run = action(elements.policies, "run");
  assert.equal(run.dataset.backupClass, "config");
  await library.actions.runNow("config");
  const post = api.calls.find((call) => call.url === "/api/admin/backups/run");
  assert.deepEqual(JSON.parse(post.options.body), { backup_class: "config" });
  assert.deepEqual(banners.at(-1), ["A backup or clean up is already running. Try again when it finishes.", "info"]);
  await library.actions.runNow("bogus");
  assert.equal(api.calls.filter((call) => call.url === "/api/admin/backups/run").length, 1);
});

test("a 503 from an action says the backup scheduler isn't running", async () => {
  const missing = Object.assign(new Error("Service Unavailable"), { status: 503 });
  const api = fakeApi({ "POST /api/admin/backups/run": () => missing });
  const { library, banners } = mount({ api });
  await library.load();
  await library.actions.runNow("full");
  assert.deepEqual(banners.at(-1), ["Backup failed: The backup scheduler isn't running on this server.", "error"]);
});

test("target test shows the result next to the target, scrubbed", async () => {
  const { elements, library } = mount();
  await library.load();
  await library.actions.testTarget("legacy-ftp");
  await library.actions.testTarget("offsite-sftp");
  assert.match(elements.targets.querySelector('tr[data-target-id="legacy-ftp"]').textContent, /Works \(42 ms\)/);
  const failed = elements.targets.querySelector('tr[data-target-id="offsite-sftp"]').textContent;
  assert.match(failed, /Failed: connect to sftp:\/\/sftp\.example\.test/);
  assert.doesNotMatch(failed, /svc:pw|\/srv\//);
});

test("a size-only target test and an FTP copy say the upload check was size only (#722)", async () => {
  const api = fakeApi({
    "POST /api/admin/backups/targets/legacy-ftp/test": () => ({ ok: true, detail: "ftp archive destination is writable; the remote size matched", duration_ms: 42, provider: "ftp", transport_encrypted: false, upload_check: "size" }),
    "GET /api/admin/backups/full-odd": () => ({ ...syntheticLibrary().artifacts[4], upload_check: "size", last_verify: null }),
  });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.testTarget("legacy-ftp");
  const row = elements.targets.querySelector('tr[data-target-id="legacy-ftp"]').textContent;
  assert.match(row, /Works \(42 ms\)\. Size checked only; Verify re-reads a backup\./);
  await library.actions.showDetails("full-odd");
  await settle();
  assert.match(elements.dialog.textContent, /VerifiedSize only at upload; Verify re-reads and hashes it/);
});

test("a size-only copy that passed an explicit Verify shows Yes", async () => {
  const api = fakeApi({
    "GET /api/admin/backups/full-odd": () => ({ ...syntheticLibrary().artifacts[4], upload_check: "size", last_verify: { at: "2026-09-21T10:05:00Z", ok: true, detail: "" } }),
  });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.showDetails("full-odd");
  await settle();
  assert.match(elements.dialog.textContent, /VerifiedYes/);
  assert.doesNotMatch(elements.dialog.textContent, /Size only at upload/);
});

// #1202: the list's Verified column and the details' Verified fact must make
// the same claim. The list row carries the upload check and the outcome of the
// last explicit Verify (without its text); the details carry the full result.
const SIZE_ONLY_TEXT = "Size only at upload; Verify re-reads and hashes it";

function verifiedColumn(elements, id) {
  const headers = elements.artifacts.querySelectorAll("th").map((cell) => cell.textContent);
  const row = rowFor(elements, id);
  const columns = row.children.filter((cell) => cell.tagName === "TD");
  return columns[headers.indexOf("Verified")].textContent;
}

function verifiedFact(elements) {
  const facts = elements.dialog.querySelector("dl").children;
  const index = facts.findIndex((node) => node.tagName === "DT" && node.textContent === "Verified");
  assert.notEqual(index, -1, "details dialog has a Verified fact");
  return facts[index + 1].textContent;
}

async function listAndDetails(listFields, detailFields = listFields) {
  const api = fakeApi();
  Object.assign(api.data.artifacts[4], listFields);
  const detail = { ...api.data.artifacts[4], ...detailFields };
  const { elements, library } = mount({
    api: { ...api, fetchJson: (url, options) => (url === "/api/admin/backups/full-odd" ? Promise.resolve(detail) : api.fetchJson(url, options)) },
  });
  await library.load();
  const column = verifiedColumn(elements, "full-odd");
  await library.actions.showDetails("full-odd");
  await settle();
  return { column, fact: verifiedFact(elements) };
}

test("a size-only copy reads as size only in the list and the details until Verify (#1202)", async () => {
  const before = await listAndDetails(
    { upload_check: "size", last_verify: null },
  );
  assert.deepEqual(before, { column: SIZE_ONLY_TEXT, fact: SIZE_ONLY_TEXT });

  // A failed or unreachable explicit Verify does not upgrade the claim either.
  const failed = await listAndDetails(
    { upload_check: "size", last_verify: { at: "2026-09-21T10:05:00Z", ok: false } },
    { upload_check: "size", last_verify: { at: "2026-09-21T10:05:00Z", ok: false, detail: "target unreachable" } },
  );
  assert.deepEqual(failed, { column: SIZE_ONLY_TEXT, fact: SIZE_ONLY_TEXT });

  // Only a boolean true counts as a passed Verify, in both views.
  const malformed = await listAndDetails(
    { upload_check: "size", last_verify: { at: "2026-09-21T10:05:00Z", ok: "true" } },
  );
  assert.deepEqual(malformed, { column: SIZE_ONLY_TEXT, fact: SIZE_ONLY_TEXT });
});

test("a successful explicit Verify turns the list and the details to Yes together (#1202)", async () => {
  const api = fakeApi({
    "POST /api/admin/backups/full-odd/verify": () => {
      api.data.artifacts[4].last_verify = { at: "2026-09-21T10:05:00Z", ok: true };
      return { ok: true, artifact: api.data.artifacts[4], detail: "Readback matched the catalogued size and SHA-256." };
    },
    "GET /api/admin/backups/full-odd": () => ({ ...api.data.artifacts[4], last_verify: { ...api.data.artifacts[4].last_verify, detail: "" } }),
  });
  Object.assign(api.data.artifacts[4], { upload_check: "size", last_verify: null });
  const { elements, library, banners } = mount({ api });
  await library.load();
  assert.equal(verifiedColumn(elements, "full-odd"), SIZE_ONLY_TEXT);
  await library.actions.verify("full-odd");
  assert.deepEqual(banners.at(-1), ["Backup verified.", "success"]);
  assert.equal(verifiedColumn(elements, "full-odd"), "Yes");
  await library.actions.showDetails("full-odd");
  await settle();
  assert.equal(verifiedFact(elements), "Yes");
});

test("the Verify banner follows the operation's own result, not the copy's catalogue flag (#1202)", async () => {
  // What the scheduler returns when the target cannot be reached: the
  // operation failed and nothing was read, but the catalogue keeps the copy's flag.
  const unreachable = {
    ok: false,
    artifact: { ...syntheticLibrary().artifacts[4], verified: true, upload_check: "size", last_verify: { at: "2026-09-24T12:00:00+00:00", ok: false } },
    detail: "ConnectionRefusedError: connection refused",
  };
  const responses = [
    [unreachable, ["Backup did not verify: ConnectionRefusedError: connection refused", "error"]],
    [{ ...unreachable, verified: true }, ["Backup did not verify: ConnectionRefusedError: connection refused", "error"]],
    [{ artifact: unreachable.artifact }, ["Backup did not verify.", "error"]],
    [{ ok: "true", artifact: unreachable.artifact }, ["Backup did not verify.", "error"]],
    // Control: a Verify that read the copy back and matched.
    [{ ok: true, artifact: { ...unreachable.artifact, last_verify: { at: "2026-09-24T12:00:00+00:00", ok: true } }, detail: "Readback matched the catalogued size and SHA-256." }, ["Backup verified.", "success"]],
  ];
  for (const [response, banner] of responses) {
    const api = fakeApi({
      "POST /api/admin/backups/full-odd/verify": () => {
        Object.assign(api.data.artifacts[4], response.artifact);
        return response;
      },
    });
    Object.assign(api.data.artifacts[4], { upload_check: "size", last_verify: null });
    const { elements, library, banners } = mount({ api });
    await library.load();
    await library.actions.verify("full-odd");
    assert.deepEqual(banners.at(-1), banner, JSON.stringify(response));
    assert.equal(verifiedColumn(elements, "full-odd"), banner[1] === "success" ? "Yes" : SIZE_ONLY_TEXT);
  }
});

test("Last check says Worked only for a Verify whose result is boolean true (#1202)", async () => {
  async function lastCheck(lastVerify) {
    const api = fakeApi({
      "GET /api/admin/backups/full-odd": () => ({ ...syntheticLibrary().artifacts[4], upload_check: "size", last_verify: lastVerify }),
    });
    const { elements, library } = mount({ api });
    await library.load();
    await library.actions.showDetails("full-odd");
    await settle();
    const facts = elements.dialog.querySelector("dl").children;
    const index = facts.findIndex((node) => node.tagName === "DT" && node.textContent === "Last check");
    return { verified: verifiedFact(elements), lastCheck: index === -1 ? null : facts[index + 1].textContent };
  }
  for (const ok of ["true", 1, "yes", {}]) {
    const result = await lastCheck({ at: "2026-09-21T10:05:00Z", ok, detail: "" });
    assert.equal(result.verified, SIZE_ONLY_TEXT, JSON.stringify(ok));
    assert.doesNotMatch(result.lastCheck, /Worked/, JSON.stringify(ok));
    assert.match(result.lastCheck, /^Failed/, JSON.stringify(ok));
  }
  // Controls: a passed and a failed Verify of this copy.
  assert.deepEqual(await lastCheck({ at: "2026-09-21T10:05:00Z", ok: true, detail: "" }), { verified: "Yes", lastCheck: "Worked (T(2026-09-21T10:05:00Z))" });
  assert.deepEqual(
    await lastCheck({ at: "2026-09-21T10:05:00Z", ok: false, detail: "Readback does not match" }),
    { verified: SIZE_ONLY_TEXT, lastCheck: "Failed: Readback does not match (T(2026-09-21T10:05:00Z))" },
  );
  // No Verify of this copy recorded: no Last check line.
  assert.deepEqual(await lastCheck(null), { verified: SIZE_ONLY_TEXT, lastCheck: null });
});

const NOT_RECORDED_TEXT = "Upload check not recorded; Verify re-reads and hashes it";

test("rows without upload-check provenance say so, the same way in both views (#1202)", async () => {
  // A remote copy catalogued before #722 has no recorded upload check: unknown,
  // not Yes, until an explicit Verify reads it back.
  assert.deepEqual(await listAndDetails({}), { column: NOT_RECORDED_TEXT, fact: NOT_RECORDED_TEXT });
  assert.deepEqual(await listAndDetails({ upload_check: null, last_verify: null }), { column: NOT_RECORDED_TEXT, fact: NOT_RECORDED_TEXT });
  assert.deepEqual(
    await listAndDetails({ upload_check: null, last_verify: { at: "2026-09-21T10:05:00Z", ok: true } }),
    { column: "Yes", fact: "Yes" },
  );
  // A remote copy read back at upload, and a local copy, are plainly verified.
  assert.deepEqual(await listAndDetails({ upload_check: "readback", last_verify: null }), { column: "Yes", fact: "Yes" });
  assert.deepEqual(await listAndDetails({ location: "local", upload_check: null, last_verify: null }), { column: "Yes", fact: "Yes" });
  // A copy that is not verified stays "Not yet", whatever the upload check was.
  assert.deepEqual(await listAndDetails({ verified: false }), { column: "Not yet", fact: "Not yet" });
  assert.deepEqual(await listAndDetails({ verified: false, upload_check: "size", last_verify: null }), { column: "Not yet", fact: "Not yet" });
});

// A checksum the storage provider reports at upload is not this application
// reading the copy back (#1202).
const PROVIDER_CHECKSUM_TEXT = "Checked by the provider's checksum at upload; Verify re-reads and hashes it";
const EITHER_TEXT = "Checked at upload by the provider's checksum or by read-back; Verify re-reads and hashes it";

test("a provider-checksum copy says so in both views until its own Verify passes (#1202)", async () => {
  const own = { at: "2026-09-21T10:05:00Z", ok: true };
  for (const [uploadCheck, text] of [["provider_checksum", PROVIDER_CHECKSUM_TEXT], ["provider_checksum_or_readback", EITHER_TEXT]]) {
    assert.deepEqual(await listAndDetails({ upload_check: uploadCheck, last_verify: null }), { column: text, fact: text }, uploadCheck);
    // A failed, unreachable or malformed Verify does not upgrade it.
    assert.deepEqual(await listAndDetails({ upload_check: uploadCheck, last_verify: { ...own, ok: false } }), { column: text, fact: text }, uploadCheck);
    assert.deepEqual(await listAndDetails({ upload_check: uploadCheck, last_verify: { ...own, ok: "true" } }), { column: text, fact: text }, uploadCheck);
    // Its own passed Verify read it back.
    assert.deepEqual(await listAndDetails({ upload_check: uploadCheck, last_verify: own }), { column: "Yes", fact: "Yes" }, uploadCheck);
    assert.deepEqual(await listAndDetails({ upload_check: uploadCheck, verified: false }), { column: "Not yet", fact: "Not yet" }, uploadCheck);
  }
  // Control: a copy this application read back at upload.
  assert.deepEqual(await listAndDetails({ upload_check: "readback", last_verify: null }), { column: "Yes", fact: "Yes" });
});

test("a provider-checksum target test does not read as a readback (#1202)", async () => {
  const api = fakeApi({
    "POST /api/admin/backups/targets/legacy-ftp/test": () => ({ ok: true, detail: "s3 archive destination is writable; the provider's checksum matched the bytes sent (the content was not read back).", duration_ms: 42, provider: "s3", transport_encrypted: true, upload_check: "provider_checksum" }),
  });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.testTarget("legacy-ftp");
  const row = elements.targets.querySelector('tr[data-target-id="legacy-ftp"]').textContent;
  assert.match(row, /Works \(42 ms\)\. Provider checksum checked only; Verify re-reads a backup\./);
});

// The page's real shared transport over a synthetic fetch, so a body with
// ok:false reaches the library exactly as the browser delivers it.
function realTransportApi(replies) {
  const base = fakeApi();
  const transport = loadAdminFunctions(transportNames, {
    navigator: { onLine: true },
    fetch: async (url, options = {}) => {
      const key = `${String(options.method || "GET").toUpperCase()} ${url.split("?")[0]}`;
      if (replies[key]) return jsonResponse(...replies[key](base.data));
      return jsonResponse(200, await base.fetchJson(url, options));
    },
  });
  return { ...base, fetchJson: transport.fetchJson };
}

test("a failed Verify shows its own result through the page's real transport (#1202)", async () => {
  // Exactly what the scheduler and the admin route return (HTTP 200).
  const artifact = (data, verified) => ({ ...data.artifacts[4], verified, upload_check: "size", last_verify: { at: "2026-09-24T12:00:00+00:00", ok: false } });
  const cases = [
    ["unreachable", (data) => [200, { ok: false, artifact: artifact(data, true), detail: "ConnectionRefusedError" }],
      ["Backup did not verify: ConnectionRefusedError", "error"], SIZE_ONLY_TEXT],
    ["mismatch", (data) => [200, { ok: false, artifact: artifact(data, false), detail: "Readback does not match the catalogued size and SHA-256." }],
      ["Backup did not verify: Readback does not match the catalogued size and SHA-256.", "error"], "Not yet"],
  ];
  for (const [name, reply, banner, column] of cases) {
    const api = realTransportApi({
      "POST /api/admin/backups/full-odd/verify": (data) => {
        const [status, body] = reply(data);
        Object.assign(data.artifacts[4], body.artifact);
        return [status, body];
      },
    });
    Object.assign(api.data.artifacts[4], { upload_check: "size", last_verify: null });
    const { elements, library, banners } = mount({ api });
    await library.load();
    await library.actions.verify("full-odd");
    assert.deepEqual(banners.at(-1), banner, name);
    assert.equal(verifiedColumn(elements, "full-odd"), column, name);
  }
  // Controls: a passed Verify, and a refusal status, through the same transport.
  const passed = realTransportApi({
    "POST /api/admin/backups/full-odd/verify": (data) => [200, { ok: true, artifact: data.artifacts[4], detail: "Readback matched the catalogued size and SHA-256." }],
  });
  let mounted = mount({ api: passed });
  await mounted.library.load();
  await mounted.library.actions.verify("full-odd");
  assert.deepEqual(mounted.banners.at(-1), ["Backup verified.", "success"]);
  const missing = realTransportApi({ "POST /api/admin/backups/full-odd/verify": () => [404, { detail: "Artifact not found." }] });
  mounted = mount({ api: missing });
  await mounted.library.load();
  await mounted.library.actions.verify("full-odd");
  assert.equal(mounted.banners.at(-1)[1], "error");
  assert.match(mounted.banners.at(-1)[0], /^Verify failed: Artifact not found\./);
});

function lastCheckFact(elements) {
  const facts = elements.dialog.querySelector("dl").children;
  const index = facts.findIndex((node) => node.tagName === "DT" && node.textContent === "Last check");
  return index === -1 ? null : facts[index + 1].textContent;
}

// #1202: a Verify has three outcomes and the page says which one happened.
// These are the scheduler's real response shapes (VerifyOutcomeTests in
// tests/test_backup_list_assurance.py), delivered through the real transport.
const NOT_READ_BANNER = "This Verify could not read the copy, so nothing was checked: ConnectionRefusedError. Its earlier status is unchanged.";

test("a Verify says whether the copy was read and matched, read and did not match, or could not be read (#1202)", async () => {
  const at = "2026-09-24T12:00:00+00:00";
  const notReadLastCheck = `Could not read the copy, nothing checked: ConnectionRefusedError; earlier status unchanged (T(${at}))`;
  const kinds = [["readback", "Yes"], ["size", SIZE_ONLY_TEXT], ["provider_checksum", PROVIDER_CHECKSUM_TEXT]];
  for (const [uploadCheck, uploadText] of kinds) {
    const outcomes = [
      ["matched", true, true, "Readback matched the catalogued size and SHA-256.",
        ["Backup verified.", "success"], "Yes", `Worked (T(${at}))`],
      ["not_read", false, true, "ConnectionRefusedError",
        [NOT_READ_BANNER, "error"], uploadText, notReadLastCheck],
      ["mismatch", false, false, "Readback does not match the catalogued size and SHA-256.",
        ["Backup did not verify: Readback does not match the catalogued size and SHA-256.", "error"], "Not yet",
        `Failed: Readback does not match the catalogued size and SHA-256. (T(${at}))`],
    ];
    for (const [outcome, ok, verified, detail, banner, column, lastCheck] of outcomes) {
      const name = `${uploadCheck}/${outcome}`;
      const api = realTransportApi({
        "POST /api/admin/backups/full-odd/verify": (data) => {
          Object.assign(data.artifacts[4], { verified, upload_check: uploadCheck, last_verify: { at, ok } });
          return [200, { ok, outcome, artifact: data.artifacts[4], detail }];
        },
        "GET /api/admin/backups/full-odd": (data) => [200, { ...data.artifacts[4], last_verify: { at, ok, outcome, detail } }],
      });
      Object.assign(api.data.artifacts[4], { upload_check: uploadCheck, last_verify: null });
      const { elements, library, banners } = mount({ api });
      await library.load();
      await library.actions.verify("full-odd");
      assert.deepEqual(banners.at(-1), banner, name);
      assert.equal(verifiedColumn(elements, "full-odd"), column, name);
      await library.actions.showDetails("full-odd");
      await settle();
      assert.equal(verifiedFact(elements), column, name);
      assert.equal(lastCheckFact(elements), lastCheck, name);
    }
  }
  // Control: a result from an earlier release carries no outcome and keeps today's wording.
  const api = realTransportApi({
    "POST /api/admin/backups/full-odd/verify": (data) => [200, { ok: false, artifact: data.artifacts[4], detail: "ConnectionRefusedError" }],
    "GET /api/admin/backups/full-odd": (data) => [200, { ...data.artifacts[4], last_verify: { at, ok: false, detail: "ConnectionRefusedError" } }],
  });
  Object.assign(api.data.artifacts[4], { upload_check: "size", last_verify: { at, ok: false } });
  const { elements, library, banners } = mount({ api });
  await library.load();
  await library.actions.verify("full-odd");
  assert.deepEqual(banners.at(-1), ["Backup did not verify: ConnectionRefusedError", "error"]);
  await library.actions.showDetails("full-odd");
  await settle();
  assert.equal(lastCheckFact(elements), `Failed: ConnectionRefusedError (T(${at}))`);
  // A not-read outcome beside a passed result is malformed and never reads as unread.
  const odd = realTransportApi({
    "GET /api/admin/backups/full-odd": (data) => [200, { ...data.artifacts[4], last_verify: { at, ok: true, outcome: "not_read", detail: "" } }],
  });
  const mounted = mount({ api: odd });
  await mounted.library.load();
  await mounted.library.actions.showDetails("full-odd");
  await settle();
  assert.equal(lastCheckFact(mounted.elements), `Worked (T(${at}))`);
});

test("a clean-up refused by its guard shows the refusal through the page's real transport (#1202)", async () => {
  const refusal = "CatalogError: Artifact is the newest copy of its class at this location that passed its last completed check (the upload check or a later Verify), so it is kept.";
  const api = realTransportApi({
    "POST /api/admin/backups/lifecycle/apply": () => [200, { ok: false, deleted: [], already_missing: [], failed: { id: "cfg-old", error: refusal }, not_attempted: [], downgraded: [], kept_after_replan: [] }],
  });
  const { elements, library, banners } = mount({ api });
  await library.load();
  await library.actions.openCleanup();
  await library.actions.applyCleanup();
  assert.deepEqual(banners.at(-1), [`Clean up stopped after 0 of 1: ${refusal}`, "error"]);
  assert.match(elements.dialog.textContent, /Clean up stopped after 0 of 1: CatalogError: Artifact is the newest copy/);
  assert.doesNotMatch(banners.at(-1)[0], /verified copy|Request failed|may already be gone/);
});

test("a long unbroken value wraps inside the details dialog (#1202)", () => {
  const rule = STYLES.match(/\.backup-policy-list dd \{[^}]*\}/)?.[0] || "";
  assert.match(rule, /overflow-wrap:\s*anywhere/);
});

test("restore from the server inspects, shows the existing confirmation, then imports with the receipt", async () => {
  const { elements, library, api, banners, refreshes } = mount();
  await library.load();
  action(rowFor(elements, "full-new"), "restore").click();
  assert.equal(elements.dialog.open, true);
  elements.dialog.querySelector("#backup-restore-passphrase").value = " two words ";
  await library.actions.restoreInspect();

  const inspect = api.calls.find((call) => call.url.endsWith("/restore/inspect"));
  assert.equal(inspect.method, "POST");
  assert.equal(inspect.options.body, undefined, "no archive bytes are uploaded");
  assert.equal(Buffer.from(inspect.options.headers["X-Backup-Passphrase-Base64"], "base64").toString("utf8"), " two words ");
  assert.match(elements.dialog.textContent, /Restoring replaces all current settings, mappings and history with this backup\. Continue\?/);
  assert.equal(api.calls.some((call) => call.url.includes("/restore/import")), false, "nothing is imported before confirming");

  const restoreButton = action(elements.dialog, "restore-import");
  assert.equal(restoreButton.textContent, "Restore");
  await library.actions.restoreImport();
  const imported = api.calls.find((call) => call.url.includes("/restore/import"));
  assert.equal(imported.url, "/api/admin/backups/full-new/restore/import?stop_services=true&restart_services=true");
  assert.equal(imported.options.headers["X-Backup-Expected-Encryption"], "plaintext");
  assert.equal(imported.options.headers["X-Backup-Inspection-Receipt"], "receipt-1");
  assert.equal(imported.options.body, undefined);
  assert.deepEqual(banners.at(-1), ["Backup restored.", "success"]);
  assert.equal(refreshes.length, 1);
  assert.equal(elements.dialog.querySelector("#backup-restore-passphrase").value, "");
});

test("restore refuses an inspection without an observed mode and receipt", async () => {
  const api = fakeApi({ "POST /api/admin/backups/full-new/restore/inspect": () => ({ ok: true, encryption_mode: "unknown" }) });
  const { elements, library } = mount({ api });
  await library.load();
  library.actions.openRestore("full-new");
  await library.actions.restoreInspect();
  assert.match(elements.dialog.textContent, /Check failed: The backup check did not finish/);
  assert.equal(action(elements.dialog, "restore-import"), undefined);
  await library.actions.restoreImport();
  assert.equal(api.calls.some((call) => call.url.includes("/restore/import")), false);
});

test("restore is not offered for a non-restorable copy even if called directly", async () => {
  const { elements, library } = mount();
  await library.load();
  library.actions.openRestore("full-part");
  assert.equal(elements.dialog.open, false);
});

test("clean up shows the dry-run plan and applies exactly its token", async () => {
  const { elements, library, api, banners } = mount();
  await library.load();
  await library.actions.openCleanup();
  const items = elements.dialog.querySelectorAll("li[data-artifact-id]");
  assert.deepEqual(items.map((item) => item.dataset.artifactId), ["cfg-old", "full-part"]);
  const text = elements.dialog.textContent;
  assert.match(text, /These 2 copies will be deleted/);
  assert.match(text, /only 1 copies are kept and this is number 2/);
  assert.match(text, /never verified, and older than 1 day \(it is 2 days old\)/);
  // The plan knows only the catalogue's check result, which can be a size
  // check at upload, so it promises no more than that (#1202).
  assert.match(text, /These 2 copies will be deleted\. Kept copies and the newest copy in each place that passed its last completed check are never deleted\./);
  assert.match(text, /1 more is over a limit but kept, because it is the newest copy in its place that passed its last completed check\. That check may have been by size only; the Verified column says which\./);
  assert.doesNotMatch(text, /newest verified copy/);
  assert.equal(action(elements.dialog, "cleanup-apply").textContent, "Delete 2");
  await library.actions.applyCleanup();
  const apply = api.calls.find((call) => call.url.endsWith("/lifecycle/apply"));
  assert.deepEqual(JSON.parse(apply.options.body), { plan_token: "plan-token-1" });
  assert.deepEqual(banners.at(-1), ["Deleted 2 copies.", "success"]);
  await library.actions.applyCleanup();
  assert.equal(api.calls.filter((call) => call.url.endsWith("/lifecycle/apply")).length, 1, "a used plan is not re-applied");
});

test("clean up reports a partial run and a stale plan plainly", async () => {
  const partial = fakeApi({ "POST /api/admin/backups/lifecycle/apply": () => ({ ok: false, deleted: ["cfg-old"], already_missing: [], failed: { id: "full-part", error: "permission denied at /srv/app/backups/x" }, not_attempted: [] }) });
  let mounted = mount({ api: partial });
  await mounted.library.load();
  await mounted.library.actions.openCleanup();
  await mounted.library.actions.applyCleanup();
  assert.match(mounted.elements.dialog.textContent, /Clean up stopped after 1 of 2: permission denied at \[path\]/);

  const stale = fakeApi({ "POST /api/admin/backups/lifecycle/apply": () => Object.assign(new Error("gone"), { status: 409 }) });
  mounted = mount({ api: stale });
  await mounted.library.load();
  await mounted.library.actions.openCleanup();
  await mounted.library.actions.applyCleanup();
  assert.match(mounted.elements.dialog.textContent, /The list changed since this plan was made, so nothing was deleted/);
});

test("clean up that kept copies after a failed keeper check says so, not plain success (#1188)", async () => {
  const api = fakeApi({ "POST /api/admin/backups/lifecycle/apply": () => ({
    ok: true, deleted: [], already_missing: [], failed: null, not_attempted: [],
    downgraded: ["full-new"], kept_after_replan: ["cfg-old", "full-part"] }) });
  const { elements, library, banners } = mount({ api });
  await library.load();
  await library.actions.openCleanup();
  assert.match(elements.dialog.textContent, /These 2 copies will be deleted/);
  await library.actions.applyCleanup();
  const text = "Deleted 0 copies. 1 of the copies it kept failed the size and SHA-256 check and was marked unverified. "
    + "2 planned deletions were not carried out, because the copies that would have stayed could not be confirmed good. "
    + "Run Verify on the copy marked unverified: if it passes, clean up again; if not, back up again to replace it.";
  assert.ok(elements.dialog.textContent.includes(text), elements.dialog.textContent);
  assert.deepEqual(banners.at(-1), [text, "error"]);
  // Once a result is shown, the plan's introduction no longer promises deletions.
  assert.doesNotMatch(elements.dialog.textContent, /will be deleted/);
  assert.match(elements.dialog.textContent, /These 2 copies were in the clean-up plan\./);
  assert.doesNotMatch(elements.dialog.textContent, /The result is below/);
  // The result carries the same problem tone as the banner, not the muted note colour.
  const result = elements.dialog.querySelector(".backup-dialog-result");
  assert.match(result.className, /(^|\s)is-bad(\s|$)/);
  assert.match(STYLES, /\.backup-dialog-result\.is-bad[\s,{]/, "the problem tone has a style");
});

test("a plain clean-up result keeps the muted tone and the past-tense introduction (#1188)", async () => {
  const { elements, library, banners } = mount();
  await library.load();
  await library.actions.openCleanup();
  await library.actions.applyCleanup();
  assert.deepEqual(banners.at(-1), ["Deleted 2 copies.", "success"]);
  assert.doesNotMatch(elements.dialog.querySelector(".backup-dialog-result").className, /is-bad/);
  assert.doesNotMatch(elements.dialog.textContent, /will be deleted/);
  assert.match(elements.dialog.textContent, /These 2 copies were in the clean-up plan\./);
  assert.doesNotMatch(elements.dialog.textContent, /The result is below/);
});

test("several kept copies that failed the check read in the plural, with no copy/copies shortcut (#1188)", async () => {
  const api = fakeApi({ "POST /api/admin/backups/lifecycle/apply": () => ({
    ok: true, deleted: ["cfg-old"], already_missing: [], failed: null, not_attempted: [],
    downgraded: ["k1", "k2", "k3"], kept_after_replan: ["full-part"] }) });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.openCleanup();
  await library.actions.applyCleanup();
  const text = "Deleted 1 copy. 3 of the copies it kept failed the size and SHA-256 check and were marked unverified. "
    + "1 planned deletion was not carried out, because the copies that would have stayed could not be confirmed good. "
    + "Run Verify on the copies marked unverified: if they pass, clean up again; if not, back up again to replace them.";
  assert.ok(elements.dialog.textContent.includes(text), elements.dialog.textContent);
  assert.doesNotMatch(LIBRARY_SOURCE, /copy\/copies|deletion\/deletions/);
});

// #1188: the one thing the page says when no usable answer to a clean-up came
// back. It matches the route's detail.
const CLEANUP_STATE_UNKNOWN = "No answer came back for this clean-up. It may have run, may still be running, or may not have "
  + "started. Reload this page in a few minutes and check Last clean-up before starting another.";

function resultNode(elements) {
  return elements.dialog.querySelector(".backup-dialog-result");
}

function listReads(api) {
  return api.calls.filter(({ method, url }) => method === "GET" && url === "/api/admin/backups").length;
}

test("a clean-up with no answer says so in the warning tone, reads nothing more, and no later read changes it (#1188)", async () => {
  const api = fakeApi({ "POST /api/admin/backups/lifecycle/apply": () => ({ state: "cleanup_state_unknown", detail: "ignored server text" }) });
  // A clean-up shows as running at the first read, so the ordinary running poll is on.
  api.data.running = { backup_class: "lifecycle", started_at: "2026-09-24T12:00:00Z" };
  const timers = [];
  const { elements, library, banners } = mount({ api, deps: { setTimeout: (callback) => timers.push(callback), isVisible: () => true } });
  await library.load();
  assert.match(elements.status.textContent, /A clean-up is running\./);
  assert.equal(timers.length, 1, "the ordinary running poll");
  await library.actions.openCleanup();
  const reads = listReads(api);
  await library.actions.applyCleanup();
  assert.ok(elements.dialog.textContent.includes(CLEANUP_STATE_UNKNOWN), elements.dialog.textContent);
  assert.deepEqual(banners.at(-1), [CLEANUP_STATE_UNKNOWN, "warning"]);
  assert.match(resultNode(elements).className, /(^|\s)is-warning(\s|$)/);
  assert.match(STYLES, /\.backup-dialog-result\.is-warning[\s,{]/, "the warning tone has a style");
  assert.equal(listReads(api), reads, "no list or status read after the lost answer");
  assert.equal(timers.length, 1, "no poll of its own");
  // The ordinary poll and a manual refresh show the newer Last clean-up line, and leave the message alone.
  const bannerCount = banners.length;
  api.data.running = null;
  api.data.grooming = { at: "2026-09-24T12:05:00Z", ok: true, deleted: 2, detail: null, failed_locations: {} };
  timers.shift()();
  await settle();
  await settle();
  await library.load();
  assert.equal(banners.length, bannerCount, "no read replaces the message");
  assert.equal(resultNode(elements).textContent, CLEANUP_STATE_UNKNOWN);
  assert.match(resultNode(elements).className, /(^|\s)is-warning(\s|$)/);
  assert.match(elements.status.textContent, /Last clean-up: T\(2026-09-24T12:05:00Z\), 2 removed\./);
  assert.doesNotMatch(elements.status.textContent, /A clean-up is running/);
  assert.equal(timers.length, 0);
});

test("an apply answer with no result is not known, never a result (#1188)", async () => {
  for (const answer of [{}, { state: "still_running", detail: "x" }, { state: "cleanup_running" }, { state: "cleanup_not_running" }, { state: "something-new" }, { ok: true }]) {
    const api = fakeApi({ "POST /api/admin/backups/lifecycle/apply": () => answer });
    const { elements, library, banners } = mount({ api });
    await library.load();
    await library.actions.openCleanup();
    await library.actions.applyCleanup();
    assert.deepEqual(banners.at(-1), [CLEANUP_STATE_UNKNOWN, "warning"], JSON.stringify(answer));
    assert.equal(resultNode(elements).textContent, CLEANUP_STATE_UNKNOWN, JSON.stringify(answer));
  }
  assert.doesNotMatch(LIBRARY_SOURCE, /cleanup_running|no longer running|cleanupWatch|bannerRevision/);
});

test("when the page's own timer ends the apply wait, the clean-up is not known, not failed (#1188)", async () => {
  const timers = new Map();
  let timerId = 0;
  let applied = 0;
  const transport = loadAdminFunctions(transportNames, {
    navigator: { onLine: true },
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms }); return id; },
    clearTimeout: (id) => timers.delete(id),
    fetch: async (url) => {
      if (url.endsWith("/lifecycle/apply")) {
        applied += 1;
        return new Promise(() => {}); // the answer never comes
      }
      if (url.endsWith("/lifecycle/plan")) {
        return jsonResponse(200, { plan_token: "plan-token-1", items: [
          { id: "cfg-old", location: "local", backup_class: "config", reason: "x", kind: "retention" }] });
      }
      return jsonResponse(200, syntheticLibrary());
    },
  });
  const { elements, library, banners } = mount({ deps: { fetchJson: transport.fetchJson } });
  await library.load();
  await library.actions.openCleanup();
  const applying = library.actions.applyCleanup();
  await settle();
  assert.equal(applied, 1);
  const pageTimer = [...timers.values()].find(({ ms }) => ms === 15 * 60 * 1000);
  assert.ok(pageTimer, "the apply has the page's 15-minute timer");
  pageTimer.callback();
  await applying;
  assert.deepEqual(banners.at(-1), [CLEANUP_STATE_UNKNOWN, "warning"]);
  assert.equal(resultNode(elements).textContent, CLEANUP_STATE_UNKNOWN);
  assert.doesNotMatch(elements.dialog.textContent, /failed|not available|isn't running/);
});

test("a lost connection or an undecided HTTP error after the apply went out is not known, never failed (#1188)", async () => {
  const answers = {
    "lost connection": () => Promise.reject(new TypeError("Failed to fetch")),
    "HTTP 500": () => jsonResponse(500, { detail: "Internal Server Error" }),
    "HTTP 502": () => jsonResponse(502, { detail: "Bad Gateway" }),
    "HTTP 503": () => jsonResponse(503, { detail: "The backup scheduler service is not available." }),
    "HTTP 504": () => jsonResponse(504, { detail: "Gateway Timeout" }),
  };
  for (const [name, answer] of Object.entries(answers)) {
    let lists = 0;
    const transport = loadAdminFunctions(transportNames, {
      navigator: { onLine: true },
      setTimeout: () => 0,
      clearTimeout: () => {},
      fetch: async (url) => {
        if (url.endsWith("/lifecycle/apply")) return answer();
        if (url.endsWith("/lifecycle/plan")) {
          return jsonResponse(200, { plan_token: "plan-token-1", items: [
            { id: "cfg-old", location: "local", backup_class: "config", reason: "x", kind: "retention" }] });
        }
        lists += 1;
        return jsonResponse(200, syntheticLibrary());
      },
    });
    const { elements, library, banners } = mount({ deps: { fetchJson: transport.fetchJson } });
    await library.load();
    await library.actions.openCleanup();
    const before = lists;
    await library.actions.applyCleanup();
    assert.deepEqual(banners.at(-1), [CLEANUP_STATE_UNKNOWN, "warning"], name);
    assert.equal(resultNode(elements).textContent, CLEANUP_STATE_UNKNOWN, name);
    assert.doesNotMatch(elements.dialog.textContent, /Clean up failed|isn't running/, name);
    assert.equal(lists, before, `${name}: no read after the lost answer`);
  }
  // A decided refusal from the admin service still says the clean-up failed.
  const api = fakeApi({ "POST /api/admin/backups/lifecycle/apply": () => Object.assign(new Error("Forbidden"), { status: 403, adminOutcome: "error" }) });
  const { library, banners } = mount({ api });
  await library.load();
  await library.actions.openCleanup();
  await library.actions.applyCleanup();
  assert.match(banners.at(-1)[0], /^Clean up failed: Forbidden/);
  assert.equal(banners.at(-1)[1], "error");
});

const CLEANUP_NOT_STARTED = "Clean up did not start: The backup scheduler service is not available.";
const NOT_SENT_BODY = {
  ok: false,
  state: "cleanup_not_sent",
  detail: "The backup scheduler service is not available.",
  request_id: "0123456789abcdef0123456789abcdef",
};

async function applyThroughTransport(answer) {
  let lists = 0;
  let applies = 0;
  const transport = loadAdminFunctions(transportNames, {
    navigator: { onLine: true },
    setTimeout: () => 0,
    clearTimeout: () => {},
    fetch: async (url) => {
      if (url.endsWith("/lifecycle/apply")) {
        applies += 1;
        return answer();
      }
      if (url.endsWith("/lifecycle/plan")) {
        return jsonResponse(200, { plan_token: "plan-token-1", items: [
          { id: "cfg-old", location: "local", backup_class: "config", reason: "x", kind: "retention" }] });
      }
      lists += 1;
      return jsonResponse(200, syntheticLibrary());
    },
  });
  const { elements, library, banners } = mount({ deps: { fetchJson: transport.fetchJson } });
  await library.load();
  await library.actions.openCleanup();
  const before = lists;
  await library.actions.applyCleanup();
  return { elements, banners, extraReads: lists - before, applies };
}

const EMPTY_RESULT = { deleted: [], already_missing: [], failed: null, not_attempted: [], downgraded: [], kept_after_replan: [] };

test("a stopped or refused clean-up answered with ok: false shows its result through the page's own transport (#1188)", async () => {
  // The scheduler answers a run that stopped, or that a keeper check refused,
  // with HTTP 200 and ok: false. The admin transport treats ok: false as a
  // failed request unless the caller accepts the result.
  const reason = "Kept copy could not be checked (keeper-check-budget-exhausted).";
  const cases = {
    "keeper check refused after three damaged copies": [
      { ok: false, deleted: [], already_missing: [], failed: { id: "cfg-old", error: reason }, not_attempted: [],
        downgraded: ["full-a", "full-b", "full-c"], kept_after_replan: [] },
      `Clean up stopped after 0 of 1: ${reason} `
        + "3 of the copies it kept failed the size and SHA-256 check and were marked unverified. "
        + "Run Verify on the copies marked unverified, or back up again to replace them.",
    ],
    "a deletion failed": [
      { ok: false, deleted: [], already_missing: [], failed: { id: "cfg-old", error: "permission denied" }, not_attempted: [],
        downgraded: [], kept_after_replan: [] },
      "Clean up stopped after 0 of 1: permission denied",
    ],
    "nothing attempted": [
      { ok: false, deleted: [], already_missing: [], failed: null, not_attempted: ["cfg-old"], downgraded: [], kept_after_replan: [] },
      "Clean up stopped after 0 of 1.",
    ],
  };
  for (const [name, [answer, text]] of Object.entries(cases)) {
    const { elements, banners, applies } = await applyThroughTransport(() => jsonResponse(200, answer));
    assert.equal(applies, 1, name);
    assert.equal(resultNode(elements).textContent, text, name);
    assert.deepEqual(banners.at(-1), [text, "error"], name);
    assert.doesNotMatch(elements.dialog.textContent, /Clean up failed|Request failed with 200/, name);
  }
});

test("an ok: false answer that is not a clean-up result is still a failed request (#1188)", async () => {
  // Control: only the scheduler's own result shape is accepted.
  const answers = {
    "no result": { ok: false, detail: "synthetic refusal" },
    "a result list that is not a list": { ok: false, deleted: "cfg-old" },
    "a named state beside a result": { ok: false, state: "cleanup_state_unknown", deleted: [] },
    // One list is not the result: every list must be there and well formed, and so must a failed entry when one is given.
    "only the deleted list": { ok: false, deleted: [] },
    "a deleted entry that is not an id": { ...EMPTY_RESULT, ok: false, deleted: [null] },
    "a missing list": { ok: false, deleted: [], already_missing: [], failed: null, not_attempted: [], downgraded: [] },
    "a list entry that is not an id": { ...EMPTY_RESULT, ok: false, not_attempted: [{ id: "cfg-old" }] },
    "a failed entry without its error": { ...EMPTY_RESULT, ok: false, failed: { id: "cfg-old" } },
    "a failed entry that is text": { ...EMPTY_RESULT, ok: false, failed: "cfg-old" },
    "a named state beside a whole result": { ...EMPTY_RESULT, ok: false, state: "cleanup_state_unknown" },
  };
  for (const [name, answer] of Object.entries(answers)) {
    const { elements, banners } = await applyThroughTransport(() => jsonResponse(200, answer));
    assert.match(resultNode(elements).textContent, /^Clean up failed: /, name);
    assert.doesNotMatch(elements.dialog.textContent, /Clean up stopped|Deleted \d/, name);
    assert.equal(banners.at(-1)[1], "error", name);
  }
});

test("a clean-up that reports itself incomplete never reads as a plain success (#1188)", async () => {
  // The scheduler does not send this (incomplete means a failed or unattempted
  // deletion), but the page must not turn "not complete" into "Deleted 0 copies."
  const { elements, banners } = await applyThroughTransport(() => jsonResponse(200, { ...EMPTY_RESULT, ok: false }));
  assert.equal(resultNode(elements).textContent, "Clean up stopped after 0 of 0.");
  assert.deepEqual(banners.at(-1), ["Clean up stopped after 0 of 0.", "error"]);
  // Control: the same lists with ok: true are a finished clean-up.
  const done = await applyThroughTransport(() => jsonResponse(200, { ...EMPTY_RESULT, ok: true }));
  assert.equal(resultNode(done.elements).textContent, "Deleted 0 copies.");
  assert.deepEqual(done.banners.at(-1), ["Deleted 0 copies.", "success"]);
});

test("a clean-up the admin service never sent says it did not start, and reads nothing more (#1188)", async () => {
  const { elements, banners, extraReads, applies } = await applyThroughTransport(() => jsonResponse(503, NOT_SENT_BODY));
  assert.equal(applies, 1);
  assert.deepEqual(banners.at(-1), [CLEANUP_NOT_STARTED, "info"]);
  assert.equal(resultNode(elements).textContent, CLEANUP_NOT_STARTED);
  assert.doesNotMatch(resultNode(elements).className, /is-warning|is-bad/, "the busy refusal's plain tone");
  assert.doesNotMatch(elements.dialog.textContent, /No answer came back|may have run|Clean up failed/);
  assert.equal(extraReads, 0, "no list or status read after the refusal");
});

test("only the admin's not-sent 503 says did not start; a gateway's 5xx or a lost send stays not known (#1188)", async () => {
  const answers = {
    "plain HTTP 503": () => jsonResponse(503, { ok: false, detail: NOT_SENT_BODY.detail, request_id: NOT_SENT_BODY.request_id }),
    "HTTP 503 with another state": () => jsonResponse(503, { ...NOT_SENT_BODY, state: "cleanup_state_unknown" }),
    "HTTP 500 with the state": () => jsonResponse(500, NOT_SENT_BODY),
    "HTTP 502 with the state": () => jsonResponse(502, NOT_SENT_BODY),
    "HTTP 504 with the state": () => jsonResponse(504, NOT_SENT_BODY),
    "send failed": () => Promise.reject(new TypeError("Failed to fetch")),
    "202 with the state": () => jsonResponse(202, { state: "cleanup_not_sent", detail: NOT_SENT_BODY.detail }),
  };
  for (const [name, answer] of Object.entries(answers)) {
    const { elements, banners, extraReads } = await applyThroughTransport(answer);
    assert.deepEqual(banners.at(-1), [CLEANUP_STATE_UNKNOWN, "warning"], name);
    assert.equal(resultNode(elements).textContent, CLEANUP_STATE_UNKNOWN, name);
    assert.doesNotMatch(elements.dialog.textContent, /did not start|Clean up failed/, name);
    assert.equal(extraReads, 0, `${name}: no read after the lost answer`);
  }
});

test("the clean-up result sits directly under the plan's introduction, which no longer points below (#1188)", async () => {
  const outcomes = {
    success: () => ({ ok: true, deleted: ["cfg-old", "full-part"], already_missing: [], failed: null, not_attempted: [] }),
    "not known": () => ({ state: "cleanup_state_unknown" }),
    busy: () => Object.assign(new Error("A backup or grooming run is already in progress."), { status: 409 }),
    stale: () => Object.assign(new Error("gone"), { status: 409 }),
  };
  for (const [name, answer] of Object.entries(outcomes)) {
    const api = fakeApi({ "POST /api/admin/backups/lifecycle/apply": answer });
    const { elements, library } = mount({ api });
    await library.load();
    await library.actions.openCleanup();
    await library.actions.applyCleanup();
    const body = elements.dialog.querySelector(".backup-dialog-body");
    const [first, second, third] = body.children;
    assert.equal(first.className, "backup-cleanup-intro", name);
    assert.equal(first.textContent, "These 2 copies were in the clean-up plan.", name);
    assert.match(second.className, /backup-dialog-result/, name);
    assert.match(third.className, /backup-cleanup-list/, name);
    assert.equal(elements.dialog.querySelectorAll(".backup-dialog-result").length, 1, name);
    assert.doesNotMatch(elements.dialog.textContent, /The result is below/, name);
  }
});

test("a one-copy plan reads in the singular before and after Delete (#1188)", async () => {
  const api = fakeApi({ "GET /api/admin/backups/lifecycle/plan": () => ({ plan_token: "t1", items: [
    { id: "cfg-old", location: "local", backup_class: "config", reason: "x", kind: "retention" }] }),
  "POST /api/admin/backups/lifecycle/apply": () => ({ ok: true, deleted: ["cfg-old"], already_missing: [], failed: null, not_attempted: [] }) });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.openCleanup();
  const intro = elements.dialog.querySelector(".backup-cleanup-intro");
  assert.match(intro.textContent, /^This copy will be deleted\. /);
  assert.doesNotMatch(elements.dialog.textContent, /These 1 cop/);
  await library.actions.applyCleanup();
  assert.equal(elements.dialog.querySelector(".backup-cleanup-intro").textContent, "This copy was in the clean-up plan.");
});

test("a long clean-up plan scrolls inside its list, so the introduction, result and buttons stay in view (#1188)", async () => {
  const items = Array.from({ length: 14 }, (_, index) => ({ id: `copy-${index}`, location: "local", backup_class: "full", reason: "x", kind: "retention" }));
  const api = fakeApi({ "GET /api/admin/backups/lifecycle/plan": () => ({ plan_token: "t14", items }),
    "POST /api/admin/backups/lifecycle/apply": () => ({ ok: true, deleted: items.map(({ id }) => id), already_missing: [], failed: null, not_attempted: [] }) });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.openCleanup();
  await library.actions.applyCleanup();
  // The structure the styles rely on: the list is a direct child of the dialog
  // body, after the introduction and the result; the buttons sit outside the body.
  const dialogChildren = elements.dialog.children.map((node) => node.className);
  const body = elements.dialog.querySelector(".backup-dialog-body");
  assert.deepEqual(body.children.slice(0, 3).map((node) => node.className.split(" ").find((name) => name.startsWith("backup-"))),
    ["backup-cleanup-intro", "backup-dialog-result", "backup-change-list"]);
  assert.match(body.children[2].className, /(^|\s)backup-cleanup-list(\s|$)/);
  assert.equal(body.children[2].children.length, 14);
  assert.ok(dialogChildren.some((name) => /backup-dialog-actions/.test(name)), dialogChildren.join());
  assert.equal(body.querySelector(".backup-dialog-actions"), null);
  const rule = (selector) => {
    const start = STYLES.indexOf(`${selector} {`);
    assert.ok(start >= 0, `missing style ${selector}`);
    return STYLES.slice(start, STYLES.indexOf("}", start));
  };
  assert.match(rule(".backup-dialog[open]:has(.backup-cleanup-list)"), /display:\s*flex;[\s\S]*flex-direction:\s*column;/);
  assert.match(rule(".backup-dialog-body:has(> .backup-cleanup-list)"), /display:\s*flex;[\s\S]*min-height:\s*0;/);
  assert.match(rule(".backup-cleanup-list"), /overflow-y:[ ]*auto;/);
});

test("a clean-up applied while another run holds the scheduler says why nothing happened (#1188)", async () => {
  const api = fakeApi({ "POST /api/admin/backups/lifecycle/apply": () => Object.assign(
    new Error("A backup or grooming run is already in progress."), { status: 409 }) });
  const { elements, library, banners } = mount({ api });
  await library.load();
  await library.actions.openCleanup();
  await library.actions.applyCleanup();
  // True whether the running job is a backup or a clean-up: no promise about which line shows its result.
  const text = "A backup or clean-up is already running on the backup service, so this clean-up did not start "
    + "and deleted nothing. Open Clean up again after it finishes.";
  assert.ok(elements.dialog.textContent.includes(text), elements.dialog.textContent);
  assert.doesNotMatch(elements.dialog.textContent, /The list changed since this plan was made/);
  assert.doesNotMatch(LIBRARY_SOURCE, /When the running one finishes, its result shows in the Last clean-up line/);
  assert.deepEqual(banners.at(-1), [text, "info"]);
});

test("the admin banner has a warning tone (#1188)", () => {
  const classes = new Set(["is-error", "is-success"]);
  const banner = { textContent: "", classList: {
    add: (...names) => names.forEach((name) => classes.add(name)),
    remove: (...names) => names.forEach((name) => classes.delete(name)),
  } };
  const context = loadAdminFunctions(["setBanner"], { state: {}, elements: { banner } });
  context.setBanner("Not known.", "warning");
  assert.deepEqual([...classes], ["is-warning"]);
  context.setBanner("Fine.", "success");
  assert.deepEqual([...classes], ["is-success"]);
  assert.match(STYLES, /\.status-banner\.is-warning\s*\{/);
});

test("the status line shows a clean-up that finished but marked copies unverified (#1188)", async () => {
  const api = fakeApi();
  api.data.grooming = { at: "2026-09-24T12:00:00Z", ok: true, deleted: 0,
    detail: "1 of the copies it kept failed the size and SHA-256 check and was marked unverified.", failed_locations: {} };
  const { elements, library } = mount({ api });
  await library.load();
  assert.equal(elements.status.querySelector("span.backup-run-status.is-ok"), null);
  assert.equal(elements.status.querySelector("span.backup-run-status.is-bad").textContent,
    "Last clean-up: T(2026-09-24T12:00:00Z), 0 removed. 1 of the copies it kept failed the size and SHA-256 check and was marked unverified.");
});

test("clean up with nothing to do, or without a token, never applies", async () => {
  const empty = fakeApi({ "GET /api/admin/backups/lifecycle/plan": () => ({ plan_token: "t", items: [] }) });
  let mounted = mount({ api: empty });
  await mounted.library.load();
  await mounted.library.actions.openCleanup();
  assert.match(mounted.elements.dialog.textContent, /Nothing to clean up/);
  assert.equal(action(mounted.elements.dialog, "cleanup-apply"), undefined);

  const tokenless = fakeApi({ "GET /api/admin/backups/lifecycle/plan": () => [{ id: "cfg-old", location: "local", reason: "x" }] });
  mounted = mount({ api: tokenless });
  await mounted.library.load();
  await mounted.library.actions.openCleanup();
  assert.match(mounted.elements.dialog.textContent, /without a token/);
  await mounted.library.actions.applyCleanup();
  assert.equal(tokenless.calls.some((call) => call.url.endsWith("/lifecycle/apply")), false);
});

test("a missing backend reads as plain words, not an error dump", async () => {
  const api = fakeApi({ "GET /api/admin/backups": () => Object.assign(new Error("Not Found"), { status: 404 }) });
  const { elements, library } = mount({ api });
  await library.load();
  assert.equal(elements.status.textContent, "This admin version has no backup list.");
  assert.equal(elements.cleanupButton.disabled, true);
});

test("a stopped admin session disables every library action", async () => {
  const { elements, library } = mount({ stopped: true });
  await library.load();
  const buttons = elements.root.querySelectorAll("button");
  assert.ok(buttons.length > 5);
  assert.ok(buttons.every((node) => node.disabled));
});

test("admin.js mounts the library as a Backups view and base.html loads it first", () => {
  assert.match(TEMPLATE, /data-admin-view-button="backups"[^>]*>Backups</);
  assert.match(TEMPLATE, /data-admin-view-panel="backups"/);
  assert.match(TEMPLATE, /<dialog id="backup-library-dialog"/);
  assert.ok(BASE.indexOf("admin_backups.js") > 0 && BASE.indexOf("admin_backups.js") < BASE.indexOf("path='admin.js'"));
  assert.match(ADMIN_SOURCE, /return value === "builder" \|\| value === "backups" \? value : "operations";/);
  assert.match(ADMIN_SOURCE, /window\.AdminBackupLibrary\?\.createBackupLibrary\(/);
  assert.match(ADMIN_SOURCE, /describeBackupRestoreConfirmation,\n/);
  // the upload path for off-server files stays
  assert.match(TEMPLATE, /id="backup-import-file"/);
  assert.match(ADMIN_SOURCE, /\/api\/admin\/backup\/inspect/);
});

test("library copy stays plain and the module has no framework or innerHTML", () => {
  const panel = TEMPLATE.slice(TEMPLATE.indexOf('id="backup-library"'), TEMPLATE.indexOf('data-admin-view-panel="builder"'));
  const words = panel.replace(/<code>[^<]*<\/code>/g, "").replace(/<[^>]+>/g, " ");
  for (const word of [/sidecar/i, /artifact/i, /catalog/i, /lifecycle/i, /grooming/i, /tombstone/i]) {
    assert.doesNotMatch(words, word);
  }
  // Operator-facing literals: text: "..." and template strings that start with a capital.
  const strings = (LIBRARY_SOURCE.match(/text: "[^"]*"|`[A-Z][^`]*`/g) || [])
    .join("\n")
    .replace(/\$\{[^}]*\}/g, "");
  for (const word of [/artifact/i, /tombstone/i, /grooming/i, /lifecycle/i, /catalog/i]) {
    assert.doesNotMatch(strings, word);
  }
  assert.doesNotMatch(LIBRARY_SOURCE, /innerHTML|insertAdjacentHTML|\beval\(|new Function/);
  assert.doesNotMatch(LIBRARY_SOURCE.replace('require("./admin_backup_model.js")', ""), /require\(|import /);
});

// Restore acceptance uses the actual controller and complete production transport.
function restoreFixture({ imported = () => jsonResponse(200, restoreResult()), refreshError = false, listError = false, signal } = {}) {
  const calls = [];
  const timers = new Map();
  const navigator = { onLine: true };
  let timerId = 0;
  let inspections = 0;
  let refreshes = 0;
  let mounted;
  const receiptAtDispatch = [];
  const transport = loadAdminFunctions([
    ...transportNames, ...restoreNames,
    "describeMaintenanceOutcome", "maintenanceKeys", "describeServices", "serviceName",
  ], {
    navigator,
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms }); return id; },
    clearTimeout: (id) => timers.delete(id),
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("/restore/inspect")) {
        return jsonResponse(200, { ok: true, encryption_mode: "plaintext", inspection_receipt: `receipt-${++inspections}` });
      }
      if (url.includes("/restore/import")) {
        receiptAtDispatch.push(mounted.library.state.restoreInspection);
        return imported(url, options);
      }
      assert.equal(url, "/api/admin/backups");
      return listError && refreshes ? jsonResponse(503, { detail: "Synthetic list refresh failed" }) : jsonResponse(200, syntheticLibrary());
    },
  });
  // Caller-signal injection exercises cancellation without replacing transport.
  const withSignal = (fn) => (url, options = {}) => fn(url, { ...options, signal });
  mounted = mount({ deps: {
    fetchJson: withSignal(transport.fetchJson),
    fetchBackupRestore: withSignal(transport.fetchBackupRestore),
    describeBackupRestoreFailure: transport.describeBackupRestoreFailure,
    describeMaintenanceOutcome: transport.describeMaintenanceOutcome,
    renderMaintenanceResult: (node, lead, outcome) => { node.textContent = `${lead}${outcome.ok ? "." : `, but ${outcome.sentence}`}`; },
    refreshAdminState: async () => { refreshes++; if (refreshError) throw new Error("Synthetic refresh failed"); },
  } });
  return { ...mounted, calls, timers, navigator, receiptAtDispatch,
    get refreshCount() { return refreshes; },
    applies: () => calls.filter(({ url }) => url.includes("/restore/import")),
    expire() { for (const { callback } of [...timers.values()]) callback(); },
    async prepare(id = "full-new") {
      if (!mounted.library.state.data) await mounted.library.load();
      mounted.library.actions.openRestore(id);
      mounted.elements.dialog.querySelector("#backup-restore-passphrase").value = " synthetic phrase ";
      await mounted.library.actions.restoreInspect();
    },
  };
}

for (const [name, imported] of [
  ["transport loss", () => { throw new TypeError("Failed to fetch"); }],
  ["bad JSON", () => ({ ...jsonResponse(200), json: async () => { throw new SyntaxError("private raw body"); } })],
  ["wrong-shaped 2xx", () => jsonResponse(200, { unexpected: "value" })],
  ["ok-only 2xx", () => jsonResponse(200, { ok: true })],
  ["refused 2xx", () => jsonResponse(200, { ok: false, detail: "No decided restore result" })],
  ["undecided 5xx", () => jsonResponse(503, { detail: "Service unavailable" })],
  ["body deadline", () => ({ ...jsonResponse(200), json: () => new Promise(() => {}) })],
  ["body abort", () => ({ ...jsonResponse(200), json: () => new Promise(() => {}) })],
]) {
  test(`library restore preserves unknown outcome after ${name}`, async () => {
    const controller = new AbortController();
    const fixture = restoreFixture({ imported, signal: controller.signal });
    await fixture.prepare();
    let done = false;
    const pending = fixture.library.actions.restoreImport().then(() => { done = true; });
    await settle();
    if (name === "body deadline") {
      assert.deepEqual([...fixture.timers.values()].map(({ ms }) => ms), [15 * 60 * 1000]);
      fixture.expire();
    }
    if (name === "body abort") controller.abort();
    await settle();
    assert.equal(done, true, "body readers ignoring abort must not retain the restore owner");
    await pending;
    const message = fixture.elements.dialog.querySelector(".backup-dialog-result").textContent;
    assert.match(message, /unknown whether the restore/i);
    assert.match(message, /check.*before.*restor/i);
    assert.match(message, /new inspection receipt/i);
    assert.doesNotMatch(message, /Import failed|Restore failed|private raw body|scheduler isn't running/i);
    assert.equal(fixture.banners.at(-1)[0], message);
    assert.equal(fixture.banners.at(-1)[1], "error");
    assert.equal(fixture.applies().length, 1);
    assert.equal(fixture.refreshCount, 0);
    assert.equal(fixture.library.state.restoreInspection, null);
    assert.deepEqual(fixture.receiptAtDispatch, [null], "receipt retired before transport dispatch");
    assert.equal(fixture.elements.dialog.querySelector("#backup-restore-passphrase").value, " synthetic phrase ");
    assert.equal(action(fixture.elements.dialog, "restore-import"), undefined);
    assert.ok(action(fixture.elements.dialog, "restore-inspect"), "manual retry starts with inspection");
    await fixture.library.actions.restoreImport();
    assert.equal(fixture.applies().length, 1, "no direct replay with the consumed receipt");
    assert.equal(fixture.timers.size, 0);
    assert.equal(fixture.library.state.pending.size, 0);
  });
}

for (const status of [400, 403, 409, 422]) {
  test(`library restore preserves definite HTTP ${status} refusal and scrubs detail`, async () => {
    const fixture = restoreFixture({ imported: () => jsonResponse(status, { detail: "Synthetic admission refused at /srv/synthetic/archive" }) });
    await fixture.prepare();
    await fixture.library.actions.restoreImport();
    const message = fixture.banners.at(-1)[0];
    assert.match(message, /Import failed:.*Synthetic admission refused.*\[path\]/);
    assert.doesNotMatch(message, /unknown|may or may not|\/srv\//);
    assert.equal(fixture.refreshCount, 0);
    assert.equal(fixture.library.state.restoreInspection, null);
    assert.equal(fixture.applies().length, 1);
  });
}

test("library restore attempts reachable local apply despite offline hint", async () => {
  const fixture = restoreFixture();
  await fixture.prepare();
  fixture.navigator.onLine = false;
  await fixture.library.actions.restoreImport();
  assert.equal(fixture.applies().length, 1);
  assert.equal(fixture.refreshCount, 1);
  assert.match(fixture.banners.at(-1)[0], /Backup restored/i);
  assert.doesNotMatch(fixture.banners.at(-1)[0], /unknown|may or may not/i);
  assert.equal(fixture.library.state.restoreInspection, null);
});

test("library restore deliberate retry requires a fresh inspection and confirmation", async () => {
  const fixture = restoreFixture({ imported: () => { throw new TypeError("Failed to fetch"); } });
  await fixture.prepare();
  await fixture.library.actions.restoreImport();
  await fixture.library.actions.restoreImport();
  assert.equal(fixture.applies().length, 1);
  action(fixture.elements.dialog, "restore-inspect").click();
  await settle();
  assert.equal(fixture.applies().length, 1, "inspection does not automatically replay apply");
  assert.match(fixture.elements.dialog.textContent, /Continue\?/);
  action(fixture.elements.dialog, "restore-import").click();
  await settle();
  assert.deepEqual(fixture.applies().map(({ options }) => options.headers["X-Backup-Inspection-Receipt"]), ["receipt-1", "receipt-2"]);
});

for (const failures of [{}, { ui: "Synthetic start failure" }]) {
  test(`library restore accepts decided success with restart failures ${JSON.stringify(failures)}`, async () => {
    const fixture = restoreFixture({ imported: () => jsonResponse(200, restoreResult({ systems: [], default_system_id: null, stopped_containers: ["ui"], restarted_containers: Object.keys(failures).length ? [] : ["ui"], restart_failures: failures })) });
    await fixture.prepare();
    await fixture.library.actions.restoreImport();
    assert.match(fixture.banners.at(-1)[0], /^Backup restored/);
    assert.equal(fixture.banners.at(-1)[1], Object.keys(failures).length ? "error" : "success");
    assert.match(fixture.elements.dialog.querySelector(".backup-dialog-result").textContent, /^Restored/);
    assert.equal(fixture.refreshCount, 1);
    assert.equal(fixture.elements.dialog.querySelector("#backup-restore-passphrase").value, "");
    assert.deepEqual(fixture.receiptAtDispatch, [null]);
    assert.equal(fixture.applies().length, 1);
  });
}

for (const failure of ["refreshError", "listError"]) {
  test(`library decided restore survives ${failure}`, async () => {
    const fixture = restoreFixture({ [failure]: true });
    await fixture.prepare();
    await fixture.library.actions.restoreImport();
    assert.match(fixture.elements.dialog.querySelector(".backup-dialog-result").textContent, /^Restored/);
    assert.match(fixture.banners.at(-1)[0], /^Backup restored/);
    assert.doesNotMatch(fixture.banners.at(-1)[0], /Restore failed|Import failed|unknown whether/i);
    if (failure === "refreshError") assert.match(fixture.banners.at(-1)[0], /refresh/i);
    else assert.match(fixture.elements.status.textContent, /unavailable|running/i);
    assert.equal(fixture.applies().length, 1);
  });
}

test("library restore holds duplicate requests to one admitted apply and retires receipt first", async () => {
  let release;
  const fixture = restoreFixture({ imported: () => new Promise((resolve) => { release = resolve; }) });
  await fixture.prepare();
  const first = fixture.library.actions.restoreImport();
  const second = fixture.library.actions.restoreImport();
  assert.equal(first, second);
  await settle();
  assert.equal(fixture.applies().length, 1);
  assert.equal(fixture.library.state.restoreInspection, null);
  assert.deepEqual(fixture.receiptAtDispatch, [null]);
  const third = fixture.library.actions.restoreImport();
  assert.equal(first, third, "pending ownership remains after receipt retirement");
  release(jsonResponse(200, restoreResult()));
  await first;
  assert.equal(fixture.refreshCount, 1);
});

for (const timing of ["before queuing", "after queuing"]) {
  test(`library restore cancelled ${timing} cannot dispatch from a closed dialog`, async () => {
    const fixture = restoreFixture();
    await fixture.prepare();
    if (timing === "before queuing") fixture.library.actions.closeDialog();
    const pending = fixture.library.actions.restoreImport();
    if (timing === "after queuing") fixture.library.actions.closeDialog();
    await pending;
    assert.equal(fixture.applies().length, 0);
    assert.equal(fixture.banners.length, 0);
  });
}

for (const success of [true, false]) {
  test(`library stale restore ${success ? "success" : "failure"} cannot overwrite a successor dialog`, async () => {
    let release;
    const fixture = restoreFixture({ imported: () => new Promise((resolve) => { release = resolve; }) });
    await fixture.prepare();
    const pending = fixture.library.actions.restoreImport();
    await settle();
    fixture.library.actions.closeDialog();
    await fixture.prepare("cfg-new");
    const successorInspection = fixture.library.state.restoreInspection;
    const successorText = fixture.elements.dialog.textContent;
    release(success ? jsonResponse(200, restoreResult()) : jsonResponse(503, { detail: "Service unavailable" }));
    await pending;
    assert.equal(fixture.library.state.restoreInspection, successorInspection);
    assert.equal(fixture.elements.dialog.textContent, successorText);
    assert.equal(fixture.elements.dialog.querySelector("#backup-restore-passphrase").value, " synthetic phrase ");
    assert.equal(fixture.banners.length, 0, "stale dialog does not publish banners into a successor operation");
    assert.equal(fixture.refreshCount, success ? 1 : 0);
    assert.equal(fixture.calls.filter(({ url }) => url === "/api/admin/backups").length, success ? 2 : 1);
    assert.equal(fixture.applies().length, 1);
  });
}

test("admin library mount injects the production restore transport and outcome formatter", () => {
  const context = loadAdminFunctions([...transportNames, ...restoreNames], {
    document: { getElementById: () => null },
    formatBytes() {}, formatLocalTimestamp() {}, setBanner() {}, encodeUtf8Base64() {},
    describeBackupRestoreConfirmation() {}, describeMaintenanceOutcome() {}, renderMaintenanceResult() {},
    window: { AdminBackupLibrary: { createBackupLibrary(deps) { context.captured = deps; return null; } } },
  });
  const start = ADMIN_SOURCE.indexOf("  const backupLibrary = window.AdminBackupLibrary");
  const end = ADMIN_SOURCE.indexOf("  backupLibrary?.bind();", start);
  assert.ok(start > 0 && end > start);
  vm.runInContext(ADMIN_SOURCE.slice(start, end), context);
  assert.equal(context.captured.fetchBackupRestore, context.fetchBackupRestore);
  assert.equal(context.captured.describeBackupRestoreFailure, context.describeBackupRestoreFailure);
});

// ------------------------------------------------ settings editor (#573) --

function syntheticPolicyView(overrides = {}) {
  return {
    revision: "rev-1",
    classes: {
      config: { values: { enabled: true, local_keep: 30, remote_keep: null, remote_max_age_days: null, debounce_seconds: 30, max_delay_seconds: 600 }, locked: {} },
      full: { values: { enabled: false, local_keep: 7, remote_keep: null, remote_max_age_days: 90, schedule: "0 3 * * *", archive_format: "tar.zst" }, locked: { schedule: "BACKUP_FULL_SCHEDULE" } },
    },
    archive_formats: ["7z", "tar.zst"],
    targets: [{
      values: { target_id: "office-nas", label: "Office NAS", enabled: true, provider: "sftp", root: "/srv/backups/jbod", hostname: "nas.example.test", username: "backup", known_hosts_path: "/run/backup-secrets/archive_known_hosts" },
      original_target_id: "office-nas",
      secrets: {
        password_file: { configured: true, present: false },
        private_key_file: { configured: true, present: true },
        private_key_passphrase_file: { configured: false, present: null },
        access_key_id_file: { configured: false, present: null },
        secret_access_key_file: { configured: false, present: null },
      },
    }],
    targets_locked_by: null,
    providers: ["filesystem", "ftp", "sftp", "smb", "nfs", "s3"],
    target_fields: ["target_id", "provider", "root", "hostname", "port", "username", "timeout_seconds", "use_tls", "known_hosts_path", "trust_on_first_use"],
    secret_fields: ["password_file", "private_key_file", "private_key_passphrase_file", "access_key_id_file", "secret_access_key_file"],
    problems: [],
    restart_command: "docker compose --profile backup-scheduler restart enclosure-backup-scheduler",
    ...overrides,
  };
}

test("settings editor shows secret state only and locks environment values", async () => {
  const api = fakeApi({ "GET /api/admin/backups/policy": () => syntheticPolicyView() });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.openPolicyEditor(elements.editButton);
  await settle();
  const text = elements.dialog.textContent;
  assert.match(text, /Password file: Secret file missing or not private/);
  assert.match(text, /Private key file: Secret file present/);
  assert.match(text, /Set by BACKUP_FULL_SCHEDULE in the environment/);
  assert.equal(elements.dialog.querySelector("#backup-edit-full-schedule").disabled, true);
  assert.equal(elements.dialog.querySelector("#backup-edit-target-0-password_file").value, "", "secret inputs start empty");
  assert.doesNotMatch(text, /hunter2|BEGIN .*PRIVATE KEY/);
});

test("saving sends edits, file-path secret changes and clears, never locked values", async () => {
  let sent = null;
  const api = fakeApi({
    "GET /api/admin/backups/policy": () => syntheticPolicyView(),
    "PUT /api/admin/backups/policy": ({ options }) => {
      sent = JSON.parse(options.body);
      return { ...syntheticPolicyView({ revision: "rev-2" }), ok: true, restart_required: true };
    },
  });
  const { elements, library, banners } = mount({ api });
  await library.load();
  await library.actions.openPolicyEditor(elements.editButton);
  await settle();
  elements.dialog.querySelector("#backup-edit-config-local_keep").value = "12";
  elements.dialog.querySelector("#backup-edit-config-remote_keep").value = "";
  elements.dialog.querySelector("#backup-edit-target-0-label").value = "Office NAS 2";
  elements.dialog.querySelector("#backup-edit-target-0-password_file").value = " /run/backup-secrets/new_password ";
  elements.dialog.querySelector("#backup-edit-target-0-private_key_file-clear").checked = true;
  await library.actions.savePolicy();
  assert.equal(sent.revision, "rev-1");
  assert.equal(sent.classes.config.local_keep, 12);
  assert.equal(sent.classes.config.remote_keep, null);
  assert.equal("schedule" in sent.classes.full, false, "a locked value is never sent");
  assert.equal(sent.targets[0].values.label, "Office NAS 2");
  assert.deepEqual(sent.targets[0].secrets, { password_file: "/run/backup-secrets/new_password", private_key_file: null });
  for (const key of Object.keys(sent.targets[0].values)) {
    assert.doesNotMatch(key, /_file$|password|secret/);
  }
  assert.match(elements.dialog.textContent, /Restart the backup scheduler/);
  assert.match(banners.at(-1)[0], /Backup settings saved/);
});

test("the archive format is a choice of the allowed values and saves as a string", async () => {
  const sent = [];
  const api = fakeApi({
    "GET /api/admin/backups/policy": () => syntheticPolicyView(),
    "PUT /api/admin/backups/policy": ({ options }) => {
      sent.push(JSON.parse(options.body));
      return { ...syntheticPolicyView({ revision: "rev-2" }), ok: true, restart_required: true };
    },
  });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.openPolicyEditor(elements.editButton);
  await settle();
  const select = elements.dialog.querySelector("#backup-edit-full-archive_format");
  assert.equal(select.tagName, "SELECT");
  assert.deepEqual(select.querySelectorAll("option").map((option) => option.getAttribute("value")), ["7z", "tar.zst"]);
  assert.equal(select.value, "tar.zst");
  await library.actions.savePolicy();
  assert.equal(sent[0].classes.full.archive_format, "tar.zst", "an untouched format round-trips as the string, not null");
  elements.dialog.querySelector("#backup-edit-full-archive_format").value = "7z";
  await library.actions.savePolicy();
  assert.equal(sent[1].classes.full.archive_format, "7z");
});

test("an archive format set in the environment renders disabled and is never sent", async () => {
  let sent = null;
  const locked = syntheticPolicyView();
  locked.classes.full.locked.archive_format = "BACKUP_FULL_ARCHIVE_FORMAT";
  const api = fakeApi({
    "GET /api/admin/backups/policy": () => locked,
    "PUT /api/admin/backups/policy": ({ options }) => { sent = JSON.parse(options.body); return syntheticPolicyView(); },
  });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.openPolicyEditor(elements.editButton);
  await settle();
  assert.equal(elements.dialog.querySelector("#backup-edit-full-archive_format").disabled, true);
  assert.match(elements.dialog.textContent, /Set by BACKUP_FULL_ARCHIVE_FORMAT in the environment/);
  await library.actions.savePolicy();
  assert.equal("archive_format" in sent.classes.full, false);
});

test("one click on Add a target or Remove this target acts once, with the dialog inside the section", async () => {
  const base = syntheticPolicyView();
  const three = ["alpha", "bravo", "charlie"].map((name) => ({
    ...base.targets[0],
    values: { ...base.targets[0].values, target_id: name, label: name },
    original_target_id: name,
  }));
  const api = fakeApi({ "GET /api/admin/backups/policy": () => syntheticPolicyView({ targets: three }) });
  const { elements, library } = mount({ api });
  assert.match(TEMPLATE, /<section id="backup-library"(?:(?!<\/section>)[\s\S])*<dialog id="backup-library-dialog"/, "the template nests the dialog in the section");
  assert.equal(elements.dialog.parentNode, elements.root, "the harness nests the dialog as the template does");
  await library.load();
  await library.actions.openPolicyEditor(elements.editButton);
  await settle();
  const targetIds = () => elements.dialog.querySelectorAll("[data-target-key]")
    .filter((input) => input.dataset.targetKey === "target_id")
    .map((input) => input.value);
  action(elements.dialog, "policy-remove-target", "0").click();
  assert.deepEqual(targetIds(), ["bravo", "charlie"]);
  action(elements.dialog, "policy-add-target").click();
  assert.deepEqual(targetIds(), ["bravo", "charlie", ""]);
});

test("adding and removing targets keeps what was typed; a stale revision says reload", async () => {
  let sent = null;
  const api = fakeApi({
    "GET /api/admin/backups/policy": () => syntheticPolicyView(),
    "PUT /api/admin/backups/policy": ({ options }) => {
      sent = JSON.parse(options.body);
      return Object.assign(new Error("config.yaml changed"), { status: 409 });
    },
  });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.openPolicyEditor(elements.editButton);
  await settle();
  elements.dialog.querySelector("#backup-edit-target-0-label").value = "Typed label";
  library.actions.addPolicyTarget();
  assert.equal(elements.dialog.querySelector("#backup-edit-target-0-label").value, "Typed label");
  elements.dialog.querySelector("#backup-edit-target-1-target_id").value = "usb-disk";
  elements.dialog.querySelector("#backup-edit-target-1-root").value = "/srv/usb/jbod";
  library.actions.removePolicyTarget(0);
  await library.actions.savePolicy();
  assert.deepEqual(sent.targets.map((target) => target.values.target_id), ["usb-disk"]);
  assert.match(elements.dialog.textContent, /changed since this editor opened/);
});

test("pending secret edits survive adding or removing a target; saved rows keep their identity", async () => {
  let sent = null;
  const api = fakeApi({
    "GET /api/admin/backups/policy": () => syntheticPolicyView(),
    "PUT /api/admin/backups/policy": ({ options }) => {
      sent = JSON.parse(options.body);
      return { ...syntheticPolicyView({ revision: "rev-2" }), ok: true, restart_required: true };
    },
  });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.openPolicyEditor(elements.editButton);
  await settle();
  elements.dialog.querySelector("#backup-edit-target-0-target_id").value = "office-nas-renamed";
  elements.dialog.querySelector("#backup-edit-target-0-password_file").value = "/run/backup-secrets/new_password";
  elements.dialog.querySelector("#backup-edit-target-0-private_key_file-clear").checked = true;
  library.actions.addPolicyTarget();
  assert.equal(elements.dialog.querySelector("#backup-edit-target-0-password_file").value, "/run/backup-secrets/new_password");
  assert.equal(elements.dialog.querySelector("#backup-edit-target-0-private_key_file-clear").checked, true);
  elements.dialog.querySelector("#backup-edit-target-1-target_id").value = "usb-disk";
  elements.dialog.querySelector("#backup-edit-target-1-root").value = "/srv/usb/jbod";
  library.actions.removePolicyTarget(1);
  library.actions.addPolicyTarget();
  elements.dialog.querySelector("#backup-edit-target-1-target_id").value = "office-nas";
  elements.dialog.querySelector("#backup-edit-target-1-root").value = "/srv/usb/jbod";
  await library.actions.savePolicy();
  assert.deepEqual(sent.targets[0].secrets, { password_file: "/run/backup-secrets/new_password", private_key_file: null });
  assert.equal(sent.targets[0].values.target_id, "office-nas-renamed");
  assert.equal(sent.targets[0].original_target_id, "office-nas", "a renamed row keeps its saved identity");
  assert.equal("original_target_id" in sent.targets[1], false, "a new row reusing an old ID inherits nothing");
});

test("environment-managed targets are read-only in the editor", async () => {
  let sent = null;
  const api = fakeApi({
    "GET /api/admin/backups/policy": () => syntheticPolicyView({ targets_locked_by: "BACKUP_TARGETS_JSON" }),
    "PUT /api/admin/backups/policy": ({ options }) => { sent = JSON.parse(options.body); return syntheticPolicyView(); },
  });
  const { elements, library } = mount({ api });
  await library.load();
  await library.actions.openPolicyEditor(elements.editButton);
  await settle();
  assert.match(elements.dialog.textContent, /Targets are set by BACKUP_TARGETS_JSON/);
  assert.equal(action(elements.dialog, "policy-add-target"), undefined);
  assert.equal(elements.dialog.querySelector("#backup-edit-target-0-label").disabled, true);
  await library.actions.savePolicy();
  assert.equal("targets" in sent, false);
});

test("the panel offers Edit settings and no longer calls the settings read-only", () => {
  assert.match(TEMPLATE, /id="backup-library-edit-button"[^>]*>Edit settings</);
  assert.doesNotMatch(TEMPLATE, /shown here read-only/);
  assert.match(ADMIN_SOURCE, /editButton: document\.getElementById\("backup-library-edit-button"\)/);
});

// The pure model lives in its own script (admin_backup_model.js). It loads on
// its own, the library takes its names from it, and the page loads it first.
// The URL builders stay in the library beside the requests that use them.
const MODEL_PATH = path.join(ROOT, "admin_service/static/admin_backup_model.js");
const LIBRARY_PATH = path.join(ROOT, "admin_service/static/admin_backups.js");
const MODEL_NAMES = [
  "buildPolicyPayload", "secretStateText", "scrubText", "validId", "artifactUrl", "targetUrl", "providerLabel",
  "locationLabel", "isRestorable", "canDownload", "stateLabel", "groupArtifacts", "describePolicy",
  "humanizePlanReason", "normalizePlan", "lastRunSummary", "cleanupSummary",
];
const LIBRARY_URL_BUILDERS = ["artifactUrl", "targetUrl"];

function runBrowserScripts(context, ...files) {
  vm.createContext(context);
  for (const file of files) {
    vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file });
  }
  return context;
}

test("the backup model loads on its own, as a browser script and as a CommonJS module", () => {
  const page = runBrowserScripts({}, MODEL_PATH);
  assert.equal(page.AdminBackupLibrary, undefined);
  assert.equal(page.AdminBackupModel.scrubText("see /srv/a/b now"), "see [path] now");
  const loaded = require("node:child_process").execFileSync(process.execPath, ["-e", [
    `const api = require(${JSON.stringify(MODEL_PATH)});`,
    "const files = Object.keys(require.cache).map((name) => require('node:path').basename(name));",
    "process.stdout.write(JSON.stringify({ files, names: Object.keys(api) }));",
  ].join("\n")], { encoding: "utf8" });
  const result = JSON.parse(loaded);
  assert.deepEqual(result.files, ["admin_backup_model.js"]);
  assert.deepEqual(result.names, Object.keys(page.AdminBackupModel));
});

test("the library's model keeps every name, the moved ones the model script's own functions", () => {
  const standalone = require(MODEL_PATH);
  assert.deepEqual(Object.keys(model), MODEL_NAMES);
  for (const name of MODEL_NAMES) {
    assert.equal(typeof model[name], "function", name);
    if (!LIBRARY_URL_BUILDERS.includes(name)) assert.equal(model[name], standalone[name], name);
  }
  for (const name of LIBRARY_URL_BUILDERS) assert.equal(standalone[name], undefined, name);
  assert.equal(model.artifactUrl("a1", "/verify"), "/api/admin/backups/a1/verify");
  assert.throws(() => model.targetUrl("../x"), /That target id is not valid\./);
});

test("in a page the library uses the model script loaded before it", () => {
  const page = runBrowserScripts({}, MODEL_PATH, LIBRARY_PATH);
  for (const name of MODEL_NAMES.filter((item) => !LIBRARY_URL_BUILDERS.includes(item))) {
    assert.equal(page.AdminBackupLibrary.model[name], page.AdminBackupModel[name], name);
  }
  // The library binds the names of whichever model the page loaded, not copies of its own.
  const ids = [];
  const substitute = { ...page.AdminBackupModel, validId: (value) => { ids.push(value); return true; } };
  const other = runBrowserScripts({ AdminBackupModel: substitute }, LIBRARY_PATH);
  assert.equal(other.AdminBackupLibrary.model.validId, substitute.validId);
  assert.equal(other.AdminBackupLibrary.model.artifactUrl("../x"), "/api/admin/backups/..%2Fx");
  assert.deepEqual(ids, ["../x"]);
  const modelAt = BASE.indexOf("path='admin_backup_model.js'");
  assert.ok(modelAt > 0 && modelAt < BASE.indexOf("path='admin_backups.js'"), "base.html must load the model before the library");
});

test("the model script keeps the library's source rules", () => {
  const source = fs.readFileSync(MODEL_PATH, "utf8");
  const strings = (source.match(/text: "[^"]*"|`[A-Z][^`]*`/g) || []).join("\n").replace(/\$\{[^}]*\}/g, "");
  assert.ok(strings.includes("Last clean-up"), "the scan must find the model's operator-facing strings");
  for (const word of [/artifact/i, /tombstone/i, /grooming/i, /lifecycle/i, /catalog/i]) {
    assert.doesNotMatch(strings, word);
  }
  assert.doesNotMatch(source, /innerHTML|insertAdjacentHTML|\beval\(|new Function/);
  assert.doesNotMatch(source, /require\(|import /);
  assert.doesNotMatch(source, /copy\/copies|deletion\/deletions/);
  assert.doesNotMatch(source, /cleanup_running|no longer running|cleanupWatch|bannerRevision/);
  assert.doesNotMatch(source, /When the running one finishes, its result shows in the Last clean-up line/);
  // Request paths stay in the library, where the read-reach generator resolves them.
  assert.doesNotMatch(source, /\/api\//);
});
