"use strict";

// #924: selection in the main Connections panel is optional and does not own
// the fault state. Production assets and templates; only HTTP is synthetic.
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const CONTROLLER = "controller:synthetic-hba";
const FAILED_PATH = "path:synthetic-hba:fail";

function syntheticFabric() {
  const bays = [0, 1, 2, 3];
  return {
    available: true,
    platform: "linux",
    system_id: "synthetic-system",
    selected_enclosure_id: "enc-a",
    raw: { fabric_domain: "storage_fabric", fabric_kind: "linux_ses" },
    nodes: [
      {
        id: CONTROLLER,
        kind: "controller",
        label: "Synthetic HBA",
        related_slots: bays,
        metrics: {},
        raw: {},
      },
      {
        id: "node:synthetic-expander",
        kind: "expander",
        label: "Synthetic expander",
        controller_id: CONTROLLER,
        related_slots: bays,
        metrics: {},
        raw: {},
      },
    ],
    controllers: [{ id: CONTROLLER, name: "synthetic-hba", related_slots: bays }],
    paths: [
      {
        id: FAILED_PATH,
        controller: "synthetic-hba",
        label: "Failed path",
        state: "fail",
        slots: [0, 1],
        count: 2,
      },
    ],
    traces: [
      ...bays.map((bay) => ({
        id: `bay:${bay}`,
        kind: "bay",
        label: `Synthetic bay ${bay}`,
        slots: [bay],
        node_ids: [CONTROLLER, "node:synthetic-expander"],
        metrics: {},
      })),
      {
        id: FAILED_PATH,
        kind: "path",
        label: "Failed path",
        slots: [0, 1],
        node_ids: [CONTROLLER],
        metrics: { state: "fail" },
      },
    ],
    links: [],
    aliases: [],
    warnings: [],
    sources: {},
  };
}

let fixture;
test.beforeAll(() => {
  fixture = JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json
from scripts.build_current_source_browser_fixture import build_live_fixture_html, build_synthetic_live_snapshot
from app.models.domain import StorageViewRuntimePayload
runtime = StorageViewRuntimePayload(system_id="synthetic-system", views=[])
print(json.dumps(dict(html=build_live_fixture_html(runtime), snapshot=build_synthetic_live_snapshot().model_dump(mode="json"))))
`], { cwd: root, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 }));
});

test.use({ viewport: { width: 1600, height: 1000 }, serviceWorkers: "block" });

async function openConnections(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== "https://synthetic.invalid") throw new Error(`Unexpected external request ${url.origin}`);
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: fixture.html });
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(root, "app", url.pathname);
      return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
    }
    let data = { ok: true, available: false };
    if (url.pathname === "/api/inventory") data = fixture.snapshot;
    if (url.pathname === "/api/storage-views") data = { system_id: "synthetic-system", views: [] };
    if (url.pathname === "/api/sas-fabric") data = syntheticFabric();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto("https://synthetic.invalid/");
  await page.locator("#auto-refresh-toggle").uncheck();
  const selectedGridBay = page.locator("#slot-grid .slot-tile.selected");
  await expect(selectedGridBay).toHaveCount(1);
  await selectedGridBay.click();
  await expect(selectedGridBay).toHaveCount(0);
  await page.locator("#sas-fabric-toggle-button").click();
  await expect(page.locator("#sas-fabric-lanes .sas-fabric-lane")).toHaveCount(1);
  return errors;
}

async function expectFullMap(page) {
  await expect(page.locator("#sas-fabric-panel .is-selected")).toHaveCount(0);
  await expect(page.locator("#slot-grid .fabric-highlight")).toHaveCount(0);
  await expect(page.locator("#slot-grid .fabric-dimmed")).toHaveCount(0);
  await expect(page.locator("#sas-fabric-show-all-button")).toBeHidden();
  await expect(page.locator(`[data-sas-fabric-trace="${FAILED_PATH}"]`)).toHaveClass(/status-fail/);
}

test("path, node and bay selections can return to the full map without clearing faults", async ({ page }) => {
  const errors = await openConnections(page);
  const failedPath = page.locator(`[data-sas-fabric-trace="${FAILED_PATH}"]`);
  const controller = page.locator(`[data-sas-fabric-node="${CONTROLLER}"]`).first();
  const showAll = page.locator("#sas-fabric-show-all-button");

  await expectFullMap(page);

  await failedPath.click();
  await expect(failedPath).toHaveClass(/is-selected/);
  await expect(showAll).toBeVisible();
  await expect(page.locator("#slot-grid .fabric-highlight")).toHaveCount(1);
  await failedPath.click();
  await expectFullMap(page);

  await controller.click();
  await expect(controller).toHaveClass(/is-selected/);
  await controller.click();
  await expectFullMap(page);

  const bay = page.locator('[data-sas-fabric-slot="0"]').first();
  await bay.click();
  await expect(page.locator('#slot-grid .slot-tile[data-slot="0"]')).toHaveClass(/selected/);
  await expect(page.locator("#slot-grid .fabric-highlight")).toHaveCount(1);
  await bay.click();
  await expect(page.locator("#slot-grid .slot-tile.selected")).toHaveCount(0);
  await expectFullMap(page);

  await failedPath.click();
  await showAll.click();
  await expectFullMap(page);

  await controller.click();
  await controller.focus();
  await page.keyboard.press("Escape");
  await expectFullMap(page);

  await page.locator("#sas-fabric-refresh-button").click();
  await expectFullMap(page);
  expect(errors).toEqual([]);
});
