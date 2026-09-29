"use strict";

// The checked-in public-demo/index.html is only rebuilt when a release is cut,
// so between releases it may embed older sources than the working tree. Tests
// that assert behaviour of the generated demo read a throwaway build of the
// current source instead, named by PUBLIC_DEMO_ARTIFACT. CI's "Checked-in public
// demo artifact" job builds one and runs the unit tests with it set; without it
// these tests skip with the reason below rather than test stale bytes.

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const SKIP_REASON =
  "set PUBLIC_DEMO_ARTIFACT to a current-source build "
  + "(python scripts/build_public_demo.py --output <path>)";

function currentSourceDemo() {
  const requested = process.env.PUBLIC_DEMO_ARTIFACT;
  if (!requested) {
    return null;
  }
  const artifactPath = path.isAbsolute(requested) ? requested : path.resolve(ROOT, requested);
  return fs.readFileSync(artifactPath, "utf8");
}

module.exports = { SKIP_REASON, currentSourceDemo };
