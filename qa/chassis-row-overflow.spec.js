"use strict";

// #1065: a single-row chassis face that is wider than its bezel scrolls, and
// every bay is reachable from scrollLeft 0 to the maximum. A centred bay group
// that overflows its scroll container spills past the start edge, where
// scrolling cannot reach it. Complete production assets and templates; only
// HTTP and the snapshot are synthetic.
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");

// Two members of the same class: the reported 24-bay 2.5" front (top latch,
// dense layout) and a 16-bay 3.5" single row (bottom latch, standard layout),
// which overflows even the widest desktop bezel. `fitsAt` is a viewport where
// the row fits and must stay centred exactly as before.
const FACES = {
  "24-bay 2.5-inch single-row front": {
    id: "synthetic-single-row-24", face_style: "front-drive", latch_edge: "top", bay_size: "2.5", bays: 24, fitsAt: 2560,
  },
  "16-bay 3.5-inch single-row face": {
    id: "synthetic-single-row-16", face_style: "generic", latch_edge: "bottom", bay_size: "3.5", bays: 16, fitsAt: null,
  },
};

function buildFixture(face) {
  return JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json
import sys

import scripts.build_current_source_browser_fixture as builder
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload

face = json.loads(sys.argv[1])
bays = face["bays"]
data = builder.build_synthetic_live_snapshot().model_dump(mode="json")
template = data["slots"][0]
data["slots"] = [
    {
        **template,
        "slot": index,
        "slot_label": f"{index:02d}",
        "column_index": index,
        "device_name": f"sd{index}",
        "serial": f"SANITIZED-ROW-{index:02d}",
        "gptid": f"synthetic-gptid-{index}",
        "notes": None,
    }
    for index in range(bays)
]
layout = [list(range(bays))]
data["layout_rows"] = layout
data["layout_slot_count"] = bays
data["layout_columns"] = bays
data["selected_profile"] = {
    **data["selected_profile"],
    "id": face["id"],
    "label": "Synthetic single row",
    "panel_title": "Synthetic single row",
    "face_style": face["face_style"],
    "latch_edge": face["latch_edge"],
    "bay_size": face["bay_size"],
    "rows": 1,
    "columns": bays,
    "slot_count": bays,
    "slot_layout": layout,
}
data["enclosures"][0].update(profile_id=face["id"], rows=1, columns=bays, slot_count=bays, slot_layout=layout)
data["summary"].update(disk_count=bays, mapped_slot_count=bays, manual_mapping_count=0)
snapshot = InventorySnapshot.model_validate(data)
builder.build_synthetic_live_snapshot = lambda: snapshot
runtime = StorageViewRuntimePayload(system_id="synthetic-system", views=[])
print(json.dumps(dict(html=builder.build_live_fixture_html(runtime), snapshot=snapshot.model_dump(mode="json"))))
`, JSON.stringify(face)], { cwd: root, encoding: "utf8", maxBuffer: 16 * 1024 * 1024 }));
}

const fixtures = {};
test.beforeAll(() => {
  for (const [name, face] of Object.entries(FACES)) fixtures[name] = buildFixture(face);
});

test.use({ serviceWorkers: "block" });

async function openChassis(page, name) {
  const fixture = fixtures[name];
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== "https://synthetic.invalid") throw new Error(`Unexpected external request ${url.origin}`);
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: fixture.html });
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(root, "app", url.pathname);
      return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
    }
    let data = { ok: true, available: false };
    if (url.pathname === "/api/inventory") data = fixture.snapshot;
    if (url.pathname === "/api/storage-views") data = { system_id: "synthetic-system", views: [] };
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto("https://synthetic.invalid/");
  await page.locator("#auto-refresh-toggle").uncheck();
  await expect(page.locator("#slot-grid [data-slot]")).toHaveCount(FACES[name].bays);
  await expect(page.locator("#chassis-shell")).toHaveAttribute("data-layout-rows", "1");
  await expect(page.locator(".detail-panel")).toBeVisible();
  return errors;
}

async function shellBox(page) {
  return page.locator("#chassis-shell").evaluate((shell) => {
    const rect = shell.getBoundingClientRect();
    const left = rect.left + shell.clientLeft;
    return {
      overflowX: getComputedStyle(shell).overflowX,
      clientWidth: shell.clientWidth,
      scrollWidth: shell.scrollWidth,
      borderLeft: rect.left,
      borderRight: rect.right,
      left,
      right: left + shell.clientWidth,
    };
  });
}

async function scrollShell(page, to) {
  await page.locator("#chassis-shell").evaluate((shell, target) => {
    shell.scrollLeft = target === "max" ? shell.scrollWidth : target;
  }, to);
}

function bay(page, index) {
  return page.locator(`#slot-grid [data-slot="${index}"]`);
}

