"use strict";

// Request admission for the UI coverage crawler.
//
// Every browser request passes through `admit()` before it leaves the
// browser. The rules are fixed and small:
//
//   1. Only the target's own origins are reachable; everything else aborts.
//   2. GET/HEAD pass, except a GET that forces an appliance refresh
//      (`force`/`refresh` set to a true value), which aborts unless the
//      running state declared it in `writes`.
//   3. A non-GET passes only if it is a declared read POST (inventory
//      `read_posts`, or the running state's `read_requests`), or matches one
//      of the running state's `writes` with budget left. Writes are admitted
//      only between `openWrites()` and `closeWrites()`, which the actuation
//      runner calls around one app-write-revert or led state.
//   4. Everything else aborts and is recorded.
//
// The guard never sees credentials: it gets method, URL and the request body.

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);

function globToRegExp(pattern) {
  const escaped = String(pattern).replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, "[^/]+");
  return new RegExp(`^${escaped}$`);
}

function forcesRefresh(url) {
  for (const name of ["force", "refresh"]) {
    const value = url.searchParams.get(name);
    if (value !== null && TRUE_VALUES.has(value.toLowerCase())) return true;
  }
  return false;
}

function matchesRule(rule, method, url, body) {
  if (String(rule.method || "GET").toUpperCase() !== method) return false;
  if (!globToRegExp(rule.path).test(url.pathname)) return false;
  if (rule.force === true && !forcesRefresh(url)) return false;
  const text = typeof body === "string" ? body : "";
  if (rule.body_includes && !text.includes(rule.body_includes)) return false;
  if (rule.body_excludes && text.replace(/\s+/g, "").includes(rule.body_excludes)) return false;
  return true;
}

class RequestGuard {
  constructor({ origins, readPosts = [] }) {
    if (!Array.isArray(origins) || origins.length === 0) throw new Error("RequestGuard needs at least one origin");
    this.origins = new Set(origins.map((origin) => new URL(origin).origin));
    this.readPosts = readPosts;
    this.window = null;
    this.log = [];
  }

  // Admit the declared writes of one state. `stateId` names the owner in logs.
  openWrites(stateId, writes, readRequests = []) {
    if (this.window) throw new Error(`write window already open for ${this.window.stateId}`);
    this.window = {
      stateId,
      readRequests,
      writes: (writes || []).map((rule) => ({ rule, used: 0, max: Number.isInteger(rule.max) ? rule.max : 1 })),
      admitted: [],
    };
  }

  closeWrites() {
    const closed = this.window;
    this.window = null;
    return closed ? closed.admitted : [];
  }

  // Declared read requests (for example the export download) for a read state.
  openReads(stateId, readRequests) {
    this.openWrites(stateId, [], readRequests);
  }

  admit({ method, url, body = null }) {
    const verb = String(method || "GET").toUpperCase();
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      return this.record(verb, url, false, "unparseable URL");
    }
    if (parsed.protocol === "data:" || parsed.protocol === "blob:") return this.record(verb, url, true, "inline");
    if (!this.origins.has(parsed.origin)) return this.record(verb, url, false, "foreign origin");
    if (verb === "GET" || verb === "HEAD") {
      if (!forcesRefresh(parsed)) return this.record(verb, url, true, "read");
      const write = this.matchWrite(verb, parsed, body);
      if (write) return this.record(verb, url, true, `declared forced refresh for ${this.window.stateId}`, write);
      return this.record(verb, url, false, "forced appliance refresh");
    }
    if (this.readPosts.some((rule) => matchesRule(rule, verb, parsed, body))) return this.record(verb, url, true, "read post");
    if (this.window && this.window.readRequests.some((rule) => matchesRule(rule, verb, parsed, body))) {
      return this.record(verb, url, true, `declared read for ${this.window.stateId}`);
    }
    const write = this.matchWrite(verb, parsed, body);
    if (write) return this.record(verb, url, true, `declared write for ${this.window.stateId}`, write);
    return this.record(verb, url, false, this.window ? `undeclared write during ${this.window.stateId}` : "write outside a write state");
  }

  matchWrite(verb, parsed, body) {
    if (!this.window) return null;
    const slot = this.window.writes.find((entry) => entry.used < entry.max && matchesRule(entry.rule, verb, parsed, body));
    if (!slot) return null;
    slot.used += 1;
    return slot;
  }

  record(method, url, allowed, reason, write = null) {
    const path = (() => {
      try {
        const parsed = new URL(url);
        return `${parsed.pathname}${parsed.search}`;
      } catch {
        return String(url).slice(0, 120);
      }
    })();
    const entry = { method, path, allowed, reason };
    if (write && this.window) this.window.admitted.push({ method, path, rule: write.rule.path });
    if (!allowed || write) this.log.push(entry);
    return entry;
  }

  blocked() {
    return this.log.filter((entry) => !entry.allowed);
  }
}

module.exports = { RequestGuard, forcesRefresh, matchesRule, globToRegExp };
