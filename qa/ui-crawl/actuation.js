"use strict";

// Safety-class enforcement for one inventory state (#ui-coverage).
//
// This is the only code that decides whether a state may change anything.
// It is deliberately small; the driver supplies `runSteps`, the request
// guard, dialog handling and the semantic store reads, so the rules here are
// testable without a browser (tests/js/ui_crawl*.test.js).
//
//   read              steps run with no write window. Declared `read_requests`
//                     (export download; appliance polls for appliance_load
//                     states) open a read window bound to the target.
//   app-write-revert  capture -> semantic store read -> open a window for the
//                     declared writes, bound to the write system, the target
//                     enclosure and the captured bay -> steps -> revert
//                     (always, once anything was written) -> close window ->
//                     verify by re-reading -> semantic store read again, which
//                     must equal the first. Any failure is `revert-failed`.
//   led               same shape: identify on, then off (revert retried), then
//                     verify it reads back off.
//   forbidden         never actuated. With `confirm_preview` and an explicit
//                     opt-in, the control may be pressed with every native
//                     dialog dismissed and no window open. The preview passes
//                     only if a dialog was seen and dismissed and the page sent
//                     nothing that needed admission; otherwise `preview-failed`.

const { SAFETY_CLASSES, substitute } = require("./common");
const { DECLARABLE } = require("./routes");

function result(state, status, extra = {}) {
  return { state: state.id, safety: state.safety, status, ...extra };
}

function errorText(error) {
  return error ? String(error.message || error).split("\n")[0].slice(0, 300) : null;
}

function windowOptions(driver, classes) {
  return { bind: driver.bind || {}, service: driver.service || "ui", classes: [...classes] };
}

async function runRead(driver, state) {
  const reads = substitute(state.read_requests || [], driver.vars);
  const classes = new Set(DECLARABLE.read);
  if (state.appliance_load) classes.add("appliance-load");
  if (reads.length) driver.guard.openReads(state.id, reads, windowOptions(driver, classes));
  let admitted = [];
  try {
    await driver.runSteps(state.steps || [], driver.vars);
  } finally {
    if (reads.length) admitted = driver.guard.closeWrites();
  }
  return result(state, "done", admitted.length ? { admitted } : {});
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  return value;
}

function semanticDiff(before, after) {
  const out = [];
  for (const key of Object.keys(before)) {
    if (JSON.stringify(stable(before[key])) !== JSON.stringify(stable(after[key]))) out.push(key);
  }
  return out;
}

async function runReverted(driver, state, options) {
  const vars = driver.vars;
  if (state.capture) await driver.runSteps(state.capture, vars);
  if (state.precondition && typeof driver.checkPrecondition === "function") {
    const reason = await driver.checkPrecondition(state.precondition, vars);
    if (reason) return result(state, "not-applicable", { reason });
  }
  const semantic = state.semantic_reads || [];
  let before = null;
  if (semantic.length) {
    if (typeof driver.semanticSnapshot !== "function") return result(state, "precheck-failed", { error: "no semantic store reader" });
    try {
      before = await driver.semanticSnapshot(semantic, vars);
    } catch (error) {
      // Fail closed: nothing is written when the store cannot be read first.
      return result(state, "precheck-failed", { error: `store read before the write failed: ${errorText(error)}` });
    }
  }
  driver.guard.openWrites(state.id, substitute(state.writes, vars), [], windowOptions(driver, DECLARABLE[state.safety]));
  let actError = null;
  let revertError = null;
  let admitted = [];
  let reverted = false;
  try {
    try {
      await driver.runSteps(state.steps, vars);
    } catch (error) {
      actError = error;
    }
    const wrote = driver.guard.window.admitted.length > 0;
    if (wrote) {
      const attempts = 1 + (options.revertRetries ?? 2);
      for (let attempt = 1; attempt <= attempts && !reverted; attempt += 1) {
        try {
          await driver.runSteps(state.revert, vars);
          reverted = true;
          revertError = null;
        } catch (error) {
          revertError = error;
          if (attempt < attempts && typeof driver.pause === "function") await driver.pause(2000 * attempt);
        }
      }
    }
  } finally {
    admitted = driver.guard.closeWrites();
  }
  if (admitted.length === 0) {
    return result(state, actError ? "failed" : "no-write", { error: errorText(actError), admitted });
  }
  let verifyError = null;
  try {
    await driver.runSteps(state.verify, vars);
  } catch (error) {
    verifyError = error;
  }
  let semanticError = null;
  if (before && !verifyError) {
    try {
      const after = await driver.semanticSnapshot(semantic, vars);
      const changed = semanticDiff(before, after);
      if (changed.length) semanticError = new Error(`store differs after the revert: ${changed.join(", ")}`);
    } catch (error) {
      semanticError = new Error(`store read after the revert failed: ${errorText(error)}`);
    }
  }
  if (!reverted || verifyError || semanticError) {
    return result(state, "revert-failed", {
      error: errorText(revertError || verifyError || semanticError),
      actError: errorText(actError),
      admitted,
    });
  }
  return result(state, actError ? "failed" : "done", {
    error: errorText(actError), admitted, reverted: true, verified: true, semanticChecked: semantic.length > 0,
  });
}

async function runForbidden(driver, state, options) {
  if (!state.confirm_preview || !options.confirmPreview || !(state.steps || []).length) {
    return result(state, "listed", { reason: state.reason || "forbidden" });
  }
  if (driver.guard.window) throw new Error("write window open before a forbidden state");
  // Every native dialog is dismissed; nothing may arm acceptance here.
  driver.acceptNextDialog = false;
  const before = driver.dialogsSeen;
  const loggedBefore = driver.guard.log.length;
  const steps = state.steps.filter((step) => !("acceptNextDialog" in step));
  let error = null;
  try {
    await driver.runSteps(steps, driver.vars);
  } catch (caught) {
    error = caught;
  } finally {
    driver.acceptNextDialog = false;
  }
  if (typeof driver.settle === "function") await driver.settle();
  const dialogs = driver.dialogsSeen - before;
  const attempted = driver.guard.log.slice(loggedBefore).map((entry) => `${entry.method} ${entry.path} (${entry.reason})`);
  if (error) return result(state, "preview-failed", { dialogs, attempted, error: errorText(error) });
  if (dialogs < 1) return result(state, "preview-failed", { dialogs, attempted, error: "no confirmation dialog was shown and dismissed" });
  if (attempted.length) return result(state, "preview-failed", { dialogs, attempted, error: "the page tried to send a request after the dialog was dismissed" });
  return result(state, "previewed", { dialogs });
}

// `options.allow` is the set of classes the run may actuate; read is always
// allowed. Forbidden is never in it.
async function runState(driver, state, options = {}) {
  if (!SAFETY_CLASSES.includes(state.safety)) throw new Error(`${state.id}: unknown safety class ${state.safety}`);
  const allow = new Set(["read", ...(options.allow || [])]);
  allow.delete("forbidden");
  if (state.safety === "forbidden") return runForbidden(driver, state, options);
  if (!allow.has(state.safety)) return result(state, "skipped", { reason: `${state.safety} not allowed in this run` });
  if (state.safety === "read") return runRead(driver, state);
  return runReverted(driver, state, options);
}

module.exports = { SAFETY_CLASSES, runState, semanticDiff };
