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
//   3. Query booleans are read the way FastAPI/Pydantic reads them (1, on, t,
//      true, y, yes in any case are true; FastAPI takes the last of repeated
//      keys). Any `force`/`refresh`/`fresh` value that is not a clean false,
//      or a route's own `query_classes`, raises a read to `appliance-load`.
//   4. `read` and `read-post` pass only when the running state's read
//      allowlist names the exact manifest route and every query key the
//      request carries, and the request is bound to the target. The list is
//      the page's own documents (qa/ui-states.json) plus the GENERATED reach
//      of the page and the target's scope kind (qa/ui-read-reach.json: every
//      /api read the page's scripts can send; D-TNJ-20261109-CRAWLER-SWEEP).
//      Bound: each `bind` key the target has must be carried with the
//      target's value (every copy), each `bind_if_sent` key it carries must
//      equal the target's, and each `bind_params` path parameter (a storage
//      view's id) must equal the target's.
//   5. Any other class passes only inside a declared window, and only when a
//      rule matches exactly: method, the substituted path (the captured bay),
//      the route class, body predicates (a route with `object_keys` must have
//      them bound to the captured object), the query keys, a per-rule budget,
//      and the bound scope (`system_id` and `enclosure_id` must equal the
//      target's). Windows are the state's own (writes, declared reads), the
//      page-load window (a platform's startup refresh, opened only around a
//      navigation; Sol #1108) and a state's declared follow-up window (#1128):
//      after a read state with a declared forced refresh ends, only the
//      forced reads it declared, with the budget they have left, stay
//      admitted for its `follow_up_ms` (monotonic clock), and only until the
//      next navigation. A follow-up after that is refused like any other.
//      A request whose page-load rule has spent its budget is not refused by
//      that alone: the state's own window and the follow-up window are tried
//      next, each with its own budget and binding (Codex #1108).
//   6. Everything else aborts and is recorded.
//
// The guard never sees credentials: it gets method, URL and the request body.

const { RouteTable, BASE_READ_CLASSES, templateRegExp, loadManifest, parseQueryBool, forcesRefresh, FORCE_PARAMS } = require("./routes");

const BOUND_CLASSES = new Set(["app-write", "led", "appliance-load"]);
const BIND_KEYS = ["system_id", "enclosure_id"];
// Target keys a read rule may bind: the window keys plus the storage view.
const READ_BIND_KEYS = [...BIND_KEYS, "storage_view_id"];

