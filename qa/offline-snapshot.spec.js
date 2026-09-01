const { test, expect } = require("@playwright/test");
const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");

function buildOfflineSnapshotFixture({ redactSensitive = false } = {}) {
  const repoRoot = path.resolve(__dirname, "..");
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-offline-snapshot-"));
  const outputPath = path.join(tempDir, "offline-history.html");
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const script = `
import asyncio
import importlib.util
import pathlib
import sys

root = pathlib.Path.cwd()
spec = importlib.util.spec_from_file_location("snapshot_export_fixtures", root / "tests" / "test_snapshot_export.py")
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

async def main():
    snapshot = module.build_snapshot()
    exporter = module.SnapshotExportService(module.Settings(), module.FakeHistoryBackend(), module.templates)
    rendered = await exporter.build_enclosure_snapshot_html(
        request=module.build_request(),
        snapshot=snapshot,
        smart_summary_cache=module.build_smart_summary_cache(),
        selected_slot=0,
        history_window_hours=24,
        history_panel_open=True,
        io_chart_mode="total",
        redact_sensitive=${redactSensitive ? "True" : "False"},
    )
    pathlib.Path(sys.argv[1]).write_text(rendered.html, encoding="utf-8")

asyncio.run(main())
`;
  const result = spawnSync(python, ["-c", script, outputPath], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`Offline snapshot fixture generation failed:\n${result.stdout}\n${result.stderr}`);
  }
  return outputPath;
}

function buildOfflineLegacyFaceSnapshotFixture(faceStyle) {
  const supportedFaces = new Set(["generic", "front-drive", "rear-drive"]);
  if (!supportedFaces.has(faceStyle)) {
    throw new Error(`Unsupported synthetic legacy face: ${faceStyle}`);
  }
  const repoRoot = path.resolve(__dirname, "..");
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), `jbod-${faceStyle}-snapshot-`));
  const outputPath = path.join(tempDir, `offline-${faceStyle}.html`);
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const script = `
import asyncio
import importlib.util
import pathlib
import sys

from app.models.domain import EnclosureProfileView

root = pathlib.Path.cwd()
spec = importlib.util.spec_from_file_location("snapshot_export_fixtures", root / "tests" / "test_snapshot_export.py")
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

async def main():
    face_style = sys.argv[2]
    columns = 14
    layout = [list(range(columns))]
    profile = EnclosureProfileView(
        id=f"synthetic-{face_style}-14",
        label=f"Synthetic {face_style} 14-column face",
        face_style=face_style,
        latch_edge="bottom",
        bay_size="3.5",
        rows=1,
        columns=columns,
        slot_layout=layout,
    )
    slots = [
        module.SlotView(
            slot=slot_number,
            slot_label=f"{slot_number:02}",
            row_index=0,
            column_index=slot_number,
            enclosure_id="synthetic-enclosure",
            enclosure_label="Synthetic Enclosure",
            present=True,
            state=module.SlotState.healthy,
            device_name=f"disk{slot_number}",
            serial=f"SYNTH{slot_number:04}",
            model="Synthetic Disk",
            size_human="1 TB",
            pool_name="synthetic-pool",
            vdev_name="synthetic-vdev",
            health="ONLINE",
        )
        for slot_number in range(columns)
    ]
    snapshot = module.InventorySnapshot(
        slots=slots,
        layout_rows=layout,
        layout_slot_count=columns,
        layout_columns=columns,
        refresh_interval_seconds=30,
        selected_system_id="synthetic-system",
        selected_system_label="Synthetic System",
        selected_enclosure_id="synthetic-enclosure",
        selected_enclosure_label="Synthetic Enclosure",
        selected_profile=profile,
        systems=[module.SystemOption(id="synthetic-system", label="Synthetic System", platform="linux")],
        enclosures=[
            module.EnclosureOption(
                id="synthetic-enclosure",
                label="Synthetic Enclosure",
                profile_id=profile.id,
                rows=profile.rows,
                columns=profile.columns,
                slot_count=columns,
                slot_layout=layout,
            )
        ],
        sources={
            "api": module.SourceStatus(enabled=True, ok=True, message="Synthetic API fixture"),
            "ssh": module.SourceStatus(enabled=False, ok=True, message="SSH disabled for synthetic fixture"),
        },
        summary=module.InventorySummary(
            disk_count=columns,
            pool_count=1,
            enclosure_count=1,
            mapped_slot_count=columns,
            manual_mapping_count=0,
            ssh_slot_hint_count=0,
        ),
    )
    exporter = module.SnapshotExportService(module.Settings(), module.FakeHistoryBackend(), module.templates)
    rendered = await exporter.build_enclosure_snapshot_html(
        request=module.build_request(),
        snapshot=snapshot,
        smart_summary_cache={},
        selected_slot=0,
        history_window_hours=24,
        history_panel_open=False,
        io_chart_mode="total",
    )
    pathlib.Path(sys.argv[1]).write_text(rendered.html, encoding="utf-8")

asyncio.run(main())
`;
  const result = spawnSync(python, ["-c", script, outputPath, faceStyle], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`Offline ${faceStyle} snapshot fixture generation failed:\n${result.stdout}\n${result.stderr}`);
  }
  return outputPath;
}

