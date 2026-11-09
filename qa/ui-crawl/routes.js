"use strict";

// Backend route manifest for the UI coverage crawler (#1120).
//
// The crawler's safety classes are not trusted as hand-written labels. Every
// HTTP route the backends declare is read from the Python sources (FastAPI
// decorators), fingerprinted, and must appear in qa/ui-routes.json with a
// reviewed class. The request guard then admits a browser request only when it
// matches one of these exact method + path templates, and only for the classes
// the running state may use. CI fails when:
//
//   - a route is added, removed, or its handler source changes without the
//     manifest being updated (the fingerprint is the reviewed handler text),
//   - a route path is not a string literal (it cannot be reviewed statically),
//   - a browser script calls an /api/ path that no manifest route matches.
//
// Classes, from harmless to never:
//   read            GET that reads app state or app-cached appliance data
//   read-post       POST that only reads (batch reads, estimates, previews)
//   read-heavy      read that builds a large artefact (export); declared per state
//   appliance-load  explicit appliance poll or forced refresh; declared per state
//   app-write       app-local store write (journaled when config backups are on)
//   led             identify/locate light
//   forbidden       appliance/array change, accounts, backups, runtime, history
//                   maintenance, config saves; never admitted
//   internal        not reachable from a browser page (sidecar-internal, metrics)

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const MANIFEST_PATH = path.join(__dirname, "..", "ui-routes.json");
const ROUTE_CLASSES = ["read", "read-post", "read-heavy", "appliance-load", "app-write", "led", "forbidden", "internal"];
// Plain read classes: admitted without a per-state declaration, but only from
// the generated page reach (qa/ui-read-reach.json, qa/ui-crawl/reach.js) and
// bound to the target.
const BASE_READ_CLASSES = new Set(["read", "read-post"]);
// What each inventory safety class may declare in `writes` / `read_requests`.
const DECLARABLE = {
  read: new Set(["read-heavy"]),
  "app-write-revert": new Set(["app-write", "appliance-load"]),
  led: new Set(["led"]),
  forbidden: new Set(),
};

// Query booleans exactly as FastAPI/Pydantic 2 read them (bool query fields):
// case-insensitive; anything else is a 422. FastAPI reads the last of
// repeated keys. The guard treats every value that is not a clean false as
// true, so a spelling the backend accepts can never pass as a plain read.
const QUERY_TRUE = new Set(["1", "on", "t", "true", "y", "yes"]);
const QUERY_FALSE = new Set(["0", "off", "f", "false", "n", "no"]);
// Query names that force an appliance poll on some route; a backend handler
// with a bool parameter of one of these names must declare it in
// `query_classes` (checkManifest).
const FORCE_PARAMS = ["force", "refresh", "fresh"];

function parseQueryBool(value) {
  const text = String(value).toLowerCase();
  if (QUERY_TRUE.has(text)) return true;
  if (QUERY_FALSE.has(text)) return false;
  return null;
}

// True unless every value of `name` is a clean false (absent is false).
function queryRaised(url, name) {
  return url.searchParams.getAll(name).some((value) => parseQueryBool(value) !== false);
}

function forcesRefresh(url, names = FORCE_PARAMS) {
  return names.some((name) => queryRaised(url, name));
}

