"use strict";

// #418 acceptance item 2: the admin client must distinguish transport/offline,
// validation and unknown mutation outcomes instead of collapsing every failed
// response into one plain Error.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");

function functionSource(name, { optional = false } = {}) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  // The outcome helpers are looked up leniently so this file still loads
  // fetchJson on a revision that does not have them: the assertions below then
  // fail on the collapsed outcome itself rather than on a missing name.
  if (start === -1 && optional) {
    return "";
  }
  assert.notEqual(start, -1, `missing ${name}`);
  const bodyStart = SOURCE.indexOf("{", SOURCE.indexOf(")", start));
  let depth = 0;
  for (let index = bodyStart; index < SOURCE.length; index += 1) {
    if (SOURCE[index] === "{") {
      depth += 1;
    } else if (SOURCE[index] === "}") {
      depth -= 1;
      if (depth === 0) {
        return SOURCE.slice(start, index + 1);
      }
    }
  }
  assert.fail(`unterminated ${name}`);
}

const REQUIRED_NAMES = [
  "fetchJson",
  "readJsonResponse",
  "describeRequestFailure",
  "describeApiError",
  "validatedRequestId",
  "fetchOrReportStopped",
  "sessionRemainingMs",
  "fetchWithTimeout",
  "requestTimeoutError",
];

const OUTCOME_NAMES = [
  "isMutatingRequest",
  "browserIsOffline",
  "adminRequestError",
  "classifyTransportFailure",
  "describeTransportFailure",
  "classifyResponseFailure",
  "describeResponseFailure",
];

function loadFetchJson(bindings = {}) {
  const context = vm.createContext({ state: { admin: {} }, AbortController, setTimeout, clearTimeout, ...bindings });
  const sources = [
    ...REQUIRED_NAMES.map((name) => functionSource(name)),
    ...OUTCOME_NAMES.map((name) => functionSource(name, { optional: true })),
  ].filter(Boolean);
  vm.runInContext(
    `const SERVER_REQUEST_ID_PATTERN = /^[0-9a-f]{32}$/;\nconst DEFAULT_REQUEST_TIMEOUT_MS = 60000;\n` +
      `${sources.join("\n")}\n` +
      `globalThis.__tested = { fetchJson };`,
    context,
    { filename: "admin-request-outcomes.js" }
  );
  return context.__tested;
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  };
}

async function captureError(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  assert.fail("expected fetchJson to reject");
}

test("an unreachable sidecar on a read is a transport outcome, not a plain Error", async () => {
  const { fetchJson } = loadFetchJson({
    fetch: async () => {
      throw new TypeError("Failed to fetch");
    },
    navigator: { onLine: true },
  });

  const error = await captureError(fetchJson("/api/admin/state"));

  assert.equal(error.adminOutcome, "transport");
  assert.equal(error.outcomeUnknown, false);
  assert.ok(/could not be reached/i.test(error.message), error.message);
  assert.ok(/nothing was changed/i.test(error.message), error.message);
});

for (const method of ["GET", "POST"]) {
  test(`offline hint does not veto reachable local ${method}`, async () => {
    let calls = 0;
    const { fetchJson } = loadFetchJson({
      fetch: async () => { calls++; return jsonResponse(200, { ok: true }); },
      navigator: { onLine: false },
    });
    assert.deepEqual(await fetchJson("/api/admin/profiles", { method }), { ok: true });
    assert.equal(calls, 1);
  });

  test(`offline hint with failed ${method} uses actual dispatch, without retry`, async () => {
    let calls = 0;
    const { fetchJson } = loadFetchJson({
      fetch: async () => { calls++; throw new TypeError("Failed to fetch"); },
      navigator: { onLine: false },
    });
    const error = await captureError(fetchJson("/api/admin/profiles", { method }));
    assert.equal(calls, 1);
    assert.equal(error.requestDispatched, true);
    assert.equal(error.adminOutcome, method === "POST" ? "unknown" : "transport");
    assert.equal(error.outcomeUnknown, method === "POST");
    assert.doesNotMatch(error.message, /not sent/i);
    if (method === "POST") assert.match(error.message, /check before retrying/i);
  });
}

