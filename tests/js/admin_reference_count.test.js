"use strict";

// #436: no top-level function in the admin scripts is left unreferenced, and
// every key in admin.js's `state` object is read somewhere (directly, or by
// name through a `state[config.someKey]` lookup).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "../..");
const FILES = ["admin_service/static/admin.js", "admin_service/static/admin_backups.js"];

function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

function escapeRegExp(text) {
  return text.replace(/[$]/g, "\\$");
}

function referenceCount(code, name) {
  return (code.match(new RegExp(`(?<![\\w$])${escapeRegExp(name)}(?![\\w$])`, "g")) || []).length;
}

for (const file of FILES) {
  test(`${file} has no unreferenced functions`, () => {
    const code = stripComments(fs.readFileSync(path.join(ROOT, file), "utf8"));
    const names = [...code.matchAll(/^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)].map((match) => match[1]);
    assert.ok(names.length > 20, "the scan must find the script's functions");
    const unreferenced = names.filter((name) => referenceCount(code, name) < 2);
    assert.deepEqual(unreferenced, []);
  });
}

test("every admin.js state key is read", () => {
  const code = stripComments(fs.readFileSync(path.join(ROOT, FILES[0]), "utf8"));
  const block = code.match(/const state = \{([\s\S]*?)\n  \};/);
  assert.ok(block, "admin.js must declare its state object");
  const keys = [...block[1].matchAll(/^ {4}([A-Za-z_$][\w$]*):/gm)].map((match) => match[1]);
  assert.ok(keys.length > 20, "the scan must find the state keys");
  const unread = keys.filter((key) => {
    const name = escapeRegExp(key);
    const directRead = new RegExp(`state\\.${name}(?![\\w$])(?!\\s*=(?!=))`).test(code);
    const lookupByName = new RegExp(`["'\`]${name}["'\`]`).test(code) && /state\[config\.\w+\]/.test(code);
    return !directRead && !lookupByName;
  });
  assert.deepEqual(unread, []);
});

test("every admin.js request has a timeout; backup transfers get the long one", () => {
  const code = stripComments(fs.readFileSync(path.join(ROOT, FILES[0]), "utf8"));
  const rawFetches = [...code.matchAll(/(?<![\w$.])fetch\(/g)];
  assert.equal(rawFetches.length, 1, "only fetchWithTimeout may call fetch directly");
  const wrapper = code.indexOf("async function fetchWithTimeout(");
  const wrapperEnd = code.indexOf("\n  }\n", wrapper);
  assert.ok(rawFetches[0].index > wrapper && rawFetches[0].index < wrapperEnd);
  for (const route of ["/api/admin/backup/export", "/api/admin/debug/export", "/api/admin/backup/inspect", "/api/admin/backup/import"]) {
    const at = code.indexOf(route);
    assert.ok(at > 0, `${route} must be requested`);
    const call = code.slice(code.lastIndexOf("fetchWithTimeout(", at), at + 400);
    assert.match(call, /timeoutMs: BACKUP_TRANSFER_TIMEOUT_MS/, `${route} needs the long transfer timeout`);
  }
  assert.match(code, /const BACKUP_TRANSFER_TIMEOUT_MS = 30 \* 60 \* 1000;/);
});
