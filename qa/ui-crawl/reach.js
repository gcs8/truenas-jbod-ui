"use strict";

// Generated read reach for the UI coverage crawler (D-TNJ-20261109-CRAWLER-SWEEP).
//
// Plain reads (manifest class `read` or `read-post`) are no longer declared by
// hand per state. This module reads the browser scripts each page loads and
// extracts every `/api/...` request they can send: the path (template
// literals included), the HTTP method, and the query keys the call site and
// its query helpers can set. Each call is matched to its exact route in the
// reviewed manifest (qa/ui-routes.json) and assigned to the pages that load
// its script and to the scope kinds whose code path can reach it:
//
//   - a route whose path carries a storage view (`{view_id}`) is reachable
//     only with a selected storage view (app.js reaches those routes through
//     getSelectedStorageViewRuntime()), so only on a storage-view scope, and
//     its view must be the target's own;
//   - every other call is reachable on every scope kind of its page.
//
// The result is qa/ui-read-reach.json (scripts/generate_ui_read_reach.js
// writes it; CI fails when regenerating changes it). The request guard then
// admits a plain read only when it is in the reach of the running page and
// scope kind AND bound to the target (system, enclosure, storage view).
//
// Nothing else is generated: appliance-load, app-write, led, forbidden,
// internal and read-heavy routes stay explicit per-state declarations with
// budgets. They are listed under `declared_per_state` for review only; the
// guard never reads that list.
//
// The extraction is static and conservative on purpose: query keys are the
// union of every key the enclosing function and the query helpers it calls
// can set, so a key can be admitted that one particular request never sends,
// but never a key outside the page's own code.

const fs = require("node:fs");
const path = require("node:path");
const { loadManifest, BASE_READ_CLASSES } = require("./routes");

const REACH_PATH = path.join(__dirname, "..", "ui-read-reach.json");
const GENERATED_CLASSES = BASE_READ_CLASSES; // read, read-post
// Query keys bound to the target. `bind`: when the target has the key, the
// request must carry it and every copy must equal the target's (a read
// without system_id reads the backend's default system, so it is refused on
// another system's target). `bind_if_sent`: every copy the request carries
// must equal the target's; the page may omit it (app.js sends storage_view_id
// only on its own location URL).
const BIND_QUERY_KEYS = ["system_id", "enclosure_id"];
const BIND_IF_SENT_KEYS = ["storage_view_id"];
// Path parameters bound to the target, by the target key they must equal.
const BIND_PATH_PARAMS = { view_id: "storage_view_id" };

// Pages, the template each one renders and its scope kinds. The service is the
// origin the guard resolves (an offline copy has no /api routes at all).
const PAGES = {
  main: { service: "ui", template: "app/templates/index.html", scopes: ["enclosure", "storage-view"] },
  fabric: { service: "ui", template: "app/templates/sas_fabric.html", scopes: ["enclosure"] },
  history: { service: "history", template: "history_service/templates/dashboard.html", scopes: ["page"] },
  admin: { service: "admin", template: "admin_service/templates/index.html", scopes: ["page"] },
  // The offline copy is a static document: app.js runs in snapshot mode and
  // sends no /api request, and the offline origin serves no /api route.
  offline: { service: "offline", template: null, scopes: ["page"], why: "static snapshot document; snapshot mode sends no /api request" },
};

// One template expression inside a normalised call path.
const EXPR = "\u0000";

// Manifest routes of `service` and `method` a call path can reach: segment
// by segment, a literal equals a literal or fills a route parameter, and an
// expression fills a parameter or any literal (`.../${action}` reaches stop,
// start and restart alike).
function matchRoutes(manifest, service, method, callPath) {
  const entries = [...(manifest.routes || []), ...(manifest.extra_routes || [])]
    .filter((entry) => entry.service === service && entry.method === method && entry.path && !entry.path_expr);
  const callSegments = callPath.split("/");
  const segmentRe = (segment) => new RegExp(`^${segment.split(EXPR).map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(".+")}$`);
  const hits = entries.filter((entry) => {
    const routeSegments = entry.path.split("/");
    if (/\{\w+:path\}/.test(entry.path)) return false; // static mounts are not /api routes
    if (routeSegments.length !== callSegments.length) return false;
    return routeSegments.every((routeSegment, index) => {
      const call = callSegments[index];
      if (/^\{\w+\}$/.test(routeSegment)) return call.length > 0;
      if (call.includes(EXPR)) return segmentRe(call).test(routeSegment);
      return call === routeSegment;
    });
  });
  // Prefer the closest reading: a literal call segment matching a literal
  // route segment beats a parameter (RouteTable does the same:
  // /api/slots/smart-batch vs /api/slots/{slot}/...), and an expression
  // filling a parameter beats it standing for a literal (artifactUrl(id) is
  // /api/admin/backups/{artifact_id}, not /api/admin/backups/policy).
  const cost = (entry) => entry.path.split("/").reduce((sum, routeSegment, index) => {
    const param = /^\{\w+\}$/.test(routeSegment);
    const expr = callSegments[index].includes(EXPR);
    return sum + (expr && !param ? 2 : !expr && param ? 1 : 0);
  }, 0);
  const best = Math.min(...hits.map(cost));
  return hits.filter((entry) => cost(entry) === best);
}

