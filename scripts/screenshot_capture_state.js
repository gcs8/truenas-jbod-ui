#!/usr/bin/env node
"use strict";

async function normalizeCaptureState(page) {
  await page.mouse.move(0, 0);
  const hoveredInteractiveCount = await page.evaluate(async () => {
    const activeElement = document.activeElement;
    if (activeElement && typeof activeElement.blur === "function") {
      activeElement.blur();
    }
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return document.querySelectorAll("button:hover, a:hover, [role='button']:hover").length;
  });
  if (hoveredInteractiveCount !== 0) {
    throw new Error("capture state is unstable: an interactive element remains hovered");
  }
}

module.exports = { normalizeCaptureState };
