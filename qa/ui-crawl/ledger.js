"use strict";

// Identify-light leases and out-of-band cleanup (#1117).
//
// The request guard is the only place an IDENTIFY can leave the browser. The
// moment it is admitted, before the request is forwarded, the exact target
// (origin, bay path, system_id, enclosure_id) is written to a ledger file as
// a lease: the crawler's PID, the monotonic time it was armed (`armedMono`,
// CLOCK_MONOTONIC via process.hrtime, shared by every process on the host,
// with the kernel boot id it belongs to) and its length (`leaseMs`, capped at
// MAX_LEASE_MS). The write is fsynced, renamed into place and the parent
// directory fsynced, so the entry survives host loss.
//
// Lease expiry is judged on the monotonic clock (`leaseExpired`): a wall-clock
// step backwards, or an edited ISO `expiresAt`, cannot extend it. The
// wall-clock fields are for people; they can only end a lease early. A lease
// with no monotonic stamp, from another boot, or longer than the cap is held
// to the cap or treated as expired.
//
// An IDENTIFY is refused unless (checked on every arm):
//   - an independent watchdog process (led-watchdog.js, started detached in
//     its own session by the CLI) is alive and heartbeating for this crawler:
//     its PID exists and is not a zombie, and its monotonic heartbeat from this
//     boot is fresh (HEARTBEAT_STALE_MS); and
//   - the crawler's own watchdog supervisor (led-supervisor.js, a worker
//     thread, so it runs even while the crawler's main thread is stuck) is
//     running. It watches the watchdog after arming: if the watchdog stops
//     heartbeating it turns every lease of this crawler off itself and the
//     crawl fails; it also clears a lease the watchdog let run past its cap.
// The watchdog does not depend on the crawler surviving: it turns the exact
// bay off and verifies it when the lease expires (identify-on time is capped
// even if the crawler hangs) or the crawler's PID disappears (SIGKILL,
// crash), and keeps re-checking until the owner is gone and the light has
// stayed off for a settle period.
//
// Anything still in the ledger is also cleared out of band (plain HTTP from
// Node, no browser) by:
//
//   - the CLI's `finally` and its SIGTERM/SIGINT/SIGHUP handlers,
//   - a browser disconnect (the crawl stops, then the same cleanup runs),
//   - the startup sweep of the next run (`--led-ledger`): no crawl starts
//     until every lease, expired or not, is clear (host loss of the crawler
//     and its watchdog together),
//   - `scripts/ui_crawl.js --led-sweep`, which the restricted runner calls
//     after every write chunk whatever its exit status.
//
// Cleanup is verified the same way the in-browser probe is: the CLEAR reply
// must not report the light on, and a fresh inventory read must not show the
// bay identifying. An unverified cleanup leaves the entry in the ledger and is
// reported; callers exit non-zero.

const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_LEASE_MS = 20_000;
const MAX_LEASE_MS = 120_000;
const MIN_LEASE_MS = 500;
// A watchdog whose heartbeat is older than this is not watching.
const HEARTBEAT_STALE_MS = 5000;

// Milliseconds on CLOCK_MONOTONIC (process.hrtime): the same clock in every
// process on this host, never stepped by NTP or an operator, and comparable
// only within one boot (see bootId).
function monoNow() {
  return Number(process.hrtime.bigint() / 1_000_000n);
}

// The kernel's id for this boot, or null where there is none (non-Linux).
let cachedBootId;
function bootId() {
  if (cachedBootId === undefined) {
    try {
      cachedBootId = fs.readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim() || null;
    } catch {
      cachedBootId = null;
    }
  }
  return cachedBootId;
}

// A process that exists and is not a zombie. A watchdog SIGKILLed while its
// parent (the crawler) is stuck stays a zombie until the parent reaps it; it
// is not watching anything.
function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
  } catch (error) {
    if (error.code !== "EPERM") return false;
  }
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    const state = stat.slice(stat.lastIndexOf(")") + 2, stat.lastIndexOf(")") + 3);
    if (state === "Z" || state === "X") return false;
  } catch {
    // no procfs (or the process just went): kill(0) answered
  }
  return true;
}

