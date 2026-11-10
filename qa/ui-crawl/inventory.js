"use strict";

// Load, validate and expand qa/ui-states.json (#ui-coverage).

const fs = require("node:fs");
const path = require("node:path");
const { SAFETY_CLASSES, substitute } = require("./common");
const { RouteTable, DECLARABLE, BASE_READ_CLASSES, FORCE_PARAMS, loadManifest } = require("./routes");
const reachModule = require("./reach");

const INVENTORY_PATH = path.join(__dirname, "..", "ui-states.json");
const STEP_KINDS = new Set([
  "click", "clickIfPresent", "select", "selectIfVisible", "selectBay", "open", "openAll", "fill", "press", "wait",
  "waitFor", "waitText", "waitResponse", "check", "uncheck", "hover", "setRange", "forEachOption", "read",
  "reload", "expectValue", "acceptNextDialog", "download", "ledOff", "requireVisible",
]);
// Placeholders a write rule may use; the actuation runner substitutes them
// from the captured state before the window opens, so a window always names
// one object.
const RULE_PLACEHOLDERS = new Set(["slot", "system", "object_id"]);
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
    // #1120: a route that names its object in the body is bound to the captured one.
    for (const key of entry.object_keys || []) {
      const value = (rule.body_json || {})[key];
      if (typeof value !== "string" || !/^\{\w+\}$/.test(value)) errors.push(`${id}: ${field} ${rule.method} ${rule.path} must bind body ${key} to a captured placeholder such as {${key}}`);
    }
  }
}

// The exact manifest entry a read rule names (method + path template).
function exactEntry(manifest, service, method, routePath) {
  const all = [...(manifest.routes || []), ...(manifest.extra_routes || [])];
  return all.find((entry) => entry.service === service && entry.method === method && entry.path === routePath && !entry.path_expr) || null;
}

// #1120: read allowlists name exact read/read-post routes and their query keys.
function validateReads(errors, where, reads, service, manifest, table) {
  if (reads === undefined) return;
  if (!Array.isArray(reads)) {
    errors.push(`${where}: reads must be a list`);
    return;
  }
  for (const rule of reads) {
    const method = String(rule.method || "").toUpperCase();
    if (!method || !rule.path) {
      errors.push(`${where}: every read needs method and path`);
      continue;
    }
    if (rule.force) errors.push(`${where}: reads cannot force a refresh (${method} ${rule.path}); declare it in read_requests of an appliance_load state`);
    if (!Array.isArray(rule.query)) errors.push(`${where}: read ${method} ${rule.path} query must list the query keys it sends ([] for none)`);
    else if ((rule.query || []).some((key) => typeof key !== "string")) errors.push(`${where}: read ${method} ${rule.path} query keys must be strings`);
    if (!manifest) continue;
    const routeService = rule.service || service;
    const entry = exactEntry(manifest, routeService, method, rule.path);
    if (!entry) {
      const loose = table.match(routeService, method, rule.path);
      errors.push(loose
        ? `${where}: read ${method} ${rule.path} must name the exact manifest template (${loose.path})`
        : `${where}: read ${method} ${rule.path} matches no ${routeService} route in qa/ui-routes.json`);
      continue;
    }
    if (!BASE_READ_CLASSES.has(entry.class)) errors.push(`${where}: read ${method} ${rule.path} is a ${entry.class} route; reads may only name read or read-post routes`);
    // D-TNJ-20261109-CRAWLER-SWEEP: /api reads come from the generated reach
    // (qa/ui-read-reach.json) only; a hand copy is a second source of truth.
    if (String(rule.path).startsWith("/api/")) errors.push(`${where}: read ${method} ${rule.path} is generated from the page scripts (qa/ui-read-reach.json); remove the hand declaration`);
  }
}

// D-TNJ-20261109-CRAWLER-SWEEP: the generated reach must cover every page,
// name only exact read/read-post manifest routes of the page's service, and
// bind only the target keys the guard knows.
function validateReach(errors, inventory, reach, manifest) {
  if (!reach || !reach.pages) {
    errors.push("qa/ui-read-reach.json is missing or has no pages; run node scripts/generate_ui_read_reach.js");
    return;
  }
  for (const [pageId, page] of Object.entries(inventory.pages || {})) {
    const generated = reach.pages[pageId];
    if (!generated) {
      errors.push(`qa/ui-read-reach.json has no page ${pageId}; add it to qa/ui-crawl/reach.js PAGES and regenerate`);
      continue;
    }
    if ((generated.service || "ui") !== (page.service || "ui")) errors.push(`qa/ui-read-reach.json ${pageId}: service ${generated.service} is not the page's ${page.service || "ui"}`);
    for (const [scope, rules] of Object.entries(generated.scopes || {})) {
      for (const rule of rules || []) {
        const where = `qa/ui-read-reach.json ${pageId}/${scope}: ${rule.method} ${rule.path}`;
        const entry = manifest ? exactEntry(manifest, page.service || "ui", String(rule.method || "").toUpperCase(), rule.path) : null;
        if (manifest && !entry) {
          errors.push(`${where} is not an exact ${page.service || "ui"} manifest route`);
          continue;
        }
        if (entry && !BASE_READ_CLASSES.has(entry.class)) errors.push(`${where} is a ${entry.class} route; the generated reach may only hold read or read-post routes`);
        if (entry && rule.class !== entry.class) errors.push(`${where}: class ${rule.class} is not the manifest's ${entry.class}`);
        if (!Array.isArray(rule.query)) errors.push(`${where}: needs its query keys`);
        if (rule.force) errors.push(`${where}: a generated read cannot force a refresh`);
        for (const key of [...(rule.bind || []), ...(rule.bind_if_sent || [])]) {
          if (!["system_id", "enclosure_id", "storage_view_id"].includes(key) || !(rule.query || []).includes(key)) errors.push(`${where}: cannot bind ${key}`);
        }
      }
    }
  }
}

