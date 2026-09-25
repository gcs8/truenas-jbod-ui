"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { fixture, load } = require("./fabric_aggregate_provenance.test.js");

for (const n of [60, 347]) for (const mode of ["renderLanesMode", "renderTraceMode", "renderImpactMode"]) {
  test(`bounded actual provenance work: ${n} ${mode}`, (t) => {
    const data = fixture(n);
    const ui = load(data, true);
    ui[mode](ui.state.fabric);
    t.diagnostic(JSON.stringify(ui.stats));
    assert.ok(ui.stats.traceBuilds <= 1, `trace builds: ${ui.stats.traceBuilds}`);
    assert.ok(ui.stats.traceEntries <= data.fabric.traces.length * 4, `trace ID iterations: ${ui.stats.traceEntries}`);
    assert.ok(ui.stats.slotEntries <= n * 2, `slot iterations: ${ui.stats.slotEntries}`);
  });
}
for (const replaced of ["snapshot", "fabric"]) {
  test(`replacement of ${replaced} invalidates affirmative provenance`, () => {
    const ui = load(fixture(2));
    assert.equal(ui.diskLocation(0).physical, true);
    const replacement = fixture(2)[replaced];
    if (replaced === "snapshot") replacement.slots[0].physical_location_known = false;
    else replacement.traces.find(t => t.id === "bay:0").metrics = { virtual_enclosure: true };
    ui.state[replaced] = replacement;
    assert.equal(ui.diskLocation(0).physical, false);
    ui.state.selectedTraceId = "bay:0";
    ui.renderInspector(ui.state.fabric);
    assert.match(ui.elements.get("fabric-inspector-body").innerHTML, /data-fabric-alias-edit="bay:0"/);
    assert.match(ui.elements.get("fabric-inspector-title").textContent, /Selected Disk/);
    ui.state[replaced] = fixture(2)[replaced];
    assert.equal(ui.diskLocation(0).physical, true);
  });
}
