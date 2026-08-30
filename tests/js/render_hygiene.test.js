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
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = source.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
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
      if (character === "\\") {
        index += 1;
      } else if (character === quote) {
        quote = null;
      }
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

function loadFunctions(source, names, context = {}) {
  const sandbox = vm.createContext({ ...context });
  const code = names.map((name) => functionSource(source, name)).join("\n");
  const exportsCode = names.map((name) => `this.__loaded_${name} = ${name};`).join("\n");
  vm.runInContext(`${code}\n${exportsCode}`, sandbox, { filename: `${names.join("_")}.behavior.js` });
  const loaded = {};
  names.forEach((name) => {
    loaded[name] = sandbox[`__loaded_${name}`];
  });
  return { fns: loaded, context: sandbox };
}

function fakeLocationContext(pathname, search) {
  const calls = [];
  return {
    calls,
    context: {
      window: {
        location: { pathname, search },
        history: {
          replaceState(stateValue, title, url) {
            calls.push(url);
          },
        },
      },
    },
  };
}

function trackedTextNode(initialText = "") {
  const node = { writes: 0, _text: initialText, style: {} };
  Object.defineProperty(node, "textContent", {
    get() {
      return this._text;
    },
    set(value) {
      this.writes += 1;
      this._text = value;
    },
  });
  return node;
}

function trackedBarNode(initialWidth = "") {
  const style = { writes: 0, _width: initialWidth };
  Object.defineProperty(style, "width", {
    get() {
      return this._width;
    },
    set(value) {
      this.writes += 1;
      this._width = value;
    },
  });
  return { style };
}

test("main UI syncLocation only calls history.replaceState when the URL actually changes", () => {
  const { calls, context } = fakeLocationContext("/", "?system_id=system-a");
  const { fns } = loadFunctions(APP_SOURCE, ["replaceLocationIfChanged"], context);

  assert.equal(fns.replaceLocationIfChanged("/?system_id=system-a"), false);
  assert.deepEqual(calls, []);

  assert.equal(fns.replaceLocationIfChanged("/?system_id=system-b"), true);
  assert.deepEqual(calls, ["/?system_id=system-b"]);
});

test("Storage Fabric page syncLocation only calls history.replaceState when the URL actually changes", () => {
  const { calls, context } = fakeLocationContext("/sas-fabric", "?system_id=system-a&mode=trace");
  const { fns } = loadFunctions(FABRIC_SOURCE, ["replaceLocationIfChanged"], context);

  assert.equal(fns.replaceLocationIfChanged("/sas-fabric?system_id=system-a&mode=trace"), false);
  assert.equal(fns.replaceLocationIfChanged("/sas-fabric?system_id=system-a&mode=trace"), false);
  assert.deepEqual(calls, []);

  assert.equal(fns.replaceLocationIfChanged("/sas-fabric"), true);
  assert.deepEqual(calls, ["/sas-fabric"]);
});

