// Storage Fabric "Topology" mode (#937): a wiring diagram of the SAS fabric
// from the host down to the bays, drawn only from the /api/sas-fabric payload
// the page already has. Two parts:
//
//   - a pure layout model (buildTopologyModel, tracePath, searchTopology,
//     renderTopology): payload in, tiers, nodes, link routes, labels and an
//     SVG string out, no DOM. tests/js/fabric_topology_model.test.js loads it
//     in Node the same way the other fabric tests load sas_fabric_view.js;
//   - a small DOM layer (render, attach) that sas_fabric_view.js calls to draw
//     the mode and to trace a path on hover, focus, click, search and Esc.
//
// Rules: one drawn line per payload link between drawn nodes; links into bays
// are drawn as one bundle per enclosure and bay group, labelled with the count
// of payload links it carries. No node, link, lane count, PHY number or rate is
// ever invented. A tier the payload does not carry says "not read" or "not
// collected", never an empty tier that looks healthy. A large bay group is
// collapsed into a summary block that expands; no bay is ever dropped.
(function () {
  "use strict";

  const NOT_COLLECTED = "HBA and expander hops are not collected on this platform.";
  const NOT_SHOWN = "Pools and vdevs are not drawn here; Impact Map and Disk Path show them. "
    + "Backplane zones, the CORE multipath groupings (shown instead as path marks on each bay) "
    + "and PHY error counters (Disk Path) are not drawn either.";
  // A bay group larger than this is drawn as a summary block until expanded.
  const BAY_DETAIL_LIMIT = 96;
  const CELLS_PER_ROW = 24;
  const CELL = 32;
  const NODE_W = 184;
  const NODE_H = 62;
  const H_GAP = 28;
  const LEFT = 168;
  const MARGIN = 16;
  const CHAR_W = 7;
  const SUMMARY_W = 300;
  const SUMMARY_H = 104;
  const GROUP_HEAD = 42;
  const LANE_LINKS = new Set(["controller-expander", "expander-expander"]);
  const HIDDEN_KINDS = new Set(["pool", "vdev", "backplane"]);
  const PATH_STATE_TONES = {
    active: "active",
    online: "active",
    optimal: "active",
    passive: "standby",
    standby: "standby",
    mapped: "mapped",
    failed: "fault",
    fault: "fault",
    degraded: "fault",
    offline: "fault",
  };

  function list(value) {
    return Array.isArray(value) ? value : [];
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

  function naturalCompare(left, right) {
    return String(left ?? "").localeCompare(String(right ?? ""), undefined, { numeric: true, sensitivity: "base" });
  }

  function fabricKind(fabric) {
    if (fabric?.raw?.fabric_kind) {
      return fabric.raw.fabric_kind;
    }
    return fabric?.platform === "core" ? "core_sas" : "unknown";
  }

  function readState(fabric, domain) {
    const counts = fabric?.raw?.controller_read?.counts;
    if (!counts || typeof counts !== "object") {
      return "complete";
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

  // ---- tiers and nodes -----------------------------------------------------

  function tierForNode(item, core) {
    const kind = String(item.kind || "");
    if (kind === "host") return "host";
    if (kind === "controller") return "controllers";
    if (kind === "expander") return core ? "expanders" : null;
    if (kind === "path") return core ? null : "devices";
    if (kind === "bay") return "bays";
    if (HIDDEN_KINDS.has(kind)) return null;
    if (kind.endsWith("enclosure")) return "enclosures";
    return null;
  }

  function expanderLevel(item) {
    const level = finite(item?.metrics?.sas_level) ?? finite(item?.raw?.sas_level);
    return level === null ? null : level;
  }

  function countTier(fabric, domain, count, base) {
    const state = readState(fabric, domain);
    if (count > 0) {
      if (state === "partial") {
        return { state: "partial", note: `at least ${count}; some controller reads are missing` };
      }
      return { state: "drawn", note: `${count} ${count === 1 ? base.one : base.many}` };
    }
    if (state === "complete") {
      return { state: "none", note: base.none };
    }
    return { state: "not_read", note: `${base.many} not read` };
  }

  function nodeFacts(item, core) {
    const metrics = item.metrics || {};
    const notRead = new Set(list(metrics.not_read));
    const facts = [];
    if (item.kind === "controller") {
      if (core) {
        const board = item.raw?.board;
        if (board) facts.push(String(board));
        if (notRead.has("phy_count") || notRead.has("linked_phy_count")) {
          facts.push("PHY table not read");
        } else if (finite(metrics.phy_count) !== null) {
          const linked = finite(metrics.linked_phy_count);
          facts.push(`${linked === null ? "?" : linked} of ${metrics.phy_count} PHYs linked`);
        }
      } else {
        facts.push("storage source, not an HBA");
      }
    } else if (item.kind === "expander") {
      const level = expanderLevel(item);
      const parts = [level === null ? "level not reported" : `level ${level}`];
      if (notRead.has("linked_phys")) {
        parts.push("PHY table not read");
      } else if (finite(metrics.num_phys) !== null) {
        parts.push(`${finite(metrics.linked_phys) ?? "?"} of ${metrics.num_phys} PHYs linked`);
      }
      facts.push(parts.join(" · "));
    } else if (item.kind === "host") {
      facts.push("host");
    } else if (item.kind === "path") {
      facts.push(item.status ? `SES device · ${item.status}` : "SES device");
    } else {
      facts.push(String(item.kind || "").replace(/-/g, " "));
    }
    return facts;
  }

  // ---- model ---------------------------------------------------------------

  function buildTopologyModel(fabric, options = {}) {
    const expanded = options.expanded || {};
    const kind = fabricKind(fabric);
    const core = kind === "core_sas";
    const payloadNodes = list(fabric?.nodes);
    const payloadLinks = list(fabric?.links);
    const traces = new Map(list(fabric?.traces).map((trace) => [trace.id, trace]));

    const nodes = [];
    const nodeById = new Map();
    const bayNodes = [];
    for (const item of payloadNodes) {
      const tier = tierForNode(item, core);
      if (!tier || nodeById.has(item.id)) continue;
      if (tier === "bays") {
        bayNodes.push(item);
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
        facts: nodeFacts(item, core),
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

    // Lines: every payload link whose both ends are drawn nodes.
    const links = [];
    const linkById = new Map();
    const bayLinks = [];
    const bayIdSet = new Set(bayNodes.map((item) => item.id));
    for (const item of payloadLinks) {
      const source = nodeById.get(item.source);
      if (!source) continue;
      if (bayIdSet.has(item.target)) {
        if (source.tier === "enclosures" || (!core && source.tier === "devices")) {
          bayLinks.push(item);
        }
        continue;
      }
      if (!nodeById.has(item.target) || linkById.has(item.id) || item.source === item.target) continue;
      const lanesPayload = item.reported_lanes && typeof item.reported_lanes === "object" ? item.reported_lanes : null;
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
    for (const item of payloadLinks) {
      if (item.kind !== "path-bay") continue;
      const controller = String(item.source || "").split(":")[1] || "";
      if (!pathStatesByBay.has(item.target)) pathStatesByBay.set(item.target, []);
      pathStatesByBay.get(item.target).push({ controller, state: String(item.status || "unknown") });
    }
    const groupsByKey = new Map();
    const groupByBay = new Map();
    const bays = [];
    const seenBays = new Set();
    for (const item of bayNodes) {
      if (seenBays.has(item.id)) continue;
      seenBays.add(item.id);
      const trace = traces.get(item.id);
      const traced = list(trace?.metrics?.path_states).map((state) => ({
        controller: String(state?.controller ?? ""),
        state: String(state?.state ?? "unknown"),
      }));
      const pathStates = traced.length ? traced : list(pathStatesByBay.get(item.id));
      const present = item.metrics?.present;
      const bay = {
        bayId: item.id,
        traceId: trace ? trace.id : "",
        slot: Number.isInteger(item.slot) ? item.slot : null,
        label: nodeLabel(item),
        status: item.status || "",
        present: present === true ? "yes" : present === false ? "no" : "unknown",
        deviceName: item.metrics?.device_name || trace?.metrics?.device_name || "",
        serial: item.metrics?.serial || item.raw?.serial || trace?.metrics?.serial || "",
        sasAddress: item.metrics?.sas_address || item.raw?.sas_address || "",
        pathStates,
        sources: [...(baySources.get(item.id) || [])].sort(naturalCompare),
        x: 0,
        y: 0,
      };
      bays.push(bay);
      const key = bay.sources.join("|");
      if (!groupsByKey.has(key)) {
        groupsByKey.set(key, { id: `group:${key || "unlinked"}`, enclosureIds: bay.sources, bays: [] });
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
        if (bay.present === "yes") summary.withDisk += 1;
        else if (bay.present === "no") summary.empty += 1;
        else summary.unknown += 1;
        for (const state of bay.pathStates) {
          const name = state.controller || "path";
          summary.pathStates[name] = summary.pathStates[name] || {};
          summary.pathStates[name][state.state] = (summary.pathStates[name][state.state] || 0) + 1;
        }
      }
      const group = {
        id: raw.id,
        tier: "bays",
        enclosureIds: raw.enclosureIds,
        bayIds: raw.bays.map((bay) => bay.bayId),
        bays: raw.bays,
        cells: collapsed ? [] : raw.bays,
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
          labelLines: [`${memberLinkIds.length} ${memberLinkIds.length === 1 ? "bay" : "bays"}`],
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
    tiers.push({ id: "host", label: "Host", state: "drawn", note: "", nodeIds: tierNodes("host").map((entry) => entry.id) });
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
            note: expanderStatus.state === "partial" ? expanderStatus.note : `${members.length} ${members.length === 1 ? "expander" : "expanders"}`,
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
        note: controllers.length ? `${controllers.length} ${controllers.length === 1 ? "source" : "sources"}` : "no source reported",
        nodeIds: controllers.map((entry) => entry.id),
      });
      tiers.push({ id: "not-collected", label: "HBAs and expanders", state: "not_collected", note: NOT_COLLECTED, nodeIds: [] });
      const devices = tierNodes("devices");
      if (devices.length) {
        tiers.push({
          id: "devices",
          label: kind === "linux_ses" ? "SES devices" : "Storage paths",
          state: "drawn",
          note: `${devices.length} ${devices.length === 1 ? "device" : "devices"}`,
          nodeIds: devices.map((entry) => entry.id),
        });
      }
      const enclosures = tierNodes("enclosures");
      tiers.push({
        id: "enclosures",
        label: "Enclosures",
        state: enclosures.length ? "drawn" : "none",
        note: enclosures.length ? `${enclosures.length} ${enclosures.length === 1 ? "enclosure" : "enclosures"}` : "no enclosure reported",
        nodeIds: enclosures.map((entry) => entry.id),
      });
    }
    tiers.push({
      id: "bays",
      label: "Bays",
      state: bays.length ? "drawn" : "none",
      note: bays.length ? `${bays.length} ${bays.length === 1 ? "bay" : "bays"}` : "no bays in this payload",
      nodeIds: groups.map((group) => group.id),
    });

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
      counts: { bayNodes: seenBays.size, baysPlaced: groups.reduce((total, group) => total + group.bayIds.length, 0) },
      width: 0,
      height: 0,
    };
    for (const group of groups) {
      if (group.collapsed) {
        const names = group.enclosureIds.map((id) => nodeById.get(id)?.label || id).join(" and ");
        model.notices.push(`${group.bayIds.length} bays${names ? ` behind ${names}` : ""} are shown as a summary block; expand it to draw each bay.`);
      }
    }
    if (model.counts.baysPlaced !== model.counts.bayNodes) {
      model.notices.push(`${model.counts.bayNodes - model.counts.baysPlaced} bays in the payload could not be placed.`);
    }
    layoutModel(model);
    return model;
  }

  function modelParts(fabric, core) {
    const parts = [];
    for (const [name, source] of Object.entries(fabric?.sources || {})) {
      let state = "ok";
      if (!source?.enabled) state = "disabled";
      else if (!source.ok) state = source.required_ok ? "partial" : "failed";
      parts.push({ name, label: name, state, title: source?.message || "" });
    }
    const caches = [fabric?.snapshot_cache_state, fabric?.source_cache_state];
    let cache = "ok";
    if (caches.includes("trusted-fallback")) cache = "fallback";
    else if (caches.includes("stale-hit")) cache = "stale";
    parts.push({ name: "cache", label: "cache", state: cache, title: caches.filter(Boolean).join(", ") });
    if (core) {
      for (const [domain, label] of [["controllers", "HBAs"], ["expanders", "expanders"], ["enclosures", "enclosures"]]) {
        const state = readState(fabric, domain);
        parts.push({ name: domain, label, state: state === "complete" ? "ok" : state === "partial" ? "partial" : "not read", title: "" });
      }
      if (Number(fabric?.raw?.controller_read?.unread_phy_tables) > 0) {
        parts.push({ name: "phy-tables", label: "PHY tables", state: "partial", title: "" });
      }
    }
    return parts;
  }

  // ---- layout --------------------------------------------------------------

  function groupSize(group) {
    if (group.collapsed) {
      return [SUMMARY_W, SUMMARY_H];
    }
    const count = group.cells.length;
    const columns = Math.max(1, Math.min(CELLS_PER_ROW, count));
    const rows = Math.max(1, Math.ceil(count / CELLS_PER_ROW));
    return [Math.max(NODE_W, columns * CELL + 16), GROUP_HEAD + rows * CELL + 10];
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
    const rowTop = new Map();
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
      rowTop.set(row.tier.id, y);
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
        bay.x = group.x + 8 + (index % CELLS_PER_ROW) * CELL;
        bay.y = group.y + GROUP_HEAD + Math.floor(index / CELLS_PER_ROW) * CELL;
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

  // ---- path trace and search -----------------------------------------------

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

  function walkDown(model, id, path, onlyBays = null) {
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
            if (bay.sources.includes(link.source) && (!onlyBays || onlyBays.has(bay.bayId))) path.bays.add(bay.bayId);
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

  function searchTopology(model, query) {
    const text = String(query ?? "").trim().toLowerCase();
    const result = { query: text, matches: [], path: emptyPath() };
    if (!model || !text) return result;
    const bayNumber = /^(?:bay\s*)?(\d+)$/.exec(text);
    for (const bay of model.bays) {
      const group = model.groupByBay.get(bay.bayId);
      let hit = false;
      if (bayNumber) {
        hit = bay.slot !== null && bay.slot === Number(bayNumber[1]);
      } else {
        hit = [bay.label, bay.deviceName, bay.serial, bay.sasAddress].some((value) => value && String(value).toLowerCase().includes(text));
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

  // ---- SVG rendering -------------------------------------------------------

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
    "path-active": ["dot", "path active"],
    "path-standby": ["dot", "path passive or standby"],
    "path-fault": ["dot", "path failed or degraded"],
    "path-mapped": ["dot", "mapped by SES (no path state)"],
    "path-other": ["dot", "path state not recognised"],
  };

  function pathTone(state) {
    return PATH_STATE_TONES[String(state || "").toLowerCase()] || "other";
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
    const target = model.nodeById.get(link.target)?.label || (link.bundle ? "bay group" : link.target);
    return `Link ${source} to ${target}`;
  }

  function linkDescription(model, link) {
    const parts = [linkName(model, link)];
    if (link.lanesState === "reported") {
      parts.push(`${link.lanes.count} ${link.lanes.count === 1 ? "lane" : "lanes"}`, formatRate(link.lanes.rate));
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
    const targetAttr = model.nodeById.has(link.target) ? ` data-fabric-node="${escapeHtml(link.target)}"` : "";
    let body;
    if (link.labelLines.length) {
      body = `<rect class="topo-label-box" x="${Math.round(box.x)}" y="${Math.round(box.y)}" width="${Math.round(box.w)}" height="${Math.round(box.h)}" rx="7"></rect>`
        + link.labelLines.map((text, index) => `<text class="topo-label-text" x="${Math.round(box.x + box.w / 2)}" y="${Math.round(box.y + 14 + index * 15)}" text-anchor="middle">${escapeHtml(text)}</text>`).join("");
    } else {
      body = `<circle class="topo-junction" cx="${Math.round(box.x + box.w / 2)}" cy="${Math.round(box.y + box.h / 2)}" r="6"></circle>`;
    }
    const label = `<g class="topo-link-label ${style}${highlight}" role="button" tabindex="0" data-fabric-topology-link="${escapeHtml(link.id)}"${targetAttr} data-topo-link="${escapeHtml(link.id)}" aria-label="${escapeHtml(name)}"><title>${escapeHtml(name)}</title>${body}</g>`;
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
    const occupancy = bay.present === "yes" ? "disk present" : bay.present === "no" ? "empty" : "occupancy not read";
    const paths = bay.pathStates.length
      ? `paths: ${bay.pathStates.map((state) => `${state.controller || "path"} ${state.state}`).join(", ")}`
      : "no path state in payload";
    return [bay.label, occupancy, bay.deviceName, paths].filter(Boolean).join(", ");
  }

  function renderBayCell(bay, path, used) {
    const highlight = onPathClass(path, "bays", bay.bayId);
    used.add(`bay-${bay.present}`);
    const hook = bay.traceId ? `data-fabric-trace="${escapeHtml(bay.traceId)}"` : `data-fabric-node="${escapeHtml(bay.bayId)}"`;
    const dots = bay.pathStates.slice(0, 4).map((state, index) => {
      const tone = pathTone(state.state);
      used.add(`path-${tone}`);
      return `<circle class="topo-path-dot path-${tone}" cx="${bay.x + 6 + index * 6}" cy="${bay.y + CELL - 8}" r="2.4"></circle>`;
    }).join("");
    const title = bayTitle(bay);
    const number = bay.slot === null ? "" : `<text class="topo-bay-number" x="${bay.x + (CELL - 2) / 2}" y="${bay.y + 13}" text-anchor="middle">${escapeHtml(bay.slot)}</text>`;
    return `<g class="topo-bay bay-${bay.present} state-${token(bay.status)}${highlight}" role="button" tabindex="0" ${hook} data-topo-bay="${escapeHtml(bay.bayId)}" aria-label="${escapeHtml(title)}"><title>${escapeHtml(title)}</title><rect x="${bay.x}" y="${bay.y}" width="${CELL - 2}" height="${CELL - 2}" rx="3"></rect>${number}${dots}</g>`;
  }

  function pathSummary(summary) {
    return Object.entries(summary.pathStates)
      .sort(([left], [right]) => naturalCompare(left, right))
      .map(([name, states]) => `${name}: ${Object.entries(states).map(([state, count]) => `${count} ${state}`).join(", ")}`)
      .join(" · ");
  }

  function renderGroup(model, group, path, used) {
    const highlight = path ? (path.groups.has(group.id) ? " is-on-path" : " is-dim") : "";
    const slots = group.bays.map((bay) => bay.slot).filter((slot) => slot !== null);
    const range = slots.length ? `Bays ${Math.min(...slots)}–${Math.max(...slots)}` : "Bays";
    const linked = group.enclosureIds.length ? "" : " · no enclosure link in payload";
    if (!group.enclosureIds.length) used.add("mark-not-read");
    const head = clip(`${range} · ${group.bayIds.length} ${group.bayIds.length === 1 ? "bay" : "bays"}${linked}`, group.w);
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
      const label = group.collapsed ? `Draw all ${group.bayIds.length} bays` : "Collapse to a summary";
      const y = group.collapsed ? group.y + group.h - 30 : group.y + 4;
      const x = group.collapsed ? group.x + 10 : group.x + group.w - 170;
      toggle = `<g class="topo-expand" role="button" tabindex="0" data-fabric-expand-slots="${escapeHtml(group.expandKey)}" aria-expanded="${group.collapsed ? "false" : "true"}" aria-label="${escapeHtml(label)}"><rect x="${x}" y="${y}" width="160" height="22" rx="6"></rect><text class="topo-label-text" x="${x + 80}" y="${y + 15}" text-anchor="middle">${escapeHtml(label)}</text></g>`;
    }
    const name = `${head}${group.collapsed ? `, ${group.summary.withDisk} with a disk, ${summaryText}` : ""}`;
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

  // view: { pin: ref, hover: ref, query }
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
      .map(([style, [shape, text]]) => `<span class="fabric-topology-legend-item" data-topology-legend="${style}"><span class="fabric-topology-swatch swatch-${shape} ${style}" aria-hidden="true"></span>${escapeHtml(text)}</span>`)
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

  // ---- DOM layer (browser only) --------------------------------------------

  const live = { model: null, pin: null, hover: null, query: "" };

  function refExists(model, ref) {
    if (!model || !ref) return false;
    if (ref.kind === "link") return model.linkById.has(ref.id);
    if (ref.kind === "bay") return model.bayById.has(ref.id);
    if (ref.kind === "group") return model.groupById.has(ref.id);
    return model.nodeById.has(ref.id);
  }

  function render(fabric, options = {}) {
    live.model = buildTopologyModel(fabric, options);
    if (!refExists(live.model, live.pin)) live.pin = null;
    live.hover = null;
    return renderTopology(live.model, { pin: live.pin, query: live.query });
  }

  function refFromElement(element) {
    const target = element?.closest?.("[data-topo-link], [data-topo-bay], [data-topo-node], [data-topo-group]");
    if (!target || !target.closest(".fabric-topology")) return null;
    if (target.dataset.topoLink) return { kind: "link", id: target.dataset.topoLink };
    if (target.dataset.topoBay) return { kind: "bay", id: target.dataset.topoBay };
    if (target.dataset.topoNode) return { kind: "node", id: target.dataset.topoNode };
    return { kind: "group", id: target.dataset.topoGroup };
  }

  function applyHighlight(container) {
    const root = container?.querySelector?.(".fabric-topology");
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

  function updateSearchStatus(status) {
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

  function attach(container, searchInput, searchStatus) {
    if (!container || container.dataset?.topologyAttached) return;
    if (container.dataset) container.dataset.topologyAttached = "true";
    const hover = (event) => {
      const ref = refFromElement(event.target);
      if (!ref) return;
      live.hover = ref;
      applyHighlight(container);
    };
    const leave = (event) => {
      if (!live.hover) return;
      const next = refFromElement(event.relatedTarget);
      live.hover = next;
      applyHighlight(container);
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
      applyHighlight(container);
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
      live.query = "";
      if (searchInput) searchInput.value = "";
      updateSearchStatus(searchStatus);
      applyHighlight(container);
    });
    if (searchInput) {
      searchInput.addEventListener("input", () => {
        live.query = searchInput.value.trim();
        updateSearchStatus(searchStatus);
        applyHighlight(container);
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
    render,
    attach,
  };
})();
