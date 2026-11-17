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
  const H_GAP = 28;
  const LEFT = 168;
  const MARGIN = 16;
  const CHAR_W = 7;
  const SUMMARY_W = 300;
  const SUMMARY_H = 104;
  const GROUP_HEAD = 42;
  const NAME_LIMIT = 4;
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

  function fabricKind(fabric) {
    if (typeof fabric?.raw?.fabric_kind === "string" && fabric.raw.fabric_kind) {
      return fabric.raw.fabric_kind;
    }
    return fabric?.platform === "core" ? "core_sas" : "unknown";
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

  // ---- tiers and nodes -------------------------------------------------------

  function tierForNode(item, core) {
    const kind = String(item.kind || "");
    if (kind === "host") return "host";
    if (kind === "controller") return "controllers";
    if (kind === "expander") return core ? "expanders" : null;
    if (kind === "path") return core ? null : "devices";
    if (kind === "bay") return "bays";
    if (kind.endsWith("enclosure")) return "enclosures";
    return null;
  }

  // Kinds the view leaves out on purpose; anything else it cannot draw is named.
  function hiddenByDesign(item, core) {
    return HIDDEN_KINDS.has(String(item.kind || "")) || (core && item.kind === "path");
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
    const caption = ses ? "SES device" : PATH_CAPTIONS[metrics.path_type] || "storage path";
    if (!item.status) return caption;
    return metrics.state_basis === MEMBER_HEALTH ? `${caption} · member health ${item.status}` : `${caption} · ${item.status}`;
  }

  function nodeFacts(item, core, kind) {
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
      } else {
        facts.push("storage source, not an HBA");
      }
    } else if (item.kind === "expander") {
      const level = expanderLevel(item);
      const parts = [level === null ? "level not reported" : `level ${level}`];
      const phys = decimal(metrics.num_phys);
      if (notRead.has("linked_phys")) {
        parts.push("PHY table not read");
      } else if (phys !== null) {
        const linked = decimal(metrics.linked_phys);
        parts.push(`${linked === null ? "?" : linked} of ${phys} PHYs linked`);
      }
      facts.push(parts.join(" · "));
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
    const kind = fabricKind(fabric);
    const core = kind === "core_sas";
    const payload = readPayload(fabric);
    const traces = new Map(payload.traces.map((trace) => [trace.id, trace]));
    const inventory = inventoryBySlot(fabric, options.inventory);
    const skipped = { unsupported: [], duplicateNodes: [], missingEnd: [], duplicateLinks: [], unjoined: [] };

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
      const tier = tierForNode(item, core);
      if (!tier) {
        if (!hiddenByDesign(item, core)) skipped.unsupported.push(`${item.kind || "no kind"}: ${item.id}`);
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
        status: item.status || "",
        level: item.kind === "expander" ? expanderLevel(item) : null,
        facts: nodeFacts(item, core, kind),
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
      const unsupportedEnd = [sourceItem, targetItem].some((end) => !tierForNode(end, core) && !hiddenByDesign(end, core));
      if (bayIds.has(item.target) && source && (source.tier === "enclosures" || (!core && source.tier === "devices"))) {
        bayLinks.push(item);
        continue;
      }
      if (!source || !target || item.source === item.target) {
        const hiddenEnd = [sourceItem, targetItem].some((end) => hiddenByDesign(end, core) || (end.kind === "bay" && !source));
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
      if ((entry.tier === "expanders" || entry.tier === "enclosures" || entry.tier === "devices" || entry.tier === "controllers") && !incoming.has(entry.id)) {
        entry.parentUnknown = true;
      }
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
    const pathStatesByBay = new Map();
    for (const item of payload.links) {
      if (item.kind !== "path-bay") continue;
      const controller = String(item.source || "").split(":")[1] || "";
      if (!pathStatesByBay.has(item.target)) pathStatesByBay.set(item.target, []);
      pathStatesByBay.get(item.target).push({ controller, state: String(item.status || "unknown"), basis: "" });
    }
    const groupsByKey = new Map();
    const groupByBay = new Map();
    const bays = [];
    for (const item of bayRows) {
      if (payloadNodeIds.get(item.id) !== item) continue;
      const trace = traces.get(item.id);
      const metrics = isRecord(item.metrics) ? item.metrics : {};
      const traced = list(trace?.metrics?.path_states).filter(isRecord).map((state) => ({
        controller: String(state.controller ?? ""),
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
        label: nodeLabel(item),
        locationKnown: located,
        status: item.status || "",
        occupancy: bayOccupancy(metrics),
        deviceName: metrics.device_name || trace?.metrics?.device_name || "",
        terms: searchTerms(item, trace, inventorySlot),
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
      const collapsed = raw.bays.length > BAY_DETAIL_LIMIT && !expanded[expandKey];
      const summary = { bays: raw.bays.length, withDisk: 0, empty: 0, unknown: 0, pathStates: {} };
      for (const bay of raw.bays) {
        if (bay.occupancy === "yes") summary.withDisk += 1;
        else if (bay.occupancy === "no") summary.empty += 1;
        else summary.unknown += 1;
        for (const state of bay.pathStates) {
          const name = state.controller || "path";
          summary.pathStates[name] = summary.pathStates[name] || {};
          summary.pathStates[name][state.state] = (summary.pathStates[name][state.state] || 0) + 1;
        }
      }
      // A disk without a physical location is drawn with its own label, so
      // its cell is as wide as the longest label in the group.
      const cellWidth = raw.locationKnown ? CELL_BOX : Math.max(CELL_BOX, ...raw.bays.map((bay) => bay.label.length * CHAR_W + 12));
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
      notices: [],
      counts: { bayNodes: bayRows.length, baysPlaced: groups.reduce((total, group) => total + group.bayIds.length, 0) },
      scope: isRecord(fabric) ? `${fabric.system_id ?? ""}\u0000${fabric.selected_enclosure_id ?? ""}` : "",
      width: 0,
      height: 0,
    };
    if (payload.problems.length) {
      model.notices.push(`Payload not readable: ${payload.problems.join("; ")}. What could not be read is not drawn.`);
    }
    const notDrawn = [];
    if (skipped.unsupported.length) notDrawn.push(`${plural(skipped.unsupported.length, "node")} of a kind this view does not draw (${named(skipped.unsupported)})`);
    if (skipped.missingEnd.length) notDrawn.push(`${plural(skipped.missingEnd.length, "link")} with an end the payload does not carry (${named(skipped.missingEnd)})`);
    if (skipped.unjoined.length) notDrawn.push(`${plural(skipped.unjoined.length, "link")} between items this view does not join (${named(skipped.unjoined)})`);
    if (skipped.duplicateNodes.length) notDrawn.push(`${plural(skipped.duplicateNodes.length, "duplicate node id")} (${named(skipped.duplicateNodes)})`);
    if (skipped.duplicateLinks.length) notDrawn.push(`${plural(skipped.duplicateLinks.length, "duplicate link id")} (${named(skipped.duplicateLinks)})`);
    if (model.counts.bayNodes !== model.counts.baysPlaced) {
      notDrawn.push(`${plural(model.counts.bayNodes, "bay row")} in the payload, ${plural(model.counts.baysPlaced, "bay")} drawn`);
    }
    if (notDrawn.length) {
      model.notices.push(`Not drawn: ${notDrawn.join("; ")}.`);
    }
    for (const group of groups) {
      if (group.collapsed) {
        const names = group.enclosureIds.map((id) => nodeById.get(id)?.label || id).join(" and ");
        const what = group.locationKnown ? "bays" : "disks";
        model.notices.push(`${group.bayIds.length} ${what}${names ? ` behind ${names}` : ""} are shown as a summary block; expand it to draw each one.`);
      }
    }
    layoutModel(model);
    return model;
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

  function groupSize(group) {
    if (group.collapsed) {
      return [SUMMARY_W, SUMMARY_H];
    }
    const count = group.cells.length;
    const pitch = group.cellWidth + (CELL - CELL_BOX);
    const perRow = group.locationKnown ? CELLS_PER_ROW : Math.max(1, Math.floor((CELLS_PER_ROW * CELL) / pitch));
    const columns = Math.max(1, Math.min(perRow, count));
    const rows = Math.max(1, Math.ceil(count / perRow));
    group.perRow = perRow;
    group.pitch = pitch;
    // An unlinked or logical group is wide enough to say so in full.
    const minimum = group.enclosureIds.length && group.locationKnown ? NODE_W : 360;
    return [Math.max(minimum, columns * pitch + 16), GROUP_HEAD + rows * CELL + 10];
  }

  function layoutModel(model) {
    const rows = model.tiers.map((tier) => ({
      tier,
      items: tier.id === "bays" ? model.groups : tier.nodeIds.map((id) => model.nodeById.get(id)).filter(Boolean),
    }));
    for (const group of model.groups) {
      [group.w, group.h] = groupSize(group);
    }
    const centre = (item) => item.x + item.w / 2;
    const sweep = (items, desired) => {
      items.sort((left, right) => desired(left) - desired(right) || naturalCompare(left.label || left.id, right.label || right.id));
      let cursor = LEFT;
      for (const item of items) {
        const want = desired(item) - item.w / 2;
        item.x = Math.max(cursor, Number.isFinite(want) ? want : cursor);
        cursor = item.x + item.w + H_GAP;
      }
    };
    // Top down: each item under the mean of its parents; then bottom up.
    let order = 0;
    for (const row of rows) {
      sweep(row.items, (item) => {
        const parents = list(model.incoming.get(item.id)).map((link) => model.nodeById.get(link.source)).filter(Boolean);
        return parents.length ? parents.reduce((sum, parent) => sum + centre(parent), 0) / parents.length : LEFT + (order += 1);
      });
    }
    for (let index = rows.length - 2; index >= 0; index -= 1) {
      sweep(rows[index].items, (item) => {
        const children = list(model.outgoing.get(item.id))
          .map((link) => model.nodeById.get(link.target) || model.groupById.get(link.target))
          .filter(Boolean);
        return children.length ? children.reduce((sum, child) => sum + centre(child), 0) / children.length : centre(item);
      });
    }

    // Vertical: each gap holds a bus track per parent and the stacked labels.
    let y = MARGIN + 8;
    rows.forEach((row, index) => {
      const into = model.links.filter((link) => row.items.some((item) => item.id === link.target));
      if (index > 0) {
        const parents = new Set(into.map((link) => link.source));
        const perChild = new Map();
        for (const link of into) perChild.set(link.target, (perChild.get(link.target) || 0) + 1);
        const maxIncoming = Math.max(0, ...perChild.values());
        const step = into.some((link) => link.labelLines.length > 1) ? 34 : 20;
        row.track = Math.min(6, 90 / Math.max(1, parents.size));
        row.busZone = 12 + parents.size * row.track;
        row.step = step;
        row.gapTop = y;
        y += row.busZone + maxIncoming * step + 14;
        if (!into.length) y += 8;
      }
      row.y = y;
      const height = row.items.length ? Math.max(...row.items.map((item) => item.h)) : 36;
      row.height = height;
      for (const item of row.items) item.y = y;
      row.tier.y = y;
      row.tier.h = height;
      y += height;
    });
    model.height = y + MARGIN;
    model.width = Math.max(LEFT + NODE_W + MARGIN, ...rows.flatMap((row) => row.items.map((item) => item.x + item.w + MARGIN)));

    // Cells inside bay groups.
    for (const group of model.groups) {
      group.cells.forEach((bay, index) => {
        bay.x = group.x + 8 + (index % group.perRow) * group.pitch;
        bay.y = group.y + GROUP_HEAD + Math.floor(index / group.perRow) * CELL;
      });
    }

    // Routes: parent exit -> bus track above the child's row -> drop -> child.
    const rowOf = new Map();
    rows.forEach((row) => row.items.forEach((item) => rowOf.set(item.id, row)));
    const itemOf = (id) => model.nodeById.get(id) || model.groupById.get(id);
    for (const row of rows) {
      const into = model.links.filter((link) => rowOf.get(link.target) === row);
      const parents = [...new Set(into.map((link) => link.source))]
        .sort((left, right) => centre(itemOf(left)) - centre(itemOf(right)));
      for (const link of into) {
        const parent = itemOf(link.source);
        const child = itemOf(link.target);
        if (!parent || !child) continue;
        const siblings = list(model.outgoing.get(link.source)).slice().sort((left, right) => centre(itemOf(left.target)) - centre(itemOf(right.target)));
        const exitIndex = siblings.indexOf(link);
        const exitX = parent.x + parent.w * (exitIndex + 1) / (siblings.length + 1);
        const arrivals = list(model.incoming.get(link.target)).slice().sort((left, right) => centre(itemOf(left.source)) - centre(itemOf(right.source)));
        const k = arrivals.indexOf(link);
        const dropX = child.x + child.w * (k + 1) / (arrivals.length + 1);
        const busY = row.gapTop + 8 + parents.indexOf(link.source) * row.track;
        const startY = parent.y + parent.h;
        link.points = startY < busY
          ? [[exitX, startY], [exitX, busY], [dropX, busY], [dropX, child.y]]
          : [[exitX, startY], [dropX, child.y]];
        const lines = link.labelLines.length ? link.labelLines : [];
        const width = lines.length ? Math.max(...lines.map((line) => line.length)) * CHAR_W + 14 : 14;
        const height = lines.length ? lines.length * 15 + 4 : 14;
        const labelBottom = child.y - 6 - (arrivals.length - 1 - k) * row.step;
        link.labelBox = { x: dropX - width / 2, y: labelBottom - height, w: width, h: height };
      }
    }
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

  // A number (or "bay N") is a bay number and matches only bays with a
  // physical location; any other text matches labels, disk names, serials,
  // disk and expander SAS addresses.
  function searchTopology(model, query) {
    const text = String(query ?? "").trim().toLowerCase();
    const result = { query: text, matches: [], path: emptyPath() };
    if (!model || !text) return result;
    const bayNumber = /^(?:bay\s*)?(\d+)$/.exec(text);
    for (const bay of model.bays) {
      const group = model.groupByBay.get(bay.bayId);
      let hit = false;
      if (bayNumber) {
        hit = bay.locationKnown && bay.slot !== null && bay.slot === Number(bayNumber[1]);
      } else {
        hit = bay.label.toLowerCase().includes(text) || bay.terms.some((value) => value.includes(text));
      }
      if (hit) {
        result.matches.push({ id: bay.bayId, kind: "bay", label: bay.label, groupId: group?.id || "", collapsed: Boolean(group?.collapsed) });
        bayPath(model, bay.bayId, result.path);
      }
    }
    if (!bayNumber) {
      for (const entry of model.nodes) {
        if (entry.kind === "host") continue;
        if ([entry.label, entry.rawId, entry.id].some((value) => value && String(value).toLowerCase().includes(text))) {
          result.matches.push({ id: entry.id, kind: "node", label: entry.label, groupId: "", collapsed: false });
          tracePath(model, { kind: "node", id: entry.id }, result.path);
        }
      }
    }
    return result;
  }

  // ---- SVG rendering ---------------------------------------------------------

  // Path marks differ by shape as well as colour (8 px, centred on cx, cy).
  const MARK_SHAPES = {
    active: (cx, cy) => `<circle cx="${cx}" cy="${cy}" r="4"`,
    standby: (cx, cy) => `<circle cx="${cx}" cy="${cy}" r="3.25"`,
    fault: (cx, cy) => `<path d="M${cx - 4} ${cy - 4}L${cx + 4} ${cy + 4}M${cx + 4} ${cy - 4}L${cx - 4} ${cy + 4}"`,
    missing: (cx, cy) => `<rect x="${cx - 3.25}" y="${cy - 3.25}" width="6.5" height="6.5"`,
    mapped: (cx, cy) => `<path d="M${cx} ${cy - 4.5}L${cx + 4.5} ${cy}L${cx} ${cy + 4.5}L${cx - 4.5} ${cy}Z"`,
    unmapped: (cx, cy) => `<path d="M${cx} ${cy - 4}L${cx + 4} ${cy}L${cx} ${cy + 4}L${cx - 4} ${cy}Z"`,
    unknown: (cx, cy) => `<rect x="${cx - 4}" y="${cy - 4}" width="8" height="8"`,
    member: (cx, cy) => `<path d="M${cx} ${cy - 4}L${cx + 4.5} ${cy + 4}L${cx - 4.5} ${cy + 4}Z"`,
    other: (cx, cy) => `<path d="M${cx} ${cy - 3.5}L${cx + 4} ${cy + 3.5}L${cx - 4} ${cy + 3.5}Z"`,
  };

  const LEGEND = {
    "is-lanes-reported": ["line", "lanes reported by both ends: count, rate, PHYs at each end"],
    "is-lanes-unreported": ["line", "lanes not reported: the link is in the payload, its lanes are not"],
    "is-plain-link": ["line", "link between a host, source, device and enclosure (no lane data kept)"],
    "is-bundle": ["line", "bay bundle: the count of enclosure-to-bay links it carries"],
    "is-on-path": ["line", "traced path (hover, focus, search or pin)"],
    "mark-parent-unknown": ["mark", "parent link not reported"],
    "mark-not-read": ["mark", "not read: the tier was not collected or could not be read"],
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

  function pathTone(state, basis = "") {
    const tone = PATH_STATE_TONES[String(state || "").toLowerCase()];
    if (tone) return tone;
    return basis === MEMBER_HEALTH ? "member" : "other";
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

  function renderLink(model, link, path, used) {
    if (!link.points) return { line: "", label: "" };
    const style = linkStyle(link);
    used.add(style);
    const highlight = onPathClass(path, "links", link.id);
    const points = link.points.map(([x, y]) => `${Math.round(x)},${Math.round(y)}`).join(" ");
    const name = linkDescription(model, link);
    const line = `<polyline class="topo-link ${style}${highlight}" data-topo-link="${escapeHtml(link.id)}" points="${points}"><title>${escapeHtml(name)}</title></polyline>`;
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

  function renderNode(entry, path, used) {
    const highlight = onPathClass(path, "nodes", entry.id);
    const title = [entry.label, ...entry.facts, entry.parentUnknown ? "parent link not reported" : ""].filter(Boolean).join(", ");
    const lines = [clip(entry.label, entry.w), ...entry.facts.map((fact) => clip(fact, entry.w))].slice(0, 3);
    if (entry.phyGroups.length) {
      lines[2] = clip(`PHYs ${entry.phyGroups.map((group) => phyRanges(group.phys)).join(" | ")}`, entry.w);
    }
    if (entry.parentUnknown && entry.kind !== "host") {
      used.add("mark-parent-unknown");
      lines[2] = "parent link not reported";
    }
    const text = lines.filter(Boolean).map((line, index) => `<text class="topo-node-${index ? "fact" : "title"}" x="${entry.x + 10}" y="${entry.y + 20 + index * 16}">${escapeHtml(line)}</text>`).join("");
    const unknown = entry.parentUnknown && entry.kind !== "host" ? " mark-parent-unknown" : "";
    return `<g class="topo-node kind-${token(entry.kind)}${unknown}${highlight}" role="button" tabindex="0" data-fabric-node="${escapeHtml(entry.id)}" data-topo-node="${escapeHtml(entry.id)}" aria-label="${escapeHtml(title)}"><title>${escapeHtml(title)}</title><rect x="${entry.x}" y="${entry.y}" width="${entry.w}" height="${entry.h}" rx="8"></rect>${text}</g>`;
  }

  function bayTitle(bay) {
    const occupancy = { yes: "disk present", no: "empty", unknown: "occupancy not read" }[bay.occupancy];
    const paths = bay.pathStates.length
      ? `paths: ${bay.pathStates.map((state) => `${state.controller || "path"} ${state.state}`).join(", ")}`
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
    const highlight = onPathClass(path, "bays", bay.bayId);
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
    const shownLabel = bay.locationKnown ? (bay.slot === null ? "" : String(bay.slot)) : bay.label;
    const number = shownLabel ? `<text class="topo-bay-number" x="${bay.x + bay.w / 2}" y="${bay.y + 14}" text-anchor="middle">${escapeHtml(shownLabel)}</text>` : "";
    return `<g class="topo-bay bay-${bay.occupancy} state-${token(bay.status)}${bay.locationKnown ? "" : " is-logical"}${highlight}" role="button" tabindex="0" ${hook} data-topo-bay="${escapeHtml(bay.bayId)}" aria-label="${escapeHtml(title)}"><title>${escapeHtml(title)}</title><rect x="${bay.x}" y="${bay.y}" width="${bay.w}" height="${CELL_BOX}" rx="3"></rect>${number}${marks}${count}</g>`;
  }

  function pathSummary(summary) {
    return Object.entries(summary.pathStates)
      .sort(([left], [right]) => naturalCompare(left, right))
      .map(([name, states]) => `${name}: ${Object.entries(states).map(([state, count]) => `${count} ${state}`).join(", ")}`)
      .join(" · ");
  }

  function groupHeading(group) {
    if (!group.locationKnown) {
      return `Disks, ${LOCATION_UNKNOWN} · ${plural(group.bayIds.length, "disk")}`;
    }
    const count = plural(group.bayIds.length, "bay");
    if (!group.enclosureIds.length) return `No enclosure link · ${count}`;
    const slots = group.bays.map((bay) => bay.slot).filter((slot) => slot !== null);
    const range = slots.length ? `Bays ${Math.min(...slots)}–${Math.max(...slots)}` : "Bays";
    return `${range} · ${count}`;
  }

  function renderGroup(model, group, path, used) {
    const highlight = path ? (path.groups.has(group.id) ? " is-on-path" : " is-dim") : "";
    if (!group.enclosureIds.length) used.add("mark-not-read");
    const head = clip(groupHeading(group), group.w);
    const parts = [`<rect class="topo-group-box" x="${group.x}" y="${group.y}" width="${group.w}" height="${group.h}" rx="8"></rect>`,
      `<text class="topo-node-title" x="${group.x + 10}" y="${group.y + 18}">${escapeHtml(head)}</text>`];
    const summaryText = pathSummary(group.summary);
    if (group.collapsed) {
      const counts = `${group.summary.withDisk} with a disk · ${group.summary.empty} empty · ${group.summary.unknown} not read`;
      parts.push(`<text class="topo-node-fact" x="${group.x + 10}" y="${group.y + 36}">${escapeHtml(clip(counts, group.w))}</text>`);
      parts.push(`<text class="topo-node-fact" x="${group.x + 10}" y="${group.y + 52}">${escapeHtml(clip(summaryText || "no path state in payload", group.w))}</text>`);
    } else if (summaryText) {
      parts.push(`<text class="topo-node-fact" x="${group.x + 10}" y="${group.y + 34}">${escapeHtml(clip(summaryText, group.w))}</text>`);
    }
    const cells = group.cells.map((bay) => renderBayCell(bay, path, used)).join("");
    let toggle = "";
    if (group.expandable) {
      const label = group.collapsed ? `Draw all ${group.bayIds.length} ${group.locationKnown ? "bays" : "disks"}` : "Collapse to a summary";
      const y = group.collapsed ? group.y + group.h - 30 : group.y + 4;
      const x = group.collapsed ? group.x + 10 : group.x + group.w - 170;
      toggle = `<g class="topo-expand" role="button" tabindex="0" data-fabric-expand-slots="${escapeHtml(group.expandKey)}" aria-expanded="${group.collapsed ? "false" : "true"}" aria-label="${escapeHtml(label)}"><rect x="${x}" y="${y}" width="160" height="22" rx="6"></rect><text class="topo-label-text" x="${x + 80}" y="${y + 15}" text-anchor="middle">${escapeHtml(label)}</text></g>`;
    }
    const name = `${groupHeading(group)}${group.collapsed ? `, ${group.summary.withDisk} with a disk, ${summaryText}` : ""}`;
    return `<g class="topo-group${group.collapsed ? " is-collapsed" : ""}${highlight}" data-topo-group="${escapeHtml(group.id)}" aria-label="${escapeHtml(name)}" role="group">${parts.join("")}${cells}${toggle}</g>`;
  }

  function renderTier(tier, model) {
    const y = tier.y ?? 0;
    const missing = tier.state === "not_read" || tier.state === "not_collected" || tier.state === "none";
    const tone = tier.state === "drawn" ? "ok" : token(tier.state);
    const label = `<g class="topo-tier"><title>${escapeHtml(`${tier.label}: ${tier.note}`)}</title>`
      + `<text class="topo-tier-label" x="${MARGIN}" y="${y + 18}">${escapeHtml(clip(tier.label, LEFT - MARGIN))}</text>`
      + `<text class="topo-tier-note tone-${tone}" x="${MARGIN}" y="${y + 36}">${escapeHtml(clip(tier.note, LEFT - MARGIN))}</text></g>`;
    let band = "";
    if (missing) {
      const width = Math.max(model.width - LEFT - MARGIN, NODE_W);
      band = `<g class="topo-missing-tier mark-not-read tone-${tone}"><rect x="${LEFT}" y="${y}" width="${width}" height="${tier.h || 36}" rx="8"></rect>`
        + `<text class="topo-node-fact" x="${LEFT + 12}" y="${y + 22}">${escapeHtml(tier.note)}</text></g>`;
    }
    return label + band;
  }

  // view: { pin: ref, hover: ref, query }. Hover wins while it lasts; a pin is
  // newer than the search it was made after, and typing a search clears it.
  function activePath(model, view = {}) {
    if (view.hover) return tracePath(model, view.hover);
    if (view.pin) return tracePath(model, view.pin);
    if (view.query) {
      const found = searchTopology(model, view.query);
      return found.matches.length ? found.path : null;
    }
    return null;
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
    return entry ? [entry.label, ...entry.facts].join(", ") : "";
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
    const used = new Set();
    if (path) used.add("is-on-path");
    const lines = [];
    const labels = [];
    for (const link of model.links) {
      const rendered = renderLink(model, link, path, used);
      lines.push(rendered.line);
      labels.push(rendered.label);
    }
    const tiers = model.tiers.map((tier) => renderTier(tier, model)).join("");
    if (model.tiers.some((tier) => tier.state === "not_read" || tier.state === "not_collected")) used.add("mark-not-read");
    const nodes = model.nodes.map((entry) => renderNode(entry, path, used)).join("");
    const groups = model.groups.map((group) => renderGroup(model, group, path, used)).join("");
    const svgLabel = `Storage Fabric topology: ${model.tiers.map((tier) => `${tier.label} ${tier.note}`).join("; ")}`;
    const svg = `<svg class="fabric-topology-svg" role="group" aria-label="${escapeHtml(svgLabel)}" width="${Math.ceil(model.width)}" height="${Math.ceil(model.height)}" viewBox="0 0 ${Math.ceil(model.width)} ${Math.ceil(model.height)}">`
      + `<g class="topo-tiers">${tiers}</g><g class="topo-lines">${lines.join("")}</g><g class="topo-nodes">${nodes}${groups}</g><g class="topo-labels">${labels.join("")}</g></svg>`;
    const parts = model.parts.map((part) => `<span class="fabric-topology-part tone-${token(part.state)}"${part.title ? ` title="${escapeHtml(part.title)}"` : ""}>${escapeHtml(part.label)}: ${escapeHtml(part.state)}</span>`).join("");
    const legend = Object.entries(LEGEND)
      .filter(([style]) => used.has(style))
      .map(([style, [shape, text]]) => `<span class="fabric-topology-legend-item" data-topology-legend="${style}">${legendSwatch(style, shape)}${escapeHtml(text)}</span>`)
      .join("");
    const notices = model.notices.map((text) => `<div class="warning-item muted compact">${escapeHtml(text)}</div>`).join("");
    const pinned = view.pin ? `Pinned: ${refLabel(model, view.pin)}. Esc clears.` : "Hover or focus anything to trace its path; click or press Enter to pin it; Esc clears.";
    return `<div class="fabric-topology" data-topology-kind="${escapeHtml(model.kind)}">`
      + `<div class="fabric-topology-parts" aria-label="Part status">${parts}</div>`
      + `<div class="fabric-topology-legend" aria-label="Legend">${legend}</div>`
      + notices
      + `<p class="fabric-topology-pin subtle" data-topology-pin>${escapeHtml(pinned)}</p>`
      + `<div class="fabric-topology-scroll">${svg}</div>`
      + `<p class="fabric-topology-not-shown subtle">${escapeHtml(NOT_SHOWN)}</p>`
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
      const names = carried.length <= 48
        ? carried.map((bay) => bay.label).join(", ")
        : `${carried.length} ${unit}s, slots ${phyRanges(carried.map((bay) => bay.slot).filter((slot) => slot !== null).sort((a, b) => a - b))}`;
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

  const live = { model: null, pin: null, hover: null, query: "", scope: null, container: null, input: null, status: null, hooks: {} };

  function refExists(model, ref) {
    if (!model || !ref) return false;
    if (ref.kind === "link") return model.linkById.has(ref.id);
    if (ref.kind === "bay") return model.bayById.has(ref.id);
    if (ref.kind === "group") return model.groupById.has(ref.id);
    return model.nodeById.has(ref.id);
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

  function render(fabric, options = {}) {
    const model = buildTopologyModel(fabric, options);
    if (live.scope !== null && model.scope !== live.scope) {
      live.pin = null;
      clearInput();
    }
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
    return renderTopology(model, { pin: live.pin, query: live.query });
  }

  // Leaving the mode: no pin, hover or search carries over.
  function release() {
    live.pin = null;
    live.hover = null;
    live.scope = null;
    clearInput();
  }

  function view() {
    return { pin: live.pin, hover: live.hover, query: live.query };
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
    mark("[data-topo-bay]", "bays", "data-topo-bay");
    mark("[data-topo-group]", "groups", "data-topo-group");
    const pin = root.querySelector("[data-topology-pin]");
    if (pin) {
      pin.textContent = live.pin
        ? `Pinned: ${refLabel(live.model, live.pin)}. Esc clears.`
        : "Hover or focus anything to trace its path; click or press Enter to pin it; Esc clears.";
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

  // hooks: { clearSelection(), requestRender() } from the page, so a link pin,
  // a typed search and Esc keep the page's inspector selection in step.
  function attach(container, searchInput, searchStatus, hooks = {}) {
    if (!container || container.dataset?.topologyAttached) return;
    if (container.dataset) container.dataset.topologyAttached = "true";
    Object.assign(live, { container, input: searchInput || null, status: searchStatus || null, hooks: hooks || {} });
    const hover = (event) => {
      const ref = refFromElement(event.target);
      if (!ref) return;
      live.hover = ref;
      applyHighlight();
    };
    const leave = (event) => {
      if (!live.hover) return;
      live.hover = refFromElement(event.relatedTarget);
      applyHighlight();
    };
    container.addEventListener("mouseover", hover);
    container.addEventListener("focusin", hover);
    container.addEventListener("mouseout", leave);
    container.addEventListener("focusout", leave);
    container.addEventListener("click", (event) => {
      const element = event.target?.closest?.("[data-topo-link], [data-topo-bay], [data-topo-node]");
      const ref = refFromElement(element);
      if (!ref) return;
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
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !container.querySelector(".fabric-topology")) return;
      if (event.target?.closest?.("[data-fabric-alias-form]")) return;
      live.pin = null;
      live.hover = null;
      clearInput();
      applyHighlight();
      // Esc clears the drawing's pin and the inspector's selection together.
      live.hooks.clearSelection?.();
      live.hooks.requestRender?.();
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
    linkDetail,
    render,
    release,
    view,
    inspectorDetail,
    attach,
  };
})();
