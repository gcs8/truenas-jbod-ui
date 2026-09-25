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
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
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

function extractFunction(code, name) {
  const start = code.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0, `function ${name} must exist`);
  const bodyStart = code.indexOf("{", code.indexOf(")", start));
  let depth = 0;
  for (let index = bodyStart; index < code.length; index += 1) {
    if (code[index] === "{") depth += 1;
    if (code[index] === "}") {
      depth -= 1;
      if (depth === 0) return code.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

test("fetchWithTimeout keeps the timer running while readBody reads a stalled body", async () => {
  const vm = require("node:vm");
  const code = fs.readFileSync(path.join(ROOT, FILES[0]), "utf8");
  let bodyAborted = false;
  const context = vm.createContext({
    AbortController,
    setTimeout,
    clearTimeout,
    Math,
    Number,
    DEFAULT_REQUEST_TIMEOUT_MS: 60000,
    // Headers arrive at once; the body never finishes unless the request is aborted.
    fetch: async (_url, { signal }) => ({
      ok: true,
      blob: () => new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => {
          bodyAborted = true;
          reject(new DOMException("aborted", "AbortError"));
        });
      }),
    }),
  });
  vm.runInContext(
    `${extractFunction(code, "requestTimeoutError")}\n${extractFunction(code, "fetchWithTimeout")}\nglobalThis.fetchWithTimeout = fetchWithTimeout;`,
    context
  );
  await assert.rejects(
    context.fetchWithTimeout("/api/admin/backup/export", { timeoutMs: 20, readBody: (response) => response.blob() }),
    (error) => error.timedOut === true && /Timed out after 1 second/.test(error.message)
  );
  assert.equal(bodyAborted, true, "the stalled download must be aborted");
});

test("a restore that times out after upload is reported as unknown, not failed", () => {
  const code = fs.readFileSync(path.join(ROOT, FILES[0]), "utf8");
  const source = extractFunction(code, "runImportBackup");
  const dispatched = source.indexOf("importDispatched = true;");
  assert.ok(dispatched > source.indexOf("window.confirm"), "the flag is set only once the restore is sent");
  assert.ok(dispatched < source.indexOf("/api/admin/backup/import"));
  const catchBlock = source.slice(source.lastIndexOf("} catch (error) {"));
  const unknownBranch = catchBlock.indexOf("if (importDispatched && error?.timedOut)");
  assert.ok(unknownBranch >= 0 && unknownBranch < catchBlock.indexOf("Import failed:"));
  assert.match(catchBlock, /It is unknown whether the restore from \$\{file\.name\} finished\./);
  assert.match(catchBlock, /before restoring again/);
});
