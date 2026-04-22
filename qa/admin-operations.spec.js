const { test, expect } = require("@playwright/test");

test.use({
  baseURL: process.env.PLAYWRIGHT_ADMIN_BASE_URL || "http://127.0.0.1:8082",
});

async function gotoAdmin(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "System Setup And Recovery" })).toBeVisible();
  await expect(page.locator("#backup-path-list")).toBeVisible();
  await expect(page.locator("#debug-path-list")).toBeVisible();
}

test.describe("admin sidecar smoke", () => {
  test("operations view exposes backup, debug, and demo-builder controls", async ({ page }) => {
    await gotoAdmin(page);

    await expect(page.locator("#backup-path-summary")).toHaveText(/\d+ of \d+ selected\./);
    await expect(page.locator("#debug-path-summary")).toContainText("disabled while secrets scrub is on");
    await expect(page.locator("#debug-scrub-secrets-toggle")).toBeVisible();
    await expect(page.locator("#debug-scrub-identifiers-toggle")).toBeVisible();
    await expect(page.locator("#setup-create-demo-button")).toBeVisible();
    await expect(page.locator("#setup-result")).toContainText(
      "restart the read UI after a new system is added"
    );
  });

  test("locked full-backup pills force encrypted portable export", async ({ page }) => {
    await gotoAdmin(page);

    const lockedPills = page.locator("#backup-path-list .path-pill.is-locked");
    expect(await lockedPills.count()).toBeGreaterThan(0);

    const lockedPill = lockedPills.first();
    const selectedBefore = await page.locator("#backup-path-list .path-pill.is-selected").count();

    await lockedPill.click();

    await expect(lockedPill).toHaveClass(/is-selected/);
    await expect(page.locator("#backup-encrypt-toggle")).toBeChecked();
    await expect(page.locator("#backup-encrypt-toggle")).toBeDisabled();
    await expect(page.locator("#backup-packaging")).toHaveValue("7z");
    await expect(page.locator("#backup-path-summary")).toContainText(
      `${selectedBefore + 1} of`
    );
  });

  test("split debug scrub controls gate locked debug paths", async ({ page }) => {
    await gotoAdmin(page);

    const secretsToggle = page.locator("#debug-scrub-secrets-toggle");
    const identifiersToggle = page.locator("#debug-scrub-identifiers-toggle");
    const lockedPill = page.locator("#debug-path-list .path-pill.is-locked").first();

    await expect(secretsToggle).toBeChecked();
    await expect(identifiersToggle).toBeChecked();
    await expect(lockedPill).toBeDisabled();
    await expect(page.locator("#debug-path-summary")).toContainText("disabled while secrets scrub is on");

    await secretsToggle.uncheck();

    await expect(lockedPill).toBeEnabled();
    await expect(page.locator("#debug-path-summary")).not.toContainText(
      "disabled while secrets scrub is on"
    );

    await lockedPill.click();

    await expect(lockedPill).toHaveClass(/is-selected/);
    await expect(page.locator("#debug-encrypt-toggle")).toBeChecked();
    await expect(page.locator("#debug-encrypt-toggle")).toBeDisabled();
    await expect(page.locator("#debug-packaging")).toHaveValue("7z");
  });
});