function buildOfflineTopLoaderSnapshotFixture() {
  const repoRoot = path.resolve(__dirname, "..");
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-top-loader-snapshot-"));
  const outputPath = path.join(tempDir, "offline-top-loader.html");
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const script = `
import asyncio
import importlib.util
import pathlib
import sys

from app.services.profile_registry import CORE_CSE_946_PROFILE_ID, ProfileRegistry

root = pathlib.Path.cwd()
spec = importlib.util.spec_from_file_location("snapshot_export_fixtures", root / "tests" / "test_snapshot_export.py")
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

async def main():
    profile = ProfileRegistry(module.Settings()).get(CORE_CSE_946_PROFILE_ID)
    assert profile is not None
    slots = []
    populated_slots = {57, 58, 59}
    for row_index, row in enumerate(profile.slot_layout):
        for column_index, slot_number in enumerate(row):
            if slot_number is None:
                continue
            populated = slot_number in populated_slots
            slots.append(
                module.SlotView(
                    slot=slot_number,
                    slot_label=f"{slot_number:02}",
                    row_index=row_index,
                    column_index=column_index,
                    enclosure_id="top-loader",
                    enclosure_label="Top Loader",
                    present=populated,
                    state=module.SlotState.healthy if populated else module.SlotState.empty,
                    device_name=f"da{slot_number}" if populated else None,
                    serial=f"TOP{slot_number:04}" if populated else None,
                    model="Disk Model" if populated else None,
                    size_human="1 TB" if populated else None,
                    pool_name="tank" if populated else None,
                    vdev_name="mirror-0" if populated else None,
                    health="ONLINE" if populated else None,
                )
            )
    snapshot = module.InventorySnapshot(
        slots=slots,
        layout_rows=profile.slot_layout,
        layout_slot_count=60,
        layout_columns=15,
        refresh_interval_seconds=30,
        selected_system_id="archive-core",
        selected_system_label="Archive CORE",
        selected_enclosure_id="top-loader",
        selected_enclosure_label="Top Loader",
        selected_profile=profile,
        systems=[module.SystemOption(id="archive-core", label="Archive CORE", platform="core")],
        enclosures=[
            module.EnclosureOption(
                id="top-loader",
                label="Top Loader",
                profile_id=profile.id,
                rows=profile.rows,
                columns=profile.columns,
                slot_count=60,
                slot_layout=profile.slot_layout,
            )
        ],
        sources={
            "api": module.SourceStatus(enabled=True, ok=True, message="API healthy on Archive CORE"),
            "ssh": module.SourceStatus(enabled=False, ok=True, message="SSH disabled for test fixture"),
        },
        summary=module.InventorySummary(
            disk_count=len(populated_slots),
            pool_count=1,
            enclosure_count=1,
            mapped_slot_count=len(populated_slots),
            manual_mapping_count=0,
            ssh_slot_hint_count=0,
        ),
    )
    exporter = module.SnapshotExportService(module.Settings(), module.FakeHistoryBackend(), module.templates)
    rendered = await exporter.build_enclosure_snapshot_html(
        request=module.build_request(),
        snapshot=snapshot,
        smart_summary_cache={},
        selected_slot=57,
        history_window_hours=24,
        history_panel_open=True,
        io_chart_mode="total",
    )
    pathlib.Path(sys.argv[1]).write_text(rendered.html, encoding="utf-8")

asyncio.run(main())
`;
  const result = spawnSync(python, ["-c", script, outputPath], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`Offline top-loader snapshot fixture generation failed:\n${result.stdout}\n${result.stderr}`);
  }
  return outputPath;
}

