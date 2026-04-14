from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import Page, sync_playwright


BASE_URL = "http://localhost:8080/"
ROOT = Path(__file__).resolve().parents[1]
IMAGES_DIR = ROOT / "docs" / "images"

CORE_PARAMS = {
    "system_id": "archive-core",
    "enclosure_id": "500304801f715f3f+500304801f5a003f",
}
SCALE_PARAMS = {
    "system_id": "offsite-scale",
    "enclosure_id": "5003048001c1043f",
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


def open_and_select_slot(page: Page, params: dict[str, str], slot: int) -> None:
    page.goto(f"{BASE_URL}?{urlencode(params)}", wait_until="networkidle")
    tile = page.locator(f'#slot-grid .slot-tile[data-slot="{slot}"]')
    tile.wait_for(state="visible", timeout=120_000)
    tile.click()
    page.locator("#detail-content").wait_for(state="visible", timeout=120_000)


def capture_core(page: Page) -> None:
    open_and_select_slot(page, CORE_PARAMS, 21)
    for label in ("Read Cache", "Transport", "Link Rate"):
        wait_for_kv_value(page, label)
    capture_app_shell(page, "core-overview-v0.3.1.png")


def capture_scale(page: Page) -> None:
    open_and_select_slot(page, SCALE_PARAMS, 0)
    for label in ("Temp", "Read Cache", "Transport", "Link Rate"):
        wait_for_kv_value(page, label)
    capture_app_shell(page, "scale-overview-v0.3.1.png")


def main() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1900, "height": 2600}, device_scale_factor=1)
        capture_core(page)
        capture_scale(page)
        browser.close()


if __name__ == "__main__":
    main()
