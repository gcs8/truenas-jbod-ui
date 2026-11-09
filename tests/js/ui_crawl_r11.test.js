"use strict";

// Round-11 regressions for the UI coverage crawler (#1108, owner decision
// D-TNJ-20261109-CRAWLER-R11).
//
//   B (Codex PRRT_kwDOSAqK-M61Jv7a): live target discovery checks every HTTP
//     status. A busy or 5xx answer is retried with navigate()'s bounded busy
//     policy; a final failure, a main page without systems, a requested system
//     that discovery did not find, or a crawl that would run (or ran) no state
//     fails the crawl non-zero. A live crawl that exercised nothing is never a
//     success.
//   C (Codex PRRT_kwDOSAqK-M61JfVj): a baseline visual diff whose screenshot
//     changed size is a visual-diff finding that names both sizes; same-size
//     images keep the changed-pixel threshold.
// Synthetic data only; the CLI tests use a local fake stack and never start a
// browser.

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const UI = "http://ui.example.test";
const MAIN_PAGE = `<select id="system-select"><option value="sys-a">A</option><option value="sys-b">B</option></select>`;
const INVENTORY = JSON.stringify({ snapshot: { selected_system_platform: "core", enclosures: [{ id: "enc-1" }] } });
const VIEWS = JSON.stringify({ views: [] });

function tmpDir(label) {
  return fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), `uic11-${label}-`));
}

// A scripted stack: each key ("/" or path+search) answers its list of
// responses in order, repeating the last one.
function scripted(script) {
  const calls = [];
  const fetchImpl = async (url) => {
    const parsed = new URL(url);
    const key = parsed.pathname === "/" ? "/" : `${parsed.pathname}${parsed.search}`;
    calls.push(key);
    const answers = script[key] || [{ status: 404, body: "not found" }];
    const seen = calls.filter((k) => k === key).length;
    const answer = answers[Math.min(seen, answers.length) - 1];
    return new Response(answer.body ?? "", { status: answer.status ?? 200, headers: answer.headers || {} });
  };
  return { calls, fetchImpl };
}

const healthy = (overrides = {}) => ({
  "/": [{ body: MAIN_PAGE }],
  "/api/inventory?system_id=sys-a": [{ body: INVENTORY }],
  "/api/inventory?system_id=sys-b": [{ body: INVENTORY }],
  "/api/storage-views?system_id=sys-a": [{ body: VIEWS }],
  "/api/storage-views?system_id=sys-b": [{ body: VIEWS }],
  ...overrides,
});

async function discover(script, args = {}) {
  const cli = req("scripts/ui_crawl.js");
  const stack = scripted(script);
  const waits = [];
  const realFetch = global.fetch;
  global.fetch = stack.fetchImpl;
  try {
    const targets = await cli.liveTargets({ ui: UI, systems: null, history: null, admin: null, ...args }, { sleep: async (ms) => { waits.push(ms); } });
    return { targets, calls: stack.calls, waits };
  } catch (error) {
    error.calls = stack.calls;
    error.waits = waits;
    throw error;
  } finally {
    global.fetch = realFetch;
  }
}

async function rejects(script, args, pattern) {
  let caught = null;
  try {
    await discover(script, args);
  } catch (error) {
    caught = error;
  }
  assert.ok(caught, "discovery must fail");
  assert.match(String(caught.message), pattern);
  return caught;
}

// ------------------------------------------------- B: discovery statuses --

test("B: a busy (503) main page is retried with the navigate() busy policy, then discovery succeeds", async () => {
  const { targets, calls, waits } = await discover(healthy({ "/": [{ status: 503, body: "busy", headers: { "Retry-After": "2" } }, { body: MAIN_PAGE }] }));
  assert.equal(calls.filter((k) => k === "/").length, 2);
  assert.deepEqual(waits, [2000], "Retry-After seconds x attempt, capped like navigate()");
  assert.deepEqual([...new Set(targets.map((t) => t.system))].sort(), ["sys-a", "sys-b"]);
});

test("B: a persistently busy main page fails after navigate()'s bounded attempts, never an empty success", async () => {
  const { BUSY_RETRY } = req("qa/ui-crawl/driver");
  assert.equal(BUSY_RETRY.attempts, 4);
  const error = await rejects(healthy({ "/": [{ status: 503, body: "busy" }] }), {}, /503/);
  assert.equal(error.calls.filter((k) => k === "/").length, BUSY_RETRY.attempts);
  assert.equal(error.waits.length, BUSY_RETRY.attempts - 1);
  for (const ms of error.waits) assert.ok(ms <= BUSY_RETRY.maxWaitMs, `wait ${ms} capped`);
  assert.match(error.message, /after 4 attempts/);
});

test("B: any 5xx is retried; another non-2xx fails at once", async () => {
  const ok = await discover(healthy({ "/": [{ status: 502 }, { status: 500 }, { body: MAIN_PAGE }] }));
  assert.equal(ok.calls.filter((k) => k === "/").length, 3);
  const error = await rejects(healthy({ "/": [{ status: 401, body: "no" }] }), {}, /401/);
  assert.equal(error.calls.filter((k) => k === "/").length, 1, "a 4xx is not retried");
  assert.deepEqual(error.waits, []);
});

