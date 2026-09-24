"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
const BASE_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/base.html"), "utf8");
const STYLES = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");

function functionSource(name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = APP_SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
    const next = APP_SOURCE[index + 1];
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
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
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
    if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth === 0) return APP_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ Number, String, Boolean, Map, Array, Infinity, ...context });
  const source = names.map(functionSource).join("\n");
  const exports = names.map((name) => `this.__${name} = ${name};`).join("\n");
  vm.runInContext(`${source}\n${exports}`, sandbox, { filename: "main-ui-recovery.behavior.js" });
  return Object.fromEntries(names.map((name) => [name, sandbox[`__${name}`]]));
}

function classList(initial = []) {
  const values = new Set(initial);
  return {
    add(name) { values.add(name); },
    remove(name) { values.delete(name); },
    toggle(name, force) {
      if (force === undefined ? !values.has(name) : force) values.add(name);
      else values.delete(name);
    },
    contains(name) { return values.has(name); },
  };
}

function textNode(text = "") {
  return { textContent: text, classList: classList([]), dataset: {} };
}

test("background refreshes leave the status line alone so an earlier error stays visible", () => {
  const source = functionSource("refreshSnapshot");
  assert.match(source, /const background = reason === "auto-refresh" \|\| String\(reason\)\.endsWith\("-led-verify"\)/);
  assert.match(source, /if \(!background\) \{\s*setStatus\(refreshStatusMessage\(force, reason\)\);/);
  assert.match(source, /if \(!background\) \{\s*setStatus\("Inventory updated\."\);/);
  assert.match(source, /setStatus\(`Refresh failed: \$\{error\.message \|\| error\}`, "error"\)/, "a failed refresh is still reported");
  assert.match(functionSource("setStatus"), /setTextIfChanged\(statusText, message\)/);
});

test("live regions are written only when their text changes and decorative panels are not live", () => {
  for (const name of ["renderSummary", "renderUiPerfPanel", "renderHeatmapControls"]) {
    const source = functionSource(name);
    assert.match(source, /setTextIfChanged\(/, `${name} must use setTextIfChanged`);
  }
  assert.doesNotMatch(functionSource("renderSummary"), /mappingHealthSummary\.textContent =/);
  assert.doesNotMatch(functionSource("renderHeatmapControls"), /heatmapLegendStatus\.textContent =/);
  for (const id of ["mapping-health-summary", "heatmap-legend", "heatmap-metric-context", "ui-perf-summary"]) {
    const line = TEMPLATE.split("\n").find((candidate) => candidate.includes(`id="${id}"`));
    assert.ok(line, `${id} must exist`);
    assert.doesNotMatch(line, /aria-live/, `${id} must not be a live region`);
  }
  assert.match(TEMPLATE, /id="status-text"[^>]*role="status"[^>]*aria-live="polite"/);
});

test("a configured refresh interval outside the preset list is offered and used", () => {
  const { refreshIntervalOptionsFor, formatRefreshInterval } = loadFunctions(
    ["refreshIntervalOptionsFor", "formatRefreshInterval"],
    { supportedRefreshIntervals: [15, 30, 60, 300] },
  );
  assert.deepEqual([...refreshIntervalOptionsFor(45)], [15, 30, 45, 60, 300]);
  assert.deepEqual([...refreshIntervalOptionsFor(30)], [15, 30, 60, 300]);
  assert.deepEqual([...refreshIntervalOptionsFor(0)], [15, 30, 60, 300]);
  assert.equal(formatRefreshInterval(45), "45 sec");
  assert.equal(formatRefreshInterval(120), "2 min");
  assert.equal(formatRefreshInterval(60), "1 min");

  const options = [15, 30, 60, 300].map((value) => ({ value: String(value), textContent: "" }));
  const inserted = [];
  const select = {
    options,
    insertBefore(option, before) { inserted.push([option.value, option.textContent, before?.value ?? null]); },
  };
  const { ensureRefreshIntervalOption } = loadFunctions(["ensureRefreshIntervalOption", "formatRefreshInterval"], {
    refreshIntervalSelect: select,
    state: { refreshIntervalSeconds: 45 },
    document: { createElement: () => ({ value: "", textContent: "" }) },
  });
  ensureRefreshIntervalOption();
  assert.deepEqual(inserted, [["45", "45 sec (server default)", "60"]]);

  assert.match(APP_SOURCE, /refreshIntervalSeconds: bootstrapRefreshInterval,/);
  assert.match(APP_SOURCE, /state\.refreshIntervalSeconds = refreshIntervalOptions\.includes\(selected\) \? selected : 30;/);
});

test("an empty enclosure list says so instead of claiming an automatic choice", () => {
  assert.match(TEMPLATE, /<option value="">No enclosures found<\/option>/);
  assert.match(functionSource("renderSelectors"), /No enclosures found/);
  assert.doesNotMatch(TEMPLATE, /Auto-selected/);
  assert.doesNotMatch(APP_SOURCE, /Auto-selected/);
});

test("search reports how many bays match, offers a clear control, and selects a lone match", () => {
  const { searchSummaryText } = loadFunctions(["searchSummaryText"]);
  assert.equal(searchSummaryText(3, 60, "tank"), "3 of 60 bays match");
  assert.equal(searchSummaryText(0, 60, "zzz"), 'No bays match "zzz".');
  assert.equal(searchSummaryText(0, 60, ""), "");

  const tiles = [
    { dataset: { slot: "4" }, classList: classList(["filtered-out"]) },
    { dataset: { slot: "7" }, classList: classList([]) },
  ];
  const summary = textNode();
  const clearButton = { classList: classList(["hidden"]) };
  const selected = [];
  const state = { search: "sn-0007", selectedSlot: null };
  const { renderSearchSummary } = loadFunctions(["renderSearchSummary", "searchSummaryText"], {
    grid: { querySelectorAll: () => tiles },
    searchBox: { value: " SN-0007 " },
    searchSummary: summary,
    searchClearButton: clearButton,
    state,
    setTextIfChanged(node, text) { node.textContent = text; },
    mappingEditorHasUnsavedChanges: () => false,
    selectSlot(slot) { selected.push(slot); },
  });
  renderSearchSummary({ autoSelect: true });
  assert.equal(summary.textContent, "1 of 2 bays match");
  assert.equal(summary.classList.contains("hidden"), false);
  assert.equal(clearButton.classList.contains("hidden"), false);
  assert.deepEqual(selected, [7]);

  renderSearchSummary();
  assert.deepEqual(selected, [7], "a plain re-render never changes the selection");

  state.search = "";
  renderSearchSummary({ autoSelect: true });
  assert.equal(summary.classList.contains("hidden"), true);
  assert.equal(clearButton.classList.contains("hidden"), true);

  assert.match(TEMPLATE, /id="search-clear"[^>]*aria-controls="search-box"/);
  assert.match(TEMPLATE, /id="search-summary"[^>]*role="status"/);
  assert.match(APP_SOURCE, /searchClearButton\.addEventListener\("click", clearSearch\)/);
  assert.match(APP_SOURCE, /event\.key === "Escape" && state\.search/);
});

test("bay tiles get a short accessible name and point at the tooltip for the rest", () => {
  const { slotAccessibleName } = loadFunctions(["slotAccessibleName", "slotLocationLabel", "stateLabel"], {
    currentPlatform: () => "core",
  });
  assert.equal(
    slotAccessibleName({ slot: 5, slot_label: "05", device_name: "da5", pool_name: "tank", health: "ONLINE", state: "healthy" }),
    "Slot 05, da5, pool tank, online",
  );
  assert.equal(slotAccessibleName({ slot: 6, slot_label: "06", state: "empty" }), "Slot 06, empty");
  assert.equal(
    slotAccessibleName({ slot: 1, slot_label: "Disk 1", physical_location_known: false, device_name: "sda", state: "unmapped" }),
    "Disk 1, sda, unmapped",
  );

  const renderGrid = functionSource("renderGrid");
  assert.match(renderGrid, /tile\.setAttribute\("aria-label", slotAccessibleName\(slot\)\)/);
  assert.match(renderGrid, /tile\.setAttribute\("aria-describedby", "slot-tooltip"\)/);
  assert.doesNotMatch(APP_SOURCE, /function slotTooltip\(/, "the multi-line tooltip is no longer an accessible name");
  assert.match(TEMPLATE, /id="slot-tooltip"[^>]*role="tooltip"/);
});

test("arrow keys move by row across the bay layout", () => {
  const makeTile = (slot, hidden = false) => ({
    dataset: { slot: String(slot) },
    classList: classList(hidden ? ["filtered-out"] : []),
    disabled: false,
  });
  const tiles = [0, 1, 2, 3, 4, 5].map((slot) => makeTile(slot, slot === 4));
  const visible = () => tiles.filter((tile) => !tile.classList.contains("filtered-out"));
  const { nextGridTileForKey } = loadFunctions(
    ["nextGridTileForKey", "adjacentRowTile", "gridSlotPosition"],
    {
      activeLayoutRows: () => [[0, 1, 2], [3, 4, 5]],
      visibleGridTiles: visible,
    },
  );
  assert.equal(nextGridTileForKey(tiles[1], "ArrowDown"), tiles[3], "the hidden bay below is skipped for its nearest neighbour");
  assert.equal(nextGridTileForKey(tiles[0], "ArrowDown"), tiles[3]);
  assert.equal(nextGridTileForKey(tiles[5], "ArrowUp"), tiles[2]);
  assert.equal(nextGridTileForKey(tiles[1], "ArrowUp"), null, "the top row has nothing above it");
  assert.equal(nextGridTileForKey(tiles[3], "ArrowRight"), tiles[5], "Right still walks the visible order");
  assert.equal(nextGridTileForKey(tiles[3], "ArrowLeft"), tiles[2]);

  const { nextGridTileForKey: withoutLayout } = loadFunctions(
    ["nextGridTileForKey", "adjacentRowTile", "gridSlotPosition"],
    { activeLayoutRows: () => [], visibleGridTiles: visible },
  );
  assert.equal(withoutLayout(tiles[1], "ArrowDown"), tiles[2], "without a layout the old linear order is kept");
});

test("the empty-bay detail is one line and SMART rows are collapsed behind a summary", () => {
  const source = functionSource("renderLiveSlotDetail");
  assert.match(source, /isEmptyBay\(slot\)\s*\? '<p class="detail-empty-bay">This bay is empty\.<\/p>'/);
  assert.equal((source.match(/\bkvRow\(/g) || []).length, 4, "only Device, Serial, Pool and Health are always shown");
  assert.match(source, /smartDetailsDisclosure\(\[/);
  assert.match(functionSource("smartDetailsDisclosure"), /<summary>SMART details<\/summary>/);
  assert.match(source, /state\.detailSmartExpanded = Boolean\(event\.target\.open\)/);
  const { isEmptyBay, smartDetailsDisclosure } = loadFunctions(["isEmptyBay", "smartDetailsDisclosure"], {
    state: { detailSmartExpanded: true },
  });
  assert.equal(isEmptyBay({ state: "empty" }), true);
  assert.equal(isEmptyBay({ state: "empty", serial: "SN-0001" }), false);
  assert.equal(smartDetailsDisclosure(["", ""]), "");
  assert.match(smartDetailsDisclosure(["<div>row</div>"]), /<details class="detail-smart-details" open>/);
});

test("the import control follows the write policy instead of re-enabling itself", () => {
  const button = { disabled: false, title: "" };
  const { renderMappingImportControl } = loadFunctions(["renderMappingImportControl"], {
    importMappingsButton: button,
    mappingImportUnavailable: null,
    mappingImportUnavailableReason: () => null,
    writePolicyAllowsWrites: () => false,
    writePolicyReason: () => "Sign in first.",
  });
  renderMappingImportControl();
  assert.equal(button.disabled, true);
  assert.equal(button.title, "Sign in first.");
});

test("a newer server version asks for a reload once and never nags", () => {
  const note = textNode("Up to date");
  const statuses = [];
  const state = { appUpdated: false };
  const { noteServerAppVersion } = loadFunctions(["noteServerAppVersion", "renderAppVersionNote"], {
    bootstrap: { appVersion: "0.23.0" },
    state,
    appVersionNote: note,
    setTextIfChanged(node, text) { node.textContent = text; },
    setStatus(message, tone) { statuses.push({ message, tone }); },
  });
  noteServerAppVersion("0.23.0");
  assert.equal(note.textContent, "Up to date");
  noteServerAppVersion("0.24.0");
  assert.equal(note.textContent, "The app was updated. Reload this page.");
  assert.deepEqual(statuses, [{ message: "The app was updated. Reload this page.", tone: "error" }]);
  noteServerAppVersion("0.24.0");
  assert.equal(statuses.length, 1);
  noteServerAppVersion(undefined);
  assert.equal(state.appUpdated, true);

  assert.match(BASE_TEMPLATE, /href="\{\{ url_for\('static', path='style\.css'\) \}\}\?v=\{\{ app_version \}\}"/);
  assert.match(BASE_TEMPLATE, /src="\{\{ url_for\('static', path='app\.js'\) \}\}\?v=\{\{ app_version \}\}"/);
  assert.match(TEMPLATE, /appVersion: \{\{ app_version \| tojson \}\},/);
  assert.match(TEMPLATE, /release_status_payload\.status == "disabled" %\}\{% set release_note = "" %\}/);
});

test("the enclosure header has one Connections button and the full page link lives in its panel", () => {
  assert.match(TEMPLATE, /id="sas-fabric-toggle-button"[^>]*aria-controls="sas-fabric-panel"[^>]*>\s*Connections\s*</);
  const panelStart = TEMPLATE.indexOf('id="sas-fabric-panel"');
  const linkStart = TEMPLATE.indexOf('id="sas-fabric-view-link"');
  assert.ok(panelStart > 0 && linkStart > panelStart, "the full-page link sits inside the Connections panel");
  assert.match(TEMPLATE, /id="sas-fabric-view-link"[^>]*>\s*Open full page\s*</);
  assert.match(TEMPLATE, /class="enclosure-alias-edit-text">Rename</);
});

test("overflow bay counts are real buttons", () => {
  assert.match(
    functionSource("renderSasFabricSlotList"),
    /<button type="button" class="sas-fabric-slot-overflow" data-sas-fabric-expand-slots=/,
  );
  assert.doesNotMatch(APP_SOURCE, /role="button" tabindex="0" data-sas-fabric-expand-slots/);
  assert.match(STYLES, /\.sas-fabric-slot-overflow \{[^}]*font: inherit;/);
});
