"use strict";

// Record every file a Node process reads, for the topology mutation runs
// (#1516). The mutation runners load this with NODE_OPTIONS=--require for
// the unmutated run only, with TOPOLOGY_READ_TRACE naming a log, so every
// Node process of that run (test files and the children they start) appends
// the absolute path of each file it read, opened, checked for or loaded as
// a module when it exits. The runner holds that list to the CI job's path
// filter: a file the tests read that the filter does not cover fails the
// run. Without TOPOLOGY_READ_TRACE this does nothing.

const fs = require("node:fs");
const path = require("node:path");

const LOG = process.env.TOPOLOGY_READ_TRACE;
if (LOG) {
  const seen = new Set();
  const note = (file) => {
    if (typeof file === "string") seen.add(path.resolve(file));
    else if (file instanceof URL && file.protocol === "file:") seen.add(path.resolve(decodeURIComponent(file.pathname)));
  };
  const append = fs.appendFileSync;
  for (const name of ["readFileSync", "openSync", "existsSync", "readdirSync", "readFile", "open", "createReadStream"]) {
    const original = fs[name];
    if (typeof original !== "function") continue;
    fs[name] = function traced(file, ...rest) {
      note(file);
      return original.call(this, file, ...rest);
    };
  }
  for (const name of ["readFile", "open"]) {
    const original = fs.promises[name];
    fs.promises[name] = function traced(file, ...rest) {
      note(file);
      return original.call(this, file, ...rest);
    };
  }
  // A file a child process is given by path is read too, though the child
  // may run without this hook.
  const childProcess = require("node:child_process");
  for (const name of ["spawn", "spawnSync", "execFile", "execFileSync", "fork"]) {
    const original = childProcess[name];
    childProcess[name] = function traced(command, args, ...rest) {
      const options = [args, ...rest].find((item) => item && typeof item === "object" && !Array.isArray(item)) || {};
      const cwd = typeof options.cwd === "string" ? options.cwd : process.cwd();
      for (const item of [command, ...(Array.isArray(args) ? args : [])]) {
        if (typeof item !== "string") continue;
        const candidate = path.resolve(cwd, item);
        try {
          if (fs.statSync(candidate).isFile()) seen.add(candidate);
        } catch {
          // Not a file: an option or a word.
        }
      }
      return original.call(this, command, args, ...rest);
    };
  }
  process.on("exit", () => {
    for (const file of Object.keys(require.cache)) note(file);
    append(LOG, [...seen].sort().map((file) => `${file}\n`).join(""));
  });
}
