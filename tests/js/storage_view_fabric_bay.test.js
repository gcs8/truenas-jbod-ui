"use strict";

// #1166: with a storage view selected, state.selectedSlot is the view's
// slot_index. Every path that turns it into a Storage Fabric bay trace must use
// the view slot's backing enclosure bay (snapshot_slot), or no bay at all.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(name) {
  const start = APP_SOURCE.indexOf(`function ${name}(`);
  if (start === -1) return null;
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
    const next = APP_SOURCE[index + 1];
    if (lineComment) {
      lineComment = character !== "\n";
      continue;
    }
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "/" && next === "/") {
      lineComment = true;
      index += 1;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth === 0) return APP_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

// The real fabric functions, loaded together. Names absent from the source
// are skipped so these tests fail on the predecessor by behavior where they can.
const FABRIC_FUNCTIONS = [
  "escapeHtml",
  "sasFabricList",
  "sasFabricTraceMap",
  "sasFabricNodeMap",
  "sasFabricTraceById",
  "sasFabricNodeById",
  "storageViewSlotIsManualOnly",
  "sasFabricViewSlotForBay",
  "sasFabricBayForViewSlot",
  "sasFabricViewSlotNote",
  "selectSlotForBay",
  "defaultSasFabricTraceId",
  "resolveSasFabricTraceId",
  "syncSasFabricTraceToSlot",
  "selectedSasFabricTrace",
  "selectedSasFabricNode",
  "selectSlot",
  "openSasFabricPanel",
  "renderSasFabricInspector",
];

// View slot indexes collide with real fabric bay numbers: view slot 0 sits on
// bay 02, view slot 1 on bay 00, view slot 2 is a PCIe NVMe device with no
// bay, and view slot 3 is a manual-only chassis slot with no live bay link.
function mixedView() {
  return {
    id: "mixed-view",
    kind: "ses_enclosure",
    source: "inventory_binding",
    backing_enclosure_id: "front",
    slots: [
      { slot_index: 0, slot_label: "00", snapshot_slot: 2, snapshot_enclosure_id: "front", device_name: "sdc", source: "inventory_candidate" },
      { slot_index: 1, slot_label: "01", snapshot_slot: 0, snapshot_enclosure_id: "front", device_name: "sda", source: "inventory_candidate" },
      { slot_index: 2, slot_label: "M2-3", device_name: "nvd2", bus: "PCIe", source: "inventory_candidate" },
      { slot_index: 3, slot_label: "03", device_name: "sdd", manual_mapping_only: true, source: "manual_mapping" },
    ],
  };
}

// A boot SATADOM pair on the motherboard: view slot 0 (DOM-A) collides with
// fabric bay 00 but has no enclosure bay at all.
function satadomView() {
  return {
    id: "boot-satadoms",
    template_id: "satadom-pair-2",
    kind: "boot_devices",
    source: "inventory_binding",
    slots: [
      { slot_index: 0, slot_label: "DOM-A", device_name: "ada0", bus: "SATA", source: "inventory_candidate" },
      { slot_index: 1, slot_label: "DOM-B", device_name: "ada1", bus: "SATA", source: "inventory_candidate" },
    ],
  };
}

function fabric() {
  return {
    available: true,
    traces: [0, 1, 2, 3].map((bay) => ({ id: `bay:${bay}`, kind: "bay", label: `Bay ${bay}`, slots: [bay], node_ids: [], metrics: {} })),
    nodes: [{ id: "controller:hba", kind: "controller", related_slots: [0, 1, 2, 3] }],
  };
}

function element() {
  return { textContent: "", innerHTML: "", scrollIntoView() {} };
}

