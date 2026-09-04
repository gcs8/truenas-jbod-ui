"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

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
    attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
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
  "applyWritePolicy",
  "handleWriteRejection",
  "writeBlockedByPolicy",
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
