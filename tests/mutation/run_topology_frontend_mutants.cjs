#!/usr/bin/env node
"use strict";

// Storage Fabric Topology frontend mutation run (#1516).
//
// Each mutant in topology_frontend_mutants.json is one textual change to a
// topology script, planted in memory by topology_frontend_hook.cjs. A mutant
// is killed only when a test the run collected fails on the mutated
// behaviour: an assertion, or an exception thrown from the product code
// (the innermost frame of its stack that names a file is under app/). The
// tests run in the stages topology_frontend_tests.json lists, fastest first,
// and a mutant stops at the first stage that kills it; a mutant that
// survives every stage has run every listed test.
//
// The run fails when a listed test file is missing, when a topology test
// file on disk is not in the list, when a listed file runs no test of its
// own, when the unmutated suite fails, when the unmutated suite reads a
// tracked file the CI job's path filter does not cover (read_trace/ records
// every file it opens, its children included), and for any mutant that
// cannot be planted or loaded (an anchor that is missing or not unique, a
// mutant that does not parse, tests that never read the mutated script),
// whose run reports no test count, fails to load a test file, reaches a
// missing module, or fails any test by anything but an assertion or a
// product exception (an exception from test or harness code, a hook, a
// timeout), even beside a test that a kill would count. It also fails when
// a mutant survives that topology_equivalent_mutants.json does not list
// with a reason, and when a listed equivalent is killed or no longer exists.
//
// Usage: node tests/mutation/run_topology_frontend_mutants.cjs
//   [--shard I/N] [--jobs N] [--only NAME,NAME] [--report FILE]

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..", "..");
const HOOK = path.join(__dirname, "topology_frontend_hook.cjs");
const TRACE = path.join(__dirname, "read_trace", "trace.cjs");
const MUTANTS = JSON.parse(fs.readFileSync(path.join(__dirname, "topology_frontend_mutants.json"), "utf8"));
const EQUIVALENT = JSON.parse(fs.readFileSync(path.join(__dirname, "topology_equivalent_mutants.json"), "utf8")).frontend;
const LISTED = JSON.parse(fs.readFileSync(path.join(__dirname, "topology_frontend_tests.json"), "utf8"));
const STAGE_TIMEOUT_MS = 20 * 60 * 1000;

function option(name, fallback) {
  const at = process.argv.indexOf(name);
  return at >= 0 ? process.argv[at + 1] : fallback;
}

// The listed stages, after checking the list against the tree: every listed
// file exists, and every topology test file on disk is listed.
function stages() {
  const listed = LISTED.stages.flat();
  const missing = listed.filter((file) => !fs.existsSync(path.join(ROOT, file)));
  if (missing.length) throw new Error(`listed topology test missing: ${missing.join(", ")}`);
  const pattern = new RegExp(LISTED.pattern || "^tests/js/fabric_topology[^/]*\\.test\\.js$");
  const onDisk = fs.readdirSync(path.join(ROOT, "tests", "js")).map((name) => `tests/js/${name}`).filter((file) => pattern.test(file));
  const unlisted = onDisk.filter((file) => !listed.includes(file));
  if (unlisted.length) throw new Error(`topology test not in the stage list (tests/mutation/topology_frontend_tests.json): ${unlisted.join(", ")}`);
  return LISTED.stages;
}

// Where an error was thrown: the innermost frame of its stack that names a
// file. Node's own frames and native ones name none. A file under app/ is
// the product; anything else (a test, a helper, an unnamed script a test
// evaluates) is not.
function thrownFrom(stack) {
  for (const line of stack) {
    const file = (/\(([^()]+):\d+:\d+\)$/.exec(line) || /^(?:at )?([^\s()]+):\d+:\d+$/.exec(line) || [])[1];
    if (!file || file.startsWith("node:")) continue;
    const relative = path.relative(ROOT, path.resolve(file));
    return { file, product: !relative.startsWith("..") && !path.isAbsolute(relative) && relative.split(path.sep)[0] === "app" };
  }
  return { file: "", product: false };
}