async function bayRect(page, index) {
  return bay(page, index).evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { left: rect.left, right: rect.right };
  });
}

async function hitsBay(page, index) {
  return bay(page, index).evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return Boolean(hit && hit.closest("[data-slot]") === element);
  });
}

async function expectEveryBayReachable(page, bays) {
  const box = await shellBox(page);
  expect(box.overflowX).toBe("auto");

  await scrollShell(page, 0);
  const first = await bayRect(page, 0);
  expect(first.left, "bay 00 at scrollLeft 0").toBeGreaterThanOrEqual(box.left - 0.5);
  expect(first.right, "bay 00 at scrollLeft 0").toBeLessThanOrEqual(box.right + 0.5);

  await scrollShell(page, "max");
  const last = await bayRect(page, bays - 1);
  expect(last.left, "last bay at max scroll").toBeGreaterThanOrEqual(box.left - 0.5);
  expect(last.right, "last bay at max scroll").toBeLessThanOrEqual(box.right + 0.5);

  for (let index = 0; index < bays; index += 1) {
    await bay(page, index).evaluate((element) => element.scrollIntoView({ block: "nearest", inline: "nearest" }));
    const shell = await shellBox(page);
    const rect = await bayRect(page, index);
    expect(rect.left, `bay ${index} left edge`).toBeGreaterThanOrEqual(shell.left - 0.5);
    expect(rect.right, `bay ${index} right edge`).toBeLessThanOrEqual(shell.right + 0.5);
    expect(await hitsBay(page, index), `bay ${index} hit-test`).toBe(true);
  }
}

for (const [name, face] of Object.entries(FACES)) {
  for (const viewport of [{ width: 1600, height: 1000 }, { width: 1920, height: 1080 }]) {
    test(`every bay of a ${name} is reachable at ${viewport.width} px`, async ({ page }) => {
      await page.setViewportSize(viewport);
      const errors = await openChassis(page, name);
      // Optional local evidence: the bezel at scrollLeft 0, before any assertion.
      if (process.env.CHASSIS_ROW_SCREENSHOT_DIR) {
        await scrollShell(page, 0);
        await page.locator(".enclosure-face").screenshot({
          path: path.join(process.env.CHASSIS_ROW_SCREENSHOT_DIR, `${face.id}-${viewport.width}.png`),
        });
      }
      await expectEveryBayReachable(page, face.bays);
      expect(errors).toEqual([]);
    });
  }

  if (face.fitsAt) {
    test(`a ${name} that fits stays centred in its bezel`, async ({ page }) => {
      await page.setViewportSize({ width: face.fitsAt, height: 1200 });
      const errors = await openChassis(page, name);
      const box = await shellBox(page);
      expect(box.scrollWidth).toBeLessThanOrEqual(box.clientWidth);
      const first = await bayRect(page, 0);
      const last = await bayRect(page, face.bays - 1);
      // Bays sit symmetrically between the bezel edges.
      expect(Math.abs((first.left - box.borderLeft) - (box.borderRight - last.right))).toBeLessThanOrEqual(2);
      expect(errors).toEqual([]);
    });
  }
}