const DECORATOR_RE = /^([ \t]*)@(\w+)\.(get|post|put|delete|patch|head|options|api_route|websocket)\(\s*(?:("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|([A-Za-z_][\w.]*))/gm;

function loadManifest(file = MANIFEST_PATH) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

// The decorated function's source, from the first decorator of its stack to
// the end of its body, with trailing whitespace normalised.
function handlerBlock(lines, startLine) {
  const indent = (line) => line.match(/^[ \t]*/)[0].length;
  const base = indent(lines[startLine]);
  let i = startLine;
  while (i < lines.length && !/^\s*(async\s+)?def\s/.test(lines[i])) i += 1;
  const defIndent = i < lines.length ? indent(lines[i]) : base;
  let end = i + 1;
  let open = 0;
  for (let j = i; j < lines.length; j += 1) {
    open += (lines[j].match(/[([{]/g) || []).length - (lines[j].match(/[)\]}]/g) || []).length;
    if (/:\s*(#.*)?$/.test(lines[j]) && open <= 0) {
      end = j + 1;
      break;
    }
  }
  while (end < lines.length) {
    const line = lines[end];
    if (line.trim() && indent(line) <= defIndent) break;
    end += 1;
  }
  return lines.slice(startLine, end).map((line) => line.replace(/\s+$/, "")).join("\n").replace(/\n+$/, "");
}

function fingerprint(text) {
  // 24 hex characters: short enough to review, and not the 16-hex shape the
  // public-text privacy scan treats as a SAS WWN.
  return crypto.createHash("sha256").update(text).digest("hex").slice(0, 24);
}

// One source file -> its routes. Non-literal paths are returned with
// `pathExpr` so the manifest must name them explicitly.
function scanRouteSource(relPath, source, service) {
  const lines = source.split("\n");
  const routes = [];
  const problems = [];
  let m;
  DECORATOR_RE.lastIndex = 0;
  while ((m = DECORATOR_RE.exec(source))) {
    const line = source.slice(0, m.index).split("\n").length - 1;
    // Only the first decorator of a stack starts a block; later stacked
    // decorators (rare) share the same handler.
    let first = line;
    while (first > 0 && /^\s*@/.test(lines[first - 1])) first -= 1;
    const block = handlerBlock(lines, first);
    const verb = m[3].toUpperCase();
    const literal = m[4] ? m[4].slice(1, -1) : null;
    if (verb === "API_ROUTE") problems.push(`${relPath}:${line + 1}: api_route() is not supported by the route manifest; use one decorator per method`);
    const boolParams = [...block.matchAll(/\b(\w+)\s*:\s*bool\s*=/g)].map((hit) => hit[1]);
    routes.push({
      service, method: verb, path: literal, pathExpr: literal === null ? m[5] : undefined,
      file: relPath, line: line + 1, fingerprint: fingerprint(block), boolParams,
    });
  }
  if (/\.add_api_route\(|\.add_route\(/.test(source)) problems.push(`${relPath}: add_api_route/add_route is not scanned; declare routes with decorators`);
  return { routes, problems };
}

function scanRoutes(root, manifest) {
  const routes = [];
  const problems = [];
  for (const [rel, service] of Object.entries(manifest.sources)) {
    const out = scanRouteSource(rel, fs.readFileSync(path.join(root, rel), "utf8"), service);
    routes.push(...out.routes);
    problems.push(...out.problems);
  }
  return { routes, problems };
}

// Python files that declare routes but are not listed in `sources`.
function discoverRouteFiles(root) {
  const found = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(path.join(root, dir), { withFileTypes: true })) {
      const rel = path.posix.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!["tests", "__pycache__", "node_modules"].includes(entry.name)) walk(rel);
      } else if (entry.name.endsWith(".py")) {
        const text = fs.readFileSync(path.join(root, rel), "utf8");
        DECORATOR_RE.lastIndex = 0;
        if (DECORATOR_RE.test(text)) found.push(rel);
      }
    }
  };
  for (const dir of ["app", "history_service", "admin_service"]) if (fs.existsSync(path.join(root, dir))) walk(dir);
  return found.sort();
}

function routeKey(route) {
  return `${route.service} ${route.method} ${route.path ?? `<${route.pathExpr}>`}`;
}

// Compare scanned routes with the manifest. Returns human-readable problems.
function checkManifest(scanned, manifest) {
  const problems = [...(scanned.problems || [])];
  const listed = new Map();
  for (const entry of manifest.routes || []) {
    const key = routeKey({ service: entry.service, method: entry.method, path: entry.path_expr ? null : entry.path, pathExpr: entry.path_expr });
    if (listed.has(key)) problems.push(`duplicate manifest route ${key}`);
    listed.set(key, entry);
    if (!ROUTE_CLASSES.includes(entry.class)) problems.push(`${key}: class must be one of ${ROUTE_CLASSES.join(", ")}`);
    if (!entry.why) problems.push(`${key}: every route needs a short \`why\` for its class`);
  }
  const seen = new Set();
  for (const route of scanned.routes) {
    const key = routeKey(route);
    seen.add(key);
    const entry = listed.get(key);
    if (!entry) {
      problems.push(`unclassified route ${key} (${route.file}:${route.line}); add it to qa/ui-routes.json with a reviewed class`);
      continue;
    }
    if (entry.fingerprint !== route.fingerprint) {
      problems.push(`route ${key} changed (${route.file}:${route.line}, fingerprint ${route.fingerprint}, reviewed ${entry.fingerprint}); re-review its class and update the fingerprint`);
    }
    // #1120: a forced-refresh flag must raise the class (no undeclared bypass).
    for (const name of (route.boolParams || []).filter((param) => FORCE_PARAMS.includes(param))) {
      if (!(entry.query_classes || {})[name]) problems.push(`route ${key} takes a \`${name}\` flag; declare it in query_classes (appliance-load)`);
    }
  }
  for (const key of listed.keys()) if (!seen.has(key)) problems.push(`stale manifest route ${key}: not declared in the sources any more`);
  return problems;
}

function templateRegExp(template) {
  const escaped = String(template).replace(/[.+?^$()|[\]\\]/g, "\\$&")
    .replace(/\{[A-Za-z_]\w*:path\}/g, "\u0001")
    .replace(/\{[A-Za-z_]\w*\}/g, "\u0000")
    .replace(/\*\*/g, "\u0001")
    .replace(/\u0000/g, "[^/]+")
    .replace(/\u0001/g, ".+");
  return new RegExp(`^${escaped}$`);
}

// Route lookup for the guard: service -> method -> [{re, entry}].
class RouteTable {
  constructor(manifest) {
    this.byService = new Map();
    const add = (entry) => {
      if (!entry.path || entry.path_expr && !entry.path) return;
      const service = this.byService.get(entry.service) || new Map();
      this.byService.set(entry.service, service);
      const methods = entry.method === "GET" ? ["GET", "HEAD"] : [entry.method];
      for (const method of methods) {
        if (!service.has(method)) service.set(method, []);
        service.get(method).push({ re: templateRegExp(entry.path), entry });
      }
    };
    for (const entry of manifest.routes || []) add(entry);
    for (const entry of manifest.extra_routes || []) add(entry);
    // Literal paths win over templated ones (/api/slots/smart-batch vs /api/slots/{slot}/...).
    for (const service of this.byService.values()) {
      for (const list of service.values()) list.sort((a, b) => (a.entry.path.includes("{") ? 1 : 0) - (b.entry.path.includes("{") ? 1 : 0));
    }
  }

  match(service, method, pathname) {
    const list = this.byService.get(service)?.get(String(method).toUpperCase()) || [];
    const hit = list.find((item) => item.re.test(pathname));
    return hit ? hit.entry : null;
  }

  // The class a concrete request has: a route's `query_classes` can raise it
  // (for example `force=true` turns a cached read into an appliance poll).
  classify(service, method, url) {
    const entry = this.match(service, method, url.pathname);
    if (!entry) return { entry: null, cls: null };
    let cls = entry.class;
    for (const [name, raised] of Object.entries(entry.query_classes || {})) {
      if (queryRaised(url, name)) cls = raised;
    }
    return { entry, cls };
  }
}

// Static trace from browser scripts to routes: every /api/ path literal (with
// `${...}` as one segment) must match a manifest route of the script's service.
function scanScriptApiPaths(relPath, source) {
  const constants = {};
  for (const m of source.matchAll(/const\s+([A-Z_][A-Z0-9_]*)\s*=\s*["'`](\/api\/[^"'`$]*)["'`]/g)) constants[m[1]] = m[2];
  const out = [];
  const re = /(["'`])((?:\$\{[A-Z_][A-Z0-9_]*\})?\/api\/(?:[^"'`\\$]|\$\{[^}]*\})*)\1/g;
  let m;
  while ((m = re.exec(source))) {
    let raw = m[2];
    raw = raw.replace(/^\$\{([A-Z_][A-Z0-9_]*)\}/, (s, name) => constants[name] ?? s);
    if (!raw.startsWith("/api/")) continue;
    const pathOnly = raw.split("?")[0].replace(/\$\{[^}]*\}$/, "");
    if (pathOnly.endsWith("/") && pathOnly !== "/api/") continue;
    const normalized = pathOnly.replace(/\$\{[^}]*\}/g, "x");
    out.push({ file: relPath, line: source.slice(0, m.index).split("\n").length, raw, path: normalized });
  }
  return out;
}

const SCRIPT_SERVICE = [
  [/^app\/static\//, "ui"],
  [/^admin_service\/static\//, "admin"],
  [/^history_service\/static\//, "history"],
];

function checkScriptApiPaths(root, scripts, manifest) {
  const table = new RouteTable(manifest);
  const problems = [];
  for (const rel of scripts) {
    const service = (SCRIPT_SERVICE.find(([re]) => re.test(rel)) || [])[1];
    if (!service) continue;
    for (const call of scanScriptApiPaths(rel, fs.readFileSync(path.join(root, rel), "utf8"))) {
      const methods = ["GET", "POST", "PUT", "DELETE", "PATCH"];
      if (!methods.some((method) => table.match(service, method, call.path))) {
        problems.push(`${call.file}:${call.line} calls ${call.raw}, which matches no ${service} route in qa/ui-routes.json`);
      }
    }
  }
  return problems;
}

module.exports = {
  MANIFEST_PATH, ROUTE_CLASSES, BASE_READ_CLASSES, DECLARABLE, FORCE_PARAMS, parseQueryBool, forcesRefresh, queryRaised,
  loadManifest, scanRouteSource, scanRoutes, discoverRouteFiles, checkManifest, RouteTable, templateRegExp,
  scanScriptApiPaths, checkScriptApiPaths, fingerprint, handlerBlock, routeKey,
};