// Every test result of a TAP run, at any depth, with its error code, its
// failure type and the frame its error was thrown from. Node reports a test
// file that failed to load, or that has no test, as one result named after
// the file.
function results(output, files) {
  const names = new Set([...files, ...files.map((file) => path.basename(file))]);
  const lines = output.split("\n");
  const found = [];
  lines.forEach((line, index) => {
    const match = /^(\s*)(not ok|ok) \d+ - (.*)$/.exec(line);
    if (!match) return;
    // Its YAML block is indented two past the result and closes with "..."
    // at exactly that indent: an error text can contain either line.
    const indent = `${match[1]}  `;
    let code = "";
    let failure = "";
    const stack = [];
    if (lines[index + 1] === `${indent}---`) {
      for (let next = index + 2; next < lines.length && lines[next] !== `${indent}...`; next += 1) {
        const field = lines[next].startsWith(`${indent}code: '`) ? /^\s*code: '([^']*)'/.exec(lines[next]) : null;
        if (field && !code) code = field[1];
        const type = lines[next].startsWith(`${indent}failureType: '`) ? /^\s*failureType: '([^']*)'/.exec(lines[next]) : null;
        if (type && !failure) failure = type[1];
        if (lines[next] === `${indent}stack: |-` && !stack.length) {
          for (let frame = next + 1; frame < lines.length && lines[frame].startsWith(`${indent}  `); frame += 1) stack.push(lines[frame].trim());
        }
      }
    }
    const name = match[3].trim();
    const thrown = thrownFrom(stack);
    found.push({ depth: match[1].length, ok: match[2] === "ok", name, code, failure, top: thrown.file || stack[0] || "", product: thrown.product, file: names.has(name) || names.has(path.basename(name)) });
  });
  return found;
}

// What the CI job's path filter covers: its pattern, read from the workflow.
function scopePattern() {
  const workflow = path.join(ROOT, ".github", "workflows", "ci.yml");
  const found = fs.existsSync(workflow) ? /^\s*pattern='([^']+)'$/m.exec(fs.readFileSync(workflow, "utf8")) : null;
  if (!found) throw new Error("the CI path filter of the topology mutation job is not found in .github/workflows/ci.yml");
  return new RegExp(found[1]);
}

// Every tracked file (every file, where the tree is not a checkout).
function trackedFiles() {
  const listed = require("node:child_process").spawnSync("git", ["ls-files", "-z"], { cwd: ROOT, encoding: "utf8" });
  if (listed.status === 0) return new Set(listed.stdout.split("\0").filter(Boolean));
  const all = new Set();
  const walk = (folder) => {
    for (const entry of fs.readdirSync(folder, { withFileTypes: true })) {
      if ([".git", "node_modules"].includes(entry.name)) continue;
      const full = path.join(folder, entry.name);
      if (entry.isDirectory()) walk(full);
      else all.add(path.relative(ROOT, full).split(path.sep).join("/"));
    }
  };
  walk(ROOT);
  return all;
}

// The tracked files a traced run read that the CI path filter misses.
function outsideScope(log) {
  const pattern = scopePattern();
  const tracked = trackedFiles();
  const read = new Set(fs.existsSync(log) ? fs.readFileSync(log, "utf8").split("\n").filter(Boolean) : []);
  // -yy gives the resolved path of the returned descriptor, even when a
  // grandchild changed cwd, cleared its environment or used openat(dirfd).
  // Keep the language hooks too: cached modules and path-existence checks
  // can be dependencies without opening the source file.
  if (process.argv.includes("--trace-opens")) {
    for (const line of fs.readFileSync(`${log}.opens`, "utf8").split("\n")) {
      const opened = / = \d+<([^<>]+)>$/.exec(line);
      if (opened) read.add(opened[1]);
    }
  }
  return [...read].map((file) => path.relative(ROOT, file).split(path.sep).join("/")).filter((file) => tracked.has(file) && !pattern.test(file)).sort();
}

