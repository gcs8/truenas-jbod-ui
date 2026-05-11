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
