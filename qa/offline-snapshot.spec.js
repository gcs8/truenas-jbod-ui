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

function buildOfflineLegacyFaceSnapshotFixture(faceStyle, builtinProfileId = "") {
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
from app.services.profile_registry import ProfileRegistry

root = pathlib.Path.cwd()
spec = importlib.util.spec_from_file_location("snapshot_export_fixtures", root / "tests" / "test_snapshot_export.py")
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

async def main():
    face_style = sys.argv[2]
    builtin_id = sys.argv[3]
    if builtin_id:
        # The shipped built-in profile; default Settings() never reads local config.
        profile = ProfileRegistry(module.Settings()).get(builtin_id)
        assert profile is not None and profile.face_style == face_style, builtin_id
        layout = profile.slot_layout
        columns = profile.columns
    else:
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
  const result = spawnSync(python, ["-c", script, outputPath, faceStyle, builtinProfileId], {
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
    storage_view_runtime = module.build_storage_view_runtime()
    nvme_view = storage_view_runtime.views[0].model_copy(deep=True)
    nvme_view.id = "nvme-carrier"
    nvme_view.label = "Synthetic NVMe Carrier"
    nvme_view.kind = "nvme_carrier"
    nvme_view.template_id = "nvme-carrier-4"
    nvme_view.template_label = "NVMe Carrier"
    storage_view_runtime.views.append(nvme_view)
    exporter = module.SnapshotExportService(module.Settings(), module.FakeHistoryBackend(), module.templates)
    rendered = await exporter.build_enclosure_snapshot_html(
        request=module.build_request(),
        snapshot=snapshot,
        smart_summary_cache=module.build_smart_summary_cache(),
        storage_view_runtime=storage_view_runtime,
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


function buildBoundedVirtualHistoryFixture({ redact, selectedView }) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbod-bounded-history-"));
  const outputPath = path.join(tempDir, "snapshot.html");
  const script = `
import asyncio
import pathlib
import sys
from tests.test_snapshot_export import build_bounded_history_fixture
async def main():
    rendered, estimate, wire = await build_bounded_history_fixture(
        redact=sys.argv[2] == "true", selected_view=sys.argv[3])
    pathlib.Path(sys.argv[1]).write_text(rendered.html, encoding="utf-8")
asyncio.run(main())
`;
  const result = spawnSync(process.env.PYTHON || "python3", ["-c", script, outputPath, String(redact), selectedView], {
    cwd: path.resolve(__dirname, ".."), encoding: "utf8",
  });
  if (result.status !== 0) throw new Error(`Bounded history fixture failed: ${result.stdout} ${result.stderr}`);
  return outputPath;
}

for (const redact of [false, true]) {
  for (const selectedView of ["boot", "nvme", "bound"]) {
    test(`bounded virtual history ${selectedView} ${redact ? "partial" : "plain"}`, async ({ page }, testInfo) => {
      const snapshotPath = buildBoundedVirtualHistoryFixture({ redact, selectedView });
      const network = [];
      const errors = [];
      page.on("request", request => { if (/^https?:/.test(request.url())) network.push(request.url()); });
      page.on("pageerror", error => errors.push(error.message));
      page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
      await page.route(/^https?:/, route => route.abort());
      try {
        await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });
        const raw = await page.evaluate(() => {
          const b = window.APP_BOOTSTRAP;
          const targets = b.storageViewsRuntime.views.map(view => {
            const slot = view.slots[0];
            const enclosure = Number.isInteger(slot.snapshot_slot) ? view.backing_enclosure_id : `storage-view:${view.id}`;
            const key = `${b.snapshot.selected_system_id}|${enclosure}|${slot.snapshot_slot ?? slot.slot_index}`;
            return { id: view.id, key, history: b.preloadedHistoryBySlot[key] || null };
          });
          return { targets, selected: b.initialSelectedStorageViewId, meta: b.snapshotExportMeta };
        });
        expect(raw.targets.map(target => Boolean(target.history))).toEqual([true, true, true]);
        expect(new Set(raw.targets.map(target => target.key)).size).toBe(3);
        expect(raw.targets.map(target => target.history.latest_values.temperature_c)).toEqual([41, 42, 37]);
        expect(raw.selected).toBe(raw.targets[["boot", "nvme", "bound"].indexOf(selectedView)].id);
        await expect(page.locator("#detail-history-panel")).toBeVisible();
        await expect(page.locator("#detail-history-empty")).toBeHidden();
        await expect(page.locator("#detail-history-content")).toBeVisible();
        await expect(page.locator("#history-metric-grid")).toContainText(`${{ boot: 41, nvme: 42, bound: 37 }[selectedView]} C`);
        await page.locator("#heatmap-toggle-button").click();
        await page.locator("#heatmap-metric-select").selectOption("temperature_c");
        await expect(page.locator("#slot-grid .slot-heatmap-value").first()).toContainText(String({ boot: 41, nvme: 42, bound: 37 }[selectedView]));
        await page.locator(".snapshot-banner-about > summary").click();
        await expect(page.locator(".snapshot-banner-about")).toContainText("History is incomplete");
        await expect(page.locator(".snapshot-banner-about")).not.toContainText("full history detail");
        expect(raw.meta.history_coverage).toBe("truncated");
        expect(network).toEqual([]);
        expect(errors).toEqual([]);
        await testInfo.attach("synthetic-snapshot", { path: snapshotPath, contentType: "text/html" });
        await testInfo.attach("cache-agreement", { body: JSON.stringify(raw), contentType: "application/json" });
        await page.screenshot({ path: testInfo.outputPath("history.png"), fullPage: true });
      } finally {
        console.log(JSON.stringify({ selectedView, redact, httpRequests: network.length, pageErrors: errors.length }));
        fs.rmSync(path.dirname(snapshotPath), { recursive: true, force: true });
      }
    });
  }
}

test("offline snapshot renders preloaded slot history without a live backend", async ({ page }) => {
  const snapshotPath = buildOfflineSnapshotFixture();

  await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

  await expect(page.locator(".snapshot-banner-badge")).toContainText("Offline copy");
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
  // The bay line is plain text (#508): only the status line announces changes.
  expect(await health.getAttribute("aria-live")).toBeNull();
  await expect(health).toContainText(/known bay|not matched to a bay|unknown state|No disks/);
  await expect(page.locator("#mapping-health-evidence")).toContainText(/Bay positions/);
  await expect(page.locator("#status-text")).toHaveAttribute("role", "status");
  await expect(page.locator("#status-text")).toHaveAttribute("aria-live", "polite");

  // A saved copy hides the Summary counters (#485); the "About this copy"
  // disclosure keeps the keyboard contract.
  await expect(page.locator("#inventory-evidence-disclosure")).toBeHidden();
  const evidence = page.locator(".summary-disclosure.snapshot-banner-about");
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
  await expect(page.locator('#heatmap-metric-select option[value="attention_score"]')).toHaveText("Attention Score");
  await expect(page.locator("#heatmap-metric-context")).toContainText("hotter than its neighbours, SMART errors, and heavy writes");
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

// 2.5" sleds are tall and narrow and fit a desktop chassis without scrolling.
for (const { faceStyle, profileId, layoutMode, minHeightRatio } of [
  { faceStyle: "front-drive", profileId: "supermicro-ssg-2028r-shared-front-24", layoutMode: "dense-2.5", minHeightRatio: 2.5 },
  { faceStyle: "rear-drive", profileId: "supermicro-sys-2029gp-tr-right-nvme-2", layoutMode: "compact", minHeightRatio: 2 },
]) {
  test(`offline ${profileId} draws 2.5-inch sleds`, async ({ page }) => {
    const snapshotPath = buildOfflineLegacyFaceSnapshotFixture(faceStyle, profileId);
    await page.setViewportSize({ width: 1920, height: 1100 });
    await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

    const shell = page.locator("#chassis-shell");
    await expect(shell).toHaveAttribute("data-drive-scale", "2.5");
    await expect(shell).toHaveAttribute("data-layout-mode", layoutMode);
    const geometry = await shell.evaluate((element) => {
      const grid = element.querySelector(".slot-grid");
      const tiles = [...element.querySelectorAll(".slot-tile")].map((tile) => tile.getBoundingClientRect());
      return {
        minHeightRatio: Math.min(...tiles.map((rect) => rect.height / rect.width)),
        gridScrollWidth: grid.scrollWidth,
        gridClientWidth: grid.clientWidth,
      };
    });
    expect(geometry.minHeightRatio).toBeGreaterThanOrEqual(minHeightRatio);
    expect(geometry.gridScrollWidth).toBeLessThanOrEqual(geometry.gridClientWidth + 1);
  });
}

test("offline 24-bay 2.5-inch front keeps the state chip off the latch, LED and labels", async ({ page }) => {
  const snapshotPath = buildOfflineLegacyFaceSnapshotFixture("front-drive", "supermicro-ssg-2028r-shared-front-24");
  for (const width of [1920, 1100]) {
    await page.setViewportSize({ width, height: 1100 });
    await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });
    const { chips, collisions } = await page.locator("#chassis-shell").evaluate((shell) => {
      const overlaps = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
      const hits = [];
      let chipCount = 0;
      // The fixture selects slot 0 and its vdev peers, which draw rings
      // instead of chips; clear that so every bay shows its state chip.
      for (const tile of shell.querySelectorAll(".slot-tile")) tile.classList.remove("selected", "peer-highlight");
      for (const tile of shell.querySelectorAll(".slot-tile")) {
        const tileRect = tile.getBoundingClientRect();
        const chip = getComputedStyle(tile, "::after");
        if (chip.content === "none" || chip.content === '""') continue;
        chipCount += 1;
        const width = parseFloat(chip.width);
        const height = parseFloat(chip.height);
        const centered = chip.transform !== "none";
        const left = tileRect.left + parseFloat(chip.left) - (centered ? width / 2 : 0);
        const top = tileRect.top + parseFloat(chip.top) - (centered ? height / 2 : 0);
        const chipRect = { left, top, right: left + width, bottom: top + height };
        for (const part of [".slot-status-led", ".slot-latch", ".slot-number", ".slot-device", ".slot-pool"]) {
          const element = tile.querySelector(part);
          if (element && overlaps(chipRect, element.getBoundingClientRect())) hits.push(`${tile.dataset.slot}${part}`);
        }
      }
      return { chips: chipCount, collisions: hits };
    });
    expect(chips, `viewport ${width}`).toBe(24);
    expect(collisions, `viewport ${width}`).toEqual([]);
  }
});

test("offline top-loader snapshot keeps exported row geometry", async ({ page }) => {
  const snapshotPath = buildOfflineTopLoaderSnapshotFixture();

  await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

  const shell = page.locator("#chassis-shell");
  await expect(page.locator(".snapshot-banner-badge")).toContainText("Offline copy");
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
  await expect(page.locator(".snapshot-banner-badge")).toContainText("Offline copy");
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

test("arrow navigation handles storage and NVMe grids while excluding unavailable tiles", async ({ page }) => {
  const snapshotPath = buildOfflineSnapshotWithViewsFixture();
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto(pathToFileURL(snapshotPath).href, { waitUntil: "load" });

  await page.evaluate(() => {
    window.__arrowDefaultPrevented = [];
    document.addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
        window.__arrowDefaultPrevented.push({ key: event.key, defaultPrevented: event.defaultPrevented });
      }
    });
  });

  const selector = page.locator("#enclosure-select");
  await selector.selectOption("view:boot-doms");
  await expect(page.locator("#chassis-shell")).toHaveAttribute("data-face-style", "boot-devices");
  let tiles = page.locator("#slot-grid .slot-tile");
  await expect(tiles).toHaveCount(2);
  const firstStorageSlot = await tiles.first().getAttribute("data-slot");
  await tiles.first().focus();
  await tiles.nth(1).evaluate((tile) => tile.classList.add("filtered-out"));
  await page.keyboard.press("ArrowRight");
  expect(await page.evaluate(() => document.activeElement?.dataset?.slot || null)).toBe(firstStorageSlot);
  await tiles.nth(1).evaluate((tile) => tile.classList.remove("filtered-out"));
  await page.keyboard.press("ArrowRight");
  expect(await page.evaluate(() => document.activeElement?.dataset?.slot || null)).not.toBe(firstStorageSlot);

  await selector.selectOption("view:nvme-carrier");
  await expect(page.locator("#chassis-shell")).toHaveAttribute("data-face-style", "nvme-carrier");
  tiles = page.locator("#slot-grid .slot-tile:not(.filtered-out):not(:disabled)");
  await expect(tiles).toHaveCount(2);
  const firstNvmeSlot = await tiles.first().getAttribute("data-slot");
  await tiles.first().focus();
  await page.keyboard.press("ArrowRight");
  expect(await page.evaluate(() => document.activeElement?.dataset?.slot || null)).not.toBe(firstNvmeSlot);

  expect(await page.evaluate(() => window.__arrowDefaultPrevented)).toEqual([
    { key: "ArrowRight", defaultPrevented: false },
    { key: "ArrowRight", defaultPrevented: true },
    { key: "ArrowRight", defaultPrevented: true },
  ]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
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
  await expect(page.locator(".snapshot-banner-badge")).toContainText("Offline copy");
  await expect(page.locator(".snapshot-banner-meta")).toContainText("2 enclosures");
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