// The identify time a lease may have: its own leaseMs, held to the cap.
function leaseCapMs(entry) {
  const ms = Number(entry && entry.leaseMs);
  return Number.isFinite(ms) && ms > 0 ? Math.min(MAX_LEASE_MS, Math.max(MIN_LEASE_MS, ms)) : MAX_LEASE_MS;
}

// Has this lease run out? Judged on the monotonic clock; the wall clock can
// only end it early (a forward step clears sooner, which is safe). Without a
// monotonic stamp from this boot there is nothing trustworthy: expired.
function leaseExpired(entry, { mono = monoNow(), wall = Date.now() } = {}) {
  if (!entry || !Number.isFinite(entry.armedMono) || (entry.bootId ?? null) !== bootId()) return true;
  const cap = leaseCapMs(entry);
  const age = mono - entry.armedMono;
  if (age < 0 || age >= cap) return true;
  const armedWall = Date.parse(entry.armedAt);
  return !Number.isFinite(armedWall) || wall - armedWall >= cap;
}

// A watchdog heartbeat that proves the watchdog is alive and watching
// `ownerPid` now: written by a live, non-zombie process of this boot, with a
// monotonic stamp younger than HEARTBEAT_STALE_MS (not from the future).
function heartbeatFresh(beat, ownerPid, mono = monoNow()) {
  if (!beat || beat.ownerPid !== ownerPid || !pidAlive(beat.pid)) return false;
  if ((beat.bootId ?? null) !== bootId() || !Number.isFinite(beat.heartbeatMono)) return false;
  const age = mono - beat.heartbeatMono;
  return age >= 0 && age < HEARTBEAT_STALE_MS;
}

