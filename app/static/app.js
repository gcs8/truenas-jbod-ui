(function () {
  const bootstrap = window.APP_BOOTSTRAP || {};
  const supportedRefreshIntervals = [15, 30, 60, 300];
  const bootstrapRefreshInterval = Number(bootstrap.refreshIntervalSeconds) || 30;
  const state = {
    snapshot: bootstrap.snapshot || { slots: [], systems: [], enclosures: [] },
    layoutRows: bootstrap.layoutRows || [],
    selectedSlot: null,
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
  const detailEmpty = document.getElementById("detail-empty");
  const detailContent = document.getElementById("detail-content");
  const detailSecondary = document.getElementById("detail-secondary");
  const detailLedControls = document.getElementById("detail-led-controls");
  const detailSlotTitle = document.getElementById("detail-slot-title");
  const detailStatePill = document.getElementById("detail-state-pill");
  const detailKvGrid = document.getElementById("detail-kv-grid");
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

  function splitRowIntoGroups(row) {
    if (row.length === 15) {
      return [row.slice(0, 6), row.slice(6, 12), row.slice(12)];
    }
    return [row];
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

  function slotTooltip(slot) {
    return [
      `Slot ${slot.slot_label}`,
      slot.device_name ? `Device: ${slot.device_name}` : "Device: n/a",
      slot.serial ? `Serial: ${slot.serial}` : "Serial: n/a",
      slot.gptid ? `GPTID: ${slot.gptid}` : "GPTID: n/a",
      slot.pool_name ? `Pool: ${slot.pool_name}` : "Pool: n/a",
      slot.vdev_name ? `Vdev: ${slot.vdev_name}` : "Vdev: n/a",
      slot.health ? `Health: ${slot.health}` : "Health: n/a",
    ].join("\n");
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

  function formatSectorSizeValue(smartEntry) {
    const logical = smartEntry?.data?.logical_block_size;
    const physical = smartEntry?.data?.physical_block_size;
    if (Number.isInteger(logical) && Number.isInteger(physical)) {
      return `Logical ${logical} B / Physical ${physical} B`;
    }
    if (smartEntry?.loading) {
      return "Loading...";
    }
    return smartEntry?.data?.message ? "Unavailable" : "n/a";
  }

  function passesFilter(slot) {
    if (!state.search) return true;
    return (slot.search_text || "").includes(state.search);
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
          tile.title = slotTooltip(slot);
          tile.innerHTML = `
            <span class="slot-status-led" aria-hidden="true"></span>
            <span class="slot-number">${slot.slot_label}</span>
            <span class="slot-device">${escapeHtml(slotPrimaryLabel(slot))}</span>
            <span class="slot-pool">${escapeHtml(slot.pool_name || stateLabel(slot))}</span>
            <span class="slot-latch" aria-hidden="true"></span>
          `;
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
      detailSecondary.classList.remove("hidden");
      detailLedControls.classList.add("hidden");
      renderTopologyContext(null);
      renderMultipathContext(null);
      resetMappingForm();
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
    detailSlotTitle.textContent = `Slot ${slot.slot_label}`;
    detailStatePill.textContent = stateLabel(slot);
    detailStatePill.className = `state-pill state-${slot.state}`;
    const smartEntry = getSmartSummaryEntry(slot);

    detailKvGrid.innerHTML = [
      kvRow("Device", slot.device_name),
      kvRow("Serial", slot.serial, true),
      kvRow("Model", slot.model),
      kvRow("Size", slot.size_human),
      kvRow("GPTID", slot.gptid, true),
      kvRow("Pool", slot.pool_name),
      kvRow("Vdev", slot.vdev_name),
      kvRow("Class", slot.vdev_class),
      kvRow("Topology", slot.topology_label),
      kvRow("Health", slot.health),
      kvRow("Temp", formatTemperatureValue(slot, smartEntry)),
      kvRow("Last SMART Test", formatLastSmartTestValue(slot, smartEntry)),
      kvRow("Power On", formatPowerOnValue(smartEntry)),
      kvRow("Sector Size", formatSectorSizeValue(smartEntry)),
      kvRow("Enclosure", slot.enclosure_label || slot.enclosure_name || slot.enclosure_id),
      kvRow("LED", ledStatusLabel(slot)),
      kvRow("Mapping", slot.mapping_source),
      kvRow("Notes", slot.notes),
    ].join("");

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
      return;
    }

    if (!slot.pool_name || !slot.vdev_name) {
      topologyContext.innerHTML = '<div class="warning-item muted">This slot is not currently tied to a pool vdev.</div>';
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
      multipathContext.innerHTML = '<div class="warning-item muted">Select a multipath-backed slot to inspect active and standby member paths.</div>';
      return;
    }

    const multipath = slot.multipath;
    if (!multipath) {
      multipathContext.innerHTML = '<div class="warning-item muted">This slot is not currently presented through gmultipath.</div>';
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
      state.snapshot = snapshot;
      state.selectedSystemId = snapshot.selected_system_id || state.selectedSystemId;
      state.selectedEnclosureId = snapshot.selected_enclosure_id || null;
      if (state.selectedSlot !== null && !getSlotById(state.selectedSlot)) {
        state.selectedSlot = null;
      }
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
      state.snapshot = payload.snapshot;
      state.selectedSystemId = payload.snapshot.selected_system_id || state.selectedSystemId;
      state.selectedEnclosureId = payload.snapshot.selected_enclosure_id || state.selectedEnclosureId;
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
      state.snapshot = result.snapshot;
      state.selectedSystemId = result.snapshot.selected_system_id || state.selectedSystemId;
      state.selectedEnclosureId = result.snapshot.selected_enclosure_id || state.selectedEnclosureId;
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
      state.snapshot = result.snapshot;
      state.selectedSystemId = result.snapshot.selected_system_id || state.selectedSystemId;
      state.selectedEnclosureId = result.snapshot.selected_enclosure_id || state.selectedEnclosureId;
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
      state.snapshot = result.snapshot;
      state.selectedSystemId = result.snapshot.selected_system_id || state.selectedSystemId;
      state.selectedEnclosureId = result.snapshot.selected_enclosure_id || state.selectedEnclosureId;
      if (state.selectedSlot !== null && !getSlotById(state.selectedSlot)) {
        state.selectedSlot = result.snapshot.slots.length ? result.snapshot.slots[0].slot : null;
      }
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
      return;
    }

    state.smartSummaries[cacheKey] = { loading: true };
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
