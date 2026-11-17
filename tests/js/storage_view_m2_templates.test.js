"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../admin_service/static/admin.js");
const SOURCE = [path.resolve(__dirname, "../../admin_service/static/admin_profile_model.js"), SCRIPT_PATH].map((file) => fs.readFileSync(file, "utf8")).join("\n");

function sourceBetween(startMarker, endMarker) {
  const start = SOURCE.indexOf(startMarker);
  assert.notEqual(start, -1, `missing start marker: ${startMarker}`);
  const end = SOURCE.indexOf(endMarker, start);
  assert.notEqual(end, -1, `missing end marker: ${endMarker}`);
  return SOURCE.slice(start, end);
}

const SNIPPETS = [
  sourceBetween("  function slugify", "\n  function parseSlotLabelsText"),
  sourceBetween("  function getStorageViewTemplate(templateId) {", "\n  function storageViewAddValueForProfile"),
  sourceBetween("  function nextStorageViewOrder() {", "\n  function cloneStorageView"),
  sourceBetween("  function normalizeStorageView(rawView", "\n  function existingSavedChassisView"),
];

function emptyBinding(mode) {
  return {
    mode,
    target_system_id: null,
    enclosure_ids: [],
    pool_names: [],
    serials: [],
    pcie_addresses: [],
    device_names: [],
  };
}

const VISIBLE_RENDER = { show_in_main_ui: true, show_in_admin_ui: true, default_collapsed: false };

// Same shape as serialize_storage_view_templates() for the two M.2 templates.
const AOC_TEMPLATE = {
  id: "aoc-slg4-2h8m2-2",
  label: "Supermicro AOC-SLG4-2H8M2",
  default_id: "aoc-slg4-2h8m2",
  default_label: "AOC-SLG4-2H8M2",
  kind: "nvme_carrier",
  default_render: VISIBLE_RENDER,
  default_binding: emptyBinding("hybrid"),
  platform_binding_defaults: {
    esxi: {
      ...emptyBinding("hybrid"),
      pcie_addresses: ["C0 x4", "C1 x4"],
      device_names: ["13:0", "13:1"],
    },
  },
  default_slot_labels: { 0: "M2-1", 1: "M2-2" },
};

const PASSIVE_TEMPLATE = {
  id: "m2-carrier-passive-2",
  label: "2x M.2 carrier (passive)",
  default_id: "m2-carrier",
  default_label: "2x M.2 Carrier",
  kind: "nvme_carrier",
  default_render: VISIBLE_RENDER,
  default_binding: emptyBinding("auto"),
  platform_binding_defaults: {},
  default_slot_labels: { 0: "M2-1", 1: "M2-2" },
};

const ESXI_MEMBER_IDS = ["13:0", "13:1", "C0 x4", "C1 x4"];

function createFor(templateId, platform) {
  const state = { storageViewTemplates: [AOC_TEMPLATE, PASSIVE_TEMPLATE], storageViews: [] };
  const context = vm.createContext({ console, state, currentSetupPlatform: () => platform });
  vm.runInContext(
    `${SNIPPETS.join("\n")}\nglobalThis.__tested = { createStorageViewFromTemplate };`,
    context,
    { filename: "admin.js" }
  );
  return JSON.parse(JSON.stringify(context.__tested.createStorageViewFromTemplate(templateId)));
}

function bindingValues(view) {
  return [...view.binding.device_names, ...view.binding.pcie_addresses];
}

test("an AOC-SLG4-2H8M2 view on a non-ESXi system carries no ESXi member IDs", () => {
  for (const platform of ["core", "scale", "linux", "quantastor"]) {
    const view = createFor("aoc-slg4-2h8m2-2", platform);
    assert.equal(view.render.show_in_main_ui, true, platform);
    assert.equal(view.binding.mode, "hybrid", platform);
    for (const value of ESXI_MEMBER_IDS) {
      assert.ok(!bindingValues(view).includes(value), `${platform} view has ${value}`);
    }
  }
});

test("an AOC-SLG4-2H8M2 view on an ESXi system starts with the RAID member IDs", () => {
  const view = createFor("aoc-slg4-2h8m2-2", "esxi");

  assert.equal(view.render.show_in_main_ui, true);
  assert.equal(view.binding.mode, "hybrid");
  assert.deepEqual(view.binding.device_names, ["13:0", "13:1"]);
  assert.deepEqual(view.binding.pcie_addresses, ["C0 x4", "C1 x4"]);
});

