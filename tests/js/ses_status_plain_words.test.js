"use strict";

// The enclosure reports each bay as a status name plus four raw bytes, for
// example "Not Installed (0x05 0x00 0x00 0x00)". The page shows plain words and
// keeps the raw text in the Health row's hover title.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = require("./helpers/main_page_source").mainPageSource();

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const bodyStart = source.indexOf("{", source.indexOf(")", start));
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

function loadFunctions(names) {
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(
    `${names.map((name) => functionSource(APP_SOURCE, name)).join("\n")}\nthis.loaded = { ${names.join(", ")} };`,
    sandbox,
  );
  return sandbox.loaded;
}

test("enclosure status bytes read as plain words", () => {
  const { formatSlotHealth } = loadFunctions(["formatSlotHealth"]);

  assert.equal(formatSlotHealth("Not Installed (0x05 0x00 0x00 0x00)"), "Empty bay");
  // Byte 2 bit 1 is the locate light; the LED row reports that, not Health.
  assert.equal(formatSlotHealth("Not Installed (0x05 0x00 0x02 0x00)"), "Empty bay");
  assert.equal(formatSlotHealth("OK (0x01 0x00 0x00 0x00)"), "OK");
  assert.equal(formatSlotHealth("OK (0x11 0x00 0x00 0x00)"), "OK, reseated or replaced");
  assert.equal(
    formatSlotHealth("Critical (0x72 0x00 0x00 0x00)"),
    "Critical, predicted failure, disabled, reseated or replaced",
  );
  assert.equal(formatSlotHealth("<Status 0x9> (0x09 0x00 0x00 0x00)"), "<Status 0x9>");

  // Pool and disk health from the OS, and SES text without bytes, pass through.
  assert.equal(formatSlotHealth("ONLINE"), "ONLINE");
  assert.equal(formatSlotHealth("Not installed"), "Not installed");
  assert.equal(formatSlotHealth(null), null);
  assert.equal(formatSlotHealth(""), "");
});

test("long health text is capped and parsed in linear time", () => {
  const { formatSlotHealth, slotHealthTitle } = loadFunctions(["formatSlotHealth", "slotHealthTitle"]);
  // A status padded with whitespace used to backtrack quadratically through
  // the suffix pattern on every grid render.
  const padded = `OK${" ".repeat(64 * 1024)}x`;
  const started = process.hrtime.bigint();
  const shown = formatSlotHealth(padded);
  const title = slotHealthTitle(padded);
  const tail = formatSlotHealth(`OK${" ".repeat(64 * 1024)}(0x11 0x00 0x00 0x00)`);
  const elapsedMs = Number(process.hrtime.bigint() - started) / 1e6;

  assert.ok(elapsedMs < 250, `took ${elapsedMs.toFixed(1)} ms`);
  assert.ok(shown.length <= 160, `shown ${shown.length} characters`);
  assert.ok(shown.endsWith("\u2026"));
  assert.equal(title, "");
  assert.equal(tail, "OK, reseated or replaced");
  assert.equal(formatSlotHealth("OK  (0x11 0x00 0x00 0x00)  "), "OK, reseated or replaced");
});

test("the raw enclosure status stays reachable as a hover title", () => {
  const { slotHealthTitle } = loadFunctions(["slotHealthTitle"]);

  assert.equal(slotHealthTitle("OK (0x11 0x00 0x00 0x00)"), "Enclosure status: OK (0x11 0x00 0x00 0x00)");
  assert.equal(slotHealthTitle("ONLINE"), "");
  assert.equal(slotHealthTitle(undefined), "");
});

test("SES flags include the swap flag and show on every platform", () => {
  const { formatSesStateValue } = loadFunctions(["formatSesStateValue"]);

  assert.equal(formatSesStateValue({ raw_status: { ses_swapped: true } }), "Reseated or replaced");
  assert.equal(
    formatSesStateValue({ raw_status: { ses_predicted_failure: true, ses_swapped: true } }),
    "Predicted Failure, Reseated or replaced",
  );
  assert.equal(formatSesStateValue({ raw_status: { ses_swapped: false } }), "n/a");

  assert.match(
    APP_SOURCE,
    /kvRowIfMeaningful\("SES Flags", formatSesStateValue\(slot\), false, slot\?\.raw_status\?\.ses_swapped \? SES_SWAP_TITLE : ""\)/,
  );
  assert.doesNotMatch(APP_SOURCE, /showQuantastorContext \? kvRowIfMeaningful\("SES Flags"/);
});

test("every Health display goes through the plain-word formatter", () => {
  assert.doesNotMatch(APP_SOURCE, /Health: \$\{slot\.health\}/);
  assert.doesNotMatch(APP_SOURCE, /"Health", (?:slot|storageViewSlot)\.health\)/);
  assert.doesNotMatch(APP_SOURCE, /String\(slot\.health\)\.toLowerCase\(\)/);
  assert.match(APP_SOURCE, /"Health", formatSlotHealth\(slot\.health\), false, slotHealthTitle\(slot\.health\)\)/);
  assert.match(
    APP_SOURCE,
    /"Health",\s*formatSlotHealth\(storageViewSlot\.health\),\s*false,\s*slotHealthTitle\(storageViewSlot\.health\),?\s*\)/,
  );
});

test("kvRow puts an optional title on the value only", () => {
  const { kvRow } = loadFunctions(["escapeHtml", "kvRow"]);

  assert.match(kvRow("Health", "OK", false, 'Enclosure status: OK "x"'), /<div class="kv-value" title="Enclosure status: OK &quot;x&quot;">OK<\/div>/);
  assert.match(kvRow("Health", "OK"), /<div class="kv-value">OK<\/div>/);
});
