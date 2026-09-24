#!/usr/bin/env node
"use strict";

// Reports the fonts Chromium actually rasterised the public demo with.
//
// fc-match answers a fontconfig question, not a Chromium one: it says which
// family fontconfig would pick, not which family the renderer used for the
// glyphs in the screenshot. This script asks the renderer, through the DevTools
// protocol, so the capture workflow can assert on the families that decide the
// pixels. It only reads public-demo/index.html over file:// and writes the JSON
// report it is given; it never touches docs/images or wiki/images.

const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium } = require("@playwright/test");

const repoRoot = path.resolve(__dirname, "..");
const artifactPath = path.join(repoRoot, "public-demo", "index.html");
const viewport = { width: 1920, height: 1080 };
const probes = [
  {
    role: "sans",
    selector: "h1",
    reason: "body copy inherits var(--sans): Segoe UI, Tahoma, Geneva, Verdana, sans-serif",
  },
  {
    role: "mono",
    selector: "#app-version-value",
    reason: "meta-value uses var(--mono): Cascadia Mono, SFMono-Regular, Consolas, monospace",
  },
];

function usage() {
  return "usage: node scripts/report_public_demo_platform_fonts.js <report.json>";
}

async function settle(page) {
  await page.waitForLoadState("load");
  await page.locator("#slot-grid .slot-tile").first().waitFor({ state: "visible" });
  await page.evaluate(async () => {
    if (document.fonts && document.fonts.ready) {
      await document.fonts.ready;
    }
  });
}

async function platformFontsFor(session, documentNodeId, selector) {
  const found = await session.send("DOM.querySelector", {
    nodeId: documentNodeId,
    selector,
  });
  if (!found.nodeId) {
    throw new Error(`selector ${selector} matched no node in the demo artifact`);
  }
  const { fonts } = await session.send("CSS.getPlatformFontsForNode", {
    nodeId: found.nodeId,
  });
  const families = fonts.map((font) => ({
    familyName: font.familyName,
    glyphCount: font.glyphCount,
    isCustomFont: Boolean(font.isCustomFont),
  }));
  families.sort(
    (left, right) =>
      right.glyphCount - left.glyphCount || left.familyName.localeCompare(right.familyName)
  );
  if (!families.length) {
    throw new Error(`Chromium reported no platform font for ${selector}`);
  }
  return families;
}

async function main() {
  const destination = process.argv[2];
  if (!destination) {
    throw new Error(usage());
  }
  if (!fs.existsSync(artifactPath)) {
    throw new Error("public-demo/index.html is missing");
  }

  const browser = await chromium.launch({ headless: true });
  const report = {
    artifact: "public-demo/index.html",
    probes: {},
    schema_version: 1,
    viewport,
  };
  try {
    const context = await browser.newContext({
      viewport,
      reducedMotion: "reduce",
      colorScheme: "dark",
      timezoneId: "UTC",
    });
    const page = await context.newPage();
    try {
      await page.goto(pathToFileURL(artifactPath).href, { waitUntil: "load" });
      await settle(page);
      const session = await page.context().newCDPSession(page);
      await session.send("DOM.enable");
      await session.send("CSS.enable");
      const { root } = await session.send("DOM.getDocument");
      for (const probe of probes) {
        const families = await platformFontsFor(session, root.nodeId, probe.selector);
        report.probes[probe.role] = {
          dominant_family: families[0].familyName,
          families,
          reason: probe.reason,
          selector: probe.selector,
        };
      }
    } finally {
      await context.close();
    }
  } finally {
    await browser.close();
  }

  for (const probe of probes) {
    const result = report.probes[probe.role];
    const rendered = result.families
      .map((font) => `${font.familyName} (${font.glyphCount} glyphs)`)
      .join(", ");
    console.log(`platform fonts for ${probe.role} (${result.selector}): ${rendered}`);
  }
  fs.writeFileSync(destination, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  console.log(`wrote the platform font report to ${destination}`);
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