function load({ view = mixedView(), selectedSlot = null, selectedTraceId = null, data = fabric() } = {}) {
  const events = [];
  const state = {
    selectedSlot,
    snapshotMode: true,
    selectedStorageViewRuntimeId: view ? view.id : null,
    snapshot: { selected_enclosure_id: "front" },
    history: { panelError: null },
    sasFabric: { open: true, data, selectedTraceId, selectedNodeId: null, selectionCleared: false },
  };
  const context = {
    Number,
    Map,
    Set,
    state,
    events,
    getSelectedStorageViewRuntime: () => view,
    getSelectedStorageViewRuntimeSlot: (index) => (view ? view.slots.find((slot) => slot.slot_index === index) || null : null),
    inventoryScopeMatchesSelection: () => true,
    confirmMappingDraftDiscard: () => true,
    refreshGridSelectionState() { events.push("grid"); },
    renderSasFabric() { events.push("render-fabric"); },
    renderDetail() { events.push("detail"); },
    renderAll() { events.push("render-all"); },
    fetchSasFabric() { events.push("fetch"); },
    formatSasFabricKind: (kind) => String(kind),
    renderSasFabricTraceInspector: (trace) => `trace ${trace.id}`,
    renderSasFabricNodeInspector: (node) => `node ${node.id}`,
    sasFabricPanel: null,
    sasFabricInspectorTitle: element(),
    sasFabricInspectorBody: element(),
  };
  const sandbox = vm.createContext(context);
  const present = FABRIC_FUNCTIONS.filter((name) => functionSource(name) !== null);
  vm.runInContext(
    `${present.map(functionSource).join("\n")}\nthis.__loaded = { ${present.join(", ")} };`,
    sandbox,
    { filename: "storage-view-fabric-bay.behavior.js" },
  );
  return { ...sandbox.__loaded, state, events, context };
}

test("a view slot's fabric bay is its backing bay, never the view index", () => {
  const loaded = load();
  assert.equal(typeof loaded.sasFabricBayForViewSlot, "function", "one helper maps a view slot to its bay");
  assert.equal(loaded.sasFabricBayForViewSlot(0), 2, "view slot 0 sits on bay 02");
  assert.equal(loaded.sasFabricBayForViewSlot(1), 0, "view slot 1 sits on bay 00");
  assert.equal(loaded.sasFabricBayForViewSlot(2), null, "the NVMe slot has no bay");
  assert.equal(loaded.sasFabricBayForViewSlot(3), null, "a manual-only slot has no live bay");
  assert.equal(loaded.sasFabricBayForViewSlot(7), null, "no such view slot");
  for (const invalid of [null, undefined, "0", 0.5, NaN]) assert.equal(loaded.sasFabricBayForViewSlot(invalid), null);

  const live = load({ view: null });
  assert.equal(live.sasFabricBayForViewSlot(2), 2, "no storage view: the slot is the bay");
});

test("a view slot whose bay belongs to another enclosure or is ambiguous selects no bay", () => {
  const foreign = mixedView();
  foreign.slots[0].snapshot_enclosure_id = "rear";
  assert.equal(load({ view: foreign }).sasFabricBayForViewSlot(0), null);
  const duplicate = mixedView();
  duplicate.slots.push({ ...duplicate.slots[0], slot_index: 4 });
  assert.equal(load({ view: duplicate }).sasFabricBayForViewSlot(0), null, "two view slots claim bay 02");
});

test("the first fabric load selects the backing bay, or none", () => {
  for (const [selectedSlot, expected] of [[0, "bay:2"], [1, "bay:0"], [2, null], [3, null], [null, null]]) {
    const loaded = load({ selectedSlot });
    assert.equal(loaded.defaultSasFabricTraceId(), expected, `view slot ${selectedSlot}`);
    assert.equal(loaded.resolveSasFabricTraceId(null), expected, `view slot ${selectedSlot} after load`);
  }
  assert.equal(load({ view: null, selectedSlot: 2 }).defaultSasFabricTraceId(), "bay:2", "no view: unchanged");
});

test("grid clicks and search auto-select (selectSlot) sync the backing bay, or clear it", () => {
  const loaded = load({ selectedSlot: 1, selectedTraceId: "bay:0" });
  assert.equal(loaded.selectSlot(0), true);
  assert.equal(loaded.state.selectedSlot, 0);
  assert.equal(loaded.state.sasFabric.selectedTraceId, "bay:2", "view slot 0 is bay 02, not bay 00");

  assert.equal(loaded.selectSlot(2), true);
  assert.equal(loaded.state.selectedSlot, 2);
  assert.equal(loaded.state.sasFabric.selectedTraceId, null, "the NVMe slot drops bay 02's trace");
  assert.equal(loaded.state.sasFabric.selectedNodeId, null);

  assert.equal(loaded.selectSlot(3), true);
  assert.equal(loaded.state.sasFabric.selectedTraceId, null, "never bay:3 for a manual-only slot");

  assert.equal(loaded.selectSlot(1), true);
  assert.equal(loaded.state.sasFabric.selectedTraceId, "bay:0");

  // #810's chip direction still passes the physical bay explicitly.
  assert.equal(loaded.selectSlot(0, { sasFabricSlot: 2 }), true);
  assert.equal(loaded.state.sasFabric.selectedTraceId, "bay:2");
});

