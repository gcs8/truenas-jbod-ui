(function () {
  const bootstrap = window.APP_BOOTSTRAP || {};
  const supportedRefreshIntervals = [15, 30, 60, 300];
  const bootstrapRefreshInterval = Number(bootstrap.refreshIntervalSeconds) || 30;
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
  const warningList = document.getElementById("warning-list");
  const searchBox = document.getElementById("search-box");
  const refreshButton = document.getElementById("refresh-button");
  const autoRefreshToggle = document.getElementById("auto-refresh-toggle");
  const refreshIntervalSelect = document.getElementById("refresh-interval-select");
  const systemSelect = document.getElementById("system-select");
  const enclosureSelect = document.getElementById("enclosure-select");
  const lastUpdated = document.getElementById("last-updated");
  const apiStatusChip = document.getElementById("api-status-chip");
  const sshStatusChip = document.getElementById("ssh-status-chip");
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
    };
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
    if (state.selectedSlot !== null && !getSlotById(state.selectedSlot)) {
      state.selectedSlot = null;
    }
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

  function formatTimestamp(value) {
    if (!value) return "Unknown";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
  }

  function stateLabel(slot) {
    return (slot.state || "unknown").replace("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function slotPrimaryLabel(slot) {
    if (slot.device_name) return slot.device_name;
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

  function getSmartCacheKey(slot) {
    const systemPart = state.snapshot.selected_system_id || state.selectedSystemId || "system";
    const enclosurePart = state.snapshot.selected_enclosure_id || state.selectedEnclosureId || "all-enclosures";
    return `${systemPart}|${enclosurePart}|${slot.slot}|${slot.device_name || "unknown"}`;
  }

  function getSmartSummaryEntry(slot) {
    if (!slot) return null;
    return state.smartSummaries[getSmartCacheKey(slot)] || null;
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

  function formatCacheFlagValue(value, smartEntry) {
    if (value === true) {
      return "Enabled";
    }
    if (value === false) {
      return "Disabled";
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return "n/a";
  }

  function formatReadCacheValue(smartEntry) {
    return formatCacheFlagValue(smartEntry?.data?.read_cache_enabled, smartEntry);
  }

  function formatWritebackCacheValue(smartEntry) {
    return formatCacheFlagValue(smartEntry?.data?.writeback_cache_enabled, smartEntry);
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

  function formatAttachedSasAddressValue(smartEntry) {
    const formatted = formatHexIdentifier(smartEntry?.data?.attached_sas_address);
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

  function formatUnsafeShutdownsValue(smartEntry) {
    return formatOptionalCount(smartEntry?.data?.unsafe_shutdowns, smartEntry);
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

    const endurance = formatEnduranceValue(smartEntry);
    if (endurance !== "n/a") {
      lines.push(`Wear: ${endurance}`);
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
      const rowGroups = splitRowIntoGroups(row);
      rowSlots.style.gridTemplateColumns = rowGroups.map((group) => `minmax(0, ${group.length}fr)`).join(" ");

      rowGroups.forEach((group) => {
        const rowGroup = document.createElement("div");
        rowGroup.className = "row-group";
        rowGroup.style.setProperty("--group-columns", String(group.length));

        group.forEach((slotNumber) => {
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
          rowGroup.appendChild(tile);
        });

        rowSlots.appendChild(rowGroup);
      });

      rowWrapper.appendChild(rowSlots);
      grid.appendChild(rowWrapper);
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
      kvRow("Health", slot.health),
      kvRow("Temp", formatTemperatureValue(slot, smartEntry)),
      kvRowIfMeaningful("Warning Temp", formatWarningTemperatureValue(smartEntry)),
      kvRowIfMeaningful("Critical Temp", formatCriticalTemperatureValue(smartEntry)),
      kvRow("Last SMART Test", formatLastSmartTestValue(slot, smartEntry)),
      kvRow("Power On", formatPowerOnValue(smartEntry)),
      kvRow("Sector Size", formatSectorSizeValue(slot, smartEntry)),
      kvRow("Rotation", formatRotationValue(smartEntry)),
      kvRow("Form Factor", formatFormFactorValue(smartEntry)),
      kvRowIfMeaningful("Firmware", formatFirmwareValue(smartEntry)),
      kvRowIfMeaningful("Protocol Version", formatProtocolVersionValue(smartEntry)),
      kvRowIfMeaningful("Endurance", formatEnduranceValue(smartEntry)),
      kvRowIfMeaningful("Available Spare", formatAvailableSpareValue(smartEntry)),
      kvRowIfMeaningful("Bytes Read", formatBytesReadValue(smartEntry)),
      kvRowIfMeaningful("Bytes Written", formatBytesWrittenValue(smartEntry)),
      kvRowIfMeaningful("Annualized Write", formatAnnualizedWriteValue(smartEntry)),
      kvRowIfMeaningful("Est. TBW Left", formatEstimatedRemainingWriteValue(smartEntry)),
      kvRowIfMeaningful("Media Errors", formatMediaErrorsValue(smartEntry)),
      kvRowIfMeaningful("Unsafe Shutdowns", formatUnsafeShutdownsValue(smartEntry)),
      kvRow("Read Cache", formatReadCacheValue(smartEntry)),
      kvRow("Writeback Cache", formatWritebackCacheValue(smartEntry)),
      kvRow("Transport", formatTransportValue(smartEntry)),
      showSasTransportFields ? kvRow("Logical Unit ID", formatLogicalUnitIdValue(slot, smartEntry)) : "",
      showSasTransportFields ? kvRow("SAS Address", formatSasAddressValue(slot, smartEntry)) : "",
      showSasTransportFields ? kvRow("Attached SAS", formatAttachedSasAddressValue(smartEntry)) : "",
      showSasTransportFields ? kvRow("Link Rate", formatLinkRateValue(smartEntry)) : "",
      kvRow("Enclosure", slot.enclosure_label || slot.enclosure_name || slot.enclosure_id),
      kvRow("LED", ledStatusLabel(slot)),
      kvRow("Mapping", slot.mapping_source),
      kvRow("Notes", slot.notes),
    ].filter(Boolean).join("");

    const smartMessage = smartEntry?.data?.message;
    const enduranceEstimateNote = Number.isInteger(smartEntry?.data?.estimated_remaining_bytes_written)
      ? "Estimated write-endurance values extrapolate current writes against the NVMe percentage-used SMART field."
      : null;
    const smartNoteText = [smartMessage, enduranceEstimateNote].filter(Boolean).join(" ");
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

    lastUpdated.textContent = formatTimestamp(state.snapshot.last_updated);
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
    renderAll();
  }

  function clearSelectedSlot() {
    state.selectedSlot = null;
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
      setStatus(`Slot ${slot.slot_label} LED action ${action} completed via ${slot.led_backend === "ssh" ? "SSH SES" : "API"}.`);
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
    if (entry?.loading || entry?.data) {
      if (state.hoveredSlot === slot.slot) {
        refreshHoveredTooltip();
      }
      return;
    }

    state.smartSummaries[cacheKey] = { loading: true };
    if (state.hoveredSlot === slot.slot) {
      refreshHoveredTooltip();
    }
    try {
      const payload = await sendScopedRequest(`/api/slots/${slot.slot}/smart`);
      state.smartSummaries[cacheKey] = { loading: false, data: payload };
    } catch (error) {
      state.smartSummaries[cacheKey] = {
        loading: false,
        data: { available: false, message: error.message || String(error) },
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

  document.querySelectorAll("[data-led-action]").forEach((button) => {
    button.addEventListener("click", () => sendLedAction(button.dataset.ledAction));
  });

  renderAll();
  resetTimer();
})();