function run(files, mutant, trace = "") {
  return new Promise((resolve) => {
    const log = mutant ? path.join(fs.mkdtempSync(path.join(os.tmpdir(), "topology-mutant-")), "applied.log") : "";
    const args = [...(mutant ? ["--require", HOOK] : []), "--test", "--test-reporter=tap", "--test-concurrency=1", ...files];
    const env = { ...process.env, TOPOLOGY_MUTANT: mutant || "", TOPOLOGY_MUTANT_LOG: log };
    if (!mutant) delete env.TOPOLOGY_MUTANT;
    // The unmutated run records every file its processes read.
    if (trace) Object.assign(env, { TOPOLOGY_READ_TRACE: trace, PYTHONPATH: path.dirname(TRACE), NODE_OPTIONS: `${env.NODE_OPTIONS || ""} --require=${TRACE}`.trim() });
    const native = trace && process.argv.includes("--trace-opens");
    const child = spawn(native ? "strace" : process.execPath, native ? ["-f", "-qq", "-yy", "-A", "-o", `${trace}.opens`, "-e", "trace=open,openat,openat2", "--", process.execPath, ...args] : args, { cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.on("data", (chunk) => { output += chunk; });
    const timer = setTimeout(() => child.kill("SIGKILL"), STAGE_TIMEOUT_MS);
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      const count = (name) => Number((output.match(new RegExp(`^# ${name} (\\d+)$`, "m")) || [])[1] ?? NaN);
      const applied = mutant ? fs.existsSync(log) && fs.readFileSync(log, "utf8").trim().length > 0 : true;
      if (log) fs.rmSync(path.dirname(log), { recursive: true, force: true });
      const entries = results(output, files);
      const failed = entries.filter((entry) => !entry.ok);
      // A kill is a collected test failing on the mutated behaviour: an
      // assertion, or an exception thrown from the product code itself. A
      // test failing only because a subtest did is judged by its subtests.
      // Anything else (a test file not loading, an exception from test or
      // harness code, a hook, a timeout) is not shown to be the mutant's
      // doing, and fails the run whatever else failed beside it.
      const judged = failed.filter((entry) => !entry.file && entry.failure !== "subtestsFailed");
      const asserted = judged.filter((entry) => (entry.code === "ERR_ASSERTION" || entry.product) && ["", "testCodeFailure"].includes(entry.failure));
      const harness = judged.filter((entry) => !asserted.includes(entry));
      const result = { code, signal, tests: count("tests"), pass: count("pass"), fail: count("fail"), applied, entries, asserted };
      // Any sign the mutant was not run as planted, or that no assertion
      // caught it, is an error, never a kill.
      const loadFailure = failed.find((entry) => entry.file);
      const problem = /TOPOLOGY-MUTANT-ERROR/.test(output) ? (output.match(/TOPOLOGY-MUTANT-ERROR [^\n]*/) || [""])[0]
        : /MODULE_NOT_FOUND|Cannot find module/.test(output) ? "a module was not found"
        : signal ? `stopped by ${signal}`
        : !Number.isInteger(result.tests) || result.tests === 0 ? "no test count"
        : !applied ? "the tests never loaded the mutated script"
        : loadFailure ? `a test file did not load: ${loadFailure.name}`
        : harness.length ? `${harness.length} tests failed with neither an assertion nor a product exception${asserted.length ? ` (beside ${asserted.length} that did)` : ""}: ${harness[0].name}${harness[0].failure ? ` (${harness[0].failure})` : ""}${harness[0].top ? ` (thrown at ${harness[0].top})` : ""}`
        : failed.length && !asserted.length ? `${failed.length} tests failed with neither an assertion nor a product exception: ${failed[0].name}`
        : result.fail === 0 && code !== 0 ? `exit ${code} with no failing test`
        : "";
      resolve({ ...result, problem, tail: problem ? output.slice(-1500) : "" });
    });
  });
}

async function judge(name, plan) {
  const mutant = MUTANTS[name];
  const steps = mutant.tests ? [mutant.tests] : plan;
  const counts = [];
  for (const files of steps) {
    const result = await run(files, name);
    counts.push(`${result.pass}/${result.tests}`);
    if (result.problem) return { name, verdict: "error", detail: result.problem, tail: result.tail };
    if (result.asserted.length) {
      const thrown = result.asserted.filter((entry) => entry.code !== "ERR_ASSERTION").length;
      return { name, verdict: "killed", detail: `${result.asserted.length - thrown} assertion failures and ${thrown} product exceptions in stage ${counts.length} (${counts.join(", ")} passed)` };
    }
  }
  return { name, verdict: "survived", detail: `${counts.join(", ")} passed` };
}

