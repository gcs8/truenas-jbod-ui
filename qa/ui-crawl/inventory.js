"use strict";

// Load, validate and expand qa/ui-states.json (#ui-coverage).

const fs = require("node:fs");
const path = require("node:path");
const { SAFETY_CLASSES } = require("./actuation");

const INVENTORY_PATH = path.join(__dirname, "..", "ui-states.json");
const STEP_KINDS = new Set([
  "click", "clickIfPresent", "select", "selectIfVisible", "selectBay", "open", "openAll", "fill", "press", "wait",
  "waitFor", "waitText", "waitResponse", "check", "uncheck", "hover", "setRange", "forEachOption", "read",
  "reload", "expectValue", "acceptNextDialog", "download", "ledOff", "requireVisible",
]);
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

function validateInventory(inventory) {
  const errors = [];
  const ids = new Set();
  const pages = inventory.pages || {};
  for (const state of inventory.states || []) {
    const id = state.id || "(missing id)";
    if (!state.id) errors.push("state without id");
    if (ids.has(id)) errors.push(`${id}: duplicate id`);
    ids.add(id);
    if (!pages[state.page]) errors.push(`${id}: unknown page ${state.page}`);
    if (!SAFETY_CLASSES.includes(state.safety)) errors.push(`${id}: safety must be one of ${SAFETY_CLASSES.join(", ")}`);
    if (!Array.isArray(state.covers)) errors.push(`${id}: covers must be a list`);
    validateSteps(errors, id, "steps", state.steps);
    validateSteps(errors, id, "capture", state.capture, { readOnly: true });
    validateSteps(errors, id, "verify", state.verify, { readOnly: true });
    validateSteps(errors, id, "revert", state.revert);
    const writes = state.writes || [];
    if (state.safety === "read" && writes.length) errors.push(`${id}: a read state cannot declare writes`);
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
        if (!rule.method || !rule.path) errors.push(`${id}: every write needs method and path`);
        if (!Number.isInteger(rule.max) || rule.max < 1) errors.push(`${id}: every write needs a positive integer max`);
      }
    }
    if (state.safety === "led") {
      const bodies = writes.map((rule) => rule.body_includes);
      if (!bodies.includes("IDENTIFY") || !bodies.includes("CLEAR")) errors.push(`${id}: led states admit exactly IDENTIFY and CLEAR`);
      if (writes.some((rule) => !/\/led$/.test(rule.path))) errors.push(`${id}: led states may only write the led endpoint`);
      if (!(state.verify || []).some((step) => step.ledOff)) errors.push(`${id}: led states must verify ledOff`);
    }
  }
  for (const entry of inventory.ignored_controls || []) {
    if (!entry.key || !entry.reason) errors.push(`ignored control needs key and reason: ${JSON.stringify(entry)}`);
  }
  for (const known of inventory.known_findings || []) {
    if (!known.state || !known.type || !known.issue) errors.push(`known finding needs state, type and issue: ${JSON.stringify(known)}`);
  }
  return errors;
}

function substitute(value, vars) {
  if (typeof value === "string") return value.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? String(vars[name]) : match));
  if (Array.isArray(value)) return value.map((item) => substitute(item, vars));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, substitute(item, vars)]));
  return value;
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

function summarize(inventory) {
  const byPage = {};
  for (const state of expandStates(inventory)) {
    const page = (byPage[state.page] = byPage[state.page] || { states: 0 });
    page.states += 1;
    page[state.safety] = (page[state.safety] || 0) + 1;
  }
  return byPage;
}

module.exports = { INVENTORY_PATH, loadInventory, validateInventory, expandStates, selectStates, substitute, summarize, stepKind };