function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function fsyncDir(dir) {
  const fd = fs.openSync(dir, "r");
  try {
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

// Atomic, durable replacement of a small file: data fsync, rename, then the
// directory entry fsync.
function writeDurable(file, text) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
  const fd = fs.openSync(tmp, "w", 0o600);
  try {
    fs.writeSync(fd, text);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, file);
  fsyncDir(dir);
}

class LedLedger {
  constructor(file, { leaseMs = DEFAULT_LEASE_MS } = {}) {
    if (!file) throw new Error("LedLedger needs a file");
    this.file = path.resolve(file);
    this.leaseMs = Math.min(MAX_LEASE_MS, Math.max(MIN_LEASE_MS, Number(leaseMs) || DEFAULT_LEASE_MS));
  }

  get lockFile() {
    return `${this.file}.lock`;
  }

  get watchdogFile() {
    return `${this.file}.watchdog`;
  }

  read() {
    if (!fs.existsSync(this.file)) return [];
    const text = fs.readFileSync(this.file, "utf8");
    const doc = JSON.parse(text || "{}");
    if (!Array.isArray(doc.armed)) throw new Error(`malformed LED ledger ${this.file}`);
    return doc.armed;
  }

  write(armed) {
    writeDurable(this.file, JSON.stringify({ armed }, null, 1));
  }

  // The crawler, the watchdog and a sweep all rewrite the ledger; one at a
  // time. A lock held by a dead process (or an unreadable one older than a few
  // seconds) is broken.
  withLock(fn, { timeoutMs = 10_000 } = {}) {
    fs.mkdirSync(path.dirname(this.file), { recursive: true, mode: 0o700 });
    const deadline = monoNow() + timeoutMs;
    for (;;) {
      try {
        const fd = fs.openSync(this.lockFile, "wx", 0o600);
        fs.writeSync(fd, String(process.pid));
        fs.closeSync(fd);
        break;
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
        let holder = NaN;
        let age = 0;
        try {
          holder = Number(fs.readFileSync(this.lockFile, "utf8"));
          // (mtime is wall-clock; a step either way only breaks an unreadable lock sooner)
          age = Math.abs(Date.now() - fs.statSync(this.lockFile).mtimeMs);
        } catch {
          continue;
        }
        if ((Number.isInteger(holder) && holder > 0 && !pidAlive(holder)) || (!(holder > 0) && age > 3000)) {
          try {
            fs.unlinkSync(this.lockFile);
          } catch {
            // someone else broke it
          }
          continue;
        }
        if (monoNow() > deadline) throw new Error(`LED ledger lock busy (${this.lockFile})`);
        sleepSync(10);
      }
    }
    try {
      return fn();
    } finally {
      try {
        fs.unlinkSync(this.lockFile);
      } catch {
        // already gone
      }
    }
  }

  // The watchdog's heartbeat, or null.
  watchdog() {
    try {
      return JSON.parse(fs.readFileSync(this.watchdogFile, "utf8"));
    } catch {
      return null;
    }
  }

  // A live, heartbeating watchdog that watches `ownerPid` (this process by default).
  watchdogAlive(ownerPid = process.pid) {
    return heartbeatFresh(this.watchdog(), ownerPid);
  }

  // Synchronous on purpose: the lease must be durable before the request
  // leaves, and only when something independent will honour it. Checked on
  // every IDENTIFY: the watchdog is alive and heartbeating now, and this
  // process's own supervisor (led-supervisor.js) is running to watch it.
  arm(entry) {
    if (!this.watchdogAlive()) throw new Error("no live identify watchdog for this crawler; IDENTIFY refused (#1117)");
    if (!this.supervisor || !this.supervisor.alive()) throw new Error("no running watchdog supervisor in this crawler; IDENTIFY refused (#1117)");
    const now = Date.now();
    const lease = {
      ...entry, ownerPid: process.pid, bootId: bootId(), armedMono: monoNow(), leaseMs: this.leaseMs,
      // For people only; expiry is judged on armedMono (leaseExpired).
      armedAt: new Date(now).toISOString(), expiresAt: new Date(now + this.leaseMs).toISOString(),
    };
    this.withLock(() => {
      const armed = this.read().filter((item) => keyOf(item) !== keyOf(entry));
      armed.push(lease);
      this.write(armed);
    });
    return lease;
  }

  disarm(match) {
    return this.withLock(() => {
      const armed = this.read();
      const kept = armed.filter((item) => !matches(item, match));
      if (kept.length !== armed.length) this.write(kept);
      return armed.length - kept.length;
    });
  }

  // Merge fields into matching entries (the watchdog's own bookkeeping).
  update(match, fields) {
    return this.withLock(() => {
      const armed = this.read();
      let changed = 0;
      const next = armed.map((item) => {
        if (!matches(item, match)) return item;
        changed += 1;
        return { ...item, ...fields };
      });
      if (changed) this.write(next);
      return changed;
    });
  }

  entries() {
    return this.read();
  }
}

function keyOf(entry) {
  return `${entry.origin} ${entry.path} ${entry.system_id || ""} ${entry.enclosure_id || ""}`;
}

function targetOf(entry) {
  return { origin: entry.origin, path: entry.path, system_id: entry.system_id, enclosure_id: entry.enclosure_id };
}

function matches(item, match) {
  return Object.entries(match).every(([key, value]) => item[key] === value);
}

// The armed target of an admitted IDENTIFY request.
function armedEntry(url, state) {
  const parsed = new URL(url);
  const slot = /^\/api\/slots\/([^/]+)\/led$/.exec(parsed.pathname);
  if (!slot) throw new Error(`not a bay LED request: ${parsed.pathname}`);
  return {
    origin: parsed.origin, path: parsed.pathname, slot: decodeURIComponent(slot[1]), state: state || null,
    system_id: parsed.searchParams.get("system_id"), enclosure_id: parsed.searchParams.get("enclosure_id"),
  };
}

function scopeQuery(entry) {
  const query = new URLSearchParams();
  if (entry.system_id) query.set("system_id", entry.system_id);
  if (entry.enclosure_id) query.set("enclosure_id", entry.enclosure_id);
  const text = query.toString();
  return text ? `?${text}` : "";
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Read the bay's identify state once: { off: bool } or { error }.
async function readIdentify(entry, { fetchImpl = fetch, requestMs = 60_000 } = {}) {
  try {
    const response = await fetchImpl(`${entry.origin}/api/inventory${scopeQuery(entry)}`, {
      headers: { Accept: "application/json" }, signal: AbortSignal.timeout(requestMs),
    });
    if (!response.ok) return { seen: { status: response.status } };
    const data = await response.json();
    const snapshot = data.snapshot || data;
    const slot = (snapshot.slots || []).find((item) => String(item.slot) === String(entry.slot));
    if (!slot) return { seen: { missing: true } };
    return { off: slot.identify_active !== true && slot.state !== "identify", seen: { state: slot.state, identify_active: slot.identify_active } };
  } catch (error) {
    return { seen: { error: String(error && error.message ? error.message : error).slice(0, 120) } };
  }
}

// Turn one armed light off out of band and verify it reads back off.
async function clearAndVerify(entry, { fetchImpl = fetch, attempts = 3, verifyMs = 45_000, pollMs = 3000, requestMs = 60_000 } = {}) {
  const result = { target: { path: entry.path, system_id: entry.system_id, enclosure_id: entry.enclosure_id }, slot: entry.slot, cleared: false, verified: false, detail: null };
  for (let attempt = 1; attempt <= attempts && !result.cleared; attempt += 1) {
    try {
      const response = await fetchImpl(`${entry.origin}${entry.path}${scopeQuery(entry)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ action: "CLEAR" }),
        signal: AbortSignal.timeout(requestMs),
      });
      const json = await response.json().catch(() => null);
      if (response.ok && !(json && json.identify_active === true)) result.cleared = true;
      else result.detail = `CLEAR answered ${response.status}${json && json.identify_active === true ? " with the light still on" : ""}`;
    } catch (error) {
      result.detail = `CLEAR failed: ${String(error && error.message ? error.message : error).slice(0, 160)}`;
    }
    if (!result.cleared && attempt < attempts) await sleep(Math.min(pollMs, 2000) * attempt);
  }
  if (!result.cleared) return result;
  const deadline = monoNow() + verifyMs;
  let seen = null;
  do {
    const read = await readIdentify(entry, { fetchImpl, requestMs });
    seen = read.seen;
    if (read.off) {
      result.verified = true;
      result.detail = null;
      return result;
    }
    if (monoNow() + pollMs > deadline) break;
    await sleep(pollMs);
  } while (monoNow() < deadline);
  result.detail = `off not verified: ${JSON.stringify(seen)}`;
  return result;
}

// Clear and verify every armed entry (expired or not); verified ones leave
// the ledger.
async function sweepLedger(ledger, options = {}) {
  const armed = ledger.entries();
  const results = [];
  for (const entry of armed) {
    const outcome = await clearAndVerify(entry, options);
    results.push(outcome);
    if (outcome.verified) ledger.disarm(targetOf(entry));
  }
  return { at: new Date().toISOString(), armed: armed.length, results, ok: results.every((r) => r.verified), remaining: ledger.entries().length };
}

module.exports = {
  LedLedger, armedEntry, clearAndVerify, readIdentify, sweepLedger, pidAlive, targetOf, writeDurable,
  monoNow, bootId, leaseExpired, leaseCapMs, heartbeatFresh, keyOf,
  DEFAULT_LEASE_MS, MAX_LEASE_MS, MIN_LEASE_MS, HEARTBEAT_STALE_MS,
};
