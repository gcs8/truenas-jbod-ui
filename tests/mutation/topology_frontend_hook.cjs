"use strict";

// Loaded with `node --require` by run_topology_frontend_mutants.cjs: plants
// one mutant (named by TOPOLOGY_MUTANT) in memory, in whichever Storage
// Fabric Topology script holds its anchor. The anchor must occur exactly
// once across those scripts and the mutated script must still parse;
// otherwise this throws, and the runner reports the mutant as an error,
// never as killed. Each replacement is recorded in TOPOLOGY_MUTANT_LOG so
// the runner can tell that the tests loaded the mutated code at all.

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const NAME = process.env.TOPOLOGY_MUTANT;
const LOG = process.env.TOPOLOGY_MUTANT_LOG;
const ROOT = path.resolve(__dirname, "..", "..");
const STATIC = path.join(ROOT, "app", "static");
const MUTANTS = JSON.parse(fs.readFileSync(path.join(__dirname, "topology_frontend_mutants.json"), "utf8"));

const fail = (message) => {
  // A marker the runner looks for in the test output.
  const text = `TOPOLOGY-MUTANT-ERROR ${NAME}: ${message}`;
  process.stderr.write(`${text}\n`);
  throw new Error(text);
};

const mutant = MUTANTS[NAME];
if (!mutant) fail("unknown mutant");
const scripts = fs.readdirSync(STATIC).filter((name) => /^sas_fabric_topology.*\.js$/.test(name));
if (!scripts.length) fail(`no topology scripts under ${STATIC}`);
const holders = scripts.map((name) => [name, fs.readFileSync(path.join(STATIC, name), "utf8").split(mutant.from).length - 1]).filter(([, count]) => count > 0);
const total = holders.reduce((sum, [, count]) => sum + count, 0);
if (total !== 1) fail(`anchor found ${total} times across ${scripts.join(", ")}: mutation not unique`);
const target = path.join(STATIC, holders[0][0]);

const read = fs.readFileSync;
fs.readFileSync = function readMutated(file, ...rest) {
  const source = read.call(this, file, ...rest);
  if (typeof source !== "string" || typeof file !== "string" || path.resolve(file) !== target) return source;
  const mutated = source.replace(mutant.from, () => mutant.to);
  try {
    new vm.Script(mutated, { filename: target });
  } catch (error) {
    fail(`the mutated script does not parse: ${error.message}`);
  }
  if (LOG) fs.appendFileSync(LOG, `${process.pid} ${path.basename(target)}\n`);
  return mutated;
};
