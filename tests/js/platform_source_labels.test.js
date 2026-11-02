"use strict";

// #1066: non-TrueNAS systems are not labelled as TrueNAS API sources, an
// optional SSH read that failed is a partial state rather than an error, and a
// mount every bay shares does not fill each tile's second line.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(name) {
  const start = APP_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
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

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ ...context });
  const source = names.map(functionSource).join("\n");
  const exports = names.map((name) => `this.__${name} = ${name};`).join("\n");
  vm.runInContext(`${source}\n${exports}`, sandbox, { filename: "platform-source-labels.js" });
  return Object.fromEntries(names.map((name) => [name, sandbox[`__${name}`]]));
}

const STATUS_FUNCTIONS = [
  "renderStatus",
  "snapshotSshStatus",
  "apiStatusChipState",
  "sshStatusChipState",
  "platformApiName",
  "currentPlatform",
];

function chip() {
  return { className: "status-chip", textContent: "", title: "" };
}

function renderStatusWith({ platform, sources, snapshotMode = false }) {
  const apiStatusChip = chip();
  const sshStatusChip = chip();
  const { renderStatus } = loadFunctions(STATUS_FUNCTIONS, {
    state: {
      snapshotMode,
      snapshotExportMeta: null,
      snapshot: { sources, selected_system_platform: platform, last_updated: "2026-11-02T12:00:00Z" },
      history: { configured: false },
    },
    snapshotStatusChip: chip(),
    apiStatusChip,
    sshStatusChip,
    historyStatusChip: chip(),
    lastUpdated: { textContent: "", title: "" },
    formatTimestamp: (value) => String(value),
    renderTimezoneLabel() {},
    renderSnapshotBanner() {},
    syncLocation() {},
  });
  renderStatus();
  return { apiStatusChip, sshStatusChip };
}

// The shape a profile-backed Linux appliance reports when only an optional
// SSH read failed (synthetic values).
const LINUX_APPLIANCE_SOURCES = {
  api: { enabled: false, ok: true, message: "Linux systems have no platform API; disks are read over SSH." },
  ssh: {
    enabled: true,
    ok: false,
    required_ok: true,
    message: "Some optional SSH reads failed (Linux SCSI transport detail). The bay map is complete.",
  },
};

test("a disabled API source is shown as not used, never OK", () => {
  for (const snapshotMode of [false, true]) {
    const { apiStatusChip } = renderStatusWith({ platform: "linux", sources: LINUX_APPLIANCE_SOURCES, snapshotMode });
    assert.doesNotMatch(apiStatusChip.textContent, /OK|TrueNAS/, `snapshotMode=${snapshotMode}`);
    assert.match(apiStatusChip.textContent, /^API: not used/);
    assert.doesNotMatch(apiStatusChip.className, /\bok\b|\berror\b/);
  }
  const live = renderStatusWith({ platform: "linux", sources: LINUX_APPLIANCE_SOURCES }).apiStatusChip;
  assert.equal(live.textContent, "API: not used");
  assert.equal(live.className, "status-chip");
  assert.equal(live.title, LINUX_APPLIANCE_SOURCES.api.message);
  const saved = renderStatusWith({ platform: "linux", sources: LINUX_APPLIANCE_SOURCES, snapshotMode: true }).apiStatusChip;
  assert.equal(saved.textContent, "API: not used at capture");
  assert.equal(saved.className, "status-chip snapshot");
});

test("the API chip names the platform's own API", () => {
  const cases = [
    ["core", { enabled: true, ok: true }, "TrueNAS API: OK", "status-chip ok"],
    ["scale", { enabled: true, ok: false }, "TrueNAS API: error", "status-chip error"],
    ["quantastor", { enabled: true, ok: true }, "QuantaStor API: OK", "status-chip ok"],
    ["esxi", { enabled: false, ok: true }, "ESXi API: not used", "status-chip"],
    ["ipmi", { enabled: false, ok: true }, "API: not used", "status-chip"],
    // Older payloads without `enabled` keep the TrueNAS behaviour.
    ["core", { ok: true }, "TrueNAS API: OK", "status-chip ok"],
  ];
  for (const [platform, api, text, className] of cases) {
    const { apiStatusChip } = renderStatusWith({
      platform,
      sources: { api: { ...api, message: "synthetic" }, ssh: { enabled: false, ok: true } },
    });
    assert.equal(apiStatusChip.textContent, text, platform);
    assert.equal(apiStatusChip.className, className, platform);
  }
  const saved = renderStatusWith({
    platform: "quantastor",
    snapshotMode: true,
    sources: { api: { enabled: true, ok: true }, ssh: { enabled: false, ok: true } },
  }).apiStatusChip;
  assert.equal(saved.textContent, "QuantaStor API: OK at capture");
  assert.match(saved.title, /No QuantaStor API is connected/);
});

