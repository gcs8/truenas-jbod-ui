"use strict";

// Identify-light ledger and out-of-band cleanup (#1117).
//
// The request guard is the only place an IDENTIFY can leave the browser. The
// moment it is admitted, before the request is forwarded, the exact target
// (origin, bay path, system_id, enclosure_id) is written to a ledger file and
// fsynced. The entry is removed only after the light reads back off. Anything
// still in the ledger is cleared out of band (plain HTTP from Node, no
// browser) by:
//
//   - the CLI's `finally` and its SIGTERM/SIGINT/SIGHUP handlers,
//   - a browser disconnect (the crawl stops, then the same cleanup runs),
//   - the startup sweep of the next run (`--led-ledger`), which covers SIGKILL,
//     a crashed runtime or a lost host: no crawl starts until it is clear,
//   - `scripts/ui_crawl.js --led-sweep`, which the restricted runner calls
//     after every write chunk whatever its exit status.
//
// Cleanup is verified the same way the in-browser probe is: the CLEAR reply
// must not report the light on, and a fresh inventory read must not show the
// bay identifying. An unverified cleanup leaves the entry in the ledger and is
// reported; callers exit non-zero.

const fs = require("node:fs");
const path = require("node:path");

class LedLedger {
  constructor(file) {
    if (!file) throw new Error("LedLedger needs a file");
    this.file = path.resolve(file);
  }

  read() {
    if (!fs.existsSync(this.file)) return [];
    const text = fs.readFileSync(this.file, "utf8");
    const doc = JSON.parse(text || "{}");
    if (!Array.isArray(doc.armed)) throw new Error(`malformed LED ledger ${this.file}`);
    return doc.armed;
  }

  write(armed) {
    fs.mkdirSync(path.dirname(this.file), { recursive: true, mode: 0o700 });
    const tmp = `${this.file}.${process.pid}.tmp`;
    const fd = fs.openSync(tmp, "w", 0o600);
    try {
      fs.writeSync(fd, JSON.stringify({ armed }, null, 1));
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(tmp, this.file);
  }

  // Synchronous on purpose: it must be durable before the request leaves.
  arm(entry) {
    const armed = this.read().filter((item) => keyOf(item) !== keyOf(entry));
    armed.push({ ...entry, armedAt: new Date().toISOString() });
    this.write(armed);
  }

  disarm(match) {
    const armed = this.read();
    const kept = armed.filter((item) => !matches(item, match));
    if (kept.length !== armed.length) this.write(kept);
    return armed.length - kept.length;
  }

  entries() {
    return this.read();
  }
}

function keyOf(entry) {
  return `${entry.origin} ${entry.path} ${entry.system_id || ""} ${entry.enclosure_id || ""}`;
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

// Turn one armed light off out of band and verify it reads back off.
async function clearAndVerify(entry, { fetchImpl = fetch, attempts = 3, verifyMs = 45_000, pollMs = 3000, requestMs = 60_000 } = {}) {
  const result = { target: { path: entry.path, system_id: entry.system_id, enclosure_id: entry.enclosure_id }, cleared: false, verified: false, detail: null };
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
  const deadline = Date.now() + verifyMs;
  let seen = null;
  do {
    try {
      const response = await fetchImpl(`${entry.origin}/api/inventory${scopeQuery(entry)}`, {
        headers: { Accept: "application/json" }, signal: AbortSignal.timeout(requestMs),
      });
      if (response.ok) {
        const data = await response.json();
        const snapshot = data.snapshot || data;
        const slot = (snapshot.slots || []).find((item) => String(item.slot) === String(entry.slot));
        seen = slot ? { state: slot.state, identify_active: slot.identify_active } : { missing: true };
        if (slot && slot.identify_active !== true && slot.state !== "identify") {
          result.verified = true;
          result.detail = null;
          return result;
        }
      } else {
        seen = { status: response.status };
      }
    } catch (error) {
      seen = { error: String(error && error.message ? error.message : error).slice(0, 120) };
    }
    if (Date.now() + pollMs > deadline) break;
    await sleep(pollMs);
  } while (Date.now() < deadline);
  result.detail = `off not verified: ${JSON.stringify(seen)}`;
  return result;
}

// Clear and verify every armed entry; verified ones leave the ledger.
async function sweepLedger(ledger, options = {}) {
  const armed = ledger.entries();
  const results = [];
  for (const entry of armed) {
    const outcome = await clearAndVerify(entry, options);
    results.push(outcome);
    if (outcome.verified) ledger.disarm({ origin: entry.origin, path: entry.path, system_id: entry.system_id, enclosure_id: entry.enclosure_id });
  }
  return { at: new Date().toISOString(), armed: armed.length, results, ok: results.every((r) => r.verified), remaining: ledger.entries().length };
}

module.exports = { LedLedger, armedEntry, clearAndVerify, sweepLedger };
