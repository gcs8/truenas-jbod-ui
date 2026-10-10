"use strict";

// #925: every bay keeps its LED dot, and a bay with its locate light on says so
// and blinks the dot (a steady ring instead when motion is reduced).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const STYLE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");

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

// Top-level rules only, as [selectors, declarations]; @media blocks are read
// separately below.
function cssRules(source) {
  const rules = [];
  for (const match of source.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    rules.push([match[1].split(",").map((value) => value.trim()), match[2]]);
  }
  return rules;
}

function mediaBlock(query) {
  const start = STYLE.indexOf(`@media ${query}`);
  assert.notEqual(start, -1, `missing @media ${query}`);
  let depth = 0;
  for (let index = STYLE.indexOf("{", start); index < STYLE.length; index += 1) {
    if (STYLE[index] === "{") depth += 1;
    else if (STYLE[index] === "}") {
      depth -= 1;
      if (depth === 0) return STYLE.slice(STYLE.indexOf("{", start) + 1, index);
    }
  }
  assert.fail(`@media ${query} must close`);
}

function declarations(source, selector) {
  return cssRules(source)
    .filter(([selectors]) => selectors.includes(selector))
    .map(([, body]) => body)
    .join("\n");
}

test("no rule hides a bay's LED dot, including NVMe carrier and boot-device cards", () => {
  const hiding = cssRules(STYLE)
    .filter(([selectors, body]) => selectors.some((selector) => selector.includes(".slot-status-led"))
      && /display:\s*none/.test(body))
    .map(([selectors]) => selectors.join(", "));
  assert.deepEqual(hiding, []);
});

test("dimmed bays fade the tile but keep the LED dot at full opacity", () => {
  for (const [selectors, body] of cssRules(STYLE)) {
    if (!selectors.some((selector) => /\.(peer|fabric)-dimmed\b.*\.slot-status-led/.test(selector))) continue;
    const opacity = body.match(/opacity:\s*([\d.]+)/);
    assert.ok(!opacity || Number(opacity[1]) >= 1, `${selectors.join(", ")} fades the dot to ${opacity?.[1]}`);
  }
});

test("the dot blinks amber while the locate light is on, and holds a steady ring under reduced motion", () => {
  const keyframes = STYLE.match(/@keyframes\s+slot-led-locate\s*\{([\s\S]*?)\}\s*\}/);
  assert.ok(keyframes, "@keyframes slot-led-locate must exist");
  const identify = declarations(STYLE, ".slot-tile.state-identify .slot-status-led");
  assert.match(identify, /background:\s*var\(--identify\)/);
  assert.match(identify, /animation:[^;]*\bslot-led-locate\b[^;]*\binfinite\b/);

  const reduced = mediaBlock("(prefers-reduced-motion: reduce)");
  const steady = declarations(reduced, ".slot-tile.state-identify .slot-status-led");
  assert.match(steady, /animation:\s*none/);
  // An outline, because the NVMe, boot and UniFi dots set their own box-shadow.
  assert.match(steady, /outline:\s*\d+px solid var\(--identify\)/, "an extra amber ring stands in for the blink");
});

test("a bay's accessible name and tooltip say when its locate light is on", () => {
  const context = {
    currentPlatform: () => "core",
    persistentIdLabel: () => "GPTID",
    appendSmartTooltipMetrics: () => {},
    appendHeatmapTooltipLines: (lines) => lines,
    formatSesHostValue: () => "n/a",
    formatSesStateValue: () => "n/a",
    formatLogicalUnitIdValue: () => "n/a",
    getLiveBackedStorageViewSlot: (view, slot) => view.live?.[slot.slot_index] || null,
  };
  const { slotAccessibleName, buildTooltipLines, buildStorageViewTooltipLines } = loadFunctions(
    ["slotAccessibleName", "slotLocationLabel", "stateLabel", "buildTooltipLines", "buildStorageViewTooltipLines"],
    context,
  );
  const lit = { slot: 4, slot_label: "04", device_name: "da4", pool_name: "tank", health: "ONLINE", state: "identify", identify_active: true };
  const dark = { ...lit, state: "healthy", identify_active: false };

  assert.equal(slotAccessibleName(lit), "Slot 04, da4, pool tank, online, Locate light on");
  assert.equal(slotAccessibleName(dark), "Slot 04, da4, pool tank, online");
  assert.ok(buildTooltipLines(lit, null).includes("Locate light on"));
  assert.ok(!buildTooltipLines(dark, null).includes("Locate light on"));

  // A saved enclosure view reads the locate light from the live bay behind it.
  const viewSlot = { slot_index: 0, slot_label: "Bay 1", occupied: true, device_name: "da4" };
  assert.ok(buildStorageViewTooltipLines(viewSlot, { label: "Front", live: [lit] }, null).includes("Locate light on"));
  assert.ok(!buildStorageViewTooltipLines(viewSlot, { label: "Front", live: [dark] }, null).includes("Locate light on"));
  assert.ok(!buildStorageViewTooltipLines(viewSlot, { label: "Front" }, null).includes("Locate light on"));
});
