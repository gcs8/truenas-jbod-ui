const { test, expect } = require("@playwright/test");

const browserErrors = new WeakMap();

test.use({
  baseURL: process.env.PLAYWRIGHT_ADMIN_BASE_URL || "http://127.0.0.1:8082",
});

async function gotoAdmin(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Admin", exact: true })).toBeVisible();
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
      "Saved systems appear in the main UI after a restart."
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
    await expect(page.locator("#setup-bootstrap-details")).not.toHaveAttribute("open", /.*/);
    await expect(page.locator("#setup-ha-toggle")).toBeHidden();
    await page.locator("#setup-ssh-commands-details > summary").click();
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

  test("restart choices and failed timing drafts survive refresh", async ({ page }) => {
    await gotoAdmin(page);
    for (const prefix of ["backup-export", "backup-import", "debug-export"]) {
      const stop = page.locator(`#${prefix}-stop-toggle`);
      const restart = page.locator(`#${prefix}-restart-toggle`);
      await stop.uncheck();
      await expect(restart).toBeChecked();
      await expect(restart).toBeDisabled();
      await stop.check();
      await expect(restart).toBeChecked();
      await restart.uncheck();
      await stop.uncheck();
      await stop.check();
      await expect(restart).not.toBeChecked();
    }
    const field = page.locator('input[data-runtime-behavior-key]:enabled').first();
    await field.fill("99");
    await field.focus();
    await page.route("**/api/admin/runtime-behavior", route => route.fulfill({status: 200, contentType: "text/html", body: "<h1>Synthetic gateway response</h1>"}));
    await page.locator("#runtime-behavior-save-button").click();
    await expect(page.locator("#runtime-behavior-result")).toContainText("save outcome is unknown");
    await expect(page.locator("#runtime-behavior-result")).toContainText("may already have been saved");
    await expect(page.locator("#runtime-behavior-result")).not.toContainText(/retry/i);
    await expect(field).toHaveValue("99");
    await field.focus();
    await page.locator("#refresh-state-button").evaluate(button => button.click());
    await expect(page.locator("#admin-status-banner")).toContainText("Refreshed.");
    await expect(field).toHaveValue("99");
    await expect(field).toBeFocused();
  });

  test("demo creation preserves a loaded existing synthetic system", async ({ page }) => {
    test.skip(process.env.PLAYWRIGHT_ADMIN_SYNTHETIC_MUTATIONS !== "1", "Requires the isolated synthetic admin runner.");
    const created = await page.request.post("/api/admin/system-setup", {data: {
      system_id: "qa-safety-existing", label: "Synthetic existing system", platform: "linux",
      truenas_host: "https://synthetic.example.invalid", ssh_enabled: false, make_default: true,
    }});
    expect(created.ok()).toBeTruthy();
    await gotoAdmin(page);
    const before = await (await page.request.get("/api/admin/state")).json();
    const original = before.systems[0];
    expect(original).toBeTruthy();
    await page.locator("#setup-system-id").fill(original.id);
    await page.locator("#setup-system-label").fill(original.label);
    const posted = page.waitForRequest(request => request.url().endsWith("/api/admin/system-setup/demo") && request.method() === "POST");
    await page.locator("#setup-create-demo-button").click();
    const request = await posted;
    expect(request.postDataJSON().replace_existing).toBe(false);
    expect(request.postDataJSON().system_id).not.toBe(original.id);
    await expect(page.locator("#setup-result")).toContainText(/created|saved/i);
    const after = await (await page.request.get("/api/admin/state")).json();
    expect(after.systems.find(system => system.id === original.id)).toEqual(original);
    expect(after.systems.length).toBe(before.systems.length + 1);
  });
});

