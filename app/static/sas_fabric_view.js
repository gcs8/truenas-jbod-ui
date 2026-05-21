(function () {
  const bootstrap = window.SAS_FABRIC_BOOTSTRAP || {};
  const modeIds = new Set(["lanes", "impact", "trace", "disk"]);
  const modeCopy = {
    lanes: {
      title: "Storage Lanes",
      subtitle: "Top-down source lanes with paths, transport details, enclosures, views, and mapped bays aligned in each lane.",
    },
    impact: {
      title: "Impact Map",
      subtitle: "Start from paths and degraded states, then show affected slots, pools, vdevs, and trace hops.",
    },
    trace: {
      title: "Physical Trace",
      subtitle: "Follow the selected component or bay through host, source, path, enclosure or view, pool, and disk layers.",
    },
    disk: {
      title: "Disk Path",
      subtitle: "Pick a bay and render the available path evidence from host to source, enclosure or view, pool, vdev, and disk.",
    },
  };
  const coreModeCopy = {
    lanes: {
      title: "Storage Lanes",
      subtitle: "Top-down HBA lanes with paths, expanders, enclosures, and impacted bays aligned in each lane.",
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
    selectedDiskTraceId: null,
    selectionTrail: [],
    expandedSlotLists: {},
    diagnosticTables: {},
    smartSummaries: {},
    smartRequests: {},
    aliasEditObjectId: null,
    mode: modeIds.has(initialMode) ? initialMode : "lanes",
    loading: false,
    error: null,
  };

  const elements = {
    systemSelect: document.getElementById("fabric-system-select"),
    enclosureSelect: document.getElementById("fabric-enclosure-select"),
    refreshButton: document.getElementById("fabric-refresh-button"),
    pageEyebrow: document.getElementById("fabric-page-eyebrow"),
    pageSummary: document.getElementById("fabric-page-summary"),
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

  function fabricKind(fabric = state.fabric) {
    if (fabric?.raw?.fabric_kind) {
      return fabric.raw.fabric_kind;
    }
    return fabric?.platform === "core" ? "core_sas" : "unknown";
  }

  function fabricPlatformLabel(fabric = state.fabric) {
    const platform = String(fabric?.platform || state.snapshot?.selected_system_platform || "").toLowerCase();
    if (platform === "scale") {
      return "TrueNAS SCALE";
    }
    if (platform === "linux") {
      return "Linux";
    }
    if (platform === "core") {
      return "TrueNAS CORE";
    }
    if (platform === "quantastor") {
      return "Quantastor";
    }
    if (platform === "esxi") {
      return "ESXi";
    }
    if (platform === "ipmi") {
      return "BMC / IPMI";
    }
    return platform ? formatKind(platform) : "System";
  }

  function fabricViewCopy(fabric = state.fabric) {
    const platformLabel = fabricPlatformLabel(fabric);
    const kind = fabricKind(fabric);
    const linuxSes = kind === "linux_ses";
    if (linuxSes) {
      return {
        kind: "linux_ses",
        storageFabric: true,
        eyebrow: `${platformLabel} / Storage Fabric`,
        pageSummary: "Linux SES SG-device, enclosure, storage, and bay mapping for the selected live enclosure.",
        refresh: "Refresh Storage Fabric",
        statusBase: "STORAGE",
        unavailableMap: "No Linux SES-backed Storage Fabric map is available for this selection yet.",
        modeLabels: {
          lanes: "Storage Lanes",
          impact: "Impact Map",
          trace: "Physical Trace",
          disk: "Disk Path",
        },
        modes: {
          lanes: {
            title: "Storage Lanes",
            subtitle: "Host, Linux SES source, SG enclosure devices, storage objects, and mapped bays aligned by enclosure path.",
          },
          impact: {
            title: "Impact Map",
            subtitle: "Show mapped SES device groups, affected slots, pools, vdevs, and trace hops.",
          },
          trace: {
            title: "Physical Trace",
            subtitle: "Follow the selected SG device or bay through host, Linux SES source, enclosure, backplane zone, and disk layers.",
          },
          disk: {
            title: "Disk Path",
            subtitle: "Pick a bay and render the read-only Linux SES path from host to SG enclosure, backplane zone, and disk.",
          },
        },
        summary: {
          controllers: ["Sources", "Linux SES source"],
          paths: ["Storage Paths", "SG device groups"],
          expanders: ["Transport Detail", "not exposed"],
          enclosures: ["Enclosures", "SG enclosure objects"],
        },
        laneStages: {
          controller: "Source",
          paths: "SES Paths",
          expanders: "Transport Detail",
          enclosures: "SG Enclosures",
          bays: "Mapped Bays",
        },
        hostControllerNoun: "source",
      };
    }
    if (String(kind || "").startsWith("storage_") || fabric?.raw?.fabric_domain === "storage_fabric") {
      const storageCopyByKind = {
        storage_quantastor: {
          pageSummary: "Quantastor HA-node, enclosure, pool, disk, and ownership/fence evidence for the selected storage view.",
          unavailableMap: "No Quantastor Storage Fabric evidence is available for this selection yet.",
          controllers: ["Sources", "storage system / HA node"],
          paths: ["Storage Paths", "pool, SES, or owner groups"],
          enclosures: ["Views", "Quantastor / SES objects"],
          diskLabels: {
            host: "Cluster",
            source: "HA Node",
            path: "SES / Pool Path",
            enclosure: "Storage View",
            backplane: "Bay Group",
            disk: "Disk",
          },
          modes: {
            ...modeCopy,
            lanes: {
              title: "Storage Lanes",
              subtitle: "Quantastor HA nodes, SES paths, storage views, pool/vdev membership, and mapped bays grouped by source evidence.",
            },
            trace: {
              title: "Physical Trace",
              subtitle: "Follow the selected bay through HA ownership, SES path evidence, storage view, pool/vdev membership, and disk identity.",
            },
            disk: {
              title: "Disk Path",
              subtitle: "Pick a bay and show the Quantastor path evidence we can prove: HA node, SES device, view, pool, vdev, and disk.",
            },
          },
        },
        storage_esxi: {
          pageSummary: "ESXi host, controller, member, datastore, LUN, and SMART evidence for the selected local storage view.",
          unavailableMap: "No ESXi Storage Fabric evidence is available for this selection yet.",
          controllers: ["Sources", "ESXi / vendor CLI"],
          paths: ["Storage Paths", "controller/member groups"],
          enclosures: ["Enclosures", "vendor/BMC/profile objects"],
          diskLabels: {
            host: "ESXi Host",
            source: "Controller",
            path: "Member Path",
            enclosure: "View / Enclosure",
            backplane: "Slot Group",
            disk: "Disk",
          },
          modes: {
            ...modeCopy,
            disk: {
              title: "Disk Path",
              subtitle: "Pick a bay and show the ESXi evidence we can prove: host, controller, member path, enclosure/profile, datastore or vdev, and disk.",
            },
          },
        },
        storage_linux: {
          pageSummary: "Linux block, NVMe, mdadm, profile, SMART, and optional SES evidence for the selected storage view.",
          unavailableMap: "No Linux Storage Fabric evidence is available for this selection yet.",
          controllers: ["Sources", "Linux storage source"],
          paths: ["Storage Paths", "block, NVMe, or mdadm groups"],
          enclosures: ["Views", "profile or enclosure objects"],
          diskLabels: {
            host: "Host",
            source: "Storage Source",
            path: "Block Path",
            enclosure: "View",
            backplane: "Slot Group",
            disk: "Device",
          },
        },
        storage_scale: {
          pageSummary: "TrueNAS SCALE storage, pool, disk, Linux block, and optional SES evidence for the selected view.",
          unavailableMap: "No SCALE Storage Fabric evidence is available for this selection yet.",
          controllers: ["Sources", "SCALE / Linux storage"],
          paths: ["Storage Paths", "pool, block, or SES groups"],
          enclosures: ["Enclosures", "middleware/profile objects"],
        },
        storage_bmc: {
          pageSummary: "BMC slot and chassis evidence for the selected platform view.",
          unavailableMap: "No BMC Storage Fabric evidence is available for this selection yet.",
          controllers: ["Sources", "BMC / IPMI"],
          paths: ["Storage Paths", "slot inventory groups"],
          enclosures: ["Enclosures", "BMC chassis objects"],
        },
      };
      const details = storageCopyByKind[kind] || {
        pageSummary: "Best-effort storage path evidence for the selected platform view.",
        unavailableMap: `No Storage Fabric evidence is available for ${platformLabel}.`,
        controllers: ["Sources", "platform evidence"],
        paths: ["Storage Paths", "platform groups"],
        enclosures: ["Enclosures", "platform objects"],
      };
      return {
        kind,
        storageFabric: true,
        eyebrow: `${platformLabel} / Storage Fabric`,
        pageSummary: details.pageSummary,
        refresh: "Refresh Storage Fabric",
        statusBase: "STORAGE",
        unavailableMap: details.unavailableMap,
        modeLabels: {
          lanes: "Storage Lanes",
          impact: "Impact Map",
          trace: "Physical Trace",
          disk: "Disk Path",
        },
        modes: details.modes || modeCopy,
        diskLabels: details.diskLabels || null,
        summary: {
          controllers: details.controllers,
          paths: details.paths,
          expanders: ["Transport Detail", "platform-native evidence"],
          enclosures: details.enclosures,
        },
        laneStages: {
          controller: "Source",
          paths: "Storage Paths",
          expanders: "Transport Detail",
          enclosures: "Enclosures / Views",
          bays: "Mapped Bays",
        },
        hostControllerNoun: "source",
      };
    }
    if (kind === "linux_no_ses" || kind === "platform_unsupported" || (fabric?.available === false && fabric?.platform && fabric.platform !== "core")) {
      return {
        kind: "storage_unavailable",
        storageFabric: true,
        eyebrow: `${platformLabel} / Storage Fabric`,
        pageSummary: `Storage Fabric evidence is not available for ${platformLabel} in this snapshot.`,
        refresh: "Refresh Storage Fabric",
        statusBase: "STORAGE",
        unavailableMap: `No Storage Fabric topology map is available for ${platformLabel}.`,
        modeLabels: {
          lanes: "Storage Lanes",
          impact: "Impact Map",
          trace: "Physical Trace",
          disk: "Disk Path",
        },
        modes: modeCopy,
        summary: {
          controllers: ["Sources", "no graph evidence"],
          paths: ["Storage Paths", "no graph evidence"],
          expanders: ["Transport Detail", "not exposed"],
          enclosures: ["Enclosures", "platform inventory only"],
        },
        laneStages: {
          controller: "Source",
          paths: "Storage Paths",
          expanders: "Transport Detail",
          enclosures: "Enclosures / Views",
          bays: "Mapped Bays",
        },
        hostControllerNoun: "source",
      };
    }
    return {
      kind: "core_sas",
      eyebrow: "TrueNAS CORE / Storage Fabric",
      pageSummary: "HBA, path, expander, SES, and affected-bay topology for the selected live enclosure.",
      refresh: "Refresh Storage Fabric",
      statusBase: "STORAGE",
      unavailableMap: "No topology map is available for this platform yet.",
      modeLabels: {
        lanes: "Storage Lanes",
        impact: "Impact Map",
        trace: "Physical Trace",
        disk: "Disk Path",
      },
      modes: coreModeCopy,
      diskLabels: {
        host: "Host",
        source: "HBA",
        path: "SAS Link",
        enclosure: "Expander / SES",
        backplane: "Backplane",
        disk: "Disk",
      },
      summary: {
        controllers: ["Controllers", "HBAs reported"],
        paths: ["Paths", "multipath states"],
        expanders: ["Expanders", "MPR expander rows"],
        enclosures: ["Enclosures", "MPR/SES objects"],
      },
      laneStages: {
        controller: "Controller",
        paths: "Paths",
        expanders: "Expanders",
        enclosures: "SES / MPR Enclosures",
        bays: "Impacted Bays",
      },
      hostControllerNoun: "controller",
    };
  }

  function setSummaryCardText(valueElement, label, note) {
    const card = valueElement?.closest(".summary-card");
    if (!card) {
      return;
    }
    const labelElement = card.querySelector(".summary-label");
    const noteElement = card.querySelector(".summary-note");
    if (labelElement && label) {
      labelElement.textContent = label;
    }
    if (noteElement && note) {
      noteElement.textContent = note;
    }
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
      "storage-enclosure": 6,
      backplane: 7,
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

  function evidenceChips(values) {
    const chips = list(values)
      .filter(Boolean)
      .map((value) => `<span class="fabric-evidence-chip">${escapeHtml(formatValue(value))}</span>`)
      .join("");
    return chips || '<span class="fabric-empty-note">No source evidence listed.</span>';
  }

  function inspectorFact(label, value, { tone = "", wide = false } = {}) {
    const formatted = formatValue(value);
    if (formatted === "n/a") {
      return "";
    }
    return `
      <div class="fabric-inspector-fact${tone ? ` tone-${classToken(tone)}` : ""}${wide ? " is-wide" : ""}">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(formatted)}</strong>
      </div>
    `;
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
      rememberDiskTrace(ref.id);
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

  function rememberDiskTrace(traceId, fabric = state.fabric) {
    const trace = traceById(traceId, fabric);
    if (trace?.kind === "bay" && sortedSlots(trace.slots).length) {
      state.selectedDiskTraceId = trace.id;
      return trace;
    }
    return null;
  }

  function rememberedDiskTrace(fabric = state.fabric) {
    const trace = rememberDiskTrace(state.selectedDiskTraceId, fabric);
    if (!trace) {
      state.selectedDiskTraceId = null;
    }
    return trace;
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
      rememberDiskTrace(ref.id);
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

  function smartCacheKey(slotNumber) {
    return [state.selectedSystemId || "", state.selectedEnclosureId || "", String(slotNumber)].join("|");
  }

  function smartSummaryForSlot(slotNumber) {
    if (!Number.isInteger(Number(slotNumber))) {
      return null;
    }
    return state.smartSummaries[smartCacheKey(slotNumber)] || null;
  }

  function selectedSmartSlotNumber() {
    const trace = selectedDiskTrace(state.fabric);
    const slotNumber = sortedSlots(trace?.slots)[0];
    return Number.isInteger(slotNumber) ? slotNumber : null;
  }

  function formatInteger(value) {
    const numberValue = Number(value);
    return Number.isFinite(numberValue) ? numberValue.toLocaleString() : "";
  }

  function formatSmartBytes(value) {
    const numberValue = Number(value);
    if (!Number.isFinite(numberValue) || numberValue <= 0) {
      return "";
    }
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let scaled = numberValue;
    let unitIndex = 0;
    while (scaled >= 1000 && unitIndex < units.length - 1) {
      scaled /= 1000;
      unitIndex += 1;
    }
    return `${scaled >= 10 ? scaled.toFixed(1) : scaled.toFixed(2)} ${units[unitIndex]}`;
  }

  function formatSmartPowerOn(summary) {
    if (!summary) {
      return "";
    }
    if (Number.isFinite(Number(summary.power_on_days))) {
      return `${formatInteger(summary.power_on_days)} d`;
    }
    if (Number.isFinite(Number(summary.power_on_hours))) {
      return `${formatInteger(summary.power_on_hours)} h`;
    }
    return "";
  }

  function smartSummaryLine(summary) {
    if (!summary) {
      return "";
    }
    if (summary.available === false) {
      return summary.message || "SMART unavailable";
    }
    return [
      summary.smart_health_status,
      summary.temperature_c != null ? `${summary.temperature_c} C` : null,
      formatSmartPowerOn(summary),
    ].filter(Boolean).join(" / ");
  }

  function renderSmartSummaryCard(slotNumber, slot) {
    const summary = smartSummaryForSlot(slotNumber);
    const key = smartCacheKey(slotNumber);
    const loading = state.smartRequests[key];
    if (!summary && loading) {
      return '<div class="fabric-identity-card"><span>Loading selected-disk SMART...</span></div>';
    }
    if (!summary) {
      return '<div class="fabric-identity-card"><span class="fabric-empty-note">SMART detail has not loaded yet.</span></div>';
    }
    if (summary.available === false) {
      return `<div class="fabric-identity-card"><span class="fabric-empty-note">${escapeHtml(summary.message || "SMART detail is unavailable for this disk.")}</span></div>`;
    }
    const lastTest = [summary.last_test_type, summary.last_test_status].filter(Boolean).join(" / ");
    const ioTotals = [
      formatSmartBytes(summary.bytes_read) ? `R ${formatSmartBytes(summary.bytes_read)}` : null,
      formatSmartBytes(summary.bytes_written) ? `W ${formatSmartBytes(summary.bytes_written)}` : null,
    ].filter(Boolean).join(" / ");
    const errorCounts = [
      summary.media_errors != null ? `media ${formatInteger(summary.media_errors)}` : null,
      summary.non_medium_errors != null ? `non-medium ${formatInteger(summary.non_medium_errors)}` : null,
      summary.interface_crc_errors != null ? `CRC ${formatInteger(summary.interface_crc_errors)}` : null,
    ].filter(Boolean).join(" / ");
    const transport = [
      summary.transport_protocol || slot?.transport_protocol,
      summary.negotiated_link_rate,
      summary.sas_address || slot?.sas_address,
    ].filter(Boolean).join(" / ");
    return `
      <div class="fabric-identity-card fabric-smart-card">
        <div>
          ${summary.smart_health_status ? `<span><em>Health</em>${escapeHtml(summary.smart_health_status)}</span>` : ""}
          ${summary.temperature_c != null ? `<span><em>Temp</em>${escapeHtml(`${summary.temperature_c} C`)}</span>` : ""}
          ${formatSmartPowerOn(summary) ? `<span><em>Power on</em>${escapeHtml(formatSmartPowerOn(summary))}</span>` : ""}
          ${lastTest ? `<span><em>Last test</em>${escapeHtml(lastTest)}</span>` : ""}
          ${ioTotals ? `<span><em>I/O totals</em>${escapeHtml(ioTotals)}</span>` : ""}
          ${errorCounts ? `<span><em>Errors</em>${escapeHtml(errorCounts)}</span>` : ""}
          ${transport ? `<span><em>Transport</em>${escapeHtml(transport)}</span>` : ""}
          ${summary.firmware_version ? `<span><em>Firmware</em>${escapeHtml(summary.firmware_version)}</span>` : ""}
        </div>
      </div>
    `;
  }

  function ensureSelectedSmartSummary() {
    const slotNumber = selectedSmartSlotNumber();
    if (!Number.isInteger(slotNumber) || !state.fabric || state.fabric.available === false) {
      return;
    }
    const slot = slotByNumber(slotNumber);
    if (!slot || (!slot.device_name && !list(slot.smart_device_names).length && !slot.serial)) {
      return;
    }
    const key = smartCacheKey(slotNumber);
    if (state.smartSummaries[key] || state.smartRequests[key]) {
      return;
    }
    state.smartRequests[key] = true;
    fetchJson(scopedUrl(`/api/slots/${slotNumber}/smart`))
      .then((summary) => {
        state.smartSummaries[key] = summary || { available: false, message: "SMART detail returned an empty payload." };
      })
      .catch((error) => {
        state.smartSummaries[key] = { available: false, message: error.message || String(error) };
      })
      .finally(() => {
        delete state.smartRequests[key];
        if (smartCacheKey(selectedSmartSlotNumber()) === key) {
          render();
        }
      });
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

  function renderBayChips(slots, limit = 96, { modeTarget = "" } = {}) {
    const sorted = sortedSlots(slots);
    if (!sorted.length) {
      return '<span class="fabric-empty-note">No mapped bays</span>';
    }
    const activeSlots = selectedSlots();
    const chips = sorted.slice(0, limit).map((slotNumber) => {
      const selected = activeSlots.has(slotNumber);
      const slot = slotByNumber(slotNumber);
      const title = [slot?.device_name, slot?.pool_name, slot?.vdev_name].filter(Boolean).join(" / ");
      const modeTargetAttribute = modeIds.has(modeTarget)
        ? ` data-fabric-mode-target="${escapeHtml(modeTarget)}"`
        : "";
      return `
        <button type="button" class="fabric-bay-chip${selected ? " is-selected" : ""}" data-fabric-trace="bay:${slotNumber}"${modeTargetAttribute} title="${escapeHtml(title || `Bay ${formatSlotLabel(slotNumber)}`)}">
          ${escapeHtml(formatSlotLabel(slotNumber))}
        </button>
      `;
    }).join("");
    const overflow = sorted.length > limit ? `<span class="fabric-empty-note">+${sorted.length - limit} bays</span>` : "";
    return `${chips}${overflow}`;
  }

  function renderLanesMode(fabric) {
    const copy = fabricViewCopy(fabric);
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
      const enclosures = sortFabricNodesForLane(list(fabric.nodes).filter((node) => (
        ["mpr-enclosure", "ses-enclosure", "storage-enclosure"].includes(node.kind) && node.controller_id === controllerId
      )));
      const laneSlots = new Set(sortedSlots(controller.related_slots || controllerNode?.related_slots));
      paths.forEach((path) => sortedSlots(path.slots).forEach((slot) => laneSlots.add(slot)));
      expanders.forEach((node) => sortedSlots(node.related_slots).forEach((slot) => laneSlots.add(slot)));
      enclosures.forEach((node) => sortedSlots(node.related_slots).forEach((slot) => laneSlots.add(slot)));
      const slots = Array.from(laneSlots).sort((left, right) => left - right);
      const related = selectionTouchesNode(controllerId) || selectionTouchesSlots(slots);
      return `
        <section class="fabric-dedicated-lane${related ? " is-related" : ""}">
          <div class="fabric-stage controller">
            <span class="fabric-stage-title">${escapeHtml(copy.laneStages.controller)}</span>
            ${renderNodeButton(controllerNode || { id: controllerId, kind: "controller", label: controllerName, related_slots: slots, status: "unknown", metrics: {} }, {
              label: controller.display_label || controller.alias || displayLabel(controllerNode) || controllerName,
              meta: nodeMeta(controllerNode, controller) || controller.device || copy.laneStages.controller,
            })}
          </div>
          <div class="fabric-stage">
            <span class="fabric-stage-title">${escapeHtml(copy.laneStages.paths)}</span>
            <div class="fabric-path-grid">${paths.length ? paths.map((path) => renderPathButton(path, { compact: true })).join("") : '<span class="fabric-empty-note">No path states reported</span>'}</div>
          </div>
          <div class="fabric-stage">
            <span class="fabric-stage-title">${escapeHtml(copy.laneStages.expanders)}</span>
            <div class="fabric-node-grid">${renderNodeGrid(expanders, 8)}</div>
          </div>
          <div class="fabric-stage">
            <span class="fabric-stage-title">${escapeHtml(copy.laneStages.enclosures)}</span>
            <div class="fabric-node-grid">${renderNodeGrid(enclosures, 8)}</div>
          </div>
          <div class="fabric-stage">
            <span class="fabric-stage-title">${escapeHtml(copy.laneStages.bays)}</span>
            <div class="fabric-bay-grid">${renderBayChips(slots, 120)}</div>
          </div>
        </section>
      `;
    }).join("");
    return `
      <div class="fabric-host-strip">
        ${renderNodeButton(hostNode, {
          label: displayLabel(hostNode) || "Host",
          meta: `${controllerRecords.length} ${copy.hostControllerNoun}${controllerRecords.length === 1 ? "" : "s"} / ${list(fabric.nodes).length} nodes / ${list(fabric.links).length} links`,
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
              <div class="fabric-bay-grid compact">${renderBayChips(slots, 80, { modeTarget: "disk" })}</div>
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
      rememberDiskTrace(trace.id, fabric);
      return trace;
    }
    const node = selectedNode();
    if (node?.kind === "bay") {
      const baySlot = Number.isInteger(Number(node.slot)) ? Number(node.slot) : sortedSlots(node.related_slots)[0];
      const bayTrace = tracesById.get(`bay:${baySlot}`);
      if (bayTrace) {
        rememberDiskTrace(bayTrace.id, fabric);
        return bayTrace;
      }
    }
    const rememberedTrace = rememberedDiskTrace(fabric);
    if (rememberedTrace) {
      return rememberedTrace;
    }
    const candidateSlots = sortedSlots(trace?.slots || node?.related_slots);
    for (const slotNumber of candidateSlots) {
      const bayTrace = tracesById.get(`bay:${slotNumber}`);
      if (bayTrace) {
        rememberDiskTrace(bayTrace.id, fabric);
        return bayTrace;
      }
    }
    const fallbackTrace = traces.find((traceItem) => traceItem.kind === "bay" && diskTraceHasDisk(traceItem))
      || traces.find((traceItem) => traceItem.kind === "bay")
      || null;
    if (fallbackTrace) {
      rememberDiskTrace(fallbackTrace.id, fabric);
    }
    return fallbackTrace;
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

  const DIAGNOSTIC_IMPACT_META = {
    error: {
      label: "Fault",
      glyph: "!",
      title: "Path-impacting fault or failed command",
    },
    warning: {
      label: "Warning",
      glyph: "~",
      title: "Warning, recovery, or non-critical condition",
    },
    info: {
      label: "Context",
      glyph: "i",
      title: "Supporting command or diagnostic context",
    },
  };

  const DIAGNOSTIC_CONFIDENCE_LABELS = {
    standard: "T10 standard",
    "standard-partial": "T10 partial",
    "vendor-reference": "Vendor ref",
    "vendor-reference-partial": "Vendor partial",
    observed: "Observed",
    unconfirmed: "Unconfirmed",
    none: "No decode",
  };

  function diagnosticSeverity(value) {
    const severity = classToken(value?.severity || value || "info");
    return DIAGNOSTIC_IMPACT_META[severity] ? severity : "info";
  }

  function diagnosticSeverityLabel(value) {
    const severity = diagnosticSeverity(value);
    return DIAGNOSTIC_IMPACT_META[severity].label;
  }

  function diagnosticImpactBadge(value) {
    const severity = diagnosticSeverity(value);
    const meta = DIAGNOSTIC_IMPACT_META[severity];
    return `
      <span class="fabric-diagnostic-impact severity-${escapeHtml(severity)}" title="${escapeHtml(meta.title)}">
        <b aria-hidden="true">${escapeHtml(meta.glyph)}</b>
        <span>${escapeHtml(meta.label)}</span>
      </span>
    `;
  }

  function diagnosticConfidenceValue(eventOrValue) {
    const confidence = typeof eventOrValue === "string"
      ? eventOrValue
      : eventOrValue?.decode_confidence || eventOrValue?.decoded?.decode_confidence;
    return confidence ? classToken(confidence) : "none";
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
    const confidence = diagnosticConfidenceValue(event);
    return DIAGNOSTIC_CONFIDENCE_LABELS[confidence] || confidence.replace(/[-_]/g, " ");
  }

  function diagnosticImpactCounts(rows) {
    const counts = { error: 0, warning: 0, info: 0 };
    rows.forEach((event) => {
      counts[diagnosticSeverity(event)] += 1;
    });
    return counts;
  }

  function diagnosticPanelImpact(rows) {
    const counts = diagnosticImpactCounts(rows);
    if (counts.error) {
      return "error";
    }
    if (counts.warning) {
      return "warning";
    }
    return "info";
  }

  function renderDiagnosticImpactSummary(rows) {
    const counts = diagnosticImpactCounts(rows);
    const total = counts.error + counts.warning + counts.info;
    if (!total) {
      return "";
    }
    return `
      <div class="fabric-diagnostic-impact-strip" aria-label="Diagnostic event impact summary">
        ${["error", "warning", "info"].map((severity) => {
          const meta = DIAGNOSTIC_IMPACT_META[severity];
          return `
            <span class="fabric-diagnostic-impact-count severity-${escapeHtml(severity)}" title="${escapeHtml(meta.title)}">
              ${diagnosticImpactBadge(severity)}
              <em>${escapeHtml(formatValue(counts[severity]))}</em>
            </span>
          `;
        }).join("")}
      </div>
    `;
  }

  function diagnosticFindingChips(value) {
    const findings = list(value?.top_findings).slice(0, 4);
    if (findings.length) {
      return findings.map((finding) => {
        const severity = diagnosticSeverity(finding);
        return `
        <span class="fabric-diagnostic-chip severity-${severity}">
          ${diagnosticImpactBadge(severity)}
          <strong>${escapeHtml(finding.label || finding.family || "Finding")}</strong>
          <em>${escapeHtml(formatValue(finding.count || 0))}</em>
        </span>
      `;
      }).join("");
    }
    const families = value?.fault_family_counts || {};
    return Object.entries(families).slice(0, 4).map(([family, count]) => `
      <span class="fabric-diagnostic-chip">
        ${diagnosticImpactBadge("info")}
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
    const severity = diagnosticSeverity(event);
    const confidence = diagnosticConfidenceValue(event);
    const meta = [
      event?.event_type,
      event?.device,
      event?.target ? `target ${event.target}` : null,
      event?.loginfo ? `loginfo ${event.loginfo}` : null,
      event?.opcode || decoded.opcode,
      (event?.service_action || decoded.service_action) ? `SA ${event?.service_action || decoded.service_action}` : null,
      event?.log_page || decoded.log_page,
      (event?.asc || decoded.asc) ? `ASC ${event?.asc || decoded.asc}` : null,
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
      <li class="fabric-diagnostic-event severity-${severity}">
        <div class="fabric-diagnostic-event-title">
          ${diagnosticImpactBadge(severity)}
          <strong>${escapeHtml(diagnosticEventLabel(event))}</strong>
          ${confidence === "none" ? "" : `<span class="fabric-diagnostic-confidence confidence-${escapeHtml(confidence)}">${escapeHtml(diagnosticConfidenceLabel(event))}</span>`}
        </div>
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
        severity: "all",
        confidence: "all",
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
      diagnosticSeverityLabel(event),
      diagnosticConfidenceLabel(event),
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
    const severity = String(tableState.severity || "all");
    const confidence = String(tableState.confidence || "all");
    return rows.filter((event) => {
      if (type !== "all" && String(event?.event_type || "event") !== type) {
        return false;
      }
      if (severity !== "all" && diagnosticSeverity(event) !== severity) {
        return false;
      }
      if (confidence !== "all" && diagnosticConfidenceValue(event) !== confidence) {
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

  function diagnosticSeverityOptions(rows) {
    const counts = diagnosticImpactCounts(rows);
    return ["error", "warning", "info"].map((severity) => [severity, counts[severity] || 0]);
  }

  function diagnosticConfidenceOptions(rows) {
    const counts = new Map();
    rows.forEach((event) => {
      const confidence = diagnosticConfidenceValue(event);
      counts.set(confidence, (counts.get(confidence) || 0) + 1);
    });
    const order = ["standard", "standard-partial", "vendor-reference", "vendor-reference-partial", "observed", "unconfirmed", "none"];
    return Array.from(counts.entries()).sort(([left], [right]) => {
      const leftIndex = order.indexOf(left);
      const rightIndex = order.indexOf(right);
      if (leftIndex !== -1 || rightIndex !== -1) {
        return (leftIndex === -1 ? order.length : leftIndex) - (rightIndex === -1 ? order.length : rightIndex);
      }
      return left.localeCompare(right);
    });
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
    severity,
    confidence,
    typeOptions,
    severityOptions,
    confidenceOptions,
    hasSourceTimestamps,
  }) {
    const hasFilter = String(filter || "").trim() || type !== "all" || severity !== "all" || confidence !== "all";
    const typeSelectOptions = [
      `<option value="all"${type === "all" ? " selected" : ""}>All types</option>`,
      ...typeOptions.map(([eventType, count]) => `
        <option value="${escapeHtml(eventType)}"${type === eventType ? " selected" : ""}>${escapeHtml(diagnosticEventTypeLabel(eventType))} (${escapeHtml(formatValue(count))})</option>
      `),
    ].join("");
    const severitySelectOptions = [
      `<option value="all"${severity === "all" ? " selected" : ""}>All impacts</option>`,
      ...severityOptions.map(([optionSeverity, count]) => `
        <option value="${escapeHtml(optionSeverity)}"${severity === optionSeverity ? " selected" : ""}>${escapeHtml(diagnosticSeverityLabel(optionSeverity))} (${escapeHtml(formatValue(count))})</option>
      `),
    ].join("");
    const confidenceSelectOptions = [
      `<option value="all"${confidence === "all" ? " selected" : ""}>All evidence</option>`,
      ...confidenceOptions.map(([optionConfidence, count]) => `
        <option value="${escapeHtml(optionConfidence)}"${confidence === optionConfidence ? " selected" : ""}>${escapeHtml(diagnosticConfidenceLabel(optionConfidence))} (${escapeHtml(formatValue(count))})</option>
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
        <label>
          <span>Impact</span>
          <select data-fabric-diagnostic-severity-key="${escapeHtml(key)}">${severitySelectOptions}</select>
        </label>
        <label>
          <span>Evidence</span>
          <select data-fabric-diagnostic-confidence-key="${escapeHtml(key)}">${confidenceSelectOptions}</select>
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
    const severity = diagnosticSeverity(event);
    const confidence = diagnosticConfidenceValue(event);
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
    return `
      <tr class="severity-${severity}">
        <td title="${escapeHtml(diagnosticEventTimeTitle(event))}">${escapeHtml(diagnosticEventTime(event))}</td>
        <td>${diagnosticImpactBadge(severity)}</td>
        <td>${escapeHtml(event?.event_type || "event")}</td>
        <td>${escapeHtml(diagnosticEventLabel(event))}</td>
        <td>${escapeHtml(scope || "-")}</td>
        <td>${escapeHtml(code || "-")}</td>
        <td><span class="fabric-diagnostic-confidence confidence-${escapeHtml(confidence)}">${escapeHtml(diagnosticConfidenceLabel(event))}</span></td>
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
    const panelImpact = diagnosticPanelImpact(eventRows);
    const rawRows = [
      ["Targets", formatSlots(diagnostics.targets, 999)],
      ["Devices", list(diagnostics.devices).join(", ")],
      ["Sense", diagnosticSenseSummary(diagnostics)],
      ["Loginfo", diagnosticLoginfoSummary(diagnostics)],
      ["Operations", formatCountMap(diagnostics.operation_counts)],
    ].filter(([, value]) => value);
    return `
      <div class="fabric-diagnostic-evidence impact-${escapeHtml(panelImpact)}">
        <div class="fabric-diagnostic-evidence-head">
          <span class="fabric-stage-title">Fault Evidence</span>
          <strong>${escapeHtml(summary || `${diagnostics.event_count} kernel events`)}</strong>
          ${scopeLabel ? `<small>Path leg: ${escapeHtml(scopeLabel)}</small>` : ""}
          ${layers ? `<small>Likely layer: ${escapeHtml(layers)}</small>` : ""}
        </div>
        ${renderDiagnosticImpactSummary(eventRows)}
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
              severity: tableState.severity,
              confidence: tableState.confidence,
              typeOptions: diagnosticEventTypeOptions(eventRows),
              severityOptions: diagnosticSeverityOptions(eventRows),
              confidenceOptions: diagnosticConfidenceOptions(eventRows),
              hasSourceTimestamps,
            })}
            <div class="fabric-diagnostic-table-wrap">
              <table class="fabric-diagnostic-table">
                <thead>
                  <tr><th>Time / Order</th><th>Impact</th><th>Type</th><th>Finding</th><th>Scope</th><th>Code</th><th>Evidence</th></tr>
                </thead>
                <tbody>${tableRows.length ? tableRows.map(renderDiagnosticTableRow).join("") : '<tr><td colspan="7">No events match the current filter.</td></tr>'}</tbody>
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

  function compactDeviceLabel(value, limit = 38) {
    const text = String(value || "").trim();
    if (!text) {
      return "";
    }
    const leaf = text
      .replace(/^\/dev\//, "/dev/")
      .replace(/^.*\/disk\/by-id\//, "")
      .replace(/^disk\/by-id\//, "")
      .replace(/^(scsi-|ata-|nvme-|wwn-)/i, "");
    if (leaf.length <= limit) {
      return leaf;
    }
    const head = Math.max(12, Math.floor((limit - 3) * 0.62));
    const tail = Math.max(8, limit - head - 3);
    return `${leaf.slice(0, head)}...${leaf.slice(-tail)}`;
  }

  function diskPathLabels(copy, linuxSes, storageFabric) {
    return {
      host: linuxSes ? "Host" : storageFabric ? "Host" : "Host",
      source: linuxSes ? "SES Source" : storageFabric ? "Storage Source" : "HBA",
      path: linuxSes ? "SES Link" : storageFabric ? "Storage Path" : "SAS Link",
      enclosure: linuxSes ? "SES Enclosure" : storageFabric ? "View / Enclosure" : "Expander / SES",
      backplane: "Backplane",
      pool: "Pool",
      vdev: "Vdev",
      disk: "Disk",
      ...(copy.diskLabels || {}),
    };
  }

  function renderDiskPathCard({ kind, title, subtitle = "", facts = [], hoverFacts = [], status = "", nodeId = "", traceId = "", modeTarget = "", extra = "" }) {
    const selected = (nodeId && state.selectedNodeId === nodeId) || (traceId && state.selectedTraceId === traceId);
    const actionAttribute = nodeId
      ? `data-fabric-node="${escapeHtml(nodeId)}"`
      : (traceId ? `data-fabric-trace="${escapeHtml(traceId)}"` : "");
    const modeTargetAttribute = actionAttribute && modeIds.has(modeTarget)
      ? ` data-fabric-mode-target="${escapeHtml(modeTarget)}"`
      : "";
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
      <${tagName}${typeAttribute} class="disk-path-card status-${classToken(status)}${selected ? " is-selected" : ""} ${extra}" ${actionAttribute}${modeTargetAttribute}${titleAttribute}>
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
    const copy = fabricViewCopy(fabric);
    const linuxSes = copy.kind === "linux_ses";
    const storageFabric = copy.storageFabric && !linuxSes && copy.kind !== "core_sas";
    const labels = diskPathLabels(copy, linuxSes, storageFabric);
    const nodes = nodeMap(fabric);
    const slot = slotByNumber(slotNumber);
    const smartSummary = smartSummaryForSlot(slotNumber);
    const hostNode = nodes.get("host") || {
      id: "host",
      kind: "host",
      label: fabric.system_label || fabric.system_id || "Host",
      metrics: {},
    };
    const mprDevice = branchMprDevice(seed, trace);
    const controllerName = seed.controller || mprDevice?.controller || controllerNameFromId(seed.controller_id) || "controller";
    const stateName = seed.state || seed.pathState?.state || seed.pathNode?.status || mprDevice?.state || "unknown";
    const controllerId = controllerName ? `controller:${controllerName}` : seed.controller_id;
    const controllerNode = nodes.get(controllerId) || seed.controllerNode || firstTraceNode(trace, fabric, (node) => node?.kind === "controller");
    const pathId = seed.pathState?.path_id || seed.pathNode?.id || (controllerName && stateName ? `path:${controllerName}:${classToken(stateName)}` : null);
    const pathNode = nodes.get(pathId) || seed.pathNode || firstTraceNode(trace, fabric, (node) => (
      node?.kind === "path" && (!controllerId || node.controller_id === controllerId)
    ));
    const pathNodeId = pathNode?.id && nodes.has(pathNode.id) ? pathNode.id : "";
    const pathTraceId = !pathNodeId && pathId && traceById(pathId, fabric) ? pathId : "";
    const expanderId = list(mprDevice?.expander_ids)[0];
    const expanderNode = nodes.get(expanderId) || seed.expanderNode || firstTraceNode(trace, fabric, (node) => (
      node?.kind === "expander" && (!controllerId || node.controller_id === controllerId)
    ));
    const enclosureNode = nodes.get(mprDevice?.enclosure_id) || firstTraceNode(trace, fabric, (node) => (
      ["mpr-enclosure", "ses-enclosure", "storage-enclosure"].includes(node?.kind) && (!controllerId || !node.controller_id || node.controller_id === controllerId)
    ));
    const expanderPhy = expanderPhyForDevice(expanderNode, mprDevice);
    const sesNodes = list(trace.node_ids)
      .map((nodeId) => nodes.get(nodeId))
      .filter((node) => node?.kind === "ses-enclosure" && selectionTouchesSlots([slotNumber]));
    const poolNode = firstTraceNode(trace, fabric, (node) => node?.kind === "pool");
    const vdevNode = firstTraceNode(trace, fabric, (node) => node?.kind === "vdev");
    const zone = backplaneZoneForSlot(slotNumber, fabric);
    const backplaneNode = zone.id ? nodes.get(zone.id) : null;
    const fullDeviceName = trace.metrics?.device_name || slot?.device_name || "";
    const compactDiskDevice = compactDeviceLabel(fullDeviceName, 40);
    const diskModel = slot?.model || trace.metrics?.model || seed.pathState?.model;
    const diskSerial = slot?.serial || trace.metrics?.serial || seed.pathState?.serial;
    const diskSize = slot?.size_human || trace.metrics?.size_human || seed.pathState?.size_human;
    const diskLun = slot?.logical_unit_id || trace.metrics?.logical_unit_id || seed.pathState?.logical_unit_id;
    const diskSasAddress = slot?.sas_address || trace.metrics?.sas_address || seed.pathState?.sas_address;
    const diskVdevClass = slot?.vdev_class || trace.metrics?.vdev_class || seed.pathState?.vdev_class;
    const diskBlockSizes = [
      slot?.logical_block_size || trace.metrics?.logical_block_size || seed.pathState?.logical_block_size,
      slot?.physical_block_size || trace.metrics?.physical_block_size || seed.pathState?.physical_block_size,
    ].filter(Boolean).join(" / ");
    const diskSmartDevices = list(slot?.smart_device_names || trace.metrics?.smart_device_names || seed.pathState?.smart_device_names).join(", ");
    const diskTitle = diskModel || compactDeviceLabel(fullDeviceName, 36) || `Bay ${formatSlotLabel(slotNumber)}`;
    const serialAlreadyShown = diskSerial && compactDiskDevice.includes(diskSerial);
    const diskSubtitle = [
      compactDiskDevice,
      serialAlreadyShown ? null : diskSerial,
      diskSize,
    ].filter(Boolean).join(" / ");
    const iocFacts = controllerNode?.raw?.iocfacts || {};
    const controllerDiagnostics = controllerNode?.metrics?.kernel_diagnostics || controllerNode?.raw?.kernel_diagnostics;
    const linkDiagnostics = mprDevice?.diagnostics;
    const branchDiagnostics = Number(linkDiagnostics?.event_count || 0) ? linkDiagnostics : controllerDiagnostics;
    const poolTitle = displayLabel(poolNode) || slot?.pool_name || trace.metrics?.pool_name;
    const vdevTitle = displayLabel(vdevNode) || slot?.vdev_name || trace.metrics?.vdev_name;
    const sourceTitle = displayLabel(controllerNode) || controllerName;
    const pathTitle = displayLabel(pathNode) || seed.pathState?.ses_device || controllerName;
    const pathContext = compactDeviceLabel(mprDevice?.member_device_name || seed.device_name || fullDeviceName, 42);
    const branchHeaderContext = [
      pathTitle && pathTitle !== sourceTitle ? pathTitle : null,
      pathContext,
    ].filter(Boolean).join(" / ");
    const cards = [
      renderDiskPathCard({
        kind: labels.host,
        title: displayLabel(hostNode) || "Host",
        subtitle: fabric.selected_enclosure_label || fabric.system_id || "",
        facts: [
          ["Bay", `Bay ${formatSlotLabel(slotNumber)}`],
          [linuxSes || storageFabric ? "Sources" : "Controllers", list(fabric.controllers).length],
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
        kind: labels.source,
        title: sourceTitle,
        subtitle: controllerNode?.raw?.board || controllerNode?.raw_id || "",
        facts: [
          [linuxSes ? "SES devices" : storageFabric ? "Source" : "PCIe slot", linuxSes ? controllerNode?.metrics?.ses_device_count : storageFabric ? controllerNode?.raw?.source : controllerNode?.metrics?.pcie_slot || controllerNode?.raw?.pcie_slot],
          ["FW", linuxSes || storageFabric ? null : controllerNode?.metrics?.firmware || controllerNode?.raw?.firmware],
          ["Temp", linuxSes || storageFabric ? null : controllerNode?.metrics?.temperature],
          ["PHY", linuxSes || storageFabric ? null : controllerNode?.metrics?.linked_phy_count && controllerNode?.metrics?.phy_count
            ? `${controllerNode.metrics.linked_phy_count}/${controllerNode.metrics.phy_count}`
            : controllerNode?.metrics?.linked_phys],
          ["IOC", linuxSes || storageFabric ? null : iocFacts.iocstatus],
          ["Events", linuxSes || storageFabric ? null : diagnosticSummary(controllerDiagnostics)],
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
        kind: labels.path,
        title: pathTitle,
        subtitle: compactDeviceLabel(seed.pathState?.ses_device || pathNode?.raw?.ses_device || mprDevice?.member_device_name || seed.device_name || displayLabel(pathNode) || "controller-level fabric", 42),
        facts: [
          [linuxSes ? "SES device" : storageFabric ? "Path" : "Speed", linuxSes ? seed.pathState?.ses_device || pathNode?.raw?.ses_device : storageFabric ? pathNode?.raw?.path_type || pathNode?.raw?.source : formatLinkSpeed(mprDevice?.speed)],
          [linuxSes ? "Block SG" : "PHY", linuxSes ? seed.pathState?.sg_device : storageFabric ? null : expanderPhy?.phy],
          [linuxSes ? "HCTL" : "Remote", linuxSes ? seed.pathState?.scsi_hctl : storageFabric ? null : expanderPhy?.remote_phy],
          [linuxSes ? "Transport" : "Handle", linuxSes ? [seed.pathState?.transport_protocol, seed.pathState?.target_port_protocol].filter(Boolean).join(" / ") : storageFabric ? null : mprDevice?.handle],
          ["Parent", linuxSes || storageFabric ? null : mprDevice?.parent],
          ["Disk slot", linuxSes || storageFabric ? null : mprDevice?.mpr_slot],
          ["Events", linuxSes || storageFabric ? null : diagnosticSummary(linkDiagnostics)],
        ],
        hoverFacts: [
          ["Path state", stateName],
          ["Member device", mprDevice?.member_device_name || seed.device_name],
          ["Disk identity", [seed.pathState?.model, seed.pathState?.serial, seed.pathState?.size_human].filter(Boolean).join(" / ")],
          ["SAS / LUN", seed.pathState?.sas_address || seed.pathState?.logical_unit_id],
          ["Attached SAS", seed.pathState?.attached_sas_address],
          ["PHY", seed.pathState?.phy_identifier],
          ["Block size", [seed.pathState?.logical_block_size, seed.pathState?.physical_block_size].filter(Boolean).join(" / ")],
          ["SMART device", list(seed.pathState?.smart_device_names).join(", ")],
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
        nodeId: pathNodeId,
        traceId: pathNodeId ? "" : pathTraceId,
      }),
      renderDiskPathCard({
        kind: labels.enclosure,
        title: displayLabel(expanderNode) || displayLabel(enclosureNode) || sesNodes.map(displayLabel).filter(Boolean).join(" + ") || (linuxSes ? "SES enclosure" : storageFabric ? "Storage view" : "Expander unknown"),
        subtitle: [
          linuxSes ? seed.pathState?.ses_device || pathNode?.raw?.ses_device || enclosureNode?.raw_id : null,
          !linuxSes && !storageFabric && displayLabel(enclosureNode) && displayLabel(enclosureNode) !== displayLabel(expanderNode) ? `MPR ${displayLabel(enclosureNode)}` : null,
          sesNodes.map(displayLabel).join(" + "),
        ].filter(Boolean).join(" / "),
        facts: [
          [linuxSes ? "SG device" : storageFabric ? "Evidence" : "WWN", linuxSes ? enclosureNode?.raw_id || seed.pathState?.ses_device : storageFabric ? enclosureNode?.raw?.source : expanderNode?.raw_id || enclosureNode?.raw_id],
          ["PHY", linuxSes || storageFabric ? null : expanderNode?.metrics?.linked_phys && expanderNode?.metrics?.num_phys
            ? `${expanderNode.metrics.linked_phys}/${expanderNode.metrics.num_phys}`
            : expanderNode?.metrics?.linked_phys],
          ["Enclosure", linuxSes ? enclosureNode?.raw?.enclosure_label : mprDevice?.enclosure_handle || enclosureNode?.raw?.enc_handle],
          ["Devices", linuxSes || storageFabric ? null : formatCountMap(expanderNode?.raw?.device_counts)],
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
        kind: labels.backplane,
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
      poolTitle && (storageFabric || linuxSes) ? renderDiskPathCard({
        kind: labels.pool,
        title: poolTitle,
        subtitle: [
          pathNode?.raw?.pool_owner_label ? `owner ${pathNode.raw.pool_owner_label}` : null,
          pathNode?.raw?.fence_owner_label ? `fence ${pathNode.raw.fence_owner_label}` : null,
        ].filter(Boolean).join(" / "),
        facts: [
          ["Visible on", list(pathNode?.raw?.visible_on_labels).join(", ")],
          ["Slots", sortedSlots(poolNode?.slots).length || null],
        ],
        status: poolNode?.status || stateName,
        nodeId: poolNode?.id || "",
      }) : "",
      vdevTitle && (storageFabric || linuxSes) ? renderDiskPathCard({
        kind: labels.vdev,
        title: vdevTitle,
        subtitle: slot?.pool_name || trace.metrics?.pool_name || "",
        facts: [
          ["Slots", sortedSlots(vdevNode?.slots).length || null],
          ["State", vdevNode?.status],
        ],
        status: vdevNode?.status || stateName,
        nodeId: vdevNode?.id || "",
      }) : "",
      renderDiskPathCard({
        kind: labels.disk,
        title: diskTitle,
        subtitle: diskSubtitle,
        facts: [
          ["Pool / vdev", [slot?.pool_name, slot?.vdev_name].filter(Boolean).join(" / ")],
          ["Health", [smartSummary?.smart_health_status || slot?.health || slot?.state, smartSummary?.temperature_c != null ? `${smartSummary.temperature_c} C` : slot?.temperature_c ? `${slot.temperature_c} C` : null].filter(Boolean).join(" / ")],
          ["SAS / LUN", mprDevice?.sas_address || diskSasAddress || diskLun],
          ["SMART", smartSummaryLine(smartSummary)],
          ["Member", mprDevice?.member_device_name || seed.device_name],
          ["Events", diagnosticSummary(linkDiagnostics)],
        ],
        hoverFacts: [
          ["Serial", diskSerial],
          ["Model", diskModel],
          ["Size", diskSize],
          ["Vdev class", diskVdevClass],
          ["Block size", diskBlockSizes],
          ["SMART device", diskSmartDevices],
          ["Kernel sense", diagnosticSenseSummary(linkDiagnostics)],
          ["Recent kernel events", diagnosticRecentSummary(linkDiagnostics)],
          ["GPTID", slot?.gptid],
          ["Multipath state", slot?.multipath?.state || slot?.multipath?.provider_state],
          ["SMART", smartSummaryLine(smartSummary)],
          ["Power on", formatSmartPowerOn(smartSummary)],
          ["Last SMART test", [slot?.last_smart_test_type, slot?.last_smart_test_status].filter(Boolean).join(" / ")],
          ["Transport", [slot?.transport_protocol, slot?.scsi_hctl, slot?.sg_device].filter(Boolean).join(" / ")],
          ["Attached SAS", slot?.attached_sas_address],
        ],
        status: slot?.health || slot?.state || stateName,
        traceId: trace.id,
      }),
    ];
    return `
      <section class="disk-path-branch status-${classToken(stateName)}">
        <div class="disk-path-branch-header">
          <span>${escapeHtml(sourceTitle)}</span>
          <strong>${escapeHtml(stateName)}</strong>
          <small>${escapeHtml(branchHeaderContext)}</small>
        </div>
        <div class="disk-path-flow">
          ${cards.filter(Boolean).map((card, index) => `${index ? '<span class="disk-path-arrow">-&gt;</span>' : ""}${card}`).join("")}
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
    const selectedModel = slot?.model || trace.metrics?.model;
    const selectedSerial = slot?.serial || trace.metrics?.serial;
    const selectedSize = slot?.size_human || trace.metrics?.size_human;
    const selectedPool = slot?.pool_name || trace.metrics?.pool_name;
    const selectedVdev = slot?.vdev_name || trace.metrics?.vdev_name;
    const selectedDevice = slot?.device_name || trace.metrics?.device_name;
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
            <small>${escapeHtml([
              selectedModel,
              compactDeviceLabel(selectedDevice, 44),
              selectedSerial && !compactDeviceLabel(selectedDevice, 44).includes(selectedSerial) ? selectedSerial : null,
              selectedSize,
              selectedPool,
              selectedVdev,
            ].filter(Boolean).join(" / ") || "No disk metadata")}</small>
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

  function renderBayPathMembers(pathStates) {
    return list(pathStates).length ? `
      <div class="fabric-path-member-list">
        ${list(pathStates).map((pathState) => {
          const stateName = pathState.state || "unknown";
          const deviceName = pathState.device_name || pathState.member_device_name || "";
          return `
            <div class="fabric-path-member-card status-${classToken(stateName)}">
              <div>
                <strong>${escapeHtml(pathState.controller || pathState.path_id || "path")}</strong>
                <span>${escapeHtml(stateName)}</span>
              </div>
              ${deviceName ? `<small title="${escapeHtml(deviceName)}">${escapeHtml(compactDeviceLabel(deviceName, 54))}</small>` : ""}
              <dl>
                ${pathState.source ? `<span><dt>Source</dt><dd>${escapeHtml(pathState.source)}</dd></span>` : ""}
                ${pathState.path_type ? `<span><dt>Type</dt><dd>${escapeHtml(pathState.path_type)}</dd></span>` : ""}
                ${pathState.ses_device ? `<span><dt>SES</dt><dd>${escapeHtml(pathState.ses_device)}</dd></span>` : ""}
                ${pathState.sg_device ? `<span><dt>SG</dt><dd>${escapeHtml(pathState.sg_device)}</dd></span>` : ""}
                ${pathState.scsi_hctl ? `<span><dt>HCTL</dt><dd>${escapeHtml(pathState.scsi_hctl)}</dd></span>` : ""}
                ${pathState.transport_protocol ? `<span><dt>Transport</dt><dd>${escapeHtml([pathState.transport_protocol, pathState.target_port_protocol].filter(Boolean).join(" / "))}</dd></span>` : ""}
                ${pathState.logical_unit_id || pathState.sas_address ? `<span><dt>SAS/LUN</dt><dd>${escapeHtml(pathState.sas_address || pathState.logical_unit_id)}</dd></span>` : ""}
              </dl>
            </div>
          `;
        }).join("")}
      </div>
    ` : '<span class="fabric-empty-note">No path members reported.</span>';
  }

  function renderSelectedBayInspector(trace, fabric) {
    const nodes = nodeMap(fabric);
    const slotNumber = sortedSlots(trace.slots)[0];
    const slot = slotByNumber(slotNumber);
    const pathStates = list(trace.metrics?.path_states);
    const sourceNode = firstTraceNode(trace, fabric, (node) => node?.kind === "controller");
    const pathNode = firstTraceNode(trace, fabric, (node) => node?.kind === "path");
    const enclosureNode = firstTraceNode(trace, fabric, (node) => ["ses-enclosure", "mpr-enclosure", "storage-enclosure"].includes(node?.kind));
    const poolNode = firstTraceNode(trace, fabric, (node) => node?.kind === "pool");
    const vdevNode = firstTraceNode(trace, fabric, (node) => node?.kind === "vdev");
    const backplaneNode = firstTraceNode(trace, fabric, (node) => node?.kind === "backplane");
    const deviceName = trace.metrics?.device_name || slot?.device_name || "";
    const compactDevice = compactDeviceLabel(deviceName, 56);
    const stateName = slot?.health || slot?.state || pathStates[0]?.state || trace.status || "unknown";
    const bayLabel = `Bay ${formatSlotLabel(slotNumber)}`;
    const bayModel = slot?.model || trace.metrics?.model;
    const baySerial = slot?.serial || trace.metrics?.serial;
    const baySize = slot?.size_human || trace.metrics?.size_human;
    const bayLun = slot?.logical_unit_id || trace.metrics?.logical_unit_id;
    const bayBlockSizes = [
      slot?.logical_block_size || trace.metrics?.logical_block_size,
      slot?.physical_block_size || trace.metrics?.physical_block_size,
    ].filter(Boolean).join(" / ");
    const baySmartDevices = list(slot?.smart_device_names || trace.metrics?.smart_device_names).join(", ");
    const modelLine = [bayModel, baySize].filter(Boolean).join(" / ");
    const sourceLabel = displayLabel(sourceNode) || pathStates[0]?.controller || fabric.system_label || "source";
    const pathLabel = displayLabel(pathNode) || pathStates[0]?.ses_device || pathStates[0]?.path_id || pathStates[0]?.path_type;
    const smartSummary = smartSummaryForSlot(slotNumber);
    return `
      <div class="fabric-selected-bay">
        <section class="fabric-selected-bay-hero status-${classToken(stateName)}">
          <div>
            <span class="fabric-stage-title">Selected Bay</span>
            <strong>${escapeHtml(bayLabel)}</strong>
            <small>${escapeHtml(modelLine || compactDevice || "No disk metadata")}</small>
          </div>
          <span class="fabric-selected-bay-state">${escapeHtml(stateName)}</span>
        </section>

        <div class="fabric-inspector-fact-grid">
          ${inspectorFact("Device", compactDevice || deviceName, { wide: true })}
          ${inspectorFact("Model", bayModel)}
          ${inspectorFact("Serial", baySerial)}
          ${inspectorFact("Size", baySize)}
          ${inspectorFact("Pool", displayLabel(poolNode) || trace.metrics?.pool_name || slot?.pool_name)}
          ${inspectorFact("Vdev", displayLabel(vdevNode) || trace.metrics?.vdev_name || slot?.vdev_name)}
          ${inspectorFact("Source", sourceLabel)}
          ${inspectorFact("Path", pathLabel)}
          ${inspectorFact("View", displayLabel(enclosureNode) || slot?.enclosure_name)}
          ${inspectorFact("Bay Group", displayLabel(backplaneNode))}
          ${inspectorFact("Fabric", trace.metrics?.fabric_kind)}
        </div>

        <section class="fabric-inspector-section">
          <h4>Device Identity</h4>
          <div class="fabric-identity-card">
            ${deviceName ? `<code title="${escapeHtml(deviceName)}">${escapeHtml(deviceName)}</code>` : '<span class="fabric-empty-note">No device path reported.</span>'}
            <div>
              ${baySerial ? `<span><em>Serial</em>${escapeHtml(baySerial)}</span>` : ""}
              ${bayModel ? `<span><em>Model</em>${escapeHtml(bayModel)}</span>` : ""}
              ${baySize ? `<span><em>Size</em>${escapeHtml(baySize)}</span>` : ""}
              ${bayLun ? `<span><em>LUN</em>${escapeHtml(bayLun)}</span>` : ""}
              ${bayBlockSizes ? `<span><em>Block size</em>${escapeHtml(bayBlockSizes)}</span>` : ""}
              ${baySmartDevices ? `<span><em>SMART device</em>${escapeHtml(baySmartDevices)}</span>` : ""}
              ${smartSummaryLine(smartSummary) ? `<span><em>SMART</em>${escapeHtml(smartSummaryLine(smartSummary))}</span>` : ""}
              ${slot?.transport_protocol ? `<span><em>Transport</em>${escapeHtml([slot.transport_protocol, slot.scsi_hctl].filter(Boolean).join(" / "))}</span>` : ""}
              ${slot?.sg_device ? `<span><em>SG device</em>${escapeHtml(slot.sg_device)}</span>` : ""}
              ${slot?.attached_sas_address ? `<span><em>Attached SAS</em>${escapeHtml(slot.attached_sas_address)}</span>` : ""}
            </div>
          </div>
        </section>

        <section class="fabric-inspector-section">
          <h4>Selected Disk SMART</h4>
          ${renderSmartSummaryCard(slotNumber, slot)}
        </section>

        <section class="fabric-inspector-section">
          <h4>Source Evidence</h4>
          <div class="fabric-evidence-chip-list">${evidenceChips(trace.evidence)}</div>
        </section>

        <section class="fabric-inspector-section">
          <h4>Path Members</h4>
          ${renderBayPathMembers(pathStates)}
        </section>

        <section class="fabric-inspector-section">
          <h4>Friendly Label</h4>
          <div class="kv-grid">
            ${renderAliasRow("Bay", { objectId: trace.id, objectKind: trace.kind, item: trace })}
          </div>
        </section>

        <section class="fabric-inspector-section">
          <h4>Trace Nodes</h4>
          <div class="fabric-node-grid fabric-inspector-node-grid">${renderNodeGrid(list(trace.node_ids).map((nodeId) => nodes.get(nodeId)).filter(Boolean), 18)}</div>
        </section>
      </div>
    `;
  }

  function aliasScopeLabel(kind) {
    const kindName = String(kind || "").toLowerCase();
    if (["bay", "backplane", "ses-enclosure", "mpr-enclosure", "storage-enclosure", "expander"].includes(kindName)) {
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
      if (trace.kind === "bay") {
        const slotNumber = sortedSlots(trace.slots)[0];
        elements.inspectorTitle.textContent = `Selected Bay ${Number.isInteger(slotNumber) ? formatSlotLabel(slotNumber) : ""}`.trim();
        elements.inspectorBody.innerHTML = renderSelectedBayInspector(trace, fabric);
        return;
      }
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
    const copy = fabricViewCopy(fabric);
    setSummaryCardText(elements.summaryControllers, ...copy.summary.controllers);
    setSummaryCardText(elements.summaryPaths, ...copy.summary.paths);
    setSummaryCardText(elements.summaryExpanders, ...copy.summary.expanders);
    setSummaryCardText(elements.summaryEnclosures, ...copy.summary.enclosures);
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
    const copy = fabricViewCopy(fabric);
    let className = "status-chip";
    let text = copy.statusBase;
    if (state.error) {
      className += " error";
      text = `${copy.statusBase} ERR`;
      elements.statusText.textContent = state.error;
      elements.statusText.dataset.tone = "error";
    } else if (state.loading) {
      className += " partial";
      text = `${copy.statusBase} ...`;
      elements.statusText.textContent = fabric ? `Refreshing ${copy.statusBase.toLowerCase()} data.` : `Loading ${copy.statusBase.toLowerCase()} data.`;
      elements.statusText.dataset.tone = "info";
    } else if (fabric?.available === false) {
      className += " partial";
      text = `${copy.statusBase} OFF`;
      elements.statusText.textContent = list(fabric.warnings)[0] || "Storage Fabric is not available for this system.";
      elements.statusText.dataset.tone = "error";
    } else if (fabric) {
      className += " ok";
      text = `${copy.statusBase} OK`;
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
    const text = String(warning || "").toLowerCase();
    return text.includes("sas fabric enrichment probes")
      || text.includes("storage fabric enrichment probes")
      || text.includes("fabric map is built from linux ses slot evidence")
      || text.includes("storage fabric is built from")
      || text.includes("storage fabric map is built from");
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
    const copy = fabricViewCopy(state.fabric);
    const modeDetails = copy.modes[state.mode] || copy.modes.lanes;
    if (elements.pageEyebrow) {
      elements.pageEyebrow.textContent = copy.eyebrow;
    }
    if (elements.pageSummary) {
      elements.pageSummary.textContent = copy.pageSummary;
    }
    if (elements.refreshButton) {
      elements.refreshButton.textContent = copy.refresh;
    }
    elements.mapTitle.textContent = modeDetails.title;
    elements.mapSubtitle.textContent = modeDetails.subtitle;
    elements.modeButtons.forEach((button) => {
      const active = button.dataset.fabricMode === state.mode;
      const label = copy.modeLabels[button.dataset.fabricMode];
      if (label) {
        button.textContent = label;
      }
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function renderMap() {
    const fabric = state.fabric;
    if (!fabric) {
      elements.mapPanel.innerHTML = '<div class="warning-item muted compact">No Storage Fabric payload has been loaded yet.</div>';
      renderFocusStrip(null);
      renderInspector(null);
      return;
    }
    if (fabric.available === false) {
      const copy = fabricViewCopy(fabric);
      elements.mapPanel.innerHTML = `<div class="warning-item muted compact">${escapeHtml(copy.unavailableMap)}</div>`;
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
    rememberedDiskTrace(fabric);
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
    ensureSelectedSmartSummary();
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
    rememberedDiskTrace(fabric);
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
    state.selectedDiskTraceId = null;
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
    state.selectedDiskTraceId = null;
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
    if (!target?.matches("[data-fabric-diagnostic-type-key], [data-fabric-diagnostic-severity-key], [data-fabric-diagnostic-confidence-key]")) {
      return;
    }
    const key = target.dataset.fabricDiagnosticTypeKey
      || target.dataset.fabricDiagnosticSeverityKey
      || target.dataset.fabricDiagnosticConfidenceKey
      || "";
    const tableState = diagnosticTableState(key);
    if (target.matches("[data-fabric-diagnostic-type-key]")) {
      tableState.type = target.value || "all";
    } else if (target.matches("[data-fabric-diagnostic-severity-key]")) {
      tableState.severity = target.value || "all";
    } else {
      tableState.confidence = target.value || "all";
    }
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
