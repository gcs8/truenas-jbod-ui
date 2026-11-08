"use strict";

// Static scan of the UI sources for interactive controls (#ui-coverage).
//
// Every control the templates or the browser scripts can render gets a stable
// key, and qa/ui-states.json must name that key in a state's `covers` list or
// in `ignored_controls` with a reason. The scan is deliberately simple and
// conservative: it reads tag text, not a DOM, so it works without a running
// stack and fails closed when it cannot key a control.
//
// Key forms, in order of preference:
//   #id                    a static id attribute
//   [data-hook]            the first data-* attribute that is not styling data
//   [name=field]           a form field name
//   .class                 the first class that is not a generic button class
//   tag"Text"@#ancestor    last resort: tag, visible text, nearest ancestor id

const fs = require("node:fs");
const path = require("node:path");

const CONTROL_TAGS = new Set(["button", "select", "input", "textarea", "details", "dialog", "a"]);
const VOID_TAGS = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
// data-* attributes that describe styling or bookkeeping, never a click hook.
const NON_HOOK_DATA = new Set([
  "data-tone", "data-face-style", "data-latch-edge", "data-notice-version", "data-cooldown-seconds-remaining",
  "data-layout-rows", "data-layout-mode", "data-state",
]);
const GENERIC_CLASSES = new Set([
  "button", "secondary", "small", "danger", "primary", "compact", "active", "hidden", "is-selected", "is-active",
  "meta-select", "field", "toggle",
]);

// Source files the scan reads. A new UI source file must be added here; the
// coverage spec fails if a template or static script exists that is not listed.
const UI_SOURCES = {
  templates: [
    "app/templates/index.html",
    "app/templates/sas_fabric.html",
    "app/templates/base.html",
    "admin_service/templates/index.html",
    "admin_service/templates/base.html",
    "history_service/templates/dashboard.html",
  ],
  scripts: [
    "app/static/app.js",
    "app/static/sas_fabric_view.js",
    "admin_service/static/admin.js",
    "admin_service/static/admin_backups.js",
    "history_service/static/dashboard.js",
  ],
};

