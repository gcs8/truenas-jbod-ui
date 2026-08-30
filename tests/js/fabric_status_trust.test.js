"use strict";

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
  const parametersEnd = FABRIC_SOURCE.indexOf(")", start);
  const bodyStart = FABRIC_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < FABRIC_SOURCE.length; index += 1) {
    const character = FABRIC_SOURCE[index];
    const next = FABRIC_SOURCE[index + 1];
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
      if (depth === 0) return FABRIC_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadTrustStatus() {
  const sandbox = vm.createContext({ Array, Object, Set, String });
  const source = [
    functionSource("isSasFabricEnrichmentWarning"),
    functionSource("fabricTrustStatus"),
  ].join("\n");
  vm.runInContext(`${source}\nthis.__loaded = fabricTrustStatus;`, sandbox);
  return sandbox.__loaded;
}

test("stale and trusted-fallback cache states cannot report Storage Fabric OK", () => {
  const fabricTrustStatus = loadTrustStatus();
  for (const [field, cacheState] of [
    ["snapshot_cache_state", "stale-hit"],
    ["snapshot_cache_state", "trusted-fallback"],
    ["source_cache_state", "stale-hit"],
  ]) {
    const status = fabricTrustStatus({
      available: true,
      traces: [{ id: "trace-a" }],
      [field]: cacheState,
    }, "STORAGE");
    assert.equal(status.suffix, "STALE");
    assert.equal(status.chipTone, "partial");
    assert.equal(status.statusTone, "error");
    assert.match(status.message, /retained/i);
  }
});

test("an enabled failed source reports partial Storage Fabric evidence", () => {
  const fabricTrustStatus = loadTrustStatus();
  const status = fabricTrustStatus({
    available: true,
    traces: [{ id: "trace-a" }, { id: "trace-b" }],
    snapshot_cache_state: "hit",
    source_cache_state: "hit",
    sources: {
      api: { enabled: true, ok: true },
      ssh: { enabled: true, ok: false },
      bmc: { enabled: false, ok: false },
    },
  }, "STORAGE");

  assert.equal(status.suffix, "PARTIAL");
  assert.equal(status.chipTone, "partial");
  assert.equal(status.statusTone, "error");
  assert.match(status.message, /2 traces/i);
  assert.match(status.message, /ssh/i);
  assert.doesNotMatch(status.message, /bmc/i);
});

test("a non-informational warning reports partial Storage Fabric evidence", () => {
  const fabricTrustStatus = loadTrustStatus();
  const status = fabricTrustStatus({
    available: true,
    traces: [{ id: "trace-a" }],
    snapshot_cache_state: "miss",
    source_cache_state: "hit",
    warnings: ["SSH connection failed before inventory commands could run."],
  }, "STORAGE");

  assert.equal(status.suffix, "PARTIAL");
  assert.match(status.message, /SSH connection failed/);
});

test("ordinary evidence-scope notes do not turn a healthy map partial", () => {
  const fabricTrustStatus = loadTrustStatus();
  const status = fabricTrustStatus({
    available: true,
    traces: [{ id: "trace-a" }],
    snapshot_cache_state: "miss",
    source_cache_state: "hit",
    sources: { api: { enabled: true, ok: true } },
    warnings: ["Storage Fabric is built from Linux SES slot evidence."],
  }, "STORAGE");

  assert.equal(status.suffix, "OK");
  assert.equal(status.chipTone, "ok");
  assert.equal(status.statusTone, "info");
});

test("renderStatus delegates available payload wording to fabricTrustStatus", () => {
  assert.match(functionSource("renderStatus"), /fabricTrustStatus\(fabric, copy\.statusBase\)/);
});
