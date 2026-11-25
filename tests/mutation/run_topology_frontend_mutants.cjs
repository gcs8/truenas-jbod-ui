#!/usr/bin/env node
"use strict";

// Storage Fabric Topology frontend mutation run (#1516).
//
// Each mutant in topology_frontend_mutants.json is one textual change to a
// topology script, planted in memory by topology_frontend_hook.cjs. A mutant
// is killed when some topology test fails with it in place. The tests run in
// stages, fastest first, and a mutant stops at the first stage that kills
// it; a mutant that survives every stage has run every topology test.
//
// The run fails when the unmutated suite fails or runs no test, when any
// mutant cannot be planted or loaded (an anchor that is missing or not
// unique, a mutant that does not parse, tests that never read the mutated
// script, a run that reports no test count), when a mutant survives that is
// not listed in topology_equivalent_mutants.json with a reason, and when a
// listed equivalent is killed or no longer exists (the list is stale).
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
const STAGE_TIMEOUT_MS = 20 * 60 * 1000;

function option(name, fallback) {
  const at = process.argv.indexOf(name);
  return at >= 0 ? process.argv[at + 1] : fallback;
}

// Every topology test, in stages by cost: the quick files first, then the
// layout ones, then the two property-heavy files, each alone.
function stages() {
  const dir = path.join(ROOT, "tests", "js");
  const files = fs.readdirSync(dir).filter((name) => /^fabric_topology.*\.test\.js$/.test(name)).sort();
  if (!files.length) throw new Error(`no topology tests under ${dir}`);
  const late = [["fabric_topology_layout.test.js", "fabric_topology_trace_band.test.js"], ["fabric_topology_truth.test.js"], ["fabric_topology_coverage.test.js"]];
  for (const group of late) for (const name of group) if (!files.includes(name)) throw new Error(`missing topology test ${name}`);
  const quick = [...files.filter((name) => !late.flat().includes(name)), "fabric_linux_sas_transport.test.js"];
  return [quick, ...late].map((group) => group.map((name) => path.join("tests", "js", name)));
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
      const result = { code, signal, tests: count("tests"), pass: count("pass"), fail: count("fail"), applied };
      // Any sign the mutant was not run as planted is an error, never a kill.
      const problem = /TOPOLOGY-MUTANT-ERROR/.test(output) ? (output.match(/TOPOLOGY-MUTANT-ERROR [^\n]*/) || [""])[0]
        : /MODULE_NOT_FOUND|Cannot find module/.test(output) ? "a module was not found"
        : signal ? `stopped by ${signal}`
        : !Number.isInteger(result.tests) || result.tests === 0 ? "no test count"
        : !applied ? "the tests never loaded the mutated script"
        : result.fail === 0 && code !== 0 ? `exit ${code} with no failing test`
        : "";
      resolve({ ...result, problem, tail: problem ? output.slice(-1500) : "" });
    });
  });
}

async function judge(name) {
  const mutant = MUTANTS[name];
  const plan = mutant.tests ? [mutant.tests] : stages();
  const counts = [];
  for (const files of plan) {
    const result = await run(files, name);
    counts.push(`${result.pass}/${result.tests}`);
    if (result.problem) return { name, verdict: "error", detail: result.problem, tail: result.tail };
    if (result.fail > 0) return { name, verdict: "killed", detail: `${result.fail} failing in stage ${counts.length} (${counts.join(", ")} passed)` };
  }
  return { name, verdict: "survived", detail: `${counts.join(", ")} passed` };
}

async function pool(items, jobs, work) {
  const results = [];
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(jobs, items.length) }, async () => {
    while (next < items.length) {
      const item = items[next++];
      const result = await work(item);
      results.push(result);
      console.log(`${result.name.padEnd(28)} ${result.verdict.padEnd(9)} ${result.detail}`);
      if (result.tail) console.log(result.tail.split("\n").map((line) => `    ${line}`).join("\n"));
    }
  }));
  return results;
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

  // The unmutated suite first: every stage must run tests and pass.
  console.log(`node ${process.version}; shard ${index}/${shards}; ${chosen.length} of ${names.length} mutants; ${jobs} at a time`);
  const plans = [...new Set([...stages(), ...chosen.filter((name) => MUTANTS[name].tests).map((name) => MUTANTS[name].tests)].map((files) => JSON.stringify(files)))].map((files) => JSON.parse(files));
  for (const files of plans) {
    const result = await run(files, "");
    console.log(`stock ${files.length} files: ${result.pass}/${result.tests} passed`);
    if (result.problem || result.fail !== 0 || result.pass !== result.tests) {
      problems.push(`the unmutated suite does not pass (${files.join(" ")}): ${result.problem || `${result.fail} failing`}`);
    }
  }
  if (problems.length) {
    for (const problem of problems) console.log(`FAIL ${problem}`);
    process.exit(1);
  }

  const results = await pool(chosen, jobs, judge);
  const by = (verdict) => results.filter((item) => item.verdict === verdict).map((item) => item.name).sort();
  const survivors = by("survived");
  for (const name of by("error")) problems.push(`${name} could not be run as planted`);
  for (const name of survivors.filter((item) => !EQUIVALENT[item])) problems.push(`${name} survived and is not listed as equivalent`);
  for (const name of by("killed").filter((item) => EQUIVALENT[item])) problems.push(`${name} is listed as equivalent but was killed: remove it from the list`);
  console.log(`\n${by("killed").length} killed, ${survivors.length} survived (${survivors.map((name) => `${name}${EQUIVALENT[name] ? " (listed equivalent)" : ""}`).join(", ") || "none"}), ${by("error").length} errors, of ${chosen.length}`);
  const report = option("--report", "");
  if (report) fs.writeFileSync(report, `${JSON.stringify({ shard: `${index}/${shards}`, results: results.sort((a, b) => a.name.localeCompare(b.name)), problems }, null, 1)}\n`);
  for (const problem of problems) console.log(`FAIL ${problem}`);
  process.exit(problems.length ? 1 : 0);
}

main().catch((error) => {
  console.log(`FAIL ${error.stack || error}`);
  process.exit(1);
});
