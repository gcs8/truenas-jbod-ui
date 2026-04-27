const { test, expect } = require("@playwright/test");

async function gotoApp(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#system-select")).toBeVisible();
  await expect(page.locator("#enclosure-select")).toBeVisible();
  await expect(page.locator("#slot-grid")).toBeVisible();
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
      await page.locator("#system-select").selectOption(fatTwinSystemId);
      await expect(page.locator("#system-select")).toHaveValue(fatTwinSystemId);
      await expect(page.locator("#status-text")).toHaveText(/Inventory updated\.|Ready\./, {
        timeout: 30_000,
      });
      await expect(page.locator("#enclosure-select")).toHaveValue("enclosure:supermicro-fat-twin-front-6");

      const tiles = page.locator("#slot-grid .slot-tile");
      await expect(tiles).toHaveCount(6);

      const mappedTile = page.locator('#slot-grid .slot-tile[data-slot="2"]');
      await expect(mappedTile).toContainText("252:2");
      await mappedTile.click();

      await expect(page.locator("#detail-content")).toBeVisible();
      await expect(page.locator("#detail-content")).toContainText("H7240AS60SUN4.0T");
      await expect(page.locator("#detail-content")).toContainText("ESXi local JBOD");
      await expect(page.locator("#detail-content")).toContainText("JBOD");
      await expect(page.locator("#detail-led-controls")).toBeVisible();
      await expect(page.locator("#detail-smart-note")).toContainText(
        /StorCLI physical-drive health|local JBOD physical device/
      );
      return;
    }

    await page.locator("#system-select").selectOption("cryostorage-esxi");
    await expect(page.locator("#system-select")).toHaveValue("cryostorage-esxi");
    await expect(page.locator("#status-text")).toHaveText(/Inventory updated\.|Ready\./, {
      timeout: 30_000,
    });
    await expect(page.locator("#enclosure-select")).toHaveValue("enclosure:supermicro-aoc-slg4-2h8m2");
    await expect(page.locator(".nvme-carrier-board-image")).toHaveAttribute("src", /aoc-slg4-2h8m2\.jpg$/);

    const enclosureOptions = await page.locator("#enclosure-select option").evaluateAll((options) =>
      options
        .map((option) => option.value)
        .filter((value) => typeof value === "string" && value.length > 0)
    );
    expect(enclosureOptions).not.toContain("view:aoc-slg4-2h8m2");

    const tiles = page.locator("#slot-grid .slot-tile");
    await expect(tiles).toHaveCount(2);
    await expect(tiles.first()).toContainText("13:1");
    await expect(tiles.nth(1)).toContainText("13:0");

    await tiles.first().click();
    await expect(page.locator("#detail-content")).toBeVisible();
    await expect(page.locator("#detail-content")).toContainText("Samsung SSD 970 EVO 2TB");
    await expect(page.locator("#detail-led-controls")).toBeHidden();
    await expect(page.locator("#detail-smart-note")).toBeVisible();
    await expect(page.locator(".nvme-carrier-edge-note")).toContainText("M2-1 lower slot");
  });
});
