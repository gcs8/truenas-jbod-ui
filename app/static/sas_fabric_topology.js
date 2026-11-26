/* Storage Fabric Topology: payload model and evidence. */
(function () {
  const context = {};
"use strict";

const NOT_COLLECTED = "HBA and expander hops are not collected on this platform.";

const NOT_SHOWN = "Pools and vdevs are not drawn here; Impact Map and Disk Path show them. "
    + "The CORE multipath groupings (shown instead as path marks on each bay) "
    + "and PHY error counters (Disk Path) are not drawn either.";

const NOT_SHOWN_COUNTERS = "Pools and vdevs are not drawn here; Impact Map and Disk Path show them. "
    + "The CORE multipath groupings are shown as the port marks on each bay. "
    + "PHY error counters are in each hover card and in the inspector; Disk Path shows them too.";

const NOT_SHOWN_STORAGE = "Pools and vdevs are not drawn here; Impact Map and Disk Path show them.";

const NOT_SHOWN_SHARED = "Pools and vdevs are not drawn here; Impact Map and Disk Path show them. "
    + "Both head ends are drawn on the one backplane they share, whichever of them this page is on.";

const LOCATION_UNKNOWN = "physical location unavailable";

const BAY_DETAIL_LIMIT = 96;

const CELLS_PER_ROW = 24;

const CELL = 34;

const CELL_BOX = 32;

const MARK = 8;

const MARK_PITCH = 9;

const NODE_W = 184;

const NODE_H = 62;

const TEXT_W = 8;

const LINE_H = 16;

const NODE_MIN_CHARS = 21;

const NODE_MAX_CHARS = 37;

const TIER_LABEL_CHARS = 16;

const TIER_NOTE_CHARS = 20;

const BAND_CHARS = 30;

const H_GAP = 48;

const LEFT = 168;

const MARGIN = 16;

const CHAR_W = 7;

const SUMMARY_W = 300;

const PHYS_CELL_W = 40;

const PHYS_CELL_H = 40;

const PHYS_CELL_MAX = 64;

const PHYS_CELL_MIN = 36;

const PHYS_CELL_NAMED = 60;

const PHYS_CELL_TALL = 54;

const PHYS_GAP = 2;

const GROUP_GAP = 10;

const STACK_GAP = 6;

const CAPTION_H = 20;

const RAIL_CLEAR = 12;

const RAIL_STEP = 16;

const STACK_ROOM = 138;

const DOWN_STEP = 10;

const BUS_STEP = 6;

const BUS_PAD = 7;

const CHIP_GAP = 24;

const LEFT_BOARD = 140;

const BOARD_ROOM = 158;

const PHY_COUNTERS = [
    ["invalid_dword_count", "invalid dword"],
    ["running_disparity_error_count", "running disparity"],
    ["loss_of_dword_synchronization_count", "loss of dword sync"],
    ["phy_reset_problem_count", "PHY reset problem"],
  ];

const NOT_READ = "not read";

const PROVISIONAL_PHY_THRESHOLDS = {
    invalid_dword_count: { warning: 1, critical: 100 },
    running_disparity_error_count: { warning: 1, critical: 100 },
    loss_of_dword_synchronization_count: { warning: 1, critical: 10 },
    phy_reset_problem_count: { warning: 1, critical: 10 },
  };

const THRESHOLD_NOTE = `Provisional thresholds, per PHY: ${PHY_COUNTERS.map(([key, label]) => `${label} warning from ${PROVISIONAL_PHY_THRESHOLDS[key].warning}, critical from ${PROVISIONAL_PHY_THRESHOLDS[key].critical}`).join("; ")}.`;

const TREND_NOTE = "Trend: not read (one reading). A count rising between readings would be critical.";

const TIER_WORDS = { critical: "Critical", warning: "Warning" };

function worse(...tiers) {
    return tiers.includes("critical") ? "critical" : tiers.includes("warning") ? "warning" : "";
  }

function phyTier(row) {
    if (!row || !row.counters) return "";
    return worse(...PHY_COUNTERS.map(([key]) => {
      const count = row.counters[key];
      const limit = PROVISIONAL_PHY_THRESHOLDS[key];
      return count === null || count < limit.warning ? "" : count >= limit.critical ? "critical" : "warning";
    }));
  }

const TOGGLE_H = 26;

const SCROLL_BORDER = 1;

const TRACK = 10;

const GAP_PAD = 10;

const LABEL_GAP = 8;

const LABEL_CLEAR = 8;

const JUNCTION = 14;

const NAME_LIMIT = 4;

const SIMPLIFY_ABOVE = 512;

const STUB_GROUP_ABOVE = 3;

const LANE_LINKS = new Set(["controller-expander", "expander-expander"]);

const HIDDEN_KINDS = new Set(["pool", "vdev", "backplane"]);

const MEMBER_HEALTH = "member-health-not-transport";

const CACHE_FRESH = new Set(["hit", "hit-after-wait", "miss", "forced-refresh"]);

const STATE_UNREAD = "present, state not read";

// A path state the payload did not read, on any platform: no state, CORE's
// "unknown" or a `not_read` naming the state, or the Linux transport's text.
function isUnreadState(state) {
    const value = String(state?.state ?? "").trim();
    const notRead = [...list(state?.not_read), ...list(state?.raw?.not_read)];
    return !value || value.toLowerCase() === "unknown" || value === STATE_UNREAD || notRead.includes("state");
  }

const PATH_STATE_TONES = {
    active: "active",
    online: "active",
    optimal: "active",
    passive: "standby",
    standby: "standby",
    fail: "fault",
    failed: "fault",
    fault: "fault",
    missing: "missing",
    unknown: "unknown",
    [STATE_UNREAD]: "unknown",
    mapped: "mapped",
    unmapped: "unmapped",
    present: "member",
    mixed: "member",
  };

const PATH_CAPTIONS = {
    block: "block device",
    nvme: "NVMe device",
    mdadm: "mdadm member",
    "scale-storage": "SCALE storage path",
    "storcli-member": "RAID member",
    "esxi-storage": "ESXi storage path",
    "bmc-slot": "BMC slot",
    quantastor: "Quantastor path",
    "quantastor-ha": "Quantastor HA path",
  };

function list(value) {
    return Array.isArray(value) ? value : [];
  }

function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

function own(table, key) {
    return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
  }

function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

function token(value) {
    return String(value ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "unknown";
  }

function finite(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

function decimal(value) {
    if (typeof value === "number") {
      return Number.isSafeInteger(value) && value >= 0 ? value : null;
    }
    return typeof value === "string" && /^[0-9]{1,9}$/.test(value) ? Number(value) : null;
  }

function naturalCompare(left, right) {
    return String(left ?? "").localeCompare(String(right ?? ""), undefined, { numeric: true, sensitivity: "base" });
  }

function plural(count, one, many = `${one}s`) {
    return `${count} ${count === 1 ? one : many}`;
  }

function named(values) {
    const shown = values.slice(0, NAME_LIMIT).join(", ");
    return values.length > NAME_LIMIT ? `${shown} and ${values.length - NAME_LIMIT} more` : shown;
  }

const PLATFORM_WORDS = { quantastor: "QuantaStor", scale: "TrueNAS SCALE", core: "TrueNAS CORE", linux: "Linux", esxi: "ESXi" };

const NAMED_PLATFORMS = new Set(["core", "scale", "linux", "quantastor", "esxi", "ipmi"]);

const KIND_PLATFORMS = {
    core_mpr: ["core"],
    linux_ses: ["scale", "linux"],
    storage_scale: ["scale"],
    storage_linux: ["linux"],
    storage_quantastor: ["quantastor"],
    storage_esxi: ["esxi"],
    storage_bmc: ["ipmi"],
  };

const GENERIC_KIND = "storage_generic";

function kindMatchesPlatform(kind, platform) {
    if (!platform) return true;
    if (kind === GENERIC_KIND) return !NAMED_PLATFORMS.has(platform);
    return own(KIND_PLATFORMS, kind).includes(platform);
  }

function fabricContract(fabric) {
    const named = typeof fabric?.raw?.fabric_kind === "string" ? fabric.raw.fabric_kind : "";
    const platform = typeof fabric?.platform === "string" ? fabric.platform : "";
    const noClaim = "it draws the records the payload carries except pools, vdevs and backplane zones, which this view does not draw, and makes no claim about what was or was not collected";
    if (!named) {
      if (platform === "core") return { kind: "core_sas", contract: "core", notice: "" };
      return { kind: "unknown", contract: "unknown", notice: `The payload names no fabric kind${platform ? ` (platform "${platform}")` : ""}: ${noClaim}.` };
    }
    const core = named === "core_mpr";
    if (!core && named !== GENERIC_KIND && !own(KIND_PLATFORMS, named)) {
      return { kind: named, contract: "unknown", notice: `The fabric kind "${named}" is not one this view knows: ${noClaim}.` };
    }
    if (!kindMatchesPlatform(named, platform)) {
      return { kind: named, contract: "unknown", notice: `The fabric kind "${named}" does not match platform "${platform}": ${noClaim}.` };
    }
    return { kind: named, contract: core ? "core" : "storage", notice: "" };
  }

function readState(fabric, domain) {
    const counts = fabric?.raw?.controller_read?.counts;
    if (!isRecord(counts) || counts[domain] === undefined || counts[domain] === null) {
      return "unknown";
    }
    const value = counts[domain];
    return value === "complete" || value === "partial" ? value : "not_read";
  }

function farEnd(lanes) {
    if (!lanes.targetPhys.length) return lanes.basis === "sysfs_parent_port_membership" ? "↔ far end not read" : "↔ PHY ?";
    if (!lanes.targetBasis) return `↔ PHY ${phyRanges(lanes.targetPhys)}`;
    return `↔ PHY ${phyRanges(lanes.targetPhys)}${lanes.targetBasis === "inferred" ? " inferred" : ""}`;
  }

function phyRanges(values) {
    const numbers = list(values).filter((value) => Number.isInteger(value));
    const ranges = [];
    let start = null;
    let previous = null;
    for (const number of numbers) {
      if (start !== null && number === previous + 1) {
        previous = number;
        continue;
      }
      if (start !== null) {
        ranges.push(start === previous ? `${start}` : `${start}–${previous}`);
      }
      start = number;
      previous = number;
    }
    if (start !== null) {
      ranges.push(start === previous ? `${start}` : `${start}–${previous}`);
    }
    return ranges.join(", ");
  }

function laneRate(lanes) {
    if (lanes.rate !== null) return formatRate(lanes.rate);
    const rates = lanes.rows.map((lane) => lane.rate);
    if (!rates.length || rates.length !== lanes.count || rates.some((rate) => rate === null)) return formatRate(null);
    const [low, high] = [Math.min(...rates), Math.max(...rates)];
    return low === high ? formatRate(low) : `${low}–${high} Gb/s`;
  }

function formatRate(rate) {
    return rate === null ? "rate not reported" : `${Number(rate)} Gb/s`;
  }

function nodeLabel(item) {
    return String(item?.alias || item?.display_label || item?.label || item?.id || "");
  }

function clip(text, width) {
    const value = String(text ?? "");
    const max = Math.max(4, Math.floor((width - 16) / CHAR_W));
    return value.length > max ? `${value.slice(0, max - 1)}…` : value;
  }

function bayOccupancy(metrics) {
    const value = isRecord(metrics) ? metrics.present : undefined;
    return value === true ? "yes" : value === false ? "no" : "unknown";
  }

function locationKnown(...sources) {
    return !sources.some((item) => isRecord(item) && (item.physical_location_known === false || item.virtual_enclosure === true));
  }

function readPayload(fabric) {
    const problems = [];
    if (!isRecord(fabric)) {
      problems.push("the fabric payload is not an object");
      return { nodes: [], links: [], traces: [], problems, nodesReadable: false };
    }
    const collection = (name, one, many) => {
      if (!Array.isArray(fabric[name])) {
        problems.push(`${name} is not a list`);
        return { items: [], readable: false };
      }
      const items = fabric[name].filter((item) => isRecord(item) && typeof item.id === "string" && item.id);
      const skipped = fabric[name].length - items.length;
      if (skipped) {
        problems.push(`${plural(skipped, `${one} entry`, `${many} entries`)} without an object and an id ${skipped === 1 ? "was" : "were"} skipped`);
      }
      return { items, readable: !skipped };
    };
    const nodes = collection("nodes", "node", "node");
    const links = collection("links", "link", "link");
    const traces = collection("traces", "trace", "trace");
    return { nodes: nodes.items, links: links.items, traces: traces.items, problems, nodesReadable: nodes.readable };
  }

function inventoryBySlot(fabric, inventory) {
    const slots = new Map();
    if (!isRecord(inventory) || !isRecord(fabric)) return slots;
    const differs = (left, right) => Boolean(left && right && left !== right);
    if (differs(fabric.system_id, inventory.selected_system_id) || differs(fabric.selected_enclosure_id, inventory.selected_enclosure_id)) {
      return slots;
    }
    for (const slot of list(inventory.slots)) {
      if (isRecord(slot) && Number.isInteger(slot.slot) && !slots.has(slot.slot)) slots.set(slot.slot, slot);
    }
    return slots;
  }

function searchTerms(item, trace, slot) {
    const metrics = isRecord(item.metrics) ? item.metrics : {};
    const raw = isRecord(item.raw) ? item.raw : {};
    const traced = isRecord(trace?.metrics) ? trace.metrics : {};
    const multipath = isRecord(slot?.multipath) ? slot.multipath : {};
    const values = [
      metrics.device_name, traced.device_name, metrics.serial, raw.serial, metrics.sas_address, raw.sas_address,
      metrics.logical_unit_id, metrics.gptid,
      ...list(traced.path_states).map((state) => state?.device_name),
      ...list(traced.mpr_devices).flatMap((device) => [device?.sas_address, device?.member_device_name]),
      slot?.serial, slot?.device_name, slot?.sas_address, slot?.logical_unit_id, slot?.gptid, slot?.persistent_id_label,
      multipath.device_name, ...list(multipath.members).map((member) => member?.device_name),
    ];
    return [...new Set(values.filter((value) => typeof value === "string" && value).map((value) => value.toLowerCase()))];
  }

function identityTerms(item, trace, slot) {
    const metrics = isRecord(item.metrics) ? item.metrics : {};
    const raw = isRecord(item.raw) ? item.raw : {};
    const values = [
      metrics.serial, raw.serial, metrics.sas_address, raw.sas_address, metrics.logical_unit_id,
      ...list(trace?.metrics?.mpr_devices).map((device) => device?.sas_address),
      slot?.serial, slot?.sas_address, slot?.logical_unit_id,
    ];
    return [...new Set(values.filter((value) => typeof value === "string" && value).map((value) => value.toLowerCase()))];
  }

// The disk port addresses a bay's own records report, and the enclosures
// they place it in: its node, its trace's records and its inventory slot.
// A field a record does not read adds nothing.
function reportedPlace(item, trace, slot) {
    const records = [item.metrics, item.raw, trace?.metrics, slot, slot?.raw_status,
      ...[trace?.metrics?.path_states, trace?.metrics?.sas_transport_ports, trace?.metrics?.mpr_devices].flatMap(list)].filter(isRecord);
    const values = (key) => [...new Set(records.map((record) => sasAddressOf(record[key])).filter(Boolean))].sort();
    return { addresses: values("sas_address"), enclosures: values("enclosure_id") };
  }

function bayNumberOf(label, inventoryLabel) {
    const labelled = /^Bay\s+([0-9]+)$/.exec(String(label || ""));
    if (labelled) return labelled[1];
    const fallback = String(inventoryLabel ?? "");
    return /^[0-9]+$/.test(fallback) ? fallback : "";
  }

function sasRate(value) {
    if (typeof value === "number") return Number.isFinite(value) && value > 0 ? value : null;
    return typeof value === "string" && /^[0-9]{1,3}(\.[0-9]{1,2})?$/.test(value) && Number(value) > 0 ? Number(value) : null;
  }

function text(value) {
    return typeof value === "string" ? value : "";
  }

function sasAddressOf(value) {
    const found = text(value).trim().toLowerCase().replace(/^0x/, "");
    return /^[0-9a-f]{8,32}$/.test(found) && !/^0+$/.test(found) ? found : "";
  }

let tailDigits = 4;

function tail(address) {
    return `…${String(address).slice(-tailDigits)}`;
  }

function addressTail(fabric) {
    const all = new Set();
    for (const item of list(fabric?.nodes)) {
      if (!isRecord(item)) continue;
      if (item.kind === "expander") all.add(sasAddressOf(item.raw_id) || sasAddressOf(item.raw?.sas_address));
      if (item.kind === "controller") all.add(sasAddressOf(item.raw?.sas_address));
    }
    all.delete("");
    let digits = 4;
    while (digits < 16 && new Set([...all].map((address) => address.slice(-digits))).size < all.size) digits += 1;
    return digits;
  }

function protocolOf(...words) {
    for (const word of words) {
      const value = text(word).toUpperCase();
      if (/\bSATA\b|\bSTP\b/.test(value)) return "SATA";
      if (/\bSAS\b|\bSSP\b/.test(value)) return "SAS";
    }
    return "";
  }

function phyFacts(row) {
    const measured = row.counter_state === "measured";
    // Each counter is read or not on its own: a row the source measured has
    // all four; a row that names the counters it did not read has the rest.
    const named = Array.isArray(row.not_read) ? row.not_read : null;
    const counters = {};
    for (const [key] of PHY_COUNTERS) {
      const value = row[key];
      counters[key] = Number.isSafeInteger(value) && value >= 0 && (measured || (named !== null && !named.includes(key))) ? value : null;
    }
    return {
      phy: decimal(row.phy),
      remote: decimal(row.remote_phy),
      handle: text(row.dev_handle),
      device: text(row.device),
      rate: sasRate(row.speed),
      min: sasRate(row.min),
      max: sasRate(row.max),
      counters,
      counterState: text(row.counter_state) || NOT_READ,
      counterReason: text(row.counter_reason),
      errors: Object.values(counters).reduce((sum, value) => sum + (value || 0), 0),
      measured: Object.values(counters).every((value) => value !== null),
    };
  }

function sasIndex(fabric) {
    const expanders = new Map();
    const controllers = new Map();
    for (const item of list(fabric?.expanders)) {
      if (!isRecord(item) || typeof item.id !== "string" || expanders.has(item.id)) continue;
      const phys = list(item.phys).filter(isRecord).map(phyFacts).filter((row) => row.phy !== null);
      const log = isRecord(item.phy_error_counters) ? item.phy_error_counters : null;
      expanders.set(item.id, {
        id: item.id,
        address: text(item.sas_address),
        handle: text(item.dev_handle),
        controllerId: text(item.controller_id),
        // The handle of the device this expander reports as its parent.
        parent: text(item.parent),
        product: text(item.product),
        reportedLevel: text(item.reported_level),
        enclosureAddress: text(item.enclosure_sas_address),
        phys,
        // Every row of each PHY number: two rows claiming one PHY are both
        // kept, never one replaced by the other. Only a number with one row
        // is a PHY that can be looked up by number.
        rowsByPhy: phys.reduce((map, row) => map.set(row.phy, [...(map.get(row.phy) || []), row]), new Map()),
        byPhy: new Map(phys.filter((row, index) => phys.findIndex((other) => other.phy === row.phy) === index && phys.filter((other) => other.phy === row.phy).length === 1).map((row) => [row.phy, row])),
        total: decimal(item.num_phys),
        linked: decimal(item.linked_phys),
        attached: isRecord(item.device_counts) ? Object.entries(item.device_counts).filter(([, count]) => Number.isSafeInteger(count) && count >= 0) : [],
        log: log ? {
          state: text(log.state),
          reason: text(log.reason) || text(log.message),
          source: text(log.source),
          readAt: text(log.read_at),
          measured: decimal(log.measured_phys),
          withErrors: decimal(log.phys_with_errors),
        } : null,
      });
    }
    for (const item of list(fabric?.controllers)) {
      if (!isRecord(item) || typeof item.id !== "string" || controllers.has(item.id)) continue;
      controllers.set(item.id, {
        id: item.id,
        board: text(item.board),
        chip: text(item.chip),
        firmware: text(item.firmware),
        temperature: text(item.temperature),
        slot: text(item.pcie_slot),
        pci: text(item.pci_address),
        pathCounts: isRecord(item.path_counts) ? Object.entries(item.path_counts).filter(([, count]) => Number.isSafeInteger(count) && count >= 0) : [],
        sasAddress: text(item.sas_address),
        // An HBA's PHY rows carry error counters only where the source reads them.
        phys: list(item.phy_rows).filter(isRecord).map((row) => ({
          ...(row.counter_state ? phyFacts(row) : { counters: null, measured: false, errors: 0 }),
          phy: decimal(row.phy),
          handle: text(row.device_handle),
          // The HBA's own handle on this row, which the far end names back.
          // (0000 is the utility's "no handle").
          ownHandle: /^0+$/.test(text(row.controller_handle)) ? "" : text(row.controller_handle),
          // What the builder found the handle names across the whole unit,
          // before selection: "ambiguous", "mismatch", the one device's kind
          // ("end_device", "sep", "other_device"), or nothing.
          handleState: isRecord(item.device_handle_states) ? text(item.device_handle_states[String(row.phy)]) : "",
          device: text(row.device),
          disabled: row.disabled === "Y",
          rate: sasRate(row.speed),
          min: sasRate(row.min),
          max: sasRate(row.max),
        })).filter((row) => row.phy !== null),
      });
    }
    // An HBA's PHY table is looked up as an expander's is: a disk attached
    // directly to an HBA is joined on that HBA's own PHY.
    for (const record of controllers.values()) {
      record.rowsByPhy = record.phys.reduce((map, row) => map.set(row.phy, [...(map.get(row.phy) || []), row]), new Map());
      record.byPhy = new Map([...record.rowsByPhy].filter(([, rows]) => rows.length === 1).map(([phy, rows]) => [phy, rows[0]]));
      record.handle = "";
    }
    // Each HBA's enclosure processors by handle, as its enclosure list
    // reports them: a handle any record carries is a device of that HBA.
    const seps = new Map();
    for (const item of list(fabric?.nodes)) {
      if (!isRecord(item) || item.kind !== "mpr-enclosure" || !isRecord(item.raw) || !text(item.raw.sep_handle) || typeof item.controller_id !== "string") continue;
      if (!seps.has(item.controller_id)) seps.set(item.controller_id, new Map());
      const held = seps.get(item.controller_id);
      held.set(text(item.raw.sep_handle), [...(held.get(text(item.raw.sep_handle)) || []), item.id]);
    }
    // Which of each HBA's lists were read, and every handle its unit's read
    // records show naming more than one device: as the HBA carries them and
    // as the payload's list states give them by the HBA's own name (never its
    // label). A list is read only where every source that reports it says so.
    const listed = isRecord(fabric?.raw?.controller_read?.lists) ? fabric.raw.controller_read.lists : {};
    for (const item of list(fabric?.controllers)) {
      const record = isRecord(item) && controllers.get(item.id);
      if (!record || record.lists !== undefined) continue;
      const sources = [item.mpr_lists, listed[text(item.name)]].filter(isRecord);
      record.lists = sources.length ? Object.fromEntries([...new Set(sources.flatMap(Object.keys))].map((key) => [key, sources.every((source) => !(key in source) || ["read", "empty"].includes(source[key])) ? "read" : "not_read"])) : null;
      record.ambiguousHandles = new Set(list(item.ambiguous_device_handles).map(text).filter(Boolean));
    }
    return { expanders, controllers, seps };
  }

// The device type a row is given when its PHY name is on more than one row
// of its owner's table: a census record naming it identifies neither row.
const NAME_NOT_UNIQUE = "PHY name on more than one row";

// Every attached-device type a source prints, with what it says is on the
// PHY: a disk (an end device), an initiator (an HBA), an expander, a cable
// end the payload inferred, the enclosure processor, nothing, or a type
// whose role the source did not read. One table for the builder and the
// checks: every type mprutil prints and every expander and initiator type
// the CORE builder reads is in it (a test holds the Python sets to it). A
// type not in it has its role not read.
const DEVICE_KINDS = new Map([
  ["SAS Target", "disk"], ["SATA Target", "disk"], ["STP Target", "disk"], ["ATAPI Target", "disk"],
  ["End device (disk)", "disk"], ["End device, no block device", "disk"],
  ["SMP Target", "expander"], ["Edge Expander", "expander"], ["Fanout Expander", "expander"], ["Expander", "expander"],
  ["SAS Initiator", "initiator"], ["SATA Initiator", "sata initiator"],
  ["Cable far end (inferred)", "cable"],
  ["SEP Target", "sep"],
  ["No Device", "none"],
  ["Unknown Device", "unread"], ["Attachment not read", "unread"], [NAME_NOT_UNIQUE, "unread"],
]);
// What each kind is, as a role: a disk, a link (an initiator, an expander or
// an inferred cable end), the enclosure processor, nothing attached.
const KIND_ROLES = { disk: "disk", initiator: "link", "sata initiator": "link", expander: "link", cable: "link", sep: "sep", none: "none", unread: "unread" };
// Which kinds can be at each end of each cable, as the CORE builder reads
// them: the child end of a cable from an HBA sees the HBA (an initiator), the
// child end of a cable from an expander sees an expander; the parent end of
// a cascade sees the child expander. An HBA's own row may name either. A
// SATA initiator ends no SAS cable.
const CABLE_ENDS = {
  "controller-expander": { near: ["initiator", "expander", "cable"], far: ["initiator", "cable"] },
  "expander-expander": { near: ["expander", "cable"], far: ["expander", "cable"] },
};

function deviceKind(device) {
    const value = text(device);
    return value ? DEVICE_KINDS.get(value) || "unknown" : "";
  }

function deviceRole(device) {
    const kind = deviceKind(device);
    return kind ? KIND_ROLES[kind] || "unknown" : "none";
  }

function cableEndAccepts(linkKind, side, device) {
    const kind = DEVICE_KINDS.has(device) ? DEVICE_KINDS.get(device) : device;
    return list(CABLE_ENDS[linkKind]?.[side]).includes(kind);
  }

// The transport census as the checks read it: each record's PHYs are the
// numbers it lists and the names it lists that resolve to exactly one row of
// its owner's table, so a record naming its PHY only by name still names it.
function censusOf(fabric) {
    const byName = new Map();
    // A record naming a PHY by a name on more than one row of its owner's
    // table identifies nothing: it is unread evidence, never the first row.
    for (const item of list(fabric?.nodes)) {
      if (!isRecord(item) || typeof item.id !== "string" || !isRecord(item.raw)) continue;
      for (const row of list(item.kind === "controller" ? item.raw.phy_rows : item.raw.phys).filter(isRecord)) {
        if (typeof row.name !== "string") continue;
        const key = `${item.id}|${row.name}`;
        byName.set(key, [...(byName.get(key) || []), decimal(row.phy)]);
      }
    }
    return list(fabric?.raw?.sas_transport_ports).filter(isRecord).map((port) => {
      const named = list(port.phy_names).map((name) => byName.get(`${port.owner_id}|${name}`)).filter((found) => found?.length === 1 && Number.isInteger(found[0])).map((found) => found[0]);
      const ambiguous = list(port.phy_names).filter((name) => (byName.get(`${port.owner_id}|${name}`) || []).length > 1);
      const phys = [...new Set([...list(port.phys).filter((phy) => Number.isInteger(phy)), ...named])];
      return ambiguous.length ? { ...port, phys, ambiguous, state: port.state === "read" ? "ambiguous" : port.state } : { ...port, phys };
    });
  }

// Two roles for one PHY, said the same whichever record came first.
function bothRoles(held, device) {
    return [...new Set([...held.split(" / "), device])].sort().join(" / ");
  }

function joinPort(sas, device) {
    const ids = list(device.expander_ids).filter((id) => typeof id === "string");
    const found = ids.map((id) => sas.expanders.get(id) || sas.controllers.get(id)).filter(Boolean);
    const parent = text(device.parent);
    const expander = found.find((item) => parent && item.handle === parent) || (found.length === 1 && !parent ? found[0] : null);
    // A source without handles names the expander PHY itself.
    if (expander && Number.isInteger(device.expander_phy)) {
      const row = expander.byPhy.get(device.expander_phy);
      if (list(expander.rowsByPhy.get(device.expander_phy)).length > 1) return { expanderId: expander.id, phy: null, why: `more than one row of ${sas.controllers.has(expander.id) ? "the HBA's" : "the expander's"} PHY table has the PHY number this port names` };
      const table = sas.controllers.has(expander.id) ? "the HBA's PHY table" : "the expander's PHY table";
      return row ? { expanderId: expander.id, phy: row, why: "", basis: text(device.phy_basis) } : { expanderId: expander.id, phy: null, why: `${table} has no row for the PHY this port names` };
    }
    if (expander && device.expander_phy === null && !text(device.handle)) return { expanderId: expander.id, phy: null, why: "the source does not say which expander PHY this port is on" };
    if (!expander) return { expanderId: ids.length === 1 ? ids[0] : "", phy: null, why: "the payload has no PHY table for the port's expander" };
    const handle = text(device.handle);
    const rows = handle ? expander.phys.filter((row) => row.handle === handle) : [];
    if (rows.length !== 1) return { expanderId: expander.id, phy: null, why: rows.length ? "more than one expander PHY reports this device handle" : "no expander PHY reports this device handle" };
    return { expanderId: expander.id, phy: rows[0], why: "" };
  }

function rateText(rate) {
    return rate === null ? `rate ${NOT_READ}` : `${rate} Gb/s`;
  }

function counterText(row) {
    const read = PHY_COUNTERS.filter(([key]) => row.counters?.[key]).map(([key, label]) => `${row.counters[key]} ${label}`);
    // A counter not read is named, never shown as a value.
    const unread = row.measured || !row.counters ? [] : PHY_COUNTERS.filter(([key]) => row.counters[key] === null).map(([, label]) => label);
    return [...read, ...(unread.length ? [`${unread.join(", ")} ${NOT_READ}`] : [])].join(", ");
  }

function errorDigest(rows) {
    const bad = rows.filter((row) => row.errors > 0);
    if (bad.length <= 3) return bad.map((row) => `PHY ${row.phy}: ${counterText(row)}`).join("; ");
    // A range spans only the counts that were read; the PHYs that did not
    // read a counter are counted and named, never taken as 0.
    const spans = PHY_COUNTERS.map(([key, label]) => {
      const counts = bad.map((row) => row.counters[key]).filter((value) => value !== null && value !== undefined);
      const lost = bad.length - counts.length;
      if (!counts.length || !Math.max(...counts)) return "";
      const [low, high] = [Math.min(...counts), Math.max(...counts)];
      return `${low === high ? low : `${low}–${high}`} ${label}${lost ? ` (${lost} ${NOT_READ})` : ""}`;
    }).filter(Boolean);
    return `PHY ${phyRanges(bad.map((row) => row.phy).sort((a, b) => a - b))} (${bad.length} PHYs), each: ${spans.join(", ")}`;
  }

const TIER_RANK = { critical: 2, warning: 1, "": 0 };

// One reading per physical PHY: every observation of it (one per HBA that
// reaches its chip), each judged counter by counter. The worst observation
// by evaluated severity stands, then by count; never by count alone. It is
// read only where every observation read every counter; a counter that was
// read is never dropped because another was not. Its role is what any
// observation says is attached. Every surface and check reads this.
function phyReading(model, id, phy) {
    if (!model.phyReadings) model.phyReadings = new Map();
    const chip = chipOfNode(model, id);
    const owners = chip ? [...new Set([chip.id, ...list(chip.rows).map((row) => row.id), id])] : [id];
    const key = `${owners[0]}|${phy}`;
    if (model.phyReadings.has(key)) return model.phyReadings.get(key);
    // Every row any observation reports for this PHY: a duplicate number is
    // two observations, never one. An observation of the chip that has no
    // row for it is a missing observation: not read.
    const tables = owners.map((owner) => [owner, model.sas.expanders.get(owner)?.rowsByPhy.get(phy) || model.sas.controllers.get(owner)?.phys.filter((item) => item.phy === phy) || null, Boolean(model.sas.expanders.get(owner) || model.sas.controllers.get(owner))]);
    const observations = tables.flatMap(([owner, rows]) => list(rows).map((row) => ({ owner, row, tier: phyTier(row) })));
    const missing = tables.filter(([, rows, known]) => known && !list(rows).length).map(([owner]) => owner);
    const conflict = tables.some(([, rows]) => list(rows).length > 1);
    const worst = observations.reduce((best, item) => (!best || TIER_RANK[item.tier] > TIER_RANK[best.tier] || (TIER_RANK[item.tier] === TIER_RANK[best.tier] && item.row.errors > best.row.errors) ? item : best), null);
    const disagree = PHY_COUNTERS.some(([name]) => new Set(observations.map((item) => item.row.counters?.[name]).filter((value) => value !== null && value !== undefined)).size > 1);
    const devices = [...new Set(observations.map((item) => item.row.device).filter(Boolean))];
    const reading = {
      phy, chipId: owners[0], observations, worst: worst?.row || null, tier: worst?.tier || "",
      // Read only where every observation has its own one row, measured:
      // a number on two rows of one expander is not this PHY's identity.
      read: observations.length > 0 && !missing.length && !conflict && observations.every((item) => item.row.measured),
      // Some counter was read somewhere, though not all.
      partial: observations.some((item) => PHY_COUNTERS.some(([name]) => item.row.counters?.[name] !== null && item.row.counters?.[name] !== undefined)),
      missing, conflict, disagree, devices, device: worst?.row.device || devices[0] || "",
      rate: observations.map((item) => item.row.rate).find((value) => value !== null && value !== undefined) ?? null,
    };
    model.phyReadings.set(key, reading);
    return reading;
  }

// Every PHY number any observation of a chip reports.
// Every PHY number expected of a chip: any observation's rows, any the
// transport census lists for it, and any a cable lists at it. A number
// expected with no row is a PHY not read, never one that is not there.
// Fixed on first use, before neighbours are grouped.
function chipPhyNumbers(model, id) {
    if (!model.chipPhyCache) model.chipPhyCache = new Map();
    const chip = chipOfNode(model, id);
    const owners = chip ? [...new Set([chip.id, ...list(chip.rows).map((row) => row.id), id])] : [id];
    const key = owners[0];
    if (model.chipPhyCache.has(key)) return model.chipPhyCache.get(key);
    const numbers = new Set(owners.flatMap((owner) => list((model.sas.expanders.get(owner) || model.sas.controllers.get(owner))?.phys).map((row) => row.phy)));
    for (const port of list(model.censusRows)) {
      if (owners.includes(port.owner_id)) for (const phy of list(port.phys)) if (Number.isInteger(phy)) numbers.add(phy);
    }
    for (const link of list(model.links)) {
      if (!link.lanes) continue;
      if (owners.includes(link.source)) for (const phy of link.lanes.sourcePhys) numbers.add(phy);
      if (owners.includes(link.target)) for (const phy of link.lanes.targetPhys) numbers.add(phy);
    }
    const sorted = [...numbers].sort((a, b) => a - b);
    model.chipPhyCache.set(key, sorted);
    return sorted;
  }

const disagreement = (reading) => (reading?.disagree ? `; the ${plural(reading.observations.length, "observation")} of this PHY disagree, the worst is shown` : "");

// The measurements that set a verdict: the critical PHYs and their counts
// first, then the warnings; a counter not read is named beside them, never
// in their place.
function faultDigest(bad) {
    const ordered = [...bad].sort((left, right) => left.phy - right.phy);
    const groups = ["critical", "warning"].map((tier) => ordered.filter((reading) => reading.tier === tier)).filter((group) => group.length);
    // A disagreement is said of the PHY whose observations disagree.
    const text = groups.map((group) => (group.length <= 3
      ? group.map((reading) => `PHY ${reading.phy}: ${counterText(reading.worst)}${disagreement(reading)}`).join("; ")
      : `${errorDigest(group.map((reading) => reading.worst))}${group.some((reading) => reading.disagree) ? `; the observations of PHY ${phyRanges(group.filter((reading) => reading.disagree).map((reading) => reading.phy))} disagree, the worst is shown` : ""}`)).join("; ");
    return text;
  }

// What a set of PHY readings says: the faults, and what was not read, both.
function readingSummary(readings, wanted) {
    const found = readings.filter(Boolean);
    const bad = found.filter((reading) => reading.tier);
    const read = found.filter((reading) => reading.read).length;
    const unread = Math.max(0, wanted - read);
    const faultText = faultDigest(bad);
    const parts = faultText ? [faultText] : [];
    if (!bad.length && read) parts.push(`none on ${plural(read, "PHY")}`);
    if (unread > 0) parts.push(`${plural(unread, "PHY")} ${NOT_READ}`);
    return { text: parts.join("; ") || NOT_READ, faultText, warn: worse(...bad.map((reading) => reading.tier)), unread, bad: bad.length, read };
  }

function errorSummary(rows, wanted) {
    if (!rows.length) return { text: NOT_READ, warn: false };
    const unread = wanted - rows.filter((row) => row.measured).length;
    const bad = rows.filter((row) => row.errors > 0);
    const parts = bad.length ? [errorDigest(rows)] : [];
    if (!bad.length) parts.push(`none on ${plural(rows.filter((row) => row.measured).length, "PHY")}`);
    if (unread > 0) parts.push(`${plural(unread, "PHY")} ${NOT_READ}`);
    return { text: parts.join("; "), warn: worse(...bad.map(phyTier)) };
  }

function tierForNode(item, mode) {
    const kind = String(item.kind || "");
    if (kind === "host") return "host";
    if (kind === "head") return "heads";
    if (kind === "controller") return "controllers";
    if (kind === "expander") return mode === "storage" ? null : "expanders";
    if (kind === "path") return mode === "core" ? null : "devices";
    if (kind === "bay") return "bays";
    if (kind.endsWith("enclosure")) return "enclosures";
    return null;
  }

function hiddenByDesign(item, mode) {
    return HIDDEN_KINDS.has(String(item.kind || "")) || (mode === "core" && item.kind === "path");
  }

function expanderLevel(item) {
    return decimal(item?.metrics?.sas_level) ?? decimal(item?.raw?.sas_level);
  }

function countTier(fabric, domain, count, base) {
    const state = readState(fabric, domain);
    if (count > 0) {
      if (state === "partial") {
        return { state: "partial", note: `at least ${count}; some controller reads are missing` };
      }
      const note = plural(count, base.one, base.many);
      return { state: "drawn", note: state === "complete" ? note : `${note}; read completeness not reported` };
    }
    if (state === "complete") {
      return { state: "none", note: base.none };
    }
    return { state: "not_read", note: state === "unknown" ? `${base.many}: read not reported` : `${base.many} not read` };
  }

function pathCaption(item, kind) {
    const metrics = isRecord(item.metrics) ? item.metrics : {};
    const raw = isRecord(item.raw) ? item.raw : {};
    const ses = kind === "linux_ses" || raw.source === "linux_ses" || raw.fabric_kind === "linux_ses" || Boolean(metrics.ses_device);
    const caption = ses ? "SES device" : own(PATH_CAPTIONS, metrics.path_type) || "storage path";
    if (!item.status) return caption;
    return metrics.state_basis === MEMBER_HEALTH ? `${caption} · member health ${item.status}` : `${caption} · ${item.status}`;
  }

function nodeFacts(item, mode, kind) {
    const core = mode === "core";
    const metrics = isRecord(item.metrics) ? item.metrics : {};
    const notRead = new Set(list(metrics.not_read));
    const facts = [];
    if (item.kind === "controller") {
      if (core) {
        // The board, and the temperature it reports (a reading, no threshold).
        const board = [item.raw?.board, metrics.temperature].filter((value) => typeof value === "string" && value).join(" · ");
        if (board) facts.push(board);
        const address = sasAddressOf(item.raw?.sas_address);
        if (address) facts.push(`SAS ${tail(address)}`);
        const phys = decimal(metrics.phy_count);
        if (notRead.has("phy_count") || notRead.has("linked_phy_count")) {
          facts.push("PHY table not read");
        } else if (phys !== null) {
          const linked = decimal(metrics.linked_phy_count);
          facts.push(`${linked === null ? "?" : linked} of ${phys} PHYs linked`);
        }
      } else if (mode === "storage") {
        facts.push("storage source, not an HBA");
      } else {
        facts.push("controller or source");
      }
    } else if (item.kind === "expander") {
      const level = expanderLevel(item);
      facts.push(level === null ? "level not reported" : `level ${level}`);
      const phys = decimal(metrics.num_phys);
      if (notRead.has("linked_phys")) {
        facts.push("PHY table not read");
      } else if (phys !== null) {
        const linked = decimal(metrics.linked_phys);
        facts.push(`${linked === null ? "?" : linked} of ${phys} PHYs linked`);
      }
      // What the expander says it is, where the source reads that.
      const product = [text(item.raw?.vendor), text(item.raw?.product), text(item.raw?.revision) ? `rev ${item.raw.revision}` : ""].filter(Boolean).join(" ");
      if (product) facts.push(product);
    } else if (item.kind === "host") {
      facts.push("host");
    } else if (item.kind === "path") {
      facts.push(pathCaption(item, kind));
    } else {
      facts.push(String(item.kind || "").replace(/-/g, " "));
    }
    return facts;
  }

function slotIdentities(slot) {
    const found = new Set();
    if (!isRecord(slot)) return found;
    const add = (value) => {
      if (typeof value === "string" && value.trim()) found.add(value.trim().toLowerCase());
    };
    if (isRecord(slot.raw_status)) add(slot.raw_status.enclosure_id);
    for (const target of list(slot.ssh_ses_targets)) if (isRecord(target)) add(target.enclosure_id);
    return found;
  }

function inventoryLayout(fabric, inventory) {
    if (!isRecord(inventory) || !isRecord(fabric)) return null;
    const differs = (left, right) => Boolean(left && right && left !== right);
    if (differs(fabric.system_id, inventory.selected_system_id) || differs(fabric.selected_enclosure_id, inventory.selected_enclosure_id)) return null;
    const rows = list(inventory.layout_rows).filter(Array.isArray).map((row) => row.filter((slot) => Number.isInteger(slot)));
    if (!rows.length || rows.some((row) => !row.length)) return null;
    const seen = new Set();
    for (const slot of rows.flat()) {
      if (seen.has(slot)) return null;
      seen.add(slot);
    }
    const profile = isRecord(inventory.selected_profile) ? inventory.selected_profile : {};
    const columns = Math.max(...rows.map((row) => row.length));
    const groups = list(profile.row_groups).filter((value) => Number.isInteger(value) && value > 0);
    const even = groups.length > 1 && groups.reduce((sum, value) => sum + value, 0) === columns && rows.every((row) => row.length === columns);
    return {
      rows,
      // Column indexes that start a new column group.
      breaks: even ? groups.slice(0, -1).reduce((all, value) => [...all, (all[all.length - 1] || 0) + value], []) : [],
      edgeLabel: profile.latch_edge === "bottom" && typeof profile.edge_label === "string" ? profile.edge_label : "",
    };
  }

function physicalEnclosures({ nodes, links, bayLinks, bayRows, payloadNodeIds, inventory, located }) {
    const observed = nodes.filter((entry) => entry.tier === "enclosures");
    // A controller's enclosure row names the enclosure by its logical id; a
    // Linux SES device's row by the enclosure id it reports.
    const identityOf = (entry) => {
      const raw = payloadNodeIds.get(entry.id)?.raw;
      const value = raw?.logical_id || raw?.shared_backplane_id || (raw?.source === "linux_ses" ? raw?.enclosure_id : "");
      return typeof value === "string" && value.trim() ? value.trim().toLowerCase() : "";
    };
    const enclosures = new Map();
    const unidentified = [];
    for (const entry of observed) {
      const identity = identityOf(entry);
      if (!identity) {
        unidentified.push(entry);
        continue;
      }
      if (!enclosures.has(identity)) {
        enclosures.set(identity, { identity, id: `enclosure:${identity}`, canonical: null, observations: [], ses: [], bayIds: new Set(), name: "", inferredBays: 0 });
      }
      if (entry.kind === "enclosure") enclosures.get(identity).canonical = entry;
      else enclosures.get(identity).observations.push(entry);
    }
    // A controller's enclosure row without an identity cannot be joined to
    // anything; only SES devices, which never carry one, are joined below.
    if (!enclosures.size || unidentified.some((entry) => entry.kind !== "ses-enclosure")) return null;
    const baysBySource = new Map();
    for (const link of bayLinks) {
      if (!baysBySource.has(link.source)) baysBySource.set(link.source, new Set());
      baysBySource.get(link.source).add(link.target);
    }
    const memberOf = new Map();
    const reported = new Set();
    let conflict = false;
    const claim = (bayId, identity, basis) => {
      const had = memberOf.get(bayId);
      if (had && had !== identity) conflict = true;
      else memberOf.set(bayId, identity);
      if (basis !== "coverage") reported.add(bayId);
    };
    // What each controller reports: the bays it sees a disk in.
    for (const enclosure of enclosures.values()) {
      if (enclosure.canonical) for (const bayId of baysBySource.get(enclosure.canonical.id) || []) claim(bayId, enclosure.identity, "reported");
      for (const entry of enclosure.observations) {
        for (const bayId of baysBySource.get(entry.id) || []) claim(bayId, enclosure.identity, "controller");
      }
    }
    // What the inventory reports for each slot, empty bays included.
    const slotOf = new Map();
    for (const item of bayRows) {
      if (!Number.isInteger(item.slot) || slotOf.has(item.id)) continue;
      const slot = inventory.get(item.slot);
      slotOf.set(item.id, slot);
      const ids = [...slotIdentities(slot)].filter((identity) => enclosures.has(identity));
      if (ids.length > 1) return null;
      if (ids.length) claim(item.id, ids[0], "inventory");
    }
    // SES devices: by the enclosure the inventory names for the device; else
    // when every bay the device reports is already in one enclosure; else,
    // marked as inferred, when the device covers every bay known to be in one
    // enclosure and no bay of another.
    const sesNamed = new Map();
    for (const slot of inventory.values()) {
      for (const target of list(slot.ssh_ses_targets)) {
        if (!isRecord(target) || typeof target.ses_device !== "string" || typeof target.enclosure_id !== "string") continue;
        if (!sesNamed.has(target.ses_device)) sesNamed.set(target.ses_device, new Set());
        sesNamed.get(target.ses_device).add(target.enclosure_id.trim().toLowerCase());
      }
    }
    // Every SES device is judged against what the HBAs and the inventory
    // report, never against another SES device's join.
    const before = new Map(memberOf);
    for (const entry of unidentified) {
      const device = String(payloadNodeIds.get(entry.id)?.raw?.ses_device || entry.rawId || entry.label);
      const bays = baysBySource.get(entry.id) || new Set();
      const named = [...(sesNamed.get(device) || [])];
      let identity = "";
      let basis = "";
      if (named.length === 1 && enclosures.has(named[0])) {
        [identity, basis] = [named[0], "reported"];
      } else if (named.length) {
        return null;
      } else {
        const seen = new Set([...bays].map((bayId) => before.get(bayId)).filter(Boolean));
        if (seen.size !== 1) return null;
        [identity] = [...seen];
        if ([...bays].every((bayId) => before.get(bayId) === identity)) {
          basis = "bays";
        } else {
          const known = [...before].filter(([, value]) => value === identity).map(([bayId]) => bayId);
          if (!known.every((bayId) => bays.has(bayId))) return null;
          basis = "coverage";
        }
      }
      for (const bayId of bays) claim(bayId, identity, basis === "bays" ? "inventory" : basis);
      enclosures.get(identity).ses.push({ entry, device, basis, bays: bays.size });
    }
    if (conflict) return null;
    const byBay = new Map();
    for (const item of bayRows) {
      const identity = memberOf.get(item.id);
      if (!identity) continue;
      // A disk without a physical location is never drawn as a bay.
      if (!located(item)) return null;
      const enclosure = enclosures.get(identity);
      enclosure.bayIds.add(item.id);
      if (!reported.has(item.id)) enclosure.inferredBays += 1;
      byBay.set(item.id, enclosure);
    }
    for (const enclosure of enclosures.values()) {
      if (!enclosure.bayIds.size) return null;
      const names = new Set([...enclosure.bayIds].flatMap((bayId) => {
        const slot = slotOf.get(bayId);
        const name = isRecord(slot) ? slot.enclosure_name || slot.raw_status?.enclosure_name : "";
        return typeof name === "string" && name && !/^(none|null)$/i.test(name) ? [name] : [];
      }));
      if (names.size === 1) [enclosure.name] = [...names];
      enclosure.observations.sort((left, right) => naturalCompare(left.id, right.id));
      enclosure.ses.sort((left, right) => naturalCompare(left.entry.id, right.entry.id));
      // The lines the payload draws into each controller row: its parents.
      enclosure.parents = new Map(enclosure.observations.map((entry) => [entry.id, links.filter((link) => link.target === entry.id)]));
    }
    return { enclosures: [...enclosures.values()], memberOf: byBay, stacked: false };
  }

function enclosureName(enclosure) {
    return enclosure.alias || enclosure.title || enclosure.name || enclosure.identity;
  }

function stackEnclosures(physical, layout, slotOfBay) {
    if (!layout || physical.enclosures.length < 1) return false;
    const owner = new Map();
    for (const enclosure of physical.enclosures) {
      for (const bayId of enclosure.bayIds) {
        const slot = slotOfBay.get(bayId);
        if (slot === null || slot === undefined || owner.has(slot)) return false;
        owner.set(slot, enclosure);
      }
    }
    const placed = new Set();
    const order = [];
    const rowsOf = new Map(physical.enclosures.map((enclosure) => [enclosure, []]));
    for (const row of layout.rows) {
      const owners = new Set(row.map((slot) => owner.get(slot)));
      if (owners.size !== 1) return false;
      const [enclosure] = [...owners];
      // A row of slots no enclosure here owns: not this view's layout.
      if (!enclosure) return false;
      if (order[order.length - 1] !== enclosure) {
        if (order.includes(enclosure)) return false;
        order.push(enclosure);
      }
      rowsOf.get(enclosure).push(row);
      for (const slot of row) placed.add(slot);
    }
    if (placed.size !== owner.size || order.length !== physical.enclosures.length) return false;
    physical.enclosures = order;
    for (const enclosure of order) enclosure.layoutRows = rowsOf.get(enclosure);
    for (const enclosure of order) enclosure.breaks = layout.breaks;
    physical.stacked = true;
    physical.edgeLabel = layout.edgeLabel;
    return true;
  }

function planNeighbours(context) {
    const { nodes, nodeById, links, fabric, sas, inventory, hrefFor } = context;
    const off = nodes.filter((entry) => entry.kind === "expander" && !entry.chipOf && !entry.mergedInto);
    if (!off.length || !nodes.some((entry) => entry.chipOf)) return;
    const lanes = links.filter((link) => !link.bundle && LANE_LINKS.has(link.kind));
    const isOff = new Set(off.map((entry) => entry.id));
    // Below the boards: a chip's own children (planBoards), then theirs.
    const queue = off.filter((entry) => entry.below);
    for (const entry of queue) entry.below = 1;
    while (queue.length) {
      const entry = queue.shift();
      for (const link of lanes) {
        const target = link.source === entry.id ? nodeById.get(link.target) : null;
        if (!target || !isOff.has(target.id) || target.below) continue;
        target.below = entry.below + 1;
        queue.push(target);
      }
    }
    // Above: what a chip is cabled from, and what that is cabled from.
    const above = new Map();
    let frontier = new Set(nodes.filter((entry) => entry.chipOf).map((entry) => entry.id));
    for (let distance = 1; frontier.size; distance += 1) {
      const next = new Set();
      for (const link of lanes) {
        if (!frontier.has(link.target) || !isOff.has(link.source) || above.has(link.source) || nodeById.get(link.source).below) continue;
        above.set(link.source, distance);
        next.add(link.source);
      }
      frontier = next;
    }
    const census = list(fabric?.raw?.sas_transport_ports).filter((port) => isRecord(port) && typeof port.owner_id === "string");
    const byIdentity = new Map();
    for (const item of list(inventory?.enclosures)) {
      if (!isRecord(item) || typeof item.id !== "string") continue;
      // A combined view's id is its members' ids joined by "+".
      for (const part of item.id.toLowerCase().split("+")) if (part.trim() && !byIdentity.has(part.trim())) byIdentity.set(part.trim(), item);
    }
    const selected = new Set(String(fabric?.selected_enclosure_id || "").toLowerCase().split("+"));
    for (const entry of off) {
      const address = sasAddressOf(entry.rawId);
      const owned = census.filter((port) => port.owner_id === entry.id);
      const ports = owned.filter((port) => port.remote_kind === "end_device");
      const identities = [...new Set(ports.map((port) => text(port.enclosure_identifier).toLowerCase()).filter(Boolean))];
      const found = identities.length === 1 ? byIdentity.get(identities[0]) : !identities.length && address ? byIdentity.get(address) : null;
      const relation = entry.below === 1 ? "Next in chain" : entry.below ? "Further down the chain" : above.get(entry.id) === 1 ? "Reached through" : above.has(entry.id) ? "Further up the chain" : "Elsewhere in the fabric";
      entry.address = address;
      entry.neighbour = {
        relation,
        known: Boolean(found),
        name: found ? text(found.alias) || text(found.label) || `enclosure …${found.id.slice(-4)}` : "enclosure not read",
        basis: !found ? "" : identities.length === 1 ? "the enclosure id its disks report" : "an enclosure id equal to its SAS address",
        enclosureId: found ? found.id : "",
        bays: found && Number.isInteger(found.slot_count) ? found.slot_count : null,
        // The disks on it: its end devices with a block device, where the
        // payload lists its ports; and its ports whose attachment is not read.
        // Only where the census lists this owner's own ports: another
        // owner's rows say nothing of this one.
        disks: owned.length ? ports.filter((port) => port.has_block === true).length : null,
        unreadAttachments: owned.filter((port) => port.remote_kind !== "expander" && (port.remote_kind !== "end_device" || typeof port.has_block !== "boolean")).length,
        product: text(sas.expanders.get(entry.id)?.product),
        href: found && hrefFor && !found.id.toLowerCase().split("+").some((part) => selected.has(part)) ? String(hrefFor(found.id) || "") : "",
      };
      entry.viaLine = viaRows([entry], nodeById);
      entry.aliases = [address, entry.rawId, entry.id, entry.label, entry.neighbour.name].filter(Boolean);
      entry.label = `${relation}: ${entry.neighbour.name}`;
      entry.facts = stubFacts(entry, "");
    }
  }

function stubFacts(entry, health) {
    const { neighbour } = entry;
    // A count is exact only where every port's attachment was read.
    const partial = neighbour.unreadAttachments ? `, ${plural(neighbour.unreadAttachments, "attachment")} not read` : "";
    const disks = neighbour.disks === null ? "disks not read"
      : partial ? `${plural(neighbour.disks, "disk")} seen${neighbour.bays === null ? ", bays not read" : ` of ${neighbour.bays} bays`}${partial}`
      : neighbour.bays === null ? `${plural(neighbour.disks, "disk")}, bays not read` : `${neighbour.disks}/${neighbour.bays} disks`;
    return [
      [neighbour.product, entry.address ? tail(entry.address) : ""].filter(Boolean).join(" · "),
      [entry.level === null ? "level not reported" : `level ${entry.level}`, disks].join(" · "),
      health,
      neighbour.href ? "open its Topology view ›" : "",
    ].filter(Boolean);
  }

function enclosureOfExpander(model, id) {
    const entry = chipOfNode(model, id);
    if (!entry) return "";
    if (entry.chipOf) return enclosureName(model.groupById.get(entry.chipOf)?.enclosure || {}) || "";
    return entry.neighbour ? entry.neighbour.name : "";
  }

function neighbourRows(model, entry) {
    const rows = [];
    const ids = new Set(list(entry.rows).length ? entry.rows.map((row) => row.id) : [entry.id]);
    const cables = model.links.filter((link) => LANE_LINKS.has(link.kind));
    if (entry.neighbour) {
      const { neighbour } = entry;
      rows.push({ label: "Enclosure", value: neighbour.known ? `${neighbour.name}: another enclosure of this system, by ${neighbour.basis}. Its bays are on its own page` : `${NOT_READ}: the payload does not say which enclosure this expander is the backplane of` });
      rows.push({ label: "Disks on it", value: neighbour.disks === null ? `${NOT_READ}: the payload lists no ports for it` : `${neighbour.disks}${neighbour.unreadAttachments ? ` seen; what ${plural(neighbour.unreadAttachments, "other port")} ${neighbour.unreadAttachments === 1 ? "is" : "are"} attached to is ${NOT_READ}` : ""}${neighbour.bays === null ? "; its bay count is not read" : ` of ${plural(neighbour.bays, "bay")}`}`, warn: neighbour.unreadAttachments > 0 });
      if (neighbour.health) rows.push({ label: "State", value: neighbour.health, warn: neighbour.tier || false });
    }
    const from = [...new Set(cables.filter((link) => ids.has(link.target)).map((link) => neighbourText(model, link.source)))];
    if (from.length) rows.push({ label: "Upstream", value: from.join("; ") });
    const to = [...new Set(cables.filter((link) => ids.has(link.source)).map((link) => neighbourText(model, link.target)))];
    if (to.length) rows.push({ label: "Cascades to", value: to.join("; ") });
    return rows;
  }

function neighbourText(model, id) {
    const entry = chipOfNode(model, id);
    if (!entry) return id;
    if (entry.kind !== "expander") return `${entry.label}${entry.kind === "controller" && model.core && !/\bHBA\b/.test(entry.label) ? " (HBA)" : ""}`;
    const address = entry.address || sasAddressOf(entry.rawId);
    return `${enclosureOfExpander(model, id) || "enclosure not read"}${address ? ` (${tail(address)})` : ""}${entry.level === null ? "" : `, level ${entry.level}`}`;
  }

function finishNeighbours(model) {
    const stubs = model.nodes.filter((entry) => entry.neighbour);
    for (const entry of stubs) {
      const cables = model.links.filter((link) => LANE_LINKS.has(link.kind) && (link.source === entry.id || link.target === entry.id));
      const flags = [...context.flagsOf(model, "node", entry.id), ...cables.flatMap((link) => context.flagsOf(model, "link", link.id))];
      // The same readings the checks use: one per PHY, over every
      // observation of the chip, the worst by severity.
      const all = context.chipPhyNumbers(model, entry.id).map((phy) => context.phyReading(model, entry.id, phy));
      const tier = worse(...flags.map((item) => item.tier), ...all.map((reading) => reading.tier));
      const measured = all.filter((reading) => reading.read).length;
      // Clean only where every PHY in use was read: linked, or attached to
      // something the payload names (a disk, an HBA, another expander),
      // whether or not its rate was read.
      const linked = all.filter((reading) => reading.rate !== null || reading.devices.length);
      const rows = linked.length ? linked : all;
      const read = rows.filter((reading) => reading.read).length;
      const clean = read === rows.length ? "no PHY errors" : `no PHY errors on ${read} of ${rows.length} ${linked.length ? (rows.length === 1 ? "PHY in use" : "PHYs in use") : rows.length === 1 ? "PHY" : "PHYs"}; counters not read on ${rows.length - read}`;
      entry.neighbour.tier = tier;
      const said = [...new Set(flags.filter((item) => item.tier === tier).map((item) => item.short))];
      entry.neighbour.health = tier ? `${tier}: ${said.length ? said.join("; ") : "PHY errors"}${read < rows.length ? `; counters not read on ${rows.length - read}` : ""}` : measured ? clean : "PHY counters not read";
      entry.facts = stubFacts(entry, entry.neighbour.health);
    }
    // More than three under one parent: one stub that lists them.
    const byParent = new Map();
    for (const link of model.links) {
      const target = LANE_LINKS.has(link.kind) ? model.nodeById.get(link.target) : null;
      if (!target?.neighbour || !target.below) continue;
      if (!byParent.has(link.source)) byParent.set(link.source, []);
      byParent.get(link.source).push(link);
    }
    for (const [parentId, cables] of byParent) {
      if (cables.length <= STUB_GROUP_ABOVE) continue;
      const members = cables.map((link) => model.nodeById.get(link.target));
      // Everything further down the grouped ones goes with them.
      const gone = new Set(members.map((entry) => entry.id));
      for (let grew = true; grew;) {
        grew = false;
        for (const link of model.links) {
          if (!gone.has(link.source) || gone.has(link.target) || !model.nodeById.get(link.target)?.neighbour) continue;
          gone.add(link.target);
          grew = true;
        }
      }
      const beyond = gone.size - members.length;
      // The group carries the worst of everything it hides, members and
      // everything further down alike.
      const tier = worse(...[...gone].map((id) => model.nodeById.get(id)?.neighbour?.tier || ""));
      const first = members[0];
      const group = {
        ...first, id: `stubs:${parentId}`, label: `Next in chain: ${members.length} enclosures`, alias: "", rawId: "", address: "", neighbour: null, stubGroup: members, stubCables: cables,
        aliases: members.flatMap((entry) => entry.aliases), flagged: "", phyGroups: [], parentUnknown: false, w: NODE_W, h: NODE_H,
        facts: [
          `${members.slice(0, STUB_GROUP_ABOVE).map((entry) => entry.neighbour.name).join(", ")}${members.length > STUB_GROUP_ABOVE ? `, +${members.length - STUB_GROUP_ABOVE} more` : ""}`,
          [tier ? `worst: ${tier}` : "no findings", beyond ? `${plural(beyond, "more expander")} beyond them` : ""].filter(Boolean).join(" · "),
          "pin it to list them",
        ],
      };
      group.groupTier = tier;
      const cable = {
        id: `cables:${parentId}`, kind: "expander-group", source: parentId, target: group.id, status: "", bundle: false, lanes: null, lanesState: "n/a",
        labelLines: [plural(cables.length, "cable"), [...new Set(cables.map((link) => (link.lanes ? `x${link.lanes.count}` : "lanes not reported")))].join(", ")],
        memberLinkIds: cables.map((link) => link.id), count: cables.length, cable: cables.some((link) => link.cable), stubCables: cables,
      };
      for (const link of cables) link.targetName = model.nodeById.get(link.target).neighbour.name;
      const dropped = new Set(model.links.filter((link) => gone.has(link.source) || gone.has(link.target)));
      model.nodes = model.nodes.filter((entry) => !gone.has(entry.id));
      model.nodes.push(group);
      model.nodeById.set(group.id, group);
      model.links = model.links.filter((link) => !dropped.has(link));
      model.links.push(cable);
      model.linkById.set(cable.id, cable);
      for (const [id, items] of model.outgoing) model.outgoing.set(id, items.filter((link) => !dropped.has(link)));
      for (const [id, items] of model.incoming) model.incoming.set(id, items.filter((link) => !dropped.has(link)));
      model.outgoing.set(parentId, [...list(model.outgoing.get(parentId)), cable]);
      model.incoming.set(group.id, [cable]);
      for (const tier of model.tiers) tier.nodeIds = tier.nodeIds.filter((id) => !gone.has(id));
      model.tiers.find((tier) => tier.id === `beyond:${first.below}`)?.nodeIds.push(group.id);
      // The group and its cable carry their members' findings.
      model.flags.set(`node:${group.id}`, members.flatMap((entry) => context.flagsOf(model, "node", entry.id)));
      model.flags.set(`link:${cable.id}`, cables.flatMap((link) => context.flagsOf(model, "link", link.id)));
      model.tiers = model.tiers.filter((tier) => !/^beyond:/.test(tier.id) || tier.nodeIds.length);
    }
  }

function adoptLinuxTransport(payload, options = {}) {
    if (!isRecord(payload) || payload.raw?.sas_transport_available !== true) return null;
    // With the collector's rows in, every controller is a SAS host: the
    // collector takes the SES source's stand-in controller out.
    const hardware = list(payload.nodes).filter((item) => isRecord(item) && typeof item.id === "string" && ["controller", "expander"].includes(item.kind))
      .map((item) => ({ ...item, raw: isRecord(item.raw) ? item.raw : {} }));
    if (!hardware.some((item) => item.kind === "controller")) return null;
    const rename = (id) => (options.prefix && typeof id === "string" ? id.replace(/^(controller|expander):/, `$1:${options.prefix}:`) : id);
    const resolve = window.SasFabricTransport?.resolveTransportPort;
    const index = isRecord(payload.raw.sas_transport_port_index) ? payload.raw.sas_transport_port_index : {};
    const census = list(payload.raw.sas_transport_ports).filter(isRecord);
    // What each PHY is attached to, by its owner and its sysfs name.
    const role = new Map();
    const inPort = new Set();
    // A name on more than one row of an owner's table names neither row.
    const names = new Map();
    // The same table the rows below are built from: an HBA's phy_rows, an
    // expander's phys (an empty alias beside them is no table).
    for (const item of hardware) for (const row of list(item.kind === "controller" ? item.raw.phy_rows : item.raw.phys).filter(isRecord)) {
      const key = `${item.id}|${text(row.name)}`;
      if (text(row.name)) names.set(key, (names.get(key) || 0) + 1);
    }
    for (const port of census) {
      // A record that was not read names no role, as it names no destination;
      // nor does one naming any PHY by a name on more than one row.
      if (port.state !== "read" || list(port.phy_names).some((name) => (names.get(`${port.owner_id}|${name}`) || 0) > 1)) continue;
      const device = port.remote_kind === "expander" ? "SMP Target" : port.remote_kind !== "end_device" ? "Attachment not read" : port.has_block ? "End device (disk)" : "End device, no block device";
      for (const name of list(port.phy_names)) {
        // Two census ports naming one PHY: both roles are kept, never the
        // later one alone.
        const held = role.get(`${port.owner_id}|${name}`);
        role.set(`${port.owner_id}|${name}`, held && held !== device ? bothRoles(held, device) : device);
        inPort.add(`${port.owner_id}|${name}`);
      }
    }
    // The port rows of this view's bays.
    const ports = new Map();
    const owners = new Set();
    for (const trace of list(payload.traces)) {
      if (!isRecord(trace)) continue;
      // A path state that names its transport port is that port's row too.
      const rows = list(trace.sas_transport_ports ?? trace.metrics?.sas_transport_ports
        ?? list(trace.metrics?.path_states).filter((row) => isRecord(row) && typeof row.sas_transport_port_id === "string")).filter(isRecord);
      const mapped = rows.map((row) => {
        const found = resolve ? resolve(payload, row) : { state: "not_read", phys: [] };
        const entry = own(index, row.sas_transport_port_id);
        // The owner of an entry the shared resolver rejected is not read:
        // its PHY names did not resolve on it (#1494).
        const ownerId = found.state === "read" && isRecord(entry) && typeof entry.owner_id === "string" ? entry.owner_id : "";
        const protocol = protocolOf(row.target_port_protocol, row.transport_protocol);
        const device = protocol ? `${protocol} Target` : "End device (disk)";
        const phys = found.state === "read" ? list(found.phys) : [];
        if (ownerId) owners.add(rename(ownerId));
        // A bay's own port names its disk more exactly than the census does;
        // a census role that is not a disk's is kept beside it.
        for (const phy of phys) {
          const held = role.get(`${ownerId}|${phy.name}`);
          role.set(`${ownerId}|${phy.name}`, held && held !== device && /SMP Target|SAS Initiator|Attachment not read| \/ /.test(held) ? bothRoles(held, device) : device);
        }
        return {
          controller: options.label || text(row.controller), state: isUnreadState(row) ? STATE_UNREAD : row.state, member_device_name: text(row.member_device_name),
          mpr_device: protocol ? device : "", sas_address: text(row.sas_address), expander_ids: ownerId ? [rename(ownerId)] : [],
          expander_phy: phys.length === 1 && Number.isInteger(phys[0].phy) ? phys[0].phy : null, phy_basis: "",
          // Every PHY the port's own read census entry names: a wide port's
          // PHYs are its bay's, though no single one is its path's.
          expander_phys: phys.map((phy) => phy.phy).filter((phy) => Number.isInteger(phy)),
        };
      });
      if (mapped.length) ports.set(trace.id, mapped);
    }
    // Cables: the parent's PHYs as reported; the far end inferred or not read.
    const links = [];
    for (const item of list(payload.links)) {
      if (!isRecord(item) || !LANE_LINKS.has(item.kind)) continue;
      const lanes = isRecord(item.reported_lanes) ? { ...item.reported_lanes } : null;
      const child = hardware.find((node) => node.id === item.target);
      if (lanes && !list(lanes.target_phys).length && census.length && census.every((port) => port.state === "read") && child) {
        const free = list(child.raw.phys).filter((row) => isRecord(row) && finite(row.negotiated_rate_gbps) !== null && Number.isInteger(row.phy) && !inPort.has(`${child.id}|${row.name}`));
        if (free.length === lanes.lane_count) {
          lanes.target_phys = free.map((row) => row.phy).sort((left, right) => left - right);
          lanes.target_basis = "inferred";
          for (const row of free) role.set(`${child.id}|${row.name}`, "Cable far end (inferred)");
        }
      }
      links.push({ ...item, id: `${item.kind}:${rename(item.source)}->${rename(item.target)}`, source: rename(item.source), target: rename(item.target), reported_lanes: lanes });
    }
    const phyRow = (ownerId, row) => {
      const measured = row.counter_state === "measured";
      return {
        phy: row.phy, name: text(row.name), dev_handle: "", device: (names.get(`${ownerId}|${text(row.name)}`) || 0) > 1 ? NAME_NOT_UNIQUE : role.get(`${ownerId}|${row.name}`) || "",
        speed: finite(row.negotiated_rate_gbps), min: finite(row.minimum_rate_gbps), max: finite(row.maximum_rate_gbps),
        ...Object.fromEntries(PHY_COUNTERS.map(([key]) => [key, row[key] ?? null])),
        // Which counters sysfs did not return, by name: the others were read.
        not_read: list(row.not_read).filter((key) => typeof key === "string"),
        counter_state: measured ? "measured" : "not_read", counter_reason: measured ? "" : "sysfs did not return this PHY's counters",
      };
    };
    const controllers = [];
    const expanders = [];
    const nodes = [];
    for (const item of hardware) {
      const id = rename(item.id);
      const rows = list(item.kind === "controller" ? item.raw.phy_rows : item.raw.phys).filter(isRecord).map((row) => phyRow(item.id, row));
      const live = rows.filter((row) => row.speed !== null).length;
      const raw = { ...item.raw };
      if (item.kind === "controller") {
        controllers.push({ id, board: text(item.raw.board), chip: text(item.raw.driver), firmware: text(item.raw.firmware), sas_address: text(item.raw.sas_address), phy_rows: rows.map((row) => ({ ...row, device_handle: "" })) });
        nodes.push({
          ...item, id, label: options.label || nodeLabel(item), metrics: { ...(isRecord(item.metrics) ? item.metrics : {}), phy_count: rows.length, linked_phy_count: live },
          raw: { ...raw, board: [text(item.raw.board), text(item.raw.firmware) ? `fw ${item.raw.firmware}` : ""].filter(Boolean).join(" · ") },
        });
        continue;
      }
      const address = sasAddressOf(item.raw.sas_address);
      const measured = rows.filter((row) => row.counter_state === "measured");
      expanders.push({
        id, sas_address: address, dev_handle: "", num_phys: rows.length, linked_phys: live, phys: rows,
        product: [text(item.raw.vendor), text(item.raw.product), text(item.raw.revision) ? `rev ${item.raw.revision}` : ""].filter(Boolean).join(" "),
        reported_level: text(item.raw.reported_level),
        phy_error_counters: {
          state: measured.length ? "available" : "not_read", source: "Linux sas_phy sysfs counters", read_at: null, measured_phys: measured.length, unknown_phys: rows.length - measured.length,
          phys_with_errors: measured.filter((row) => PHY_COUNTERS.some(([key]) => row[key] > 0)).length,
        },
      });
      nodes.push({
        ...item, id, label: address ? `Expander ${tail(address)}` : nodeLabel(item), raw_id: address || item.raw_id, controller_id: rename(item.controller_id), raw,
        metrics: { ...(isRecord(item.metrics) ? item.metrics : {}), num_phys: rows.length, linked_phys: live, not_read: list(item.metrics?.not_read).filter((key) => key !== "linked_phys") },
      });
    }
    return { controllers, expanders, nodes, links, ports, owners, census: census.length > 0, resolved: Boolean(resolve) };
  }

function withLinuxTransport(fabric) {
    const adopted = adoptLinuxTransport(fabric);
    if (!adopted) return null;
    const nodes = new Map(adopted.nodes.map((node) => [node.id, node]));
    const lanes = new Map(adopted.links.map((link) => [link.id, link]));
    return {
      ...fabric,
      nodes: list(fabric.nodes).map((item) => nodes.get(item?.id) || item),
      links: list(fabric.links).map((item) => lanes.get(item?.id) || item),
      traces: list(fabric.traces).map((trace) => {
        if (!isRecord(trace) || trace.kind !== "bay") return trace;
        // A row with no controller is SES detail kept without a path: not a path.
        const states = list(trace.metrics?.path_states).filter((state) => isRecord(state) && (state.controller || state.controller_label))
          .map((state) => (state.state === "unknown" && state.source === "linux_sas_transport" ? { ...state, state: STATE_UNREAD } : state));
        return { ...trace, metrics: { ...(isRecord(trace.metrics) ? trace.metrics : {}), path_states: states, mpr_devices: adopted.ports.get(trace.id) || [] } };
      }),
      controllers: adopted.controllers,
      expanders: adopted.expanders,
      raw: {
        ...fabric.raw,
        // The collector admits only a complete survey of the SAS classes. An
        // enclosure is another read (SES): complete where it gave rows.
        controller_read: { counts: { controllers: "complete", expanders: "complete", ...(list(fabric.nodes).some((item) => isRecord(item) && String(item.kind || "").endsWith("enclosure")) ? { enclosures: "complete" } : {}) } },
        linux_transport: { census: adopted.census, resolved: adopted.resolved },
      },
    };
  }

function deviceShort(name) {
    return text(name).split("/").pop();
  }

function expanderInPath(name) {
    const found = /(?:^|[-/])sas-exp0x([0-9a-f]{16})-phy([0-9]{1,3})(?:-|$)/i.exec(text(name));
    return found ? { address: found[1].toLowerCase(), phy: Number(found[2]) } : null;
  }

function notCollectedReason(fabric) {
    const said = list(fabric?.warnings).filter((item) => typeof item === "string").flatMap((item) => item.split(/(?<=[.;])\s+/))
      .find((item) => /not exposed|not collected|not available|appear only when/i.test(item));
    return said ? `${said.charAt(0).toUpperCase()}${said.slice(1)}` : NOT_COLLECTED;
  }

function headFacts(item, fabric, shared) {
    const head = headInfo(item, fabric, shared);
    const facts = [head.system, head.role === "active" ? `active (pool owner): ${head.pool}` : head.role ? `standby: ${head.pool} active on ${head.owner}` : "role not read"];
    if (head.selected) facts.push("the view this page is on");
    return facts;
  }

function headInfo(item, fabric, shared) {
    const raw = isRecord(item.raw) ? item.raw : {};
    const label = nodeLabel(item);
    const pools = list(fabric.nodes).filter((node) => isRecord(node) && node.kind === "pool").map(nodeLabel);
    const owner = text(raw.pool_owner_label);
    return {
      label,
      owner,
      // The platform as the payload names it; an unknown one is just "storage system".
      system: `${own(PLATFORM_WORDS, text(fabric.platform)) || ""} storage system`.trim(),
      role: owner ? (owner === label ? "active" : "standby") : "",
      pool: pools.length ? `pool ${pools.join(", ")}` : "pool",
      fence: text(raw.fence_owner_label),
      fencing: typeof raw.io_fencing_enabled === "boolean" ? raw.io_fencing_enabled : null,
      visibleOn: list(raw.visible_on_labels).filter((value) => typeof value === "string"),
      selected: Boolean(shared.heads.find((head) => head.controller_id === item.id)?.selected),
    };
  }

function buildTopologyModel(given, options = {}) {
    tailDigits = addressTail(given);
    const fabric = withLinuxTransport(given) || given;
    tailDigits = Math.max(tailDigits, addressTail(fabric));
    // Per-head aggregation awaits its own backend contract.
    const shared = null;
    // The transport collector's rows are in the payload: HBAs, expanders,
    // lanes and disk ports are read, so it is drawn and checked as a SAS fabric.
    const transport = isRecord(fabric?.raw?.linux_transport) ? fabric.raw.linux_transport : null;
    const expanded = isRecord(options.expanded) ? options.expanded : {};
    const contract = fabricContract(fabric);
    const { kind } = contract;
    // expanders and lanes: it is drawn and checked as a SAS fabric.
    const mode = shared?.transport || transport ? "core" : contract.contract;
    const core = mode === "core";
    const payload = readPayload(fabric);
    const skipped = {
      unsupported: [], duplicateNodes: [], missingEnd: [], duplicateLinks: [], unjoined: [], duplicateTraces: [], unsupportedTraces: [], upward: [],
    };
    // Every trace record: the first of an id is read, a repeat is named, and a
    // kind the view does not read is named.
    const traces = new Map();
    for (const trace of payload.traces) {
      if (traces.has(trace.id)) skipped.duplicateTraces.push(trace.id);
      else if (trace.kind === "bay" || trace.kind === "path") traces.set(trace.id, trace);
      else skipped.unsupportedTraces.push(`${trace.kind || "no kind"}: ${trace.id}`);
    }
    const inventory = inventoryBySlot(fabric, options.inventory);
    const sas = sasIndex(fabric);

    const nodes = [];
    const nodeById = new Map();
    const payloadNodeIds = new Map();
    const bayRows = [];
    for (const item of payload.nodes) {
      if (payloadNodeIds.has(item.id)) {
        skipped.duplicateNodes.push(item.id);
        if (item.kind === "bay") bayRows.push(item);
        continue;
      }
      payloadNodeIds.set(item.id, item);
      const tier = tierForNode(item, mode);
      if (!tier) {
        if (!hiddenByDesign(item, mode)) skipped.unsupported.push(`${item.kind || "no kind"}: ${item.id}`);
        continue;
      }
      if (tier === "bays") {
        bayRows.push(item);
        continue;
      }
      const entry = {
        id: item.id,
        kind: item.kind,
        tier,
        label: nodeLabel(item),
        // The operator's own name for it, where one is saved.
        alias: text(item.alias) || text(item.display_label),
        peer: false,
        rawId: item.raw_id || "",
        controllerId: typeof item.controller_id === "string" ? item.controller_id : "",
        status: item.status || "",
        level: item.kind === "expander" ? expanderLevel(item) : null,
        facts: shared && item.raw?.head_end ? headFacts(item, fabric, shared) : nodeFacts(item, mode, kind),
        head: shared && item.raw?.head_end ? headInfo(item, fabric, shared) : null,
        phyGroups: [],
        parentUnknown: false,
        x: 0,
        y: 0,
        w: NODE_W,
        h: NODE_H,
      };
      nodes.push(entry);
      nodeById.set(entry.id, entry);
    }
    const bayIds = new Set(bayRows.map((item) => item.id));

    // Lines: every payload link whose both ends are drawn nodes. Every other
    // link is either into a bay (a bundle), touches a kind left out on
    // purpose, or is named in the notice.
    const links = [];
    const linkById = new Map();
    const bayLinks = [];
    const pathBayLinks = [];
    const seenLinks = new Set();
    for (const item of payload.links) {
      if (["enclosure-observation", "enclosure-ses"].includes(item.kind)) continue;
      if (seenLinks.has(item.id)) {
        skipped.duplicateLinks.push(item.id);
        continue;
      }
      seenLinks.add(item.id);
      const sourceItem = payloadNodeIds.get(item.source);
      const targetItem = payloadNodeIds.get(item.target);
      if (!sourceItem || !targetItem) {
        skipped.missingEnd.push(item.id);
        continue;
      }
      const source = nodeById.get(item.source);
      const target = nodeById.get(item.target);
      if (item.kind === "path-bay") pathBayLinks.push(item);
      const unsupportedEnd = [sourceItem, targetItem].some((end) => !tierForNode(end, mode) && !hiddenByDesign(end, mode));
      // Without SES the collector links a disk's bay to the expander or HBA its port is on.
      const transportBay = Boolean(transport) && item.kind === "transport-bay" && ["controller", "expander"].includes(source?.kind);
      if (bayIds.has(item.target) && source && (source.tier === "enclosures" || (!core && source.tier === "devices") || transportBay)) {
        bayLinks.push(item);
        continue;
      }
      if (!source || !target || item.source === item.target) {
        const hiddenEnd = [sourceItem, targetItem].some((end) => hiddenByDesign(end, mode) || (end.kind === "bay" && !source));
        if (!hiddenEnd && !unsupportedEnd) skipped.unjoined.push(item.id);
        continue;
      }
      const lanesPayload = isRecord(item.reported_lanes) ? item.reported_lanes : null;
      let lanes = null;
      if (lanesPayload && Number.isInteger(lanesPayload.lane_count)) {
        lanes = {
          count: lanesPayload.lane_count,
          sourcePhys: list(lanesPayload.source_phys).filter((value) => Number.isInteger(value)),
          targetPhys: list(lanesPayload.target_phys).filter((value) => Number.isInteger(value)),
          rate: finite(lanesPayload.negotiated_rate_gbps),
          // "inferred" where the far end's PHYs are worked out, not read; and
          // the basis the source gives for what it did read.
          targetBasis: text(lanesPayload.target_basis),
          basis: text(lanesPayload.basis),
          // Each lane as reported: its PHY at each end and its own rate.
          rows: list(lanesPayload.lanes).filter(isRecord)
            .filter((lane) => Number.isInteger(lane.source_phy) && Number.isInteger(lane.target_phy))
            .map((lane) => ({ source: lane.source_phy, target: lane.target_phy, rate: sasRate(lane.negotiated_rate_gbps) })),
        };
        // A lane's rate is read only where each end's own one row reads it
        // and agrees with the list: a retained list never outranks its ends,
        // and a row that reads no speed, a PHY number on two rows, or an end
        // with no PHY table in the payload leaves the lane's rate not read on
        // every surface (label, card, pin, slow lanes, the lanes check).
        const tables = (id) => sas.controllers.get(id) || sas.expanders.get(id) || null;
        const ends = [tables(item.source), tables(item.target)];
        const rowRate = (table, phy) => table?.byPhy.get(phy)?.rate ?? null;
        for (const lane of lanes.rows) {
          const read = [rowRate(ends[0], lane.source), rowRate(ends[1], lane.target)];
          if (read.some((rate) => rate === null || rate !== lane.rate)) {
            lane.rate = null;
            lanes.rate = null;
          }
        }
        // A list without pairs: every PHY each end lists must read the list's
        // rate (an end that lists none, such as a far end not read, adds none).
        if (!lanes.rows.length && lanes.rate !== null) {
          const listed = [[ends[0], lanes.sourcePhys], [ends[1], lanes.targetPhys]].filter(([, phys]) => phys.length);
          if (listed.some(([table, phys]) => phys.some((phy) => rowRate(table, phy) !== lanes.rate))) lanes.rate = null;
        }
      }
      const laneLink = LANE_LINKS.has(item.kind);
      const entry = {
        id: item.id,
        kind: item.kind,
        source: item.source,
        target: item.target,
        status: item.status || "",
        bundle: false,
        lanes: laneLink ? lanes : null,
        lanesState: laneLink ? (lanes ? "reported" : "not_reported") : "n/a",
        labelLines: [],
        memberLinkIds: [],
        count: 1,
      };
      if (entry.lanesState === "reported") {
        entry.labelLines = [`x${lanes.count} · ${laneRate(lanes)}`, lanes.targetPhys.length && !lanes.targetBasis
          ? `PHY ${phyRanges(lanes.sourcePhys) || "?"} ↔ ${phyRanges(lanes.targetPhys)}`
          : lanes.basis || lanes.targetBasis ? `PHY ${phyRanges(lanes.sourcePhys) || "?"} ${farEnd(lanes)}` : `PHY ${phyRanges(lanes.sourcePhys) || "?"} ↔ ?`];
      } else if (entry.lanesState === "not_reported") {
        entry.labelLines = ["lanes not reported"];
      }
      links.push(entry);
      linkById.set(entry.id, entry);
    }

    // Physical enclosures (CORE): the controller and SES rows of one enclosure
    // become one node, drawn as the head of its bay block; the lines into a
    // controller row become that enclosure's paths, drawn below as bundles.
    const bayLocated = (item) => {
      const found = traces.get(item.id);
      const slot = Number.isInteger(item.slot) ? inventory.get(item.slot) : null;
      return locationKnown(item.metrics, item.raw, found?.kind === "bay" ? found.metrics : null, slot, slot?.raw_status);
    };
    const physical = mode !== "unknown"
      ? physicalEnclosures({ nodes, links, bayLinks, bayRows: bayRows.filter((item) => payloadNodeIds.get(item.id) === item), payloadNodeIds, inventory, located: bayLocated })
      : null;
    if (physical) {
      const consumed = new Set(physical.enclosures.flatMap((enclosure) => [...(enclosure.canonical ? [enclosure.canonical] : []), ...enclosure.observations, ...enclosure.ses.map((ses) => ses.entry)].map((entry) => entry.id)));
      const parentLinks = new Set(physical.enclosures.flatMap((enclosure) => [...enclosure.parents.values()].flat()));
      for (const link of links.slice()) {
        if (!consumed.has(link.source) && !consumed.has(link.target)) continue;
        links.splice(links.indexOf(link), 1);
        linkById.delete(link.id);
        if (!parentLinks.has(link) || consumed.has(link.source)) skipped.unjoined.push(link.id);
      }
      for (const enclosure of physical.enclosures) {
        for (const [id, parents] of enclosure.parents) enclosure.parents.set(id, parents.filter((link) => !consumed.has(link.source)));
      }
      for (let index = nodes.length - 1; index >= 0; index -= 1) {
        if (!consumed.has(nodes[index].id)) continue;
        nodeById.delete(nodes[index].id);
        nodes.splice(index, 1);
      }
      for (const enclosure of physical.enclosures) {
        const members = [...(enclosure.canonical ? [enclosure.canonical] : []), ...enclosure.observations, ...enclosure.ses.map((ses) => ses.entry)];
        // A friendly name saved on any HBA's row for the enclosure names it;
        // the first row (in the payload's order) is the one the editor saves to.
        const canonical = enclosure.canonical;
        enclosure.alias = canonical?.alias || enclosure.observations.map((entry) => entry.alias).find(Boolean) || "";
        // A row's own label, where it is a name and not the id again.
        enclosure.title = enclosure.observations.find((entry) => entry.label && entry.label.toLowerCase() !== enclosure.identity && entry.label !== entry.rawId)?.label || "";
        if (shared) {
          // One backplane two heads report: named as that, not as a head.
          const profile = isRecord(options.inventory?.selected_profile) ? options.inventory.selected_profile : {};
          enclosure.title = "Shared backplane";
          enclosure.name = text(profile.label);
          enclosure.sharedBy = shared.heads.map((head) => head.label);
        }
        enclosure.sesNames = [...enclosure.ses.map((ses) => ses.device), ...enclosure.observations.filter((entry) => entry.kind === "ses-enclosure" && entry.rawId).map((entry) => entry.rawId)];
        const mine = canonical || enclosure.observations.find((entry) => !entry.peer) || enclosure.observations[0];
        enclosure.aliasTarget = { objectId: mine.id, objectKind: mine.kind };
        const entry = {
          id: enclosure.id,
          kind: "enclosure",
          tier: "enclosures",
          label: enclosure.alias || enclosure.title || (enclosure.name ? `${enclosure.name} · ${enclosure.identity}` : enclosure.identity),
          rawId: enclosure.identity,
          controllerId: "",
          status: "",
          level: null,
          facts: [],
          phyGroups: [],
          parentUnknown: false,
          physical: enclosure,
          memberIds: members.map((member) => member.id),
          // What a search may name it by: its rows' own names and ids.
          aliases: members.flatMap((member) => [member.label, member.rawId, member.id]).filter(Boolean),
          x: 0,
          y: 0,
          w: NODE_W,
          h: NODE_H,
        };
        enclosure.node = entry;
        nodes.push(entry);
        nodeById.set(entry.id, entry);
      }
      stackEnclosures(physical, inventoryLayout(fabric, options.inventory), new Map(bayRows.map((item) => [item.id, Number.isInteger(item.slot) ? item.slot : null])));
    }

    const incoming = new Map();
    const outgoing = new Map();
    for (const item of links) {
      if (!incoming.has(item.target)) incoming.set(item.target, []);
      if (!outgoing.has(item.source)) outgoing.set(item.source, []);
      incoming.get(item.target).push(item);
      outgoing.get(item.source).push(item);
    }
    for (const entry of nodes) {
      if (entry.kind === "controller") {
        entry.phyGroups = list(outgoing.get(entry.id))
          .filter((item) => item.lanesState === "reported" && item.lanes.sourcePhys.length)
          .map((item) => ({ phys: item.lanes.sourcePhys, target: item.target, linkId: item.id }));
      }
    }

    // Bays: grouped by the set of drawn nodes that link to them.
    const baySources = new Map();
    const bayLinkIds = new Map();
    for (const item of bayLinks) {
      if (!baySources.has(item.target)) baySources.set(item.target, new Set());
      baySources.get(item.target).add(item.source);
      const key = `${item.source}->${item.target}`;
      if (!bayLinkIds.has(key)) bayLinkIds.set(key, []);
      bayLinkIds.get(key).push(item.id);
    }
    // Path states from path-bay links, only from links admitted above (one per
    // id, both ends present), for bays whose trace carries none.
    const pathStatesByBay = new Map();
    for (const item of pathBayLinks) {
      const controller = String(item.source || "").split(":")[1] || "";
      if (!pathStatesByBay.has(item.target)) pathStatesByBay.set(item.target, []);
      pathStatesByBay.get(item.target).push({ controller, state: String(item.status || "unknown"), basis: "" });
    }
    const groupsByKey = new Map();
    const groupByBay = new Map();
    const bays = [];
    const controllerNodes = nodes.filter((entry) => entry.kind === "controller");
    // A controller by its payload id only; a label, the payload's or a
    // Friendly Label, is what is shown, never what is joined.
    const controllerOf = (named, display = "") => {
      const owner = named ? controllerNodes.find((entry) => entry.id === `controller:${named}` || entry.id === named) : null;
      // An id the graph does not have is kept as the payload's own text, for
      // joins; the label is display only.
      return { controller: owner ? owner.label : display || named, controllerId: owner ? owner.id : "", controllerRaw: owner ? owner.id : named ? `raw:${named}` : "" };
    };
    // Two paths name one controller only when they name the same id, the
    // graph's or the payload's own; never by a label, and never when neither
    // names one.
    const sameController = (left, right) => Boolean(left.controllerRaw) && left.controllerRaw === right.controllerRaw;
    for (const item of bayRows) {
      if (payloadNodeIds.get(item.id) !== item) continue;
      const found = traces.get(item.id);
      const trace = found?.kind === "bay" ? found : null;
      const metrics = isRecord(item.metrics) ? item.metrics : {};
      // A path names its controller by the tail of its id, or by a label
      // only one controller has. Joins use the id; the label is display.
      const traced = list(trace?.metrics?.path_states).filter(isRecord).map((state) => ({
        ...controllerOf(text(state.controller), text(state.controller_label)),
        // A state the payload did not read is shown as not read everywhere.
        state: isUnreadState(state) ? STATE_UNREAD : String(state.state),
        basis: String(state.state_basis ?? ""),
        device: text(state.device_name),
        // Its port's reported PHY rows through the shared resolver: a count
        // only where the reference resolves; otherwise not read (#1494).
        transportPhyCount: window.SasFabricTransport?.pathState?.(fabric, state)?.transportPhyCount,
        raw: state,
      }));
      const pathStates = traced.length ? traced : list(pathStatesByBay.get(item.id)).map((state) => ({ ...state, ...controllerOf(state.controller) }));
      const slot = Number.isInteger(item.slot) ? item.slot : null;
      const inventorySlot = slot === null ? null : inventory.get(slot);
      const located = locationKnown(metrics, item.raw, trace?.metrics, inventorySlot, inventorySlot?.raw_status);
      const bay = {
        bayId: item.id,
        traceId: trace ? trace.id : "",
        slot,
        number: bayNumberOf(item.label, inventorySlot?.slot_label),
        label: nodeLabel(item),
        locationKnown: located,
        status: item.status || "",
        occupancy: bayOccupancy(metrics),
        deviceName: metrics.device_name || trace?.metrics?.device_name || "",
        terms: searchTerms(item, trace, inventorySlot),
        identities: identityTerms(item, trace, inventorySlot),
        reported: reportedPlace(item, trace, inventorySlot),
        pathStates,
        ports: [],
        sources: [...(baySources.get(item.id) || [])].sort(naturalCompare),
        x: 0,
        y: 0,
        w: CELL_BOX,
      };
      // One port per path: the path's state and, where the payload has the
      // HBA's device row for it, its SAS address and its expander PHY. A
      // path that reports the disk's own SAS data (Linux SES) carries that:
      // the address the disk's port is attached to, the disk's PHY, its SCSI
      // address. Each value is the payload's; none is derived.
      const devices = list(trace?.metrics?.mpr_devices).filter(isRecord);
      const taken = new Set();
      const port = (state, device) => {
        const joined = device ? joinPort(sas, device)
          : { expanderId: "", phy: null, why: core ? "the payload has no port row for this path, so its expander and PHY are not matched" : "the payload has no expander PHY table for this path" };
        const owner = state || controllerOf(String(device.controller || ""));
        const raw = state?.raw || {};
        const spelled = shared ? expanderInPath(raw.device_name) : null;
        return {
          key: `${item.id}#${bay.ports.length}`,
          bayId: item.id,
          controller: owner.controller,
          controllerId: owner.controllerId,
          controllerRaw: owner.controllerRaw || "",
          state: state ? state.state : String(device.state ?? "unknown"),
          basis: state ? state.basis : "",
          device: text(device?.member_device_name) || state?.device || "",
          sasAddress: sasAddressOf(device?.sas_address) || sasAddressOf(raw.sas_address),
          handle: text(device?.handle),
          // The handle of what the disk's own path is attached to.
          parent: text(device?.parent),
          rate: device ? sasRate(device.speed) : null,
          protocol: protocolOf(device?.mpr_device, raw.target_port_protocol, raw.sas_device_type === "end device" ? raw.transport_protocol : ""),
          attached: sasAddressOf(raw.attached_sas_address) || spelled?.address || "",
          // The expander PHY the device path spells out, where it does.
          pathPhy: !sasAddressOf(raw.attached_sas_address) && spelled ? spelled.phy : null,
          diskPhy: sasAddressOf(raw.sas_address) ? text(raw.phy_identifier) : "",
          hctl: text(raw.scsi_hctl),
          sg: sasAddressOf(raw.sas_address) ? text(raw.sg_device) : "",
          expanderId: joined.expanderId,
          phy: joined.phy,
          // All PHYs of the port the payload names on that owner (a wide port).
          phys: joined.phy ? [joined.phy.phy] : list(device?.expander_phys).filter((phy) => Number.isInteger(phy)),
          phyBasis: joined.basis || "",
          why: joined.why,
        };
      };
      // A path takes only a row its own evidence names: the row of its own
      // member device, or the one row of its controller when that controller
      // has one path and neither names a member. Otherwise not read: never a
      // sibling path's row (#1504).
      const memberOf = (state) => text(state.raw?.member_device_name) || state.device;
      for (const state of pathStates) {
        const open = devices.map((device, at) => ({ device, at })).filter(({ device, at }) => !taken.has(at) && sameController(controllerOf(String(device.controller || "")), state));
        const member = memberOf(state);
        const named = open.filter(({ device }) => text(device.member_device_name));
        const own = member ? named.filter(({ device }) => text(device.member_device_name) === member) : [];
        const sole = open.length === 1 && pathStates.filter((other) => sameController(other, state)).length === 1 && (!member || !named.length) ? open : [];
        const found = own.length === 1 ? own[0] : sole[0] || null;
        if (found) taken.add(found.at);
        bay.ports.push(port(state, found ? found.device : null));
      }
      devices.forEach((device, at) => {
        if (!taken.has(at)) bay.ports.push(port(null, device));
      });
      bay.protocol = [...new Set(bay.ports.map((item) => item.protocol).filter(Boolean))].join("/");
      bay.heads = list(metrics.heads).filter(isRecord);
      // What the producer says it did not read for this bay (path states past a limit, ...).
      bay.notRead = list(trace?.metrics?.not_read).filter((key) => typeof key === "string");
      // How many it did not read, where it says (paths past its limit).
      bay.notReadCounts = isRecord(trace?.metrics?.not_read_counts) ? trace.metrics.not_read_counts : {};
      bays.push(bay);
      // A bay of a physical enclosure is in that enclosure's block, with or
      // without a disk; any other bay is grouped by the nodes that link to it.
      const enclosure = physical?.memberOf.get(item.id) || null;
      const key = enclosure ? enclosure.id : `${bay.sources.join("|")}#${located ? "located" : "logical"}`;
      if (!groupsByKey.has(key)) {
        const id = enclosure ? `group:${enclosure.id}` : `group:${bay.sources.join("|") || "unlinked"}${located ? "" : ":logical"}`;
        groupsByKey.set(key, { id, enclosureIds: enclosure ? [enclosure.id] : bay.sources, locationKnown: located, bays: [], enclosure });
      }
      groupsByKey.get(key).bays.push(bay);
    }
    const groups = [];
    const controllerIds = nodes.filter((entry) => entry.kind === "controller").map((entry) => entry.id);
    const viewLayout = inventoryLayout(fabric, options.inventory);
    const viewSlots = new Set(viewLayout ? viewLayout.rows.flat() : []);
    const unplaced = [];
    for (const raw of groupsByKey.values()) {
      raw.bays.sort((left, right) => (left.slot ?? Infinity) - (right.slot ?? Infinity) || naturalCompare(left.label, right.label));
      const expandKey = `topology:${raw.id}`;
      const collapsed = raw.bays.length > BAY_DETAIL_LIMIT && !own(expanded, expandKey);
      // Path state counts per controller, keyed by payload text: Maps.
      const summary = { bays: raw.bays.length, withDisk: 0, empty: 0, unknown: 0, noPath: 0, pathStates: new Map() };
      for (const bay of raw.bays) {
        // In an enclosure block the marks are in HBA order in every cell.
        if (raw.enclosure) {
          const rank = (state) => {
            const index = controllerIds.indexOf(state.controllerId);
            return index < 0 ? controllerIds.length : index;
          };
          bay.pathStates = bay.pathStates.map((state, index) => ({ state, index })).sort((left, right) => rank(left.state) - rank(right.state) || left.index - right.index).map((item) => item.state);
          bay.ports = bay.ports.map((state, index) => ({ state, index })).sort((left, right) => rank(left.state) - rank(right.state) || left.index - right.index).map((item) => item.state);
          if (bay.occupancy === "yes" && !bay.pathStates.length) summary.noPath += 1;
        }
        if (bay.occupancy === "yes") summary.withDisk += 1;
        else if (bay.occupancy === "no") summary.empty += 1;
        else summary.unknown += 1;
        for (const state of bay.pathStates) {
          const name = state.controller || "path";
          if (!summary.pathStates.has(name)) summary.pathStates.set(name, new Map());
          const counts = summary.pathStates.get(name);
          counts.set(state.state, (counts.get(state.state) || 0) + 1);
        }
      }
      // A disk without a physical location, and a bay whose label carries no
      // number, is drawn with its own label, so its cell is as wide as the
      // longest such label in the group.
      // The enclosure view's rows for this block, top row first, each cell
      // with its column in the view: a physical enclosure's own rows, and
      // for any other group of located bays the rows its bays are in. The
      // bays of every platform follow the enclosure view's grid.
      let rows = null;
      const bySlot = new Map(raw.bays.map((bay) => [bay.slot, bay]));
      // An enclosure the view's rows cross keeps its cells' rows and columns
      // in the view, in order, beside the other enclosure's gaps.
      const inView = raw.locationKnown && raw.bays.every((bay) => viewSlots.has(bay.slot));
      const gridRows = raw.enclosure?.layoutRows || (inView ? viewLayout.rows : null);
      if (raw.enclosure && !gridRows && viewLayout) {
        unplaced.push(enclosureName(raw.enclosure) || raw.enclosure.id);
      }
      if (gridRows) {
        rows = gridRows.map((row) => row.map((slot, column) => ({ bay: bySlot.get(slot), column })).filter((cell) => cell.bay)).filter((row) => row.length);
      }
      const labelled = raw.bays.filter((bay) => !raw.locationKnown || !bay.number);
      const cellWidth = Math.max(rows ? PHYS_CELL_W : CELL_BOX, ...labelled.map((bay) => bay.label.length * CHAR_W + 12));
      for (const bay of raw.bays) bay.w = cellWidth;
      const group = {
        id: raw.id,
        tier: "bays",
        enclosureIds: raw.enclosureIds,
        locationKnown: raw.locationKnown,
        bayIds: raw.bays.map((bay) => bay.bayId),
        bays: raw.bays,
        enclosure: raw.enclosure || null,
        breakColumns: rows ? list(raw.enclosure ? raw.enclosure.breaks : viewLayout?.breaks) : [],
        paths: [],
        rows: collapsed ? null : rows,
        cells: collapsed ? [] : rows ? rows.flat().map((cell) => cell.bay) : raw.bays,
        cellWidth,
        collapsed,
        expandable: raw.bays.length > BAY_DETAIL_LIMIT,
        expandKey,
        summary,
        x: 0,
        y: 0,
        w: 0,
        h: 0,
      };
      if (raw.enclosure) raw.enclosure.group = group;
      groups.push(group);
      for (const bay of raw.bays) groupByBay.set(bay.bayId, group);
    }
    const groupById = new Map(groups.map((group) => [group.id, group]));

    // A physical enclosure: one bundle per path, from the node the payload
    // links to the controller's enclosure row (its expander) into the block.
    // It carries that payload link and the row's links to the bays it sees a
    // disk in. A row no line leads to has a path with no line, which the
    // block says. SES rows are named in the block; no line is drawn for one.
    for (const enclosure of physical ? physical.enclosures : []) {
      const group = enclosure.group;
      for (const observation of enclosure.observations) {
        const carried = group.bays.filter((bay) => bay.sources.includes(observation.id));
        const bayLinksHere = carried.flatMap((bay) => list(bayLinkIds.get(`${observation.id}->${bay.bayId}`)));
        // A row that links only some bays links the ones it sees a disk in;
        // a row that links every bay says nothing by that, so its disks are
        // the bays that hold one. Told from the links, not from the row's kind.
        const seen = carried.length === group.bays.length ? carried.filter((bay) => bay.occupancy === "yes") : carried;
        const path = {
          id: observation.id,
          label: nodeById.get(observation.controllerId)?.label || observation.label,
          controllerId: observation.controllerId,
          handle: String(payloadNodeIds.get(observation.id)?.raw?.enc_handle || ""),
          bayIds: new Set(seen.map((bay) => bay.bayId)),
          linkIds: [],
          sourceIds: [],
        };
        enclosure.parents.get(observation.id).forEach((parent, index) => {
          const entry = {
            id: `bundle:${observation.id}->${group.id}${index ? `:${index + 1}` : ""}`,
            kind: "bay-bundle",
            source: parent.source,
            target: group.id,
            via: observation.id,
            status: "",
            bundle: true,
            lanes: null,
            lanesState: "n/a",
            labelLines: [plural(group.bays.length, "bay"), `${plural(seen.length, "disk")} seen`],
            memberLinkIds: [parent.id, ...bayLinksHere],
            count: seen.length,
          };
          links.push(entry);
          linkById.set(entry.id, entry);
          if (!incoming.has(group.id)) incoming.set(group.id, []);
          if (!outgoing.has(parent.source)) outgoing.set(parent.source, []);
          incoming.get(group.id).push(entry);
          outgoing.get(parent.source).push(entry);
          path.linkIds.push(entry.id);
          path.sourceIds.push(parent.source);
        });
        group.paths.push(path);
      }
      for (const ses of enclosure.ses) ses.linkIds = group.bays.flatMap((bay) => list(bayLinkIds.get(`${ses.entry.id}->${bay.bayId}`)));
      enclosure.node.groupId = group.id;
      enclosure.node.parentUnknown = group.paths.some((path) => !path.linkIds.length);
    }
    if (physical) {
      planBoards(physical, { nodes, nodeById, links, linkById, incoming, outgoing, controllerNodes });
      for (const enclosure of physical.enclosures) enclosure.node.facts = enclosureFacts(enclosure, enclosure.group, nodeById);
    }
    // An enclosure not drawn as a board (no layout rows to stack it by) still
    // has its expanders: the ones its paths come through, for the checks.
    if (physical && !physical.boards) {
      for (const enclosure of physical.enclosures) {
        const sources = [...new Set(enclosure.group.paths.flatMap((path) => path.sourceIds))].map((id) => nodeById.get(id)).filter((entry) => entry?.kind === "expander");
        enclosure.group.chipIds = sources.map((entry) => entry.id);
        for (const entry of sources) {
          entry.rows = [entry];
          entry.address = sasAddressOf(entry.rawId);
        }
      }
    }
    // Expanders on no board of this view: the enclosures before and after
    // this one in the chain, each drawn as a compact stub.
    planNeighbours({ nodes, nodeById, links, fabric, sas, inventory: options.inventory, hrefFor: typeof options.enclosureHref === "function" ? options.enclosureHref : null });

    // One bundle line per (source, bay group), carrying the payload link ids.
    for (const group of groups) {
      if (group.enclosure) continue;
      for (const sourceId of group.enclosureIds) {
        const memberLinkIds = group.bays.flatMap((bay) => list(bayLinkIds.get(`${sourceId}->${bay.bayId}`)));
        const entry = {
          id: `bundle:${sourceId}->${group.id}`,
          kind: "bay-bundle",
          source: sourceId,
          target: group.id,
          status: "",
          bundle: true,
          lanes: null,
          lanesState: "n/a",
          labelLines: [plural(memberLinkIds.length, group.locationKnown ? "bay" : "disk")],
          memberLinkIds,
          count: memberLinkIds.length,
        };
        links.push(entry);
        linkById.set(entry.id, entry);
        if (!incoming.has(group.id)) incoming.set(group.id, []);
        if (!outgoing.has(sourceId)) outgoing.set(sourceId, []);
        incoming.get(group.id).push(entry);
        outgoing.get(sourceId).push(entry);
      }
    }

    // Tier rows, top to bottom.
    const tiers = [];
    const tierNodes = (name) => nodes.filter((entry) => entry.tier === name);
    const unreadable = !payload.nodesReadable;
    const hosts = tierNodes("host");
    tiers.push({
      id: "host",
      label: "Host",
      state: hosts.length ? "drawn" : "not_read",
      note: hosts.length ? "" : "host not in the payload",
      nodeIds: hosts.map((entry) => entry.id),
    });
    const headEnds = tierNodes("heads");
    if (headEnds.length) tiers.push({ id: "heads", label: "Head ends", state: "drawn", note: plural(headEnds.length, "storage system"), nodeIds: headEnds.map((entry) => entry.id) });
    const controllers = tierNodes("controllers");
    if (core) {
      const status = countTier(fabric, "controllers", controllers.length, { one: "HBA", many: "HBAs", none: "no HBAs reported" });
      tiers.push({ id: "controllers", label: "HBAs", ...status, nodeIds: controllers.map((entry) => entry.id) });
      const expanders = tierNodes("expanders");
      const expanderStatus = countTier(fabric, "expanders", expanders.length, { one: "expander", many: "expanders", none: "no expanders reported (read complete)" });
      if (!expanders.length) {
        tiers.push({ id: "expanders", label: "Expanders", ...expanderStatus, nodeIds: [] });
      } else {
        const levels = [...new Set(expanders.map((entry) => entry.level))].sort((left, right) => (left ?? Infinity) - (right ?? Infinity));
        for (const level of levels) {
          const members = expanders.filter((entry) => entry.level === level);
          for (const entry of members) entry.tier = `expanders:${level ?? "unknown"}`;
          // An expander drawn as a chip on its board has no row of its own,
          // and one reached through a board stands below the boards.
          const inRow = members.filter((entry) => !entry.chipOf && !entry.below);
          if (!inRow.length) continue;
          // A row of nothing but stubs is named for what they are to this view.
          const stubs = inRow.every((entry) => entry.neighbour);
          const upstream = stubs && inRow.every((entry) => /^(Reached through|Further up)/.test(entry.neighbour.relation));
          tiers.push({
            id: `expanders:${level ?? "unknown"}`,
            label: upstream ? "Upstream" : stubs ? "Other enclosures" : level === null ? "Expanders, level not reported" : `Expanders, level ${level}`,
            state: expanderStatus.state,
            note: expanderStatus.state === "partial" ? expanderStatus.note : stubs ? `${plural(inRow.length, "enclosure")}${upstream ? " this one is reached through" : ""}${level === null ? "" : `, level ${level}`}` : plural(inRow.length, "expander"),
            nodeIds: inRow.map((entry) => entry.id),
          });
        }
      }
      const enclosures = tierNodes("enclosures");
      const enclosureStatus = countTier(fabric, "enclosures", enclosures.length, { one: "enclosure", many: "enclosures", none: "no enclosures reported (read complete)" });
      // Physical enclosures are the heads of their bay blocks: no row of their own.
      if (!physical) tiers.push({ id: "enclosures", label: "Enclosures", ...enclosureStatus, nodeIds: enclosures.map((entry) => entry.id) });
      else physical.status = enclosureStatus;
    } else if (mode === "unknown") {
      // No contract: draw the rows the payload has, say nothing about the rest.
      const rowOf = (id, label, members) => {
        if (members.length) tiers.push({ id, label, state: "drawn", note: plural(members.length, "item"), nodeIds: members.map((entry) => entry.id) });
      };
      rowOf("controllers", "Controllers or sources", controllers);
      const expanders = tierNodes("expanders");
      const levels = [...new Set(expanders.map((entry) => entry.level))].sort((left, right) => (left ?? Infinity) - (right ?? Infinity));
      for (const level of levels) {
        const members = expanders.filter((entry) => entry.level === level);
        rowOf(`expanders:${level ?? "unknown"}`, level === null ? "Expanders, level not reported" : `Expanders, level ${level}`, members);
        for (const entry of members) entry.tier = `expanders:${level ?? "unknown"}`;
      }
      rowOf("devices", "Storage paths", tierNodes("devices"));
      rowOf("enclosures", "Enclosures", tierNodes("enclosures"));
    } else {
      tiers.push({
        id: "controllers",
        label: shared ? "Head ends" : "Source",
        state: controllers.length ? "drawn" : "none",
        note: controllers.length ? plural(controllers.length, shared ? "storage system" : "source") : "no source reported",
        nodeIds: controllers.map((entry) => entry.id),
      });
      tiers.push({ id: "not-collected", label: shared ? "Controllers and expanders" : "HBAs and expanders", state: "not_collected", note: shared ? notCollectedReason(fabric) : NOT_COLLECTED, nodeIds: [] });
      const devices = tierNodes("devices");
      if (devices.length) {
        tiers.push({
          id: "devices",
          label: kind === "linux_ses" ? "SES devices" : "Storage paths",
          state: "drawn",
          note: plural(devices.length, "device"),
          nodeIds: devices.map((entry) => entry.id),
        });
      }
      const enclosures = tierNodes("enclosures");
      if (physical) physical.status = { state: "drawn", note: plural(enclosures.length, "enclosure") };
      else {
        tiers.push({
          id: "enclosures",
          label: "Enclosures",
          state: enclosures.length ? "drawn" : "none",
          note: enclosures.length ? plural(enclosures.length, "enclosure") : "no enclosure reported",
          nodeIds: enclosures.map((entry) => entry.id),
        });
      }
    }
    const located = bays.filter((bay) => bay.locationKnown).length;
    const logical = bays.length - located;
    let bayNote = "no bays in this payload";
    if (located && logical) bayNote = `${plural(located, "bay")}; ${plural(logical, "disk")} without a physical location`;
    else if (logical) bayNote = `${plural(logical, "disk")}; ${LOCATION_UNKNOWN}`;
    else if (located) bayNote = plural(located, "bay");
    if (physical?.stacked) {
      // One row per enclosure, in the enclosure view's order, top first; the
      // first block's head is above its bays and the last one's below, so the
      // bay rows of the blocks sit together as they do in the enclosure view.
      const last = physical.enclosures.length - 1;
      physical.enclosures.forEach((enclosure, index) => {
        const group = enclosure.group;
        group.stack = true;
        group.headAt = index === last && last > 0 && !group.expandable ? "bottom" : "top";
        group.caption = index === last ? physical.edgeLabel : "";
        tiers.push({
          id: `bays:${enclosure.id}`,
          label: shared ? "Shared backplane" : enclosure.board ? "Backplane" : "Enclosure",
          state: physical.status.state === "partial" ? "partial" : "drawn",
          note: physical.status.state === "partial" ? physical.status.note : bayRange(group).toLowerCase(),
          nodeIds: [group.id],
          stack: true,
        });
      });
      const rest = groups.filter((group) => !group.enclosure);
      if (rest.length) {
        tiers.push({ id: "bays", label: "Other bays", state: "drawn", note: plural(rest.reduce((sum, group) => sum + group.bayIds.length, 0), "bay"), nodeIds: rest.map((group) => group.id) });
      }
      const depths = [...new Set(nodes.filter((entry) => entry.below).map((entry) => entry.below))].sort((left, right) => left - right);
      for (const depth of depths) {
        const members = nodes.filter((entry) => entry.below === depth);
        tiers.push({ id: `beyond:${depth}`, label: depth === 1 ? "Beyond this backplane" : "Further down", state: "drawn", note: `${plural(members.length, "enclosure")} reached through ${depth === 1 ? "it" : "those above"}`, nodeIds: members.map((entry) => entry.id) });
      }
    } else {
      tiers.push({
        id: "bays",
        label: physical ? "Enclosures and bays" : logical && !located ? "Disks" : logical ? "Bays and disks" : "Bays",
        state: bays.length ? "drawn" : "none",
        note: physical ? `${physical.status.state === "partial" ? physical.status.note : plural(physical.enclosures.length, "enclosure")}; ${bayNote}` : bayNote,
        nodeIds: groups.map((group) => group.id),
      });
    }
    if (payload.problems.length && !payload.nodesReadable) {
      // Node entries could not all be read: nothing drawn is a measured zero.
      for (const tier of tiers) {
        if ((tier.state === "none" || tier.state === "drawn") && !tier.nodeIds.length) {
          tier.state = "not_read";
          tier.note = "payload not readable";
        } else if (unreadable && tier.state === "none") {
          tier.state = "not_read";
        }
      }
    }

    // A link that points up the tiers (an expander to the host, say) is named
    // and not drawn: lines run from a tier down to a lower one.
    const rowOf = new Map();
    tiers.forEach((tier, index) => tier.nodeIds.forEach((id) => rowOf.set(id, index)));
    for (const link of links.slice()) {
      if (link.bundle) continue;
      const from = rowOf.get(link.source);
      const to = rowOf.get(link.target);
      if (from === undefined || to === undefined || to > from || (to === from && to > 0)) continue;
      skipped.upward.push(link.id);
      links.splice(links.indexOf(link), 1);
      linkById.delete(link.id);
      incoming.set(link.target, list(incoming.get(link.target)).filter((item) => item !== link));
      outgoing.set(link.source, list(outgoing.get(link.source)).filter((item) => item !== link));
    }
    for (const entry of nodes) {
      if (entry.physical) continue;
      if (["expanders", "enclosures", "devices", "controllers", "heads"].includes(entry.tier.replace(/:.*$/, "")) && !list(incoming.get(entry.id)).length) {
        entry.parentUnknown = true;
      }
    }

    const model = {
      kind,
      core,
      tiers,
      nodes,
      nodeById,
      links,
      linkById,
      groups,
      groupById,
      groupByBay,
      bays,
      bayById: new Map(bays.map((bay) => [bay.bayId, bay])),
      incoming,
      outgoing,
      parts: modelParts(fabric, core),
      physical,
      shared,
      transport,
      tailDigits,
      // The role of the head end each path's source belongs to: its own
      // label without transport, its HBA's label with it.
      roleOf: new Map(shared ? nodes.filter((entry) => entry.head).flatMap((entry) => {
        const info = shared.heads.find((head) => head.controller_id === entry.id);
        const role = { role: entry.head.role, head: entry.label };
        // By the payload's id first; by its label where a path names no id.
        return [[entry.id, role], [info?.hba_label || entry.label, role]];
      }) : []),
      warnings: list(fabric?.warnings).filter((item) => typeof item === "string"),
      // What a path's upper end is called here.
      sideWord: core ? "HBA" : shared ? "head" : "source",
      sas,
      // The system's other enclosures, as the inventory of this scope names them.
      otherEnclosures: inventory.size ? list(options.inventory?.enclosures).filter((item) => isRecord(item) && item.id !== fabric.selected_enclosure_id).map((item) => text(item.label) || text(item.id)).filter(Boolean) : [],
      portByKey: new Map(bays.flatMap((bay) => bay.ports.map((port) => [port.key, port]))),
      // The transport census as the payload lists it: which PHYs it expects.
      censusRows: censusOf(fabric),
      checks: [],
      flags: new Map(),
      contract: mode,
      notices: [],
      counts: { bayNodes: bayRows.length, baysPlaced: groups.reduce((total, group) => total + group.bayIds.length, 0) },
      scope: scopeOf(fabric),
      width: 0,
      height: 0,
    };
    if (contract.notice) model.notices.push(contract.notice);
    // Bays with a place in the chassis but no rows to lay them out by: said,
    // because the order drawn is then the bay numbers', not the chassis's.
    if (inventory.size && !viewLayout && bays.some((bay) => bay.locationKnown)) {
      model.notices.push("The inventory gives no layout rows for this enclosure: its bays are drawn in bay-number order, not as they sit in the chassis.");
    }
    if (unplaced.length) {
      model.notices.push(`The inventory's layout rows do not place every bay of ${unplaced.join(", ")}: ${unplaced.length === 1 ? "its" : "their"} bays are drawn in bay-number order, not as they sit in the chassis.`);
    }

    if (transport && !transport.resolved) model.notices.push("The transport port helper (sas_fabric_transport.js) is not loaded: each disk's expander PHY is not read.");
    if (payload.problems.length) {
      model.notices.push(`Payload not readable: ${payload.problems.join("; ")}. What could not be read is not drawn.`);
    }
    const notDrawn = [];
    if (skipped.unsupported.length) notDrawn.push(`${plural(skipped.unsupported.length, "node")} of a kind this view does not draw (${named(skipped.unsupported)})`);
    if (skipped.missingEnd.length) notDrawn.push(`${plural(skipped.missingEnd.length, "link")} with an end the payload does not carry (${named(skipped.missingEnd)})`);
    if (skipped.unjoined.length) notDrawn.push(`${plural(skipped.unjoined.length, "link")} between items this view does not join (${named(skipped.unjoined)})`);
    if (skipped.duplicateNodes.length) notDrawn.push(`${plural(skipped.duplicateNodes.length, "duplicate node id")} (${named(skipped.duplicateNodes)})`);
    const orphanTraces = [...traces.values()].filter((trace) => trace.kind === "bay" && !payloadNodeIds.has(trace.id)).map((trace) => trace.id);
    if (orphanTraces.length) notDrawn.push(`${plural(orphanTraces.length, "bay trace")} without a bay node (${named(orphanTraces)})`);
    if (skipped.duplicateTraces.length) notDrawn.push(`${plural(skipped.duplicateTraces.length, "duplicate trace id")} (${named(skipped.duplicateTraces)}); the first trace of each id was kept`);
    if (skipped.unsupportedTraces.length) notDrawn.push(`${plural(skipped.unsupportedTraces.length, "trace")} of a kind this view does not draw (${named(skipped.unsupportedTraces)})`);
    if (skipped.upward.length) notDrawn.push(`${plural(skipped.upward.length, "link")} that ${skipped.upward.length === 1 ? "points" : "point"} up the tiers (${named(skipped.upward)})`);
    if (skipped.duplicateLinks.length) notDrawn.push(`${plural(skipped.duplicateLinks.length, "duplicate link id")} (${named(skipped.duplicateLinks)})`);
    if (model.counts.bayNodes !== model.counts.baysPlaced) {
      notDrawn.push(`${plural(model.counts.bayNodes, "bay row")} in the payload, ${plural(model.counts.baysPlaced, "bay")} drawn`);
    }
    if (notDrawn.length) {
      model.notices.push(`Not drawn: ${notDrawn.join("; ")}.`);
    }
    for (const enclosure of physical ? physical.enclosures : []) {
      const inferred = enclosure.ses.filter((ses) => ses.basis === "coverage").map((ses) => ses.device);
      if (inferred.length) {
        model.notices.push(`${named(inferred)} ${inferred.length === 1 ? "is" : "are"} shown with enclosure ${enclosure.identity} because ${inferred.length === 1 ? "it reports" : "they report"} every bay the HBAs see a disk in there and none of another enclosure: `
          + `the payload does not name the SES device's enclosure, so this is inferred${enclosure.inferredBays ? `, and so is the place of ${plural(enclosure.inferredBays, "bay")} no HBA reports a disk in` : ""}.`);
      }
    }
    // One notice per large bay group, collapsed or drawn in full, so that
    // expanding or collapsing a shelf does not move the drawing on the page.
    for (const group of groups) {
      if (!group.expandable) continue;
      const names = group.enclosureIds.map((id) => nodeById.get(id)?.label || id).join(" and ");
      const what = `${group.bayIds.length} ${group.locationKnown ? "bays" : "disks"}${names ? ` behind ${names}` : ""}`;
      model.notices.push(group.collapsed
        ? `${what} are shown as a summary block; expand it to draw each one.`
        : `${what} are drawn in full; collapse them to a summary block to shorten the drawing.`);
    }
    context.unmatchedTargets(model);
    context.wiringChecks(model, fabric);
    // A cable whose members are not read says so on its drawn label: its
    // listed PHYs are what the payload lists, not established members.
    for (const link of model.links) {
      if (link.lanes && list(link.labelLines).length && context.LANE_LINKS.has(link.kind) && !context.membershipRead(model, link)) link.labelLines = [...link.labelLines, "membership not read"];
    }
    model.transportNote = context.transportNote(model);
    finishNeighbours(model);
    // What the payload reports beyond a box's own lines, for its name, the
    // trace line and the hover card: SAS detail where the payload has it, and
    // the wiring checks' flags.
    for (const entry of model.nodes) {
      const detailed = entry.kind === "controller" ? core && sas.controllers.has(entry.id) : entry.kind === "expander" && (sas.expanders.has(entry.id) || Boolean(entry.reported) || Boolean(entry.stubGroup));
      const rows = detailed ? context.cardFor(model, { kind: "node", id: entry.id }).rows : context.flagRows(model, "node", entry.id);
      entry.extra = rows.filter((row) => !entry.facts.includes(row.value)).map((row) => `${row.label}: ${row.value}`);
      entry.flagged = entry.stubGroup ? entry.groupTier : context.flagTier(model, "node", entry.id);
    }
    for (const bay of bays) {
      bay.extra = [
        ...bay.ports.filter((port) => port.phy || port.sasAddress).map((port) => `via ${port.controller}: ${context.portBrief(port, model)}`).filter((line, index, all) => all.indexOf(line) === index),
        ...context.flagRows(model, "bay", bay.bayId).map((row) => `${row.label}: ${row.value}`),
      ];
      bay.flagged = context.flagTier(model, "bay", bay.bayId);
    }
    context.layoutModel(model, options);
    model.box = { width: Number(options.width) || 0, height: Number(options.height) || 0 };
    return model;
  }

function planBoards(physical, context) {
    const { nodes, nodeById, links, linkById, outgoing, controllerNodes } = context;
    if (!physical.stacked) return false;
    const order = (entry) => {
      const index = controllerNodes.findIndex((item) => item.id === entry.controllerId);
      return index < 0 ? controllerNodes.length : index;
    };
    const plans = [];
    const claimed = new Set();
    for (const enclosure of physical.enclosures) {
      const group = enclosure.group;
      const rows = [];
      for (const path of group.paths) {
        const row = path.sourceIds.length === 1 ? nodeById.get(path.sourceIds[0]) : null;
        if (row?.kind !== "expander") continue;
        path.rowId = row.id;
        if (!rows.includes(row)) rows.push(row);
      }
      const chips = [];
      if (rows.length) {
        // Every path comes through an expander row no other enclosure has.
        if (group.paths.some((path) => !path.rowId) || rows.some((row) => claimed.has(row.id))) return false;
        // The same expander as another HBA sees it (one row per host, one
        // SAS address) is a row of the same chip, whether or not a path of
        // this view's bays was joined through it.
        const addresses = new Set(rows.map((row) => sasAddressOf(row.rawId)).filter(Boolean));
        for (const twin of nodes) {
          if (twin.kind !== "expander" || rows.includes(twin) || claimed.has(twin.id) || !addresses.has(sasAddressOf(twin.rawId))) continue;
          if (rows.some((row) => row.controllerId === twin.controllerId && sasAddressOf(row.rawId) === sasAddressOf(twin.rawId))) continue;
          rows.push(twin);
        }
        rows.forEach((row) => claimed.add(row.id));
        rows.sort((left, right) => order(left) - order(right) || naturalCompare(left.id, right.id));
        const byAddress = new Map();
        for (const row of rows) {
          const address = sasAddressOf(row.rawId) || row.rawId || row.label;
          if (!byAddress.has(address)) byAddress.set(address, []);
          byAddress.get(address).push(row);
        }
        for (const [address, members] of byAddress) {
          // One row per HBA: two rows of one address on one HBA are not one chip.
          if (new Set(members.map((row) => row.controllerId)).size !== members.length) return false;
          chips.push({ address, rows: members });
        }
      } else {
        const counts = new Map();
        for (const bay of group.bays) {
          for (const port of bay.ports) {
            if (!port.attached || port.expanderId) continue;
            if (!counts.has(port.attached)) counts.set(port.attached, []);
            counts.get(port.attached).push(port);
          }
        }
        if (!counts.size) return false;
        // In the order of the heads that name them, then by address.
        const first = (ports) => Math.min(...ports.map((port) => controllerNodes.findIndex((entry) => entry.id === port.controllerId)).map((index) => (index < 0 ? controllerNodes.length : index)));
        for (const [address, ports] of [...counts].sort((left, right) => first(left[1]) - first(right[1]) || naturalCompare(left[0], right[0]))) {
          chips.push({ address, rows: [], reportedBy: ports.length, fromPath: ports.every((port) => port.pathPhy !== null), namedBy: [...new Set(ports.map((port) => port.controller).filter(Boolean))] });
        }
      }
      plans.push({ enclosure, chips });
    }
    if (!plans.length) return false;
    const busLinks = links.filter((link) => link.bundle);
    for (const { enclosure, chips } of plans) {
      const group = enclosure.group;
      group.chipIds = [];
      for (const chip of chips) {
        let primary = chip.rows[0];
        if (!primary) {
          // Known only from its disks: each reports this address as the one
          // its port is attached to. Its own PHY table and its uplink are
          // not in the payload.
          primary = {
            id: `expander:reported:${enclosure.identity}:${chip.address}`, kind: "expander", tier: "expanders", label: "", alias: "", rawId: chip.address,
            controllerId: "", status: "", level: null, facts: [], phyGroups: [], parentUnknown: false, reported: chip.reportedBy, fromPath: chip.fromPath, namedBy: chip.namedBy,
            x: 0, y: 0, w: NODE_W, h: NODE_H,
          };
          nodes.push(primary);
          nodeById.set(primary.id, primary);
          const disks = group.bays.filter((bay) => bay.ports.some((port) => port.attached === chip.address && !port.expanderId));
          for (const bay of disks) {
            for (const port of bay.ports) {
              if (port.attached !== chip.address || port.expanderId) continue;
              port.expanderId = primary.id;
              port.why = "the expander's PHY table is not in the payload";
              port.fromPath = port.pathPhy !== null;
            }
          }
          const bus = {
            id: `bus:${primary.id}`, kind: "bay-bundle", source: primary.id, target: group.id, via: null, status: "", bundle: true, bus: true, reported: true,
            lanes: null, lanesState: "n/a", labelLines: [], memberLinkIds: [], count: disks.length,
          };
          links.push(bus);
          linkById.set(bus.id, bus);
          outgoing.set(primary.id, [bus]);
          primary.busId = bus.id;
        }
        const rows = chip.rows.length ? chip.rows : [primary];
        const rowIds = rows.map((row) => row.id);
        const rest = primary.facts.filter((fact) => !/^level /.test(fact));
        const alias = rows.map((row) => row.alias).find(Boolean) || "";
        primary.rows = rows;
        primary.address = chip.address;
        primary.viaLine = chip.rows.length ? viaRows(rows, nodeById)
          : chip.fromPath ? `from device path${chip.namedBy.length ? ` (${chip.namedBy.join(", ")})` : ""}` : `attached address of ${plural(chip.reportedBy, "disk port")}`;
        primary.aliases = [chip.address, ...rows.flatMap((row) => [row.label, row.rawId, row.id])].filter(Boolean);
        primary.label = alias || `Expander ${tail(chip.address)}`;
        primary.facts = [primary.viaLine, chip.address, ...(chip.rows.length ? rest : ["PHY table not read"])];
        for (const row of rows) {
          row.chipOf = group.id;
          if (row !== primary) row.mergedInto = primary.id;
        }
        for (const path of group.paths) if (rowIds.includes(path.rowId)) path.chipId = primary.id;
        // One bus per chip: the first of its rows' bundles is the one drawn.
        const buses = chip.rows.length ? busLinks.filter((link) => rowIds.includes(link.source) && link.target === group.id) : [linkById.get(primary.busId)];
        buses.forEach((link, index) => {
          link.bus = true;
          link.labelLines = [];
          if (index) link.busOf = buses[0].id;
        });
        if (buses[0]) buses[0].rowIds = rowIds;
        primary.busId = buses[0]?.id || "";
        group.chipIds.push(primary.id);
      }
      enclosure.node.label = `${enclosureName(enclosure)} · ${bayRange(group).toLowerCase()}`;
      enclosure.board = true;
    }
    for (const link of links) {
      if (link.bundle || link.lanesState === "n/a") continue;
      const target = nodeById.get(link.target);
      // A chip's cable to an expander that is on no board of this view (the
      // backplane of another enclosure): that expander stands below the
      // boards, reached by a rail like any other cable.
      const down = Boolean(nodeById.get(link.source)?.chipOf) && target?.kind === "expander" && !target.chipOf;
      if (!target?.chipOf && !down) continue;
      if (down) target.below = true;
      // A cable: beside a rail its label is three short lines.
      link.cable = true;
      if (link.lanesState === "reported") {
        link.labelLines = [`x${link.lanes.count} · ${laneRate(link.lanes)}`, `PHY ${phyRanges(link.lanes.sourcePhys) || "?"}`, farEnd(link.lanes)];
      }
    }
    physical.boards = true;
    return true;
  }

function viaRows(rows, nodeById) {
    const name = (row) => nodeById.get(row.controllerId)?.label || "an HBA not reported";
    const level = (row) => (row.level === null || row.level === undefined ? "level not reported" : `level ${row.level}`);
    if (new Set(rows.map(level)).size === 1) return `via ${rows.map(name).join(" + ")}, ${level(rows[0])}`;
    return `via ${rows.map((row) => `${name(row)} (${level(row)})`).join(" + ")}`;
  }

function chipOfNode(model, id) {
    const entry = model.nodeById.get(id);
    return entry?.mergedInto ? model.nodeById.get(entry.mergedInto) : entry || null;
  }

function nodeName(model, id) {
    const entry = chipOfNode(model, id);
    if (!entry) return id;
    // A stub is named as the expander it is, with its enclosure.
    if (entry.neighbour) return `${entry.address ? `Expander ${tail(entry.address)}` : "Expander"} (${entry.neighbour.name})`;
    return entry.label;
  }

function viaText(controller, level) {
    return `via ${controller || "an HBA not reported"}, level ${level === null || level === undefined ? "not reported" : level}`;
  }

function bayRange(group) {
    const numbers = group.bays.map((bay) => bay.number).filter(Boolean).sort((left, right) => Number(left) - Number(right));
    if (!numbers.length || numbers.length < group.bays.length) return "Bays";
    return numbers[0] === numbers[numbers.length - 1] ? `Bay ${numbers[0]}` : `Bays ${numbers[0]}–${numbers[numbers.length - 1]}`;
  }

function enclosureFacts(enclosure, group, nodeById) {
    const { summary } = group;
    const facts = [];
    const occupied = `${summary.withDisk} of ${summary.bays} with a disk${summary.unknown ? ` · ${summary.unknown} occupancy not read` : ""}`;
    const inferred = enclosure.ses.some((ses) => ses.basis === "coverage");
    const ses = list(enclosure.sesNames).length ? `SES ${enclosure.sesNames.join(", ")}${inferred ? " (joined by bay coverage, not reported)" : ""}` : "";
    const states = context.pathSummary(summary);
    const without = summary.noPath ? `${plural(summary.noPath, "disk")} with no path reported` : "";
    if (enclosure.board) {
      // A board: its chips say how it is reached; the head keeps its identity.
      const shown = enclosureName(enclosure);
      if (enclosure.sharedBy) facts.push(`one backplane, reported by ${enclosure.sharedBy.join(" and ")}`);
      facts.push([shown !== enclosure.name ? enclosure.name : "", shown !== enclosure.identity && !enclosure.sharedBy ? enclosure.identity : ""].filter(Boolean).join(" · "));
      facts.push(ses);
      facts.push([occupied, without].filter(Boolean).join(" · "));
      if (states) facts.push(states);
      return facts.filter(Boolean);
    }
    const paths = group.paths.map((path) => {
      const through = path.sourceIds.map((id) => {
        const source = nodeById.get(id);
        return source ? `${source.kind === "expander" ? "expander " : ""}${source.label}` : id;
      });
      return `${path.label} via ${through.length ? through.join(" and ") : "a parent link not reported"}`;
    });
    facts.push([bayRange(group), occupied, ses].filter(Boolean).join(" · "));
    if (paths.length) facts.push(paths.join(" · "));
    if (states || without) facts.push([states, without].filter(Boolean).join(" · "));
    return facts;
  }

function scopeOf(fabric) {
    return isRecord(fabric) ? `${fabric.system_id ?? ""}\u0000${fabric.selected_enclosure_id ?? ""}` : "";
  }

function modelParts(fabric, core) {
    const parts = [];
    for (const [name, source] of Object.entries(isRecord(fabric?.sources) ? fabric.sources : {})) {
      let state = "ok";
      if (!isRecord(source)) state = "not reported";
      else if (!source.enabled) state = "disabled";
      else if (!source.ok) state = source.required_ok ? "partial" : "failed";
      parts.push({ name, label: name, state, title: isRecord(source) ? source.message || "" : "" });
    }
    const caches = [fabric?.snapshot_cache_state, fabric?.source_cache_state];
    let cache = "not reported";
    if (caches.includes("trusted-fallback")) cache = "fallback";
    else if (caches.includes("stale-hit")) cache = "stale";
    else if (caches.every((value) => CACHE_FRESH.has(value))) cache = "ok";
    parts.push({ name: "cache", label: "cache", state: cache, title: caches.filter((value) => typeof value === "string").join(", ") });
    if (core) {
      for (const [domain, label] of [["controllers", "HBAs"], ["expanders", "expanders"], ["enclosures", "enclosures"]]) {
        const state = readState(fabric, domain);
        const shown = { complete: "ok", partial: "partial", unknown: "not reported" }[state] || "not read";
        parts.push({ name: domain, label, state: shown, title: "" });
      }
      if (Number(fabric?.raw?.controller_read?.unread_phy_tables) > 0) {
        parts.push({ name: "phy-tables", label: "PHY tables", state: "partial", title: "" });
      }
    }
    return parts;
  }

  Object.defineProperties(context, {
    NOT_COLLECTED: { configurable: true, get: () => NOT_COLLECTED },
    NOT_SHOWN: { configurable: true, get: () => NOT_SHOWN },
    NOT_SHOWN_COUNTERS: { configurable: true, get: () => NOT_SHOWN_COUNTERS },
    NOT_SHOWN_STORAGE: { configurable: true, get: () => NOT_SHOWN_STORAGE },
    NOT_SHOWN_SHARED: { configurable: true, get: () => NOT_SHOWN_SHARED },
    LOCATION_UNKNOWN: { configurable: true, get: () => LOCATION_UNKNOWN },
    BAY_DETAIL_LIMIT: { configurable: true, get: () => BAY_DETAIL_LIMIT },
    CELLS_PER_ROW: { configurable: true, get: () => CELLS_PER_ROW },
    CELL: { configurable: true, get: () => CELL },
    CELL_BOX: { configurable: true, get: () => CELL_BOX },
    MARK: { configurable: true, get: () => MARK },
    MARK_PITCH: { configurable: true, get: () => MARK_PITCH },
    NODE_W: { configurable: true, get: () => NODE_W },
    NODE_H: { configurable: true, get: () => NODE_H },
    TEXT_W: { configurable: true, get: () => TEXT_W },
    LINE_H: { configurable: true, get: () => LINE_H },
    NODE_MIN_CHARS: { configurable: true, get: () => NODE_MIN_CHARS },
    NODE_MAX_CHARS: { configurable: true, get: () => NODE_MAX_CHARS },
    TIER_LABEL_CHARS: { configurable: true, get: () => TIER_LABEL_CHARS },
    TIER_NOTE_CHARS: { configurable: true, get: () => TIER_NOTE_CHARS },
    BAND_CHARS: { configurable: true, get: () => BAND_CHARS },
    H_GAP: { configurable: true, get: () => H_GAP },
    LEFT: { configurable: true, get: () => LEFT },
    MARGIN: { configurable: true, get: () => MARGIN },
    CHAR_W: { configurable: true, get: () => CHAR_W },
    SUMMARY_W: { configurable: true, get: () => SUMMARY_W },
    PHYS_CELL_W: { configurable: true, get: () => PHYS_CELL_W },
    PHYS_CELL_H: { configurable: true, get: () => PHYS_CELL_H },
    PHYS_CELL_MAX: { configurable: true, get: () => PHYS_CELL_MAX },
    PHYS_CELL_MIN: { configurable: true, get: () => PHYS_CELL_MIN },
    PHYS_CELL_NAMED: { configurable: true, get: () => PHYS_CELL_NAMED },
    PHYS_CELL_TALL: { configurable: true, get: () => PHYS_CELL_TALL },
    PHYS_GAP: { configurable: true, get: () => PHYS_GAP },
    GROUP_GAP: { configurable: true, get: () => GROUP_GAP },
    STACK_GAP: { configurable: true, get: () => STACK_GAP },
    CAPTION_H: { configurable: true, get: () => CAPTION_H },
    RAIL_CLEAR: { configurable: true, get: () => RAIL_CLEAR },
    RAIL_STEP: { configurable: true, get: () => RAIL_STEP },
    STACK_ROOM: { configurable: true, get: () => STACK_ROOM },
    DOWN_STEP: { configurable: true, get: () => DOWN_STEP },
    BUS_STEP: { configurable: true, get: () => BUS_STEP },
    BUS_PAD: { configurable: true, get: () => BUS_PAD },
    CHIP_GAP: { configurable: true, get: () => CHIP_GAP },
    LEFT_BOARD: { configurable: true, get: () => LEFT_BOARD },
    BOARD_ROOM: { configurable: true, get: () => BOARD_ROOM },
    PHY_COUNTERS: { configurable: true, get: () => PHY_COUNTERS },
    NOT_READ: { configurable: true, get: () => NOT_READ },
    PROVISIONAL_PHY_THRESHOLDS: { configurable: true, get: () => PROVISIONAL_PHY_THRESHOLDS },
    THRESHOLD_NOTE: { configurable: true, get: () => THRESHOLD_NOTE },
    TREND_NOTE: { configurable: true, get: () => TREND_NOTE },
    TIER_WORDS: { configurable: true, get: () => TIER_WORDS },
    worse: { configurable: true, get: () => worse },
    phyTier: { configurable: true, get: () => phyTier },
    TOGGLE_H: { configurable: true, get: () => TOGGLE_H },
    SCROLL_BORDER: { configurable: true, get: () => SCROLL_BORDER },
    TRACK: { configurable: true, get: () => TRACK },
    GAP_PAD: { configurable: true, get: () => GAP_PAD },
    LABEL_GAP: { configurable: true, get: () => LABEL_GAP },
    LABEL_CLEAR: { configurable: true, get: () => LABEL_CLEAR },
    JUNCTION: { configurable: true, get: () => JUNCTION },
    NAME_LIMIT: { configurable: true, get: () => NAME_LIMIT },
    SIMPLIFY_ABOVE: { configurable: true, get: () => SIMPLIFY_ABOVE },
    STUB_GROUP_ABOVE: { configurable: true, get: () => STUB_GROUP_ABOVE },
    LANE_LINKS: { configurable: true, get: () => LANE_LINKS },
    HIDDEN_KINDS: { configurable: true, get: () => HIDDEN_KINDS },
    MEMBER_HEALTH: { configurable: true, get: () => MEMBER_HEALTH },
    CACHE_FRESH: { configurable: true, get: () => CACHE_FRESH },
    STATE_UNREAD: { configurable: true, get: () => STATE_UNREAD },
    isUnreadState: { configurable: true, get: () => isUnreadState },
    PATH_STATE_TONES: { configurable: true, get: () => PATH_STATE_TONES },
    PATH_CAPTIONS: { configurable: true, get: () => PATH_CAPTIONS },
    list: { configurable: true, get: () => list },
    isRecord: { configurable: true, get: () => isRecord },
    own: { configurable: true, get: () => own },
    escapeHtml: { configurable: true, get: () => escapeHtml },
    token: { configurable: true, get: () => token },
    finite: { configurable: true, get: () => finite },
    decimal: { configurable: true, get: () => decimal },
    naturalCompare: { configurable: true, get: () => naturalCompare },
    plural: { configurable: true, get: () => plural },
    named: { configurable: true, get: () => named },
    PLATFORM_WORDS: { configurable: true, get: () => PLATFORM_WORDS },
    NAMED_PLATFORMS: { configurable: true, get: () => NAMED_PLATFORMS },
    KIND_PLATFORMS: { configurable: true, get: () => KIND_PLATFORMS },
    GENERIC_KIND: { configurable: true, get: () => GENERIC_KIND },
    kindMatchesPlatform: { configurable: true, get: () => kindMatchesPlatform },
    fabricContract: { configurable: true, get: () => fabricContract },
    readState: { configurable: true, get: () => readState },
    farEnd: { configurable: true, get: () => farEnd },
    phyRanges: { configurable: true, get: () => phyRanges },
    laneRate: { configurable: true, get: () => laneRate },
    formatRate: { configurable: true, get: () => formatRate },
    nodeLabel: { configurable: true, get: () => nodeLabel },
    clip: { configurable: true, get: () => clip },
    bayOccupancy: { configurable: true, get: () => bayOccupancy },
    locationKnown: { configurable: true, get: () => locationKnown },
    readPayload: { configurable: true, get: () => readPayload },
    inventoryBySlot: { configurable: true, get: () => inventoryBySlot },
    searchTerms: { configurable: true, get: () => searchTerms },
    identityTerms: { configurable: true, get: () => identityTerms },
    bayNumberOf: { configurable: true, get: () => bayNumberOf },
    sasRate: { configurable: true, get: () => sasRate },
    text: { configurable: true, get: () => text },
    sasAddressOf: { configurable: true, get: () => sasAddressOf },
    tailDigits: { configurable: true, get: () => tailDigits, set: (value) => { tailDigits = value; } },
    tail: { configurable: true, get: () => tail },
    addressTail: { configurable: true, get: () => addressTail },
    protocolOf: { configurable: true, get: () => protocolOf },
    phyFacts: { configurable: true, get: () => phyFacts },
    sasIndex: { configurable: true, get: () => sasIndex },
    joinPort: { configurable: true, get: () => joinPort },
    rateText: { configurable: true, get: () => rateText },
    counterText: { configurable: true, get: () => counterText },
    errorDigest: { configurable: true, get: () => errorDigest },
    errorSummary: { configurable: true, get: () => errorSummary },
    phyReading: { configurable: true, get: () => phyReading },
    deviceRole: { configurable: true, get: () => deviceRole },
    deviceKind: { configurable: true, get: () => deviceKind },
    cableEndAccepts: { configurable: true, get: () => cableEndAccepts },
    NAME_NOT_UNIQUE: { configurable: true, get: () => NAME_NOT_UNIQUE },
    chipPhyNumbers: { configurable: true, get: () => chipPhyNumbers },
    readingSummary: { configurable: true, get: () => readingSummary },
    faultDigest: { configurable: true, get: () => faultDigest },
    disagreement: { configurable: true, get: () => disagreement },
    tierForNode: { configurable: true, get: () => tierForNode },
    hiddenByDesign: { configurable: true, get: () => hiddenByDesign },
    expanderLevel: { configurable: true, get: () => expanderLevel },
    countTier: { configurable: true, get: () => countTier },
    pathCaption: { configurable: true, get: () => pathCaption },
    nodeFacts: { configurable: true, get: () => nodeFacts },
    slotIdentities: { configurable: true, get: () => slotIdentities },
    inventoryLayout: { configurable: true, get: () => inventoryLayout },
    physicalEnclosures: { configurable: true, get: () => physicalEnclosures },
    enclosureName: { configurable: true, get: () => enclosureName },
    stackEnclosures: { configurable: true, get: () => stackEnclosures },
    planNeighbours: { configurable: true, get: () => planNeighbours },
    stubFacts: { configurable: true, get: () => stubFacts },
    enclosureOfExpander: { configurable: true, get: () => enclosureOfExpander },
    neighbourRows: { configurable: true, get: () => neighbourRows },
    neighbourText: { configurable: true, get: () => neighbourText },
    finishNeighbours: { configurable: true, get: () => finishNeighbours },
    adoptLinuxTransport: { configurable: true, get: () => adoptLinuxTransport },
    withLinuxTransport: { configurable: true, get: () => withLinuxTransport },
    deviceShort: { configurable: true, get: () => deviceShort },
    expanderInPath: { configurable: true, get: () => expanderInPath },
    notCollectedReason: { configurable: true, get: () => notCollectedReason },
    headFacts: { configurable: true, get: () => headFacts },
    headInfo: { configurable: true, get: () => headInfo },
    buildTopologyModel: { configurable: true, get: () => buildTopologyModel },
    planBoards: { configurable: true, get: () => planBoards },
    viaRows: { configurable: true, get: () => viaRows },
    chipOfNode: { configurable: true, get: () => chipOfNode },
    nodeName: { configurable: true, get: () => nodeName },
    viaText: { configurable: true, get: () => viaText },
    bayRange: { configurable: true, get: () => bayRange },
    enclosureFacts: { configurable: true, get: () => enclosureFacts },
    scopeOf: { configurable: true, get: () => scopeOf },
    modelParts: { configurable: true, get: () => modelParts },
  });
  window.SasFabricTopologyEvidence(context);
  window.SasFabricTopologyLayout(context);
  window.SasFabricTopologyUI(context);
  window.SasFabricTopology = {
    NOT_COLLECTED,
    BAY_DETAIL_LIMIT,
    buildTopologyModel,
    deviceRole,
    cableEndAccepts,
    tracePath: context.tracePath,
    searchTopology: context.searchTopology,
    renderTopology: context.renderTopology,
    pathTone: context.pathTone,
    chainStyle: context.chainStyle,
    searchHelp: context.searchHelp,
    linkDetail: context.linkDetail,
    cardFor: context.cardFor,
    detailFor: context.detailFor,
    boardLabel: context.boardLabel,
    composeInspector: context.composeInspector,
    render: context.render,
    // After the drawing is in place: the band's height is held first, so
    // the page is as long as it stays when mount puts the view and the page
    // back; then the band is followed.
    mount: (container, ...rest) => {
      context.holdBand(container?.querySelector?.("[data-topology-trace]"));
      context.mount(container, ...rest);
      context.watchBand(container);
    },
    release: context.release,
    view: context.view,
    drawnModel: context.drawnModel,
    inspectorDetail: context.inspectorDetail,
    attach: context.attach,
  };
})();
