"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const FABRIC_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/sas_fabric.html"), "utf8");
const ROUTES_SOURCE = fs.readFileSync(path.join(ROOT, "app/routes.py"), "utf8");

const NETWORK_REASON =
  "Writes are disabled: this deployment runs the read UI in network auth mode. "
  + "Set ADMIN_AUTH_MODE=basic to enable mapping, LED and alias changes.";

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

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ URLSearchParams, ...context });
  const source = names.map((name) => functionSource(APP_SOURCE, name)).join("\n");
  const exports = names.map((name) => `this.__${name} = ${name};`).join("\n");
  vm.runInContext(`${source}\n${exports}`, sandbox, { filename: "write-policy-affordance.behavior.js" });
  return Object.fromEntries(names.map((name) => [name, sandbox[`__${name}`]]));
}

function loadFabricFunctions(names, context = {}) {
  const sandbox = vm.createContext({ ...context });
  const source = names.map((name) => functionSource(FABRIC_SOURCE, name)).join("\n");
  const exports = names.map((name) => `this.__${name} = ${name};`).join("\n");
  vm.runInContext(`${source}\n${exports}`, sandbox, { filename: "fabric-write-policy.behavior.js" });
  return Object.fromEntries(names.map((name) => [name, sandbox[`__${name}`]]));
}

function classList(initial = []) {
  const values = new Set(initial);
  return {
    add(name) { values.add(name); },
    remove(name) { values.delete(name); },
    toggle(name, force) {
      if (force === undefined ? !values.has(name) : force) values.add(name);
      else values.delete(name);
    },
    contains(name) { return values.has(name); },
  };
}

function control(name) {
  return {
    name,
    disabled: false,
    title: "",
    dataset: {},
    attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    getAttribute(key) { return this.attributes[key] ?? null; },
    hasAttribute(key) { return Object.hasOwn(this.attributes, key); },
    removeAttribute(key) { delete this.attributes[key]; },
  };
}

const SUBMIT_SELECTOR = 'button[type="submit"]';

const POLICY_FUNCTIONS = [
  "normalizeWritePolicy",
  "writePolicyAllowsWrites",
  "writePolicyReason",
  "writePolicyControls",
  "syncWritePolicyControls",
  "renderWritePolicyNotice",
  "renderReadUiAuth",
  "applyWritePolicy",
  "clearReadUiAuthorization",
  "handleWriteRejection",
  "writeBlockedByPolicy",
];

const AUTH_FUNCTIONS = [
  "encodeBasicAuthorization",
  "readUiAuthenticatedHeaders",
  "fetchJson",
  "clearReadUiAuthorization",
];

function buildHarness(writePolicy) {
  const statuses = [];
  const ledButtons = [control("led-identify"), control("led-clear")];
  const clearMappingButton = control("clear-mapping");
  const importMappingsButton = control("import-mappings");
  const enclosureAliasEditButton = control("alias-edit");
  const enclosureAliasClear = control("alias-clear");
  const mappingSubmit = control("mapping-save");
  const aliasSubmit = control("alias-save");
  const mappingForm = { querySelector: (selector) => (selector === SUBMIT_SELECTOR ? mappingSubmit : null) };
  const enclosureAliasForm = { querySelector: (selector) => (selector === SUBMIT_SELECTOR ? aliasSubmit : null) };
  const writePolicyNotice = { textContent: "", classList: classList(["hidden"]) };
  const state = { snapshotMode: false, writePolicy };
  const fns = loadFunctions(POLICY_FUNCTIONS, {
    state,
    ledButtons,
    clearMappingButton,
    importMappingsButton,
    enclosureAliasEditButton,
    enclosureAliasClear,
    mappingForm,
    enclosureAliasForm,
    writePolicyNotice,
    readUiAuthPanel: null,
    readUiAuthUsername: null,
    readUiAuthPassword: null,
    setStatus(message, tone) { statuses.push({ message, tone }); },
  });
  const controls = [
    ...ledButtons,
    clearMappingButton,
    importMappingsButton,
    enclosureAliasEditButton,
    enclosureAliasClear,
    mappingSubmit,
    aliasSubmit,
  ];
  return { fns, state, controls, writePolicyNotice, statuses };
}

