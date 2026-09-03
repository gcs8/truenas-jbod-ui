const { test, expect } = require("@playwright/test");
const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");

const repoRoot = path.resolve(__dirname, "..");

function resolveArtifactPath(requestedPath) {
  const artifactPath = path.isAbsolute(requestedPath)
    ? requestedPath
    : path.resolve(repoRoot, requestedPath);
  if (!fs.existsSync(artifactPath)) {
    throw new Error(`Public demo artifact does not exist: ${artifactPath}`);
  }
  return artifactPath;
}

function buildPublicDemoFromLocalHistory() {
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

function resolvePublicDemoArtifact() {
  if (process.env.PUBLIC_DEMO_ARTIFACT) {
    return resolveArtifactPath(process.env.PUBLIC_DEMO_ARTIFACT);
  }

  if (process.env.PUBLIC_DEMO_BUILD_FROM_HISTORY === "1") {
    return buildPublicDemoFromLocalHistory();
  }

  const checkedInArtifact = path.join(repoRoot, "public-demo", "index.html");
  if (fs.existsSync(checkedInArtifact)) {
    return checkedInArtifact;
  }

  throw new Error(
    "No checked-in public-demo/index.html artifact found. Set PUBLIC_DEMO_ARTIFACT "
      + "to an existing artifact, or set PUBLIC_DEMO_BUILD_FROM_HISTORY=1 on a "
      + "release-maintainer checkout with local ignored history/history.db."
  );
}

function resolveSlotFocusArtifact() {
  if (!process.env.SLOT_FOCUS_ARTIFACT) {
    throw new Error("Set SLOT_FOCUS_ARTIFACT to a current-source synthetic snapshot.");
  }
  return resolveArtifactPath(process.env.SLOT_FOCUS_ARTIFACT);
}

test("public demo static artifact is explorable without a live backend", async ({ page }) => {
  const demoPath = resolvePublicDemoArtifact();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });

  await page.goto(pathToFileURL(demoPath).href, { waitUntil: "load" });

  const selector = page.locator("#enclosure-select");
  await expect(page.locator(".snapshot-banner-badge")).toContainText("Frozen Sanitized Snapshot");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("Artifact app v");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("Capture time");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("60 visible bays");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("2 saved/virtual views");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("Scrambled IDs");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("7d");
  await expect(page.locator("#system-setup-button")).toHaveCount(0);
  await expect(page.locator("#export-snapshot-button")).toHaveCount(0);
  await expect(page.locator("#status-text")).toContainText("Frozen offline snapshot loaded");

  const storageFabricAction = page.locator("#sas-fabric-view-link");
  await expect(storageFabricAction).toHaveAttribute("href", "#sas-fabric-panel");
  await storageFabricAction.click();
  await expect(page.locator("#sas-fabric-panel")).toBeVisible();
  await expect(page).toHaveURL(/#sas-fabric-panel$/);

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

test("slot keyboard selection preserves the focused tile DOM identity", async ({ page }) => {
  const demoPath = resolveSlotFocusArtifact();
  await page.goto(pathToFileURL(demoPath).href, { waitUntil: "load" });

  const tile = page.locator("#slot-grid .slot-tile:not(.filtered-out)").first();
  await expect(tile).toBeVisible();
  const originalTile = await tile.elementHandle();
  expect(originalTile).toBeTruthy();

  await tile.focus();
  await page.keyboard.press("Enter");

  await expect(tile).toHaveClass(/selected/);
  await expect(tile).toHaveAttribute("aria-pressed", "true");
  expect(await originalTile.evaluate((node) => node.isConnected && document.activeElement === node)).toBeTruthy();

  await page.keyboard.press("Space");
  await expect(tile).not.toHaveClass(/selected/);
  await expect(tile).toHaveAttribute("aria-pressed", "false");
  expect(await originalTile.evaluate((node) => node.isConnected && document.activeElement === node)).toBeTruthy();
});

test("slot grid arrow navigation moves visible focus", async ({ page }) => {
  const demoPath = resolveSlotFocusArtifact();
  await page.goto(pathToFileURL(demoPath).href, { waitUntil: "load" });

  const tiles = page.locator("#slot-grid .slot-tile:not(.filtered-out)");
  expect(await tiles.count()).toBeGreaterThan(1);
  const firstSlot = await tiles.first().getAttribute("data-slot");
  await tiles.first().focus();

  const focusStyle = await tiles.first().evaluate((node) => {
    const style = getComputedStyle(node);
    return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth };
  });
  expect(focusStyle.outlineStyle).not.toBe("none");
  expect(Number.parseFloat(focusStyle.outlineWidth)).toBeGreaterThan(0);

  await page.keyboard.press("ArrowRight");
  const activeSlot = await page.evaluate(() => document.activeElement?.dataset?.slot || null);
  expect(activeSlot).not.toBe(firstSlot);
  expect(activeSlot).toBeTruthy();

  await page.keyboard.press("Tab");
  const tabSlot = await page.evaluate(() => document.activeElement?.dataset?.slot || null);
  expect(tabSlot).toBeTruthy();
  expect(tabSlot).not.toBe(activeSlot);
});

test("delegated slot hover preserves identify state and tooltip behavior", async ({ page }) => {
  const demoPath = resolveSlotFocusArtifact();
  await page.goto(pathToFileURL(demoPath).href, { waitUntil: "load" });

  const identifyTile = page.locator("#slot-grid .slot-tile.state-identify:not(.filtered-out)").first();
  await expect(identifyTile).toBeVisible();
  await identifyTile.hover();

  await expect(identifyTile).toHaveClass(/state-identify/);
  await expect(page.locator("#slot-tooltip")).toHaveAttribute("aria-hidden", "false");

  await page.mouse.move(0, 0);
  await expect(page.locator("#slot-tooltip")).toHaveAttribute("aria-hidden", "true");
});

test("slot grid rebuild restores focus to the same visible slot", async ({ page }) => {
  const demoPath = resolveSlotFocusArtifact();
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto(pathToFileURL(demoPath).href, { waitUntil: "load" });

  const tile = page.locator("#slot-grid .slot-tile:not(.filtered-out)").first();
  await expect(tile).toBeVisible();
  const slotNumber = await tile.getAttribute("data-slot");
  await tile.focus();

  const focusTransition = await page.evaluate(() => {
    const search = document.getElementById("search-box");
    const before = document.activeElement?.dataset?.slot || null;
    search.value = "";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    return {
      before,
      after: document.activeElement?.dataset?.slot || null,
      activeTag: document.activeElement?.tagName || null,
    };
  });

  expect(pageErrors).toEqual([]);
  expect(focusTransition).toEqual({ before: slotNumber, after: slotNumber, activeTag: "BUTTON" });
  await expect(page.locator(`#slot-grid .slot-tile[data-slot="${slotNumber}"]`)).toBeVisible();
});
