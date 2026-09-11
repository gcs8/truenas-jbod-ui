const { test, expect } = require("@playwright/test");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { pathToFileURL } = require("url");

const repoRoot = path.resolve(__dirname, "..");
const CHROME_LOCALHOST_DEVTOOLS_PROBE = "/.well-known/appspecific/com.chrome.devtools.json";

function isExpectedPagesRequest(requestURL) {
  const requestPath = new URL(requestURL, "http://127.0.0.1").pathname;
  return requestPath.startsWith("/truenas-jbod-ui/")
    || requestPath === CHROME_LOCALHOST_DEVTOOLS_PROBE;
}

function resolveArtifactPath(requestedPath) {
  const artifactPath = path.isAbsolute(requestedPath)
    ? requestedPath
    : path.resolve(repoRoot, requestedPath);
  if (!fs.existsSync(artifactPath)) {
    throw new Error(`Public demo artifact does not exist: ${artifactPath}`);
  }
  return artifactPath;
}

function resolvePublicDemoArtifact() {
  if (process.env.PUBLIC_DEMO_ARTIFACT) {
    return resolveArtifactPath(process.env.PUBLIC_DEMO_ARTIFACT);
  }


  const checkedInArtifact = path.join(repoRoot, "public-demo", "index.html");
  if (fs.existsSync(checkedInArtifact)) {
    return checkedInArtifact;
  }

  throw new Error(
    "No checked-in public-demo/index.html artifact found. Set PUBLIC_DEMO_ARTIFACT "
      + "to an existing deterministic synthetic artifact."
  );
}

function resolveSlotFocusArtifact() {
  if (!process.env.SLOT_FOCUS_ARTIFACT) {
    throw new Error("Set SLOT_FOCUS_ARTIFACT to a current-source synthetic snapshot.");
  }
  return resolveArtifactPath(process.env.SLOT_FOCUS_ARTIFACT);
}

