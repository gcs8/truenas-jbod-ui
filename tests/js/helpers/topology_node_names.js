"use strict";

const H = require("./fabric_topology");

// Invented four-bay network-recorder shape. No appliance evidence is reused.
function recorderFabric() {
  const fabric = H.coreFabric();
  fabric.platform = "linux";
  fabric.raw = { fabric_kind: "storage_linux", fabric_domain: "storage_fabric" };
  fabric.nodes[0].label = "Synthetic recorder";
  const source = H.node("controller:synthetic-block", "controller", { label: "Linux block" });
  const path = H.node("path:synthetic-volume", "path", {
    label: "/volume/synthetic-recording-archive-with-a-deliberately-long-single-token > array-demo > data",
    status: "healthy",
    metrics: { path_type: "block", state_basis: "member-health-not-transport" },
  });
  const enclosure = H.node("storage-enclosure:synthetic-front", "storage-enclosure", { label: "Synthetic front four" });
  fabric.nodes.push(source, path, enclosure);
  fabric.links.push(H.link("host", source.id, "host-controller"), H.link(source.id, path.id, "controller-path"), H.link(path.id, enclosure.id, "path-storage-enclosure"));
  H.addBays(fabric, [0, 1, 2, 3], [enclosure.id], [{ controller: "Linux block", state: "healthy" }]);
  for (const node of fabric.nodes.filter((node) => node.kind === "bay")) {
    node.label = `Bay ${String(node.slot + 1).padStart(2, "0")}`;
    node.metrics.device_name = `synthetic-disk-${node.slot}`;
  }
  for (const trace of fabric.traces) {
    trace.label = fabric.nodes.find((node) => node.id === trace.id).label;
    trace.metrics.device_name = `synthetic-disk-${trace.slots[0]}`;
    trace.node_ids = ["host", source.id, path.id, enclosure.id, trace.id];
  }
  return fabric;
}

function overflowFabric() {
  const fabric = recorderFabric();
  fabric.traces[0].metrics.path_states = Array.from({ length: 9 }, (_, index) => ({
    controller: "Linux block", state: index ? "passive" : "active", device_name: `synthetic-port-${index}`,
  }));
  return fabric;
}

function nameCases() {
  const cases = Object.fromEntries(Object.entries(H.producerCases()).map(([name, value]) => [name, value.fabric]));
  cases.recorder_long_path = recorderFabric();
  const unknown = H.coreFabric();
  H.addController(unknown, "synthetic-hba");
  H.addExpander(unknown, "synthetic-hba", 0xab01, 1, null);
  cases.unknown_parent = unknown;
  return cases;
}

module.exports = { recorderFabric, overflowFabric, nameCases };
