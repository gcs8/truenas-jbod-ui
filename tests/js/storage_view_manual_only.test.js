"use strict";

// Codex security P2 on #1070: a saved chassis view drawn with a layout-only
// UniFi profile (G2, G2-Pro, ENVR, ENVR-Core) has no validated bay order, so
// its bays must never borrow the live enclosure bay at the same index for the
// detail panel, heat map, connections, SMART, history or actions. Only a
// saved manual mapping places a disk there, and the server sends it on the
// view slot itself. A stale or older payload that still carries a bay link is
// refused on the browser side too.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const helperText = fs.readFileSync(path.join(__dirname, "refresh_races.test.js"), "utf8");
const helpers = vm.createContext({ assert });
vm.runInContext(
  helperText.slice(helperText.indexOf("function functionSource("), helperText.indexOf("function loadFunction(")),
  helpers,
);

const LIVE_SLOTS = [
  { slot: 0, serial: "SANITIZED-LIVE-0", device_name: "da0" },
  { slot: 1, serial: "SANITIZED-LIVE-1", device_name: "da1" },
];

function load(view) {
  const context = vm.createContext({
    state: { snapshot: { selected_enclosure_id: "enc-a", slots: LIVE_SLOTS } },
    getSelectedStorageViewRuntime: () => view,
    currentLiveEnclosureId: () => "enc-a",
  });
  vm.runInContext(
    [
      "getSlotById",
      "isLiveStyledStorageView",
      "storageViewSlotBayInEnclosure",
      "storageViewSlotIsManualOnly",
      "getLiveBackedStorageViewSlot",
      "sasFabricList",
      "sasFabricViewSlotForBay",
      "buildStorageViewHistoryContextSlot",
    ].map((name) => helpers.functionSource(APP_SOURCE, name)).join("\n"),
    context,
  );
  return context;
}

function chassisView(slots) {
  return {
    id: "pinned-nvr",
    kind: "ses_enclosure",
    source: "selected_enclosure_snapshot",
    backing_enclosure_id: "enc-a",
    slots,
  };
}

test("a manual-only view bay never borrows the live bay at its index", () => {
  const stale = { slot_index: 1, snapshot_slot: 1, snapshot_enclosure_id: "enc-a", source: "snapshot_slot", manual_mapping_only: true };
  const view = chassisView([stale]);
  const app = load(view);
  assert.equal(app.getLiveBackedStorageViewSlot(view, stale), null);
  assert.equal(app.sasFabricViewSlotForBay(1), null, "connections must not select an unvalidated bay");
  const history = app.buildStorageViewHistoryContextSlot(view, stale);
  assert.equal(history.enclosure_id, "storage-view:pinned-nvr", "heat map and history keep the view's own scope");
  assert.equal(history.slot, 1);
});

test("UNVR and other validated chassis views still follow their live bays", () => {
  const live = { slot_index: 1, snapshot_slot: 1, snapshot_enclosure_id: "enc-a", source: "snapshot_slot", manual_mapping_only: false };
  const view = chassisView([live]);
  const app = load(view);
  assert.equal(app.getLiveBackedStorageViewSlot(view, live)?.serial, "SANITIZED-LIVE-1");
  assert.equal(app.sasFabricViewSlotForBay(1), 1);
  assert.equal(app.buildStorageViewHistoryContextSlot(view, live).enclosure_id, "enc-a");
});
