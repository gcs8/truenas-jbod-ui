from __future__ import annotations

import os
from pathlib import Path
import sys
from urllib.parse import urlencode

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import __version__


BASE_URL = "http://localhost:8080/"
IMAGES_DIR = ROOT / "docs" / "images" / "screenshots"
SCREENSHOT_TAG = os.environ.get("SCREENSHOT_TAG", f"v{__version__}")

CORE_PARAMS = {
    "system_id": "archive-core",
    "enclosure_id": "500304801f715f3f+500304801f5a003f",
}
SCALE_PARAMS = {
    "system_id": "offsite-scale",
    "enclosure_id": "5003048001c1043f",
}
GPU_PARAMS = {
    "system_id": "gpu-server",
}
UNVR_PARAMS = {
    "system_id": "unvr",
}
UNVR_PRO_PARAMS = {
    "system_id": "unvr-pro",
}
QUANTASTOR_PARAMS = {
    "system_id": "qsosn-ha",
}


def kv_value(page: Page, label: str) -> str:
    locator = page.locator(
        ".kv-row",
        has=page.locator(".kv-label", has_text=label),
    ).locator(".kv-value")
    return locator.first.inner_text().strip()


def wait_for_kv_value(page: Page, label: str) -> None:
    page.wait_for_function(
        """([label]) => {
            const rows = Array.from(document.querySelectorAll('.kv-row'));
            const row = rows.find((candidate) => {
                const labelNode = candidate.querySelector('.kv-label');
                return labelNode && labelNode.textContent.trim() === label;
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


def capture_app_shell(page: Page, filename: str) -> None:
    target = IMAGES_DIR / filename
    page.locator(".app-shell").screenshot(path=str(target))


def hide_debug_chrome(page: Page) -> None:
    page.evaluate(
        """() => {
            const uiPerfPanel = document.getElementById('ui-perf-panel');
            if (uiPerfPanel) {
                uiPerfPanel.classList.add('hidden');
            }
        }"""
    )


def open_and_select_slot(page: Page, params: dict[str, str], slot: int) -> None:
    page.goto(f"{BASE_URL}?{urlencode(params)}", wait_until="load")
    hide_debug_chrome(page)
    tile = page.locator(f'#slot-grid .slot-tile[data-slot="{slot}"]')
    tile.wait_for(state="visible", timeout=120_000)
    tile.click()
    page.locator("#detail-content").wait_for(state="visible", timeout=120_000)


def capture_core(page: Page) -> None:
    open_and_select_slot(page, CORE_PARAMS, 21)
    for label in ("Read Cache", "Transport", "Link Rate"):
        wait_for_kv_value(page, label)
    capture_app_shell(page, f"core-overview-{SCREENSHOT_TAG}.png")


def capture_scale(page: Page) -> None:
    open_and_select_slot(page, SCALE_PARAMS, 0)
    for label in ("Temp", "Read Cache", "Transport", "Link Rate"):
        wait_for_kv_value(page, label)
    capture_app_shell(page, f"scale-overview-{SCREENSHOT_TAG}.png")


def capture_gpu_server(page: Page) -> None:
    open_and_select_slot(page, GPU_PARAMS, 0)
    for label in ("Array", "Transport", "Endurance"):
        wait_for_kv_value(page, label)
    capture_app_shell(page, f"gpu-server-overview-{SCREENSHOT_TAG}.png")


def capture_unvr(page: Page) -> None:
    open_and_select_slot(page, UNVR_PARAMS, 0)
    for label in ("Mount", "Array", "Transport"):
        wait_for_kv_value(page, label)
    capture_app_shell(page, f"unvr-overview-{SCREENSHOT_TAG}.png")


def capture_unvr_pro(page: Page) -> None:
    open_and_select_slot(page, UNVR_PRO_PARAMS, 0)
    for label in ("Mount", "Array", "Transport"):
        wait_for_kv_value(page, label)
    capture_app_shell(page, f"unvr-pro-overview-{SCREENSHOT_TAG}.png")


def capture_quantastor(page: Page) -> None:
    open_and_select_slot(page, QUANTASTOR_PARAMS, 0)
    for label in ("Presented By", "Pool Active On", "Transport"):
        wait_for_kv_value(page, label)
    capture_app_shell(page, f"quantastor-overview-{SCREENSHOT_TAG}.png")


def main() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1900, "height": 2600}, device_scale_factor=1)
        capture_core(page)
        capture_scale(page)
        capture_gpu_server(page)
        capture_unvr(page)
        capture_unvr_pro(page)
        capture_quantastor(page)
        browser.close()


if __name__ == "__main__":
    main()