test("offline hint does not change pre-dispatch cancellation or dispatch a retry", async () => {
  let calls = 0;
  const controller = new AbortController();
  controller.abort();
  const { fetchJson } = loadFetchJson({
    fetch: async () => { calls++; return jsonResponse(200, { ok: true }); },
    navigator: { onLine: false },
  });
  const error = await captureError(fetchJson("/api/admin/profiles", { method: "POST", signal: controller.signal }));
  assert.equal(error.name, "AbortError");
  assert.equal(error.requestDispatched, false);
  assert.equal(calls, 0);
});

test("a mutation dispatched online that fails once the browser is offline is an unknown outcome", async () => {
  // navigator.onLine read at catch time cannot prove the request never left:
  // the browser was online when fetch was invoked, so the sidecar may have
  // received and applied the change before the link dropped.
  const navigator = { onLine: true };
  const { fetchJson } = loadFetchJson({
    fetch: async () => {
      navigator.onLine = false;
      throw new TypeError("Failed to fetch");
    },
    navigator,
  });

  const error = await captureError(fetchJson("/api/admin/profiles", { method: "POST" }));

  assert.equal(error.adminOutcome, "unknown");
  assert.equal(error.outcomeUnknown, true);
  assert.ok(/unknown whether the change was applied/i.test(error.message), error.message);
  assert.ok(!/not sent/i.test(error.message), error.message);
});

test("a read dispatched online that fails once the browser is offline does not claim it was never sent", async () => {
  const navigator = { onLine: true };
  const { fetchJson } = loadFetchJson({
    fetch: async () => {
      navigator.onLine = false;
      throw new TypeError("Failed to fetch");
    },
    navigator,
  });

  const error = await captureError(fetchJson("/api/admin/state"));

  assert.equal(error.adminOutcome, "transport");
  assert.equal(error.outcomeUnknown, false);
  assert.ok(/could not be reached/i.test(error.message), error.message);
  assert.ok(!/not sent/i.test(error.message), error.message);
});

test("a 422 is a validation outcome that names the input", async () => {
  const { fetchJson } = loadFetchJson({
    fetch: async () =>
      jsonResponse(422, { detail: [{ loc: ["body", "label"], msg: "field required" }] }),
    navigator: { onLine: true },
  });

  const error = await captureError(
    fetchJson("/api/admin/profiles", { method: "POST", body: "{}" })
  );

  assert.equal(error.adminOutcome, "validation");
  assert.equal(error.outcomeUnknown, false);
  // #487 maps pydantic locations to the form label the user sees.
  assert.ok(error.message.includes("Name: field required"), error.message);
  assert.ok(/correct the submitted values/i.test(error.message), error.message);
});

test("a mutation that fails without reaching a decision is an unknown outcome", async () => {
  const { fetchJson } = loadFetchJson({
    fetch: async () => jsonResponse(502, { detail: "Upstream failure." }),
    navigator: { onLine: true },
  });

  const error = await captureError(fetchJson("/api/admin/system-setup", { method: "POST" }));

  assert.equal(error.adminOutcome, "unknown");
  assert.equal(error.outcomeUnknown, true);
  assert.ok(/may or may not have been applied/i.test(error.message), error.message);
});

test("a mutation whose transport dies after the send is an unknown outcome", async () => {
  const { fetchJson } = loadFetchJson({
    fetch: async () => {
      throw new TypeError("NetworkError when attempting to fetch resource.");
    },
    navigator: { onLine: true },
  });

  const error = await captureError(fetchJson("/api/admin/profiles", { method: "DELETE" }));

  assert.equal(error.adminOutcome, "unknown");
  assert.equal(error.outcomeUnknown, true);
  assert.ok(/unknown whether the change was applied/i.test(error.message), error.message);
});

test("a decided server refusal stays a plain error outcome", async () => {
  const { fetchJson } = loadFetchJson({
    fetch: async () => jsonResponse(403, { detail: "Admin authentication required." }),
    navigator: { onLine: true },
  });

  const error = await captureError(fetchJson("/api/admin/state"));

  assert.equal(error.adminOutcome, "error");
  assert.equal(error.outcomeUnknown, false);
  assert.equal(error.message, "Admin authentication required.");
});

test("a read that fails with 500 is not claimed as an unknown mutation", async () => {
  const { fetchJson } = loadFetchJson({
    fetch: async () => jsonResponse(500, { detail: "Internal error." }),
    navigator: { onLine: true },
  });

  const error = await captureError(fetchJson("/api/admin/state"));

  assert.equal(error.adminOutcome, "error");
  assert.ok(!/may or may not/i.test(error.message), error.message);
});

