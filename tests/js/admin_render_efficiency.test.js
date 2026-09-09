"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const SOURCE = fs.readFileSync(path.join(ROOT, "admin_service/static/admin.js"), "utf8");
const MAIN_PY = fs.readFileSync(path.join(ROOT, "admin_service/main.py"), "utf8");

function functionSource(name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  let parametersEnd = SOURCE.indexOf("(", start);
  let parenDepth = 0;
  for (; parametersEnd < SOURCE.length; parametersEnd += 1) {
    if (SOURCE[parametersEnd] === "(") {
      parenDepth += 1;
    } else if (SOURCE[parametersEnd] === ")") {
      parenDepth -= 1;
      if (parenDepth === 0) {
        break;
      }
    }
  }
  const bodyStart = SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < SOURCE.length; index += 1) {
    const character = SOURCE[index];
    const next = SOURCE[index + 1];
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
        return SOURCE.slice(start, index + 1);
      }
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunctions(names, bindings = {}) {
  const context = vm.createContext({ URLSearchParams, console, setTimeout, clearTimeout, Array, Boolean, Number, String, Math, ...bindings });
  vm.runInContext(
    `${names.map(functionSource).join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.__tested;
}

function fakeField(value = "") {
  return {
    value,
    checked: false,
    disabled: false,
    textContent: "",
    classList: { toggle() {}, add() {}, remove() {} },
  };
}

function sparseElements(overrides = {}) {
  return new Proxy({ ...overrides }, {
    get(target, key) {
      if (!(key in target)) {
        target[key] = fakeField();
      }
      return target[key];
    },
  });
}

function counter() {
  const counts = {};
  const stub = (name) => (...args) => {
    counts[name] = (counts[name] || 0) + 1;
    return args[0];
  };
  return { counts, stub };
}

async function flushPromises(rounds = 8) {
  for (let index = 0; index < rounds; index += 1) {
    await Promise.resolve();
  }
}

function fakeFrames() {
  const queued = [];
  return {
    queued,
    requestAnimationFrame(callback) {
      queued.push(callback);
      return queued.length;
    },
    cancelAnimationFrame(frameId) {
      queued[frameId - 1] = null;
    },
    run() {
      const pending = queued.splice(0, queued.length);
      pending.forEach((callback) => callback && callback());
    },
  };
}

test("loading a saved system paints the storage-view panel once, not four times", async () => {
  const { counts, stub } = counter();
  const frames = fakeFrames();
  const state = {
    sshKeys: [],
    systems: [],
    storageViews: [],
    selectedStorageViewId: "",
    liveEnclosuresRequestSeq: 0,
    storageViewCandidatesRequestSeq: 0,
    storageViewRenderFrameId: null,
    storageViewRenderFull: false,
  };
  const functions = loadFunctions(
    [
      "loadSystemIntoForm",
      "replaceStorageViewState",
      "renderStorageViews",
      "renderStorageViewsNow",
      "scheduleStorageViewRender",
      "flushStorageViewRender",
      "requestRenderFrame",
      "cancelRenderFrame",
      "fetchLiveEnclosures",
      "fetchStorageViewCandidates",
      "resetLiveEnclosureState",
      "resetStorageViewCandidateState",
    ],
    {
      state,
      elements: sparseElements(),
      requestAnimationFrame: frames.requestAnimationFrame,
      cancelAnimationFrame: frames.cancelAnimationFrame,
      normalizeStorageViews: (views) => views,
      normalizeHaNodes: (nodes) => nodes || [],
      renderStorageViewTemplateOptions: stub("renderStorageViewTemplateOptions"),
      renderStorageViewList: stub("renderStorageViewList"),
      syncStorageViewEditorFromState: stub("syncStorageViewEditorFromState"),
      renderStorageViewPreview: stub("renderStorageViewPreview"),
      renderStorageViewCandidates: stub("renderStorageViewCandidates"),
      renderProfileOptions: stub("renderProfileOptions"),
      setRedactedSecretField: stub("setRedactedSecretField"),
      savedSecretConfigured: () => false,
      recommendedSshUserForPlatform: () => "jbodmap",
      loadSshCommandState: stub("loadSshCommandState"),
      syncTlsTrustStatus: stub("syncTlsTrustStatus"),
      renderSshKeyOptions: stub("renderSshKeyOptions"),
      applySelectedKey: stub("applySelectedKey"),
      syncPlatformHelp: stub("syncPlatformHelp"),
      syncVerifySslHelp: stub("syncVerifySslHelp"),
      syncTlsServerNameHelp: stub("syncTlsServerNameHelp"),
      renderTlsServerNameSuggestions: stub("renderTlsServerNameSuggestions"),
      renderProfilePreview: stub("renderProfilePreview"),
      renderProfileCatalog: stub("renderProfileCatalog"),
      renderQuantastorHaSection: stub("renderQuantastorHaSection"),
      renderTlsInspection: stub("renderTlsInspection"),
      syncBmcFields: stub("syncBmcFields"),
      syncSshFields: stub("syncSshFields"),
      updateCreateButton: stub("updateCreateButton"),
      renderExistingSystems: stub("renderExistingSystems"),
      scheduleSudoersPreviewRefresh: stub("scheduleSudoersPreviewRefresh"),
      currentStorageViewSystemId: () => "system-a",
      currentStorageViewTargetSystemId: () => "",
      setBanner() {},
      fetchJson: async (url) => (url.includes("live-enclosures")
        ? { system_id: "system-a", enclosures: [] }
        : { system_id: "system-a", candidates: [] }),
    }
  );

  functions.loadSystemIntoForm({ id: "system-a", label: "System A", platform: "core", storage_views: [] });
  await flushPromises();
  assert.equal(counts.renderStorageViewList || 0, 0, "nothing paints before the next animation frame");
  frames.run();
  await flushPromises();
  frames.run();

  assert.equal(counts.renderStorageViewList, 1, "one storage-view render per load");
  assert.equal(counts.renderStorageViewPreview, 1, "one preview grid rebuild per load");
  assert.equal(counts.renderStorageViewCandidates, 1, "one candidate list render per load");
  assert.equal(state.liveEnclosuresLoading, false);
  assert.equal(state.storageViewCandidatesLoading, false);
  assert.equal(state.storageViewRenderFrameId, null);
});

test("a candidate-only render request folds into a pending full render", () => {
  const { counts, stub } = counter();
  const frames = fakeFrames();
  const state = { storageViewRenderFrameId: null, storageViewRenderFull: false };
  const functions = loadFunctions(
    ["renderStorageViews", "renderStorageViewsNow", "scheduleStorageViewRender", "flushStorageViewRender", "requestRenderFrame", "cancelRenderFrame"],
    {
      state,
      requestAnimationFrame: frames.requestAnimationFrame,
      cancelAnimationFrame: frames.cancelAnimationFrame,
      renderStorageViewTemplateOptions: stub("renderStorageViewTemplateOptions"),
      renderStorageViewList: stub("renderStorageViewList"),
      syncStorageViewEditorFromState: stub("syncStorageViewEditorFromState"),
      renderStorageViewPreview: stub("renderStorageViewPreview"),
      renderStorageViewCandidates: stub("renderStorageViewCandidates"),
    }
  );

  functions.scheduleStorageViewRender({ full: false });
  functions.renderStorageViews();
  functions.scheduleStorageViewRender({ full: false });
  assert.equal(frames.queued.filter(Boolean).length, 1, "one frame is requested");
  frames.run();
  assert.equal(counts.renderStorageViewList, 1);
  assert.equal(counts.renderStorageViewCandidates, 1);

  functions.scheduleStorageViewRender({ full: false });
  functions.flushStorageViewRender();
  assert.equal(counts.renderStorageViewList, 1, "a candidate-only flush leaves the rest of the panel alone");
  assert.equal(counts.renderStorageViewCandidates, 2);
  assert.equal(frames.queued.filter(Boolean).length, 0, "flushing cancels the pending frame");
});

test("rendering the Quantastor HA rows normalizes the node list once and caches the row fields", () => {
  let normalizeCalls = 0;
  let queries = 0;
  const elements = sparseElements({
    setupPlatform: fakeField("quantastor"),
    setupHaEnabled: { checked: true, disabled: false },
  });
  const functions = loadFunctions(
    ["renderQuantastorHaSection", "currentQuantastorHaNodes", "haNodeFieldValue", "syncSshHostCopy", "currentSetupPlatform"],
    {
      elements,
      haNodeFieldCache: new Map(),
      document: {
        querySelector() {
          queries += 1;
          return fakeField();
        },
      },
      state: {
        haNodes: [
          { system_id: "node-a", label: "", host: "192.0.2.10" },
          { system_id: "node-b", label: "", host: "" },
        ],
        haNodesLoading: false,
      },
      normalizeHaNodes(nodes) {
        normalizeCalls += 1;
        return nodes;
      },
    }
  );

  functions.renderQuantastorHaSection();
  assert.equal(normalizeCalls, 1, "one normalizeHaNodes per HA render");
  assert.equal(queries, 9, "three rows times three fields are looked up on the first render");
  assert.match(elements.setupHaNodesResult.textContent, /Loaded 2 Quantastor HA node rows/);

  functions.renderQuantastorHaSection();
  assert.equal(normalizeCalls, 2);
  assert.equal(queries, 9, "the second render reuses the cached row fields");
});

test("fetchJson gives up on a stalled request with a plain timeout message", async () => {
  let abortedSignal = null;
  const functions = loadFunctions(
    ["fetchJson", "fetchWithTimeout", "requestTimeoutError", "readJsonResponse", "describeApiError"],
    {
      DEFAULT_REQUEST_TIMEOUT_MS: 60000,
      AbortController,
      DOMException,
      fetch(url, options) {
        return new Promise((resolve, reject) => {
          options.signal.addEventListener("abort", () => {
            abortedSignal = options.signal;
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      },
    }
  );

  await assert.rejects(
    () => functions.fetchJson("/api/admin/storage-views/live-enclosures?system_id=system-a", { timeoutMs: 5 }),
    (error) => {
      assert.equal(error.timedOut, true);
      assert.match(error.message, /^Timed out after 1 second\. Check that the host is reachable and try again\.$/);
      return true;
    }
  );
  assert.ok(abortedSignal?.aborted, "the underlying fetch was aborted");
});

test("fetchJson keeps a caller's own cancellation distinct from a timeout", async () => {
  const functions = loadFunctions(
    ["fetchJson", "fetchWithTimeout", "requestTimeoutError", "readJsonResponse", "describeApiError"],
    {
      DEFAULT_REQUEST_TIMEOUT_MS: 60000,
      AbortController,
      DOMException,
      fetch(url, options) {
        return new Promise((resolve, reject) => {
          options.signal.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      },
    }
  );
  const controller = new AbortController();
  const pending = functions.fetchJson("/api/admin/runtime", { signal: controller.signal, timeoutMs: 60000 });
  controller.abort();
  await assert.rejects(pending, (error) => error.name === "AbortError" && !error.timedOut);
});

test("fetchJson returns normally when the response arrives in time", async () => {
  const functions = loadFunctions(
    ["fetchJson", "fetchWithTimeout", "requestTimeoutError", "readJsonResponse", "describeApiError"],
    {
      DEFAULT_REQUEST_TIMEOUT_MS: 60000,
      AbortController,
      fetch: async () => ({ ok: true, status: 200, json: async () => ({ enclosures: [] }) }),
    }
  );
  assert.deepEqual(await functions.fetchJson("/api/admin/state", { timeoutMs: 1000 }), { enclosures: [] });
});

test("the hero countdown tick only writes when the text changes", () => {
  let writes = 0;
  let text = "5m 00s";
  const countdown = {
    get textContent() {
      return this.stored || "";
    },
    set textContent(value) {
      writes += 1;
      this.stored = value;
    },
  };
  const functions = loadFunctions(["tickCountdown"], {
    elements: { countdown },
    formatCountdown: () => text,
  });
  functions.tickCountdown();
  functions.tickCountdown();
  functions.tickCountdown();
  assert.equal(writes, 1);
  text = "4m 59s";
  functions.tickCountdown();
  assert.equal(writes, 2);
  assert.equal(countdown.textContent, "4m 59s");
});

test("bundlePathGroupByKey indexes the path groups once per list and prefers backup groups", () => {
  let scans = 0;
  const backupGroup = { key: "config", bundle_types: ["backup", "debug"], sensitive: true };
  const debugGroup = { key: "config", bundle_types: ["debug"], sensitive: false };
  const debugOnly = { key: "logs", bundle_types: ["debug"] };
  const state = { backupDefaults: { path_groups: [debugGroup, backupGroup, debugOnly] } };
  const functions = loadFunctions(["bundlePathGroupByKey"], {
    state,
    bundlePathGroupIndex: { source: null, byKey: new Map() },
    bundlePathGroups(bundleType) {
      scans += 1;
      return state.backupDefaults.path_groups.filter((group) => group.bundle_types.includes(bundleType));
    },
  });

  assert.equal(functions.bundlePathGroupByKey("config"), backupGroup);
  assert.equal(functions.bundlePathGroupByKey("logs"), debugOnly);
  assert.equal(functions.bundlePathGroupByKey("missing"), null);
  assert.equal(scans, 2, "backup and debug lists are scanned once each");

  state.backupDefaults = { path_groups: [debugOnly] };
  assert.equal(functions.bundlePathGroupByKey("config"), null);
  assert.equal(scans, 4, "a refreshed list is indexed again");
});

test("buildSequentialLayout still pads every row to the full width", () => {
  const functions = loadFunctions(["buildSequentialLayout", "buildRectangularProfileLayout"]);
  const layout = (rows, columns, slotCount) => JSON.stringify(functions.buildSequentialLayout(rows, columns, slotCount));
  assert.equal(layout(2, 3, 4), JSON.stringify([[0, 1, 2], [3, null, null]]));
  assert.equal(layout(1, 1, 5), JSON.stringify([[0]]));
  assert.equal(layout(0, 0, 0), JSON.stringify([[0]]));
});

test("countSlots counts bay numbers across rows and ignores blanks", () => {
  const functions = loadFunctions(["countSlots"]);
  assert.equal(functions.countSlots([[0, 1, null], [2, "x"], "not-a-row"]), 3);
  assert.equal(functions.countSlots(null), 0);
});

test("refreshing admin state paints before the removed-system history scan runs", async () => {
  const order = [];
  const state = { selectedBackupPaths: [], selectedDebugPaths: [], selectedEsxiHostPrepToken: "" };
  const functions = loadFunctions(["runRefreshState"], {
    state,
    elements: { refreshStateButton: { disabled: false } },
    setBanner() {},
    fetchJson: async () => ({ systems: [], profiles: [] }),
    currentStagedEsxiHostPrepPackages: () => [],
    renderAll() {
      order.push("renderAll");
    },
    loadOrphanedHistory() {
      order.push("loadOrphanedHistory");
      return Promise.resolve();
    },
    fetchLiveEnclosures: () => Promise.resolve(),
    fetchStorageViewCandidates: () => Promise.resolve(),
  });
  await functions.runRefreshState({ quiet: true });
  assert.deepEqual(order, ["renderAll", "loadOrphanedHistory"]);
});

test("syncSshFields looks the SSH fields up once and toggles them on every call", () => {
  let queries = 0;
  const fields = [fakeField(), fakeField()];
  const elements = sparseElements({ setupSshEnabled: { checked: false }, setupSshHost: fakeField("nas.example.test") });
  const functions = loadFunctions(["syncSshFields"], {
    elements,
    sshFieldNodes: null,
    document: {
      querySelectorAll() {
        queries += 1;
        return fields;
      },
    },
    suggestedConnectionHost: () => "",
    maybeLoadRecommendedSshUser() {},
    syncPlatformSpecificSetupFields() {},
    syncBootstrapFields() {},
    syncEsxiHostPrepFields() {},
    syncKeyMode() {},
  });

  functions.syncSshFields();
  assert.equal(queries, 1);
  assert.deepEqual(fields.map((field) => field.disabled), [true, true]);
  elements.setupSshEnabled.checked = true;
  functions.syncSshFields();
  assert.equal(queries, 1, "the second call reuses the cached fields");
  assert.deepEqual(fields.map((field) => field.disabled), [false, false]);
});

test("the dead admin.js branches and unused bootstrap keys stay gone", () => {
  for (const pattern of [
    /setupSshKnownHosts/,
    /state\.paths\b/,
    /layoutSlotCount/,
    /countProfilePreviewSlots/,
    /allowedSizes/,
    /\.flat\(\)\.filter\(\(value\) => Number\.isInteger\(value\)\)\.length/,
    /case "scale":/,
  ]) {
    assert.doesNotMatch(SOURCE, pattern);
  }
  assert.equal((SOURCE.match(/new Set\(\["2230", "2242", "2260", "2280", "22110"\]\)/g) || []).length, 1);
  assert.doesNotMatch(functionSource("syncSshFields"), /syncKeyHelp\(\)/);
  assert.doesNotMatch(MAIN_PY, /"debug_scrub_sensitive"/);
  assert.doesNotMatch(MAIN_PY, /"clean_backup_targets": list\(/);
});