test("SSH is OK, partial or error by whether a required command failed", () => {
  const cases = [
    [{ enabled: true, ok: true, required_ok: true }, "SSH: OK", "status-chip ok"],
    [{ enabled: true, ok: true }, "SSH: OK", "status-chip ok"],
    [LINUX_APPLIANCE_SOURCES.ssh, "SSH: partial", "status-chip partial"],
    [{ enabled: true, ok: false, required_ok: false, message: "StorCLI commands unavailable." }, "SSH: error", "status-chip error"],
    // No assessment (older payloads, SCALE/QuantaStor overlays) stays an error.
    [{ enabled: true, ok: false, message: "Some SSH commands failed." }, "SSH: error", "status-chip error"],
    [{ enabled: true, ok: false, required_ok: "true" }, "SSH: error", "status-chip error"],
    [{ enabled: false, ok: true }, "SSH: off", "status-chip"],
  ];
  for (const [ssh, text, className] of cases) {
    const { sshStatusChip } = renderStatusWith({ platform: "linux", sources: { api: LINUX_APPLIANCE_SOURCES.api, ssh } });
    assert.equal(sshStatusChip.textContent, text, JSON.stringify(ssh));
    assert.equal(sshStatusChip.className, className, JSON.stringify(ssh));
  }
  const partial = renderStatusWith({ platform: "linux", sources: LINUX_APPLIANCE_SOURCES }).sshStatusChip;
  // The tooltip names the optional read that failed.
  assert.match(partial.title, /Linux SCSI transport detail/);
});

test("an offline copy uses the same SSH rules", () => {
  const { snapshotSshStatus } = loadFunctions(["snapshotSshStatus"]);
  assert.deepEqual(
    { ...snapshotSshStatus(LINUX_APPLIANCE_SOURCES.ssh) },
    { className: "status-chip partial", textContent: "SSH: partial at capture" },
  );
  assert.deepEqual(
    { ...snapshotSshStatus({ enabled: true, ok: false, required_ok: false }) },
    { className: "status-chip error", textContent: "SSH: error at capture" },
  );
  const saved = renderStatusWith({ platform: "linux", sources: LINUX_APPLIANCE_SOURCES, snapshotMode: true }).sshStatusChip;
  assert.equal(saved.textContent, "SSH: partial at capture");
  assert.match(saved.title, /Linux SCSI transport detail/);
});

test("bay sources are named for the platform that reported them", () => {
  const { mappingSourceLabel } = loadFunctions(["mappingSourceLabel", "platformMappingSourceLabel"]);
  // TrueNAS keeps its wording.
  for (const platform of [undefined, "core", "scale"]) {
    for (const code of ["api", "api-slot", "api-slot-fallback"]) {
      assert.equal(mappingSourceLabel(code, platform), "TrueNAS API", `${platform} ${code}`);
    }
    assert.equal(mappingSourceLabel("device-name", platform), "SSH (device name)");
  }
  for (const code of ["api", "api-slot", "api-slot-fallback"]) {
    assert.equal(mappingSourceLabel(code, "quantastor"), "QuantaStor API", code);
    assert.equal(mappingSourceLabel(code, "ipmi"), "BMC", code);
  }
  // On a Linux host a bay number only comes from the UniFi storage tool; on
  // ESXi only from StorCLI.
  for (const code of ["api-slot", "api-slot-fallback"]) {
    assert.equal(mappingSourceLabel(code, "linux"), "SSH (UniFi storage)", code);
    assert.equal(mappingSourceLabel(code, "esxi"), "SSH (StorCLI)", code);
  }
  assert.equal(mappingSourceLabel("api", "linux"), "enclosure layout + SSH");
  assert.equal(mappingSourceLabel("api", "esxi"), "enclosure layout + SSH");
  assert.equal(mappingSourceLabel("ubntstorage", "linux"), "SSH (UniFi storage)");
  assert.equal(mappingSourceLabel("storcli-slot", "esxi"), "SSH (StorCLI)");
  assert.equal(mappingSourceLabel("bmc-slot", "esxi"), "BMC");
  for (const platform of ["linux", "esxi"]) {
    assert.equal(mappingSourceLabel("ssh", platform), "enclosure layout + SSH", platform);
    assert.equal(mappingSourceLabel("device-name", platform), "enclosure layout + SSH (device name)", platform);
  }
  assert.equal(mappingSourceLabel("ssh", "core"), "SSH (enclosure)");
  // Codes without a platform meaning keep their shared label.
  assert.equal(mappingSourceLabel("manual", "linux"), "saved assignment");
  assert.equal(mappingSourceLabel("", "linux"), "n/a");
});

