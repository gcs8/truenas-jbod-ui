"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

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
      if (character === "\\") {
        index += 1;
      } else if (character === quote) {
        quote = null;
      }
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") {
      depth += 1;
    } else if (character === "}") {
      depth -= 1;
      if (depth === 0) {
        return source.slice(start, index + 1);
      }
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunctions(names) {
  const sandbox = vm.createContext({});
  const code = names.map((name) => functionSource(FABRIC_SOURCE, name)).join("\n");
  const exportsCode = names.map((name) => `this.__loaded_${name} = ${name};`).join("\n");
  vm.runInContext(`${code}\n${exportsCode}`, sandbox);
  return Object.fromEntries(names.map((name) => [name, sandbox[`__loaded_${name}`]]));
}

test("diagnostic rows use the canonical event table and never revive an unbounded legacy copy", () => {
  const { diagnosticEventRows, list } = loadFunctions(["diagnosticEventRows", "list"]);
  const recent = [{ event_id: "recent" }];

  assert.deepEqual(
    Array.from(diagnosticEventRows({
      event_table: { rows: [] },
      decoded_records: [{ event_id: "legacy-unbounded" }],
      recent_events: recent,
    })),
    recent,
  );
  assert.deepEqual(
    Array.from(diagnosticEventRows({
      event_table: { rows: [{ event_id: "sample" }] },
      decoded_records: [{ event_id: "legacy-unbounded" }],
      recent_events: recent,
    })),
    [{ event_id: "sample" }],
  );
  assert.equal(typeof list, "function");
});

test("diagnostic UI labels bounded rows as a recent sample instead of a full table", () => {
  const panel = functionSource(FABRIC_SOURCE, "renderDiagnosticEvidencePanel");
  const controls = functionSource(FABRIC_SOURCE, "renderDiagnosticTableControls");

  assert.match(panel, /Recent event sample/);
  assert.doesNotMatch(panel, /Full event table/);
  assert.match(controls, /sampled events/);
  assert.match(controls, /newest .* shipped/i);
});

test("diagnostic controls distinguish shipped samples from total events", () => {
  const { renderDiagnosticTableControls } = loadFunctions([
    "escapeHtml",
    "formatValue",
    "diagnosticPageNumbers",
    "renderDiagnosticPageButtons",
    "diagnosticEventTypeLabel",
    "diagnosticSeverityLabel",
    "diagnosticConfidenceLabel",
    "renderDiagnosticTableControls",
  ]);
  const base = {
    key: "controller:mpr0",
    page: 1,
    pageCount: 1,
    pageSize: 25,
    start: 1,
    end: 25,
    total: 40,
    filteredTotal: 25,
    filter: "",
    type: "all",
    severity: "all",
    confidence: "all",
    typeOptions: [],
    severityOptions: [],
    confidenceOptions: [],
    hasSourceTimestamps: true,
  };

  const unfiltered = renderDiagnosticTableControls(base);
  const filtered = renderDiagnosticTableControls({
    ...base,
    end: 3,
    filteredTotal: 3,
    filter: "timeout",
  });

  assert.match(unfiltered, /Showing 1-25 of 25 sampled events/);
  assert.match(unfiltered, /40 total events; newest 25 shipped/);
  assert.match(filtered, /Showing 1-3 of 3 sampled events/);
  assert.match(filtered, /3 matches in the shipped sample; 40 total events/);
});

test("generic SAS formatters keep legacy decoded records hidden", () => {
  const mainFormatter = functionSource(APP_SOURCE, "formatSasFabricValue");
  const fabricFormatter = functionSource(FABRIC_SOURCE, "formatValue");

  assert.match(mainFormatter, /"decoded_records"/);
  assert.match(fabricFormatter, /"decoded_records"/);
});
