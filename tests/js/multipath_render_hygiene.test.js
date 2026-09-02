"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const SOURCES = [
  ["live asset", fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8")],
  ["checked public demo", fs.readFileSync(path.join(ROOT, "public-demo/index.html"), "utf8")],
];

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = source.indexOf(")", start);
  const bodyStart = source.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < source.length; index += 1) {
    const character = source[index];
    const next = source[index + 1];
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
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadRenderer(source) {
  const sandbox = vm.createContext({});
  const script = [
    functionSource(source, "escapeHtml"),
    functionSource(source, "sasFabricClassToken"),
    functionSource(source, "renderMultipathPills"),
    "this.__renderMultipathPills = renderMultipathPills;",
  ].join("\n");
  vm.runInContext(script, sandbox, { filename: "multipath-render-hygiene.behavior.js" });
  return sandbox.__renderMultipathPills;
}

for (const [label, source] of SOURCES) {
  test(`${label} bounds the multipath state class token and escapes its label`, () => {
    const renderMultipathPills = loadRenderer(source);
    const html = renderMultipathPills([
      {
        device_name: "path-a",
        state: 'active" data-unsafe="yes',
        controller_label: "Controller A",
      },
    ]);

    assert.doesNotMatch(html, /\sdata-unsafe=/);
    assert.match(html, /class="topology-pill path-state-[a-z0-9_-]+"/);
    assert.match(html, /<small>ACTIVE&quot; DATA-UNSAFE=&quot;YES \/ Controller A<\/small>/);
  });
}
