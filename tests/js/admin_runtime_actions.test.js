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

const RUNTIME_FUNCTIONS = [
  "runtimeContainerObservation",
  "runtimeActionHasConverged",
  "describeRuntimeObservation",
  "setRuntimeContainerPending",
  "sleepForRuntimePoll",
  "waitForRuntimeConvergence",
  "executeRuntimeAction",
  "runRuntimeAction",
  "cancelRuntimeActionPolling",
  "cancelAllRuntimeActionPolling",
];

function loadRuntimeFunctions(bindings = {}) {
  const context = vm.createContext({
    AbortController,
    DOMException,
    Error,
    Map,
    Promise,
    setTimeout,
    clearTimeout,
    ...bindings,
  });
  vm.runInContext(
    `${RUNTIME_FUNCTIONS.map(functionSource).join("\n")}\nglobalThis.__tested = { ${RUNTIME_FUNCTIONS.join(", ")} };`,
    context,
    { filename: "admin.js" }
  );
  return context.__tested;
}

function runtimePayload(container) {
  return {
    ok: true,
    runtime: {
      available: true,
      detail: null,
      containers: [
        {
          key: "ui",
          status: container.running ? "running" : "exited",
          status_text: container.status_text || (container.running ? "Up" : "Exited"),
          running: container.running,
          health: Object.hasOwn(container, "health") ? container.health : null,
        },
      ],
    },
  };
}

function fakeButton(containerKey) {
  return { dataset: { containerKey }, disabled: false };
}

function runtimeHarness(observations) {
  const uiStop = fakeButton("ui");
  const uiRestart = fakeButton("ui");
  const historyStart = fakeButton("history");
  const buttons = [uiStop, uiRestart, historyStart];
  const calls = [];
  const banners = [];
  const state = {
    runtime: runtimePayload({ running: true, health: "healthy" }).runtime,
    runtimeActionPromises: new Map(),
    runtimeActionControllers: new Map(),
  };
  const functions = loadRuntimeFunctions({
    state,
    elements: {
      runtimeCards: {
        querySelectorAll() {
          return buttons;
        },
      },
    },
    async fetchJson(url, options = {}) {
      calls.push([url, options.method || "GET"]);
      if (options.method === "POST") {
        return runtimePayload({ running: true, health: "healthy" });
      }
      const next = observations.shift();
      assert.ok(next, "poll should remain within the supplied observations");
      return runtimePayload(next);
    },
    renderRuntimeCards() {},
    setBanner(message, tone) {
      banners.push([message, tone]);
    },
    encodeURIComponent,
  });
  return { functions, state, buttons, calls, banners };
}

const noDelay = async () => {};

test("a resolved poll delay removes its abort listener", async () => {
  const listeners = new Set();
  const signal = {
    aborted: false,
    addEventListener(eventName, listener) {
      assert.equal(eventName, "abort");
      listeners.add(listener);
    },
    removeEventListener(eventName, listener) {
      assert.equal(eventName, "abort");
      listeners.delete(listener);
    },
  };
  const functions = loadRuntimeFunctions();

  await functions.sleepForRuntimePoll(0, signal);

  assert.equal(listeners.size, 0);
});

test("duplicate actions share one request and disable only that container until healthy convergence", async () => {
  const harness = runtimeHarness([
    { running: true, health: "starting", status_text: "Up (health: starting)" },
    { running: true, health: "healthy", status_text: "Up (healthy)" },
  ]);

  const first = harness.functions.runRuntimeAction("ui", "restart", {
    maxAttempts: 3,
    pollIntervalMs: 0,
    sleep: noDelay,
  });
  const duplicate = harness.functions.runRuntimeAction("ui", "restart", {
    maxAttempts: 3,
    pollIntervalMs: 0,
    sleep: noDelay,
  });

  assert.equal(first, duplicate, "duplicate clicks coalesce onto the pending promise");
  assert.deepEqual(harness.buttons.map((button) => button.disabled), [true, true, false]);

  const succeeded = await first;

  assert.equal(succeeded, true);
  assert.equal(harness.calls.filter(([, method]) => method === "POST").length, 1);
  assert.equal(harness.calls.filter(([url]) => url === "/api/admin/runtime").length, 2);
  assert.deepEqual(harness.buttons.map((button) => button.disabled), [false, false, false]);
  assert.ok(harness.banners.some(([message, tone]) => tone === "success" && /confirmed running and healthy/i.test(message)));
});

