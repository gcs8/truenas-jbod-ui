"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
const FABRIC_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/sas_fabric.html"), "utf8");
const STYLE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const bodyStart = source.indexOf("{", start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

function loadFunctions(source, names, context = {}) {
  const sandbox = { ...context };
  vm.createContext(sandbox);
  vm.runInContext(`${names.map((name) => functionSource(source, name)).join("\n")}\nthis.loaded = { ${names.join(", ")} };`, sandbox);
  return sandbox.loaded;
}

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function cssRule(selector) {
  const match = STYLE.match(new RegExp(`(?:^|\\n)${escapeRegex(selector)}\\s*\\{([\\s\\S]*?)\\}`));
  assert.ok(match, `missing CSS rule ${selector}`);
  return match[1];
}

function cssDeclarationsForSelector(selector) {
  const declarations = [];
  for (const match of STYLE.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = match[1].split(",").map((value) => value.trim());
    if (selectors.includes(selector)) declarations.push(match[2]);
  }
  assert.notEqual(declarations.length, 0, `missing CSS selector ${selector}`);
  return declarations.join("\n");
}

test("mapping health separates matched empty unmatched and unknown bays", () => {
  const { summarizeMappingHealth } = loadFunctions(APP_SOURCE, ["summarizeMappingHealth"]);
  const result = summarizeMappingHealth([
    { slot: 0, state: "healthy", mapping_source: "api" },
    { slot: 1, state: "identify", mapping_source: "manual" },
    { slot: 2, state: "fault", mapping_source: "ssh" },
    { slot: 3, state: "empty", mapping_source: "empty" },
    { slot: 4, state: "unmapped", mapping_source: "unknown" },
    { slot: 5, state: "unknown", mapping_source: "unknown" },
  ], 7);

  assert.deepEqual(
    { populated: result.populated, matched: result.matched, empty: result.empty, unmatched: result.unmatched, unknown: result.unknown },
    { populated: 4, matched: 3, empty: 1, unmatched: 1, unknown: 2 },
  );
  assert.equal(result.total, 7);
  assert.match(result.headline, /1 populated bay needs mapping/);
  assert.equal(
    summarizeMappingHealth([{ slot: 0, state: "unknown" }], 1).headline,
    "1 bay has unknown mapping state.",
  );
});

test("mapping health reports all matched and names bounded evidence sources", () => {
  const { summarizeMappingHealth, mappingHealthEvidenceNote } = loadFunctions(
    APP_SOURCE,
    ["summarizeMappingHealth", "mappingHealthEvidenceNote"],
  );
  const slots = [
    { slot: 0, state: "healthy", mapping_source: "api" },
    { slot: 1, state: "healthy", mapping_source: "manual" },
    { slot: 2, state: "empty", mapping_source: "empty" },
  ];
  assert.equal(summarizeMappingHealth(slots, 3).headline, "All 2 populated bays are matched.");
  assert.equal(
    mappingHealthEvidenceNote(slots, "2026-08-30T12:00:00Z"),
    "Evidence: API and saved mapping. Snapshot: 2026-08-30T12:00:00Z.",
  );
});

test("mapping health scope follows the selected saved view", () => {
  const { mappingHealthScope, summarizeMappingHealth } = loadFunctions(
    APP_SOURCE,
    ["mappingHealthScope", "summarizeMappingHealth"],
  );
  const scope = mappingHealthScope(
    {
      slots: [{ slot: 0, state: "healthy", mapping_source: "api" }],
      layout_slot_count: 1,
      last_updated: "2026-08-30T12:00:00Z",
    },
    {
      label: "Saved shelf",
      slot_count: 3,
      slots: [
        { slot_index: 0, state: "matched", occupied: true, source: "inventory_candidate" },
        { slot_index: 1, state: "unmapped", occupied: true, source: "inventory_candidate" },
        { slot_index: 2, state: "empty", occupied: false, source: "placeholder" },
      ],
    },
  );

  assert.equal(scope.label, "Saved shelf");
  assert.equal(scope.layoutSlotCount, 3);
  const health = summarizeMappingHealth(scope.slots, scope.layoutSlotCount);
  assert.deepEqual(
    {
      matched: health.matched,
      empty: health.empty,
      unmatched: health.unmatched,
      unknown: health.unknown,
      populated: health.populated,
      total: health.total,
      headline: health.headline,
    },
    {
      matched: 1,
      empty: 1,
      unmatched: 1,
      unknown: 0,
      populated: 2,
      total: 3,
      headline: "1 populated bay needs mapping.",
    },
  );
  assert.equal(scope.slots[0].mapping_source, "inventory_candidate");
});

test("main template exposes mapping health and polite status regions", () => {
  assert.match(TEMPLATE, /id="mapping-health-summary"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(TEMPLATE, /id="mapping-health-evidence"/);
  assert.match(TEMPLATE, /id="status-text"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(FABRIC_TEMPLATE, /id="fabric-status-text"[^>]*role="status"[^>]*aria-live="polite"/);
});

test("slot states have glyph legends and patterned high-contrast cues", () => {
  for (const [state, glyph] of Object.entries({ healthy: "✓", empty: "○", identify: "◎", fault: "!", unknown: "?", unmapped: "◇" })) {
    assert.match(TEMPLATE, new RegExp(`swatch ${state}[^>]*>${escapeRegex(glyph)}<`));
    assert.match(STYLE, new RegExp(`\\.slot-tile\\.state-${state}::after[\\s\\S]*content:\\s*"${escapeRegex(glyph)}"`));
  }
  assert.match(STYLE, /@media\s*\(forced-colors:\s*active\)/);
  assert.match(STYLE, /repeating-linear-gradient/);
});

test("selection and topology rings reset the state-chip geometry", () => {
  for (const selector of [
    ".slot-tile.selected::after",
    ".slot-tile.peer-highlight::after",
    ".slot-tile.fabric-highlight::after",
  ]) {
    const rule = cssRule(selector);
    assert.match(rule, /width:\s*auto;/);
    assert.match(rule, /height:\s*auto;/);
    assert.match(rule, /background:\s*none;/);
  }
});

test("drawer faces floor tray widths and own horizontal scrolling", () => {
  const shell = cssRule('.chassis-shell[data-face-style="drawer"]');
  assert.match(shell, /overflow-x:\s*auto;/);
  assert.match(shell, /--slot-bay-aspect:\s*0\.46\s*\/\s*1;/);

  const group = cssRule('.chassis-shell[data-face-style="drawer"] .row-group');
  assert.match(group, /minmax\(72px,\s*1fr\)/);
});

test("legacy chassis faces floor slot widths and own horizontal scrolling", () => {
  for (const [face, minimum] of Object.entries({
    generic: "72px",
    "front-drive": "72px",
    "rear-drive": "72px",
    "top-loader": "76px",
  })) {
    const shellSelector = `.chassis-shell[data-face-style="${face}"]`;
    const shell = cssDeclarationsForSelector(shellSelector);
    const group = cssDeclarationsForSelector(`${shellSelector} .row-group`);
    assert.match(shell, /overflow-x:\s*auto;/);
    assert.match(shell, new RegExp(`--slot-track-min-width:\\s*${escapeRegex(minimum)};`));
    assert.match(group, /minmax\(var\(--slot-track-min-width\),\s*1fr\)/);
  }

  assert.match(cssRule(".enclosure-face"), /overflow-x:\s*hidden;/);
});

test("diagnostic evidence starts collapsed behind an operator summary", () => {
  const source = functionSource(FABRIC_SOURCE, "renderDiagnosticEvidencePanel");
  assert.match(source, /<details class="fabric-diagnostic-evidence/);
  assert.match(source, /<summary>/);
  assert.match(source, /Fault Evidence/);
  assert.doesNotMatch(source, /<div class="fabric-diagnostic-evidence impact-/);
});

test("Fabric modes retain simpler pressed-button toolbar semantics", () => {
  assert.match(FABRIC_TEMPLATE, /class="fabric-mode-tabs" role="group" aria-label="Storage Fabric map mode"/);
  assert.equal((FABRIC_TEMPLATE.match(/data-fabric-mode=/g) || []).length, 4);
  assert.equal((FABRIC_TEMPLATE.match(/aria-pressed=/g) || []).length, 4);
  assert.doesNotMatch(FABRIC_TEMPLATE, /role="tab(list)?"/);
});

test("heuristic and temperature metrics explain derivation and action context", () => {
  const definitions = functionSource(APP_SOURCE, "heatmapMetricDefinitions");
  assert.match(definitions, /label:\s*"Derived Attention Score"/);
  assert.match(definitions, /label:\s*"Temperature \(C\)"/);
  assert.match(definitions, /label:\s*"Temperature vs View Average \(C\)"/);
  assert.match(TEMPLATE, /id="heatmap-metric-context"[^>]*aria-live="polite"/);
  assert.match(APP_SOURCE, /Higher scores combine relative temperature, errors, and write load; inspect the selected bay before acting\./);
  assert.match(APP_SOURCE, /Use the drive vendor's warning and critical thresholds when available\./);
});
