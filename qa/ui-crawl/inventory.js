"use strict";

// Load, validate and expand qa/ui-states.json (#ui-coverage).

const fs = require("node:fs");
const path = require("node:path");
const { SAFETY_CLASSES, substitute } = require("./common");
const { RouteTable, DECLARABLE, BASE_READ_CLASSES, loadManifest } = require("./routes");

const INVENTORY_PATH = path.join(__dirname, "..", "ui-states.json");
const STEP_KINDS = new Set([
  "click", "clickIfPresent", "select", "selectIfVisible", "selectBay", "open", "openAll", "fill", "press", "wait",
  "waitFor", "waitText", "waitResponse", "check", "uncheck", "hover", "setRange", "forEachOption", "read",
  "reload", "expectValue", "acceptNextDialog", "download", "ledOff", "requireVisible",
]);
// Placeholders a write rule may use; the actuation runner substitutes them
// from the captured state before the window opens, so a window always names
// one object.
const RULE_PLACEHOLDERS = new Set(["slot", "system"]);
// Steps that can make the page send a write. They are allowed in `steps` of
// any class (the guard decides), but never in `capture` or `verify`.
const ACTING_STEPS = new Set(["click", "clickIfPresent", "fill", "select", "selectIfVisible", "check", "uncheck", "press", "setRange", "forEachOption", "download", "acceptNextDialog"]);
const SAFE_CAPTURE_CLICKS = /^(#enclosure-alias-edit-button|#enclosure-alias-cancel)$/;

function loadInventory(file = INVENTORY_PATH) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function stepKind(step) {
  const kinds = Object.keys(step).filter((key) => STEP_KINDS.has(key));
  return kinds.length === 1 ? kinds[0] : null;
}

function validateSteps(errors, id, field, steps, { readOnly = false } = {}) {
  if (steps === undefined) return;
  if (!Array.isArray(steps)) {
    errors.push(`${id}: ${field} must be a list`);
    return;
  }
  steps.forEach((step, index) => {
    const kind = stepKind(step || {});
    if (!kind) {
      errors.push(`${id}: ${field}[${index}] has no single known step kind (${Object.keys(step || {}).join(", ")})`);
      return;
    }
    if (readOnly && ACTING_STEPS.has(kind) && !(kind === "click" && SAFE_CAPTURE_CLICKS.test(step.click))) {
      errors.push(`${id}: ${field}[${index}] '${kind}' can act; ${field} must only read`);
    }
  });
}

// Check a state's declared rules against the route manifest: each names one
// exact route (placeholders only for the captured bay/system), of a class the
// state's safety class may declare.
function validateRules(errors, id, field, rules, state, page, table) {
  const allowed = new Set(DECLARABLE[state.safety] || []);
  if (state.safety === "read" && state.appliance_load) allowed.add("appliance-load");
  for (const rule of rules || []) {
    if (!rule.method || !rule.path) {
      errors.push(`${id}: every ${field} rule needs method and path`);
      continue;
    }
    if (/\*/.test(rule.path)) {
      errors.push(`${id}: ${field} ${rule.method} ${rule.path} uses a glob; name the exact path with {slot}`);
      continue;
    }
    const unknown = [...String(rule.path).matchAll(/\{(\w+)\}/g)].map((m) => m[1]).filter((name) => !RULE_PLACEHOLDERS.has(name));
    if (unknown.length) errors.push(`${id}: ${field} ${rule.path} uses unknown placeholders ${unknown.join(", ")}`);
    if (!table) continue;
    const sample = String(rule.path).replace(/\{slot\}/g, "1").replace(/\{system\}/g, "sample-system");
    const probe = new URL(`http://rule.invalid${sample}${rule.force ? "?force=true" : ""}`);
    const service = (page && page.service) || "ui";
    const { entry, cls } = table.classify(service, String(rule.method).toUpperCase(), probe);
    if (!entry) {
      errors.push(`${id}: ${field} ${rule.method} ${rule.path} matches no ${service} route in qa/ui-routes.json`);
      continue;
    }
    const resolved = BASE_READ_CLASSES.has(cls) && rule.force ? "appliance-load" : cls;
    if (!allowed.has(resolved)) {
      errors.push(`${id}: ${field} ${rule.method} ${rule.path} is a ${resolved} route; a ${state.safety}${state.appliance_load ? " appliance_load" : ""} state may only declare ${[...allowed].join(", ") || "nothing"}`);
    }
  }
}

function validateInventory(inventory, { manifest = undefined } = {}) {
  const errors = [];
  const ids = new Set();
  const pages = inventory.pages || {};
  const resolvedManifest = manifest === undefined ? loadManifest() : manifest;
  const table = resolvedManifest ? new RouteTable(resolvedManifest) : null;
  for (const state of inventory.states || []) {
    const id = state.id || "(missing id)";
    if (!state.id) errors.push("state without id");
    if (ids.has(id)) errors.push(`${id}: duplicate id`);
    ids.add(id);
    if (!pages[state.page]) errors.push(`${id}: unknown page ${state.page}`);
    if (!SAFETY_CLASSES.includes(state.safety)) errors.push(`${id}: safety must be one of ${SAFETY_CLASSES.join(", ")}`);
    if (!Array.isArray(state.covers)) errors.push(`${id}: covers must be a list`);
    // A state no runner selects is a coverage claim with no rendering behind it.
    if (state.fixture_only && !state.ci) errors.push(`${id}: fixture_only without ci never runs; give it a fixture variant and ci: true`);
    validateSteps(errors, id, "steps", state.steps);
    validateSteps(errors, id, "capture", state.capture, { readOnly: true });
    validateSteps(errors, id, "verify", state.verify, { readOnly: true });
    validateSteps(errors, id, "revert", state.revert);
    const writes = state.writes || [];
    if (state.safety === "read" && writes.length) errors.push(`${id}: a read state cannot declare writes`);
    // The guard admits an appliance poll only when the state declares it.
    if (state.appliance_load && (state.steps || []).length && !(state.read_requests || []).length) {
      errors.push(`${id}: an appliance_load state with steps needs read_requests for the polls it sends`);
    }
    if (SAFETY_CLASSES.includes(state.safety)) {
      validateRules(errors, id, "writes", writes, state, pages[state.page], table);
      validateRules(errors, id, "read_requests", state.read_requests, state, pages[state.page], table);
    }
    if (state.safety === "forbidden") {
      if (writes.length) errors.push(`${id}: a forbidden state cannot declare writes`);
      if ((state.steps || []).length && !state.confirm_preview) errors.push(`${id}: forbidden states have no steps unless confirm_preview is set`);
      if (!state.reason) errors.push(`${id}: forbidden states need a reason`);
    }
    if (state.safety === "app-write-revert" || state.safety === "led") {
      for (const field of ["steps", "revert", "verify", "writes"]) {
        if (!Array.isArray(state[field]) || state[field].length === 0) errors.push(`${id}: ${state.safety} needs a non-empty ${field}`);
      }
      for (const rule of writes) {
        if (!Number.isInteger(rule.max) || rule.max < 1) errors.push(`${id}: every write needs a positive integer max`);
      }
    }
    if (state.safety === "app-write-revert") {
      // #1118: the revert is checked through the app's read API, not only the rendered value.
      if (!Array.isArray(state.semantic_reads) || state.semantic_reads.length === 0) errors.push(`${id}: app-write-revert needs semantic_reads (the store read API that must be equal before and after)`);
    }
    if (state.safety === "led") {
      const actions = writes.map((rule) => (rule.body_json || {}).action);
      if (!actions.includes("IDENTIFY") || !actions.includes("CLEAR")) errors.push(`${id}: led states admit exactly IDENTIFY and CLEAR (body_json.action)`);
      if (!(state.verify || []).some((step) => step.ledOff)) errors.push(`${id}: led states must verify ledOff`);
    }
  }
  for (const entry of inventory.ignored_controls || []) {
    if (!entry.key || !entry.reason) errors.push(`ignored control needs key and reason: ${JSON.stringify(entry)}`);
  }
  for (const gap of inventory.render_gaps || []) {
    if (!gap.key || !gap.reason) errors.push(`render gap needs key and reason: ${JSON.stringify(gap)}`);
  }
  for (const known of inventory.known_findings || []) {
    if (!known.state || !known.type || !known.issue) errors.push(`known finding needs state, type and issue: ${JSON.stringify(known)}`);
  }
  return errors;
}

// Expand `matrix` entries into concrete states (`main.select` x selection
// -> `main.select[healthy]` ...). Substitution touches steps only.
function expandStates(inventory) {
  const out = [];
  for (const state of inventory.states || []) {
    const matrix = state.matrix || {};
    const axes = Object.entries(matrix);
    if (!axes.length) {
      out.push({ ...state, baseId: state.id });
      continue;
    }
    let combos = [{}];
    for (const [name, values] of axes) combos = combos.flatMap((combo) => values.map((value) => ({ ...combo, [name]: value })));
    for (const combo of combos) {
      const suffix = Object.values(combo).join(",");
      out.push({ ...state, ...substitute({ steps: state.steps }, combo), id: `${state.id}[${suffix}]`, baseId: state.id, matrixValues: combo });
    }
  }
  return out;
}

function selectStates(states, { pages = null, ciOnly = false, includeFixtureOnly = false, includeLiveOnly = true, ids = null } = {}) {
  return states.filter((state) => {
    if (pages && !pages.includes(state.page)) return false;
    if (ciOnly && !state.ci) return false;
    if (state.fixture_only && !includeFixtureOnly) return false;
    if (state.live_only && !includeLiveOnly) return false;
    if (ids && !ids.some((id) => state.id === id || state.baseId === id || state.id.startsWith(`${id}[`))) return false;
    return true;
  });
}

// #1120: the safety a state really has, from the route classes its requests
// used (observed by the guard during a fixture crawl, keys "cls METHOD path").
const SAFETY_RANK = { read: 0, "app-write-revert": 1, led: 2, forbidden: 3 };
const CLASS_SAFETY = {
  read: "read", "read-post": "read", "read-heavy": "read", "appliance-load": "read",
  "app-write": "app-write-revert", led: "led", forbidden: "forbidden", internal: "forbidden",
};

function deriveSafety(observed) {
  let worst = "read";
  for (const key of observed) {
    const cls = String(key).split(" ")[0];
    const safety = CLASS_SAFETY[cls] || "forbidden";
    if (SAFETY_RANK[safety] > SAFETY_RANK[worst]) worst = safety;
  }
  return worst;
}

function checkObserved(state, observed) {
  const problems = [];
  const declared = SAFETY_RANK[state.safety] ?? 0;
  for (const key of observed) {
    const cls = String(key).split(" ")[0];
    const safety = CLASS_SAFETY[cls] || "forbidden";
    if (SAFETY_RANK[safety] > declared) problems.push(`${state.id} is ${state.safety} but called ${key}`);
    if (cls === "appliance-load" && state.safety === "read" && !state.appliance_load) problems.push(`${state.id} is not appliance_load but called ${key}`);
  }
  return problems;
}

function summarize(inventory) {
  const byPage = {};
  for (const state of expandStates(inventory)) {
    const page = (byPage[state.page] = byPage[state.page] || { states: 0 });
    page.states += 1;
    page[state.safety] = (page[state.safety] || 0) + 1;
  }
  return byPage;
}

module.exports = { INVENTORY_PATH, loadInventory, validateInventory, expandStates, selectStates, substitute, summarize, stepKind, deriveSafety, checkObserved };