async function startPagesServer(artifactPath) {
  const payload = fs.readFileSync(artifactPath);
  const requests = [];
  const server = http.createServer((request, response) => {
    requests.push(request.url);
    if (new URL(request.url, "http://127.0.0.1").pathname !== "/truenas-jbod-ui/") {
      response.writeHead(404);
      response.end();
      return;
    }
    response.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Length": payload.length,
    });
    response.end(payload);
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("Pages fixture server did not bind a TCP port");
  }
  return {
    baseURL: `http://127.0.0.1:${address.port}/truenas-jbod-ui/`,
    requests,
    close: () => new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

test("public demo static artifact is explorable without a live backend", async ({ page }) => {
  const demoPath = resolvePublicDemoArtifact();
  const consoleErrors = [];
  const outboundRequests = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("request", (request) => {
    if (!/^(?:file|data|blob):/.test(request.url())) {
      outboundRequests.push(request.url());
    }
  });

  await page.goto(pathToFileURL(demoPath).href, { waitUntil: "load" });

  const selector = page.locator("#enclosure-select");
  await expect(page.locator(".snapshot-banner-badge")).toContainText("Frozen Sanitized Snapshot");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("Artifact app v");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("Capture time");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("60 bays in artifact");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("2 saved/virtual views");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("1 event");
  await expect(page.locator(".snapshot-banner-facts")).not.toContainText("1 events");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("Synthetic IDs");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("Source revision");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("Build ID");
  await expect(page.locator(".public-demo-identity")).toHaveCount(2);
  const identities = await page.locator(".public-demo-identity").allTextContents();
  expect(identities[0]).toMatch(/^[0-9a-f]{40}$/);
  expect(identities[1]).toMatch(/^[0-9a-f]{64}$/);
  await expect(page.locator(".snapshot-banner-meta")).toContainText("7d");
  await expect(page.locator("#system-setup-button")).toHaveCount(0);
  await expect(page.locator("#export-snapshot-button")).toHaveCount(0);
  await expect(selector.locator("option:checked")).toContainText("Saved copy · Demo 60-Bay Top Loader");
  await expect(page.locator("#api-status-chip")).toHaveText("API AT CAPTURE");
  await expect(page.locator("#ssh-status-chip")).toHaveText("SSH OFF AT CAPTURE");
  await expect(page.locator("#history-status-chip")).toHaveText("HIST PRELOADED");
  await expect(page.locator("#last-updated").locator("xpath=.." )).toContainText("Snapshot time");
  await expect(page.locator("#status-text")).toContainText("Frozen offline snapshot loaded");

  await expect(page.locator("#sas-fabric-view-link")).toHaveCount(0);
  await page.locator("#sas-fabric-toggle-button").click();
  await expect(page.locator("#sas-fabric-panel")).toBeVisible();
  await expect(page.locator("#sas-fabric-status")).toContainText(
    "This offline snapshot does not include Storage Fabric data or live refresh capability."
  );
  await expect(page.locator("#sas-fabric-inspector-body")).toContainText(
    "No Storage Fabric payload is included in this snapshot."
  );
  await expect(page.locator("#sas-fabric-lanes")).toContainText(
    "No Storage Fabric payload is included in this snapshot."
  );
  await expect(page.locator("#sas-fabric-lanes")).not.toContainText("yet");

  await expect(selector).toBeEnabled();
  await expect(page.locator("#chassis-shell")).toHaveAttribute("data-face-style", "top-loader");
  await expect(page.locator("#slot-grid .row-slots-flat-grouped")).toHaveCount(4);
  await expect(page.locator("#slot-grid .slot-tile.selected")).toHaveCount(0);
  await expect(page.locator("#detail-empty")).toContainText("Select a slot tile");
  await page.locator('#slot-grid .slot-tile[data-slot="57"]').click();
  await expect(page.locator("#detail-kv-grid")).toContainText("Demo Flash SSD 4TB");
  await expect(page.locator("#detail-kv-grid")).toContainText("DEMO-SN-CORE-0057");
  await expect(page.locator("#detail-kv-grid")).toContainText("mirror-8");
  await expect(page.locator("#multipath-context")).toContainText("at capture");
  await expect(page.locator("#multipath-context")).not.toContainText("currently");
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
  await expect(page.locator("#enclosure-panel-title")).toContainText("Demo Boot Modules");
  await page.locator('#slot-grid .slot-tile[data-slot="0"]').click();
  await expect(page.locator("#detail-kv-grid")).toContainText("Demo Boot Flash 128GB");
  await expect(page.locator("#detail-kv-grid")).toContainText("34 C");
  await selector.selectOption("view:nvme-carrier-x4");
  await expect(page.locator("#enclosure-panel-title")).toContainText("Demo 4x NVMe Carrier");
  await page.locator('#slot-grid .slot-tile[data-slot="0"]').click();
  await expect(page.locator("#detail-kv-grid")).toContainText("Demo NVMe Flash 2TB");
  await expect(page.locator("#detail-kv-grid")).toContainText("DEMO-SN-NVME-0000");
  await page.locator("#heatmap-toggle-button").click();
  await expect(page.locator('#slot-grid .slot-tile[data-slot="0"] .slot-heatmap-value')).toBeVisible();

  await expect(page.locator("#refresh-button")).toBeDisabled();
  await expect(page.locator("#disk-inventory-sync-controls")).toHaveClass(/hidden/);
  await expect(page.locator("#export-mappings-button")).toBeDisabled();
  await expect(page.locator("#import-mappings-button")).toBeDisabled();
  expect(consoleErrors).toEqual([]);
  expect(outboundRequests).toEqual([]);
});

test("public demo works from a Pages subpath through reload and history navigation", async ({ page }) => {
  const fixture = await startPagesServer(resolvePublicDemoArtifact());
  const failedResponses = [];
  expect(isExpectedPagesRequest("/truenas-jbod-ui/?review=1")).toBe(true);
  expect(isExpectedPagesRequest(CHROME_LOCALHOST_DEVTOOLS_PROBE)).toBe(true);
  expect(isExpectedPagesRequest("/outside-pages-subpath")).toBe(false);
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedResponses.push(`${response.status()} ${response.url()}`);
    }
  });
  try {
    await page.goto(fixture.baseURL, { waitUntil: "load" });
    await expect(page.locator(".snapshot-banner-badge")).toContainText("Frozen Sanitized Snapshot");
    const probeResponse = await page.request.get(
      new URL(CHROME_LOCALHOST_DEVTOOLS_PROBE, fixture.baseURL).href,
    );
    expect(probeResponse.status()).toBe(404);
    await page.reload({ waitUntil: "load" });
    await page.goto(`${fixture.baseURL}?review=1`, { waitUntil: "load" });
    await expect(page.locator("#enclosure-panel-title")).toContainText("Demo 60-Bay Top Loader");
    expect(new URL(page.url()).searchParams.get("review")).toBe("1");
    await page.goBack({ waitUntil: "load" });
    await expect(page.locator("#enclosure-panel-title")).toContainText("Demo 60-Bay Top Loader");
    expect(new URL(page.url()).searchParams.has("review")).toBe(false);
    await page.goForward({ waitUntil: "load" });
    await expect(page.locator("#enclosure-panel-title")).toContainText("Demo 60-Bay Top Loader");
    expect(new URL(page.url()).searchParams.get("review")).toBe("1");
  } finally {
    await fixture.close();
  }
  expect(failedResponses).toEqual([]);
  expect(fixture.requests.length).toBeGreaterThanOrEqual(3);
  expect(fixture.requests.map((requestURL) => new URL(requestURL, fixture.baseURL).pathname))
    .toContain(CHROME_LOCALHOST_DEVTOOLS_PROBE);
  const unexpectedPagesRequests = fixture.requests.filter(
    (requestURL) => !isExpectedPagesRequest(requestURL),
  );
  expect(unexpectedPagesRequests).toEqual([]);
});

