const { test, expect } = require("@playwright/test");
const { observeAocCarrier } = require("./esxi-carrier-observer");
const { waitForSelectedScope, switchSelectedScope } = require("./release-readiness");

const liveApplianceQaEnabled = process.env.PLAYWRIGHT_LIVE_APPLIANCE_QA === "1";

test.beforeAll(() => {
  if (!liveApplianceQaEnabled) {
    throw new Error("Live appliance QA requires PLAYWRIGHT_LIVE_APPLIANCE_QA=1.");
  }
});

async function gotoApp(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#system-select")).toBeVisible();
  await expect(page.locator("#enclosure-select")).toBeVisible();
  await expect(page.locator("#slot-grid")).toBeVisible();
  await waitForSelectedScope(page);
}

async function setAutoRefresh(page, enabled) {
  const toggle = page.locator("#auto-refresh-toggle");
  const currentValue = await toggle.isChecked();
  if (currentValue !== enabled) {
    await toggle.click();
  }
}

async function systemOptions(page) {
  return page.locator("#system-select option").evaluateAll((options) =>
    options
      .map((option) => option.value)
      .filter((value) => typeof value === "string" && value.length > 0)
  );
}

test.describe("ESXi smoke", () => {
  test("saved ESXi host renders a supported read-only hardware view", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);

    const systems = await systemOptions(page);
    const fatTwinSystemId = ["esxi-ft-node-2", "esxi-ft-node-3"].find((candidate) => systems.includes(candidate));
    const hasAocSystem = systems.includes("cryostorage-esxi");
    test.skip(!fatTwinSystemId && !hasAocSystem, "Need a saved ESXi system for ESXi smoke coverage.");

    if (fatTwinSystemId) {
      await switchSelectedScope(page, "#system-select", fatTwinSystemId);
      await expect(page.locator("#enclosure-select")).toHaveValue("enclosure:supermicro-fat-twin-front-6");

      const tiles = page.locator("#slot-grid .slot-tile");
      await expect(tiles).toHaveCount(6);

      const mappedTile = page.locator('#slot-grid .slot-tile[data-slot="2"]');
      await expect(mappedTile).toContainText("252:2");
      await mappedTile.click();

      await expect(page.locator("#detail-content")).toBeVisible();
      await expect(page.locator("#detail-content")).toContainText("H7240AS60SUN4.0T");
      const detailText = await page.locator("#detail-content").innerText();
      await expect(page.locator("#detail-led-controls")).toBeVisible();
      if (detailText.includes("ESXi local JBOD")) {
        await expect(page.locator("#detail-content")).toContainText("JBOD");
        await expect(page.locator("#detail-smart-note")).toContainText(
          /StorCLI physical-drive health|local JBOD physical device/
        );
      } else {
        await expect(page.locator("#detail-content")).toContainText("Mapping");
        await expect(page.locator("#detail-content")).toContainText("ssh");
        await expect(page.locator("#detail-smart-note")).toContainText(
          "StorCLI physical-drive detail is unavailable"
        );
      }
      return;
    }

    await switchSelectedScope(page, "#system-select", "cryostorage-esxi");
    // The built-in AOC profile puts physical bay 01 above bay 00.
    const carrierTiles = page.locator("#slot-grid .slot-tile");
    expect(await carrierTiles.evaluateAll(nodes => nodes.map(node => node.dataset.slot))).toEqual(["1", "0"]);
    await expect(carrierTiles.locator(".storage-view-runtime-card-slot")).toHaveText(["01", "00"]);
    const upper = await carrierTiles.nth(0).boundingBox();
    const lower = await carrierTiles.nth(1).boundingBox();
    expect(upper.y).toBeLessThan(lower.y);
    await observeAocCarrier(page);
  });
});
