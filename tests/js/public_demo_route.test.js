"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = APP_SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    if (character === "}") {
      depth -= 1;
      if (depth === 0) return APP_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunction(name, context = {}) {
  const sandbox = vm.createContext({ ...context });
  vm.runInContext(`${functionSource(name)}\nthis.__loaded = ${name};`, sandbox, {
    filename: `${name}.behavior.js`,
  });
  return sandbox.__loaded;
}

test("live Storage Fabric link preserves the server-provided root path", () => {
  const sasFabricViewLink = {};
  const updateSasFabricViewLink = loadFunction("updateSasFabricViewLink", {
    bootstrap: { sasFabricViewUrl: "/truenas-jbod-ui/sas-fabric" },
    state: { snapshotMode: false },
    sasFabricViewLink,
    buildScopedUrl: (url) => `${url}?system_id=demo`,
  });

  updateSasFabricViewLink();

  assert.equal(sasFabricViewLink.href, "/truenas-jbod-ui/sas-fabric?system_id=demo");
});
