const { test, expect } = require("@playwright/test");
const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");

function buildOfflineSnapshotFixture() {
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
