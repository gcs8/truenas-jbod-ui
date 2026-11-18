// The Backups status line for a clean-up that did not finish (#1291). The
// newest-kept-copy guard's refusal already ends a sentence, so the summary
// must not add a second full stop; a reason without one still gets one.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { cleanupSummary } = require("../../admin_service/static/admin_backup_model.js");

// The guard's shipping refusal, taken from the catalogue source so the test
// follows the real wording rather than a copy of it.
function guardRefusal() {
  const source = fs.readFileSync(
    path.join(__dirname, "..", "..", "history_service", "backup_archive", "catalog.py"),
    "utf8",
  );
  const match = source.match(/raise CatalogError\(\s*"(Artifact is the newest copy[^"]*)"\s*"([^"]*so it is kept\.)"\s*\)/);
  assert.ok(match, "the keeper refusal is still raised by the catalogue");
  return `CatalogError: ${match[1]}${match[2]}`;
}

const TARGETS = [];
const AT = "2026-11-18T03:00:00Z";

test("the guard refusal really ends in a full stop (fixture control)", () => {
  assert.match(guardRefusal(), /so it is kept\.$/);
});

test("a refusal for one place ends with exactly one full stop (#1291)", () => {
  const refusal = guardRefusal();
  const summary = cleanupSummary({ at: AT, ok: false, deleted: 0, detail: refusal, failed_locations: { local: refusal } }, TARGETS);
  assert.equal(summary.tone, "is-bad");
  assert.match(summary.text, /didn't finish: /);
  assert.ok(summary.text.endsWith("so it is kept."), summary.text);
  assert.doesNotMatch(summary.text, /\.\.$/);
});

test("a refusal carried only in the detail ends with exactly one full stop (#1291)", () => {
  const refusal = guardRefusal();
  const summary = cleanupSummary({ at: AT, ok: false, deleted: 0, detail: refusal, failed_locations: {} }, TARGETS);
  assert.equal(summary.tone, "is-bad");
  assert.ok(summary.text.endsWith(`didn't finish: ${refusal}`), summary.text);
});

test("a reason that ends in a question or exclamation mark gets no extra full stop", () => {
  for (const detail of ["Is the share mounted?", "Target refused!"]) {
    const summary = cleanupSummary({ at: AT, ok: false, deleted: 0, detail, failed_locations: {} }, TARGETS);
    assert.ok(summary.text.endsWith(`didn't finish: ${detail}`), summary.text);
  }
});

test("a reason without sentence punctuation still gets one full stop (control)", () => {
  const summary = cleanupSummary({ at: AT, ok: false, deleted: 0, detail: "ConnectionRefusedError", failed_locations: {} }, TARGETS);
  assert.ok(summary.text.endsWith("didn't finish: ConnectionRefusedError."), summary.text);
  const empty = cleanupSummary({ at: AT, ok: false, deleted: 0, detail: null, failed_locations: {} }, TARGETS);
  assert.ok(empty.text.endsWith("didn't finish: no detail."), empty.text);
  const trailing = cleanupSummary({ at: AT, ok: false, deleted: 0, detail: "Timed out   ", failed_locations: {} }, TARGETS);
  assert.doesNotMatch(trailing.text, /\s\.$/);
});
