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

// #1166: storage views whose slot indexes collide with real fabric bays. The
// mixed view puts view slot 0 on bay 02 and view slot 1 on bay 00; view slot 2
// is a PCIe NVMe carrier slot and the SATADOM view's DOM-A is view slot 0,
// neither with an enclosure bay.
const VIEW_FIXTURE_PYTHON = `
import json
import scripts.build_current_source_browser_fixture as source
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload

base = source.build_synthetic_live_snapshot().model_dump(mode="json")
bay = base["slots"][0]
base["slots"] = [
    {**bay, "slot": n, "slot_label": f"{n:02d}", "column_index": n, "device_name": f"sd{'abcd'[n]}",
     "serial": f"SYNTH-BAY-{n}", "gptid": f"synthetic-gptid-{n}", "vdev_name": "mirror-0" if n in (0, 2) else "mirror-1"}
    for n in range(4)
]
base["layout_rows"] = [[0, 1, 2, 3]]
base["layout_slot_count"] = 4
base["layout_columns"] = 4
snapshot = InventorySnapshot.model_validate(base)
source.build_synthetic_live_snapshot = lambda: snapshot

def view_slot(index, label, device, bay=None, bus="SAS"):
    row = dict(slot_index=index, slot_label=label, occupied=True, state="matched", source="inventory_candidate",
               device_name=device, serial=f"SYNTH-{device.upper()}", bus=bus)
    if bay is not None:
        row.update(snapshot_slot=bay, snapshot_enclosure_id="enc-a")
    return row

def view(view_id, label, kind, slots, **extra):
    return dict(id=view_id, label=label, kind=kind, template_id=view_id, enabled=True, order=10,
                render=dict(show_in_main_ui=True), binding=dict(mode="auto"), source="inventory_binding",
                slot_layout=[[slot["slot_index"] for slot in slots]], slot_count=len(slots),
                matched_count=len(slots), slots=slots, **extra)

views = [
    view("mixed-shelf", "Mixed Shelf", "ses_enclosure", [
        view_slot(0, "00", "sdc", bay=2), view_slot(1, "01", "sda", bay=0), view_slot(2, "M2-3", "nvd2", bus="PCIe"),
    ], backing_enclosure_id="enc-a", backing_enclosure_label="Live Shelf", face_style="generic", bay_size="3.5"),
    view("boot-satadoms", "Boot SATADOMs", "boot_devices", [
        view_slot(0, "DOM-A", "ada0", bus="SATA"), view_slot(1, "DOM-B", "ada1", bus="SATA"),
    ]),
]
out = {}
for key, default in (("mixed", "view:mixed-shelf"), ("satadom", "view:boot-satadoms")):
    runtime = StorageViewRuntimePayload(system_id="synthetic-system", system_label="Synthetic System", views=views,
                                        default_selection=default)
    out[key] = dict(html=source.build_live_fixture_html(runtime), runtime=runtime.model_dump(mode="json"))
out["snapshot"] = snapshot.model_dump(mode="json")
print(json.dumps(out))
`;

let viewFixture;
test.beforeAll(() => {
  viewFixture = JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", VIEW_FIXTURE_PYTHON], {
    cwd: root, encoding: "utf8", maxBuffer: 16 * 1024 * 1024,
  }));
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
  await showAll.focus();
  await expect(showAll).toBeFocused();
  await page.keyboard.press("Enter");
  await expectFullMap(page);
  // Inspect automatic focus before sending Tab or focusing any other control.
  expect(await page.evaluate(() => ({
    id: document.activeElement.id,
    visible: document.activeElement.getClientRects().length > 0,
  }))).toEqual({id: "sas-fabric-heading", visible: true});
  await page.keyboard.press("Tab");
  await expect(page.locator("#sas-fabric-refresh-button")).toBeFocused();

  await controller.click();
  await controller.focus();
  await page.keyboard.press("Escape");
  await expectFullMap(page);

  await page.locator("#sas-fabric-refresh-button").click();
  await expectFullMap(page);
  expect(errors).toEqual([]);
});

