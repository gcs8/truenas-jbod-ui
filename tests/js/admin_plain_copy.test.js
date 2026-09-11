"use strict";

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
  const start = SOURCE.search(new RegExp(`^  (?:async )?function ${name}\\(`, "m"));
  assert.ok(start >= 0, `function ${name} must exist`);
  const next = SOURCE.slice(start + 3).search(/^  (?:async )?function |^  const [A-Z_]+ = \{/m);
  return SOURCE.slice(start, next < 0 ? undefined : start + 3 + next);
}

function constSource(name) {
  const start = SOURCE.indexOf(`  const ${name} = {`);
  assert.ok(start >= 0, `constant ${name} must exist`);
  const end = SOURCE.indexOf("\n  };\n", start);
  return SOURCE.slice(start, end + 5);
}

function load(names, bindings = {}, extraSource = "") {
  const context = vm.createContext({ console, ...bindings });
  vm.runInContext(
    `${extraSource}\n${names.map(functionSource).join("\n")}\nglobalThis.tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.tested;
}

class FakeElement {
  constructor(attributes = {}) {
    this.attributes = attributes;
    this.dataset = attributes.dataset || {};
    this.classes = new Set(attributes.classes || []);
    this.textContent = "";
    this.classList = {
      toggle: (name, force) => {
        if (force === undefined) {
          force = !this.classes.has(name);
        }
        if (force) {
          this.classes.add(name);
        } else {
          this.classes.delete(name);
        }
        return force;
      },
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
    };
  }

  get hidden() {
    return this.classes.has("hidden");
  }
}

test("the one-time bootstrap panel and SSH command list are collapsed by default", () => {
  const bootstrap = TEMPLATE.match(/<details id="setup-bootstrap-details"[^>]*>/);
  assert.ok(bootstrap, "bootstrap panel must sit inside a <details> element");
  assert.doesNotMatch(bootstrap[0], /\bopen\b/, "bootstrap details must start collapsed");
  const bootstrapBlock = TEMPLATE.slice(TEMPLATE.indexOf('<details id="setup-bootstrap-details"'));
  assert.ok(
    bootstrapBlock.indexOf('id="setup-bootstrap-panel"') < bootstrapBlock.indexOf("</details>"),
    "the bootstrap panel must be inside the collapsed section"
  );

  const commands = TEMPLATE.match(/<details id="setup-ssh-commands-details"[^>]*>/);
  assert.ok(commands, "SSH command list must sit inside a <details> element");
  assert.doesNotMatch(commands[0], /\bopen\b/);
  const commandsBlock = TEMPLATE.slice(TEMPLATE.indexOf('<details id="setup-ssh-commands-details"'));
  assert.ok(commandsBlock.indexOf('id="setup-ssh-commands"') < commandsBlock.indexOf("</details>"));
});

test("the QuantaStor HA toggle starts hidden and only shows for QuantaStor", () => {
  assert.match(TEMPLATE, /<label id="setup-ha-toggle" class="toggle hidden">/);

  const setupHaToggle = new FakeElement({ classes: ["hidden"] });
  const setupHaEnabled = { checked: false, disabled: false };
  const setupHaNodesResult = { textContent: "" };
  let platform = "scale";
  const { renderQuantastorHaSection } = load(["renderQuantastorHaSection"], {
    state: { haNodesLoading: false },
    elements: { setupHaToggle, setupHaEnabled, setupHaNodesResult, setupHaPanel: new FakeElement() },
    currentSetupPlatform: () => platform,
    syncSshHostCopy() {},
    currentQuantastorHaNodes: () => [],
    haNodeFieldValue: () => null,
  });

  renderQuantastorHaSection();
  assert.equal(setupHaToggle.hidden, true, "non-QuantaStor platforms must not see the HA toggle");
  assert.equal(setupHaNodesResult.textContent, "");

  platform = "quantastor";
  renderQuantastorHaSection();
  assert.equal(setupHaToggle.hidden, false);
  assert.equal(setupHaEnabled.disabled, false);
  assert.match(setupHaNodesResult.textContent, /two QuantaStor nodes/);
  assert.doesNotMatch(setupHaNodesResult.textContent, /Quantastor/);
});

test("storage view editor only shows the fields that apply to the view kind", () => {
  const fields = [
    "profile", "binding_mode", "target_system", "enclosure_ids", "pool_names",
    "serials", "pcie_addresses", "device_names", "slot_labels", "slot_sizes",
  ].map((name) => new FakeElement({ dataset: { storageViewField: name } }));
  const editor = { querySelectorAll: () => fields };
  const { syncStorageViewFieldVisibility } = load(
    ["storageViewFieldsForKind", "syncStorageViewFieldVisibility"],
    { elements: { setupStorageViewEditor: editor } },
    constSource("STORAGE_VIEW_FIELDS_BY_KIND")
  );
  const visible = () => fields.filter((field) => !field.hidden).map((field) => field.dataset.storageViewField).sort();

  syncStorageViewFieldVisibility("boot_devices");
  assert.deepEqual(visible(), ["binding_mode", "device_names", "serials", "slot_labels"]);

  syncStorageViewFieldVisibility("ses_enclosure");
  assert.deepEqual(visible(), ["enclosure_ids", "profile", "slot_labels"]);

  syncStorageViewFieldVisibility("nvme_carrier", { haTargetsAvailable: true });
  assert.deepEqual(visible(), ["binding_mode", "device_names", "pcie_addresses", "serials", "slot_labels", "slot_sizes", "target_system"]);
});

test("the dead show-in-admin toggle is gone but the saved flag is preserved", () => {
  assert.doesNotMatch(TEMPLATE, /setup-storage-view-show-admin/);
  assert.doesNotMatch(SOURCE, /setupStorageViewShowAdmin/);
  assert.match(functionSource("saveStorageViewEditorToState"), /show_in_admin_ui: storageView\.render\?\.show_in_admin_ui !== false/);
});

test("validation errors name the field instead of the pydantic location", () => {
  const { describeApiError } = load(["describeApiError"]);
  assert.equal(
    describeApiError([{ loc: ["body", "ssh_port"], msg: "Input should be a valid integer" }]),
    "SSH port: Input should be a valid integer"
  );
  assert.equal(
    describeApiError([{ loc: ["body", "ha_nodes", 1, "host"], msg: "Field required" }]),
    "HA nodes > item 2 > host: Field required"
  );
  assert.equal(
    describeApiError("Unable to inspect orphaned history; see admin logs."),
    "Unable to inspect orphaned history; see admin logs. Run `docker compose logs enclosure-admin` for details."
  );
  assert.equal(describeApiError("Choose a saved system first."), "Choose a saved system first.");
});

test("restore confirmation reads as a sentence instead of JSON", () => {
  const { describeBackupInspection } = load(["describeBackupInspection"], {
    formatLocalTimestamp: (value) => `on ${value}`,
  });
  const text = describeBackupInspection({
    encryption_mode: "encrypted",
    exported_at: "2026-09-01",
    aggregate_counts: {
      systems: 2,
      profiles: 3,
      storage_views: 1,
      mappings: 0,
      history: { tracked_slots: 4, event_count: 1000, metric_sample_count: 240 },
    },
  });
  assert.equal(
    text,
    "This backup contains 2 systems, 3 layouts, 1 storage view, 0 saved mappings, 1,240 history rows, exported on 2026-09-01 (encrypted)."
  );
  assert.doesNotMatch(functionSource("importBackup"), /JSON\.stringify\(inspection/);
});

test("restore confirmation shows the older-version note before the replace warning", () => {
  const { describeBackupInspection, describeBackupRestoreConfirmation } = load(["describeBackupInspection", "describeBackupRestoreConfirmation"], {
    formatLocalTimestamp: (value) => `on ${value}`,
  });
  const baseInspection = {
    encryption_mode: "not_encrypted",
    exported_at: "2026-09-01",
    aggregate_counts: { systems: 1, profiles: 0, storage_views: 0, mappings: 0 },
  };

  const withNote = describeBackupRestoreConfirmation({
    ...baseInspection,
    app_version_note: "This backup was made by v0.22.2; settings and history will be brought up to date during restore.",
  });
  const noteIndex = withNote.indexOf(
    "This backup was made by v0.22.2; settings and history will be brought up to date during restore."
  );
  const replaceIndex = withNote.indexOf("Restoring replaces all current settings");
  assert.ok(noteIndex >= 0, "the version note must appear");
  assert.ok(replaceIndex > noteIndex, "the version note must come before the replace warning");
  assert.equal(
    withNote.split("This backup was made by v0.22.2; settings and history will be brought up to date during restore.").length,
    2,
    "the version note must appear exactly once"
  );

  const withoutNote = describeBackupRestoreConfirmation({ ...baseInspection, app_version_note: null });
  assert.doesNotMatch(withoutNote, /brought up to date during restore/);
  assert.equal(
    withoutNote,
    `${describeBackupInspection(baseInspection)}\n\n` +
      "Restoring replaces all current settings, mappings and history with this backup. Continue?"
  );
});

test("staged ESXi packages are described as name, size and upload age", () => {
  const { describeStagedPackage } = load(["formatRelativeAge", "describeStagedPackage"], {
    formatBytes: () => "41 MiB",
    Date: class extends Date {
      static now() {
        return new Date("2026-09-09T12:03:00Z").getTime();
      }
    },
  });
  assert.equal(
    describeStagedPackage({ filename: "storcli.zip", size_bytes: 43000000, created_at: "2026-09-09T12:00:00Z" }),
    "storcli.zip, 41 MiB, uploaded 3 minutes ago"
  );
  assert.doesNotMatch(functionSource("renderEsxiHostPrepPackages"), /JSON\.stringify/);
});

test("history deletion label carries the row count when it is known", () => {
  const state = { historyRowCounts: { "archive-core": 1240 } };
  const { describeHistoryDeletion } = load(["historyRowCountForSystem", "describeHistoryDeletion"], { state });
  assert.equal(describeHistoryDeletion("archive-core"), "Also delete its history (1,240 rows)");
  assert.equal(describeHistoryDeletion("unknown"), "Also delete its history");
  assert.match(functionSource("deleteSelectedSystem"), /will be deleted too/);
  assert.doesNotMatch(functionSource("deleteSelectedSystem"), /history sidecar/);
});

test("clicking a system pill loads it after a discard check", () => {
  const source = SOURCE.slice(SOURCE.indexOf("elements.currentSystemsList?.addEventListener(\"click\""));
  const handler = source.slice(0, source.indexOf("\n    });\n") + 8);
  assert.match(handler, /confirmDiscardSetupChanges\(\)/);
  assert.match(handler, /loadSystemIntoForm\(system\)/);
  assert.doesNotMatch(TEMPLATE, /existing-system-load-button/);
});

test("admin copy no longer leaks implementation vocabulary", () => {
  // The page header and the debug-bundle notes are rewritten separately; check everything else below the status banner.
  const userFacing = TEMPLATE
    .slice(TEMPLATE.indexOf('id="admin-status-banner"'))
    .replace(/<code>[^<]*<\/code>/g, "")
    .replace(/<div class="preview-card debug-bundle-card">[\s\S]*?<\/div>\s*<\/div>/, "")
    .replace(/<[^>]+>/g, " ");
  for (const word of [/sidecar/i, /\bpills?\b/i, /geometry/i, /\bpreset\b/i, /first.pass/i, /enrichment/i, /Quantastor/]) {
    assert.doesNotMatch(userFacing, word);
  }
  assert.match(TEMPLATE, /accept="\.7z,\.zip,\.tar\.gz,\.tgz,\.tar\.zst,\.zst,\.gz"/);
  assert.match(TEMPLATE, /Management controller only \(BMC \/ IPMI\)/);
  assert.match(TEMPLATE, /Add a sample system \(fake data\)/);
});
