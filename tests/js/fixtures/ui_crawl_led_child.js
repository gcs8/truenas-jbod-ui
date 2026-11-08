"use strict";

// Child process for tests/js/ui_crawl_safety.test.js and ui_crawl_r3.test.js
// (#1117): starts the independent identify watchdog the way the crawler CLI
// does, arms one identify lease through the same ledger and exit guards,
// turns the light on against a local fake appliance, then hangs until the test
// interrupts it (SIGTERM: in-process cleanup must run; SIGKILL: the watchdog
// must clear the light within the lease cap without this process).

const path = require("node:path");

const root = path.resolve(__dirname, "..", "..", "..");
const { createExitGuards, startLedWatchdog } = require(path.join(root, "scripts/ui_crawl"));
const { LedLedger, armedEntry } = require(path.join(root, "qa/ui-crawl/ledger"));

const [origin, ledgerPath, outDir] = process.argv.slice(2);
const leaseMs = 1500;
const ledger = new LedLedger(ledgerPath, { leaseMs });
const guards = createExitGuards({ ledger, outDir, sweepOptions: { verifyMs: 3000, pollMs: 200 } }).install();

(async () => {
  // Like the CLI: a startup sweep on the (empty) ledger runs first. Cleanup at
  // exit must still see the light armed after it (Codex #1108 P1).
  await guards.sweep();
  const watchdog = await startLedWatchdog({ ledger, outDir, pollMs: 100, settleMs: 800, verifyMs: 3000 });
  process.stdout.write(`WATCHDOG ${watchdog.pid}\n`);
  const url = `${origin}/api/slots/5/led?system_id=sys-a&enclosure_id=enc-1`;
  ledger.arm(armedEntry(url, "main.led"));
  await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "IDENTIFY" }) });
  process.stdout.write("ARMED\n");
  setInterval(() => {}, 1000);
})();
