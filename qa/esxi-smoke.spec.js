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
  test("saved ESXi host renders the read-only AOC carrier view", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);

    const systems = await systemOptions(page);
    test.skip(!systems.includes("cryostorage-esxi"), "Need the saved cryostorage-esxi system for ESXi smoke coverage.");

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
    await expect(page.locator("#detail-content")).toContainText("ESXi + VMs");
    await expect(page.locator("#detail-led-controls")).toBeHidden();
    await expect(page.locator("#detail-smart-note")).toContainText("ESXi first-pass support");
    const historyButton = page.locator("#history-toggle-button");
    if (await historyButton.isVisible()) {
      await historyButton.click();
      await expect(page.locator("#detail-history-panel")).toBeVisible();
      await expect(page.locator("#history-drawer-title")).toContainText("Slot 01 History");
    } else {
      await expect(historyButton).toBeHidden();
    }
    await expect(page.locator(".nvme-carrier-edge-note")).toContainText("M2-1 lower slot");
  });
});
