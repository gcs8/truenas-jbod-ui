"use strict";

const { test, expect } = require("@playwright/test");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..");
const appSource = fs.readFileSync(path.join(repoRoot, "app/static/app.js"), "utf8");
const styleSource = fs.readFileSync(path.join(repoRoot, "app/static/style.css"), "utf8");
const syntheticRuntime = {
  system_id: "synthetic-system",
  system_label: "Synthetic System",
  views: [
    {
      id: "saved-chassis",
      label: "Saved Chassis",
      kind: "ses_enclosure",
      template_id: "synthetic-profile",
      profile_id: "synthetic-profile",
      profile_label: "Synthetic Profile",
      face_style: "generic",
      latch_edge: "bottom",
      bay_size: "3.5",
      enabled: true,
      render: { show_in_main_ui: true },
      binding: { mode: "auto" },
      order: 10,
      template_label: "Synthetic Profile",
      slot_layout: [[0]],
      source: "selected_enclosure_snapshot",
      backing_enclosure_id: "enc-a",
      backing_enclosure_label: "Live Shelf",
      notes: ["Synthetic saved chassis view."],
      matched_count: 1,
      slot_count: 1,
      slots: [
        {
          slot_index: 0,
          slot_label: "00",
          occupied: true,
          state: "matched",
          source: "snapshot_slot",
          snapshot_slot: 0,
          device_name: "sdx",
          serial: "LIVE-SERIAL-0",
          gptid: "synthetic-gptid-0",
          description: "Synthetic saved slot",
        },
      ],
    },
  ],
};

function buildCurrentSourceFixture() {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-saved-view-selection-"));
  const outputPath = path.join(tempDir, "index.html");
  const runtimePath = path.join(tempDir, "storage-view-runtime.json");
  const malformedConfigPath = path.join(tempDir, "malformed-config.yaml");
  fs.writeFileSync(runtimePath, JSON.stringify(syntheticRuntime), "utf8");
  fs.writeFileSync(malformedConfigPath, "systems: [\n", "utf8");
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const generatorPath = path.join(repoRoot, "scripts/build_current_source_browser_fixture.py");
  const result = spawnSync(
    python,
    [generatorPath, "--output", outputPath, "--live-mode-runtime", runtimePath],
    {
      cwd: repoRoot,
      encoding: "utf8",
      env: { ...process.env, APP_CONFIG_PATH: malformedConfigPath },
    }
  );
  if (result.status !== 0) {
    const error = new Error(`Current-source fixture generation failed:\n${result.stdout}\n${result.stderr}`);
    fs.rmSync(tempDir, { recursive: true, force: true });
    throw error;
  }
  return { tempDir, html: fs.readFileSync(outputPath, "utf8") };
}

let fixture;

test.beforeAll(() => {
  fixture = buildCurrentSourceFixture();
});

test.afterAll(() => {
  if (fixture?.tempDir) fs.rmSync(fixture.tempDir, { recursive: true, force: true });
});

test("saved-view selection protects and then rebinds the real mapping form", async ({ page }) => {
  const apiRequests = [];
  const consoleErrors = [];
  let confirmDiscard = false;

  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) apiRequests.push(`${request.method()} ${url.pathname}${url.search}`);
  });
  page.on("dialog", async (dialog) => {
    expect(dialog.message()).toContain("Discard unsaved calibration edits");
    if (confirmDiscard) await dialog.accept();
    else await dialog.dismiss();
  });

  await page.route("https://synthetic.invalid/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/") {
      await route.fulfill({ status: 200, contentType: "text/html", body: fixture.html });
      return;
    }
    if (url.pathname === "/static/app.js") {
      await route.fulfill({ status: 200, contentType: "text/javascript", body: appSource });
      return;
    }
    if (url.pathname === "/static/style.css") {
      await route.fulfill({ status: 200, contentType: "text/css", body: styleSource });
      return;
    }
    if (url.pathname === "/api/storage-views") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(syntheticRuntime) });
      return;
    }
    if (url.pathname === "/api/history/status") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ configured: false, available: false }) });
      return;
    }
    if (url.pathname.startsWith("/api/smart/")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ available: false, summaries: [] }) });
      return;
    }
    if (url.pathname.startsWith("/api/")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, available: false }) });
      return;
    }
    if (url.pathname.startsWith("/static/")) {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    await route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
  });

  await page.goto("https://synthetic.invalid/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#enclosure-select")).toHaveValue("enclosure:enc-a");
  await expect(page.locator('#slot-grid .slot-tile[data-slot="0"]')).toHaveClass(/selected/);
  await expect(page.locator("#detail-slot-title")).toHaveText("Slot 00");
  await expect(page.locator("#mapping-form")).toBeVisible();

  await page.locator("#enclosure-alias-edit-button").click();
  await expect(page.locator("#enclosure-alias-form")).toBeVisible();

  const draft = {
    serial: "UNSAVED-SERIAL",
    device_name: "unsaved-device",
    gptid: "unsaved-gptid",
    notes: "unsaved notes",
  };
  for (const [name, value] of Object.entries(draft)) {
    await page.locator(`#mapping-form [name="${name}"]`).fill(value);
  }
  await page.waitForTimeout(300);
  apiRequests.length = 0;

  await page.locator("#enclosure-select").selectOption("view:saved-chassis");

  await expect(page.locator("#enclosure-select")).toHaveValue("enclosure:enc-a");
  await expect(page.locator('#slot-grid .slot-tile[data-slot="0"]')).toHaveClass(/selected/);
  await expect(page.locator("#detail-slot-title")).toHaveText("Slot 00");
  await expect(page.locator("#enclosure-alias-form")).toBeVisible();
  for (const [name, value] of Object.entries(draft)) {
    await expect(page.locator(`#mapping-form [name="${name}"]`)).toHaveValue(value);
  }
  await page.waitForTimeout(100);
  expect(apiRequests).toEqual([]);

  confirmDiscard = true;
  await page.locator("#enclosure-select").selectOption("view:saved-chassis");

  await expect(page.locator("#enclosure-select")).toHaveValue("view:saved-chassis");
  await expect(page.locator("#enclosure-panel-title")).toHaveText("Saved Chassis");
  await expect(page.locator("#mapping-health-summary")).toContainText("Saved Chassis: All 1 populated bay is matched.");
  await expect(page.locator("#enclosure-alias-form")).toBeHidden();
  await expect(page.locator("#detail-empty")).toBeVisible();
  await expect(page.locator("#mapping-form")).toBeHidden();
  for (const name of Object.keys(draft)) {
    await expect(page.locator(`#mapping-form [name="${name}"]`)).toHaveValue("");
  }

  const savedSlot = page.locator('#slot-grid .slot-tile[data-slot="0"]');
  await expect(savedSlot).toHaveCount(1);
  await savedSlot.click();

  await expect(savedSlot).toHaveClass(/selected/);
  await expect(page.locator("#detail-content")).toBeVisible();
  await expect(page.locator("#detail-slot-title")).toHaveText("Slot 00");
  await expect(page.locator("#mapping-form")).toBeVisible();
  await expect(page.locator('#mapping-form [name="serial"]')).toHaveValue("LIVE-SERIAL-0");
  await expect(page.locator('#mapping-form [name="device_name"]')).toHaveValue("sdx");
  await expect(page.locator('#mapping-form [name="gptid"]')).toHaveValue("synthetic-gptid-0");
  await expect(page.locator('#mapping-form [name="notes"]')).toHaveValue("Saved mapping note");
  expect(consoleErrors).toEqual([]);
});