// Page-load requests (Sol #1108): what the page itself sends while it loads on
// a given platform, before any state window can open. Forced appliance reads
// only, each budgeted and scoped to named platforms.
function validateLoadRequests(errors, pageId, page, table) {
  for (const rule of page.load_requests || []) {
    const where = `pages.${pageId}.load_requests ${rule.method} ${rule.path}`;
    if (!Array.isArray(rule.platforms) || !rule.platforms.length) errors.push(`${where}: load_requests need platforms (the platforms whose page sends it)`);
    if (!Number.isInteger(rule.max) || rule.max < 1) errors.push(`${where}: load_requests need a positive integer max`);
    if (!rule.why) errors.push(`${where}: load_requests need a why`);
    if (!table) continue;
    const probe = new URL(`http://rule.invalid${rule.path}${rule.force ? "?force=true" : ""}`);
    const { entry, cls } = table.classify(page.service || "ui", String(rule.method || "GET").toUpperCase(), probe);
    const resolved = entry && BASE_READ_CLASSES.has(cls) && rule.force ? "appliance-load" : cls;
    if (!entry || resolved !== "appliance-load") errors.push(`${where}: load_requests may only declare appliance-load reads`);
  }
}

// D-TNJ-20261109-CRAWLER-SWEEP: the selection's reads are generated now
// (page reach, bound to the target); `selection` only names the controls
// whose press selects a bay, for tests that crawl every selecting state.
function validateSelection(errors, pageId, page) {
  const selection = page.selection;
  if (selection === undefined) return;
  const where = `pages.${pageId}.selection`;
  if (!selection.why) errors.push(`${where} needs a why`);
  if (!Array.isArray(selection.controls) || !selection.controls.length || selection.controls.some((c) => typeof c !== "string" || !c)) {
    errors.push(`${where} needs controls (the selectors whose press selects a bay)`);
  }
  if (selection.reads !== undefined) errors.push(`${where}.reads: selection reads are generated from the page scripts (qa/ui-read-reach.json); remove them`);
}

// Does the state select a bay on its page (Codex #1108)? Derived from every
// step list: a selectBay step, or pressing/filling a selection control.
function selectsBay(inventory, state) {
  const selection = (((inventory || {}).pages || {})[state.page] || {}).selection;
  if (!selection) return false;
  const controls = selection.controls || [];
  const steps = ["capture", "steps", "revert", "verify"].flatMap((field) => state[field] || []);
  return steps.some((step) => step && (step.selectBay !== undefined
    || ["click", "clickIfPresent", "fill"].some((kind) => typeof step[kind] === "string" && controls.some((control) => step[kind].includes(control)))));
}

// #1128: the longest follow-up window a state may declare. Short on purpose:
// a delayed follow-up after it is a hard failure, not a reason to wait longer.
const MAX_FOLLOW_UP_MS = 60_000;

function validateFollowUp(errors, id, state) {
  if (state.follow_up_ms === undefined) return;
  const ms = state.follow_up_ms;
  if (!Number.isInteger(ms) || ms < 1 || ms > MAX_FOLLOW_UP_MS) errors.push(`${id}: follow_up_ms must be an integer from 1 to ${MAX_FOLLOW_UP_MS}`);
  const forced = (state.read_requests || []).some((rule) => rule.force === true);
  if (state.safety !== "read" || !state.appliance_load || !forced) {
    errors.push(`${id}: follow_up_ms is only for appliance_load read states whose read_requests declare forced refreshes (the follow-ups they cause)`);
  }
}

