"use strict";

// Shared registration for the existing CI entry point (#1356).
module.exports = function registerFixtureSuite({ test, expect }) {
// Header selects on the Storage Fabric page and the main page. A selected
// System or Enclosure label that fits the row is shown whole; when two long
// labels do not fit, the two selects share the shortfall and end in an
// ellipsis; each select's title is its full selected label. The room a label
// needs is measured by the browser itself: a hidden copy of the select that
// holds only that label, at its intrinsic width (same class, font, padding
// and native arrow), plus a check of the label's own text width against the
// select's content box. Complete production templates and assets; only HTTP
// and the data are synthetic.
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const ORIGIN = "https://synthetic.invalid";
const WIDTHS = [1280, 1600, 1920];
// The smallest room the native arrow takes beside the text; the hidden-copy
// measurement is the exact check, this one only has to agree with it.
const MIN_ARROW_PX = 8;
const SYSTEMS = {
  "sys-short": "SYNTH-HOST-014",
  "sys-long": "SYNTH-HOST-ABCDEFGHIJKLMNOPQRSTUVWXYZ-ABCDEFGHIJKLMNOPQRSTUV",
};
const ENCLOSURES = {
  "enc-ten": "SYNTH-SH10",
  "enc-shelf": "SYNTH-SHELF",
  "enc-long": "SYNTH-SHELF-ABCDEFGHIJKLMNOPQRSTUVWXYZ-ABCDEFGHIJKLMNOPQRSTU",
};
const CASES = {
  short: { system: "sys-short", enclosure: "enc-shelf" },
  ten: { system: "sys-short", enclosure: "enc-ten" },
  mixed: { system: "sys-long", enclosure: "enc-shelf" },
  long: { system: "sys-long", enclosure: "enc-long" },
};
const SCREENSHOT_DIR = process.env.HEADER_SELECT_SCREENSHOT_DIR || "";
const fixture = {};

test.beforeAll(() => {
  expect(SYSTEMS["sys-short"]).toHaveLength(14);
  expect(SYSTEMS["sys-long"]).toHaveLength(60);
  expect(ENCLOSURES["enc-ten"]).toHaveLength(10);
  expect(ENCLOSURES["enc-shelf"]).toHaveLength(11);
  expect(ENCLOSURES["enc-long"]).toHaveLength(60);
  Object.assign(fixture, JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json
import sys

import scripts.build_current_source_browser_fixture as builder
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload

systems = json.loads(sys.argv[1])
enclosures = json.loads(sys.argv[2])
cases = json.loads(sys.argv[3])
original = builder.build_synthetic_live_snapshot
base = original().model_dump(mode="json")
pages = {}
for name, case in cases.items():
    data = json.loads(json.dumps(base))
    system_template = data["systems"][0]
    enclosure_template = data["enclosures"][0]
    data["systems"] = [dict(system_template, id=key, label=label) for key, label in systems.items()]
    data["enclosures"] = [dict(enclosure_template, id=key, label=label, alias=label) for key, label in enclosures.items()]
    data["selected_system_id"] = case["system"]
    data["selected_system_label"] = systems[case["system"]]
    data["selected_enclosure_id"] = case["enclosure"]
    data["selected_enclosure_label"] = enclosures[case["enclosure"]]
    for slot in data["slots"]:
        slot.update(enclosure_id=case["enclosure"], enclosure_label=enclosures[case["enclosure"]])
    snapshot = InventorySnapshot.model_validate(data)
    builder.build_synthetic_live_snapshot = lambda snapshot=snapshot: snapshot
    snapshot_json = snapshot.model_dump(mode="json")
    trace = lambda index: dict(id=f"bay:{index}", kind="bay", label=f"Disk {index + 1}", slots=[index], node_ids=[f"bay:{index}"], link_ids=[], metrics={}, evidence=[])
    node = lambda index: dict(id=f"bay:{index}", kind="bay", label=f"Disk {index + 1}", related_slots=[index], metrics={}, raw={})
    fabric = dict(available=True, platform="linux", system_id=case["system"], selected_enclosure_id=case["enclosure"],
                  nodes=[node(0)], traces=[trace(0)], links=[], controllers=[], paths=[], expanders=[], enclosures=[],
                  warnings=[], sources=dict(api=dict(enabled=True, ok=True)), raw={},
                  snapshot_cache_state="hit", source_cache_state="hit")
    fabric_html = builder.TEMPLATES.env.get_template("sas_fabric.html").render(
        request=builder.build_fixture_request(), snapshot=snapshot_json, fabric=fabric, app_version="synthetic",
        bootstrap_json=json.dumps(dict(snapshot=snapshot_json, fabric=fabric)))
    main_html = builder.build_live_fixture_html(StorageViewRuntimePayload(system_id=case["system"], views=[]))
    pages[name] = dict(snapshot=snapshot_json, fabric=fabric, fabricHtml=fabric_html, mainHtml=main_html)
builder.build_synthetic_live_snapshot = original
print(json.dumps(pages))
`, JSON.stringify(SYSTEMS), JSON.stringify(ENCLOSURES), JSON.stringify(CASES)], {
    cwd: root,
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
  })));
});

test.use({ serviceWorkers: "block" });

// Reads answer with the selection the request names, so a selection change
// round-trips like it does against the service.
function scoped(page, url) {
  const systemId = url.searchParams.get("system_id") || page.snapshot.selected_system_id;
  const enclosureId = url.searchParams.get("enclosure_id") || page.snapshot.selected_enclosure_id;
  return {
    snapshot: {
      ...page.snapshot,
      selected_system_id: systemId,
      selected_system_label: SYSTEMS[systemId] || systemId,
      selected_enclosure_id: enclosureId,
      selected_enclosure_label: ENCLOSURES[enclosureId] || enclosureId,
    },
    fabric: { ...page.fabric, system_id: systemId, selected_enclosure_id: enclosureId },
  };
}

async function routeSynthetic(page, fixturePage, body) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN) return route.abort();
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html; charset=utf-8", body });
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(root, "app", url.pathname);
      return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404, body: "" });
    }
    let data = { ok: true, available: false };
    if (url.pathname === "/api/inventory") data = scoped(fixturePage, url).snapshot;
    if (url.pathname === "/api/sas-fabric") data = scoped(fixturePage, url).fabric;
    if (url.pathname === "/api/storage-views") data = { system_id: fixturePage.snapshot.selected_system_id, views: [] };
    if (url.pathname === "/api/slots/smart-batch") data = { summaries: [] };
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  return errors;
}

const PAGES = {
  fabric: { system: "#fabric-system-select", enclosure: "#fabric-enclosure-select" },
  main: { system: "#system-select", enclosure: "#enclosure-select" },
};

async function openPage(page, kind, caseName, width) {
  await page.setViewportSize({ width, height: 1000 });
  const fixturePage = fixture[caseName];
  const errors = await routeSynthetic(page, fixturePage, kind === "fabric" ? fixturePage.fabricHtml : fixturePage.mainHtml);
  await page.goto(`${ORIGIN}/`);
  if (kind === "main") {
    const autoRefresh = page.locator("#auto-refresh-toggle");
    if (await autoRefresh.count() && await autoRefresh.isVisible()) await autoRefresh.uncheck();
  } else {
    await expect.poll(() => page.locator(".fabric-page-topbar .topbar-meta").evaluate((row) => (
      row.style.getPropertyValue("--fabric-system-card-room") && row.style.getPropertyValue("--fabric-enclosure-card-room")
    ))).toBeTruthy();
  }
  await expect(page.locator(PAGES[kind].system)).toHaveAttribute("title", SYSTEMS[CASES[caseName].system]);
  await expect(page.locator(PAGES[kind].enclosure)).toHaveAttribute("title", new RegExp(`${ENCLOSURES[CASES[caseName].enclosure]}$`));
  await page.evaluate(() => document.fonts.ready.then(() => new Promise((resolve) => requestAnimationFrame(() => resolve()))));
  return errors;
}

// Geometry of one header select and its card, measured by the browser.
async function selectGeometry(page, selector) {
  return page.locator(selector).evaluate((select) => {
    const option = select.options[select.selectedIndex];
    const text = option ? option.textContent.trim() : "";
    const style = getComputedStyle(select);
    const copy = document.createElement("select");
    copy.className = select.className;
    copy.style.cssText = "position: absolute; left: 0; top: 0; visibility: hidden; width: auto; min-width: 0; max-width: none;";
    const copyOption = document.createElement("option");
    copyOption.textContent = text;
    copy.append(copyOption);
    select.parentElement.append(copy);
    const natural = copy.getBoundingClientRect().width;
    copy.remove();
    const probe = document.createElement("span");
    probe.textContent = text;
    for (const property of ["font-family", "font-size", "font-weight", "font-style", "letter-spacing", "font-feature-settings", "font-variant-ligatures"]) {
      probe.style.setProperty(property, style.getPropertyValue(property));
    }
    Object.assign(probe.style, { position: "absolute", visibility: "hidden", whiteSpace: "pre", left: "0", top: "0" });
    document.body.append(probe);
    const textWidth = probe.getBoundingClientRect().width;
    probe.remove();
    const rect = select.getBoundingClientRect();
    const card = select.closest(".meta-card").getBoundingClientRect();
    return {
      text,
      title: select.title,
      width: rect.width,
      natural,
      textWidth,
      textRoom: select.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
      textOverflow: style.textOverflow,
      whiteSpace: style.whiteSpace,
      overflowX: style.overflowX,
      card: { left: card.left, right: card.right, top: card.top, bottom: card.bottom, width: card.width },
    };
  });
}

async function rowGeometry(page) {
  return page.locator(".topbar-meta").first().evaluate((row) => {
    const box = row.getBoundingClientRect();
    const cards = Array.from(row.children).map((card) => {
      const rect = card.getBoundingClientRect();
      return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
    });
    return {
      row: { left: box.left, right: box.right },
      cards,
      viewport: window.innerWidth,
      page: { scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth },
    };
  });
}

function expectWhole(geometry, label) {
  const detail = `${label} ${JSON.stringify(geometry)}`;
  expect(geometry.width, `${detail}: select is narrower than its label needs`).toBeGreaterThanOrEqual(geometry.natural - 0.5);
  expect(geometry.textRoom, `${detail}: text box leaves no room for the arrow`).toBeGreaterThanOrEqual(geometry.textWidth + MIN_ARROW_PX);
}

function expectEllipsis(geometry, label) {
  const detail = `${label} ${JSON.stringify(geometry)}`;
  expect(geometry.width, `${detail}: a long label is expected not to fit`).toBeLessThan(geometry.natural - 1);
  expect(geometry.textOverflow, detail).toBe("ellipsis");
  expect(geometry.whiteSpace, detail).toBe("nowrap");
  // Chromium's own select rule computes the authored `hidden` as `clip`;
  // both cut the text at the padding box, which the ellipsis needs.
  expect(["hidden", "clip"], detail).toContain(geometry.overflowX);
}

function expectCommon(geometry, label) {
  expect(geometry.title, `${label}: title is the full selected label`).toBe(geometry.text);
  expect(geometry.textOverflow, label).toBe("ellipsis");
}

function expectRowIntact(row, width) {
  expect(row.viewport, "actual viewport width").toBe(width);
  expect(row.page.scrollWidth, "no sideways page scroll").toBeLessThanOrEqual(row.page.clientWidth);
  for (const [index, card] of row.cards.entries()) {
    expect(card.left, `card ${index + 1} inside the row`).toBeGreaterThanOrEqual(row.row.left - 0.5);
    expect(card.right, `card ${index + 1} inside the row`).toBeLessThanOrEqual(row.row.right + 0.5);
    for (const other of row.cards.slice(index + 1)) {
      const overlaps = card.left < other.left ? card.right > other.left + 0.5 : other.right > card.left + 0.5;
      const sameLine = card.top < other.bottom - 0.5 && other.top < card.bottom - 0.5;
      expect(overlaps && sameLine, `card ${index + 1} overlaps another card`).toBe(false);
    }
  }
}

async function capture(page, name) {
  if (!SCREENSHOT_DIR) return;
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.locator("header.topbar").first().screenshot({ path: path.join(SCREENSHOT_DIR, `${name}.png`) });
}

for (const width of WIDTHS) {
  for (const caseName of ["short", "ten"]) {
    test(`Storage Fabric shows the selected 14-character system and ${ENCLOSURES[CASES[caseName].enclosure].length}-character enclosure whole at ${width} px`, async ({ page }) => {
      const errors = await openPage(page, "fabric", caseName, width);
      const system = await selectGeometry(page, PAGES.fabric.system);
      const enclosure = await selectGeometry(page, PAGES.fabric.enclosure);
      expectCommon(system, "System");
      expectCommon(enclosure, "Enclosure");
      expectWhole(system, "System");
      expectWhole(enclosure, "Enclosure");
      expectRowIntact(await rowGeometry(page), width);
      await capture(page, `fabric-${caseName}-${width}`);
      expect(errors).toEqual([]);
    });
  }

  test(`Storage Fabric keeps the enclosure whole beside a 60-character system at ${width} px`, async ({ page }) => {
    const errors = await openPage(page, "fabric", "mixed", width);
    const system = await selectGeometry(page, PAGES.fabric.system);
    const enclosure = await selectGeometry(page, PAGES.fabric.enclosure);
    expectCommon(system, "System");
    expectCommon(enclosure, "Enclosure");
    expectEllipsis(system, "System");
    expectWhole(enclosure, "Enclosure");
    expectRowIntact(await rowGeometry(page), width);
    await capture(page, `fabric-mixed-${width}`);
    expect(errors).toEqual([]);
  });

  test(`Storage Fabric shares the row between two 60-character labels at ${width} px`, async ({ page }) => {
    const errors = await openPage(page, "fabric", "long", width);
    const system = await selectGeometry(page, PAGES.fabric.system);
    const enclosure = await selectGeometry(page, PAGES.fabric.enclosure);
    expectCommon(system, "System");
    expectCommon(enclosure, "Enclosure");
    expectEllipsis(system, "System");
    expectEllipsis(enclosure, "Enclosure");
    expect(Math.abs(system.card.width - enclosure.card.width), "the two long labels share the shortfall").toBeLessThanOrEqual(1);
    expectRowIntact(await rowGeometry(page), width);
    await capture(page, `fabric-long-${width}`);
    expect(errors).toEqual([]);
  });

  for (const caseName of ["short", "long"]) {
    test(`main page header selects with ${caseName} labels keep whole labels or an ellipsis and full-label titles at ${width} px`, async ({ page }) => {
      const errors = await openPage(page, "main", caseName, width);
      const system = await selectGeometry(page, PAGES.main.system);
      const enclosure = await selectGeometry(page, PAGES.main.enclosure);
      expectCommon(system, "System");
      expectCommon(enclosure, "Enclosure");
      expect(enclosure.text.endsWith(ENCLOSURES[CASES[caseName].enclosure])).toBe(true);
      if (caseName === "short") {
        expectWhole(system, "System");
        expectWhole(enclosure, "Enclosure");
      } else {
        for (const [label, geometry] of [["System", system], ["Enclosure", enclosure]]) {
          if (geometry.width < geometry.natural - 1) expectEllipsis(geometry, label);
          else expectWhole(geometry, label);
        }
      }
      expectRowIntact(await rowGeometry(page), width);
      await capture(page, `main-${caseName}-${width}`);
      expect(errors).toEqual([]);
    });
  }
}

test("Storage Fabric re-measures and re-titles the enclosure select after a selection change", async ({ page }) => {
  const errors = await openPage(page, "fabric", "short", 1600);
  await page.locator(PAGES.fabric.enclosure).selectOption("enc-long");
  await expect(page.locator(PAGES.fabric.enclosure)).toHaveAttribute("title", ENCLOSURES["enc-long"]);
  await expect.poll(() => page.locator(PAGES.fabric.enclosure).evaluate((select) => select.value)).toBe("enc-long");
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));
  const system = await selectGeometry(page, PAGES.fabric.system);
  const enclosure = await selectGeometry(page, PAGES.fabric.enclosure);
  expectCommon(system, "System");
  expectCommon(enclosure, "Enclosure");
  expectWhole(system, "System");
  expectEllipsis(enclosure, "Enclosure");
  expectRowIntact(await rowGeometry(page), 1600);
  await page.locator(PAGES.fabric.enclosure).selectOption("enc-shelf");
  await expect(page.locator(PAGES.fabric.enclosure)).toHaveAttribute("title", ENCLOSURES["enc-shelf"]);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));
  expectWhole(await selectGeometry(page, PAGES.fabric.enclosure), "Enclosure");
  expect(errors).toEqual([]);
});
};