test("a SATADOM-pair view slot never selects the bay sharing its index", () => {
  for (const selectedSlot of [0, 1]) {
    const loaded = load({ view: satadomView(), selectedSlot });
    assert.equal(loaded.sasFabricBayForViewSlot(selectedSlot), null);
    assert.equal(loaded.defaultSasFabricTraceId(), null, `DOM slot ${selectedSlot} on first load`);
    loaded.openSasFabricPanel();
    assert.equal(loaded.state.sasFabric.selectedTraceId, null, `DOM slot ${selectedSlot} on open`);
  }
  const loaded = load({ view: satadomView(), selectedSlot: 1, selectedTraceId: "bay:1" });
  assert.equal(loaded.selectSlot(0), true);
  assert.equal(loaded.state.sasFabric.selectedTraceId, null, "DOM-A must not light bay 00");
  loaded.renderSasFabricInspector(loaded.state.sasFabric.data);
  assert.match(loaded.context.sasFabricInspectorBody.innerHTML, /ada0 is not on the SAS fabric/);
});

test("without a storage view selectSlot still selects the bay of the same number", () => {
  const loaded = load({ view: null, selectedSlot: null });
  assert.equal(loaded.selectSlot(3), true);
  assert.equal(loaded.state.sasFabric.selectedTraceId, "bay:3");
});

test("opening Connections syncs the backing bay, or clears a stale bay", () => {
  const onBay = load({ selectedSlot: 0, selectedTraceId: null });
  onBay.openSasFabricPanel();
  assert.equal(onBay.state.sasFabric.selectedTraceId, "bay:2");

  const offFabric = load({ selectedSlot: 2, selectedTraceId: "bay:2" });
  offFabric.openSasFabricPanel();
  assert.equal(offFabric.state.sasFabric.selectedTraceId, null, "the NVMe slot must not light bay 02");

  const unselected = load({ selectedSlot: null, selectedTraceId: "bay:1" });
  unselected.openSasFabricPanel();
  assert.equal(unselected.state.sasFabric.selectedTraceId, "bay:1", "no slot selected: unchanged");
});

test("topology pills name physical bays and select the view slot that bay backs", () => {
  const loaded = load({ selectedSlot: 0, selectedTraceId: "bay:2" });
  assert.equal(typeof loaded.selectSlotForBay, "function");
  assert.equal(loaded.selectSlotForBay(0), true);
  assert.equal(loaded.state.selectedSlot, 1, "bay 00 backs view slot 1");
  assert.equal(loaded.state.sasFabric.selectedTraceId, "bay:0");
  assert.equal(loaded.selectSlotForBay(3), false, "no view slot backs bay 03");
  assert.equal(loaded.state.selectedSlot, 1);

  const live = load({ view: null, selectedSlot: 0 });
  assert.equal(live.selectSlotForBay(3), true);
  assert.equal(live.state.selectedSlot, 3, "no view: the bay is the slot");

  const start = APP_SOURCE.indexOf("function renderTopologyContext(");
  const body = APP_SOURCE.slice(start, APP_SOURCE.indexOf("function renderMultipathContext(", start));
  assert.match(body, /selectSlotForBay\(slotNumber\)/, "the topology click handler maps the bay");
  assert.doesNotMatch(body, /\bselectSlot\(slotNumber\)/);
});

test("Connections reserves off-fabric wording for known non-SAS transport", () => {
  const cases = [
    // An unresolved foreign bay is still not a backing bay for this view.
    { device_name: "nvme0n1", bus: "nvme", snapshot_slot: 2, snapshot_enclosure_id: "rear" },
    { device_name: "nvd2", bus: "UNKNOWN" },
    { device_name: "nvme0n1", bus: null },
    { device_name: "nvd2", bus: "PCIe" },
    { device_name: "ada0", bus: "SATA", template_id: "satadom-pair-2" },
  ];
  for (const { template_id, ...slot } of cases) {
    const view = mixedView();
    view.template_id = template_id || "generic";
    view.slots[2] = { slot_index: 2, occupied: true, source: "inventory_candidate", ...slot };
    const loaded = load({ view, selectedSlot: 0, selectedTraceId: "bay:2" });
    loaded.selectSlot(2);
    assert.equal(loaded.state.sasFabric.selectedTraceId, null);
    assert.equal(loaded.state.sasFabric.selectedNodeId, null);
    loaded.renderSasFabricInspector(loaded.state.sasFabric.data);
    assert.equal(loaded.context.sasFabricInspectorBody.innerHTML,
      `<p class="sas-fabric-view-slot-note">${slot.device_name} is not on the SAS fabric, so no bay path is shown.</p>`, JSON.stringify(slot));
  }
});