function validateInventory(inventory, { manifest = undefined, reach = undefined } = {}) {
  const errors = [];
  const ids = new Set();
  const pages = inventory.pages || {};
  const resolvedManifest = manifest === undefined ? loadManifest() : manifest;
  const resolvedReach = reach === undefined ? reachModule.loadReach() : reach;
  const table = resolvedManifest ? new RouteTable(resolvedManifest) : null;
  if (resolvedReach !== null) validateReach(errors, inventory, resolvedReach, resolvedManifest);
  for (const [pageId, page] of Object.entries(pages)) {
    validateReads(errors, `pages.${pageId}`, page.reads, page.service || "ui", resolvedManifest, table);
    validateLoadRequests(errors, pageId, page, table);
    validateSelection(errors, pageId, page);
  }
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
    validateReads(errors, id, state.reads, (pages[state.page] || {}).service || "ui", resolvedManifest, table);
    // read-heavy keeps its per-state declaration and budget; it must also be
    // a request the page's own scripts can send.
    if (resolvedReach && resolvedReach.pages && table) {
      const generated = resolvedReach.pages[state.page] || {};
      for (const rule of state.read_requests || []) {
        const probe = new URL(`http://rule.invalid${String(rule.path).replace(/\{slot\}/g, "1")}`);
        const { entry } = table.classify((pages[state.page] || {}).service || "ui", String(rule.method || "GET").toUpperCase(), probe);
        if (!entry || entry.class !== "read-heavy") continue;
        const reachable = (generated.declared_per_state || []).some((item) => item.method === entry.method && item.path === entry.path);
        if (!reachable) errors.push(`${id}: read_requests ${rule.method} ${rule.path} is read-heavy but no script of page ${state.page} sends it (qa/ui-read-reach.json)`);
      }
    }
    validateFollowUp(errors, id, state);
    // #1143: `requires_enabled` makes a present-but-disabled `requires`
    // control not-applicable before any step runs. A confirm-preview state
    // that presses its required control must declare it, so a target that
    // renders the control disabled is not reported as a failed preview.
    if ("requires_enabled" in state && state.requires_enabled !== true) errors.push(`${id}: requires_enabled must be true or absent`);
    if (state.requires_enabled === true && !state.requires) errors.push(`${id}: requires_enabled needs requires (the control that must be enabled)`);
    if (state.confirm_preview && state.requires && state.requires_enabled !== true
      && (state.steps || []).some((step) => step && step.click === state.requires)) {
      errors.push(`${id}: a confirm_preview state that clicks its required control ${state.requires} needs requires_enabled: true (a disabled control is not-applicable, #1143)`);
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
  // A page's own declared load-time request (pages.<page>.load_requests), admitted by the page-load window.
  "page-load": "read",
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

// #1120: `observed` keys are "cls METHOD template"; `reads` is the state's
// read allowlist (readsFor). A read outside it is a misclassified control even
// when its class rank is fine.
function checkObserved(state, observed, reads = null) {
  const problems = [];
  const declared = SAFETY_RANK[state.safety] ?? 0;
  const listed = reads ? new Set(reads.map((rule) => `${String(rule.method).toUpperCase()} ${rule.path}`)) : null;
  for (const key of observed) {
    const cls = String(key).split(" ")[0];
    const safety = CLASS_SAFETY[cls] || "forbidden";
    if (SAFETY_RANK[safety] > declared) problems.push(`${state.id} is ${state.safety} but called ${key}`);
    if (cls === "appliance-load" && state.safety === "read" && !state.appliance_load) problems.push(`${state.id} is not appliance_load but called ${key}`);
    if (listed && BASE_READ_CLASSES.has(cls) && !listed.has(String(key).split(" ").slice(1).join(" "))) {
      problems.push(`${state.id} read ${String(key).split(" ").slice(1).join(" ")} outside its read allowlist`);
    }
  }
  return problems;
}

// The exact read allowlist of a state on one scope kind of its page: its
// page's own documents and static assets (hand-declared), the GENERATED
// reach of that page and scope kind (qa/ui-read-reach.json, bound to the
// target; D-TNJ-20261109-CRAWLER-SWEEP), then any non-/api read the state
// declares itself. `scope` defaults to the page's first scope kind (an
// enclosure on the main page); "*" is the union over every scope kind, for
// checking what a state observed across several targets.
function readsFor(inventory, state, scope = null, reach = reachModule.loadReach()) {
  const page = (inventory.pages || {})[state.page] || {};
  const generated = !reach || !(reach.pages || {})[state.page] ? []
    : scope === "*" ? reachModule.reachRulesAnyScope(reach, state.page) : reachModule.reachRules(reach, state.page, scope);
  return [...(page.reads || []), ...generated, ...(state.reads || [])];
}

// The page-load requests of a page on one platform (Sol #1108).
function loadRequestsFor(inventory, pageId, platform) {
  const page = (inventory.pages || {})[pageId] || {};
  const name = String(platform || "").toLowerCase();
  return (page.load_requests || []).filter((rule) => (rule.platforms || []).map((p) => String(p).toLowerCase()).includes(name))
    .map(({ platforms, why, ...rule }) => rule);
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

module.exports = {
  INVENTORY_PATH, loadInventory, validateInventory, expandStates, selectStates, substitute, summarize, stepKind, deriveSafety, checkObserved,
  readsFor, loadRequestsFor, selectsBay, FORCE_PARAMS, MAX_FOLLOW_UP_MS,
};
