"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");

function functionSource(name) {
  const marker = `function ${name}(`;
  const start = SOURCE.indexOf(marker);
  assert.notEqual(start, -1, `missing ${name}`);
  const bodyStart = SOURCE.indexOf("{", start);
  let depth = 0;
  for (let index = bodyStart; index < SOURCE.length; index += 1) {
    if (SOURCE[index] === "{") {
      depth += 1;
    } else if (SOURCE[index] === "}") {
      depth -= 1;
      if (depth === 0) {
        return SOURCE.slice(start, index + 1);
      }
    }
  }
  assert.fail(`unterminated ${name}`);
}

function loadFunctions(names) {
  const context = vm.createContext({});
  vm.runInContext(
    `${names.map(functionSource).join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin-command-state.js" }
  );
  return context.__tested;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test("unchanged redacted command placeholders preserve the source list", () => {
  const { loadSshCommandState, collectSshCommandUpdate } = loadFunctions([
    "normalizeCommandText",
    "loadSshCommandState",
    "unchangedRedactedSshCommands",
    "collectSshCommandUpdate",
  ]);
  const field = { value: "", dataset: {} };
  const system = {
    id: "source-system",
    ssh_commands: ["Saved command 1 (hidden)", "Saved command 2 (hidden)"],
    ssh_commands_redacted: true,
  };

  loadSshCommandState(field, system);
  const update = collectSshCommandUpdate(field, true);

  assert.equal(field.value, "Saved command 1 (hidden)\nSaved command 2 (hidden)");
  assert.deepEqual(plain(update), {
    ssh_commands: [],
    ssh_commands_action: "preserve",
    ssh_commands_source_system_id: "source-system",
  });
});

test("editing or clearing redacted placeholders explicitly replaces the list", () => {
  const { loadSshCommandState, collectSshCommandUpdate } = loadFunctions([
    "normalizeCommandText",
    "loadSshCommandState",
    "unchangedRedactedSshCommands",
    "collectSshCommandUpdate",
  ]);
  const field = { value: "", dataset: {} };
  loadSshCommandState(field, {
    id: "source-system",
    ssh_commands: ["Saved command 1 (hidden)"],
    ssh_commands_redacted: true,
  });

  field.value = "/usr/local/bin/replacement --read-only";
  assert.deepEqual(plain(collectSshCommandUpdate(field, true)), {
    ssh_commands: ["/usr/local/bin/replacement --read-only"],
    ssh_commands_action: "replace",
    ssh_commands_source_system_id: null,
  });

  field.value = "";
  assert.deepEqual(plain(collectSshCommandUpdate(field, true)), {
    ssh_commands: [],
    ssh_commands_action: "replace",
    ssh_commands_source_system_id: null,
  });
});

test("command guidance identifies only unchanged hidden placeholders as redacted", () => {
  const { loadSshCommandState, unchangedRedactedSshCommands } = loadFunctions([
    "normalizeCommandText",
    "loadSshCommandState",
    "unchangedRedactedSshCommands",
  ]);
  const field = { value: "", dataset: {} };
  loadSshCommandState(field, {
    id: "source-system",
    ssh_commands: ["Saved command 1 (hidden)"],
    ssh_commands_redacted: true,
  });

  assert.equal(unchangedRedactedSshCommands(field), true);
  field.value = "/usr/local/bin/replacement --read-only";
  assert.equal(unchangedRedactedSshCommands(field), false);
  field.value = "";
  assert.equal(unchangedRedactedSshCommands(field), false);

  const helpSource = functionSource("syncPlatformSpecificSetupFields");
  assert.match(helpSource, /unchangedRedactedSshCommands\(elements\.setupSshCommands\)/);
  assert.match(helpSource, /Saved SSH commands are hidden/);
});

test("form hydration and save collection use the redacted command contract", () => {
  const loadSource = functionSource("loadSystemIntoForm");
  const resetSource = functionSource("resetSetupForm");
  const collectStart = SOURCE.indexOf("  function collectSetupPayload");
  const collectEnd = SOURCE.indexOf("\n  async function discoverQuantastorHaNodes", collectStart);
  assert.notEqual(collectStart, -1, "missing collectSetupPayload");
  assert.notEqual(collectEnd, -1, "missing collectSetupPayload end marker");
  const collectSource = SOURCE.slice(collectStart, collectEnd);

  assert.match(loadSource, /loadSshCommandState\(elements\.setupSshCommands, system\)/);
  assert.match(resetSource, /loadSshCommandState\(elements\.setupSshCommands, null\)/);
  assert.match(collectSource, /\.\.\.collectSshCommandUpdate\(elements\.setupSshCommands, preserveRedactedSecrets\)/);
});

test("editing command placeholders refreshes the guidance immediately", () => {
  const listenerStart = SOURCE.indexOf('elements.setupSshCommands?.addEventListener("input"');
  const listenerEnd = SOURCE.indexOf("\n    });", listenerStart);
  assert.notEqual(listenerStart, -1, "missing SSH command input listener");
  assert.notEqual(listenerEnd, -1, "missing SSH command input listener end marker");
  const listenerSource = SOURCE.slice(listenerStart, listenerEnd);

  assert.match(listenerSource, /syncPlatformSpecificSetupFields\(\)/);
});

test("probe and host-prep request bodies omit command preservation controls", () => {
  for (const name of ["discoverQuantastorHaNodes", "collectEsxiHostPrepInstallPayload"]) {
    const source = functionSource(name);
    assert.doesNotMatch(source, /ssh_commands_(?:action|source_system_id)/);
  }
});