test("a hung action request is aborted before convergence polling begins", async () => {
  const uiButton = fakeButton("ui");
  const banners = [];
  const state = {
    runtime: runtimePayload({ running: true, health: "healthy" }).runtime,
    runtimeActionPromises: new Map(),
    runtimeActionControllers: new Map(),
  };
  let getCalls = 0;
  const functions = loadRuntimeFunctions({
    state,
    elements: { runtimeCards: { querySelectorAll: () => [uiButton] } },
    fetchJson(_url, options = {}) {
      if (options.method !== "POST") {
        getCalls += 1;
        return Promise.resolve(runtimePayload({ running: true, health: "healthy" }));
      }
      return new Promise((_resolve, reject) => {
        const fallbackTimer = setTimeout(
          () => reject(new Error("test fallback: action request was not aborted")),
          100
        );
        options.signal?.addEventListener("abort", () => {
          clearTimeout(fallbackTimer);
          reject(new DOMException("Aborted", "AbortError"));
        }, { once: true });
      });
    },
    renderRuntimeCards() {},
    setBanner(message, tone) {
      banners.push([message, tone]);
    },
    encodeURIComponent,
  });

  const startedAt = Date.now();
  const succeeded = await functions.runRuntimeAction("ui", "restart", {
    actionTimeoutMs: 10,
    pollIntervalMs: 0,
    sleep: noDelay,
  });
  const elapsedMs = Date.now() - startedAt;

  assert.equal(succeeded, false);
  assert.equal(getCalls, 0);
  assert.ok(elapsedMs < 80, `hung action should abort promptly, elapsed=${elapsedMs}ms`);
  assert.ok(
    banners.some(([message, tone]) =>
      tone === "error"
      && /status unknown/i.test(message)
      && /action request timed out after 10 ms/i.test(message)
      && /state may have changed/i.test(message)
    )
  );
  assert.equal(uiButton.disabled, false);
});

test("stop is not reported successful until a follow-up observation has running false", async () => {
  const harness = runtimeHarness([
    { running: true, health: "healthy", status_text: "Up (healthy)" },
    { running: false, health: null, status_text: "Exited (0)" },
  ]);

  const succeeded = await harness.functions.runRuntimeAction("ui", "stop", {
    maxAttempts: 3,
    pollIntervalMs: 0,
    sleep: noDelay,
  });

  assert.equal(succeeded, true);
  assert.equal(harness.calls.filter(([url]) => url === "/api/admin/runtime").length, 2);
  assert.ok(harness.banners.some(([message, tone]) => tone === "success" && /confirmed stopped/i.test(message)));
});

test("start accepts running state when no health check is available", async () => {
  const harness = runtimeHarness([
    { running: true, health: null, status_text: "Up 2 seconds" },
  ]);

  const succeeded = await harness.functions.runRuntimeAction("ui", "start", {
    maxAttempts: 2,
    pollIntervalMs: 0,
    sleep: noDelay,
  });

  assert.equal(succeeded, true);
  assert.ok(harness.banners.some(([message, tone]) => tone === "success" && /health unavailable/i.test(message)));
});

test("start treats an explicit unavailable health state as unavailable rather than healthy", async () => {
  const harness = runtimeHarness([
    { running: true, health: "unavailable", status_text: "Up 2 seconds" },
  ]);

  const succeeded = await harness.functions.runRuntimeAction("ui", "start", {
    maxAttempts: 2,
    pollIntervalMs: 0,
    sleep: noDelay,
  });

  assert.equal(succeeded, true);
  assert.ok(harness.banners.some(([message, tone]) => tone === "success" && /health unavailable/i.test(message)));
  assert.ok(!harness.banners.some(([message, tone]) => tone === "success" && /running and healthy/i.test(message)));
});

