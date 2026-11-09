"use strict";

// The crawler's own watch on its identify watchdog (#1117, round 4).
//
// The independent watchdog (led-watchdog.js) caps identify time when the
// crawler dies or hangs. This covers the opposite failure: the watchdog dies
// (or stops heartbeating) while the crawler is alive, possibly stuck. It runs
// in a worker thread, so it keeps working while the crawler's main thread is
// blocked, and it shares nothing with the crawler but the ledger file:
//
//   - every tick it records its own liveness (the ledger refuses to arm an
//     IDENTIFY unless this supervisor is running);
//   - if the watchdog is not alive and heartbeating (its PID is gone or a
//     zombie, or its monotonic heartbeat is stale, or from another boot), it
//     turns every lease of this crawler off itself (CLEAR on the exact
//     system/enclosure/bay, verified off), keeps re-checking them (an
//     IDENTIFY already in flight can land late), and reports the loss once;
//     the crawler then fails. Leases stay in the ledger for the crawler's
//     final verified sweep; no new IDENTIFY can be armed without a watchdog;
//   - if the watchdog is alive but has not started clearing a lease of this
//     crawler `graceMs` past its monotonic cap, it clears that lease too and
//     reports the overrun (the crawler fails).
//
// Expiry and heartbeat age are judged on the monotonic clock (ledger.js), so a
// wall-clock step in either direction changes nothing here.
//
//   const supervisor = await startLedSupervisor({ ledger, onLost });
//   // ledger.arm() now also requires supervisor.alive()

const path = require("node:path");
const { Worker, isMainThread, parentPort, workerData } = require("node:worker_threads");

const DEFAULT_INTERVAL_MS = 250;
const DEFAULT_GRACE_MS = 3000;

// ------------------------------------------------------------- main thread

async function startLedSupervisor({ ledger, intervalMs = DEFAULT_INTERVAL_MS, graceMs = DEFAULT_GRACE_MS, verifyMs = 45_000, onLost = () => {} }) {
  const { monoNow } = require("./ledger");
  // Slot 0: the worker's last tick (monotonic ms). Read synchronously by arm().
  const ticks = new Float64Array(new SharedArrayBuffer(8));
  const worker = new Worker(__filename, {
    workerData: { file: ledger.file, ownerPid: process.pid, intervalMs, graceMs, verifyMs, ticks: ticks.buffer },
  });
  worker.unref();
  const events = [];
  let exited = false;
  let reported = false;
  const report = (event) => {
    events.push(event);
    if (!reported) {
      reported = true;
      Promise.resolve().then(() => onLost(event)).catch(() => {});
    }
  };
  worker.on("message", (message) => {
    if (message && message.type) report(message);
  });
  worker.on("error", (error) => report({ type: "supervisor-failed", error: String(error && error.message ? error.message : error).slice(0, 200) }));
  worker.on("exit", () => {
    exited = true;
  });
  const supervisor = {
    events,
    // Running and ticking: a worker that stopped ticking is not watching.
    alive: () => !exited && monoNow() - ticks[0] < Math.max(2000, intervalMs * 8),
    async stop() {
      if (ledger.supervisor === supervisor) ledger.supervisor = null;
      await worker.terminate().catch(() => {});
    },
  };
  // Wait for the first tick, so an IDENTIFY right after this call is admitted.
  const deadline = monoNow() + 10_000;
  while (!(ticks[0] > 0)) {
    if (exited || monoNow() > deadline) throw new Error("the identify watchdog supervisor did not start; no IDENTIFY will be sent");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  ledger.supervisor = supervisor;
  return supervisor;
}

// ------------------------------------------------------------ worker thread

async function supervise() {
  const { LedLedger, clearAndVerify, readIdentify, leaseExpired, leaseCapMs, monoNow, keyOf } = require(path.join(__dirname, "ledger"));
  const { file, ownerPid, intervalMs, graceMs, verifyMs } = workerData;
  const ticks = new Float64Array(workerData.ticks);
  const ledger = new LedLedger(file);
  const verifyOptions = { verifyMs, pollMs: Math.min(3000, Math.max(100, Math.floor(verifyMs / 10))), attempts: 3 };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  // Ticks run on their own timer, so a long CLEAR never makes this look dead.
  ticks[0] = monoNow();
  setInterval(() => { ticks[0] = monoNow(); }, Math.max(20, Math.floor(intervalMs / 2)));
  let lost = null;
  const cleared = new Map(); // lease key -> { at, verified }
  const clear = async (entry, reason) => {
    const outcome = await clearAndVerify(entry, verifyOptions);
    cleared.set(keyOf(entry), { at: monoNow(), verified: outcome.verified });
    try {
      if (outcome.verified) ledger.update({ origin: entry.origin, path: entry.path, system_id: entry.system_id, enclosure_id: entry.enclosure_id }, { supervisorClearedReason: reason });
    } catch {
      // the ledger is re-read every tick
    }
    return { slot: entry.slot, system_id: entry.system_id, enclosure_id: entry.enclosure_id, reason, verified: outcome.verified, detail: outcome.detail };
  };
  for (;;) {
    let entries = [];
    try {
      entries = ledger.entries().filter((entry) => entry.ownerPid === ownerPid);
    } catch {
      entries = [];
    }
    const watchdogAlive = ledger.watchdogAlive(ownerPid);
    if (!watchdogAlive) {
      if (!lost) lost = { at: monoNow(), results: [] };
      const results = [];
      for (const entry of entries) {
        const last = cleared.get(keyOf(entry));
        if (!last || !last.verified) results.push(await clear(entry, "watchdog-lost"));
        else if (monoNow() - last.at >= Math.max(500, intervalMs * 4)) {
          // Re-check: an IDENTIFY in flight when the watchdog died may land late.
          const read = await readIdentify(entry, { requestMs: verifyOptions.verifyMs });
          if (read.off !== true) results.push(await clear(entry, "watchdog-lost-again"));
          else cleared.set(keyOf(entry), { at: monoNow(), verified: true });
        }
      }
      if (!lost.reported) {
        lost.reported = true;
        parentPort.postMessage({ type: "watchdog-lost", detail: "the identify watchdog stopped heartbeating; this crawler turned its identify lights off and fails", results });
      } else if (results.length) {
        parentPort.postMessage({ type: "watchdog-lost-again", results });
      }
    } else {
      const mono = monoNow();
      for (const entry of entries) {
        if (entry.watchdogClearingAt || entry.watchdogClearedAt || cleared.has(keyOf(entry))) continue;
        // The watchdog had the cap plus a grace period to clear it.
        if (leaseExpired(entry, { mono }) && (!Number.isFinite(entry.armedMono) || mono - entry.armedMono >= leaseCapMs(entry) + graceMs)) {
          const result = await clear(entry, "lease-overrun");
          parentPort.postMessage({ type: "lease-overrun", detail: "an identify lease outlived its cap with the watchdog alive; this crawler cleared it and fails", results: [result] });
        }
      }
    }
    await sleep(intervalMs);
  }
}

if (!isMainThread && workerData && workerData.ticks) {
  supervise().catch((error) => {
    // Unexpected: say so; the main thread's alive() goes false when ticks stop.
    parentPort.postMessage({ type: "supervisor-failed", error: String(error && error.message ? error.message : error).slice(0, 200) });
    process.exit(1);
  });
}

module.exports = { startLedSupervisor, DEFAULT_INTERVAL_MS, DEFAULT_GRACE_MS };
