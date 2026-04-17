const { test, expect } = require("@playwright/test");

async function gotoApp(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#system-select")).toBeVisible();
  await expect(page.locator("#slot-grid")).toBeVisible();
}

async function selectFirstVisibleSlot(page) {
  const tiles = page.locator("#slot-grid .slot-tile");
  test.skip((await tiles.count()) === 0, "Need at least one visible slot tile.");
  await tiles.first().click();
}

async function getSelectValues(page, selector) {
  return page.locator(`${selector} option`).evaluateAll((options) =>
    options
      .map((option) => option.value)
      .filter((value) => typeof value === "string" && value.length > 0)
  );
}

async function latestUiPerfRun(page) {
  return page.evaluate(() => window.__JBOD_UI_PERF?.recentRuns?.[0] || null);
}

async function uiPerfEnabled(page) {
  return page.evaluate(() => Boolean(window.__JBOD_UI_PERF));
}

async function setAutoRefresh(page, enabled) {
  const toggle = page.locator("#auto-refresh-toggle");
  const currentlyEnabled = await toggle.isChecked();
  if (currentlyEnabled !== enabled) {
    await toggle.click();
  }
}

async function waitForRefreshToSettle(page, reason, previousRunId = null) {
  if (await uiPerfEnabled(page)) {
    await page.waitForFunction(
      ([expectedReason, previousId]) => {
        const latest = window.__JBOD_UI_PERF?.recentRuns?.[0];
        return Boolean(
          latest &&
            latest.reason === expectedReason &&
            latest.status === "done" &&
            latest.id !== previousId
        );
      },
      [reason, previousRunId],
      { timeout: 20_000 }
    );
  }
  await expect(page.locator("#status-text")).toHaveText(/Inventory updated\./, {
    timeout: 20_000,
  });
}

async function switchSelect(page, selector, value, reason) {
  const previousRunId = (await latestUiPerfRun(page))?.id || null;
  await page.locator(selector).selectOption(value);
  await waitForRefreshToSettle(page, reason, previousRunId);
}

async function switchSystem(page, value) {
  await switchSelect(page, "#system-select", value, "system-switch");
}

async function switchEnclosure(page, value) {
  await switchSelect(page, "#enclosure-select", value, "enclosure-switch");
}

async function findSystemWithMultipleEnclosures(page) {
  const systems = await getSelectValues(page, "#system-select");
  const currentSystem = await page.locator("#system-select").inputValue();
  const candidates = [currentSystem, ...systems.filter((value) => value !== currentSystem)];

  for (const systemId of candidates) {
    if (systemId !== currentSystem) {
      await switchSystem(page, systemId);
    }
    const enclosures = await getSelectValues(page, "#enclosure-select");
    if (enclosures.length > 1) {
      return { systemId, enclosures };
    }
  }
  return null;
}

