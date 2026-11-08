"use strict";

// Child process for tests/js/ui_crawl_safety.test.js (#1117): arms one
// identify light through the same ledger and exit guards the crawler CLI
// uses, turns it on against a local fake appliance, then hangs until the test
// interrupts it (SIGTERM: cleanup must run; SIGKILL: the next sweep must).

const path = require("node:path");

const root = path.resolve(__dirname, "..", "..", "..");
const { createExitGuards } = require(path.join(root, "scripts/ui_crawl"));
const { LedLedger, armedEntry } = require(path.join(root, "qa/ui-crawl/ledger"));

const [origin, ledgerPath, outDir] = process.argv.slice(2);
const ledger = new LedLedger(ledgerPath);
const guards = createExitGuards({ ledger, outDir, sweepOptions: { verifyMs: 3000, pollMs: 200 } }).install();

(async () => {
  // Like the CLI: a startup sweep on the (empty) ledger runs first. Cleanup at
  // exit must still see the light armed after it (Codex #1108 P1).
  await guards.sweep();
  const url = `${origin}/api/slots/5/led?system_id=sys-a&enclosure_id=enc-1`;
  ledger.arm(armedEntry(url, "main.led"));
  await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "IDENTIFY" }) });
  process.stdout.write("ARMED\n");
  setInterval(() => {}, 1000);
})();
