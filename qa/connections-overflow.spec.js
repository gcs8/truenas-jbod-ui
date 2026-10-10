"use strict";

// #914: every node and bay in the Connections panel is reachable. Complete
// production assets and templates; only HTTP is synthetic.
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const CONTROLLER = "controller:synthetic-hba";
const TRACE_NODES = 15;
const ENCLOSURES = 9;
const PATH_BAYS = 24;

function syntheticFabric() {
  const bays = Array.from({ length: PATH_BAYS }, (_, bay) => bay);
  const traceNodes = Array.from({ length: TRACE_NODES }, (_, index) => ({
    id: `node:trace-${index}`,
    kind: "expander",
    label: `Synthetic trace node ${String(index).padStart(2, "0")}`,
    controller_id: index < 3 ? CONTROLLER : "controller:elsewhere",
    related_slots: [0],
    metrics: {},
    raw: {},
  }));
  const enclosures = Array.from({ length: ENCLOSURES }, (_, index) => ({
    id: `node:enclosure-${index}`,
    kind: "storage-enclosure",
    label: `Synthetic view ${String(index).padStart(2, "0")}`,
    controller_id: CONTROLLER,
    related_slots: [index],
    metrics: {},
    raw: {},
  }));
  return {
    available: true,
    platform: "linux",
    system_id: "synthetic-system",
    selected_enclosure_id: "enc-a",
    raw: { fabric_domain: "storage_fabric", fabric_kind: "linux_ses" },
    nodes: [
      { id: CONTROLLER, kind: "controller", label: "synthetic-hba", related_slots: bays, metrics: {}, raw: {} },
      ...traceNodes,
      ...enclosures,
    ],
    controllers: [{ id: CONTROLLER, name: "synthetic-hba", related_slots: bays }],
    paths: [{ id: "path:synthetic-hba:mapped", controller: "synthetic-hba", state: "mapped", slots: bays, count: PATH_BAYS }],
    traces: [
      { id: "bay:0", kind: "bay", label: "Disk 0", slots: [0], node_ids: traceNodes.map((node) => node.id), metrics: {} },
      { id: "path:synthetic-hba:mapped", kind: "path", label: "mapped", slots: bays, node_ids: [CONTROLLER], metrics: {} },
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
  const fabric = syntheticFabric();
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
    if (url.pathname === "/api/sas-fabric") data = fabric;
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto("https://synthetic.invalid/");
  await page.locator("#auto-refresh-toggle").uncheck();
  await page.locator("#sas-fabric-toggle-button").click();
  await expect(page.locator("#sas-fabric-lanes .sas-fabric-lane")).toHaveCount(1);
  return errors;
}

async function nestedInteractiveCount(page) {
  return page.evaluate(() => document.querySelectorAll(
    "#sas-fabric-panel button button, #sas-fabric-panel button a, #sas-fabric-panel button input, #sas-fabric-panel a button"
  ).length);
}

test("a storage path card lists every bay inside the card", async ({ page }) => {
  const errors = await openConnections(page);
  const card = page.locator("#sas-fabric-lanes .sas-fabric-path-card");
  await expect(card).toHaveCount(1);
  const expected = Array.from({ length: PATH_BAYS }, (_, bay) => String(bay).padStart(2, "0")).join(", ");
  await expect(card.locator(".sas-fabric-item-slots")).toHaveText(expected);
  // Nothing escapes the card into the path list.
  await expect(page.locator("#sas-fabric-lanes .sas-fabric-path-list > :not(.sas-fabric-path-card)")).toHaveCount(0);
  expect(await nestedInteractiveCount(page)).toBe(0);
  expect(errors).toEqual([]);
});

test("Enclosures / Views expands to every node and collapses again", async ({ page }) => {
  const errors = await openConnections(page);
  const stage = page.locator("#sas-fabric-lanes .sas-fabric-stage").filter({ hasText: "Enclosures / Views" });
  const nodes = stage.locator("[data-sas-fabric-node]");
  await expect(nodes).toHaveCount(6);
  const toggle = stage.locator("button[data-sas-fabric-expand-slots]");
  await expect(toggle).toHaveText(`+${ENCLOSURES - 6}`);
  await expect(toggle).toHaveAttribute("aria-expanded", "false");

  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(nodes).toHaveCount(ENCLOSURES);
  await expect(toggle).toHaveText("Show fewer");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(toggle).toBeFocused();
  for (let index = 0; index < ENCLOSURES; index += 1) {
    await expect(stage.locator(`[data-sas-fabric-node="node:enclosure-${index}"]`)).toBeVisible();
  }

  await toggle.click();
  await expect(nodes).toHaveCount(6);
  await expect(toggle).toHaveText(`+${ENCLOSURES - 6}`);
  expect(await nestedInteractiveCount(page)).toBe(0);
  expect(errors).toEqual([]);
});

test("Selected Bay trace nodes are all reachable", async ({ page }) => {
  const errors = await openConnections(page);
  await expect(page.locator('[data-sas-fabric-slot="0"]').first()).toHaveClass(/is-selected/);
  const inspector = page.locator("#sas-fabric-inspector-body");
  await expect(inspector.locator("h4", { hasText: "Trace Nodes" })).toBeVisible();
  const nodes = inspector.locator("[data-sas-fabric-node]");
  await expect(nodes).toHaveCount(12);
  const toggle = inspector.locator("button[data-sas-fabric-expand-slots]");
  await expect(toggle).toHaveText(`+${TRACE_NODES - 12}`);

  await toggle.click();
  await expect(nodes).toHaveCount(TRACE_NODES);
  for (let index = 0; index < TRACE_NODES; index += 1) {
    await expect(inspector.locator(`[data-sas-fabric-node="node:trace-${index}"]`)).toBeVisible();
  }
  await expect(toggle).toHaveText("Show fewer");
  await toggle.click();
  await expect(nodes).toHaveCount(12);
  expect(await nestedInteractiveCount(page)).toBe(0);
  expect(errors).toEqual([]);
});