async function openViewPage(page, key) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== "https://synthetic.invalid") throw new Error(`Unexpected external request ${url.origin}`);
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: viewFixture[key].html });
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(root, "app", url.pathname);
      return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
    }
    let data = { ok: true, available: false };
    if (url.pathname === "/api/inventory") data = viewFixture.snapshot;
    if (url.pathname === "/api/storage-views") data = viewFixture[key].runtime;
    if (url.pathname === "/api/sas-fabric") data = syntheticFabric();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto("https://synthetic.invalid/");
  await page.locator("#auto-refresh-toggle").uncheck();
  return errors;
}

const inspectorBody = (page) => page.locator("#sas-fabric-inspector-body");
const litChips = (page) => page.locator("#sas-fabric-panel .sas-fabric-bay-chip.is-selected");

async function expectFabricBay(page, bay) {
  await expect(page.locator("#sas-fabric-inspector-title")).toHaveText("Selected Bay");
  await expect(inspectorBody(page)).toContainText(`Synthetic bay ${bay}`);
  const lit = await litChips(page).evaluateAll((chips) => [...new Set(chips.map((chip) => chip.dataset.sasFabricSlot))]);
  expect(lit).toEqual([String(bay)]);
}

async function expectNoFabricBay(page, device) {
  await expect(page.locator("#sas-fabric-inspector-title")).toHaveText("Fabric Inspector");
  await expect(inspectorBody(page)).toHaveText(`${device} is not on the SAS fabric, so no bay path is shown.`);
  await expect(litChips(page)).toHaveCount(0);
}

test("storage-view slots select their backing fabric bay, or none, on every path (#1166)", async ({ page }) => {
  const errors = await openViewPage(page, "mixed");
  const tile = (slot) => page.locator(`#slot-grid .slot-tile[data-slot="${slot}"]`);
  await expect(page.locator("#enclosure-select")).toHaveValue("view:mixed-shelf");
  await expect(tile(0)).toHaveClass(/selected/);

  // Opening the panel and the first fabric load: view slot 0 is bay 02.
  await page.locator("#sas-fabric-toggle-button").click();
  await expect(page.locator("#sas-fabric-lanes .sas-fabric-lane")).toHaveCount(1);
  await expectFabricBay(page, 2);

  // Grid clicks.
  await tile(1).click();
  await expect(tile(1)).toHaveClass(/selected/);
  await expectFabricBay(page, 0);
  await tile(2).click();
  await expect(tile(2)).toHaveClass(/selected/);
  await expectNoFabricBay(page, "nvd2");

  // Topology pills name physical bays: bay 02 backs view slot 0.
  await tile(1).click();
  await expectFabricBay(page, 0);
  await page.locator('#topology-context [data-topology-slot="2"]').click();
  await expect(tile(0)).toHaveClass(/selected/);
  await expectFabricBay(page, 2);

  // Search auto-select.
  await page.locator("#search-box").fill("nvd2");
  await expect(tile(2)).toHaveClass(/selected/);
  await expectNoFabricBay(page, "nvd2");
  await page.locator("#search-box").fill("");

  // Reopening the panel with data already loaded keeps the view slot's bay.
  await page.locator("#sas-fabric-toggle-button").click();
  await expect(page.locator("#sas-fabric-panel")).toBeHidden();
  await tile(1).click();
  await page.locator("#sas-fabric-toggle-button").click();
  await expectFabricBay(page, 0);
  expect(errors).toEqual([]);
});

test("a SATADOM view slot never shows the bay sharing its index (#1166)", async ({ page }) => {
  const errors = await openViewPage(page, "satadom");
  await expect(page.locator("#enclosure-select")).toHaveValue("view:boot-satadoms");
  await expect(page.locator('#slot-grid .slot-tile[data-slot="0"]')).toHaveClass(/selected/);
  await page.locator("#sas-fabric-toggle-button").click();
  await expect(page.locator("#sas-fabric-lanes .sas-fabric-lane")).toHaveCount(1);
  await expectNoFabricBay(page, "ada0");
  await page.locator('#slot-grid .slot-tile[data-slot="1"]').click();
  await expectNoFabricBay(page, "ada1");
  expect(errors).toEqual([]);
});
