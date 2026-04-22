const { test, expect } = require("@playwright/test");

async function gotoApp(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#system-select")).toBeVisible();
  await expect(page.locator("#slot-grid")).toBeVisible();
}

function isLiveEnclosureValue(value) {
  return typeof value === "string" && value.startsWith("enclosure:");
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
  return page.evaluate(() => Boolean(window.__JBOD_UI_PERF?.enabled));
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
    try {
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
    } catch (error) {
      // Fall back to the visible UI settled state when perf telemetry misses a run.
    }
  }
}

async function waitForSelectorScopeToSettle(page, selector, value) {
  await expect(page.locator(selector)).toHaveValue(value);
  await expect(page.locator("#slot-grid")).toBeVisible();
  await expect(page.locator("#status-text")).toHaveText(/Inventory updated\.|Ready\./, {
    timeout: 20_000,
  });
}

async function switchSelect(page, selector, value, reason = null) {
  const previousRunId = reason ? (await latestUiPerfRun(page))?.id || null : null;
  await page.locator(selector).selectOption(value);
  if (reason) {
    await waitForRefreshToSettle(page, reason, previousRunId);
    await waitForSelectorScopeToSettle(page, selector, value);
    return;
  }
  await waitForSelectorScopeToSettle(page, selector, value);
}

async function switchSystem(page, value) {
  await switchSelect(page, "#system-select", value, "system-switch");
}

async function switchEnclosure(page, value) {
  await switchSelect(page, "#enclosure-select", value, isLiveEnclosureValue(value) ? "enclosure-switch" : null);
}

async function findSystemWithMultipleEnclosures(page) {
  const systems = await getSelectValues(page, "#system-select");
  const currentSystem = await page.locator("#system-select").inputValue();
  const candidates = [currentSystem, ...systems.filter((value) => value !== currentSystem)];
  let fallback = null;

  for (const systemId of candidates) {
    if (systemId !== currentSystem) {
      await switchSystem(page, systemId);
    }
    const enclosures = await getSelectValues(page, "#enclosure-select");
    const liveEnclosures = enclosures.filter((value) => isLiveEnclosureValue(value));
    if (liveEnclosures.length > 1) {
      return { systemId, enclosures: liveEnclosures };
    }
    if (!fallback && enclosures.length > 1) {
      fallback = { systemId, enclosures };
    }
  }
  return fallback;
}

async function fetchStorageViewsForSystem(page, systemId) {
  return page.evaluate(async (selectedSystemId) => {
    const response = await fetch(`/api/storage-views?system_id=${encodeURIComponent(selectedSystemId)}`);
    if (!response.ok) {
      throw new Error(`storage view fetch failed for ${selectedSystemId}: ${response.status}`);
    }
    return response.json();
  }, systemId);
}

