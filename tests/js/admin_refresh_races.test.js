"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");

function functionSource(name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = SOURCE.indexOf(")", start);
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
  const context = vm.createContext({ URLSearchParams, console, setTimeout, clearTimeout, ...bindings });
  vm.runInContext(
    `${names.map(functionSource).join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.__tested;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flushPromises() {
  for (let index = 0; index < 6; index += 1) {
    await Promise.resolve();
  }
}

function baseRefreshState() {
  return {
    refreshInFlight: false,
    refreshPromise: null,
    refreshQueued: null,
    refreshQueuedQuiet: true,
    admin: {},
    appVersion: "0",
    releaseStatus: null,
    systems: [],
    defaultSystemId: null,
    profiles: [],
    storageViewTemplates: [],
    platformDefaults: {},
    sshKeys: [],
    esxiHostPrep: {},
    selectedEsxiHostPrepToken: "",
    runtime: {},
    runtimeBehavior: {},
    backupDefaults: {},
    selectedBackupPaths: [],
    selectedDebugPaths: [],
    paths: {},
    loadedSystemId: null,
  };
}

test("refreshState coalesces overlapping calls into one queued follow-up instead of dropping them", async () => {
  const requests = [];
  const banners = [];
  const state = baseRefreshState();
  const { refreshState } = loadFunctions(["refreshState", "startRefreshState", "runRefreshState"], {
    state,
    elements: { refreshStateButton: { disabled: false } },
    setBanner(message, tone) {
      banners.push([message, tone]);
    },
    fetchJson() {
      const request = deferred();
      requests.push(request);
      return request.promise;
    },
    currentStagedEsxiHostPrepPackages() {
      return [];
    },
    loadOrphanedHistory() {
      return Promise.resolve();
    },
    renderAll() {},
    fetchLiveEnclosures() {
      return Promise.resolve();
    },
    fetchStorageViewCandidates() {
      return Promise.resolve();
    },
    Array,
    Boolean,
  });

  const first = refreshState({ quiet: true });
  await flushPromises();
  assert.equal(requests.length, 1, "first refresh is in flight");

  const second = refreshState({ quiet: true });
  const third = refreshState({ quiet: false });
  assert.equal(second, third, "overlapping callers share the single queued follow-up");
  assert.equal(requests.length, 1, "no second request until the first settles");

  let secondSettled = false;
  second.then(() => {
    secondSettled = true;
  });

  requests[0].resolve({ systems: [{ id: "old" }] });
  await first;
  await flushPromises();
  assert.equal(requests.length, 2, "queued follow-up starts after the first refresh settles");
  assert.equal(secondSettled, false, "queued callers wait for the follow-up, not the first refresh");
  assert.deepEqual(state.systems, [{ id: "old" }]);

  requests[1].resolve({ systems: [{ id: "new" }] });
  await second;
  await third;
  assert.equal(secondSettled, true);
  assert.deepEqual(state.systems, [{ id: "new" }], "post-save callers observe the newer state");
  assert.equal(state.refreshPromise, null);
  assert.equal(state.refreshQueued, null);
  assert.equal(state.refreshInFlight, false);
  assert.ok(
    banners.some(([message, tone]) => tone === "success" && /refreshed/i.test(message)),
    "a non-quiet queued caller still gets the completion banner"
  );

  const fourth = refreshState({ quiet: true });
  await flushPromises();
  assert.equal(requests.length, 3, "after everything settles a new refresh starts immediately");
  requests[2].resolve({ systems: [] });
  await fourth;
});

test("refreshState follow-up still runs when the in-flight refresh fails", async () => {
  const requests = [];
  const state = baseRefreshState();
  const { refreshState } = loadFunctions(["refreshState", "startRefreshState", "runRefreshState"], {
    state,
    elements: {},
    setBanner() {},
    fetchJson() {
      const request = deferred();
      requests.push(request);
      return request.promise;
    },
    currentStagedEsxiHostPrepPackages() {
      return [];
    },
    loadOrphanedHistory() {
      return Promise.resolve();
    },
    renderAll() {},
    fetchLiveEnclosures() {
      return Promise.resolve();
    },
    fetchStorageViewCandidates() {
      return Promise.resolve();
    },
    Array,
    Boolean,
  });

  const first = refreshState();
  await flushPromises();
  const queued = refreshState();
  requests[0].reject(new Error("boom"));
  await first;
  await flushPromises();
  assert.equal(requests.length, 2, "a failed first refresh must not strand the queued follow-up");
  requests[1].resolve({ systems: [{ id: "recovered" }] });
  await queued;
  assert.deepEqual(state.systems, [{ id: "recovered" }]);
});

test("fetchLiveEnclosures ignores a slow response for a system the operator already left", async () => {
  const requests = [];
  const banners = [];
  let systemId = "system-a";
  const state = {
    liveEnclosuresRequestSeq: 0,
    liveEnclosuresLoading: false,
    liveEnclosuresError: null,
    liveEnclosuresSystemId: null,
    liveEnclosures: [],
  };
  const { fetchLiveEnclosures } = loadFunctions(["fetchLiveEnclosures"], {
    state,
    currentStorageViewSystemId() {
      return systemId;
    },
    resetLiveEnclosureState() {},
    renderStorageViews() {},
    setBanner(message, tone) {
      banners.push([message, tone]);
    },
    fetchJson(url) {
      const request = deferred();
      requests.push({ url, ...request });
      return request.promise;
    },
    Array,
  });

  const stale = fetchLiveEnclosures({ quiet: false });
  systemId = "system-b";
  const fresh = fetchLiveEnclosures({ quiet: false });
  assert.equal(requests.length, 2);
  assert.match(requests[0].url, /system_id=system-a/);
  assert.match(requests[1].url, /system_id=system-b/);

  requests[1].resolve({ system_id: "system-b", enclosures: [{ id: "b-front" }] });
  await fresh;
  assert.equal(state.liveEnclosuresSystemId, "system-b");
  assert.deepEqual(state.liveEnclosures, [{ id: "b-front" }]);
  assert.equal(state.liveEnclosuresLoading, false);

  requests[0].resolve({ system_id: "system-a", enclosures: [{ id: "a-front" }, { id: "a-rear" }] });
  await stale;
  assert.equal(state.liveEnclosuresSystemId, "system-b", "stale response must not overwrite the newer system");
  assert.deepEqual(state.liveEnclosures, [{ id: "b-front" }]);
  assert.equal(banners.filter(([message]) => /system-a/.test(message)).length, 0, "no banner for the stale system");
  assert.equal(banners.filter(([message]) => /system-b/.test(message)).length, 1);
});

test("fetchLiveEnclosures ignores a stale failure and keeps the loading flag owned by the newest request", async () => {
  const requests = [];
  let systemId = "system-a";
  const state = {
    liveEnclosuresRequestSeq: 0,
    liveEnclosuresLoading: false,
    liveEnclosuresError: null,
    liveEnclosuresSystemId: null,
    liveEnclosures: [],
  };
  const { fetchLiveEnclosures } = loadFunctions(["fetchLiveEnclosures"], {
    state,
    currentStorageViewSystemId() {
      return systemId;
    },
    resetLiveEnclosureState() {},
    renderStorageViews() {},
    setBanner() {},
    fetchJson() {
      const request = deferred();
      requests.push(request);
      return request.promise;
    },
    Array,
  });

  const stale = fetchLiveEnclosures({ quiet: true });
  systemId = "system-b";
  const fresh = fetchLiveEnclosures({ quiet: true });
  requests[0].reject(new Error("timeout for system-a"));
  await stale;
  assert.equal(state.liveEnclosuresLoading, true, "the newer request is still loading");
  assert.equal(state.liveEnclosuresError, null, "stale failure must not surface as the current error");

  requests[1].resolve({ system_id: "system-b", enclosures: [] });
  await fresh;
  assert.equal(state.liveEnclosuresLoading, false);
  assert.equal(state.liveEnclosuresSystemId, "system-b");
});

test("fetchStorageViewCandidates ignores a slow response for a system the operator already left", async () => {
  const requests = [];
  const banners = [];
  let systemId = "system-a";
  const state = {
    storageViewCandidatesRequestSeq: 0,
    storageViewCandidatesLoading: false,
    storageViewCandidatesSystemId: null,
    storageViewCandidates: [],
  };
  const { fetchStorageViewCandidates } = loadFunctions(["fetchStorageViewCandidates"], {
    state,
    currentStorageViewSystemId() {
      return systemId;
    },
    currentStorageViewTargetSystemId() {
      return "";
    },
    renderStorageViewCandidates() {},
    setBanner(message, tone) {
      banners.push([message, tone]);
    },
    fetchJson(url) {
      const request = deferred();
      requests.push({ url, ...request });
      return request.promise;
    },
    Array,
  });

  const stale = fetchStorageViewCandidates({ quiet: false });
  systemId = "system-b";
  const fresh = fetchStorageViewCandidates({ quiet: false });
  requests[1].resolve({ system_id: "system-b", candidates: [{ id: "cand-b" }] });
  await fresh;
  requests[0].reject(new Error("system-a exploded"));
  await stale;

  assert.equal(state.storageViewCandidatesSystemId, "system-b");
  assert.deepEqual(state.storageViewCandidates, [{ id: "cand-b" }]);
  assert.equal(state.storageViewCandidatesLoading, false);
  assert.equal(banners.filter(([, tone]) => tone === "error").length, 0, "stale failure must not banner");
});

test("clearing the selected system invalidates in-flight live-enclosure and candidate requests", async () => {
  const liveRequest = deferred();
  const candidateRequest = deferred();
  let systemId = "system-a";
  const banners = [];
  const state = {
    liveEnclosuresRequestSeq: 0,
    liveEnclosuresLoading: false,
    liveEnclosuresError: null,
    liveEnclosuresSystemId: null,
    liveEnclosures: [],
    storageViewCandidatesRequestSeq: 0,
    storageViewCandidatesLoading: false,
    storageViewCandidatesSystemId: null,
    storageViewCandidates: [],
  };
  const { fetchLiveEnclosures, fetchStorageViewCandidates, resetLiveEnclosureState, resetStorageViewCandidateState } = loadFunctions(
    ["resetLiveEnclosureState", "resetStorageViewCandidateState", "fetchLiveEnclosures", "fetchStorageViewCandidates"],
    {
      state,
      currentStorageViewSystemId() {
        return systemId;
      },
      currentStorageViewTargetSystemId() {
        return "";
      },
      renderStorageViews() {},
      renderStorageViewCandidates() {},
      setBanner(message, tone) {
        banners.push([message, tone]);
      },
      fetchJson(url) {
        return url.includes("live-enclosures") ? liveRequest.promise : candidateRequest.promise;
      },
      Array,
    }
  );

  const staleLive = fetchLiveEnclosures({ quiet: false });
  const staleCandidates = fetchStorageViewCandidates({ quiet: false });
  systemId = "";
  await fetchLiveEnclosures({ quiet: false });
  await fetchStorageViewCandidates({ quiet: false });

  liveRequest.resolve({ system_id: "system-a", enclosures: [{ id: "stale-enclosure" }] });
  candidateRequest.resolve({ system_id: "system-a", candidates: [{ id: "stale-candidate" }] });
  await staleLive;
  await staleCandidates;

  assert.equal(state.liveEnclosures.length, 0);
  assert.equal(state.liveEnclosuresSystemId, null);
  assert.equal(state.storageViewCandidates.length, 0);
  assert.equal(state.storageViewCandidatesSystemId, null);
  assert.equal(banners.length, 0, "cleared requests must not emit stale success banners");
  assert.match(functionSource("resetSetupForm"), /resetLiveEnclosureState\(\)/);
  assert.match(functionSource("resetSetupForm"), /resetStorageViewCandidateState\(\)/);
});

test("backup export, debug export, and import errors are described instead of stringified objects", () => {
  const { describeApiError } = loadFunctions(["describeApiError"], { JSON, Array, String });
  const validationDetail = [
    { loc: ["body", "included_paths", 0], msg: "value is not a valid path", type: "value_error" },
    { loc: ["body", "packaging"], msg: "unexpected value", type: "value_error" },
  ];
  const described = describeApiError(validationDetail);
  assert.equal(
    described,
    "body.included_paths.0: value is not a valid path; body.packaging: unexpected value"
  );
  assert.doesNotMatch(described, /\[object Object\]/);

  assert.doesNotMatch(
    SOURCE,
    /throw new Error\(payload\?\.detail \|\|/,
    "every raw payload?.detail throw must route through describeApiError"
  );
  for (const name of ["exportBackup", "exportDebugBundle", "importBackup"]) {
    assert.match(functionSource(name), /describeApiError\(payload\?\.detail\)/, `${name} must describe API errors`);
  }
});

test("backup import reports source-absent groups whose live data was preserved", () => {
  const source = functionSource("importBackup");

  assert.match(source, /payload\.preserved_absent_groups/);
  assert.match(source, /Preserved live data/);
  assert.match(source, /"info"/);
});

test("backup encryption remains enabled after JavaScript startup state sync", () => {
  assert.match(SOURCE, /backupManualEncrypt:\s*true/);
  assert.match(functionSource("syncSingleBundleControls"), /encryptToggle\.checked = encryptEnabled/);
});