test("timeout reports the last observed state and always unlocks the container controls", async () => {
  const harness = runtimeHarness([
    { running: true, health: "starting", status_text: "Up (health: starting)" },
    { running: true, health: "unhealthy", status_text: "Up (unhealthy)" },
  ]);

  const succeeded = await harness.functions.runRuntimeAction("ui", "restart", {
    maxAttempts: 2,
    pollIntervalMs: 0,
    sleep: noDelay,
  });

  assert.equal(succeeded, false);
  assert.equal(harness.calls.filter(([url]) => url === "/api/admin/runtime").length, 2, "polling is bounded");
  assert.deepEqual(harness.buttons.map((button) => button.disabled), [false, false, false]);
  assert.ok(
    harness.banners.some(([message, tone]) =>
      tone === "error"
      && /timed out/i.test(message)
      && /running=true/i.test(message)
      && /health=unhealthy/i.test(message)
      && /Up \(unhealthy\)/i.test(message)
    )
  );
});

test("pending polling can be cancelled and still unlocks controls", async () => {
  const uiButton = fakeButton("ui");
  const state = {
    runtime: runtimePayload({ running: true, health: "starting" }).runtime,
    runtimeActionPromises: new Map(),
    runtimeActionControllers: new Map(),
  };
  let getCalls = 0;
  const functions = loadRuntimeFunctions({
    state,
    elements: { runtimeCards: { querySelectorAll: () => [uiButton] } },
    fetchJson(url, options = {}) {
      if (options.method === "POST") {
        return Promise.resolve(runtimePayload({ running: true, health: "starting" }));
      }
      getCalls += 1;
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    },
    renderRuntimeCards() {},
    setBanner() {},
    encodeURIComponent,
  });

  const pending = functions.runRuntimeAction("ui", "restart", { maxAttempts: 20, pollIntervalMs: 0, sleep: noDelay });
  for (let index = 0; index < 6 && getCalls === 0; index += 1) {
    await Promise.resolve();
  }
  assert.equal(getCalls, 1, "the follow-up GET is in flight before cancellation");
  assert.equal(uiButton.disabled, true);

  functions.cancelRuntimeActionPolling("ui");
  const succeeded = await pending;

  assert.equal(succeeded, false);
  assert.equal(getCalls, 1);
  assert.equal(uiButton.disabled, false);
  assert.equal(state.runtimeActionPromises.size, 0);
  assert.equal(state.runtimeActionControllers.size, 0);
});

test("a hung status observation is aborted by the per-poll timeout", async () => {
  const uiButton = fakeButton("ui");
  const banners = [];
  const state = {
    runtime: runtimePayload({ running: true, health: "starting" }).runtime,
    runtimeActionPromises: new Map(),
    runtimeActionControllers: new Map(),
  };
  let getCalls = 0;
  const functions = loadRuntimeFunctions({
    state,
    elements: { runtimeCards: { querySelectorAll: () => [uiButton] } },
    fetchJson(_url, options = {}) {
      if (options.method === "POST") {
        return Promise.resolve(runtimePayload({ running: true, health: "starting" }));
      }
      getCalls += 1;
      return new Promise((_resolve, reject) => {
        const fallbackTimer = setTimeout(
          () => reject(new Error("test fallback: request was not aborted")),
          100
        );
        options.signal?.addEventListener("abort", () => {
          clearTimeout(fallbackTimer);
          reject(new DOMException("Aborted", "AbortError"));
        }, { once: true });
      });
    },
    renderRuntimeCards() {},
    setBanner(message, tone) {
      banners.push([message, tone]);
    },
    encodeURIComponent,
  });

  const startedAt = Date.now();
  const succeeded = await functions.runRuntimeAction("ui", "restart", {
    maxAttempts: 1,
    pollIntervalMs: 0,
    pollTimeoutMs: 10,
    sleep: noDelay,
  });
  const elapsedMs = Date.now() - startedAt;

  assert.equal(succeeded, false);
  assert.equal(getCalls, 1);
  assert.ok(elapsedMs < 80, `hung poll should abort promptly, elapsed=${elapsedMs}ms`);
  assert.ok(
    banners.some(([message, tone]) =>
      tone === "error" && /status request timed out after 10 ms/i.test(message)
    )
  );
  assert.equal(uiButton.disabled, false);
});
