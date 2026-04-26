(function () {
  const bootstrap = window.APP_BOOTSTRAP || {};
  const supportedRefreshIntervals = [15, 30, 60, 300];
  const bootstrapRefreshInterval = Number(bootstrap.refreshIntervalSeconds) || 30;
  const SMART_BATCH_REQUEST_MAX_CONCURRENCY = Math.max(1, Number(bootstrap.smartBatchMaxConcurrency) || 12);
  const SMART_PREFETCH_DELAY_MS = Math.max(1, Number(bootstrap.smartPrefetchDelayMs) || 120);
  const SMART_PREFETCH_STRATEGY = ["auto", "single", "chunked"].includes(String(bootstrap.smartPrefetchStrategy || "").toLowerCase())
    ? String(bootstrap.smartPrefetchStrategy).toLowerCase()
    : "auto";
  const SMART_PREFETCH_SINGLE_THRESHOLD = Math.max(1, Number(bootstrap.smartPrefetchSingleThreshold) || 128);
  const SMART_PREFETCH_CHUNK_SIZE = Math.max(1, Number(bootstrap.smartPrefetchChunkSize) || 24);
  const SMART_PREFETCH_BATCH_CONCURRENCY = Math.max(1, Number(bootstrap.smartPrefetchBatchConcurrency) || 2);
  const SMART_PREFETCH_STALE_MS = 15000;
  const SMART_SUMMARY_CACHE_TTL_MS = 5 * 60 * 1000;
  const HISTORY_STATUS_CACHE_TTL_MS = 15 * 1000;
  const DEFAULT_HISTORY_TIMEFRAME_HOURS = 24;
  const HISTORY_UI_STORAGE_KEY = "truenas-jbod-ui.history-ui.v1";
  const EXPORT_UI_STORAGE_KEY = "truenas-jbod-ui.export-ui.v1";
  const snapshotMode = Boolean(bootstrap.snapshotMode);
  const UI_PERF_ENABLED = Boolean(bootstrap.uiPerfEnabled) && !snapshotMode;
  const UI_PERF_HISTORY_LIMIT = 6;
  const persistedHistoryUi = snapshotMode ? null : loadStoredJson(HISTORY_UI_STORAGE_KEY);
  const persistedExportUi = snapshotMode ? null : loadStoredJson(EXPORT_UI_STORAGE_KEY);
  const initialHistoryWindowHours = bootstrap.initialHistoryTimeframeHours === null
    ? null
    : Number(bootstrap.initialHistoryTimeframeHours) || DEFAULT_HISTORY_TIMEFRAME_HOURS;
  const restoredHistoryWindowHours = snapshotMode
    ? initialHistoryWindowHours
    : normalizePersistedHistoryWindowHours(
        persistedHistoryUi?.timeframeHours,
        initialHistoryWindowHours
      );
  const restoredHistoryIoChartMode = snapshotMode
    ? (bootstrap.initialHistoryIoChartMode === "average" ? "average" : "total")
    : normalizePersistedIoChartMode(persistedHistoryUi?.ioChartMode);
  const preloadedHistoryBySlot = bootstrap.preloadedHistoryBySlot || {};
  const preloadedSmartSummariesBySlot = bootstrap.preloadedSmartSummariesBySlot || {};
  const preloadedHistorySummary = bootstrap.preloadedHistorySummary || { counts: {}, collector: {} };
  const availableSetupProfiles = Array.isArray(bootstrap.availableSetupProfiles) ? bootstrap.availableSetupProfiles : [];
  const setupPlatformDefaults = bootstrap.setupPlatformDefaults || {};
  const snapshotHistoryAvailable = Boolean(bootstrap.historyConfigured);
  const initialSelectedSlot = Number.isInteger(bootstrap.initialSelectedSlot)
    ? bootstrap.initialSelectedSlot
    : null;
  const initialStorageViewId = snapshotMode
    ? ""
    : (new URLSearchParams(window.location.search).get("storage_view_id") || "");
  const state = {
    snapshotMode,
    snapshotExportMeta: bootstrap.snapshotExportMeta || null,
    snapshot: bootstrap.snapshot || { slots: [], systems: [], enclosures: [] },
    storageViewsRuntime: bootstrap.storageViewsRuntime || { system_id: bootstrap.snapshot?.selected_system_id || null, views: [] },
    selectedStorageViewRuntimeId: initialStorageViewId,
    storageViewsRuntimeLoading: false,
    layoutRows: bootstrap.layoutRows || [],
    selectedSlot: initialSelectedSlot,
    hoveredSlot: null,
    selectedSystemId: bootstrap.snapshot?.selected_system_id || null,
    selectedEnclosureId: bootstrap.snapshot?.selected_enclosure_id || null,
    snapshotReuseCache: {},
    search: "",
    autoRefresh: snapshotMode ? false : true,
    refreshIntervalSeconds: supportedRefreshIntervals.includes(bootstrapRefreshInterval) ? bootstrapRefreshInterval : 30,
    timerId: null,
    identifyVerifyTimerId: null,
    refreshesInFlight: 0,
    latestRefreshToken: 0,
    storageViewsRuntimeRequestToken: 0,
    smartSummaries: {},
    preloadedSmartSummariesBySlot,
    smartSummaryGeneration: 0,
    smartPrefetchToken: 0,
    smartPrefetchTimerId: null,
    smartPrefetchRunning: false,
    smartPrefetchScopeKey: null,
    export: {
      redactSensitive: Boolean(persistedExportUi?.redactSensitive),
      packaging: normalizePersistedPackaging(persistedExportUi?.packaging),
      allowOversize: Boolean(persistedExportUi?.allowOversize),
      running: false,
      estimate: {
        loading: false,
        error: null,
        data: null,
        requestToken: 0,
      },
    },
    history: {
      configured: snapshotMode ? snapshotHistoryAvailable : Boolean(bootstrap.historyConfigured),
      checked: snapshotMode,
      available: snapshotMode ? snapshotHistoryAvailable : false,
      loading: false,
      statusFetchedAt: snapshotMode ? Date.now() : 0,
      statusRefreshPromise: null,
      detail: snapshotMode ? null : null,
      counts: snapshotMode ? (preloadedHistorySummary.counts || {}) : {},
      collector: snapshotMode ? (preloadedHistorySummary.collector || {}) : {},
      panelOpen: snapshotMode ? Boolean(bootstrap.initialHistoryPanelOpen) : false,
      timeframeHours: restoredHistoryWindowHours,
      ioChartMode: restoredHistoryIoChartMode,
      panelLoading: false,
      panelError: null,
        slotCache: preloadedHistoryBySlot,
      },
    setup: {
      step: 0,
      exportRunning: false,
      importRunning: false,
      createRunning: false,
      defaultsLoadedForPlatform: null,
      backupPackaging: "tar.zst",
      sshKeys: [],
      sshKeysLoading: false,
      sshKeyMode: "reuse",
    },
    uiPerf: {
      enabled: UI_PERF_ENABLED,
      runCounter: 0,
      currentRun: null,
      recentRuns: [],
    },
    platformDetails: {
      open: false,
      expandedSections: {},
    },
  };

  const grid = document.getElementById("slot-grid");
  const enclosureFace = document.querySelector(".enclosure-face");
  const headerEyebrow = document.getElementById("header-eyebrow");
  const headerSummary = document.getElementById("header-summary");
  const enclosurePanelTitle = document.getElementById("enclosure-panel-title");
  const enclosureEdgeLabel = document.getElementById("enclosure-edge-label");
  const platformDetailsToggleButton = document.getElementById("platform-details-toggle-button");
  const platformDetailsPanel = document.getElementById("platform-details-panel");
  const platformDetailsTitle = document.getElementById("platform-details-title");
  const platformDetailsSummary = document.getElementById("platform-details-summary");
  const platformDetailsSections = document.getElementById("platform-details-sections");
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
  const snapshotGeneratedValue = document.getElementById("snapshot-generated-value");
  const snapshotGeneratedNote = document.getElementById("snapshot-generated-note");
  const snapshotStatusChip = document.getElementById("snapshot-status-chip");
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
  const storageViewsPanel = document.getElementById("storage-views-panel");
  const storageViewsSummary = document.getElementById("storage-views-summary");
  const storageViewList = document.getElementById("storage-view-list");
  const storageViewEmpty = document.getElementById("storage-view-empty");
  const storageViewContent = document.getElementById("storage-view-content");
  const storageViewTitle = document.getElementById("storage-view-title");
  const storageViewNote = document.getElementById("storage-view-note");
  const storageViewMeta = document.getElementById("storage-view-meta");
  const storageViewGrid = document.getElementById("storage-view-grid");
  const storageViewMappingList = document.getElementById("storage-view-mapping-list");
  const mappingForm = document.getElementById("mapping-form");
  const clearMappingButton = document.getElementById("clear-mapping-button");
  const prefillMappingButton = document.getElementById("prefill-mapping-button");
  const exportMappingsButton = document.getElementById("export-mappings-button");
  const importMappingsButton = document.getElementById("import-mappings-button");
  const mappingImportFile = document.getElementById("mapping-import-file");
  const mappingEmpty = document.getElementById("mapping-empty");
  const exportSnapshotButton = document.getElementById("export-snapshot-button");
  const exportSnapshotDialog = document.getElementById("export-snapshot-dialog");
  const exportRedactToggle = document.getElementById("export-redact-toggle");
  const exportPackagingSelect = document.getElementById("export-packaging-select");
  const exportAllowOversizeToggle = document.getElementById("export-allow-oversize-toggle");
  const exportSnapshotConfirm = document.getElementById("export-snapshot-confirm");
  const exportSnapshotCancel = document.getElementById("export-snapshot-cancel");
  const exportSnapshotNote = document.getElementById("export-snapshot-note");
  const exportSnapshotWindowHint = document.getElementById("export-snapshot-window-hint");
  const exportSnapshotEstimate = document.getElementById("export-snapshot-estimate");
  const systemSetupButton = document.getElementById("system-setup-button");
  const systemSetupDialog = document.getElementById("system-setup-dialog");
  const systemSetupClose = document.getElementById("system-setup-close");
  const systemBackupPackagingSelect = document.getElementById("system-backup-packaging-select");
  const systemBackupEncryptToggle = document.getElementById("system-backup-encrypt-toggle");
  const systemBackupExportPassphrase = document.getElementById("system-backup-export-passphrase");
  const systemBackupImportPassphrase = document.getElementById("system-backup-import-passphrase");
  const exportSystemBackupButton = document.getElementById("export-system-backup-button");
  const importSystemBackupButton = document.getElementById("import-system-backup-button");
  const systemBackupImportFile = document.getElementById("system-backup-import-file");
  const setupStepIndicators = Array.from(document.querySelectorAll("[data-setup-step-indicator]"));
  const setupStepPanels = Array.from(document.querySelectorAll("[data-setup-step-panel]"));
  const setupPrevButton = document.getElementById("setup-prev-button");
  const setupNextButton = document.getElementById("setup-next-button");
  const setupCreateButton = document.getElementById("setup-create-button");
  const setupPlatformHelp = document.getElementById("setup-platform-help");
  const setupSystemLabel = document.getElementById("setup-system-label");
  const setupSystemId = document.getElementById("setup-system-id");
  const setupPlatformSelect = document.getElementById("setup-platform-select");
  const setupProfileSelect = document.getElementById("setup-profile-select");
  const setupMakeDefaultToggle = document.getElementById("setup-make-default-toggle");
  const setupTruenasHost = document.getElementById("setup-truenas-host");
  const setupVerifySslToggle = document.getElementById("setup-verify-ssl-toggle");
  const setupEnclosureFilter = document.getElementById("setup-enclosure-filter");
  const setupApiKey = document.getElementById("setup-api-key");
  const setupApiUser = document.getElementById("setup-api-user");
  const setupApiPassword = document.getElementById("setup-api-password");
  const setupSshEnabledToggle = document.getElementById("setup-ssh-enabled-toggle");
  const setupSshHost = document.getElementById("setup-ssh-host");
  const setupSshUser = document.getElementById("setup-ssh-user");
  const setupSshPort = document.getElementById("setup-ssh-port");
  const setupSshKeyMode = document.getElementById("setup-ssh-key-mode");
  const setupSshExistingKeySelect = document.getElementById("setup-ssh-existing-key-select");
  const setupRefreshSshKeysButton = document.getElementById("setup-refresh-ssh-keys-button");
  const setupGenerateSshKeyButton = document.getElementById("setup-generate-ssh-key-button");
  const setupSshGenerateName = document.getElementById("setup-ssh-generate-name");
  const setupSshKeyPath = document.getElementById("setup-ssh-key-path");
  const setupSshKeyHelp = document.getElementById("setup-ssh-key-help");
  const setupSshKeyModePanels = Array.from(document.querySelectorAll("[data-setup-ssh-key-mode-panel]"));
  const setupSshPassword = document.getElementById("setup-ssh-password");
  const setupSshSudoPassword = document.getElementById("setup-ssh-sudo-password");
  const setupSshKnownHostsPath = document.getElementById("setup-ssh-known-hosts-path");
  const setupSshStrictHostKeyToggle = document.getElementById("setup-ssh-strict-host-key-toggle");
  const setupSshCommands = document.getElementById("setup-ssh-commands");
  const setupLoadPlatformCommandsButton = document.getElementById("setup-load-platform-commands-button");
  const historyToggleButton = document.getElementById("history-toggle-button");
  const detailHistoryPanel = document.getElementById("detail-history-panel");
  const historyDrawerTitle = document.getElementById("history-drawer-title");
  const historyDrawerContext = document.getElementById("history-drawer-context");
  const historyCloseButton = document.getElementById("history-close-button");
  const historyTimeframeSelect = document.getElementById("history-timeframe-select");
  const detailHistorySummary = document.getElementById("detail-history-summary");
  const detailHistoryNote = document.getElementById("detail-history-note");
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
  const uiPerfPanel = document.getElementById("ui-perf-panel");
  const uiPerfSummary = document.getElementById("ui-perf-summary");
  const uiPerfRecent = document.getElementById("ui-perf-recent");
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

  function snapshotMatchesSelectedSystem() {
    return (state.snapshot?.selected_system_id || null) === (state.selectedSystemId || null);
  }

  function currentLiveEnclosureId() {
    if (state.selectedEnclosureId) {
      return state.selectedEnclosureId;
    }
    return snapshotMatchesSelectedSystem() ? (state.snapshot.selected_enclosure_id || null) : null;
  }

  function currentLiveEnclosureLabel() {
    return getSelectedEnclosureOption()?.label || (snapshotMatchesSelectedSystem() ? (state.snapshot.selected_enclosure_label || null) : null);
  }

  function getSelectedProfile() {
    const selectedStorageView = getSelectedStorageViewRuntime();
    if (selectedStorageView?.kind === "ses_enclosure" && selectedStorageView.profile_id) {
      return {
        id: selectedStorageView.profile_id,
        label: selectedStorageView.profile_label || selectedStorageView.label || selectedStorageView.id,
        eyebrow: selectedStorageView.eyebrow || null,
        summary: selectedStorageView.summary || null,
        panel_title: selectedStorageView.panel_title || selectedStorageView.label || selectedStorageView.id,
        edge_label: selectedStorageView.edge_label || null,
        face_style: selectedStorageView.face_style || "generic",
        latch_edge: selectedStorageView.latch_edge || "bottom",
        bay_size: selectedStorageView.bay_size || null,
        row_groups: Array.isArray(selectedStorageView.row_groups) ? selectedStorageView.row_groups : [],
      };
    }
    return state.snapshot.selected_profile || null;
  }

  function currentPlatform() {
    return (state.snapshot.selected_system_platform || "core").toLowerCase();
  }

  function currentPlatformContext() {
    return state.snapshot.platform_context || {};
  }

  function currentPlatformDetails() {
    const details = currentPlatformContext()?.details;
    if (!details || !Array.isArray(details.sections)) {
      return null;
    }
    const sections = details.sections.filter((section) => {
      if (!section || typeof section !== "object") {
        return false;
      }
      const entries = Array.isArray(section.entries) ? section.entries.filter(Boolean) : [];
      const groups = Array.isArray(section.groups) ? section.groups.filter(Boolean) : [];
      return entries.length || groups.length;
    });
    if (!sections.length) {
      return null;
    }
    return {
      ...details,
      sections,
    };
  }

  function storageViewRuntimeViews() {
    const views = Array.isArray(state.storageViewsRuntime?.views) ? state.storageViewsRuntime.views : [];
    return [...views].sort((left, right) => (Number(left.order) || 0) - (Number(right.order) || 0));
  }

  function getMainUiStorageViewRuntimeOptions() {
    return storageViewRuntimeViews().filter((view) => view.enabled !== false && view.render?.show_in_main_ui !== false);
  }

  function getStorageViewRuntimeById(viewId) {
    return storageViewRuntimeViews().find((view) => view.id === viewId) || null;
  }

  function ensureStorageViewRuntimeSelection(preferVisible = false) {
    const views = storageViewRuntimeViews();
    const mainUiViews = getMainUiStorageViewRuntimeOptions();
    if (!views.length) {
      state.selectedStorageViewRuntimeId = "";
      return null;
    }
    if (state.selectedStorageViewRuntimeId && mainUiViews.some((view) => view.id === state.selectedStorageViewRuntimeId)) {
      return getStorageViewRuntimeById(state.selectedStorageViewRuntimeId);
    }
    if (preferVisible) {
      const preferredVisible = mainUiViews[0] || null;
      state.selectedStorageViewRuntimeId = preferredVisible?.id || "";
      return preferredVisible || null;
    }
    state.selectedStorageViewRuntimeId = "";
    return null;
  }

  function getSelectedStorageViewRuntime() {
    return ensureStorageViewRuntimeSelection(false);
  }

  function getSelectedStorageViewRuntimeSlot(slotIndex) {
    if (slotIndex === null || slotIndex === undefined || slotIndex === "") {
      return null;
    }
    const normalizedSlotIndex = Number(slotIndex);
    if (!Number.isInteger(normalizedSlotIndex)) {
      return null;
    }
    const selectedView = getSelectedStorageViewRuntime();
    if (!selectedView) {
      return null;
    }
    return (selectedView.slots || []).find((slot) => Number(slot.slot_index) === normalizedSlotIndex) || null;
  }

  function activeLayoutRows() {
    const selectedView = getSelectedStorageViewRuntime();
    return Array.isArray(selectedView?.slot_layout) && selectedView.slot_layout.length
      ? selectedView.slot_layout
      : (state.layoutRows || []);
  }

  function currentLayoutSlotCount() {
    const selectedView = getSelectedStorageViewRuntime();
    if (selectedView) {
      return Number(selectedView.slot_count) || countLayoutSlots(activeLayoutRows()) || 0;
    }
    return Number(state.snapshot.layout_slot_count) || countLayoutSlots(activeLayoutRows()) || 0;
  }

  function storageViewRuntimeMeta(view) {
    if (!view) {
      return [];
    }
    return [
      view.kind ? view.kind.replace(/_/g, " ") : null,
      view.template_label || view.template_id || null,
      Number.isFinite(Number(view.slot_count)) ? `${Number(view.slot_count)} slots` : null,
      Number.isFinite(Number(view.matched_count)) ? `${Number(view.matched_count)} matched` : null,
      view.binding?.mode ? `binding: ${view.binding.mode}` : null,
      view.render?.show_in_main_ui === false ? "maintenance-only view" : "main UI view",
      view.backing_enclosure_label ? `live: ${view.backing_enclosure_label}` : null,
      view.source === "selected_enclosure_snapshot" ? "live enclosure snapshot" : "inventory binding",
    ].filter(Boolean);
  }

  function selectorLabelForEnclosureOption(enclosure) {
    return `Live Enclosure · ${enclosure?.label || enclosure?.id || "Unknown enclosure"}`;
  }

  function isSavedChassisView(view) {
    return view?.kind === "ses_enclosure";
  }

  function storageViewKindLabel(view) {
    return isSavedChassisView(view) ? "Saved Chassis View" : "Virtual Storage View";
  }

  function selectorLabelForStorageViewOption(view) {
    const suffixes = [];
    if (view?.render?.show_in_main_ui === false) {
      suffixes.push("maintenance");
    }
    if (isSavedChassisView(view) && view?.backing_enclosure_label) {
      suffixes.push(`mirrors ${view.backing_enclosure_label}`);
    }
    return `${storageViewKindLabel(view)} · ${view?.label || view?.id || "Storage View"}${suffixes.length ? ` (${suffixes.join(" / ")})` : ""}`;
  }

  function storageViewRuntimeTilePrimary(slot, selectedView) {
    if (!slot?.occupied) {
      return selectedView?.kind === "nvme_carrier" ? "Unmapped slot" : "Empty";
    }
    if (selectedView?.kind === "nvme_carrier" || selectedView?.kind === "boot_devices") {
      return slot.device_name || slot.serial || slot.pool_name || "Matched disk";
    }
    return slot.serial || slot.device_name || slot.pool_name || "Matched disk";
  }

  function storageViewRuntimeTileSummary(slot, selectedView) {
    if (!slot?.occupied) {
      return selectedView?.kind === "nvme_carrier"
        ? "No live disk matched yet."
        : "No live disk is currently landing in this storage-view slot.";
    }
    if (selectedView?.kind === "nvme_carrier") {
      return [
        slot.device_name && slot.serial ? slot.serial : null,
        slot.pool_name ? `pool ${slot.pool_name}` : null,
      ].filter(Boolean).join(" • ") || "Matched from live inventory.";
    }
    return [
      slot.device_name || null,
      slot.pool_name ? `pool ${slot.pool_name}` : null,
      slot.transport_address || null,
    ].filter(Boolean).join(" • ");
  }

  function buildNvmeRuntimeTileMarkup(slot, selectedView) {
    const slotLabel = slot?.slot_label || `Slot ${Number(slot?.slot_index ?? 0) + 1}`;
    const slotSize = slot?.slot_size || "";
    const primary = storageViewRuntimeTilePrimary(slot, selectedView);
    const summary = storageViewRuntimeTileSummary(slot, selectedView);
    const lengthTag = formatNvmeFormFactorTag(slotSize);
    return `
      <span class="slot-status-led" aria-hidden="true"></span>
      <div class="storage-view-runtime-card storage-view-runtime-card--nvme" data-slot-size="${escapeHtml(slotSize)}">
        <span class="storage-view-runtime-card-hole" aria-hidden="true"></span>
        <div class="storage-view-runtime-card-content">
          <div class="storage-view-runtime-card-head">
            <span class="storage-view-runtime-card-slot">${escapeHtml(slotLabel)}</span>
            <span class="storage-view-runtime-card-size-chip">${escapeHtml(lengthTag)}</span>
          </div>
          <span class="storage-view-runtime-card-device">${escapeHtml(primary)}</span>
          <span class="storage-view-runtime-card-summary">${escapeHtml(summary)}</span>
        </div>
        <span class="storage-view-runtime-card-latch" aria-hidden="true"></span>
      </div>
    `;
  }

  function buildSatadomRuntimeTileMarkup(slot, selectedView) {
    const slotLabel = slot?.slot_label || `Slot ${Number(slot?.slot_index ?? 0) + 1}`;
    const primary = storageViewRuntimeTilePrimary(slot, selectedView);
    const summary = storageViewRuntimeTileSummary(slot, selectedView);
    const tertiary = storageViewRuntimeTertiaryLabel(slot);
    return `
      <span class="slot-status-led" aria-hidden="true"></span>
      <div class="storage-view-runtime-card storage-view-runtime-card--satadom">
        <div class="storage-view-runtime-card-photo-wrap" aria-hidden="true">
          <img class="storage-view-runtime-card-photo" src="/static/images/satadom-ml-3ie3-v2.png" alt="" loading="lazy" decoding="async">
        </div>
        <div class="storage-view-runtime-card-overlay">
          <div class="storage-view-runtime-card-label-plate">
            <div class="storage-view-runtime-card-head">
              <span class="storage-view-runtime-card-slot">${escapeHtml(slotLabel)}</span>
            </div>
            <span class="storage-view-runtime-card-device">${escapeHtml(primary)}</span>
          </div>
          <div class="storage-view-runtime-card-content storage-view-runtime-card-content--satadom">
            <span class="storage-view-runtime-card-summary">${escapeHtml(summary || stateLabel(slot))}</span>
            ${tertiary ? `<span class="storage-view-runtime-card-tertiary">${escapeHtml(tertiary)}</span>` : ""}
          </div>
        </div>
      </div>
    `;
  }

  function isSatadomBootTemplate(selectedView) {
    return selectedView?.kind === "boot_devices" && selectedView?.template_id === "satadom-pair-2";
  }

  function buildEmbeddedBootMediaRuntimeTileMarkup(slot) {
    const slotLabel = slot?.slot_label || `Slot ${Number(slot?.slot_index ?? 0) + 1}`;
    const primary = slot?.occupied
      ? (slot.device_name ? `/dev/${slot.device_name}` : slot.serial || "Embedded boot media")
      : "Embedded boot media";
    const summary = slot?.occupied
      ? [
          slot.model || null,
          slot.size_human || null,
          slot.smart_device_type ? `smartctl -d ${slot.smart_device_type}` : null,
        ].filter(Boolean).join(" • ") || "Matched from live inventory."
      : "No live boot device is currently landing in this storage-view slot.";
    const tertiary = slot?.occupied
      ? [
          slot.serial || null,
          slot.bus || null,
        ].filter(Boolean).join(" • ")
      : (slot?.smart_device_type ? `smartctl -d ${slot.smart_device_type}` : "");
    return `
      <span class="slot-status-led" aria-hidden="true"></span>
      <div class="storage-view-runtime-card storage-view-runtime-card--boot-media">
        <span class="storage-view-runtime-card-boot-chip storage-view-runtime-card-boot-chip--large" aria-hidden="true"></span>
        <span class="storage-view-runtime-card-boot-chip storage-view-runtime-card-boot-chip--small" aria-hidden="true"></span>
        <span class="storage-view-runtime-card-boot-connector" aria-hidden="true"></span>
        <div class="storage-view-runtime-card-overlay storage-view-runtime-card-overlay--boot-media">
          <div class="storage-view-runtime-card-label-plate storage-view-runtime-card-label-plate--boot-media">
            <div class="storage-view-runtime-card-head">
              <span class="storage-view-runtime-card-slot">${escapeHtml(slotLabel)}</span>
            </div>
            <span class="storage-view-runtime-card-device">${escapeHtml(primary)}</span>
          </div>
          <div class="storage-view-runtime-card-content storage-view-runtime-card-content--boot-media">
            <span class="storage-view-runtime-card-summary">${escapeHtml(summary || stateLabel(slot))}</span>
            ${tertiary ? `<span class="storage-view-runtime-card-tertiary storage-view-runtime-card-tertiary--boot-media">${escapeHtml(tertiary)}</span>` : ""}
          </div>
        </div>
      </div>
    `;
  }

  function formatNvmeFormFactorTag(slotSize) {
    const text = String(slotSize || "").trim();
    if (!text) {
      return "Form Factor: auto";
    }
    if (/^22\d{2,3}$/.test(text)) {
      return `Form Factor: ${text}`;
    }
    return `Form Factor: ${text}`;
  }

  const NVME_CARRIER_BOARD_LAYOUT = {
    width: 902,
    height: 526,
    imageSrc: "/static/images/hyper-m2-gen3-card.png",
    className: "",
    edgeNote: "PCIe edge / slot 1",
    connectorRight: 638,
    cardHeight: 56,
    holeInset: 8,
    defaultSlotSize: "2280",
    rowCenters: [55, 151, 247, 342],
    screwCenters: {
      "2242": 458,
      "2230": 506,
      "2260": 381,
      "2280": 312,
      "22110": 186,
      default: 312,
    },
  };

  const AOC_SLG4_2H8M2_BOARD_LAYOUT = {
    width: 800,
    height: 800,
    imageSrc: "/static/images/aoc-slg4-2h8m2.jpg",
    className: "is-aoc-slg4-2h8m2",
    edgeNote: "PCIe edge / M2-1 lower slot",
    connectorRight: 730,
    cardHeight: 58,
    holeInset: 8,
    defaultSlotSize: "2280",
    // Keep the overlays centered on the printed M.2 connector lanes.
    rowCenters: [278, 403],
    screwCenters: {
      "2230": 554,
      "2242": 554,
      "2260": 554,
      "2280": 374,
      "22110": 236,
      default: 374,
    },
  };

  function nvmeCarrierBoardLayout(surface) {
    if (surface?.template_id === "aoc-slg4-2h8m2-2" || surface?.id === "supermicro-aoc-slg4-2h8m2") {
      return AOC_SLG4_2H8M2_BOARD_LAYOUT;
    }
    return NVME_CARRIER_BOARD_LAYOUT;
  }

  function nvmeCarrierSlotMetrics(slot, orderIndex, boardLayout = NVME_CARRIER_BOARD_LAYOUT) {
    const slotSize = String(slot?.slot_size || "").trim();
    const screwCenterPx =
      boardLayout.screwCenters[slotSize]
      || boardLayout.screwCenters.default;
    const widthPx = boardLayout.connectorRight - screwCenterPx + boardLayout.holeInset;
    const centerPx =
      boardLayout.rowCenters[orderIndex]
      ?? boardLayout.rowCenters[boardLayout.rowCenters.length - 1];
    const topPx = centerPx - (boardLayout.cardHeight / 2);
    const leftPx = boardLayout.connectorRight - widthPx;
    return {
      left: `${(leftPx / boardLayout.width) * 100}%`,
      top: `${(topPx / boardLayout.height) * 100}%`,
      width: `${(widthPx / boardLayout.width) * 100}%`,
      minHeight: `${(boardLayout.cardHeight / boardLayout.height) * 100}%`,
    };
  }

  function bindStorageViewTileInteractions(tile, slot, selectedView) {
    tile.addEventListener("mouseenter", (event) => {
      state.hoveredSlot = slot.slot_index;
      refreshHoveredTooltip(tile);
      positionSlotTooltip(event.clientX, event.clientY);
      void ensureStorageViewSmartSummary(selectedView, slot);
    });
    tile.addEventListener("mousemove", (event) => {
      if (state.hoveredSlot === slot.slot_index) {
        positionSlotTooltip(event.clientX, event.clientY);
      }
    });
    tile.addEventListener("mouseleave", () => {
      if (state.hoveredSlot === slot.slot_index) {
        state.hoveredSlot = null;
      }
      hideSlotTooltip();
    });
    tile.addEventListener("focus", () => {
      state.hoveredSlot = slot.slot_index;
      refreshHoveredTooltip(tile);
      positionSlotTooltipFromElement(tile);
      void ensureStorageViewSmartSummary(selectedView, slot);
    });
    tile.addEventListener("blur", () => {
      if (state.hoveredSlot === slot.slot_index) {
        state.hoveredSlot = null;
      }
      hideSlotTooltip();
    });
    tile.addEventListener("click", (event) => {
      event.stopPropagation();
      if (state.selectedSlot === slot.slot_index) {
        clearSelectedSlot();
        return;
      }
      selectSlot(slot.slot_index);
    });
  }

  function storageViewRuntimeShortLabel(slot) {
    if (!slot || !slot.occupied) {
      return "Empty";
    }
    return slot.serial || slot.device_name || slot.pool_name || "Matched disk";
  }

  function storageViewRuntimeSecondaryLabel(slot) {
    if (!slot || !slot.occupied) {
      return "No live disk is currently landing in this storage-view slot.";
    }
    return [
      slot.device_name || null,
      slot.pool_name ? `pool ${slot.pool_name}` : null,
      slot.transport_address || null,
    ].filter(Boolean).join(" • ");
  }

  function storageViewRuntimeTertiaryLabel(slot) {
    if (!slot?.occupied) {
      return slot?.slot_size || "";
    }
    return [
      slot.slot_size || null,
      slot.placement_key || null,
    ].filter(Boolean).join(" • ");
  }

  function storageViewRuntimeMatchesFilter(slot) {
    if (!state.search) {
      return true;
    }
    const haystack = [
      slot?.slot_label,
      slot?.serial,
      slot?.device_name,
      slot?.pool_name,
      slot?.transport_address,
      slot?.placement_key,
      slot?.model,
      slot?.description,
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(state.search);
  }

  function buildStorageViewRuntimeTooltip(slot, selectedView) {
    if (!slot) {
      return "";
    }
    return buildStorageViewTooltipLines(slot, selectedView, getStorageViewSmartSummaryEntry(selectedView, slot)).join("\n");
  }

  function renderStorageViewGrid(selectedView) {
    hideSlotTooltip();
    grid.innerHTML = "";
    const slotLayout = Array.isArray(selectedView?.slot_layout) ? selectedView.slot_layout : [];
    const slotsByIndex = new Map((selectedView?.slots || []).map((slot) => [Number(slot.slot_index), slot]));
    const peerContext = getSelectedPeerContext();
    const profile = buildViewProfile();
    const driveScale = inferDominantDriveScale(profile);
    const layoutMode = inferChassisLayoutMode(profile, driveScale);

    if (selectedView.kind === "nvme_carrier") {
      const boardLayout = nvmeCarrierBoardLayout(selectedView);
      const board = document.createElement("div");
      board.className = ["nvme-carrier-canvas", boardLayout.className].filter(Boolean).join(" ");
      const boardImage = document.createElement("img");
      boardImage.className = "nvme-carrier-board-image";
      boardImage.src = boardLayout.imageSrc;
      boardImage.alt = "";
      boardImage.setAttribute("aria-hidden", "true");
      board.appendChild(boardImage);
      const orderedSlots = slotLayout.flat().filter((slotValue) => Number.isInteger(slotValue));

      orderedSlots.forEach((slotValue, orderIndex) => {
        const slot = slotsByIndex.get(Number(slotValue)) || {
          slot_index: Number(slotValue),
          slot_label: `Slot ${Number(slotValue) + 1}`,
          occupied: false,
          state: "empty",
          slot_size: null,
        };
        const visualState = slot.state === "matched" ? "healthy" : slot.state;
        const tile = document.createElement("button");
        tile.type = "button";
        tile.className = `slot-tile state-${visualState} storage-view-slot storage-view-slot-nvme storage-view-slot-nvme-absolute`;
        tile.dataset.slot = String(slot.slot_index);
        tile.dataset.slotOrder = String(orderIndex);
        if (slot.slot_size) {
          tile.dataset.slotSize = String(slot.slot_size);
        } else {
          delete tile.dataset.slotSize;
        }
        const metrics = nvmeCarrierSlotMetrics(slot, orderIndex, boardLayout);
        tile.style.left = metrics.left;
        tile.style.top = metrics.top;
        tile.style.width = metrics.width;
        tile.style.minHeight = metrics.minHeight;
        if (!storageViewRuntimeMatchesFilter(slot)) {
          tile.classList.add("filtered-out");
        }
        if (state.selectedSlot === slot.slot_index) {
          tile.classList.add("selected");
        }
        tile.setAttribute("aria-label", buildStorageViewRuntimeTooltip(slot, selectedView));
        tile.innerHTML = buildNvmeRuntimeTileMarkup(slot, selectedView);
        bindStorageViewTileInteractions(tile, slot, selectedView);
        board.appendChild(tile);
      });

      const edgeNote = document.createElement("div");
      edgeNote.className = "nvme-carrier-edge-note";
      edgeNote.textContent = boardLayout.edgeNote;
      board.appendChild(edgeNote);

      grid.appendChild(board);
      return;
    }

    slotLayout.forEach((row) => {
      const rowWrapper = document.createElement("div");
      rowWrapper.className = "slot-row";
      const rowSlots = document.createElement("div");
      rowSlots.className = "row-slots";
      rowSlots.dataset.slotCount = String(row.length);
      const rowGroups = splitRowIntoGroups(row);
      const isFlatTopLoaderGrouping = String(layoutMode || "").startsWith("top-loader") && rowGroups.length > 1;
      const flatGroupBreakpoints = rowGroupBreakpoints(rowGroups);

      const appendTile = (container, slotValue) => {
        if (!Number.isInteger(slotValue)) {
          const gapTile = document.createElement("div");
          gapTile.className = "slot-gap";
          gapTile.setAttribute("aria-hidden", "true");
          container.appendChild(gapTile);
          return;
        }

        const slot = slotsByIndex.get(Number(slotValue)) || {
          slot_index: Number(slotValue),
          slot_label: `Slot ${Number(slotValue) + 1}`,
          occupied: false,
          state: "empty",
          slot_size: null,
        };
        const visualState = slot.state === "matched" ? "healthy" : slot.state;
        const liveSlot = getLiveBackedStorageViewSlot(selectedView, slot);
        const tile = document.createElement("button");
        tile.type = "button";
        tile.className = liveSlot
          ? `slot-tile state-${liveSlot.state || "unknown"}`
          : `slot-tile state-${visualState} storage-view-slot`;
        if (selectedView.kind === "nvme_carrier") {
          tile.classList.add("storage-view-slot-nvme");
        }
        if (selectedView.kind === "boot_devices") {
          tile.classList.add("storage-view-slot-boot");
        }
        if (liveSlot ? !passesFilter(liveSlot) : !storageViewRuntimeMatchesFilter(slot)) {
          tile.classList.add("filtered-out");
        }
        if (state.selectedSlot === slot.slot_index) {
          tile.classList.add("selected");
        } else if (liveSlot && peerContext.active && peerContext.peerSlots.has(slot.slot_index)) {
          tile.classList.add("peer-highlight");
        } else if (liveSlot && peerContext.active) {
          tile.classList.add("peer-dimmed");
        }
        tile.dataset.slot = String(slot.slot_index);
        if (slot.slot_size) {
          tile.dataset.slotSize = String(slot.slot_size);
        } else {
          delete tile.dataset.slotSize;
        }
        tile.setAttribute(
          "aria-label",
          liveSlot
            ? slotTooltip(liveSlot, getStorageViewSmartSummaryEntry(selectedView, slot) || getSmartSummaryEntry(liveSlot))
            : buildStorageViewRuntimeTooltip(slot, selectedView)
        );
        tile.innerHTML =
          selectedView.kind === "nvme_carrier"
            ? buildNvmeRuntimeTileMarkup(slot, selectedView)
            : selectedView.kind === "boot_devices"
              ? (isSatadomBootTemplate(selectedView)
                ? buildSatadomRuntimeTileMarkup(slot, selectedView)
                : buildEmbeddedBootMediaRuntimeTileMarkup(slot))
            : liveSlot
              ? buildLiveSlotTileMarkup(liveSlot)
            : `
              <span class="slot-status-led" aria-hidden="true"></span>
              <span class="slot-number">${escapeHtml(slot.slot_label)}</span>
              <span class="slot-device">${escapeHtml(storageViewRuntimeShortLabel(slot))}</span>
              <span class="slot-pool">${escapeHtml(storageViewRuntimeSecondaryLabel(slot) || storageViewRuntimeTertiaryLabel(slot) || stateLabel(slot))}</span>
              <span class="slot-tertiary">${escapeHtml(storageViewRuntimeTertiaryLabel(slot))}</span>
              <span class="slot-latch" aria-hidden="true"></span>
            `;
        bindStorageViewTileInteractions(tile, slot, selectedView);
        container.appendChild(tile);
      };

      if (isFlatTopLoaderGrouping) {
        rowSlots.classList.add("row-slots-flat-grouped");
        row.forEach((slotValue, tileIndex) => {
          appendTile(rowSlots, slotValue);
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

          group.forEach((slotValue) => {
            appendTile(rowGroup, slotValue);
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

  function renderLiveNvmeCarrierGrid(selectedProfile) {
    hideSlotTooltip();
    grid.innerHTML = "";
    const slotLayout = activeLayoutRows();
    const slotsByNumber = new Map(state.snapshot.slots.map((slot) => [slot.slot, slot]));
    const peerContext = getSelectedPeerContext();
    const boardLayout = nvmeCarrierBoardLayout(selectedProfile);
    const board = document.createElement("div");
    board.className = ["nvme-carrier-canvas", boardLayout.className].filter(Boolean).join(" ");
    const boardImage = document.createElement("img");
    boardImage.className = "nvme-carrier-board-image";
    boardImage.src = boardLayout.imageSrc;
    boardImage.alt = "";
    boardImage.setAttribute("aria-hidden", "true");
    board.appendChild(boardImage);

    slotLayout.flat().filter((slotValue) => Number.isInteger(slotValue)).forEach((slotValue, orderIndex) => {
      const slotNumber = Number(slotValue);
      const liveSlot = slotsByNumber.get(slotNumber) || {
        slot: slotNumber,
        slot_label: formatSlotLabel(slotNumber),
        state: "unknown",
      };
      const slotSize = boardLayout.defaultSlotSize || "";
      const displaySlot = {
        slot_index: liveSlot.slot,
        slot_label: liveSlot.slot_label || formatSlotLabel(slotNumber),
        occupied: Boolean(liveSlot.device_name || liveSlot.serial || liveSlot.pool_name),
        state: liveSlot.state || "unknown",
        slot_size: slotSize,
        device_name: liveSlot.device_name || null,
        pool_name: liveSlot.pool_name || null,
        transport_address: liveSlot.operator_context?.transport_address || liveSlot.operator_context?.pcie_address || null,
        placement_key: liveSlot.operator_context?.placement_key || null,
        serial: liveSlot.serial || null,
        model: liveSlot.model || null,
      };
      const tile = document.createElement("button");
      tile.type = "button";
      tile.className = `slot-tile state-${liveSlot.state || "unknown"} storage-view-slot storage-view-slot-nvme storage-view-slot-nvme-absolute`;
      if (!passesFilter(liveSlot)) {
        tile.classList.add("filtered-out");
      }
      if (state.selectedSlot === liveSlot.slot) {
        tile.classList.add("selected");
      } else if (peerContext.active && peerContext.peerSlots.has(liveSlot.slot)) {
        tile.classList.add("peer-highlight");
      } else if (peerContext.active) {
        tile.classList.add("peer-dimmed");
      }
      tile.dataset.slot = String(liveSlot.slot);
      tile.dataset.slotSize = slotSize;
      const metrics = nvmeCarrierSlotMetrics(displaySlot, orderIndex, boardLayout);
      tile.style.left = metrics.left;
      tile.style.top = metrics.top;
      tile.style.width = metrics.width;
      tile.style.minHeight = metrics.minHeight;
      tile.setAttribute("aria-label", slotTooltip(liveSlot, getSmartSummaryEntry(liveSlot)));
      tile.innerHTML = buildNvmeRuntimeTileMarkup(displaySlot, { kind: "nvme_carrier" });
      tile.addEventListener("mouseenter", (event) => {
        state.hoveredSlot = liveSlot.slot;
        refreshHoveredTooltip(tile);
        positionSlotTooltip(event.clientX, event.clientY);
        void ensureSmartSummary(liveSlot);
      });
      tile.addEventListener("mousemove", (event) => {
        if (state.hoveredSlot === liveSlot.slot) {
          positionSlotTooltip(event.clientX, event.clientY);
        }
      });
      tile.addEventListener("mouseleave", () => {
        if (state.hoveredSlot === liveSlot.slot) {
          state.hoveredSlot = null;
        }
        hideSlotTooltip();
      });
      tile.addEventListener("focus", () => {
        state.hoveredSlot = liveSlot.slot;
        refreshHoveredTooltip(tile);
        positionSlotTooltipFromElement(tile);
        void ensureSmartSummary(liveSlot);
      });
      tile.addEventListener("blur", () => {
        if (state.hoveredSlot === liveSlot.slot) {
          state.hoveredSlot = null;
        }
        hideSlotTooltip();
      });
      tile.addEventListener("click", (event) => {
        event.stopPropagation();
        if (state.selectedSlot === liveSlot.slot) {
          clearSelectedSlot();
          return;
        }
        selectSlot(liveSlot.slot);
      });
      board.appendChild(tile);
    });

    const edgeNote = document.createElement("div");
    edgeNote.className = "nvme-carrier-edge-note";
    edgeNote.textContent = boardLayout.edgeNote;
    board.appendChild(edgeNote);

    grid.appendChild(board);
  }

  function renderStorageViewsRuntime() {
    if (!storageViewList || !storageViewEmpty || !storageViewContent || !storageViewTitle || !storageViewNote || !storageViewMeta || !storageViewGrid || !storageViewMappingList) {
      return;
    }

    const views = storageViewRuntimeViews();
    const selectedView = ensureStorageViewRuntimeSelection();
    if (storageViewsPanel) {
      storageViewsPanel.classList.toggle("hidden", !views.length);
    }
    if (storageViewsSummary) {
      if (state.storageViewsRuntimeLoading) {
        storageViewsSummary.textContent = `Inspecting runtime storage-view matches on ${state.selectedSystemId || state.snapshot.selected_system_id || "the selected system"}...`;
      } else if (!views.length) {
        storageViewsSummary.textContent = "No saved chassis or virtual storage views are configured for this system yet. Live discovered enclosures still show up in the selector above.";
      } else {
        storageViewsSummary.textContent = `Read-only runtime mapping for ${views.length} saved chassis or virtual storage view${views.length === 1 ? "" : "s"} on ${state.storageViewsRuntime?.system_label || state.snapshot.selected_system_label || state.selectedSystemId || "the selected system"}.`;
      }
    }

    storageViewList.innerHTML = views
      .map((view) => `
        <button
          class="storage-view-card${view.id === state.selectedStorageViewRuntimeId ? " is-selected" : ""}${view.render?.show_in_main_ui === false ? " is-hidden" : ""}"
          type="button"
          data-storage-view-runtime-id="${escapeHtml(view.id)}"
        >
          <div class="storage-view-card-header">
            <div>
              <h4>${escapeHtml(view.label || view.id)}</h4>
              <p class="subtle">${escapeHtml(view.notes?.[0] || "Runtime storage-view mapping.")}</p>
            </div>
            <span class="state-pill state-${view.enabled === false ? "empty" : "healthy"}">${escapeHtml(view.enabled === false ? "Disabled" : "Enabled")}</span>
          </div>
          <div class="profile-preview-meta">
            ${storageViewRuntimeMeta(view).map((item) => `<span class="meta-chip">${escapeHtml(item)}</span>`).join("")}
          </div>
        </button>
      `)
      .join("");

    if (!selectedView) {
      storageViewEmpty.classList.remove("hidden");
      storageViewContent.classList.add("hidden");
      return;
    }

    storageViewEmpty.classList.add("hidden");
    storageViewContent.classList.remove("hidden");
    storageViewTitle.textContent = selectedView.label || selectedView.id;
    storageViewNote.textContent = (selectedView.notes || []).join(" ");
    storageViewMeta.innerHTML = storageViewRuntimeMeta(selectedView)
      .map((item) => `<span class="meta-chip">${escapeHtml(item)}</span>`)
      .join("");

    const slotLayout = Array.isArray(selectedView.slot_layout) ? selectedView.slot_layout : [];
    const columnCount = Math.max(1, ...slotLayout.map((row) => (Array.isArray(row) ? row.length : 0)), 1);
    const slotsByIndex = new Map((selectedView.slots || []).map((slot) => [Number(slot.slot_index), slot]));
    storageViewGrid.style.gridTemplateColumns = `repeat(${columnCount}, minmax(0, 1fr))`;
    storageViewGrid.classList.toggle("is-nvme-carrier", selectedView.kind === "nvme_carrier");
    storageViewGrid.innerHTML = slotLayout
      .flat()
      .map((slotIndex) => {
        if (!Number.isInteger(slotIndex)) {
          return '<div class="storage-view-runtime-cell is-gap" aria-hidden="true"></div>';
        }
        const slot = slotsByIndex.get(Number(slotIndex));
        const stateClass = slot?.state || "empty";
        return `
          <article class="storage-view-runtime-cell state-${escapeHtml(stateClass)}${selectedView.kind === "ses_enclosure" ? " is-ses" : ""}${selectedView.kind === "nvme_carrier" ? " is-nvme" : ""}">
            ${selectedView.kind === "nvme_carrier"
              ? `
                <div class="storage-view-runtime-cell-card">
                  <div class="storage-view-runtime-card storage-view-runtime-card--nvme" data-slot-size="${escapeHtml(slot?.slot_size || "")}">
                    <span class="storage-view-runtime-card-hole" aria-hidden="true"></span>
                    <div class="storage-view-runtime-card-content">
                      <span class="storage-view-runtime-card-slot">${escapeHtml(slot?.slot_label || `Slot ${Number(slotIndex) + 1}`)}</span>
                      <span class="storage-view-runtime-card-size">${escapeHtml(slot?.slot_size || "auto")}</span>
                      <span class="storage-view-runtime-card-device">${escapeHtml(storageViewRuntimeTilePrimary(slot, selectedView))}</span>
                      <span class="storage-view-runtime-card-summary">${escapeHtml(storageViewRuntimeTileSummary(slot, selectedView))}</span>
                    </div>
                    <span class="storage-view-runtime-card-latch" aria-hidden="true"></span>
                  </div>
                </div>
              `
              : `
                <span class="storage-view-runtime-slot-label">${escapeHtml(slot?.slot_label || `Slot ${Number(slotIndex) + 1}`)}</span>
                <div class="storage-view-runtime-device">${escapeHtml(storageViewRuntimeShortLabel(slot))}</div>
                <div class="storage-view-runtime-secondary">${escapeHtml(storageViewRuntimeSecondaryLabel(slot))}</div>
              `}
          </article>
        `;
      })
      .join("");

    storageViewMappingList.innerHTML = (selectedView.slots || [])
      .map((slot) => `
        <article class="storage-view-mapping-item${slot.occupied ? "" : " is-empty"}">
          <div class="storage-view-mapping-slot">${escapeHtml(slot.slot_label)}</div>
          <div class="storage-view-mapping-body">
            <div class="storage-view-mapping-title">${escapeHtml(storageViewRuntimeShortLabel(slot))}</div>
            <div class="storage-view-mapping-copy">
              ${escapeHtml(
                slot.occupied
                  ? [
                      slot.device_name ? `device ${slot.device_name}` : null,
                      slot.pool_name ? `pool ${slot.pool_name}` : null,
                      slot.transport_address ? `PCIe ${slot.transport_address}` : null,
                      slot.snapshot_slot !== null && slot.snapshot_slot !== undefined ? `live slot ${slot.snapshot_slot}` : null,
                      slot.placement_key ? `placement ${slot.placement_key}` : null,
                      Array.isArray(slot.match_reasons) && slot.match_reasons.length ? `matched by ${slot.match_reasons.join(", ")}` : null,
                    ].filter(Boolean).join(" • ")
                  : "No live disk is currently matched to this layout slot."
              )}
            </div>
          </div>
        </article>
      `)
      .join("");
  }

  function usesGenericPersistentIdLabel() {
    return ["scale", "linux", "esxi"].includes(currentPlatform());
  }

  function buildViewProfile() {
    const selectedStorageView = getSelectedStorageViewRuntime();
    const profile = getSelectedProfile();
    const system = getSelectedSystemOption();
    const enclosure = getSelectedEnclosureOption();
    const enclosureLabel = enclosure?.label || currentLiveEnclosureLabel() || "Enclosure";
    const systemLabel = system?.label || state.snapshot.selected_system_label || "TrueNAS JBOD Enclosure UI";
    if (selectedStorageView) {
      const baseSummary =
        selectedStorageView.kind === "nvme_carrier"
          ? "Runtime storage-view map for an internal 4x NVMe carrier card."
          : selectedStorageView.kind === "boot_devices"
            ? "Runtime storage-view map for internal boot media."
            : selectedStorageView.backing_enclosure_label
              ? `Saved chassis view layered on top of the live enclosure ${selectedStorageView.backing_enclosure_label}.`
              : "Runtime storage-view map for the selected saved hardware view.";
      return {
        eyebrow:
          selectedStorageView.kind === "ses_enclosure"
            ? (profile?.eyebrow || `${systemLabel} / ${storageViewKindLabel(selectedStorageView)}`)
            : `${systemLabel} / ${storageViewKindLabel(selectedStorageView)}`,
        summary:
          selectedStorageView.kind === "ses_enclosure"
            ? (selectedStorageView.notes?.[0] || profile?.summary || baseSummary)
            : (selectedStorageView.notes?.[0] || baseSummary),
        enclosureTitle:
          selectedStorageView.kind === "ses_enclosure"
            ? (profile?.panel_title || selectedStorageView.label || selectedStorageView.id)
            : (selectedStorageView.label || selectedStorageView.id),
        edgeLabel:
          selectedStorageView.kind === "nvme_carrier"
            ? (selectedStorageView.template_id === "aoc-slg4-2h8m2-2" ? "PCIe edge / M2-1 lower slot" : "PCIe edge / slot 1")
            : selectedStorageView.kind === "ses_enclosure"
              ? (profile?.edge_label || "Front of chassis")
              : "Storage view",
        faceStyle:
          selectedStorageView.kind === "nvme_carrier"
            ? "nvme-carrier"
            : selectedStorageView.kind === "boot_devices"
              ? "boot-devices"
              : selectedStorageView.kind === "ses_enclosure"
                ? (profile?.face_style || "generic")
                : "generic",
        latchEdge:
          selectedStorageView.kind === "ses_enclosure"
            ? (profile?.latch_edge || "bottom")
            : "bottom",
        baySize: selectedStorageView.kind === "ses_enclosure" ? (profile?.bay_size || null) : "2.5",
      };
    }
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

  function countLayoutSlots(rows) {
    return (rows || []).reduce(
      (total, row) => total + row.filter((slotNumber) => Number.isInteger(slotNumber)).length,
      0
    );
  }

  function inferDominantDriveScale(profile) {
    if (profile.baySize === "3.5" || profile.baySize === "2.5") {
      return profile.baySize;
    }

    const slotCount = currentLayoutSlotCount();

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
    const rowCount = activeLayoutRows().length || 0;
    const slotCount = currentLayoutSlotCount();

    if (profile.faceStyle === "nvme-carrier") {
      return "nvme-carrier";
    }

    if (profile.faceStyle === "boot-devices") {
      return "boot-devices";
    }

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
    chassisShell.dataset.layoutRows = String(activeLayoutRows().length || 0);
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

  function buildSelectionParams({ includeStorageView = false } = {}) {
    const params = new URLSearchParams();
    if (state.selectedSystemId) {
      params.set("system_id", state.selectedSystemId);
    }
    const liveEnclosureId = currentLiveEnclosureId();
    if (liveEnclosureId) {
      params.set("enclosure_id", liveEnclosureId);
    }
    if (includeStorageView && state.selectedStorageViewRuntimeId) {
      params.set("storage_view_id", state.selectedStorageViewRuntimeId);
    }
    return params;
  }

  function buildScopedUrl(url) {
    const params = buildSelectionParams();
    return params.toString() ? `${url}?${params.toString()}` : url;
  }

  function platformSetupCopy(platform) {
    switch (String(platform || "").toLowerCase()) {
      case "scale":
        return "TrueNAS SCALE usually wants an API key plus optional SSH enrichment for SES and smartctl detail.";
      case "linux":
        return "Generic Linux can run inventory-only over SSH, or you can pair it with a light API endpoint if you have one.";
      case "quantastor":
        return "Quantastor usually authenticates with API user/password, then optionally adds SSH for qs and SES detail.";
      case "esxi":
        return "VMware ESXi is read-only and SSH-only in this first pass; StorCLI supplies physical RAID-member detail and LED/write actions stay disabled.";
      default:
        return "TrueNAS CORE usually wants an API key, with SSH as the optional fallback for enclosure mapping and LED control.";
    }
  }

  function defaultSetupSshCommands(platform) {
    const normalizedPlatform = String(platform || "core").toLowerCase();
    const commands = setupPlatformDefaults?.[normalizedPlatform]?.ssh_commands;
    return Array.isArray(commands) ? commands : [];
  }

  function recommendedSetupSshUser(platform = setupPlatformSelect?.value || "core") {
    return String(platform || "").toLowerCase() === "esxi" ? "root" : "jbodmap";
  }

  function renderSetupProfileOptions() {
    if (!setupProfileSelect) {
      return;
    }
    const selectedValue = setupProfileSelect.value || "";
    const options = [
      '<option value="">Auto-select from platform</option>',
      ...availableSetupProfiles.map((profile) => {
        const summary = profile.summary ? ` data-summary="${escapeHtml(profile.summary)}"` : "";
        const selected = profile.id === selectedValue ? " selected" : "";
        return `<option value="${escapeHtml(profile.id)}"${summary}${selected}>${escapeHtml(profile.label)}</option>`;
      }),
    ];
    setupProfileSelect.innerHTML = options.join("");
    if (selectedValue) {
      setupProfileSelect.value = selectedValue;
    }
  }

  function syncSetupPlatformHelp() {
    if (!setupPlatformHelp || !setupPlatformSelect) {
      return;
    }
    setupPlatformHelp.textContent = platformSetupCopy(setupPlatformSelect.value || "core");
  }

  function formatBackupPackaging(packaging) {
    if (packaging === "zip") {
      return "ZIP";
    }
    if (packaging === "7z") {
      return "7Z";
    }
    if (packaging === "tar.gz") {
      return "TAR.GZ";
    }
    return "TAR.ZST";
  }

  function normalizeSetupSshKeyMode(value) {
    if (value === "generate" || value === "manual") {
      return value;
    }
    return "reuse";
  }

  function normalizeSetupSshKeyName(value) {
    return String(value || "")
      .trim()
      .replace(/[^a-zA-Z0-9._-]+/g, "-")
      .replace(/^[._-]+|[._-]+$/g, "")
      .toLowerCase()
      .slice(0, 128);
  }

  function suggestedSetupSshKeyName() {
    const systemIdCandidate = normalizeSetupSshKeyName(setupSystemId?.value);
    const labelCandidate = normalizeSetupSshKeyName(setupSystemLabel?.value).toLowerCase();
    const hostCandidate = normalizeSetupSshKeyName(
      (setupTruenasHost?.value || "")
        .replace(/^https?:\/\//i, "")
        .split("/")[0]
        .split(":")[0]
    ).toLowerCase();
    if (systemIdCandidate) {
      return `id_${systemIdCandidate}`;
    }
    if (labelCandidate) {
      return `id_${labelCandidate}`;
    }
    if (hostCandidate) {
      return `id_${hostCandidate}`;
    }
    return "id_truenas";
  }

  function getSetupSshKeyByName(name) {
    return (state.setup.sshKeys || []).find((key) => key.name === name) || null;
  }

  function preferredSetupSshKeyName(preferredName = null) {
    if (preferredName && getSetupSshKeyByName(preferredName)) {
      return preferredName;
    }
    const selectedValue = setupSshExistingKeySelect?.value || "";
    if (selectedValue && getSetupSshKeyByName(selectedValue)) {
      return selectedValue;
    }
    const currentPath = setupSshKeyPath?.value?.trim() || "";
    if (currentPath) {
      const matchingKey = (state.setup.sshKeys || []).find(
        (key) => key.runtime_private_path === currentPath || key.private_path === currentPath
      );
      if (matchingKey) {
        return matchingKey.name;
      }
    }
    if (getSetupSshKeyByName("id_truenas")) {
      return "id_truenas";
    }
    return state.setup.sshKeys[0]?.name || "";
  }

  function renderSetupSshKeyOptions(preferredName = null) {
    if (!setupSshExistingKeySelect) {
      return;
    }
    const selectedName = preferredSetupSshKeyName(preferredName);
    if (!state.setup.sshKeys.length) {
      setupSshExistingKeySelect.innerHTML = '<option value="">No SSH keys found under config/ssh</option>';
      setupSshExistingKeySelect.value = "";
      return;
    }
    setupSshExistingKeySelect.innerHTML = state.setup.sshKeys
      .map((key) => `<option value="${escapeHtml(key.name)}">${escapeHtml(key.name)} (${escapeHtml(key.algorithm || "ssh")})</option>`)
      .join("");
    setupSshExistingKeySelect.value = selectedName;
  }

  function applySelectedExistingSshKey() {
    if (!setupSshKeyPath || state.setup.sshKeyMode !== "reuse") {
      return;
    }
    const selectedKey = getSetupSshKeyByName(setupSshExistingKeySelect?.value || "");
    if (!selectedKey) {
      return;
    }
    setupSshKeyPath.value = selectedKey.runtime_private_path || selectedKey.private_path || setupSshKeyPath.value;
  }

  function syncSetupSshKeyHelp() {
    if (!setupSshKeyHelp) {
      return;
    }
    if (!setupSshEnabledToggle?.checked) {
      setupSshKeyHelp.textContent = "SSH key controls unlock after SSH enrichment is enabled for this system.";
      return;
    }
    if (state.setup.sshKeysLoading) {
      setupSshKeyHelp.textContent = "Loading SSH key pairs from config/ssh...";
      return;
    }

    if (state.setup.sshKeyMode === "reuse") {
      if (!state.setup.sshKeys.length) {
        setupSshKeyHelp.textContent = "No reusable keys were found under config/ssh yet. Generate one here or switch to a manual path.";
        return;
      }
      const selectedKey = getSetupSshKeyByName(setupSshExistingKeySelect?.value || "");
      if (!selectedKey) {
        setupSshKeyHelp.textContent = "Choose an existing key pair to populate the runtime SSH key path automatically.";
        return;
      }
      const publicPath = selectedKey.public_path ? ` Public key: ${selectedKey.public_path}.` : "";
      setupSshKeyHelp.textContent = `Using ${selectedKey.runtime_private_path || selectedKey.private_path} (${selectedKey.fingerprint}).${publicPath}`;
      return;
    }

    if (state.setup.sshKeyMode === "generate") {
      const suggestedName = normalizeSetupSshKeyName(setupSshGenerateName?.value) || suggestedSetupSshKeyName();
      setupSshKeyHelp.textContent = `New Ed25519 key pairs are written under config/ssh and become available at /run/ssh immediately. Suggested name: ${suggestedName}.`;
      return;
    }

    const currentPath = setupSshKeyPath?.value?.trim() || "/run/ssh/id_truenas";
    setupSshKeyHelp.textContent = `Manual mode leaves the key path editable. Current path: ${currentPath}.`;
  }

  function syncSetupSshKeyMode(preferredName = null) {
    state.setup.sshKeyMode = normalizeSetupSshKeyMode(setupSshKeyMode?.value);
    if (setupSshKeyMode) {
      setupSshKeyMode.value = state.setup.sshKeyMode;
    }
    renderSetupSshKeyOptions(preferredName);
    setupSshKeyModePanels.forEach((panel) => {
      panel.classList.toggle("hidden", panel.dataset.setupSshKeyModePanel !== state.setup.sshKeyMode);
    });
    if (setupSshGenerateName && state.setup.sshKeyMode === "generate" && !setupSshGenerateName.value.trim()) {
      setupSshGenerateName.value = suggestedSetupSshKeyName();
    }
    if (setupSshKeyPath) {
      setupSshKeyPath.readOnly = state.setup.sshKeyMode !== "manual";
      setupSshKeyPath.classList.toggle("is-readonly", state.setup.sshKeyMode !== "manual");
    }
    applySelectedExistingSshKey();
    syncSetupSshKeyHelp();
  }

  function syncSystemBackupControls() {
    state.setup.backupPackaging = systemBackupPackagingSelect?.value === "zip"
      ? "zip"
      : systemBackupPackagingSelect?.value === "tar.gz"
        ? "tar.gz"
        : "tar.zst";
    if (systemBackupPackagingSelect) {
      systemBackupPackagingSelect.value = state.setup.backupPackaging;
    }
    if (systemBackupExportPassphrase) {
      systemBackupExportPassphrase.disabled = !Boolean(systemBackupEncryptToggle?.checked);
    }
  }

  function syncSetupSshFields() {
    const sshEnabled = Boolean(setupSshEnabledToggle?.checked);
    document.querySelectorAll("[data-setup-ssh-field]").forEach((element) => {
      element.disabled = !sshEnabled;
    });
    if (setupLoadPlatformCommandsButton) {
      setupLoadPlatformCommandsButton.disabled = !sshEnabled;
    }
    if (setupRefreshSshKeysButton) {
      setupRefreshSshKeysButton.disabled = !sshEnabled || state.setup.sshKeysLoading;
    }
    if (setupGenerateSshKeyButton) {
      setupGenerateSshKeyButton.disabled = !sshEnabled || state.setup.sshKeysLoading;
    }
    if (setupSshKeyMode) {
      setupSshKeyMode.disabled = !sshEnabled || state.setup.sshKeysLoading;
    }
    if (setupSshExistingKeySelect) {
      setupSshExistingKeySelect.disabled = !sshEnabled
        || state.setup.sshKeysLoading
        || state.setup.sshKeyMode !== "reuse"
        || !state.setup.sshKeys.length;
    }
    if (setupSshGenerateName) {
      setupSshGenerateName.disabled = !sshEnabled || state.setup.sshKeyMode !== "generate";
    }
    syncSetupSshKeyMode();
  }

  async function loadSetupSshKeys({ preferredName = null, silent = false } = {}) {
    if (state.snapshotMode || state.setup.sshKeysLoading) {
      return;
    }
    try {
      state.setup.sshKeysLoading = true;
      syncSetupSshFields();
      if (!silent) {
        setStatus("Loading SSH key pairs for setup...");
      }
      const result = await fetchJson("/api/system-setup/ssh-keys");
      state.setup.sshKeys = Array.isArray(result.keys) ? result.keys : [];
      syncSetupSshKeyMode(preferredName);
      if (!silent) {
        setStatus(
          state.setup.sshKeys.length
            ? `Loaded ${state.setup.sshKeys.length} reusable SSH key pair${state.setup.sshKeys.length === 1 ? "" : "s"}.`
            : "No reusable SSH key pairs were found under config/ssh."
        );
      }
    } catch (error) {
      if (!silent) {
        setStatus(`SSH key lookup failed: ${error.message || error}`, "error");
      }
    } finally {
      state.setup.sshKeysLoading = false;
      syncSetupSshFields();
    }
  }

  async function generateSetupSshKey() {
    if (state.snapshotMode || state.setup.sshKeysLoading) {
      return;
    }
    const requestedName = normalizeSetupSshKeyName(setupSshGenerateName?.value) || suggestedSetupSshKeyName();
    if (!requestedName) {
      setStatus("Enter a key name before generating an SSH key pair.", "error");
      return;
    }
    try {
      state.setup.sshKeysLoading = true;
      syncSetupSshFields();
      setStatus(`Generating SSH key pair ${requestedName}...`);
      const result = await fetchJson("/api/system-setup/ssh-keys/generate", {
        method: "POST",
        body: JSON.stringify({ name: requestedName }),
      });
      state.setup.sshKeys = Array.isArray(result.keys) ? result.keys : state.setup.sshKeys;
      if (setupSshGenerateName) {
        setupSshGenerateName.value = requestedName;
      }
      if (setupSshKeyMode) {
        setupSshKeyMode.value = "reuse";
      }
      syncSetupSshKeyMode(result.key?.name || requestedName);
      setStatus(`Generated SSH key pair ${result.key?.name || requestedName}.`);
    } catch (error) {
      setStatus(`SSH key generation failed: ${error.message || error}`, "error");
    } finally {
      state.setup.sshKeysLoading = false;
      syncSetupSshFields();
    }
  }

  function maybeLoadRecommendedSetupCommands(force = false) {
    if (!setupSshCommands || !setupPlatformSelect) {
      return;
    }
    const platform = setupPlatformSelect.value || "core";
    const recommendedText = defaultSetupSshCommands(platform).join("\n");
    const previousRecommended = defaultSetupSshCommands(state.setup.defaultsLoadedForPlatform || platform).join("\n");
    const currentText = (setupSshCommands.value || "").trim();
    if (force || !currentText || currentText === previousRecommended.trim()) {
      setupSshCommands.value = recommendedText;
      state.setup.defaultsLoadedForPlatform = platform;
    }
  }

  function syncSystemSetupStep() {
    const currentStep = Math.max(0, Math.min(state.setup.step, setupStepPanels.length - 1));
    state.setup.step = currentStep;
    setupStepPanels.forEach((panel, index) => {
      panel.classList.toggle("hidden", index !== currentStep);
    });
    setupStepIndicators.forEach((indicator, index) => {
      indicator.classList.toggle("is-active", index === currentStep);
      indicator.classList.toggle("is-complete", index < currentStep);
    });
    if (setupPrevButton) {
      setupPrevButton.disabled = currentStep === 0 || state.setup.createRunning;
    }
    if (setupNextButton) {
      setupNextButton.classList.toggle("hidden", currentStep >= setupStepPanels.length - 1);
      setupNextButton.disabled = state.setup.createRunning;
    }
    if (setupCreateButton) {
      setupCreateButton.classList.toggle("hidden", currentStep < setupStepPanels.length - 1);
      setupCreateButton.disabled = state.setup.createRunning;
    }
  }

  function setSystemSetupStep(nextStep) {
    state.setup.step = Math.max(0, Math.min(Number(nextStep) || 0, setupStepPanels.length - 1));
    syncSystemSetupStep();
  }

  function initializeSystemSetupForm() {
    if (!setupPlatformSelect) {
      return;
    }
    renderSetupProfileOptions();
    setupPlatformSelect.value = currentPlatform() || "core";
    if (setupSystemLabel) {
      setupSystemLabel.value = "";
    }
    if (setupSystemId) {
      setupSystemId.value = "";
    }
    if (setupProfileSelect) {
      setupProfileSelect.value = "";
    }
    if (setupMakeDefaultToggle) {
      setupMakeDefaultToggle.checked = false;
    }
    if (setupTruenasHost) {
      setupTruenasHost.value = "";
    }
    if (setupVerifySslToggle) {
      setupVerifySslToggle.checked = true;
    }
    if (setupEnclosureFilter) {
      setupEnclosureFilter.value = "";
    }
    if (setupApiKey) {
      setupApiKey.value = "";
    }
    if (setupApiUser) {
      setupApiUser.value = "";
    }
    if (setupApiPassword) {
      setupApiPassword.value = "";
    }
    if (setupSshEnabledToggle) {
      setupSshEnabledToggle.checked = false;
    }
    if (setupSshHost) {
      setupSshHost.value = "";
    }
    if (setupSshUser) {
      setupSshUser.value = recommendedSetupSshUser();
    }
    if (setupSshPort) {
      setupSshPort.value = "22";
    }
    if (setupSshKeyMode) {
      setupSshKeyMode.value = "reuse";
    }
    state.setup.sshKeyMode = "reuse";
    if (setupSshGenerateName) {
      setupSshGenerateName.value = suggestedSetupSshKeyName();
    }
    if (setupSshKeyPath) {
      setupSshKeyPath.value = "/run/ssh/id_truenas";
    }
    if (setupSshPassword) {
      setupSshPassword.value = "";
    }
    if (setupSshSudoPassword) {
      setupSshSudoPassword.value = "";
    }
    if (setupSshKnownHostsPath) {
      setupSshKnownHostsPath.value = "/app/data/known_hosts";
    }
    if (setupSshStrictHostKeyToggle) {
      setupSshStrictHostKeyToggle.checked = true;
    }
    if (systemBackupEncryptToggle) {
      systemBackupEncryptToggle.checked = false;
    }
    state.setup.backupPackaging = "tar.zst";
    if (systemBackupPackagingSelect) {
      systemBackupPackagingSelect.value = state.setup.backupPackaging;
    }
    if (systemBackupExportPassphrase) {
      systemBackupExportPassphrase.value = "";
    }
    if (systemBackupImportPassphrase) {
      systemBackupImportPassphrase.value = "";
    }
    state.setup.defaultsLoadedForPlatform = null;
    maybeLoadRecommendedSetupCommands(true);
    syncSetupPlatformHelp();
    syncSystemBackupControls();
    syncSetupSshFields();
    setSystemSetupStep(0);
  }

  function openSystemSetupDialog() {
    if (!systemSetupDialog) {
      return;
    }
    if (systemSetupDialog.open) {
      return;
    }
    if (typeof systemSetupDialog.showModal === "function") {
      systemSetupDialog.showModal();
    } else {
      systemSetupDialog.setAttribute("open", "open");
    }
    syncSetupPlatformHelp();
    syncSystemBackupControls();
    syncSetupSshFields();
    syncSystemSetupStep();
    void loadSetupSshKeys({ silent: true });
  }

  function closeSystemSetupDialog() {
    if (!systemSetupDialog) {
      return;
    }
    if (!systemSetupDialog.open) {
      return;
    }
    if (typeof systemSetupDialog.close === "function") {
      systemSetupDialog.close();
    } else {
      systemSetupDialog.removeAttribute("open");
    }
  }

  function collectSystemSetupPayload() {
    const sshEnabled = Boolean(setupSshEnabledToggle?.checked);
    if (sshEnabled) {
      applySelectedExistingSshKey();
    }
    return {
      system_id: setupSystemId?.value?.trim() || null,
      label: setupSystemLabel?.value?.trim() || "",
      platform: setupPlatformSelect?.value || "core",
      truenas_host: setupTruenasHost?.value?.trim() || "",
      api_key: setupApiKey?.value?.trim() || null,
      api_user: setupApiUser?.value?.trim() || null,
      api_password: setupApiPassword?.value || null,
      verify_ssl: Boolean(setupVerifySslToggle?.checked),
      enclosure_filter: setupEnclosureFilter?.value?.trim() || null,
      ssh_enabled: sshEnabled,
      ssh_host: setupSshHost?.value?.trim() || null,
      ssh_port: Number(setupSshPort?.value) || 22,
      ssh_user: setupSshUser?.value?.trim() || null,
      ssh_key_path: setupSshKeyPath?.value?.trim() || null,
      ssh_password: setupSshPassword?.value || null,
      ssh_sudo_password: setupSshSudoPassword?.value || null,
      ssh_known_hosts_path: setupSshKnownHostsPath?.value?.trim() || null,
      ssh_strict_host_key_checking: Boolean(setupSshStrictHostKeyToggle?.checked),
      ssh_commands: (setupSshCommands?.value || "")
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean),
      default_profile_id: setupProfileSelect?.value || null,
      make_default: Boolean(setupMakeDefaultToggle?.checked),
    };
  }

  function readOptionalSecretValue(field) {
    const value = field?.value;
    return value === undefined || value === null || value === "" ? null : value;
  }

  function encodeUtf8Base64(value) {
    const text = String(value ?? "");
    const bytes = new TextEncoder().encode(text);
    let binary = "";
    bytes.forEach((byte) => {
      binary += String.fromCharCode(byte);
    });
    return window.btoa(binary);
  }

  async function exportSystemBackup() {
    if (state.snapshotMode) {
      setStatus("Full backup export is disabled in an offline snapshot export.", "error");
      return;
    }
    if (state.setup.exportRunning) {
      return;
    }
    const encrypt = Boolean(systemBackupEncryptToggle?.checked);
    const passphrase = readOptionalSecretValue(systemBackupExportPassphrase);
    const packaging = systemBackupPackagingSelect?.value || state.setup.backupPackaging || "tar.zst";
    if (encrypt && !passphrase) {
      setStatus("Enter a passphrase before exporting an encrypted full backup.", "error");
      return;
    }
    try {
      state.setup.exportRunning = true;
      if (exportSystemBackupButton) {
        exportSystemBackupButton.disabled = true;
      }
      setStatus("Preparing full config and database backup...");
      const response = await fetch("/api/system-backup/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          encrypt,
          passphrase,
          packaging,
        }),
      });
      if (!response.ok) {
        let detail = `Request failed with ${response.status}`;
        try {
          const payload = await response.json();
          detail = payload.detail || detail;
        } catch (error) {
          // Ignore JSON parsing failures and fall back to the HTTP status.
        }
        throw new Error(detail);
      }
      const blob = await response.blob();
      const objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      const resolvedPackaging = response.headers.get("X-Backup-Packaging") || packaging;
      anchor.download = resolveDownloadFilename(
        response,
        `jbod-system-backup${resolvedPackaging === "tar.zst" ? ".tar.zst" : resolvedPackaging === "tar.gz" ? ".tar.gz" : `.${resolvedPackaging}`}`
      );
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(objectUrl);
      setStatus(
        encrypt
          ? `Encrypted ${formatBackupPackaging(resolvedPackaging)} full backup exported.`
          : `${formatBackupPackaging(resolvedPackaging)} full backup exported.`
      );
    } catch (error) {
      setStatus(`Full backup export failed: ${error.message || error}`, "error");
    } finally {
      state.setup.exportRunning = false;
      if (exportSystemBackupButton) {
        exportSystemBackupButton.disabled = false;
      }
    }
  }

  async function importSystemBackupFromFile(file) {
    if (state.snapshotMode) {
      setStatus("Full backup import is disabled in an offline snapshot export.", "error");
      return;
    }
    if (!file || state.setup.importRunning) {
      return;
    }
    try {
      state.setup.importRunning = true;
      if (importSystemBackupButton) {
        importSystemBackupButton.disabled = true;
      }
      setStatus(`Importing full backup from ${file.name}...`);
      const passphrase = readOptionalSecretValue(systemBackupImportPassphrase);
      const response = await fetch("/api/system-backup/import", {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
          ...(passphrase !== null
            ? { "X-Backup-Passphrase-Base64": encodeUtf8Base64(passphrase) }
            : {}),
        },
        body: await file.arrayBuffer(),
      });
      const result = await response.json();
      if (!response.ok || result.ok === false) {
        throw new Error(result.detail || `Request failed with ${response.status}`);
      }
      const nextSystemId = result.default_system_id || result.system?.id || result.systems?.[0]?.id || null;
      if (nextSystemId) {
        state.selectedSystemId = nextSystemId;
      }
      state.selectedEnclosureId = null;
      state.snapshotReuseCache = {};
      closeSystemSetupDialog();
      await refreshSnapshot(false, "system-backup-import");
      setStatus(
        `Imported full backup${result.restored_history_database ? " and restored history database" : ""}.`
      );
    } catch (error) {
      setStatus(`Full backup import failed: ${error.message || error}`, "error");
    } finally {
      state.setup.importRunning = false;
      if (importSystemBackupButton) {
        importSystemBackupButton.disabled = false;
      }
      if (systemBackupImportFile) {
        systemBackupImportFile.value = "";
      }
    }
  }

  async function createSystemFromWalkthrough() {
    if (state.setup.createRunning) {
      return;
    }
    try {
      state.setup.createRunning = true;
      syncSystemSetupStep();
      const payload = collectSystemSetupPayload();
      if (!payload.label || !payload.truenas_host) {
        throw new Error("System label and host are required.");
      }
      setStatus(`Creating system entry for ${payload.label}...`);
      const result = await fetchJson("/api/system-setup", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.selectedSystemId = result.system?.id || state.selectedSystemId;
      state.selectedEnclosureId = null;
      state.snapshotReuseCache = {};
      closeSystemSetupDialog();
      await refreshSnapshot(false, "system-setup");
      setStatus(`Created system ${result.system?.label || payload.label}.`);
    } catch (error) {
      setStatus(`System setup failed: ${error.message || error}`, "error");
    } finally {
      state.setup.createRunning = false;
      syncSystemSetupStep();
    }
  }

  function cloneJsonValue(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function snapshotReuseCacheKey(systemId, enclosureId = null) {
    return `${systemId || "__system__"}::${enclosureId || "__auto__"}`;
  }

  function snapshotHasTrustedTopology(snapshot) {
    if (!snapshot) {
      return false;
    }
    const platform = String(snapshot.selected_system_platform || "").toLowerCase();
    if (platform !== "quantastor") {
      return true;
    }
    const platformContext = snapshot.platform_context;
    if (!platformContext || typeof platformContext !== "object") {
      return true;
    }
    return platformContext.topology_complete !== false;
  }

  function rememberReusableSnapshot(snapshot) {
    if (!snapshot || !snapshot.selected_system_id || !snapshotHasTrustedTopology(snapshot)) {
      return;
    }
    const cloned = cloneJsonValue(snapshot);
    state.snapshotReuseCache[snapshotReuseCacheKey(snapshot.selected_system_id, snapshot.selected_enclosure_id || null)] = cloned;
    state.snapshotReuseCache[snapshotReuseCacheKey(snapshot.selected_system_id, null)] = cloned;
  }

  function findReusableSnapshot(systemId, enclosureId = null) {
    const exact = state.snapshotReuseCache[snapshotReuseCacheKey(systemId, enclosureId)];
    if (exact) {
      return cloneJsonValue(exact);
    }
    const fallback = state.snapshotReuseCache[snapshotReuseCacheKey(systemId, null)];
    return fallback ? cloneJsonValue(fallback) : null;
  }

  function applyReusableSnapshot(systemId, enclosureId = null) {
    const reusableSnapshot = findReusableSnapshot(systemId, enclosureId);
    if (!reusableSnapshot) {
      return false;
    }
    if (enclosureId) {
      reusableSnapshot.selected_enclosure_id = enclosureId;
      const selectedOption = (reusableSnapshot.enclosures || []).find((option) => option.id === enclosureId);
      if (selectedOption) {
        reusableSnapshot.selected_enclosure_label = selectedOption.label || reusableSnapshot.selected_enclosure_label;
        reusableSnapshot.selected_enclosure_name = selectedOption.name || reusableSnapshot.selected_enclosure_name;
      }
    }
    applySnapshot(reusableSnapshot);
    renderAll();
    return true;
  }

  function applySnapshot(snapshot) {
    rememberReusableSnapshot(snapshot);
    state.snapshot = snapshot;
    state.layoutRows = snapshot.layout_rows || state.layoutRows || [];
    state.selectedSystemId = snapshot.selected_system_id || state.selectedSystemId;
    state.selectedEnclosureId = snapshot.selected_enclosure_id || null;
    pruneSmartSummaryCache();
    if (state.selectedSlot !== null && !getSlotById(state.selectedSlot) && !getSelectedStorageViewRuntimeSlot(state.selectedSlot)) {
      state.selectedSlot = null;
    }
  }

  function pruneSmartSummaryCache() {
    const validKeys = new Set(
      (state.snapshot.slots || []).map((slot) => getSmartCacheKey(slot))
    );
    const currentScopePrefix = `${currentSmartPrefetchScopeKey()}|`;
    Object.keys(state.smartSummaries).forEach((key) => {
      const entry = state.smartSummaries[key];
      const stale = smartSummaryAgeMs(entry) > SMART_SUMMARY_CACHE_TTL_MS && !isSmartEntryInFlight(entry);
      const invalidForCurrentScope = key.startsWith(currentScopePrefix) && !validKeys.has(key);
      if (stale || invalidForCurrentScope) {
        delete state.smartSummaries[key];
      }
    });
  }

  function syncLocation() {
    if (state.snapshotMode) {
      return;
    }
    const params = buildSelectionParams({ includeStorageView: true });
    const query = params.toString();
    window.history.replaceState({}, "", query ? `/?${query}` : "/");
  }

  function setStatus(message, tone = "info") {
    statusText.textContent = message;
    statusText.dataset.tone = tone;
  }

  function uiPerfNow() {
    return window.performance && typeof window.performance.now === "function"
      ? window.performance.now()
      : Date.now();
  }

  function uiPerfRound(value) {
    return Number.isFinite(value) ? Math.round(value * 10) / 10 : null;
  }

  function uiPerfElapsed(startedAt, finishedAt) {
    return Number.isFinite(startedAt) && Number.isFinite(finishedAt)
      ? uiPerfRound(finishedAt - startedAt)
      : null;
  }

  function uiPerfReasonLabel(reason) {
    switch (reason) {
      case "system-switch":
        return "System Switch";
      case "enclosure-switch":
        return "Enclosure Switch";
      case "startup-led-verify":
        return "Startup LED Verify";
      case "system-switch-led-verify":
        return "System LED Verify";
      case "enclosure-switch-led-verify":
        return "Enclosure LED Verify";
      case "manual-refresh":
        return "Manual Refresh";
      case "auto-refresh":
        return "Auto Refresh";
      default:
        return reason || "UI Timing";
    }
  }

  function refreshStatusMessage(force, reason) {
    if (reason === "system-switch") {
      return "Loading system view...";
    }
    if (reason === "enclosure-switch") {
      return "Loading enclosure view...";
    }
    if (reason === "startup-led-verify" || reason === "system-switch-led-verify" || reason === "enclosure-switch-led-verify") {
      return "Checking current identify LED state...";
    }
    return force ? "Refreshing inventory..." : "Auto-refreshing inventory...";
  }

  function shouldQueueIdentifyVerify() {
    if (state.snapshotMode || !state.snapshot) {
      return false;
    }
    if (currentPlatform() !== "quantastor") {
      return false;
    }
    return Array.isArray(state.snapshot.slots) && state.snapshot.slots.some((slot) => (
      slot && (slot.led_supported || slot.identify_active || slot.led_backend === "quantastor_sg_ses")
    ));
  }

  function queueIdentifyVerify(reason) {
    if (!shouldQueueIdentifyVerify()) {
      return;
    }
    if (state.identifyVerifyTimerId) {
      window.clearTimeout(state.identifyVerifyTimerId);
    }
    state.identifyVerifyTimerId = window.setTimeout(() => {
      state.identifyVerifyTimerId = null;
      if (!shouldQueueIdentifyVerify() || state.refreshesInFlight > 0) {
        return;
      }
      void refreshSnapshot(true, `${reason}-led-verify`);
    }, 0);
  }

  function uiPerfScopeLabel(summary) {
    const systemPart = summary.systemId || "system";
    const enclosurePart = summary.enclosureId || "auto";
    return `${systemPart} / ${enclosurePart}`;
  }

  function uiPerfMetricDisplay(summary, key, fallback = "n/a") {
    const value = summary[key];
    if (Number.isFinite(value)) {
      return `${value} ms`;
    }
    if (summary.status === "running") {
      if (key === "historyReadyMs" && !summary.historyTracked) {
        return fallback;
      }
      if (key === "smartReadyMs" && !summary.smartTracked) {
        return fallback;
      }
      return "pending";
    }
    return fallback;
  }

  function buildUiPerfSummary(run, status = null) {
    return {
      id: run.id,
      status: status || run.status || "running",
      reason: run.reason,
      reasonLabel: uiPerfReasonLabel(run.reason),
      systemId: run.systemId || state.selectedSystemId || null,
      enclosureId: run.enclosureId || state.selectedEnclosureId || null,
      startedAtIso: run.startedAtIso,
      requestMs: uiPerfElapsed(run.startedAt, run.inventoryResponseAt),
      paintMs: uiPerfElapsed(run.startedAt, run.renderPaintAt),
      historyReadyMs: uiPerfElapsed(run.startedAt, run.historyReadyAt),
      smartReadyMs: uiPerfElapsed(run.startedAt, run.smartSettledAt),
      settledMs: uiPerfElapsed(run.startedAt, run.settledAt),
      historyTracked: Boolean(run.historyTracked),
      smartTracked: Boolean(run.smartTracked),
      error: run.error || null,
    };
  }

  function syncUiPerfGlobal() {
    window.__JBOD_UI_PERF = {
      enabled: Boolean(state.uiPerf.enabled),
      current: state.uiPerf.currentRun ? buildUiPerfSummary(state.uiPerf.currentRun) : null,
      recentRuns: [...state.uiPerf.recentRuns],
    };
  }

  function renderUiPerfCard(summary, current = false) {
    return `
      <div class="ui-perf-card${current ? " current" : ""}">
        <div class="ui-perf-title">
          <span class="ui-perf-label">${escapeHtml(summary.reasonLabel)}</span>
          <span class="ui-perf-note">${escapeHtml(uiPerfScopeLabel(summary))}</span>
        </div>
        <div class="ui-perf-metrics">
          <div class="ui-perf-metric">
            <span class="ui-perf-metric-label">Request</span>
            <span class="ui-perf-metric-value">${escapeHtml(uiPerfMetricDisplay(summary, "requestMs"))}</span>
          </div>
          <div class="ui-perf-metric">
            <span class="ui-perf-metric-label">Paint</span>
            <span class="ui-perf-metric-value">${escapeHtml(uiPerfMetricDisplay(summary, "paintMs"))}</span>
          </div>
          <div class="ui-perf-metric">
            <span class="ui-perf-metric-label">History</span>
            <span class="ui-perf-metric-value">${escapeHtml(uiPerfMetricDisplay(summary, "historyReadyMs"))}</span>
          </div>
          <div class="ui-perf-metric">
            <span class="ui-perf-metric-label">SMART</span>
            <span class="ui-perf-metric-value">${escapeHtml(uiPerfMetricDisplay(summary, "smartReadyMs"))}</span>
          </div>
          <div class="ui-perf-metric">
            <span class="ui-perf-metric-label">Settled</span>
            <span class="ui-perf-metric-value">${escapeHtml(uiPerfMetricDisplay(summary, "settledMs"))}</span>
          </div>
          <div class="ui-perf-metric">
            <span class="ui-perf-metric-label">Status</span>
            <span class="ui-perf-metric-value">${escapeHtml(summary.error ? `error: ${summary.error}` : summary.status)}</span>
          </div>
        </div>
      </div>
    `;
  }

  function renderUiPerfPanel() {
    if (!uiPerfPanel || !uiPerfSummary || !uiPerfRecent) {
      return;
    }
    if (!state.uiPerf.enabled) {
      uiPerfPanel.classList.add("hidden");
      syncUiPerfGlobal();
      return;
    }

    uiPerfPanel.classList.remove("hidden");
    const currentSummary = state.uiPerf.currentRun ? buildUiPerfSummary(state.uiPerf.currentRun) : null;
    const latestSummary = currentSummary || state.uiPerf.recentRuns[0] || null;
    if (!latestSummary) {
      uiPerfSummary.textContent = "Browser timing is idle.";
      uiPerfRecent.innerHTML = "";
      syncUiPerfGlobal();
      return;
    }

    uiPerfSummary.textContent = currentSummary
      ? `Current ${currentSummary.reasonLabel.toLowerCase()} on ${uiPerfScopeLabel(currentSummary)}: request ${uiPerfMetricDisplay(currentSummary, "requestMs")}, paint ${uiPerfMetricDisplay(currentSummary, "paintMs")}, history ${uiPerfMetricDisplay(currentSummary, "historyReadyMs")}, SMART ${uiPerfMetricDisplay(currentSummary, "smartReadyMs")}, settled ${uiPerfMetricDisplay(currentSummary, "settledMs")}.`
      : `Last ${latestSummary.reasonLabel.toLowerCase()} on ${uiPerfScopeLabel(latestSummary)}: request ${uiPerfMetricDisplay(latestSummary, "requestMs")}, paint ${uiPerfMetricDisplay(latestSummary, "paintMs")}, history ${uiPerfMetricDisplay(latestSummary, "historyReadyMs")}, SMART ${uiPerfMetricDisplay(latestSummary, "smartReadyMs")}, settled ${uiPerfMetricDisplay(latestSummary, "settledMs")}.`;

    const recentCards = [];
    if (currentSummary) {
      recentCards.push(renderUiPerfCard(currentSummary, true));
    }
    recentCards.push(
      ...state.uiPerf.recentRuns
        .slice(0, UI_PERF_HISTORY_LIMIT)
        .map((summary) => renderUiPerfCard(summary))
    );
    uiPerfRecent.innerHTML = recentCards.join("");
    syncUiPerfGlobal();
  }

  function archiveUiPerfRun(run, status = "done", error = null) {
    if (!run) {
      return;
    }
    run.status = status;
    if (error) {
      run.error = error;
    }
    if (!Number.isFinite(run.settledAt)) {
      run.settledAt = Math.max(
        run.renderPaintAt || 0,
        run.historyReadyAt || 0,
        run.smartSettledAt || 0,
        uiPerfNow()
      );
    }
    const summary = buildUiPerfSummary(run, status);
    state.uiPerf.recentRuns.unshift(summary);
    state.uiPerf.recentRuns = state.uiPerf.recentRuns.slice(0, UI_PERF_HISTORY_LIMIT);
    if (state.uiPerf.currentRun && state.uiPerf.currentRun.id === run.id) {
      state.uiPerf.currentRun = null;
    }
    renderUiPerfPanel();
    console.info("[ui-perf]", summary);
  }

  function maybeFinalizeUiPerfRun(run) {
    if (!run || !state.uiPerf.currentRun || state.uiPerf.currentRun.id !== run.id) {
      return;
    }
    if (!Number.isFinite(run.renderPaintAt)) {
      return;
    }
    if (run.historyTracked && !Number.isFinite(run.historyReadyAt)) {
      return;
    }
    if (run.smartTracked && !Number.isFinite(run.smartSettledAt)) {
      return;
    }
    run.settledAt = Math.max(
      run.renderPaintAt || 0,
      run.historyReadyAt || 0,
      run.smartSettledAt || 0
    );
    archiveUiPerfRun(run, run.error ? "error" : "done");
  }

  function beginUiPerfRun(reason, details = {}) {
    if (!state.uiPerf.enabled) {
      return null;
    }
    if (state.uiPerf.currentRun) {
      archiveUiPerfRun(state.uiPerf.currentRun, "superseded");
    }
    state.uiPerf.currentRun = {
      id: `ui-perf-${++state.uiPerf.runCounter}`,
      reason,
      startedAt: uiPerfNow(),
      startedAtIso: new Date().toISOString(),
      systemId: details.systemId || null,
      enclosureId: details.enclosureId || null,
      inventoryResponseAt: null,
      renderPaintAt: null,
      historyReadyAt: null,
      smartSettledAt: null,
      settledAt: null,
      historyTracked: false,
      smartTracked: false,
      smartScopeKey: null,
      status: "running",
      error: null,
    };
    renderUiPerfPanel();
    return state.uiPerf.currentRun;
  }

  function completeUiPerfHistory(run) {
    if (!run || !state.uiPerf.currentRun || state.uiPerf.currentRun.id !== run.id) {
      return;
    }
    run.historyReadyAt = uiPerfNow();
    renderUiPerfPanel();
    maybeFinalizeUiPerfRun(run);
  }

  function setUiPerfSmartPending(run, tracked, scopeKey) {
    if (!run || !state.uiPerf.currentRun || state.uiPerf.currentRun.id !== run.id) {
      return;
    }
    run.smartTracked = tracked;
    run.smartScopeKey = tracked ? scopeKey : null;
    if (!tracked) {
      run.smartSettledAt = uiPerfNow();
      maybeFinalizeUiPerfRun(run);
    }
    renderUiPerfPanel();
  }

  function completeUiPerfSmart(scopeKey) {
    const run = state.uiPerf.currentRun;
    if (!run || !run.smartTracked || run.smartScopeKey !== scopeKey || Number.isFinite(run.smartSettledAt)) {
      return;
    }
    run.smartSettledAt = uiPerfNow();
    renderUiPerfPanel();
    maybeFinalizeUiPerfRun(run);
  }

  function waitForNextPaint() {
    return new Promise((resolve) => {
      window.requestAnimationFrame(() => window.requestAnimationFrame(resolve));
    });
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

  function renderSnapshotBanner() {
    if (!snapshotGeneratedValue || !state.snapshotMode) {
      return;
    }
    const generatedAt = state.snapshotExportMeta?.generated_at || state.snapshot?.last_updated || null;
    snapshotGeneratedValue.textContent = generatedAt ? formatTimestamp(generatedAt) : "Unknown";
    snapshotGeneratedValue.title = generatedAt || "";
    if (snapshotGeneratedNote) {
      const browserTimeZone = getBrowserTimeZone();
      snapshotGeneratedNote.textContent = browserTimeZone
        ? `Rendered in viewer local time (${browserTimeZone})`
        : "Rendered in viewer local time";
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

  function buildLiveSlotTileMarkup(slot) {
    return `
      <span class="slot-status-led" aria-hidden="true"></span>
      <span class="slot-number">${slot.slot_label}</span>
      <span class="slot-device">${escapeHtml(slotPrimaryLabel(slot))}</span>
      <span class="slot-pool">${escapeHtml(slot.pool_name || stateLabel(slot))}</span>
      <span class="slot-latch" aria-hidden="true"></span>
    `;
  }

  function isLiveStyledStorageView(view) {
    return Boolean(view && view.kind === "ses_enclosure");
  }

  function getLiveBackedStorageViewSlot(view, slot) {
    if (!isLiveStyledStorageView(view) || !slot || !Number.isInteger(slot.snapshot_slot)) {
      return null;
    }
    return getSlotById(slot.snapshot_slot);
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
    const enclosurePart =
      slot?.enclosure_id ||
      state.snapshot.selected_enclosure_id ||
      state.selectedEnclosureId ||
      "all-enclosures";
    const windowPart = currentHistoryWindowHours() === null ? "all" : String(currentHistoryWindowHours());
    return `${systemPart}|${enclosurePart}|${slot.slot}|${windowPart}`;
  }

  function getStorageViewSmartCacheKey(view, slot) {
    const systemPart = state.snapshot.selected_system_id || state.selectedSystemId || "system";
    return `${systemPart}|storage-view|${view.id}|${slot.slot_index}|${slot.device_name || slot.serial || "unknown"}`;
  }

  function buildStorageViewHistoryContextSlot(view, slot) {
    if (!view || !slot) {
      return null;
    }
    const usesLiveSlotHistory = Number.isInteger(slot.snapshot_slot);
    return {
      slot: usesLiveSlotHistory ? slot.snapshot_slot : slot.slot_index,
      slot_label: slot.slot_label,
      device_name: slot.device_name || null,
      serial: slot.serial || null,
      enclosure_id: usesLiveSlotHistory
        ? (view.backing_enclosure_id || currentLiveEnclosureId() || `${view.id}`)
        : `storage-view:${view.id}`,
      enclosure_label: usesLiveSlotHistory
        ? (view.backing_enclosure_label || view.label || null)
        : view.label,
      enclosure_name: view.label || null,
      history_scope_label: view.label || null,
      history_source_label: usesLiveSlotHistory ? (view.backing_enclosure_label || null) : null,
    };
  }

  function getSelectedHistoryTarget() {
    const selectedStorageView = getSelectedStorageViewRuntime();
    const storageViewSlot = selectedStorageView ? getSelectedStorageViewRuntimeSlot(state.selectedSlot) : null;
    const windowHours = currentHistoryWindowHours();
    if (selectedStorageView && storageViewSlot) {
      const slot = buildStorageViewHistoryContextSlot(selectedStorageView, storageViewSlot);
      const params = buildSelectionParams();
      if (Number.isInteger(windowHours)) {
        params.set("window_hours", String(windowHours));
      }
      const baseUrl = `/api/storage-views/${encodeURIComponent(selectedStorageView.id)}/slots/${storageViewSlot.slot_index}/history`;
      return {
        slot,
        cacheKey: getHistoryCacheKey(slot),
        fetchUrl: params.toString() ? `${baseUrl}?${params.toString()}` : baseUrl,
      };
    }

    const slot = getSlotById(state.selectedSlot);
    if (!slot) {
      return null;
    }
    const baseUrl = buildScopedUrl(`/api/slots/${slot.slot}/history`);
    const url = new URL(baseUrl, window.location.origin);
    if (Number.isInteger(windowHours)) {
      url.searchParams.set("window_hours", String(windowHours));
    }
    return {
      slot,
      cacheKey: getHistoryCacheKey(slot),
      fetchUrl: `${url.pathname}${url.search}`,
    };
  }

  function getSmartSummaryEntry(slot) {
    if (!slot) return null;
    const liveEntry = state.smartSummaries[getSmartCacheKey(slot)] || null;
    if (liveEntry) {
      return liveEntry;
    }
    const preloadedSummary =
      state.preloadedSmartSummariesBySlot[String(slot.slot)] ||
      state.preloadedSmartSummariesBySlot[slot.slot] ||
      null;
    if (!preloadedSummary) {
      return null;
    }
    return {
      loading: false,
      refreshing: false,
      data: preloadedSummary,
      requestedAt: 0,
      generation: state.smartSummaryGeneration,
    };
  }

  function getStorageViewSmartSummaryEntry(view, slot) {
    if (!view || !slot) return null;
    return state.smartSummaries[getStorageViewSmartCacheKey(view, slot)] || null;
  }

  function currentSmartPrefetchScopeKey() {
    const systemPart = state.snapshot.selected_system_id || state.selectedSystemId || "system";
    const enclosurePart = state.snapshot.selected_enclosure_id || state.selectedEnclosureId || "all-enclosures";
    return `${systemPart}|${enclosurePart}`;
  }

  function smartSummaryAgeMs(entry) {
    const requestedAt = Number(entry?.requestedAt) || 0;
    if (requestedAt <= 0) {
      return Number.POSITIVE_INFINITY;
    }
    return Date.now() - requestedAt;
  }

  function isSmartEntryCurrent(entry) {
    return Boolean(entry?.data) && smartSummaryAgeMs(entry) <= SMART_SUMMARY_CACHE_TTL_MS;
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
    const slotCount = currentLayoutSlotCount();
    const usesStaticGeometry =
      profile.faceStyle === "top-loader" ||
      profile.faceStyle === "unifi-drive" ||
      profile.faceStyle === "nvme-carrier" ||
      profile.faceStyle === "boot-devices" ||
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

  function shouldUseSingleSmartPrefetchRequest(slots) {
    if (SMART_PREFETCH_STRATEGY === "single") {
      return true;
    }
    if (SMART_PREFETCH_STRATEGY === "chunked") {
      return false;
    }
    return slots.length <= SMART_PREFETCH_SINGLE_THRESHOLD;
  }

  async function requestSmartBatchForSlots(slots) {
    return sendScopedRequest("/api/slots/smart-batch", {
      method: "POST",
      body: JSON.stringify({
        slots: slots.map((slot) => slot.slot),
        max_concurrency: Math.min(SMART_BATCH_REQUEST_MAX_CONCURRENCY, slots.length),
      }),
    });
  }

  function applySmartPrefetchPayload(slots, payload) {
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
    slots.forEach((slot) => {
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
  }

  function applySmartPrefetchError(slots, error) {
    slots.forEach((slot) => {
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

      const chunkedFetch = async () => {
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
              const payload = await requestSmartBatchForSlots(chunk);
              if (runToken !== state.smartPrefetchToken || scopeKey !== state.smartPrefetchScopeKey) {
                return;
              }
              applySmartPrefetchPayload(chunk, payload);
            } catch (error) {
              console.error("SMART prefetch failed", error);
              applySmartPrefetchError(chunk, error);
            }
            updateSmartPrefetchViews();
          }
        };

        const workerCount = Math.min(SMART_PREFETCH_BATCH_CONCURRENCY, chunks.length);
        await Promise.all(Array.from({ length: workerCount }, () => processNextChunk()));
      };

      const useSingleRequest = shouldUseSingleSmartPrefetchRequest(slots);
      if (useSingleRequest) {
        try {
          const payload = await requestSmartBatchForSlots(slots);
          if (runToken !== state.smartPrefetchToken || scopeKey !== state.smartPrefetchScopeKey) {
            return;
          }
          applySmartPrefetchPayload(slots, payload);
          updateSmartPrefetchViews();
        } catch (error) {
          console.error("SMART prefetch single-request path failed", error);
          if (SMART_PREFETCH_STRATEGY === "single") {
            applySmartPrefetchError(slots, error);
            updateSmartPrefetchViews();
          } else {
            await chunkedFetch();
          }
        }
      } else {
        await chunkedFetch();
      }
    } finally {
      if (runToken === state.smartPrefetchToken && scopeKey === state.smartPrefetchScopeKey) {
        state.smartPrefetchRunning = false;
        if (candidateSlotsForSmartPrefetch().length) {
          scheduleSmartPrefetch();
        } else {
          completeUiPerfSmart(scopeKey);
        }
      }
    }
  }

  function scheduleSmartPrefetch() {
    if (state.snapshotMode) {
      return;
    }
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

  function formatPowerCycleValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.power_cycle_count, smartEntry);
  }

  function formatPowerOnResetsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.power_on_resets, smartEntry);
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
    const text = String(value).trim().split(/error:/i, 1)[0].trim();
    if (!text) {
      return null;
    }
    if (/^0x[0-9a-f]+$/i.test(text)) {
      return text.toLowerCase();
    }
    if (/^[0-9a-f]+$/i.test(text)) {
      return `0x${text.toLowerCase()}`;
    }
    return null;
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
      return value.toLocaleString();
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

  function formatReadCommandsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.read_commands, smartEntry);
  }

  function formatWriteCommandsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.write_commands, smartEntry);
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

  function formatHardwareResetsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.hardware_resets, smartEntry);
  }

  function formatInterfaceCrcErrorsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.interface_crc_errors, smartEntry);
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

  function appendSmartTooltipMetrics(lines, slot, smartEntry) {
    if (smartEntry?.loading) {
      lines.push("SMART: loading...");
      return;
    }

    const temperature = formatTemperatureValue(slot, smartEntry);
    if (temperature !== "n/a") {
      lines.push(`Temp: ${temperature}`);
    }

    const smartHealth = formatSmartHealthStatusValue(smartEntry);
    if (smartHealth !== "n/a") {
      lines.push(`SMART: ${smartHealth}`);
    }

    const powerOn = formatPowerOnValue(smartEntry);
    if (powerOn !== "n/a" && powerOn !== "Unavailable") {
      lines.push(`Power On: ${powerOn}`);
    }

    const endurance = formatEnduranceValue(smartEntry);
    if (endurance !== "n/a") {
      lines.push(`Wear: ${endurance}`);
    }

    const availableSpare = formatAvailableSpareValue(smartEntry);
    if (availableSpare !== "n/a") {
      lines.push(`Spare: ${availableSpare}`);
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

    const protocolVersion = formatProtocolVersionValue(smartEntry);
    if (protocolVersion !== "n/a") {
      lines.push(`Protocol: ${protocolVersion}`);
    }

    const firmware = formatFirmwareValue(smartEntry);
    if (firmware !== "n/a") {
      lines.push(`Firmware: ${firmware}`);
    }

    const linkRate = formatLinkRateValue(smartEntry);
    if (linkRate !== "n/a") {
      lines.push(`Link Rate: ${linkRate}`);
    }

    const attachedSas = formatAttachedSasAddressValue(slot, smartEntry);
    if (attachedSas !== "n/a") {
      lines.push(`Attached SAS: ${attachedSas}`);
    }
  }

  function buildStorageViewTooltipLines(slot, selectedView, smartEntry) {
    if (!slot) {
      return [];
    }
    const persistentIdText = slot.gptid ? `${persistentIdLabel(slot)}: ${slot.gptid}` : `${persistentIdLabel(slot)}: n/a`;
    const lines = [
      `${selectedView?.label || "Storage View"} • ${slot.slot_label}`,
      slot.occupied ? (slot.serial || slot.device_name || "Matched disk") : "No live disk matched",
      slot.device_name ? `Device: ${slot.device_name}` : null,
      slot.serial ? `Serial: ${slot.serial}` : "Serial: n/a",
      persistentIdText,
      slot.pool_name ? `Pool: ${slot.pool_name}` : "Pool: n/a",
      slot.health ? `Health: ${slot.health}` : null,
      slot.target_system_label ? `Target: ${slot.target_system_label}` : null,
      slot.placement_key ? `Placement: ${slot.placement_key}` : null,
    ].filter(Boolean);

    appendSmartTooltipMetrics(lines, slot, smartEntry);

    const logicalUnitId = formatLogicalUnitIdValue(slot, smartEntry);
    if (logicalUnitId !== "n/a") {
      lines.push(`Logical Unit ID: ${logicalUnitId}`);
    }

    if (slot.transport_address) {
      lines.push(`Address: ${slot.transport_address}`);
    }

    const matchReasons = Array.isArray(slot.match_reasons) ? slot.match_reasons.filter(Boolean) : [];
    if (matchReasons.length) {
      lines.push(`Matched By: ${matchReasons.join(", ")}`);
    }

    if (slot.source) {
      lines.push(`Source: ${slot.source}`);
    }

    return lines;
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

    appendSmartTooltipMetrics(lines, slot, smartEntry);

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
    const selectedView = getSelectedStorageViewRuntime();
    let lines = [];

    if (selectedView) {
      const storageViewSlot = getSelectedStorageViewRuntimeSlot(state.hoveredSlot);
      if (!storageViewSlot) {
        state.hoveredSlot = null;
        hideSlotTooltip();
        return;
      }
      const smartEntry = getStorageViewSmartSummaryEntry(selectedView, storageViewSlot);
      lines = buildStorageViewTooltipLines(storageViewSlot, selectedView, smartEntry);
    } else {
      const slot = getSlotById(state.hoveredSlot);
      if (!slot) {
        state.hoveredSlot = null;
        hideSlotTooltip();
        return;
      }
      const smartEntry = getSmartSummaryEntry(slot);
      lines = buildTooltipLines(slot, smartEntry);
    }
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
    const selectedStorageView = getSelectedStorageViewRuntime();
    if (selectedStorageView) {
      if (!isLiveStyledStorageView(selectedStorageView)) {
        return { active: false, peerSlots: new Set() };
      }
      const selectedStorageViewSlot = getSelectedStorageViewRuntimeSlot(state.selectedSlot);
      const selectedLiveSlot = getLiveBackedStorageViewSlot(selectedStorageView, selectedStorageViewSlot);
      if (!selectedLiveSlot || !selectedLiveSlot.pool_name || !selectedLiveSlot.vdev_name) {
        return { active: false, peerSlots: new Set() };
      }

      const peerSlots = new Set(
        (selectedStorageView.slots || [])
          .filter((candidate) => {
            const candidateLiveSlot = getLiveBackedStorageViewSlot(selectedStorageView, candidate);
            return Boolean(
              candidateLiveSlot &&
              candidateLiveSlot.pool_name === selectedLiveSlot.pool_name &&
              candidateLiveSlot.vdev_name === selectedLiveSlot.vdev_name &&
              candidateLiveSlot.vdev_class === selectedLiveSlot.vdev_class &&
              candidateLiveSlot.device_name
            );
          })
          .map((candidate) => candidate.slot_index)
      );

      return {
        active: peerSlots.size > 1,
        peerSlots,
      };
    }
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
    const selectedStorageView = getSelectedStorageViewRuntime();
    if (selectedStorageView) {
      renderStorageViewGrid(selectedStorageView);
      return;
    }
    const selectedProfile = getSelectedProfile();
    if (selectedProfile?.face_style === "nvme-carrier") {
      renderLiveNvmeCarrierGrid(selectedProfile);
      return;
    }
    hideSlotTooltip();
    grid.innerHTML = "";
    const slotsByNumber = new Map(state.snapshot.slots.map((slot) => [slot.slot, slot]));
    const peerContext = getSelectedPeerContext();

    activeLayoutRows().forEach((row) => {
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
        if (!Number.isInteger(slotNumber)) {
          const gapTile = document.createElement("div");
          gapTile.className = "slot-gap";
          if (tileIndex !== null && breakpoints.includes(tileIndex + 1)) {
            gapTile.classList.add("group-divider-after");
          }
          gapTile.setAttribute("aria-hidden", "true");
          container.appendChild(gapTile);
          return;
        }
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
        tile.innerHTML = buildLiveSlotTileMarkup(slot);
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

  function wireCopyButtons(root) {
    if (!root) {
      return;
    }
    root.querySelectorAll("[data-copy]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(decodeURIComponent(button.dataset.copy));
          setStatus("Copied to clipboard.");
        } catch (error) {
          setStatus(`Copy failed: ${error}`, "error");
        }
      });
    });
  }

  function wireDetailCopyButtons() {
    wireCopyButtons(detailKvGrid);
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

  function currentHistoryReferenceTimestampMs() {
    if (state.snapshotMode && state.snapshotExportMeta?.generated_at) {
      const timestamp = new Date(state.snapshotExportMeta.generated_at).getTime();
      if (Number.isFinite(timestamp)) {
        return timestamp;
      }
    }
    return Date.now();
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

  function formatArtifactSize(sizeBytes) {
    const numericSize = Number(sizeBytes);
    if (!(numericSize >= 0)) {
      return "n/a";
    }
    if (numericSize < 1024) {
      return `${numericSize} B`;
    }
    const kib = numericSize / 1024;
    if (kib < 1024) {
      return `${kib.toFixed(1)} KiB`;
    }
    const mib = kib / 1024;
    if (mib < 1024) {
      return `${mib.toFixed(1)} MiB`;
    }
    return `${(mib / 1024).toFixed(1)} GiB`;
  }

  function loadStoredJson(storageKey) {
    try {
      const rawValue = window.localStorage.getItem(storageKey);
      return rawValue ? JSON.parse(rawValue) : null;
    } catch (error) {
      return null;
    }
  }

  function storeJson(storageKey, payload) {
    if (state.snapshotMode) {
      return;
    }
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(payload));
    } catch (error) {
      // Ignore storage write failures and keep the UI functional.
    }
  }

  function normalizePersistedPackaging(value) {
    return value === "zip" ? "zip" : value === "html" ? "html" : "auto";
  }

  function normalizePersistedHistoryWindowHours(value, fallbackValue = DEFAULT_HISTORY_TIMEFRAME_HOURS) {
    if (value === null || value === "all") {
      return null;
    }
    const numericValue = Number(value);
    return Number.isFinite(numericValue) && numericValue > 0
      ? numericValue
      : fallbackValue;
  }

  function normalizePersistedIoChartMode(value) {
    return value === "average" ? "average" : "total";
  }

  function persistHistoryUiPreferences() {
    storeJson(HISTORY_UI_STORAGE_KEY, {
      timeframeHours: state.history.timeframeHours,
      ioChartMode: state.history.ioChartMode,
    });
  }

  function persistExportUiPreferences() {
    storeJson(EXPORT_UI_STORAGE_KEY, {
      redactSensitive: state.export.redactSensitive,
      packaging: state.export.packaging,
      allowOversize: state.export.allowOversize,
    });
  }

  function snapshotExportRequestPayload() {
    const includeHistoryPanel = Boolean(state.history.panelOpen && isHistoryAvailable());
    return {
      selected_slot: state.selectedSlot,
      history_window_hours: currentHistoryWindowHours(),
      history_panel_open: includeHistoryPanel,
      io_chart_mode: state.history.ioChartMode,
      redact_sensitive: state.export.redactSensitive,
      packaging: state.export.packaging,
      allow_oversize: state.export.allowOversize,
    };
  }

  function estimatePackagingLabel(packaging) {
    if (packaging === "zip") {
      return "ZIP";
    }
    if (packaging === "html") {
      return "HTML";
    }
    if (packaging === "oversize") {
      return "Oversize";
    }
    return "n/a";
  }

  function estimateCurrentPackagingLabel(estimate) {
    if (!estimate) {
      return "n/a";
    }
    if (estimate.selected_packaging === "auto") {
      if (estimate.auto_packaging === "html") {
        return "Auto -> HTML";
      }
      if (estimate.auto_packaging === "zip") {
        return "Auto -> ZIP";
      }
      if (estimate.allow_oversize) {
        return "Auto -> Oversize ZIP";
      }
      return "Auto -> Over target";
    }
    return estimatePackagingLabel(estimate.effective_packaging || estimate.selected_packaging);
  }

  function estimateCurrentPackagingMeta(estimate) {
    if (!estimate) {
      return "Needs adjustment";
    }
    if (estimate.selected_size_label) {
      if (estimate.selected_within_limit) {
        return estimate.selected_size_label;
      }
      if (estimate.selected_allowed) {
        return `${estimate.selected_size_label} (over target)`;
      }
    }
    if (estimate.auto_packaging === "oversize") {
      return `ZIP ${estimate.zip_size_label || "n/a"} (over target)`;
    }
    return "Needs adjustment";
  }

  function buildEstimateAdvice(estimate) {
    if (!estimate) {
      return "Live estimate is unavailable right now. Export can still run with the current settings.";
    }
    const targetLabel = estimate.size_limit_label || "24 MiB";
    const downsamplingPart =
      estimate.downsampling_label && estimate.downsampling_label !== "None"
        ? ` ${estimate.downsampling_note || `Adaptive ${estimate.downsampling_label} will be applied.`}`
        : "";

    if (estimate.selected_packaging === "auto") {
      if (estimate.auto_packaging === "html") {
        return `Auto should stay in HTML and fit under ${targetLabel}.${downsamplingPart}`;
      }
      if (estimate.auto_packaging === "zip") {
        return `Auto is expected to switch to ZIP to stay under ${targetLabel}.${downsamplingPart}`;
      }
      if (estimate.allow_oversize) {
        return `Even after adaptive rollups, both HTML and ZIP are still over ${targetLabel}. Oversize export is enabled, so Auto will continue as ZIP at about ${estimate.zip_size_label || "n/a"}.${downsamplingPart}`;
      }
      return `Even after adaptive rollups, both HTML and ZIP are still over ${targetLabel}. Try a shorter history window, enable redaction, or allow oversize.`;
    }

    if (estimate.selected_packaging === "html") {
      if (estimate.selected_within_limit) {
        return `Plain HTML is estimated to fit under ${targetLabel}.${downsamplingPart}`;
      }
      if (estimate.selected_allowed) {
        return `Plain HTML is estimated at ${estimate.selected_size_label || estimate.html_size_label} and exceeds ${targetLabel}. Oversize export is enabled, so it can still continue.${downsamplingPart}`;
      }
      return `Plain HTML is estimated at ${estimate.selected_size_label || estimate.html_size_label} and would exceed ${targetLabel}. Try Auto, Force ZIP, a shorter history window, or allow oversize.`;
    }

    if (estimate.selected_packaging === "zip") {
      if (estimate.selected_within_limit) {
        return `ZIP is estimated to fit under ${targetLabel}.${downsamplingPart}`;
      }
      if (estimate.selected_allowed) {
        return `ZIP is estimated at ${estimate.selected_size_label || estimate.zip_size_label} and exceeds ${targetLabel}. Oversize export is enabled, so it can still continue.${downsamplingPart}`;
      }
      return `ZIP is estimated at ${estimate.selected_size_label || estimate.zip_size_label} and still exceeds ${targetLabel}. Try a shorter history window, enable redaction, or allow oversize.`;
    }

    return "Estimate ready.";
  }

  function renderSnapshotExportEstimate() {
    if (!exportSnapshotEstimate) {
      return;
    }
    const estimateState = state.export.estimate;
    if (estimateState.loading && !estimateState.data) {
      exportSnapshotEstimate.innerHTML = '<p class="snapshot-export-estimate-message">Estimating export size...</p>';
      return;
    }
    if (estimateState.error && !estimateState.data) {
      exportSnapshotEstimate.innerHTML = `<p class="snapshot-export-estimate-message warning">${escapeHtml(estimateState.error)}</p>`;
      return;
    }
    const estimate = estimateState.data;
    if (!estimate) {
      exportSnapshotEstimate.innerHTML = "";
      return;
    }

    const selectedTone = estimate.selected_within_limit
      ? "ok"
      : estimate.selected_allowed
        ? "warning"
        : "error";
    const adviceClass = estimate.selected_within_limit
      ? "snapshot-export-estimate-message"
      : `snapshot-export-estimate-message ${selectedTone === "error" ? "error" : "warning"}`;
    exportSnapshotEstimate.innerHTML = `
      <div class="snapshot-export-estimate-grid">
        <div class="snapshot-export-estimate-card">
          <span class="snapshot-export-estimate-label">HTML</span>
          <span class="snapshot-export-estimate-value">${escapeHtml(estimate.html_size_label || "n/a")}</span>
          <span class="snapshot-export-estimate-meta">${escapeHtml(estimate.html_within_limit ? "Fits target" : "Over target")}</span>
        </div>
        <div class="snapshot-export-estimate-card">
          <span class="snapshot-export-estimate-label">ZIP</span>
          <span class="snapshot-export-estimate-value">${escapeHtml(estimate.zip_size_label || "n/a")}</span>
          <span class="snapshot-export-estimate-meta">${escapeHtml(estimate.zip_within_limit ? "Fits target" : "Over target")}</span>
        </div>
        <div class="snapshot-export-estimate-card ${selectedTone === "error" ? "error" : selectedTone === "warning" ? "warning" : ""}">
          <span class="snapshot-export-estimate-label">Current Choice</span>
          <span class="snapshot-export-estimate-value">${escapeHtml(estimateCurrentPackagingLabel(estimate))}</span>
          <span class="snapshot-export-estimate-meta">${escapeHtml(estimateCurrentPackagingMeta(estimate))}</span>
        </div>
        <div class="snapshot-export-estimate-card">
          <span class="snapshot-export-estimate-label">Downsampling</span>
          <span class="snapshot-export-estimate-value">${escapeHtml(estimate.downsampling_label || "None")}</span>
          <span class="snapshot-export-estimate-meta">${escapeHtml(`${estimate.metric_sample_count ?? 0} samples / ${estimate.event_count ?? 0} events`)}</span>
        </div>
      </div>
      <p class="${adviceClass}">${escapeHtml(buildEstimateAdvice(estimate))}</p>
    `;
  }

  async function refreshSnapshotExportEstimate() {
    if (state.snapshotMode || !exportSnapshotDialog || !exportSnapshotDialog.open) {
      return;
    }
    const nextToken = state.export.estimate.requestToken + 1;
    state.export.estimate.requestToken = nextToken;
    state.export.estimate.loading = true;
    state.export.estimate.error = null;
    state.export.estimate.data = null;
    syncSnapshotExportDialog();

    try {
      const response = await fetch(buildScopedUrl("/api/export/enclosure-snapshot/estimate"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshotExportRequestPayload()),
      });
      if (!response.ok) {
        let detail = `Estimate failed with ${response.status}`;
        try {
          const payload = await response.json();
          detail = payload.detail || detail;
        } catch (error) {
          // Ignore parsing issues and use the HTTP status.
        }
        throw new Error(detail);
      }
      const estimate = await response.json();
      if (nextToken !== state.export.estimate.requestToken) {
        return;
      }
      state.export.estimate.data = estimate;
      state.export.estimate.error = null;
    } catch (error) {
      if (nextToken !== state.export.estimate.requestToken) {
        return;
      }
      state.export.estimate.error = `Live estimate unavailable: ${error.message || error}`;
      state.export.estimate.data = null;
    } finally {
      if (nextToken === state.export.estimate.requestToken) {
        state.export.estimate.loading = false;
        syncSnapshotExportDialog();
      }
    }
  }

  function syncSnapshotExportDialog() {
    if (!exportSnapshotNote) {
      return;
    }
    const scopeLabel =
      getSelectedEnclosureOption()?.label ||
      state.snapshot.selected_enclosure_label ||
      state.snapshot.selected_enclosure_id ||
      "current enclosure";
    const slot = getSlotById(state.selectedSlot);
    const parts = [
      `Scope ${scopeLabel}.`,
      `Window ${formatHistoryWindowDescription(currentHistoryWindowHours())}.`,
      state.export.packaging === "zip"
        ? "Force ZIP packaging."
        : state.export.packaging === "html"
          ? "Force plain HTML packaging."
          : "Auto prefers HTML, then falls back to ZIP if needed.",
      state.export.allowOversize
        ? "Oversize exports are allowed."
        : "Default target stays under about 24 MiB.",
      state.export.redactSensitive
        ? "Host and enclosure aliases plus partial ID masking are enabled."
        : "Full identifiers will be included.",
    ];
    if (slot) {
      parts.push(`Slot ${slot.slot_label} is currently selected and will stay selected in the snapshot.`);
      if (!isHistoryAvailable()) {
        parts.push("History is currently unavailable, so the snapshot will omit history data and hide the History action.");
      } else if (state.history.panelOpen) {
        parts.push("The history drawer is open now and will open in the snapshot too.");
      } else {
        parts.push("The history drawer is closed now and will stay closed in the snapshot.");
      }
    } else {
      parts.push("No slot is currently selected, so the snapshot will open with no bay preselected.");
    }
    if (state.export.estimate.data?.downsampling_label && state.export.estimate.data.downsampling_label !== "None") {
      parts.push(`Adaptive ${state.export.estimate.data.downsampling_label.toLowerCase()} will be used to stay closer to the size target.`);
    }
    if (state.export.estimate.error) {
      parts.push(state.export.estimate.error);
    }
    exportSnapshotNote.textContent = parts.join(" ");
    if (exportSnapshotWindowHint) {
      if (isHistoryAvailable()) {
        exportSnapshotWindowHint.textContent = `Snapshot history uses the current History window (${formatHistoryWindowDescription(currentHistoryWindowHours())}). Change it in the History drawer first if you want a different export range. New sessions default to 24h.`;
      } else {
        exportSnapshotWindowHint.textContent = "History is currently unavailable, so this snapshot will be exported without historical samples or events.";
      }
    }
    renderSnapshotExportEstimate();
    if (exportSnapshotConfirm) {
      const estimate = state.export.estimate.data;
      const shouldBlock = Boolean(estimate && !estimate.selected_allowed);
      exportSnapshotConfirm.disabled = state.export.running || state.export.estimate.loading || shouldBlock;
    }
  }

  function openSnapshotExportDialog() {
    if (!exportSnapshotDialog) {
      void exportEnclosureSnapshot();
      return;
    }
    if (exportRedactToggle) {
      exportRedactToggle.checked = state.export.redactSensitive;
    }
    if (exportPackagingSelect) {
      exportPackagingSelect.value = state.export.packaging;
    }
    if (exportAllowOversizeToggle) {
      exportAllowOversizeToggle.checked = state.export.allowOversize;
    }
    syncSnapshotExportDialog();
    if (typeof exportSnapshotDialog.showModal === "function") {
      if (!exportSnapshotDialog.open) {
        exportSnapshotDialog.showModal();
      }
    }
    void refreshSnapshotExportEstimate();
  }

  function closeSnapshotExportDialog() {
    if (exportSnapshotDialog && exportSnapshotDialog.open) {
      exportSnapshotDialog.close();
    }
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

  function isHistoryCounterScaleDiscontinuity(metricName, previousValue, currentValue) {
    if (metricName !== "bytes_read" && metricName !== "bytes_written") {
      return false;
    }
    if (!Number.isFinite(previousValue) || !Number.isFinite(currentValue) || previousValue <= 0 || currentValue < previousValue) {
      return false;
    }
    return previousValue <= 1024 ** 3 && (currentValue / previousValue) >= 1024;
  }

  function hasHistoryCounterScaleDiscontinuity(metricName, samples, windowHours = null, referenceTimestampMs = Date.now()) {
    const ordered = filterHistorySamplesToWindow(samples, windowHours, referenceTimestampMs);
    if (ordered.length < 2) {
      return false;
    }

    for (let index = 1; index < ordered.length; index += 1) {
      const previousValue = Number(ordered[index - 1]?.value);
      const currentValue = Number(ordered[index]?.value);
      if (isHistoryCounterScaleDiscontinuity(metricName, previousValue, currentValue)) {
        return true;
      }
    }
    return false;
  }

  function summarizeHistoryCounterChanges(metricName, samples, windowHours = null, referenceTimestampMs = Date.now()) {
    const ordered = filterHistorySamplesToWindow(samples, windowHours, referenceTimestampMs);
    if (ordered.length < 2) {
      return null;
    }

    let totalDelta = 0;
    let totalHours = 0;
    let segmentCount = 0;
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
        || isHistoryCounterScaleDiscontinuity(metricName, previousValue, currentValue)
        || !Number.isFinite(previousTimestamp)
        || !Number.isFinite(currentTimestamp)
        || currentTimestamp <= previousTimestamp
      ) {
        continue;
      }
      totalDelta += currentValue - previousValue;
      totalHours += (currentTimestamp - previousTimestamp) / 3600000;
      segmentCount += 1;
    }

    if (!segmentCount) {
      return null;
    }

    return {
      totalDelta,
      totalHours,
      segmentCount,
    };
  }

  function computeHistoryCounterAverage(metricName, samples, windowHours = null, referenceTimestampMs = Date.now()) {
    const summary = summarizeHistoryCounterChanges(metricName, samples, windowHours, referenceTimestampMs);
    if (!summary || !(summary.totalHours > 0) || summary.totalDelta <= 0) {
      return null;
    }

    return summary.totalDelta / summary.totalHours;
  }

  function formatHistoryWindowDelta(metricName, samples, windowHours = null, prefix = "Delta", referenceTimestampMs = Date.now()) {
    const summary = summarizeHistoryCounterChanges(metricName, samples, windowHours, referenceTimestampMs);
    if (!summary || summary.totalDelta < 0) {
      return null;
    }

    const windowLabel = Number.isFinite(Number(windowHours)) && Number(windowHours) > 0
      ? formatHistoryWindowLabel(windowHours)
      : "";
    const prefixLabel = `${prefix}${windowLabel ? ` ${windowLabel}` : ""}`;
    if (metricName === "bytes_written" || metricName === "bytes_read") {
      if (summary.totalDelta === 0) {
        return `${prefixLabel} +0 B`;
      }
      const formatted = formatMetricBytes(summary.totalDelta);
      return formatted ? `${prefixLabel} +${formatted}` : null;
    }
    return null;
  }

  function formatHistoryAverage(metricName, samples, windowHours = null, prefix = "Rate", referenceTimestampMs = Date.now()) {
    const ratePerHour = computeHistoryCounterAverage(metricName, samples, windowHours, referenceTimestampMs);
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
    const windowDeltaLabel = formatHistoryWindowDelta(metricName, allSamples, windowHours, "Delta", referenceTimestampMs);
    if (windowDeltaLabel) {
      noteParts.push(windowDeltaLabel);
    }
    const averageLabel = formatHistoryAverage(metricName, allSamples, windowHours, "Rate", referenceTimestampMs);
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

  function getSelectedHistorySmartEntry() {
    const selectedStorageView = getSelectedStorageViewRuntime();
    const storageViewSlot = selectedStorageView ? getSelectedStorageViewRuntimeSlot(state.selectedSlot) : null;
    if (selectedStorageView && storageViewSlot) {
      return {
        slotLike: storageViewSlot,
        smartEntry: getStorageViewSmartSummaryEntry(selectedStorageView, storageViewSlot),
      };
    }

    const liveSlot = getSlotById(state.selectedSlot);
    return {
      slotLike: liveSlot,
      smartEntry: getSmartSummaryEntry(liveSlot),
    };
  }

  function buildHistoryNoteText(payload, windowHours = null, referenceTimestampMs = Date.now()) {
    const notes = [];
    const { slotLike, smartEntry } = getSelectedHistorySmartEntry();
    const smartMessage = smartEntry?.data?.message;
    if (smartMessage) {
      notes.push(smartMessage);
    }

    const ataHistoryCounterNote = (
      smartEntry?.data?.transport_protocol === "ATA"
      && ((payload?.metrics?.bytes_read || []).length || (payload?.metrics?.bytes_written || []).length)
    )
      ? "ATA read/write totals are lifetime SMART host counters. Lifetime shows the stored totals, while Rate converts them into per-hour movement between slower SMART samples."
      : null;
    if (ataHistoryCounterNote) {
      notes.push(ataHistoryCounterNote);
    }

    const lowHourAnnualizedNote = (
      Number.isInteger(smartEntry?.data?.power_on_hours)
      && smartEntry.data.power_on_hours < (24 * 30)
      && Number.isInteger(smartEntry?.data?.bytes_written)
    )
      ? "Annualized write stays hidden until the disk has at least about 30 days of power-on time."
      : null;
    if (lowHourAnnualizedNote) {
      notes.push(lowHourAnnualizedNote);
    }

    const enduranceEstimateNote = Number.isInteger(smartEntry?.data?.estimated_remaining_bytes_written)
      ? "Estimated write-endurance values extrapolate current writes against the NVMe percentage-used SMART field."
      : null;
    if (enduranceEstimateNote) {
      notes.push(enduranceEstimateNote);
    }

    const temperatureSamples = Number(payload?.sample_counts?.temperature_c);
    const slowMetricCounts = ["bytes_read", "bytes_written", "annualized_bytes_written", "power_on_hours"]
      .map((metricName) => Number(payload?.sample_counts?.[metricName]))
      .filter((count) => Number.isFinite(count) && count > 0);
    if (
      Number.isFinite(temperatureSamples)
      && slowMetricCounts.length
      && slowMetricCounts.some((count) => count < temperatureSamples)
    ) {
      notes.push("Read/write and power-on metrics are collected on the slower SMART cadence, so those samples can lag behind the latest temperature point.");
    }

    const hasCounterScaleBreak =
      hasHistoryCounterScaleDiscontinuity("bytes_read", payload?.metrics?.bytes_read || [], windowHours, referenceTimestampMs)
      || hasHistoryCounterScaleDiscontinuity("bytes_written", payload?.metrics?.bytes_written || [], windowHours, referenceTimestampMs);
    if (hasCounterScaleBreak) {
      notes.push("Read/write history includes an older low-scale sample, so Rate mode may need a few fresh slow samples to settle after the SMART counter-source correction.");
    }

    const diskHistory = payload?.disk_history;
    const historyHomes = Array.isArray(diskHistory?.homes) ? diskHistory.homes : [];
    if (diskHistory?.followed && historyHomes.length) {
      const priorHomes = historyHomes
        .filter((home) => home && home.sample_count)
        .map((home) => {
          const systemLabel = home.system_label || home.system_id || "unknown system";
          const enclosureLabel = home.enclosure_label || home.enclosure_id || "unknown enclosure";
          const slotLabel = home.slot_label || home.slot;
          const lastSeenAt = home.last_seen_at ? formatTimestamp(home.last_seen_at) : null;
          return `${systemLabel} / ${enclosureLabel} / slot ${slotLabel}${lastSeenAt ? ` through ${lastSeenAt}` : ""}`;
        });
      if (priorHomes.length) {
        notes.push(`Disk metrics in this window follow the same disk across homes: ${priorHomes.join("; ")}. Slot-change events below still belong only to the currently selected slot.`);
      }
    }

    return notes.join(" ");
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
    const scopeLabel = slot.history_scope_label || null;
    const sourceLabel = slot.history_source_label || null;
    const enclosureLabel =
      scopeLabel ||
      slot.enclosure_label ||
      slot.enclosure_name ||
      slot.enclosure_id ||
      getSelectedEnclosureOption()?.label ||
      state.snapshot.selected_enclosure_label ||
      null;
    if (systemLabel) {
      fragments.push(systemLabel);
    }
    if (enclosureLabel) {
      fragments.push(enclosureLabel);
    }
    if (sourceLabel && sourceLabel !== enclosureLabel) {
      fragments.push(sourceLabel);
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

  function buildHistoryRateSamples(metricName, samples) {
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
        || isHistoryCounterScaleDiscontinuity(metricName, previousValue, currentValue)
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
      ? filterHistorySamplesToWindow(buildHistoryRateSamples("bytes_read", readSamples), windowHours, referenceTimestampMs)
      : filterHistorySamplesToWindow(readSamples, windowHours, referenceTimestampMs);
    const orderedWrite = mode === "average"
      ? filterHistorySamplesToWindow(buildHistoryRateSamples("bytes_written", writeSamples), windowHours, referenceTimestampMs)
      : filterHistorySamplesToWindow(writeSamples, windowHours, referenceTimestampMs);
    if (labelTarget) {
      const labelParts = [];
      if (orderedRead.length) {
        labelParts.push(`${orderedRead.length} ${mode === "average" ? "read rate samples" : "read counter samples"}`);
      }
      if (orderedWrite.length) {
        labelParts.push(`${orderedWrite.length} ${mode === "average" ? "write rate samples" : "write counter samples"}`);
      }
      labelParts.push(formatHistoryWindowDescription(windowHours));
      labelTarget.textContent = labelParts.join(" / ");
    }
    if (!orderedRead.length && !orderedWrite.length) {
      const emptyMessage = mode === "average"
        ? `Read and write rate mode needs at least two slower SMART samples inside ${formatHistoryWindowDescription(windowHours)}.`
        : `No read or write lifetime totals in ${formatHistoryWindowDescription(windowHours)} yet.`;
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
    const writeAverage = formatHistoryAverage("bytes_written", writeSamples, windowHours, "Write Rate", referenceTimestampMs);
    const readAverage = formatHistoryAverage("bytes_read", readSamples, windowHours, "Read Rate", referenceTimestampMs);
    const readPrimaryLabel = mode === "average"
      ? formatHistoryRateValue(readLatestValue)
      : formatHistoryMetricValue("bytes_read", readLatestValue);
    const writePrimaryLabel = mode === "average"
      ? formatHistoryRateValue(writeLatestValue)
      : formatHistoryMetricValue("bytes_written", writeLatestValue);
    const readWindowDelta = formatHistoryWindowDelta("bytes_read", readSamples, windowHours, "Read Delta", referenceTimestampMs);
    const writeWindowDelta = formatHistoryWindowDelta("bytes_written", writeSamples, windowHours, "Write Delta", referenceTimestampMs);
    const chartAriaLabel = mode === "average" ? "Read and write rate history" : "Read and write lifetime counter history";
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
          ${readLatestValue !== null ? `<span class="history-legend-chip"><i class="history-legend-swatch read"></i>${escapeHtml(mode === "average" ? `Read Rate ${readPrimaryLabel}` : `Read Lifetime ${readPrimaryLabel}`)}</span>` : ""}
          ${mode === "average"
            ? (readAverage ? `<span>${escapeHtml(readAverage)}</span>` : "")
            : (readWindowDelta ? `<span>${escapeHtml(readWindowDelta)}</span>` : "")}
          ${writeLatestValue !== null ? `<span class="history-legend-chip"><i class="history-legend-swatch write"></i>${escapeHtml(mode === "average" ? `Write Rate ${writePrimaryLabel}` : `Write Lifetime ${writePrimaryLabel}`)}</span>` : ""}
          ${mode === "average"
            ? (writeAverage ? `<span>${escapeHtml(writeAverage)}</span>` : "")
            : (writeWindowDelta ? `<span>${escapeHtml(writeWindowDelta)}</span>` : "")}
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

  function renderHistoryPanel() {
    if (!detailHistoryPanel || !historyToggleButton) {
      return;
    }

    const historyTarget = getSelectedHistoryTarget();
    const slot = historyTarget?.slot || null;
    const windowHours = currentHistoryWindowHours();
    const referenceTimestampMs = currentHistoryReferenceTimestampMs();
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
    if (detailHistoryNote) {
      detailHistoryNote.classList.add("hidden");
      detailHistoryNote.textContent = "";
    }

    const payload = historyTarget ? state.history.slotCache[historyTarget.cacheKey] : null;
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
    const historyNote = buildHistoryNoteText(payload, windowHours, referenceTimestampMs);
    if (detailHistoryNote) {
      detailHistoryNote.textContent = historyNote;
      detailHistoryNote.classList.toggle("hidden", !historyNote);
    }
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

  function isHistoryStatusCurrent() {
    const fetchedAt = Number(state.history.statusFetchedAt) || 0;
    return state.history.checked && fetchedAt > 0 && Date.now() - fetchedAt <= HISTORY_STATUS_CACHE_TTL_MS;
  }

  async function refreshHistoryStatus(silent = true) {
    if (state.snapshotMode) {
      state.history.loading = false;
      state.history.checked = true;
      renderStatus();
      renderHistoryPanel();
      return;
    }
    if (!state.history.configured) {
      state.history.loading = false;
      state.history.checked = true;
      state.history.available = false;
      state.history.detail = null;
      state.history.counts = {};
      state.history.collector = {};
      renderStatus();
      renderHistoryPanel();
      return;
    }

    if (isHistoryStatusCurrent()) {
      renderStatus();
      renderHistoryPanel();
      return;
    }

    if (state.history.statusRefreshPromise) {
      await state.history.statusRefreshPromise;
      return;
    }

    state.history.loading = true;
    renderStatus();

    state.history.statusRefreshPromise = (async () => {
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
        state.history.statusFetchedAt = Date.now();
        state.history.statusRefreshPromise = null;
      }
      if (!state.history.available) {
        state.history.panelOpen = false;
      }

      renderStatus();
      renderHistoryPanel();
    })();
    await state.history.statusRefreshPromise;
  }

  async function loadHistoryForSelectedSlot(force = false) {
    const historyTarget = getSelectedHistoryTarget();
    if (!historyTarget || !historyTarget.slot || !isHistoryAvailable()) {
      return;
    }

    const { slot, cacheKey, fetchUrl } = historyTarget;
    if (state.snapshotMode) {
      state.history.panelError = null;
      renderHistoryPanel();
      return;
    }
    if (!force && state.history.slotCache[cacheKey]) {
      state.history.panelError = null;
      renderHistoryPanel();
      return;
    }

    state.history.panelLoading = true;
    state.history.panelError = null;
    renderHistoryPanel();

    try {
      const payload = await fetchJson(fetchUrl);
      state.history.slotCache[cacheKey] = payload;
      const activeTarget = getSelectedHistoryTarget();
      if (!activeTarget || cacheKey !== activeTarget.cacheKey) {
        return;
      }
    } catch (error) {
      state.history.panelError = error.message || String(error);
    } finally {
      state.history.panelLoading = false;
    }

    renderHistoryPanel();
  }

  function buildSmartNoteText(slotLike, smartEntry) {
    const smartMessage = smartEntry?.data?.message;
    const ledControlNote = (
      !slotLike?.led_supported
      && String(slotLike?.led_reason || "").trim()
    )
      ? String(slotLike.led_reason).trim()
      : null;
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
    return [smartMessage, ledControlNote, ataVolumeCounterNote, lowHourAnnualizedNote, enduranceEstimateNote].filter(Boolean).join(" ");
  }

  function renderEmptyDetailState(message, { showDetailSecondary = true } = {}) {
    detailEmpty.textContent = message;
    detailEmpty.classList.remove("hidden");
    detailContent.classList.add("hidden");
    detailKvGrid.innerHTML = "";
    detailSmartNote.classList.add("hidden");
    detailSmartNote.textContent = "";
    detailSecondary.classList.toggle("hidden", !showDetailSecondary);
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
    renderHistoryPanel();
  }

  function renderLiveSlotDetail(slot, options = {}) {
    const detailTitle = options.detailTitle || `Slot ${slot.slot_label}`;
    const smartEntry = options.smartEntry || getSmartSummaryEntry(slot);
    const showSasTransportFields = shouldShowSasTransportFields(slot, smartEntry);
    const showLinkRate = showSasTransportFields || formatLinkRateValue(smartEntry) !== "n/a";
    const showQuantastorContext = currentPlatform() === "quantastor";
    const showLedControls = !state.snapshotMode && (slot.led_supported || slot.identify_active);

    detailEmpty.classList.add("hidden");
    detailContent.classList.remove("hidden");
    detailSecondary.classList.remove("hidden");
    detailLedControls.classList.toggle("hidden", !showLedControls);
    if (mappingEmpty) {
      mappingEmpty.classList.add("hidden");
    }
    mappingForm.classList.remove("hidden");
    detailSlotTitle.textContent = detailTitle;
    detailStatePill.textContent = stateLabel(slot);
    detailStatePill.className = `state-pill state-${slot.state}`;
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
      kvRowIfMeaningful("Power Cycles", formatPowerCycleValue(smartEntry)),
      kvRowIfMeaningful("Power-On Resets", formatPowerOnResetsValue(smartEntry)),
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
      kvRowIfMeaningful("Read Commands", formatReadCommandsValue(smartEntry)),
      kvRowIfMeaningful("Write Commands", formatWriteCommandsValue(smartEntry)),
      kvRowIfMeaningful("Media Errors", formatMediaErrorsValue(smartEntry)),
      kvRowIfMeaningful("Predictive Errors", formatPredictiveErrorsValue(smartEntry)),
      kvRowIfMeaningful("Non-Medium Errors", formatNonMediumErrorsValue(smartEntry)),
      kvRowIfMeaningful("Uncorrected Read", formatUncorrectedReadErrorsValue(smartEntry)),
      kvRowIfMeaningful("Uncorrected Write", formatUncorrectedWriteErrorsValue(smartEntry)),
      kvRowIfMeaningful("Unsafe Shutdowns", formatUnsafeShutdownsValue(smartEntry)),
      kvRowIfMeaningful("Hardware Resets", formatHardwareResetsValue(smartEntry)),
      kvRowIfMeaningful("Interface CRC Errors", formatInterfaceCrcErrorsValue(smartEntry)),
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

    const smartNoteText = buildSmartNoteText(slot, smartEntry);
    if (smartNoteText) {
      detailSmartNote.textContent = smartNoteText;
      detailSmartNote.classList.remove("hidden");
    } else {
      detailSmartNote.classList.add("hidden");
      detailSmartNote.textContent = "";
    }

    wireDetailCopyButtons();
    renderTopologyContext(slot);
    renderMultipathContext(slot);

    mappingForm.serial.value = slot.serial || "";
    mappingForm.device_name.value = slot.device_name || "";
    mappingForm.gptid.value = slot.gptid || "";
    mappingForm.notes.value = slot.notes || "";
    setMappingFormEnabled(!state.snapshotMode);
    ledButtons.forEach((button) => {
      button.disabled = state.snapshotMode || !slot.led_supported;
    });
    renderHistoryPanel();
    if (state.history.panelOpen && isHistoryAvailable()) {
      void loadHistoryForSelectedSlot(false);
    }
    if (typeof options.ensureSmart === "function") {
      void options.ensureSmart();
      return;
    }
    void ensureSmartSummary(slot);
  }

  function renderDetail() {
    const selectedStorageView = getSelectedStorageViewRuntime();
    const storageViewSlot = selectedStorageView ? getSelectedStorageViewRuntimeSlot(state.selectedSlot) : null;
    if (selectedStorageView) {
      if (!storageViewSlot) {
        renderEmptyDetailState(
          isLiveStyledStorageView(selectedStorageView)
            ? "Select a slot tile to view disk, pool, and mapping details."
            : "Select a storage-view slot to inspect how a live disk landed in this saved layout.",
          { showDetailSecondary: isLiveStyledStorageView(selectedStorageView) }
        );
        return;
      }

      const liveBackedSlot = getLiveBackedStorageViewSlot(selectedStorageView, storageViewSlot);
      if (liveBackedSlot) {
        renderLiveSlotDetail(liveBackedSlot, {
          detailTitle: `Slot ${storageViewSlot.slot_label}`,
          smartEntry: getStorageViewSmartSummaryEntry(selectedStorageView, storageViewSlot) || getSmartSummaryEntry(liveBackedSlot),
          ensureSmart: () => ensureStorageViewSmartSummary(selectedStorageView, storageViewSlot),
        });
        return;
      }

      const smartEntry = getStorageViewSmartSummaryEntry(selectedStorageView, storageViewSlot);
      if (!state.snapshotMode) {
        void ensureStorageViewSmartSummary(selectedStorageView, storageViewSlot);
      }

      detailEmpty.classList.add("hidden");
      detailContent.classList.remove("hidden");
      detailSecondary.classList.add("hidden");
      detailLedControls.classList.add("hidden");
      detailSlotTitle.textContent = `${selectedStorageView.label} / ${storageViewSlot.slot_label}`;
      detailStatePill.textContent = stateLabel(storageViewSlot);
      detailStatePill.className = `state-pill state-${storageViewSlot.state === "matched" ? "healthy" : (storageViewSlot.state || "unknown")}`;
      detailKvGrid.innerHTML = [
        (() => {
          const storageViewDetailSlot = {
            ...storageViewSlot,
            raw_status: {
              attached_sas_address: storageViewSlot.attached_sas_address,
            },
          };
          const showSasTransportFields = shouldShowSasTransportFields(storageViewDetailSlot, smartEntry);
          const showLinkRate = showSasTransportFields || formatLinkRateValue(smartEntry) !== "n/a";
          return [
        kvRow("Storage View", selectedStorageView.label),
        kvRow("Template", selectedStorageView.template_label || selectedStorageView.template_id),
        kvRow("Slot", storageViewSlot.slot_label),
        kvRowIfMeaningful("Configured Size", storageViewSlot.slot_size),
        kvRow("State", stateLabel(storageViewSlot)),
        kvRow("Device", storageViewSlot.device_name),
        kvRow("Serial", storageViewSlot.serial, true),
        kvRowIfMeaningful("Model", storageViewSlot.model),
        kvRowIfMeaningful("Capacity", storageViewSlot.size_human),
        kvRowIfMeaningful(persistentIdLabel(storageViewSlot), storageViewSlot.gptid, true),
        kvRowIfMeaningful("Namespace EUI64", formatNamespaceEui64Value(storageViewSlot, smartEntry), true),
        kvRowIfMeaningful("Namespace NGUID", formatNamespaceNguidValue(smartEntry), true),
        kvRowIfMeaningful("Pool", storageViewSlot.pool_name),
        kvRowIfMeaningful("Health", storageViewSlot.health),
        kvRow("Temp", formatTemperatureValue(storageViewSlot, smartEntry)),
        kvRowIfMeaningful("Warning Temp", formatWarningTemperatureValue(smartEntry)),
        kvRowIfMeaningful("Critical Temp", formatCriticalTemperatureValue(smartEntry)),
        kvRow("SMART Status", formatSmartHealthStatusValue(smartEntry)),
        kvRow("Last SMART Test", formatLastSmartTestValue(storageViewSlot, smartEntry)),
        kvRow("Power On", formatPowerOnValue(smartEntry)),
        kvRowIfMeaningful("Power Cycles", formatPowerCycleValue(smartEntry)),
        kvRowIfMeaningful("Power-On Resets", formatPowerOnResetsValue(smartEntry)),
        kvRow("Sector Size", formatSectorSizeValue(storageViewSlot, smartEntry)),
        kvRowIfMeaningful("Rotation", formatRotationValue(smartEntry)),
        kvRowIfMeaningful("SMART Form Factor", formatFormFactorValue(smartEntry)),
        kvRowIfMeaningful("Firmware", formatFirmwareValue(smartEntry)),
        kvRowIfMeaningful("Protocol Version", formatProtocolVersionValue(smartEntry)),
        kvRowIfMeaningful("Endurance", formatEnduranceValue(smartEntry)),
        kvRowIfMeaningful("Available Spare", formatAvailableSpareValue(smartEntry)),
        kvRowIfMeaningful("TRIM", formatTrimSupportedValue(smartEntry)),
        kvRowIfMeaningful("Bytes Read", formatBytesReadValue(smartEntry)),
        kvRowIfMeaningful("Bytes Written", formatBytesWrittenValue(smartEntry)),
        kvRowIfMeaningful("Annualized Write", formatAnnualizedWriteValue(smartEntry)),
        kvRowIfMeaningful("Est. TBW Left", formatEstimatedRemainingWriteValue(smartEntry)),
        kvRowIfMeaningful("Read Commands", formatReadCommandsValue(smartEntry)),
        kvRowIfMeaningful("Write Commands", formatWriteCommandsValue(smartEntry)),
        kvRowIfMeaningful("Media Errors", formatMediaErrorsValue(smartEntry)),
        kvRowIfMeaningful("Predictive Errors", formatPredictiveErrorsValue(smartEntry)),
        kvRowIfMeaningful("Non-Medium Errors", formatNonMediumErrorsValue(smartEntry)),
        kvRowIfMeaningful("Uncorrected Read", formatUncorrectedReadErrorsValue(smartEntry)),
        kvRowIfMeaningful("Uncorrected Write", formatUncorrectedWriteErrorsValue(smartEntry)),
        kvRowIfMeaningful("Unsafe Shutdowns", formatUnsafeShutdownsValue(smartEntry)),
        kvRowIfMeaningful("Hardware Resets", formatHardwareResetsValue(smartEntry)),
        kvRowIfMeaningful("Interface CRC Errors", formatInterfaceCrcErrorsValue(smartEntry)),
        kvRowIfMeaningful("Read Cache", formatReadCacheValue(smartEntry)),
        kvRowIfMeaningful("Writeback Cache", formatWritebackCacheValue(smartEntry)),
        kvRowIfMeaningful("Transport", formatTransportValue(smartEntry)),
        showSasTransportFields ? kvRow("Logical Unit ID", formatLogicalUnitIdValue(storageViewDetailSlot, smartEntry)) : "",
        showSasTransportFields ? kvRow("SAS Address", formatSasAddressValue(storageViewDetailSlot, smartEntry)) : "",
        showSasTransportFields ? kvRow("Attached SAS", formatAttachedSasAddressValue(storageViewDetailSlot, smartEntry)) : "",
        showLinkRate ? kvRow("Link Rate", formatLinkRateValue(smartEntry)) : "",
        kvRowIfMeaningful("Transport Address", storageViewSlot.transport_address),
        kvRowIfMeaningful("Placement", storageViewSlot.placement_key),
        kvRowIfMeaningful("Matched By", Array.isArray(storageViewSlot.match_reasons) ? storageViewSlot.match_reasons.join(", ") : null),
        kvRowIfMeaningful("Assignment Rank", storageViewSlot.assignment_rank),
        kvRowIfMeaningful("Source", storageViewSlot.source),
        kvRowIfMeaningful("Notes", storageViewRuntimeSecondaryLabel(storageViewSlot)),
          ].filter(Boolean).join("");
        })(),
      ].filter(Boolean).join("");
      wireDetailCopyButtons();
      const smartNoteText = buildSmartNoteText(storageViewSlot, smartEntry);
      if (smartNoteText) {
        detailSmartNote.textContent = smartNoteText;
        detailSmartNote.classList.remove("hidden");
      } else {
        detailSmartNote.classList.add("hidden");
        detailSmartNote.textContent = "";
      }
      renderTopologyContext(null);
      renderMultipathContext(null);
      resetMappingForm();
      if (mappingEmpty) {
        mappingEmpty.classList.remove("hidden");
      }
      mappingForm.classList.add("hidden");
      setMappingFormEnabled(false);
      renderHistoryPanel();
      if (state.history.panelOpen && isHistoryAvailable()) {
        void loadHistoryForSelectedSlot(false);
      }
      return;
    }

    const slot = getSlotById(state.selectedSlot);
    if (!slot) {
      renderEmptyDetailState("Select a slot tile to view disk, pool, and mapping details.");
      return;
    }

    renderLiveSlotDetail(slot);
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

    if (snapshotStatusChip) {
      if (state.snapshotMode) {
        const generatedAt = state.snapshotExportMeta?.generated_at;
        snapshotStatusChip.className = "status-chip snapshot";
        snapshotStatusChip.textContent = "SNAPSHOT";
        snapshotStatusChip.title = generatedAt
          ? `Frozen offline artifact generated ${formatTimestamp(generatedAt)}.`
          : "Frozen offline artifact.";
      } else {
        snapshotStatusChip.className = "status-chip hidden";
        snapshotStatusChip.textContent = "SNAPSHOT";
        snapshotStatusChip.title = "";
      }
    }

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
    renderSnapshotBanner();
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

  function renderPlatformDetailsEntryGrid(entries) {
    if (!Array.isArray(entries) || !entries.length) {
      return "";
    }
    return `
      <div class="platform-details-entry-grid">
        ${entries.map((entry) => kvRow(entry.label, entry.value, Boolean(entry.copyable))).join("")}
      </div>
    `;
  }

  function renderPlatformDetailsGroup(group) {
    const entries = Array.isArray(group?.entries) ? group.entries.filter(Boolean) : [];
    if (!entries.length) {
      return "";
    }
    return `
      <article class="platform-details-group-card">
        <div class="platform-details-group-head">
          <div class="platform-details-group-title">${escapeHtml(group.title || "Details")}</div>
          ${group.summary ? `<div class="platform-details-group-summary">${escapeHtml(group.summary)}</div>` : ""}
        </div>
        ${renderPlatformDetailsEntryGrid(entries)}
      </article>
    `;
  }

  function renderPlatformDetailsSection(section) {
    const entries = Array.isArray(section?.entries) ? section.entries.filter(Boolean) : [];
    const groups = Array.isArray(section?.groups) ? section.groups.filter(Boolean) : [];
    if (!entries.length && !groups.length) {
      return "";
    }
    const isOpen = Boolean(state.platformDetails.expandedSections[section.id]);
    return `
      <section class="platform-details-section${isOpen ? " is-open" : ""}">
        <button
          class="platform-details-toggle"
          type="button"
          data-platform-details-section-toggle="${escapeHtml(section.id)}"
          aria-expanded="${isOpen ? "true" : "false"}"
        >
          <span class="platform-details-chevron">▸</span>
          <span class="platform-details-toggle-text">
            <span class="platform-details-section-title">${escapeHtml(section.title || "Section")}</span>
            ${section.summary ? `<span class="platform-details-section-summary">(${escapeHtml(section.summary)})</span>` : ""}
          </span>
        </button>
        <div class="platform-details-body${isOpen ? "" : " hidden"}">
          ${renderPlatformDetailsEntryGrid(entries)}
          ${groups.length ? `<div class="platform-details-group-grid">${groups.map((group) => renderPlatformDetailsGroup(group)).join("")}</div>` : ""}
        </div>
      </section>
    `;
  }

  function renderPlatformDetails() {
    if (!platformDetailsToggleButton || !platformDetailsPanel || !platformDetailsSections) {
      return;
    }

    const details = currentPlatformDetails();
    if (!details) {
      state.platformDetails.open = false;
      state.platformDetails.expandedSections = {};
      platformDetailsToggleButton.classList.add("hidden");
      platformDetailsToggleButton.setAttribute("aria-expanded", "false");
      platformDetailsToggleButton.textContent = "Platform Details";
      platformDetailsPanel.classList.add("hidden");
      platformDetailsSections.innerHTML = "";
      return;
    }

    const validSectionIds = new Set(details.sections.map((section) => section.id).filter(Boolean));
    Object.keys(state.platformDetails.expandedSections).forEach((sectionId) => {
      if (!validSectionIds.has(sectionId)) {
        delete state.platformDetails.expandedSections[sectionId];
      }
    });

    platformDetailsToggleButton.classList.remove("hidden");
    platformDetailsToggleButton.textContent = state.platformDetails.open ? "Hide Platform Details" : "Platform Details";
    platformDetailsToggleButton.setAttribute("aria-expanded", state.platformDetails.open ? "true" : "false");
    platformDetailsTitle.textContent = details.title || "Platform Details";
    platformDetailsSummary.textContent = details.summary || "Read-only platform context.";

    if (!state.platformDetails.open) {
      platformDetailsPanel.classList.add("hidden");
      platformDetailsSections.innerHTML = "";
      return;
    }

    platformDetailsPanel.classList.remove("hidden");
    platformDetailsSections.innerHTML = details.sections.map((section) => renderPlatformDetailsSection(section)).join("");
    platformDetailsSections.querySelectorAll("[data-platform-details-section-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const sectionId = button.dataset.platformDetailsSectionToggle;
        if (!sectionId) {
          return;
        }
        state.platformDetails.expandedSections[sectionId] = !state.platformDetails.expandedSections[sectionId];
        renderPlatformDetails();
      });
    });
    wireCopyButtons(platformDetailsSections);
  }

  function renderRefreshControls() {
    autoRefreshToggle.checked = state.autoRefresh;
    refreshIntervalSelect.value = String(state.refreshIntervalSeconds);
    refreshButton.disabled = state.snapshotMode;
    autoRefreshToggle.disabled = state.snapshotMode;
    refreshIntervalSelect.disabled = state.snapshotMode || !state.autoRefresh;
  }

  function renderSelectors() {
    const systems = state.snapshot.systems || [];
    const enclosures = state.snapshot.enclosures || [];
    const storageViews = getMainUiStorageViewRuntimeOptions();

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
      systemSelect.disabled = state.snapshotMode || systems.length <= 1;
    }

    if (enclosureSelect) {
      if (!enclosures.length && !storageViews.length) {
        enclosureSelect.innerHTML = '<option value="">Auto-selected</option>';
      } else {
        const enclosureOptions = enclosures
          .map((enclosure) => `<option value="enclosure:${escapeHtml(enclosure.id)}">${escapeHtml(selectorLabelForEnclosureOption(enclosure))}</option>`)
          .join("");
        const savedChassisViewOptions = storageViews
          .filter((view) => isSavedChassisView(view))
          .map((view) => `<option value="view:${escapeHtml(view.id)}">${escapeHtml(selectorLabelForStorageViewOption(view))}</option>`)
          .join("");
        const virtualStorageViewOptions = storageViews
          .filter((view) => !isSavedChassisView(view))
          .map((view) => `<option value="view:${escapeHtml(view.id)}">${escapeHtml(selectorLabelForStorageViewOption(view))}</option>`)
          .join("");
        enclosureSelect.innerHTML = [
          enclosureOptions ? `<optgroup label="Live Enclosures">${enclosureOptions}</optgroup>` : "",
          savedChassisViewOptions ? `<optgroup label="Saved Chassis Views">${savedChassisViewOptions}</optgroup>` : "",
          virtualStorageViewOptions ? `<optgroup label="Virtual Storage Views">${virtualStorageViewOptions}</optgroup>` : "",
        ].filter(Boolean).join("");
      }
      const selectedValue = state.selectedStorageViewRuntimeId
        ? `view:${state.selectedStorageViewRuntimeId}`
        : (currentLiveEnclosureId() ? `enclosure:${currentLiveEnclosureId()}` : "");
      if (selectedValue) {
        enclosureSelect.value = selectedValue;
      } else if (!enclosureSelect.value && enclosureSelect.options.length) {
        enclosureSelect.selectedIndex = 0;
      }
      enclosureSelect.disabled = state.snapshotMode || (enclosures.length + storageViews.length) <= 1;
    }
  }

  function renderAll() {
    renderViewChrome();
    renderPlatformDetails();
    renderGrid();
    renderDetail();
    renderStorageViewsRuntime();
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
    return fetchJson(buildScopedUrl(url), options);
  }

  function applyStorageViewRuntime(payload) {
    state.storageViewsRuntime = payload || {
      system_id: state.selectedSystemId || state.snapshot.selected_system_id || null,
      system_label: state.snapshot.selected_system_label || state.selectedSystemId || null,
      views: [],
    };
    ensureStorageViewRuntimeSelection();
  }

  async function fetchStorageViewRuntime(force = false, quiet = true) {
    if (state.snapshotMode) {
      return;
    }
    const requestToken = ++state.storageViewsRuntimeRequestToken;
    try {
      state.storageViewsRuntimeLoading = true;
      renderStorageViewsRuntime();
      renderSelectors();
      const params = buildSelectionParams();
      params.set("force", force ? "true" : "false");
      const payload = await fetchJson(`/api/storage-views?${params.toString()}`);
      if (requestToken !== state.storageViewsRuntimeRequestToken) {
        return;
      }
      applyStorageViewRuntime(payload);
      renderAll();
      if (!quiet) {
        setStatus(`Loaded ${Array.isArray(payload.views) ? payload.views.length : 0} storage view${Array.isArray(payload.views) && payload.views.length === 1 ? "" : "s"} for ${payload.system_label || payload.system_id || "the selected system"}.`);
      }
    } catch (error) {
      if (requestToken !== state.storageViewsRuntimeRequestToken) {
        return;
      }
      if (!quiet) {
        setStatus(`Storage view refresh failed: ${error.message || error}`, "error");
      }
    } finally {
      if (requestToken === state.storageViewsRuntimeRequestToken) {
        state.storageViewsRuntimeLoading = false;
        renderAll();
      }
    }
  }

  function resolveDownloadFilename(response, fallbackName) {
    const contentDisposition = response.headers.get("Content-Disposition") || "";
    const match = contentDisposition.match(/filename="([^"]+)"/i);
    return match?.[1] || fallbackName;
  }

  async function exportEnclosureSnapshot() {
    if (state.snapshotMode) {
      setStatus("This view is already an offline snapshot export.", "error");
      return;
    }
    if (state.export.running) {
      return;
    }
    try {
      state.export.running = true;
      if (exportSnapshotConfirm) {
        exportSnapshotConfirm.disabled = true;
      }
      setStatus("Preparing enclosure snapshot export...");
      const response = await fetch(buildScopedUrl("/api/export/enclosure-snapshot"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshotExportRequestPayload()),
      });
      if (!response.ok) {
        let detail = `Request failed with ${response.status}`;
        try {
          const payload = await response.json();
          detail = payload.detail || detail;
        } catch (error) {
          // Ignore JSON parsing failures and fall back to the HTTP status.
        }
        throw new Error(detail);
      }
      const blob = await response.blob();
      const objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = resolveDownloadFilename(response, `jbod-snapshot-${formatScopeLabel()}.html`);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(objectUrl);
      closeSnapshotExportDialog();
      const packaging = (response.headers.get("X-Export-Packaging") || "html").toUpperCase();
      const redactionMode = response.headers.get("X-Export-Redaction") || "none";
      const redaction = redactionMode === "partial" ? "partially redacted " : "";
      const sizeLabel = formatArtifactSize(response.headers.get("X-Export-Size-Bytes"));
      setStatus(`Exported ${redaction}${packaging} snapshot (${sizeLabel}).`);
    } catch (error) {
      setStatus(`Snapshot export failed: ${error.message || error}`, "error");
    } finally {
      state.export.running = false;
      if (exportSnapshotConfirm) {
        exportSnapshotConfirm.disabled = false;
      }
    }
  }

  async function refreshSnapshot(force = false, reason = force ? "manual-refresh" : "auto-refresh") {
    if (state.snapshotMode) {
      setStatus("Offline snapshot export. Live refresh is disabled.", "error");
      return;
    }
    const refreshToken = ++state.latestRefreshToken;
    state.refreshesInFlight += 1;
    cancelAutoRefreshTimer();
    const perfRun = beginUiPerfRun(reason, {
      systemId: state.selectedSystemId,
      enclosureId: state.selectedEnclosureId,
    });
    try {
      setStatus(refreshStatusMessage(force, reason));
      const params = buildSelectionParams();
      params.set("force", force ? "true" : "false");
      const snapshot = await fetchJson(`/api/inventory?${params.toString()}`);
      if (refreshToken !== state.latestRefreshToken) {
        if (perfRun && state.uiPerf.currentRun?.id === perfRun.id) {
          archiveUiPerfRun(perfRun, "superseded");
        }
        return;
      }
      if (perfRun && state.uiPerf.currentRun?.id === perfRun.id) {
        perfRun.inventoryResponseAt = uiPerfNow();
      }
      applySnapshot(snapshot);
      renderAll();
      void fetchStorageViewRuntime(force, true);
      await waitForNextPaint();
      if (refreshToken !== state.latestRefreshToken) {
        if (perfRun && state.uiPerf.currentRun?.id === perfRun.id) {
          archiveUiPerfRun(perfRun, "superseded");
        }
        return;
      }
      if (perfRun && state.uiPerf.currentRun?.id === perfRun.id) {
        perfRun.renderPaintAt = uiPerfNow();
        perfRun.systemId = state.selectedSystemId;
        perfRun.enclosureId = state.selectedEnclosureId;
        perfRun.historyTracked = !state.snapshotMode && state.history.configured;
      }
      renderUiPerfPanel();
      void refreshHistoryStatus(true).finally(() => {
        if (refreshToken === state.latestRefreshToken) {
          completeUiPerfHistory(perfRun);
        }
      });
      setUiPerfSmartPending(
        perfRun,
        !state.snapshotMode && candidateSlotsForSmartPrefetch().length > 0,
        currentSmartPrefetchScopeKey()
      );
      scheduleSmartPrefetch();
      maybeFinalizeUiPerfRun(perfRun);
      setStatus("Inventory updated.");
    } catch (error) {
      if (perfRun && state.uiPerf.currentRun?.id === perfRun.id) {
        archiveUiPerfRun(perfRun, "error", error.message || String(error));
      }
      if (refreshToken === state.latestRefreshToken) {
        state.storageViewsRuntimeLoading = false;
        renderStorageViewsRuntime();
        setStatus(`Refresh failed: ${error.message || error}`, "error");
      }
    } finally {
      state.refreshesInFlight = Math.max(0, state.refreshesInFlight - 1);
      if (refreshToken === state.latestRefreshToken && state.refreshesInFlight === 0) {
        scheduleAutoRefresh();
      }
    }
  }

  async function sendLedAction(action) {
    if (state.snapshotMode) {
      setStatus("LED actions are disabled in an offline snapshot export.", "error");
      return;
    }
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
    if (state.snapshotMode) {
      setStatus("Mapping changes are disabled in an offline snapshot export.", "error");
      return;
    }
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
    if (state.snapshotMode) {
      setStatus("Mapping changes are disabled in an offline snapshot export.", "error");
      return;
    }
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
    if (state.snapshotMode) {
      setStatus("Mapping export is disabled in an offline snapshot export.", "error");
      return;
    }
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
    if (state.snapshotMode) {
      setStatus("Mapping import is disabled in an offline snapshot export.", "error");
      return;
    }
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
    if (state.snapshotMode) {
      return;
    }
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

  async function ensureStorageViewSmartSummary(view, slot) {
    if (state.snapshotMode || !view || !slot) {
      return;
    }
    const cacheKey = getStorageViewSmartCacheKey(view, slot);
    const entry = state.smartSummaries[cacheKey];
    if (isSmartEntryCurrent(entry) && (entry?.data || isSmartEntryInFlight(entry))) {
      return;
    }

    state.smartSummaries[cacheKey] = {
      loading: !entry?.data,
      refreshing: Boolean(entry?.data),
      data: entry?.data || null,
      requestedAt: Date.now(),
      generation: state.smartSummaryGeneration,
    };
    if (state.selectedStorageViewRuntimeId === view.id && state.hoveredSlot === slot.slot_index) {
      refreshHoveredTooltip();
    }
    try {
      const params = buildSelectionParams();
      const scopedUrl = params.toString()
        ? `/api/storage-views/${encodeURIComponent(view.id)}/slots/${slot.slot_index}/smart?${params.toString()}`
        : `/api/storage-views/${encodeURIComponent(view.id)}/slots/${slot.slot_index}/smart`;
      const payload = await fetchJson(scopedUrl);
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

    if (state.selectedStorageViewRuntimeId === view.id && state.selectedSlot === slot.slot_index) {
      renderDetail();
    }
    if (state.selectedStorageViewRuntimeId === view.id && state.hoveredSlot === slot.slot_index) {
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

  function cancelAutoRefreshTimer() {
    if (state.timerId) {
      window.clearTimeout(state.timerId);
      state.timerId = null;
    }
  }

  function scheduleAutoRefresh(delayMs = state.refreshIntervalSeconds * 1000) {
    cancelAutoRefreshTimer();
    if (state.snapshotMode || !state.autoRefresh) {
      return;
    }
    const waitMs = Math.max(1000, Number.isFinite(delayMs) ? delayMs : state.refreshIntervalSeconds * 1000);
    state.timerId = window.setTimeout(async () => {
      state.timerId = null;
      if (state.refreshesInFlight > 0) {
        scheduleAutoRefresh();
        return;
      }
      await refreshSnapshot(false, "auto-refresh");
    }, waitMs);
  }

  function resetTimer() {
    scheduleAutoRefresh();
  }

  searchBox.addEventListener("input", (event) => {
    state.search = event.target.value.trim().toLowerCase();
    renderGrid();
  });

  refreshButton.addEventListener("click", () => refreshSnapshot(true, "manual-refresh"));
  if (systemSelect) {
    systemSelect.addEventListener("change", async (event) => {
      state.selectedSystemId = event.target.value || null;
      state.selectedEnclosureId = null;
      state.storageViewsRuntime = {
        system_id: state.selectedSystemId,
        system_label: getSelectedSystemOption()?.label || state.selectedSystemId,
        views: [],
      };
      state.storageViewsRuntimeLoading = true;
      state.selectedStorageViewRuntimeId = "";
      clearSelectedSlot();
      applyReusableSnapshot(state.selectedSystemId, null);
      await refreshSnapshot(false, "system-switch");
      queueIdentifyVerify("system-switch");
    });
  }
  if (enclosureSelect) {
    enclosureSelect.addEventListener("change", async (event) => {
      const rawValue = event.target.value || "";
      clearSelectedSlot();
      if (rawValue.startsWith("view:")) {
        state.selectedStorageViewRuntimeId = rawValue.slice("view:".length);
        state.selectedEnclosureId = currentLiveEnclosureId();
        renderAll();
        syncLocation();
        return;
      }
      state.selectedStorageViewRuntimeId = "";
      state.selectedEnclosureId = rawValue.startsWith("enclosure:") ? rawValue.slice("enclosure:".length) : (rawValue || null);
      state.storageViewsRuntimeLoading = true;
      applyReusableSnapshot(state.selectedSystemId, state.selectedEnclosureId);
      await refreshSnapshot(false, "enclosure-switch");
      queueIdentifyVerify("enclosure-switch");
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
  if (storageViewList) {
    storageViewList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-storage-view-runtime-id]");
      if (!button) {
        return;
      }
      state.selectedStorageViewRuntimeId = button.dataset.storageViewRuntimeId || "";
      renderStorageViewsRuntime();
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
  if (exportSnapshotButton) {
    exportSnapshotButton.addEventListener("click", openSnapshotExportDialog);
  }
  if (exportSnapshotCancel) {
    exportSnapshotCancel.addEventListener("click", closeSnapshotExportDialog);
  }
  if (exportSnapshotConfirm) {
    exportSnapshotConfirm.addEventListener("click", () => {
      void exportEnclosureSnapshot();
    });
  }
  if (exportRedactToggle) {
    exportRedactToggle.addEventListener("change", (event) => {
      state.export.redactSensitive = Boolean(event.target.checked);
      persistExportUiPreferences();
      syncSnapshotExportDialog();
      void refreshSnapshotExportEstimate();
    });
  }
  if (exportPackagingSelect) {
    exportPackagingSelect.addEventListener("change", (event) => {
      state.export.packaging = event.target.value === "zip"
        ? "zip"
        : event.target.value === "html"
          ? "html"
          : "auto";
      persistExportUiPreferences();
      syncSnapshotExportDialog();
      void refreshSnapshotExportEstimate();
    });
  }
  if (exportAllowOversizeToggle) {
    exportAllowOversizeToggle.addEventListener("change", (event) => {
      state.export.allowOversize = Boolean(event.target.checked);
      persistExportUiPreferences();
      syncSnapshotExportDialog();
      void refreshSnapshotExportEstimate();
    });
  }
  if (exportSnapshotDialog) {
    exportSnapshotDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeSnapshotExportDialog();
    });
  }
  if (systemSetupButton) {
    systemSetupButton.addEventListener("click", openSystemSetupDialog);
  }
  if (systemSetupClose) {
    systemSetupClose.addEventListener("click", closeSystemSetupDialog);
  }
  if (systemSetupDialog) {
    systemSetupDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeSystemSetupDialog();
    });
  }
  if (systemBackupPackagingSelect) {
    systemBackupPackagingSelect.addEventListener("change", () => {
      syncSystemBackupControls();
    });
  }
  if (systemBackupEncryptToggle) {
    systemBackupEncryptToggle.addEventListener("change", syncSystemBackupControls);
  }
  if (exportSystemBackupButton) {
    exportSystemBackupButton.addEventListener("click", () => {
      void exportSystemBackup();
    });
  }
  if (importSystemBackupButton && systemBackupImportFile) {
    importSystemBackupButton.addEventListener("click", () => systemBackupImportFile.click());
    systemBackupImportFile.addEventListener("change", (event) => {
      const [file] = event.target.files || [];
      if (!file) return;
      void importSystemBackupFromFile(file);
    });
  }
  if (setupPlatformSelect) {
    setupPlatformSelect.addEventListener("change", () => {
      syncSetupPlatformHelp();
      maybeLoadRecommendedSetupCommands();
    });
  }
  if (setupSshEnabledToggle) {
    setupSshEnabledToggle.addEventListener("change", () => {
      syncSetupSshFields();
      if (setupSshEnabledToggle.checked && setupSshHost && !setupSshHost.value.trim() && setupTruenasHost?.value.trim()) {
        setupSshHost.value = setupTruenasHost.value.trim();
      }
      if (setupSshEnabledToggle.checked && !state.setup.sshKeys.length) {
        void loadSetupSshKeys({ silent: true });
      }
    });
  }
  if (setupSshKeyMode) {
    setupSshKeyMode.addEventListener("change", () => {
      syncSetupSshKeyMode();
      syncSetupSshFields();
    });
  }
  if (setupSshExistingKeySelect) {
    setupSshExistingKeySelect.addEventListener("change", () => {
      applySelectedExistingSshKey();
      syncSetupSshKeyHelp();
    });
  }
  if (setupRefreshSshKeysButton) {
    setupRefreshSshKeysButton.addEventListener("click", () => {
      void loadSetupSshKeys();
    });
  }
  if (setupGenerateSshKeyButton) {
    setupGenerateSshKeyButton.addEventListener("click", () => {
      void generateSetupSshKey();
    });
  }
  if (setupSshGenerateName) {
    setupSshGenerateName.addEventListener("input", syncSetupSshKeyHelp);
  }
  if (setupSshKeyPath) {
    setupSshKeyPath.addEventListener("input", syncSetupSshKeyHelp);
  }
  if (setupTruenasHost && setupSshHost) {
    setupTruenasHost.addEventListener("change", () => {
      if (setupSshEnabledToggle?.checked && !setupSshHost.value.trim()) {
        setupSshHost.value = setupTruenasHost.value.trim();
      }
      if (state.setup.sshKeyMode === "generate" && setupSshGenerateName && !normalizeSetupSshKeyName(setupSshGenerateName.value)) {
        setupSshGenerateName.value = suggestedSetupSshKeyName();
      }
      syncSetupSshKeyHelp();
    });
  }
  [setupSystemLabel, setupSystemId].forEach((element) => {
    if (!element) {
      return;
    }
    element.addEventListener("input", () => {
      if (state.setup.sshKeyMode === "generate" && setupSshGenerateName && !normalizeSetupSshKeyName(setupSshGenerateName.value)) {
        setupSshGenerateName.value = suggestedSetupSshKeyName();
      }
      syncSetupSshKeyHelp();
    });
  });
  if (setupLoadPlatformCommandsButton) {
    setupLoadPlatformCommandsButton.addEventListener("click", () => {
      maybeLoadRecommendedSetupCommands(true);
    });
  }
  if (setupPrevButton) {
    setupPrevButton.addEventListener("click", () => setSystemSetupStep(state.setup.step - 1));
  }
  if (setupNextButton) {
    setupNextButton.addEventListener("click", () => setSystemSetupStep(state.setup.step + 1));
  }
  if (setupCreateButton) {
    setupCreateButton.addEventListener("click", () => {
      void createSystemFromWalkthrough();
    });
  }
  if (platformDetailsToggleButton) {
    platformDetailsToggleButton.addEventListener("click", () => {
      if (!currentPlatformDetails()) {
        return;
      }
      state.platformDetails.open = !state.platformDetails.open;
      renderPlatformDetails();
      if (state.platformDetails.open && platformDetailsPanel) {
        platformDetailsPanel.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  }
  if (historyCloseButton) {
    historyCloseButton.addEventListener("click", () => {
      state.history.panelOpen = false;
      renderHistoryPanel();
    });
  }
  if (historyToggleButton) {
    historyToggleButton.addEventListener("click", () => {
      if (!Number.isInteger(state.selectedSlot) || !isHistoryAvailable()) {
        return;
      }
      state.history.panelOpen = !state.history.panelOpen;
      renderHistoryPanel();
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
      persistHistoryUiPreferences();
      renderHistoryPanel();
      if (state.history.panelOpen && Number.isInteger(state.selectedSlot) && !state.snapshotMode && isHistoryAvailable()) {
        void loadHistoryForSelectedSlot(true);
      }
    });
  }
  historyIoModeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const nextMode = button.dataset.historyIoMode === "average" ? "average" : "total";
      if (state.history.ioChartMode === nextMode) {
        return;
      }
      state.history.ioChartMode = nextMode;
      persistHistoryUiPreferences();
      renderHistoryPanel();
    });
  });

  document.querySelectorAll("[data-led-action]").forEach((button) => {
    button.addEventListener("click", () => sendLedAction(button.dataset.ledAction));
  });

  rememberReusableSnapshot(state.snapshot);
  initializeSystemSetupForm();
  syncSystemBackupControls();
  renderAll();
  renderUiPerfPanel();
  if (state.snapshotMode) {
    setStatus("Frozen offline snapshot loaded. Live actions are disabled.");
  }
  void fetchStorageViewRuntime(false, true);
  void refreshHistoryStatus(true);
  scheduleSmartPrefetch();
  resetTimer();
  queueIdentifyVerify("startup");
})();
