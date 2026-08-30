const { test, expect } = require("@playwright/test");

const browserErrors = new WeakMap();

test.use({
  baseURL: process.env.PLAYWRIGHT_ADMIN_BASE_URL || "http://127.0.0.1:8082",
});

async function gotoAdmin(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "System Setup And Recovery" })).toBeVisible();
  await expect(page.locator("#backup-path-list")).toBeVisible();
  await expect(page.locator("#debug-path-list")).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  const errors = [];
  browserErrors.set(page, errors);
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(`console:${message.text()}`);
    }
  });
  page.on("pageerror", (error) => {
    errors.push(`page:${error.message}`);
  });
});

test.afterEach(async ({ page }) => {
  expect(browserErrors.get(page) || []).toEqual([]);
});

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

  test("ESXi setup guidance disables the Linux bootstrap path", async ({ page }) => {
    await gotoAdmin(page);

    const resetButton = page.locator("#existing-system-reset-button");
    if (await resetButton.isEnabled()) {
      await resetButton.click();
    }
    await page.locator("#setup-platform").selectOption("esxi");
    await page.locator("#setup-ssh-enabled").check();

    await expect(page.locator("#setup-platform-help")).toContainText("host-managed");
    await expect(page.locator("#setup-platform-help")).toContainText("StorCLI");
    await expect(page.locator("#setup-platform-help")).toContainText("BMC");
    await expect(page.locator("#setup-ssh-user")).toHaveValue("root");
    await expect(page.locator("#setup-ssh-sudo-password-field")).toBeHidden();
    await expect(page.locator("#setup-bootstrap-enabled")).toBeDisabled();
    await expect(page.locator("#setup-bootstrap-result")).toContainText(
      "does not use the one-time Linux service-account bootstrap"
    );
    await expect(page.locator("#setup-bootstrap-sudoers-preview")).toContainText(
      "does not use the Linux sudoers/bootstrap flow"
    );
    await page.locator("#setup-load-recommended-button").click();
    await expect(page.locator("#setup-ssh-commands")).toHaveValue(/\/opt\/lsi\/storcli64\/storcli64 \/c0\/eall\/sall show all J/);
  });

  test("manual SSH and BMC fields do not resurrect connection defaults", async ({ page }) => {
    await gotoAdmin(page);

    const sshEnabled = page.locator("#setup-ssh-enabled");
    const sshUser = page.locator("#setup-ssh-user");
    await sshEnabled.check();
    await expect(sshUser).toHaveValue("jbodmap");
    await sshUser.fill("");
    await sshEnabled.uncheck();
    await sshEnabled.check();
    await expect(sshUser).toHaveValue("");

    await page.locator("#setup-truenas-host").fill("https://api.example.test");
    await page.locator("#setup-bmc-enabled").check();
    await expect(page.locator("#setup-bmc-host")).toHaveValue("");
  });

  test("admin view and profile controls are keyboard operable with visible focus", async ({ page }) => {
    await gotoAdmin(page);

    const builderButton = page.locator('[data-admin-view-button="builder"]');
    await builderButton.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-admin-view-panel="builder"]')).toBeVisible();

    const profileCard = page.locator("#profile-catalog .profile-card").nth(1);
    await expect(profileCard).toBeVisible();
    await profileCard.focus();
    await expect.poll(() => profileCard.evaluate((element) => {
      const style = getComputedStyle(element);
      return `${style.outlineStyle}:${style.outlineWidth}`;
    })).not.toBe("none:0px");
    await page.keyboard.press("Enter");
    await expect(profileCard).toHaveAttribute("aria-pressed", "true");
    await expect(profileCard).toBeFocused();

    const operationsButton = page.locator('[data-admin-view-button="operations"]');
    await operationsButton.focus();
    await page.keyboard.press("Space");
    await expect(page.locator('[data-admin-view-panel="operations"]')).toBeVisible();
  });

  async function expectTopLoaderPreviewGeometry(selector, page) {
    const previewGrid = page.locator(selector);
    await expect(previewGrid).toHaveAttribute("data-face-style", "top-loader");
    await expect(previewGrid).toHaveAttribute("data-layout-mode", /top-loader/);
    await expect(previewGrid).toHaveAttribute("data-layout-rows", "4");
    await expect(page.locator(`${selector} .profile-preview-row.is-flat-grouped`)).toHaveCount(4);
    await expect(page.locator(`${selector} .profile-preview-divider`)).toHaveCount(8);
  }

  test("admin previews keep top-loader row group geometry", async ({ page }) => {
    await gotoAdmin(page);

    const topLoaderOption = page.locator('#setup-profile option[value="supermicro-cse-946-top-60"]');
    await expect(topLoaderOption).toHaveCount(1);

    await page.locator("#setup-profile").selectOption("supermicro-cse-946-top-60");

    await expectTopLoaderPreviewGeometry("#profile-preview-grid", page);

    await page.locator('[data-admin-view-button="builder"]').click();
    await page.locator("#profile-builder-load-button").click();

    await expectTopLoaderPreviewGeometry("#profile-builder-preview-grid", page);

    await page.locator('[data-admin-view-button="operations"]').click();

    const topLoaderAddOption = page.locator(
      '#setup-storage-view-template option[value="profile:supermicro-cse-946-top-60"]'
    );
    await expect(topLoaderAddOption).toHaveCount(1);

    await page.locator("#setup-storage-view-template").selectOption("profile:supermicro-cse-946-top-60");
    await page.locator("#setup-storage-view-add-button").click();

    await expectTopLoaderPreviewGeometry("#setup-storage-view-preview-grid", page);
  });
});
