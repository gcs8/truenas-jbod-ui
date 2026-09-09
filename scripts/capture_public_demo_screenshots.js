#!/usr/bin/env node
"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium } = require("@playwright/test");

const repoRoot = path.resolve(__dirname, "..");
const artifactPath = path.join(repoRoot, "public-demo", "index.html");
const docsRoot = path.join(repoRoot, "docs", "images", "screenshots");
const wikiRoot = path.join(repoRoot, "wiki", "images");
const plans = [
  { name: "public-demo-overview.png", viewport: { width: 1920, height: 1080 }, state: "overview" },
  { name: "public-demo-history.png", viewport: { width: 1920, height: 1080 }, state: "history" },
];

function sha256(payload) {
  return crypto.createHash("sha256").update(payload).digest("hex");
}

function sourceRevision(artifact) {
  const prefix = "<!-- public-demo-source-parity ";
  const suffix = " -->\n";
  if (!artifact.startsWith(prefix)) {
    throw new Error("public demo source parity manifest is missing");
  }
  const end = artifact.indexOf(suffix, prefix.length);
  if (end < 0) {
    throw new Error("public demo source parity manifest is malformed");
  }
  const manifest = JSON.parse(artifact.slice(prefix.length, end));
  if (!/^[0-9a-f]{40}$/.test(manifest.source_revision || "")) {
    throw new Error("public demo source revision is invalid");
  }
  return manifest.source_revision;
}

function pngDimensions(payload) {
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (!payload.subarray(0, 8).equals(signature) || payload.subarray(12, 16).toString("ascii") !== "IHDR") {
    throw new Error("captured output is not a PNG");
  }
  return [payload.readUInt32BE(16), payload.readUInt32BE(20)];
}

async function settle(page) {
  await page.waitForLoadState("load");
  await page.locator("#slot-grid .slot-tile").first().waitFor({ state: "visible" });
  await page.evaluate(async () => {
    if (document.fonts && document.fonts.ready) {
      await document.fonts.ready;
    }
  });
  await page.addStyleTag({
    content: "*, *::before, *::after { animation: none !important; transition: none !important; caret-color: transparent !important; }",
  });
}

async function capture(browser, plan, destination) {
  const context = await browser.newContext({
    viewport: plan.viewport,
    reducedMotion: "reduce",
    colorScheme: "dark",
    timezoneId: "UTC",
  });
  const page = await context.newPage();
  const unexpected = [];
  page.on("request", (request) => {
    if (!/^(?:file|data|blob):/.test(request.url())) {
      unexpected.push(request.url());
    }
  });
  try {
    await page.goto(pathToFileURL(artifactPath).href, { waitUntil: "load" });
    await settle(page);
    if (plan.state === "overview") {
      await page.locator('#slot-grid .slot-tile[data-slot="57"]').click();
      await page.locator("#sas-fabric-toggle-button").click();
      await page.locator("#sas-fabric-panel").waitFor({ state: "visible" });
    } else if (plan.state === "history") {
      await page.locator('#slot-grid .slot-tile[data-slot="57"]').click();
      await page.locator("#history-toggle-button").click();
      await page.locator("#history-metric-grid").waitFor({ state: "visible" });
    }
    if (unexpected.length) {
      throw new Error(`capture attempted ${unexpected.length} network request(s)`);
    }
    await page.mouse.move(0, 0);
    await page.screenshot({ path: destination, fullPage: true, animations: "disabled" });
  } finally {
    await context.close();
  }
}

async function main() {
  if (!fs.existsSync(artifactPath)) {
    throw new Error("public-demo/index.html is missing");
  }
  const artifact = fs.readFileSync(artifactPath);
  const revision = sourceRevision(artifact.toString("utf8"));
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "truenas-jbod-ui-public-screenshots-"));
  const browserOptions = { headless: true };
  if (process.env.PLAYWRIGHT_BROWSER_CHANNEL) {
    browserOptions.channel = process.env.PLAYWRIGHT_BROWSER_CHANNEL;
  }
  const browser = await chromium.launch(browserOptions);
  try {
    for (const plan of plans) {
      await capture(browser, plan, path.join(tempRoot, plan.name));
    }
  } finally {
    await browser.close();
  }

  fs.mkdirSync(docsRoot, { recursive: true });
  fs.mkdirSync(wikiRoot, { recursive: true });
  const imageRecords = {};
  for (const plan of plans) {
    const payload = fs.readFileSync(path.join(tempRoot, plan.name));
    imageRecords[plan.name] = {
      bytes: payload.length,
      dimensions: pngDimensions(payload),
      pixel_review: "PENDING",
      sha256: sha256(payload),
    };
    fs.writeFileSync(path.join(docsRoot, plan.name), payload);
    fs.writeFileSync(path.join(wikiRoot, plan.name), payload);
  }
  const manifest = {
    images: imageRecords,
    provenance: "synthetic-public-demo",
    schema_version: 1,
    source_artifact_sha256: sha256(artifact),
    source_revision: revision,
  };
  fs.writeFileSync(
    path.join(docsRoot, "manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8"
  );
  fs.rmSync(tempRoot, { recursive: true, force: true });
  console.log(`Captured ${plans.length} synthetic public-demo screenshots; pixel review remains PENDING.`);
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
