from __future__ import annotations

import os
import tempfile
from pathlib import Path
from shutil import copy2
from urllib.parse import urlencode

from playwright.sync_api import Page, sync_playwright

from app import __version__


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://localhost:8080/"
DOCS_IMAGES_DIR = ROOT / "docs" / "images" / "screenshots"
WIKI_IMAGES_DIR = ROOT / "wiki" / "images"
SCREENSHOT_TAG = os.environ.get("SCREENSHOT_TAG", f"v{__version__}")

ARCHIVE_CORE_PARAMS = {
    "system_id": "archive-core",
    "enclosure_id": "500304801f715f3f+500304801f5a003f",
}
HISTORY_SLOT = 30
HISTORY_WINDOW_VALUE = "72"


def wait_for_kv_value(page: Page, label: str) -> None:
    page.wait_for_function(
        """([wantedLabel]) => {
            const rows = Array.from(document.querySelectorAll('.kv-row'));
            const row = rows.find((candidate) => {
                const labelNode = candidate.querySelector('.kv-label');
                return labelNode && labelNode.textContent.trim() === wantedLabel;
            });
            if (!row) return false;
            const valueNode = row.querySelector('.kv-value');
            if (!valueNode) return false;
            const value = valueNode.textContent.trim();
            return value && value !== 'n/a' && value !== 'Loading...';
        }""",
        arg=[label],
        timeout=120_000,
    )


def write_screenshot(locator, filename: str) -> Path:
    DOCS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    WIKI_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    docs_target = DOCS_IMAGES_DIR / filename
    wiki_target = WIKI_IMAGES_DIR / filename
    locator.screenshot(path=str(docs_target))
    copy2(docs_target, wiki_target)
    return docs_target


def open_archive_core_slot(page: Page, slot: int) -> None:
    page.goto(f"{BASE_URL}?{urlencode(ARCHIVE_CORE_PARAMS)}", wait_until="networkidle")
    slot_tile = page.locator(f'#slot-grid .slot-tile[data-slot="{slot}"]')
    slot_tile.wait_for(state="visible", timeout=120_000)
    slot_tile.click()
    page.locator("#detail-content").wait_for(state="visible", timeout=120_000)
    wait_for_kv_value(page, "Power On")


def open_history_drawer(page: Page, window_value: str) -> None:
    history_button = page.locator("#history-toggle-button")
    history_button.wait_for(state="visible", timeout=120_000)
    history_button.click()
    page.locator("#detail-history-panel").wait_for(state="visible", timeout=120_000)
    page.locator("#detail-history-content").wait_for(state="visible", timeout=120_000)
    page.locator("#history-timeframe-select").select_option(window_value)
    page.wait_for_timeout(1200)


def capture_history_drawer(page: Page) -> None:
    write_screenshot(page.locator(".app-shell"), f"history-drawer-{SCREENSHOT_TAG}.png")


def open_export_dialog(page: Page) -> None:
    page.locator("#export-snapshot-button").click()
    dialog = page.locator("#export-snapshot-dialog")
    dialog.wait_for(state="visible", timeout=120_000)
    page.locator("#export-packaging-select").select_option("html")
    page.locator("#export-snapshot-estimate .snapshot-export-estimate-card").first.wait_for(
        state="visible",
        timeout=120_000,
    )
    page.wait_for_timeout(800)


def capture_export_dialog(page: Page) -> None:
    write_screenshot(
        page.locator("#export-snapshot-dialog .snapshot-export-form"),
        f"snapshot-export-dialog-{SCREENSHOT_TAG}.png",
    )


def capture_offline_snapshot(browser, page: Page) -> None:
    with page.expect_download(timeout=180_000) as download_info:
        page.locator("#export-snapshot-confirm").click()
    download = download_info.value
    temp_dir = Path(tempfile.mkdtemp(prefix="jbod-snapshot-"))
    download_path = temp_dir / download.suggested_filename
    download.save_as(str(download_path))

    offline_page = browser.new_page(viewport={"width": 1900, "height": 2600}, device_scale_factor=1)
    offline_page.goto(download_path.as_uri(), wait_until="load")
    offline_page.locator(".snapshot-banner-badge").wait_for(state="visible", timeout=120_000)
    offline_page.locator("#detail-content").wait_for(state="visible", timeout=120_000)
    offline_page.locator("#detail-history-panel").wait_for(state="visible", timeout=120_000)
    offline_page.wait_for_timeout(800)
    write_screenshot(offline_page.locator(".app-shell"), f"offline-snapshot-{SCREENSHOT_TAG}.png")
    offline_page.close()


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1900, "height": 2600}, device_scale_factor=1)
        open_archive_core_slot(page, HISTORY_SLOT)
        open_history_drawer(page, HISTORY_WINDOW_VALUE)
        capture_history_drawer(page)
        open_export_dialog(page)
        capture_export_dialog(page)
        capture_offline_snapshot(browser, page)
        browser.close()


if __name__ == "__main__":
    main()
