"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const ADMIN_SOURCE = fs.readFileSync(path.join(ROOT, "admin_service/static/admin.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
const STYLE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");

function functionSource(source, name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = source.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = source.indexOf("{", source.indexOf(")", start));
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function assertAbsent(source, pattern, message) {
  assert.equal(pattern.test(source), false, message);
}

function assertPresent(source, pattern, message) {
  assert.equal(pattern.test(source), true, message);
}

test("dead storage-view panel contract stays absent while the main saved-view path remains", () => {
  const deletedIds = [
    "storage-views-panel",
    "storage-views-summary",
    "storage-view-list",
    "storage-view-empty",
    "storage-view-content",
    "storage-view-title",
    "storage-view-note",
    "storage-view-meta",
    "storage-view-grid",
    "storage-view-mapping-list",
  ];
  for (const id of deletedIds) {
    assertAbsent(APP_SOURCE, new RegExp(`getElementById\\("${id}"\\)`), `${id} must not be queried`);
    assertAbsent(TEMPLATE, new RegExp(`id="${id}"`), `${id} must not be rendered`);
  }

  for (const symbol of [
    "storageViewRuntimeMeta",
    "renderStorageViewsRuntime",
    "selectStorageViewRuntimeFromCard",
    "storageViewList",
  ]) {
    assertAbsent(APP_SOURCE, new RegExp(`\\b${symbol}\\b`), `${symbol} must stay deleted`);
  }

  const deletedCssTokens = [
    "storage-views-panel",
    "storage-views-layout",
    "storage-view-list",
    "storage-view-card",
    "storage-view-card-header",
    "storage-view-runtime-head",
    "storage-view-runtime-detail",
    "storage-view-content",
    "profile-preview-meta",
    "meta-chip",
    "storage-view-runtime-grid",
    "storage-view-runtime-cell",
    "storage-view-runtime-slot-label",
    "storage-view-runtime-device",
    "storage-view-runtime-secondary",
    "storage-view-runtime-cell-card",
    "storage-view-mapping-list",
    "storage-view-mapping-item",
    "storage-view-mapping-slot",
    "storage-view-mapping-body",
    "storage-view-mapping-title",
    "storage-view-mapping-copy",
  ];
  for (const token of deletedCssTokens) {
    assertAbsent(STYLE, new RegExp(`\\.${token}(?![a-zA-Z0-9_-])`), `${token} CSS must stay deleted`);
  }

  const renderAll = functionSource(APP_SOURCE, "renderAll");
  assert.match(renderAll, /renderGrid\(\);/);
  assert.match(renderAll, /renderDetail\(\);/);
  assert.match(renderAll, /renderSummary\(\);/);
  assertPresent(APP_SOURCE, /function renderStorageViewGrid\(/, "main saved-view grid renderer must remain");
  assertPresent(STYLE, /\.storage-view-runtime-card--nvme/, "shared main-grid runtime-card CSS must remain");

  const handlerStart = APP_SOURCE.indexOf('enclosureSelect.addEventListener("change"');
  assert.notEqual(handlerStart, -1, "live enclosure/view selector handler must exist");
  const handlerSource = APP_SOURCE.slice(handlerStart, handlerStart + 1800);
  assert.match(handlerSource, /confirmMappingDraftDiscard\(\)/);
  assert.match(handlerSource, /closeEnclosureAliasEditor\(false\);/);
  assert.match(handlerSource, /state\.selectedStorageViewRuntimeId = rawValue\.slice\("view:"\.length\);/);
  assert.match(handlerSource, /renderAll\(\);/);
  assert.match(handlerSource, /syncLocation\(\);/);
  assert.match(handlerSource, /ensureHeatmapData\(\);/);
});

test("dead per-key cancellation stays absent while pagehide keeps cancel-all wiring", () => {
  assertAbsent(ADMIN_SOURCE, /function cancelRuntimeActionPolling\(/, "per-key cancellation must stay deleted");
  assertPresent(ADMIN_SOURCE, /function cancelAllRuntimeActionPolling\(\)/, "cancel-all must remain");
  assertPresent(
    ADMIN_SOURCE,
    /window\.addEventListener\("pagehide", \(\) => cancelAllRuntimeActionPolling\(\)\)/,
    "pagehide must cancel every pending runtime action",
  );
});
