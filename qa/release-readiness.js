"use strict";

const { expect } = require("@playwright/test");
const settleTimeout = Number.parseInt(process.env.PLAYWRIGHT_SYSTEM_SETTLE_TIMEOUT_MS || "90000", 10);

async function waitForSelectedScope(page, { systemId, enclosureValue } = {}) {
  systemId ??= await page.locator("#system-select").inputValue();
  enclosureValue ??= await page.locator("#enclosure-select").inputValue();
  // Read the complete visible contract together. Sequential locator assertions
  // can combine an old hidden runtime note with a new settled countdown.
  await expect.poll(() => page.evaluate(async ([system, enclosure]) => {
    const visible = node => Boolean(node && getComputedStyle(node).visibility !== "hidden"
      && node.getBoundingClientRect().width && node.getBoundingClientRect().height);
    const ready = () => {
      const grid = document.getElementById("slot-grid");
      const scopeNote = document.getElementById("inventory-scope-note");
      const runtimeNote = document.getElementById("storage-view-runtime-note");
      const countdown = document.getElementById("refresh-countdown-label");
      const status = document.getElementById("status-text");
      const params = new URL(location.href).searchParams;
      return document.getElementById("system-select")?.value === system
        && document.getElementById("enclosure-select")?.value === enclosure
        && visible(grid) && grid.getAttribute("aria-busy") === "false" && !grid.inert
        && scopeNote && !visible(scopeNote) && runtimeNote && !visible(runtimeNote)
        && countdown && countdown.textContent.trim() !== "Refreshing..."
        && status && status.getAttribute("data-tone") !== "error"
        // syncLocation is published by the real render, including local views.
        && params.get("system_id") === system
        && (enclosure.startsWith("view:") ? `view:${params.get("storage_view_id")}`
          : `enclosure:${params.get("enclosure_id")}`) === enclosure;
    };
    if (!ready()) return false;
    // Recheck across a real paint opportunity, not a timer or network-idle guess.
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return ready();
  }, [systemId, enclosureValue]), { timeout: settleTimeout }).toBe(true);
}

async function refreshSelectedScope(page, action, { systemId, enclosureId, force = false, viewId = null }) {
  const origin = new URL(page.url()).origin;
  let ownedRequest;
  let latestRequest;
  const observeRequest = request => {
    const url = new URL(request.url());
    if (url.origin === origin && url.pathname === "/api/inventory"
      && request.method() === "GET" && url.searchParams.get("system_id") === systemId
      && (enclosureId === undefined || url.searchParams.get("enclosure_id") === enclosureId)
      && url.searchParams.get("force") === String(force)) {
      ownedRequest ??= request;
      latestRequest = request;
    }
  };
  // Own a request dispatched after registration, not just a response arriving
  // afterward. An already-in-flight request with the identical URL is excluded.
  page.on("request", observeRequest);
  const responsePromise = page.waitForResponse(response => response.request() === ownedRequest,
    { timeout: settleTimeout });
  try {
    const [, response] = await Promise.all([Promise.resolve().then(action), responsePromise]);
    expect(response.ok()).toBe(true);
    const snapshot = await response.json();
    expect(snapshot.ok).not.toBe(false);
    expect(snapshot.selected_system_id).toBe(systemId);
    const requestedEnclosure = new URL(response.url()).searchParams.get("enclosure_id");
    if (requestedEnclosure) expect(snapshot.selected_enclosure_id).toBe(requestedEnclosure);
    await waitForSelectedScope(page, { systemId, enclosureValue: viewId
      ? `view:${viewId}` : `enclosure:${snapshot.selected_enclosure_id}` });
    // A later matching dispatch supersedes this operation, even if its final
    // selected scope happens to be identical. It must not borrow our response.
    expect(latestRequest, "readiness request was superseded").toBe(ownedRequest);
  } finally {
    page.off("request", observeRequest);
  }
}

async function switchSelectedScope(page, selector, value) {
  await waitForSelectedScope(page);
  if (await page.locator(selector).inputValue() === value) return;
  const systemId = selector === "#system-select" ? value : await page.locator("#system-select").inputValue();
  if (selector === "#enclosure-select" && value.startsWith("view:")) {
    await page.locator(selector).selectOption(value);
    await waitForSelectedScope(page, { systemId, enclosureValue: value });
    return;
  }
  await refreshSelectedScope(page, () => page.locator(selector).selectOption(value), {
    // A system switch may restore its cached enclosure before dispatch.
    systemId, enclosureId: selector === "#system-select" ? undefined : value.slice("enclosure:".length),
  });
}

module.exports = { waitForSelectedScope, refreshSelectedScope, switchSelectedScope };
