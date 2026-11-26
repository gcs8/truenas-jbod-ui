#!/usr/bin/env node
"use strict";

// Storage Fabric Topology frontend mutation run (#1516).
//
// Each mutant in topology_frontend_mutants.json is one textual change to a
// topology script, planted in memory by topology_frontend_hook.cjs. A mutant
// is killed only when an assertion fails in a test the run collected. The
// tests run in the stages topology_frontend_tests.json lists, fastest first,
// and a mutant stops at the first stage that kills it; a mutant that
// survives every stage has run every listed test.
//
// The run fails when a listed test file is missing, when a topology test
// file on disk is not in the list, when a listed file runs no test of its
// own, when the unmutated suite fails, and for any mutant that cannot be
// planted or loaded (an anchor that is missing or not unique, a mutant that
// does not parse, tests that never read the mutated script), whose run
// reports no test count, fails to load a test file, reaches a missing
// module, or fails tests without any assertion failing. It also fails when
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

// Every test result of a TAP run, at any depth, with its error code. Node
// reports a test file that failed to load, or that has no test, as one
// result named after the file.
function results(output, files) {
  const names = new Set([...files, ...files.map((file) => path.basename(file))]);
  const lines = output.split("\n");
  const found = [];
  lines.forEach((line, index) => {
    const match = /^(\s*)(not ok|ok) \d+ - (.*)$/.exec(line);
    if (!match) return;
    let code = "";
    for (let next = index + 1; next < lines.length && !/^\s*(not ok|ok) \d+ - /.test(lines[next]); next += 1) {
      const field = /^\s*code: '([^']*)'/.exec(lines[next]);
      if (field && !code) code = field[1];
      if (/^\s*\.\.\.$/.test(lines[next])) break;
    }
    const name = match[3].trim();
    found.push({ depth: match[1].length, ok: match[2] === "ok", name, code, file: names.has(name) || names.has(path.basename(name)) });
  });
  return found;
}

function run(files, mutant) {
  return new Promise((resolve) => {
    const log = mutant ? path.join(fs.mkdtempSync(path.join(os.tmpdir(), "topology-mutant-")), "applied.log") : "";
    const args = [...(mutant ? ["--require", HOOK] : []), "--test", "--test-reporter=tap", "--test-concurrency=1", ...files];
    const env = { ...process.env, TOPOLOGY_MUTANT: mutant || "", TOPOLOGY_MUTANT_LOG: log };
    if (!mutant) delete env.TOPOLOGY_MUTANT;
    const child = spawn(process.execPath, args, { cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"] });
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
      const asserted = failed.filter((entry) => entry.code === "ERR_ASSERTION" && !entry.file);
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
        : failed.length && !asserted.length ? `${failed.length} tests failed and no assertion failed: ${failed[0].name}`
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
    if (result.asserted.length) return { name, verdict: "killed", detail: `${result.asserted.length} assertion failures in stage ${counts.length} (${counts.join(", ")} passed)` };
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
  for (const file of files) {
    const result = await run([file], "");
    const own = result.entries.filter((entry) => !entry.file);
    if (result.problem || result.fail !== 0 || result.pass !== result.tests) problems.push(`the unmutated ${file} does not pass: ${result.problem || `${result.fail} failing`}`);
    else if (!own.length) problems.push(`${file} runs no test of its own`);
  }
  console.log(`stock: ${files.length} test files, each passing with tests of its own${problems.length ? " (except as below)" : ""}`);
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
