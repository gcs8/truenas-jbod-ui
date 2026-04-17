(function () {
  const bootstrap = window.APP_BOOTSTRAP || {};
  const supportedRefreshIntervals = [15, 30, 60, 300];
  const bootstrapRefreshInterval = Number(bootstrap.refreshIntervalSeconds) || 30;
  const SMART_PREFETCH_DELAY_MS = 180;
  const SMART_PREFETCH_CHUNK_SIZE = 8;
  const SMART_PREFETCH_BATCH_CONCURRENCY = 2;
  const SMART_PREFETCH_STALE_MS = 15000;
  const DEFAULT_HISTORY_TIMEFRAME_HOURS = 24;
  const state = {
    snapshot: bootstrap.snapshot || { slots: [], systems: [], enclosures: [] },
    layoutRows: bootstrap.layoutRows || [],
    selectedSlot: null,
    hoveredSlot: null,
    selectedSystemId: bootstrap.snapshot?.selected_system_id || null,
    selectedEnclosureId: bootstrap.snapshot?.selected_enclosure_id || null,
    search: "",
    autoRefresh: true,
    refreshIntervalSeconds: supportedRefreshIntervals.includes(bootstrapRefreshInterval) ? bootstrapRefreshInterval : 30,
    timerId: null,
    smartSummaries: {},
    smartSummaryGeneration: 0,
    smartPrefetchToken: 0,
    smartPrefetchTimerId: null,
    smartPrefetchRunning: false,
    smartPrefetchScopeKey: null,
    history: {
      configured: Boolean(bootstrap.historyConfigured),
      checked: false,
      available: false,
      loading: false,
      detail: null,
      counts: {},
      collector: {},
      panelOpen: false,
      timeframeHours: DEFAULT_HISTORY_TIMEFRAME_HOURS,
      ioChartMode: "total",
      panelLoading: false,
      panelError: null,
      slotCache: {},
    },
  };

  const grid = document.getElementById("slot-grid");
  const enclosureFace = document.querySelector(".enclosure-face");
  const headerEyebrow = document.getElementById("header-eyebrow");
  const headerSummary = document.getElementById("header-summary");
  const enclosurePanelTitle = document.getElementById("enclosure-panel-title");
  const enclosureEdgeLabel = document.getElementById("enclosure-edge-label");
  const chassisShell = document.getElementById("chassis-shell");
  const slotTooltipEl = document.getElementById("slot-tooltip");
  const detailEmpty = document.getElementById("detail-empty");
  const detailContent = document.getElementById("detail-content");
  const detailSecondary = document.getElementById("detail-secondary");
  const detailLedControls = document.getElementById("detail-led-controls");
  const detailSlotTitle = document.getElementById("detail-slot-title");
  const detailStatePill = document.getElementById("detail-state-pill");
  const detailKvGrid = document.getElementById("detail-kv-grid");
  const detailSmartNote = document.getElementById("detail-smart-note");
  const topologyContext = document.getElementById("topology-context");
  const multipathContext = document.getElementById("multipath-context");
  const secondaryContextTitle = document.getElementById("secondary-context-title");
  const warningList = document.getElementById("warning-list");
  const searchBox = document.getElementById("search-box");
  const refreshButton = document.getElementById("refresh-button");
  const autoRefreshToggle = document.getElementById("auto-refresh-toggle");
  const refreshIntervalSelect = document.getElementById("refresh-interval-select");
  const systemSelect = document.getElementById("system-select");
  const enclosureSelect = document.getElementById("enclosure-select");
  const lastUpdated = document.getElementById("last-updated");
  const timezoneLabel = document.getElementById("timezone-label");
  const apiStatusChip = document.getElementById("api-status-chip");
  const sshStatusChip = document.getElementById("ssh-status-chip");
  const historyStatusChip = document.getElementById("history-status-chip");
  const statusText = document.getElementById("status-text");
  const summaryDiskCount = document.getElementById("summary-disk-count");
  const summaryPoolCount = document.getElementById("summary-pool-count");
  const summaryEnclosureCount = document.getElementById("summary-enclosure-count");
  const summaryMappedSlotCount = document.getElementById("summary-mapped-slot-count");
  const summaryManualMappingCount = document.getElementById("summary-manual-mapping-count");
  const summarySshSlotHintCount = document.getElementById("summary-ssh-slot-hint-count");
  const mappingForm = document.getElementById("mapping-form");
  const clearMappingButton = document.getElementById("clear-mapping-button");
  const prefillMappingButton = document.getElementById("prefill-mapping-button");
  const exportMappingsButton = document.getElementById("export-mappings-button");
  const importMappingsButton = document.getElementById("import-mappings-button");
  const mappingImportFile = document.getElementById("mapping-import-file");
  const mappingEmpty = document.getElementById("mapping-empty");
  const historyToggleButton = document.getElementById("history-toggle-button");
  const detailHistoryPanel = document.getElementById("detail-history-panel");
  const historyDrawerTitle = document.getElementById("history-drawer-title");
  const historyDrawerContext = document.getElementById("history-drawer-context");
  const historyCloseButton = document.getElementById("history-close-button");
  const historyTimeframeSelect = document.getElementById("history-timeframe-select");
  const detailHistorySummary = document.getElementById("detail-history-summary");
  const detailHistoryEmpty = document.getElementById("detail-history-empty");
  const detailHistoryLoading = document.getElementById("detail-history-loading");
  const detailHistoryError = document.getElementById("detail-history-error");
  const detailHistoryContent = document.getElementById("detail-history-content");
  const historyMetricGrid = document.getElementById("history-metric-grid");
  const historyTemperatureLabel = document.getElementById("history-temperature-label");
  const historyIoLabel = document.getElementById("history-io-label");
  const historyIoModeToggle = document.getElementById("history-io-mode-toggle");
  const historyIoModeButtons = Array.from(document.querySelectorAll("[data-history-io-mode]"));
  const historyTemperatureChart = document.getElementById("history-temperature-chart");
  const historyIoChart = document.getElementById("history-io-chart");
  const historyEventList = document.getElementById("history-event-list");
  const ledButtons = Array.from(document.querySelectorAll("[data-led-action]"));

  function getSlotById(slotNumber) {
    return state.snapshot.slots.find((slot) => slot.slot === slotNumber) || null;
  }

  function formatSlotLabel(slotNumber) {
    return String(slotNumber).padStart(2, "0");
  }

  function formatScopeLabel() {
    const systemPart = state.snapshot.selected_system_id || state.selectedSystemId || "system";
    const enclosurePart = state.snapshot.selected_enclosure_id || state.selectedEnclosureId || "all-enclosures";
    return `${systemPart}-${enclosurePart}`.replace(/[^a-zA-Z0-9._-]+/g, "-");
  }

  function getSelectedSystemOption() {
    return (state.snapshot.systems || []).find((system) => system.id === state.selectedSystemId) || null;
  }

  function getSelectedEnclosureOption() {
    return (state.snapshot.enclosures || []).find((enclosure) => enclosure.id === state.selectedEnclosureId) || null;
  }

  function getSelectedProfile() {
    return state.snapshot.selected_profile || null;
  }

  function currentPlatform() {
    return (state.snapshot.selected_system_platform || "core").toLowerCase();
  }

  function currentPlatformContext() {
    return state.snapshot.platform_context || {};
  }

  function usesGenericPersistentIdLabel() {
    return ["scale", "linux"].includes(currentPlatform());
  }

  function buildViewProfile() {
    const profile = getSelectedProfile();
    const system = getSelectedSystemOption();
    const enclosure = getSelectedEnclosureOption();
    const enclosureLabel = enclosure?.label || state.snapshot.selected_enclosure_label || "Enclosure";
    const systemLabel = system?.label || state.snapshot.selected_system_label || "TrueNAS JBOD Enclosure UI";
    return {
      eyebrow: profile?.eyebrow || systemLabel,
      summary: profile?.summary || "Drive-bay map with API-or-SSH enrichment for the selected enclosure.",
      enclosureTitle: profile?.panel_title || enclosureLabel,
      edgeLabel: profile?.edge_label || "System front",
      faceStyle: profile?.face_style || "generic",
      latchEdge: profile?.latch_edge || "bottom",
      baySize: profile?.bay_size || null,
    };
  }

  function normalizeDriveScaleCandidate(value) {
    if (!value) {
      return null;
    }
    const text = String(value).trim().toLowerCase();
    if (!text) {
      return null;
    }
    if (text.includes("3.5")) {
      return "3.5";
    }
    if (text.includes("2.5") || text.includes("u.2") || text.includes("u2") || text.includes("m.2") || text.includes("m2")) {
      return "2.5";
    }
    return null;
  }

  function inferDominantDriveScale(profile) {
    if (profile.baySize === "3.5" || profile.baySize === "2.5") {
      return profile.baySize;
    }

    const slotCount = Number(state.snapshot.layout_slot_count) || state.layoutRows.flat().length || 0;

    if (profile.faceStyle === "unifi-drive") {
      return "3.5";
    }

    if (profile.faceStyle === "top-loader") {
      return "3.5";
    }

    if (slotCount <= 2) {
      return "2.5";
    }

    const counts = { "3.5": 0, "2.5": 0 };
    (state.snapshot.slots || []).forEach((slot) => {
      const smartEntry = getSmartSummaryEntry(slot);
      const candidate = normalizeDriveScaleCandidate(smartEntry?.data?.form_factor);
      if (candidate) {
        counts[candidate] += 1;
      }
    });

    if (counts["3.5"] || counts["2.5"]) {
      return counts["3.5"] >= counts["2.5"] ? "3.5" : "2.5";
    }

    if (profile.faceStyle === "front-drive" || profile.faceStyle === "rear-drive") {
      return "3.5";
    }

    return "default";
  }

  function inferChassisLayoutMode(profile, driveScale) {
    const rowCount = state.layoutRows.length || 0;
    const slotCount = Number(state.snapshot.layout_slot_count) || state.layoutRows.flat().length || 0;

    if (profile.faceStyle === "unifi-drive") {
      return rowCount > 1 ? "unifi-2row" : "unifi-1row";
    }

    if (slotCount <= 2) {
      return "compact";
    }

    if (profile.faceStyle === "top-loader") {
      return driveScale === "2.5" ? "top-loader-2.5" : "top-loader-3.5";
    }

    if (profile.faceStyle === "front-drive" || profile.faceStyle === "rear-drive") {
      if (slotCount >= 20) {
        return driveScale === "2.5" ? "dense-2.5" : "dense-3.5";
      }
      if (slotCount >= 8) {
        return driveScale === "2.5" ? "standard-2.5" : "standard-3.5";
      }
    }

    return driveScale === "2.5" ? "compact" : "standard-3.5";
  }

  function applyChassisDriveScale(profile) {
    if (!chassisShell) {
      return;
    }
    const scale = inferDominantDriveScale(profile);
    const layoutMode = inferChassisLayoutMode(profile, scale);
    chassisShell.dataset.driveScale = scale;
    chassisShell.dataset.layoutMode = layoutMode;
    chassisShell.dataset.layoutRows = String(state.layoutRows.length || 0);
  }

  function splitRowIntoGroups(row) {
    const groups = Array.isArray(getSelectedProfile()?.row_groups) ? getSelectedProfile().row_groups : [];
    if (!groups.length) {
      return [row];
    }
    const totalColumns = groups.reduce((sum, value) => sum + value, 0);
    if (totalColumns !== row.length) {
      return [row];
    }
    const groupedRows = [];
    let offset = 0;
    groups.forEach((groupSize) => {
      groupedRows.push(row.slice(offset, offset + groupSize));
      offset += groupSize;
    });
    return groupedRows.filter((group) => group.length > 0);
  }

  function rowGroupBreakpoints(rowGroups) {
    if (!Array.isArray(rowGroups) || rowGroups.length <= 1) {
      return [];
    }
    const breakpoints = [];
    let offset = 0;
    rowGroups.forEach((group, index) => {
      offset += group.length;
      if (index < rowGroups.length - 1) {
        breakpoints.push(offset);
      }
    });
    return breakpoints;
  }

  function flatGroupedColumnTemplate(slotCount, breakpoints) {
    const columns = [];
    for (let index = 0; index < slotCount; index += 1) {
      columns.push("minmax(0, 1fr)");
      if (breakpoints.includes(index + 1)) {
        columns.push("10px");
      }
    }
    return columns.join(" ");
  }

  function rowGroupingMetrics(layoutMode) {
    if (String(layoutMode || "").startsWith("top-loader")) {
      return { rowGap: 7, groupGap: 2 };
    }
    return { rowGap: 14, groupGap: 6 };
  }

  function groupColumnTemplate(rowGroups, layoutMode) {
    if (!Array.isArray(rowGroups) || rowGroups.length <= 1) {
      return rowGroups.map((group) => `minmax(0, ${group.length}fr)`).join(" ");
    }

    const totalSlots = rowGroups.reduce((sum, group) => sum + group.length, 0);
    if (!totalSlots) {
      return "";
    }

    const { rowGap, groupGap } = rowGroupingMetrics(layoutMode);
    const totalGapPixels =
      rowGap * Math.max(rowGroups.length - 1, 0) +
      groupGap * Math.max(totalSlots - rowGroups.length, 0);

    return rowGroups
      .map((group) => {
        const slotShare = group.length / totalSlots;
        const slotShareText = slotShare.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
        const internalGapPixels = groupGap * Math.max(group.length - 1, 0);
        return `calc((100% - ${totalGapPixels}px) * ${slotShareText} + ${internalGapPixels}px)`;
      })
      .join(" ");
  }

  function buildSelectionParams() {
    const params = new URLSearchParams();
    if (state.selectedSystemId) {
      params.set("system_id", state.selectedSystemId);
    }
    if (state.selectedEnclosureId) {
      params.set("enclosure_id", state.selectedEnclosureId);
    }
    return params;
  }

  function applySnapshot(snapshot) {
    state.snapshot = snapshot;
    state.layoutRows = snapshot.layout_rows || state.layoutRows || [];
    state.selectedSystemId = snapshot.selected_system_id || state.selectedSystemId;
    state.selectedEnclosureId = snapshot.selected_enclosure_id || null;
    state.smartSummaryGeneration += 1;
    pruneSmartSummaryCache();
    if (state.selectedSlot !== null && !getSlotById(state.selectedSlot)) {
      state.selectedSlot = null;
    }
  }

  function pruneSmartSummaryCache() {
    const validKeys = new Set(
      (state.snapshot.slots || []).map((slot) => getSmartCacheKey(slot))
    );
    Object.keys(state.smartSummaries).forEach((key) => {
      if (!validKeys.has(key)) {
        delete state.smartSummaries[key];
      }
    });
  }

  function syncLocation() {
    const params = buildSelectionParams();
    const query = params.toString();
    window.history.replaceState({}, "", query ? `/?${query}` : "/");
  }

  function setStatus(message, tone = "info") {
    statusText.textContent = message;
    statusText.dataset.tone = tone;
  }

  function getBrowserTimeZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
    } catch (error) {
      return null;
    }
  }

  function renderTimezoneLabel() {
    if (!timezoneLabel) {
      return;
    }
    const timeZone = getBrowserTimeZone();
    timezoneLabel.textContent = timeZone ? `Browser local time: ${timeZone}` : "Browser local time";
  }

  function formatTimestamp(value) {
    if (!value) return "Unknown";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    try {
      return date.toLocaleString([], {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
        timeZoneName: "short",
      });
    } catch (error) {
      return date.toLocaleString();
    }
  }

  function stateLabel(slot) {
    return (slot.state || "unknown").replace("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function isPlaceholderHintLabel(value) {
    return typeof value === "string" && /^\d+:\d+:\d+:\d+$/.test(value.trim());
  }

  function slotPrimaryLabel(slot) {
    if (slot.device_name && !isPlaceholderHintLabel(slot.device_name)) return slot.device_name;
    if (slot.state === "empty") return "EMPTY";
    if (slot.state === "unknown") return "UNKNOWN";
    if (slot.state === "unmapped") return "UNMAPPED";
    if (slot.state === "identify") return "LOCATE";
    if (slot.state === "fault") return "FAULT";
    if (slot.present) return "PRESENT";
    return "UNKNOWN";
  }

  function persistentIdLabel(slot) {
    return (
      slot?.persistent_id_label ||
      (usesGenericPersistentIdLabel() ? "Persistent ID" : "GPTID")
    );
  }

  function ledStatusLabel(slot) {
    if (!slot.led_supported && !slot.identify_active) return "Unavailable";
    return slot.identify_active ? "On" : "Off";
  }

  function ledBackendLabel(slot) {
    if (!slot || !slot.led_backend) return "unknown backend";
    if (slot.led_backend === "api") return "API";
    if (slot.led_backend === "unifi_fault") {
      return slot?.raw_status?.experimental_led ? "UniFi SSH LED (Experimental)" : "UniFi SSH LED";
    }
    return "SSH SES";
  }

  function getSmartCacheKey(slot) {
    const systemPart = state.snapshot.selected_system_id || state.selectedSystemId || "system";
    const enclosurePart = state.snapshot.selected_enclosure_id || state.selectedEnclosureId || "all-enclosures";
    return `${systemPart}|${enclosurePart}|${slot.slot}|${slot.device_name || "unknown"}`;
  }

  function getHistoryCacheKey(slot) {
    const systemPart = state.snapshot.selected_system_id || state.selectedSystemId || "system";
    const enclosurePart = state.snapshot.selected_enclosure_id || state.selectedEnclosureId || "all-enclosures";
    return `${systemPart}|${enclosurePart}|${slot.slot}`;
  }

  function getSmartSummaryEntry(slot) {
    if (!slot) return null;
    return state.smartSummaries[getSmartCacheKey(slot)] || null;
  }

  function currentSmartPrefetchScopeKey() {
    const systemPart = state.snapshot.selected_system_id || state.selectedSystemId || "system";
    const enclosurePart = state.snapshot.selected_enclosure_id || state.selectedEnclosureId || "all-enclosures";
    return `${systemPart}|${enclosurePart}`;
  }

  function isSmartEntryCurrent(entry) {
    return entry?.generation === state.smartSummaryGeneration;
  }

  function isSmartEntryInFlight(entry) {
    if (!entry || (!entry.loading && !entry.refreshing)) {
      return false;
    }
    const requestedAt = Number(entry.requestedAt) || 0;
    return requestedAt > 0 && Date.now() - requestedAt < SMART_PREFETCH_STALE_MS;
  }

  function candidateSlotsForSmartPrefetch() {
    return (state.snapshot.slots || []).filter((slot) => {
      if (!slot.present) {
        return false;
      }
      if (!slot.device_name && !(Array.isArray(slot.smart_device_names) && slot.smart_device_names.length)) {
        return false;
      }
      const entry = getSmartSummaryEntry(slot);
      if (!entry) {
        return true;
      }
      if (isSmartEntryCurrent(entry) && (entry.data || isSmartEntryInFlight(entry))) {
        return false;
      }
      return !isSmartEntryInFlight(entry);
    });
  }

  function updateSmartPrefetchViews() {
    const profile = buildViewProfile();
    const slotCount = Number(state.snapshot.layout_slot_count) || state.layoutRows.flat().length || 0;
    const usesStaticGeometry =
      profile.faceStyle === "top-loader" ||
      profile.faceStyle === "unifi-drive" ||
      slotCount <= 2;
    if (!usesStaticGeometry) {
      applyChassisDriveScale(profile);
    }
    if (state.selectedSlot !== null) {
      renderDetail();
    }
    if (state.hoveredSlot !== null) {
      refreshHoveredTooltip();
    }
  }

  async function runSmartPrefetch(runToken, scopeKey) {
    if (runToken !== state.smartPrefetchToken || scopeKey !== state.smartPrefetchScopeKey) {
      return;
    }
    state.smartPrefetchRunning = true;
    const slots = candidateSlotsForSmartPrefetch();
    if (!slots.length) {
      state.smartPrefetchRunning = false;
      return;
    }

    try {
      slots.forEach((slot) => {
        const cacheKey = getSmartCacheKey(slot);
        const existingEntry = state.smartSummaries[cacheKey];
        state.smartSummaries[cacheKey] = {
          loading: !existingEntry?.data,
          refreshing: Boolean(existingEntry?.data),
          data: existingEntry?.data || null,
          requestedAt: Date.now(),
          generation: state.smartSummaryGeneration,
        };
      });
      updateSmartPrefetchViews();

      const chunks = [];
      for (let index = 0; index < slots.length; index += SMART_PREFETCH_CHUNK_SIZE) {
        chunks.push(slots.slice(index, index + SMART_PREFETCH_CHUNK_SIZE));
      }

      let nextChunkIndex = 0;
      const processNextChunk = async () => {
        while (nextChunkIndex < chunks.length) {
          if (runToken !== state.smartPrefetchToken || scopeKey !== state.smartPrefetchScopeKey) {
            return;
          }
          const chunk = chunks[nextChunkIndex];
          nextChunkIndex += 1;
        try {
          const payload = await sendScopedRequest("/api/slots/smart-batch", {
            method: "POST",
            body: JSON.stringify({ slots: chunk.map((slot) => slot.slot) }),
          });
          if (runToken !== state.smartPrefetchToken || scopeKey !== state.smartPrefetchScopeKey) {
            return;
          }
          const seenSlots = new Set();
          (payload.summaries || []).forEach((item) => {
            const slot = getSlotById(item.slot);
            if (!slot) {
              return;
            }
            seenSlots.add(item.slot);
            state.smartSummaries[getSmartCacheKey(slot)] = {
              loading: false,
              refreshing: false,
              data: item.summary,
              requestedAt: Date.now(),
              generation: state.smartSummaryGeneration,
            };
          });
          chunk.forEach((slot) => {
            if (seenSlots.has(slot.slot)) {
              return;
            }
            const existingEntry = state.smartSummaries[getSmartCacheKey(slot)];
            state.smartSummaries[getSmartCacheKey(slot)] = {
              loading: false,
              refreshing: false,
              data: existingEntry?.data || { available: false, message: "SMART prefetch returned no data for this slot." },
              requestedAt: Date.now(),
              generation: state.smartSummaryGeneration,
            };
          });
        } catch (error) {
          console.error("SMART prefetch failed", error);
          chunk.forEach((slot) => {
            const existingEntry = state.smartSummaries[getSmartCacheKey(slot)];
            state.smartSummaries[getSmartCacheKey(slot)] = {
              loading: false,
              refreshing: false,
              data: existingEntry?.data || {
                available: false,
                message: error.message || String(error),
              },
              requestedAt: Date.now(),
              generation: state.smartSummaryGeneration,
            };
          });
        }
        updateSmartPrefetchViews();
        }
      };

      const workerCount = Math.min(SMART_PREFETCH_BATCH_CONCURRENCY, chunks.length);
      await Promise.all(Array.from({ length: workerCount }, () => processNextChunk()));
    } finally {
      if (runToken === state.smartPrefetchToken && scopeKey === state.smartPrefetchScopeKey) {
        state.smartPrefetchRunning = false;
        if (candidateSlotsForSmartPrefetch().length) {
          scheduleSmartPrefetch();
        }
      }
    }
  }

  function scheduleSmartPrefetch() {
    if (state.smartPrefetchTimerId) {
      window.clearTimeout(state.smartPrefetchTimerId);
      state.smartPrefetchTimerId = null;
    }
    const scopeKey = currentSmartPrefetchScopeKey();
    if (state.smartPrefetchRunning && state.smartPrefetchScopeKey === scopeKey) {
      return;
    }
    const runToken = state.smartPrefetchToken + 1;
    state.smartPrefetchToken = runToken;
    state.smartPrefetchScopeKey = scopeKey;
    state.smartPrefetchTimerId = window.setTimeout(() => {
      state.smartPrefetchTimerId = null;
      void runSmartPrefetch(runToken, scopeKey);
    }, SMART_PREFETCH_DELAY_MS);
  }

  function formatTemperatureValue(slot, smartEntry) {
    const temperature = smartEntry?.data?.temperature_c ?? slot.temperature_c;
    if (Number.isInteger(temperature)) {
      return `${temperature} C`;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatLastSmartTestValue(slot, smartEntry) {
    const data = smartEntry?.data;
    const testType = data?.last_test_type || slot.last_smart_test_type;
    const testStatus = data?.last_test_status || slot.last_smart_test_status;
    const ageHours = data?.last_test_age_hours;
    const lifetimeHours = data?.last_test_lifetime_hours ?? slot.last_smart_test_lifetime_hours;

    if (testType && testStatus) {
      let suffix = "";
      if (Number.isInteger(ageHours)) {
        suffix = ` (${ageHours}h ago)`;
      } else if (Number.isInteger(lifetimeHours)) {
        suffix = ` (@ ${lifetimeHours}h)`;
      }
      return `${testType}: ${testStatus}${suffix}`;
    }

    if (smartEntry?.loading) {
      return "Loading...";
    }

    return "n/a";
  }

  function formatPowerOnValue(smartEntry) {
    const hours = smartEntry?.data?.power_on_hours;
    const days = smartEntry?.data?.power_on_days;
    if (Number.isInteger(hours)) {
      return Number.isInteger(days) ? `${hours} hr (${days} d)` : `${hours} hr`;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return smartEntry?.data?.message ? "Unavailable" : "n/a";
  }

  function formatSectorSizeValue(slot, smartEntry) {
    const logical = smartEntry?.data?.logical_block_size ?? slot?.logical_block_size;
    const physical = smartEntry?.data?.physical_block_size ?? slot?.physical_block_size;
    if (Number.isInteger(logical) && Number.isInteger(physical)) {
      return `Logical ${logical} B / Physical ${physical} B`;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return smartEntry?.data?.message ? "Unavailable" : "n/a";
  }

  function formatRotationValue(smartEntry) {
    const rpm = smartEntry?.data?.rotation_rate_rpm;
    if (rpm === 0) {
      return "SSD";
    }
    if (Number.isInteger(rpm)) {
      return `${rpm} rpm`;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatFormFactorValue(smartEntry) {
    const formFactor = smartEntry?.data?.form_factor;
    if (formFactor) {
      return formFactor;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatBooleanFlagValue(value, smartEntry, trueLabel = "Enabled", falseLabel = "Disabled") {
    if (value === true) {
      return trueLabel;
    }
    if (value === false) {
      return falseLabel;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatCacheFlagValue(value, smartEntry) {
    return formatBooleanFlagValue(value, smartEntry);
  }

  function formatReadCacheValue(smartEntry) {
    return formatCacheFlagValue(smartEntry?.data?.read_cache_enabled, smartEntry);
  }

  function formatWritebackCacheValue(smartEntry) {
    return formatCacheFlagValue(smartEntry?.data?.writeback_cache_enabled, smartEntry);
  }

  function formatTrimSupportedValue(smartEntry) {
    return formatBooleanFlagValue(smartEntry?.data?.trim_supported, smartEntry, "Supported", "Not Supported");
  }

  function formatHexIdentifier(value) {
    if (value === null || value === undefined || value === "") {
      return null;
    }
    const text = String(value).trim();
    if (!text) {
      return null;
    }
    if (/^0x[0-9a-f]+$/i.test(text)) {
      return text.toLowerCase();
    }
    if (/^[0-9a-f]+$/i.test(text)) {
      return `0x${text.toLowerCase()}`;
    }
    return text;
  }

  function formatTransportValue(smartEntry) {
    const transport = smartEntry?.data?.transport_protocol;
    if (transport) {
      return transport;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatLogicalUnitIdValue(slot, smartEntry) {
    const logicalUnitId = smartEntry?.data?.logical_unit_id || slot?.logical_unit_id || slot?.multipath?.lunid;
    const formatted = formatHexIdentifier(logicalUnitId);
    if (formatted) {
      return formatted;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatSasAddressValue(slot, smartEntry) {
    const sasAddress = smartEntry?.data?.sas_address || slot?.sas_address;
    const formatted = formatHexIdentifier(sasAddress);
    if (formatted) {
      return formatted;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function firstSesTarget(slot) {
    if (!Array.isArray(slot?.ssh_ses_targets)) {
      return null;
    }
    return slot.ssh_ses_targets.find((target) => target && (target.ssh_host || target.ses_device)) || null;
  }

  function formatAttachedSasAddressValue(slot, smartEntry) {
    const formatted = formatHexIdentifier(
      smartEntry?.data?.attached_sas_address || slot?.raw_status?.attached_sas_address
    );
    if (formatted && formatted !== "0x0") {
      return formatted;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatLinkRateValue(smartEntry) {
    const linkRate = smartEntry?.data?.negotiated_link_rate;
    if (linkRate) {
      return linkRate;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatSesHostValue(slot) {
    const host = firstSesTarget(slot)?.ssh_host;
    return host || "n/a";
  }

  function formatSesStateValue(slot) {
    const flags = [];
    if (slot?.raw_status?.ses_hot_spare) {
      flags.push("Hot Spare");
    }
    if (slot?.raw_status?.ses_do_not_remove) {
      flags.push("Do Not Remove");
    }
    if (slot?.raw_status?.ses_predicted_failure) {
      flags.push("Predicted Failure");
    }
    if (slot?.raw_status?.ses_fault_sensed) {
      flags.push("Fault Sensed");
    }
    if (slot?.raw_status?.ses_fault_requested) {
      flags.push("Fault Requested");
    }
    if (slot?.raw_status?.ses_disabled) {
      flags.push("Disabled");
    }
    return flags.length ? flags.join(", ") : "n/a";
  }

  function shouldShowSasTransportFields(slot, smartEntry) {
    const transport = String(smartEntry?.data?.transport_protocol || "").toLowerCase();
    if (transport.includes("sas")) {
      return true;
    }
    if (transport.includes("nvme") || transport.includes("ata") || transport.includes("sata")) {
      return false;
    }

    return Boolean(
      smartEntry?.data?.logical_unit_id ||
      smartEntry?.data?.sas_address ||
      smartEntry?.data?.attached_sas_address ||
      smartEntry?.data?.negotiated_link_rate ||
      slot?.logical_unit_id ||
      slot?.sas_address ||
      slot?.raw_status?.attached_sas_address ||
      slot?.multipath?.lunid
    );
  }

  function formatNamespaceIdentifierValue(value, smartEntry) {
    if (value) {
      return value;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatNamespaceNguidValue(smartEntry) {
    return formatNamespaceIdentifierValue(smartEntry?.data?.namespace_nguid, smartEntry);
  }

  function formatNamespaceEui64Value(slot, smartEntry) {
    const namespaceEui64 = smartEntry?.data?.namespace_eui64;
    const persistentId = slot?.gptid;
    if (
      namespaceEui64 &&
      (!persistentId || namespaceEui64.toLowerCase() !== String(persistentId).toLowerCase())
    ) {
      return namespaceEui64;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatFirmwareValue(smartEntry) {
    const firmwareVersion = smartEntry?.data?.firmware_version;
    if (firmwareVersion) {
      return firmwareVersion;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatProtocolVersionValue(smartEntry) {
    const protocolVersion = smartEntry?.data?.protocol_version;
    if (protocolVersion) {
      return protocolVersion;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatThresholdTemperatureValue(value, smartEntry) {
    if (Number.isInteger(value)) {
      return `${value} C`;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatWarningTemperatureValue(smartEntry) {
    return formatThresholdTemperatureValue(smartEntry?.data?.warning_temperature_c, smartEntry);
  }

  function formatCriticalTemperatureValue(smartEntry) {
    return formatThresholdTemperatureValue(smartEntry?.data?.critical_temperature_c, smartEntry);
  }

  function formatMetricBytes(value) {
    if (!Number.isFinite(value) || value <= 0) {
      return null;
    }
    const suffixes = ["B", "KB", "MB", "GB", "TB", "PB", "EB"];
    let size = value;
    let suffixIndex = 0;
    while (size >= 1000 && suffixIndex < suffixes.length - 1) {
      size /= 1000;
      suffixIndex += 1;
    }
    const fractionDigits = size >= 100 || suffixIndex === 0 ? 0 : size >= 10 ? 1 : 2;
    return `${size.toFixed(fractionDigits)} ${suffixes[suffixIndex]}`;
  }

  function formatOptionalCount(value, smartEntry) {
    if (Number.isInteger(value)) {
      return String(value);
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatEnduranceValue(smartEntry) {
    const remaining = smartEntry?.data?.endurance_remaining_percent;
    const used = smartEntry?.data?.endurance_used_percent;
    if (Number.isInteger(remaining) && Number.isInteger(used)) {
      return `${remaining}% remaining (${used}% used)`;
    }
    if (Number.isInteger(remaining)) {
      return `${remaining}% remaining`;
    }
    if (Number.isInteger(used)) {
      return `${used}% used`;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatAvailableSpareValue(smartEntry) {
    const spare = smartEntry?.data?.available_spare_percent;
    const threshold = smartEntry?.data?.available_spare_threshold_percent;
    if (Number.isInteger(spare) && Number.isInteger(threshold)) {
      return `${spare}% (threshold ${threshold}%)`;
    }
    if (Number.isInteger(spare)) {
      return `${spare}%`;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatBytesValue(value, smartEntry) {
    const formatted = formatMetricBytes(value);
    if (formatted) {
      return formatted;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatBytesWrittenValue(smartEntry) {
    return formatBytesValue(smartEntry?.data?.bytes_written, smartEntry);
  }

  function formatBytesReadValue(smartEntry) {
    return formatBytesValue(smartEntry?.data?.bytes_read, smartEntry);
  }

  function formatAnnualizedWriteValue(smartEntry) {
    const formatted = formatMetricBytes(smartEntry?.data?.annualized_bytes_written);
    if (formatted) {
      return `${formatted}/yr`;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatEstimatedRemainingWriteValue(smartEntry) {
    return formatBytesValue(smartEntry?.data?.estimated_remaining_bytes_written, smartEntry);
  }

  function formatMediaErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.media_errors, smartEntry);
  }

  function formatPredictiveErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.predictive_errors, smartEntry);
  }

  function formatNonMediumErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.non_medium_errors, smartEntry);
  }

  function formatUncorrectedReadErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.uncorrected_read_errors, smartEntry);
  }

  function formatUncorrectedWriteErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.uncorrected_write_errors, smartEntry);
  }

  function formatUnsafeShutdownsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.unsafe_shutdowns, smartEntry);
  }

  function formatSmartHealthStatusValue(smartEntry) {
    const status = smartEntry?.data?.smart_health_status;
    if (status) {
      return status;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function buildTooltipLines(slot, smartEntry) {
    const persistentIdText = slot.gptid ? `${persistentIdLabel(slot)}: ${slot.gptid}` : `${persistentIdLabel(slot)}: n/a`;
    const poolLabel = currentPlatform() === "linux" ? "Mount" : "Pool";
    const vdevLabel = currentPlatform() === "linux" ? "Array" : "Vdev";
    const lines = [
      `Slot ${slot.slot_label}${slot.device_name ? ` - ${slot.device_name}` : ""}`,
      slot.serial ? `Serial: ${slot.serial}` : "Serial: n/a",
      persistentIdText,
      slot.pool_name ? `${poolLabel}: ${slot.pool_name}` : `${poolLabel}: n/a`,
      slot.vdev_name ? `${vdevLabel}: ${slot.vdev_name}` : `${vdevLabel}: n/a`,
      slot.health ? `Health: ${slot.health}` : "Health: n/a",
    ];

    if (smartEntry?.loading) {
      lines.push("SMART: loading...");
      return lines;
    }

    const temperature = formatTemperatureValue(slot, smartEntry);
    if (temperature !== "n/a") {
      lines.push(`Temp: ${temperature}`);
    }

    const smartHealth = formatSmartHealthStatusValue(smartEntry);
    if (smartHealth !== "n/a") {
      lines.push(`SMART: ${smartHealth}`);
    }

    const endurance = formatEnduranceValue(smartEntry);
    if (endurance !== "n/a") {
      lines.push(`Wear: ${endurance}`);
    }

    const bytesRead = formatBytesReadValue(smartEntry);
    if (bytesRead !== "n/a") {
      lines.push(`Reads: ${bytesRead}`);
    }

    const bytesWritten = formatBytesWrittenValue(smartEntry);
    if (bytesWritten !== "n/a") {
      lines.push(`Writes: ${bytesWritten}`);
    }

    const annualizedWrite = formatAnnualizedWriteValue(smartEntry);
    if (annualizedWrite !== "n/a") {
      lines.push(`Annualized Write: ${annualizedWrite}`);
    }

    const estimatedRemaining = formatEstimatedRemainingWriteValue(smartEntry);
    if (estimatedRemaining !== "n/a") {
      lines.push(`Est. TBW Left: ${estimatedRemaining}`);
    }

    const mediaErrors = formatMediaErrorsValue(smartEntry);
    if (mediaErrors !== "n/a") {
      lines.push(`Media Errors: ${mediaErrors}`);
    }

    const predictiveErrors = formatPredictiveErrorsValue(smartEntry);
    if (predictiveErrors !== "n/a") {
      lines.push(`Predictive Errors: ${predictiveErrors}`);
    }

    const transport = formatTransportValue(smartEntry);
    if (transport !== "n/a") {
      lines.push(`Transport: ${transport}`);
    }

    const linkRate = formatLinkRateValue(smartEntry);
    if (linkRate !== "n/a") {
      lines.push(`Link Rate: ${linkRate}`);
    }

    const attachedSas = formatAttachedSasAddressValue(slot, smartEntry);
    if (attachedSas !== "n/a") {
      lines.push(`Attached SAS: ${attachedSas}`);
    }

    if (currentPlatform() === "quantastor") {
      const sesHost = formatSesHostValue(slot);
      if (sesHost !== "n/a") {
        lines.push(`SES Host: ${sesHost}`);
      }
      const sesState = formatSesStateValue(slot);
      if (sesState !== "n/a") {
        lines.push(`SES Flags: ${sesState}`);
      }
    }

    return lines;
  }

  function slotTooltip(slot, smartEntry) {
    return buildTooltipLines(slot, smartEntry).join("\n");
  }

  function passesFilter(slot) {
    if (!state.search) return true;
    return (slot.search_text || "").includes(state.search);
  }

  function hideSlotTooltip() {
    if (!slotTooltipEl) {
      return;
    }
    slotTooltipEl.classList.add("hidden");
    slotTooltipEl.setAttribute("aria-hidden", "true");
    slotTooltipEl.innerHTML = "";
  }

  function positionSlotTooltip(clientX, clientY) {
    if (!slotTooltipEl || slotTooltipEl.classList.contains("hidden")) {
      return;
    }

    const margin = 14;
    const maxLeft = window.innerWidth - slotTooltipEl.offsetWidth - margin;
    const maxTop = window.innerHeight - slotTooltipEl.offsetHeight - margin;
    const left = Math.min(clientX + 18, Math.max(margin, maxLeft));
    const top = Math.min(clientY + 18, Math.max(margin, maxTop));
    slotTooltipEl.style.left = `${Math.max(margin, left)}px`;
    slotTooltipEl.style.top = `${Math.max(margin, top)}px`;
  }

  function positionSlotTooltipFromElement(element) {
    if (!element) {
      return;
    }
    const rect = element.getBoundingClientRect();
    positionSlotTooltip(rect.right, rect.top + rect.height / 2);
  }

  function refreshHoveredTooltip(anchorElement = null) {
    if (state.hoveredSlot === null || !slotTooltipEl) {
      return;
    }
    const slot = getSlotById(state.hoveredSlot);
    if (!slot) {
      state.hoveredSlot = null;
      hideSlotTooltip();
      return;
    }
    const smartEntry = getSmartSummaryEntry(slot);
    const lines = buildTooltipLines(slot, smartEntry);
    const [headline, ...rest] = lines;
    slotTooltipEl.innerHTML = `
      <div class="slot-tooltip-title">${escapeHtml(headline)}</div>
      ${rest.map((line) => `<div class="slot-tooltip-line">${escapeHtml(line)}</div>`).join("")}
    `;
    slotTooltipEl.classList.remove("hidden");
    slotTooltipEl.setAttribute("aria-hidden", "false");
    if (anchorElement) {
      positionSlotTooltipFromElement(anchorElement);
    }
  }

  function getSelectedPeerContext() {
    const slot = getSlotById(state.selectedSlot);
    if (!slot || !slot.pool_name || !slot.vdev_name) {
      return { active: false, peerSlots: new Set() };
    }

    const peerSlots = new Set(
      state.snapshot.slots
        .filter((candidate) =>
          candidate.pool_name === slot.pool_name &&
          candidate.vdev_name === slot.vdev_name &&
          candidate.vdev_class === slot.vdev_class &&
          candidate.device_name
        )
        .map((candidate) => candidate.slot)
    );

    return {
      active: peerSlots.size > 1,
      peerSlots,
    };
  }

  function renderGrid() {
    hideSlotTooltip();
    grid.innerHTML = "";
    const slotsByNumber = new Map(state.snapshot.slots.map((slot) => [slot.slot, slot]));
    const peerContext = getSelectedPeerContext();

    state.layoutRows.forEach((row) => {
      const rowWrapper = document.createElement("div");
      rowWrapper.className = "slot-row";

      const rowSlots = document.createElement("div");
      rowSlots.className = "row-slots";
      rowSlots.dataset.slotCount = String(row.length);
      const rowGroups = splitRowIntoGroups(row);
      const profile = buildViewProfile();
      const driveScale = inferDominantDriveScale(profile);
      const layoutMode = inferChassisLayoutMode(profile, driveScale);
      const isFlatTopLoaderGrouping = String(layoutMode || "").startsWith("top-loader") && rowGroups.length > 1;
      const flatGroupBreakpoints = rowGroupBreakpoints(rowGroups);

      const appendTile = (container, slotNumber, tileIndex = null, breakpoints = []) => {
        const slot = slotsByNumber.get(slotNumber) || {
          slot: slotNumber,
          slot_label: formatSlotLabel(slotNumber),
          state: "unknown",
        };
        const tile = document.createElement("button");
        tile.type = "button";
        tile.className = `slot-tile state-${slot.state}`;
        if (!passesFilter(slot)) {
          tile.classList.add("filtered-out");
        }
        if (state.selectedSlot === slot.slot) {
          tile.classList.add("selected");
        } else if (peerContext.active && peerContext.peerSlots.has(slot.slot)) {
          tile.classList.add("peer-highlight");
        } else if (peerContext.active) {
          tile.classList.add("peer-dimmed");
        }
        if (tileIndex !== null && breakpoints.includes(tileIndex + 1)) {
          tile.classList.add("group-divider-after");
        }
        tile.dataset.slot = String(slot.slot);
        tile.setAttribute("aria-label", slotTooltip(slot, getSmartSummaryEntry(slot)));
        tile.innerHTML = `
          <span class="slot-status-led" aria-hidden="true"></span>
          <span class="slot-number">${slot.slot_label}</span>
          <span class="slot-device">${escapeHtml(slotPrimaryLabel(slot))}</span>
          <span class="slot-pool">${escapeHtml(slot.pool_name || stateLabel(slot))}</span>
          <span class="slot-latch" aria-hidden="true"></span>
        `;
        tile.addEventListener("mouseenter", (event) => {
          state.hoveredSlot = slot.slot;
          refreshHoveredTooltip(tile);
          positionSlotTooltip(event.clientX, event.clientY);
          void ensureSmartSummary(slot);
        });
        tile.addEventListener("mousemove", (event) => {
          if (state.hoveredSlot === slot.slot) {
            positionSlotTooltip(event.clientX, event.clientY);
          }
        });
        tile.addEventListener("mouseleave", () => {
          if (state.hoveredSlot === slot.slot) {
            state.hoveredSlot = null;
          }
          hideSlotTooltip();
        });
        tile.addEventListener("focus", () => {
          state.hoveredSlot = slot.slot;
          refreshHoveredTooltip(tile);
          positionSlotTooltipFromElement(tile);
          void ensureSmartSummary(slot);
        });
        tile.addEventListener("blur", () => {
          if (state.hoveredSlot === slot.slot) {
            state.hoveredSlot = null;
          }
          hideSlotTooltip();
        });
        tile.addEventListener("click", (event) => {
          event.stopPropagation();
          if (state.selectedSlot === slot.slot) {
            clearSelectedSlot();
            return;
          }
          selectSlot(slot.slot);
        });
        container.appendChild(tile);
      };

      if (isFlatTopLoaderGrouping) {
        rowSlots.classList.add("row-slots-flat-grouped");
        row.forEach((slotNumber, tileIndex) => {
          appendTile(rowSlots, slotNumber, tileIndex, flatGroupBreakpoints);
          if (flatGroupBreakpoints.includes(tileIndex + 1)) {
            const divider = document.createElement("span");
            divider.className = "row-metal-divider";
            divider.setAttribute("aria-hidden", "true");
            rowSlots.appendChild(divider);
          }
        });
      } else {
        rowGroups.forEach((group) => {
          const rowGroup = document.createElement("div");
          rowGroup.className = "row-group";
          rowGroup.dataset.slotCount = String(group.length);
          rowGroup.style.setProperty("--group-columns", String(group.length));

          group.forEach((slotNumber) => {
            appendTile(rowGroup, slotNumber);
          });

          rowSlots.appendChild(rowGroup);
        });
      }

      rowWrapper.appendChild(rowSlots);
      grid.appendChild(rowWrapper);
      rowSlots.style.gridTemplateColumns = isFlatTopLoaderGrouping
        ? flatGroupedColumnTemplate(row.length, flatGroupBreakpoints)
        : groupColumnTemplate(rowGroups, layoutMode);
    });
  }

  function kvRow(label, value, copyable = false) {
    const hasValue = value !== null && value !== undefined && value !== "";
    const safeValue = escapeHtml(hasValue ? String(value) : "n/a");
    const encodedValue = hasValue ? encodeURIComponent(String(value)) : "";
    return `
      <div class="kv-row">
        <div class="kv-label">${escapeHtml(label)}</div>
        <div class="kv-value">${safeValue}</div>
        <div>${copyable && hasValue ? `<button class="copy-button" data-copy="${encodedValue}">Copy</button>` : ""}</div>
      </div>
    `;
  }

  function kvRowIfMeaningful(label, value, copyable = false) {
    if (value === null || value === undefined || value === "" || value === "n/a") {
      return "";
    }
    return kvRow(label, value, copyable);
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function operatorContext(slot) {
    return slot?.operator_context || {};
  }

  function formatVisibleOnValue(slot) {
    const labels = operatorContext(slot).visible_on_labels;
    return Array.isArray(labels) && labels.length ? labels.join(", ") : "n/a";
  }

  function formatQuantastorContextValue(slot, key) {
    const value = operatorContext(slot)[key];
    if (Array.isArray(value)) {
      return value.length ? value.join(", ") : "n/a";
    }
    return value ?? "n/a";
  }

  function isHistoryAvailable() {
    return state.history.configured && state.history.available;
  }

  function historyEventTypeLabel(eventType) {
    switch (eventType) {
      case "slot_state_changed":
        return "State Change";
      case "slot_identity_changed":
        return "Identity Change";
      case "slot_topology_changed":
        return "Topology Change";
      case "slot_multipath_changed":
        return "Multipath Change";
      default:
        return eventType || "History Event";
    }
  }

  function formatHistoryScalar(value) {
    if (value === null || value === undefined || value === "") {
      return "n/a";
    }
    if (value === true) {
      return "true";
    }
    if (value === false) {
      return "false";
    }
    return String(value);
  }

  function formatHistoryMetricValue(metricName, value) {
    if (!Number.isFinite(Number(value))) {
      return "n/a";
    }
    const numericValue = Number(value);
    switch (metricName) {
      case "temperature_c":
        return `${numericValue} C`;
      case "bytes_read":
      case "bytes_written":
        return formatMetricBytes(numericValue) || "n/a";
      case "annualized_bytes_written": {
        const formatted = formatMetricBytes(numericValue);
        return formatted ? `${formatted}/yr` : "n/a";
      }
      case "power_on_hours": {
        const days = Math.floor(numericValue / 24);
        return `${numericValue} hr (${days} d)`;
      }
      default:
        return String(numericValue);
    }
  }

  function sortHistorySamplesAscending(samples) {
    return [...(samples || [])]
      .filter((sample) => Number.isFinite(Number(sample?.value)))
      .sort((left, right) => new Date(left.observed_at).getTime() - new Date(right.observed_at).getTime());
  }

  function sampleTimestampMs(sample) {
    const timestamp = new Date(sample?.observed_at).getTime();
    return Number.isFinite(timestamp) ? timestamp : null;
  }

  function currentHistoryWindowHours() {
    return state.history.timeframeHours;
  }

  function formatHistoryWindowLabel(windowHours) {
    if (!Number.isFinite(Number(windowHours)) || Number(windowHours) <= 0) {
      return "All";
    }
    const numericWindow = Number(windowHours);
    if (numericWindow === 24) {
      return "24h";
    }
    if (numericWindow < 24) {
      return `${numericWindow}h`;
    }
    if (numericWindow === 24 * 365) {
      return "1y";
    }
    if (numericWindow % 24 === 0) {
      return `${numericWindow / 24}d`;
    }
    return `${numericWindow}h`;
  }

  function formatHistoryWindowDescription(windowHours) {
    return Number.isFinite(Number(windowHours)) && Number(windowHours) > 0
      ? formatHistoryWindowLabel(windowHours)
      : "all history";
  }

  function historyWindowCutoffMs(windowHours, referenceTimestampMs = Date.now()) {
    if (!Number.isFinite(Number(windowHours)) || Number(windowHours) <= 0) {
      return null;
    }
    return referenceTimestampMs - Number(windowHours) * 3600000;
  }

  function filterHistorySamplesToWindow(samples, windowHours, referenceTimestampMs = Date.now()) {
    const ordered = sortHistorySamplesAscending(samples);
    const cutoff = historyWindowCutoffMs(windowHours, referenceTimestampMs);
    if (!ordered.length || cutoff === null) {
      return ordered;
    }
    return ordered.filter((sample) => {
      const timestamp = sampleTimestampMs(sample);
      return Number.isFinite(timestamp) && timestamp >= cutoff;
    });
  }

  function sortHistoryEventsDescending(events) {
    return [...(events || [])].sort(
      (left, right) => new Date(right?.observed_at).getTime() - new Date(left?.observed_at).getTime()
    );
  }

  function filterHistoryEventsToWindow(events, windowHours, referenceTimestampMs = Date.now()) {
    const ordered = sortHistoryEventsDescending(events);
    const cutoff = historyWindowCutoffMs(windowHours, referenceTimestampMs);
    if (!ordered.length || cutoff === null) {
      return ordered;
    }
    return ordered.filter((event) => {
      const timestamp = new Date(event?.observed_at).getTime();
      return Number.isFinite(timestamp) && timestamp >= cutoff;
    });
  }

  function collectHistoryTimestamps(payload) {
    const timestamps = [];
    (payload?.events || []).forEach((event) => {
      if (event?.observed_at) {
        timestamps.push(event.observed_at);
      }
    });
    Object.values(payload?.metrics || {}).forEach((samples) => {
      (samples || []).forEach((sample) => {
        if (sample?.observed_at) {
          timestamps.push(sample.observed_at);
        }
      });
    });
    return timestamps;
  }

  function computeHistoryCounterAverage(samples, windowHours = null, referenceTimestampMs = Date.now()) {
    const ordered = filterHistorySamplesToWindow(samples, windowHours, referenceTimestampMs);
    if (ordered.length < 2) {
      return null;
    }

    let totalDelta = 0;
    let totalHours = 0;
    for (let index = 1; index < ordered.length; index += 1) {
      const previous = ordered[index - 1];
      const current = ordered[index];
      const previousValue = Number(previous?.value);
      const currentValue = Number(current?.value);
      const previousTimestamp = sampleTimestampMs(previous);
      const currentTimestamp = sampleTimestampMs(current);
      if (
        !Number.isFinite(previousValue)
        || !Number.isFinite(currentValue)
        || currentValue < previousValue
        || !Number.isFinite(previousTimestamp)
        || !Number.isFinite(currentTimestamp)
        || currentTimestamp <= previousTimestamp
      ) {
        continue;
      }
      totalDelta += currentValue - previousValue;
      totalHours += (currentTimestamp - previousTimestamp) / 3600000;
    }

    if (!(totalHours > 0) || totalDelta <= 0) {
      return null;
    }

    return totalDelta / totalHours;
  }

  function formatHistoryAverage(metricName, samples, windowHours = null, prefix = "Avg", referenceTimestampMs = Date.now()) {
    const ratePerHour = computeHistoryCounterAverage(samples, windowHours, referenceTimestampMs);
    if (!Number.isFinite(ratePerHour) || ratePerHour <= 0) {
      return null;
    }
    if (metricName === "bytes_written" || metricName === "bytes_read") {
      const formatted = formatMetricBytes(ratePerHour);
      if (!formatted) {
        return null;
      }
      const windowLabel = Number.isFinite(Number(windowHours)) && Number(windowHours) > 0
        ? formatHistoryWindowLabel(windowHours)
        : "";
      return `${prefix}${windowLabel ? ` ${windowLabel}` : ""} +${formatted}/hr`;
    }
    return null;
  }

  function formatHistoryRateValue(value) {
    if (!Number.isFinite(Number(value))) {
      return "n/a";
    }
    const numericValue = Number(value);
    if (numericValue === 0) {
      return "0 B/hr";
    }
    const formatted = formatMetricBytes(numericValue);
    return formatted ? `${formatted}/hr` : "n/a";
  }

  function historyMetricCard(label, metricName, payload, windowHours, referenceTimestampMs = Date.now()) {
    const allSamples = payload?.metrics?.[metricName] || [];
    const filteredSamples = filterHistorySamplesToWindow(allSamples, windowHours, referenceTimestampMs);
    const latestValue = filteredSamples.length ? filteredSamples[filteredSamples.length - 1].value : null;
    const sampleCount = filteredSamples.length;
    const totalCount = payload?.sample_counts?.[metricName] ?? allSamples.length;
    const noteParts = [];
    if (sampleCount > 0) {
      if (windowHours === null) {
        noteParts.push(`${sampleCount} stored sample${sampleCount === 1 ? "" : "s"}`);
      } else {
        noteParts.push(`${sampleCount} sample${sampleCount === 1 ? "" : "s"} in ${formatHistoryWindowLabel(windowHours)}`);
      }
    } else if (windowHours === null) {
      noteParts.push("No stored samples yet");
    } else {
      noteParts.push(`No samples in ${formatHistoryWindowLabel(windowHours)}`);
    }
    if (windowHours !== null && totalCount > sampleCount) {
      noteParts.push(`${totalCount} total stored`);
    }
    const averageLabel = formatHistoryAverage(metricName, allSamples, windowHours, "Avg", referenceTimestampMs);
    if (averageLabel) {
      noteParts.push(averageLabel);
    }
    return `
      <div class="history-metric-card">
        <div class="history-metric-label">${escapeHtml(label)}</div>
        <div class="history-metric-value">${escapeHtml(formatHistoryMetricValue(metricName, latestValue))}</div>
        <div class="history-metric-note">${escapeHtml(noteParts.join(" | "))}</div>
      </div>
    `;
  }

  function buildHistorySummary(payload, windowHours, referenceTimestampMs = Date.now()) {
    const allTimestamps = collectHistoryTimestamps(payload);
    if (!allTimestamps.length) {
      return "No slot-specific history collected yet.";
    }
    const windowLabel = formatHistoryWindowDescription(windowHours);
    const cutoff = historyWindowCutoffMs(windowHours, referenceTimestampMs);
    const filteredTimestamps = cutoff === null
      ? [...allTimestamps]
      : allTimestamps.filter((timestamp) => {
        const numericTimestamp = new Date(timestamp).getTime();
        return Number.isFinite(numericTimestamp) && numericTimestamp >= cutoff;
      });
    filteredTimestamps.sort((left, right) => new Date(right).getTime() - new Date(left).getTime());
    if (filteredTimestamps.length) {
      return `Showing ${windowLabel} • Latest point ${formatTimestamp(filteredTimestamps[0])}`;
    }

    allTimestamps.sort((left, right) => new Date(right).getTime() - new Date(left).getTime());
    return `No history points in ${windowLabel}. Latest stored point ${formatTimestamp(allTimestamps[0])}`;
  }

  function buildHistoryDrawerContext(slot) {
    if (!slot) {
      return "";
    }
    const fragments = [`Slot ${slot.slot_label}`];
    if (slot.device_name) {
      fragments.push(slot.device_name);
    } else if (slot.serial) {
      fragments.push(slot.serial);
    }
    const systemLabel = getSelectedSystemOption()?.label || state.snapshot.selected_system_label || null;
    const enclosureLabel =
      getSelectedEnclosureOption()?.label ||
      state.snapshot.selected_enclosure_label ||
      slot.enclosure_label ||
      slot.enclosure_name ||
      slot.enclosure_id ||
      null;
    if (systemLabel) {
      fragments.push(systemLabel);
    }
    if (enclosureLabel) {
      fragments.push(enclosureLabel);
    }
    return fragments.join(" | ");
  }

  function buildHistoryChartScale(sampleGroups) {
    const samples = sampleGroups
      .flat()
      .filter((sample) => Number.isFinite(Number(sample?.value)) && Number.isFinite(sampleTimestampMs(sample)));
    if (!samples.length) {
      return null;
    }

    const timestamps = samples.map((sample) => sampleTimestampMs(sample));
    const values = samples.map((sample) => Number(sample.value));
    return {
      minTimestamp: Math.min(...timestamps),
      maxTimestamp: Math.max(...timestamps),
      minValue: Math.min(...values),
      maxValue: Math.max(...values),
    };
  }

  function buildHistoryRateSamples(samples) {
    const ordered = sortHistorySamplesAscending(samples);
    if (ordered.length < 2) {
      return [];
    }

    const rateSamples = [];
    for (let index = 1; index < ordered.length; index += 1) {
      const previous = ordered[index - 1];
      const current = ordered[index];
      const previousValue = Number(previous?.value);
      const currentValue = Number(current?.value);
      const previousTimestamp = sampleTimestampMs(previous);
      const currentTimestamp = sampleTimestampMs(current);
      if (
        !Number.isFinite(previousValue)
        || !Number.isFinite(currentValue)
        || currentValue < previousValue
        || !Number.isFinite(previousTimestamp)
        || !Number.isFinite(currentTimestamp)
        || currentTimestamp <= previousTimestamp
      ) {
        continue;
      }
      const elapsedHours = (currentTimestamp - previousTimestamp) / 3600000;
      rateSamples.push({
        ...current,
        value: elapsedHours > 0 ? (currentValue - previousValue) / elapsedHours : 0,
      });
    }
    return rateSamples;
  }

  function buildHistoryChartPoints(samples, scale, width, height, padding) {
    const innerWidth = width - padding * 2;
    const innerHeight = height - padding * 2;
    const timeRange = scale.maxTimestamp - scale.minTimestamp;
    const valueRange = scale.maxValue - scale.minValue;
    return samples.map((sample) => {
      const timestamp = sampleTimestampMs(sample);
      const x = samples.length === 1 || !(timeRange > 0)
        ? width / 2
        : padding + ((timestamp - scale.minTimestamp) / timeRange) * innerWidth;
      const numericValue = Number(sample.value);
      const scaled = valueRange > 0 ? (numericValue - scale.minValue) / valueRange : 0.5;
      const y = padding + innerHeight - scaled * innerHeight;
      return [x, y];
    });
  }

  function escapeSvgTitle(value) {
    return escapeHtml(String(value)).replaceAll("\n", "&#10;");
  }

  function buildHistoryPointHoverMarkup(pointPairs, samples, className, buildTitle) {
    return pointPairs
      .map(([x, y], index) => {
        const title = buildTitle(samples[index], index);
        return `
          <circle class="${escapeHtml(className)}" cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="3.25"></circle>
          <circle class="history-hit-dot" cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="7">
            <title>${escapeSvgTitle(title)}</title>
          </circle>
        `;
      })
      .join("");
  }

  function renderHistoryChart(target, labelTarget, samples, metricName, emptyMessage, windowHours, referenceTimestampMs = Date.now()) {
    if (!target) {
      return;
    }

    const ordered = filterHistorySamplesToWindow(samples, windowHours, referenceTimestampMs);
    if (labelTarget) {
      labelTarget.textContent = ordered.length
        ? `${ordered.length} sample${ordered.length === 1 ? "" : "s"} / ${formatHistoryWindowDescription(windowHours)}`
        : formatHistoryWindowDescription(windowHours);
    }
    if (!ordered.length) {
      const scopedMessage = windowHours === null
        ? emptyMessage
        : `No ${metricName === "temperature_c" ? "temperature" : "history"} samples in ${formatHistoryWindowLabel(windowHours)} yet.`;
      target.innerHTML = `<div class="history-chart-empty">${escapeHtml(scopedMessage)}</div>`;
      return;
    }

    const values = ordered.map((sample) => Number(sample.value));
    const width = 320;
    const height = 120;
    const padding = 10;
    const scale = buildHistoryChartScale([ordered]);
    const pointPairs = buildHistoryChartPoints(ordered, scale, width, height, padding);
    const pointString = pointPairs.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
    const latestValue = values[values.length - 1];
    const areaPoints = [
      `${pointPairs[0][0].toFixed(2)},${(height - padding).toFixed(2)}`,
      pointString,
      `${pointPairs[pointPairs.length - 1][0].toFixed(2)},${(height - padding).toFixed(2)}`,
    ].join(" ");
    const firstObserved = ordered[0]?.observed_at ? formatTimestamp(ordered[0].observed_at) : "n/a";
    const lastObserved = ordered[ordered.length - 1]?.observed_at ? formatTimestamp(ordered[ordered.length - 1].observed_at) : "n/a";
    const pointMarkup = buildHistoryPointHoverMarkup(
      pointPairs,
      ordered,
      "history-dot",
      (sample) => `${formatTimestamp(sample?.observed_at)}\n${formatHistoryMetricValue(metricName, sample?.value)}`
    );

    target.innerHTML = `
      <div class="history-chart-shell">
        <div class="history-chart-meta">
          <span>Latest ${escapeHtml(formatHistoryMetricValue(metricName, latestValue))}</span>
          <span>Min ${escapeHtml(formatHistoryMetricValue(metricName, scale.minValue))}</span>
          <span>Max ${escapeHtml(formatHistoryMetricValue(metricName, scale.maxValue))}</span>
        </div>
        <svg viewBox="0 0 ${width} ${height}" class="history-chart-svg" role="img" aria-label="${escapeHtml(metricName)} history">
          <line class="history-grid-line" x1="${padding}" y1="${padding}" x2="${width - padding}" y2="${padding}"></line>
          <line class="history-grid-line" x1="${padding}" y1="${height / 2}" x2="${width - padding}" y2="${height / 2}"></line>
          <line class="history-grid-line" x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}"></line>
          <polygon class="history-area" points="${areaPoints}"></polygon>
          <polyline class="history-line" points="${pointString}"></polyline>
          ${pointMarkup}
        </svg>
        <div class="history-chart-meta">
          <span>First ${escapeHtml(firstObserved)}</span>
          <span>Latest ${escapeHtml(lastObserved)}</span>
        </div>
      </div>
    `;
  }

  function renderHistoryReadWriteChart(
    target,
    labelTarget,
    readSamples,
    writeSamples,
    mode = "total",
    windowHours = null,
    referenceTimestampMs = Date.now()
  ) {
    if (!target) {
      return;
    }

    const orderedRead = mode === "average"
      ? filterHistorySamplesToWindow(buildHistoryRateSamples(readSamples), windowHours, referenceTimestampMs)
      : filterHistorySamplesToWindow(readSamples, windowHours, referenceTimestampMs);
    const orderedWrite = mode === "average"
      ? filterHistorySamplesToWindow(buildHistoryRateSamples(writeSamples), windowHours, referenceTimestampMs)
      : filterHistorySamplesToWindow(writeSamples, windowHours, referenceTimestampMs);
    if (labelTarget) {
      const labelParts = [];
      if (orderedRead.length) {
        labelParts.push(`${orderedRead.length} ${mode === "average" ? "read rate" : "read"}`);
      }
      if (orderedWrite.length) {
        labelParts.push(`${orderedWrite.length} ${mode === "average" ? "write rate" : "write"}`);
      }
      labelParts.push(formatHistoryWindowDescription(windowHours));
      labelTarget.textContent = labelParts.join(" / ");
    }
    if (!orderedRead.length && !orderedWrite.length) {
      const emptyMessage = mode === "average"
        ? `Average read and write rates need at least two slower samples inside ${formatHistoryWindowDescription(windowHours)}.`
        : `No read or write samples in ${formatHistoryWindowDescription(windowHours)} yet.`;
      target.innerHTML = `<div class="history-chart-empty">${escapeHtml(emptyMessage)}</div>`;
      return;
    }

    const width = 320;
    const height = 120;
    const padding = 10;
    const scale = buildHistoryChartScale([orderedRead, orderedWrite]);
    const writePoints = orderedWrite.length ? buildHistoryChartPoints(orderedWrite, scale, width, height, padding) : [];
    const readPoints = orderedRead.length ? buildHistoryChartPoints(orderedRead, scale, width, height, padding) : [];
    const firstObserved = [orderedWrite[0]?.observed_at, orderedRead[0]?.observed_at]
      .filter(Boolean)
      .sort((left, right) => new Date(left).getTime() - new Date(right).getTime())[0];
    const lastObserved = [
      orderedWrite[orderedWrite.length - 1]?.observed_at,
      orderedRead[orderedRead.length - 1]?.observed_at,
    ]
      .filter(Boolean)
      .sort((left, right) => new Date(right).getTime() - new Date(left).getTime())[0];
    const writeLatestValue = orderedWrite.length ? Number(orderedWrite[orderedWrite.length - 1].value) : null;
    const readLatestValue = orderedRead.length ? Number(orderedRead[orderedRead.length - 1].value) : null;
    const writeAverage = formatHistoryAverage("bytes_written", writeSamples, windowHours, "Avg-W", referenceTimestampMs);
    const readAverage = formatHistoryAverage("bytes_read", readSamples, windowHours, "Avg-R", referenceTimestampMs);
    const readPrimaryLabel = mode === "average"
      ? formatHistoryRateValue(readLatestValue)
      : formatHistoryMetricValue("bytes_read", readLatestValue);
    const writePrimaryLabel = mode === "average"
      ? formatHistoryRateValue(writeLatestValue)
      : formatHistoryMetricValue("bytes_written", writeLatestValue);
    const chartAriaLabel = mode === "average" ? "Average read and write rate history" : "Read and write history";
    const readPointMarkup = buildHistoryPointHoverMarkup(
      readPoints,
      orderedRead,
      "history-dot history-dot-read",
      (sample) => `${formatTimestamp(sample?.observed_at)}\nRead ${mode === "average" ? formatHistoryRateValue(sample?.value) : formatHistoryMetricValue("bytes_read", sample?.value)}`
    );
    const writePointMarkup = buildHistoryPointHoverMarkup(
      writePoints,
      orderedWrite,
      "history-dot history-dot-write",
      (sample) => `${formatTimestamp(sample?.observed_at)}\nWrite ${mode === "average" ? formatHistoryRateValue(sample?.value) : formatHistoryMetricValue("bytes_written", sample?.value)}`
    );

    target.innerHTML = `
      <div class="history-chart-shell">
        <div class="history-chart-meta">
          ${readLatestValue !== null ? `<span class="history-legend-chip"><i class="history-legend-swatch read"></i>Read ${escapeHtml(readPrimaryLabel)}</span>` : ""}
          ${readAverage ? `<span>${escapeHtml(readAverage)}</span>` : ""}
          ${writeLatestValue !== null ? `<span class="history-legend-chip"><i class="history-legend-swatch write"></i>Write ${escapeHtml(writePrimaryLabel)}</span>` : ""}
          ${writeAverage ? `<span>${escapeHtml(writeAverage)}</span>` : ""}
        </div>
        <svg viewBox="0 0 ${width} ${height}" class="history-chart-svg" role="img" aria-label="${escapeHtml(chartAriaLabel)}">
          <line class="history-grid-line" x1="${padding}" y1="${padding}" x2="${width - padding}" y2="${padding}"></line>
          <line class="history-grid-line" x1="${padding}" y1="${height / 2}" x2="${width - padding}" y2="${height / 2}"></line>
          <line class="history-grid-line" x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}"></line>
          ${writePoints.length ? `<polyline class="history-line history-line-write" points="${writePoints.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ")}"></polyline>` : ""}
          ${readPoints.length ? `<polyline class="history-line history-line-read" points="${readPoints.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ")}"></polyline>` : ""}
          ${writePointMarkup}
          ${readPointMarkup}
        </svg>
        <div class="history-chart-meta">
          <span>First ${escapeHtml(firstObserved ? formatTimestamp(firstObserved) : "n/a")}</span>
          <span>Latest ${escapeHtml(lastObserved ? formatTimestamp(lastObserved) : "n/a")}</span>
        </div>
      </div>
    `;
  }

  function renderHistoryEvents(events, windowHours, referenceTimestampMs = Date.now()) {
    if (!historyEventList) {
      return;
    }
    const filteredEvents = filterHistoryEventsToWindow(events, windowHours, referenceTimestampMs);
    if (!filteredEvents.length) {
      historyEventList.innerHTML = Array.isArray(events) && events.length && windowHours !== null
        ? `<div class="warning-item muted compact">No slot-change events in ${escapeHtml(formatHistoryWindowLabel(windowHours))}.</div>`
        : '<div class="warning-item muted compact">No slot-change events have been recorded for this bay yet.</div>';
      return;
    }

    historyEventList.innerHTML = filteredEvents
      .map((event) => {
        let detailText = "";
        try {
          const details = JSON.parse(event.details_json || "{}");
          const fragments = Object.values(details).map((detail) => {
            const previousValue = formatHistoryScalar(detail?.previous);
            const currentValue = formatHistoryScalar(detail?.current);
            return `${detail?.label || "Value"}: ${previousValue} -> ${currentValue}`;
          });
          detailText = fragments.join(" | ");
        } catch (error) {
          detailText = "";
        }

        return `
          <div class="history-event-item">
            <div class="history-event-head">
              <span class="history-event-type">${escapeHtml(historyEventTypeLabel(event.event_type))}</span>
              <span class="history-event-time">${escapeHtml(formatTimestamp(event.observed_at))}</span>
            </div>
            <div class="history-event-summary">${escapeHtml(`${event.previous_value || "n/a"} -> ${event.current_value || "n/a"}`)}</div>
            ${detailText ? `<div class="history-event-detail">${escapeHtml(detailText)}</div>` : ""}
          </div>
        `;
      })
      .join("");
  }

  function renderHistoryPanel(slot) {
    if (!detailHistoryPanel || !historyToggleButton) {
      return;
    }

    const windowHours = currentHistoryWindowHours();
    const referenceTimestampMs = Date.now();
    historyIoModeButtons.forEach((button) => {
      button.classList.toggle("active", button.dataset.historyIoMode === state.history.ioChartMode);
    });
    if (historyTimeframeSelect) {
      historyTimeframeSelect.value = windowHours === null ? "all" : String(windowHours);
    }

    const shouldShowButton = Boolean(slot) && isHistoryAvailable();
    historyToggleButton.classList.toggle("hidden", !shouldShowButton);
    historyToggleButton.textContent = state.history.panelOpen ? "Hide History" : "History";
    if (historyDrawerTitle) {
      historyDrawerTitle.textContent = slot ? `Slot ${slot.slot_label} History` : "Slot History";
    }
    if (historyDrawerContext) {
      historyDrawerContext.textContent = buildHistoryDrawerContext(slot);
    }
    if (historyCloseButton) {
      historyCloseButton.classList.toggle("hidden", !slot || !shouldShowButton || !state.history.panelOpen);
    }

    if (!slot || !shouldShowButton || !state.history.panelOpen) {
      detailHistoryPanel.classList.add("hidden");
      return;
    }

    detailHistoryPanel.classList.remove("hidden");
    detailHistoryEmpty.classList.add("hidden");
    detailHistoryLoading.classList.add("hidden");
    detailHistoryError.classList.add("hidden");
    detailHistoryContent.classList.add("hidden");
    detailHistoryError.textContent = "";

    const cacheKey = getHistoryCacheKey(slot);
    const payload = state.history.slotCache[cacheKey];
    detailHistorySummary.textContent = payload?.available ? buildHistorySummary(payload, windowHours, referenceTimestampMs) : "";

    if (state.history.panelLoading) {
      detailHistoryLoading.classList.remove("hidden");
      detailHistorySummary.textContent = "Loading history...";
      return;
    }

    if (state.history.panelError) {
      detailHistoryError.textContent = state.history.panelError;
      detailHistoryError.classList.remove("hidden");
      detailHistorySummary.textContent = "History backend reachable, but this slot query failed.";
      return;
    }

    if (!payload) {
      detailHistoryEmpty.classList.remove("hidden");
      return;
    }

    if (!payload.available) {
      detailHistoryError.textContent = payload.detail || "History is unavailable for this slot.";
      detailHistoryError.classList.remove("hidden");
      return;
    }

    detailHistoryContent.classList.remove("hidden");
    historyMetricGrid.innerHTML = [
      historyMetricCard("Temperature", "temperature_c", payload, windowHours, referenceTimestampMs),
      historyMetricCard("Bytes Read", "bytes_read", payload, windowHours, referenceTimestampMs),
      historyMetricCard("Bytes Written", "bytes_written", payload, windowHours, referenceTimestampMs),
      historyMetricCard("Annualized Write", "annualized_bytes_written", payload, windowHours, referenceTimestampMs),
      historyMetricCard("Power On", "power_on_hours", payload, windowHours, referenceTimestampMs),
    ].join("");

    renderHistoryChart(
      historyTemperatureChart,
      historyTemperatureLabel,
      payload.metrics?.temperature_c || [],
      "temperature_c",
      "Temperature history will appear here after a few collection cycles.",
      windowHours,
      referenceTimestampMs
    );
    renderHistoryReadWriteChart(
      historyIoChart,
      historyIoLabel,
      payload.metrics?.bytes_read || [],
      payload.metrics?.bytes_written || [],
      state.history.ioChartMode,
      windowHours,
      referenceTimestampMs
    );
    renderHistoryEvents(payload.events || [], windowHours, referenceTimestampMs);
  }

  async function refreshHistoryStatus(silent = true) {
    if (!state.history.configured) {
      state.history.loading = false;
      state.history.checked = true;
      state.history.available = false;
      state.history.detail = null;
      state.history.counts = {};
      state.history.collector = {};
      renderStatus();
      renderHistoryPanel(getSlotById(state.selectedSlot));
      return;
    }

    state.history.loading = true;
    renderStatus();

    try {
      const payload = await fetchJson("/api/history/status");
      state.history.checked = true;
      state.history.available = Boolean(payload.available);
      state.history.detail = payload.detail || null;
      state.history.counts = payload.counts || {};
      state.history.collector = payload.collector || {};
    } catch (error) {
      state.history.checked = true;
      state.history.available = false;
      state.history.detail = error.message || String(error);
      state.history.counts = {};
      state.history.collector = {};
      if (!silent) {
        setStatus(`History status check failed: ${state.history.detail}`, "error");
      }
    } finally {
      state.history.loading = false;
    }

    renderStatus();
    renderHistoryPanel(getSlotById(state.selectedSlot));
  }

  async function loadHistoryForSelectedSlot(force = false) {
    const slot = getSlotById(state.selectedSlot);
    if (!slot || !isHistoryAvailable()) {
      return;
    }

    const cacheKey = getHistoryCacheKey(slot);
    if (!force && state.history.slotCache[cacheKey]) {
      state.history.panelError = null;
      renderHistoryPanel(slot);
      return;
    }

    state.history.panelLoading = true;
    state.history.panelError = null;
    renderHistoryPanel(slot);

    try {
      const payload = await sendScopedRequest(`/api/slots/${slot.slot}/history`);
      state.history.slotCache[cacheKey] = payload;
      if (cacheKey !== getHistoryCacheKey(getSlotById(state.selectedSlot) || slot)) {
        return;
      }
    } catch (error) {
      state.history.panelError = error.message || String(error);
    } finally {
      state.history.panelLoading = false;
    }

    renderHistoryPanel(getSlotById(state.selectedSlot));
  }

  function renderDetail() {
    const slot = getSlotById(state.selectedSlot);
    if (!slot) {
      detailEmpty.classList.remove("hidden");
      detailContent.classList.add("hidden");
      detailSmartNote.classList.add("hidden");
      detailSmartNote.textContent = "";
      detailSecondary.classList.remove("hidden");
      detailLedControls.classList.add("hidden");
      renderTopologyContext(null);
      renderMultipathContext(null);
      resetMappingForm();
      if (mappingEmpty) {
        mappingEmpty.classList.remove("hidden");
      }
      mappingForm.classList.add("hidden");
      setMappingFormEnabled(false);
      ledButtons.forEach((button) => {
        button.disabled = true;
      });
      if (historyToggleButton) {
        historyToggleButton.classList.add("hidden");
      }
      if (detailHistoryPanel) {
        detailHistoryPanel.classList.add("hidden");
      }
      return;
    }

    detailEmpty.classList.add("hidden");
    detailContent.classList.remove("hidden");
    detailSecondary.classList.remove("hidden");
    detailLedControls.classList.remove("hidden");
    if (mappingEmpty) {
      mappingEmpty.classList.add("hidden");
    }
    mappingForm.classList.remove("hidden");
    detailSlotTitle.textContent = `Slot ${slot.slot_label}`;
    detailStatePill.textContent = stateLabel(slot);
    detailStatePill.className = `state-pill state-${slot.state}`;
    const smartEntry = getSmartSummaryEntry(slot);
    const showSasTransportFields = shouldShowSasTransportFields(slot, smartEntry);
    const showLinkRate = showSasTransportFields || formatLinkRateValue(smartEntry) !== "n/a";
    const showQuantastorContext = currentPlatform() === "quantastor";

    detailKvGrid.innerHTML = [
      kvRow("Device", slot.device_name),
      kvRow("Serial", slot.serial, true),
      kvRow("Model", slot.model),
      kvRow("Size", slot.size_human),
      kvRow(persistentIdLabel(slot), slot.gptid, true),
      kvRowIfMeaningful("Namespace EUI64", formatNamespaceEui64Value(slot, smartEntry), true),
      kvRowIfMeaningful("Namespace NGUID", formatNamespaceNguidValue(smartEntry), true),
      kvRow(currentPlatform() === "linux" ? "Mount" : "Pool", slot.pool_name),
      kvRow(currentPlatform() === "linux" ? "Array" : "Vdev", slot.vdev_name),
      kvRow(currentPlatform() === "linux" ? "Role" : "Class", slot.vdev_class),
      kvRow("Topology", slot.topology_label),
      showQuantastorContext ? kvRowIfMeaningful("Presented By", formatQuantastorContextValue(slot, "presented_by_label")) : "",
      showQuantastorContext ? kvRowIfMeaningful("Pool Active On", formatQuantastorContextValue(slot, "pool_owner_label")) : "",
      showQuantastorContext ? kvRowIfMeaningful("I/O Fence On", formatQuantastorContextValue(slot, "fence_owner_label")) : "",
      showQuantastorContext ? kvRowIfMeaningful("Visible On", formatVisibleOnValue(slot)) : "",
      showQuantastorContext ? kvRowIfMeaningful("SES Host", formatSesHostValue(slot)) : "",
      kvRow("Health", slot.health),
      kvRow("Temp", formatTemperatureValue(slot, smartEntry)),
      kvRowIfMeaningful("Warning Temp", formatWarningTemperatureValue(smartEntry)),
      kvRowIfMeaningful("Critical Temp", formatCriticalTemperatureValue(smartEntry)),
      kvRowIfMeaningful("SMART Status", formatSmartHealthStatusValue(smartEntry)),
      kvRow("Last SMART Test", formatLastSmartTestValue(slot, smartEntry)),
      kvRow("Power On", formatPowerOnValue(smartEntry)),
      kvRow("Sector Size", formatSectorSizeValue(slot, smartEntry)),
      kvRow("Rotation", formatRotationValue(smartEntry)),
      kvRow("Form Factor", formatFormFactorValue(smartEntry)),
      kvRowIfMeaningful("Firmware", formatFirmwareValue(smartEntry)),
      kvRowIfMeaningful("Protocol Version", formatProtocolVersionValue(smartEntry)),
      kvRowIfMeaningful("Endurance", formatEnduranceValue(smartEntry)),
      kvRowIfMeaningful("Available Spare", formatAvailableSpareValue(smartEntry)),
      kvRowIfMeaningful("TRIM", formatTrimSupportedValue(smartEntry)),
      kvRowIfMeaningful("Bytes Read", formatBytesReadValue(smartEntry)),
      kvRowIfMeaningful("Bytes Written", formatBytesWrittenValue(smartEntry)),
      kvRowIfMeaningful("Annualized Write", formatAnnualizedWriteValue(smartEntry)),
      kvRowIfMeaningful("Est. TBW Left", formatEstimatedRemainingWriteValue(smartEntry)),
      kvRowIfMeaningful("Media Errors", formatMediaErrorsValue(smartEntry)),
      kvRowIfMeaningful("Predictive Errors", formatPredictiveErrorsValue(smartEntry)),
      kvRowIfMeaningful("Non-Medium Errors", formatNonMediumErrorsValue(smartEntry)),
      kvRowIfMeaningful("Uncorrected Read", formatUncorrectedReadErrorsValue(smartEntry)),
      kvRowIfMeaningful("Uncorrected Write", formatUncorrectedWriteErrorsValue(smartEntry)),
      kvRowIfMeaningful("Unsafe Shutdowns", formatUnsafeShutdownsValue(smartEntry)),
      kvRow("Read Cache", formatReadCacheValue(smartEntry)),
      kvRow("Writeback Cache", formatWritebackCacheValue(smartEntry)),
      kvRow("Transport", formatTransportValue(smartEntry)),
      showSasTransportFields ? kvRow("Logical Unit ID", formatLogicalUnitIdValue(slot, smartEntry)) : "",
      showSasTransportFields ? kvRow("SAS Address", formatSasAddressValue(slot, smartEntry)) : "",
      showSasTransportFields ? kvRow("Attached SAS", formatAttachedSasAddressValue(slot, smartEntry)) : "",
      showLinkRate ? kvRow("Link Rate", formatLinkRateValue(smartEntry)) : "",
      showQuantastorContext ? kvRowIfMeaningful("SES Flags", formatSesStateValue(slot)) : "",
      kvRow("Enclosure", slot.enclosure_label || slot.enclosure_name || slot.enclosure_id),
      kvRow("LED", ledStatusLabel(slot)),
      kvRow("Mapping", slot.mapping_source),
      kvRow("Notes", slot.notes),
    ].filter(Boolean).join("");

    const smartMessage = smartEntry?.data?.message;
    const ataVolumeCounterNote = (
      smartEntry?.data?.transport_protocol === "ATA"
      && (Number.isInteger(smartEntry?.data?.bytes_read) || Number.isInteger(smartEntry?.data?.bytes_written))
    )
      ? "ATA read/write totals are lifetime SMART host counters and can exceed current pool usage, especially after array initialization, parity build, or rebuild."
      : null;
    const lowHourAnnualizedNote = (
      Number.isInteger(smartEntry?.data?.power_on_hours)
      && smartEntry.data.power_on_hours < (24 * 30)
      && Number.isInteger(smartEntry?.data?.bytes_written)
    )
      ? "Annualized write is hidden until the disk has at least about 30 days of power-on time."
      : null;
    const enduranceEstimateNote = Number.isInteger(smartEntry?.data?.estimated_remaining_bytes_written)
      ? "Estimated write-endurance values extrapolate current writes against the NVMe percentage-used SMART field."
      : null;
    const smartNoteText = [smartMessage, ataVolumeCounterNote, lowHourAnnualizedNote, enduranceEstimateNote].filter(Boolean).join(" ");
    if (smartNoteText) {
      detailSmartNote.textContent = smartNoteText;
      detailSmartNote.classList.remove("hidden");
    } else {
      detailSmartNote.classList.add("hidden");
      detailSmartNote.textContent = "";
    }

    detailKvGrid.querySelectorAll("[data-copy]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(decodeURIComponent(button.dataset.copy));
          setStatus("Copied to clipboard.");
        } catch (error) {
          setStatus(`Copy failed: ${error}`, "error");
        }
      });
    });

    renderTopologyContext(slot);
    renderMultipathContext(slot);

    mappingForm.serial.value = slot.serial || "";
    mappingForm.device_name.value = slot.device_name || "";
    mappingForm.gptid.value = slot.gptid || "";
    mappingForm.notes.value = slot.notes || "";
    setMappingFormEnabled(true);
    ledButtons.forEach((button) => {
      button.disabled = !slot.led_supported;
    });
    renderHistoryPanel(slot);
    if (state.history.panelOpen && isHistoryAvailable()) {
      void loadHistoryForSelectedSlot(false);
    }
    void ensureSmartSummary(slot);
  }

  function renderTopologyContext(slot) {
    if (!topologyContext) {
      return;
    }

    if (!slot) {
      topologyContext.innerHTML = '<div class="warning-item muted">Select a mapped slot to inspect its vdev peers.</div>';
      if (currentPlatform() === "linux") {
        topologyContext.innerHTML = '<div class="warning-item muted">Select a mapped slot to inspect its storage peers.</div>';
      }
      return;
    }

    if (!slot.pool_name || !slot.vdev_name) {
      topologyContext.innerHTML = currentPlatform() === "linux"
        ? '<div class="warning-item muted">This slot is not currently tied to a mapped mdadm stack.</div>'
        : '<div class="warning-item muted">This slot is not currently tied to a pool vdev.</div>';
      return;
    }

    const vdevMembers = state.snapshot.slots
      .filter((candidate) =>
        candidate.pool_name === slot.pool_name &&
        candidate.vdev_name === slot.vdev_name &&
        candidate.vdev_class === slot.vdev_class &&
        candidate.device_name
      )
      .sort((left, right) => left.slot - right.slot);

    const ancestry = escapeHtml(slot.topology_label || `${slot.pool_name} > ${slot.vdev_name}`);
    const pills = vdevMembers
      .map((member) => {
        const selected = member.slot === slot.slot ? " selected" : "";
        return `
          <button type="button" class="topology-pill state-${member.state}${selected}" data-topology-slot="${member.slot}">
            <span>${member.slot_label}</span>
            <small>${escapeHtml(member.device_name || member.serial || member.state)}</small>
          </button>
        `;
      })
      .join("");

    topologyContext.innerHTML = `
      <div class="topology-summary">
        <div class="topology-label">Selected Path</div>
        <div class="topology-path">${ancestry}</div>
      </div>
      <div class="topology-summary">
        <div class="topology-label">Peer Slots In This Vdev</div>
        <div class="topology-pill-row">${pills}</div>
      </div>
    `;

    topologyContext.querySelectorAll("[data-topology-slot]").forEach((button) => {
      button.addEventListener("click", () => {
        const slotNumber = Number(button.dataset.topologySlot);
        if (!Number.isNaN(slotNumber)) {
          selectSlot(slotNumber);
        }
      });
    });
  }

  function renderMultipathContext(slot) {
    if (!multipathContext) {
      return;
    }

    if (currentPlatform() === "quantastor") {
      renderQuantastorContext(slot);
      return;
    }

    if (!slot) {
      multipathContext.innerHTML = currentPlatform() === "linux"
        ? '<div class="warning-item muted">Select a slot to inspect transport and member-path details when available.</div>'
        : '<div class="warning-item muted">Select a multipath-backed slot to inspect active and standby member paths.</div>';
      return;
    }

    const multipath = slot.multipath;
    if (!multipath) {
      multipathContext.innerHTML = currentPlatform() === "linux"
        ? '<div class="warning-item muted">This slot is not currently presented through a multipath stack.</div>'
        : '<div class="warning-item muted">This slot is not currently presented through gmultipath.</div>';
      return;
    }

    const activeMembers = multipath.members.filter((member) => (member.state || "").toUpperCase() === "ACTIVE");
    const passiveMembers = multipath.members.filter((member) => (member.state || "").toUpperCase() === "PASSIVE");
    const failedMembers = multipath.members.filter((member) => {
      const stateName = (member.state || "").toUpperCase();
      return stateName === "FAIL" || stateName === "FAILED";
    });
    const otherMembers = multipath.members.filter((member) => {
      const stateName = (member.state || "").toUpperCase();
      return stateName && stateName !== "ACTIVE" && stateName !== "PASSIVE" && stateName !== "FAIL" && stateName !== "FAILED";
    });

    // Keep the operator view grouped by path role so degraded shelves read as
    // "active / passive / failed" first, with oddball states falling into a
    // final catch-all bucket instead of being silently mixed together.
    const sections = [
      {
        label: activeMembers.length > 1 ? "Active Paths" : "Active Path",
        members: activeMembers,
      },
      {
        label: passiveMembers.length > 1 ? "Passive Paths" : "Passive Path",
        members: passiveMembers,
      },
      {
        label: failedMembers.length > 1 ? "Failed Paths" : "Failed Path",
        members: failedMembers,
      },
      {
        label: otherMembers.length > 1 ? "Other Member Paths" : "Other Member Path",
        members: otherMembers,
      },
    ].filter((section) => section.members.length);

    const activeControllers = summarizeControllerLabels(activeMembers);
    const passiveControllers = summarizeControllerLabels(passiveMembers);
    const failedControllers = summarizeControllerLabels(failedMembers);
    const multipathAlert = buildMultipathAlert(multipath, activeControllers, passiveControllers, failedControllers);

    multipathContext.innerHTML = `
      <div class="topology-summary">
        <div class="topology-label">Multipath Device</div>
        <div class="topology-path">${escapeHtml(multipath.device_name)}</div>
      </div>
      ${multipathAlert ? `<div class="warning-item compact">${escapeHtml(multipathAlert)}</div>` : ""}
      <div class="topology-grid">
        ${topologyInfoCard("Mode", multipath.mode)}
        ${topologyInfoCard("State", multipath.state || multipath.provider_state)}
        ${topologyInfoCard("Transport", multipath.bus)}
        ${topologyInfoCard("LUN ID", multipath.lunid)}
        ${topologyInfoCardIfPresent(activeMembers.length > 1 ? "Active HBAs" : "Active HBA", activeControllers)}
        ${topologyInfoCardIfPresent(passiveMembers.length > 1 ? "Passive HBAs" : "Passive HBA", passiveControllers)}
        ${topologyInfoCardIfPresent(failedMembers.length > 1 ? "Failed HBAs" : "Failed HBA", failedControllers)}
      </div>
      ${sections.length
        ? sections
            .map(
              (section) => `
                <div class="topology-summary">
                  <div class="topology-label">${escapeHtml(section.label)}</div>
                  <div class="topology-pill-row">${renderMultipathPills(section.members)}</div>
                </div>
              `
            )
            .join("")
        : `
            <div class="topology-summary">
              <div class="topology-label">Member Paths</div>
              <div class="topology-pill-row">${renderMultipathPills(multipath.members)}</div>
            </div>
          `}
    `;
  }

  function renderQuantastorContext(slot) {
    if (!multipathContext) {
      return;
    }

    const clusterContext = currentPlatformContext();
    if (!slot) {
      multipathContext.innerHTML = '<div class="warning-item muted">Select a slot to inspect Quantastor ownership and fencing context.</div>';
      return;
    }

    const context = operatorContext(slot);
    const visibleOnLabels = Array.isArray(context.visible_on_labels) ? context.visible_on_labels : [];
    const visibleOnMarkup = visibleOnLabels.length
      ? visibleOnLabels.map((label) => `<div class="context-chip">${escapeHtml(label)}</div>`).join("")
      : '<div class="warning-item muted compact">Only the current node has reported this disk so far.</div>';
    const clusterRows = [
      contextRow("Selected View", clusterContext.selected_view_label),
      contextRow("Cluster Master", clusterContext.master_label),
      contextRow("Peer Nodes", Array.isArray(clusterContext.peer_labels) && clusterContext.peer_labels.length ? clusterContext.peer_labels.join(", ") : null),
      contextRow(
        "I/O Fencing",
        clusterContext.io_fencing_enabled === true ? "Enabled" : clusterContext.io_fencing_enabled === false ? "Disabled" : null
      ),
    ].filter(Boolean).join("");
    const slotRows = [
      contextRow("Presented By", context.presented_by_label),
      contextRow("Pool Active On", context.pool_owner_label),
      contextRow("I/O Fence On", context.fence_owner_label),
      contextRow("Pool Device Node", context.pool_device_node_label),
      contextRow("Ownership Rev", context.ownership_revision),
    ].filter(Boolean).join("");
    const notes = Array.isArray(context.notes) ? context.notes.filter(Boolean) : [];

    multipathContext.innerHTML = `
      <div class="topology-summary">
        <div class="topology-label">Cluster State</div>
        <div class="context-grid">${clusterRows || '<div class="warning-item muted compact">No extra cluster metadata is available.</div>'}</div>
      </div>
      <div class="topology-summary">
        <div class="topology-label">Slot Ownership</div>
        <div class="context-grid">${slotRows || '<div class="warning-item muted compact">This slot has no extra ownership overlay yet.</div>'}</div>
      </div>
      <div class="topology-summary">
        <div class="topology-label">Visible On</div>
        <div class="topology-pill-row">${visibleOnMarkup}</div>
      </div>
      ${notes.map((note) => `<div class="warning-item compact">${escapeHtml(note)}</div>`).join("")}
    `;
  }

  function contextRow(label, value) {
    if (value === null || value === undefined || value === "" || value === "n/a") {
      return "";
    }
    return `
      <div class="context-row">
        <span class="context-label">${escapeHtml(label)}</span>
        <span class="context-value">${escapeHtml(value)}</span>
      </div>
    `;
  }

  function topologyInfoCard(label, value) {
    const safeValue = escapeHtml(value || "n/a");
    return `
      <div class="topology-mini-card">
        <div class="topology-label">${escapeHtml(label)}</div>
        <div class="topology-path">${safeValue}</div>
      </div>
    `;
  }

  function topologyInfoCardIfPresent(label, value) {
    if (!value) {
      return "";
    }
    return topologyInfoCard(label, value);
  }

  function summarizeControllerLabels(members) {
    const labels = Array.from(
      new Set(
        members
          .map((member) => member.controller_label)
          .filter((value) => value && value.trim())
      )
    );

    if (!labels.length) {
      return null;
    }

    return labels.join(", ");
  }

  function buildMultipathAlert(multipath, activeControllers, passiveControllers, failedControllers) {
    const stateName = (multipath.state || multipath.provider_state || "").toUpperCase();
    if (stateName !== "DEGRADED" && !failedControllers) {
      return "";
    }

    const fragments = [];
    if (activeControllers) {
      fragments.push(`active on ${activeControllers}`);
    }
    if (passiveControllers) {
      fragments.push(`standby on ${passiveControllers}`);
    }
    if (failedControllers) {
      fragments.push(`failed on ${failedControllers}`);
    }

    if (!fragments.length) {
      return "Multipath is degraded.";
    }

    return `Multipath is degraded: ${fragments.join(", ")}.`;
  }

  function renderMultipathPills(members) {
    if (!members.length) {
      return '<div class="warning-item muted compact">No member-path detail returned.</div>';
    }

    return members
      .map((member) => {
        const stateName = (member.state || "Unknown").toUpperCase();
        const detailParts = [stateName];
        if (member.controller_label) {
          detailParts.push(member.controller_label);
        }
        return `
          <div class="topology-pill path-state-${stateName.toLowerCase()}">
            <span>${escapeHtml(member.device_name)}</span>
            <small>${escapeHtml(detailParts.join(" / "))}</small>
          </div>
        `;
      })
      .join("");
  }

  function renderWarnings() {
    const warnings = state.snapshot.warnings || [];
    warningList.innerHTML = warnings.length
      ? warnings.map((warning) => `<div class="warning-item">${escapeHtml(warning)}</div>`).join("")
      : '<div class="warning-item muted">No warnings.</div>';
  }

  function renderStatus() {
    const api = state.snapshot.sources?.api || { ok: false, message: "Unavailable" };
    const ssh = state.snapshot.sources?.ssh || { enabled: false, ok: true, message: "Disabled" };

    apiStatusChip.className = `status-chip ${api.ok ? "ok" : "error"}`;
    apiStatusChip.textContent = api.ok ? "API OK" : "API ERR";

    let sshClass = "ok";
    if (!ssh.enabled) {
      sshClass = "partial";
    } else if (!ssh.ok) {
      sshClass = "error";
    }
    sshStatusChip.className = `status-chip ${sshClass}`;
    sshStatusChip.textContent = !ssh.enabled ? "SSH OFF" : ssh.ok ? "SSH OK" : "SSH ERR";

    if (historyStatusChip) {
      if (!state.history.configured) {
        historyStatusChip.className = "status-chip hidden";
        historyStatusChip.textContent = "HIST";
        historyStatusChip.title = "";
      } else {
        let historyClass = "partial";
        let historyText = "HIST ...";
        if (state.history.available) {
          historyClass = "ok";
          historyText = "HIST OK";
        } else if (state.history.checked && !state.history.loading) {
          historyText = "HIST OFF";
        }

        const trackedSlots = state.history.counts?.tracked_slots;
        const metricSamples = state.history.counts?.metric_sample_count;
        const lastCompletedAt = state.history.collector?.last_success_at || state.history.collector?.last_completed_at;
        const detailParts = [];
        if (Number.isInteger(trackedSlots)) {
          detailParts.push(`${trackedSlots} tracked slots`);
        }
        if (Number.isInteger(metricSamples)) {
          detailParts.push(`${metricSamples} samples`);
        }
        if (lastCompletedAt) {
          detailParts.push(`Last run ${formatTimestamp(lastCompletedAt)}`);
        }
        if (!detailParts.length && state.history.detail) {
          detailParts.push(state.history.detail);
        }
        if (!detailParts.length && state.history.loading) {
          detailParts.push("Checking optional history backend.");
        }

        historyStatusChip.className = `status-chip ${historyClass}`;
        historyStatusChip.textContent = historyText;
        historyStatusChip.title = detailParts.join(" | ");
      }
    }

    lastUpdated.textContent = formatTimestamp(state.snapshot.last_updated);
    lastUpdated.title = "Rendered in your browser's local timezone.";
    renderTimezoneLabel();
    syncLocation();
  }

  function renderSummary() {
    const summary = state.snapshot.summary || {};
    summaryDiskCount.textContent = String(summary.disk_count ?? 0);
    summaryPoolCount.textContent = String(summary.pool_count ?? 0);
    summaryEnclosureCount.textContent = String(summary.enclosure_count ?? 0);
    summaryMappedSlotCount.textContent = String(summary.mapped_slot_count ?? 0);
    summaryManualMappingCount.textContent = String(summary.manual_mapping_count ?? 0);
    summarySshSlotHintCount.textContent = String(summary.ssh_slot_hint_count ?? 0);
  }

  function renderViewChrome() {
    const profile = buildViewProfile();
    if (headerEyebrow) {
      headerEyebrow.textContent = profile.eyebrow;
    }
    if (headerSummary) {
      headerSummary.textContent = profile.summary;
    }
    if (enclosurePanelTitle) {
      enclosurePanelTitle.textContent = profile.enclosureTitle;
    }
    if (enclosureEdgeLabel) {
      enclosureEdgeLabel.textContent = profile.edgeLabel;
    }
    if (chassisShell) {
      chassisShell.dataset.faceStyle = profile.faceStyle;
      chassisShell.dataset.latchEdge = profile.latchEdge;
    }
    applyChassisDriveScale(profile);
    if (secondaryContextTitle) {
      secondaryContextTitle.textContent = currentPlatform() === "quantastor" ? "HA Context" : "Multipath Presentation";
    }
  }

  function renderRefreshControls() {
    autoRefreshToggle.checked = state.autoRefresh;
    refreshIntervalSelect.value = String(state.refreshIntervalSeconds);
    refreshIntervalSelect.disabled = !state.autoRefresh;
  }

  function renderSelectors() {
    const systems = state.snapshot.systems || [];
    const enclosures = state.snapshot.enclosures || [];

    if (systemSelect) {
      systemSelect.innerHTML = systems
        .map((system) => {
          const selected = system.id === state.selectedSystemId ? " selected" : "";
          return `<option value="${escapeHtml(system.id)}"${selected}>${escapeHtml(system.label)}</option>`;
        })
        .join("");
      if (state.selectedSystemId) {
        systemSelect.value = state.selectedSystemId;
      }
      systemSelect.disabled = systems.length <= 1;
    }

    if (enclosureSelect) {
      if (!enclosures.length) {
        enclosureSelect.innerHTML = '<option value="">Auto-selected</option>';
      } else {
        enclosureSelect.innerHTML = enclosures
          .map((enclosure) => {
            const selected = enclosure.id === state.selectedEnclosureId ? " selected" : "";
            return `<option value="${escapeHtml(enclosure.id)}"${selected}>${escapeHtml(enclosure.label)}</option>`;
          })
          .join("");
      }
      enclosureSelect.value = state.selectedEnclosureId || "";
      enclosureSelect.disabled = enclosures.length <= 1;
    }
  }

  function renderAll() {
    renderViewChrome();
    renderGrid();
    renderDetail();
    renderWarnings();
    renderStatus();
    renderSummary();
    renderRefreshControls();
    renderSelectors();
  }

  function selectSlot(slotNumber) {
    state.selectedSlot = slotNumber;
    state.history.panelError = null;
    renderAll();
  }

  function clearSelectedSlot() {
    state.selectedSlot = null;
    state.history.panelError = null;
    renderAll();
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.detail || `Request failed with ${response.status}`);
    }
    return payload;
  }

  async function sendScopedRequest(url, options = {}) {
    const params = buildSelectionParams();
    const scopedUrl = params.toString() ? `${url}?${params.toString()}` : url;
    return fetchJson(scopedUrl, options);
  }

  async function refreshSnapshot(force = false) {
    try {
      setStatus(force ? "Refreshing inventory..." : "Auto-refreshing inventory...");
      const params = buildSelectionParams();
      params.set("force", force ? "true" : "false");
      const snapshot = await fetchJson(`/api/inventory?${params.toString()}`);
      applySnapshot(snapshot);
      renderAll();
      scheduleSmartPrefetch();
      void refreshHistoryStatus(true);
      setStatus("Inventory updated.");
    } catch (error) {
      setStatus(`Refresh failed: ${error.message || error}`, "error");
    }
  }

  async function sendLedAction(action) {
    const slot = getSlotById(state.selectedSlot);
    if (!slot) return;
    if (!slot.led_supported) {
      setStatus(slot.led_reason || `LED control is unavailable for slot ${slot.slot_label}.`, "error");
      return;
    }
    try {
      setStatus(`Sending ${action} for slot ${slot.slot_label}...`);
      const payload = await sendScopedRequest(`/api/slots/${slot.slot}/led`, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      applySnapshot(payload.snapshot);
      renderAll();
      scheduleSmartPrefetch();
      setStatus(`Slot ${slot.slot_label} LED action ${action} completed via ${ledBackendLabel(slot)}.`);
    } catch (error) {
      setStatus(`LED action failed: ${error.message || error}`, "error");
    }
  }

  async function saveMapping(event) {
    event.preventDefault();
    const slot = getSlotById(state.selectedSlot);
    if (!slot) return;

    const formData = new FormData(mappingForm);
    const payload = {
      serial: formData.get("serial") || null,
      device_name: formData.get("device_name") || null,
      gptid: formData.get("gptid") || null,
      notes: formData.get("notes") || null,
      clear_identify_after_save: Boolean(formData.get("clear_identify_after_save")),
    };

    try {
      setStatus(`Saving calibration for slot ${slot.slot_label}...`);
      const result = await sendScopedRequest(`/api/slots/${slot.slot}/mapping`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      applySnapshot(result.snapshot);
      renderAll();
      scheduleSmartPrefetch();
      setStatus(result.warning || `Saved mapping for slot ${slot.slot_label}.`);
    } catch (error) {
      setStatus(`Save mapping failed: ${error.message || error}`, "error");
    }
  }

  async function clearMapping() {
    const slot = getSlotById(state.selectedSlot);
    if (!slot) return;

    if (!window.confirm(`Clear the saved mapping for slot ${slot.slot_label}?`)) {
      return;
    }

    try {
      setStatus(`Clearing mapping for slot ${slot.slot_label}...`);
      const result = await sendScopedRequest(`/api/slots/${slot.slot}/mapping`, { method: "DELETE" });
      applySnapshot(result.snapshot);
      renderAll();
      scheduleSmartPrefetch();
      setStatus(`Cleared mapping for slot ${slot.slot_label}.`);
    } catch (error) {
      setStatus(`Clear mapping failed: ${error.message || error}`, "error");
    }
  }

  async function exportMappings() {
    try {
      setStatus("Preparing mapping export...");
      const bundle = await sendScopedRequest("/api/mappings/export");
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
      const objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = `slot-mappings-${formatScopeLabel()}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(objectUrl);
      setStatus(`Exported ${bundle.mappings?.length || 0} mappings.`);
    } catch (error) {
      setStatus(`Export failed: ${error.message || error}`, "error");
    }
  }

  async function importMappingsFromFile(file) {
    if (!file) return;
    try {
      setStatus(`Importing mappings from ${file.name}...`);
      const rawText = await file.text();
      const bundle = JSON.parse(rawText);
      const result = await sendScopedRequest("/api/mappings/import", {
        method: "POST",
        body: JSON.stringify(bundle),
      });
      applySnapshot(result.snapshot);
      renderAll();
      scheduleSmartPrefetch();
      setStatus(`Imported ${result.imported} mappings into the active scope.`);
    } catch (error) {
      setStatus(`Import failed: ${error.message || error}`, "error");
    } finally {
      if (mappingImportFile) {
        mappingImportFile.value = "";
      }
    }
  }

  function prefillMapping() {
    const slot = getSlotById(state.selectedSlot);
    if (!slot) return;
    mappingForm.serial.value = slot.serial || "";
    mappingForm.device_name.value = slot.device_name || "";
    mappingForm.gptid.value = slot.gptid || "";
  }

  function resetMappingForm() {
    mappingForm.serial.value = "";
    mappingForm.device_name.value = "";
    mappingForm.gptid.value = "";
    mappingForm.notes.value = "";
  }

  function setMappingFormEnabled(enabled) {
    mappingForm.querySelectorAll("input, textarea, button").forEach((element) => {
      element.disabled = !enabled;
    });
    prefillMappingButton.disabled = !enabled;
  }

  async function ensureSmartSummary(slot) {
    const cacheKey = getSmartCacheKey(slot);
    const entry = state.smartSummaries[cacheKey];
    if (isSmartEntryCurrent(entry) && (entry?.data || isSmartEntryInFlight(entry))) {
      if (state.hoveredSlot === slot.slot) {
        refreshHoveredTooltip();
      }
      return;
    }

    state.smartSummaries[cacheKey] = {
      loading: !entry?.data,
      refreshing: Boolean(entry?.data),
      data: entry?.data || null,
      requestedAt: Date.now(),
      generation: state.smartSummaryGeneration,
    };
    if (state.hoveredSlot === slot.slot) {
      refreshHoveredTooltip();
    }
    try {
      const payload = await sendScopedRequest(`/api/slots/${slot.slot}/smart`);
      state.smartSummaries[cacheKey] = {
        loading: false,
        refreshing: false,
        data: payload,
        requestedAt: Date.now(),
        generation: state.smartSummaryGeneration,
      };
    } catch (error) {
      state.smartSummaries[cacheKey] = {
        loading: false,
        refreshing: false,
        data: entry?.data || { available: false, message: error.message || String(error) },
        requestedAt: Date.now(),
        generation: state.smartSummaryGeneration,
      };
    }

    if (state.selectedSlot === slot.slot) {
      renderDetail();
    }
    if (state.hoveredSlot === slot.slot) {
      refreshHoveredTooltip();
    }
  }

  function formatRefreshInterval(seconds) {
    switch (seconds) {
      case 15:
        return "15 sec";
      case 30:
        return "30 sec";
      case 60:
        return "1 min";
      case 300:
        return "5 min";
      default:
        return `${seconds} sec`;
    }
  }

  function resetTimer() {
    if (state.timerId) {
      window.clearInterval(state.timerId);
      state.timerId = null;
    }
    if (!state.autoRefresh) return;
    state.timerId = window.setInterval(() => refreshSnapshot(false), state.refreshIntervalSeconds * 1000);
  }

  searchBox.addEventListener("input", (event) => {
    state.search = event.target.value.trim().toLowerCase();
    renderGrid();
  });

  refreshButton.addEventListener("click", () => refreshSnapshot(true));
  if (systemSelect) {
    systemSelect.addEventListener("change", async (event) => {
      state.selectedSystemId = event.target.value || null;
      state.selectedEnclosureId = null;
      clearSelectedSlot();
      await refreshSnapshot(true);
    });
  }
  if (enclosureSelect) {
    enclosureSelect.addEventListener("change", async (event) => {
      state.selectedEnclosureId = event.target.value || null;
      clearSelectedSlot();
      await refreshSnapshot(true);
    });
  }
  if (enclosureFace) {
    enclosureFace.addEventListener("click", (event) => {
      if (event.target.closest(".slot-tile")) {
        return;
      }
      clearSelectedSlot();
    });
  }
  autoRefreshToggle.addEventListener("change", (event) => {
    state.autoRefresh = event.target.checked;
    resetTimer();
    renderRefreshControls();
  });
  refreshIntervalSelect.addEventListener("change", (event) => {
    const selected = Number(event.target.value);
    state.refreshIntervalSeconds = supportedRefreshIntervals.includes(selected) ? selected : 30;
    resetTimer();
    renderRefreshControls();
    setStatus(`Auto-refresh interval set to ${formatRefreshInterval(state.refreshIntervalSeconds)}.`);
  });
  mappingForm.addEventListener("submit", saveMapping);
  clearMappingButton.addEventListener("click", clearMapping);
  prefillMappingButton.addEventListener("click", prefillMapping);
  if (exportMappingsButton) {
    exportMappingsButton.addEventListener("click", exportMappings);
  }
  if (importMappingsButton && mappingImportFile) {
    importMappingsButton.addEventListener("click", () => mappingImportFile.click());
    mappingImportFile.addEventListener("change", (event) => {
      const [file] = event.target.files || [];
      if (!file) return;
      importMappingsFromFile(file);
    });
  }
  if (historyCloseButton) {
    historyCloseButton.addEventListener("click", () => {
      state.history.panelOpen = false;
      renderHistoryPanel(getSlotById(state.selectedSlot));
    });
  }
  if (historyToggleButton) {
    historyToggleButton.addEventListener("click", () => {
      if (!state.selectedSlot || !isHistoryAvailable()) {
        return;
      }
      state.history.panelOpen = !state.history.panelOpen;
      renderHistoryPanel(getSlotById(state.selectedSlot));
      if (state.history.panelOpen) {
        if (detailHistoryPanel) {
          detailHistoryPanel.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        void loadHistoryForSelectedSlot(false);
      }
    });
  }
  if (historyTimeframeSelect) {
    historyTimeframeSelect.addEventListener("change", () => {
      const nextValue = historyTimeframeSelect.value;
      state.history.timeframeHours = nextValue === "all" ? null : Number(nextValue) || DEFAULT_HISTORY_TIMEFRAME_HOURS;
      renderHistoryPanel(getSlotById(state.selectedSlot));
    });
  }
  historyIoModeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const nextMode = button.dataset.historyIoMode === "average" ? "average" : "total";
      if (state.history.ioChartMode === nextMode) {
        return;
      }
      state.history.ioChartMode = nextMode;
      renderHistoryPanel(getSlotById(state.selectedSlot));
    });
  });

  document.querySelectorAll("[data-led-action]").forEach((button) => {
    button.addEventListener("click", () => sendLedAction(button.dataset.ledAction));
  });

  renderAll();
  void refreshHistoryStatus(true);
  scheduleSmartPrefetch();
  resetTimer();
})();
