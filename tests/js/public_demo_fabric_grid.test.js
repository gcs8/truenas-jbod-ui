"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
const PUBLIC_DEMO = fs.readFileSync(path.join(ROOT, "public-demo/index.html"), "utf8");

test("the read UI accepts a preloaded Storage Fabric payload in snapshot mode only", () => {
  assert.match(
    TEMPLATE,
    /preloadedSasFabric: \{\{ preloaded_sas_fabric_json \| default\("null"\) \| script_json_text \}\},/,
    "the bootstrap must carry the payload through the hardened script_json filter"
  );
  assert.match(
    APP_SOURCE,
    /const preloadedSasFabric = snapshotMode \? \(bootstrap\.preloadedSasFabric \|\| null\) : null;/,
    "a live page must ignore any preloaded payload and fetch its own"
  );
  assert.match(APP_SOURCE, /data: preloadedSasFabric,/, "snapshot mode seeds state.sasFabric.data");
});

test("the checked-in public demo embeds a frozen fabric payload that lights the bay grid", () => {
  const marker = "preloadedSasFabric: ";
  const start = PUBLIC_DEMO.indexOf(marker);
  assert.notEqual(start, -1, "the artifact must carry a preloadedSasFabric bootstrap entry");
  const end = PUBLIC_DEMO.indexOf("\n", start);
  const raw = PUBLIC_DEMO.slice(start + marker.length, end).replace(/,$/, "");
  const payload = JSON.parse(JSON.parse(raw));

  assert.equal(payload.available, true);
  assert.equal(payload.platform, "core");
  assert.equal(payload.controllers.length, 2);
  assert.equal(payload.paths.length, 3);
  assert.ok(
    payload.paths.some((entry) => entry.state !== "active"),
    "at least one non-active path so the degraded lane is visible"
  );

  const laneSlots = new Map();
  for (const entry of payload.paths) {
    const seen = laneSlots.get(entry.controller) || new Set();
    entry.slots.forEach((slotNumber) => seen.add(slotNumber));
    laneSlots.set(entry.controller, seen);
  }
  assert.equal(laneSlots.size, 2, "two lanes, so each grid dims the bays the other owns");
  for (const [controller, slots] of laneSlots) {
    assert.ok(slots.size > 0 && slots.size < 60, `${controller} must light some bays and dim others`);
  }

  // Bays the fixture reports as empty and that fall outside a lane become the
  // empty placeholders; populated bays outside a lane become the dim ones.
  const laneOne = laneSlots.get("demo-hba-0");
  assert.equal(laneOne.has(45), false, "bay 45 is empty and outside lane one");
  assert.equal(laneOne.has(57), false, "bay 57 is populated and outside lane one");

  assert.ok(
    payload.traces.some((trace) => trace.id === "bay:57"),
    "the capture clicks bay 57, so its bay trace must exist"
  );
});

test("the artifact still ships the layout-shaped grid renderer", () => {
  for (const token of [
    "sas-fabric-bay-layout",
    "sas-fabric-bay-row",
    "sas-fabric-bay-group",
    "sas-fabric-bay-edge-label",
    "buildLayoutGridRows",
  ]) {
    assert.ok(PUBLIC_DEMO.includes(token), `${token} must be present in the published artifact`);
  }
});