test("B: a 200 main page without a system select fails discovery", async () => {
  await rejects(healthy({ "/": [{ body: "<html><body>maintenance</body></html>" }] }), {}, /no systems/);
  await rejects(healthy({ "/": [{ body: `<select id="system-select"></select>` }] }), {}, /no systems/);
});

test("B: a requested system that discovery did not find fails, naming it", async () => {
  await rejects(healthy(), { systems: ["sys-a", "sys-missing"] }, /sys-missing/);
  await rejects(healthy(), { systems: ["sys-missing"] }, /sys-missing/);
  const { targets } = await discover(healthy(), { systems: ["sys-b"] });
  assert.deepEqual([...new Set(targets.map((t) => t.system))], ["sys-b"]);
});

test("B: per-system inventory and storage-view discovery reads retry busy answers and fail on a final error", async () => {
  const retried = await discover(healthy({ "/api/inventory?system_id=sys-a": [{ status: 503 }, { body: INVENTORY }] }));
  assert.equal(retried.calls.filter((k) => k === "/api/inventory?system_id=sys-a").length, 2);
  await rejects(healthy({ "/api/inventory?system_id=sys-a": [{ status: 503 }] }), {}, /503.*\/api\/inventory|\/api\/inventory.*503/);
  // A storage-view error no longer silently drops that system's view targets.
  await rejects(healthy({ "/api/storage-views?system_id=sys-b": [{ status: 500 }] }), {}, /500.*\/api\/storage-views|\/api\/storage-views.*500/);
});

test("B: a live crawl that planned or executed no state is a failure", () => {
  const { liveCoverageProblems } = req("scripts/ui_crawl.js");
  const target = { label: "sys-a/main/enc-0" };
  assert.match(liveCoverageProblems({ plan: [] }).join("\n"), /no targets/);
  assert.match(liveCoverageProblems({ plan: [{ target, states: [] }] }).join("\n"), /no state/);
  assert.deepEqual(liveCoverageProblems({ plan: [{ target, states: [{ id: "main.default" }] }] }), []);
  // After the crawl: every record not applicable (or none at all) exercised nothing.
  assert.match(liveCoverageProblems({ plan: [{ target, states: [{ id: "main.default" }] }], records: [] }).join("\n"), /executed no state/);
  assert.match(liveCoverageProblems({ plan: [{ target, states: [{ id: "a" }] }], records: [{ status: "not-applicable" }, { status: "not-applicable" }] }).join("\n"), /executed no state/);
  assert.match(liveCoverageProblems({ plan: [{ target, states: [{ id: "a" }] }], records: [{ status: "skipped" }] }).join("\n"), /executed no state/, "a state its class kept from running did not run");
  assert.deepEqual(liveCoverageProblems({ plan: [{ target, states: [{ id: "a" }] }], records: [{ status: "not-applicable" }, { status: "done" }] }), []);
});

// ------------------------------------------------- B: the CLI end to end --

function fakeStack(script) {
  const counts = {};
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, "http://x");
    const key = url.pathname === "/" ? "/" : `${url.pathname}${url.search}`;
    counts[key] = (counts[key] || 0) + 1;
    const answers = script[key] || [{ status: 404, body: "not found" }];
    const answer = answers[Math.min(counts[key], answers.length) - 1];
    response.writeHead(answer.status ?? 200, { "Retry-After": "0", ...(answer.headers || {}) });
    response.end(answer.body ?? "");
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, counts, origin: `http://127.0.0.1:${server.address().port}` })));
}