function buildOfflineSnapshotWithViewsFixture() {
  const repoRoot = path.resolve(__dirname, "..");
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-offline-snapshot-views-"));
  const outputPath = path.join(tempDir, "offline-views.html");
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const script = `
import asyncio
import importlib.util
import pathlib
import sys

root = pathlib.Path.cwd()
spec = importlib.util.spec_from_file_location("snapshot_export_fixtures", root / "tests" / "test_snapshot_export.py")
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

async def main():
    snapshot = module.build_snapshot()
    exporter = module.SnapshotExportService(module.Settings(), module.FakeHistoryBackend(), module.templates)
    rendered = await exporter.build_enclosure_snapshot_html(
        request=module.build_request(),
        snapshot=snapshot,
        smart_summary_cache=module.build_smart_summary_cache(),
        storage_view_runtime=module.build_storage_view_runtime(),
        storage_view_smart_summary_cache=module.build_storage_view_smart_summary_cache(),
        selected_slot=0,
        history_window_hours=24,
        history_panel_open=True,
        io_chart_mode="total",
    )
    pathlib.Path(sys.argv[1]).write_text(rendered.html, encoding="utf-8")

asyncio.run(main())
`;
  const result = spawnSync(python, ["-c", script, outputPath], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`Offline snapshot-with-views fixture generation failed:\n${result.stdout}\n${result.stderr}`);
  }
  return outputPath;
}

function buildOfflineSnapshotWithEnclosuresAndViewsFixture() {
  const repoRoot = path.resolve(__dirname, "..");
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-offline-snapshot-system-"));
  const outputPath = path.join(tempDir, "offline-system.html");
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const script = `
import asyncio
import importlib.util
import pathlib
import sys

root = pathlib.Path.cwd()
spec = importlib.util.spec_from_file_location("snapshot_export_fixtures", root / "tests" / "test_snapshot_export.py")
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

async def main():
    snapshot = module.build_snapshot_with_rear_option()
    rear_snapshot = module.build_rear_snapshot()
    exporter = module.SnapshotExportService(module.Settings(), module.FakeHistoryBackend(), module.templates)
    rendered = await exporter.build_enclosure_snapshot_html(
        request=module.build_request(),
        snapshot=snapshot,
        smart_summary_cache=module.build_smart_summary_cache(),
        live_enclosure_snapshots={
            "front": snapshot,
            "rear": rear_snapshot,
        },
        live_enclosure_smart_summary_cache={
            "front": module.build_smart_summary_cache(),
            "rear": module.build_rear_smart_summary_cache(),
        },
        storage_view_runtime=module.build_storage_view_runtime(),
        storage_view_smart_summary_cache=module.build_storage_view_smart_summary_cache(),
        selected_slot=0,
        history_window_hours=24,
        history_panel_open=True,
        io_chart_mode="total",
    )
    pathlib.Path(sys.argv[1]).write_text(rendered.html, encoding="utf-8")

asyncio.run(main())
`;
  const result = spawnSync(python, ["-c", script, outputPath], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`Offline whole-system snapshot fixture generation failed:\n${result.stdout}\n${result.stderr}`);
  }
  return outputPath;
}