function stripJinja(text) {
  // Keep line numbers stable: replace Jinja blocks/comments with spaces.
  return text
    .replace(/\{#[\s\S]*?#\}/g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/\{%[\s\S]*?%\}/g, (m) => m.replace(/[^\n]/g, " "));
}

function parseAttributes(raw) {
  const attrs = {};
  const re = /([:@a-zA-Z_][\w:.-]*)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
  let m;
  while ((m = re.exec(raw))) {
    const name = m[1].toLowerCase();
    if (name in attrs) continue;
    attrs[name] = m[3] ?? m[4] ?? m[5] ?? "";
  }
  return attrs;
}

function isDynamic(value) {
  return /\{\{|\$\{/.test(String(value || ""));
}

function lineOf(text, index) {
  let line = 1;
  for (let i = 0; i < index && i < text.length; i += 1) if (text.charCodeAt(i) === 10) line += 1;
  return line;
}

function textAfter(text, index) {
  const rest = text.slice(index, index + 400);
  const close = rest.search(/<\/(button|a|summary|option)\b/i);
  const chunk = close >= 0 ? rest.slice(0, close) : rest.slice(0, 80);
  return chunk.replace(/<[^>]*>/g, " ").replace(/\$\{[^}]*\}|\{\{[^}]*\}\}/g, " ").replace(/\s+/g, " ").trim().slice(0, 40);
}

function keyFor(tag, attrs, context) {
  if (attrs.id && !isDynamic(attrs.id)) return `#${attrs.id}`;
  const dataNames = Object.keys(attrs).filter((name) => name.startsWith("data-") && !NON_HOOK_DATA.has(name));
  if (dataNames.length) return `[${dataNames[0]}]`;
  if (attrs.name && !isDynamic(attrs.name)) return `[name=${attrs.name}]`;
  const classes = String(attrs.class || "")
    .replace(/\$\{[^}]*\}|\{\{[^}]*\}\}/g, " ")
    .split(/\s+/)
    .filter((c) => c && !GENERIC_CLASSES.has(c) && /^[a-z][\w-]*$/i.test(c));
  if (classes.length) return `.${classes[0]}`;
  const label = context.text || attrs["aria-label"] || attrs.type || "";
  return `${tag}"${label}"@${context.ancestor || "(root)"}`;
}

function isControl(tag, attrs) {
  if (!CONTROL_TAGS.has(tag)) return false;
  if (tag === "input" && String(attrs.type || "").toLowerCase() === "hidden") return false;
  if (tag === "a") {
    // Links that act as controls: buttons styled as links, or links with ids.
    return Boolean(attrs.id) || /\bbutton\b/.test(String(attrs.class || "")) || attrs.role === "button";
  }
  return true;
}

function scanTemplate(relPath, source) {
  const text = stripJinja(source);
  const controls = [];
  const stack = [];
  const re = /<(\/?)([a-zA-Z][\w-]*)\b((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
  let m;
  while ((m = re.exec(text))) {
    const closing = m[1] === "/";
    const tag = m[2].toLowerCase();
    if (closing) {
      const at = stack.map((e) => e.tag).lastIndexOf(tag);
      if (at >= 0) stack.length = at;
      continue;
    }
    const attrs = parseAttributes(m[3]);
    if (isControl(tag, attrs)) {
      const ancestor = [...stack].reverse().find((e) => e.id)?.id;
      controls.push({
        key: keyFor(tag, attrs, { text: textAfter(text, re.lastIndex), ancestor: ancestor ? `#${ancestor}` : null }),
        tag,
        file: relPath,
        line: lineOf(text, m.index),
      });
    }
    const selfClosing = /\/\s*$/.test(m[3]);
    if (!VOID_TAGS.has(tag) && !selfClosing && tag !== "script" && tag !== "style") {
      stack.push({ tag, id: attrs.id && !isDynamic(attrs.id) ? attrs.id : null });
    }
    if (tag === "script" || tag === "style") {
      const end = text.indexOf(`</${tag}`, re.lastIndex);
      if (end > 0) re.lastIndex = end;
    }
  }
  return controls;
}

function scanScript(relPath, source) {
  const controls = [];
  // 1. HTML tags inside template literals or strings.
  const tagRe = /<(button|select|input|textarea|details|dialog|a)\b((?:[^>"'`$]|\$(?!\{)|"[^"]*"|'[^']*'|\$\{[^}]*\})*)>/g;
  let m;
  while ((m = tagRe.exec(source))) {
    const tag = m[1].toLowerCase();
    const attrs = parseAttributes(m[2].replace(/\$\{[^}]*\}/g, (s) => (s.includes('"') ? " " : s)));
    if (!isControl(tag, attrs)) continue;
    controls.push({
      key: keyFor(tag, attrs, { text: textAfter(source, tagRe.lastIndex), ancestor: relPath }),
      tag,
      file: relPath,
      line: lineOf(source, m.index),
    });
  }
  // 1b. Delegated click hooks: handlers that look up `closest("[data-x]")`
  // make every element carrying data-x a control, however it was rendered.
  const delegatedRe = /closest\??\.?\(\s*["'`]\[(data-[\w-]+)/g;
  while ((m = delegatedRe.exec(source))) {
    controls.push({ key: `[${m[1]}]`, tag: "delegated", file: relPath, line: lineOf(source, m.index) });
  }
  // 2. document.createElement("button" | "select" | ...).
  const createRe = /(?:const|let|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*(?:[^;\n]*\|\|\s*)?(?:\(?[^;\n]*\?\s*)?document\.createElement\(\s*["'](button|select|input|textarea|details|dialog)["']\s*\)/g;
  while ((m = createRe.exec(source))) {
    const variable = m[1];
    const tag = m[2];
    const window = source.slice(createRe.lastIndex, createRe.lastIndex + 1600);
    const escaped = variable.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const id = new RegExp(`\\b${escaped}\\.id\\s*=\\s*["'\`]([\\w-]+)["'\`]`).exec(window);
    const data = new RegExp(`\\b${escaped}\\.dataset\\.([A-Za-z]+)\\s*=`).exec(window)
      || new RegExp(`\\b${escaped}\\.setAttribute\\(\\s*["'](data-[\\w-]+)["']`).exec(window);
    const cls = new RegExp(`\\b${escaped}\\.className\\s*=\\s*[\`"']([\\w-]+)`).exec(window)
      || new RegExp(`\\b${escaped}\\.classList\\.add\\(\\s*["']([\\w-]+)["']`).exec(window);
    const type = new RegExp(`\\b${escaped}\\.type\\s*=\\s*["']([\\w-]+)["']`).exec(window);
    let key;
    if (id) key = `#${id[1]}`;
    else if (data) key = `[${data[1].startsWith("data-") ? data[1] : `data-${data[1].replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`}]`;
    else if (cls && !GENERIC_CLASSES.has(cls[1])) key = `.${cls[1]}`;
    else key = `${tag}"${type ? type[1] : ""}"@${relPath}`;
    if (tag === "input" && type && type[1] === "hidden") continue;
    controls.push({ key, tag, file: relPath, line: lineOf(source, m.index) });
  }
  return controls;
}

function scanSources(root, sources = UI_SOURCES) {
  const controls = [];
  for (const rel of sources.templates) {
    controls.push(...scanTemplate(rel, fs.readFileSync(path.join(root, rel), "utf8")));
  }
  for (const rel of sources.scripts) {
    controls.push(...scanScript(rel, fs.readFileSync(path.join(root, rel), "utf8")));
  }
  return controls;
}

// UI source files that exist on disk; the spec compares this with UI_SOURCES.
function discoverUiSourceFiles(root) {
  const found = [];
  const dirs = [
    ["app/templates", ".html"], ["admin_service/templates", ".html"], ["history_service/templates", ".html"],
    ["app/static", ".js"], ["admin_service/static", ".js"], ["history_service/static", ".js"],
  ];
  for (const [dir, ext] of dirs) {
    const abs = path.join(root, dir);
    if (!fs.existsSync(abs)) continue;
    for (const name of fs.readdirSync(abs)) if (name.endsWith(ext)) found.push(`${dir}/${name}`);
  }
  return found.sort();
}

function coverageReport(controls, inventory) {
  const covered = new Map();
  for (const state of inventory.states || []) {
    for (const key of state.covers || []) {
      if (!covered.has(key)) covered.set(key, []);
      covered.get(key).push(state.id);
    }
  }
  const ignored = new Map((inventory.ignored_controls || []).map((entry) => [entry.key, entry]));
  const sourceKeys = new Set(controls.map((c) => c.key));
  const uncovered = [];
  const seen = new Set();
  for (const control of controls) {
    if (covered.has(control.key) || ignored.has(control.key) || seen.has(control.key)) continue;
    seen.add(control.key);
    uncovered.push(control);
  }
  const runtimeOnly = new Set(inventory.runtime_controls || []);
  const stale = [...covered.keys(), ...ignored.keys()].filter((key) => !sourceKeys.has(key) && !runtimeOnly.has(key));
  return { uncovered, stale: [...new Set(stale)].sort(), total: sourceKeys.size };
}

function formatUncovered(uncovered) {
  return uncovered
    .map((c) => `  ${c.key}  (${c.tag} at ${c.file}:${c.line})`)
    .join("\n");
}

module.exports = {
  UI_SOURCES,
  scanTemplate,
  scanScript,
  scanSources,
  discoverUiSourceFiles,
  coverageReport,
  formatUncovered,
};
