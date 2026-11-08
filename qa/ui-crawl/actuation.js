"use strict";

// Safety-class enforcement for one inventory state (#ui-coverage).
//
// This is the only code that decides whether a state may change anything.
// It is deliberately small; the driver supplies `runSteps`, the request
// guard, and dialog handling, so the rules here are testable without a
// browser (tests/js/ui_crawl.test.js).
//
//   read              steps run with the write window closed.
//   app-write-revert  capture -> open window for the declared writes -> steps
//                     -> revert (always, once anything was written) -> close
//                     window -> verify by re-reading. A failed revert or
//                     verify is reported as `revert-failed`, the loudest result.
//   led               same shape: identify on, then off (revert retried), then
//                     verify it reads back off.
//   forbidden         never actuated. With `confirm_preview` and an explicit
//                     opt-in, the control may be pressed with every native
//                     dialog dismissed and the write window closed, so the
//                     guard aborts any request it would send.

const SAFETY_CLASSES = ["read", "app-write-revert", "led", "forbidden"];

function result(state, status, extra = {}) {
  return { state: state.id, safety: state.safety, status, ...extra };
}

function errorText(error) {
  return error ? String(error.message || error).split("\n")[0].slice(0, 300) : null;
}

async function runRead(driver, state) {
  const reads = state.read_requests || [];
  if (reads.length) driver.guard.openReads(state.id, reads);
  try {
    await driver.runSteps(state.steps || [], driver.vars);
    return result(state, "done");
  } finally {
    if (reads.length) driver.guard.closeWrites();
  }
}

async function runReverted(driver, state, options) {
  const vars = driver.vars;
  if (state.capture) await driver.runSteps(state.capture, vars);
  if (state.precondition && typeof driver.checkPrecondition === "function") {
    const reason = await driver.checkPrecondition(state.precondition, vars);
    if (reason) return result(state, "not-applicable", { reason });
  }
  driver.guard.openWrites(state.id, state.writes);
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
  if (!reverted || verifyError) {
    return result(state, "revert-failed", {
      error: errorText(revertError || verifyError),
      actError: errorText(actError),
      admitted,
    });
  }
  return result(state, actError ? "failed" : "done", { error: errorText(actError), admitted, reverted: true, verified: true });
}

async function runForbidden(driver, state, options) {
  if (!state.confirm_preview || !options.confirmPreview || !(state.steps || []).length) {
    return result(state, "listed", { reason: state.reason || "forbidden" });
  }
  if (driver.guard.window) throw new Error("write window open before a forbidden state");
  // Every native dialog is dismissed; nothing may arm acceptance here.
  driver.acceptNextDialog = false;
  const before = driver.dialogsSeen;
  const steps = state.steps.filter((step) => !("acceptNextDialog" in step));
  try {
    await driver.runSteps(steps, driver.vars);
  } finally {
    driver.acceptNextDialog = false;
  }
  return result(state, "previewed", { dialogs: driver.dialogsSeen - before });
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

module.exports = { SAFETY_CLASSES, runState };