test("offline snapshot renders preloaded slot history without a live backend", async ({ page }) => {
  const snapshotPath = buildOfflineSnapshotFixture();

  await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

  await expect(page.locator(".snapshot-banner-badge")).toContainText("Frozen Offline Artifact");
  await expect(page.locator("#detail-history-panel")).toBeVisible();
  await expect(page.locator("#detail-history-empty")).toBeHidden();
  await expect(page.locator("#detail-history-content")).toBeVisible();
  await expect(page.locator("#history-metric-grid")).toContainText("Temperature");
  await expect(page.locator("#history-metric-grid")).toContainText("37 C");
});

test("offline snapshot exposes mapping health without color-only cues", async ({ page }) => {
  const snapshotPath = buildOfflineSnapshotFixture({ redactSensitive: true });
  const consoleErrors = [];
  const failedRequests = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("requestfailed", (request) => failedRequests.push(request.url()));

  await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

  const health = page.locator("#mapping-health-summary");
  await expect(health).toHaveAttribute("role", "status");
  await expect(health).toHaveAttribute("aria-live", "polite");
  await expect(health).toContainText(/matched/);
  await expect(page.locator("#mapping-health-evidence")).toContainText(/Evidence:.*Snapshot:/);
  await expect(page.locator("#status-text")).toHaveAttribute("role", "status");
  await expect(page.locator("#status-text")).toHaveAttribute("aria-live", "polite");

  const evidence = page.locator(".summary-disclosure");
  await expect(evidence).not.toHaveAttribute("open", "");
  const evidenceSummary = evidence.locator(":scope > summary");
  await evidenceSummary.focus();
  await expect(evidenceSummary).toBeFocused();
  await evidenceSummary.press("Enter");
  await expect(evidence).toHaveAttribute("open", "");

  await expect(page.locator(".legend-item")).toHaveCount(6);
  await expect(page.locator(".swatch.healthy")).toHaveText("✓");
  await expect(page.locator(".swatch.empty")).toHaveText("○");
  await expect(page.locator(".swatch.fault")).toHaveText("!");
  await expect(page.locator(".swatch.unknown")).toHaveText("?");

  const tileCue = await page.locator("#slot-grid .slot-tile").first().evaluate((tile) =>
    window.getComputedStyle(tile, "::after").content
  );
  expect(tileCue).not.toBe("none");
  expect(tileCue).not.toBe("normal");

  await page.locator("#heatmap-toggle-button").click();
  await expect(page.locator('#heatmap-metric-select option[value="attention_score"]')).toHaveText("Derived Attention Score");
  await expect(page.locator("#heatmap-metric-context")).toContainText("relative temperature, errors, and write load");
  await page.locator("#heatmap-metric-select").selectOption("temperature_c");
  await expect(page.locator("#heatmap-metric-context")).toContainText("Degrees Celsius");
  await expect(page.locator("#heatmap-metric-context")).toContainText("vendor's warning and critical thresholds");

  await page.emulateMedia({ forcedColors: "active" });
  const forcedColorCue = await page.locator("#slot-grid .slot-tile").first().evaluate((tile) => {
    const style = window.getComputedStyle(tile, "::after");
    return { content: style.content, borderStyle: style.borderStyle };
  });
  expect(forcedColorCue.content).not.toBe("none");
  expect(["solid", "dotted", "double", "dashed"]).toContain(forcedColorCue.borderStyle);

  const privacyRuleCounts = await page.locator("body").evaluate((body) => {
    const text = body.innerText;
    return {
      privateEndpoints: (text.match(/\b(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)/g) || []).length,
      hostPaths: (text.match(/(?:\/home\/|\/mnt\/|[A-Z]:\\Users\\)/g) || []).length,
      credentialAssignments: (text.match(/\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+/gi) || []).length,
      longBareHex: (text.match(/\b[0-9a-f]{48,}\b/gi) || []).length,
    };
  });
  expect(privacyRuleCounts).toEqual({
    privateEndpoints: 0,
    hostPaths: 0,
    credentialAssignments: 0,
    longBareHex: 0,
  });

  expect(failedRequests).toEqual([]);
  expect(consoleErrors).toEqual([]);
});

