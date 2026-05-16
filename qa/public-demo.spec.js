const { test, expect } = require("@playwright/test");
const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");

function buildPublicDemoFixture() {
  const repoRoot = path.resolve(__dirname, "..");
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-public-demo-"));
  const outputPath = path.join(tempDir, "index.html");
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const result = spawnSync(python, ["scripts/build_public_demo.py", "--output", outputPath], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`Public demo generation failed:\n${result.stdout}\n${result.stderr}`);
  }
  return outputPath;
}

test("public demo static artifact is explorable without a live backend", async ({ page }) => {
  const demoPath = buildPublicDemoFixture();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });

  await page.goto(pathToFileURL(demoPath).href, { waitUntil: "load" });

  const selector = page.locator("#enclosure-select");
  await expect(page.locator(".snapshot-banner-badge")).toContainText("Frozen Offline Artifact");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("60 visible bays");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("2 saved/virtual views");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("Scrambled IDs");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("7d");
  await expect(page.locator("#system-setup-button")).toHaveCount(0);
  await expect(page.locator("#export-snapshot-button")).toHaveCount(0);
  await expect(page.locator("#status-text")).toContainText("Frozen offline snapshot loaded");

  await expect(selector).toBeEnabled();
  await expect(page.locator("#chassis-shell")).toHaveAttribute("data-face-style", "top-loader");
  await expect(page.locator("#slot-grid .row-slots-flat-grouped")).toHaveCount(4);
  await expect(page.locator("#slot-grid .slot-tile.selected")).toHaveCount(0);
  await expect(page.locator("#detail-empty")).toContainText("Select a slot tile");
  await page.locator('#slot-grid .slot-tile[data-slot="57"]').click();
  await expect(page.locator("#detail-kv-grid")).toContainText("SAMSUNG MZILT3T8HALS/007");
  await expect(page.locator("#detail-kv-grid")).toContainText("DEMO-SN-CORE-0057");
  await expect(page.locator("#detail-kv-grid")).toContainText("mirror-8");
  await page.locator("#history-toggle-button").click();
  await expect(page.locator("#history-metric-grid")).toContainText("Temperature");
  await page.locator("#heatmap-toggle-button").click();
  await page.locator("#heatmap-metric-select").selectOption("temperature_c");
  await page.locator("#heatmap-playback-select").selectOption("timeline");
  const scrubSlider = page.locator("#heatmap-scrub-slider");
  await expect(scrubSlider).toBeEnabled();
  const scrubTarget = await scrubSlider.evaluate((slider) => Math.floor(Number(slider.max) / 2));
  await scrubSlider.evaluate((slider, value) => {
    slider.value = String(value);
    slider.dispatchEvent(new Event("input", { bubbles: true }));
  }, scrubTarget);
  await expect(page.locator("#heatmap-scrub-value")).toContainText("/");
  await expect(page.locator('#slot-grid .slot-tile[data-slot="57"] .slot-heatmap-value')).toBeVisible();
  await page.locator("#heatmap-toggle-button").click();

  await selector.selectOption("view:boot-doms");
  await expect(page.locator("#enclosure-panel-title")).toContainText("Boot SATADOMs");
  await page.locator('#slot-grid .slot-tile[data-slot="0"]').click();
  await expect(page.locator("#detail-kv-grid")).toContainText("SuperMicro SSD");
  await expect(page.locator("#detail-kv-grid")).toContainText("48 C");
  await selector.selectOption("view:nvme-carrier-x4");
  await expect(page.locator("#enclosure-panel-title")).toContainText("4x NVMe Carrier Card");
  await page.locator('#slot-grid .slot-tile[data-slot="0"]').click();
  await expect(page.locator("#detail-kv-grid")).toContainText("Samsung SSD 970 EVO 2TB");
  await expect(page.locator("#detail-kv-grid")).toContainText("DEMO-SN-NVME-0000");
  await page.locator("#heatmap-toggle-button").click();
  await expect(page.locator('#slot-grid .slot-tile[data-slot="0"] .slot-heatmap-value')).toBeVisible();

  await expect(page.locator("#refresh-button")).toBeDisabled();
  expect(consoleErrors).toEqual([]);
});
