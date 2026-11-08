"use strict";

// Request admission for the UI coverage crawler (#ui-coverage, #1120).
//
// Every browser request passes through `admit()` before it leaves the
// browser. Default deny, exact routes:
//
//   1. Only the target's own origins are reachable; each origin is a service
//      (ui, history, admin, offline, assets). Everything else aborts.
//   2. The request must match an exact method + path template of that service
//      in qa/ui-routes.json (the reviewed route manifest). An unknown route
//      aborts and is a safety-gate failure.
//   3. The route's class decides: `read` and `read-post` pass for every state.
//      A truthy `force`/`refresh`/`fresh` query (or a route's own
//      `query_classes`) raises a read to `appliance-load`.
//   4. Any other class passes only while a state's window is open and one of
//      its declared rules matches exactly: method, the substituted path (the
//      captured bay), the route class, body predicates, a per-rule budget, and
//      the bound scope (`system_id` and `enclosure_id` query parameters must
//      equal the write system and target enclosure).
//   5. Everything else aborts and is recorded.
//
// The guard never sees credentials: it gets method, URL and the request body.

const { RouteTable, BASE_READ_CLASSES, templateRegExp, loadManifest } = require("./routes");

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);
const FORCE_PARAMS = ["force", "refresh", "fresh"];
const BOUND_CLASSES = new Set(["app-write", "led", "appliance-load"]);

function forcesRefresh(url) {
  for (const name of FORCE_PARAMS) {
    const value = url.searchParams.get(name);
    if (value !== null && TRUE_VALUES.has(value.toLowerCase())) return true;
  }
  return false;
}

function bodyJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

// Every key of `want` must equal the parsed body's value (deep, exact).
function jsonIncludes(body, want) {
  if (!body || typeof body !== "object") return false;
  return Object.entries(want).every(([key, value]) => JSON.stringify(body[key]) === JSON.stringify(value));
}

function matchesRule(rule, method, url, body) {
  if (String(rule.method || "GET").toUpperCase() !== method) return false;
  if (!rule.re.test(url.pathname)) return false;
  if (rule.force === true && !forcesRefresh(url)) return false;
  const text = typeof body === "string" ? body : "";
  if (rule.body_includes && !text.includes(rule.body_includes)) return false;
  if (rule.body_excludes && text.replace(/\s+/g, "").includes(rule.body_excludes)) return false;
  if (rule.body_json && !jsonIncludes(bodyJson(text), rule.body_json)) return false;
  return true;
}

function compileRule(rule) {
  if (/[{}*]/.test(String(rule.path))) {
    throw new Error(`write rule ${rule.method} ${rule.path} is not bound to one object (unsubstituted placeholder or glob)`);
  }
  return { ...rule, re: templateRegExp(rule.path) };
}

class RequestGuard {
  // origins: { service: origin } (or an array, then every origin is "ui").
  constructor({ origins, manifest = null, routes = null }) {
    // Fixture variant origins ("ui-write-sign-in") route like their base service.
    const entries = (Array.isArray(origins) ? origins.map((origin) => ["ui", origin]) : Object.entries(origins || {}))
      .map(([service, origin]) => [String(service).replace(/-.*$/, ""), origin]);
    const usable = entries.filter(([, origin]) => origin);
    if (usable.length === 0) throw new Error("RequestGuard needs at least one origin");
    this.services = new Map(usable.map(([service, origin]) => [new URL(origin).origin, service]));
    this.routes = routes || new RouteTable(manifest || loadManifest());
    this.window = null;
    this.state = null;
    this.observed = new Map();
    this.log = [];
  }

  // The state now running (for per-state observation of route classes).
  beginState(stateId) {
    this.state = stateId;
  }

  endState() {
    this.state = null;
  }

  // Admit the declared rules of one state. Every rule must name one exact
  // manifest route of `service` whose class is in `classes` (when given);
  // `bind` = { system_id, enclosure_id } scopes app-write/led/appliance-load.
  openWrites(stateId, writes, readRequests = [], { bind = {}, service = "ui", classes = null } = {}) {
    if (this.window) throw new Error(`write window already open for ${this.window.stateId}`);
    const compile = (list) => (list || []).map((raw) => {
      const rule = compileRule(raw);
      const probe = new URL(`http://rule.invalid${rule.path}${rule.force ? "?force=true" : ""}`);
      const { entry, cls } = this.routes.classify(service, String(rule.method || "GET").toUpperCase(), probe);
      if (!entry) throw new Error(`${stateId}: rule ${rule.method} ${rule.path} matches no ${service} route in the manifest`);
      const resolved = BASE_READ_CLASSES.has(cls) && forcesRefresh(probe) ? "appliance-load" : cls;
      if (classes && !classes.includes(resolved)) throw new Error(`${stateId}: rule ${rule.method} ${rule.path} is ${resolved}, not allowed here (${classes.join(", ")})`);
      return { rule: { ...rule, cls: resolved }, used: 0, max: Number.isInteger(raw.max) ? raw.max : 1 };
    });
    this.window = { stateId, rules: [...compile(writes), ...compile(readRequests)], admitted: [], bind: { ...bind } };
  }