for (const faceStyle of ["generic", "front-drive", "rear-drive"]) {
  test(`offline ${faceStyle} face keeps slot controls separate at narrow desktop widths`, async ({ page }) => {
    const snapshotPath = buildOfflineLegacyFaceSnapshotFixture(faceStyle);
    const consoleErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") {
        consoleErrors.push(message.text());
      }
    });
    await page.setViewportSize({ width: 820, height: 1000 });

    await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

    const geometry = await page.locator("#chassis-shell").evaluate((shell) => {
      const tiles = [...shell.querySelectorAll(".slot-tile")];
      const populated = shell.querySelector('.slot-tile[data-slot="0"]');
      const led = populated?.querySelector(".slot-status-led");
      const number = populated?.querySelector(".slot-number");
      if (!populated || !led || !number || tiles.length === 0) {
        throw new Error("synthetic legacy fixture is missing slot geometry controls");
      }
      const ledRect = led.getBoundingClientRect();
      const numberRect = number.getBoundingClientRect();
      const overlapWidth = Math.max(0, Math.min(ledRect.right, numberRect.right) - Math.max(ledRect.left, numberRect.left));
      const overlapHeight = Math.max(0, Math.min(ledRect.bottom, numberRect.bottom) - Math.max(ledRect.top, numberRect.top));
      return {
        faceStyle: shell.dataset.faceStyle,
        shellOverflowX: getComputedStyle(shell).overflowX,
        shellClientWidth: shell.clientWidth,
        shellScrollWidth: shell.scrollWidth,
        documentClientWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        minTileWidth: Math.min(...tiles.map((tile) => tile.getBoundingClientRect().width)),
        controlsOverlap: overlapWidth > 0 && overlapHeight > 0,
      };
    });

    expect(geometry.faceStyle).toBe(faceStyle);
    expect(geometry.shellOverflowX).toBe("auto");
    expect(geometry.shellScrollWidth).toBeGreaterThan(geometry.shellClientWidth);
    expect(geometry.documentScrollWidth).toBeLessThanOrEqual(geometry.documentClientWidth + 1);
    expect(geometry.minTileWidth).toBeGreaterThanOrEqual(72);
    expect(geometry.controlsOverlap).toBe(false);
    expect(consoleErrors).toEqual([]);
  });
}

test("offline top-loader snapshot keeps exported row geometry", async ({ page }) => {
  const snapshotPath = buildOfflineTopLoaderSnapshotFixture();

  await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

  const shell = page.locator("#chassis-shell");
  await expect(page.locator(".snapshot-banner-badge")).toContainText("Frozen Offline Artifact");
  await expect(shell).toHaveAttribute("data-face-style", "top-loader");
  await expect(shell).toHaveAttribute("data-layout-mode", /top-loader/);
  await expect(shell).toHaveAttribute("data-layout-rows", "4");
  await expect(page.locator("#slot-grid .row-slots-flat-grouped")).toHaveCount(4);
  await expect(page.locator("#slot-grid .row-metal-divider")).toHaveCount(8);
  await expect(page.locator('#slot-grid .slot-tile[data-slot="57"]')).toBeVisible();
  await expect(page.locator("#detail-history-panel")).toBeVisible();
  await expect(page.locator("#history-metric-grid")).toContainText("Temperature");
});

