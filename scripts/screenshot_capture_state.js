#!/usr/bin/env node
"use strict";

async function normalizeCaptureState(page) {
  await page.mouse.move(0, 0);
  const state = await page.evaluate(async () => {
    const activeElement = document.activeElement;
    if (activeElement && typeof activeElement.blur === "function") {
      activeElement.blur();
    }
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const interactiveSelector = "button, a[href], input, select, textarea, [role='button'], [tabindex]:not([tabindex='-1'])";
    return {
      focusedInteractive: Boolean(document.activeElement?.matches?.(interactiveSelector)),
      hoveredInteractiveCount: document.querySelectorAll(
        "button:hover, a[href]:hover, input:hover, select:hover, textarea:hover, [role='button']:hover, [tabindex]:not([tabindex='-1']):hover"
      ).length,
    };
  });
  if (state.focusedInteractive) {
    throw new Error("capture state is unstable: an interactive element remains focused");
  }
  if (state.hoveredInteractiveCount !== 0) {
    throw new Error("capture state is unstable: an interactive element remains hovered");
  }
}

module.exports = { normalizeCaptureState };