// Backups library (#398). The /api/admin/backups* responses are a synthetic
// fake served by page.route, so the page is exercised without real archives.
test.describe("admin backups library", () => {
  const SHA = "b".repeat(64);
  function library() {
    return {
      classes: {
        config: { enabled: true, debounce_seconds: 60, max_delay_seconds: 900, local_keep: 10, remote_keep: 20, remote_max_age_days: null, pending_changes: 0, last_run: null },
        full: { enabled: true, schedule: "0 3 * * *", next_run_at: "2026-09-25T03:00:00Z", local_keep: 3, remote_keep: 5, remote_max_age_days: 90, last_run: null },
      },
      targets: [
        { id: "offsite", label: "Offsite SFTP", provider: "sftp", transport_encrypted: true, enabled: true, last_run: { at: "2026-09-20T03:00:00Z", ok: true, detail: "" } },
        { id: "plain-ftp", label: "Lab FTP", provider: "ftp", transport_encrypted: false, enabled: true, last_run: null },
      ],
      artifacts: [
        { id: "cfg-1", backup_class: "config", location: "local", created_at: "2026-09-21T10:00:00Z", size: 2048, sha256: SHA, verified: true, restorable: true, state: "ok", preserved: false, preserve_reason: null, preserved_by: null, change_count: 1, app_version: "0.23.0" },
        { id: "full-1", backup_class: "full", location: "local", created_at: "2026-09-21T03:00:00Z", size: 1048576, sha256: SHA, verified: true, restorable: true, state: "ok", preserved: true, preserve_reason: "before upgrade", preserved_by: "admin", change_count: 0, app_version: "0.23.0" },
        { id: "full-2", backup_class: "full", location: "offsite", created_at: "2026-09-22T03:00:00Z", size: 4096, sha256: null, verified: false, restorable: false, state: "incomplete", preserved: false, change_count: 0, app_version: null },
      ],
      storage: { local: { config_bytes: 2048, full_bytes: 1048576, count: 2 }, offsite: { config_bytes: 0, full_bytes: 4096, count: 1 } },
      available: true,
      detail: null,
      running: null,
    };
  }

  async function fakeBackupApi(page) {
    const seen = [];
    await page.route(/\/api\/admin\/backups(\/|$|\?)/, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const key = `${request.method()} ${url.pathname}`;
      seen.push({ key, headers: request.headers(), body: request.postData() });
      const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      switch (key) {
        case "GET /api/admin/backups": return json(library());
        case "GET /api/admin/backups/cfg-1": return json({ ...library().artifacts[0], changes: [{ change_id: "c1", at: "2026-09-21T09:59:00Z", action: "system saved", subject: "nas-a.example.test" }] });
        case "POST /api/admin/backups/full-1/restore/inspect": return json({ ok: true, encryption_mode: "plaintext", inspection_receipt: "synthetic-receipt", aggregate_counts: { systems: 1 } });
        case "POST /api/admin/backups/full-1/restore/import": return json({ ok: true, systems: [], stopped_containers: [], restarted_containers: [], restart_failures: {} });
        case "GET /api/admin/backups/lifecycle/plan": return json({ plan_token: "synthetic-plan", expires_at: "2026-09-24T12:00:00Z", items: [{ id: "full-2", location: "offsite", backup_class: "full", kind: "unverified", reason: "unverified for longer than grace 1d (age 2d)" }], guarded: [] });
        case "POST /api/admin/backups/lifecycle/apply": return json({ ok: true, deleted: ["full-2"], already_missing: [], failed: null, not_attempted: [] });
        case "POST /api/admin/backups/targets/plain-ftp/test": return json({ ok: true, detail: "writable", duration_ms: 12 });
        default: return json({ detail: `synthetic fake has no ${key}` }, 404);
      }
    });
    return seen;
  }

  async function openBackups(page) {
    await gotoAdmin(page);
    const tab = page.locator('[data-admin-view-button="backups"]');
    await tab.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-admin-view-panel="backups"]')).toBeVisible();
    await expect(tab).toHaveAttribute("aria-pressed", "true");
    await expect(page).toHaveURL(/view=backups/);
    await expect(page.locator("#backup-library-status")).toHaveText("3 backup copies.");
  }

  test("lists copies by type with state, kept reason and unencrypted label", async ({ page }) => {
    await fakeBackupApi(page);
    await openBackups(page);

    await expect(page.locator("#backup-library-policies")).toContainText("Settings backups");
    await expect(page.locator("#backup-library-policies")).toContainText("Copies kept on targets20");
    const ftp = page.locator('#backup-library-targets tr[data-target-id="plain-ftp"]');
    await expect(ftp.locator(".backup-plain-badge")).toHaveText("Unencrypted");
    await expect(ftp).toContainText("Not used yet");
    await ftp.getByRole("button", { name: "Test" }).click();
    await expect(ftp).toContainText("Works (12 ms).");

    const incomplete = page.locator('tr[data-artifact-id="full-2"]');
    await expect(incomplete).toContainText("Incomplete, can't restore");
    await expect(incomplete.getByRole("button", { name: "Restore" })).toBeDisabled();
    await expect(page.locator('tr[data-artifact-id="full-1"]')).toContainText("Kept before upgrade");
    await expect(page.locator('tr[data-artifact-id="cfg-1"] a[data-backup-action="download"]'))
      .toHaveAttribute("href", "/api/admin/backups/cfg-1/download");
    await expect(page.locator("#backup-library-storage")).toContainText("This server");
    await expect(page.locator('[data-admin-view-panel="backups"]')).not.toContainText(/\/(?:srv|data|run|home)\//);
  });

  test("details dialog shows changes and Escape returns focus", async ({ page }) => {
    await fakeBackupApi(page);
    await openBackups(page);
    const details = page.locator('tr[data-artifact-id="cfg-1"]').getByRole("button", { name: "Details" });
    await details.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Backup details" });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("system saved: nas-a.example.test");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(details).toBeFocused();
  });

  test("restore from the server inspects, confirms, then imports without an upload", async ({ page }) => {
    const seen = await fakeBackupApi(page);
    await openBackups(page);
    await page.locator('tr[data-artifact-id="full-1"]').getByRole("button", { name: "Restore" }).click();
    const dialog = page.getByRole("dialog", { name: "Restore from this backup" });
    await expect(dialog.locator("#backup-restore-passphrase")).toBeFocused();
    await dialog.getByRole("button", { name: "Check backup" }).click();
    await expect(dialog).toContainText("Restoring replaces all current settings, mappings and history with this backup. Continue?");
    expect(seen.some((call) => call.key.endsWith("/restore/import"))).toBe(false);
    await dialog.getByRole("button", { name: "Restore", exact: true }).click();
    await expect(dialog).toContainText("Restored.");
    const imported = seen.find((call) => call.key.endsWith("/restore/import"));
    expect(imported.headers["x-backup-inspection-receipt"]).toBe("synthetic-receipt");
    expect(imported.headers["x-backup-expected-encryption"]).toBe("plaintext");
    expect(imported.body).toBeNull();
    await expect(page.locator("#admin-status-banner")).toContainText("Backup restored.");
  });

  test("clean up previews the plan and applies exactly its token", async ({ page }) => {
    const seen = await fakeBackupApi(page);
    await openBackups(page);
    await page.locator("#backup-library-cleanup-button").click();
    const dialog = page.getByRole("dialog", { name: "Clean up old backups" });
    await expect(dialog).toContainText("never verified, and older than 1 day (it is 2 days old)");
    await dialog.getByRole("button", { name: "Delete 1" }).click();
    await expect(dialog).toContainText("Deleted 1 copy.");
    const applied = seen.find((call) => call.key === "POST /api/admin/backups/lifecycle/apply");
    expect(JSON.parse(applied.body)).toEqual({ plan_token: "synthetic-plan" });
  });
});
