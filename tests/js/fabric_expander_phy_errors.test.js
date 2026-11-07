"use strict";

// CORE expander PHY error counters (#941): a counter the app could not read
// must render as unknown or not available, never as zero.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

function functionSource(name) {
  const start = FABRIC_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = FABRIC_SOURCE.indexOf("{", FABRIC_SOURCE.indexOf(")", start));
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < FABRIC_SOURCE.length; index += 1) {
    const character = FABRIC_SOURCE[index];
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth === 0) return FABRIC_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

const context = vm.createContext({});
vm.runInContext(`${functionSource("expanderPhyErrorText")}; this.expanderPhyErrorText = expanderPhyErrorText;`, context);
const { expanderPhyErrorText } = context;

test("measured expander PHY counters are listed by name", () => {
  assert.equal(
    expanderPhyErrorText({
      counter_state: "measured",
      invalid_dword_count: 9,
      running_disparity_error_count: 0,
      loss_of_dword_synchronization_count: 2,
      phy_reset_problem_count: 4294967295,
    }),
    "invalid dword 9, disparity 0, loss of sync 2, reset problem saturated",
  );
});

test("refused or unreadable counters never render as zero", () => {
  assert.equal(
    expanderPhyErrorText({ counter_state: "permission_denied", invalid_dword_count: null }),
    "Not available (permission not granted)",
  );
  for (const counterState of ["unknown", "no_route", "unavailable", "not_read"]) {
    const text = expanderPhyErrorText({ counter_state: counterState, invalid_dword_count: null });
    assert.equal(text, "Unknown");
    assert.doesNotMatch(text, /\b0\b/);
  }
});

test("rows without counter data add no row", () => {
  assert.equal(expanderPhyErrorText(null), null);
  assert.equal(expanderPhyErrorText({ phy: "3", device: "End Device" }), null);
});

test("both trace cards and the node inspector show the counter state", () => {
  assert.match(FABRIC_SOURCE, /\["PHY errors", linuxSes \|\| storageFabric \? null : expanderPhyErrorText\(expanderPhy\)\]/);
  assert.match(FABRIC_SOURCE, /\["PHY errors", linuxSes \|\| storageFabric \? null : expanderNode\?\.metrics\?\.phy_error_counters\]/);
  assert.match(FABRIC_SOURCE, /phy_error_counters: "PHY error counters"/);
});
