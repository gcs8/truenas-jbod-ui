"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../admin_service/static/admin.js"), "utf8");
const fn = (name) => source.match(new RegExp(`  (?:async )?function ${name}\\([^]*?\\n  }`))[0];

test("the key-list refresh shows configured warnings even without a selectable key and clears corrected ones", async () => {
  const note = { textContent: "" };
  const state = {};
  const warning = "Private key <example> needs chmod 600.";
  let warnings = [warning];
  const context = vm.createContext({ state, elements: { setupSshKeyWarnings: note },
    fetchJson: async () => ({ keys: [], ssh_key_warnings: warnings }), setupDraftSnapshot() {},
    syncKeyMode() {}, recordSetupDraftChange() {}, setBanner() {} });
  vm.runInContext(`${fn("syncKeyHelp")}\n${fn("loadSshKeys")}`, context);
  await context.loadSshKeys();
  assert.equal(note.textContent, warning);
  warnings = [];
  await context.loadSshKeys();
  assert.equal(note.textContent, "");
});