test("public demo desktop accessibility contract holds at supported viewports", async ({ page }) => {
  const demoURL = pathToFileURL(resolvePublicDemoArtifact()).href;
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  for (const viewport of [
    { width: 1280, height: 720 },
    { width: 1920, height: 1080 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto(demoURL, { waitUntil: "load" });
    await expect(page.locator("main")).toHaveCount(1);
    await expect(page.locator("h1")).toHaveCount(1);
    await expect(page.locator("#enclosure-select")).toHaveAccessibleName(/enclosure|view/i);
    await expect(page.locator("#slot-scroll-hint")).toBeHidden();
    const geometry = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      rightmost: Math.max(
        ...Array.from(document.querySelectorAll("button, select"))
          .filter((element) => {
            const style = getComputedStyle(element);
            return style.visibility !== "hidden" && style.display !== "none" && !element.closest("#slot-grid");
          })
          .map((element) => element.getBoundingClientRect().right),
      ),
      unnamedButtons: Array.from(document.querySelectorAll("button"))
        .filter((button) => {
          const style = getComputedStyle(button);
          return style.visibility !== "hidden" && style.display !== "none";
        })
        .filter((button) => !(button.getAttribute("aria-label") || button.textContent || "").trim())
        .length,
    }));
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
    expect(geometry.rightmost).toBeLessThanOrEqual(geometry.clientWidth + 1);
    expect(geometry.unnamedButtons).toBe(0);
    if (viewport.width === 1920) {
      const systemCardWidth = await page.locator(".system-select-card").evaluate(
        (element) => element.getBoundingClientRect().width,
      );
      const enclosureCardWidth = await page.locator(".enclosure-select-card").evaluate(
        (element) => element.getBoundingClientRect().width,
      );
      expect(systemCardWidth).toBeGreaterThanOrEqual(224);
      expect(enclosureCardWidth).toBeGreaterThanOrEqual(384);
    }
  }

  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
  const firstSlot = page.locator("#slot-grid .slot-tile").first();
  await firstSlot.focus();
  await expect(firstSlot).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(firstSlot).toBeFocused();
  const focusStyle = await firstSlot.evaluate((element) => {
    const style = getComputedStyle(element);
    return { outlineStyle: style.outlineStyle, outlineWidth: parseFloat(style.outlineWidth) };
  });
  expect(focusStyle.outlineStyle).not.toBe("none");
  expect(focusStyle.outlineWidth).toBeGreaterThanOrEqual(2);

  const contrast = await page.evaluate(() => {
    const rgba = (value) => {
      const match = value.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
      return match ? [Number(match[1]), Number(match[2]), Number(match[3]), Number(match[4] ?? 1)] : null;
    };
    const luminance = (color) => {
      const channels = color.slice(0, 3).map((value) => {
        const normalized = value / 255;
        return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
    };
    const background = (element) => {
      const base = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
      const baseMatch = base.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
      let result = baseMatch
        ? [parseInt(baseMatch[1], 16), parseInt(baseMatch[2], 16), parseInt(baseMatch[3], 16)]
        : [12, 16, 21];
      const ancestors = [];
      for (let current = element; current; current = current.parentElement) {
        ancestors.push(current);
      }
      for (const current of ancestors.reverse()) {
        const color = rgba(getComputedStyle(current).backgroundColor);
        if (!color || color[3] <= 0) {
          continue;
        }
        result = color.slice(0, 3).map((channel, index) => (
          channel * color[3] + result[index] * (1 - color[3])
        ));
      }
      return [...result, 1];
    };
    const result = {};
    for (const selector of ["#status-text", ".snapshot-banner-badge", ".summary-note", "#enclosure-select"]) {
      const element = document.querySelector(selector);
      const foreground = element ? rgba(getComputedStyle(element).color) : null;
      const behind = element ? background(element) : null;
      if (!foreground || !behind) {
        result[selector] = 0;
        continue;
      }
      const light = Math.max(luminance(foreground), luminance(behind));
      const dark = Math.min(luminance(foreground), luminance(behind));
      result[selector] = (light + 0.05) / (dark + 0.05);
    }
    return result;
  });
  for (const [selector, ratio] of Object.entries(contrast)) {
    expect(ratio, `${selector} text contrast`).toBeGreaterThanOrEqual(4.5);
  }

  // A 640 CSS-pixel viewport is the layout width of a supported 1280-pixel desktop window at 200% browser zoom.
  await page.setViewportSize({ width: 640, height: 720 });
  await page.goto(demoURL, { waitUntil: "load" });
  const zoomGeometry = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(zoomGeometry.scrollWidth).toBeLessThanOrEqual(zoomGeometry.clientWidth + 1);
  expect(consoleErrors).toEqual([]);
});

if (process.env.PUBLIC_DEMO_URL) {
  test("published public demo loads anonymously with exact provenance and same-origin requests", async ({ page }) => {
    const publishedURL = process.env.PUBLIC_DEMO_URL;
    const unexpected = [];
    const failed = [];
    const expectedOrigin = new URL(publishedURL).origin;
    page.on("request", (request) => {
      const target = new URL(request.url());
      if (!/^(?:data|blob):/.test(target.protocol) && target.origin !== expectedOrigin) {
        unexpected.push(request.url());
      }
    });
    page.on("response", (response) => {
      if (response.status() >= 400) {
        failed.push(`${response.status()} ${response.url()}`);
      }
    });
    await page.goto(publishedURL, { waitUntil: "load" });
    await expect(page.locator(".snapshot-banner-badge")).toContainText("Frozen Sanitized Snapshot");
    await expect(page.locator(".snapshot-banner-meta")).toContainText("Source revision");
    await expect(page.locator(".snapshot-banner-meta")).toContainText("Build ID");
    await page.reload({ waitUntil: "load" });
    await expect(page.locator("#slot-grid .slot-tile")).toHaveCount(60);
    expect(unexpected).toEqual([]);
    expect(failed).toEqual([]);
  });
}

test("current-source heat-map overlays win the face and empty-bay cascade", async ({ page }) => {
  const artifactPath = resolveSlotFocusArtifact();
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto(pathToFileURL(artifactPath).href, { waitUntil: "load" });

  const computedOverlays = await page.locator("#slot-grid .slot-tile").first().evaluate((tile) => {
    const shell = document.getElementById("chassis-shell");
    const results = {};
    const faces = [
      { name: "generic", faceStyle: "generic", layoutMode: "standard-3.5" },
      { name: "top-loader", faceStyle: "top-loader", layoutMode: "top-loader-3.5" },
      { name: "unifi-drive", faceStyle: "unifi-drive", layoutMode: "unifi-1row" },
    ];
    for (const face of faces) {
      shell.dataset.faceStyle = face.faceStyle;
      shell.dataset.layoutMode = face.layoutMode;
      for (const stateName of ["state-healthy", "state-empty"]) {
        tile.className = `slot-tile ${stateName} heatmap-active`;
        tile.style.setProperty("--heatmap-rgb", "17, 34, 51");
        tile.style.setProperty("--heatmap-alpha", "0.62");
        const activeStyle = getComputedStyle(tile, "::before");
        results[`${face.name}:${stateName}:active`] = {
          content: activeStyle.content,
          backgroundImage: activeStyle.backgroundImage,
        };

        tile.className = `slot-tile ${stateName} heatmap-missing`;
        const missingStyle = getComputedStyle(tile, "::before");
        results[`${face.name}:${stateName}:missing`] = {
          content: missingStyle.content,
          backgroundImage: missingStyle.backgroundImage,
        };
      }
    }
    return results;
  });

  const activeFailures = Object.entries(computedOverlays)
    .filter(([key, style]) => key.endsWith(":active") && (
      style.content === "none" || !style.backgroundImage.includes("17, 34, 51")
    ))
    .map(([key]) => key);
  const missingFailures = Object.entries(computedOverlays)
    .filter(([key, style]) => key.endsWith(":missing") && (
      style.content === "none" || !style.backgroundImage.includes("repeating-linear-gradient(135deg")
    ))
    .map(([key]) => key);
  expect.soft(activeFailures, "every face/state must retain active heat-map paint").toEqual([]);
  expect.soft(missingFailures, "every face/state must retain the missing-data hatch").toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
});

test("enclosure selector keeps its quoted option, focus, and node across a normal render", async ({ page }) => {
  const artifactPath = resolveSlotFocusArtifact();
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto(pathToFileURL(artifactPath).href, { waitUntil: "load" });

  const observation = await page.evaluate(async () => {
    const select = document.getElementById("enclosure-select");
    const selectedValue = "enclosure:synthetic-enclosure";
    const originalOption = Array.from(select.options).find((option) => option.value === selectedValue);
    if (!originalOption) {
      throw new Error("synthetic enclosure option is missing");
    }
    const mutations = [];
    const observer = new MutationObserver((records) => mutations.push(...records));
    observer.observe(select, { childList: true, subtree: true });
    select.focus();
    document.getElementById("sas-fabric-toggle-button").click();
    await new Promise((resolve) => queueMicrotask(resolve));
    observer.disconnect();
    return {
      childMutations: mutations.filter((record) => record.type === "childList").length,
      focused: document.activeElement === select,
      sameOption: originalOption === Array.from(select.options).find((option) => option.value === selectedValue)
        && originalOption.isConnected,
      selected: originalOption.selected,
      value: select.value,
      text: originalOption.text,
    };
  });

  expect(observation).toEqual({
    childMutations: 0,
    focused: true,
    sameOption: true,
    selected: true,
    value: "enclosure:synthetic-enclosure",
    text: 'Saved copy · Synthetic "Enclosure"',
  });
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
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
