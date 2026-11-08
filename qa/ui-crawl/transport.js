"use strict";

// Request routing for the UI coverage crawler (#ui-coverage).
//
// Every request goes through the RequestGuard first. Then either:
//   - fixture mode: answer from the synthetic fixture (CI, no stack), or
//   - live mode: pass through, serving repeated read API answers from a
//     short-lived harness cache so that reloading a page for every state does
//     not repeat appliance reads (Archive load rules). Any admitted write
//     clears the cache.

const fs = require("node:fs");
const path = require("node:path");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const STATIC_ROOTS = {
  ui: path.join(REPO_ROOT, "app", "static"),
  offline: path.join(REPO_ROOT, "app", "static"),
  admin: path.join(REPO_ROOT, "admin_service", "static"),
  history: path.join(REPO_ROOT, "history_service", "static"),
};

class ReadCache {
  constructor({ ttlMs = 15 * 60_000, maxEntries = 600, maxBytes = 12 * 1024 * 1024 } = {}) {
    this.ttlMs = ttlMs;
    this.maxEntries = maxEntries;
    this.maxBytes = maxBytes;
    this.entries = new Map();
    this.hits = 0;
    this.misses = 0;
  }

  key(method, url, body) {
    return `${method} ${url} ${body || ""}`;
  }

  get(key) {
    const entry = this.entries.get(key);
    if (!entry) return null;
    if (Date.now() - entry.at > this.ttlMs) {
      this.entries.delete(key);
      return null;
    }
    return entry;
  }

  set(key, value) {
    if (value.body.length > this.maxBytes) return;
    if (this.entries.size >= this.maxEntries) this.entries.delete(this.entries.keys().next().value);
    this.entries.set(key, { ...value, at: Date.now() });
  }

  clear() {
    this.entries.clear();
  }
}

function cacheable(method, url) {
  if (!url.pathname.startsWith("/api/")) return false;
  if (url.pathname === "/api/release-status" || url.pathname.startsWith("/api/export/")) return false;
  if (method === "GET") return true;
  return method === "POST" && /\/smart-batch$|^\/api\/history\/scopes\/bundle$/.test(url.pathname);
}

// origins: { ui: "http://host:port", history: ..., admin: ..., offline: ... }
async function installRoutes(context, { guard, origins, fixture = null, cache = null, stats }) {
  const inflight = new Set();
  const serviceFor = (url) => Object.entries(origins).find(([, origin]) => origin && new URL(origin).origin === url.origin)?.[0] || null;
  await context.route("**/*", (route) => {
    const task = (async () => {
      const request = route.request();
      const method = request.method();
      let url;
      try {
        url = new URL(request.url());
      } catch {
        return route.abort();
      }
      const verdict = guard.admit({ method, url: url.href, body: request.postData() });
      if (!verdict.allowed) {
        stats.blocked.push(`${method} ${url.pathname} (${verdict.reason})`);
        return route.abort("blockedbyclient");
      }
      if (verdict.reason.startsWith("declared write") || verdict.reason.startsWith("declared forced")) {
        stats.writes.push(`${method} ${url.pathname}${url.search}`);
        if (cache) cache.clear();
      }
      if (fixture) return fixture(route, { method, url, service: serviceFor(url), body: request.postData() });
      if (cache && cacheable(method, url)) {
        const key = cache.key(method, url.href, request.postData());
        const hit = cache.get(key);
        if (hit) {
          cache.hits += 1;
          return route.fulfill({ status: hit.status, headers: hit.headers, body: hit.body });
        }
        cache.misses += 1;
        const response = await route.fetch({ maxRedirects: 0, timeout: 300_000 });
        const body = await response.body();
        const headers = response.headers();
        if (response.status() < 400) cache.set(key, { status: response.status(), headers, body });
        return route.fulfill({ status: response.status(), headers, body });
      }
      return route.continue();
    })().catch((error) => {
      stats.routeErrors.push(String(error && error.message ? error.message : error).split("\n")[0].slice(0, 160));
      return route.abort().catch(() => {});
    });
    inflight.add(task);
    task.finally(() => inflight.delete(task));
    return task;
  });
  return {
    async drain() {
      await Promise.allSettled([...inflight]);
    },
  };
}

// Synthetic answers for CI. `data` is scripts/build_ui_crawl_fixture.py output.
function fixtureHandler(data, { writes = [] } = {}) {
  return async (route, { method, url, service }) => {
    const key = `${service}:${url.pathname}`;
    if (method === "GET" && data.pages[key] !== undefined) {
      return route.fulfill({ contentType: "text/html; charset=utf-8", body: data.pages[key] });
    }
    if (url.pathname.startsWith("/static/")) {
      const root = STATIC_ROOTS[service] || STATIC_ROOTS.ui;
      const file = path.join(root, url.pathname.slice("/static/".length));
      if (file.startsWith(root) && fs.existsSync(file) && fs.statSync(file).isFile()) return route.fulfill({ path: file });
      return route.fulfill({ status: 404, body: "" });
    }
    if (method !== "GET" && method !== "HEAD") {
      writes.push(`${method} ${url.pathname}`);
      if (/\/smart-batch$/.test(url.pathname)) {
        let slots = [];
        try {
          slots = JSON.parse(route.request().postData() || "{}").slots || [];
        } catch {
          slots = [];
        }
        const summaries = slots.filter((slot) => data.smart_batch && data.smart_batch[String(slot)]).map((slot) => ({ slot, summary: data.smart_batch[String(slot)] }));
        return route.fulfill({ contentType: "application/json", body: JSON.stringify({ summaries }) });
      }
      if (/\/led$/.test(url.pathname)) {
        const action = /CLEAR/.test(route.request().postData() || "") ? false : true;
        return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true, identify_active: action, requested_active: action, confirmed: true, slot: Number(url.pathname.split("/")[3]) }) });
      }
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true }) });
    }
    if (data.api[key] !== undefined) return route.fulfill({ contentType: "application/json", body: JSON.stringify(data.api[key]) });
    return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "not in the synthetic fixture" }) });
  };
}

module.exports = { installRoutes, fixtureHandler, ReadCache, cacheable };
