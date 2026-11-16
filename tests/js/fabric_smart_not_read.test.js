"use strict";

// Storage Fabric SMART line, SMART card and the disk's Health fact read the
// summary's unread_fields: a field the SMART JSON reported in a form that
// could not be read says "not read" instead of being left out like an absent
// one, and it is not replaced by an inventory value.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

function functionSource(name) {
  const start = FABRIC_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = FABRIC_SOURCE.indexOf("{", FABRIC_SOURCE.indexOf(")", start));
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < FABRIC_SOURCE.length; index += 1) {
    const character = FABRIC_SOURCE[index];
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
      if (depth === 0) return FABRIC_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

const NAMES = [
  "escapeHtml", "formatInteger", "formatSmartBytes", "formatSmartPowerOn", "smartPowerOnNotRead",
  "formatSmartPowerOnFact", "smartFieldNotRead", "smartSummaryLine", "renderSmartSummaryCard", "smartHealthFact",
];
const sandbox = vm.createContext({ state: { smartRequests: {} }, smartCacheKey: () => "k", current: null });
sandbox.smartSummaryForSlot = () => sandbox.current;
vm.runInContext(`${NAMES.map(functionSource).join("\n")}\nthis.loaded = { ${NAMES.join(", ")} };`, sandbox);
const fabric = sandbox.loaded;

function summary(fields, unread = []) {
  return {
    available: true, message: null, smart_health_status: null, temperature_c: null, power_on_hours: null,
    power_on_days: null, last_test_type: null, last_test_status: null, bytes_read: null, bytes_written: null,
    media_errors: null, non_medium_errors: null, interface_crc_errors: null, transport_protocol: null,
    negotiated_link_rate: null, sas_address: null, firmware_version: null, ...fields, unread_fields: unread,
  };
}

function card(value, slot = {}) {
  sandbox.current = value;
  return fabric.renderSmartSummaryCard(1, slot).replace(/\s+/g, " ");
}

test("the SMART line names an unread health or temperature", () => {
  assert.equal(fabric.smartSummaryLine(summary({ smart_health_status: "PASSED", power_on_days: 2 }, ["temperature_c"])),
    "PASSED / temperature not read / 2 d");
  assert.equal(fabric.smartSummaryLine(summary({ temperature_c: 30 }, ["smart_health_status"])), "health not read / 30 C");
  // Controls: absent fields are left out, readings unchanged.
  assert.equal(fabric.smartSummaryLine(summary({ smart_health_status: "PASSED", power_on_days: 2 })), "PASSED / 2 d");
  assert.equal(fabric.smartSummaryLine(summary({ smart_health_status: "PASSED", temperature_c: 30, power_on_days: 2 },
    ["interface_crc_errors"])), "PASSED / 30 C / 2 d");
});

test("the SMART card shows not read for each unread field it uses", () => {
  const shown = card(summary({ firmware_version: "FW01" }, [
    "smart_health_status", "temperature_c", "last_test_type", "bytes_read", "interface_crc_errors",
    "transport_protocol", "media_errors",
  ]), { transport_protocol: "SAS", sas_address: "0x5000c500aa" });
  for (const fact of [
    "<em>Health</em>not read", "<em>Temp</em>not read", "<em>Last test</em>not read", "<em>I/O totals</em>R not read",
    "<em>Errors</em>media not read / CRC not read", "<em>Transport</em>not read / 0x5000c500aa",
  ]) {
    assert.ok(shown.includes(fact), fact);
  }
  const firmware = card(summary({ temperature_c: 30 }, ["firmware_version"]));
  assert.ok(firmware.includes("<em>Firmware</em>not read"));
});

test("SMART card controls: absent omitted, zero and readings kept", () => {
  const shown = card(summary({
    smart_health_status: "PASSED", temperature_c: 30, interface_crc_errors: 0, media_errors: 0, bytes_read: 2000,
    firmware_version: "FW01",
  }), { transport_protocol: "SAS" });
  assert.ok(!shown.includes("not read"));
  for (const fact of ["<em>Health</em>PASSED", "<em>Temp</em>30 C", "<em>Errors</em>media 0 / CRC 0",
    "<em>I/O totals</em>R 2.00 KB", "<em>Transport</em>SAS"]) {
    assert.ok(shown.includes(fact), fact);
  }
  assert.ok(!card(summary({ temperature_c: 30 })).includes("<em>Health</em>"));
});

test("the disk Health fact does not replace an unread SMART value with the inventory", () => {
  const slot = { health: "OK", temperature_c: 41 };
  assert.equal(fabric.smartHealthFact(summary({}, ["smart_health_status", "temperature_c"]), slot),
    "SMART health not read / temperature not read");
  assert.equal(fabric.smartHealthFact(summary({ smart_health_status: "PASSED", temperature_c: 30 }), slot), "PASSED / 30 C");
  assert.equal(fabric.smartHealthFact(summary({}), slot), "OK / 41 C");
  assert.equal(fabric.smartHealthFact(null, { state: "matched" }), "matched");
});