test("no non-TrueNAS platform names the TrueNAS API for any bay source", () => {
  const { mappingSourceLabel } = loadFunctions(["mappingSourceLabel", "platformMappingSourceLabel"]);
  const start = APP_SOURCE.indexOf("function mappingSourceLabel(");
  const block = APP_SOURCE.slice(start, APP_SOURCE.indexOf("};", start));
  const codes = Array.from(block.matchAll(/^\s*"?([A-Za-z0-9_-]+)"?:\s*"/gm), (match) => match[1]);
  assert.ok(codes.includes("api-slot"), codes.join(","));
  for (const platform of ["linux", "esxi", "quantastor", "ipmi"]) {
    for (const code of codes) {
      assert.doesNotMatch(mappingSourceLabel(code, platform), /TrueNAS/, `${platform} ${code}`);
    }
  }
});

test("the Bays banner and the slot details row use the platform-aware label", () => {
  const { mappingHealthSourceNote } = loadFunctions([
    "mappingHealthSourceNote",
    "mappingSourceLabel",
    "platformMappingSourceLabel",
  ]);
  const unvrSlots = [0, 1, 2, 3].map((slot) => ({ slot, state: "healthy", mapping_source: "api-slot" }));
  assert.equal(mappingHealthSourceNote(unvrSlots, "linux"), "Bay positions come from: SSH (UniFi storage).");
  assert.equal(mappingHealthSourceNote(unvrSlots, "core"), "Bay positions come from: TrueNAS API.");
  assert.equal(mappingHealthSourceNote(unvrSlots), "Bay positions come from: TrueNAS API.");
  const proSlots = [
    { slot: 0, state: "healthy", mapping_source: "api-slot" },
    { slot: 1, state: "empty", mapping_source: "ubntstorage" },
  ];
  assert.equal(mappingHealthSourceNote(proSlots, "linux"), "Bay positions come from: SSH (UniFi storage).");
  // Both callers pass the selected system's platform; the offline copy runs
  // the same code with the platform it saved.
  assert.match(APP_SOURCE, /kvRowIfMeaningful\("Bay reported by", mappingSourceLabel\(slot\.mapping_source, currentPlatform\(\)\)\)/);
  assert.match(APP_SOURCE, /mappingHealthSourceNote\(healthScope\.slots, currentPlatform\(\)\)/);
});

test("a mount every bay shares is replaced on the tile by a per-disk line", () => {
  const { sharedMountPoolName, liveSlotSecondaryLabel } = loadFunctions([
    "sharedMountPoolName",
    "liveSlotSecondaryLabel",
    "stateLabel",
  ]);
  const disk = (slot, extra = {}) => ({
    slot,
    state: "healthy",
    device_name: `sd${String.fromCharCode(97 + slot)}`,
    serial: `SYNTHSERIAL00${slot}AB${slot}`,
    model: "Synthetic Disk 8TB",
    pool_name: "/volume/synthetic-volume-0000",
    ...extra,
  });
  const unvr = [0, 1, 2, 3].map((slot) => disk(slot));
  const shared = sharedMountPoolName(unvr, "linux");
  assert.equal(shared, "/volume/synthetic-volume-0000");
  const lines = unvr.map((slot) => liveSlotSecondaryLabel(slot, shared));
  assert.equal(new Set(lines).size, 4, lines.join(" | "));
  assert.equal(lines[0], "S/N …000AB0");
  assert.ok(lines.every((line) => !line.includes("/volume/")), lines.join(" | "));
  // No serial: the model names the disk instead.
  assert.equal(liveSlotSecondaryLabel(disk(0, { serial: null }), shared), "Synthetic Disk 8TB");
  // An empty bay keeps its state.
  assert.equal(liveSlotSecondaryLabel({ slot: 4, state: "empty" }, shared), "Empty");
  assert.equal(sharedMountPoolName(unvr, "esxi"), "/volume/synthetic-volume-0000");

  // TrueNAS pools keep the pool name on every tile.
  assert.equal(sharedMountPoolName(unvr, "core"), null);
  assert.equal(liveSlotSecondaryLabel(unvr[0], sharedMountPoolName(unvr, "core")), "/volume/synthetic-volume-0000");
  // Different mounts already tell bays apart; one disk shares nothing.
  const mixed = [disk(0), disk(1, { pool_name: "/volume/other" })];
  assert.equal(sharedMountPoolName(mixed, "linux"), null);
  assert.equal(sharedMountPoolName([disk(0), { slot: 1, state: "empty" }], "linux"), null);
  assert.equal(liveSlotSecondaryLabel(mixed[1], null), "/volume/other");

  assert.match(APP_SOURCE, /buildLiveSlotTileMarkup\(slot, sharedMount\)/);
});
