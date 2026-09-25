"use strict";

// Backups library page (#398) against a fake of the /api/admin/backups API.
// Synthetic data only: example.test names, no real paths or credentials.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

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
    "POST /api/admin/backups/full-new/restore/import": () => ({ ok: true, stopped_containers: ["ui"], restarted_containers: ["ui"], restart_failures: {} }),
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

function mount({ api = fakeApi(), confirm = () => true, stopped = false } = {}) {
  const doc = fakeDocument();
  const ids = ["root", "heading", "status", "policies", "targets", "storage", "artifacts", "refreshButton", "cleanupButton"];
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
  doc.body.append(elements.dialog);
  const banners = [];
  const refreshes = [];
  const library = createBackupLibrary({
    document: doc,
    elements,
    fetchJson: api.fetchJson,
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

test("an unavailable backup service says so and offers nothing to run", async () => {
  const api = fakeApi({ "GET /api/admin/backups": () => ({ available: false, detail: "The backup scheduler is not running.", classes: { config: { enabled: false }, full: { enabled: false } }, targets: [], artifacts: [], storage: {}, running: null }) });
  const { elements, library } = mount({ api });
  await library.load();
  assert.equal(elements.status.textContent, "Backups aren't available: The backup scheduler is not running.");
  assert.ok(elements.policies.querySelectorAll('[data-backup-action="run"]').every((node) => node.disabled));
  assert.equal(elements.cleanupButton.disabled, true);
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
  assert.match(elements.dialog.textContent, /Check failed: Inspection did not return an observed encryption mode and receipt/);
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
  assert.match(text, /1 more is over a limit but kept, because it is the newest verified copy in its place/);
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
  assert.doesNotMatch(LIBRARY_SOURCE, /require\(|import /);
});