  closeWrites() {
    const closed = this.window;
    this.window = null;
    return closed ? closed.admitted : [];
  }

  // Declared read requests (for example the export download) for a read state.
  openReads(stateId, readRequests, options = {}) {
    this.openWrites(stateId, [], readRequests, options);
  }

  serviceFor(url) {
    return this.services.get(url.origin) || null;
  }

  admit({ method, url, body = null }) {
    const verb = String(method || "GET").toUpperCase();
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      return this.record(verb, url, false, "unparseable URL");
    }
    if (parsed.protocol === "data:" || parsed.protocol === "blob:") return this.record(verb, url, true, "inline");
    const service = this.serviceFor(parsed);
    if (!service) return this.record(verb, url, false, "foreign origin");
    const { entry, cls: routeClass } = this.routes.classify(service, verb, parsed);
    if (!entry) return this.record(verb, url, false, `unknown route ${service} ${verb} ${parsed.pathname}`);
    const cls = BASE_READ_CLASSES.has(routeClass) && forcesRefresh(parsed) ? "appliance-load" : routeClass;
    const route = `${entry.method} ${entry.path}`;
    this.observe(cls, route);
    if (BASE_READ_CLASSES.has(cls)) return this.record(verb, url, true, cls, null, { cls, route, service });
    const slot = this.matchWindow(verb, parsed, body, cls);
    if (slot && slot.error) return this.record(verb, url, false, slot.error, null, { cls, route, service });
    if (slot) return this.record(verb, url, true, `declared ${cls} for ${this.window.stateId}`, slot, { cls, route, service });
    const where = this.window ? `undeclared ${cls} route during ${this.window.stateId}` : `${cls} route outside a declaring state`;
    return this.record(verb, url, false, where, null, { cls, route, service });
  }

  matchWindow(verb, parsed, body, cls) {
    if (!this.window) return null;
    const candidates = this.window.rules.filter((entry) => entry.rule.cls === cls && matchesRule(entry.rule, verb, parsed, body));
    if (!candidates.length) return null;
    if (BOUND_CLASSES.has(cls)) {
      for (const key of ["system_id", "enclosure_id"]) {
        const want = this.window.bind[key];
        if (want === undefined || want === null) continue;
        const got = parsed.searchParams.get(key);
        if (got !== String(want)) return { error: `${cls} request outside the bound ${key} (${got === null ? "missing" : "other"})` };
      }
    }
    const slot = candidates.find((entry) => entry.used < entry.max);
    if (!slot) return { error: `budget exhausted for ${verb} ${parsed.pathname} in ${this.window.stateId}` };
    slot.used += 1;
    return slot;
  }

  observe(cls, route) {
    const key = this.state || "(between states)";
    if (!this.observed.has(key)) this.observed.set(key, new Map());
    const seen = this.observed.get(key);
    seen.set(`${cls} ${route}`, (seen.get(`${cls} ${route}`) || 0) + 1);
  }

  record(method, url, allowed, reason, write = null, info = {}) {
    const path = (() => {
      try {
        const parsed = new URL(url);
        return `${parsed.pathname}${parsed.search}`;
      } catch {
        return String(url).slice(0, 120);
      }
    })();
    const entry = { method, path, allowed, reason, ...info, state: this.state || null };
    if (write && this.window) this.window.admitted.push({ method, path, rule: write.rule.path, cls: info.cls });
    if (!allowed || write) this.log.push(entry);
    return entry;
  }

  blocked() {
    return this.log.filter((entry) => !entry.allowed);
  }
}

// Classes that change something; every admitted one must belong to an
// actuation. (Admitted appliance polls are counted separately.)
const WRITE_CLASSES = new Set(["app-write", "led", "forbidden"]);

module.exports = { RequestGuard, WRITE_CLASSES, forcesRefresh, matchesRule, compileRule, jsonIncludes };