function runCli(args) {
  return new Promise((resolve) => {
    // A CDP endpoint nothing listens on: a crawl that reached the browser
    // step fails there, with a different message, instead of launching one.
    const env = { ...process.env, TNJ_UIC11_CDP: "ws://127.0.0.1:9/devtools/browser/none" };
    delete env.PYTHONPATH;
    const child = spawn(process.execPath, [path.join(root, "scripts/ui_crawl.js"), ...args, "--cdp-env", "TNJ_UIC11_CDP"], { env, stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (chunk) => { out += chunk; });
    child.stderr.on("data", (chunk) => { out += chunk; });
    child.on("exit", (code) => resolve({ code, out }));
  });
}

async function cliCase(label, script, extra = []) {
  const stack = await fakeStack(script);
  const out = tmpDir(label);
  try {
    const result = await runCli(["--ui", stack.origin, "--out", out, "--widths", "1600", "--no-axe", ...extra]);
    const receipt = JSON.parse(fs.readFileSync(path.join(out, "receipt.json"), "utf8"));
    const report = JSON.parse(fs.readFileSync(path.join(out, "findings.json"), "utf8"));
    return { ...result, receipt, report, counts: stack.counts };
  } finally {
    stack.server.close();
  }
}

const liveScript = (overrides = {}) => ({
  "/": [{ body: MAIN_PAGE }],
  "/api/inventory?system_id=sys-a": [{ body: INVENTORY }],
  "/api/inventory?system_id=sys-b": [{ body: INVENTORY }],
  "/api/storage-views?system_id=sys-a": [{ body: VIEWS }],
  "/api/storage-views?system_id=sys-b": [{ body: VIEWS }],
  ...overrides,
});

function assertFailedDiscovery(result, pattern) {
  assert.notEqual(result.code, 0, result.out);
  assert.equal(result.code, 2, result.out);
  assert.match(result.out, pattern);
  assert.doesNotMatch(result.out, /127\.0\.0\.1:9|devtools/, "failed before any browser step");
  assert.equal(result.receipt.status, "safety-failed");
  assert.equal(result.receipt.exitCode, 2);
  assert.equal(result.report.safety.status, "safety-failed");
  assert.ok(result.report.safety.failures.some((line) => pattern.test(line)), JSON.stringify(result.report.safety.failures));
  assert.equal(result.report.records.length, 0);
}

test("B CLI: a persistent 503 from the main page exits non-zero with a clear message and a failed report", { timeout: 120_000 }, async () => {
  const result = await cliCase("busy", liveScript({ "/": [{ status: 503, body: "busy" }] }));
  assertFailedDiscovery(result, /live target discovery failed: .*503.*after 4 attempts/);
  assert.equal(result.counts["/"], 4);
});

test("B CLI: a 200 main page without a system select exits non-zero", { timeout: 120_000 }, async () => {
  const result = await cliCase("noselect", liveScript({ "/": [{ body: "<html>maintenance</html>" }] }));
  assertFailedDiscovery(result, /live target discovery failed: .*no systems/);
});

test("B CLI: a requested system missing from discovery exits non-zero", { timeout: 120_000 }, async () => {
  const result = await cliCase("missing", liveScript(), ["--systems", "sys-a,sys-gone"]);
  assertFailedDiscovery(result, /live target discovery failed: .*sys-gone/);
});

test("B CLI: a live crawl whose filters leave no state to run exits non-zero before any browser step", { timeout: 120_000 }, async () => {
  const result = await cliCase("nostates", liveScript(), ["--states", "no.such.state"]);
  assertFailedDiscovery(result, /no state/);
});

// ----------------------------------------------- C: visual diff of size --

function solid(width, height, rgb) {
  const data = Buffer.alloc(width * height * 4);
  for (let i = 0; i < width * height; i += 1) data.set([...rgb, 255], i * 4);
  return { width, height, data };
}

function baselineCrawler(image) {
  const { Crawler } = req("qa/ui-crawl/crawler");
  const png = req("qa/ui-crawl/png");
  const baselineDir = tmpDir("base");
  const rel = path.join("states", "t", "s", "1600");
  fs.mkdirSync(path.join(baselineDir, rel), { recursive: true });
  fs.writeFileSync(path.join(baselineDir, rel, "full.png"), png.encode(image));
  return { diff: (next) => Crawler.prototype.baselineDiff.call({ baselineDir }, rel, next) };
}

test("C: removing the bottom half of the screenshot is a visual-diff finding naming both sizes", () => {
  const { visualDiffFinding } = req("qa/ui-crawl/crawler");
  const png = req("qa/ui-crawl/png");
  const before = solid(40, 60, [10, 120, 200]);
  const after = png.crop(before, 0, 0, 40, 30);
  const diff = baselineCrawler(before).diff(after);
  assert.equal(diff.sizeChanged, true);
  assert.equal(diff.changedFraction, 0, "the common area itself is unchanged");
  const finding = visualDiffFinding(diff);
  assert.ok(finding, "a size change is a finding");
  assert.equal(finding.type, "visual-diff");
  assert.match(finding.text, /40x60/);
  assert.match(finding.text, /40x30/);
  assert.deepEqual(finding.detail.before, [40, 60]);
  assert.deepEqual(finding.detail.after, [40, 30]);
  // Wider, too.
  assert.ok(visualDiffFinding(baselineCrawler(before).diff(solid(41, 60, [10, 120, 200]))));
});

test("C: identical images give no finding; same-size images keep the changed-pixel threshold", () => {
  const { visualDiffFinding } = req("qa/ui-crawl/crawler");
  const image = solid(50, 50, [200, 200, 200]);
  assert.equal(visualDiffFinding(baselineCrawler(image).diff(solid(50, 50, [200, 200, 200]))), null);
  // 1 changed row of 50 (2%) is at the threshold, not over it; 2 rows (4%) are.
  const oneRow = solid(50, 50, [200, 200, 200]);
  oneRow.data.fill(0, 0, 50 * 4);
  assert.equal(visualDiffFinding(baselineCrawler(image).diff(oneRow)), null);
  const twoRows = solid(50, 50, [200, 200, 200]);
  twoRows.data.fill(0, 0, 2 * 50 * 4);
  const finding = visualDiffFinding(baselineCrawler(image).diff(twoRows));
  assert.ok(finding);
  assert.match(finding.text, /changed 4% vs baseline/);
  // No baseline, or an unreadable one: no visual-diff finding (unchanged).
  assert.equal(visualDiffFinding(null), null);
  assert.equal(visualDiffFinding({ missing: true }), null);
});
