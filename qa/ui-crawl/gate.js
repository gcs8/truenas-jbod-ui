"use strict";

// Fail-closed terminal status of a crawl (#1119).
//
// The crawl's exit status is derived from its evidence, never from the
// absence of a known-bad row. Anything not explicitly safe fails:
//
//   exit 0  ok                 every actuation ended in a safe status, no
//                              request was blocked, every admitted write
//                              belongs to an actuation, identify cleanup (if
//                              any) verified off, and no state errored.
//   exit 1  crawl-errors       safety held, but states or captures errored
//                              (coverage is incomplete; review before use).
//   exit 2  safety-failed      a revert, verify, semantic store check or
//                              preview failed; a request was blocked (an
//                              unknown route, a write outside its window or
//                              scope); an admitted write is unaccounted for;
//                              or a report could not be read or merged.
//   exit 3  cleanup-uncertain  an identify light could not be verified off.

const SAFE_ACTUATION = new Set(["done", "no-write", "not-applicable", "skipped", "listed", "previewed"]);
const EXIT = { ok: 0, "crawl-errors": 1, "safety-failed": 2, "cleanup-uncertain": 3 };

function evaluateGate({ actuations = [], stats = {}, records = [], ledCleanup = null, problems = [] } = {}) {
  const failures = [];
  for (const actuation of actuations) {
    if (!SAFE_ACTUATION.has(actuation.status)) {
      failures.push(`actuation ${actuation.state} on ${actuation.target || "?"}: ${actuation.status}${actuation.error ? ` (${actuation.error})` : ""}`);
    }
  }
  for (const blocked of stats.blocked || []) failures.push(`blocked request: ${blocked}`);
  const accounted = actuations.reduce((sum, actuation) => sum + (actuation.admitted || []).filter((entry) => !entry.cls || ["app-write", "led", "forbidden"].includes(entry.cls)).length, 0);
  const admitted = (stats.writes || []).length;
  if (admitted !== accounted) failures.push(`unexpected write: ${admitted} non-read requests admitted, ${accounted} accounted for by actuations`);
  for (const error of stats.routeErrors || []) {
    if (!/^(GET|HEAD) /.test(error)) failures.push(`request routing error: ${error}`);
  }
  failures.push(...problems);
  const cleanupFailed = ledCleanup && ledCleanup.ok === false;
  const crawlErrors = records.filter((record) => record.status === "error").map((record) => `${record.target} ${record.state}: error`);
  let status = "ok";
  if (crawlErrors.length) status = "crawl-errors";
  if (failures.length) status = "safety-failed";
  if (cleanupFailed) status = "cleanup-uncertain";
  return { status, exitCode: EXIT[status], failures, crawlErrors, ledCleanup };
}

module.exports = { evaluateGate, SAFE_ACTUATION, EXIT };
