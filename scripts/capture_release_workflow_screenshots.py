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

APP_URL = "http://localhost:8080/"
ADMIN_URL = "http://localhost:8082/"
DOCS_IMAGES_DIR = ROOT / "docs" / "images" / "screenshots"
WIKI_IMAGES_DIR = ROOT / "wiki" / "images"
SCREENSHOT_TAG = os.environ.get("SCREENSHOT_TAG", f"v{__version__}")

ARCHIVE_CORE_SYSTEM_ID = "archive-core"
ARCHIVE_CORE_FRONT_24 = "50030480090c4f7f"
BOOT_DOMS_VIEW_ID = "boot-doms"
QUANTASTOR_SYSTEM_ID = "qsosn-ha"
QUANTASTOR_SATADOMS_RIGHT_VIEW_ID = "boot-satadoms-right"
BUILDER_SOURCE_PROFILE_ID = "generic-front-24-1x24"


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


def wait_for_admin_ready(page: Page) -> None:
    page.locator("#existing-system-select").wait_for(state="visible", timeout=120_000)
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('#existing-system-select option'))
            .some((option) => (option.value || '').trim().length > 0)""",
        timeout=120_000,
    )


def wait_for_builder_ready(page: Page) -> None:
    page.locator('[data-admin-view-panel="builder"]').wait_for(state="visible", timeout=120_000)
    page.wait_for_function(
        """() => document.querySelectorAll('#profile-catalog .profile-card').length > 0""",
        timeout=120_000,
    )


def write_page_screenshot(page: Page, filename: str) -> Path:
    DOCS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    WIKI_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    docs_target = DOCS_IMAGES_DIR / filename
    wiki_target = WIKI_IMAGES_DIR / filename
    page.screenshot(path=str(docs_target))
    copy2(docs_target, wiki_target)
    return docs_target


def write_locator_screenshot(locator, filename: str) -> Path:
    DOCS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    WIKI_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    docs_target = DOCS_IMAGES_DIR / filename
    wiki_target = WIKI_IMAGES_DIR / filename
    locator.screenshot(path=str(docs_target))
    copy2(docs_target, wiki_target)
    return docs_target


def show_select_as_listbox(page: Page, selector: str, max_rows: int = 8) -> None:
    page.evaluate(
        """([selector, maxRows]) => {
            const select = document.querySelector(selector);
            if (!select) return;
            const optionCount = select.querySelectorAll('option').length || 1;
            const rows = Math.max(2, Math.min(optionCount, maxRows || optionCount));
            select.size = rows;
            select.style.height = 'auto';
            select.style.minHeight = `${rows * 30 + 26}px`;
            select.style.maxHeight = 'none';
            select.style.overflow = 'visible';
            select.style.padding = '8px 10px';
            select.style.background = 'rgba(5, 11, 21, 0.96)';
            select.style.borderRadius = '14px';
            select.style.boxShadow = '0 20px 40px rgba(0, 0, 0, 0.28)';
            select.style.position = 'relative';
            select.style.zIndex = '4';
            const parent = select.closest('.meta-card, .field, .inline-grid, .panel');
            if (parent) {
                parent.style.overflow = 'visible';
            }
            const section = select.closest('.topbar, .panel');
            if (section) {
                section.style.overflow = 'visible';
            }
        }""",
        [selector, max_rows],
    )


def capture_live_vs_storage_views(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 1180})
    page.goto(
        f"{APP_URL}?{urlencode({'system_id': ARCHIVE_CORE_SYSTEM_ID})}",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    hide_debug_chrome(page)
    wait_for_runtime_ready(page)
    wait_for_storage_view_options(page)
    show_select_as_listbox(page, "#enclosure-select", max_rows=8)
    page.wait_for_timeout(700)
    write_locator_screenshot(page.locator(".topbar"), f"live-vs-storage-views-{SCREENSHOT_TAG}.png")


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


def capture_archive_core_front_24(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 1750})
    page.goto(
        f"{APP_URL}?{urlencode({'system_id': ARCHIVE_CORE_SYSTEM_ID, 'enclosure_id': ARCHIVE_CORE_FRONT_24})}",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    hide_debug_chrome(page)
    wait_for_runtime_ready(page)
    click_slot(page, 6)
    for label in ("Temp", "Power On", "Transport"):
        wait_for_detail_value(page, label)
    page.wait_for_timeout(700)
    write_page_screenshot(page, f"archive-core-front-24-{SCREENSHOT_TAG}.png")


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


def capture_storage_view_history(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 2250})
    page.goto(
        f"{APP_URL}?{urlencode({'system_id': ARCHIVE_CORE_SYSTEM_ID})}",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    hide_debug_chrome(page)
    wait_for_runtime_ready(page)
    select_storage_view(page, BOOT_DOMS_VIEW_ID)
    click_slot(page, 0)
    wait_for_detail_value(page, "Power On")
    history_button = page.locator("#history-toggle-button")
    history_button.wait_for(state="visible", timeout=120_000)
    history_button.click()
    page.locator("#detail-history-panel").wait_for(state="visible", timeout=120_000)
    page.locator("#detail-history-content").wait_for(state="visible", timeout=120_000)
    page.locator("#history-timeframe-select").select_option("72")
    page.wait_for_timeout(1800)
    write_page_screenshot(page, f"storage-view-history-{SCREENSHOT_TAG}.png")


def capture_quantastor_satadoms(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 1700})
    page.goto(
        f"{APP_URL}?{urlencode({'system_id': QUANTASTOR_SYSTEM_ID})}",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    hide_debug_chrome(page)
    wait_for_runtime_ready(page)
    select_storage_view(page, QUANTASTOR_SATADOMS_RIGHT_VIEW_ID)
    click_slot(page, 0)
    for label in ("Power On", "Power Cycles", "Bytes Read"):
        wait_for_detail_value(page, label)
    page.wait_for_timeout(900)
    write_page_screenshot(page, f"quantastor-satadoms-right-{SCREENSHOT_TAG}.png")


def capture_admin_setup(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 1500})
    page.goto(ADMIN_URL, wait_until="domcontentloaded", timeout=120_000)
    wait_for_admin_ready(page)
    page.locator("#existing-system-select").select_option(ARCHIVE_CORE_SYSTEM_ID)
    page.locator("#existing-system-load-button").click()
    page.locator("#setup-system-id").wait_for(state="visible", timeout=120_000)
    page.wait_for_function(
        """([systemId]) => {
            const field = document.getElementById('setup-system-id');
            return field && field.value === systemId;
        }""",
        arg=[ARCHIVE_CORE_SYSTEM_ID],
        timeout=120_000,
    )
    page.wait_for_function(
        """() => document.querySelectorAll('#profile-catalog .profile-card').length > 0""",
        timeout=120_000,
    )
    page.wait_for_function(
        """() => document.querySelectorAll('#setup-storage-view-template option').length > 0""",
        timeout=120_000,
    )
    page.locator("#setup-storage-view-template").scroll_into_view_if_needed()
    page.evaluate("window.scrollBy(0, -180)")
    show_select_as_listbox(page, "#setup-storage-view-template", max_rows=10)
    page.wait_for_timeout(700)
    write_page_screenshot(page, f"admin-setup-{SCREENSHOT_TAG}.png")


def capture_admin_maintenance(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 1450})
    page.goto(ADMIN_URL, wait_until="domcontentloaded", timeout=120_000)
    wait_for_admin_ready(page)
    backup_panel = page.locator(".backup-panel")
    backup_panel.wait_for(state="visible", timeout=120_000)
    backup_panel.scroll_into_view_if_needed()
    page.wait_for_timeout(700)
    write_locator_screenshot(backup_panel, f"admin-maintenance-{SCREENSHOT_TAG}.png")


def capture_builder_workspace(page: Page) -> None:
    page.set_viewport_size({"width": 1900, "height": 1700})
    page.goto(f"{ADMIN_URL}?view=builder", wait_until="domcontentloaded", timeout=120_000)
    wait_for_builder_ready(page)
    page.locator(f'#profile-catalog .profile-card[data-profile-id="{BUILDER_SOURCE_PROFILE_ID}"]').click()
    page.locator("#profile-builder-load-button").click()
    page.locator("#profile-builder-rows").fill("3")
    page.locator("#profile-builder-columns").fill("2")
    page.locator("#profile-builder-slot-count").fill("6")
    page.locator("#profile-builder-ordering").select_option("column-major-bottom")
    page.wait_for_function(
        """() => {
            const summary = document.getElementById('profile-builder-preview-summary');
            const cells = Array.from(document.querySelectorAll('#profile-builder-preview-grid .profile-preview-cell'))
              .map((node) => (node.textContent || '').trim())
              .filter(Boolean);
            return summary && /bottom-up by columns/i.test(summary.textContent || '') &&
              cells.length === 6 &&
              cells.join(',') === '02,05,01,04,00,03';
        }""",
        timeout=120_000,
    )
    page.wait_for_timeout(700)
    write_locator_screenshot(page.locator('[data-admin-view-panel="builder"]'), f"builder-workspace-{SCREENSHOT_TAG}.png")


def main() -> None:
    DOCS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    WIKI_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(device_scale_factor=1)
        capture_live_vs_storage_views(page)
        capture_storage_view_history(page)
        capture_archive_core_front_24(page)
        capture_quantastor_satadoms(page)
        capture_admin_setup(page)
        capture_admin_maintenance(page)
        capture_builder_workspace(page)
        browser.close()


if __name__ == "__main__":
    main()