// Path parameter values of a manifest template, by name.
function pathParams(template, pathname) {
  const names = [];
  const pattern = String(template).replace(/[.+?^$()|[\]\\]/g, "\\$&")
    .replace(/\{([A-Za-z_]\w*)(:path)?\}/g, (all, name, isPath) => {
      names.push(name);
      return isPath ? "\u0001" : "\u0000";
    })
    .replace(/\u0000/g, "([^/]+)").replace(/\u0001/g, "(.+)");
  const hit = new RegExp(`^${pattern}$`).exec(pathname);
  if (!hit) return null;
  return Object.fromEntries(names.map((name, index) => {
    let value = hit[index + 1];
    try {
      value = decodeURIComponent(value);
    } catch {
      // keep the raw segment; it cannot equal a decoded target id by accident
    }
    return [name, value];
  }));
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

function queryKeys(url) {
  return [...new Set(url.searchParams.keys())];
}

// Query keys a window rule admits: the bound scope, the force key of a
// declared forced read, and whatever the rule names in `query`.
function windowQueryKeys(rule) {
  return new Set([...BIND_KEYS, ...(rule.force ? FORCE_PARAMS : []), ...(rule.query || [])]);
}

function matchesRule(rule, method, url, body) {
  if (String(rule.method || "GET").toUpperCase() !== method) return false;
  if (!rule.re.test(url.pathname)) return false;
  if (rule.force === true && !forcesRefresh(url)) return false;
  const allowedKeys = windowQueryKeys(rule);
  if (queryKeys(url).some((key) => !allowedKeys.has(key))) return false;
  const text = typeof body === "string" ? body : "";
  if (rule.body_includes && !text.includes(rule.body_includes)) return false;
  if (rule.body_excludes && text.replace(/\s+/g, "").includes(rule.body_excludes)) return false;
  if (rule.body_json && !jsonIncludes(bodyJson(text), rule.body_json)) return false;
  return true;
}

function unbound(value) {
  return /[{}*]/.test(String(value));
}

function compileRule(rule) {
  if (unbound(rule.path) || (rule.body_json && unbound(JSON.stringify(Object.values(rule.body_json))))) {
    throw new Error(`write rule ${rule.method} ${rule.path} is not bound to one object (unsubstituted placeholder or glob)`);
  }
  return { ...rule, re: templateRegExp(rule.path) };
}

class RequestGuard {
  // origins: { service: origin } (or an array, then every origin is "ui").
  // now: a monotonic millisecond clock (follow-up windows); injectable for tests.
  constructor({ origins, manifest = null, routes = null, now = () => performance.now() }) {
    // Fixture variant origins ("ui-write-sign-in") route like their base service.
    const entries = (Array.isArray(origins) ? origins.map((origin) => ["ui", origin]) : Object.entries(origins || {}))
      .map(([service, origin]) => [String(service).replace(/-.*$/, ""), origin]);
    const usable = entries.filter(([, origin]) => origin);
    if (usable.length === 0) throw new Error("RequestGuard needs at least one origin");
    this.services = new Map(usable.map(([service, origin]) => [new URL(origin).origin, service]));
    this.routes = routes || new RouteTable(manifest || loadManifest());
    this.now = now;
    this.window = null;
    this.load = null;
    this.followUp = null;
    this.reads = null;
    this.state = null;
    this.observed = new Map();
    this.log = [];
  }

  // The state now running, with its read allowlist: [{ method, path (an exact
  // manifest template), query: [keys], bind?: [scope keys] }] and the target's
  // scope `bind` ({ system_id, enclosure_id }). Reads outside it are refused;
  // a read rule with `bind` (a page's selection read, Codex #1108) also needs
  // each named key to equal the target's, when the target has one.
  beginState(stateId, { reads = [], bind = {} } = {}) {
    this.state = stateId;
    this.reads = { stateId, rules: this.compileReads(stateId, reads), bind: { ...(bind || {}) } };
  }

  // The finished state's read list stays until the next state begins, so a
  // timer that fires before the next navigation is judged by the page that
  // still owns the document.
  endState() {
    this.state = null;
  }

  compileReads(stateId, reads) {
    return (reads || []).map((rule) => {
      const method = String(rule.method || "GET").toUpperCase();
      if (!Array.isArray(rule.query)) throw new Error(`${stateId}: read ${method} ${rule.path} needs its query keys`);
      if (rule.force) throw new Error(`${stateId}: reads cannot force a refresh (${method} ${rule.path})`);
      const bind = rule.bind || [];
      const ifSent = rule.bind_if_sent || [];
      const unknown = [...bind, ...ifSent].filter((key) => !READ_BIND_KEYS.includes(key) || !rule.query.includes(key));
      if (unknown.length) throw new Error(`${stateId}: read ${method} ${rule.path} cannot bind ${unknown.join(", ")}`);
      const params = rule.bind_params || {};
      const templateParams = [...String(rule.path).matchAll(/\{(\w+)(?::\w+)?\}/g)].map((m) => m[1]);
      const badParams = Object.entries(params).filter(([name, key]) => !templateParams.includes(name) || !READ_BIND_KEYS.includes(key));
      if (badParams.length) throw new Error(`${stateId}: read ${method} ${rule.path} cannot bind path ${badParams.map(([name]) => name).join(", ")}`);
      return { key: `${method} ${rule.path}`, path: rule.path, query: new Set(rule.query), bind, ifSent, params };
    });
  }

  compileWindow(stateId, rules, { service = "ui", classes = null } = {}) {
    return (rules || []).map((raw) => {
      const rule = compileRule(raw);
      const method = String(rule.method || "GET").toUpperCase();
      const probe = new URL(`http://rule.invalid${rule.path}${rule.force ? "?force=true" : ""}`);
      const { entry, cls } = this.routes.classify(service, method, probe);
      if (!entry) throw new Error(`${stateId}: rule ${rule.method} ${rule.path} matches no ${service} route in the manifest`);
      const resolved = BASE_READ_CLASSES.has(cls) && forcesRefresh(probe) ? "appliance-load" : cls;
      if (classes && !classes.includes(resolved)) throw new Error(`${stateId}: rule ${rule.method} ${rule.path} is ${resolved}, not allowed here (${classes.join(", ")})`);
      for (const key of entry.object_keys || []) {
        const value = (rule.body_json || {})[key];
        if (typeof value !== "string" || !value) throw new Error(`${stateId}: rule ${rule.method} ${rule.path} must bind body ${key} to the captured object`);
      }
      return { rule: { ...rule, cls: resolved }, used: 0, max: Number.isInteger(raw.max) ? raw.max : 1 };
    });
  }

  // Admit the declared rules of one state. Every rule must name one exact
  // manifest route of `service` whose class is in `classes` (when given);
  // `bind` = { system_id, enclosure_id } scopes app-write/led/appliance-load.
  openWrites(stateId, writes, readRequests = [], { bind = {}, service = "ui", classes = null } = {}) {
    if (this.window) throw new Error(`write window already open for ${this.window.stateId}`);
    const rules = [...this.compileWindow(stateId, writes, { service, classes }), ...this.compileWindow(stateId, readRequests, { service, classes })];
    this.window = { stateId, rules, admitted: [], bind: { ...bind } };
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

  // The page-load window (Sol #1108): requests the page itself sends while it
  // loads, declared per page and platform in qa/ui-states.json
  // `load_requests`. Only appliance-load reads, budgeted, bound to the target.
  openLoad(stateId, rules, { bind = {}, service = "ui" } = {}) {
    if (this.load) throw new Error(`load window already open for ${this.load.stateId}`);
    this.load = { stateId, rules: this.compileWindow(stateId, rules, { service, classes: ["appliance-load"] }), admitted: [], bind: { ...bind } };
  }

  closeLoad() {
    const closed = this.load;
    this.load = null;
    return closed ? closed.admitted : [];
  }

  // #1128: the declared follow-up window of a finished read state. Only
  // appliance-load reads (the declared forced refreshes it has budget left
  // for), bound to the target, until `ms` from now on the monotonic clock or
  // until closeFollowUp() (the crawler calls it when the next navigation
  // starts), whichever is first.
  openFollowUp(stateId, rules, { bind = {}, service = "ui", ms } = {}) {
    if (!Number.isFinite(ms) || ms <= 0) throw new Error(`${stateId}: a follow-up window needs a positive duration`);
    this.closeFollowUp();
    const opened = this.now();
    this.followUp = { stateId, rules: this.compileWindow(stateId, rules, { service, classes: ["appliance-load"] }), admitted: [], bind: { ...bind }, ms, until: opened + ms };
  }

  closeFollowUp() {
    const closed = this.followUp;
    this.followUp = null;
    return closed ? closed.admitted.map((entry) => ({ state: closed.stateId, ...entry })) : [];
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
    const info = { cls, route, service };
    // Codex #1108: a page-load rule whose budget is spent does not refuse the
    // request by itself; the running state's declared requests and the
    // follow-up window still get their say (each with its own budget and
    // binding), and the request is refused only if none admits it.
    let loadSpent = null;
    if (cls === "appliance-load" && this.load) {
      const slot = this.matchWindow(this.load, verb, parsed, body, cls);
      if (slot && !slot.error) {
        // Observed as the page's own load request, not as a state's poll.
        this.observe("page-load", route, parsed);
        return this.record(verb, url, true, `declared page-load ${cls} for ${this.load.stateId}`, slot, info, this.load);
      }
      if (slot && slot.exhausted) {
        loadSpent = slot.error;
      } else if (slot && slot.error) {
        this.observe(cls, route, parsed);
        return this.record(verb, url, false, slot.error, null, info);
      }
    }
    // #1128: a finished state's declared follow-up (observed as that state's).
    if (cls === "appliance-load" && this.followUp && !this.window) {
      const followUp = this.followUp;
      const declared = followUp.rules.some((entry) => entry.rule.cls === cls && matchesRule(entry.rule, verb, parsed, body));
      if (declared) {
        this.observe(cls, route, parsed, followUp.stateId);
        if (this.now() > followUp.until) {
          return this.record(verb, url, false, `follow-up ${route} after the ${followUp.ms} ms follow-up window of ${followUp.stateId}`, null, info);
        }
        const late = this.matchWindow(followUp, verb, parsed, body, cls);
        if (late && late.error) return this.record(verb, url, false, `${late.error} (follow-up window of ${followUp.stateId})`, null, info);
        return this.record(verb, url, true, `declared follow-up ${cls} for ${followUp.stateId}`, late, info, followUp);
      }
    }
    this.observe(cls, route, parsed);
    if (BASE_READ_CLASSES.has(cls)) {
      const problem = this.matchRead(route, parsed);
      return problem ? this.record(verb, url, false, problem, null, info) : this.record(verb, url, true, cls, null, info);
    }
    const slot = this.matchWindow(this.window, verb, parsed, body, cls);
    if (slot && slot.error) return this.record(verb, url, false, slot.error, null, info);
    if (slot) return this.record(verb, url, true, `declared ${cls} for ${this.window.stateId}`, slot, info, this.window);
    if (loadSpent) return this.record(verb, url, false, `${loadSpent}; no state or follow-up window admits it`, null, info);
    const where = this.window ? `undeclared ${cls} route during ${this.window.stateId}` : `${cls} route outside a declaring state`;
    return this.record(verb, url, false, where, null, info);
  }

  // null when the running state's read allowlist admits this exact read.
  matchRead(route, parsed) {
    if (!this.reads) return `read ${route} outside any state`;
    const rules = this.reads.rules.filter((rule) => rule.key === route);
    if (!rules.length) return `read ${route} is not in the read allowlist of ${this.reads.stateId}`;
    const keys = queryKeys(parsed);
    const fitting = rules.filter((rule) => keys.every((key) => rule.query.has(key)));
    if (!fitting.length) {
      const extra = keys.filter((key) => !rules.some((rule) => rule.query.has(key)));
      return `read ${route} with query key ${extra.join(", ") || keys.join(", ")} is not declared for ${this.reads.stateId}`;
    }
    let outside = null;
    for (const rule of fitting) {
      outside = this.outsideBind(rule.bind, this.reads.bind, parsed)
        || this.outsideBindIfSent(rule.ifSent, this.reads.bind, parsed)
        || this.outsidePathBind(rule, this.reads.bind, parsed);
      if (!outside) return null;
    }
    return `read ${route} outside the bound ${outside} of ${this.reads.stateId}`;
  }

  // null when every copy of each key the request carries equals the target's.
  outsideBindIfSent(keys, bind, parsed) {
    for (const key of keys || []) {
      const want = (bind || {})[key];
      if (want === undefined || want === null) continue;
      const got = parsed.searchParams.getAll(key);
      if (got.some((value) => value !== String(want))) return `${key} (other)`;
    }
    return null;
  }

  // null when each bound path parameter equals the target's value.
  outsidePathBind(rule, bind, parsed) {
    const entries = Object.entries(rule.params || {});
    if (!entries.length) return null;
    const values = pathParams(rule.path, parsed.pathname);
    if (!values) return `path (no match for ${rule.path})`;
    for (const [name, key] of entries) {
      const want = (bind || {})[key];
      if (want === undefined || want === null) continue;
      if (values[name] !== String(want)) return `${key} (other ${name} in the path)`;
    }
    return null;
  }

  // null when every bound key the target has is carried, every copy equal.
  outsideBind(keys, bind, parsed) {
    for (const key of keys || []) {
      const want = (bind || {})[key];
      if (want === undefined || want === null) continue;
      const got = parsed.searchParams.getAll(key);
      if (!got.length || got.some((value) => value !== String(want))) return `${key} (${got.length ? "other" : "missing"})`;
    }
    return null;
  }

  matchWindow(window, verb, parsed, body, cls) {
    if (!window) return null;
    const candidates = window.rules.filter((entry) => entry.rule.cls === cls && matchesRule(entry.rule, verb, parsed, body));
    if (!candidates.length) return null;
    if (BOUND_CLASSES.has(cls)) {
      for (const key of BIND_KEYS) {
        const want = window.bind[key];
        if (want === undefined || want === null) continue;
        // FastAPI reads the last of repeated keys; every copy must match.
        const got = parsed.searchParams.getAll(key);
        if (!got.length || got.some((value) => value !== String(want))) {
          return { error: `${cls} request outside the bound ${key} (${got.length ? "other" : "missing"})` };
        }
      }
    }
    const slot = candidates.find((entry) => entry.used < entry.max);
    if (!slot) return { error: `budget exhausted for ${verb} ${parsed.pathname} in ${window.stateId}`, exhausted: true };
    slot.used += 1;
    return slot;
  }

  observe(cls, route, parsed, stateKey = null) {
    const key = stateKey || this.state || "(between states)";
    if (!this.observed.has(key)) this.observed.set(key, new Map());
    const seen = this.observed.get(key);
    seen.set(`${cls} ${route}`, (seen.get(`${cls} ${route}`) || 0) + 1);
    if (parsed && BASE_READ_CLASSES.has(cls)) {
      if (!this.observedQueries) this.observedQueries = new Map();
      if (!this.observedQueries.has(key)) this.observedQueries.set(key, new Map());
      const routes = this.observedQueries.get(key);
      if (!routes.has(route)) routes.set(route, new Set());
      for (const name of queryKeys(parsed)) routes.get(route).add(name);
    }
  }

  record(method, url, allowed, reason, write = null, info = {}, window = null) {
    const path = (() => {
      try {
        const parsed = new URL(url);
        return `${parsed.pathname}${parsed.search}`;
      } catch {
        return String(url).slice(0, 120);
      }
    })();
    const entry = { method, path, allowed, reason, ...info, state: this.state || null };
    if (write && window) window.admitted.push({ method, path, rule: write.rule.path, cls: info.cls });
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

module.exports = { RequestGuard, WRITE_CLASSES, forcesRefresh, parseQueryBool, matchesRule, compileRule, jsonIncludes, pathParams, FORCE_PARAMS, READ_BIND_KEYS };
