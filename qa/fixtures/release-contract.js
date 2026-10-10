"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "../..");
const origin = "http://127.0.0.1:18710";
const runtime = {
  system_id: "synthetic-system", system_label: "Synthetic System",
  views: [{
    id: "saved-chassis", label: "Saved Chassis", kind: "ses_enclosure",
    template_id: "synthetic-profile", profile_id: "synthetic-profile",
    profile_label: "Synthetic Profile", face_style: "generic", latch_edge: "bottom",
    bay_size: "3.5", enabled: true, render: { show_in_main_ui: true },
    binding: { mode: "auto" }, order: 10, template_label: "Synthetic Profile",
    slot_layout: [[0]], source: "selected_enclosure_snapshot",
    backing_enclosure_id: "enc-a", backing_enclosure_label: "Live Shelf",
    matched_count: 1, slot_count: 1,
    slots: [{ slot_index: 0, slot_label: "00", occupied: true, state: "matched",
      source: "snapshot_slot", snapshot_slot: 0, snapshot_enclosure_id: "enc-a", device_name: "sdx",
      serial: "SANITIZED-SLOT-0", description: "Synthetic saved slot" }],
  }],
};

function buildFixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "release-contract-"));
  try {
    const output = path.join(directory, "index.html");
    const input = path.join(directory, "runtime.json");
    fs.writeFileSync(input, JSON.stringify(runtime));
    const result = spawnSync(process.env.PYTHON || "python3", [
      path.join(root, "scripts/build_current_source_browser_fixture.py"),
      "--output", output, "--live-mode-runtime", input,
    ], { cwd: root, encoding: "utf8", timeout: 30_000 });
    if (result.status !== 0) throw new Error(`Fixture build failed: ${result.stdout}\n${result.stderr}`);
    return fs.readFileSync(output, "utf8");
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function installFixture(page, html, { timing = false, esxi = "fat-twin", emptyInventory = false } = {}) {
  const diagnostics = { errors: [], warnings: [], unexpected: [], inventory: [], runtime: [] };
  const controls = { holdInventory: null, holdRuntime: null };
  page.on("pageerror", error => diagnostics.errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error") diagnostics.errors.push(message.text());
    if (message.type() === "warning") diagnostics.warnings.push(message.text());
  });
  // Only bootstrap DATA changes. The complete template and all script bytes are current source.
  await page.addInitScript(({ timing, esxi, emptyInventory }) => {
    Object.defineProperty(window, "APP_BOOTSTRAP", { configurable: true, set(value) {
      value.uiPerfEnabled = timing;
      value.initialSelectedSlot = null;
      value.snapshot.slots[0].serial = "SANITIZED-SLOT-0";
      value.snapshot.systems.push({ id: "synthetic-second", label: "Second System", platform: "linux" });
      value.snapshot.systems.push({
        id: esxi === "fat-twin" ? "esxi-ft-node-2" : "cryostorage-esxi",
        label: "Synthetic ESXi", platform: "esxi",
      });
      value.snapshot.enclosures.push({ ...value.snapshot.enclosures[0], id: "enc-b", label: "Second Shelf" });
      if (emptyInventory) {
        value.snapshot.enclosures = [];
        value.snapshot.selected_enclosure_id = null;
        value.snapshot.selected_enclosure_label = null;
        value.snapshot.slots = [];
        value.storageViewsRuntime = { system_id: "synthetic-system", views: [] };
      }
      Object.defineProperty(window, "APP_BOOTSTRAP", { value, writable: true, configurable: true });
    } });
  }, { timing, esxi, emptyInventory });
  let initialSnapshot;
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    const templateAsset = url.origin === "https://synthetic.invalid" && url.pathname.startsWith("/static/");
    if (url.origin !== origin && !templateAsset) {
      diagnostics.unexpected.push(url.href);
      return route.abort("blockedbyclient");
    }
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    if (url.pathname.startsWith("/static/")) {
      const asset = path.resolve(root, "app", `.${url.pathname}`);
      if (asset.startsWith(path.join(root, "app/static") + path.sep) && fs.existsSync(asset)) {
        return route.fulfill({ path: asset });
      }
    }
    const systemId = url.searchParams.get("system_id") || "synthetic-system";
    const enclosureId = url.searchParams.get("enclosure_id") || "enc-a";
    let payload;
    if (url.pathname === "/api/inventory") {
      diagnostics.inventory.push(url.href);
      if (controls.holdInventory) await controls.holdInventory(url);
      initialSnapshot ||= await page.evaluate(() => window.APP_BOOTSTRAP.snapshot);
      payload = structuredClone(initialSnapshot);
      payload.selected_system_id = systemId;
      payload.selected_system_label = systemId === "synthetic-system" ? "Synthetic System" : "Second System";
      payload.selected_enclosure_id = enclosureId;
      payload.selected_enclosure_label = enclosureId === "enc-a" ? "Live Shelf" : "Second Shelf";
      payload.slots.forEach(slot => { slot.enclosure_id = enclosureId; });
      if (["esxi-ft-node-2", "cryostorage-esxi"].includes(systemId)) {
        const fatTwin = systemId === "esxi-ft-node-2";
        const profileId = fatTwin ? "supermicro-fat-twin-front-6" : "supermicro-aoc-slg4-2h8m2";
        const layout = fatTwin ? [[2, 5], [1, 4], [0, 3]] : [[0, 1]];
        payload.selected_system_platform = "esxi";
        payload.selected_enclosure_id = profileId;
        payload.selected_profile = { ...payload.selected_profile, id: profileId,
          face_style: fatTwin ? "generic" : "nvme-carrier", slot_layout: layout,
          rows: layout.length, columns: 2 };
        payload.layout_rows = layout;
        payload.layout_slot_count = layout.flat().length;
        payload.layout_columns = 2;
        payload.enclosures = [{ ...payload.enclosures[0], id: profileId, profile_id: profileId, slot_layout: layout }];
        payload.slots = layout.flat().map((slot, i) => ({ ...payload.slots[0], slot,
          slot_label: String(slot).padStart(2, "0"), row_index: Math.floor(i / 2), column_index: i % 2,
          enclosure_id: profileId, serial: `SANITIZED-ESXI-${slot}`,
          device_name: fatTwin ? `252:${slot}` : `13:${1 - slot}`,
          model: fatTwin ? "H7240AS60SUN4.0T" : "Samsung SSD 970 EVO 2TB",
          mapping_source: "ssh", led_supported: fatTwin,
          pool_name: fatTwin ? "ESXi local JBOD" : null, vdev_class: fatTwin ? "JBOD" : null,
        }));
      }
    } else if (url.pathname === "/api/storage-views") {
      diagnostics.runtime.push(url.href);
      if (controls.holdRuntime) await controls.holdRuntime(url);
      payload = systemId === "synthetic-system" ? runtime : { system_id: systemId, views: [] };
    } else if (url.pathname === "/api/history/status") {
      payload = { configured: false, available: false };
    } else if (/^\/api\/(?:slots|storage-views\/[^/]+\/slots)\/(?:\d+\/smart|smart-batch)$/.test(url.pathname)) {
      const summary = { available: false,
        message: systemId === "esxi-ft-node-2" ? "Synthetic StorCLI physical-drive health for local JBOD physical device" : "Synthetic SMART unavailable" };
      payload = url.pathname.endsWith("smart-batch")
        ? { summaries: route.request().postDataJSON().slots.map(slot => ({ slot, summary })) }
        : summary;
    } else if (url.pathname === "/api/release-status") {
      payload = {};
    } else {
      diagnostics.unexpected.push(url.href);
      return route.abort("blockedbyclient");
    }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(payload) });
  });
  return { diagnostics, controls };
}

module.exports = { buildFixture, installFixture, origin };
