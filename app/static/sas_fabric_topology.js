// Storage Fabric "Topology" mode (#937): a wiring diagram of the SAS fabric
// from the host down to the bays, drawn only from the /api/sas-fabric payload
// the page already has. Two parts:
//
//   - a pure layout model (buildTopologyModel, tracePath, searchTopology,
//     renderTopology): payload in, tiers, nodes, link routes, labels and an
//     SVG string out, no DOM. tests/js/fabric_topology_*.test.js load it in
//     Node the same way the other fabric tests load sas_fabric_view.js;
//   - a small DOM layer (render, mount, attach, release) that
//     sas_fabric_view.js calls to draw the mode and to trace a path on hover,
//     focus, click, search and Esc.
//
// Rules: one drawn line per payload link between drawn nodes; links into bays
// are drawn as one bundle per enclosure and bay group, labelled with the count
// of payload links it carries. No node, link, lane count, PHY number or rate is
// ever invented. A tier the payload does not carry says "not read" or "not
// collected", never an empty tier that looks healthy. Every input record is
// either drawn or named in a notice. A large bay group is collapsed into a
// summary block that expands; no bay is ever dropped. A disk whose physical
// location the producer does not know is never drawn as a bay.
(function () {
  "use strict";

  const NOT_COLLECTED = "HBA and expander hops are not collected on this platform.";
  const NOT_SHOWN = "Pools and vdevs are not drawn here; Impact Map and Disk Path show them. "
    + "Backplane zones, the CORE multipath groupings (shown instead as path marks on each bay) "
    + "and PHY error counters (Disk Path) are not drawn either.";
  const NOT_SHOWN_STORAGE = "Pools and vdevs are not drawn here; Impact Map and Disk Path show them. "
    + "Backplane zones are not drawn either.";
  const LOCATION_UNKNOWN = "physical location unavailable";
  // A bay group larger than this is drawn as a summary block until expanded.
  const BAY_DETAIL_LIMIT = 96;
  const CELLS_PER_ROW = 24;
  // A bay cell is a CELL_BOX square on a CELL pitch; its path marks are
  // MARK px wide on a MARK_PITCH.
  const CELL = 34;
  const CELL_BOX = 32;
  const MARK = 8;
  const MARK_PITCH = 9;
  const NODE_W = 184;
  const NODE_H = 62;
  // Text is sized at 8 px a character (12-13 px type), 16 px a line; a box
  // grows to 37 characters and then wraps rather than cutting a fact off.
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
  // An expandable bay group's control row: a 22 px control and its gap.
  const TOGGLE_H = 26;
  // The scroll box's border on each side (sas_fabric_topology.css).
  const SCROLL_BORDER = 1;
  // Gaps between rows: a 10 px track per link, then its label, 8 px clear of
  // the tracks above and of the box below.
  const TRACK = 10;
  const GAP_PAD = 10;
  const LABEL_GAP = 8;
  const LABEL_CLEAR = 8;
  const JUNCTION = 14;
  const NAME_LIMIT = 4;
  // Above this many lines (links that are not bay bundles) the layout skips
  // its polish steps; see layoutModel. Real systems stay far below it.
  const SIMPLIFY_ABOVE = 512;
  const LANE_LINKS = new Set(["controller-expander", "expander-expander"]);
  const HIDDEN_KINDS = new Set(["pool", "vdev", "backplane"]);
  const MEMBER_HEALTH = "member-health-not-transport";
  const CACHE_FRESH = new Set(["hit", "hit-after-wait", "miss", "forced-refresh"]);
  // Path states as the producers emit them: sas_fabric.py normalize_path_state
  // (active, passive, fail, missing, unknown), the Linux SES builder (mapped,
  // unmapped) and the platform builders (present, mixed, or a member health
  // word, marked by state_basis).
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

  // A table keyed by text from the payload answers for its own keys only, so
  // a word such as "__proto__" or "constructor" is just an unknown word.
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

  // The producer sends SAS levels and PHY counts as decimal strings
  // (sas_fabric_parsers.parse_mpr_expanders) and some counts as integers.
  // Only plain digits, bounded, are a reading; anything else stays unknown.
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

  // The fabric kinds the producers emit, with the platforms that emit each
  // (sas_fabric.py: _select_sas_fabric_builder_key and _storage_fabric_kind).
  // The real CORE builder names no kind and says platform "core"; the
  // synthetic demo names core_mpr. storage_generic is the builder's fallback
  // for a platform it does not name.
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

  // { kind, contract: "core" | "storage" | "unknown", notice }. Only an exact
  // supported kind on a platform that produces it is recognised; an unknown
  // or conflicting one makes no claim about what was or was not collected.
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

  // complete | partial | not_read, or unknown when the payload does not say.
  function readState(fabric, domain) {
    const counts = fabric?.raw?.controller_read?.counts;
    if (!isRecord(counts) || counts[domain] === undefined || counts[domain] === null) {
      return "unknown";
    }
    const value = counts[domain];
    return value === "complete" || value === "partial" ? value : "not_read";
  }

  // "1, 2, 3, 7" -> "1–3, 7". Only the numbers given; nothing is filled in.
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

  // ---- reading the payload ---------------------------------------------------

  // The fabric bay metric `present` is true, false or "unknown" (#1218); any
  // other value is unknown too. The model keeps its own tri-state.
  function bayOccupancy(metrics) {
    const value = isRecord(metrics) ? metrics.present : undefined;
    return value === true ? "yes" : value === false ? "no" : "unknown";
  }

  function locationKnown(...sources) {
    return !sources.some((item) => isRecord(item) && (item.physical_location_known === false || item.virtual_enclosure === true));
  }

  // Collections and members the view can read; what it cannot is named.
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

  // The inventory snapshot the page holds, when it belongs to the same system
  // and enclosure as the fabric payload: serials and disk identities by slot.
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

  // A disk's own identities: serials, SAS addresses and logical unit ids, as
  // an all-digit query matches them.
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

  // The bay number the producer labelled a bay with ("Bay 01" for slot 0
  // where the chassis counts from 1): plain ASCII digits only, never the
  // internal index. "" when no supported number can be read.
  function bayNumberOf(label, inventoryLabel) {
    const labelled = /^Bay\s+([0-9]+)$/.exec(String(label || ""));
    if (labelled) return labelled[1];
    const fallback = String(inventoryLabel ?? "");
    return /^[0-9]+$/.test(fallback) ? fallback : "";
  }

  // ---- tiers and nodes -------------------------------------------------------

  // mode: "core", "storage" or "unknown" (fabricContract).
  function tierForNode(item, mode) {
    const kind = String(item.kind || "");
    if (kind === "host") return "host";
    if (kind === "controller") return "controllers";
    if (kind === "expander") return mode === "storage" ? null : "expanders";
    if (kind === "path") return mode === "core" ? null : "devices";
    if (kind === "bay") return "bays";
    if (kind.endsWith("enclosure")) return "enclosures";
    return null;
  }

  // Kinds the view leaves out on purpose; anything else it cannot draw is named.
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
        const board = item.raw?.board;
        if (board) facts.push(String(board));
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
    } else if (item.kind === "host") {
      facts.push("host");
    } else if (item.kind === "path") {
      facts.push(pathCaption(item, kind));
    } else {
      facts.push(String(item.kind || "").replace(/-/g, " "));
    }
    return facts;
  }

  // ---- model -----------------------------------------------------------------

  function buildTopologyModel(fabric, options = {}) {
    const expanded = isRecord(options.expanded) ? options.expanded : {};
    const contract = fabricContract(fabric);
    const { kind } = contract;
    const mode = contract.contract;
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
        rawId: item.raw_id || "",
        controllerId: typeof item.controller_id === "string" ? item.controller_id : "",
        status: item.status || "",
        level: item.kind === "expander" ? expanderLevel(item) : null,
        facts: nodeFacts(item, mode, kind),
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
      if (bayIds.has(item.target) && source && (source.tier === "enclosures" || (!core && source.tier === "devices"))) {
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
        };
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
        entry.labelLines = [
          `x${lanes.count} · ${formatRate(lanes.rate)}`,
          `PHY ${phyRanges(lanes.sourcePhys) || "?"} ↔ ${phyRanges(lanes.targetPhys) || "?"}`,
        ];
      } else if (entry.lanesState === "not_reported") {
        entry.labelLines = ["lanes not reported"];
      }
      links.push(entry);
      linkById.set(entry.id, entry);
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
    for (const item of bayRows) {
      if (payloadNodeIds.get(item.id) !== item) continue;
      const found = traces.get(item.id);
      const trace = found?.kind === "bay" ? found : null;
      const metrics = isRecord(item.metrics) ? item.metrics : {};
      const traced = list(trace?.metrics?.path_states).filter(isRecord).map((state) => ({
        controller: String(state.controller_label || state.controller || ""),
        state: String(state.state ?? "unknown"),
        basis: String(state.state_basis ?? ""),
      }));
      const pathStates = traced.length ? traced : list(pathStatesByBay.get(item.id));
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
        pathStates,
        sources: [...(baySources.get(item.id) || [])].sort(naturalCompare),
        x: 0,
        y: 0,
        w: CELL_BOX,
      };
      bays.push(bay);
      const key = `${bay.sources.join("|")}#${located ? "located" : "logical"}`;
      if (!groupsByKey.has(key)) {
        const id = `group:${bay.sources.join("|") || "unlinked"}${located ? "" : ":logical"}`;
        groupsByKey.set(key, { id, enclosureIds: bay.sources, locationKnown: located, bays: [] });
      }
      groupsByKey.get(key).bays.push(bay);
    }
    const groups = [];
    for (const raw of groupsByKey.values()) {
      raw.bays.sort((left, right) => (left.slot ?? Infinity) - (right.slot ?? Infinity) || naturalCompare(left.label, right.label));
      const expandKey = `topology:${raw.id}`;
      const collapsed = raw.bays.length > BAY_DETAIL_LIMIT && !own(expanded, expandKey);
      // Path state counts per controller, keyed by payload text: Maps.
      const summary = { bays: raw.bays.length, withDisk: 0, empty: 0, unknown: 0, pathStates: new Map() };
      for (const bay of raw.bays) {
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
      const labelled = raw.bays.filter((bay) => !raw.locationKnown || !bay.number);
      const cellWidth = Math.max(CELL_BOX, ...labelled.map((bay) => bay.label.length * CHAR_W + 12));
      for (const bay of raw.bays) bay.w = cellWidth;
      const group = {
        id: raw.id,
        tier: "bays",
        enclosureIds: raw.enclosureIds,
        locationKnown: raw.locationKnown,
        bayIds: raw.bays.map((bay) => bay.bayId),
        bays: raw.bays,
        cells: collapsed ? [] : raw.bays,
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
      groups.push(group);
      for (const bay of raw.bays) groupByBay.set(bay.bayId, group);
    }
    const groupById = new Map(groups.map((group) => [group.id, group]));

    // One bundle line per (source, bay group), carrying the payload link ids.
    for (const group of groups) {
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
          tiers.push({
            id: `expanders:${level ?? "unknown"}`,
            label: level === null ? "Expanders, level not reported" : `Expanders, level ${level}`,
            state: expanderStatus.state,
            note: expanderStatus.state === "partial" ? expanderStatus.note : plural(members.length, "expander"),
            nodeIds: members.map((entry) => entry.id),
          });
          for (const entry of members) entry.tier = `expanders:${level ?? "unknown"}`;
        }
      }
      const enclosures = tierNodes("enclosures");
      const enclosureStatus = countTier(fabric, "enclosures", enclosures.length, { one: "enclosure", many: "enclosures", none: "no enclosures reported (read complete)" });
      tiers.push({ id: "enclosures", label: "Enclosures", ...enclosureStatus, nodeIds: enclosures.map((entry) => entry.id) });
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
        label: "Source",
        state: controllers.length ? "drawn" : "none",
        note: controllers.length ? plural(controllers.length, "source") : "no source reported",
        nodeIds: controllers.map((entry) => entry.id),
      });
      tiers.push({ id: "not-collected", label: "HBAs and expanders", state: "not_collected", note: NOT_COLLECTED, nodeIds: [] });
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
      tiers.push({
        id: "enclosures",
        label: "Enclosures",
        state: enclosures.length ? "drawn" : "none",
        note: enclosures.length ? plural(enclosures.length, "enclosure") : "no enclosure reported",
        nodeIds: enclosures.map((entry) => entry.id),
      });
    }
    const located = bays.filter((bay) => bay.locationKnown).length;
    const logical = bays.length - located;
    let bayNote = "no bays in this payload";
    if (located && logical) bayNote = `${plural(located, "bay")}; ${plural(logical, "disk")} without a physical location`;
    else if (logical) bayNote = `${plural(logical, "disk")}; ${LOCATION_UNKNOWN}`;
    else if (located) bayNote = plural(located, "bay");
    tiers.push({
      id: "bays",
      label: logical && !located ? "Disks" : logical ? "Bays and disks" : "Bays",
      state: bays.length ? "drawn" : "none",
      note: bayNote,
      nodeIds: groups.map((group) => group.id),
    });
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
      if (["expanders", "enclosures", "devices", "controllers"].includes(entry.tier.replace(/:.*$/, "")) && !list(incoming.get(entry.id)).length) {
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
      contract: mode,
      notices: [],
      counts: { bayNodes: bayRows.length, baysPlaced: groups.reduce((total, group) => total + group.bayIds.length, 0) },
      scope: scopeOf(fabric),
      width: 0,
      height: 0,
    };
    if (contract.notice) model.notices.push(contract.notice);
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
    layoutModel(model, options);
    model.box = { width: Number(options.width) || 0, height: Number(options.height) || 0 };
    return model;
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

  // ---- layout ----------------------------------------------------------------
  //
  // Rows are the tiers. Every row's order is chosen by barycentre sweeps and
  // the candidate with the fewest line crossings wins. The widest row is
  // spread across the available width; every other row sits under or over the
  // items it links to. Each link then gets its own track in the gap above its
  // child, so no two links share a track; a link that skips a row runs down a
  // free corridor between that row's boxes; and its label sits on its own
  // drop, clear of the boxes above and below.

  // Wrap text to lines of at most `max` characters at spaces; a single word
  // longer than that is shortened (its whole text stays in the item's name).
  // With `whole`, a long word is broken over lines instead of shortened.
  function wrap(text, max, whole = false) {
    return wrapWords(String(text ?? "").split(/\s+/).filter(Boolean), max, whole);
  }

  // The same, for words already split; a word may hold a space it keeps.
  function wrapWords(given, max, whole = false) {
    const words = given.flatMap((word) => (whole && word.length > max ? word.match(new RegExp(`.{1,${max}}`, "g")) : [word]));
    const lines = [];
    let line = "";
    for (const word of words) {
      const piece = word.length > max ? `${word.slice(0, max - 1)}…` : word;
      if (!line) line = piece;
      else if (line.length + 1 + piece.length <= max) line = `${line} ${piece}`;
      else {
        lines.push(line);
        line = piece;
      }
    }
    if (line) lines.push(line);
    return lines;
  }

  function sizeNode(entry) {
    const parts = [entry.label, ...entry.facts];
    if (entry.phyGroups.length) parts.push(`PHYs ${entry.phyGroups.map((group) => phyRanges(group.phys)).join(" | ")}`);
    if (entry.parentUnknown && entry.kind !== "host") parts.push("parent link not reported");
    const longest = Math.max(...parts.map((part) => String(part).length));
    const chars = Math.min(NODE_MAX_CHARS, Math.max(NODE_MIN_CHARS, longest));
    entry.titleLines = wrap(entry.label, chars, entry.wholeTitle).length || 1;
    entry.lines = parts.flatMap((part, index) => wrap(part, chars, index === 0 && entry.wholeTitle));
    entry.w = Math.max(entry.w, Math.max(...entry.lines.map((line) => line.length)) * TEXT_W + 20);
    entry.h = Math.max(NODE_H, 14 + entry.lines.length * LINE_H + 8);
  }

  // Two boxes in one tier never show the same title unless they are the same
  // item: first the whole name, then the HBA it hangs off, then its id.
  function distinctTitles(model) {
    const title = (entry) => entry.lines.slice(0, entry.titleLines).join(" ");
    const generated = new Set();
    for (let pass = 0; pass < 3; pass += 1) {
      const byTitle = new Map();
      for (const entry of model.nodes) {
        const key = `${entry.tier}\u0000${title(entry)}`;
        if (!byTitle.has(key)) byTitle.set(key, []);
        byTitle.get(key).push(entry);
      }
      const clashes = [...byTitle.values()].filter((group) => group.length > 1);
      if (!clashes.length) return;
      for (const entry of clashes.flat()) {
        const via = model.nodeById.get(entry.controllerId)?.label;
        if (pass === 0) entry.wholeTitle = true;
        else if (pass === 1 && via && via !== entry.label) entry.label = `${entry.label} via ${via}`;
        else if (pass === 2) entry.label = `${entry.label} (${entry.id})`;
        generated.add(entry);
        sizeNode(entry);
      }
    }
    // A generated title can equal another box's title. Titles the payload
    // gave are taken first, then every box in identifier order keeps its
    // title or gets the next free number, checked against every title in
    // its tier: the payload's order changes nothing, and a real " #2" stays.
    const key = (item) => `${item.tier}\u0000${title(item)}`;
    const byId = (left, right) => (left.id < right.id ? -1 : left.id > right.id ? 1 : 0);
    const order = [...model.nodes.filter((entry) => !generated.has(entry)).sort(byId), ...[...generated].sort(byId)];
    const taken = new Set();
    for (const entry of order) {
      if (taken.has(key(entry))) {
        const base = entry.label;
        entry.wholeTitle = true;
        for (let number = 2; taken.has(key(entry)); number += 1) {
          entry.label = `${base} #${number}`;
          sizeNode(entry);
        }
      }
      taken.add(key(entry));
    }
  }

  function groupText(group) {
    const lines = [{ text: groupHeading(group), title: true }];
    const summary = pathSummary(group.summary);
    if (group.collapsed) {
      lines.push({ text: `${group.summary.withDisk} with a disk · ${group.summary.empty} empty · ${group.summary.unknown} not read` });
      lines.push({ text: summary || "no path state in payload" });
    } else if (summary) {
      lines.push({ text: summary });
    }
    return lines;
  }

  function groupSize(group) {
    const texts = groupText(group);
    // An expandable group's control sits at its top left in both states,
    // its text below the control.
    group.textTop = group.expandable ? TOGGLE_H : 0;
    if (group.collapsed) {
      const width = Math.max(SUMMARY_W, ...texts.map((line) => line.text.length * TEXT_W + 20));
      group.lines = texts.flatMap((line) => wrap(line.text, Math.floor((width - 20) / TEXT_W)).map((text) => ({ ...line, text })));
      group.headH = 10 + group.textTop + group.lines.length * LINE_H;
      return [width, group.headH + 10];
    }
    const count = group.cells.length;
    const pitch = group.cellWidth + (CELL - CELL_BOX);
    const perRow = pitch === CELL ? CELLS_PER_ROW : Math.max(1, Math.floor((CELLS_PER_ROW * CELL) / pitch));
    const columns = Math.max(1, Math.min(perRow, count));
    const rows = Math.max(1, Math.ceil(count / perRow));
    group.perRow = perRow;
    group.pitch = pitch;
    // An unlinked or logical group is wide enough to say so in full.
    const minimum = Math.max(NODE_W, texts[0].text.length * TEXT_W + 20);
    const width = Math.max(minimum, columns * pitch + 16);
    group.lines = texts.flatMap((line) => wrap(line.text, Math.floor((width - 20) / TEXT_W)).map((text) => ({ ...line, text })));
    group.headH = 12 + group.textTop + group.lines.length * LINE_H;
    return [width, group.headH + rows * CELL + 10];
  }

  function centreOf(item) {
    return item.x + item.w / 2;
  }

  // Isotonic placement: keep the order, stay as close as possible to each
  // desired left edge, keep `gap` between neighbours and stay in [lo, hi].
  function placeRow(items, desired, gap, lo, hi) {
    const offsets = [];
    let offset = 0;
    for (const item of items) {
      offsets.push(offset);
      offset += item.w + gap;
    }
    const blocks = [];
    items.forEach((item, index) => {
      blocks.push({ sum: desired[index] - offsets[index], count: 1, start: index });
      while (blocks.length > 1) {
        const last = blocks[blocks.length - 1];
        const previous = blocks[blocks.length - 2];
        if (previous.sum / previous.count <= last.sum / last.count) break;
        previous.sum += last.sum;
        previous.count += last.count;
        blocks.pop();
      }
    });
    const lastIndex = items.length - 1;
    const upper = Math.max(lo, hi - offsets[lastIndex] - items[lastIndex].w);
    for (const [index, block] of blocks.entries()) {
      const end = index + 1 < blocks.length ? blocks[index + 1].start : items.length;
      const value = Math.min(upper, Math.max(lo, block.sum / block.count));
      for (let position = block.start; position < end; position += 1) items[position].x = value + offsets[position];
    }
  }

  function linkWidth(link) {
    return link.labelLines.length ? Math.max(...link.labelLines.map((line) => line.length)) * TEXT_W + 14 : JUNCTION;
  }

  function linkHeight(link) {
    return link.labelLines.length ? link.labelLines.length * 15 + 5 : JUNCTION;
  }

  function properCross(p, q, r, s) {
    const d = (q[0] - p[0]) * (s[1] - r[1]) - (q[1] - p[1]) * (s[0] - r[0]);
    if (Math.abs(d) < 1e-9) return false;
    const t = ((r[0] - p[0]) * (s[1] - r[1]) - (r[1] - p[1]) * (s[0] - r[0])) / d;
    const u = ((r[0] - p[0]) * (q[1] - p[1]) - (r[1] - p[1]) * (q[0] - p[0])) / d;
    return t > 0.01 && t < 0.99 && u > 0.01 && u < 0.99;
  }

  // Work counters: every loop of the layout adds what it did, so a test can
  // bound the cost by counting, not timing.
  function tick(work, key, count = 1) {
    work[key] = (work[key] || 0) + count;
    work.total += count;
  }

  function lowerBound(values, value, key = (item) => item) {
    let lo = 0;
    let hi = values.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (key(values[mid]) < value) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  // The drawn segments of the links, split into horizontal, vertical and
  // other pieces, each with its order in the drawing.
  function segmentsOf(links) {
    const horizontals = [];
    const verticals = [];
    const others = [];
    let order = 0;
    for (const link of links) {
      const points = link.points || [];
      for (let index = 0; index + 1 < points.length; index += 1) {
        const [a, b] = [points[index], points[index + 1]];
        const segment = { link, a, b, order: order += 1 };
        if (a[1] === b[1] && a[0] !== b[0]) horizontals.push(segment);
        else if (a[0] === b[0] && a[1] !== b[1]) verticals.push(segment);
        else if (a[0] !== b[0] || a[1] !== b[1]) others.push(segment);
      }
    }
    return { horizontals, verticals, others };
  }

  // Every horizontal and vertical piece of different links whose closed
  // extents meet, found by one sweep down the drawing (the work is the
  // pieces, their sorting and the pairs found, not every pair of pieces).
  function meetingPairs(segments, work) {
    const events = [];
    for (const segment of segments.verticals) {
      const [top, bottom] = [Math.min(segment.a[1], segment.b[1]), Math.max(segment.a[1], segment.b[1])];
      events.push({ y: top, kind: 0, segment }, { y: bottom, kind: 2, segment });
    }
    for (const segment of segments.horizontals) events.push({ y: segment.a[1], kind: 1, segment });
    events.sort((left, right) => left.y - right.y || left.kind - right.kind);
    tick(work, "crossingWork", events.length * Math.max(1, Math.ceil(Math.log2(events.length + 1))));
    const active = [];
    const pairs = [];
    const xOf = (segment) => segment.a[0];
    for (const event of events) {
      const { segment } = event;
      if (event.kind === 0) {
        active.splice(lowerBound(active, xOf(segment), xOf), 0, segment);
      } else if (event.kind === 2) {
        for (let index = lowerBound(active, xOf(segment), xOf); index < active.length; index += 1) {
          if (active[index] === segment) {
            active.splice(index, 1);
            break;
          }
        }
      } else {
        const [left, right] = [Math.min(segment.a[0], segment.b[0]), Math.max(segment.a[0], segment.b[0])];
        for (let index = lowerBound(active, left, xOf); index < active.length && xOf(active[index]) <= right; index += 1) {
          tick(work, "crossingWork");
          if (active[index].link !== segment.link) pairs.push([segment, active[index]]);
        }
      }
    }
    return pairs;
  }

  // Proper crossings between the drawn segments of different links.
  function countCrossings(links, work) {
    const segments = segmentsOf(links);
    let count = 0;
    for (const [horizontal, vertical] of meetingPairs(segments, work)) {
      const [first, second] = horizontal.order < vertical.order ? [horizontal, vertical] : [vertical, horizontal];
      if (properCross(first.a, first.b, second.a, second.b)) count += 1;
    }
    // A piece that is neither horizontal nor vertical (routes never make
    // one) is checked against every other piece.
    const all = [...segments.horizontals, ...segments.verticals, ...segments.others].sort((left, right) => left.order - right.order);
    for (const other of segments.others) {
      tick(work, "crossingWork", all.length);
      for (const segment of all) {
        if (segment.link === other.link || (segments.others.includes(segment) && segment.order <= other.order)) continue;
        const [first, second] = segment.order < other.order ? [segment, other] : [other, segment];
        if (properCross(first.a, first.b, second.a, second.b)) count += 1;
      }
    }
    return count;
  }

  // Where a vertical piece of one link crosses a horizontal piece of another:
  // the vertical continues, the horizontal is drawn with a small gap.
  function crossingMarks(links, work) {
    const marks = [];
    const pairs = meetingPairs(segmentsOf(links), work).sort(([h1, v1], [h2, v2]) => h1.order - h2.order || v1.order - v2.order);
    for (const [{ link: under, a, b }, { link: over, a: c, b: d }] of pairs) {
      const [left, right] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])];
      const [top, bottom] = [Math.min(c[1], d[1]), Math.max(c[1], d[1])];
      if (c[0] > left + 1 && c[0] < right - 1 && a[1] > top + 1 && a[1] < bottom - 1) marks.push({ x: c[0], y: a[1], over: over.id, under: under.id });
    }
    return marks;
  }

  // Row orders to try: the starting order and every barycentre sweep after it.
  function orderCandidates(rows, links, rowIndex, work, sweeps = 8) {
    const parents = new Map();
    const children = new Map();
    for (const link of links) {
      if (!parents.has(link.target)) parents.set(link.target, []);
      if (!children.has(link.source)) children.set(link.source, []);
      parents.get(link.target).push(link.source);
      children.get(link.source).push(link.target);
    }
    let order = rows.map((row) => row.items.slice());
    const snapshot = () => order.map((items) => items.slice());
    const candidates = [snapshot()];
    tick(work, "orderWork", links.length);
    const position = () => {
      const result = new Map();
      for (const items of order) items.forEach((item, index) => result.set(item.id, (index + 0.5) / items.length));
      tick(work, "orderWork", result.size);
      return result;
    };
    for (let iteration = 0; iteration < sweeps; iteration += 1) {
      const down = iteration % 2 === 0;
      const sequence = down ? order.map((_, index) => index).slice(1) : order.map((_, index) => index).reverse().slice(1);
      for (const r of sequence) {
        const at = position();
        const neighbours = down ? parents : children;
        const key = new Map(order[r].map((item) => {
          const ids = list(neighbours.get(item.id)).filter((id) => (down ? rowIndex.get(id) < r : rowIndex.get(id) > r));
          tick(work, "orderWork", 1 + list(neighbours.get(item.id)).length);
          return [item.id, ids.length ? ids.reduce((sum, id) => sum + at.get(id), 0) / ids.length : at.get(item.id)];
        }));
        tick(work, "orderWork", order[r].length * Math.max(1, Math.ceil(Math.log2(order[r].length + 1))));
        order[r] = order[r].slice().sort((left, right) => key.get(left.id) - key.get(right.id));
      }
      candidates.push(snapshot());
    }
    return candidates;
  }

  // Horizontal placement for one order. The widest row is spread across the
  // width; the rows under it are placed under their parents; then, from the
  // bottom row up, every parent is centred over the span of all its direct
  // children (in any row below), as near that span's midpoint as the order
  // and the gaps between boxes allow. Where the drawing's edges keep a parent
  // outside its children's span, that pass runs again without the edges and
  // the drawing grows (it scrolls) instead.
  function placeRows(rows, links, rowIndex, available, work) {
    const parents = new Map();
    const children = new Map();
    for (const link of links) {
      if (rowIndex.get(link.target) <= rowIndex.get(link.source)) continue;
      if (!parents.has(link.target)) parents.set(link.target, []);
      if (!children.has(link.source)) children.set(link.source, []);
      parents.get(link.target).push(link.source);
      children.get(link.source).push(link.target);
    }
    const byId = new Map(rows.flatMap((row) => row.items.map((item) => [item.id, item])));
    const rowWidth = (row) => row.items.reduce((sum, item) => sum + item.w, 0) + Math.max(0, row.items.length - 1) * H_GAP;
    const content = Math.max(NODE_W, ...rows.map(rowWidth), ...rows.map((row) => row.bandWidth || 0));
    const width = Math.max(available, LEFT + content + MARGIN);
    const lo = LEFT;
    const hi = width - MARGIN;
    let anchor = 0;
    rows.forEach((row, index) => {
      if (rowWidth(row) >= rowWidth(rows[anchor])) anchor = index;
    });
    const spread = (row) => {
      const extra = Math.max(0, hi - lo - rowWidth(row));
      const step = row.items.length ? extra / row.items.length : 0;
      let x = lo + step / 2;
      return row.items.map((item) => {
        const left = x;
        x += item.w + H_GAP + step;
        return left;
      });
    };
    for (const row of rows) spread(row).forEach((x, index) => { row.items[index].x = x; });
    const place = (r, wanted, bounds = [lo, hi]) => {
      const row = rows[r];
      if (!row.items.length) return;
      tick(work, "placeWork", row.items.length);
      placeRow(row.items, row.items.map((item) => wanted(item) ?? item.x), H_GAP, bounds[0], bounds[1]);
    };
    for (let r = anchor + 1; r < rows.length; r += 1) {
      place(r, (item) => {
        const ids = list(parents.get(item.id)).filter((id) => byId.has(id));
        return ids.length ? ids.reduce((sum, id) => sum + centreOf(byId.get(id)), 0) / ids.length - item.w / 2 : null;
      });
    }
    const overChildren = (item) => {
      const ids = list(children.get(item.id)).filter((id) => byId.has(id));
      if (!ids.length) return null;
      const left = Math.min(...ids.map((id) => byId.get(id).x));
      const right = Math.max(...ids.map((id) => byId.get(id).x + byId.get(id).w));
      return (left + right) / 2 - item.w / 2;
    };
    for (let r = rows.length - 2; r >= 0; r -= 1) place(r, overChildren);
    tick(work, "placeWork", links.length);
    if (!spanViolations(links, byId, rowIndex)) return width;
    tick(work, "placeWork", links.length);
    for (let r = rows.length - 2; r >= 0; r -= 1) place(r, overChildren, [-Infinity, Infinity]);
    const items = [...byId.values()];
    const shift = Math.max(0, lo - Math.min(...items.map((item) => item.x)));
    for (const item of items) item.x += shift;
    return Math.max(width, Math.max(...items.map((item) => item.x + item.w)) + MARGIN);
  }

  // Parents whose centre is outside the span of their direct children.
  function spanViolations(links, byId, rowIndex) {
    const spans = new Map();
    for (const link of links) {
      if (rowIndex.get(link.target) <= rowIndex.get(link.source)) continue;
      const child = byId.get(link.target);
      const span = spans.get(link.source) || [Infinity, -Infinity];
      spans.set(link.source, [Math.min(span[0], child.x), Math.max(span[1], child.x + child.w)]);
    }
    let count = 0;
    for (const [id, [left, right]] of spans) {
      const centre = centreOf(byId.get(id));
      if (centre < left - 0.5 || centre > right + 0.5) count += 1;
    }
    return count;
  }

  // Routes, tracks and labels for one placement. Returns the drawing height.
  function routeLinks(model, rows, links, rowIndex, width) {
    const { work } = model;
    tick(work, "routePasses");
    const byId = new Map(rows.flatMap((row) => row.items.map((item) => [item.id, item])));
    const gaps = rows.map(() => ({ horizontals: [], labels: [], verticals: [] }));
    // Each item's links out and in, collected once.
    const outOf = new Map();
    const inTo = new Map();
    for (const link of links) {
      if (!outOf.has(link.source)) outOf.set(link.source, []);
      if (!inTo.has(link.target)) inTo.set(link.target, []);
      outOf.get(link.source).push(link);
      inTo.get(link.target).push(link);
    }
    tick(work, "routeWork", links.length + byId.size);
    // Exits: spread on the parent's bottom edge in the order of their targets.
    const exits = new Map();
    const arrivals = new Map();
    for (const item of byId.values()) {
      const out = list(outOf.get(item.id)).sort((a, b) => centreOf(byId.get(a.target)) - centreOf(byId.get(b.target)));
      out.forEach((link, index) => exits.set(link, item.x + item.w * (index + 1) / (out.length + 1)));
      const into = list(inTo.get(item.id)).sort((a, b) => centreOf(byId.get(a.source)) - centreOf(byId.get(b.source)));
      arrivals.set(item.id, into);
      tick(work, "routeWork", (out.length + into.length) * Math.max(1, Math.ceil(Math.log2(out.length + into.length + 1))));
    }
    // The exits that leave into each gap, sorted, for the drop and corridor searches.
    const exitsInto = rows.map(() => []);
    for (const link of links) {
      const below = rowIndex.get(link.source) + 1;
      if (below < rows.length) exitsInto[below].push(exits.get(link));
    }
    for (const xs of exitsInto) xs.sort((a, b) => a - b);
    // Drops: spread on the child's top edge, kept 10 px from every exit in the
    // same gap and from the labels of other drops.
    const drops = new Map();
    rows.forEach((row, r) => {
      const exitXs = exitsInto[r];
      // The drops placed so far in this gap, sorted by x, and the widest half label.
      const taken = [];
      let widest = 0;
      const clear = (x, half) => {
        const at = lowerBound(exitXs, x);
        tick(work, "dropWork", 1 + Math.ceil(Math.log2(exitXs.length + taken.length + 2)));
        if ((at < exitXs.length && exitXs[at] - x < TRACK) || (at > 0 && x - exitXs[at - 1] < TRACK)) return false;
        const reach = Math.max(TRACK, widest + 4, half + 4);
        for (let index = lowerBound(taken, x - reach, (other) => other.x); index < taken.length && taken[index].x <= x + reach; index += 1) {
          tick(work, "dropWork");
          const other = taken[index];
          if (Math.abs(other.x - x) < Math.max(TRACK, other.half + 4, half + 4)) return false;
        }
        return true;
      };
      for (const item of row.items) {
        const into = arrivals.get(item.id) || [];
        into.forEach((link, index) => {
          const ideal = item.x + item.w * (index + 1) / (into.length + 1);
          const half = link.labelLines.length ? linkWidth(link) / 2 : JUNCTION / 2;
          let best = ideal;
          for (let step = 0; step <= item.w; step += 2) {
            const options = [ideal - step, ideal + step].filter((x) => x >= item.x + 10 && x <= item.x + item.w - 10);
            const found = options.find((x) => clear(x, half));
            if (found !== undefined) {
              best = found;
              break;
            }
          }
          drops.set(link, best);
          taken.splice(lowerBound(taken, best, (other) => other.x), 0, { x: best, half });
          widest = Math.max(widest, half);
        });
      }
    });
    // Vertical sizes: tracks per gap, label band, row heights.
    const spans = links.map((link) => {
      const from = rowIndex.get(link.source);
      const to = rowIndex.get(link.target);
      return { link, from, to, skip: to > from + 1, up: to <= from };
    });
    for (const span of spans) {
      const first = Math.min(span.from + 1, rows.length - 1);
      gaps[first].horizontals.push({ span, part: "first" });
      if (span.skip || span.up) gaps[span.to].horizontals.push({ span, part: "last" });
      gaps[span.to].labels.push(span.link);
    }
    let y = MARGIN + 8;
    rows.forEach((row, r) => {
      if (r > 0) {
        const gap = gaps[r];
        gap.top = y;
        gap.trackTop = y + GAP_PAD;
        const labelH = Math.max(0, ...gap.labels.map(linkHeight));
        y += GAP_PAD + gap.horizontals.length * TRACK + LABEL_GAP + labelH + LABEL_CLEAR;
        y = Math.max(y, gap.top + 32);
      }
      row.y = y;
      const height = Math.max(row.items.length ? Math.max(...row.items.map((item) => item.h)) : 36, row.labelH || 0, row.bandH || 0);
      row.height = height;
      for (const item of row.items) item.y = y;
      row.tier.y = y;
      row.tier.h = height;
      y += height;
    });
    const height = y + MARGIN + (spans.some((span) => span.up) ? 40 : 0);
    // A "not read" or "not collected" band and its note: placed where no
    // straight drop has to pass, and an obstacle for every corridor.
    rows.forEach((row, r) => {
      row.band = null;
      if (!row.bandLines) return;
      const avoid = spans
        .filter((span) => (span.skip && span.from < r && span.to > r) || (span.up && span.from < r))
        .flatMap((span) => (span.up ? [width - MARGIN - 4] : [exits.get(span.link), drops.get(span.link)]));
      const fits = (x) => x + row.bandW <= width - MARGIN && avoid.every((v) => v < x - 16 || v > x + row.bandW + 16);
      const x = [LEFT, ...avoid.map((v) => v + 17).sort((a, b) => a - b)].find(fits) ?? LEFT;
      row.band = { id: `band:${row.tier.id}`, x, y: row.y, w: row.bandW, h: row.height };
    });
    // Corridors for links that skip rows: a free x through every row and label
    // band in between, 10 px from every other vertical there.
    const corridors = [];
    const dropsInto = rows.map(() => []);
    for (const link of links) dropsInto[rowIndex.get(link.target)]?.push(drops.get(link));
    const verticalsIn = (r) => [...(exitsInto[r] || []), ...(dropsInto[r] || [])];
    for (const span of spans) {
      if (!span.skip && !span.up) continue;
      const exit = exits.get(span.link);
      const drop = drops.get(span.link);
      const top = span.up ? span.from + 1 : span.from + 1;
      const bottom = span.up ? rows.length : span.to;
      const blocked = [];
      for (let r = top; r < bottom; r += 1) {
        for (const item of rows[r]?.items || []) blocked.push([item.x - 12, item.x + item.w + 12]);
        if (rows[r]?.band) blocked.push([rows[r].band.x - 12, rows[r].band.x + rows[r].band.w + 12]);
        for (const link of gaps[r]?.labels || []) {
          const x = drops.get(link);
          const half = linkWidth(link) / 2;
          blocked.push([x - half - 8, x + half + 8]);
        }
      }
      for (let r = top; r <= Math.min(bottom, rows.length - 1); r += 1) {
        for (const x of verticalsIn(r)) if (x !== exit && x !== drop) blocked.push([x - TRACK, x + TRACK]);
      }
      for (const other of corridors) if (other.top <= bottom && other.bottom >= top) blocked.push([other.x - TRACK, other.x + TRACK]);
      const target = span.up ? width - MARGIN - 4 : (exit + drop) / 2;
      // Never through the tier label column on the left.
      const free = (candidate) => {
        tick(work, "corridorWork", 1 + blocked.length);
        return candidate >= LEFT + 4 && candidate <= width - 4 && blocked.every(([left, right]) => candidate <= left || candidate >= right);
      };
      // Straight down under the drop or the exit when that is free: one bend fewer.
      let x = span.up ? null : [drop, exit].find(free) ?? null;
      for (let step = 0; step <= width && x === null; step += 2) {
        for (const candidate of [target - step, target + step]) {
          if (free(candidate)) {
            x = candidate;
            break;
          }
        }
      }
      span.corridor = x === null ? width - 4 : x;
      corridors.push({ x: span.corridor, top, bottom });
    }
    // Tracks: one per horizontal, ordered in each gap to cross as little as possible.
    rows.forEach((row, r) => {
      const gap = gaps[r];
      if (!gap.horizontals.length) return;
      const ends = gap.horizontals.map((piece) => {
        const { span, part } = piece;
        const into = part === "first" ? exits.get(span.link) : span.corridor;
        const out = part === "first" && !span.skip && !span.up ? drops.get(span.link) : part === "first" ? span.corridor : drops.get(span.link);
        return { piece, into, out, lo: Math.min(into, out), hi: Math.max(into, out) };
      });
      // cost(a, b): crossings when track a lies above track b. Inserting an
      // end at each position in turn changes the total by one pair, so each
      // insertion is linear and a gap is quadratic in its lines.
      const cost = (a, b) => {
        tick(model.work, "trackCosts");
        return (b.into > a.lo && b.into < a.hi ? 1 : 0) + (a.out > b.lo && a.out < b.hi ? 1 : 0);
      };
      const ordered = [];
      for (const end of ends.sort((a, b) => a.lo - b.lo)) {
        let total = 0;
        for (const other of ordered) total += cost(end, other);
        let bestIndex = 0;
        let bestCost = total;
        for (let index = 0; index < ordered.length; index += 1) {
          total += cost(ordered[index], end) - cost(end, ordered[index]);
          if (total < bestCost) {
            bestCost = total;
            bestIndex = index + 1;
          }
        }
        ordered.splice(bestIndex, 0, end);
      }
      ordered.forEach((end, index) => {
        end.piece.y = gap.trackTop + index * TRACK;
        end.piece.span[end.piece.part] = end.piece.y;
      });
    });
    // Points and labels.
    for (const span of spans) {
      const { link } = span;
      const parent = byId.get(link.source);
      const child = byId.get(link.target);
      const exit = exits.get(link);
      const drop = drops.get(link);
      const start = parent.y + parent.h;
      if (span.skip || span.up) {
        link.points = [[exit, start], [exit, span.first], [span.corridor, span.first], [span.corridor, span.last], [drop, span.last], [drop, child.y]];
      } else {
        link.points = [[exit, start], [exit, span.first], [drop, span.first], [drop, child.y]];
      }
      // Drop repeated points and the middle of three in a straight line.
      const points = link.points.filter((point, index, all) => index === 0 || point[0] !== all[index - 1][0] || point[1] !== all[index - 1][1]);
      link.points = points.filter((point, index, all) => {
        if (index === 0 || index === all.length - 1) return true;
        const [a, b] = [all[index - 1], all[index + 1]];
        return !((a[0] === point[0] && point[0] === b[0]) || (a[1] === point[1] && point[1] === b[1]));
      });
      const w = linkWidth(link);
      const h = linkHeight(link);
      link.labelBox = { x: drop - w / 2, y: child.y - LABEL_CLEAR - h, w, h };
    }
    return height;
  }

  function tierLines(tier) {
    // "level 1" stays on one line: a number never sits alone on a line.
    const words = String(tier.label ?? "").split(/\s+/).filter(Boolean);
    const joined = words.reduce((all, word) => {
      if (/^[0-9]+$/.test(word) && all.length) all[all.length - 1] = `${all[all.length - 1]} ${word}`;
      else all.push(word);
      return all;
    }, []);
    tier.labelLines = wrapWords(joined, TIER_LABEL_CHARS);
    tier.noteLines = wrap(tier.note, TIER_NOTE_CHARS);
    return 10 + (tier.labelLines.length + tier.noteLines.length) * 17;
  }

  // Each link's HBA: the controller it hangs off, for its line colour.
  function assignChains(model) {
    const chainOf = new Map();
    const controllers = model.nodes.filter((entry) => entry.kind === "controller");
    controllers.forEach((entry) => chainOf.set(entry.id, entry.id));
    const pending = controllers.map((entry) => entry.id);
    while (pending.length) {
      const id = pending.shift();
      for (const link of list(model.outgoing.get(id))) {
        if (!chainOf.has(link.target)) {
          chainOf.set(link.target, chainOf.get(id));
          pending.push(link.target);
        }
      }
    }
    for (const link of model.links) {
      link.chain = model.nodeById.get(link.target)?.kind === "controller" ? link.target : chainOf.get(link.source) || null;
    }
    const drawn = new Set(model.links.map((link) => link.chain));
    model.chains = controllers.map((entry) => entry.id).filter((id) => drawn.has(id));
    // More chains than looks: every chain is still drawn and in the legend,
    // and the page names the chains that share a colour and line pattern.
    if (model.chains.length > CHAIN_LOOKS) {
      const byLook = new Map();
      model.chains.forEach((id, index) => {
        const look = index % CHAIN_LOOKS;
        if (!byLook.has(look)) byLook.set(look, []);
        byLook.get(look).push(model.nodeById.get(id)?.label || id);
      });
      const shared = [...byLook.values()].filter((names) => names.length > 1)
        .map((names) => (names.length > 2 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names.join(" and ")));
      // Every group is named: at most one per look, so the list stays short.
      model.notices.push(`${model.chains.length} chains are drawn and there are ${CHAIN_LOOKS} line looks, so these chains share a colour and line pattern: `
        + `${shared.join("; ")}. Every chain is drawn and in the legend; hover or focus a line to trace its chain.`);
    }
  }

  function layoutModel(model, options = {}) {
    const available = Math.max(0, Number(options.width) || 0);
    // Work counters, so a test can bound the cost by counting, not timing.
    model.work = { total: 0, trackCosts: 0, routePasses: 0, candidates: 0 };
    for (const entry of model.nodes) sizeNode(entry);
    distinctTitles(model);
    const rows = model.tiers.map((tier) => ({
      tier,
      items: tier.id === "bays" ? model.groups : tier.nodeIds.map((id) => model.nodeById.get(id)).filter(Boolean),
    }));
    for (const group of model.groups) [group.w, group.h] = groupSize(group);
    for (const row of rows) {
      row.labelH = tierLines(row.tier);
      const missing = !row.items.length && (row.tier.state === "not_read" || row.tier.state === "not_collected" || row.tier.state === "none");
      row.bandLines = missing ? wrap(row.tier.note, BAND_CHARS) : null;
      row.bandW = missing ? Math.max(NODE_W, ...row.bandLines.map((line) => line.length * TEXT_W + 24)) : 0;
      row.bandH = missing ? 14 + row.bandLines.length * LINE_H : 0;
      row.bandWidth = row.bandW;
    }
    // A child with several labelled arrivals is wide enough for them side by
    // side, and a child with several parents wide enough that every parent's
    // centre can sit over it (a bay group: as wide as its enclosures).
    for (const item of [...model.nodes, ...model.groups]) {
      const into = list(model.incoming.get(item.id));
      if (into.length > 1) item.w = Math.max(item.w, into.reduce((sum, link) => sum + linkWidth(link) + 12, 12));
      const parents = into.map((link) => model.nodeById.get(link.source)).filter(Boolean);
      if (parents.length > 1) {
        const row = parents.reduce((sum, parent) => sum + parent.w, 0) + (parents.length - 1) * H_GAP;
        const needed = item.tier === "bays" ? row : row - (parents[0].w + parents[parents.length - 1].w) / 2 + 40;
        item.w = Math.max(item.w, needed);
      }
    }
    const rowIndex = new Map();
    rows.forEach((row, index) => row.items.forEach((item) => rowIndex.set(item.id, index)));
    const links = model.links.filter((link) => rowIndex.has(link.source) && rowIndex.has(link.target) && link.source !== link.target);
    // The order that keeps every parent over its children wins; then the one
    // with the fewest crossings; then the shortest sideways runs.
    const byId = new Map(rows.flatMap((row) => row.items.map((item) => [item.id, item])));
    // A drawing with more lines than SIMPLIFY_ABOVE skips the polish: one
    // ordering pass instead of the candidate-order search, and no crossing
    // gaps. Every device and every line is still drawn, and the page says so.
    const lines = links.filter((link) => !link.bundle).length;
    model.simplified = lines > SIMPLIFY_ABOVE;
    // The order drawn last time for this scope is tried first and kept on a
    // tie, so expanding a shelf does not reorder the drawing under the operator.
    // Its ranks are read once per tier.
    const previous = isRecord(options.previousOrder) ? options.previousOrder : null;
    const previousCandidate = () => rows.map((row) => {
      const known = list(own(previous, row.tier.id));
      const ranks = new Map(known.map((id, index) => [id, index]));
      tick(model.work, "orderWork", known.length + row.items.length * Math.max(1, Math.ceil(Math.log2(row.items.length + 1))));
      const rank = (item) => ranks.get(item.id) ?? known.length;
      return row.items.slice().sort((left, right) => rank(left) - rank(right));
    });
    let candidates;
    if (model.simplified) {
      candidates = [previous ? previousCandidate() : orderCandidates(rows, links, rowIndex, model.work, 1).pop()];
    } else {
      candidates = orderCandidates(rows, links, rowIndex, model.work);
      if (previous) candidates.unshift(previousCandidate());
    }
    model.work.candidates = candidates.length;
    let best = null;
    for (const [number, candidate] of candidates.entries()) {
      if (candidates.length === 1) {
        best = { candidate };
        break;
      }
      rows.forEach((row, index) => {
        row.items = candidate[index];
      });
      const width = placeRows(rows, links, rowIndex, available, model.work);
      routeLinks(model, rows, links, rowIndex, width);
      const violations = spanViolations(links, byId, rowIndex);
      const score = countCrossings(links, model.work);
      const length = links.reduce((sum, link) => sum + link.points.reduce((total, point, index, all) => total + (index ? Math.abs(point[0] - all[index - 1][0]) : 0), 0), 0);
      tick(model.work, "scoreWork", links.length);
      const kept = previous && number === 0 ? 0 : 1;
      const better = !best || violations < best.violations
        || (violations === best.violations && (score < best.score
          || (score === best.score && (kept < best.kept || (kept === best.kept && length < best.length - 0.5)))));
      if (better) best = { violations, score, kept, length, candidate };
    }
    rows.forEach((row, index) => {
      row.items = best ? best.candidate[index] : row.items;
    });
    model.width = placeRows(rows, links, rowIndex, available, model.work);
    model.height = routeLinks(model, rows, links, rowIndex, model.width);
    // No coordinate is ever NaN: a link that could not be routed is named.
    for (const link of links) {
      if (link.points && !link.points.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y))) {
        link.points = null;
        link.labelBox = null;
        model.notices.push(`Not drawn: ${linkName(model, link)} could not be routed.`);
      }
    }
    if (model.simplified) {
      model.crossings = null;
      model.crossingMarks = [];
      model.notices.push(`The drawing was simplified because of its size (${lines} lines, more than ${SIMPLIFY_ABOVE}): every device and every line is drawn, `
        + "but two steps were skipped: the search for the row order with the fewest crossings (the rows keep one ordering pass) and the crossing gaps.");
    } else {
      model.crossings = countCrossings(links, model.work);
      model.crossingMarks = crossingMarks(links, model.work);
    }
    model.bands = [];
    model.tiers.forEach((tier, index) => {
      tier.order = rows[index].items.map((item) => item.id);
      tier.band = rows[index].band ? { ...rows[index].band, lines: rows[index].bandLines } : null;
      if (tier.band) model.bands.push(tier.band);
    });
    for (const group of model.groups) {
      group.cells.forEach((bay, index) => {
        bay.x = group.x + 8 + (index % group.perRow) * group.pitch;
        bay.y = group.y + group.headH + Math.floor(index / group.perRow) * CELL;
      });
    }
    assignChains(model);
  }

  // ---- path trace and search -------------------------------------------------

  function emptyPath() {
    return { nodes: new Set(), links: new Set(), groups: new Set(), bays: new Set() };
  }

  function walkUp(model, id, path) {
    const pending = [id];
    while (pending.length) {
      const current = pending.pop();
      for (const link of list(model.incoming.get(current))) {
        if (path.links.has(link.id)) continue;
        path.links.add(link.id);
        if (!path.nodes.has(link.source)) {
          path.nodes.add(link.source);
          pending.push(link.source);
        }
      }
    }
  }

  function walkDown(model, id, path) {
    const pending = [id];
    while (pending.length) {
      const current = pending.pop();
      for (const link of list(model.outgoing.get(current))) {
        if (path.links.has(link.id)) continue;
        path.links.add(link.id);
        if (link.bundle) {
          const group = model.groupById.get(link.target);
          path.groups.add(link.target);
          for (const bay of list(group?.bays)) {
            if (bay.sources.includes(link.source)) path.bays.add(bay.bayId);
          }
          continue;
        }
        if (!path.nodes.has(link.target)) {
          path.nodes.add(link.target);
          pending.push(link.target);
        }
      }
    }
  }

  function bayPath(model, bayId, path) {
    const bay = model.bayById.get(bayId);
    const group = model.groupByBay.get(bayId);
    if (!bay || !group) return path;
    path.bays.add(bayId);
    path.groups.add(group.id);
    for (const sourceId of bay.sources) {
      path.links.add(`bundle:${sourceId}->${group.id}`);
      path.nodes.add(sourceId);
      walkUp(model, sourceId, path);
    }
    return path;
  }

  // ref: { kind: "node" | "bay" | "link" | "group", id }. The whole path the
  // item lies on: up to the host and down to the bays.
  function tracePath(model, ref, path = emptyPath()) {
    if (!model || !ref) return path;
    if (ref.kind === "bay") return bayPath(model, ref.id, path);
    if (ref.kind === "group") {
      const group = model.groupById.get(ref.id);
      for (const bayId of list(group?.bayIds)) bayPath(model, bayId, path);
      return path;
    }
    if (ref.kind === "link") {
      const link = model.linkById.get(ref.id);
      if (!link) return path;
      path.links.add(link.id);
      path.nodes.add(link.source);
      walkUp(model, link.source, path);
      if (link.bundle) {
        const group = model.groupById.get(link.target);
        path.groups.add(link.target);
        for (const bay of list(group?.bays)) if (bay.sources.includes(link.source)) path.bays.add(bay.bayId);
      } else {
        path.nodes.add(link.target);
        walkDown(model, link.target, path);
      }
      return path;
    }
    if (!model.nodeById.has(ref.id)) {
      return model.bayById.has(ref.id) ? bayPath(model, ref.id, path) : path;
    }
    path.nodes.add(ref.id);
    walkUp(model, ref.id, path);
    walkDown(model, ref.id, path);
    return path;
  }

  // "bay N" is a bay number and matches only bays with a physical location.
  // A query of digits alone matches bay numbers first, then the serials and
  // SAS addresses of disks and the identities of nodes that contain it. Any
  // other text matches labels, disk names, serials, disk and expander SAS
  // addresses.
  function searchTopology(model, query) {
    const text = String(query ?? "").trim().toLowerCase();
    const result = { query: text, matches: [], path: emptyPath() };
    if (!model || !text) return result;
    const bayOnly = /^bay\s*([0-9]+)$/.exec(text);
    const digits = /^[0-9]+$/.test(text) ? text : "";
    const wanted = bayOnly ? bayOnly[1] : digits;
    const found = new Set();
    const addBay = (bay) => {
      if (found.has(bay.bayId)) return;
      found.add(bay.bayId);
      const group = model.groupByBay.get(bay.bayId);
      result.matches.push({ id: bay.bayId, kind: "bay", label: bay.label, groupId: group?.id || "", collapsed: Boolean(group?.collapsed) });
      bayPath(model, bay.bayId, result.path);
    };
    if (wanted) {
      for (const bay of model.bays) {
        if (bay.locationKnown && bay.number !== "" && Number(bay.number) === Number(wanted)) addBay(bay);
      }
    }
    if (bayOnly) return result;
    for (const bay of model.bays) {
      const hit = digits
        ? bay.identities.some((value) => value.includes(text))
        : bay.label.toLowerCase().includes(text) || bay.terms.some((value) => value.includes(text));
      if (hit) addBay(bay);
    }
    for (const entry of model.nodes) {
      if (entry.kind === "host") continue;
      // Digits match what the payload names a node by, not the view's own ids.
      const names = digits ? [entry.label, entry.rawId] : [entry.label, entry.rawId, entry.id];
      if (names.some((value) => value && String(value).toLowerCase().includes(text))) {
        result.matches.push({ id: entry.id, kind: "node", label: entry.label, groupId: "", collapsed: false });
        tracePath(model, { kind: "node", id: entry.id }, result.path);
      }
    }
    return result;
  }

  // ---- SVG rendering ---------------------------------------------------------

  // Path marks differ by shape as well as colour (at least 8 px, centred on
  // cx, cy); an open shape is a state with something missing or unknown.
  const MARK_SHAPES = {
    active: (cx, cy) => `<circle cx="${cx}" cy="${cy}" r="4"`,
    standby: (cx, cy) => `<circle cx="${cx}" cy="${cy}" r="4" fill="none"`,
    fault: (cx, cy) => `<path d="M${cx - 4} ${cy - 4}L${cx + 4} ${cy + 4}M${cx + 4} ${cy - 4}L${cx - 4} ${cy + 4}" fill="none"`,
    missing: (cx, cy) => `<rect x="${cx - 4}" y="${cy - 4}" width="8" height="8" fill="none"`,
    mapped: (cx, cy) => `<path d="M${cx} ${cy - 4.5}L${cx + 4.5} ${cy}L${cx} ${cy + 4.5}L${cx - 4.5} ${cy}Z"`,
    unmapped: (cx, cy) => `<path d="M${cx} ${cy - 4.5}L${cx + 4.5} ${cy}L${cx} ${cy + 4.5}L${cx - 4.5} ${cy}Z" fill="none"`,
    unknown: (cx, cy) => `<rect x="${cx - 4}" y="${cy - 4}" width="8" height="8" rx="2"`,
    member: (cx, cy) => `<path d="M${cx} ${cy - 4}L${cx + 4.5} ${cy + 4}L${cx - 4.5} ${cy + 4}Z"`,
    other: (cx, cy) => `<path d="M${cx - 4.5} ${cy - 4}L${cx + 4.5} ${cy - 4}L${cx} ${cy + 4}Z" fill="none"`,
  };

  const LEGEND = {
    "is-lanes-reported": ["line", "lanes reported by both ends: count, rate, PHYs at each end"],
    "is-lanes-unreported": ["line", "lanes not reported: the link is in the payload, its lanes are not"],
    "is-plain-link": ["line", "link between a host, source, device and enclosure (no lane data kept)"],
    "is-bundle": ["line", "bay bundle: the count of enclosure-to-bay links it carries"],
    "is-on-path": ["line", "traced path (hover, focus, search or pin)"],
    "mark-parent-unknown": ["mark", "parent link not reported"],
    "mark-not-read": ["mark", "dashed band: not read (not collected, or the read failed), or read, but its completeness not reported"],
    "bay-yes": ["bay", "bay with a disk"],
    "bay-no": ["bay", "empty bay"],
    "bay-unknown": ["bay", "occupancy not read"],
    "path-active": ["shape", "path active (filled circle)"],
    "path-standby": ["shape", "path passive or standby (ring)"],
    "path-fault": ["shape", "path failed (cross)"],
    "path-missing": ["shape", "path missing (open square)"],
    "path-mapped": ["shape", "mapped by SES, no path state (diamond)"],
    "path-unmapped": ["shape", "no SES mapping (open diamond)"],
    "path-unknown": ["shape", "path state unknown (square)"],
    "path-member": ["shape", "member health, not a path state (triangle)"],
    "path-other": ["shape", "path state not recognised (open triangle)"],
    "path-more": ["text", "+N: more paths than the cell can draw; the cell's name lists every path"],
  };

  // The basis is read before the word: member health is never a transport
  // or SES state, whatever word it uses.
  function pathTone(state, basis = "") {
    if (basis === MEMBER_HEALTH) return "member";
    return own(PATH_STATE_TONES, String(state || "").toLowerCase()) || "other";
  }

  function markShape(tone, cx, cy, state) {
    return `${MARK_SHAPES[tone](cx, cy)} class="topo-path-mark path-${tone}" data-path-state="${escapeHtml(state)}"></${tone === "active" || tone === "standby" ? "circle" : tone === "missing" || tone === "unknown" ? "rect" : "path"}>`;
  }

  function onPathClass(path, set, id) {
    if (!path) return "";
    return path[set].has(id) ? " is-on-path" : " is-dim";
  }

  function linkStyle(link) {
    if (link.bundle) return "is-bundle";
    if (link.lanesState === "reported") return "is-lanes-reported";
    if (link.lanesState === "not_reported") return "is-lanes-unreported";
    return "is-plain-link";
  }

  function linkName(model, link) {
    const source = model.nodeById.get(link.source)?.label || link.source;
    const group = model.groupById.get(link.target);
    const target = model.nodeById.get(link.target)?.label || (group ? (group.locationKnown ? "bay group" : "disk group") : link.target);
    return `Link ${source} to ${target}`;
  }

  function linkDescription(model, link) {
    const parts = [linkName(model, link)];
    if (link.lanesState === "reported") {
      parts.push(plural(link.lanes.count, "lane"), formatRate(link.lanes.rate));
      parts.push(`PHYs ${link.lanes.sourcePhys.join(", ") || "not listed"} to PHYs ${link.lanes.targetPhys.join(", ") || "not listed"}`);
    } else if (link.lanesState === "not_reported") {
      parts.push("lanes not reported");
    } else if (link.bundle) {
      parts.push(`${link.count} enclosure-to-bay ${link.count === 1 ? "link" : "links"}`);
    }
    return parts.join(", ");
  }

  // Each HBA chain's look: one of four palette colours and one of eight line
  // patterns. The first eight chains differ in pattern, so a chain never
  // depends on colour alone there; any two of the first sixteen differ in
  // colour, pattern or both.
  const CHAIN_COLOURS = 4;
  const CHAIN_DASHES = 8;
  const CHAIN_LOOKS = CHAIN_COLOURS * CHAIN_DASHES;
  function chainStyle(index) {
    return { colour: (index + Math.floor(index / CHAIN_DASHES)) % CHAIN_COLOURS, dash: index % CHAIN_DASHES };
  }

  function chainClass(index) {
    if (index < 0) return " chain-none";
    const style = chainStyle(index);
    return ` chain-${index} chain-colour-${style.colour} chain-dash-${style.dash}`;
  }

  function renderLink(model, link, path, used) {
    if (!link.points) return { line: "", label: "" };
    const style = linkStyle(link);
    used.add(style);
    const highlight = onPathClass(path, "links", link.id) + (pinnedRef(path, "link", link.id) ? " is-pinned" : "");
    const points = link.points.map(([x, y]) => `${Math.round(x)},${Math.round(y)}`).join(" ");
    const name = linkDescription(model, link);
    const chain = chainClass(link.chain ? model.chains.indexOf(link.chain) : -1);
    const line = `<polyline class="topo-link ${style}${chain}${highlight}" data-topo-link="${escapeHtml(link.id)}" points="${points}"><title>${escapeHtml(name)}</title></polyline>`;
    const box = link.labelBox;
    let body;
    if (link.labelLines.length) {
      body = `<rect class="topo-label-box" x="${Math.round(box.x)}" y="${Math.round(box.y)}" width="${Math.round(box.w)}" height="${Math.round(box.h)}" rx="7"></rect>`
        + link.labelLines.map((text, index) => `<text class="topo-label-text" x="${Math.round(box.x + box.w / 2)}" y="${Math.round(box.y + 14 + index * 15)}" text-anchor="middle">${escapeHtml(text)}</text>`).join("");
    } else {
      body = `<circle class="topo-junction" cx="${Math.round(box.x + box.w / 2)}" cy="${Math.round(box.y + box.h / 2)}" r="6"></circle>`;
    }
    const label = `<g class="topo-link-label ${style}${highlight}" role="button" tabindex="0" data-fabric-topology-link="${escapeHtml(link.id)}" data-topo-link="${escapeHtml(link.id)}" aria-label="${escapeHtml(name)}"><title>${escapeHtml(name)}</title>${body}</g>`;
    return { line, label };
  }

  function nodeTitle(entry) {
    const phys = entry.phyGroups.length ? `PHYs ${entry.phyGroups.map((group) => phyRanges(group.phys)).join(" | ")}` : "";
    return [entry.label, ...entry.facts, phys, entry.parentUnknown && entry.kind !== "host" ? "parent link not reported" : ""].filter(Boolean).join(", ");
  }

  function renderNode(entry, path, used) {
    const highlight = onPathClass(path, "nodes", entry.id) + (pinnedRef(path, "node", entry.id) ? " is-pinned" : "");
    const title = nodeTitle(entry);
    if (entry.parentUnknown && entry.kind !== "host") used.add("mark-parent-unknown");
    const text = entry.lines.map((line, index) => `<text class="topo-node-${index < entry.titleLines ? "title" : "fact"}" x="${entry.x + 10}" y="${entry.y + 20 + index * LINE_H}">${escapeHtml(line)}</text>`).join("");
    const unknown = entry.parentUnknown && entry.kind !== "host" ? " mark-parent-unknown" : "";
    return `<g class="topo-node kind-${token(entry.kind)}${unknown}${highlight}" role="button" tabindex="0" data-fabric-node="${escapeHtml(entry.id)}" data-topo-node="${escapeHtml(entry.id)}" aria-label="${escapeHtml(title)}"><title>${escapeHtml(title)}</title><rect x="${entry.x}" y="${entry.y}" width="${entry.w}" height="${entry.h}" rx="8"></rect>${text}</g>`;
  }

  function bayTitle(bay) {
    const occupancy = { yes: "disk present", no: "empty", unknown: "occupancy not read" }[bay.occupancy];
    const paths = bay.pathStates.length
      ? `paths: ${bay.pathStates.map((state) => `${state.controller || "path"} ${state.basis === MEMBER_HEALTH ? "member health " : ""}${state.state}`).join(", ")}`
      : "no path state in payload";
    return [bay.label, bay.locationKnown ? "" : LOCATION_UNKNOWN, occupancy, bay.deviceName, paths].filter(Boolean).join(", ");
  }

  // How many marks fit in a cell, keeping room for a "+N" count when needed.
  function marksFor(bay) {
    const fit = Math.max(1, Math.floor((bay.w - 4) / MARK_PITCH));
    if (bay.pathStates.length <= fit) return { shown: bay.pathStates, more: 0 };
    const shown = bay.pathStates.slice(0, Math.max(1, fit - 1));
    return { shown, more: bay.pathStates.length - shown.length };
  }

  function renderBayCell(bay, path, used) {
    const highlight = onPathClass(path, "bays", bay.bayId) + (pinnedRef(path, "bay", bay.bayId) ? " is-pinned" : "");
    used.add(`bay-${bay.occupancy}`);
    const hook = bay.traceId ? `data-fabric-trace="${escapeHtml(bay.traceId)}"` : `data-fabric-node="${escapeHtml(bay.bayId)}"`;
    const { shown, more } = marksFor(bay);
    const markY = bay.y + CELL_BOX - 8;
    const marks = shown.map((state, index) => {
      const tone = pathTone(state.state, state.basis);
      used.add(`path-${tone}`);
      return markShape(tone, bay.x + 2 + MARK / 2 + index * MARK_PITCH, markY, state.state);
    }).join("");
    let count = "";
    if (more) {
      used.add("path-more");
      count = `<text class="topo-path-more" x="${bay.x + bay.w - 2}" y="${markY + 4}" text-anchor="end">+${more}</text>`;
    }
    const title = bayTitle(bay);
    // A bay label with no supported number is shown as the producer wrote it.
    const shownLabel = bay.locationKnown ? bay.number || bay.label : bay.label;
    const number = shownLabel ? `<text class="topo-bay-number" x="${bay.x + bay.w / 2}" y="${bay.y + 14}" text-anchor="middle">${escapeHtml(shownLabel)}</text>` : "";
    return `<g class="topo-bay bay-${bay.occupancy} state-${token(bay.status)}${bay.locationKnown ? "" : " is-logical"}${highlight}" role="button" tabindex="0" ${hook} data-topo-bay="${escapeHtml(bay.bayId)}" aria-label="${escapeHtml(title)}"><title>${escapeHtml(title)}</title><rect x="${bay.x}" y="${bay.y}" width="${bay.w}" height="${CELL_BOX}" rx="3"></rect>${number}${marks}${count}</g>`;
  }

  function pathSummary(summary) {
    return [...summary.pathStates.entries()]
      .sort(([left], [right]) => naturalCompare(left, right))
      .map(([name, states]) => `${name}: ${[...states.entries()].map(([state, count]) => `${count} ${state}`).join(", ")}`)
      .join(" · ");
  }

  function groupHeading(group) {
    if (!group.locationKnown) {
      return `Disks, ${LOCATION_UNKNOWN} · ${plural(group.bayIds.length, "disk")}`;
    }
    const count = plural(group.bayIds.length, "bay");
    if (!group.enclosureIds.length) return `No enclosure link · ${count}`;
    const numbers = group.bays.map((bay) => bay.number).filter(Boolean).sort((left, right) => Number(left) - Number(right));
    const span = numbers.length > 1 && numbers[0] !== numbers[numbers.length - 1] ? `${numbers[0]}–${numbers[numbers.length - 1]}` : numbers[0];
    // Only some labels carry a number: say how many, never a range for all.
    if (numbers.length && numbers.length < group.bayIds.length) return `${count}, ${numbers.length} numbered: ${span}`;
    const range = numbers.length ? `Bays ${numbers[0]}–${numbers[numbers.length - 1]}` : "Bays";
    return `${range} · ${count}`;
  }

  // Where a bay group's expand or collapse control is drawn: the same place
  // on the group in both states.
  function toggleBox(group) {
    return { x: group.x + 10, y: group.y + 4, w: 160, h: 22 };
  }

  function renderGroup(model, group, path, used) {
    const highlight = path ? (path.groups.has(group.id) ? " is-on-path" : " is-dim") : "";
    if (!group.enclosureIds.length) used.add("mark-not-read");
    const parts = [`<rect class="topo-group-box" x="${group.x}" y="${group.y}" width="${group.w}" height="${group.h}" rx="8"></rect>`,
      ...group.lines.map((line, index) => `<text class="topo-node-${line.title ? "title" : "fact"}" x="${group.x + 10}" y="${group.y + (group.textTop || 0) + 18 + index * LINE_H}">${escapeHtml(line.text)}</text>`)];
    const summaryText = pathSummary(group.summary);
    const cells = group.cells.map((bay) => renderBayCell(bay, path, used)).join("");
    let toggle = "";
    if (group.expandable) {
      const label = group.collapsed ? `Draw all ${group.bayIds.length} ${group.locationKnown ? "bays" : "disks"}` : "Collapse to a summary";
      const { x, y } = toggleBox(group);
      toggle = `<g class="topo-expand" role="button" tabindex="0" data-fabric-expand-slots="${escapeHtml(group.expandKey)}" aria-expanded="${group.collapsed ? "false" : "true"}" aria-label="${escapeHtml(label)}"><rect x="${x}" y="${y}" width="160" height="22" rx="6"></rect><text class="topo-label-text" x="${x + 80}" y="${y + 15}" text-anchor="middle">${escapeHtml(label)}</text></g>`;
    }
    const name = `${groupHeading(group)}${group.collapsed ? `, ${group.summary.withDisk} with a disk, ${summaryText}` : ""}`;
    return `<g class="topo-group${group.collapsed ? " is-collapsed" : ""}${highlight}" data-topo-group="${escapeHtml(group.id)}" aria-label="${escapeHtml(name)}" role="group">${parts.join("")}${cells}${toggle}</g>`;
  }

  function renderTier(tier, model) {
    const y = tier.y ?? 0;
    const missing = tier.state === "not_read" || tier.state === "not_collected" || tier.state === "none";
    const tone = tier.state === "drawn" ? "ok" : token(tier.state);
    const labelLines = tier.labelLines || [tier.label];
    const noteLines = tier.noteLines || [tier.note];
    const label = `<g class="topo-tier"><title>${escapeHtml(`${tier.label}: ${tier.note}`)}</title>`
      + labelLines.map((line, index) => `<text class="topo-tier-label" x="${MARGIN}" y="${y + 18 + index * 17}">${escapeHtml(line)}</text>`).join("")
      + noteLines.map((line, index) => `<text class="topo-tier-note tone-${tone}" x="${MARGIN}" y="${y + 18 + (labelLines.length + index) * 17}">${escapeHtml(line)}</text>`).join("")
      + "</g>";
    let band = "";
    if (missing && tier.band) {
      const { x, w, h, lines } = tier.band;
      band = `<g class="topo-missing-tier mark-not-read tone-${tone}" data-topo-band="${escapeHtml(tier.id)}"><rect x="${Math.round(x)}" y="${y}" width="${Math.round(w)}" height="${h}" rx="8"></rect>`
        + lines.map((line, index) => `<text class="topo-node-fact" x="${Math.round(x) + 12}" y="${y + 22 + index * LINE_H}">${escapeHtml(line)}</text>`).join("") + "</g>";
    }
    return label + band;
  }

  // view: { pin: ref, hover: ref, query }. Hover wins while it lasts; a pin is
  // newer than the search it was made after, and typing a search clears it.
  function activePath(model, view = {}) {
    let path = null;
    if (view.hover) path = tracePath(model, view.hover);
    else if (view.pin) path = tracePath(model, view.pin);
    else if (view.query) {
      const found = searchTopology(model, view.query);
      path = found.matches.length ? found.path : null;
    }
    if (path) path.pin = view.pin || null;
    return path;
  }

  // The pinned item itself: what the page's inspector shows as selected.
  function pinnedRef(path, kind, id) {
    return Boolean(path?.pin && path.pin.kind === kind && path.pin.id === id);
  }

  function refLabel(model, ref) {
    if (!ref) return "";
    if (ref.kind === "link") {
      const link = model.linkById.get(ref.id);
      return link ? linkDescription(model, link) : "";
    }
    if (ref.kind === "bay") {
      const bay = model.bayById.get(ref.id);
      return bay ? bayTitle(bay) : "";
    }
    const entry = model.nodeById.get(ref.id);
    if (entry) return nodeTitle(entry);
    const group = model.groupById.get(ref.id);
    return group ? groupHeading(group) : "";
  }

  // Legend words for what this payload has: bays, disks without a physical
  // location, or both.
  function legendText(model, style, text) {
    const located = model.bays.some((bay) => bay.locationKnown);
    const logical = model.bays.some((bay) => !bay.locationKnown);
    if (!logical) return text;
    const words = located
      ? { "is-bundle": "bundle: the count of links to bays or disks it carries", "bay-yes": "bay with a disk, or a disk", "bay-no": "empty bay" }
      : { "is-bundle": "disk bundle: the count of links to disks it carries", "bay-yes": "disk present", "bay-no": "no disk" };
    return own(words, style) || text;
  }

  // The search row's label and placeholder for what this payload has.
  function searchHelp(model) {
    const located = Boolean(model?.bays.some((bay) => bay.locationKnown));
    const expanders = Boolean(model?.nodes.some((entry) => entry.kind === "expander"));
    const things = [located ? "bay" : "", "disk", expanders ? "expander" : ""].filter(Boolean);
    const label = `Find a ${things.length > 1 ? `${things.slice(0, -1).join(", ")} or ${things[things.length - 1]}` : things[0]}`;
    const placeholder = located ? "Bay number, disk name, serial or SAS address" : "Disk name, serial or SAS address";
    return { label, placeholder };
  }

  function legendSwatch(style, shape) {
    if (shape === "shape") {
      const tone = style.replace(/^path-/, "");
      return `<svg class="fabric-topology-swatch swatch-shape" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">${markShape(tone, 6, 6, tone)}</svg>`;
    }
    if (shape === "text") {
      return '<span class="fabric-topology-swatch swatch-text" aria-hidden="true">+N</span>';
    }
    return `<span class="fabric-topology-swatch swatch-${shape} ${style}" aria-hidden="true"></span>`;
  }

  function renderTopology(model, view = {}) {
    const path = activePath(model, view);
    // The traced-path entry is listed whatever is traced, so hover, focus,
    // search and a pin never change the legend's height above the drawing.
    const used = new Set(["is-on-path"]);
    const lines = [];
    const labels = [];
    for (const link of model.links) {
      const rendered = renderLink(model, link, path, used);
      lines.push(rendered.line);
      labels.push(rendered.label);
    }
    const tiers = model.tiers.map((tier) => renderTier(tier, model)).join("");
    // At a crossing the horizontal line has a small gap and the vertical one
    // runs through it: a short background stroke, then the vertical again as
    // two pieces that both start at the crossing centre, so every line pattern
    // (each starts with a dash) is painted through the centre.
    const chainIndex = new Map(model.chains.map((id, index) => [id, index]));
    const crossings = list(model.crossingMarks).map((mark) => {
      const over = model.linkById.get(mark.over);
      if (!over) return "";
      const cross = onPathClass(path, "links", over.id);
      const chain = chainClass(over.chain ? chainIndex.get(over.chain) ?? -1 : -1);
      const x = Math.round(mark.x);
      const y = Math.round(mark.y);
      const piece = (end) => `<line class="topo-link topo-cross-over ${linkStyle(over)}${chain}${cross}" data-topo-cross="${escapeHtml(over.id)}" x1="${x}" y1="${y}" x2="${x}" y2="${end}"></line>`;
      return `<line class="topo-cross-gap" x1="${x - 7}" y1="${y}" x2="${x + 7}" y2="${y}"></line>${piece(y - 9)}${piece(y + 9)}`;
    }).join("");
    if (model.tiers.some((tier) => tier.state === "not_read" || tier.state === "not_collected")) used.add("mark-not-read");
    const nodes = model.nodes.map((entry) => renderNode(entry, path, used)).join("");
    const groups = model.groups.map((group) => renderGroup(model, group, path, used)).join("");
    const svgLabel = `Storage Fabric topology: ${model.tiers.map((tier) => `${tier.label} ${tier.note}`).join("; ")}`;
    // The drawing at its own size; mount adds room around it when the view
    // kept on the screen needs it.
    const fullW = Math.ceil(model.width);
    const fullH = Math.ceil(model.height);
    // Fit: the whole drawing scaled into the box it was laid out for, never enlarged.
    const box = model.box || {};
    const scale = view.fit ? Math.min(1, box.width ? box.width / fullW : 1, box.height ? box.height / fullH : 1) : 1;
    const shownW = Math.max(1, Math.floor(fullW * scale));
    const shownH = Math.max(1, Math.floor(fullH * scale));
    const svg = `<svg class="fabric-topology-svg${view.fit ? " is-fitted" : ""}" role="group" aria-label="${escapeHtml(svgLabel)}" width="${shownW}" height="${shownH}" viewBox="0 0 ${fullW} ${fullH}">`
      + `<g class="topo-tiers">${tiers}</g><g class="topo-lines">${lines.join("")}</g><g class="topo-crossings">${crossings}</g><g class="topo-nodes">${nodes}${groups}</g><g class="topo-labels">${labels.join("")}</g></svg>`;
    const parts = model.parts.map((part) => `<span class="fabric-topology-part tone-${token(part.state)}"${part.title ? ` title="${escapeHtml(part.title)}"` : ""}>${escapeHtml(part.label)}: ${escapeHtml(part.state)}</span>`).join("");
    const source = model.core ? "HBA" : model.contract === "storage" ? "source" : "controller or source";
    const chains = model.chains.map((id, index) => `<span class="fabric-topology-legend-item" data-topology-legend="chain-${index}"><svg class="fabric-topology-swatch swatch-chain" width="28" height="8" viewBox="0 0 28 8" aria-hidden="true"><line class="topo-link is-lanes-reported${chainClass(index)}" x1="0" y1="4" x2="28" y2="4"></line></svg>${escapeHtml(`${model.nodeById.get(id)?.label || id}: every line from this ${source} down`)}</span>`).join("");
    const legend = chains + Object.entries(LEGEND)
      .filter(([style]) => used.has(style))
      .map(([style, [shape, text]]) => `<span class="fabric-topology-legend-item" data-topology-legend="${style}">${legendSwatch(style, shape)}${escapeHtml(legendText(model, style, text))}</span>`)
      .join("");
    const fitControl = `<button class="button secondary small fabric-topology-fit" type="button" data-topology-fit="${view.fit ? "actual" : "fit"}" aria-pressed="${view.fit ? "true" : "false"}">${view.fit ? "Actual size" : "Fit"}</button>`;
    const wide = !view.fit && box.width && fullW > box.width + 1
      ? `<p class="fabric-topology-wide-notice subtle" data-topology-wide-notice>The drawing is ${fullW} px wide, wider than its box (${Math.round(box.width)} px): scroll sideways for the rest, or choose Fit.</p>`
      : "";
    const smallest = 12 * scale;
    const fitNotice = scale < 1
      ? `<p class="fabric-topology-fit-notice warning-item muted compact" data-topology-fit-notice>Fitted to ${Math.round(scale * 100)}% of its size: its smallest text is ${smallest.toFixed(1)} px, under 12 px. Choose Actual size to read it.</p>`
      : "";
    const notices = model.notices.map((text) => `<div class="warning-item muted compact">${escapeHtml(text)}</div>`).join("");
    const pinned = view.pin ? `Pinned: ${refLabel(model, view.pin)}. Esc clears.` : DEFAULT_HINT;
    return `<div class="fabric-topology" data-topology-kind="${escapeHtml(model.kind)}">`
      + `<div class="fabric-topology-parts" aria-label="Part status">${parts}</div>`
      + `<div class="fabric-topology-legend" aria-label="Legend">${legend}</div>`
      + notices
      + `<div class="fabric-topology-bar">${fitControl}${wide}</div>`
      + fitNotice
      + `<div class="fabric-topology-scroll">${svg}</div>`
      // Below the drawing: hover text that wraps to more lines never moves
      // the drawing, and a label, from under the pointer.
      + `<p class="fabric-topology-pin subtle" data-topology-pin>${escapeHtml(pinned)}</p>`
      + `<p class="fabric-topology-not-shown subtle">${escapeHtml(model.core ? NOT_SHOWN : NOT_SHOWN_STORAGE)}</p>`
      + "</div>";
  }

  // The facts of a pinned link for the page's inspector: a bay bundle names its
  // source enclosure and the bays it carries; a lane link its lanes.
  function linkDetail(model, linkId) {
    const link = model?.linkById.get(linkId);
    if (!link) return null;
    const source = model.nodeById.get(link.source);
    const row = (label, value) => `<div class="kv-row"><span class="kv-label">${escapeHtml(label)}</span><span class="kv-value">${escapeHtml(value)}</span></div>`;
    if (link.bundle) {
      const group = model.groupById.get(link.target);
      const carried = list(group?.bays).filter((bay) => bay.sources.includes(link.source));
      const unit = group?.locationKnown ? "bay" : "disk";
      // Only the numbers the payload's labels carry: a bay without one is
      // counted, never read as 0.
      const numbered = carried.filter((bay) => bay.number !== "").map((bay) => Number(bay.number)).sort((a, b) => a - b);
      const unnumbered = carried.length - numbered.length;
      const described = [`${carried.length} ${unit}s`];
      if (group?.locationKnown && numbered.length) described.push(`numbers ${phyRanges(numbered)}`);
      const counted = group?.locationKnown && unnumbered
        ? `; ${plural(unnumbered, "bay")} whose ${unnumbered === 1 ? "label carries" : "labels carry"} no number`
        : "";
      const names = carried.length <= 48 ? carried.map((bay) => bay.label).join(", ") : `${described.join(", ")}${counted}`;
      return {
        title: `Selected ${unit} bundle`,
        html: `<div class="kv-grid">${row("Source enclosure", source ? `${source.label} (${source.id})` : link.source)}`
          + `${row(group?.locationKnown ? "Bays" : "Disks", plural(link.count, unit))}`
          + `${row(`${group?.locationKnown ? "Bays" : "Disks"} it carries`, names || "none")}</div>`,
      };
    }
    const target = model.nodeById.get(link.target);
    const rows = [row("From", source?.label || link.source), row("To", target?.label || link.target), row("Kind", link.kind.replace(/-/g, " "))];
    if (link.lanesState === "reported") {
      rows.push(row("Lanes", link.labelLines[0]), row("PHYs", link.labelLines[1].replace(/^PHY /, "")));
    } else if (link.lanesState === "not_reported") {
      rows.push(row("Lanes", "not reported"));
    }
    return { title: "Selected link", html: `<div class="kv-grid">${rows.join("")}</div>` };
  }

  // ---- DOM layer (browser only) ----------------------------------------------

  const live = {
    model: null, pin: null, hover: null, query: "", scope: null, fit: false, scroll: { left: 0, top: 0 },
    container: null, input: null, status: null, hooks: {},
    // Why the next mount chooses the scroll position: "open",
    // { expand: group id, anchor: its centre on screen, control: where its
    // expand control was on the page, when it was in view } or null (keep).
    firstView: null, expandedKeys: null, refocusFit: false, quiet: null,
    // scroll, actualScroll and restoreScroll are in drawing coordinates: the
    // offsets held before Fit, and those to restore on Actual size. room: the
    // space mount added before the drawing's left and top edges, so a view
    // kept in place there stays reachable. The inner width last measured
    // after a mount.
    actualScroll: null, restoreScroll: null, measuredWidths: new Set(), room: { left: 0, top: 0 },
    // The drawing's root that the last mount put in place, until release.
    mounted: null,
  };
  const DEFAULT_HINT = "Hover or focus anything to trace its path; click or press Enter to pin it; Esc clears.";

  function scrollBox(container) {
    const box = container?.querySelector?.(".fabric-topology-scroll");
    return box && typeof box === "object" ? box : null;
  }

  function refExists(model, ref) {
    if (!model || !ref) return false;
    if (ref.kind === "link") return model.linkById.has(ref.id);
    if (ref.kind === "bay") return model.bayById.has(ref.id);
    if (ref.kind === "group") return model.groupById.has(ref.id);
    return model.nodeById.has(ref.id);
  }

  function sameRef(left, right) {
    return Boolean(left && right && left.kind === right.kind && left.id === right.id);
  }

  // The page's selection ({ kind: "trace" | "node", id }) as a drawn item.
  function refFromSelection(model, selection) {
    if (!model || !selection) return null;
    if (model.bayById.has(selection.id)) return { kind: "bay", id: selection.id };
    const bay = selection.kind === "trace" ? model.bays.find((item) => item.traceId === selection.id) : null;
    if (bay) return { kind: "bay", id: bay.bayId };
    return model.nodeById.has(selection.id) ? { kind: "node", id: selection.id } : null;
  }

  function clearInput() {
    live.query = "";
    if (live.input) live.input.value = "";
    if (live.status) live.status.textContent = "";
  }

  // The bay groups drawn in full, from the page's expanded list.
  function expandedKeys(options) {
    const expanded = isRecord(options.expanded) ? options.expanded : {};
    return new Set(Object.keys(expanded).filter((key) => key.startsWith("topology:") && expanded[key]));
  }

  // The box's place on the page, where the browser can say.
  function boxRect(box) {
    const rect = typeof box?.getBoundingClientRect === "function" ? box.getBoundingClientRect() : null;
    return rect && Number.isFinite(rect.left) && Number.isFinite(rect.top) ? { left: rect.left, top: rect.top } : { left: 0, top: 0 };
  }

  function render(fabric, options = {}) {
    listen();
    const previous = live.model;
    const sameScope = live.scope !== null && previous?.scope === scopeOf(fabric);
    const previousOrder = sameScope ? Object.fromEntries(previous.tiers.map((tier) => [tier.id, tier.order || []])) : null;
    // The layout takes the scroll box's inner width: the box drawn last time
    // (scroll bar and borders already taken off), or before there is one,
    // the page's width for it less the box's two borders.
    const box = scrollBox(live.container);
    const measured = Number(box?.clientWidth) || 0;
    const width = measured > 0 ? measured : Math.max(0, (Number(options.width) || 0) - 2 * SCROLL_BORDER);
    const model = buildTopologyModel(fabric, { ...options, width, previousOrder });
    // The page replaces the map on every render: keep the drawing's scroll
    // position for the same scope, and start a new scope at the origin.
    const keys = expandedKeys(options);
    if (live.scope === null || model.scope !== live.scope) {
      if (live.scope !== null) {
        live.pin = null;
        clearInput();
      }
      live.scroll = { left: 0, top: 0 };
      live.firstView = "open";
    } else {
      if (box) live.scroll = { left: (Number(box.scrollLeft) || 0) - live.room.left, top: (Number(box.scrollTop) || 0) - live.room.top };
      // Back from Fit: the offsets the drawing had before it was fitted.
      if (live.restoreScroll) live.scroll = live.restoreScroll;
      // A shelf expanded or collapsed: keep its control where the operator
      // saw it on the screen when it was in view, else keep the shelf in view.
      const toggled = [...model.groups].find((group) => keys.has(group.expandKey) !== Boolean(live.expandedKeys?.has(group.expandKey)));
      const before = toggled && previous?.groupById.get(toggled.id);
      const clientHeight = Number(box?.clientHeight) || 0;
      if (toggled && before && live.firstView === null) {
        const control = toggleBox(before);
        const visible = Boolean(box) && measured > 0 && clientHeight > 0
          && control.x >= live.scroll.left && control.x + control.w <= live.scroll.left + measured
          && control.y >= live.scroll.top && control.y + control.h <= live.scroll.top + clientHeight;
        const rect = boxRect(box);
        live.firstView = {
          expand: toggled.id,
          anchor: centreOf(before) - live.scroll.left,
          control: visible ? { x: rect.left + control.x - live.scroll.left, y: rect.top + control.y - live.scroll.top } : null,
        };
      }
    }
    live.restoreScroll = null;
    live.expandedKeys = keys;
    live.scope = model.scope;
    live.model = model;
    if (options.selection !== undefined) {
      const selected = refFromSelection(model, options.selection);
      if (selected) live.pin = selected;
      else if (live.pin?.kind !== "link") live.pin = null;
    }
    if (!refExists(model, live.pin)) live.pin = null;
    live.hover = null;
    updateSearchStatus();
    const help = searchHelp(model);
    if (live.input) {
      live.input.placeholder = help.placeholder;
      const label = live.input.labels?.[0];
      if (label) label.textContent = help.label;
    }
    return renderTopology(model, { pin: live.pin, query: live.query, fit: live.fit });
  }

  // Where the scroll box starts: with the host and every HBA inside it when
  // they fit its width, and, after a shelf is expanded or collapsed, with
  // that shelf still where the operator saw it if both can hold.
  function firstViewLeft(model, reason, width, current) {
    const top = model.nodes.filter((entry) => entry.kind === "host" || entry.kind === "controller");
    const span = top.length ? [Math.min(...top.map((entry) => entry.x)), Math.max(...top.map((entry) => entry.x + entry.w))] : null;
    const fits = span && span[1] - span[0] <= width;
    let wanted = current;
    let range = [0, Math.max(0, model.width - width)];
    if (fits) range = [Math.max(range[0], span[1] - width), Math.min(range[1], span[0])];
    if (reason === "open") {
      wanted = span ? (span[0] + span[1]) / 2 - width / 2 : 0;
    } else if (reason?.expand) {
      const group = model.groupById.get(reason.expand);
      if (group) {
        wanted = centreOf(group) - reason.anchor;
        // The shelf itself stays in view: at least 200 px of it, or all of it.
        const keep = Math.min(group.w, width, 200);
        const own = [group.x + keep - width, group.x + group.w - keep];
        const both = [Math.max(range[0], own[0]), Math.min(range[1], own[1])];
        range = both[0] <= both[1] ? both : [Math.max(0, own[0]), Math.max(0, own[1])];
      }
    }
    return Math.round(Math.min(range[1], Math.max(range[0], wanted)));
  }

  // Room around the drawing so the view at drawing offsets (left, top) is
  // reachable: before its left or top edge where they are negative, after
  // its right or bottom edge where the view reaches past them. Mount sizes
  // it every time, so room no later view needs is given back.
  function sizeRoom(box, left, top) {
    const svg = box.querySelector?.(".fabric-topology-svg");
    const model = live.model;
    if (!svg || typeof svg.setAttribute !== "function" || !model || live.fit) return { left: 0, top: 0 };
    const room = { left: Math.max(0, Math.ceil(-left)), top: Math.max(0, Math.ceil(-top)) };
    const right = Math.max(Math.ceil(model.width), Math.ceil(left + (Number(box.clientWidth) || 0)));
    const bottom = Math.max(Math.ceil(model.height), Math.ceil(top + (Number(box.clientHeight) || 0)));
    const width = right + room.left;
    const height = bottom + room.top;
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    svg.setAttribute("viewBox", `${-room.left} ${-room.top} ${width} ${height}`);
    return room;
  }

  // After the page has put the drawing in place: set its scroll position,
  // and give focus back to Fit after a Fit click.
  function mount(container) {
    const box = scrollBox(container);
    if (box) {
      const reason = live.firstView;
      live.firstView = null;
      const width = Number(box.clientWidth) || live.model?.box?.width || 0;
      // Offsets in drawing coordinates: negative ones lie before its edge.
      let left = live.scroll.left;
      let top = live.scroll.top;
      const group = reason?.expand ? live.model?.groupById.get(reason.expand) : null;
      if (reason?.control && group && !live.fit) {
        // The shelf's expand control where it was on the screen: both
        // offsets, and the box's own move on the page, taken into account.
        // The room changes the drawing's height, so the box's own height
        // (up to its limit) and the page around it can still change: aim
        // again from where the box settled, and size the room for that.
        const control = toggleBox(group);
        const aim = () => {
          const rect = boxRect(box);
          left = rect.left + control.x - reason.control.x;
          top = rect.top + control.y - reason.control.y;
        };
        aim();
        sizeRoom(box, left, top);
        aim();
      } else if (reason && live.model && !live.fit && width) {
        left = firstViewLeft(live.model, reason, width, live.scroll.left);
        if (reason === "open") top = 0;
      }
      live.room = sizeRoom(box, left, top);
      left += live.room.left;
      top += live.room.top;
      if (box.scrollLeft !== left) box.scrollLeft = left;
      if (box.scrollTop !== top) box.scrollTop = top;
      // The box's inner width differs from the width the drawing was laid
      // out for (a scroll bar came or went): lay it out once more.
      // Once per inner width while Topology is shown, so a scroll bar that comes and goes cannot loop.
      const inner = Number(box.clientWidth) || 0;
      const root = container.querySelector?.(".fabric-topology") || null;
      live.mounted = root;
      if (inner > 0 && root && live.model && Math.abs(inner - (live.model.box?.width || 0)) >= 1 && !live.measuredWidths.has(inner)
        && typeof window.setTimeout === "function") {
        live.measuredWidths.add(inner);
        cancelWidthTimer();
        globals.widthTimer = window.setTimeout(() => {
          globals.widthTimer = null;
          // Only for the root still in place: a later redraw measured its own.
          if (live.mounted === root && live.container?.querySelector?.(".fabric-topology") === root) live.hooks.requestRender?.();
        }, 0);
      }
    }
    if (live.refocusFit) {
      live.refocusFit = false;
      container?.querySelector?.("[data-topology-fit]")?.focus?.({ preventScroll: true });
    }
  }

  // Leaving the mode: no pin, hover or search carries over, and the view's
  // document and window listeners are removed.
  function release() {
    live.pin = null;
    live.hover = null;
    live.quiet = null;
    live.scope = null;
    live.firstView = null;
    live.scroll = { left: 0, top: 0 };
    live.actualScroll = null;
    live.restoreScroll = null;
    live.measuredWidths = new Set();
    live.mounted = null;
    live.room = { left: 0, top: 0 };
    cancelWidthTimer();
    clearInput();
    unlisten();
  }

  function view() {
    return { pin: live.pin, hover: live.hover, query: live.query };
  }

  // The model the last render drew.
  function drawnModel() {
    return live.model;
  }

  function inspectorDetail() {
    return live.pin?.kind === "link" ? linkDetail(live.model, live.pin.id) : null;
  }

  function refFromElement(element) {
    const target = element?.closest?.("[data-topo-link], [data-topo-bay], [data-topo-node], [data-topo-group]");
    if (!target || !target.closest(".fabric-topology")) return null;
    if (target.dataset.topoLink) return { kind: "link", id: target.dataset.topoLink };
    if (target.dataset.topoBay) return { kind: "bay", id: target.dataset.topoBay };
    if (target.dataset.topoNode) return { kind: "node", id: target.dataset.topoNode };
    return { kind: "group", id: target.dataset.topoGroup };
  }

  function applyHighlight() {
    const root = live.container?.querySelector?.(".fabric-topology");
    if (!root || !live.model) return;
    const path = activePath(live.model, { hover: live.hover, pin: live.pin, query: live.query });
    const mark = (selector, set, key) => {
      root.querySelectorAll(selector).forEach((element) => {
        const id = element.getAttribute(key);
        element.classList.toggle("is-on-path", Boolean(path && path[set].has(id)));
        element.classList.toggle("is-dim", Boolean(path && !path[set].has(id)));
      });
    };
    mark("[data-topo-node]", "nodes", "data-topo-node");
    mark("[data-topo-link]", "links", "data-topo-link");
    mark("[data-topo-cross]", "links", "data-topo-cross");
    mark("[data-topo-bay]", "bays", "data-topo-bay");
    mark("[data-topo-group]", "groups", "data-topo-group");
    // The line under the legend names what is traced in full, so text a box
    // had to shorten is readable on hover and on keyboard focus alike.
    const pin = root.querySelector("[data-topology-pin]");
    if (pin) {
      if (live.hover) pin.textContent = `Tracing: ${refLabel(live.model, live.hover)}.${live.pin ? " Pinned item kept; Esc clears." : ""}`;
      else if (live.pin) pin.textContent = `Pinned: ${refLabel(live.model, live.pin)}. Esc clears.`;
      else pin.textContent = DEFAULT_HINT;
    }
  }

  function updateSearchStatus() {
    const status = live.status;
    if (!status) return;
    if (!live.query) {
      status.textContent = "";
      return;
    }
    const found = searchTopology(live.model, live.query);
    if (!found.matches.length) {
      status.textContent = `No match for "${live.query}".`;
      return;
    }
    const names = found.matches.slice(0, 6).map((item) => `${item.label}${item.collapsed ? " (inside a collapsed block)" : ""}`);
    const more = found.matches.length > names.length ? `, and ${found.matches.length - names.length} more` : "";
    status.textContent = `${found.matches.length} ${found.matches.length === 1 ? "match" : "matches"}: ${names.join(", ")}${more}.`;
  }

  // One document Esc handler and one window resize handler for the page,
  // however often the view is attached; added while Topology is shown and
  // removed when it is left.
  const globals = { listening: false, timer: null, widthTimer: null };

  function cancelWidthTimer() {
    if (globals.widthTimer !== null && typeof window.clearTimeout === "function") window.clearTimeout(globals.widthTimer);
    globals.widthTimer = null;
  }

  function onEscape(event) {
    if (event.key !== "Escape" || !live.container?.querySelector?.(".fabric-topology")) return;
    if (event.target?.closest?.("[data-fabric-alias-form]")) return;
    // The item that has focus stays quiet until focus or the pointer moves
    // to an item: the page puts focus back on it after the redraw.
    live.quiet = refFromElement(event.target) || live.hover || live.pin;
    live.pin = null;
    live.hover = null;
    clearInput();
    applyHighlight();
    // Esc clears the drawing's pin and the inspector's selection together.
    live.hooks.clearSelection?.();
    live.hooks.requestRender?.();
  }

  function onResize() {
    // The layout takes the box's width: lay it out again when that changes.
    if (globals.timer !== null) window.clearTimeout(globals.timer);
    globals.timer = window.setTimeout(() => {
      globals.timer = null;
      if (live.container?.querySelector?.(".fabric-topology")) live.hooks.requestRender?.();
    }, 200);
  }

  function listen() {
    if (globals.listening) return;
    if (typeof document !== "undefined" && typeof document.addEventListener === "function") document.addEventListener("keydown", onEscape);
    if (typeof window.addEventListener === "function") window.addEventListener("resize", onResize);
    globals.listening = true;
  }

  function unlisten() {
    if (!globals.listening) return;
    if (typeof document !== "undefined" && typeof document.removeEventListener === "function") document.removeEventListener("keydown", onEscape);
    if (typeof window.removeEventListener === "function") window.removeEventListener("resize", onResize);
    if (globals.timer !== null && typeof window.clearTimeout === "function") window.clearTimeout(globals.timer);
    globals.timer = null;
    globals.listening = false;
  }

  // hooks: { clearSelection(), requestRender() } from the page, so a link pin,
  // a typed search and Esc keep the page's inspector selection in step.
  function attach(container, searchInput, searchStatus, hooks = {}) {
    if (!container) return;
    Object.assign(live, { container, input: searchInput || null, status: searchStatus || null, hooks: hooks || {} });
    listen();
    if (container.dataset?.topologyAttached) return;
    if (container.dataset) container.dataset.topologyAttached = "true";
    const hover = (event) => {
      if (live.container !== container) return;
      const ref = refFromElement(event.target);
      if (!ref) return;
      if (live.quiet && event.type === "focusin" && sameRef(ref, live.quiet)) return;
      live.quiet = null;
      live.hover = ref;
      applyHighlight();
    };
    const leave = (event) => {
      if (live.container !== container || !live.hover) return;
      live.hover = refFromElement(event.relatedTarget);
      applyHighlight();
    };
    container.addEventListener("mouseover", hover);
    container.addEventListener("focusin", hover);
    container.addEventListener("mouseout", leave);
    container.addEventListener("focusout", leave);
    container.addEventListener("click", (event) => {
      if (live.container !== container) return;
      if (event.target?.closest?.("[data-topology-fit]")) {
        // Fit keeps the offsets the fitted drawing allows; Actual size goes
        // back to the offsets held before Fit.
        const box = scrollBox(container);
        if (!live.fit) live.actualScroll = { left: (Number(box?.scrollLeft) || 0) - live.room.left, top: (Number(box?.scrollTop) || 0) - live.room.top };
        else live.restoreScroll = live.actualScroll;
        live.fit = !live.fit;
        live.refocusFit = true;
        live.hooks.requestRender?.();
        return;
      }
      const element = event.target?.closest?.("[data-topo-link], [data-topo-bay], [data-topo-node]");
      const ref = refFromElement(element);
      if (!ref) return;
      live.quiet = null;
      live.pin = ref;
      applyHighlight();
      if (ref.kind === "link") {
        // A link is not a page selection: the inspector shows its facts.
        live.hooks.clearSelection?.();
        live.hooks.requestRender?.();
      }
    });
    container.addEventListener("keydown", (event) => {
      const element = event.target?.closest?.(".fabric-topology [role=\"button\"]");
      if (!element || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
      element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    });
    if (searchInput) {
      searchInput.addEventListener("input", () => {
        live.query = searchInput.value.trim();
        const hadPin = Boolean(live.pin);
        if (live.query) live.pin = null;
        updateSearchStatus();
        applyHighlight();
        if (hadPin && live.query) {
          live.hooks.clearSelection?.();
          live.hooks.requestRender?.();
        }
      });
    }
  }

  window.SasFabricTopology = {
    NOT_COLLECTED,
    BAY_DETAIL_LIMIT,
    buildTopologyModel,
    tracePath,
    searchTopology,
    renderTopology,
    pathTone,
    chainStyle,
    searchHelp,
    linkDetail,
    render,
    mount,
    release,
    view,
    drawnModel,
    inspectorDetail,
    attach,
  };
})();
