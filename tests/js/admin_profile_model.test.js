"use strict";

// The profile and layout model of the admin page lives in its own script
// (admin_profile_model.js): it loads on its own, admin.js keeps every old name
// bound to it, and the page loads it first. Nothing it computes changed.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const MODEL_PATH = path.join(ROOT, "admin_service/static/admin_profile_model.js");
const ADMIN_PATH = path.join(ROOT, "admin_service/static/admin.js");
const BASE = fs.readFileSync(path.join(ROOT, "admin_service/templates/base.html"), "utf8");
const MODEL_NAMES = [
  "countSlots", "buildRectangularProfileLayout", "serializeProfileLayoutText", "parseProfileLayoutText",
  "layoutsEqual", "detectGeneratedLayoutOrdering", "buildProfileRows", "normalizeProfilePreviewRows",
  "normalizeProfilePreviewRowGroups", "inferProfilePreviewDriveScale", "slugify", "splitDelimitedLines",
  "serializeDelimitedLines", "normalizeSlotLabelMap", "ALLOWED_M2_SIZES", "normalizeSlotSizeMap",
  "parseSlotLabelsText", "serializeSlotLabelsText", "parseSlotSizesText", "serializeSlotSizesText",
];

function browserModel() {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(MODEL_PATH, "utf8"), context, { filename: MODEL_PATH });
  return context.AdminProfileModel;
}

test("the profile model loads on its own, as a browser script and as a CommonJS module", () => {
  const page = browserModel();
  assert.deepEqual(Object.keys(page), MODEL_NAMES);
  const loaded = require("node:child_process").execFileSync(process.execPath, ["-e", [
    `const api = require(${JSON.stringify(MODEL_PATH)});`,
    "const files = Object.keys(require.cache).map((name) => require('node:path').basename(name));",
    "process.stdout.write(JSON.stringify({ files, names: Object.keys(api) }));",
  ].join("\n")], { encoding: "utf8" });
  const result = JSON.parse(loaded);
  assert.deepEqual(result.files, ["admin_profile_model.js"]);
  assert.deepEqual(result.names, MODEL_NAMES);
});

test("the model owns no page, request or saved state", () => {
  const source = fs.readFileSync(MODEL_PATH, "utf8");
  for (const pattern of [/\bdocument\b/, /\bwindow\b/, /\bfetch\w*\(/, /\bstate\.\w/, /\belements\./, /setTimeout/, /\.present\b/, /require\(/]) {
    assert.doesNotMatch(source, pattern);
  }
});

test("admin.js defines none of the moved names and binds every one from the model", () => {
  const admin = fs.readFileSync(ADMIN_PATH, "utf8");
  for (const name of MODEL_NAMES) {
    assert.doesNotMatch(admin, new RegExp(`(?:function|const|let|var) ${name}\\b`), name);
  }
  const binding = admin.match(/^  const \{([^}]*)\} = globalThis\.AdminProfileModel;$/m);
  assert.ok(binding, "admin.js must bind the model's names");
  assert.deepEqual(binding[1].split(",").map((name) => name.trim()).filter(Boolean), MODEL_NAMES);
  assert.ok(admin.indexOf(binding[0]) < admin.indexOf("\n  function "), "the names are bound before any admin.js function");
});

test("base.html loads the profile model before admin.js", () => {
  const modelAt = BASE.indexOf("path='admin_profile_model.js'");
  assert.ok(modelAt > 0 && modelAt < BASE.indexOf("path='admin.js'"));
  assert.match(BASE, /<script src="\{\{ url_for\('static', path='admin_profile_model\.js'\) \}\}" defer><\/script>/);
});

test("layout text keeps its padding, gaps, limits and error wording", () => {
  const api = require(MODEL_PATH);
  assert.equal(api.serializeProfileLayoutText([[null, 9, 10], [0]]), "-- 09 10\n00");
  assert.deepEqual(api.parseProfileLayoutText("1 -- 0\n", 1, 3, 2), { layout: [[1, null, 0]], error: null });
  assert.equal(api.parseProfileLayoutText("0\n1", 1, 2, 2).error, "Custom matrix expects 1 row.");
  assert.equal(api.parseProfileLayoutText("0 1 2", 2, 2, 3).error, "Row 1 cannot be wider than 2 columns.");
  assert.equal(api.parseProfileLayoutText("0 a", 1, 2, 2).error, "Custom matrix token \"a\" is not a slot number or \"--\".");
  assert.equal(api.parseProfileLayoutText("0 0", 1, 2, 2).error, "Custom matrix repeats slot 0.");
  assert.equal(api.parseProfileLayoutText("0", 1, 2, 2).error, "Custom matrix must contain exactly 2 visible slot numbers.");
});

test("generated orderings, row building and preview geometry keep their results", () => {
  const api = require(MODEL_PATH);
  assert.deepEqual(api.buildRectangularProfileLayout(2, 3, 5), [[3, 4], [0, 1, 2]]);
  assert.deepEqual(api.buildRectangularProfileLayout(2, 2, 4, "column-major-top"), [[0, 2], [1, 3]]);
  assert.equal(api.detectGeneratedLayoutOrdering([[1, 3], [0, 2]], 2, 2, 4), "column-major-bottom");
  assert.equal(api.detectGeneratedLayoutOrdering([[3, 0], [1, 2]], 2, 2, 4), null);
  assert.equal(api.countSlots([[0, null], [1, "2"]]), 2);
  const saved = [[5]];
  assert.equal(api.buildProfileRows({ slot_layout: saved }), saved);
  assert.deepEqual(api.buildProfileRows({ rows: 1, columns: 2 }), [[0, 1]]);
  assert.deepEqual(api.normalizeProfilePreviewRows([[0], "x", null]), [[0]]);
  assert.deepEqual(api.normalizeProfilePreviewRowGroups({ row_groups: ["2", 0, 1.5, 3] }), [2, 3]);
  assert.equal(api.inferProfilePreviewDriveScale({ face_style: "unifi-drive" }, 8), "3.5");
  assert.equal(api.inferProfilePreviewDriveScale({}, 2), "2.5");
  assert.equal(api.inferProfilePreviewDriveScale(null, 8), "default");
});

test("slot label, size and list text keep their coercion and order", () => {
  const api = require(MODEL_PATH);
  assert.equal(api.slugify("  Front Bays!  "), "front-bays");
  assert.equal(api.slugify("!!!", "view"), "view");
  assert.deepEqual(api.splitDelimitedLines("a, b\nA\r\na"), ["a", "b", "A"]);
  assert.equal(api.serializeDelimitedLines(["a", "", "b"]), "a\nb");
  assert.equal(api.serializeDelimitedLines("a"), "");
  assert.deepEqual(api.parseSlotLabelsText("2 = Two\nx=1\n1:One"), { 1: "One", 2: "Two" });
  assert.equal(api.serializeSlotLabelsText({ 10: "Ten", 2: "Two", "-1": "bad", 3: " " }), "2=Two\n10=Ten");
  assert.deepEqual(api.parseSlotSizesText("0=2280\n1=2200\n2:22110"), { 0: "2280", 2: "22110" });
  assert.equal(api.serializeSlotSizesText({ 3: "2242", 1: "2230", 2: "9999" }), "1=2230\n3=2242");
  assert.deepEqual([...api.ALLOWED_M2_SIZES], ["2230", "2242", "2260", "2280", "22110"]);
});
