"use strict";

module.exports = function registerFixtureSuite({ test, expect }) {
  const { execFileSync } = require("node:child_process");
  const fs = require("node:fs");
  const path = require("node:path");
  const { observeAocCarrier } = require("../esxi-carrier-observer");
  const { waitForSelectedScope } = require("../release-readiness");
  const root = path.resolve(__dirname, "../..");
  const origin = "https://synthetic.invalid";
  let fixtures;
  test.beforeAll(() => {
    fixtures = JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-m", "tests.esxi_carrier_fixture"], {
      cwd: root, encoding: "utf8", maxBuffer: 8 * 1024 * 1024,
    }));
  });
  test.use({ viewport: { width: 1600, height: 900 }, serviceWorkers: "block" });

  async function open(page, kind) {
    const fixture = structuredClone(fixtures[kind]);
    const systemId = fixture.snapshot.selected_system_id;
    const requests = [];
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", async route => {
      const request = route.request();
      const url = new URL(request.url());
      // Abort every unexpected request rather than falling through to a real server.
      const readBatch = request.method() === "POST" && url.pathname === "/api/slots/smart-batch";
      if (url.origin !== origin || (request.method() !== "GET" && !readBatch)) {
        errors.push(`Unexpected request: ${request.method()} ${url.origin}${url.pathname}`);
        return route.abort();
      }
      if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: fixture.html });
      if (url.pathname.startsWith("/static/") && !url.pathname.includes("..")) {
        const file = path.join(root, "app", url.pathname);
        return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
      }
      let data;
      if (readBatch) return route.fulfill({ contentType: "application/json", body: JSON.stringify({
        summaries: request.postDataJSON().slots.map(slot => ({ slot, summary: fixture.smart[slot] })),
      }) });
      if (url.pathname === "/api/inventory") {
        requests.push(url);
        expect(url.searchParams.get("system_id")).toBe(systemId);
        expect(url.searchParams.get("force")).toBe("false");
        data = fixture.snapshot;
      } else if (url.pathname === "/api/storage-views") data = fixture.runtime;
      else if (/^\/api\/slots\/[01]\/smart$/.test(url.pathname)) data = fixture.smart[url.pathname.split("/")[3]];
      else if (url.pathname === "/api/history/status") data = { available: false, configured: false };
      else {
        errors.push(`Unexpected route: ${url.pathname}`);
        return route.abort();
      }
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(`${origin}/`);
    await waitForSelectedScope(page);
    const toggle = page.locator("#auto-refresh-toggle");
    if (await toggle.isChecked()) await toggle.click();
    // These snapshots use the real built-in carrier profile, not a free-form layout.
    expect(fixture.snapshot.layout_rows).toEqual([[1], [0]]);
    const tiles = page.locator("#slot-grid .slot-tile");
    expect(await tiles.evaluateAll(nodes => nodes.map(node => node.dataset.slot))).toEqual(["1", "0"]);
    await expect(tiles.locator(".storage-view-runtime-card-slot")).toHaveText(["01", "00"]);
    const upper = await tiles.nth(0).boundingBox();
    const lower = await tiles.nth(1).boundingBox();
    expect(upper.y).toBeLessThan(lower.y);
    return { fixture, requests, errors };
  }

  for (const kind of ["unread", "populated"]) {
    test(`${kind} source passes the shared restored-live observer`, async ({ page }) => {
      const http = await open(page, kind);
      await observeAocCarrier(page);
      expect(http.errors).toEqual([]);
      if (kind === "populated") {
        await expect(page.locator("#detail-content")).toContainText("SANITIZED-AOC-1");
      }
    });
  }

  for (const [name, corrupt, failure] of [
    ["missing source warning", http => { http.fixture.snapshot.warnings = []; }, /toBe/],
    ["scoped layout differs from rendered order", http => { http.fixture.snapshot.layout_rows = [[0, 1]]; }, /toEqual/],
    ["wrong system response", http => { http.fixture.snapshot.selected_system_id = "synthetic-other"; }, /toBe/],
    ["wrong enclosure response", http => { http.fixture.snapshot.selected_enclosure_id = "synthetic-other"; }, /toBe/],
    ["known empty is not unread", http => {
      for (const slot of http.fixture.snapshot.slots) slot.presence_known = true;
    }, /toBe/],
  ]) {
    test(`unread observer rejects ${name}`, async ({ page }) => {
      const http = await open(page, "unread");
      corrupt(http);
      await expect(observeAocCarrier(page)).rejects.toThrow(failure);
    });
  }

  test("unread observer rejects a misleading matched caption", async ({ page }) => {
    await open(page, "unread");
    await page.locator("#slot-grid .slot-tile").first().locator(".storage-view-runtime-card-summary")
      .evaluate(node => { node.textContent += " Matched from live inventory."; });
    await expect(observeAocCarrier(page)).rejects.toThrow(/not.toContainText/);
  });

  test("observer rejects reversed bay order", async ({ page }) => {
    await open(page, "populated");
    await page.locator("#slot-grid .slot-tile").first().evaluate(node => node.parentNode.appendChild(node));
    await expect(observeAocCarrier(page)).rejects.toThrow(/toEqual/);
  });

  test("complete empty census is empty, not unread", async ({ page }) => {
    const http = await open(page, "empty");
    const tiles = page.locator("#slot-grid .slot-tile");
    expect(await tiles.evaluateAll(nodes => nodes.map(node => node.dataset.slot))).toEqual(["1", "0"]);
    for (const tile of [tiles.first()]) {
      await expect(tile).toHaveClass(/state-empty/);
      await expect(tile.locator(".storage-view-runtime-card-device")).toHaveText("Empty");
      await expect(tile).not.toContainText("Unknown");
      await expect(tile).not.toContainText("Matched from live inventory.");
    }
    expect(http.errors).toEqual([]);
  });
};