async function pool(items, jobs, work) {
  const done = [];
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(jobs, items.length) }, async () => {
    while (next < items.length) {
      const item = items[next++];
      const result = await work(item);
      done.push(result);
      console.log(`${result.name.padEnd(28)} ${result.verdict.padEnd(9)} ${result.detail}`);
      if (result.tail) console.log(result.tail.split("\n").map((line) => `    ${line}`).join("\n"));
    }
  }));
  return done;
}

async function main() {
  const [index, shards] = option("--shard", "1/1").split("/").map(Number);
  if (!(shards >= 1 && index >= 1 && index <= shards)) throw new Error("--shard must be I/N with 1 <= I <= N");
  const jobs = Number(option("--jobs", String(Math.max(1, os.availableParallelism ? os.availableParallelism() : os.cpus().length))));
  const only = option("--only", "");
  const names = Object.keys(MUTANTS).sort();
  for (const name of only ? only.split(",") : []) if (!MUTANTS[name]) throw new Error(`unknown mutant ${name}`);
  const chosen = (only ? only.split(",") : names).filter((name) => names.indexOf(name) % shards === index - 1 || only);
  if (!chosen.length) throw new Error("no mutant selected");
  const problems = [];
  for (const name of Object.keys(EQUIVALENT)) {
    if (!MUTANTS[name]) problems.push(`${name} is listed as equivalent but is not a mutant`);
    if (!String(EQUIVALENT[name] || "").trim()) problems.push(`${name} is listed as equivalent without a reason`);
  }
  const plan = stages();
  console.log(`node ${process.version}; shard ${index}/${shards}; ${chosen.length} of ${names.length} mutants; ${jobs} at a time`);

  // The unmutated suite first, one file at a time: each listed file must run
  // a test of its own (a file with none is reported as one passing result
  // named after the file), and every test must pass.
  const files = [...new Set([...plan.flat(), ...chosen.flatMap((name) => MUTANTS[name].tests || [])])];
  const trace = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "topology-reads-")), "reads.log");
  for (const file of files) {
    const result = await run([file], "", trace);
    const own = result.entries.filter((entry) => !entry.file);
    if (result.problem || result.fail !== 0 || result.pass !== result.tests) problems.push(`the unmutated ${file} does not pass: ${result.problem || `${result.fail} failing`}`);
    else if (!own.length) problems.push(`${file} runs no test of its own`);
  }
  // Every tracked file the tests read must be one whose change runs this job.
  const uncovered = outsideScope(trace);
  fs.rmSync(path.dirname(trace), { recursive: true, force: true });
  for (const file of uncovered) problems.push(`the tests read ${file}, which the CI path filter of this job does not cover (.github/workflows/ci.yml)`);
  console.log(`stock: ${files.length} test files, each passing with tests of its own; every file they read is in the CI path filter${problems.length ? " (except as below)" : ""}`);
  if (problems.length) {
    for (const problem of problems) console.log(`FAIL ${problem}`);
    process.exit(1);
  }

  const done = await pool(chosen, jobs, (name) => judge(name, plan));
  const by = (verdict) => done.filter((item) => item.verdict === verdict).map((item) => item.name).sort();
  const survivors = by("survived");
  for (const name of by("error")) problems.push(`${name} could not be judged on an assertion`);
  for (const name of survivors.filter((item) => !EQUIVALENT[item])) problems.push(`${name} survived and is not listed as equivalent`);
  for (const name of by("killed").filter((item) => EQUIVALENT[item])) problems.push(`${name} is listed as equivalent but was killed: remove it from the list`);
  console.log(`\n${by("killed").length} killed, ${survivors.length} survived (${survivors.map((name) => `${name}${EQUIVALENT[name] ? " (listed equivalent)" : ""}`).join(", ") || "none"}), ${by("error").length} errors, of ${chosen.length}`);
  const report = option("--report", "");
  if (report) fs.writeFileSync(report, `${JSON.stringify({ shard: `${index}/${shards}`, results: done.map(({ tail, ...rest }) => rest).sort((a, b) => a.name.localeCompare(b.name)), problems }, null, 1)}\n`);
  for (const problem of problems) console.log(`FAIL ${problem}`);
  process.exit(problems.length ? 1 : 0);
}

main().catch((error) => {
  console.log(`FAIL ${error.stack || error}`);
  process.exit(1);
});