test("both syncLocation implementations route through the changed-URL guard", () => {
  assert.match(functionSource(APP_SOURCE, "syncLocation"), /replaceLocationIfChanged\(/);
  assert.doesNotMatch(functionSource(APP_SOURCE, "syncLocation"), /history\.replaceState/);
  assert.match(functionSource(FABRIC_SOURCE, "syncLocation"), /replaceLocationIfChanged\(/);
  assert.doesNotMatch(functionSource(FABRIC_SOURCE, "syncLocation"), /history\.replaceState/);
});

test("unchanged select options do not rebuild an open native selector", () => {
  const select = { writes: 0, _html: '<option value="a">A</option>' };
  Object.defineProperty(select, "innerHTML", {
    get() { return this._html; },
    set(value) {
      this.writes += 1;
      this._html = value;
    },
  });
  const { fns } = loadFunctions(APP_SOURCE, ["setSelectOptionsIfChanged"]);

  assert.equal(fns.setSelectOptionsIfChanged(select, '<option value="a">A</option>'), false);
  assert.equal(select.writes, 0);
  assert.equal(fns.setSelectOptionsIfChanged(select, '<option value="b">B</option>'), true);
  assert.equal(select.writes, 1);
  assert.match(functionSource(APP_SOURCE, "renderSelectors"), /setSelectOptionsIfChanged\(/);
  assert.match(functionSource(FABRIC_SOURCE, "renderSelectors"), /setSelectOptionsIfChanged\(/);
});

test("Fabric mode controls use button-group semantics without fake tabs", () => {
  const template = fs.readFileSync(path.join(ROOT, "app/templates/sas_fabric.html"), "utf8");
  const groupLine = template.split("\n").find((line) => line.includes('class="fabric-mode-tabs"'));
  assert.ok(groupLine);
  assert.match(groupLine, /role="group"/);
  assert.doesNotMatch(groupLine, /role="tablist"/);
  assert.equal((template.match(/data-fabric-mode=/g) || []).length, 4);
  assert.equal((template.match(/aria-pressed=/g) || []).length, 4);
});

test("dynamic panels announce status nodes instead of rebuilt panel contents", () => {
  const template = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
  const perfPanel = template.split("\n").find((line) => line.includes('id="ui-perf-panel"'));
  const perfSummary = template.split("\n").find((line) => line.includes('id="ui-perf-summary"'));
  const fabricPanel = template.split("\n").find((line) => line.includes('id="sas-fabric-panel"'));
  const fabricStatus = template.split("\n").find((line) => line.includes('id="sas-fabric-status"'));
  assert.doesNotMatch(perfPanel, /aria-live/);
  assert.match(perfSummary, /aria-live="polite"/);
  assert.doesNotMatch(fabricPanel, /aria-live/);
  assert.match(fabricStatus, /aria-live="polite"/);
});

test("SES enclosure nodes on a disk path are matched per node against the slot being rendered", () => {
  const { fns } = loadFunctions(FABRIC_SOURCE, ["sesNodeTouchesSlot", "sortedSlots", "list"]);
  const ses = (id, slots) => ({ id, kind: "ses-enclosure", related_slots: slots });

  assert.equal(fns.sesNodeTouchesSlot(ses("ses:a", [0, 1, 2]), 1), true);
  assert.equal(fns.sesNodeTouchesSlot(ses("ses:a", [0, 1, 2]), "2"), true);
  assert.equal(fns.sesNodeTouchesSlot(ses("ses:b", [24, 25]), 1), false, "unrelated shelf must not attach to this slot");
  assert.equal(fns.sesNodeTouchesSlot(ses("ses:c", []), 1), false);
  assert.equal(fns.sesNodeTouchesSlot({ id: "expander:x", kind: "expander", related_slots: [1] }, 1), false);
  assert.equal(fns.sesNodeTouchesSlot(null, 1), false);
  assert.equal(fns.sesNodeTouchesSlot(undefined, 1), false);

  const branch = functionSource(FABRIC_SOURCE, "renderDiskPathBranch");
  assert.match(branch, /sesNodeTouchesSlot\(node, slotNumber\)/);
  assert.doesNotMatch(branch, /selectionTouchesSlots\(\[slotNumber\]\)/);
});

test("groupColumnTemplate tolerates a non-array row group list instead of calling .map on it", () => {
  const { fns } = loadFunctions(APP_SOURCE, ["groupColumnTemplate", "rowGroupingMetrics"]);

  assert.equal(fns.groupColumnTemplate(null, ""), "");
  assert.equal(fns.groupColumnTemplate(undefined, "top-loader"), "");
  assert.equal(fns.groupColumnTemplate([], ""), "");
  assert.equal(fns.groupColumnTemplate([[0, 1, 2]], ""), "minmax(0, 3fr)");
  assert.equal(typeof fns.groupColumnTemplate([[0, 1], [2, 3]], ""), "string");
});

test("history state no longer carries the constant-null ternary", () => {
  assert.doesNotMatch(APP_SOURCE, /detail:\s*snapshotMode\s*\?\s*null\s*:\s*null/);
});

test("refresh countdown rendering does not rewrite unchanged text or bar width", () => {
  const label = trackedTextNode("Next refresh pending");
  const bar = trackedBarNode("");
  const state = {
    snapshotMode: false,
    refreshesInFlight: 0,
    autoRefresh: false,
    timerDueAt: 0,
    timerDelayMs: 0,
    refreshIntervalSeconds: 30,
  };
  const { fns } = loadFunctions(APP_SOURCE, ["renderRefreshTiming", "setTextIfChanged", "setBarWidthIfChanged"], {
    state,
    refreshTimingStrip: { classList: { toggle() {} } },
    refreshCountdownLabel: label,
    refreshCountdownBar: bar,
    formatRefreshInterval: (seconds) => `${seconds}s`,
    formatTimingDuration: (seconds) => `${Math.round(seconds)}s`,
    Date,
    Number,
    Math,
  });

  fns.renderRefreshTiming();
  assert.equal(label.textContent, "Auto refresh off");
  assert.equal(bar.style.width, "0%");
  assert.equal(label.writes, 1);
  assert.equal(bar.style.writes, 1);

  for (let tick = 0; tick < 5; tick += 1) {
    fns.renderRefreshTiming();
  }
  assert.equal(label.writes, 1, "steady state must not rewrite the label each second");
  assert.equal(bar.style.writes, 1, "steady state must not rewrite the bar each second");

  state.refreshesInFlight = 1;
  fns.renderRefreshTiming();
  assert.equal(label.textContent, "Refresh running");
  assert.equal(bar.style.width, "100%");
  assert.equal(label.writes, 2);
  assert.equal(bar.style.writes, 2);
});

test("timing tick skips DOM work while the document is hidden", () => {
  let renders = 0;
  const documentStub = { hidden: true };
  const { fns } = loadFunctions(APP_SOURCE, ["timingTick"], {
    document: documentStub,
    renderTimingSurfaces() {
      renders += 1;
    },
  });

  fns.timingTick();
  assert.equal(renders, 0);

  documentStub.hidden = false;
  fns.timingTick();
  assert.equal(renders, 1);
});

test("the refresh countdown strip is not an aria-live region", () => {
  const template = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
  const stripLine = template.split("\n").find((line) => line.includes('id="refresh-timing-strip"'));
  assert.ok(stripLine, "refresh timing strip must exist");
  assert.doesNotMatch(stripLine, /aria-live/);
});

test("cache timing chips are updated in place after the first render", () => {
  const source = functionSource(APP_SOURCE, "renderCacheTimingChips");
  assert.match(source, /structureChanged/);
  assert.match(source, /setTextIfChanged\(/);
  assert.match(source, /setBarWidthIfChanged\(/);
  assert.match(source, /classList\.toggle\("is-expired"/);
});
