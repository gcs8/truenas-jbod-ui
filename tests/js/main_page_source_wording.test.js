"use strict";

// #950: the main page names where bays and enclosures came from in plain
// words, says "Empty" for an empty slot, and does not offer locate lights on a
// storage view that has none.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

function functionSource(name) {
  const start = APP_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
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
      if (depth === 0) return APP_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ ...context });
  const source = names.map(functionSource).join("\n");
  const exports = names.map((name) => `this.__${name} = ${name};`).join("\n");
  vm.runInContext(`${source}\n${exports}`, sandbox, { filename: "main-page-source-wording.js" });
  return Object.fromEntries(names.map((name) => [name, sandbox[`__${name}`]]));
}

test("SSH bay sources are named in plain words, not raw codes", () => {
  const { mappingSourceLabel, mappingHealthSourceNote } = loadFunctions([
    "mappingSourceLabel",
    "platformMappingSourceLabel",
    "mappingHealthSourceNote",
  ]);
  assert.equal(mappingSourceLabel("device-name"), "SSH (device name)");
  assert.equal(mappingSourceLabel("ses-empty"), "SSH (empty bay)");
  assert.equal(mappingSourceLabel("import"), "imported assignment");
  const note = mappingHealthSourceNote([
    { slot: 0, state: "healthy", mapping_source: "device-name" },
    { slot: 1, state: "empty", mapping_source: "ses-empty" },
  ]);
  assert.equal(note, "Bay positions come from: SSH (device name), SSH (empty bay).");
  assert.doesNotMatch(note, /device-name|ses-empty/);
});

test("an empty NVMe carrier slot is labelled Empty, like every other empty slot", () => {
  const { storageViewRuntimeTilePrimary } = loadFunctions(["storageViewRuntimeTilePrimary"]);
  for (const kind of ["nvme_carrier", "boot_devices", "manual", "ses_enclosure"]) {
    assert.equal(storageViewRuntimeTilePrimary({ occupied: false }, { kind }), "Empty", kind);
  }
  assert.doesNotMatch(APP_SOURCE, /Unmapped slot/);
});

test("a storage view without bay lights does not claim locate is available", () => {
  const { locateCapabilityForView } = loadFunctions(["locateCapabilityForView", "isLiveStyledStorageView"]);
  const available = {
    label: "Identify LEDs",
    status: "available",
    summary: "At least one slot currently exposes identify LED control.",
  };
  const noLights = (kind) => ({ kind, slots: [{ slot_index: 0, occupied: true }, { slot_index: 1, occupied: false }] });

  for (const kind of ["nvme_carrier", "boot_devices", "manual"]) {
    const result = locateCapabilityForView(available, noLights(kind));
    assert.equal(result.status, "unsupported", kind);
    assert.equal(result.summary, "This view has no bay lights.", kind);
  }
  assert.equal(available.status, "available", "the snapshot capability is not changed in place");
  // The chassis and a saved chassis view keep what the enclosure reports.
  assert.equal(locateCapabilityForView(available, null), available);
  assert.equal(locateCapabilityForView(available, noLights("ses_enclosure")), available);
  // A view slot that does have a light keeps the card as reported.
  const lit = { kind: "nvme_carrier", slots: [{ slot_index: 0, occupied: true, led_supported: true }] };
  assert.equal(locateCapabilityForView(available, lit), available);
  // No card is invented when the snapshot reported none.
  assert.equal(locateCapabilityForView(null, noLights("nvme_carrier")), null);
  assert.match(functionSource("renderCapabilities"), /locateCapabilityForView\(/);
});

function templateEnclosureNotes() {
  const match = TEMPLATE.match(/\{%-?\s*set enclosure_source_notes = (\{[^%]*\})\s*-?%\}/);
  assert.ok(match, "the template keeps its enclosure source notes in one map");
  return JSON.parse(match[1]);
}

test("the Enclosures note names where the enclosures came from, matching the template", () => {
  const { enclosureSourceNote } = loadFunctions(["enclosureSourceNote"]);
  assert.equal(enclosureSourceNote("truenas"), "reported by TrueNAS");
  assert.equal(enclosureSourceNote("ssh"), "found over SSH");
  assert.equal(enclosureSourceNote("truenas_and_ssh"), "reported by TrueNAS and found over SSH");
  assert.equal(enclosureSourceNote("quantastor"), "reported by QuantaStor");
  assert.equal(enclosureSourceNote("layout"), "from the enclosure layout");
  // A copy saved before the field existed keeps the old note.
  assert.equal(enclosureSourceNote(undefined), "reported by TrueNAS");

  const notes = templateEnclosureNotes();
  for (const [source, text] of Object.entries(notes)) {
    assert.equal(enclosureSourceNote(source), text, source);
  }
  assert.deepEqual(Object.keys(notes).sort(), ["layout", "quantastor", "ssh", "truenas", "truenas_and_ssh"]);
  assert.doesNotMatch(TEMPLATE, /<span class="summary-note">reported by TrueNAS<\/span>/);
  assert.match(TEMPLATE, /id="summary-enclosure-source">\{\{ enclosure_source_notes\.get\(/);
  assert.match(
    functionSource("renderSummary"),
    /setTextIfChanged\(summaryEnclosureSource, enclosureSourceNote\(summary\.enclosure_source\)\)/,
  );
});