test("offline top-loader keeps slot controls separate at narrow desktop widths", async ({ page }) => {
  const snapshotPath = buildOfflineTopLoaderSnapshotFixture();
  await page.setViewportSize({ width: 820, height: 1000 });

  await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

  const geometry = await page.locator("#chassis-shell").evaluate((shell) => {
    const tiles = [...shell.querySelectorAll(".slot-tile")];
    const populated = shell.querySelector('.slot-tile[data-slot="57"]');
    const led = populated?.querySelector(".slot-status-led");
    const number = populated?.querySelector(".slot-number");
    if (!populated || !led || !number || tiles.length === 0) {
      throw new Error("top-loader fixture is missing slot geometry controls");
    }
    const ledRect = led.getBoundingClientRect();
    const numberRect = number.getBoundingClientRect();
    const overlapWidth = Math.max(0, Math.min(ledRect.right, numberRect.right) - Math.max(ledRect.left, numberRect.left));
    const overlapHeight = Math.max(0, Math.min(ledRect.bottom, numberRect.bottom) - Math.max(ledRect.top, numberRect.top));
    return {
      shellOverflowX: getComputedStyle(shell).overflowX,
      shellClientWidth: shell.clientWidth,
      shellScrollWidth: shell.scrollWidth,
      documentClientWidth: document.documentElement.clientWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      minTileWidth: Math.min(...tiles.map((tile) => tile.getBoundingClientRect().width)),
      controlsOverlap: overlapWidth > 0 && overlapHeight > 0,
    };
  });

  expect(geometry.shellOverflowX).toBe("auto");
  expect(geometry.shellScrollWidth).toBeGreaterThan(geometry.shellClientWidth);
  expect(geometry.documentScrollWidth).toBeLessThanOrEqual(geometry.documentClientWidth + 1);
  expect(geometry.minTileWidth).toBeGreaterThanOrEqual(76);
  expect(geometry.controlsOverlap).toBe(false);
});

test("offline snapshot can navigate preloaded storage views without a live backend", async ({ page }) => {
  const snapshotPath = buildOfflineSnapshotWithViewsFixture();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });

  await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

  const selector = page.locator("#enclosure-select");
  await expect(page.locator(".snapshot-banner-badge")).toContainText("Frozen Offline Artifact");
  await expect(selector).toBeEnabled();
  await selector.selectOption("view:boot-doms");
  await expect(page.locator("#enclosure-panel-title")).toContainText("Boot SATADOMs");
  await expect(page.locator("#mapping-health-summary")).toContainText("Boot SATADOMs:");
  await page.locator('#slot-grid .slot-tile[data-slot="0"]').click();
  await expect(page.locator("#detail-kv-grid")).toContainText("SATADOM");
  await expect(page.locator("#detail-kv-grid")).toContainText("41 C");
  await expect(page.locator("#history-toggle-button")).toBeVisible();
  await page.locator("#history-toggle-button").click();
  await expect(page.locator("#history-metric-grid")).toContainText("Temperature");
  await page.locator("#heatmap-toggle-button").click();
  await expect(page.locator("#slot-grid .slot-tile[data-slot=\"0\"] .slot-heatmap-value")).toBeVisible();
  expect(consoleErrors).toEqual([]);
});

test("offline snapshot can navigate preloaded live enclosures without a live backend", async ({ page }) => {
  const snapshotPath = buildOfflineSnapshotWithEnclosuresAndViewsFixture();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });

  await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

  const selector = page.locator("#enclosure-select");
  await expect(page.locator(".snapshot-banner-badge")).toContainText("Frozen Offline Artifact");
  await expect(page.locator(".snapshot-banner-facts")).toContainText("2 live enclosures");
  await expect(selector).toBeEnabled();
  await selector.selectOption("enclosure:rear");
  await expect(page.locator("#enclosure-panel-title")).toContainText("Rear Shelf");
  await page.locator('#slot-grid .slot-tile[data-slot="0"]').click();
  await expect(page.locator("#detail-kv-grid")).toContainText("Rear Disk Model");
  await expect(page.locator("#detail-kv-grid")).toContainText("34 C");
  await expect(page.locator("#detail-history-panel")).toBeVisible();
  await expect(page.locator("#history-metric-grid")).toContainText("Temperature");
  await selector.selectOption("view:boot-doms");
  await expect(page.locator("#enclosure-panel-title")).toContainText("Boot SATADOMs");
  expect(consoleErrors).toEqual([]);
});
