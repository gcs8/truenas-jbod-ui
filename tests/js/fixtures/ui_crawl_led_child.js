"use strict";

// Child process for tests/js/ui_crawl_safety.test.js, ui_crawl_r3.test.js and
// ui_crawl_r4.test.js (#1117): starts the independent identify watchdog and
// the in-process watchdog supervisor the way the crawler CLI does, arms one
// identify lease through the same ledger and exit guards, turns the light on
// against a local fake appliance, then hangs until the test interrupts it
// (SIGTERM: in-process cleanup must run; SIGKILL: the watchdog must clear the
// light within the lease cap without this process).
//
//   node ui_crawl_led_child.js ORIGIN LEDGER OUTDIR [hang|block] [blockMs]
//
// `block` models a stuck crawler whose JavaScript thread never yields: after
// ARMED the main thread is blocked for blockMs (default 8000), so nothing on
// it (timers, signal handlers, reaping the watchdog) runs during that time.
// When the supervisor reports the watchdog lost, the child sweeps and exits 2
// (3 when the light could not be verified off), like the CLI's failed crawl.

const path = require("node:path");

const root = path.resolve(__dirname, "..", "..", "..");
const { createExitGuards, startLedWatchdog } = require(path.join(root, "scripts/ui_crawl"));
const { LedLedger, armedEntry } = require(path.join(root, "qa/ui-crawl/ledger"));
const { startLedSupervisor } = require(path.join(root, "qa/ui-crawl/led-supervisor"));

const [origin, ledgerPath, outDir, mode = "hang", blockMs = "8000"] = process.argv.slice(2);
const leaseMs = 1500;
const ledger = new LedLedger(ledgerPath, { leaseMs });
const guards = createExitGuards({ ledger, outDir, sweepOptions: { verifyMs: 3000, pollMs: 200 } }).install();

(async () => {
  // Like the CLI: a startup sweep on the (empty) ledger runs first. Cleanup at
  // exit must still see the light armed after it (Codex #1108 P1).
  await guards.sweep();
  const watchdog = await startLedWatchdog({ ledger, outDir, pollMs: 100, settleMs: 800, verifyMs: 3000 });
  process.stdout.write(`WATCHDOG ${watchdog.pid}\n`);
  await startLedSupervisor({
    ledger, intervalMs: 100, graceMs: 300, verifyMs: 3000,
    onLost: async (event) => {
      process.stdout.write(`LOST ${JSON.stringify(event)}\n`);
      const ledCleanup = await guards.sweep();
      process.exit(ledCleanup && ledCleanup.ok === false ? 3 : 2);
    },
  });
  const url = `${origin}/api/slots/5/led?system_id=sys-a&enclosure_id=enc-1`;
  ledger.arm(armedEntry(url, "main.led"));
  await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "IDENTIFY" }) });
  process.stdout.write("ARMED\n");
  if (mode === "block") {
    // Let the test read ARMED, then never yield for blockMs.
    setTimeout(() => {
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, Number(blockMs));
      process.stdout.write("UNBLOCKED\n");
    }, 50);
  }
  setInterval(() => {}, 1000);
})();
