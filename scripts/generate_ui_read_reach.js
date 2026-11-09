#!/usr/bin/env node
"use strict";

// Regenerate qa/ui-read-reach.json, the plain reads (read, read-post) each
// page's browser scripts can send, per scope kind (D-TNJ-20261109-CRAWLER-SWEEP).
//
//   node scripts/generate_ui_read_reach.js            write the file
//   node scripts/generate_ui_read_reach.js --check    exit 1 when it is stale
//
// Options: --root DIR (repository root), --out FILE (reach file),
// --manifest FILE (route manifest). See qa/ui-crawl/reach.js.

const fs = require("node:fs");
const path = require("node:path");
const { buildReach, renderReach, REACH_PATH } = require("../qa/ui-crawl/reach");
const { loadManifest, MANIFEST_PATH } = require("../qa/ui-crawl/routes");

function parseArgs(argv) {
  const args = { check: false, root: path.resolve(__dirname, ".."), out: REACH_PATH, manifest: MANIFEST_PATH };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === "--check") args.check = true;
    else if (flag === "--root") args.root = path.resolve(argv[++i]);
    else if (flag === "--out") args.out = path.resolve(argv[++i]);
    else if (flag === "--manifest") args.manifest = path.resolve(argv[++i]);
    else throw new Error(`unknown argument ${flag}`);
  }
  return args;
}

function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const { reach, problems } = buildReach(args.root, { manifest: loadManifest(args.manifest) });
  if (problems.length) {
    process.stderr.write(`the browser scripts' /api/ calls could not all be resolved:\n${problems.join("\n")}\n`);
    return 1;
  }
  const text = renderReach(reach);
  if (args.check) {
    const current = fs.existsSync(args.out) ? fs.readFileSync(args.out, "utf8") : null;
    if (current === text) return 0;
    process.stderr.write(`${path.relative(args.root, args.out) || args.out} is stale: a page script's reachable reads changed. `
      + "Review the change (a new read is admitted on that page and scope, bound to the target), then run "
      + "node scripts/generate_ui_read_reach.js and commit the result.\n");
    return 1;
  }
  fs.writeFileSync(args.out, text);
  return 0;
}

if (require.main === module) process.exit(main());

module.exports = { main, parseArgs };
