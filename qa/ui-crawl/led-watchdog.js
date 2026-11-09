#!/usr/bin/env node
"use strict";

// Independent identify-light watchdog for the UI coverage crawler (#1117).
//
// Started detached, in its own session, by scripts/ui_crawl.js before any
// IDENTIFY can leave the browser (the ledger refuses to arm without a live
// heartbeat from this process). It shares nothing with the crawler but the
// ledger file and the target URL, so it keeps working when the crawler is
// SIGKILLed, crashes or hangs:
//
//   - every poll it writes a heartbeat next to the ledger, stamped with the
//     monotonic clock and the boot id (the crawler and its supervisor judge
//     liveness by that stamp, never by the wall clock);
//   - a lease whose expiry has passed (judged on the monotonic clock, so a
//     backward wall-clock step or an edited ISO deadline cannot extend it), or
//     whose owner PID has gone, is turned off out of band (CLEAR on the exact
//     system/enclosure/bay, then a fresh inventory read must show it off).
//     Identify time is therefore capped by the lease even while the crawler
//     is alive but stuck;
//   - a cleared lease whose owner is still alive stays in the ledger (the
//     crawler disarms it after its own verified CLEAR) and is re-checked, so
//     an IDENTIFY still in flight when the lease expired cannot leave the
//     light on;
//   - once the owner is gone, each remaining lease is cleared, re-verified
//     after a settle period (a request the dead crawler left in flight may
//     land late), and only then removed;
//   - it exits when the owner is gone and the ledger holds nothing of its
//     owner's, writing led-watchdog.json in --out (exit 0), or after
//     --max-life-ms with leases it could not verify off (exit 3; the runner's
//     sweep and the next startup sweep take over).
//
//   node qa/ui-crawl/led-watchdog.js --ledger FILE --owner PID --out DIR
//        [--poll-ms 1000] [--settle-ms 15000] [--recheck-ms 5000]
//        [--verify-ms 45000] [--max-life-ms 1800000]

const fs = require("node:fs");
const path = require("node:path");
const { LedLedger, clearAndVerify, readIdentify, pidAlive, targetOf, writeDurable, monoNow, bootId, leaseExpired, keyOf } = require("./ledger");