test("a missing bootstrap policy means writes stay enabled (snapshot artifacts unaffected)", () => {
  const { fns } = buildHarness(undefined);
  // Spread copies the sandbox-realm object so deepEqual compares values, not prototypes.
  const normalize = (raw) => ({ ...fns.normalizeWritePolicy(raw) });
  assert.deepEqual(normalize(undefined), { enabled: true, mode: "", reason: "" });
  assert.deepEqual(normalize(null), { enabled: true, mode: "", reason: "" });
  assert.deepEqual(normalize({ enabled: true, mode: "basic", reason: "" }), {
    enabled: true,
    mode: "basic",
    reason: "",
  });
  assert.deepEqual(normalize({ enabled: false, mode: "network", reason: NETWORK_REASON }), {
    enabled: false,
    mode: "network",
    reason: NETWORK_REASON,
  });
});

test("network-mode policy disables every write control with the reason and a notice", () => {
  const { fns, state, controls, writePolicyNotice } = buildHarness(undefined);
  state.writePolicy = fns.normalizeWritePolicy({ enabled: false, mode: "network", reason: NETWORK_REASON });

  fns.renderWritePolicyNotice();
  fns.syncWritePolicyControls();

  assert.equal(fns.writePolicyAllowsWrites(), false);
  for (const element of controls) {
    assert.equal(element.disabled, true, `${element.name} must be disabled`);
    assert.equal(element.title, NETWORK_REASON, `${element.name} must carry the reason`);
    assert.equal(element.attributes["aria-describedby"], "write-policy-notice", `${element.name} must reference the notice`);
  }
  assert.equal(writePolicyNotice.textContent, NETWORK_REASON);
  assert.equal(writePolicyNotice.classList.contains("hidden"), false);
});

test("an enabled policy leaves the controls and notice untouched", () => {
  const { fns, state, controls, writePolicyNotice } = buildHarness(undefined);
  state.writePolicy = fns.normalizeWritePolicy({ enabled: true, mode: "basic", reason: "" });

  fns.renderWritePolicyNotice();
  fns.syncWritePolicyControls();

  for (const element of controls) {
    assert.equal(element.disabled, false, `${element.name} must stay enabled`);
    assert.equal(element.title, "");
    assert.equal(element.attributes["aria-describedby"], undefined);
  }
  assert.equal(writePolicyNotice.textContent, "");
  assert.equal(writePolicyNotice.classList.contains("hidden"), true);
  assert.equal(fns.writeBlockedByPolicy(), false);
});

test("sign-in restores policy-owned controls without changing independently disabled controls", () => {
  const { fns, state, controls } = buildHarness({ enabled: false, mode: "basic", reason: NETWORK_REASON });
  const policyOwned = controls[0];
  const independentlyDisabled = controls[1];
  policyOwned.title = "Identify this bay";
  policyOwned.setAttribute("aria-describedby", "slot-help");
  independentlyDisabled.disabled = true;
  independentlyDisabled.title = "LED unavailable";
  independentlyDisabled.setAttribute("aria-describedby", "led-help");

  fns.syncWritePolicyControls();
  assert.equal(policyOwned.disabled, true);
  assert.equal(policyOwned.title, NETWORK_REASON);
  assert.equal(independentlyDisabled.disabled, true);
  assert.equal(independentlyDisabled.title, "LED unavailable");
  assert.equal(independentlyDisabled.getAttribute("aria-describedby"), "led-help");

  state.writePolicy = { enabled: true, mode: "basic", reason: "" };
  fns.syncWritePolicyControls();
  assert.equal(policyOwned.disabled, false);
  assert.equal(policyOwned.title, "Identify this bay");
  assert.equal(policyOwned.getAttribute("aria-describedby"), "slot-help");
  assert.equal(independentlyDisabled.disabled, true);
  assert.equal(independentlyDisabled.title, "LED unavailable");
  assert.equal(independentlyDisabled.getAttribute("aria-describedby"), "led-help");
});

test("a 401/403 write response applies the server detail as the disabled reason", () => {
  const { fns, state, controls, writePolicyNotice, statuses } = buildHarness({ enabled: true, mode: "basic", reason: "" });

  const denied = new Error("Read UI authentication required.");
  denied.status = 401;
  denied.detail = "Read UI authentication required.";
  assert.equal(fns.handleWriteRejection(denied), true);

  assert.equal(state.writePolicy.enabled, false);
  assert.equal(state.writePolicy.mode, "basic");
  assert.equal(state.writePolicy.reason, "Read UI authentication required.");
  for (const element of controls) {
    assert.equal(element.disabled, true, `${element.name} must be disabled after a rejected write`);
    assert.equal(element.title, "Read UI authentication required.");
  }
  assert.equal(writePolicyNotice.textContent, "Read UI authentication required.");
  assert.equal(writePolicyNotice.classList.contains("hidden"), false);

  assert.equal(fns.writeBlockedByPolicy(), true);
  assert.deepEqual(statuses, [{ message: "Read UI authentication required.", tone: "error" }]);
});

