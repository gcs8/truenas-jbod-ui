"use strict";

// #459: the main page talks about bays, disks and saved copies, not about how
// the data was gathered. These checks read the template's visible text and the
// status/label strings in app.js and reject the internal vocabulary the issue
// listed, so a new string cannot quietly bring it back.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

const INTERNAL_WORDS = /\b(enrichment|evidence|calibrat\w*|scope|scoped|artifact|sanitized?|rollups?|payload|middleware|gmultipath)\b/i;

function templateVisibleText(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/\{#[\s\S]*?#\}/g, " ")
    .replace(/\{%[\s\S]*?%\}/g, " ")
    .replace(/\{\{[\s\S]*?\}\}/g, " ")
    .replace(/<[^>]+>/g, "\n")
    .split("\n")
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean);
}

function templateAttributeText(html) {
  const values = [];
  const pattern = /\b(?:title|placeholder|aria-label|label)="([^"{}]*)"/g;
  let match;
  while ((match = pattern.exec(html))) {
    values.push(match[1]);
  }
  return values;
}

// String arguments of setStatus(...) plus optgroup labels: the messages a user
// reads in the status line and the selector.
function appStatusStrings(source) {
  const strings = [];
  const status = /setStatus\(\s*(`(?:[^`\\]|\\.)*`|"(?:[^"\\]|\\.)*")/g;
  let match;
  while ((match = status.exec(source))) {
    strings.push(match[1].slice(1, -1).replace(/\$\{[^}]*\}/g, "X"));
  }
  const optgroup = /<optgroup label="([^"$]*)"/g;
  while ((match = optgroup.exec(source))) {
    strings.push(match[1]);
  }
  return strings;
}

test("main page template text avoids internal vocabulary", () => {
  const offending = [...templateVisibleText(TEMPLATE), ...templateAttributeText(TEMPLATE)]
    .filter((text) => INTERNAL_WORDS.test(text));
  assert.deepEqual(offending, []);
});

test("status lines and selector groups avoid internal vocabulary", () => {
  const strings = appStatusStrings(APP_SOURCE);
  assert.ok(strings.length > 40, `expected to read the status strings, found ${strings.length}`);
  const offending = strings.filter((text) => INTERNAL_WORDS.test(text));
  assert.deepEqual(offending, []);
});

test("locate-light, offline-copy and restore wording replaces the old labels", () => {
  for (const removed of [
    "Identify On",
    "Identify Off / Clear",
    "Export Snapshot",
    "Export Enclosure Snapshot",
    "LED action",
    "into the active scope",
    "Frozen offline snapshot loaded",
    "Live Enclosures",
    "Saved Chassis Views",
    "Virtual Storage Views",
    "Force ZIP packaging",
    "Current Choice",
  ]) {
    assert.ok(!APP_SOURCE.includes(removed) && !TEMPLATE.includes(removed), `"${removed}" should be gone`);
  }
  assert.match(TEMPLATE, /data-led-action="IDENTIFY">Locate light on</);
  assert.match(TEMPLATE, /data-led-action="CLEAR">Locate light off</);
  assert.match(TEMPLATE, /id="export-snapshot-button"[^>]*>Save offline copy</);
});

test("export enclosure rows show the bay count, not the profile id", () => {
  assert.doesNotMatch(APP_SOURCE, /enclosure\.profile_id \|\| ""\]/);
});