test("an aborted request keeps its own cancellation contract", async () => {
  const abortError = new Error("aborted");
  abortError.name = "AbortError";
  const { fetchJson } = loadFetchJson({
    fetch: async () => {
      throw abortError;
    },
    navigator: { onLine: true },
  });

  const error = await captureError(
    fetchJson("/api/admin/runtime/containers/x/restart", { method: "POST" })
  );

  assert.equal(error, abortError);
  assert.equal(error.adminOutcome, undefined);
});


// Refresh publication belongs to the admitted caller, not the eventual queue dispatch.
function refreshPublicationFixture() {
  const { loadAdminFunctions } = require("./helpers/admin_transport");
  const requests = [];
  const state = { bannerRevision: 0, setupEditorGeneration: 0, setupDraftRevision: 0,
    refreshPromise: null, refreshQueued: null, selectedBackupPaths: [], selectedDebugPaths: [],
    systems: [], profiles: [] };
  const elements = { refreshStateButton: { disabled: false },
    banner: { textContent: "Ready.", classList: { add() {}, remove() {} } } };
  const api = loadAdminFunctions(["setBanner", "refreshState", "startRefreshState", "runRefreshState"], {
    state, elements,
    fetchJson: () => new Promise((resolve, reject) => requests.push({ resolve, reject })),
    currentStagedEsxiHostPrepPackages: () => [], renderAll() {}, loadOrphanedHistory() {},
  });
  return { api, state, elements, requests };
}

for (const quiet of [false, true]) {
  test(`current refresh quiet=${quiet} publishes a real failure and releases busy ownership`, async () => {
    const f = refreshPublicationFixture();
    const run = f.api.refreshState({ quiet });
    f.requests[0].reject(new Error("Synthetic current refresh failure"));
    await run;
    assert.match(f.elements.banner.textContent, /Unable to refresh admin state: Synthetic current refresh failure/);
    assert.equal(f.elements.refreshStateButton.disabled, false);
    assert.equal(f.requests.length, 1, "no automatic retry");
  });
}

test("queued foreground refresh owns failure publication before its read dispatches", async () => {
  const f = refreshPublicationFixture();
  const first = f.api.refreshState();
  const second = f.api.refreshState();
  f.requests[0].reject(new Error("Synthetic superseded read failure"));
  await first;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.requests.length, 2, "required queued read still dispatches");
  assert.equal(f.elements.refreshStateButton.disabled, true);
  assert.doesNotMatch(f.elements.banner.textContent, /superseded/);
  f.requests[1].reject(new Error("Synthetic current queued failure"));
  await second;
  assert.match(f.elements.banner.textContent, /Synthetic current queued failure/);
  assert.equal(f.elements.refreshStateButton.disabled, false);
});

test("closed restore cannot replace a live queued refresh notification owner", async () => {
  const f = refreshPublicationFixture();
  const first = f.api.refreshState();
  f.api.setBanner("Backup restored.", "success");
  const live = f.api.refreshState({ quiet: true, canPublish: () => true, failureMessage: "Backup restored, but refresh failed." });
  const closed = f.api.refreshState({ quiet: true, canPublish: () => false });
  assert.equal(live, closed, "both restores still await the shared fresh read");
  f.requests[0].resolve({ systems: [], profiles: [] });
  await first;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.requests.length, 2);
  f.requests[1].reject(new Error("Synthetic read failure"));
  await live;
  assert.equal(f.elements.banner.textContent, "Backup restored, but refresh failed.");
});

for (const changed of ["banner", "setupEditorGeneration", "setupDraftRevision"]) {
  test(`refresh error cannot replace a later ${changed} intent`, async () => {
    const f = refreshPublicationFixture();
    const run = f.api.refreshState();
    const banner = f.elements.banner.textContent;
    if (changed === "banner") f.api.setBanner(banner); // Same text, new owner.
    else f.state[changed]++;
    f.requests[0].reject(new Error("Synthetic superseded failure"));
    await run;
    assert.equal(f.elements.banner.textContent, banner);
    assert.equal(f.elements.refreshStateButton.disabled, false);
    assert.equal(f.requests.length, 1);
  });
}