test.describe("browser qa smoke", () => {
  test("page loads and exposes the main switching chrome", async ({ page }) => {
    await gotoApp(page);

    await expect(page.locator("#system-select")).toBeVisible();
    await expect(page.locator("#enclosure-select")).toBeVisible();
    await expect(page.locator("#status-text")).toBeVisible();
    await expect(page.locator("#slot-grid .slot-tile")).not.toHaveCount(0);

    if (await uiPerfEnabled(page)) {
      await expect(page.locator("#ui-perf-panel")).toBeVisible();
      const perfState = await page.evaluate(() => window.__JBOD_UI_PERF);
      expect(perfState).toBeTruthy();
      expect(Array.isArray(perfState.recentRuns)).toBeTruthy();
    }
  });

  test("system switches reuse the cached path and complete cleanly", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);

    const systems = await getSelectValues(page, "#system-select");
    test.skip(systems.length < 2, "Need at least two configured systems for switch coverage.");

    const current = await page.locator("#system-select").inputValue();
    const target = systems.find((value) => value !== current);
    test.skip(!target, "Need a second configured system for switch coverage.");

    await switchSystem(page, target);
    await expect(page.locator("#system-select")).toHaveValue(target);

    if (await uiPerfEnabled(page)) {
      const latest = await latestUiPerfRun(page);
      expect(latest.reason).toBe("system-switch");
      expect(latest.status).toBe("done");
    }
  });

  test("enclosure switches complete without stale-scope bleed", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);

    const result = await findSystemWithMultipleEnclosures(page);
    test.skip(!result, "Need one configured system with multiple enclosure views.");

    const current = await page.locator("#enclosure-select").inputValue();
    const target = result.enclosures.find((value) => value !== current);
    test.skip(!target, "Need a second enclosure option for the selected system.");

    await switchEnclosure(page, target);
    await expect(page.locator("#enclosure-select")).toHaveValue(target);

    if (await uiPerfEnabled(page)) {
      const latest = await latestUiPerfRun(page);
      expect(latest.reason).toBe("enclosure-switch");
      expect(latest.status).toBe("done");
    }
  });

  test("slot detail clears when the operator switches systems", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);

    const systems = await getSelectValues(page, "#system-select");
    test.skip(systems.length < 2, "Need at least two configured systems for detail reset coverage.");

    await selectFirstVisibleSlot(page);
    await expect(page.locator("#detail-content")).toBeVisible();
    await expect(page.locator("#detail-empty")).toBeHidden();

    const current = await page.locator("#system-select").inputValue();
    const target = systems.find((value) => value !== current);
    test.skip(!target, "Need a second configured system for detail reset coverage.");

    await switchSystem(page, target);
    await expect(page.locator("#detail-empty")).toBeVisible();
    await expect(page.locator("#detail-content")).toBeHidden();
  });

  test("auto-refresh does not immediately fire after a system switch", async ({ page }) => {
    await gotoApp(page);
    test.skip(!(await uiPerfEnabled(page)), "Perf timing must be enabled for the auto-refresh timing assertion.");

    const systems = await getSelectValues(page, "#system-select");
    test.skip(systems.length < 2, "Need at least two configured systems for auto-refresh coverage.");

    await setAutoRefresh(page, true);
    await page.locator("#refresh-interval-select").selectOption("15");

    const current = await page.locator("#system-select").inputValue();
    const target = systems.find((value) => value !== current);
    test.skip(!target, "Need a second configured system for auto-refresh coverage.");

    await switchSystem(page, target);
    await page.waitForTimeout(4_000);

    const recentReasons = await page.evaluate(() =>
      (window.__JBOD_UI_PERF?.recentRuns || []).map((run) => run.reason)
    );
    expect(recentReasons[0]).toBe("system-switch");
    expect(recentReasons).not.toContain("auto-refresh");
  });

  test("configured systems and enclosure views complete a release sweep cleanly", async ({ page }) => {
    test.setTimeout(120_000);

    await gotoApp(page);
    await setAutoRefresh(page, false);

    const systems = await getSelectValues(page, "#system-select");
    test.skip(systems.length === 0, "Need at least one configured system for release sweep coverage.");

    for (const systemId of systems) {
      if ((await page.locator("#system-select").inputValue()) !== systemId) {
        await switchSystem(page, systemId);
      }
      await expect(page.locator("#system-select")).toHaveValue(systemId);

      const enclosures = await getSelectValues(page, "#enclosure-select");
      for (const enclosureId of enclosures) {
        if ((await page.locator("#enclosure-select").inputValue()) !== enclosureId) {
          await switchEnclosure(page, enclosureId);
        }
        await expect(page.locator("#enclosure-select")).toHaveValue(enclosureId);
        await expect(page.locator("#slot-grid")).toBeVisible();
        await expect(page.locator("#status-text")).toHaveText(/Inventory updated\.|Ready\./, {
          timeout: 20_000,
        });
      }
    }
  });

  test("history drawer opens and settles for a selected slot", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);
    await selectFirstVisibleSlot(page);

    const historyButton = page.locator("#history-toggle-button");
    test.skip(!(await historyButton.isVisible()), "History is not available for the current app instance.");

    await historyButton.click();
    await expect(page.locator("#detail-history-panel")).toBeVisible();
    await page.waitForFunction(() => {
      const loading = document.getElementById("detail-history-loading");
      return Boolean(loading && loading.classList.contains("hidden"));
    }, { timeout: 20_000 });
    await expect(page.locator("#detail-history-error")).toBeHidden();
  });

  test("export snapshot dialog renders estimate UI", async ({ page }) => {
    await page.route("**/api/export/enclosure-snapshot/estimate**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          selected_packaging: "auto",
          effective_packaging: "html",
          auto_packaging: "html",
          allow_oversize: false,
          html_size_label: "6.2 MiB",
          html_within_limit: true,
          zip_size_label: "2.4 MiB",
          zip_within_limit: true,
          selected_size_label: "6.2 MiB",
          selected_within_limit: true,
          selected_allowed: true,
          size_limit_label: "24 MiB",
          downsampling_label: "None",
          metric_sample_count: 128,
          event_count: 12,
        }),
      });
    });

    await gotoApp(page);
    await setAutoRefresh(page, false);

    const exportButton = page.locator("#export-snapshot-button");
    await expect(exportButton).toBeVisible();
    await exportButton.click();

    await expect(page.locator("#export-snapshot-dialog")).toBeVisible();
    await expect(page.locator("#export-snapshot-note")).toContainText("Scope");
    await expect(page.locator("#export-snapshot-estimate")).toContainText("Current Choice");
    await expect(page.locator("#export-snapshot-estimate")).toContainText("Auto -> HTML");
  });
});