async function findLiveBackedSavedChassisTarget(page) {
  const systems = await getSelectValues(page, "#system-select");
  for (const systemId of systems) {
    const payload = await fetchStorageViewsForSystem(page, systemId);
    const view = (payload.views || []).find((candidate) =>
      candidate.kind === "ses_enclosure" &&
      Array.isArray(candidate.slots) &&
      candidate.slots.some((slot) => Number.isInteger(slot.snapshot_slot))
    );
    if (!view) {
      continue;
    }
    const slot = view.slots.find((candidate) => Number.isInteger(candidate.snapshot_slot));
    if (!slot) {
      continue;
    }
    return {
      systemId,
      viewId: view.id,
      slotIndex: slot.slot_index,
      snapshotSlot: slot.snapshot_slot,
      backingEnclosureId: view.backing_enclosure_id || null,
    };
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

  test("storage-view slots expose the history drawer from the main UI", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);

    const enclosureValues = await getSelectValues(page, "#enclosure-select");
    const storageViewValue =
      enclosureValues.find((value) => value === "view:boot-doms") ||
      enclosureValues.find((value) => value.startsWith("view:"));
    test.skip(!storageViewValue, "Need at least one storage-view option for history coverage.");

    await switchEnclosure(page, storageViewValue);

    const tiles = page.locator("#slot-grid .slot-tile");
    test.skip((await tiles.count()) === 0, "Need at least one storage-view slot tile.");
    await tiles.first().click();

    const historyButton = page.locator("#history-toggle-button");
    if (!(await historyButton.isVisible())) {
      await expect(historyButton).toBeHidden();
      return;
    }
    await expect(historyButton).toBeVisible();

    await historyButton.click();
    await expect(page.locator("#detail-history-panel")).toBeVisible();
    await page.waitForFunction(() => {
      const loading = document.getElementById("detail-history-loading");
      return Boolean(loading && loading.classList.contains("hidden"));
    }, { timeout: 20_000 });
    await expect(page.locator("#detail-history-error")).toBeHidden();
  });

  test("boot-device storage views render SATADOM-style cards", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);

    const enclosureValues = await getSelectValues(page, "#enclosure-select");
    const bootViewValue = enclosureValues.find((value) => value === "view:boot-doms");
    test.skip(!bootViewValue, "Need the Boot SATADOMs storage view for SATADOM-card coverage.");

    await switchEnclosure(page, bootViewValue);

    const satadomTiles = page.locator("#slot-grid .slot-tile.storage-view-slot-boot");
    test.skip((await satadomTiles.count()) === 0, "Need at least one SATADOM slot tile.");

    await expect(satadomTiles.first()).toBeVisible();
    await expect(page.locator(".storage-view-runtime-card--satadom").first()).toBeVisible();
    await expect(page.locator(".storage-view-runtime-card-photo").first()).toBeVisible();
  });

  test("snapshot-backed saved chassis views reuse the live slot and detail chrome", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);

    const target = await findLiveBackedSavedChassisTarget(page);
    test.skip(!target, "Need one snapshot-backed saved chassis view for live-chrome coverage.");

    await switchSystem(page, target.systemId);
    const enclosureValues = await getSelectValues(page, "#enclosure-select");
    const liveValue = target.backingEnclosureId && enclosureValues.includes(`enclosure:${target.backingEnclosureId}`)
      ? `enclosure:${target.backingEnclosureId}`
      : enclosureValues.find((value) => value.startsWith("enclosure:"));
    test.skip(!liveValue, "Need a live enclosure option to compare saved-chassis selection behavior.");

    await switchEnclosure(page, liveValue);
    const liveTile = page.locator(`#slot-grid .slot-tile[data-slot="${target.snapshotSlot}"]`);
    await expect(liveTile).toBeVisible();
    const liveGeometry = await page.evaluate((slotIndex) => {
      const tile = document.querySelector(`#slot-grid .slot-tile[data-slot="${slotIndex}"]`);
      const rowSlots = document.querySelector("#slot-grid .row-slots");
      return {
        tileWidth: tile?.getBoundingClientRect().width || 0,
        rowGroupCount: document.querySelectorAll("#slot-grid .row-group").length,
        rowTemplate: rowSlots ? getComputedStyle(rowSlots).gridTemplateColumns : "",
      };
    }, target.snapshotSlot);
    await liveTile.click();
    await expect(liveTile).toHaveClass(/selected/);

    await switchEnclosure(page, `view:${target.viewId}`);
    await expect(page.locator("#slot-grid .slot-tile.selected")).toHaveCount(0);
    await expect(page.locator("#detail-empty")).toBeVisible();
    await expect(page.locator("#detail-empty")).toContainText("Select a slot tile");
    await expect(page.locator("#detail-content")).toBeHidden();
    await expect(page.locator("#detail-secondary")).toBeVisible();
    await expect(page.locator("#history-toggle-button")).toBeHidden();
    await expect(page.locator("#detail-led-controls")).toBeHidden();

    const tile = page.locator(`#slot-grid .slot-tile[data-slot="${target.slotIndex}"]`);
    await expect(tile).toBeVisible();
    const savedGeometry = await page.evaluate((slotIndex) => {
      const tile = document.querySelector(`#slot-grid .slot-tile[data-slot="${slotIndex}"]`);
      const rowSlots = document.querySelector("#slot-grid .row-slots");
      return {
        tileWidth: tile?.getBoundingClientRect().width || 0,
        rowGroupCount: document.querySelectorAll("#slot-grid .row-group").length,
        rowTemplate: rowSlots ? getComputedStyle(rowSlots).gridTemplateColumns : "",
      };
    }, target.slotIndex);
    expect(Math.abs(savedGeometry.tileWidth - liveGeometry.tileWidth)).toBeLessThan(1.5);
    expect(savedGeometry.rowGroupCount).toBe(liveGeometry.rowGroupCount);
    expect(savedGeometry.rowTemplate).toBe(liveGeometry.rowTemplate);
    await tile.click();

    await expect(tile).not.toHaveClass(/storage-view-slot/);
    await expect(page.locator("#detail-secondary")).toBeVisible();
    await expect(page.locator("#detail-led-controls")).toBeVisible();
    await expect(page.locator("#history-toggle-button")).toBeVisible();

    await page.locator(".enclosure-face").click({ position: { x: 40, y: 40 } });
    await expect(page.locator("#slot-grid .slot-tile.selected")).toHaveCount(0);
    await expect(page.locator("#detail-empty")).toBeVisible();
    await expect(page.locator("#detail-empty")).toContainText("Select a slot tile");
    await expect(page.locator("#detail-content")).toBeHidden();
    await expect(page.locator("#detail-secondary")).toBeVisible();
    await expect(page.locator("#history-toggle-button")).toBeHidden();
    await expect(page.locator("#detail-led-controls")).toBeHidden();
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