test("other write failures do not change the policy", () => {
  const { fns, state, controls } = buildHarness({ enabled: true, mode: "basic", reason: "" });

  const conflict = new Error("Mapping revision changed.");
  conflict.status = 409;
  conflict.detail = "Mapping revision changed.";
  assert.equal(fns.handleWriteRejection(conflict), false);
  assert.equal(fns.handleWriteRejection(new Error("network down")), false);
  assert.equal(fns.handleWriteRejection(null), false);

  assert.equal(state.writePolicy.enabled, true);
  for (const element of controls) {
    assert.equal(element.disabled, false);
  }
});

test("fetchJson exposes the HTTP status and server detail on the thrown error", async () => {
  const { fetchJson } = loadFunctions(["fetchJson"], {
    fetch: async () => ({
      ok: false,
      status: 403,
      json: async () => ({ ok: false, detail: "Read UI mutations require ADMIN_AUTH_MODE=basic." }),
    }),
  });

  await assert.rejects(fetchJson("/api/slots/0/led", { method: "POST" }), (error) => {
    assert.equal(error.status, 403);
    assert.equal(error.detail, "Read UI mutations require ADMIN_AUTH_MODE=basic.");
    assert.equal(error.message, "Read UI mutations require ADMIN_AUTH_MODE=basic.");
    return true;
  });

  const { fetchJson: fetchJsonNoBody } = loadFunctions(["fetchJson"], {
    fetch: async () => ({
      ok: false,
      status: 401,
      json: async () => { throw new SyntaxError("no body"); },
    }),
  });
  await assert.rejects(fetchJsonNoBody("/api/slots/0/mapping", { method: "POST" }), (error) => {
    assert.equal(error.status, 401);
    assert.equal(error.message, "Request failed with 401");
    return true;
  });
});