test("Connections reports unknown placement for SAS, SATA or unknown transport without a bay", () => {
  const cases = [
    // Linux inventory preserves bus=SAS with slot=None; serialization retains
    // bus and emits snapshot_slot=null, not evidence of a non-SAS disk.
    { device_name: "sda", bus: "SAS", snapshot_slot: null, snapshot_enclosure_id: null },
    { device_name: "sdb", bus: null },
    { device_name: "sdc", bus: "UNKNOWN" },
    { device_name: "sdd", bus: "SATA" }, // SATA can be behind a SAS expander.
    { device_name: "nvd2", bus: "SAS" }, // Reported transport beats a name heuristic.
    { device_name: "ada0", bus: "SAS", template_id: "satadom-pair-2" },
    { device_name: "sde", bus: "SATA", sas_address: "synthetic-sas-address" },
  ];
  for (const { template_id, ...slot } of cases) {
    const view = mixedView();
    view.template_id = template_id || "generic";
    view.slots[2] = { slot_index: 2, occupied: true, source: "inventory_candidate", ...slot };
    const loaded = load({ view, selectedSlot: 0, selectedTraceId: "bay:2" });
    loaded.selectSlot(2);
    assert.equal(loaded.state.sasFabric.selectedTraceId, null);
    assert.equal(loaded.state.sasFabric.selectedNodeId, null);
    loaded.renderSasFabricInspector(loaded.state.sasFabric.data);
    assert.equal(loaded.context.sasFabricInspectorBody.innerHTML,
      `<p class="sas-fabric-view-slot-note">No enclosure bay is mapped for ${slot.device_name}, so no bay path is shown.</p>`, JSON.stringify(slot));
  }
});

test("Connections uses a neutral note for an empty placeholder without a bay", () => {
  const view = mixedView();
  view.slots[2] = { slot_index: 2, slot_label: "M2-3", occupied: false, state: "empty", source: "placeholder",
    device_name: null, snapshot_slot: null, bus: null };
  const loaded = load({ view, selectedSlot: 0, selectedTraceId: "bay:2" });
  loaded.selectSlot(2);
  assert.equal(loaded.state.sasFabric.selectedTraceId, null);
  assert.equal(loaded.state.sasFabric.selectedNodeId, null);
  loaded.renderSasFabricInspector(loaded.state.sasFabric.data);
  assert.equal(loaded.context.sasFabricInspectorBody.innerHTML, '<p class="sas-fabric-view-slot-note">This slot is empty.</p>');
});

test("Connections says plainly when the selected view slot is not on the SAS fabric", () => {
  const offFabric = load({ selectedSlot: 2 });
  offFabric.renderSasFabricInspector(offFabric.state.sasFabric.data);
  assert.match(offFabric.context.sasFabricInspectorBody.innerHTML, /nvd2 is not on the SAS fabric/);
  assert.doesNotMatch(offFabric.context.sasFabricInspectorBody.innerHTML, /trace bay:/);

  const manualOnly = load({ selectedSlot: 3 });
  manualOnly.renderSasFabricInspector(manualOnly.state.sasFabric.data);
  assert.match(manualOnly.context.sasFabricInspectorBody.innerHTML, /No enclosure bay is mapped for sdd/);

  const onBay = load({ selectedSlot: 0, selectedTraceId: "bay:2" });
  onBay.renderSasFabricInspector(onBay.state.sasFabric.data);
  assert.equal(onBay.context.sasFabricInspectorBody.innerHTML, "trace bay:2");

  const live = load({ view: null, selectedSlot: 7 });
  live.renderSasFabricInspector(live.state.sasFabric.data);
  assert.equal(live.context.sasFabricInspectorBody.innerHTML, "Pick a controller, cable path, or bay.");
});
