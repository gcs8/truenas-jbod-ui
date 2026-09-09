"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const TEMPLATE_PATH = path.resolve(__dirname, "../../admin_service/templates/index.html");
const SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");
const TEMPLATE = fs.readFileSync(TEMPLATE_PATH, "utf8");

function functionSource(name) {
  const start = SOURCE.indexOf(`async function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = SOURCE.indexOf("{", SOURCE.indexOf(")", start));
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < SOURCE.length; index += 1) {
    const character = SOURCE[index];
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
      if (depth === 0) return SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

test("backup import inspects, confirms observed mode, then imports with the single-use receipt", () => {
  const source = functionSource("runImportBackup");
  const inspect = source.indexOf("/api/admin/backup/inspect");
  const confirm = source.indexOf("window.confirm");
  const importRequest = source.indexOf("/api/admin/backup/import");

  assert.ok(inspect >= 0, "inspection request is required");
  assert.ok(confirm > inspect, "operator confirmation must follow inspection");
  assert.ok(importRequest > confirm, "import must follow confirmation");
  assert.match(source, /inspection\.encryption_mode/);
  assert.match(source, /inspection\.inspection_receipt/);
  assert.match(source, /if \(!confirmed\)\s*\{\s*return;/);
  assert.match(source, /"X-Backup-Expected-Encryption": inspection\.encryption_mode/);
  assert.match(source, /"X-Backup-Inspection-Receipt": inspection\.inspection_receipt/);
});

test("restore form explains mandatory inspection and observed encryption confirmation", () => {
  assert.match(TEMPLATE, /inspect[^.]+observed encryption mode/i);
  assert.match(TEMPLATE, /confirm the archive before replacing/i);
  assert.doesNotMatch(TEMPLATE, /single-use inspection receipt|without pretending/i);
  assert.match(TEMPLATE, /<h1>Admin<\/h1>/);
  assert.match(TEMPLATE, /Support archive for offline inspection, not restorable/i);
  assert.match(TEMPLATE, /LAN.*auto-stops/i);
});
