"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
function topology() {
  const context = vm.createContext({ window: {} });
  vm.runInContext(fs.readFileSync(path.join(root, "app/static/sas_fabric_topology.js"), "utf8"), context);
  return context.window.SasFabricTopology;
}
function syntheticFabric(available = true) {
  const node = (id, kind, metrics = {}) => ({ id, kind, label: id, metrics, raw: {}, related_slots: [] });
  const link = (source, target, kind, extra = {}) => ({ id: `${kind}:${source}->${target}`, source, target, kind, ...extra });
  return {
    available: true, platform: "scale", raw: { fabric_kind: "linux_ses", sas_transport_available: available },
    nodes: [node("host", "host"), node("controller:host2", "controller"),
      node("expander:front", "expander", { sas_level: 1 }), node("expander:rear", "expander", { sas_level: 2 }),
      node("ses:sg9", "ses-enclosure")],
    links: [link("host", "controller:host2", "host-controller"),
      link("controller:host2", "expander:front", "controller-expander"),
      link("expander:front", "expander:rear", "expander-expander", {
        reported_lanes: { basis: "sysfs_reciprocal_port_membership", lane_count: 2,
          source_phys: [2, 3], target_phys: [0, 1], lanes: [], negotiated_rate_gbps: 12 },
      }), link("expander:rear", "ses:sg9", "expander-ses-enclosure")], traces: [],
  };
}

test("Linux transport records are drawn rather than classified as uncollected", () => {
  const model = topology().buildTopologyModel(syntheticFabric());
  assert.equal(model.contract, "sas");
  assert.equal(model.nodes.filter((node) => node.kind === "expander").length, 2);
  assert.ok(model.tiers.some((tier) => tier.label === "Expanders, level 2"));
  assert.ok(!model.tiers.some((tier) => tier.state === "not_collected"));
  const cascade = model.links.find((link) => link.kind === "expander-expander");
  assert.equal(cascade.lanes.count, 2);
  assert.equal(cascade.lanes.rate, 12);
  assert.equal(model.links.length, 4);
  assert.ok(!model.notices.some((notice) => notice.startsWith("Not drawn:")));
});

test("SES-only payloads retain the existing not-collected explanation", () => {
  const model = topology().buildTopologyModel(syntheticFabric(false));
  assert.equal(model.contract, "storage");
  assert.ok(model.tiers.some((tier) => tier.state === "not_collected"));
});

test("transport copy does not claim that reported expander detail is unexposed", () => {
  const source = fs.readFileSync(path.join(root, "app/static/sas_fabric_view.js"), "utf8");
  const start = source.indexOf("  function buildFabricViewCopy(");
  const end = source.indexOf("\n  function ", start + 10);
  assert.ok(start >= 0 && end > start);
  const context = vm.createContext({ fabricKind: (fabric) => fabric.raw.fabric_kind, modeCopy: {} });
  vm.runInContext(source.slice(start, end) + "\nthis.copy = buildFabricViewCopy;", context);
  const copy = context.copy(syntheticFabric(), "TrueNAS SCALE");
  assert.equal(copy.summary.controllers[0], "HBAs");
  assert.equal(copy.summary.expanders[0], "Expanders");
  assert.ok(!JSON.stringify(copy).includes("not exposed"));
  assert.equal(context.copy(syntheticFabric(false), "TrueNAS SCALE").summary.expanders[1], "not exposed");
});
