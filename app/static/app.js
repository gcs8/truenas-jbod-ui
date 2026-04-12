(function () {
  const bootstrap = window.APP_BOOTSTRAP || {};
  const supportedRefreshIntervals = [15, 30, 60, 300];
  const bootstrapRefreshInterval = Number(bootstrap.refreshIntervalSeconds) || 30;
  const state = {
    snapshot: bootstrap.snapshot || { slots: [] },
    layoutRows: bootstrap.layoutRows || [],
    selectedSlot: null,
    search: "",
    autoRefresh: true,
    refreshIntervalSeconds: supportedRefreshIntervals.includes(bootstrapRefreshInterval) ? bootstrapRefreshInterval : 30,
    timerId: null,
  };

  const grid = document.getElementById("slot-grid");
  const detailEmpty = document.getElementById("detail-empty");
  const detailContent = document.getElementById("detail-content");
  const detailSlotTitle = document.getElementById("detail-slot-title");
  const detailStatePill = document.getElementById("detail-state-pill");
  const detailKvGrid = document.getElementById("detail-kv-grid");
  const warningList = document.getElementById("warning-list");
  const searchBox = document.getElementById("search-box");
  const refreshButton = document.getElementById("refresh-button");
  const autoRefreshToggle = document.getElementById("auto-refresh-toggle");
  const refreshIntervalSelect = document.getElementById("refresh-interval-select");
  const lastUpdated = document.getElementById("last-updated");
  const enclosureName = document.getElementById("enclosure-name");
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
  const ledNote = document.getElementById("led-note");
  const ledButtons = Array.from(document.querySelectorAll("[data-led-action]"));

  function getSlotById(slotNumber) {
    return state.snapshot.slots.find((slot) => slot.slot === slotNumber) || null;
  }

  function formatSlotLabel(slotNumber) {
    return String(slotNumber).padStart(2, "0");
  }

  function splitRowIntoGroups(row) {
    if (row.length === 15) {
      return [row.slice(0, 6), row.slice(6, 12), row.slice(12)];
    }
    return [row];
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

  function ledNoteText(slot) {
    if (!slot) {
      return "LED control uses the TrueNAS enclosure API when available, and SSH sesutil locate on shelves that only expose SES fallback data.";
    }
    if (!slot.led_supported) {
      return slot.led_reason || "LED control is unavailable for this slot.";
    }
    if (slot.led_backend === "api") {
      return "LED control is available for this slot through the TrueNAS enclosure API.";
    }
    if (slot.led_backend === "ssh") {
      return "LED control is available for this slot through SSH sesutil locate on the mapped SES controller.";
    }
    return "LED control is available for this slot.";
  }

  function passesFilter(slot) {
    if (!state.search) return true;
    return (slot.search_text || "").includes(state.search);
  }

  function renderGrid() {
    grid.innerHTML = "";
    const slotsByNumber = new Map(state.snapshot.slots.map((slot) => [slot.slot, slot]));

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
          tile.addEventListener("click", () => selectSlot(slot.slot));
          rowGroup.appendChild(tile);
        });

        rowSlots.appendChild(rowGroup);
      });

      rowWrapper.appendChild(rowSlots);
      grid.appendChild(rowWrapper);
    });
  }

  function kvRow(label, value, copyable = false) {
    const safeValue = escapeHtml(value || "n/a");
    const encodedValue = value ? encodeURIComponent(value) : "";
    return `
      <div class="kv-row">
        <div class="kv-label">${escapeHtml(label)}</div>
        <div class="kv-value">${safeValue}</div>
        <div>${copyable && value ? `<button class="copy-button" data-copy="${encodedValue}">Copy</button>` : ""}</div>
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
      return;
    }

    detailEmpty.classList.add("hidden");
    detailContent.classList.remove("hidden");
    detailSlotTitle.textContent = `Slot ${slot.slot_label}`;
    detailStatePill.textContent = stateLabel(slot);
    detailStatePill.className = `state-pill state-${slot.state}`;

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

    mappingForm.serial.value = slot.serial || "";
    mappingForm.device_name.value = slot.device_name || "";
    mappingForm.gptid.value = slot.gptid || "";
    mappingForm.notes.value = slot.notes || "";
    ledButtons.forEach((button) => {
      button.disabled = !slot.led_supported;
    });
    ledNote.textContent = ledNoteText(slot);
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
    enclosureName.textContent =
      state.snapshot.selected_enclosure_label ||
      state.snapshot.selected_enclosure_name ||
      state.snapshot.selected_enclosure_id ||
      "Auto-selected";
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

  function renderAll() {
    renderGrid();
    renderDetail();
    renderWarnings();
    renderStatus();
    renderSummary();
    renderRefreshControls();
  }

  function renderRefreshControls() {
    autoRefreshToggle.checked = state.autoRefresh;
    refreshIntervalSelect.value = String(state.refreshIntervalSeconds);
    refreshIntervalSelect.disabled = !state.autoRefresh;
  }

  function selectSlot(slotNumber) {
    state.selectedSlot = slotNumber;
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

  async function refreshSnapshot(force = false) {
    try {
      setStatus(force ? "Refreshing inventory..." : "Auto-refreshing inventory...");
      const snapshot = await fetchJson(`/api/inventory?force=${force ? "true" : "false"}`);
      state.snapshot = snapshot;
      if (state.selectedSlot === null && snapshot.slots.length) {
        state.selectedSlot = snapshot.slots[0].slot;
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
      const payload = await fetchJson(`/api/slots/${slot.slot}/led`, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      state.snapshot = payload.snapshot;
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
      const result = await fetchJson(`/api/slots/${slot.slot}/mapping`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.snapshot = result.snapshot;
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
      const result = await fetchJson(`/api/slots/${slot.slot}/mapping`, { method: "DELETE" });
      state.snapshot = result.snapshot;
      renderAll();
      setStatus(`Cleared mapping for slot ${slot.slot_label}.`);
    } catch (error) {
      setStatus(`Clear mapping failed: ${error.message || error}`, "error");
    }
  }

  function prefillMapping() {
    const slot = getSlotById(state.selectedSlot);
    if (!slot) return;
    mappingForm.serial.value = slot.serial || "";
    mappingForm.device_name.value = slot.device_name || "";
    mappingForm.gptid.value = slot.gptid || "";
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

  document.querySelectorAll("[data-led-action]").forEach((button) => {
    button.addEventListener("click", () => sendLedAction(button.dataset.ledAction));
  });

  if (state.snapshot.slots.length) {
    state.selectedSlot = state.snapshot.slots[0].slot;
  }
  renderAll();
  resetTimer();
})();
