"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");

function functionSource(name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = SOURCE.indexOf(")", start);
  const bodyStart = SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < SOURCE.length; index += 1) {
    const character = SOURCE[index];
    const next = SOURCE[index + 1];
    if (lineComment) {
      lineComment = character !== "\n";
      continue;
    }
    if (blockComment) {
      if (character === "*" && next === "/") {
        blockComment = false;
        index += 1;
      }
      continue;
    }
    if (quote) {
      if (character === "\\") {
        index += 1;
      } else if (character === quote) {
        quote = null;
      }
      continue;
    }
    if (character === "/" && next === "/") {
      lineComment = true;
      index += 1;
      continue;
    }
    if (character === "/" && next === "*") {
      blockComment = true;
      index += 1;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") {
      depth += 1;
    } else if (character === "}") {
      depth -= 1;
      if (depth === 0) {
        return SOURCE.slice(start, index + 1);
      }
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunctions(names, bindings = {}) {
  const context = vm.createContext({ console, ...bindings });
  vm.runInContext(
    `${names.map(functionSource).join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.__tested;
}

function buildHarness() {
  const state = {
    backupDefaults: {
      allow_plaintext_backup_export: false,
      packaging: "tar.zst",
      debug_packaging: "tar.zst",
    },
    selectedBackupPaths: [],
    selectedDebugPaths: ["history_db"],
    backupManualEncrypt: true,
    backupForced7z: false,
    backupLastPlainPackaging: "tar.zst",
    debugManualEncrypt: false,
    debugForced7z: false,
    debugLastPlainPackaging: "tar.zst",
  };
  const elements = {
    backupEncryptToggle: { checked: true, disabled: false },
    backupPackaging: { value: "tar.zst", disabled: false },
    backupExportPassphrase: { value: "synthetic-backup", disabled: false },
    backupExportButton: { disabled: false },
    debugScrubSecretsToggle: { checked: false },
    debugEncryptToggle: { checked: false, disabled: false },
    debugPackaging: { value: "tar.zst", disabled: false },
    debugExportPassphrase: { value: "ignored-while-off", disabled: false },
    debugExportButton: { disabled: false },
    debugExportResult: { textContent: "initial" },
  };
  const bindings = {
    state,
    elements,
    bundleHasLockedSelection() { return false; },
    bundlePathGroupByKey() { return null; },
    renderBackupPaths() {},
    readOptionalSecretValue(field) { return String(field?.value || "").trim() || null; },
  };
  const functions = loadFunctions(
    ["syncSingleBundleControls", "getDebugExportPolicy", "syncBackupControls"],
    bindings
  );
  return { state, elements, functions };
}

test("secure-default debug form blocks impossible plaintext export and clears inactive passphrase", () => {
  const { elements, functions } = buildHarness();

  functions.syncBackupControls();

  assert.equal(elements.debugExportPassphrase.disabled, true);
  assert.equal(elements.debugExportPassphrase.value, "");
  assert.equal(elements.debugExportButton.disabled, true);
  assert.match(elements.debugExportResult.textContent, /Enable secret scrubbing or encryption/i);
});

test("scrubbed plaintext and encrypted-with-passphrase states are allowed", () => {
  const { state, elements, functions } = buildHarness();

  elements.debugScrubSecretsToggle.checked = true;
  functions.syncBackupControls();
  assert.equal(elements.debugExportButton.disabled, false);

  elements.debugScrubSecretsToggle.checked = false;
  elements.debugEncryptToggle.checked = true;
  state.debugManualEncrypt = true;
  functions.syncBackupControls();
  assert.equal(elements.debugPackaging.value, "7z");
  assert.equal(elements.debugPackaging.disabled, true);
  assert.equal(elements.debugExportButton.disabled, true);
  assert.match(elements.debugExportResult.textContent, /Enter a passphrase/i);

  elements.debugExportPassphrase.value = "synthetic-passphrase";
  functions.syncBackupControls();
  assert.equal(elements.debugExportButton.disabled, false);
});

test("explicit plaintext policy permits unscrubbed unencrypted export", () => {
  const { state, elements, functions } = buildHarness();
  state.backupDefaults.allow_plaintext_backup_export = true;

  functions.syncBackupControls();

  assert.equal(elements.debugExportButton.disabled, false);
});

test("submission and passphrase input recheck the same policy", () => {
  assert.match(functionSource("exportDebugBundle"), /getDebugExportPolicy\(\)/);
  assert.match(functionSource("exportDebugBundle"), /if \(!policy\.allowed\)/);
  assert.match(
    SOURCE,
    /debugExportPassphrase\?\.addEventListener\("input",\s*syncBackupControls\)/
  );
});
