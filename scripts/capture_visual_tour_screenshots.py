from __future__ import annotations

import os
from pathlib import Path
from shutil import copy2
import sys
from urllib.parse import urlencode

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import __version__


APP_URL = os.environ.get("SCREENSHOT_APP_URL", "http://localhost:8080/")
DOCS_IMAGES_DIR = ROOT / "docs" / "images" / "screenshots"
WIKI_IMAGES_DIR = ROOT / "wiki" / "images"
SCREENSHOT_TAG = os.environ.get("SCREENSHOT_TAG", f"v{__version__}")

ARCHIVE_CORE_SYSTEM_ID = "archive-core"
ARCHIVE_CORE_60_BAY = "500304801f715f3f+500304801f5a003f"
BOOT_DOMS_VIEW_ID = "boot-doms"


def write_locator_screenshot(locator, filename: str) -> Path:
    DOCS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    WIKI_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    docs_target = DOCS_IMAGES_DIR / filename
    wiki_target = WIKI_IMAGES_DIR / filename
    locator.screenshot(path=str(docs_target))
    copy2(docs_target, wiki_target)
    return docs_target


def hide_debug_chrome(page: Page) -> None:
    page.evaluate(
        """() => {
            const uiPerfPanel = document.getElementById('ui-perf-panel');
            if (uiPerfPanel) {
                uiPerfPanel.classList.add('hidden');
            }
        }"""
    )


def wait_for_runtime_ready(page: Page) -> None:
    page.locator("#slot-grid").wait_for(state="visible", timeout=120_000)
    page.wait_for_function(
        """() => {
            const statusNode = document.getElementById('status-text');
            if (!statusNode) return false;
            const text = (statusNode.textContent || '').trim();
            return /Ready\\.|Inventory updated\\./.test(text);
        }""",
        timeout=120_000,
    )


def wait_for_storage_view_options(page: Page) -> None:
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('#enclosure-select option'))
            .some((option) => (option.value || '').startsWith('view:'))""",
        timeout=120_000,
    )


def wait_for_detail_value(page: Page, label: str) -> None:
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


def click_slot(page: Page, slot: int) -> None:
    tile = page.locator(f'#slot-grid .slot-tile[data-slot="{slot}"]')
    tile.wait_for(state="visible", timeout=120_000)
    tile.click()
    page.locator("#detail-content").wait_for(state="visible", timeout=120_000)


def select_storage_view(page: Page, storage_view_id: str) -> None:
    wait_for_storage_view_options(page)
    page.locator("#enclosure-select").select_option(f"view:{storage_view_id}")
    page.wait_for_function(
        """([value]) => {
            const select = document.getElementById('enclosure-select');
            return select && select.value === value;
        }""",
        arg=[f"view:{storage_view_id}"],
        timeout=120_000,
    )
    wait_for_runtime_ready(page)


def open_archive_core(page: Page, enclosure_id: str | None = None) -> None:
    params = {"system_id": ARCHIVE_CORE_SYSTEM_ID}
    if enclosure_id:
        params["enclosure_id"] = enclosure_id
    page.goto(f"{APP_URL}?{urlencode(params)}", wait_until="domcontentloaded", timeout=120_000)
    hide_debug_chrome(page)
    wait_for_runtime_ready(page)


def capture_archive_core_60_bay(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 2300})
    open_archive_core(page, ARCHIVE_CORE_60_BAY)
    click_slot(page, 21)
    for label in ("Read Cache", "Transport", "Link Rate"):
        wait_for_detail_value(page, label)
    page.wait_for_timeout(700)
    write_locator_screenshot(page.locator(".app-shell"), f"archive-core-60-bay-{SCREENSHOT_TAG}.png")


def capture_runtime_selector_groups(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 1050})
    open_archive_core(page)
    wait_for_storage_view_options(page)
    page.evaluate(
        """() => {
            const select = document.getElementById('enclosure-select');
            if (!select) return;
            const topbar = select.closest('.topbar');
            const card = select.closest('.meta-card');
            if (topbar) {
                topbar.style.gridTemplateColumns =
                    'minmax(330px, 1.1fr) minmax(270px, 0.7fr) minmax(620px, 1.4fr) minmax(280px, 0.7fr) minmax(280px, 0.7fr)';
                topbar.style.alignItems = 'stretch';
                topbar.style.overflow = 'visible';
            }
            if (card) {
                card.id = 'runtime-selector-screenshot-card';
                card.style.minWidth = '620px';
                card.style.overflow = 'visible';
            }
            select.size = Math.max(6, Math.min(select.querySelectorAll('option').length, 8));
            select.style.minWidth = '560px';
            select.style.width = '100%';
            select.style.height = 'auto';
            select.style.minHeight = '252px';
            select.style.maxHeight = 'none';
            select.style.overflow = 'visible';
            select.style.padding = '10px 12px';
            select.style.background = 'rgba(5, 11, 21, 0.98)';
            select.style.borderRadius = '10px';
            select.style.fontSize = '14px';
            select.style.lineHeight = '1.6';
            select.style.boxShadow = '0 18px 42px rgba(0, 0, 0, 0.28)';
        }"""
    )
    page.wait_for_timeout(700)
    write_locator_screenshot(
        page.locator("#runtime-selector-screenshot-card"),
        f"runtime-selector-groups-{SCREENSHOT_TAG}.png",
    )


def capture_storage_view_history(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 2300})
    open_archive_core(page)
    select_storage_view(page, BOOT_DOMS_VIEW_ID)
    click_slot(page, 0)
    wait_for_detail_value(page, "Power On")
    page.locator("#history-toggle-button").click()
    page.locator("#detail-history-panel").wait_for(state="visible", timeout=120_000)
    page.locator("#detail-history-content").wait_for(state="visible", timeout=120_000)
    page.locator("#history-timeframe-select").select_option("72")
    page.wait_for_timeout(1400)
    write_locator_screenshot(page.locator(".app-shell"), f"storage-view-history-{SCREENSHOT_TAG}.png")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(device_scale_factor=1)
        capture_archive_core_60_bay(page)
        capture_runtime_selector_groups(page)
        capture_storage_view_history(page)
        browser.close()


if __name__ == "__main__":
    main()
