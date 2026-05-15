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

function isOccupiedSnapshotBackedSlot(slot) {
  return Boolean(slot?.occupied) && Number.isInteger(slot.snapshot_slot);
}

function isSnapshotBackedSlot(slot) {
  return Number.isInteger(slot?.snapshot_slot);
}

async function findLiveBackedSavedChassisTarget(page, predicate = () => true, options = {}) {
  const slotPredicate = options.requireOccupied === false
    ? isSnapshotBackedSlot
    : isOccupiedSnapshotBackedSlot;
  const systems = await getSelectValues(page, "#system-select");
  for (const systemId of systems) {
    const payload = await fetchStorageViewsForSystem(page, systemId);
    const view = (payload.views || []).find((candidate) =>
      predicate(candidate) &&
      candidate.kind === "ses_enclosure" &&
      Array.isArray(candidate.slots) &&
      candidate.slots.some(slotPredicate)
    );
    if (!view) {
      continue;
    }
    const slot = view.slots.find(slotPredicate);
    if (!slot) {
      continue;
    }
    return {
      systemId,
      viewId: view.id,
      slotIndex: slot.slot_index,
      snapshotSlot: slot.snapshot_slot,
      backingEnclosureId: view.backing_enclosure_id || null,
      faceStyle: view.face_style || null,
      baySize: view.bay_size || null,
      rowGroups: Array.isArray(view.row_groups) ? view.row_groups : [],
      rowCount: Array.isArray(view.slot_layout) ? view.slot_layout.length : 0,
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

  test("refresh timing strip shows cache TTLs and auto-refresh state", async ({ page }) => {
    await gotoApp(page);

    await expect(page.locator("#refresh-timing-strip")).toBeVisible();
    await expect(page.locator("#cache-timing-chips")).toBeVisible();
    await expect(page.locator('[data-cache-timing-key="snapshot"]')).toContainText("Snapshot");
    await expect(page.locator('[data-cache-timing-key="sources"]')).toContainText("Sources");
    await expect(page.locator('[data-cache-timing-key="smart"]')).toContainText("SMART");
    await expect(page.locator('[data-cache-timing-key="ses"]')).toContainText("SES Paths");
    await expect(page.locator(".cache-timing-chip-bar")).toHaveCount(4);

    await page.locator("#refresh-interval-select").selectOption("15");
    await expect(page.locator("#refresh-countdown-label")).toContainText(/Next refresh/);

    await setAutoRefresh(page, false);
    await expect(page.locator("#refresh-countdown-label")).toHaveText("Auto refresh off");

    await setAutoRefresh(page, true);
    await expect(page.locator("#refresh-countdown-label")).toContainText(/Next refresh/);
  });

  test("heat map mode colors slots and uses bounded history for rate metrics", async ({ page }) => {
    const scopeRequests = [];
    await page.route("**/api/history/status", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          configured: true,
          available: true,
          detail: null,
          counts: { tracked_slots: 4, metric_sample_count: 16 },
          collector: { last_success_at: "2026-05-15T12:00:00Z" },
          scopes: [],
        }),
      });
    });
    await page.route("**/api/history/scope?**", async (route) => {
      const url = new URL(route.request().url());
      const slots = url.searchParams.getAll("slots").map((slot) => Number(slot)).filter((slot) => Number.isInteger(slot));
      scopeRequests.push(url.toString());
      const histories = {};
      const currentObservedAt = new Date(Date.now() - 60_000).toISOString();
      const previousObservedAt = new Date(Date.now() - 3_600_000).toISOString();
      slots.forEach((slot) => {
        histories[String(slot)] = {
          metrics: {
            temperature_c: [
              { observed_at: currentObservedAt, value: 35 + (slot % 4) },
              { observed_at: previousObservedAt, value: 31 + (slot % 4) },
            ],
            bytes_written: [
              { observed_at: currentObservedAt, value: 2_000_000_000 + slot },
              { observed_at: previousObservedAt, value: 1_000_000_000 + slot },
            ],
            bytes_read: [
              { observed_at: currentObservedAt, value: 4_000_000_000 + slot },
              { observed_at: previousObservedAt, value: 3_000_000_000 + slot },
            ],
          },
          events: [],
          sample_counts: { temperature_c: 2, bytes_written: 2, bytes_read: 2 },
          latest_values: {},
        };
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ configured: true, histories }),
      });
    });

    await gotoApp(page);
    await page.locator("#heatmap-toggle-button").click();
    await expect(page.locator("#heatmap-toggle-button")).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("#heatmap-controls")).toBeVisible();
    await expect(page.locator("#heatmap-metric-field")).toBeVisible();
    await expect(page.locator("#heatmap-legend")).toBeVisible();
    await expect(page.locator("#heatmap-metric-select")).toContainText("Attention Score");
    await expect(page.locator("#heatmap-metric-select")).toContainText("Annualized Read");
    await expect(page.locator("#heatmap-metric-select")).toContainText("Read/Write Ratio");
    await expect(page.locator("#heatmap-timeframe-field")).toBeHidden();
    await expect(page.locator("#heatmap-playback-field")).toBeHidden();
    await page.locator("#heatmap-scale-slider").evaluate((slider) => {
      slider.value = "1.5";
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await expect(page.locator("#heatmap-scale-value")).toContainText("150%");
    await expect(page.locator("#slot-grid .slot-heatmap-value").first()).toBeVisible();

    scopeRequests.length = 0;
    await page.locator("#heatmap-metric-select").selectOption("temperature_c");
    await expect(page.locator("#heatmap-playback-field")).toBeVisible();
    await expect(page.locator("#heatmap-timeframe-field")).toBeHidden();
    await page.locator("#heatmap-playback-select").selectOption("timeline");
    await expect(page.locator("#heatmap-timeframe-field")).toBeVisible();
    await expect(page.locator("#heatmap-scrub-field")).toBeVisible();
    await expect.poll(() => scopeRequests.some((requestUrl) => {
      const url = new URL(requestUrl);
      return url.searchParams.getAll("metrics").includes("temperature_c");
    })).toBeTruthy();
    await expect(page.locator("#heatmap-scrub-value")).toContainText("2/2");
    await page.locator("#heatmap-scrub-slider").evaluate((slider) => {
      slider.value = "0";
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await expect(page.locator("#heatmap-scrub-value")).toContainText("1/2");
    await expect(page.locator("#slot-grid .slot-tile.heatmap-active").first()).toBeVisible();

    scopeRequests.length = 0;
    await page.locator("#heatmap-metric-select").selectOption("write_rate");
    await expect(page.locator("#heatmap-timeframe-field")).toBeVisible();
    await page.locator("#heatmap-playback-select").selectOption("current");
    await page.locator("#heatmap-timeframe-select").selectOption("168");
    await expect.poll(() => scopeRequests.some((requestUrl) => {
      const url = new URL(requestUrl);
      return url.searchParams.getAll("metrics").includes("bytes_written")
        && url.searchParams.get("window_hours") === "168";
    })).toBeTruthy();
    const requestedUrl = new URL(scopeRequests.find((requestUrl) => {
      const url = new URL(requestUrl);
      return url.searchParams.getAll("metrics").includes("bytes_written")
        && url.searchParams.get("window_hours") === "168";
    }));
    expect(requestedUrl.searchParams.getAll("slots").length).toBeGreaterThan(0);
    expect(requestedUrl.searchParams.getAll("metrics")).toEqual(["bytes_written"]);
    expect(requestedUrl.searchParams.get("event_limit")).toBe("0");
    expect(requestedUrl.searchParams.get("window_hours")).toBe("168");
    await expect(page.locator("#slot-grid .slot-tile.heatmap-active").first()).toBeVisible();
    await expect(page.locator("#heatmap-legend-status")).toContainText(/value|Loading/);
  });

  test("heat map history metrics degrade cleanly when history is unavailable", async ({ page }) => {
    const scopeRequests = [];
    await page.route("**/api/history/status", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          configured: true,
          available: false,
          detail: "History sidecar is offline for this smoke test.",
          counts: {},
          collector: {},
          scopes: [],
        }),
      });
    });
    await page.route("**/api/history/scope?**", async (route) => {
      scopeRequests.push(route.request().url());
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "history offline" }),
      });
    });

    await gotoApp(page);
    await page.locator("#heatmap-toggle-button").click();
    await page.locator("#heatmap-metric-select").selectOption("write_rate");

    await expect(page.locator("#heatmap-controls")).toBeVisible();
    await expect(page.locator("#heatmap-timeframe-field")).toBeVisible();
    await expect(page.locator("#heatmap-legend-status")).toContainText("History unavailable");
    await expect(page.locator("#slot-grid .slot-tile").first()).toBeVisible();
    await page.waitForTimeout(250);
    expect(scopeRequests).toHaveLength(0);
  });

  test("history sidecar exposes fast and full refresh actions", async ({ page }) => {
    const historyBaseURL = process.env.PLAYWRIGHT_HISTORY_BASE_URL || "http://127.0.0.1:8081";
    const modes = [];

    await page.route("**/api/history/refresh?mode=*", async (route) => {
      const url = new URL(route.request().url());
      const mode = url.searchParams.get("mode");
      modes.push(mode);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          mode,
          detail: `History ${mode} refresh completed.`,
        }),
      });
    });
    await page.route("**/healthz", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          collector: {
            collector_running: true,
            collection_running: true,
            collection_kind: "background",
            collection_activity: "collecting SMART metrics for Archive CORE / Front Shelf (1/2)",
            collection_elapsed_seconds: 42,
            last_inventory_at: "2026-05-15T04:20:49.054763+00:00",
            last_collection_duration_seconds: 508,
            last_collection_inventory_forced: true,
            background_backoff_seconds_remaining: 0,
            source_base_url: "http://enclosure-ui:8000",
            sqlite_path: "/app/history/history.db",
            next_collection_at: "2026-05-15T04:34:17+00:00",
          },
          database_size_bytes: 484569088,
        }),
      });
    });
    await page.route("**/api/history/overview**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          collector: {
            collector_running: true,
            collection_running: false,
            last_inventory_at: "2026-05-15T04:20:49.054763+00:00",
            last_fast_metrics_at: "2026-05-15T04:20:49.054763+00:00",
            last_slow_metrics_at: "2026-05-15T04:20:49.054763+00:00",
            last_collection_duration_seconds: 508,
            last_collection_inventory_forced: true,
            next_collection_at: "2026-05-15T04:34:17+00:00",
            background_backoff_seconds_remaining: 0,
            source_base_url: "http://enclosure-ui:8000",
            sqlite_path: "/app/history/history.db",
          },
          counts: {
            tracked_slots: 227,
            event_count: 887,
            metric_sample_count: 660430,
          },
          counts_exact: false,
          database: {
            size_bytes: 484569088,
          },
          scopes: [
            {
              system_label: "Archive CORE",
              enclosure_label: "Front 24 Bay",
              tracked_slots: 24,
              event_count: null,
              metric_sample_count: null,
              last_seen_at: "2026-05-15T04:20:49.054763+00:00",
            },
          ],
        }),
      });
    });

    const response = await page.goto(historyBaseURL, { waitUntil: "domcontentloaded" }).catch(() => null);
    test.skip(!response || !response.ok(), "History sidecar is not available for browser QA.");

    await expect(page.locator("#history-refresh-fast")).toBeVisible();
    await expect(page.locator("#history-refresh-full")).toBeVisible();
    await expect(page.getByText("DB Size")).toBeVisible();
    await expect(page.getByText("Last collection duration")).toBeVisible();
    await expect(page.getByText("Last collection inventory")).toBeVisible();
    await expect(page.locator("#collector-activity-banner")).toContainText("collecting SMART metrics", { timeout: 7000 });
    await expect(page.locator("#status-current-collection")).toContainText("collecting SMART metrics", { timeout: 7000 });
    await expect(page.locator("#status-last-collection-duration")).toContainText("508.0s");
    await expect(page.locator("#status-last-collection-inventory")).toContainText("forced");
    await page.evaluate(() => window.__HISTORY_DASHBOARD_POLL?.pollOverviewStatus());
    await expect(page.locator("#tracked-slots-value")).toContainText("227");
    await expect(page.locator("#db-size-value")).toContainText("462.1 MiB");
    await expect(page.locator("#tracked-scopes-body")).toContainText("Front 24 Bay");

    await page.locator("#history-refresh-fast").click();
    await expect(page.locator("#history-refresh-status")).toContainText("History fast refresh completed.");
    await expect.poll(() => modes.includes("fast")).toBeTruthy();

    await page.waitForLoadState("domcontentloaded");
    await expect(page.locator("#history-refresh-full")).toBeVisible();
    await page.locator("#history-refresh-full").click();
    await expect(page.locator("#history-refresh-status")).toContainText("History full refresh completed.");
    await expect.poll(() => modes.includes("full")).toBeTruthy();
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
      if (latest) {
        expect(latest.reason).toBe("system-switch");
        expect(latest.status).toBe("done");
      }
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
      if (latest) {
        expect(latest.reason).toBe("enclosure-switch");
        expect(latest.status).toBe("done");
      }
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
    const liveLedControlsVisible = await page.locator("#detail-led-controls").isVisible();
    const liveHistoryButtonVisible = await page.locator("#history-toggle-button").isVisible();

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
    if (liveLedControlsVisible) {
      await expect(page.locator("#detail-led-controls")).toBeVisible();
    } else {
      await expect(page.locator("#detail-led-controls")).toBeHidden();
    }
    if (liveHistoryButtonVisible) {
      await expect(page.locator("#history-toggle-button")).toBeVisible();
    } else {
      await expect(page.locator("#history-toggle-button")).toBeHidden();
    }

    await page.locator(".enclosure-face").click({ position: { x: 40, y: 40 } });
    await expect(page.locator("#slot-grid .slot-tile.selected")).toHaveCount(0);
    await expect(page.locator("#detail-empty")).toBeVisible();
    await expect(page.locator("#detail-empty")).toContainText("Select a slot tile");
    await expect(page.locator("#detail-content")).toBeHidden();
    await expect(page.locator("#detail-secondary")).toBeVisible();
    await expect(page.locator("#history-toggle-button")).toBeHidden();
    await expect(page.locator("#detail-led-controls")).toBeHidden();
  });

  test("top-loader saved chassis preserves profile row dividers and shell geometry", async ({ page }) => {
    await gotoApp(page);
    await setAutoRefresh(page, false);

    const target = await findLiveBackedSavedChassisTarget(
      page,
      (view) =>
        view.face_style === "top-loader" &&
        Array.isArray(view.row_groups) &&
        view.row_groups.length > 1,
      { requireOccupied: false }
    );
    test.skip(!target, "Need one snapshot-backed top-loader saved chassis view for geometry coverage.");

    await switchSystem(page, target.systemId);
    await switchEnclosure(page, `view:${target.viewId}`);

    const shell = page.locator("#chassis-shell");
    await expect(shell).toHaveAttribute("data-face-style", "top-loader");
    await expect(shell).toHaveAttribute("data-drive-scale", target.baySize || "3.5");
    await expect(shell).toHaveAttribute("data-layout-mode", /top-loader/);
    await expect(shell).toHaveAttribute("data-layout-rows", String(target.rowCount));

    await expect(page.locator("#slot-grid .row-slots-flat-grouped")).toHaveCount(target.rowCount);
    const expectedDividerCount = target.rowCount * Math.max(target.rowGroups.length - 1, 0);
    await expect(page.locator("#slot-grid .row-metal-divider")).toHaveCount(expectedDividerCount);

    const tile = page.locator(`#slot-grid .slot-tile[data-slot="${target.slotIndex}"]`);
    await expect(tile).toBeVisible();
  });

  test("export snapshot dialog renders estimate UI", async ({ page }) => {
    let estimateRequests = 0;
    await page.route("**/api/export/enclosure-snapshot/estimate**", async (route) => {
      estimateRequests += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          selected_packaging: "auto",
          effective_packaging: "html",
          auto_packaging: "html",
          allow_oversize: false,
          html_size_bytes: 6501171,
          html_size_label: "6.2 MiB",
          html_within_limit: true,
          zip_size_bytes: 2516582,
          zip_size_label: "2.4 MiB",
          zip_within_limit: true,
          selected_size_label: "6.2 MiB",
          selected_size_bytes: 6501171,
          selected_within_limit: true,
          selected_allowed: true,
          size_limit_bytes: 25165824,
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
    await expect.poll(() => estimateRequests).toBe(1);

    await page.locator("#export-packaging-select").selectOption("zip");

    await expect(page.locator("#export-snapshot-estimate")).toContainText("ZIP");
    await expect(page.locator("#export-snapshot-estimate")).toContainText("2.4 MiB");
    await expect.poll(() => estimateRequests).toBe(1);
  });
});