function parseArgs(argv) {
  const args = { pollMs: 1000, settleMs: 15_000, recheckMs: 5000, verifyMs: 45_000, maxLifeMs: 30 * 60_000 };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    const next = () => argv[++i];
    if (flag === "--ledger") args.ledger = next();
    else if (flag === "--owner") args.owner = Number(next());
    else if (flag === "--out") args.out = next();
    else if (flag === "--poll-ms") args.pollMs = Number(next());
    else if (flag === "--settle-ms") args.settleMs = Number(next());
    else if (flag === "--recheck-ms") args.recheckMs = Number(next());
    else if (flag === "--verify-ms") args.verifyMs = Number(next());
    else if (flag === "--max-life-ms") args.maxLifeMs = Number(next());
    else throw new Error(`unknown argument ${flag}`);
  }
  if (!args.ledger || !Number.isInteger(args.owner) || args.owner <= 0 || !args.out) throw new Error("--ledger, --owner and --out are required");
  return args;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function run(args) {
  const ledger = new LedLedger(args.ledger);
  const receipt = { pid: process.pid, owner: args.owner, started: new Date().toISOString(), cleared: [], problems: [] };
  const started = monoNow();
  // Per lease (keyed by target): when it was last cleared and verified off.
  const handled = new Map();
  let ownerGoneAt = null;
  const beat = () => {
    try {
      writeDurable(ledger.watchdogFile, JSON.stringify({ pid: process.pid, ownerPid: args.owner, bootId: bootId(), heartbeatMono: monoNow(), heartbeatAt: new Date().toISOString() }));
    } catch (error) {
      receipt.problems.push(`heartbeat: ${error.code || error.message}`);
    }
  };
  const save = (extra = {}) => {
    try {
      fs.mkdirSync(args.out, { recursive: true, mode: 0o700 });
      fs.writeFileSync(path.join(args.out, "led-watchdog.json"), JSON.stringify({ ...receipt, ownerGoneMs: ownerGoneAt === null ? null : ownerGoneAt - started, ...extra, at: new Date().toISOString() }, null, 1));
    } catch {
      // the exit status still carries the result
    }
  };
  const verifyOptions = { verifyMs: args.verifyMs, pollMs: Math.min(3000, Math.max(100, Math.floor(args.verifyMs / 10))) };
  // The heartbeat runs on its own timer, so a long out-of-band CLEAR never
  // makes the watchdog look dead to the crawler.
  beat();
  const heartbeat = setInterval(beat, Math.max(50, args.pollMs));
  try {
    return await loop();
  } finally {
    clearInterval(heartbeat);
  }

  async function loop() {
    for (;;) {
      const now = monoNow();
      const ownerAlive = pidAlive(args.owner);
      if (!ownerAlive && ownerGoneAt === null) ownerGoneAt = now;
      let entries = [];
      try {
        entries = ledger.entries();
      } catch (error) {
        receipt.problems.push(`ledger unreadable: ${String(error.message).slice(0, 120)}`);
      }
      // Leases of this owner, and any lease whose owner is dead (an earlier run).
      const mine = entries.filter((entry) => entry.ownerPid === args.owner || !pidAlive(entry.ownerPid));
      for (const entry of mine) {
        const key = keyOf(entry);
        const expired = leaseExpired(entry, { mono: now });
        const ownerGone = entry.ownerPid === args.owner ? !ownerAlive : true;
        const last = handled.get(key);
        if (!expired && !ownerGone) continue;
        const reason = ownerGone ? "owner-gone" : "lease-expired";
        if (!last) {
          // Tell the crawler's supervisor this lease is being cleared (a slow
          // CLEAR + verify is not an overrun).
          try {
            ledger.update(targetOf(entry), { watchdogClearingAt: new Date().toISOString() });
          } catch {
            // the clear goes ahead regardless
          }
          const outcome = await clearAndVerify(entry, verifyOptions);
          handled.set(key, { at: monoNow(), verified: outcome.verified });
          receipt.cleared.push({ slot: entry.slot, system_id: entry.system_id, enclosure_id: entry.enclosure_id, reason, verified: outcome.verified, detail: outcome.detail, at: new Date().toISOString() });
          if (outcome.verified) ledger.update(targetOf(entry), { watchdogClearedAt: new Date().toISOString() });
          save();
          continue;
        }
        // Already cleared once: re-check periodically; a late IDENTIFY is cleared again.
        if (monoNow() - last.at < (ownerGone ? Math.min(args.recheckMs, args.settleMs) : args.recheckMs)) continue;
        const read = await readIdentify(entry);
        if (read.off !== true) {
          const outcome = await clearAndVerify(entry, verifyOptions);
          handled.set(key, { at: monoNow(), verified: outcome.verified, settledFrom: outcome.verified ? monoNow() : null });
          receipt.cleared.push({ slot: entry.slot, system_id: entry.system_id, enclosure_id: entry.enclosure_id, reason: `${reason}-again`, verified: outcome.verified, detail: outcome.detail, at: new Date().toISOString() });
          save();
          continue;
        }
        handled.set(key, { ...last, at: monoNow(), verified: true, settledFrom: last.settledFrom || last.at });
        // With the owner gone, a lease that stayed off for the settle period is done.
        if (ownerGone && monoNow() - (last.settledFrom || last.at) >= args.settleMs) {
          ledger.disarm(targetOf(entry));
          handled.delete(key);
        }
      }
      if (!ownerAlive) {
        let left = [];
        try {
          left = ledger.entries().filter((entry) => entry.ownerPid === args.owner || !pidAlive(entry.ownerPid));
        } catch {
          left = [{}];
        }
        if (!left.length) {
          save({ ok: receipt.cleared.every((item) => item.verified || receipt.cleared.some((later) => later.slot === item.slot && later.verified)), remaining: 0, finished: new Date().toISOString() });
          return 0;
        }
        if (monoNow() - started > args.maxLifeMs) {
          save({ ok: false, remaining: left.length, finished: new Date().toISOString() });
          return 3;
        }
      }
      await sleep(args.pollMs);
    }
  }
}

if (require.main === module) {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exit(2);
  }
  // Never stopped by the crawler's terminal or process group.
  for (const signal of ["SIGHUP", "SIGINT"]) process.on(signal, () => {});
  run(args).then((code) => process.exit(code), (error) => {
    process.stderr.write(`led watchdog failed: ${String(error && error.message ? error.message : error).slice(0, 200)}\n`);
    process.exit(3);
  });
}

module.exports = { parseArgs, run };
