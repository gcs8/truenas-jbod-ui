"use strict";

// #949: a storage-view disk shows its pool member's vdev and state, and a
// Fault drawn from that state uses the fault style, not the healthy one.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

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
  vm.runInContext(`${names.map(functionSource).join("\n")}\nthis.loaded = { ${names.join(", ")} };`, sandbox);
  return sandbox.loaded;
}

test("a storage-view tooltip names the vdev and the pool member state", () => {
  const { buildStorageViewTooltipLines } = loadFunctions(
    ["formatSlotHealth", "slotOccupancy", "buildStorageViewTooltipLines"],
    {
      currentPlatform: () => "core",
      persistentIdLabel: () => "GPTID",
      appendSmartTooltipMetrics: () => {},
      appendHeatmapTooltipLines: (lines) => lines,
      formatLogicalUnitIdValue: () => "n/a",
      getLiveBackedStorageViewSlot: () => null,
    },
  );
  const slot = {
    slot_index: 0,
    slot_label: "M2-1",
    occupied: true,
    state: "fault",
    device_name: "nvd0",
    pool_name: "flashpool",
    vdev_name: "mirror-0",
    health: "DEGRADED",
  };

  const lines = buildStorageViewTooltipLines(slot, { label: "NVMe Carrier" }, null);
  assert.ok(lines.includes("Vdev: mirror-0"), lines.join("\n"));
  assert.ok(lines.includes("Health: DEGRADED"), lines.join("\n"));

  const withoutVdev = buildStorageViewTooltipLines({ ...slot, vdev_name: null }, { label: "NVMe Carrier" }, null);
  assert.ok(!withoutVdev.some((line) => line.startsWith("Vdev:")), withoutVdev.join("\n"));
});

test("the storage-view detail panel lists the vdev beside the pool", () => {
  const detail = functionSource("renderDetail");
  const pool = detail.indexOf('kvRowIfMeaningful("Pool", storageViewSlot.pool_name)');
  const vdev = detail.indexOf('kvRowIfMeaningful("Vdev", storageViewSlot.vdev_name)');
  assert.notEqual(pool, -1, "the storage-view detail must keep its Pool row");
  assert.ok(vdev > pool, "the storage-view detail must show the Vdev row after Pool");
});

test("only a matched storage-view disk is drawn healthy; a fault keeps its own style", () => {
  // The tile and the detail pill map the runtime state "matched" to healthy
  // and pass every other state through, so "fault" draws as a fault.
  const mappings = APP_SOURCE.match(/storageViewSlot\.state === "matched" \? "healthy" : \(storageViewSlot\.state \|\| "unknown"\)/g) || [];
  assert.equal(mappings.length, 1);
  const tileMappings = APP_SOURCE.match(/const visualState = slot\.state === "matched" \? "healthy" : slot\.state;/g) || [];
  assert.equal(tileMappings.length, 2);
});