test("every guarded write handler checks the policy first and reports rejections", () => {
  for (const name of ["sendLedAction", "saveMapping", "clearMapping", "importMappingsFromFile", "submitEnclosureAlias"]) {
    const source = functionSource(APP_SOURCE, name);
    assert.match(source, /if \(writeBlockedByPolicy\(\)\) \{/, `${name} must refuse a write the policy forbids`);
    assert.match(source, /handleWriteRejection\(error\)/, `${name} must apply a 401/403 rejection`);
  }
  assert.match(functionSource(APP_SOURCE, "setMappingFormEnabled"), /syncWritePolicyControls\(\)/);
  assert.match(APP_SOURCE, /writePolicy: normalizeWritePolicy\(bootstrap\.writePolicy\)/);
  assert.match(TEMPLATE, /id="write-policy-notice"/);
  assert.match(TEMPLATE, /writePolicy: \{\{ write_policy_json \| default\("null"\) \| script_json_text \}\}/);
});

test("Storage Fabric bootstrap and alias markup carry the same write policy", () => {
  assert.match(ROUTES_SOURCE, /"writePolicy": resolve_read_ui_write_policy\(request\)/);
  assert.match(FABRIC_SOURCE, /writePolicy: normalizeFabricWritePolicy\(bootstrap\.writePolicy\)/);
  assert.match(FABRIC_TEMPLATE, /id="fabric-write-policy-notice"/);

  const renderAliasRow = functionSource(FABRIC_SOURCE, "renderAliasRow");
  assert.match(renderAliasRow, /fabricAliasWriteAttributes\(\)/);
  const saveAliasFromForm = functionSource(FABRIC_SOURCE, "saveAliasFromForm");
  assert.match(saveAliasFromForm, /if \(fabricWriteBlockedByPolicy\(\)\) \{/);
  assert.match(saveAliasFromForm, /handleFabricWriteRejection\(error\)/);
  const handleFabricActivation = functionSource(FABRIC_SOURCE, "handleFabricActivation");
  assert.match(handleFabricActivation, /if \(fabricWriteBlockedByPolicy\(\)\) \{/);
});

test("Storage Fabric rejects blocked alias writes and adopts 401/403 details", () => {
  const state = {
    writePolicy: { enabled: false, mode: "network", reason: NETWORK_REASON },
    error: null,
  };
  const fns = loadFabricFunctions([
    "normalizeFabricWritePolicy",
    "fabricWritePolicyAllowsWrites",
    "fabricWritePolicyReason",
    "fabricAliasWriteAttributes",
    "fabricWriteBlockedByPolicy",
    "clearFabricAuthorization",
    "handleFabricWriteRejection",
  ], { state, elements: { authPassword: null } });

  assert.equal(fns.fabricWritePolicyAllowsWrites(), false);
  assert.equal(
    fns.fabricAliasWriteAttributes(),
    ' disabled aria-describedby="fabric-write-policy-notice"',
  );
  assert.equal(fns.fabricWriteBlockedByPolicy(), true);
  assert.equal(state.error, NETWORK_REASON);

  state.writePolicy = fns.normalizeFabricWritePolicy({ enabled: true, mode: "basic", reason: "" });
  const denied = new Error("Read UI authentication required.");
  denied.status = 401;
  denied.detail = "Read UI authentication required.";
  assert.equal(fns.handleFabricWriteRejection(denied), true);
  assert.equal(state.writePolicy.enabled, false);
  assert.equal(state.writePolicy.mode, "basic");
  assert.equal(state.writePolicy.reason, "Read UI authentication required.");
  assert.equal(state.error, "Read UI authentication required.");
});

test("main UI Basic credentials stay in memory and are sent only on explicit same-origin auth requests", async () => {
  const requests = [];
  const state = { writeAuthorization: null };
  const window = {
    location: {
      href: "https://ui.example.test/enclosures",
      origin: "https://ui.example.test",
    },
  };
  const fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  const fns = loadFunctions(AUTH_FUNCTIONS, {
    state,
    window,
    fetch,
    TextEncoder,
    URL,
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    readUiAuthUsername: null,
    readUiAuthPassword: null,
  });

  state.writeAuthorization = fns.encodeBasicAuthorization("opérator", "pässphrase");
  assert.match(state.writeAuthorization, /^Basic [A-Za-z0-9+/]+=*$/);

  await fns.fetchJson("/api/inventory");
  await fns.fetchJson("/api/read-ui/auth/verify", { readUiAuth: true });

  assert.equal(requests[0].options.headers.Authorization, undefined);
  assert.equal(requests[1].options.headers.Authorization, state.writeAuthorization);
  await assert.rejects(
    fns.fetchJson("https://attacker.example/write", { readUiAuth: true }),
    /same-origin/,
  );
  assert.equal(requests.length, 2, "cross-origin rejection must happen before fetch");

  fns.clearReadUiAuthorization();
  assert.equal(state.writeAuthorization, null);
  await assert.rejects(
    fns.fetchJson("/api/slots/0/led", { method: "POST", readUiAuth: true }),
    /Sign in/,
  );
  assert.equal(requests.length, 2, "signed-out rejection must happen before fetch");
});

test("successful sign-in rerenders feature-owned control state before exposing writes", async () => {
  const state = {
    snapshotMode: false,
    writePolicy: { enabled: false, mode: "basic", reason: "Sign in." },
    writeAuthorization: null,
    writeAuthPending: false,
  };
  let renderCount = 0;
  const fns = loadFunctions(["submitReadUiSignIn"], {
    state,
    readUiAuthUsername: { value: "operator" },
    readUiAuthPassword: { value: "synthetic-passphrase" },
    encodeBasicAuthorization: () => "Basic synthetic",
    renderReadUiAuth() {},
    fetchJson: async () => ({ ok: true }),
    applyWritePolicy(policy) { state.writePolicy = policy; },
    renderAll() { renderCount += 1; },
    setStatus() {},
    clearReadUiAuthorization() { state.writeAuthorization = null; },
  });

  await fns.submitReadUiSignIn({ preventDefault() {} });

  assert.equal(state.writePolicy.enabled, true);
  assert.equal(renderCount, 1);
});

test("sign-out clears the in-memory header and both credential fields on both live pages", () => {
  const mainState = { writeAuthorization: "Basic synthetic" };
  const mainUsername = { value: "operator" };
  const mainPassword = { value: "passphrase" };
  const main = loadFunctions(["clearReadUiAuthorization"], {
    state: mainState,
    readUiAuthUsername: mainUsername,
    readUiAuthPassword: mainPassword,
  });
  main.clearReadUiAuthorization();
  assert.equal(mainState.writeAuthorization, null);
  assert.equal(mainUsername.value, "");
  assert.equal(mainPassword.value, "");

  const fabricState = { writeAuthorization: "Basic synthetic" };
  const fabricUsername = { value: "operator" };
  const fabricPassword = { value: "passphrase" };
  const fabric = loadFabricFunctions(["clearFabricAuthorization"], {
    state: fabricState,
    elements: { authUsername: fabricUsername, authPassword: fabricPassword },
  });
  fabric.clearFabricAuthorization();
  assert.equal(fabricState.writeAuthorization, null);
  assert.equal(fabricUsername.value, "");
  assert.equal(fabricPassword.value, "");
});

test("a signed-in 403 state reports that writes remain blocked on both live pages", () => {
  const node = () => ({
    disabled: false,
    textContent: "",
    classList: { toggle() {} },
  });
  const mainStatus = node();
  const main = loadFunctions(["writePolicyAllowsWrites", "writePolicyReason", "renderReadUiAuth"], {
    state: {
      snapshotMode: false,
      writeAuthorization: "Basic synthetic",
      writeAuthPending: false,
      writePolicy: { enabled: false, mode: "basic", reason: "Origin is not allowed." },
    },
    readUiAuthPanel: node(),
    readUiAuthForm: node(),
    readUiAuthUsername: node(),
    readUiAuthPassword: node(),
    readUiAuthSubmit: node(),
    readUiAuthSignOut: node(),
    readUiAuthStatus: mainStatus,
  });
  main.renderReadUiAuth();
  assert.match(mainStatus.textContent, /writes are blocked/i);
  assert.match(mainStatus.textContent, /Origin is not allowed/);

  const fabricStatus = node();
  const fabric = loadFabricFunctions([
    "fabricWritePolicyAllowsWrites",
    "fabricWritePolicyReason",
    "renderFabricReadUiAuth",
  ], {
    state: {
      writeAuthorization: "Basic synthetic",
      writeAuthPending: false,
      writePolicy: { enabled: false, mode: "basic", reason: "Origin is not allowed." },
    },
    elements: {
      authPanel: node(),
      authForm: node(),
      authUsername: node(),
      authPassword: node(),
      authSubmit: node(),
      authSignOut: node(),
      authStatus: fabricStatus,
    },
  });
  fabric.renderFabricReadUiAuth();
  assert.match(fabricStatus.textContent, /writes are blocked/i);
  assert.match(fabricStatus.textContent, /Origin is not allowed/);
});

test("main and Storage Fabric templates expose memory-only sign-in and explicit sign-out controls", () => {
  for (const [template, prefix] of [[TEMPLATE, "read-ui"], [FABRIC_TEMPLATE, "fabric-read-ui"]]) {
    assert.match(template, new RegExp(`id="${prefix}-auth-form"`));
    assert.match(template, new RegExp(`id="${prefix}-auth-username"[^>]+autocomplete="off"`));
    assert.match(template, new RegExp(`id="${prefix}-auth-password"[^>]+type="password"[^>]+autocomplete="off"`));
    assert.match(template, new RegExp(`id="${prefix}-auth-sign-out"`));
  }
  const credentialLifecycleSource = [
    functionSource(APP_SOURCE, "submitReadUiSignIn"),
    functionSource(APP_SOURCE, "clearReadUiAuthorization"),
    functionSource(APP_SOURCE, "signOutReadUi"),
    functionSource(FABRIC_SOURCE, "submitFabricReadUiSignIn"),
    functionSource(FABRIC_SOURCE, "clearFabricAuthorization"),
    functionSource(FABRIC_SOURCE, "signOutFabricReadUi"),
  ].join("\n");
  assert.doesNotMatch(credentialLifecycleSource, /localStorage|sessionStorage|document\.cookie/);
});

test("every main and Storage Fabric mutation explicitly opts into the in-memory authorization header", () => {
  for (const name of ["sendLedAction", "saveMapping", "clearMapping", "importMappingsFromFile", "submitEnclosureAlias"]) {
    assert.match(functionSource(APP_SOURCE, name), /readUiAuth:\s*true/, `${name} must request the memory-only header`);
  }
  assert.match(functionSource(FABRIC_SOURCE, "saveAliasFromForm"), /readUiAuth:\s*true/);
});
