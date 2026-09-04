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

function loadFunctions(names, bindings = {}) {
  const context = vm.createContext({ ...bindings });
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

test("bootstrap and sudoers preview payloads carry the saved-command handle while placeholders are unchanged", () => {
  const field = { value: "", dataset: {} };
  const elements = {
    setupSshCommands: field,
    setupPlatform: { value: "scale" },
    setupSshUser: { value: "jbodmap" },
    setupBootstrapInstallSudo: { checked: true },
  };
  const {
    loadSshCommandState,
    collectBootstrapSudoCommandPayload,
    collectSudoersPreviewPayload,
  } = loadFunctions(
    [
      "normalizeCommandText",
      "loadSshCommandState",
      "unchangedRedactedSshCommands",
      "collectSetupCommands",
      "collectBootstrapSudoCommands",
      "collectBootstrapSudoCommandPayload",
      "collectSudoersPreviewPayload",
    ],
    {
      elements,
      bootstrapEnabledForSession: () => true,
      recommendedSshUserForPlatform: () => "jbodmap",
    }
  );

  loadSshCommandState(field, {
    id: "source-system",
    ssh_commands: ["Saved command 1 (hidden)", "Saved command 2 (hidden)"],
    ssh_commands_redacted: true,
  });

  assert.deepEqual(plain(collectBootstrapSudoCommandPayload()), {
    sudo_commands: [],
    ssh_commands_source_system_id: "source-system",
  });
  assert.deepEqual(plain(collectSudoersPreviewPayload()), {
    platform: "scale",
    service_user: "jbodmap",
    install_sudo_rules: true,
    sudo_commands: [],
    ssh_commands_source_system_id: "source-system",
  });

  field.value = "sudo -n /usr/sbin/smartctl -a /dev/sda\n/usr/sbin/zpool status -gP";
  assert.deepEqual(plain(collectBootstrapSudoCommandPayload()), {
    sudo_commands: ["sudo -n /usr/sbin/smartctl -a /dev/sda"],
    ssh_commands_source_system_id: null,
  });

  field.value = "";
  assert.deepEqual(plain(collectBootstrapSudoCommandPayload()), {
    sudo_commands: [],
    ssh_commands_source_system_id: null,
  });
});

test("the one-time bootstrap request body uses the shared sudo command payload", () => {
  const bootstrapSource = functionSource("collectBootstrapPayload");
  assert.match(bootstrapSource, /\.\.\.collectBootstrapSudoCommandPayload\(\)/);
  assert.doesNotMatch(bootstrapSource, /sudo_commands: collectBootstrapSudoCommands\(\)/);
});
