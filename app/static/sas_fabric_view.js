(function () {
  const bootstrap = window.SAS_FABRIC_BOOTSTRAP || {};
  const modeIds = new Set(["lanes", "impact", "trace", "disk"]);
  const modeCopy = {
    lanes: {
      title: "Fabric Lanes",
      subtitle: "Top-down controller lanes with paths, expanders, enclosures, and impacted bays aligned in each lane.",
    },
    impact: {
      title: "Impact Map",
      subtitle: "Start from paths and degraded states, then show affected slots, pools, vdevs, and trace hops.",
    },
    trace: {
      title: "Physical Trace",
      subtitle: "Follow the selected component or bay through the host, HBA, path, expander, enclosure, and disk layers.",
    },
    disk: {
      title: "Disk Path",
      subtitle: "Pick a bay and render each controller path from host to HBA, SAS link, expander, backplane zone, and disk.",
    },
  };

  const initialMode = new URLSearchParams(window.location.search).get("mode");
  const state = {
    snapshot: bootstrap.snapshot || { systems: [], enclosures: [], slots: [] },
    fabric: bootstrap.fabric || null,
    selectedSystemId: bootstrap.systemId || bootstrap.snapshot?.selected_system_id || null,
    selectedEnclosureId: bootstrap.enclosureId || bootstrap.snapshot?.selected_enclosure_id || null,
    selectedTraceId: null,
    selectedNodeId: null,
    selectionTrail: [],
    expandedSlotLists: {},
    diagnosticTables: {},
    aliasEditObjectId: null,
    mode: modeIds.has(initialMode) ? initialMode : "lanes",
    loading: false,
    error: null,
  };

  const elements = {
    systemSelect: document.getElementById("fabric-system-select"),
    enclosureSelect: document.getElementById("fabric-enclosure-select"),
    refreshButton: document.getElementById("fabric-refresh-button"),
    apiChip: document.getElementById("fabric-api-chip"),
    statusText: document.getElementById("fabric-status-text"),
    lastUpdated: document.getElementById("fabric-last-updated"),
    backLinks: Array.from(document.querySelectorAll("[data-fabric-back-link]")),
    summaryControllers: document.getElementById("fabric-summary-controllers"),
    summaryPaths: document.getElementById("fabric-summary-paths"),
    summaryExpanders: document.getElementById("fabric-summary-expanders"),
    summaryEnclosures: document.getElementById("fabric-summary-enclosures"),
    summaryTraces: document.getElementById("fabric-summary-traces"),
    summaryLinks: document.getElementById("fabric-summary-links"),
    warningList: document.getElementById("fabric-warning-list"),
    focusStrip: document.getElementById("fabric-focus-strip"),
    mapTitle: document.getElementById("fabric-map-title"),
    mapSubtitle: document.getElementById("fabric-map-subtitle"),
    mapPanel: document.getElementById("fabric-map-panel"),
    inspectorTitle: document.getElementById("fabric-inspector-title"),
    inspectorBody: document.getElementById("fabric-inspector-body"),
    modeButtons: Array.from(document.querySelectorAll("[data-fabric-mode]")),
  };

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function classToken(value) {
    return String(value || "unknown").toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
  }

  function formatKind(kind) {
    return String(kind || "item")
      .replace(/-/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase())
      .replace("Mpr", "MPR")
      .replace("Ses", "SES");
  }

  function formatSlotLabel(slotNumber) {
    return String(slotNumber).padStart(2, "0");
  }

  function sortedSlots(slots) {
    return Array.from(new Set(list(slots)
      .map((slot) => Number(slot))
      .filter((slot) => Number.isInteger(slot))))
      .sort((left, right) => left - right);
  }

  function naturalCompareText(left, right) {
    return String(left || "").localeCompare(String(right || ""), undefined, {
      numeric: true,
      sensitivity: "base",
    });
  }

  function finiteNumber(value) {
    const numberValue = Number(value);
    return Number.isFinite(numberValue) ? numberValue : null;
  }

  function hexNumber(value) {
    const text = String(value ?? "").trim();
    if (!text) {
      return null;
    }
    const parsed = Number.parseInt(text.replace(/^0x/i, ""), 16);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function compareNullableNumbers(left, right) {
    const leftNumber = left ?? Number.MAX_SAFE_INTEGER;
    const rightNumber = right ?? Number.MAX_SAFE_INTEGER;
    return leftNumber - rightNumber;
  }

  function firstSlotValue(item) {
    const slots = sortedSlots(item?.related_slots || item?.slots);
    return slots.length ? slots[0] : null;
  }

  function compareFabricNodeFlow(left, right) {
    const leftRaw = left?.raw || {};
    const rightRaw = right?.raw || {};
    const levelCompare = compareNullableNumbers(finiteNumber(leftRaw.sas_level), finiteNumber(rightRaw.sas_level));
    if (levelCompare) {
      return levelCompare;
    }
    const handleCompare = compareNullableNumbers(
      hexNumber(leftRaw.enc_handle ?? leftRaw.sep_handle ?? leftRaw.dev_handle),
      hexNumber(rightRaw.enc_handle ?? rightRaw.sep_handle ?? rightRaw.dev_handle),
    );
    if (handleCompare) {
      return handleCompare;
    }
    const slotCompare = compareNullableNumbers(firstSlotValue(left), firstSlotValue(right));
    if (slotCompare) {
      return slotCompare;
    }
    return naturalCompareText(displayLabel(left) || left?.id, displayLabel(right) || right?.id);
  }

  function sortFabricNodesForLane(nodes) {
    return list(nodes).slice().sort(compareFabricNodeFlow);
  }

  function traceKindRank(traceItem) {
    const rank = {
      path: 0,
      bay: 1,
      controller: 2,
      expander: 3,
      "mpr-enclosure": 4,
      "ses-enclosure": 5,
      backplane: 6,
    };
    return rank[traceItem?.kind] ?? 99;
  }

  function compareFabricTraceFlow(left, right) {
    const kindCompare = traceKindRank(left) - traceKindRank(right);
    if (kindCompare !== 0) {
      return kindCompare;
    }
    const slotCompare = compareNullableNumbers(firstSlotValue(left), firstSlotValue(right));
    if (slotCompare !== 0) {
      return slotCompare;
    }
    return naturalCompareText(displayLabel(left) || left?.id, displayLabel(right) || right?.id);
  }

  function slotLayoutRows() {
    const configuredRows = list(state.snapshot?.layout_rows).length
      ? state.snapshot.layout_rows
      : state.snapshot?.selected_profile?.slot_layout;
    const normalized = list(configuredRows)
      .map((row) => list(row).map((slot) => {
        if (slot === null || slot === undefined || slot === "") {
          return null;
        }
        const slotNumber = Number(slot);
        return Number.isInteger(slotNumber) ? slotNumber : null;
      }))
      .filter((row) => row.length);
    if (normalized.length) {
      return normalized;
    }
    const rowsByIndex = new Map();
    list(state.snapshot?.slots).forEach((slot) => {
      const rowIndex = Number(slot?.row_index);
      const columnIndex = Number(slot?.column_index);
      const slotNumber = Number(slot?.slot);
      if (!Number.isInteger(rowIndex) || !Number.isInteger(columnIndex) || !Number.isInteger(slotNumber)) {
        return;
      }
      const row = rowsByIndex.get(rowIndex) || [];
      row[columnIndex] = slotNumber;
      rowsByIndex.set(rowIndex, row);
    });
    return Array.from(rowsByIndex.entries())
      .sort(([left], [right]) => left - right)
      .map(([, row]) => row.map((slot) => (Number.isInteger(slot) ? slot : null)));
  }

  function rowGroupsForLength(length) {
    const groups = list(state.snapshot?.selected_profile?.row_groups)
      .map((value) => Number(value))
      .filter((value) => Number.isInteger(value) && value > 0);
    return groups.reduce((sum, value) => sum + value, 0) === length ? groups : [length];
  }

  function splitLayoutRow(row) {
    const groups = rowGroupsForLength(row.length);
    const rowGroups = [];
    let offset = 0;
    groups.forEach((groupSize) => {
      rowGroups.push(row.slice(offset, offset + groupSize));
      offset += groupSize;
    });
    return rowGroups.filter((group) => group.length);
  }

  function formatSlots(slots, limit = 28) {
    const sorted = sortedSlots(slots);
    if (!sorted.length) {
      return "n/a";
    }
    const visible = sorted.slice(0, limit).map(formatSlotLabel).join(", ");
    return sorted.length > limit ? `${visible}, +${sorted.length - limit}` : visible;
  }

  function renderSlotList(slots, { limit = 28, expandKey = "" } = {}) {
    const sorted = sortedSlots(slots);
    if (!sorted.length) {
      return '<span class="fabric-empty-note">No mapped bays</span>';
    }
    const expanded = expandKey && state.expandedSlotLists[expandKey];
    const visible = expanded ? sorted : sorted.slice(0, limit);
    const overflow = sorted.length - visible.length;
    const labels = visible.map(formatSlotLabel).join(", ");
    const overflowMarkup = expandKey && overflow > 0
      ? ` <span class="fabric-overflow-button" role="button" tabindex="0" data-fabric-expand-slots="${escapeHtml(expandKey)}">+${overflow}</span>`
      : "";
    return `<span>${escapeHtml(labels)}</span>${overflowMarkup}`;
  }

  function formatTimestamp(value) {
    if (!value) {
      return "n/a";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    return date.toLocaleString();
  }

  function formatValue(value) {
    if (value === null || value === undefined || value === "") {
      return "n/a";
    }
    if (typeof value === "boolean") {
      return value ? "yes" : "no";
    }
    if (Array.isArray(value)) {
      const values = value.filter((entry) => entry !== null && entry !== undefined && entry !== "");
      if (!values.length) {
        return "n/a";
      }
      if (values.every((entry) => typeof entry !== "object")) {
        return values.join(", ");
      }
      return `${values.length} record${values.length === 1 ? "" : "s"}`;
    }
    if (typeof value === "object") {
      const entries = Object.entries(value)
        .filter(([key, entryValue]) => (
          !["top_findings", "primary_fault", "recent_events", "decoded_records", "event_table"].includes(key)
          && entryValue !== null
          && entryValue !== undefined
          && entryValue !== ""
        ));
      return entries.length
        ? entries.map(([key, entryValue]) => `${key}: ${formatValue(entryValue)}`).join(", ")
        : "n/a";
    }
    return String(value);
  }

  function kvRow(label, value) {
    return `
      <div class="kv-row">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(formatValue(value))}</strong>
      </div>
    `;
  }

  function metricRows(metrics, labels = {}) {
    return Object.entries(metrics || {})
      .filter(([, value]) => value !== null && value !== undefined && value !== "" && !Array.isArray(value))
      .slice(0, 10)
      .map(([key, value]) => kvRow(labels[key] || key.replace(/_/g, " "), (
        key === "kernel_diagnostics" ? diagnosticSummary(value) : value
      )))
      .join("");
  }

  function selectedParams({ force = null, includeMode = false } = {}) {
    const params = new URLSearchParams();
    if (state.selectedSystemId) {
      params.set("system_id", state.selectedSystemId);
    }
    if (state.selectedEnclosureId) {
      params.set("enclosure_id", state.selectedEnclosureId);
    }
    if (force !== null) {
      params.set("force", force ? "true" : "false");
    }
    if (includeMode && state.mode !== "lanes") {
      params.set("mode", state.mode);
    }
    return params;
  }

  function scopedUrl(path, options = {}) {
    const params = selectedParams(options);
    return params.toString() ? `${path}?${params.toString()}` : path;
  }

  function syncLocation() {
    const pageParams = selectedParams({ includeMode: true });
    window.history.replaceState({}, "", pageParams.toString() ? `/sas-fabric?${pageParams.toString()}` : "/sas-fabric");
    const backParams = selectedParams();
    const backHref = backParams.toString() ? `/?${backParams.toString()}` : "/";
    elements.backLinks.forEach((link) => {
      link.href = backHref;
    });
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.detail || `Request failed with ${response.status}`);
    }
    return payload;
  }

  function nodeMap(fabric = state.fabric) {
    return new Map(list(fabric?.nodes).map((node) => [node.id, node]));
  }

  function linkMap(fabric = state.fabric) {
    return new Map(list(fabric?.links).map((link) => [link.id, link]));
  }

  function traceMap(fabric = state.fabric) {
    return new Map(list(fabric?.traces).map((trace) => [trace.id, trace]));
  }

  function aliasMap(fabric = state.fabric) {
    return new Map(list(fabric?.aliases).map((alias) => [alias.object_id, alias]));
  }

  function aliasForObject(objectId, fabric = state.fabric) {
    return objectId ? aliasMap(fabric).get(objectId) || null : null;
  }

  function displayLabel(item) {
    return item?.display_label || item?.alias || item?.label || item?.name || item?.id || "";
  }

  function rawLabel(item) {
    return item?.label || item?.name || item?.id || "";
  }

  function nodeById(nodeId, fabric = state.fabric) {
    return nodeId ? nodeMap(fabric).get(nodeId) || null : null;
  }

  function traceById(traceId, fabric = state.fabric) {
    return traceId ? traceMap(fabric).get(traceId) || null : null;
  }

  function diagnosticScore(diagnostics) {
    if (!diagnostics || typeof diagnostics !== "object") {
      return 0;
    }
    const severityWeight = {
      critical: 600,
      error: 420,
      warning: 180,
      info: 30,
    };
    const topFindingWeight = list(diagnostics.top_findings).reduce((total, finding) => (
      total + (severityWeight[classToken(finding.severity)] || 0) + (Number(finding.count || 0) * 10)
    ), 0);
    return topFindingWeight
      + (Number(diagnostics.error_count || 0) * 100)
      + (Number(diagnostics.ioc_terminated_count || 0) * 80)
      + (Number(diagnostics.sense_count || 0) * 55)
      + (Number(diagnostics.retry_count || 0) * 25)
      + Number(diagnostics.event_count || 0);
  }

  function kernelDiagnostics(item) {
    return item?.metrics?.kernel_diagnostics || null;
  }

  function bestDiagnosticNode(fabric = state.fabric) {
    return list(fabric?.nodes)
      .map((node) => ({ node, score: diagnosticScore(kernelDiagnostics(node)) }))
      .filter((entry) => entry.score > 0)
      .sort((left, right) => {
        if (right.score !== left.score) {
          return right.score - left.score;
        }
        return String(left.node.id || "").localeCompare(String(right.node.id || ""));
      })[0]?.node || null;
  }

  function devicesFromDiagnostics(diagnostics) {
    const devices = new Set(list(diagnostics?.devices));
    list(diagnostics?.top_findings).forEach((finding) => {
      list(finding?.affected?.devices).forEach((device) => devices.add(device));
    });
    list(diagnostics?.recent_events).forEach((event) => {
      if (event?.device) {
        devices.add(event.device);
      }
    });
    return devices;
  }

  function traceTouchesDevice(trace, devices) {
    if (!trace || !devices?.size) {
      return false;
    }
    const metrics = trace.metrics || {};
    if (metrics.device_name && devices.has(metrics.device_name)) {
      return true;
    }
    return list(metrics.path_states).some((pathState) => devices.has(pathState.device_name))
      || list(metrics.mpr_devices).some((mprDevice) => devices.has(mprDevice.member_device_name));
  }

  function selectedTrace() {
    return traceById(state.selectedTraceId);
  }

  function selectedNode() {
    return nodeById(state.selectedNodeId);
  }

  function currentSelectionRef() {
    if (state.selectedTraceId) {
      return { kind: "trace", id: state.selectedTraceId };
    }
    if (state.selectedNodeId) {
      return { kind: "node", id: state.selectedNodeId };
    }
    return null;
  }

  function selectionRefEquals(left, right) {
    return Boolean(left && right && left.kind === right.kind && left.id === right.id);
  }

  function selectionRefKey(ref) {
    return ref ? `${ref.kind}:${ref.id}` : "";
  }

  function selectionRefExists(ref, fabric = state.fabric) {
    if (!ref) {
      return false;
    }
    if (ref.kind === "trace") {
      return Boolean(traceById(ref.id, fabric));
    }
    if (ref.kind === "node") {
      return Boolean(nodeById(ref.id, fabric));
    }
    return false;
  }

  function selectionRefLabel(ref, fabric = state.fabric) {
    if (!ref) {
      return "";
    }
    if (ref.kind === "trace") {
      const trace = traceById(ref.id, fabric);
      return trace ? `Trace: ${displayLabel(trace) || trace.id}` : ref.id;
    }
    const node = nodeById(ref.id, fabric);
    return node ? `${formatKind(node.kind)}: ${displayLabel(node) || node.id}` : ref.id;
  }

  function pruneSelectionTrail(fabric = state.fabric) {
    const current = currentSelectionRef();
    const seen = new Set();
    state.selectionTrail = state.selectionTrail.filter((ref) => {
      if (!selectionRefExists(ref, fabric) || selectionRefEquals(ref, current)) {
        return false;
      }
      const key = selectionRefKey(ref);
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
  }

  function pushCurrentSelectionToTrail() {
    const current = currentSelectionRef();
    if (!current || !selectionRefExists(current)) {
      return;
    }
    const last = state.selectionTrail[state.selectionTrail.length - 1];
    if (selectionRefEquals(current, last)) {
      return;
    }
    state.selectionTrail = state.selectionTrail.filter((ref) => !selectionRefEquals(ref, current));
    state.selectionTrail.push(current);
    if (state.selectionTrail.length > 6) {
      state.selectionTrail = state.selectionTrail.slice(state.selectionTrail.length - 6);
    }
  }

  function setSelectionRef(ref, { pushTrail = true } = {}) {
    if (!ref || !selectionRefExists(ref)) {
      return false;
    }
    if (selectionRefEquals(ref, currentSelectionRef())) {
      return true;
    }
    state.aliasEditObjectId = null;
    const trailIndex = state.selectionTrail.findIndex((trailRef) => selectionRefEquals(trailRef, ref));
    if (trailIndex >= 0) {
      state.selectionTrail = state.selectionTrail.slice(0, trailIndex);
    } else if (pushTrail) {
      pushCurrentSelectionToTrail();
    }
    if (ref.kind === "trace") {
      state.selectedTraceId = ref.id;
      state.selectedNodeId = null;
      return true;
    }
    if (ref.kind === "node") {
      state.selectedNodeId = ref.id;
      state.selectedTraceId = null;
      return true;
    }
    return false;
  }

  function defaultTraceId(fabric = state.fabric) {
    const traces = list(fabric?.traces);
    if (!traces.length) {
      return null;
    }
    const degradedPath = traces.find((trace) => {
      if (trace.kind !== "path") {
        return false;
      }
      const status = classToken(trace.metrics?.state || trace.status || trace.label);
      return !["active", "online", "passive", "standby"].includes(status);
    });
    if (degradedPath) {
      return degradedPath.id;
    }
    return (traces.find((trace) => trace.kind === "path") || traces[0]).id;
  }

  function resolveTraceId(traceId, fabric = state.fabric) {
    if (traceId && traceById(traceId, fabric)) {
      return traceId;
    }
    return defaultTraceId(fabric);
  }

  function defaultDiskTraceId(fabric = state.fabric) {
    const traces = list(fabric?.traces);
    const tracesById = traceMap(fabric);
    const diagnosticNode = bestDiagnosticNode(fabric);
    const diagnosticDevices = devicesFromDiagnostics(kernelDiagnostics(diagnosticNode));
    const deviceTrace = traces.find((trace) => trace.kind === "bay" && traceTouchesDevice(trace, diagnosticDevices));
    if (deviceTrace) {
      return deviceTrace.id;
    }
    const degradedTrace = traceById(defaultTraceId(fabric), fabric);
    for (const slotNumber of sortedSlots(degradedTrace?.slots)) {
      const bayTrace = tracesById.get(`bay:${slotNumber}`);
      if (bayTrace && diskTraceHasDisk(bayTrace)) {
        return bayTrace.id;
      }
    }
    return traces.find((trace) => trace.kind === "bay" && diskTraceHasDisk(trace))?.id
      || traces.find((trace) => trace.kind === "bay")?.id
      || defaultTraceId(fabric);
  }

  function defaultSelectionRef(fabric = state.fabric, mode = state.mode) {
    if (mode === "disk") {
      const diskTraceId = defaultDiskTraceId(fabric);
      return diskTraceId ? { kind: "trace", id: diskTraceId } : null;
    }
    if (mode === "impact") {
      const traceId = defaultTraceId(fabric);
      return traceId ? { kind: "trace", id: traceId } : null;
    }
    const diagnosticNode = bestDiagnosticNode(fabric);
    if (diagnosticNode) {
      return { kind: "node", id: diagnosticNode.id };
    }
    const traceId = defaultTraceId(fabric);
    return traceId ? { kind: "trace", id: traceId } : null;
  }

  function setSelectionDirect(ref) {
    if (!ref || !selectionRefExists(ref)) {
      state.selectedTraceId = null;
      state.selectedNodeId = null;
      return false;
    }
    if (ref.kind === "trace") {
      state.selectedTraceId = ref.id;
      state.selectedNodeId = null;
      return true;
    }
    if (ref.kind === "node") {
      state.selectedNodeId = ref.id;
      state.selectedTraceId = null;
      return true;
    }
    return false;
  }

  function ensureSelectionForMode(fabric = state.fabric, { force = false, mode = state.mode } = {}) {
    const current = currentSelectionRef();
    if (!force && current && selectionRefExists(current, fabric)) {
      return;
    }
    setSelectionDirect(defaultSelectionRef(fabric, mode));
  }

  function selectedSlots() {
    const trace = selectedTrace();
    if (trace) {
      return new Set(sortedSlots(trace.slots));
    }
    const node = selectedNode();
    if (node) {
      return new Set(sortedSlots(node.related_slots));
    }
    return new Set();
  }

  function slotsOverlap(left, rightSet) {
    return sortedSlots(left).some((slot) => rightSet.has(slot));
  }

  function selectionTouchesNode(nodeId) {
    const trace = selectedTrace();
    if (trace && list(trace.node_ids).includes(nodeId)) {
      return true;
    }
    const node = selectedNode();
    return Boolean(node && node.id === nodeId);
  }

  function selectionTouchesSlots(slots) {
    const slotSet = selectedSlots();
    return slotSet.size > 0 && slotsOverlap(slots, slotSet);
  }

  function slotByNumber(slotNumber) {
    return list(state.snapshot?.slots).find((slot) => Number(slot.slot) === Number(slotNumber)) || null;
  }

  function affectedSlotSummary(slots) {
    const slotRecords = sortedSlots(slots).map(slotByNumber).filter(Boolean);
    const pools = Array.from(new Set(slotRecords.map((slot) => slot.pool_name).filter(Boolean))).sort();
    const vdevs = Array.from(new Set(slotRecords.map((slot) => slot.vdev_name).filter(Boolean))).sort();
    const devices = Array.from(new Set(slotRecords.map((slot) => slot.device_name).filter(Boolean))).sort();
    return { pools, vdevs, devices };
  }

  function relatedTracesForNode(node, fabric = state.fabric) {
    if (!node) {
      return [];
    }
    return list(fabric?.traces).filter((traceItem) => (
      list(traceItem.node_ids).includes(node.id)
    )).sort(compareFabricTraceFlow);
  }

  function traceIsInSelectionTrail(traceId) {
    return state.selectionTrail.some((ref) => ref.kind === "trace" && ref.id === traceId);
  }

  function renderNodeButton(node, { label = null, meta = null, extra = "" } = {}) {
    if (!node) {
      return "";
    }
    const selected = state.selectedNodeId === node.id;
    const related = selectionTouchesNode(node.id) || selectionTouchesSlots(node.related_slots);
    return `
      <button type="button" class="fabric-node-card status-${classToken(node.status)}${selected ? " is-selected" : ""}${related ? " is-related" : ""} ${extra}" data-fabric-node="${escapeHtml(node.id)}">
        <span class="fabric-node-kind">${escapeHtml(formatKind(node.kind))}</span>
        <strong>${escapeHtml(label || displayLabel(node) || node.id)}</strong>
        <span>${escapeHtml(meta || nodeMeta(node))}</span>
      </button>
    `;
  }

  function nodeMeta(node, fallback = {}) {
    const metrics = node?.metrics || {};
    return [
      fallback.pcie_slot || metrics.pcie_slot || node?.raw?.pcie_slot,
      fallback.board || metrics.board,
      metrics.temperature ? `temp ${metrics.temperature}` : null,
      metrics.firmware ? `fw ${metrics.firmware}` : null,
      metrics.linked_phys ? `${metrics.linked_phys}/${metrics.num_phys || "?"} phys` : null,
      sortedSlots(node?.related_slots).length ? `${sortedSlots(node.related_slots).length} bays` : null,
      node?.raw_id,
    ].filter(Boolean).join(" / ") || "n/a";
  }

  function renderPathButton(path, { compact = false } = {}) {
    const trace = traceById(path.id);
    const selected = state.selectedTraceId === path.id;
    const slots = sortedSlots(path.slots || trace?.slots);
    const related = selected || selectionTouchesSlots(slots);
    const stateName = path.state || trace?.metrics?.state || "unknown";
    return `
      <button type="button" class="fabric-path-card status-${classToken(stateName)}${selected ? " is-selected" : ""}${related ? " is-related" : ""}${compact ? " compact" : ""}" data-fabric-trace="${escapeHtml(path.id)}">
        <span>${escapeHtml(path.controller || "path")}</span>
        <strong>${escapeHtml(displayLabel(path) || stateName)}</strong>
        <small>${escapeHtml(`${path.count || slots.length || 0} bay${(path.count || slots.length) === 1 ? "" : "s"}`)}</small>
        <em>${renderSlotList(slots, { limit: compact ? 16 : 28, expandKey: `path:${path.id}` })}</em>
      </button>
    `;
  }

  function renderNodeGrid(nodes, limit = 12) {
    const visible = list(nodes).slice(0, limit);
    if (!visible.length) {
      return '<span class="fabric-empty-note">No objects reported</span>';
    }
    const overflow = nodes.length > limit ? `<span class="fabric-empty-note">+${nodes.length - limit} more</span>` : "";
    return `${visible.map((node) => renderNodeButton(node, { extra: "compact" })).join("")}${overflow}`;
  }

  function renderBayChips(slots, limit = 96) {
    const sorted = sortedSlots(slots);
    if (!sorted.length) {
      return '<span class="fabric-empty-note">No mapped bays</span>';
    }
    const activeSlots = selectedSlots();
    const chips = sorted.slice(0, limit).map((slotNumber) => {
      const selected = activeSlots.has(slotNumber);
      const slot = slotByNumber(slotNumber);
      const title = [slot?.device_name, slot?.pool_name, slot?.vdev_name].filter(Boolean).join(" / ");
      return `
        <button type="button" class="fabric-bay-chip${selected ? " is-selected" : ""}" data-fabric-trace="bay:${slotNumber}" title="${escapeHtml(title || `Bay ${formatSlotLabel(slotNumber)}`)}">
          ${escapeHtml(formatSlotLabel(slotNumber))}
        </button>
      `;
    }).join("");
    const overflow = sorted.length > limit ? `<span class="fabric-empty-note">+${sorted.length - limit} bays</span>` : "";
    return `${chips}${overflow}`;
  }

  function renderLanesMode(fabric) {
    const nodes = nodeMap(fabric);
    const hostNode = nodes.get("host") || {
      id: "host",
      kind: "host",
      label: fabric.system_label || fabric.system_id || "Host",
      metrics: {},
    };
    const controllerRecords = list(fabric.controllers).length
      ? list(fabric.controllers)
      : list(fabric.nodes).filter((node) => node.kind === "controller").map((node) => ({
        id: node.id,
        name: node.label,
        display_label: displayLabel(node),
        alias: node.alias,
        related_slots: node.related_slots,
      }));
    const lanes = controllerRecords.map((controller) => {
      const controllerId = controller.id || `controller:${controller.name}`;
      const controllerNode = nodes.get(controllerId);
      const controllerName = controller.name || controllerNode?.label || controllerId.replace(/^controller:/, "");
      const paths = list(fabric.paths).filter((path) => path.controller === controllerName);
      const expanders = sortFabricNodesForLane(list(fabric.nodes).filter((node) => node.kind === "expander" && node.controller_id === controllerId));
      const enclosures = sortFabricNodesForLane(list(fabric.nodes).filter((node) => node.kind === "mpr-enclosure" && node.controller_id === controllerId));
      const laneSlots = new Set(sortedSlots(controller.related_slots || controllerNode?.related_slots));
      paths.forEach((path) => sortedSlots(path.slots).forEach((slot) => laneSlots.add(slot)));
      expanders.forEach((node) => sortedSlots(node.related_slots).forEach((slot) => laneSlots.add(slot)));
      enclosures.forEach((node) => sortedSlots(node.related_slots).forEach((slot) => laneSlots.add(slot)));
      const slots = Array.from(laneSlots).sort((left, right) => left - right);
      const related = selectionTouchesNode(controllerId) || selectionTouchesSlots(slots);
      return `
        <section class="fabric-dedicated-lane${related ? " is-related" : ""}">
          <div class="fabric-stage controller">
            <span class="fabric-stage-title">Controller</span>
            ${renderNodeButton(controllerNode || { id: controllerId, kind: "controller", label: controllerName, related_slots: slots, status: "unknown", metrics: {} }, {
              label: controller.display_label || controller.alias || displayLabel(controllerNode) || controllerName,
              meta: nodeMeta(controllerNode, controller) || controller.device || "HBA",
            })}
          </div>
          <div class="fabric-stage">
            <span class="fabric-stage-title">Paths</span>
            <div class="fabric-path-grid">${paths.length ? paths.map((path) => renderPathButton(path, { compact: true })).join("") : '<span class="fabric-empty-note">No path states reported</span>'}</div>
          </div>
          <div class="fabric-stage">
            <span class="fabric-stage-title">Expanders</span>
            <div class="fabric-node-grid">${renderNodeGrid(expanders, 8)}</div>
          </div>
          <div class="fabric-stage">
            <span class="fabric-stage-title">SES / MPR Enclosures</span>
            <div class="fabric-node-grid">${renderNodeGrid(enclosures, 8)}</div>
          </div>
          <div class="fabric-stage">
            <span class="fabric-stage-title">Impacted Bays</span>
            <div class="fabric-bay-grid">${renderBayChips(slots, 120)}</div>
          </div>
        </section>
      `;
    }).join("");
    return `
      <div class="fabric-host-strip">
        ${renderNodeButton(hostNode, {
          label: displayLabel(hostNode) || "Host",
          meta: `${controllerRecords.length} controllers / ${list(fabric.nodes).length} nodes / ${list(fabric.links).length} links`,
          extra: "host",
        })}
      </div>
      <div class="fabric-lane-board">${lanes || '<div class="warning-item muted compact">No controller lanes are available yet.</div>'}</div>
    `;
  }

  function pathSeverity(path) {
    const stateName = classToken(path.state || "");
    if (["fail", "failed", "degraded", "missing", "offline"].includes(stateName)) {
      return 0;
    }
    if (["passive", "standby"].includes(stateName)) {
      return 1;
    }
    if (["active", "online"].includes(stateName)) {
      return 2;
    }
    return 3;
  }

  function renderImpactMode(fabric) {
    const paths = list(fabric.paths).slice().sort((left, right) => {
      const severity = pathSeverity(left) - pathSeverity(right);
      if (severity !== 0) {
        return severity;
      }
      return String(left.controller || "").localeCompare(String(right.controller || ""));
    });
    if (!paths.length) {
      return '<div class="warning-item muted compact">No path impact rows are available yet.</div>';
    }
    return `
      <div class="fabric-impact-grid">
        ${paths.map((path) => {
          const trace = traceById(path.id, fabric);
          const slots = sortedSlots(path.slots || trace?.slots);
          const summary = affectedSlotSummary(slots);
          const selected = state.selectedTraceId === path.id;
          return `
            <article class="fabric-impact-card status-${classToken(path.state)}${selected ? " is-selected" : ""}" data-fabric-trace="${escapeHtml(path.id)}" role="button" tabindex="0">
              <span class="fabric-node-kind">${escapeHtml(path.controller || "path")}</span>
              <strong>${escapeHtml(path.state || "unknown")}</strong>
              <span>${escapeHtml(`${slots.length} affected bay${slots.length === 1 ? "" : "s"}`)}</span>
              <div class="fabric-impact-facts">
                <span>Pools: ${escapeHtml(summary.pools.join(", ") || "n/a")}</span>
                <span>Vdevs: ${escapeHtml(summary.vdevs.join(", ") || "n/a")}</span>
                <span>Devices: ${escapeHtml(summary.devices.slice(0, 8).join(", ") || "n/a")}${summary.devices.length > 8 ? `, +${summary.devices.length - 8}` : ""}</span>
              </div>
              <div class="fabric-bay-grid compact">${renderBayChips(slots, 80)}</div>
            </article>
          `;
        }).join("")}
      </div>
    `;
  }

  function diskTraceHasDisk(trace) {
    const slotNumber = sortedSlots(trace?.slots)[0];
    const slot = slotByNumber(slotNumber);
    return Boolean(
      slot?.present !== false && (
        slot?.device_name ||
        slot?.serial ||
        slot?.multipath ||
        trace?.metrics?.device_name
      )
    );
  }

  function diskTraceSlots(fabric) {
    const bayTraces = list(fabric?.traces).filter((trace) => trace.kind === "bay");
    const allSlots = sortedSlots(bayTraces.flatMap((trace) => trace.slots));
    const diskSlots = sortedSlots(bayTraces
      .filter(diskTraceHasDisk)
      .flatMap((trace) => trace.slots));
    return diskSlots.length ? diskSlots : allSlots;
  }

  function selectedDiskTrace(fabric) {
    const traces = list(fabric?.traces);
    const tracesById = traceMap(fabric);
    const trace = selectedTrace();
    if (trace?.kind === "bay" && sortedSlots(trace.slots).length) {
      return trace;
    }
    const node = selectedNode();
    if (node?.kind === "bay") {
      const baySlot = Number.isInteger(Number(node.slot)) ? Number(node.slot) : sortedSlots(node.related_slots)[0];
      const bayTrace = tracesById.get(`bay:${baySlot}`);
      if (bayTrace) {
        return bayTrace;
      }
    }
    const candidateSlots = sortedSlots(trace?.slots || node?.related_slots);
    for (const slotNumber of candidateSlots) {
      const bayTrace = tracesById.get(`bay:${slotNumber}`);
      if (bayTrace) {
        return bayTrace;
      }
    }
    return traces.find((traceItem) => traceItem.kind === "bay" && diskTraceHasDisk(traceItem))
      || traces.find((traceItem) => traceItem.kind === "bay")
      || null;
  }

  function layoutSlotCount(fabric) {
    const hostSlotCount = Number(nodeMap(fabric).get("host")?.metrics?.slot_count);
    if (Number.isInteger(hostSlotCount) && hostSlotCount > 0) {
      return hostSlotCount;
    }
    const slotNumbers = list(state.snapshot?.slots)
      .map((slot) => Number(slot.slot))
      .filter((slot) => Number.isInteger(slot));
    return slotNumbers.length ? Math.max(...slotNumbers) + 1 : 0;
  }

  function backplaneZoneForSlot(slotNumber, fabric) {
    const zoneNode = Array.from(nodeMap(fabric).values()).find((node) => (
      node.kind === "backplane" && sortedSlots(node.related_slots).includes(Number(slotNumber))
    ));
    if (zoneNode) {
      const slots = sortedSlots(zoneNode.related_slots);
      const start = slots[0] ?? Number(slotNumber);
      const end = slots[slots.length - 1] ?? Number(slotNumber);
      return {
        id: zoneNode.id,
        index: Number(zoneNode.raw?.index ?? zoneNode.metrics?.zone ?? 0),
        label: displayLabel(zoneNode),
        rawLabel: rawLabel(zoneNode),
        slots,
        range: zoneNode.raw?.range || (slots.length ? `Bays ${formatSlotLabel(start)}-${formatSlotLabel(end)}` : "Bays n/a"),
      };
    }
    const slotCount = Math.max(layoutSlotCount(fabric), Number(slotNumber) + 1, 1);
    const zoneCount = slotCount >= 4 ? 4 : 1;
    const zoneSize = Math.max(1, Math.ceil(slotCount / zoneCount));
    const zoneIndex = Math.min(zoneCount - 1, Math.floor(Number(slotNumber) / zoneSize));
    const start = zoneIndex * zoneSize;
    const end = Math.min(slotCount - 1, start + zoneSize - 1);
    return {
      id: `backplane:${zoneIndex}`,
      index: zoneIndex,
      label: `Backplane Zone ${zoneIndex + 1}`,
      rawLabel: `Backplane Zone ${zoneIndex + 1}`,
      slots: Array.from({ length: end - start + 1 }, (_, index) => start + index),
      range: `Bays ${formatSlotLabel(start)}-${formatSlotLabel(end)}`,
    };
  }

  function formatLinkSpeed(value) {
    if (!value) {
      return null;
    }
    const text = String(value);
    return /bps|gb|mb/i.test(text) ? text : `${text} Gbps`;
  }

  function formatCountMap(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return null;
    }
    const entries = Object.entries(value)
      .filter(([, count]) => count !== null && count !== undefined && count !== "")
      .sort(([left], [right]) => left.localeCompare(right));
    return entries.length ? entries.map(([key, count]) => `${key}: ${count}`).join(", ") : null;
  }

  function diagnosticSummary(value) {
    if (!value || typeof value !== "object") {
      return null;
    }
    if (value.operator_summary) {
      return value.operator_summary;
    }
    const errors = Number(value.error_count || 0);
    const retries = Number(value.retry_count || 0);
    const senses = Number(value.sense_count || 0);
    const ioc = Number(value.ioc_terminated_count || 0);
    if (!errors && !retries && !senses && !ioc) {
      return null;
    }
    return `${errors} errors / ${retries} retries / ${senses} sense / ${ioc} IOC`;
  }

  function diagnosticSenseSummary(value) {
    return formatCountMap(value?.sense_counts);
  }

  function diagnosticLoginfoSummary(value) {
    return formatCountMap(value?.loginfo_counts);
  }

  function diagnosticRecentSummary(value) {
    const events = diagnosticEventRows(value).slice(-3);
    if (!events.length) {
      return null;
    }
    return events.map((event) => [event.device, diagnosticEventLabel(event)].filter(Boolean).join(": ")).join(" | ");
  }

  function diagnosticEventLabel(event) {
    const decoded = event?.decoded || {};
    const operation = event?.operation || decoded.operation;
    if (operation) {
      const details = [
        event?.direction || decoded.direction,
        Number.isFinite(Number(event?.lba ?? decoded.lba)) ? `LBA ${event?.lba ?? decoded.lba}` : null,
        Number.isFinite(Number(event?.transfer_blocks ?? decoded.transfer_blocks))
          ? `${event?.transfer_blocks ?? decoded.transfer_blocks} blocks`
          : null,
        Number.isFinite(Number(event?.allocation_length ?? decoded.allocation_length))
          ? `alloc ${event?.allocation_length ?? decoded.allocation_length} bytes`
          : null,
        Number.isFinite(Number(event?.parameter_list_length ?? decoded.parameter_list_length))
          ? `params ${event?.parameter_list_length ?? decoded.parameter_list_length} bytes`
          : null,
      ].filter(Boolean).join(" / ");
      return details ? `${operation} (${details})` : operation;
    }
    return event?.label || decoded.label || event?.reason || event?.message || "kernel event";
  }

  function diagnosticConfidenceLabel(event) {
    const confidence = event?.decode_confidence || event?.decoded?.decode_confidence;
    if (!confidence) {
      return null;
    }
    return String(confidence).replace(/_/g, " ");
  }

  function diagnosticFindingChips(value) {
    const findings = list(value?.top_findings).slice(0, 4);
    if (findings.length) {
      return findings.map((finding) => `
        <span class="fabric-diagnostic-chip severity-${classToken(finding.severity || "info")}">
          <strong>${escapeHtml(finding.label || finding.family || "Finding")}</strong>
          <em>${escapeHtml(formatValue(finding.count || 0))}</em>
        </span>
      `).join("");
    }
    const families = value?.fault_family_counts || {};
    return Object.entries(families).slice(0, 4).map(([family, count]) => `
      <span class="fabric-diagnostic-chip">
        <strong>${escapeHtml(family.replace(/_/g, " "))}</strong>
        <em>${escapeHtml(formatValue(count))}</em>
      </span>
    `).join("");
  }

  function diagnosticLikelyLayers(value) {
    return list(value?.top_findings)
      .map((finding) => finding.likely_layer)
      .filter(Boolean)
      .filter((item, index, items) => items.indexOf(item) === index)
      .slice(0, 3)
      .join(" + ");
  }

  function renderDiagnosticEvent(event) {
    const decoded = event?.decoded || {};
    const meta = [
      event?.event_type,
      event?.device,
      event?.target ? `target ${event.target}` : null,
      event?.loginfo ? `loginfo ${event.loginfo}` : null,
      event?.opcode || decoded.opcode,
      (event?.service_action || decoded.service_action) ? `SA ${event?.service_action || decoded.service_action}` : null,
      event?.log_page || decoded.log_page,
      (event?.asc || decoded.asc) ? `ASC ${event?.asc || decoded.asc}` : null,
      diagnosticConfidenceLabel(event),
    ].filter(Boolean).join(" / ");
    const details = [
      event?.likely_layer || decoded.likely_layer,
      event?.direction || decoded.direction,
      Number.isFinite(Number(event?.lba ?? decoded.lba)) ? `LBA ${event?.lba ?? decoded.lba}` : null,
      Number.isFinite(Number(event?.transfer_blocks ?? decoded.transfer_blocks))
        ? `${event?.transfer_blocks ?? decoded.transfer_blocks} blocks`
        : null,
      Number.isFinite(Number(event?.allocation_length ?? decoded.allocation_length))
        ? `allocation ${event?.allocation_length ?? decoded.allocation_length} bytes`
        : null,
      Number.isFinite(Number(event?.parameter_list_length ?? decoded.parameter_list_length))
        ? `parameter list ${event?.parameter_list_length ?? decoded.parameter_list_length} bytes`
        : null,
      event?.log_page_control_label || decoded.log_page_control_label,
    ].filter(Boolean).join(" / ");
    return `
      <li class="fabric-diagnostic-event severity-${classToken(event?.severity || "info")}">
        <strong>${escapeHtml(diagnosticEventLabel(event))}</strong>
        ${meta ? `<span>${escapeHtml(meta)}</span>` : ""}
        ${details ? `<small>${escapeHtml(details)}</small>` : ""}
        ${(event?.description || decoded.description) ? `<small>${escapeHtml(event?.description || decoded.description)}</small>` : ""}
        ${(event?.decoder_note || decoded.decoder_note) ? `<small>${escapeHtml(event?.decoder_note || decoded.decoder_note)}</small>` : ""}
      </li>
    `;
  }

  function diagnosticEventRows(diagnostics) {
    const tableRows = list(diagnostics?.event_table?.rows);
    if (tableRows.length) {
      return tableRows;
    }
    const decodedRows = list(diagnostics?.decoded_records);
    if (decodedRows.length) {
      return decodedRows;
    }
    return list(diagnostics?.recent_events);
  }

  function diagnosticTableKey(diagnostics) {
    const rows = diagnosticEventRows(diagnostics);
    const first = rows[0]?.event_id || rows[0]?.id || "first";
    const last = rows[rows.length - 1]?.event_id || rows[rows.length - 1]?.id || "last";
    const scope = [
      list(diagnostics?.devices).join("-"),
      list(diagnostics?.targets).join("-"),
      diagnostics?.event_count,
      first,
      last,
    ].filter(Boolean).join("-");
    return classToken(scope || "diagnostic-events");
  }

  function diagnosticTableState(key) {
    if (!state.diagnosticTables[key]) {
      state.diagnosticTables[key] = {
        page: 1,
        filter: "",
        type: "all",
        open: false,
      };
    }
    return state.diagnosticTables[key];
  }

  function diagnosticEventTypeLabel(type) {
    return formatKind(String(type || "event").replace(/_/g, "-"));
  }

  function diagnosticEventTime(event) {
    if (event?.timestamp || event?.timestamp_raw) {
      return event.timestamp || event.timestamp_raw;
    }
    if (event?.event_id) {
      return `#${String(event.event_id).replace(/^.*?(\d+)$/, "$1")}`;
    }
    if (Number.isFinite(Number(event?.sequence))) {
      return `#${String(Number(event.sequence) + 1).padStart(4, "0")}`;
    }
    return "-";
  }

  function diagnosticEventTimeTitle(event) {
    if (event?.timestamp || event?.timestamp_raw) {
      return "Source timestamp";
    }
    return "Source dmesg did not include wall-clock timestamps; showing event order from the collected kernel buffer.";
  }

  function diagnosticRowSearchText(event) {
    const values = [
      diagnosticEventTime(event),
      event?.event_type,
      diagnosticEventLabel(event),
      event?.controller,
      event?.device,
      event?.target,
      event?.loginfo,
      event?.asc,
      event?.opcode,
      event?.service_action,
      event?.service_action_label,
      event?.log_page,
      event?.log_page_control_label,
      event?.decode_confidence,
      event?.message,
      event?.likely_layer,
      event?.family,
    ];
    return values.filter(Boolean).join(" ").toLowerCase();
  }

  function filteredDiagnosticEventRows(rows, tableState) {
    const filter = String(tableState.filter || "").trim().toLowerCase();
    const type = String(tableState.type || "all");
    return rows.filter((event) => {
      if (type !== "all" && String(event?.event_type || "event") !== type) {
        return false;
      }
      return !filter || diagnosticRowSearchText(event).includes(filter);
    });
  }

  function diagnosticEventTypeOptions(rows) {
    const counts = new Map();
    rows.forEach((event) => {
      const type = String(event?.event_type || "event");
      counts.set(type, (counts.get(type) || 0) + 1);
    });
    return Array.from(counts.entries()).sort(([left], [right]) => left.localeCompare(right));
  }

  function diagnosticPageNumbers(page, pageCount) {
    if (pageCount <= 7) {
      return Array.from({ length: pageCount }, (_, index) => index + 1);
    }
    const pages = new Set([1, pageCount, page - 1, page, page + 1]);
    if (page <= 3) {
      pages.add(2);
      pages.add(3);
      pages.add(4);
    }
    if (page >= pageCount - 2) {
      pages.add(pageCount - 1);
      pages.add(pageCount - 2);
      pages.add(pageCount - 3);
    }
    return Array.from(pages)
      .filter((item) => item >= 1 && item <= pageCount)
      .sort((left, right) => left - right);
  }

  function renderDiagnosticPageButtons(key, page, pageCount) {
    const pages = diagnosticPageNumbers(page, pageCount);
    const buttons = [];
    let previous = 0;
    pages.forEach((pageNumber) => {
      if (previous && pageNumber - previous > 1) {
        buttons.push('<span class="fabric-diagnostic-page-gap">...</span>');
      }
      buttons.push(`
        <button type="button" class="fabric-diagnostic-page-button${pageNumber === page ? " is-active" : ""}" data-fabric-diagnostic-page="${escapeHtml(String(pageNumber))}" data-fabric-diagnostic-key="${escapeHtml(key)}" aria-current="${pageNumber === page ? "page" : "false"}">
          ${escapeHtml(pageNumber)}
        </button>
      `);
      previous = pageNumber;
    });
    return buttons.join("");
  }

  function renderDiagnosticTableControls({
    key,
    page,
    pageCount,
    pageSize,
    start,
    end,
    total,
    filteredTotal,
    filter,
    type,
    typeOptions,
    hasSourceTimestamps,
  }) {
    const hasFilter = String(filter || "").trim() || type !== "all";
    const typeSelectOptions = [
      `<option value="all"${type === "all" ? " selected" : ""}>All types</option>`,
      ...typeOptions.map(([eventType, count]) => `
        <option value="${escapeHtml(eventType)}"${type === eventType ? " selected" : ""}>${escapeHtml(diagnosticEventTypeLabel(eventType))} (${escapeHtml(formatValue(count))})</option>
      `),
    ].join("");
    return `
      <div class="fabric-diagnostic-table-controls">
        <div class="fabric-diagnostic-table-status">
          <strong>${escapeHtml(`Showing ${formatValue(start)}-${formatValue(end)} of ${formatValue(filteredTotal)} individual events`)}</strong>
          <small>${escapeHtml(hasFilter ? `${formatValue(total)} total before filter` : "Rows are not deduped; grouped counts are shown above; newest first.")}</small>
          ${hasSourceTimestamps ? "" : '<small>No source timestamps in this dmesg slice; Time / Order falls back to event order.</small>'}
        </div>
        <label>
          <span>Filter</span>
          <input type="search" value="${escapeHtml(filter || "")}" placeholder="finding, device, target, code" data-fabric-diagnostic-filter-key="${escapeHtml(key)}">
        </label>
        <label>
          <span>Type</span>
          <select data-fabric-diagnostic-type-key="${escapeHtml(key)}">${typeSelectOptions}</select>
        </label>
        <div class="fabric-diagnostic-pagination" aria-label="Diagnostic event pages">
          <button type="button" data-fabric-diagnostic-page="prev" data-fabric-diagnostic-key="${escapeHtml(key)}" aria-label="Previous event page" title="Previous event page"${page <= 1 ? " disabled" : ""}>&lt;</button>
          ${renderDiagnosticPageButtons(key, page, pageCount)}
          <button type="button" data-fabric-diagnostic-page="next" data-fabric-diagnostic-key="${escapeHtml(key)}" aria-label="Next event page" title="Next event page"${page >= pageCount ? " disabled" : ""}>&gt;</button>
          <span>${escapeHtml(`Page ${formatValue(page)} of ${formatValue(pageCount)} / ${formatValue(pageSize)} per page`)}</span>
        </div>
      </div>
    `;
  }

  function renderDiagnosticTableRow(event) {
    const scope = [event?.controller, event?.device, event?.target ? `target ${event.target}` : null]
      .filter(Boolean)
      .join(" / ");
    const code = [
      event?.loginfo ? `loginfo ${event.loginfo}` : null,
      event?.asc ? `ASC ${event.asc}` : null,
      event?.opcode,
      event?.service_action ? `SA ${event.service_action}` : null,
      event?.log_page,
    ]
      .filter(Boolean)
      .join(" / ");
    const confidence = diagnosticConfidenceLabel(event);
    return `
      <tr class="severity-${classToken(event?.severity || "info")}">
        <td title="${escapeHtml(diagnosticEventTimeTitle(event))}">${escapeHtml(diagnosticEventTime(event))}</td>
        <td>${escapeHtml(event?.event_type || "event")}</td>
        <td>${escapeHtml(diagnosticEventLabel(event))}</td>
        <td>${escapeHtml(scope || "-")}</td>
        <td>${escapeHtml([code || "-", confidence].filter(Boolean).join(" / "))}</td>
      </tr>
    `;
  }

  function renderDiagnosticEvidencePanel(diagnostics, scopeLabel = "") {
    if (!diagnostics || typeof diagnostics !== "object" || !Number(diagnostics.event_count || 0)) {
      return "";
    }
    const eventRows = diagnosticEventRows(diagnostics);
    const events = eventRows.slice(-5).reverse();
    const table = diagnostics.event_table || {};
    const tablePageSize = Math.max(1, Math.min(Number(table.page_size || 25), 100));
    const tableTotal = Number(table.total_count || eventRows.length || 0);
    const tableKey = diagnosticTableKey(diagnostics);
    const tableState = diagnosticTableState(tableKey);
    const orderedTableRows = [...eventRows].reverse();
    const filteredTableRows = filteredDiagnosticEventRows(orderedTableRows, tableState);
    const pageCount = Math.max(1, Math.ceil(filteredTableRows.length / tablePageSize));
    tableState.page = Math.max(1, Math.min(Number(tableState.page || 1), pageCount));
    const pageStartIndex = (tableState.page - 1) * tablePageSize;
    const tableRows = filteredTableRows.slice(pageStartIndex, pageStartIndex + tablePageSize);
    const visibleStart = filteredTableRows.length ? pageStartIndex + 1 : 0;
    const visibleEnd = pageStartIndex + tableRows.length;
    const hasSourceTimestamps = eventRows.some((event) => event?.timestamp || event?.timestamp_raw);
    const layers = diagnosticLikelyLayers(diagnostics);
    const summary = diagnostics.operator_summary || diagnosticSummary(diagnostics);
    const rawRows = [
      ["Targets", formatSlots(diagnostics.targets, 999)],
      ["Devices", list(diagnostics.devices).join(", ")],
      ["Sense", diagnosticSenseSummary(diagnostics)],
      ["Loginfo", diagnosticLoginfoSummary(diagnostics)],
      ["Operations", formatCountMap(diagnostics.operation_counts)],
    ].filter(([, value]) => value);
    return `
      <div class="fabric-diagnostic-evidence">
        <div class="fabric-diagnostic-evidence-head">
          <span class="fabric-stage-title">Fault Evidence</span>
          <strong>${escapeHtml(summary || `${diagnostics.event_count} kernel events`)}</strong>
          ${scopeLabel ? `<small>Path leg: ${escapeHtml(scopeLabel)}</small>` : ""}
          ${layers ? `<small>Likely layer: ${escapeHtml(layers)}</small>` : ""}
        </div>
        <div class="fabric-diagnostic-chips">
          ${diagnosticFindingChips(diagnostics)}
        </div>
        ${events.length ? `<ol class="fabric-diagnostic-events">${events.map(renderDiagnosticEvent).join("")}</ol>` : ""}
        ${rawRows.length ? `
          <details class="fabric-diagnostic-raw">
            <summary>Raw decoded buckets</summary>
            <div>
              ${rawRows.map(([label, value]) => `<span><em>${escapeHtml(label)}</em>${escapeHtml(formatValue(value))}</span>`).join("")}
            </div>
          </details>
        ` : ""}
        ${eventRows.length ? `
          <details class="fabric-diagnostic-raw"${tableState.open ? " open" : ""} data-fabric-diagnostic-table-key="${escapeHtml(tableKey)}">
            <summary>Full event table (${escapeHtml(formatValue(tableTotal))})</summary>
            ${renderDiagnosticTableControls({
              key: tableKey,
              page: tableState.page,
              pageCount,
              pageSize: tablePageSize,
              start: visibleStart,
              end: visibleEnd,
              total: tableTotal,
              filteredTotal: filteredTableRows.length,
              filter: tableState.filter,
              type: tableState.type,
              typeOptions: diagnosticEventTypeOptions(eventRows),
              hasSourceTimestamps,
            })}
            <div class="fabric-diagnostic-table-wrap">
              <table class="fabric-diagnostic-table">
                <thead>
                  <tr><th>Time / Order</th><th>Type</th><th>Finding</th><th>Scope</th><th>Code</th></tr>
                </thead>
                <tbody>${tableRows.length ? tableRows.map(renderDiagnosticTableRow).join("") : '<tr><td colspan="5">No events match the current filter.</td></tr>'}</tbody>
              </table>
            </div>
          </details>
        ` : ""}
      </div>
    `;
  }

  function expanderPhyForDevice(expanderNode, mprDevice) {
    const handle = String(mprDevice?.handle || "");
    if (!handle) {
      return null;
    }
    return list(expanderNode?.raw?.phys).find((phy) => String(phy.dev_handle || "") === handle) || null;
  }

  function tooltipText(rows) {
    return rows
      .filter(([, value]) => value !== null && value !== undefined && value !== "")
      .map(([label, value]) => `${label}: ${formatValue(value)}`)
      .join("\n");
  }

  function controllerNameFromId(controllerId) {
    return String(controllerId || "").replace(/^controller:/, "") || null;
  }

  function branchSeedsForDiskTrace(trace, fabric) {
    const nodes = nodeMap(fabric);
    const seeds = [];
    const seen = new Set();
    const addSeed = (seed) => {
      const key = [
        seed.controller || controllerNameFromId(seed.controller_id),
        classToken(seed.state || seed.pathNode?.status || seed.mprDevice?.state),
        seed.device_name || seed.mprDevice?.member_device_name || "",
        seed.expanderNode?.id || "",
      ].join(":");
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      seeds.push(seed);
    };
    list(trace.metrics?.path_states).forEach((pathState) => addSeed({
      controller: pathState.controller,
      state: pathState.state,
      device_name: pathState.device_name,
      pathState,
    }));
    list(trace.metrics?.mpr_devices).forEach((mprDevice) => addSeed({
      controller: mprDevice.controller,
      state: mprDevice.state,
      device_name: mprDevice.member_device_name,
      mprDevice,
    }));
    if (!seeds.length) {
      list(trace.node_ids)
        .map((nodeId) => nodes.get(nodeId))
        .filter((node) => node?.kind === "path")
        .forEach((pathNode) => addSeed({
          controller_id: pathNode.controller_id,
          controller: controllerNameFromId(pathNode.controller_id),
          state: pathNode.status,
          pathNode,
        }));
    }
    if (!seeds.length) {
      list(trace.node_ids)
        .map((nodeId) => nodes.get(nodeId))
        .filter((node) => node?.kind === "controller")
        .forEach((controllerNode) => {
          const expanders = list(trace.node_ids)
            .map((nodeId) => nodes.get(nodeId))
            .filter((node) => node?.kind === "expander" && node.controller_id === controllerNode.id);
          if (!expanders.length) {
            addSeed({
              controller_id: controllerNode.id,
              controller: displayLabel(controllerNode) || controllerNameFromId(controllerNode.id),
              state: controllerNode.status || "reported",
              controllerNode,
            });
            return;
          }
          expanders.forEach((expanderNode) => addSeed({
            controller_id: controllerNode.id,
            controller: displayLabel(controllerNode) || controllerNameFromId(controllerNode.id),
            state: controllerNode.status || "reported",
            controllerNode,
            expanderNode,
          }));
        });
    }
    return seeds.length ? seeds : [{}];
  }

  function branchMprDevice(seed, trace) {
    const mprDevices = list(trace.metrics?.mpr_devices);
    const controller = seed.controller || controllerNameFromId(seed.controller_id);
    const stateName = classToken(seed.state || seed.pathState?.state || seed.pathNode?.status || seed.mprDevice?.state);
    return seed.mprDevice
      || mprDevices.find((device) => (
        device.controller === controller
        && classToken(device.state) === stateName
        && (!seed.device_name || device.member_device_name === seed.device_name)
      ))
      || mprDevices.find((device) => device.controller === controller)
      || null;
  }

  function firstTraceNode(trace, fabric, predicate) {
    const nodes = nodeMap(fabric);
    return list(trace.node_ids).map((nodeId) => nodes.get(nodeId)).find(predicate) || null;
  }

  function renderDiskPathCard({ kind, title, subtitle = "", facts = [], hoverFacts = [], status = "", nodeId = "", traceId = "", extra = "" }) {
    const selected = (nodeId && state.selectedNodeId === nodeId) || (traceId && state.selectedTraceId === traceId);
    const actionAttribute = nodeId
      ? `data-fabric-node="${escapeHtml(nodeId)}"`
      : (traceId ? `data-fabric-trace="${escapeHtml(traceId)}"` : "");
    const tagName = actionAttribute ? "button" : "div";
    const typeAttribute = tagName === "button" ? ' type="button"' : "";
    const detailText = tooltipText([["Layer", kind], ["Name", title], ["Context", subtitle], ...facts, ...hoverFacts]);
    const titleAttribute = detailText ? ` title="${escapeHtml(detailText)}"` : "";
    const factRows = facts
      .filter(([, value]) => value !== null && value !== undefined && value !== "")
      .slice(0, 6)
      .map(([label, value]) => `
        <span><em>${escapeHtml(label)}</em>${escapeHtml(formatValue(value))}</span>
      `).join("");
    return `
      <${tagName}${typeAttribute} class="disk-path-card status-${classToken(status)}${selected ? " is-selected" : ""} ${extra}" ${actionAttribute}${titleAttribute}>
        <span class="disk-path-kind">${escapeHtml(kind)}</span>
        <strong>${escapeHtml(title || "n/a")}</strong>
        ${subtitle ? `<small>${escapeHtml(subtitle)}</small>` : ""}
        ${factRows ? `<div class="disk-path-facts">${factRows}</div>` : ""}
      </${tagName}>
    `;
  }

  function renderDiskPathBayChip(slotNumber, activeSlotNumber, slotSet) {
    if (!Number.isInteger(slotNumber)) {
      return '<span class="fabric-bay-chip disk-path-bay-placeholder" aria-hidden="true"></span>';
    }
    const slot = slotByNumber(slotNumber);
    const selected = Number(slotNumber) === Number(activeSlotNumber);
    const enabled = slotSet.has(slotNumber);
    const title = [slot?.device_name, slot?.serial, slot?.pool_name, slot?.vdev_name].filter(Boolean).join(" / ");
    return `
      <button type="button" class="fabric-bay-chip${selected ? " is-selected" : ""}${enabled ? "" : " is-unavailable"}" data-fabric-trace="bay:${slotNumber}" title="${escapeHtml(title || `Bay ${formatSlotLabel(slotNumber)}`)}"${enabled ? "" : " disabled"}>
        ${escapeHtml(formatSlotLabel(slotNumber))}
      </button>
    `;
  }

  function renderDiskPathBayPicker(fabric, activeSlotNumber) {
    const slots = diskTraceSlots(fabric);
    if (!slots.length) {
      return '<span class="fabric-empty-note">No disk bay traces</span>';
    }
    const slotSet = new Set(sortedSlots(slots));
    const layoutRows = slotLayoutRows().filter((row) => row.some((slotNumber) => Number.isInteger(slotNumber)));
    if (!layoutRows.length) {
      return sortedSlots(slots).map((slotNumber) => renderDiskPathBayChip(slotNumber, activeSlotNumber, slotSet)).join("");
    }
    return `
      <div class="disk-path-bay-layout">
        ${layoutRows.map((row) => `
          <div class="disk-path-bay-row">
            ${splitLayoutRow(row).map((group, groupIndex, groups) => `
              <div class="disk-path-bay-group" style="--disk-path-bay-columns: ${group.length}">
                ${group.map((slotNumber) => renderDiskPathBayChip(slotNumber, activeSlotNumber, slotSet)).join("")}
              </div>
              ${groupIndex < groups.length - 1 ? '<span class="disk-path-bay-divider" aria-hidden="true"></span>' : ""}
            `).join("")}
          </div>
        `).join("")}
      </div>
    `;
  }

  function diskPathBranchEvidence(seed, trace, fabric) {
    const nodes = nodeMap(fabric);
    const mprDevice = branchMprDevice(seed, trace);
    const controllerName = seed.controller || mprDevice?.controller || controllerNameFromId(seed.controller_id) || "controller";
    const stateName = seed.state || seed.pathState?.state || seed.pathNode?.status || mprDevice?.state || "unknown";
    const controllerId = controllerName ? `controller:${controllerName}` : seed.controller_id;
    const controllerNode = nodes.get(controllerId) || seed.controllerNode || firstTraceNode(trace, fabric, (node) => node?.kind === "controller");
    const controllerDiagnostics = controllerNode?.metrics?.kernel_diagnostics || controllerNode?.raw?.kernel_diagnostics;
    const linkDiagnostics = mprDevice?.diagnostics;
    const diagnostics = Number(linkDiagnostics?.event_count || 0) ? linkDiagnostics : controllerDiagnostics;
    const scopeLabel = [
      controllerName,
      stateName,
      mprDevice?.member_device_name || seed.device_name,
    ].filter(Boolean).join(" ");
    return renderDiagnosticEvidencePanel(diagnostics, scopeLabel);
  }

  function renderDiskPathBranch(seed, trace, fabric, slotNumber, options = {}) {
    const nodes = nodeMap(fabric);
    const slot = slotByNumber(slotNumber);
    const hostNode = nodes.get("host") || {
      id: "host",
      kind: "host",
      label: fabric.system_label || fabric.system_id || "TrueNAS CORE Host",
      metrics: {},
    };
    const mprDevice = branchMprDevice(seed, trace);
    const controllerName = seed.controller || mprDevice?.controller || controllerNameFromId(seed.controller_id) || "controller";
    const stateName = seed.state || seed.pathState?.state || seed.pathNode?.status || mprDevice?.state || "unknown";
    const controllerId = controllerName ? `controller:${controllerName}` : seed.controller_id;
    const controllerNode = nodes.get(controllerId) || seed.controllerNode || firstTraceNode(trace, fabric, (node) => node?.kind === "controller");
    const pathId = controllerName && stateName ? `path:${controllerName}:${classToken(stateName)}` : seed.pathNode?.id;
    const pathNode = nodes.get(pathId) || seed.pathNode || firstTraceNode(trace, fabric, (node) => (
      node?.kind === "path" && (!controllerId || node.controller_id === controllerId)
    ));
    const expanderId = list(mprDevice?.expander_ids)[0];
    const expanderNode = nodes.get(expanderId) || seed.expanderNode || firstTraceNode(trace, fabric, (node) => (
      node?.kind === "expander" && (!controllerId || node.controller_id === controllerId)
    ));
    const enclosureNode = nodes.get(mprDevice?.enclosure_id) || firstTraceNode(trace, fabric, (node) => (
      node?.kind === "mpr-enclosure" && (!controllerId || node.controller_id === controllerId)
    ));
    const expanderPhy = expanderPhyForDevice(expanderNode, mprDevice);
    const sesNodes = list(trace.node_ids)
      .map((nodeId) => nodes.get(nodeId))
      .filter((node) => node?.kind === "ses-enclosure" && selectionTouchesSlots([slotNumber]));
    const zone = backplaneZoneForSlot(slotNumber, fabric);
    const backplaneNode = zone.id ? nodes.get(zone.id) : null;
    const diskTitle = trace.metrics?.device_name || slot?.device_name || `Bay ${formatSlotLabel(slotNumber)}`;
    const diskSubtitle = [slot?.serial, slot?.model, slot?.size_human].filter(Boolean).join(" / ");
    const iocFacts = controllerNode?.raw?.iocfacts || {};
    const controllerDiagnostics = controllerNode?.metrics?.kernel_diagnostics || controllerNode?.raw?.kernel_diagnostics;
    const linkDiagnostics = mprDevice?.diagnostics;
    const branchDiagnostics = Number(linkDiagnostics?.event_count || 0) ? linkDiagnostics : controllerDiagnostics;
    const cards = [
      renderDiskPathCard({
        kind: "Host",
        title: displayLabel(hostNode) || "TrueNAS CORE Host",
        subtitle: fabric.selected_enclosure_label || fabric.system_id || "",
        facts: [
          ["Bay", `Bay ${formatSlotLabel(slotNumber)}`],
          ["Controllers", list(fabric.controllers).length],
        ],
        hoverFacts: [
          ["System ID", fabric.system_id],
          ["Enclosure ID", fabric.selected_enclosure_id],
          ["Trace", trace.id],
        ],
        status: hostNode.status || "online",
        nodeId: hostNode.id,
      }),
      renderDiskPathCard({
        kind: "HBA",
        title: displayLabel(controllerNode) || controllerName,
        subtitle: controllerNode?.raw?.board || controllerNode?.raw_id || "",
        facts: [
          ["PCIe slot", controllerNode?.metrics?.pcie_slot || controllerNode?.raw?.pcie_slot],
          ["FW", controllerNode?.metrics?.firmware || controllerNode?.raw?.firmware],
          ["Temp", controllerNode?.metrics?.temperature],
          ["PHY", controllerNode?.metrics?.linked_phy_count && controllerNode?.metrics?.phy_count
            ? `${controllerNode.metrics.linked_phy_count}/${controllerNode.metrics.phy_count}`
            : controllerNode?.metrics?.linked_phys],
          ["IOC", iocFacts.iocstatus],
          ["Events", diagnosticSummary(controllerDiagnostics)],
        ],
        hoverFacts: [
          ["Device", controllerNode?.raw?.device || controllerNode?.raw_id],
          ["PCI address", controllerNode?.metrics?.pci_address || controllerNode?.raw?.pci_address],
          ["PCI location", controllerNode?.raw?.pci_location],
          ["PCI parent", controllerNode?.raw?.pci_parent],
          ["ACPI handle", controllerNode?.raw?.acpi_handle],
          ["PCIe type", controllerNode?.raw?.pcie_slot_type],
          ["PCIe usage", controllerNode?.raw?.pcie_slot_usage],
          ["PCI device", controllerNode?.raw?.pci_device],
          ["PCI vendor", controllerNode?.raw?.pci_vendor],
          ["Chip", controllerNode?.raw?.chip],
          ["Path counts", formatCountMap(controllerNode?.metrics?.path_counts || controllerNode?.raw?.path_counts)],
          ["Kernel devices", list(controllerDiagnostics?.devices).join(", ")],
          ["Kernel targets", formatSlots(controllerDiagnostics?.targets, 999)],
          ["Kernel sense", diagnosticSenseSummary(controllerDiagnostics)],
          ["Kernel loginfo", diagnosticLoginfoSummary(controllerDiagnostics)],
          ["IOC log", iocFacts.iocloginfo],
          ["IOC exceptions", iocFacts.iocexceptions],
          ["Max targets", iocFacts.maxtargets],
          ["Max expanders", iocFacts.maxsasexpanders],
          ["Max enclosures", iocFacts.maxenclosures],
          ["Capabilities", iocFacts.ioccapabilities],
        ],
        status: controllerNode?.status || stateName,
        nodeId: controllerNode?.id || controllerId,
      }),
      renderDiskPathCard({
        kind: "SAS Link",
        title: `${controllerName} ${stateName}`,
        subtitle: mprDevice?.member_device_name || seed.device_name || displayLabel(pathNode) || "controller-level fabric",
        facts: [
          ["Speed", formatLinkSpeed(mprDevice?.speed)],
          ["PHY", expanderPhy?.phy],
          ["Remote", expanderPhy?.remote_phy],
          ["Handle", mprDevice?.handle],
          ["Parent", mprDevice?.parent],
          ["Disk slot", mprDevice?.mpr_slot],
          ["Events", diagnosticSummary(linkDiagnostics)],
        ],
        hoverFacts: [
          ["Path state", stateName],
          ["Member device", mprDevice?.member_device_name || seed.device_name],
          ["MPR device", mprDevice?.mpr_device || expanderPhy?.device],
          ["SAS address", mprDevice?.sas_address],
          ["Enclosure handle", mprDevice?.enclosure_handle],
          ["Kernel targets", formatSlots(linkDiagnostics?.targets, 999)],
          ["Kernel sense", diagnosticSenseSummary(linkDiagnostics)],
          ["Kernel loginfo", diagnosticLoginfoSummary(linkDiagnostics)],
          ["Recent kernel events", diagnosticRecentSummary(linkDiagnostics)],
          ["Min speed", formatLinkSpeed(expanderPhy?.min)],
          ["Max speed", formatLinkSpeed(expanderPhy?.max)],
          ["Expander device", expanderPhy?.device],
        ],
        status: stateName,
        traceId: traceById(pathNode?.id, fabric) ? pathNode.id : "",
      }),
      renderDiskPathCard({
        kind: "Expander / SES",
        title: displayLabel(expanderNode) || displayLabel(enclosureNode) || "Expander unknown",
        subtitle: [
          displayLabel(enclosureNode) && displayLabel(enclosureNode) !== displayLabel(expanderNode) ? `MPR ${displayLabel(enclosureNode)}` : null,
          sesNodes.map(displayLabel).join(" + "),
        ].filter(Boolean).join(" / "),
        facts: [
          ["WWN", expanderNode?.raw_id || enclosureNode?.raw_id],
          ["PHY", expanderNode?.metrics?.linked_phys && expanderNode?.metrics?.num_phys
            ? `${expanderNode.metrics.linked_phys}/${expanderNode.metrics.num_phys}`
            : expanderNode?.metrics?.linked_phys],
          ["Enclosure", mprDevice?.enclosure_handle || enclosureNode?.raw?.enc_handle],
          ["Devices", formatCountMap(expanderNode?.raw?.device_counts)],
        ],
        hoverFacts: [
          ["Dev handle", expanderNode?.raw?.dev_handle],
          ["Parent handle", expanderNode?.raw?.parent],
          ["Enc handle", expanderNode?.raw?.enc_handle || enclosureNode?.raw?.enc_handle],
          ["SAS level", expanderNode?.raw?.sas_level || expanderNode?.metrics?.sas_level],
          ["SES devices", sesNodes.map(displayLabel).join(" + ")],
          ["MPR enclosure type", enclosureNode?.raw?.type],
          ["Selected disk slots", formatSlots(expanderNode?.metrics?.selected_disk_slots, 999)],
        ],
        status: expanderNode?.status || enclosureNode?.status || stateName,
        nodeId: expanderNode?.id || enclosureNode?.id || "",
      }),
      renderDiskPathCard({
        kind: "Backplane",
        title: displayLabel(backplaneNode) || zone.label,
        subtitle: zone.range,
        facts: [
          ["Selected", `Bay ${formatSlotLabel(slotNumber)}`],
          ["SES element", slot?.ssh_ses_element_id],
          ["Enclosure", slot?.enclosure_name],
        ],
        hoverFacts: [
          ["Zone slots", formatSlots(zone.slots, 999)],
          ["SES targets", list(slot?.ssh_ses_targets).map((target) => `${target.ses_device}:${target.ses_element_id}`).join(", ")],
          ["SES device", slot?.ssh_ses_device],
          ["Mapping source", slot?.mapping_source],
          ["Raw status", slot?.raw_status?.status],
          ["Descriptor", slot?.raw_status?.descriptor],
        ],
        status: slot?.state || trace.status || "online",
        nodeId: backplaneNode?.id || zone.id || "",
      }),
      renderDiskPathCard({
        kind: "Disk",
        title: diskTitle,
        subtitle: diskSubtitle,
        facts: [
          ["Pool / vdev", [slot?.pool_name, slot?.vdev_name].filter(Boolean).join(" / ")],
          ["Health", [slot?.health || slot?.state, slot?.temperature_c ? `${slot.temperature_c} C` : null].filter(Boolean).join(" / ")],
          ["SAS / LUN", mprDevice?.sas_address || slot?.sas_address || slot?.logical_unit_id],
          ["Member", mprDevice?.member_device_name || seed.device_name],
          ["Events", diagnosticSummary(linkDiagnostics)],
        ],
        hoverFacts: [
          ["Serial", slot?.serial],
          ["Model", slot?.model],
          ["Size", slot?.size_human],
          ["Kernel sense", diagnosticSenseSummary(linkDiagnostics)],
          ["Recent kernel events", diagnosticRecentSummary(linkDiagnostics)],
          ["GPTID", slot?.gptid],
          ["Multipath state", slot?.multipath?.state || slot?.multipath?.provider_state],
          ["SMART", slot?.smart_status],
          ["Last SMART test", [slot?.last_smart_test_type, slot?.last_smart_test_status].filter(Boolean).join(" / ")],
          ["Transport", slot?.transport_protocol],
        ],
        status: slot?.health || slot?.state || stateName,
        traceId: trace.id,
      }),
    ];
    return `
      <section class="disk-path-branch status-${classToken(stateName)}">
        <div class="disk-path-branch-header">
          <span>${escapeHtml(controllerName)}</span>
          <strong>${escapeHtml(stateName)}</strong>
          <small>${escapeHtml(mprDevice?.member_device_name || seed.device_name || "")}</small>
        </div>
        <div class="disk-path-flow">
          ${cards.map((card, index) => `${index ? '<span class="disk-path-arrow">-&gt;</span>' : ""}${card}`).join("")}
        </div>
        ${options.includeEvidence === false ? "" : renderDiagnosticEvidencePanel(branchDiagnostics)}
      </section>
    `;
  }

  function renderDiskPathMode(fabric) {
    const trace = selectedDiskTrace(fabric);
    if (!trace) {
      return '<div class="warning-item muted compact">No disk path traces are available for this fabric payload.</div>';
    }
    const slotNumber = sortedSlots(trace.slots)[0];
    const slot = slotByNumber(slotNumber);
    const branches = branchSeedsForDiskTrace(trace, fabric);
    const evidencePanels = branches
      .map((seed) => diskPathBranchEvidence(seed, trace, fabric))
      .filter(Boolean);
    return `
      <div class="disk-path-view">
        <div class="disk-path-picker">
          <div class="disk-path-selected">
            <span class="fabric-stage-title">Selected disk</span>
            <strong>Bay ${escapeHtml(formatSlotLabel(slotNumber))}</strong>
            <small>${escapeHtml([slot?.device_name || trace.metrics?.device_name, slot?.serial, slot?.vdev_name].filter(Boolean).join(" / ") || "No disk metadata")}</small>
          </div>
          <div class="fabric-bay-grid disk-path-bay-grid is-layout">
            ${renderDiskPathBayPicker(fabric, slotNumber)}
          </div>
        </div>
        <div class="disk-path-board">
          ${branches.map((seed) => renderDiskPathBranch(seed, trace, fabric, slotNumber, { includeEvidence: false })).join("")}
        </div>
        ${evidencePanels.length ? `
          <div class="disk-path-evidence-stack">
            ${evidencePanels.join("")}
          </div>
        ` : ""}
      </div>
    `;
  }

  function renderTraceChainForTrace(trace, fabric) {
    const nodes = nodeMap(fabric);
    const links = linkMap(fabric);
    const chain = list(trace.node_ids).map((nodeId) => nodes.get(nodeId)).filter(Boolean);
    const linkRows = list(trace.link_ids).map((linkId) => links.get(linkId)).filter(Boolean);
    return `
      <div class="fabric-trace-grid">
        <div class="fabric-trace-chain">
          ${chain.map((node, index) => `
            <div class="fabric-trace-step">
              <span class="fabric-trace-index">${index + 1}</span>
              ${renderNodeButton(node, { extra: "trace-step-node" })}
            </div>
          `).join("")}
        </div>
        <div class="fabric-link-list">
          <h3>Trace Links</h3>
          ${linkRows.length ? linkRows.map((link) => `
            <button type="button" class="fabric-link-row status-${classToken(link.status)}" data-fabric-node="${escapeHtml(link.target)}">
              <span>${escapeHtml(formatKind(link.kind))}</span>
              <strong>${escapeHtml(link.source)} -> ${escapeHtml(link.target)}</strong>
              <small>${escapeHtml(formatSlots(link.related_slots || trace.slots, 64))}</small>
            </button>
          `).join("") : '<span class="fabric-empty-note">No link rows on this trace yet.</span>'}
        </div>
      </div>
    `;
  }

  function renderTraceBreadcrumbs(fabric) {
    const current = currentSelectionRef();
    const crumbs = state.selectionTrail.filter((ref) => selectionRefExists(ref, fabric));
    if (!current && !crumbs.length) {
      return "";
    }
    const crumbMarkup = crumbs.map((ref, index) => `
      <span class="fabric-trace-separator">/</span>
      <button type="button" class="fabric-trace-crumb" data-fabric-breadcrumb="${index}">
        ${escapeHtml(selectionRefLabel(ref, fabric))}
      </button>
    `).join("");
    const currentMarkup = current ? `
      <span class="fabric-trace-separator">/</span>
      <span class="fabric-trace-crumb current">${escapeHtml(selectionRefLabel(current, fabric))}</span>
    ` : "";
    return `
      <nav class="fabric-trace-breadcrumbs" aria-label="Physical trace breadcrumbs">
        <button type="button" class="fabric-trace-crumb" data-fabric-trace-home>Physical Trace</button>
        ${crumbMarkup}
        ${currentMarkup}
      </nav>
    `;
  }

  function renderTraceMode(fabric) {
    const trace = selectedTrace();
    const node = selectedNode();
    const breadcrumbs = renderTraceBreadcrumbs(fabric);
    if (trace) {
      return `${breadcrumbs}${renderTraceChainForTrace(trace, fabric)}`;
    }
    if (node) {
      const relatedTraces = relatedTracesForNode(node, fabric);
      return `
        ${breadcrumbs}
        <div class="fabric-trace-grid">
          <div class="fabric-trace-chain">
            <div class="fabric-trace-step">
              <span class="fabric-trace-index">1</span>
              ${renderNodeButton(node, { extra: "trace-step-node" })}
            </div>
          </div>
          <div class="fabric-link-list">
            <h3>Related Traces</h3>
            ${relatedTraces.length ? relatedTraces.slice(0, 24).map(renderTraceSummaryButton).join("") : '<span class="fabric-empty-note">No related traces reported.</span>'}
          </div>
        </div>
      `;
    }
    return '<div class="warning-item muted compact">Select a trace or fabric object to render the physical chain.</div>';
  }

  function renderTraceSummaryButton(trace) {
    const selected = state.selectedTraceId === trace.id;
    const visited = selected || traceIsInSelectionTrail(trace.id);
    const attributes = visited
      ? 'disabled aria-disabled="true" data-fabric-trace-disabled="true"'
      : `data-fabric-trace="${escapeHtml(trace.id)}"`;
    const slotText = formatSlots(trace.slots, 18);
    const trailText = visited ? `${slotText} / already in trace` : slotText;
    return `
      <button type="button" class="fabric-trace-summary${selected ? " is-selected" : ""}${visited ? " is-visited" : ""}" ${attributes}>
        <span>${escapeHtml(formatKind(trace.kind))}</span>
        <strong>${escapeHtml(displayLabel(trace) || trace.id)}</strong>
        <small>${escapeHtml(trailText)}</small>
      </button>
    `;
  }

  function aliasScopeLabel(kind) {
    const kindName = String(kind || "").toLowerCase();
    if (["bay", "backplane", "ses-enclosure", "mpr-enclosure", "expander"].includes(kindName)) {
      return "Enclosure";
    }
    return "System";
  }

  function renderAliasRow(label, { objectId, objectKind, item }) {
    if (!objectId) {
      return kvRow(label, "n/a");
    }
    const alias = aliasForObject(objectId);
    const savedLabel = alias?.label || "";
    const fallbackLabel = rawLabel(item) || objectId;
    const currentLabel = displayLabel(item) || fallbackLabel;
    const rawHint = currentLabel !== fallbackLabel ? `<small>Raw: ${escapeHtml(fallbackLabel)}</small>` : "";
    if (state.aliasEditObjectId === objectId) {
      return `
        <form class="kv-row fabric-alias-row is-editing" data-fabric-alias-form>
          <span>${escapeHtml(label)}</span>
          <div class="fabric-alias-editor">
            <input
              type="text"
              value="${escapeHtml(savedLabel)}"
              placeholder="${escapeHtml(fallbackLabel)}"
              maxlength="80"
              autocomplete="off"
              aria-label="Friendly name for ${escapeHtml(fallbackLabel)}"
              data-fabric-alias-input
              data-fabric-alias-object="${escapeHtml(objectId)}"
              data-fabric-alias-kind="${escapeHtml(objectKind || "")}"
              data-fabric-alias-scope="auto"
            >
            <div class="fabric-alias-actions">
              <button type="submit">Save</button>
              <button type="button" data-fabric-alias-clear${savedLabel ? "" : " disabled"}>Clear</button>
              <button type="button" data-fabric-alias-cancel>Cancel</button>
            </div>
            <small>${escapeHtml(aliasScopeLabel(objectKind))} name / raw: ${escapeHtml(fallbackLabel)}</small>
          </div>
        </form>
      `;
    }
    return `
      <div class="kv-row fabric-alias-row">
        <span>${escapeHtml(label)}</span>
        <strong class="fabric-alias-display">
          <span class="fabric-alias-value">${escapeHtml(currentLabel)}</span>
          ${rawHint}
        </strong>
        <button
          type="button"
          class="fabric-alias-edit-button"
          title="Edit friendly name"
          aria-label="Edit friendly name for ${escapeHtml(fallbackLabel)}"
          data-fabric-alias-edit="${escapeHtml(objectId)}"
        >&#9998;</button>
      </div>
    `;
  }

  async function saveAliasFromForm(form, { clear = false } = {}) {
    const input = form.querySelector("[data-fabric-alias-input]");
    if (!(input instanceof HTMLInputElement)) {
      return;
    }
    const objectId = input.dataset.fabricAliasObject || "";
    if (!objectId) {
      return;
    }
    const payload = {
      object_id: objectId,
      object_kind: input.dataset.fabricAliasKind || null,
      scope: input.dataset.fabricAliasScope || "auto",
      label: clear ? "" : input.value.trim(),
    };
    form.classList.add("is-saving");
    try {
      await fetchJson(scopedUrl("/api/sas-fabric/aliases"), {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.aliasEditObjectId = null;
      const fabric = await fetchJson(scopedUrl("/api/sas-fabric"));
      applyFabric(fabric);
      render();
    } catch (error) {
      state.error = error.message || String(error);
      render();
    } finally {
      form.classList.remove("is-saving");
    }
  }

  function renderInspector(fabric) {
    const trace = selectedTrace();
    const node = selectedNode();
    if (!fabric) {
      elements.inspectorTitle.textContent = "Fabric Inspector";
      elements.inspectorBody.innerHTML = "No fabric data loaded.";
      return;
    }
    if (trace) {
      const nodes = nodeMap(fabric);
      const pathStates = list(trace.metrics?.path_states);
      elements.inspectorTitle.textContent = `Selected ${formatKind(trace.kind)}`;
      elements.inspectorBody.innerHTML = `
        <div class="kv-grid">
          ${renderAliasRow("Trace", { objectId: trace.id, objectKind: trace.kind, item: trace })}
          ${kvRow("Type", formatKind(trace.kind))}
          ${kvRow("Slots", formatSlots(trace.slots, 999))}
          ${metricRows(trace.metrics, { state: "State", count: "Affected bays", device_name: "Device", pool_name: "Pool", vdev_name: "Vdev" })}
          ${kvRow("Evidence", trace.evidence)}
        </div>
        ${pathStates.length ? `
          <section class="fabric-inspector-section">
            <h4>Path Members</h4>
            <div class="fabric-state-list">
              ${pathStates.map((pathState) => `
                <div class="fabric-state-row status-${classToken(pathState.state)}">
                  <span>${escapeHtml(pathState.controller || "path")}</span>
                  <strong>${escapeHtml(pathState.state || "unknown")}</strong>
                  <small>${escapeHtml(pathState.device_name || "")}</small>
                </div>
              `).join("")}
            </div>
          </section>
        ` : ""}
        <section class="fabric-inspector-section">
          <h4>Trace Nodes</h4>
          <div class="fabric-node-grid">${renderNodeGrid(list(trace.node_ids).map((nodeId) => nodes.get(nodeId)).filter(Boolean), 18)}</div>
        </section>
      `;
      return;
    }
    if (node) {
      const relatedTraces = relatedTracesForNode(node, fabric);
      elements.inspectorTitle.textContent = `Selected ${formatKind(node.kind)}`;
      elements.inspectorBody.innerHTML = `
        <div class="kv-grid">
          ${renderAliasRow("Object", { objectId: node.id, objectKind: node.kind, item: node })}
          ${kvRow("Type", formatKind(node.kind))}
          ${kvRow("Status", node.status || "n/a")}
          ${kvRow("Raw ID", node.raw_id || "n/a")}
          ${kvRow("Slots", formatSlots(node.related_slots, 999))}
          ${metricRows(node.metrics, { pcie_slot: "PCIe slot", pci_address: "PCI address", linked_phys: "Linked PHYs", num_phys: "PHYs", path_counts: "Path counts" })}
          ${kvRow("Evidence", node.evidence)}
        </div>
        <section class="fabric-inspector-section">
          <h4>Related Traces</h4>
          <div class="fabric-link-list">${relatedTraces.length ? relatedTraces.slice(0, 16).map(renderTraceSummaryButton).join("") : '<span class="fabric-empty-note">No trace rows reported.</span>'}</div>
        </section>
      `;
      return;
    }
    elements.inspectorTitle.textContent = "Fabric Inspector";
    elements.inspectorBody.innerHTML = "Select a controller, path, expander, enclosure, or bay trace.";
  }

  function renderSelectors() {
    const systems = list(state.snapshot.systems);
    const enclosures = list(state.snapshot.enclosures);
    elements.systemSelect.innerHTML = systems.map((system) => {
      const selected = system.id === state.selectedSystemId ? " selected" : "";
      return `<option value="${escapeHtml(system.id)}"${selected}>${escapeHtml(system.label || system.id)}</option>`;
    }).join("");
    elements.systemSelect.value = state.selectedSystemId || "";
    elements.systemSelect.disabled = systems.length <= 1 || state.loading;

    elements.enclosureSelect.innerHTML = enclosures.length
      ? enclosures.map((enclosure) => {
        const selected = enclosure.id === state.selectedEnclosureId ? " selected" : "";
        return `<option value="${escapeHtml(enclosure.id)}"${selected}>${escapeHtml(enclosure.label || enclosure.id)}</option>`;
      }).join("")
      : '<option value="">Auto-selected</option>';
    elements.enclosureSelect.value = state.selectedEnclosureId || "";
    elements.enclosureSelect.disabled = enclosures.length <= 1 || state.loading;
  }

  function renderSummary() {
    const fabric = state.fabric || {};
    elements.summaryControllers.textContent = String(list(fabric.controllers).length);
    elements.summaryPaths.textContent = String(list(fabric.paths).length);
    elements.summaryExpanders.textContent = String(list(fabric.expanders).length);
    elements.summaryEnclosures.textContent = String(list(fabric.enclosures).length);
    elements.summaryTraces.textContent = String(list(fabric.traces).length);
    elements.summaryLinks.textContent = String(list(fabric.links).length);
    elements.lastUpdated.textContent = formatTimestamp(state.snapshot.last_updated || fabric.generated_at);
  }

  function renderStatus() {
    const fabric = state.fabric;
    let className = "status-chip";
    let text = "FABRIC";
    if (state.error) {
      className += " error";
      text = "FABRIC ERR";
      elements.statusText.textContent = state.error;
      elements.statusText.dataset.tone = "error";
    } else if (state.loading) {
      className += " partial";
      text = "FABRIC ...";
      elements.statusText.textContent = fabric ? "Refreshing fabric data." : "Loading fabric data.";
      elements.statusText.dataset.tone = "info";
    } else if (fabric?.available === false) {
      className += " partial";
      text = "FABRIC OFF";
      elements.statusText.textContent = list(fabric.warnings)[0] || "SAS Fabric is not available for this system.";
      elements.statusText.dataset.tone = "error";
    } else if (fabric) {
      className += " ok";
      text = "FABRIC OK";
      elements.statusText.textContent = `Loaded ${list(fabric.traces).length} traces for ${fabric.selected_enclosure_label || fabric.system_label || "current selection"}.`;
      elements.statusText.dataset.tone = "info";
    } else {
      elements.statusText.textContent = "Ready.";
      elements.statusText.dataset.tone = "info";
    }
    elements.apiChip.className = className;
    elements.apiChip.textContent = text;
  }

  function renderWarnings() {
    const warnings = list(state.fabric?.warnings);
    elements.warningList.innerHTML = warnings.length
      ? warnings.map((warning) => {
        const tone = isSasFabricEnrichmentWarning(warning) && state.fabric?.available !== false
          ? "muted compact"
          : "compact";
        return `<div class="warning-item ${tone}">${escapeHtml(warning)}</div>`;
      }).join("")
      : "";
  }

  function isSasFabricEnrichmentWarning(warning) {
    return String(warning || "").toLowerCase().includes("sas fabric enrichment probes");
  }

  function focusButton({ label, title, meta, ref, mode, tone = "" }) {
    if (!ref?.id) {
      return "";
    }
    const selected = selectionRefEquals(ref, currentSelectionRef());
    const refAttr = ref.kind === "node"
      ? `data-fabric-node="${escapeHtml(ref.id)}"`
      : `data-fabric-trace="${escapeHtml(ref.id)}"`;
    return `
      <button
        type="button"
        class="fabric-focus-card${selected ? " is-selected" : ""}${tone ? ` tone-${classToken(tone)}` : ""}"
        data-fabric-mode-target="${escapeHtml(mode || state.mode)}"
        ${refAttr}
      >
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(title || ref.id)}</strong>
        <small>${escapeHtml(meta || "")}</small>
      </button>
    `;
  }

  function renderFocusStrip(fabric) {
    if (!elements.focusStrip || !fabric || fabric.available === false) {
      if (elements.focusStrip) {
        elements.focusStrip.innerHTML = "";
      }
      return;
    }
    const diagnosticNode = bestDiagnosticNode(fabric);
    const diagnostics = kernelDiagnostics(diagnosticNode);
    const defaultPathId = defaultTraceId(fabric);
    const defaultPath = traceById(defaultPathId, fabric);
    const defaultPathRecord = list(fabric.paths).find((path) => path.id === defaultPathId);
    const diskTraceId = defaultDiskTraceId(fabric);
    const diskTrace = traceById(diskTraceId, fabric);
    const diskSlot = sortedSlots(diskTrace?.slots)[0];
    const diskSlotRecord = Number.isInteger(diskSlot) ? slotByNumber(diskSlot) : null;
    const items = [
      diagnosticNode ? focusButton({
        label: "Fault focus",
        title: displayLabel(diagnosticNode) || diagnosticNode.id,
        meta: diagnosticSummary(diagnostics) || "Most diagnostic evidence",
        ref: { kind: "node", id: diagnosticNode.id },
        mode: "trace",
        tone: "error",
      }) : "",
      defaultPath ? focusButton({
        label: "Path focus",
        title: displayLabel(defaultPath) || defaultPath.label || defaultPath.id,
        meta: [
          defaultPathRecord?.state || defaultPath.metrics?.state,
          `${sortedSlots(defaultPath.slots).length} bay${sortedSlots(defaultPath.slots).length === 1 ? "" : "s"}`,
        ].filter(Boolean).join(" / "),
        ref: { kind: "trace", id: defaultPath.id },
        mode: "impact",
        tone: defaultPathRecord?.state || defaultPath.metrics?.state || defaultPath.status,
      }) : "",
      diskTrace ? focusButton({
        label: "Disk path",
        title: `Bay ${formatSlotLabel(diskSlot)}`,
        meta: [diskSlotRecord?.device_name || diskTrace.metrics?.device_name, diskSlotRecord?.vdev_name || diskTrace.metrics?.vdev_name].filter(Boolean).join(" / ") || "Open disk path",
        ref: { kind: "trace", id: diskTrace.id },
        mode: "disk",
        tone: "info",
      }) : "",
    ].filter(Boolean);
    elements.focusStrip.innerHTML = items.length
      ? `<div class="fabric-focus-grid">${items.join("")}</div>`
      : "";
  }

  function renderModeChrome() {
    const copy = modeCopy[state.mode] || modeCopy.lanes;
    elements.mapTitle.textContent = copy.title;
    elements.mapSubtitle.textContent = copy.subtitle;
    elements.modeButtons.forEach((button) => {
      const active = button.dataset.fabricMode === state.mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function renderMap() {
    const fabric = state.fabric;
    if (!fabric) {
      elements.mapPanel.innerHTML = '<div class="warning-item muted compact">No SAS fabric payload has been loaded yet.</div>';
      renderFocusStrip(null);
      renderInspector(null);
      return;
    }
    if (fabric.available === false) {
      elements.mapPanel.innerHTML = '<div class="warning-item muted compact">No topology map is available for this platform yet.</div>';
      renderFocusStrip(fabric);
      renderInspector(fabric);
      return;
    }
    if (state.selectedNodeId && !nodeById(state.selectedNodeId, fabric)) {
      state.selectedNodeId = null;
    }
    if (state.selectedTraceId && !traceById(state.selectedTraceId, fabric)) {
      state.selectedTraceId = null;
    }
    ensureSelectionForMode(fabric);
    pruneSelectionTrail(fabric);
    renderFocusStrip(fabric);
    if (state.mode === "impact") {
      elements.mapPanel.innerHTML = renderImpactMode(fabric);
    } else if (state.mode === "trace") {
      elements.mapPanel.innerHTML = renderTraceMode(fabric);
    } else if (state.mode === "disk") {
      elements.mapPanel.innerHTML = renderDiskPathMode(fabric);
    } else {
      elements.mapPanel.innerHTML = renderLanesMode(fabric);
    }
    renderInspector(fabric);
  }

  function render() {
    renderSelectors();
    renderSummary();
    renderStatus();
    renderWarnings();
    renderModeChrome();
    renderMap();
    syncLocation();
    elements.refreshButton.disabled = state.loading;
  }

  function applySnapshot(snapshot) {
    state.snapshot = snapshot || state.snapshot;
    state.selectedSystemId = state.snapshot.selected_system_id || state.selectedSystemId;
    state.selectedEnclosureId = state.snapshot.selected_enclosure_id || state.selectedEnclosureId;
  }

  function applyFabric(fabric) {
    state.fabric = fabric || null;
    state.error = null;
    if (state.selectedNodeId && !nodeById(state.selectedNodeId, fabric)) {
      state.selectedNodeId = null;
    }
    if (state.selectedTraceId && !traceById(state.selectedTraceId, fabric)) {
      state.selectedTraceId = null;
    }
    ensureSelectionForMode(fabric);
    pruneSelectionTrail(fabric);
  }

  async function refreshFabric(force = false) {
    state.loading = true;
    state.error = null;
    render();
    try {
      const snapshot = await fetchJson(scopedUrl("/api/inventory", { force }));
      applySnapshot(snapshot);
      const fabric = await fetchJson(scopedUrl("/api/sas-fabric", { force }));
      applyFabric(fabric);
    } catch (error) {
      state.error = error.message || String(error);
    } finally {
      state.loading = false;
      render();
    }
  }

  elements.modeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.dataset.fabricMode;
      if (!modeIds.has(mode)) {
        return;
      }
      const current = currentSelectionRef();
      const forceSelection = (mode === "disk" && current?.kind !== "trace")
        || (mode === "disk" && traceById(current?.id)?.kind !== "bay")
        || (mode === "impact" && (current?.kind !== "trace" || traceById(current?.id)?.kind !== "path"));
      state.mode = mode;
      state.aliasEditObjectId = null;
      ensureSelectionForMode(state.fabric, { force: forceSelection, mode });
      render();
    });
  });

  elements.refreshButton.addEventListener("click", () => {
    void refreshFabric(true);
  });

  elements.systemSelect.addEventListener("change", () => {
    state.selectedSystemId = elements.systemSelect.value || null;
    state.selectedEnclosureId = null;
    state.selectedTraceId = null;
    state.selectedNodeId = null;
    state.selectionTrail = [];
    state.expandedSlotLists = {};
    state.diagnosticTables = {};
    state.aliasEditObjectId = null;
    void refreshFabric(false);
  });

  elements.enclosureSelect.addEventListener("change", () => {
    state.selectedEnclosureId = elements.enclosureSelect.value || null;
    state.selectedTraceId = null;
    state.selectedNodeId = null;
    state.selectionTrail = [];
    state.expandedSlotLists = {};
    state.diagnosticTables = {};
    state.aliasEditObjectId = null;
    void refreshFabric(false);
  });

  function handleFabricActivation(target) {
    if (!target) {
      return;
    }
    const modeTargetButton = target.closest("[data-fabric-mode-target]");
    if (modeTargetButton) {
      const mode = modeTargetButton.dataset.fabricModeTarget;
      if (modeIds.has(mode)) {
        state.mode = mode;
      }
      state.aliasEditObjectId = null;
      state.selectionTrail = [];
      const traceId = modeTargetButton.dataset.fabricTrace || "";
      const nodeId = modeTargetButton.dataset.fabricNode || "";
      if (traceId) {
        setSelectionRef({ kind: "trace", id: traceId }, { pushTrail: false });
      } else if (nodeId) {
        setSelectionRef({ kind: "node", id: nodeId }, { pushTrail: false });
      } else {
        ensureSelectionForMode(state.fabric, { force: true });
      }
      render();
      return;
    }
    const aliasEditButton = target.closest("[data-fabric-alias-edit]");
    if (aliasEditButton) {
      state.aliasEditObjectId = aliasEditButton.dataset.fabricAliasEdit || null;
      render();
      window.requestAnimationFrame(() => {
        const input = document.querySelector("[data-fabric-alias-input]");
        if (input instanceof HTMLInputElement) {
          input.focus();
          input.select();
        }
      });
      return;
    }
    const aliasCancelButton = target.closest("[data-fabric-alias-cancel]");
    if (aliasCancelButton) {
      state.aliasEditObjectId = null;
      render();
      return;
    }
    const aliasClearButton = target.closest("[data-fabric-alias-clear]");
    if (aliasClearButton) {
      const form = aliasClearButton.closest("[data-fabric-alias-form]");
      if (form instanceof HTMLFormElement) {
        void saveAliasFromForm(form, { clear: true });
      }
      return;
    }
    const diagnosticPageButton = target.closest("[data-fabric-diagnostic-page]");
    if (diagnosticPageButton) {
      const key = diagnosticPageButton.dataset.fabricDiagnosticKey || "";
      const tableState = diagnosticTableState(key);
      const action = diagnosticPageButton.dataset.fabricDiagnosticPage || "1";
      if (action === "prev") {
        tableState.page = Math.max(1, Number(tableState.page || 1) - 1);
      } else if (action === "next") {
        tableState.page = Number(tableState.page || 1) + 1;
      } else {
        tableState.page = Math.max(1, Number(action) || 1);
      }
      tableState.open = true;
      render();
      return;
    }
    const expandButton = target.closest("[data-fabric-expand-slots]");
    if (expandButton) {
      const key = expandButton.dataset.fabricExpandSlots || "";
      state.expandedSlotLists[key] = !state.expandedSlotLists[key];
      render();
      return;
    }
    const traceHomeButton = target.closest("[data-fabric-trace-home]");
    if (traceHomeButton) {
      state.selectionTrail = [];
      state.selectedNodeId = null;
      state.selectedTraceId = resolveTraceId(null);
      state.aliasEditObjectId = null;
      render();
      return;
    }
    const breadcrumbButton = target.closest("[data-fabric-breadcrumb]");
    if (breadcrumbButton) {
      const index = Number(breadcrumbButton.dataset.fabricBreadcrumb);
      const ref = Number.isInteger(index) ? state.selectionTrail[index] : null;
      if (setSelectionRef(ref, { pushTrail: false })) {
        state.selectionTrail = state.selectionTrail.slice(0, index);
        render();
      }
      return;
    }
    const traceButton = target.closest("[data-fabric-trace]");
    if (traceButton) {
      const traceId = traceButton.dataset.fabricTrace || "";
      if (setSelectionRef({ kind: "trace", id: traceId })) {
        if (state.mode === "trace") {
          traceButton.scrollIntoView({ block: "nearest" });
        }
        render();
      }
      return;
    }
    const nodeButton = target.closest("[data-fabric-node]");
    if (nodeButton) {
      const nodeId = nodeButton.dataset.fabricNode || "";
      if (setSelectionRef({ kind: "node", id: nodeId })) {
        render();
      }
    }
  }

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    handleFabricActivation(target);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target instanceof HTMLFormElement ? event.target : null;
    if (!form?.matches("[data-fabric-alias-form]")) {
      return;
    }
    event.preventDefault();
    void saveAliasFromForm(form);
  });

  document.addEventListener("input", (event) => {
    const target = event.target instanceof HTMLInputElement ? event.target : null;
    if (!target?.matches("[data-fabric-diagnostic-filter-key]")) {
      return;
    }
    const key = target.dataset.fabricDiagnosticFilterKey || "";
    const tableState = diagnosticTableState(key);
    tableState.filter = target.value || "";
    tableState.page = 1;
    tableState.open = true;
    const selectionStart = target.selectionStart;
    render();
    window.requestAnimationFrame(() => {
      const nextInput = document.querySelector(`[data-fabric-diagnostic-filter-key="${key}"]`);
      if (nextInput instanceof HTMLInputElement) {
        nextInput.focus();
        if (Number.isInteger(selectionStart)) {
          nextInput.setSelectionRange(selectionStart, selectionStart);
        }
      }
    });
  });

  document.addEventListener("change", (event) => {
    const target = event.target instanceof HTMLSelectElement ? event.target : null;
    if (!target?.matches("[data-fabric-diagnostic-type-key]")) {
      return;
    }
    const key = target.dataset.fabricDiagnosticTypeKey || "";
    const tableState = diagnosticTableState(key);
    tableState.type = target.value || "all";
    tableState.page = 1;
    tableState.open = true;
    render();
  });

  document.addEventListener("toggle", (event) => {
    const details = event.target instanceof HTMLDetailsElement ? event.target : null;
    if (!details?.matches("[data-fabric-diagnostic-table-key]")) {
      return;
    }
    const key = details.dataset.fabricDiagnosticTableKey || "";
    diagnosticTableState(key).open = details.open;
  }, true);

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    if (!target?.closest("[data-fabric-mode-target], [data-fabric-diagnostic-page], [data-fabric-expand-slots], [data-fabric-trace-home], [data-fabric-breadcrumb], [data-fabric-trace], [data-fabric-node]")) {
      return;
    }
    event.preventDefault();
    handleFabricActivation(target);
  });

  ensureSelectionForMode(state.fabric);
  render();
  if (!state.fabric) {
    void refreshFabric(false);
  }
})();
