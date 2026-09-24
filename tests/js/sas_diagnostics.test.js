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

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ ...context });
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
  const status = functionSource(FABRIC_SOURCE, "renderDiagnosticTableStatus");

  assert.match(panel, /Recent event sample/);
  assert.doesNotMatch(panel, /Full event table/);
  assert.match(status, /recent events/);
  assert.match(status, /the newest .* are listed here/i);
  assert.doesNotMatch(status, /sampled|shipped|dmesg/);
});

test("diagnostic controls distinguish the listed events from the kernel log total", () => {
  const { renderDiagnosticTableControls } = loadFunctions([
    "escapeHtml",
    "formatValue",
    "diagnosticPageNumbers",
    "renderDiagnosticPageButtons",
    "diagnosticEventTypeLabel",
    "diagnosticSeverityLabel",
    "diagnosticConfidenceLabel",
    "renderDiagnosticTableStatus",
    "renderDiagnosticTablePagination",
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

  assert.match(unfiltered, /Showing 1-25 of 25 recent events/);
  assert.match(unfiltered, /40 events in the kernel log; the newest 25 are listed here\. Filters search only these\./);
  assert.match(filtered, /Showing 1-3 of 3 recent events/);
  assert.match(filtered, /3 matches in the listed events; 40 in the kernel log\./);
  assert.doesNotMatch(unfiltered + filtered, /sample|shipped|dmesg/);
});

test("generic SAS formatters keep legacy decoded records hidden", () => {
  const mainFormatter = functionSource(APP_SOURCE, "formatSasFabricValue");
  const fabricFormatter = functionSource(FABRIC_SOURCE, "formatValue");

  assert.match(mainFormatter, /"decoded_records"/);
  assert.match(fabricFormatter, /"decoded_records"/);
});

test("diagnostic filtering updates table subregions without replacing the input", () => {
  const input = { value: "timeout" };
  const status = { innerHTML: "old status" };
  const pagination = { innerHTML: "old pages" };
  const body = { innerHTML: "old rows" };
  const details = {
    querySelector(selector) {
      return {
        "[data-fabric-diagnostic-status]": status,
        "[data-fabric-diagnostic-pagination]": pagination,
        "tbody": body,
      }[selector] || null;
    },
  };
  const state = {
    diagnosticPayloads: { table_a: { event_count: 2 } },
    diagnosticTables: { table_a: { filter: "timeout", page: 1 } },
  };
  const { refreshDiagnosticTable } = loadFunctions(["refreshDiagnosticTable"], {
    state,
    diagnosticTableState: () => state.diagnosticTables.table_a,
    diagnosticTablePresentation: () => ({ tableRows: [{ id: "event-a" }] }),
    renderDiagnosticTableStatus: () => "new status",
    renderDiagnosticTablePagination: () => "new pages",
    renderDiagnosticTableRows: () => "new rows",
  });

  assert.equal(refreshDiagnosticTable("table_a", details), true);
  assert.equal(status.innerHTML, "new status");
  assert.equal(pagination.innerHTML, "new pages");
  assert.equal(body.innerHTML, "new rows");
  assert.equal(input.value, "timeout", "the existing search input must remain untouched");

  const source = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
  const inputHandler = source.slice(source.indexOf('document.addEventListener("input"'), source.indexOf('document.addEventListener("change"'));
  assert.match(inputHandler, /refreshDiagnosticTable\(key, target\.closest\("\[data-fabric-diagnostic-table-key\]"\)\)/);
  assert.doesNotMatch(inputHandler, /\brender\(\)/);
});

test("diagnostic table identity preserves colliding content and panel scopes", () => {
  const { diagnosticTableKey, diagnosticEventRows, list } = loadFunctions([
    "diagnosticTableKey",
    "diagnosticEventRows",
    "classToken",
    "list",
  ]);
  const colonScope = {
    devices: ["da:1"],
    targets: ["10"],
    event_count: 1,
    event_table: { rows: [{ event_id: "event:1" }] },
  };
  const dashScope = {
    devices: ["da-1"],
    targets: ["10"],
    event_count: 1,
    event_table: { rows: [{ event_id: "event-1" }] },
  };

  assert.notEqual(diagnosticTableKey(colonScope, "mpr:0"), diagnosticTableKey(dashScope, "mpr-0"));
  assert.notEqual(diagnosticTableKey(colonScope, "path:a"), diagnosticTableKey(colonScope, "path-b"));
});

test("simultaneously filtered diagnostic panels refresh only their own DOM", () => {
  function panel(name) {
    const regions = {
      "[data-fabric-diagnostic-status]": { innerHTML: `${name}-old-status` },
      "[data-fabric-diagnostic-pagination]": { innerHTML: `${name}-old-pages` },
      "tbody": { innerHTML: `${name}-old-rows` },
    };
    return {
      regions,
      querySelector(selector) {
        return regions[selector] || null;
      },
    };
  }
  const firstPanel = panel("first");
  const secondPanel = panel("second");
  const state = {
    diagnosticPayloads: {
      first_key: { event_count: 1 },
      second_key: { event_count: 1 },
    },
    diagnosticTables: {
      first_key: { filter: "timeout", page: 1 },
      second_key: { filter: "medium", page: 1 },
    },
  };
  const { refreshDiagnosticTable } = loadFunctions(["refreshDiagnosticTable"], {
    state,
    document: {
      querySelector() {
        throw new Error("refresh must not query the global document");
      },
    },
    diagnosticTableState: (key) => state.diagnosticTables[key],
    diagnosticTablePresentation: (_diagnostics, tableState, key) => ({ key, filter: tableState.filter }),
    renderDiagnosticTableStatus: ({ key, filter }) => `${key}:${filter}:status`,
    renderDiagnosticTablePagination: ({ key, filter }) => `${key}:${filter}:pages`,
    renderDiagnosticTableRows: ({ key, filter }) => `${key}:${filter}:rows`,
  });

  assert.equal(refreshDiagnosticTable("first_key", firstPanel), true);
  assert.equal(refreshDiagnosticTable("second_key", secondPanel), true);
  assert.equal(firstPanel.regions.tbody.innerHTML, "first_key:timeout:rows");
  assert.equal(secondPanel.regions.tbody.innerHTML, "second_key:medium:rows");
  assert.equal(firstPanel.regions["[data-fabric-diagnostic-status]"].innerHTML, "first_key:timeout:status");
  assert.equal(secondPanel.regions["[data-fabric-diagnostic-status]"].innerHTML, "second_key:medium:status");
});

test("fallback expander panels with identical diagnostics keep distinct table state", () => {
  const state = { diagnosticTables: {} };
  const diagnostics = {
    devices: [],
    targets: ["10"],
    event_count: 1,
    event_table: { rows: [{ event_id: "event-1" }] },
  };
  const controllerNode = { metrics: { kernel_diagnostics: diagnostics } };
  const trace = { node_ids: [], metrics: {} };
  const fabric = { nodes: [] };
  const { diskPathBranchEvidence, diagnosticTableKey, diagnosticTableState, diagnosticEventRows, list } = loadFunctions([
    "diskPathBranchEvidence",
    "diagnosticTableKey",
    "diagnosticTableState",
    "diagnosticEventRows",
    "list",
  ], {
    state,
    nodeMap: () => new Map(),
    branchMprDevice: () => null,
    controllerNameFromId: () => null,
    firstTraceNode: () => null,
    renderDiagnosticEvidencePanel: (panelDiagnostics, scopeLabel, panelIdentity) => ({
      diagnostics: panelDiagnostics,
      scopeLabel,
      panelIdentity,
    }),
  });
  const first = diskPathBranchEvidence({
    controller: "mpr0",
    state: "reported",
    controllerNode,
    expanderNode: { id: "expander:first" },
  }, trace, fabric);
  const second = diskPathBranchEvidence({
    controller: "mpr0",
    state: "reported",
    controllerNode,
    expanderNode: { id: "expander:second" },
  }, trace, fabric);
  const firstKey = diagnosticTableKey(first.diagnostics, first.scopeLabel, first.panelIdentity);
  const secondKey = diagnosticTableKey(second.diagnostics, second.scopeLabel, second.panelIdentity);

  assert.notEqual(firstKey, secondKey);
  assert.notEqual(diagnosticTableState(firstKey), diagnosticTableState(secondKey));
});
