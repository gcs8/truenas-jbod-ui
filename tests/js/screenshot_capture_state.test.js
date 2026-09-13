"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { normalizeCaptureState } = require("../../scripts/screenshot_capture_state.js");

test("normalizer moves the pointer away, blurs focus, and waits two frames", async () => {
  const events = [];
  const activeElement = {
    blur() {
      events.push("blur");
    },
  };
  const priorDocument = global.document;
  const priorAnimationFrame = global.requestAnimationFrame;
  global.document = {
    activeElement,
    querySelectorAll(selector) {
      assert.equal(selector, "button:hover, a:hover, [role='button']:hover");
      events.push("hover-check");
      return [];
    },
  };
  global.requestAnimationFrame = (callback) => {
    events.push("frame");
    callback();
  };
  const page = {
    mouse: {
      async move(x, y) {
        events.push(`move:${x},${y}`);
      },
    },
    async evaluate(callback) {
      return callback();
    },
  };

  try {
    await normalizeCaptureState(page);
  } finally {
    global.document = priorDocument;
    global.requestAnimationFrame = priorAnimationFrame;
  }

  assert.deepEqual(events, ["move:0,0", "blur", "frame", "frame", "hover-check"]);
});

test("normalizer fails if an interactive element remains hovered", async () => {
  const priorDocument = global.document;
  const priorAnimationFrame = global.requestAnimationFrame;
  global.document = {
    activeElement: null,
    querySelectorAll() {
      return [{ id: "still-hovered" }];
    },
  };
  global.requestAnimationFrame = (callback) => callback();
  const page = {
    mouse: { async move() {} },
    async evaluate(callback) {
      return callback();
    },
  };

  try {
    await assert.rejects(normalizeCaptureState(page), /interactive element remains hovered/);
  } finally {
    global.document = priorDocument;
    global.requestAnimationFrame = priorAnimationFrame;
  }
});
