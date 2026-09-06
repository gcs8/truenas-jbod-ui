"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = source.indexOf(")", start);
  const bodyStart = source.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < source.length; index += 1) {
    const character = source[index];
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
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadScopedUrl(state) {
  const context = vm.createContext({
    state,
    URL,
    URLSearchParams,
    currentLiveEnclosureId: () => state.selectedEnclosureId || null,
    window: { location: { origin: "https://synthetic.example" } },
  });
  vm.runInContext(
    `${functionSource(APP_SOURCE, "buildSelectionParams")}\n${functionSource(APP_SOURCE, "buildScopedUrl")}\nthis.buildScopedUrl = buildScopedUrl;`,
    context,
  );
  return context.buildScopedUrl;
}

function parsedInternal(url) {
  return new URL(url, "https://synthetic.example");
}

test("scoped URL keeps existing revision and adds exact drawer selection once", () => {
  const buildScopedUrl = loadScopedUrl({
    selectedSystemId: "synthetic-system-a",
    selectedEnclosureId: "synthetic-shelf-a::dell-md1280-drawer-top-42",
    selectedStorageViewRuntimeId: "",
  });
  const revision = "b".repeat(64);
  const result = buildScopedUrl(`/api/slots/7/mapping?expected_revision=${revision}`);
  const parsed = parsedInternal(result);

  assert.equal(result.startsWith("/api/slots/7/mapping?"), true);
  assert.deepEqual(parsed.searchParams.getAll("expected_revision"), [revision]);
  assert.deepEqual(parsed.searchParams.getAll("system_id"), ["synthetic-system-a"]);
  assert.deepEqual(parsed.searchParams.getAll("enclosure_id"), [
    "synthetic-shelf-a::dell-md1280-drawer-top-42",
  ]);
});

test("scoped URL replaces duplicate stale scope while preserving unrelated query and fragment", () => {
  const buildScopedUrl = loadScopedUrl({
    selectedSystemId: "synthetic-system-new",
    selectedEnclosureId: "synthetic-shelf-new",
    selectedStorageViewRuntimeId: "",
  });
  const result = buildScopedUrl(
    "/api/items?keep=one&system_id=old-a&system_id=old-b&enclosure_id=old-a&enclosure_id=old-b#section",
    new URLSearchParams([
      ["system_id", "extra-old"],
      ["enclosure_id", "extra-old"],
      ["added", "yes"],
    ]),
  );
  const parsed = parsedInternal(result);

  assert.deepEqual(parsed.searchParams.getAll("keep"), ["one"]);
  assert.deepEqual(parsed.searchParams.getAll("added"), ["yes"]);
  assert.deepEqual(parsed.searchParams.getAll("system_id"), ["synthetic-system-new"]);
  assert.deepEqual(parsed.searchParams.getAll("enclosure_id"), ["synthetic-shelf-new"]);
  assert.equal(parsed.hash, "#section");
  assert.ok(result.indexOf("?") < result.indexOf("#"));
});

test("scoped URL encodes exact values once", () => {
  const systemId = "synthetic system&equals=%value";
  const enclosureId = "synthetic shelf::drawer &=100%";
  const buildScopedUrl = loadScopedUrl({
    selectedSystemId: systemId,
    selectedEnclosureId: enclosureId,
    selectedStorageViewRuntimeId: "",
  });
  const parsed = parsedInternal(buildScopedUrl("/api/items?keep=a%26b%3Dc"));

  assert.equal(parsed.searchParams.get("system_id"), systemId);
  assert.equal(parsed.searchParams.get("enclosure_id"), enclosureId);
  assert.equal(parsed.searchParams.get("keep"), "a&b=c");
});

test("scoped URL omits empty selection without disturbing existing parameters", () => {
  const buildScopedUrl = loadScopedUrl({
    selectedSystemId: "",
    selectedEnclosureId: null,
    selectedStorageViewRuntimeId: "",
  });
  const original = (
    `/api/items?expected_revision=${"c".repeat(64)}`
    + "&keep=one&system_id=stale&enclosure_id=stale#fragment"
  );
  const result = buildScopedUrl(original);
  const parsed = parsedInternal(result);

  assert.equal(parsed.searchParams.has("system_id"), false);
  assert.equal(parsed.searchParams.has("enclosure_id"), false);
  assert.equal(parsed.searchParams.get("expected_revision"), "c".repeat(64));
  assert.equal(parsed.searchParams.get("keep"), "one");
  assert.equal(parsed.hash, "#fragment");
});

test("scoped URL rejects cross-origin targets", () => {
  const buildScopedUrl = loadScopedUrl({
    selectedSystemId: "synthetic-system-a",
    selectedEnclosureId: "synthetic-shelf-a",
    selectedStorageViewRuntimeId: "",
  });

  assert.throws(
    () => buildScopedUrl("https://other.example/api/items"),
    /same-origin|cross-origin|internal/i,
  );
});