test("creating an ESXi view does not share or change the template's arrays", () => {
  const view = createFor("aoc-slg4-2h8m2-2", "esxi");
  view.binding.device_names.push("13:2");

  assert.deepEqual(AOC_TEMPLATE.platform_binding_defaults.esxi.device_names, ["13:0", "13:1"]);
  assert.deepEqual(AOC_TEMPLATE.default_binding.device_names, []);
});

test("a passive M.2 carrier view shows on the main page with auto binding on every platform", () => {
  for (const platform of ["core", "scale", "esxi"]) {
    const view = createFor("m2-carrier-passive-2", platform);
    assert.equal(view.template_id, "m2-carrier-passive-2", platform);
    assert.equal(view.kind, "nvme_carrier", platform);
    assert.equal(view.render.show_in_main_ui, true, platform);
    assert.equal(view.binding.mode, "auto", platform);
    assert.deepEqual(bindingValues(view), [], platform);
    assert.deepEqual(view.layout_overrides.slot_labels, { 0: "M2-1", 1: "M2-2" }, platform);
  }
});

// Codex review on #963: a view keeps the binding of the platform it was added
// on, so changing the Platform field must move untouched template defaults.
function loadWithViews(views, platformRef) {
  const state = { storageViewTemplates: [AOC_TEMPLATE, PASSIVE_TEMPLATE], storageViews: [] };
  const context = vm.createContext({ console, state, currentSetupPlatform: () => platformRef.value });
  vm.runInContext(
    `${SNIPPETS.join("\n")}\nglobalThis.__tested = { createStorageViewFromTemplate, syncStorageViewPlatformBindings };`,
    context,
    { filename: "admin.js" }
  );
  const tested = context.__tested;
  for (const { templateId, edit } of views) {
    const view = tested.createStorageViewFromTemplate(templateId);
    if (edit) {
      edit(view);
    }
    state.storageViews.push(view);
  }
  return { state, sync: tested.syncStorageViewPlatformBindings };
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test("switching an untouched AOC view from ESXi to CORE drops the ESXi member IDs", () => {
  const platform = { value: "esxi" };
  const { state, sync } = loadWithViews([{ templateId: "aoc-slg4-2h8m2-2" }], platform);
  assert.deepEqual(plain(state.storageViews[0].binding.device_names), ["13:0", "13:1"]);

  platform.value = "core";
  assert.equal(sync(), true);

  const binding = plain(state.storageViews[0].binding);
  assert.equal(binding.mode, "hybrid");
  assert.deepEqual([...binding.device_names, ...binding.pcie_addresses], []);
});

test("switching an untouched AOC view from SCALE to ESXi adds the ESXi member IDs", () => {
  const platform = { value: "scale" };
  const { state, sync } = loadWithViews([{ templateId: "aoc-slg4-2h8m2-2" }], platform);

  platform.value = "esxi";
  assert.equal(sync(), true);

  assert.deepEqual(plain(state.storageViews[0].binding.device_names), ["13:0", "13:1"]);
  assert.deepEqual(plain(state.storageViews[0].binding.pcie_addresses), ["C0 x4", "C1 x4"]);
  state.storageViews[0].binding.device_names.push("13:2");
  assert.deepEqual(AOC_TEMPLATE.platform_binding_defaults.esxi.device_names, ["13:0", "13:1"]);
});

test("a platform change keeps bindings the operator edited, generated views, and other templates", () => {
  const platform = { value: "core" };
  const { state, sync } = loadWithViews(
    [
      { templateId: "aoc-slg4-2h8m2-2", edit: (view) => { view.binding.device_names = ["nvd0", "nvd1"]; } },
      { templateId: "aoc-slg4-2h8m2-2", edit: (view) => { view.generated = true; } },
      { templateId: "m2-carrier-passive-2" },
    ],
    platform
  );
  const before = plain(state.storageViews);

  platform.value = "esxi";
  assert.equal(sync(), false);

  assert.deepEqual(plain(state.storageViews), before);
});

test("the Platform change handler moves template bindings before it redraws the views", () => {
  const handler = sourceBetween('    elements.setupPlatform?.addEventListener("change", () => {', "\n    });");
  const syncAt = handler.indexOf("syncStorageViewPlatformBindings()");
  assert.notEqual(syncAt, -1);
  assert.ok(syncAt < handler.indexOf("renderStorageViews()"));
});