const NOT_FUNCTION = new Set(["if", "for", "while", "switch", "catch", "with", "return", "typeof", "await", "yield"]);
const REGEX_AFTER_WORD = new Set(["return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "yield", "await", "instanceof"]);

// ---------------------------------------------------------------------------
// Lexer: two same-length views of the source (newlines kept, so offsets and
// line numbers stay true) plus every string token.
//   code      strings, template text, comments and regex literals blanked
//             (template `${...}` expressions stay code)
//   text      only comments blanked
function lexScript(source) {
  const code = source.split("");
  const text = source.split("");
  const strings = [];
  const blank = (arr, from, to) => {
    for (let i = from; i < to; i += 1) if (arr[i] !== "\n") arr[i] = " ";
  };
  // Stack of open template literals: { start, depth } where depth is the
  // brace depth of the `${` that entered code.
  const templates = [];
  let depth = 0;
  let last = ""; // last significant code character
  let lastWord = "";
  let i = 0;
  const n = source.length;
  const readTemplate = (start, from) => {
    // Scan template text from `from` until a closing backtick or `${`.
    let j = from;
    while (j < n) {
      const ch = source[j];
      if (ch === "\\") {
        j += 2;
        continue;
      }
      if (ch === "`") {
        blank(code, from, j + 1);
        const tpl = templates.pop();
        strings.push({ start: tpl.start, end: j + 1, quote: "`", raw: source.slice(tpl.start + 1, j) });
        return j + 1;
      }
      if (ch === "$" && source[j + 1] === "{") {
        blank(code, from, j);
        templates[templates.length - 1].depth = depth;
        depth += 1;
        return j + 2;
      }
      j += 1;
    }
    throw new Error(`unterminated template literal at offset ${start}`);
  };
  while (i < n) {
    const ch = source[i];
    if (ch === "/" && source[i + 1] === "/") {
      const end = source.indexOf("\n", i);
      const stop = end < 0 ? n : end;
      blank(code, i, stop);
      blank(text, i, stop);
      i = stop;
      continue;
    }
    if (ch === "/" && source[i + 1] === "*") {
      const end = source.indexOf("*/", i + 2);
      if (end < 0) throw new Error(`unterminated comment at offset ${i}`);
      blank(code, i, end + 2);
      blank(text, i, end + 2);
      i = end + 2;
      continue;
    }
    if (ch === "\"" || ch === "'") {
      let j = i + 1;
      while (j < n && source[j] !== ch) {
        if (source[j] === "\\") j += 1;
        else if (source[j] === "\n") throw new Error(`unterminated string at offset ${i}`);
        j += 1;
      }
      strings.push({ start: i, end: j + 1, quote: ch, raw: source.slice(i + 1, j) });
      blank(code, i, j + 1);
      i = j + 1;
      last = "a";
      lastWord = "";
      continue;
    }
    if (ch === "`") {
      templates.push({ start: i, depth: -1 });
      i = readTemplate(i, i + 1);
      last = "a";
      lastWord = "";
      continue;
    }
    if (ch === "/") {
      const regexStart = !last || "(,=:[!&|?{};+-*%<>~^".includes(last) || REGEX_AFTER_WORD.has(lastWord);
      if (regexStart) {
        let j = i + 1;
        let inClass = false;
        while (j < n) {
          const c = source[j];
          if (c === "\\") {
            j += 2;
            continue;
          }
          if (c === "\n") throw new Error(`unterminated regex at offset ${i}`);
          if (inClass) {
            if (c === "]") inClass = false;
          } else if (c === "[") inClass = true;
          else if (c === "/") break;
          j += 1;
        }
        j += 1;
        while (j < n && /[a-z]/i.test(source[j])) j += 1;
        blank(code, i, j);
        i = j;
        last = "a";
        lastWord = "";
        continue;
      }
    }
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      const tpl = templates[templates.length - 1];
      if (tpl && tpl.depth === depth) {
        i = readTemplate(tpl.start, i + 1);
        last = "a";
        lastWord = "";
        continue;
      }
    }
    if (/\s/.test(ch)) {
      i += 1;
      continue;
    }
    if (/[A-Za-z_$0-9]/.test(ch)) {
      let j = i;
      while (j < n && /[A-Za-z_$0-9]/.test(source[j])) j += 1;
      lastWord = source.slice(i, j);
      last = "a";
      i = j;
      continue;
    }
    last = ch;
    lastWord = "";
    i += 1;
  }
  if (depth !== 0 || templates.length) throw new Error(`unbalanced braces (${depth}) or template literals (${templates.length})`);
  return { source, code: code.join(""), text: text.join(""), strings };
}

function lineOf(source, offset) {
  let line = 1;
  for (let i = 0; i < offset; i += 1) if (source.charCodeAt(i) === 10) line += 1;
  return line;
}

function matchingClose(code, open, openCh = "(", closeCh = ")") {
  let level = 0;
  for (let i = open; i < code.length; i += 1) {
    if (code[i] === openCh) level += 1;
    else if (code[i] === closeCh) {
      level -= 1;
      if (level === 0) return i;
    }
  }
  return -1;
}

function matchingOpen(code, close, openCh = "(", closeCh = ")") {
  let level = 0;
  for (let i = close; i >= 0; i -= 1) {
    if (code[i] === closeCh) level += 1;
    else if (code[i] === openCh) {
      level -= 1;
      if (level === 0) return i;
    }
  }
  return -1;
}

// Function and method bodies (`name(...) {`, `function (...) {`) and arrow
// block bodies (`=> {`), with names.
function functionSpans(lexed) {
  const { code } = lexed;
  const spans = [];
  for (let i = 0; i < code.length; i += 1) {
    if (code[i] !== "{") continue;
    let j = i - 1;
    while (j >= 0 && /\s/.test(code[j])) j -= 1;
    if (j >= 1 && code[j] === ">" && code[j - 1] === "=") {
      const close = matchingClose(code, i, "{", "}");
      // const name = (...) => {  /  name: (...) => {
      const head = code.slice(Math.max(0, j - 400), j - 1);
      const named = /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*$/.exec(head)
        || /([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*$/.exec(head);
      spans.push({ start: i, end: close, kind: "arrow", name: named ? named[1] : null });
      continue;
    }
    if (code[j] !== ")") continue;
    const open = matchingOpen(code, j);
    if (open < 0) continue;
    let k = open - 1;
    while (k >= 0 && /\s/.test(code[k])) k -= 1;
    let end = k;
    while (k >= 0 && /[\w$]/.test(code[k])) k -= 1;
    const word = code.slice(k + 1, end + 1);
    if (!word || NOT_FUNCTION.has(word)) continue;
    let name = word;
    if (word === "function") {
      // const name = function (...) {   or an anonymous function expression.
      const head = code.slice(Math.max(0, k - 200), k + 1);
      const named = /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?$/.exec(head);
      name = named ? named[1] : null;
    } else {
      // function name(...) {   /   async name(...) {   /   name(...) { (method)
      const before = code.slice(Math.max(0, k - 40), k + 1);
      if (!/(function\s*\*?\s*|async\s+|[{},;\n]\s*|^\s*)$/.test(before)) continue;
    }
    const close = matchingClose(code, i, "{", "}");
    spans.push({ start: i, end: close, kind: "function", name, params: lexed.text.slice(open + 1, j) });
  }
  return spans;
}

// The function whose body a call site belongs to: the innermost function or
// method; when that is the file's module wrapper (or none), the outermost
// arrow block inside it (an event-listener callback, for example).
function enclosingSpan(spans, offset, length) {
  const containing = spans.filter((span) => span.start < offset && span.end > offset);
  const wrapper = (span) => span.end - span.start > length * 0.8;
  const functions = containing.filter((span) => span.kind === "function" && !wrapper(span));
  if (functions.length) return functions.reduce((a, b) => (b.start > a.start ? b : a));
  const arrows = containing.filter((span) => span.kind === "arrow" && !wrapper(span));
  if (arrows.length) return arrows.reduce((a, b) => (b.start < a.start ? b : a));
  return null;
}

// Escape every regular-expression metacharacter, backslash included, before a
// scanned name goes into a RegExp source.
const escapeRegExp = (text) => String(text).replace(/[\\^$.*+?()[\]{}|/]/g, "\\$&");
const CALL_RE = (name) => new RegExp(`(?<![\\w$.])${escapeRegExp(name)}\\s*\\(`, "g");

function calledNames(code, names) {
  return names.filter((name) => CALL_RE(name).test(code));
}

// Variables in `text` that hold query parameters: assigned from
// `new URLSearchParams`, from a query helper's result, or a URL's
// `.searchParams`.
function paramVars(text, helpers) {
  const vars = new Set();
  const re = /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;]*)/g;
  let m;
  while ((m = re.exec(text))) {
    const rhs = m[2];
    if (/new\s+URLSearchParams\b/.test(rhs) || /\.searchParams\b/.test(rhs) || helpers.some((name) => CALL_RE(name).test(rhs))) vars.add(m[1]);
  }
  return vars;
}

// Literal keys `text` sets on query parameters it owns.
function setKeys(text, helpers) {
  const keys = new Set();
  const vars = paramVars(text, helpers);
  const re = /([A-Za-z_$][\w$]*)(\.searchParams)?\.(?:set|append)\(\s*["'`]([A-Za-z_][\w-]*)["'`]/g;
  let m;
  while ((m = re.exec(text))) {
    if (m[2] || vars.has(m[1])) keys.add(m[3]);
  }
  // new URLSearchParams({ key: ..., other })
  for (const hit of text.matchAll(/new\s+URLSearchParams\(\s*\{([^{}]*)\}/g)) {
    for (const prop of hit[1].matchAll(/(?:^|,)\s*["']?([A-Za-z_][\w]*)["']?\s*(?::|,|$)/g)) keys.add(prop[1]);
  }
  return keys;
}

// Query helpers: small functions that build or edit query parameters and
// send nothing themselves (no /api/ path in them). Their keys reach every
// call site whose function calls them, directly or through another helper.
function queryHelpers(lexed, spans) {
  const named = spans.filter((span) => span.kind === "function" && span.name);
  const apiInside = (span) => lexed.strings.some((s) => s.start > span.start && s.end < span.end && /\/api\//.test(s.raw));
  const small = (span) => lineOf(lexed.source, span.end) - lineOf(lexed.source, span.start) <= 60;
  const candidates = named.filter((span) => small(span) && !apiInside(span));
  const helpers = new Map();
  let changed = true;
  while (changed) {
    changed = false;
    for (const span of candidates) {
      if (helpers.has(span.name)) continue;
      const code = lexed.code.slice(span.start, span.end);
      if (/\bURLSearchParams\b|\.searchParams\b/.test(code) || calledNames(code, [...helpers.keys()]).length) {
        helpers.set(span.name, span);
        changed = true;
      }
    }
  }
  const keysOf = new Map();
  const resolve = (name, seen = new Set()) => {
    if (keysOf.has(name)) return keysOf.get(name);
    if (seen.has(name)) return new Set();
    seen.add(name);
    const span = helpers.get(name);
    const text = lexed.text.slice(span.start, span.end);
    const keys = setKeys(text, [...helpers.keys()]);
    for (const callee of calledNames(lexed.code.slice(span.start, span.end), [...helpers.keys()].filter((other) => other !== name))) {
      for (const key of resolve(callee, seen)) keys.add(key);
    }
    keysOf.set(name, keys);
    return keys;
  };
  for (const name of helpers.keys()) resolve(name);
  return keysOf;
}

// `const NAME = "/api/..."` prefixes, as routes.scanScriptApiPaths reads them.
function apiConstants(lexed) {
  const out = {};
  for (const m of lexed.text.matchAll(/const\s+([A-Z_][A-Z0-9_]*)\s*=\s*["'`](\/api\/[^"'`$]*)["'`]/g)) out[m[1]] = m[2];
  return out;
}

// Template expressions as written: `${...}` with nested templates/braces.
function templateExpressions(raw) {
  const out = [];
  for (let i = raw.indexOf("${"); i >= 0; i = raw.indexOf("${", i + 1)) {
    let level = 0;
    for (let j = i + 1; j < raw.length; j += 1) {
      if (raw[j] === "{") level += 1;
      else if (raw[j] === "}") {
        level -= 1;
        if (level === 0) {
          out.push({ start: i, end: j + 1 });
          i = j;
          break;
        }
      }
    }
  }
  return out;
}

// A request path from a string literal: the `${CONST}` prefix resolved, the
// query (after `?`, or a trailing expression that does not follow a `/`)
// dropped, and every other `${...}` one path segment. null when the literal
// is not an /api/ path.
function normalizeApiPath(raw, constants) {
  const value = raw.replace(/^\$\{([A-Z_][A-Z0-9_]*)\}/, (s, name) => constants[name] ?? s);
  if (!value.startsWith("/api/")) return null;
  let text = value;
  const query = text.indexOf("?");
  // A `?` inside an expression (`${a ? b : c}`) is not the query start.
  const exprs = templateExpressions(text);
  const inExpr = (at) => exprs.some((e) => at > e.start && at < e.end);
  let cut = -1;
  for (let at = query; at >= 0; at = text.indexOf("?", at + 1)) {
    if (!inExpr(at)) {
      cut = at;
      break;
    }
  }
  if (cut >= 0) text = text.slice(0, cut);
  const tail = templateExpressions(text).filter((e) => e.end === text.length).pop();
  if (tail && text[tail.start - 1] !== "/") text = text.slice(0, tail.start);
  let out = "";
  let last = 0;
  for (const e of templateExpressions(text)) {
    out += `${text.slice(last, e.start)}${EXPR}`;
    last = e.end;
  }
  out += text.slice(last);
  return { value, path: out };
}

// The `(` of the call whose argument list holds offset `at`, or -1 when the
// expression is not an argument (an assignment, a return, an object value).
// Template `${...}` braces are transparent.
function enclosingCallOpen(code, at, floor) {
  let level = 0;
  for (let i = at - 1; i >= floor; i -= 1) {
    const ch = code[i];
    if (ch === ")" || ch === "]" || ch === "}") level += 1;
    else if (ch === "(" || ch === "[" || ch === "{") {
      if (level === 0) {
        if (ch === "{" && code[i - 1] === "$") continue;
        return ch === "(" ? i : -1;
      }
      level -= 1;
    } else if (level === 0 && ch === ";") return -1;
  }
  return -1;
}

const methodsIn = (text, from, to) => [...text.slice(from, to).matchAll(/\bmethod\s*:\s*["'`]([A-Za-z]+)["'`]/g)].map((m) => m[1].toUpperCase());

// HTTP methods of the request an expression at `at` becomes: the `method:`
// of the call that takes it (through query helpers such as buildScopedUrl),
// or of the calls that take the variable it is assigned to; GET when none
// names one.
function siteMethods(lexed, at, span, helperNames) {
  const { code, text } = lexed;
  const floor = span ? span.start : 0;
  let pos = at;
  for (let hops = 0; hops < 4; hops += 1) {
    const open = enclosingCallOpen(code, pos, floor);
    if (open < 0) break;
    const close = matchingClose(code, open);
    const callee = /([A-Za-z_$][\w$.]*)\s*$/.exec(code.slice(Math.max(0, open - 80), open));
    const name = callee ? callee[1].split(".").pop() : "";
    if (!name || NOT_FUNCTION.has(name)) break;
    const found = methodsIn(text, open, close);
    if (found.length) return [...new Set(found)];
    // A query helper (buildScopedUrl, sendScopedRequest) without options:
    // the request is the call around it.
    if (helperNames.includes(name)) {
      pos = open;
      continue;
    }
    return ["GET"];
  }
  // const url = `...`;  ->  the calls that take `url`
  const statement = code.slice(floor, pos);
  const assign = /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=[^;=]*$/.exec(statement) || /([A-Za-z_$][\w$]*)\s*=\s*[^;=]*$/.exec(statement)
    || /([A-Za-z_$][\w$]*)\s*:\s*[^;:,{]*$/.exec(statement);
  if (assign) {
    const variable = assign[1];
    const methods = new Set();
    const body = code.slice(floor, span ? span.end : code.length);
    for (const call of body.matchAll(new RegExp(`([A-Za-z_$][\\w$.]*)\\s*\\(\\s*${escapeRegExp(variable)}\\b`, "g"))) {
      const open = floor + call.index + call[0].lastIndexOf("(");
      const found = methodsIn(text, open, matchingClose(code, open));
      (found.length ? found : ["GET"]).forEach((method) => methods.add(method));
    }
    if (methods.size) return [...methods];
  }
  return ["GET"];
}

// Top-level comma split of an argument list's text.
function splitArgs(textArgs) {
  const out = [];
  let level = 0;
  let current = "";
  for (const ch of textArgs) {
    if ("([{".includes(ch)) level += 1;
    if (")]}".includes(ch)) level -= 1;
    if (ch === "," && level === 0) {
      out.push(current.trim());
      current = "";
    } else current += ch;
  }
  if (current.trim()) out.push(current.trim());
  return out;
}

const STRING_LITERAL = /^(["'])((?:[^"'\\]|\\.)*)\1$/;

function queryKeysAt(lexed, span, helperKeys, raw) {
  const helperNames = [...helperKeys.keys()];
  const keys = new Set();
  for (const m of String(raw).matchAll(/[?&]([A-Za-z_][\w]*)=/g)) keys.add(m[1]);
  if (!span) return keys;
  for (const key of setKeys(lexed.text.slice(span.start, span.end), helperNames)) keys.add(key);
  for (const helper of calledNames(lexed.code.slice(span.start, span.end), helperNames)) for (const key of helperKeys.get(helper)) keys.add(key);
  return keys;
}

function spanLabel(lexed, span) {
  return span ? (span.name || `(${span.kind} at line ${lineOf(lexed.source, span.start)})`) : "(top level)";
}

// Every /api/ request one browser script can send.
function extractCalls(rel, source, { service, manifest }) {
  const lexed = lexScript(source);
  const spans = functionSpans(lexed);
  const helperKeys = queryHelpers(lexed, spans);
  const helperNames = [...helperKeys.keys()];
  const constants = apiConstants(lexed);
  const calls = [];
  const problems = [];
  const add = ({ line, fn, rawPath, methods, keys }) => {
    const normalized = normalizeApiPath(rawPath, constants);
    if (!normalized) {
      problems.push(`${rel}:${line} has an /api/ literal ${JSON.stringify(rawPath).slice(0, 120)} in ${fn} that is not a request path the extractor can read; rewrite the call or extend qa/ui-crawl/reach.js`);
      return;
    }
    const matched = methods.flatMap((method) => matchRoutes(manifest, service, method, normalized.path).map((entry) => ({ method, entry })));
    if (!matched.length) {
      // Never silently dropped: an unresolved call is a generator failure.
      problems.push(`${rel}:${line} calls ${methods.join("/")} ${normalized.path.split(EXPR).join("{expr}")} in ${fn}, which matches no ${service} route in qa/ui-routes.json; fix the call, the manifest or the extractor`);
      return;
    }
    for (const { method, entry } of matched) {
      calls.push({ file: rel, line, function: fn, method, path: entry.path, class: entry.class, query: [...keys].sort() });
    }
  };
  for (const string of lexed.strings) {
    if (!/^(?:\/api\/|\$\{[A-Z_][A-Z0-9_]*\}\/)/.test(string.raw) && !/\/api\//.test(string.raw)) continue;
    const normalized = normalizeApiPath(string.raw, constants);
    if (!normalized) {
      // `/api/` inside a longer literal (an absolute URL, prose) is not a
      // same-origin request; anything that starts like one must resolve.
      if (/^\/api\//.test(string.raw) || /^\$\{[A-Z_][A-Z0-9_]*\}/.test(string.raw) && Object.keys(constants).some((name) => string.raw.startsWith(`\${${name}}`))) {
        problems.push(`${rel}:${lineOf(source, string.start)} has an /api/ literal the extractor cannot read: ${JSON.stringify(string.raw).slice(0, 120)}`);
      }
      continue;
    }
    // `const NAME = "/api/..."` is a request only where a call takes NAME
    // itself; as a `${NAME}/...` prefix the template is its own call site.
    const constant = /const\s+([A-Z_][A-Z0-9_]*)\s*=\s*$/.exec(lexed.code.slice(Math.max(0, string.start - 80), string.start));
    if (constant && constants[constant[1]] === string.raw && !new RegExp(`\\(\\s*${constant[1]}\\s*[,)]`).test(lexed.code)) continue;
    const span = enclosingSpan(spans, string.start, source.length);
    const line = lineOf(source, string.start);
    // A path builder (`function artifactUrl(id, suffix) { return `/api/...`; }`):
    // the requests are its callers', with literal arguments substituted.
    const before = lexed.code.slice(span ? span.start : 0, string.start);
    if (span && span.kind === "function" && span.name && /\breturn\s*$/.test(before)) {
      const params = (span.params || "").split(",").map((param) => param.trim()).filter(Boolean)
        .map((param) => {
          const [name, ...rest] = param.split("=");
          const fallback = rest.join("=").trim();
          return { name: name.trim(), fallback: STRING_LITERAL.test(fallback) ? STRING_LITERAL.exec(fallback)[2] : null };
        });
      for (const call of lexed.code.matchAll(CALL_RE(span.name))) {
        const open = call.index + call[0].length - 1;
        if (open > span.start && open < span.end) continue;
        if (/function\s*$/.test(lexed.code.slice(Math.max(0, call.index - 12), call.index))) continue;
        const close = matchingClose(lexed.code, open);
        const args = splitArgs(lexed.text.slice(open + 1, close));
        let rawPath = string.raw;
        params.forEach((param, index) => {
          const arg = args[index];
          const literal = arg !== undefined ? (STRING_LITERAL.exec(arg) || [])[2] : param.fallback;
          if (literal !== undefined && literal !== null) rawPath = rawPath.split(`\${${param.name}}`).join(literal);
        });
        const caller = enclosingSpan(spans, open, source.length);
        add({
          line: lineOf(source, open), fn: `${spanLabel(lexed, caller)} via ${span.name}`, rawPath,
          methods: siteMethods(lexed, call.index, caller, helperNames), keys: queryKeysAt(lexed, caller, helperKeys, rawPath),
        });
      }
      continue;
    }
    add({
      line, fn: spanLabel(lexed, span), rawPath: string.raw,
      methods: siteMethods(lexed, string.start, span, helperNames), keys: queryKeysAt(lexed, span, helperKeys, string.raw),
    });
  }
  return { calls, problems };
}

// ---------------------------------------------------------------------------
// Templates -> the scripts a page loads (Jinja `extends` and `block scripts`).

function templateBlocks(text) {
  const blocks = {};
  const re = /\{%-?\s*block\s+(\w+)\s*-?%\}([\s\S]*?)\{%-?\s*endblock(?:\s+\w+)?\s*-?%\}/g;
  let m;
  while ((m = re.exec(text))) blocks[m[1]] = m[2];
  return blocks;
}

function scriptsInText(text, staticDir) {
  return [...text.matchAll(/<script[^>]*\bsrc="\{\{\s*url_for\(\s*'static'\s*,\s*path\s*=\s*'([^']+)'\s*\)\s*\}\}[^"]*"/g)].map((m) => path.posix.join(staticDir, m[1]));
}

function pageScripts(root, templateRel) {
  const dir = path.posix.dirname(templateRel);
  const staticDir = path.posix.join(path.posix.dirname(dir), "static");
  const text = fs.readFileSync(path.join(root, templateRel), "utf8");
  const parent = /\{%-?\s*extends\s+["']([^"']+)["']\s*-?%\}/.exec(text);
  if (!parent) return scriptsInText(text, staticDir);
  const parentText = fs.readFileSync(path.join(root, dir, parent[1]), "utf8");
  const own = templateBlocks(text);
  // The parent's markup, with every block the child overrides replaced.
  let merged = parentText.replace(/\{%-?\s*block\s+(\w+)\s*-?%\}([\s\S]*?)\{%-?\s*endblock(?:\s+\w+)?\s*-?%\}/g, (all, name, body) => (own[name] !== undefined ? own[name] : body));
  if (own.body === undefined) merged += text;
  return [...new Set(scriptsInText(merged, staticDir))];
}

// ---------------------------------------------------------------------------

function ruleKey(rule) {
  return `${rule.method} ${rule.path}`;
}

function buildReach(root, { manifest = loadManifest(), pages = PAGES } = {}) {
  const problems = [];
  const byScript = new Map();
  const out = {
    schema: 1,
    about: "GENERATED by scripts/generate_ui_read_reach.js from the browser scripts each page loads; do not edit. Plain reads (read, read-post) the crawler admits per page and scope kind, each bound to the target. Every other class reachable from page JS is listed under declared_per_state for review only: those stay explicit per-state declarations with budgets (qa/ui-states.json). See qa/ui-crawl/reach.js.",
    pages: {},
  };
  for (const [pageId, page] of Object.entries(pages)) {
    const scripts = page.template ? pageScripts(root, page.template) : [];
    const reads = new Map();
    const perState = new Map();
    for (const rel of scripts) {
      const key = `${rel} ${page.service}`;
      if (!byScript.has(key)) {
        const source = fs.readFileSync(path.join(root, rel), "utf8");
        let result;
        try {
          result = extractCalls(rel, source, { service: page.service, manifest });
        } catch (error) {
          result = { calls: [], problems: [`${rel}: ${error.message}`] };
        }
        byScript.set(key, result);
        problems.push(...result.problems);
      }
      for (const call of byScript.get(key).calls) {
        const target = GENERATED_CLASSES.has(call.class) ? reads : perState;
        const id = ruleKey(call);
        const entry = target.get(id) || { method: call.method, path: call.path, class: call.class, query: new Set(), sites: new Set() };
        call.query.forEach((name) => entry.query.add(name));
        entry.sites.add(`${call.file}#${call.function}`);
        target.set(id, entry);
      }
    }
    const scopes = {};
    for (const scope of page.scopes) {
      const rules = [];
      for (const entry of [...reads.values()].sort((a, b) => ruleKey(a).localeCompare(ruleKey(b)))) {
        const params = [...entry.path.matchAll(/\{(\w+)(?::\w+)?\}/g)].map((m) => m[1]);
        const bindParams = Object.fromEntries(params.filter((name) => BIND_PATH_PARAMS[name]).map((name) => [name, BIND_PATH_PARAMS[name]]));
        // A storage-view route needs a selected storage view (see the header).
        if (Object.keys(bindParams).length && scope !== "storage-view") continue;
        const query = [...entry.query].sort();
        const rule = { method: entry.method, path: entry.path, class: entry.class, query, bind: BIND_QUERY_KEYS.filter((key) => query.includes(key)) };
        const ifSent = BIND_IF_SENT_KEYS.filter((key) => query.includes(key));
        if (ifSent.length) rule.bind_if_sent = ifSent;
        if (Object.keys(bindParams).length) rule.bind_params = bindParams;
        rule.sites = [...entry.sites].sort();
        rules.push(rule);
      }
      scopes[scope] = rules;
    }
    out.pages[pageId] = {
      service: page.service,
      template: page.template,
      scripts,
      scopes,
      declared_per_state: [...perState.values()].sort((a, b) => ruleKey(a).localeCompare(ruleKey(b)))
        .map((entry) => ({ method: entry.method, path: entry.path, class: entry.class, sites: [...entry.sites].sort() })),
    };
  }
  return { reach: out, problems };
}

// One rule per line, so a review diff shows exactly which read moved.
function renderReach(reach) {
  const lines = ["{", ` "schema": ${JSON.stringify(reach.schema)},`, ` "about": ${JSON.stringify(reach.about)},`, ' "pages": {'];
  const pages = Object.entries(reach.pages);
  pages.forEach(([pageId, page], pageIndex) => {
    lines.push(`  ${JSON.stringify(pageId)}: {`);
    lines.push(`   "service": ${JSON.stringify(page.service)},`);
    lines.push(`   "template": ${JSON.stringify(page.template)},`);
    lines.push(`   "scripts": ${JSON.stringify(page.scripts)},`);
    lines.push('   "scopes": {');
    const scopes = Object.entries(page.scopes);
    scopes.forEach(([scope, rules], scopeIndex) => {
      lines.push(`    ${JSON.stringify(scope)}: [${rules.length ? "" : `]${scopeIndex < scopes.length - 1 ? "," : ""}`}`);
      if (rules.length) {
        rules.forEach((rule, index) => lines.push(`     ${JSON.stringify(rule)}${index < rules.length - 1 ? "," : ""}`));
        lines.push(`    ]${scopeIndex < scopes.length - 1 ? "," : ""}`);
      }
    });
    lines.push("   },");
    const rest = page.declared_per_state;
    lines.push(`   "declared_per_state": [${rest.length ? "" : "]"}`);
    if (rest.length) {
      rest.forEach((entry, index) => lines.push(`    ${JSON.stringify(entry)}${index < rest.length - 1 ? "," : ""}`));
      lines.push("   ]");
    }
    lines.push(`  }${pageIndex < pages.length - 1 ? "," : ""}`);
  });
  lines.push(" }", "}");
  return `${lines.join("\n")}\n`;
}

let cachedReach = null;
function loadReach(file = REACH_PATH) {
  if (file === REACH_PATH) {
    if (!cachedReach) cachedReach = JSON.parse(fs.readFileSync(file, "utf8"));
    return cachedReach;
  }
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

// Problems that make the committed reach unusable: unresolved call sites, or
// a file that differs from what the sources generate now (a live crawl
// refuses to start on either; CI fails on either).
function reachProblems(root, { file = REACH_PATH, manifest = loadManifest() } = {}) {
  const { reach, problems } = buildReach(root, { manifest });
  if (problems.length) return problems;
  const current = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : null;
  return current === renderReach(reach) ? [] : [`${path.relative(root, file) || file} is stale: regenerate it with node scripts/generate_ui_read_reach.js and review the change`];
}

// The generated read rules of one page and scope kind, in the guard's read
// rule shape (no sites/class).
function reachRules(reach, pageId, scope = null) {
  const page = ((reach || {}).pages || {})[pageId];
  if (!page) return [];
  const kind = scope || Object.keys(page.scopes)[0];
  if (!page.scopes[kind]) throw new Error(`page ${pageId} has no scope kind ${kind} in qa/ui-read-reach.json`);
  return page.scopes[kind].map(({ sites, class: cls, ...rule }) => ({ ...rule, generated: true }));
}

// Every generated read of a page, over all its scope kinds.
function reachRulesAnyScope(reach, pageId) {
  const page = ((reach || {}).pages || {})[pageId];
  if (!page) return [];
  return Object.keys(page.scopes).flatMap((scope) => reachRules(reach, pageId, scope));
}

// The scope kind of a page target, from its URL.
function scopeKindFor(pageId, url, pages = PAGES) {
  const page = pages[pageId];
  if (!page) return null;
  if (!page.scopes.includes("storage-view")) return page.scopes[0];
  try {
    return new URL(url).searchParams.get("storage_view_id") ? "storage-view" : "enclosure";
  } catch {
    return page.scopes[0];
  }
}

module.exports = {
  REACH_PATH, PAGES, BIND_QUERY_KEYS, BIND_IF_SENT_KEYS, BIND_PATH_PARAMS, GENERATED_CLASSES,
  lexScript, functionSpans, extractCalls, matchRoutes, normalizeApiPath, pageScripts, buildReach, renderReach, loadReach, reachProblems, reachRules, reachRulesAnyScope, scopeKindFor,
  escapeRegExp,
};
