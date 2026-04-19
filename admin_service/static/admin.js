(function () {
  const bootstrap = window.ADMIN_BOOTSTRAP || {};
  const state = {
    admin: bootstrap.admin || {},
    systems: Array.isArray(bootstrap.systems) ? bootstrap.systems : [],
    defaultSystemId: bootstrap.default_system_id || null,
    profiles: Array.isArray(bootstrap.profiles) ? bootstrap.profiles : [],
    storageViewTemplates: Array.isArray(bootstrap.storage_view_templates) ? bootstrap.storage_view_templates : [],
    platformDefaults: bootstrap.setup_platform_defaults || {},
    sshKeys: Array.isArray(bootstrap.ssh_keys) ? bootstrap.ssh_keys : [],
    runtime: bootstrap.runtime || { available: false, detail: null, containers: [] },
    backupDefaults: bootstrap.backup_defaults || {},
    backupLastPlainPackaging:
      bootstrap.backup_defaults && bootstrap.backup_defaults.packaging !== "7z"
        ? bootstrap.backup_defaults.packaging
        : "tar.zst",
    backupForced7z: false,
    paths: bootstrap.paths || {},
    tlsInspection: null,
    tlsTrustStatus: {
      level: "untrusted",
      label: "Untrusted",
      detail: "No saved TLS certificate bundle has been validated for this host yet.",
    },
    storageViews: [],
    storageViewCandidates: [],
    storageViewCandidatesLoading: false,
    storageViewCandidatesSystemId: null,
    liveEnclosures: [],
    liveEnclosuresLoading: false,
    liveEnclosuresSystemId: null,
    liveEnclosuresError: null,
    selectedStorageViewId: "",
    selectedProfileId: "",
    selectedExistingSystemId:
      (Array.isArray(bootstrap.systems) && bootstrap.systems.find((system) => system.id === bootstrap.default_system_id)?.id)
      || (Array.isArray(bootstrap.systems) && bootstrap.systems[0]?.id)
      || "",
    loadedSystemId: null,
    sshKeysLoading: false,
    refreshInFlight: false,
    countdownTimerId: null,
    sudoersPreviewTimerId: null,
    sudoersPreviewRequestSeq: 0,
  };

  const elements = {
    banner: document.getElementById("admin-status-banner"),
    refreshStateButton: document.getElementById("refresh-state-button"),
    adminOriginLink: document.getElementById("admin-origin-link"),
    countdown: document.getElementById("admin-countdown"),
    startedAt: document.getElementById("admin-started-at"),
    expiresAt: document.getElementById("admin-expires-at"),
    systemCount: document.getElementById("admin-system-count"),
    profileCount: document.getElementById("admin-profile-count"),
    runtimeDetail: document.getElementById("runtime-detail"),
    runtimeCards: document.getElementById("runtime-cards"),
    backupPathList: document.getElementById("backup-path-list"),
    backupPackaging: document.getElementById("backup-packaging"),
    backupEncryptToggle: document.getElementById("backup-encrypt-toggle"),
    backupExportPassphrase: document.getElementById("backup-export-passphrase"),
    backupExportStopToggle: document.getElementById("backup-export-stop-toggle"),
    backupExportRestartToggle: document.getElementById("backup-export-restart-toggle"),
    backupExportButton: document.getElementById("backup-export-button"),
    backupExportResult: document.getElementById("backup-export-result"),
    backupImportFile: document.getElementById("backup-import-file"),
    backupImportPickButton: document.getElementById("backup-import-pick-button"),
    backupImportFileLabel: document.getElementById("backup-import-file-label"),
    backupImportPassphrase: document.getElementById("backup-import-passphrase"),
    backupImportStopToggle: document.getElementById("backup-import-stop-toggle"),
    backupImportRestartToggle: document.getElementById("backup-import-restart-toggle"),
    backupImportButton: document.getElementById("backup-import-button"),
    backupImportResult: document.getElementById("backup-import-result"),
    setupSystemLabel: document.getElementById("setup-system-label"),
    setupSystemId: document.getElementById("setup-system-id"),
    setupPlatform: document.getElementById("setup-platform"),
    setupProfile: document.getElementById("setup-profile"),
    setupMakeDefault: document.getElementById("setup-make-default"),
    setupPlatformHelp: document.getElementById("setup-platform-help"),
    setupTruenasHost: document.getElementById("setup-truenas-host"),
    setupVerifySsl: document.getElementById("setup-verify-ssl"),
    setupVerifySslHelp: document.getElementById("setup-verify-ssl-help"),
    setupTlsCaBundlePath: document.getElementById("setup-tls-ca-bundle-path"),
    setupTlsServerName: document.getElementById("setup-tls-server-name"),
    setupTlsServerNameHelp: document.getElementById("setup-tls-server-name-help"),
    setupTlsServerNameSuggestions: document.getElementById("setup-tls-server-name-suggestions"),
    setupEnclosureFilter: document.getElementById("setup-enclosure-filter"),
    setupApiKey: document.getElementById("setup-api-key"),
    setupApiUser: document.getElementById("setup-api-user"),
    setupApiPassword: document.getElementById("setup-api-password"),
    setupInspectTlsButton: document.getElementById("setup-inspect-tls-button"),
    setupTrustRemoteTlsButton: document.getElementById("setup-trust-remote-tls-button"),
    setupTlsTrustPill: document.getElementById("setup-tls-trust-pill"),
    setupTlsTrustDetail: document.getElementById("setup-tls-trust-detail"),
    setupTlsInspectionMeta: document.getElementById("setup-tls-inspection-meta"),
    setupTlsInspectionResult: document.getElementById("setup-tls-inspection-result"),
    setupTlsLeafDetails: document.getElementById("setup-tls-leaf-details"),
    setupTlsCaFile: document.getElementById("setup-tls-ca-file"),
    setupTlsCaPickButton: document.getElementById("setup-tls-ca-pick-button"),
    setupTlsCaFileLabel: document.getElementById("setup-tls-ca-file-label"),
    setupTlsImportCaButton: document.getElementById("setup-tls-import-ca-button"),
    setupTlsImportResult: document.getElementById("setup-tls-import-result"),
    setupSshEnabled: document.getElementById("setup-ssh-enabled"),
    setupSshHost: document.getElementById("setup-ssh-host"),
    setupSshUser: document.getElementById("setup-ssh-user"),
    setupSshPort: document.getElementById("setup-ssh-port"),
    setupSshKeyMode: document.getElementById("setup-ssh-key-mode"),
    setupSshKeyPath: document.getElementById("setup-ssh-key-path"),
    setupSshExistingKey: document.getElementById("setup-ssh-existing-key"),
    setupRefreshKeysButton: document.getElementById("setup-refresh-keys-button"),
    setupGenerateKeyName: document.getElementById("setup-generate-key-name"),
    setupGenerateKeyButton: document.getElementById("setup-generate-key-button"),
    setupReuseKeyPanel: document.getElementById("setup-reuse-key-panel"),
    setupGenerateKeyPanel: document.getElementById("setup-generate-key-panel"),
    setupManualKeyPanel: document.getElementById("setup-manual-key-panel"),
    setupSshPassword: document.getElementById("setup-ssh-password"),
    setupSshSudoPassword: document.getElementById("setup-ssh-sudo-password"),
    setupSshKnownHosts: document.getElementById("setup-ssh-known-hosts"),
    setupSshStrictHostKey: document.getElementById("setup-ssh-strict-host-key"),
    setupBootstrapEnabled: document.getElementById("setup-bootstrap-enabled"),
    setupBootstrapFields: document.getElementById("setup-bootstrap-fields"),
    setupBootstrapHost: document.getElementById("setup-bootstrap-host"),
    setupBootstrapUser: document.getElementById("setup-bootstrap-user"),
    setupBootstrapPassword: document.getElementById("setup-bootstrap-password"),
    setupBootstrapKeyPath: document.getElementById("setup-bootstrap-key-path"),
    setupBootstrapSudoPassword: document.getElementById("setup-bootstrap-sudo-password"),
    setupBootstrapInstallSudo: document.getElementById("setup-bootstrap-install-sudo"),
    setupBootstrapButton: document.getElementById("setup-bootstrap-button"),
    setupBootstrapResult: document.getElementById("setup-bootstrap-result"),
    setupBootstrapSudoersName: document.getElementById("setup-bootstrap-sudoers-name"),
    setupBootstrapSudoersDetail: document.getElementById("setup-bootstrap-sudoers-detail"),
    setupBootstrapSudoersPreview: document.getElementById("setup-bootstrap-sudoers-preview"),
    setupSshCommands: document.getElementById("setup-ssh-commands"),
    setupLoadRecommendedButton: document.getElementById("setup-load-recommended-button"),
    setupStorageViewTemplate: document.getElementById("setup-storage-view-template"),
    setupStorageViewAddButton: document.getElementById("setup-storage-view-add-button"),
    setupStorageViewHelp: document.getElementById("setup-storage-view-help"),
    setupStorageViewCount: document.getElementById("setup-storage-view-count"),
    setupStorageViewList: document.getElementById("setup-storage-view-list"),
    setupStorageViewTemplateBadge: document.getElementById("setup-storage-view-template-badge"),
    setupStorageViewEmpty: document.getElementById("setup-storage-view-empty"),
    setupStorageViewEditor: document.getElementById("setup-storage-view-editor"),
    setupStorageViewLabel: document.getElementById("setup-storage-view-label"),
    setupStorageViewId: document.getElementById("setup-storage-view-id"),
    setupStorageViewTemplateSelect: document.getElementById("setup-storage-view-template-select"),
    setupStorageViewProfile: document.getElementById("setup-storage-view-profile"),
    setupStorageViewBindingMode: document.getElementById("setup-storage-view-binding-mode"),
    setupStorageViewOrder: document.getElementById("setup-storage-view-order"),
    setupStorageViewEnabled: document.getElementById("setup-storage-view-enabled"),
    setupStorageViewShowMain: document.getElementById("setup-storage-view-show-main"),
    setupStorageViewShowAdmin: document.getElementById("setup-storage-view-show-admin"),
    setupStorageViewCollapsed: document.getElementById("setup-storage-view-collapsed"),
    setupStorageViewEnclosureIds: document.getElementById("setup-storage-view-enclosure-ids"),
    setupStorageViewPoolNames: document.getElementById("setup-storage-view-pool-names"),
    setupStorageViewSerials: document.getElementById("setup-storage-view-serials"),
    setupStorageViewPcieAddresses: document.getElementById("setup-storage-view-pcie-addresses"),
    setupStorageViewDeviceNames: document.getElementById("setup-storage-view-device-names"),
    setupStorageViewSlotLabels: document.getElementById("setup-storage-view-slot-labels"),
    setupStorageViewSlotSizes: document.getElementById("setup-storage-view-slot-sizes"),
    setupStorageViewCandidatesRefreshButton: document.getElementById("setup-storage-view-candidates-refresh-button"),
    setupStorageViewCandidatesAddAllButton: document.getElementById("setup-storage-view-candidates-add-all-button"),
    setupStorageViewCandidatesHelp: document.getElementById("setup-storage-view-candidates-help"),
    setupStorageViewCandidatesList: document.getElementById("setup-storage-view-candidates-list"),
    setupStorageViewMoveUpButton: document.getElementById("setup-storage-view-move-up-button"),
    setupStorageViewMoveDownButton: document.getElementById("setup-storage-view-move-down-button"),
    setupStorageViewDuplicateButton: document.getElementById("setup-storage-view-duplicate-button"),
    setupStorageViewRemoveButton: document.getElementById("setup-storage-view-remove-button"),
    setupStorageViewEditorHelp: document.getElementById("setup-storage-view-editor-help"),
    setupStorageViewKindBadge: document.getElementById("setup-storage-view-kind-badge"),
    setupStorageViewPreviewSummary: document.getElementById("setup-storage-view-preview-summary"),
    setupStorageViewPreviewGrid: document.getElementById("setup-storage-view-preview-grid"),
    setupStorageViewPreviewMeta: document.getElementById("setup-storage-view-preview-meta"),
    setupCreateButton: document.getElementById("setup-create-button"),
    setupResult: document.getElementById("setup-result"),
    existingSystemSelect: document.getElementById("existing-system-select"),
    existingSystemLoadButton: document.getElementById("existing-system-load-button"),
    existingSystemResetButton: document.getElementById("existing-system-reset-button"),
    existingSystemHelp: document.getElementById("existing-system-help"),
    existingSystemSummary: document.getElementById("existing-system-summary"),
    currentDefaultSystem: document.getElementById("current-default-system"),
    currentSystemsList: document.getElementById("current-systems-list"),
    profilePreviewBadge: document.getElementById("profile-preview-badge"),
    profilePreviewSummary: document.getElementById("profile-preview-summary"),
    profilePreviewGrid: document.getElementById("profile-preview-grid"),
    profilePreviewMeta: document.getElementById("profile-preview-meta"),
    profileCatalogCount: document.getElementById("profile-catalog-count"),
    profileCatalog: document.getElementById("profile-catalog"),
    setupSshKeyHelp: document.getElementById("setup-ssh-key-help"),
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function setBanner(message, tone = "info") {
    if (!elements.banner) {
      return;
    }
    elements.banner.textContent = message;
    elements.banner.classList.remove("is-error", "is-success");
    if (tone === "error") {
      elements.banner.classList.add("is-error");
    } else if (tone === "success") {
      elements.banner.classList.add("is-success");
    }
  }

  function defaultTlsTrustDetail({
    bundlePath = "",
    host = "",
    serverName = "",
    verifySsl = Boolean(elements.setupVerifySsl?.checked),
  } = {}) {
    const normalizedBundlePath = String(bundlePath || "").trim();
    const normalizedHost = String(host || collectTlsTargetHost() || "").trim();
    const normalizedServerName = String(serverName || collectTlsServerName() || "").trim();
    if (!verifySsl) {
      return "TLS verification is off for this saved connection, so the saved remote certificate is not being enforced yet.";
    }
    if (normalizedBundlePath && normalizedHost && normalizedServerName) {
      return `Saved bundle ${normalizedBundlePath} has not been validated for ${normalizedHost} while verifying the certificate as ${normalizedServerName} yet.`;
    }
    if (normalizedBundlePath && normalizedHost) {
      return `Saved bundle ${normalizedBundlePath} has not been validated against ${normalizedHost} yet.`;
    }
    if (normalizedBundlePath) {
      return `Saved bundle ${normalizedBundlePath} has not been validated for this host yet.`;
    }
    return "No saved TLS certificate bundle has been validated for this host yet.";
  }

  function buildTlsValidationSuggestion(validation = null) {
    const detail = String(validation?.detail || "").trim();
    if (!detail) {
      return "";
    }
    if (!/mismatch|not valid for/i.test(detail)) {
      return detail;
    }
    if (collectTlsServerName()) {
      return detail;
    }
    const suggestions = suggestedTlsServerNames();
    if (!suggestions.length) {
      return detail;
    }
    if (suggestions.length === 1) {
      return `${detail} Try TLS Verify Hostname: ${suggestions[0]}.`;
    }
    return `${detail} Try TLS Verify Hostname with one of the inspected DNS names: ${suggestions.join(", ")}.`;
  }

  function setTlsTrustStatus({
    level = "untrusted",
    label = level === "trusted" ? "Trusted" : "Untrusted",
    detail = "",
  } = {}) {
    state.tlsTrustStatus = { level, label, detail };
    renderTlsTrustStatus();
  }

  function syncTlsTrustStatus(validation = null) {
    const bundlePath = elements.setupTlsCaBundlePath?.value?.trim() || "";
    const host = collectTlsTargetHost() || "";
    const serverName = collectTlsServerName() || "";
    if (validation && validation.validated) {
      setTlsTrustStatus({
        level: "trusted",
        label: "Trusted",
        detail:
          validation.detail
          || `Verified TLS certificate checks for ${validation.host || host || "this host"} using ${validation.bundle_path || bundlePath || "the saved bundle"}.`,
      });
      return true;
    }

    setTlsTrustStatus({
      level: "untrusted",
      label: "Untrusted",
      detail:
        buildTlsValidationSuggestion(validation)
        || defaultTlsTrustDetail({
          bundlePath: validation?.bundle_path || bundlePath,
          host: validation?.host || host,
          serverName: validation?.server_hostname || serverName,
        }),
    });
    return false;
  }

  function formatLocalTimestamp(value) {
    if (!value) {
      return "-";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    return date.toLocaleString();
  }

  function renderTlsTrustStatus() {
    if (!elements.setupTlsTrustPill || !elements.setupTlsTrustDetail) {
      return;
    }
    const trustStatus = state.tlsTrustStatus || {};
    const level = trustStatus.level === "trusted" ? "trusted" : "untrusted";
    const label = trustStatus.label || (level === "trusted" ? "Trusted" : "Untrusted");

    elements.setupTlsTrustPill.textContent = label;
    elements.setupTlsTrustPill.className = `tls-trust-pill is-${level}`;
    elements.setupTlsTrustDetail.textContent = trustStatus.detail || defaultTlsTrustDetail();
  }

  function formatCountdown() {
    if (!state.admin.expires_at) {
      return "No auto-stop";
    }
    const expiresAt = new Date(state.admin.expires_at).getTime();
    const remainingMs = expiresAt - Date.now();
    if (remainingMs <= 0) {
      return "Stopping now";
    }
    const totalSeconds = Math.floor(remainingMs / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    if (hours > 0) {
      return `${hours}h ${String(minutes).padStart(2, "0")}m ${String(seconds).padStart(2, "0")}s`;
    }
    return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  }

  function startCountdownTimer() {
    if (state.countdownTimerId) {
      window.clearInterval(state.countdownTimerId);
    }
    updateAdminMeta();
    state.countdownTimerId = window.setInterval(updateAdminMeta, 1000);
  }

  function updateAdminMeta() {
    if (elements.countdown) {
      elements.countdown.textContent = formatCountdown();
    }
    if (elements.startedAt) {
      elements.startedAt.textContent = formatLocalTimestamp(state.admin.started_at);
    }
    if (elements.expiresAt) {
      elements.expiresAt.textContent = state.admin.expires_at ? formatLocalTimestamp(state.admin.expires_at) : "Manual stop only";
    }
    if (elements.systemCount) {
      elements.systemCount.textContent = String(state.systems.length);
    }
    if (elements.profileCount) {
      elements.profileCount.textContent = String(state.profiles.length);
    }
    if (elements.adminOriginLink) {
      const origin = String(state.admin.public_origin || "").trim();
      elements.adminOriginLink.href = origin || window.location.href;
      elements.adminOriginLink.classList.toggle("hidden", !origin);
    }
  }

  function renderBackupPaths() {
    if (!elements.backupPathList) {
      return;
    }
    const items = [
      ["Config", state.paths.config_file],
      ["Profiles", state.paths.profile_file],
      ["Mappings", state.paths.mapping_file],
      ["Slot Cache", state.paths.slot_detail_cache_file],
      ["History DB", state.paths.history_db],
    ].filter(([, value]) => Boolean(value));
    elements.backupPathList.innerHTML = items
      .map(([label, value]) => `<li><strong>${escapeHtml(label)}:</strong> ${escapeHtml(value)}</li>`)
      .join("");
  }

  function runtimeStatusClass(container) {
    const lifecycleState = String(container.lifecycle_state || "").toLowerCase();
    if (lifecycleState === "normal") {
      return "is-normal";
    }
    if (lifecycleState === "needs_restart") {
      return "is-needs-restart";
    }
    if (lifecycleState === "down") {
      return "is-down";
    }
    const status = String(container.status || "").toLowerCase();
    if (status === "unavailable") {
      return "is-missing";
    }
    return "is-error";
  }

  function renderRuntimeCards() {
    if (!elements.runtimeCards || !elements.runtimeDetail) {
      return;
    }
    const runtime = state.runtime || {};
    elements.runtimeDetail.textContent = runtime.available
      ? "Runtime control is available through the mounted Docker socket."
      : String(runtime.detail || "Runtime control is unavailable in this session.");
    const containers = Array.isArray(runtime.containers) ? runtime.containers : [];
    elements.runtimeCards.innerHTML = containers
      .map((container) => {
        const actions = [];
        if (container.can_stop) {
          actions.push(
            `<button class="button secondary small" type="button" data-runtime-action="stop" data-container-key="${escapeHtml(container.key)}">Stop</button>`
          );
        }
        if (container.can_restart) {
          actions.push(
            `<button class="button secondary small" type="button" data-runtime-action="restart" data-container-key="${escapeHtml(container.key)}">Restart</button>`
          );
        }
        if (container.can_start) {
          actions.push(
            `<button class="button small" type="button" data-runtime-action="start" data-container-key="${escapeHtml(container.key)}">Start</button>`
          );
        }
        const health = container.health ? ` / ${escapeHtml(container.health)}` : "";
        const lifecycleLabel = String(container.lifecycle_label || container.status_text || container.status || "unknown");
        return `
          <article class="runtime-card">
            <div class="runtime-card-header">
              <div>
                <h3 class="runtime-card-title">${escapeHtml(container.label || container.name || container.key)}</h3>
                <p class="runtime-card-copy">${escapeHtml(container.description || "")}</p>
              </div>
              <span class="runtime-card-status ${runtimeStatusClass(container)}">
                ${escapeHtml(lifecycleLabel)}${health}
              </span>
            </div>
            <div class="runtime-card-copy"><code>${escapeHtml(container.name || container.key)}</code></div>
            <div class="subtle">${escapeHtml(container.status_text || "No additional runtime detail is available.")}</div>
            <div class="button-row">${actions.join("") || '<span class="subtle">No action available from this state.</span>'}</div>
          </article>
        `;
      })
      .join("");
  }

  function getSystemById(systemId) {
    return state.systems.find((system) => system.id === systemId) || null;
  }

  function ensureExistingSystemSelection() {
    const availableIds = state.systems.map((system) => system.id);
    if (state.selectedExistingSystemId && availableIds.includes(state.selectedExistingSystemId)) {
      return;
    }
    if (state.loadedSystemId && availableIds.includes(state.loadedSystemId)) {
      state.selectedExistingSystemId = state.loadedSystemId;
      return;
    }
    if (state.defaultSystemId && availableIds.includes(state.defaultSystemId)) {
      state.selectedExistingSystemId = state.defaultSystemId;
      return;
    }
    state.selectedExistingSystemId = availableIds[0] || "";
  }

  function isEditingLoadedSystem() {
    const currentId = elements.setupSystemId?.value?.trim() || "";
    return Boolean(state.loadedSystemId && currentId === state.loadedSystemId);
  }

  function updateCreateButton() {
    if (!elements.setupCreateButton) {
      return;
    }
    elements.setupCreateButton.textContent = isEditingLoadedSystem()
      ? "Save System Changes"
      : "Create System Entry";
  }

  function suggestedTlsBundleName() {
    const systemId = elements.setupSystemId?.value?.trim();
    if (systemId) {
      return systemId;
    }
    const systemLabel = elements.setupSystemLabel?.value?.trim();
    if (systemLabel) {
      return systemLabel;
    }
    return suggestedConnectionHost() || "trusted-remote";
  }

  function renderTlsInspection() {
    if (!elements.setupTlsInspectionMeta || !elements.setupTlsLeafDetails || !elements.setupTlsInspectionResult) {
      return;
    }
    const inspection = state.tlsInspection;
    if (!inspection || !inspection.leaf) {
      elements.setupTlsInspectionMeta.innerHTML = "";
      elements.setupTlsLeafDetails.textContent = "No certificate inspected yet.";
      return;
    }

    const leaf = inspection.leaf;
    const metaChips = [
      inspection.host ? `Target: ${inspection.host}` : null,
      inspection.connect_host && inspection.connect_host !== inspection.server_hostname ? `Connect: ${inspection.connect_host}` : null,
      inspection.server_hostname && inspection.server_hostname !== inspection.connect_host ? `TLS verify as: ${inspection.server_hostname}` : null,
      leaf.is_ca ? "Leaf marked CA" : "Leaf marked end-entity",
      inspection.certificate_count ? `Chain length: ${inspection.certificate_count}` : null,
    ].filter(Boolean);
    const formatWrappedValue = (value, separatorPattern = /,\s*/g) => {
      const text = String(value || "").trim();
      if (!text) {
        return '<span class="tls-value-block">-</span>';
      }
      const parts = text.split(separatorPattern).map((item) => item.trim()).filter(Boolean);
      if (!parts.length) {
        return `<span class="tls-value-block">${escapeHtml(text)}</span>`;
      }
      return `<span class="tls-value-block">${parts.map((item) => escapeHtml(item)).join("<wbr>, ")}</span>`;
    };
    const formatFingerprintValue = (value) => {
      const text = String(value || "").trim();
      if (!text) {
        return '<code class="tls-fingerprint">-</code>';
      }
      if (text.includes(":")) {
        return `<code class="tls-fingerprint">${text.split(":").map((item) => escapeHtml(item)).join(":<wbr>")}</code>`;
      }
      const chunks = text.match(/.{1,8}/g) || [text];
      return `<code class="tls-fingerprint">${chunks.map((item) => escapeHtml(item)).join("<wbr>")}</code>`;
    };
    const formatListValue = (items) => {
      const values = Array.isArray(items) ? items.filter(Boolean) : [];
      if (!values.length) {
        return '<span class="tls-value-block">-</span>';
      }
      return `<span class="tls-value-block">${values.map((item) => escapeHtml(String(item))).join("<br>")}</span>`;
    };
    elements.setupTlsInspectionMeta.innerHTML = metaChips
      .map((item) => `<span class="meta-chip">${escapeHtml(item)}</span>`)
      .join("");
    elements.setupTlsLeafDetails.innerHTML = [
      `<div><strong>Subject</strong>${formatWrappedValue(leaf.subject)}</div>`,
      `<div><strong>Issuer</strong>${formatWrappedValue(leaf.issuer)}</div>`,
      `<div><strong>SHA-256</strong>${formatFingerprintValue(leaf.sha256_fingerprint)}</div>`,
      `<div><strong>SHA-1</strong>${formatFingerprintValue(leaf.sha1_fingerprint)}</div>`,
      `<div><strong>SPKI SHA-256</strong>${formatFingerprintValue(leaf.spki_sha256)}</div>`,
      `<div><strong>Valid From</strong><span class="tls-value-block">${escapeHtml(leaf.not_valid_before || "-")}</span></div>`,
      `<div><strong>Valid Until</strong><span class="tls-value-block">${escapeHtml(leaf.not_valid_after || "-")}</span></div>`,
      `<div><strong>SAN DNS</strong>${formatListValue(leaf.san_dns)}</div>`,
      `<div><strong>SAN IP</strong>${formatListValue(leaf.san_ip)}</div>`,
    ].join("");
  }

  function syncTlsServerNameHelp() {
    if (!elements.setupTlsServerNameHelp) {
      return;
    }
    const host = collectTlsTargetHost();
    const serverName = collectTlsServerName();
    const suggestions = suggestedTlsServerNames();
    if (serverName) {
      elements.setupTlsServerNameHelp.textContent = host
        ? `Connections will still go to ${host}, but certificate validation and SNI will use ${serverName}.`
        : `Certificate validation and SNI will use ${serverName} when this system connects over TLS.`;
      return;
    }
    if (suggestions.length) {
      elements.setupTlsServerNameHelp.textContent = `Use this when the host stays on an IP or alternate address but the certificate is issued to a DNS name. Inspected DNS names: ${suggestions.join(", ")}.`;
      return;
    }
    elements.setupTlsServerNameHelp.textContent = "Use this when you connect to an IP or alternate address, but the certificate is issued to a DNS name.";
  }

  function renderTlsServerNameSuggestions() {
    if (!elements.setupTlsServerNameSuggestions) {
      return;
    }
    const suggestions = suggestedTlsServerNames();
    if (!suggestions.length) {
      elements.setupTlsServerNameSuggestions.innerHTML = "";
      return;
    }
    const activeValue = collectTlsServerName();
    const chips = suggestions.map((value) => {
      const selected = activeValue && value.toLowerCase() === activeValue.toLowerCase();
      return `<button class="button secondary small tls-server-name-chip" type="button" data-tls-server-name="${escapeHtml(value)}">${selected ? "Using" : "Use"} ${escapeHtml(value)}</button>`;
    });
    elements.setupTlsServerNameSuggestions.innerHTML = [
      '<span class="tls-server-name-suggestion-label">Inspected names:</span>',
      ...chips,
    ].join("");
  }

  function syncVerifySslHelp() {
    if (!elements.setupVerifySslHelp) {
      return;
    }
    const customBundlePath = elements.setupTlsCaBundlePath?.value?.trim();
    const serverName = collectTlsServerName();
    if (elements.setupVerifySsl?.checked) {
      elements.setupVerifySslHelp.textContent = customBundlePath
        ? `Uses normal CA and hostname validation with the system trust store plus ${customBundlePath}.${serverName ? ` Certificate validation and SNI will use ${serverName}.` : ""} Public CAs still work, and the extra PEM bundle lets you trust a private CA or the presented remote certificate material.`
        : `Uses normal CA and hostname validation from the sidecar trust store.${serverName ? ` Certificate validation and SNI will use ${serverName}.` : ""} Public CAs work as-is, and you can inspect and import a private CA or presented remote certificate material below when you need extra trust anchors.`;
      return;
    }
    elements.setupVerifySslHelp.textContent = "TLS certificate and hostname checks are disabled for this saved connection. Use this only when you intentionally want to trust the target without CA validation.";
  }

  function renderExistingSystems() {
    if (!elements.currentSystemsList || !elements.currentDefaultSystem) {
      return;
    }
    ensureExistingSystemSelection();
    elements.currentDefaultSystem.textContent = state.defaultSystemId ? `Default: ${state.defaultSystemId}` : "No default";

    if (elements.existingSystemSelect) {
      elements.existingSystemSelect.innerHTML = state.systems.length
        ? state.systems
            .map((system) => `<option value="${escapeHtml(system.id)}">${escapeHtml(system.label || system.id)}</option>`)
            .join("")
        : '<option value="">No systems saved yet</option>';
      elements.existingSystemSelect.value = state.selectedExistingSystemId || "";
      elements.existingSystemSelect.disabled = !state.systems.length;
    }

    const selectedSystem = getSystemById(state.selectedExistingSystemId);
    if (elements.existingSystemLoadButton) {
      elements.existingSystemLoadButton.disabled = !selectedSystem;
    }
    if (elements.existingSystemResetButton) {
      elements.existingSystemResetButton.disabled = !state.loadedSystemId;
    }
    if (elements.existingSystemHelp) {
      if (!selectedSystem) {
        elements.existingSystemHelp.textContent = "No saved systems yet. This walkthrough will create the first one.";
      } else if (isEditingLoadedSystem()) {
        elements.existingSystemHelp.textContent = `Editing ${selectedSystem.label || selectedSystem.id}. Save with the same system id to update it in place, or change the id to make a copy.`;
      } else {
        elements.existingSystemHelp.textContent = `Load ${selectedSystem.label || selectedSystem.id} into the form to revise it, compare settings, or clone it into a new system id.`;
      }
    }
    if (elements.existingSystemSummary) {
      const summaryChips = selectedSystem
        ? [
            selectedSystem.platform ? `Platform: ${selectedSystem.platform}` : null,
            selectedSystem.truenas_host ? `Host: ${selectedSystem.truenas_host}` : null,
            selectedSystem.default_profile_id ? `Profile: ${selectedSystem.default_profile_id}` : "Profile: auto",
            `${Array.isArray(selectedSystem.storage_views) ? selectedSystem.storage_views.length : 0} storage views`,
            selectedSystem.verify_ssl ? "TLS verify on" : "TLS verify off",
            selectedSystem.tls_server_name ? `Verify as: ${selectedSystem.tls_server_name}` : null,
            selectedSystem.tls_ca_bundle_path ? "Custom TLS trust bundle" : "System/public trust only",
            selectedSystem.ssh_enabled ? "SSH enabled" : "SSH optional",
          ].filter(Boolean)
        : [];
      elements.existingSystemSummary.innerHTML = summaryChips
        .map((item) => `<span class="meta-chip">${escapeHtml(item)}</span>`)
        .join("");
    }
    elements.currentSystemsList.innerHTML = state.systems.length
      ? state.systems
          .map((system) => {
            const extra = [
              system.platform,
              `${Array.isArray(system.storage_views) ? system.storage_views.length : 0} views`,
              system.ssh_enabled ? "SSH enabled" : "SSH optional",
            ].filter(Boolean).join(" / ");
            const classes = ["pill"];
            if (system.id === state.defaultSystemId) {
              classes.push("is-default");
            }
            if (system.id === state.selectedExistingSystemId) {
              classes.push("is-selected");
            }
            return `<button class="${classes.join(" ")}" type="button" data-existing-system-id="${escapeHtml(system.id)}"><strong>${escapeHtml(system.label || system.id)}</strong> ${escapeHtml(extra)}</button>`;
          })
          .join("")
      : '<div class="pill">No systems configured yet.</div>';
  }

  function renderProfileOptions() {
    if (!elements.setupProfile) {
      return;
    }
    const selectedValue = elements.setupProfile.value || state.selectedProfileId || "";
    const options = ['<option value="">Auto-select from platform</option>'].concat(
      state.profiles.map((profile) => `<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.label)}</option>`)
    );
    elements.setupProfile.innerHTML = options.join("");
    if (selectedValue && state.profiles.some((profile) => profile.id === selectedValue)) {
      elements.setupProfile.value = selectedValue;
      state.selectedProfileId = selectedValue;
    } else {
      elements.setupProfile.value = "";
      state.selectedProfileId = "";
    }
  }

  function currentPinnedProfileId() {
    return elements.setupProfile?.value || state.selectedProfileId || "";
  }

  function currentPinnedProfile() {
    const explicitId = currentPinnedProfileId();
    if (!explicitId) {
      return null;
    }
    return state.profiles.find((profile) => profile.id === explicitId) || null;
  }

  function getProfileById(profileId) {
    if (!profileId) {
      return null;
    }
    return state.profiles.find((profile) => profile.id === profileId) || null;
  }

  function orderedStorageViewProfiles() {
    const pinnedId = currentPinnedProfileId();
    const orderedIds = [];
    if (pinnedId) {
      orderedIds.push(pinnedId);
    }
    state.profiles
      .filter((profile) => String(profile.id || "").startsWith("generic-"))
      .forEach((profile) => {
        if (!orderedIds.includes(profile.id)) {
          orderedIds.push(profile.id);
        }
      });
    state.profiles.forEach((profile) => {
      if (!orderedIds.includes(profile.id)) {
        orderedIds.push(profile.id);
      }
    });
    return orderedIds
      .map((profileId) => getProfileById(profileId))
      .filter(Boolean);
  }

  function currentLiveEnclosures() {
    const systemId = currentStorageViewSystemId();
    if (!systemId || state.liveEnclosuresSystemId !== systemId) {
      return [];
    }
    return Array.isArray(state.liveEnclosures) ? state.liveEnclosures : [];
  }

  function liveEnclosureMatchesForProfile(profileId) {
    const normalizedProfileId = String(profileId || "");
    if (!normalizedProfileId) {
      return [];
    }
    return currentLiveEnclosures().filter(
      (enclosure) => String(enclosure?.profile_id || "") === normalizedProfileId
    );
  }

  function addableStorageViewProfiles() {
    return orderedStorageViewProfiles().filter(
      (profile) => liveEnclosureMatchesForProfile(profile?.id).length === 0
    );
  }

  function hiddenLiveChassisProfiles() {
    return orderedStorageViewProfiles().filter(
      (profile) => liveEnclosureMatchesForProfile(profile?.id).length > 0
    );
  }

  function previewProfile() {
    const pinnedProfile = currentPinnedProfile();
    if (pinnedProfile) {
      return pinnedProfile;
    }
    return state.profiles[0] || null;
  }

  function buildProfileRows(profile) {
    if (Array.isArray(profile.slot_layout) && profile.slot_layout.length) {
      return profile.slot_layout;
    }
    const rows = Math.max(1, Number(profile.rows) || 1);
    const columns = Math.max(1, Number(profile.columns) || 1);
    let slotNumber = 0;
    return Array.from({ length: rows }, () =>
      Array.from({ length: columns }, () => slotNumber++)
    );
  }

  function renderProfilePreview() {
    const profile = previewProfile();
    if (!elements.profilePreviewSummary || !elements.profilePreviewGrid || !elements.profilePreviewMeta || !elements.profilePreviewBadge) {
      return;
    }
    if (!profile) {
      elements.profilePreviewBadge.textContent = "No profiles";
      elements.profilePreviewSummary.textContent = "No enclosure profiles are loaded right now.";
      elements.profilePreviewGrid.innerHTML = "";
      elements.profilePreviewMeta.innerHTML = "";
      return;
    }

    const previewRows = buildProfileRows(profile);
    const columnCount = Math.max(
      1,
      ...previewRows.map((row) => (Array.isArray(row) ? row.length : 0))
    );
    elements.profilePreviewBadge.textContent = elements.setupProfile?.value ? "Pinned Profile" : "Auto Preview";
    elements.profilePreviewSummary.textContent = profile.summary
      || "Profile preview for the currently selected enclosure layout.";
    elements.profilePreviewGrid.style.gridTemplateColumns = `repeat(${columnCount}, minmax(0, 1fr))`;
    elements.profilePreviewGrid.innerHTML = previewRows
      .flat()
      .map((slotValue) => {
        if (slotValue === null || slotValue === undefined) {
          return '<div class="profile-preview-cell is-gap">Gap</div>';
        }
        return `<div class="profile-preview-cell">${escapeHtml(String(slotValue).padStart(2, "0"))}</div>`;
      })
      .join("");
    const slotCount = Number(profile.slot_count) || previewRows.flat().filter((value) => Number.isInteger(value)).length;
    const chips = [
      `${profile.rows} rows`,
      `${profile.columns} columns`,
      `${slotCount} visible bays`,
      profile.bay_size ? `${profile.bay_size}" media` : null,
      profile.face_style ? profile.face_style : null,
    ].filter(Boolean);
    elements.profilePreviewMeta.innerHTML = chips
      .map((item) => `<span class="meta-chip">${escapeHtml(item)}</span>`)
      .join("");
  }

  function renderProfileCatalog() {
    if (!elements.profileCatalog || !elements.profileCatalogCount) {
      return;
    }
    const activeId = previewProfile()?.id || "";
    elements.profileCatalogCount.textContent = `${state.profiles.length} profiles`;
    elements.profileCatalog.innerHTML = state.profiles
      .map((profile) => {
        const classes = ["profile-card"];
        if (profile.id === activeId) {
          classes.push("is-selected");
        }
        const meta = [
          `${profile.rows}x${profile.columns}`,
          `${profile.slot_count || 0} bays`,
          profile.bay_size ? `${profile.bay_size}"` : null,
        ].filter(Boolean);
        return `
          <article class="${classes.join(" ")}" data-profile-id="${escapeHtml(profile.id)}">
            <div class="profile-card-header">
              <div>
                <h3>${escapeHtml(profile.label)}</h3>
                <p>${escapeHtml(profile.summary || "Reusable enclosure layout profile.")}</p>
              </div>
              <span class="badge">${escapeHtml(profile.id)}</span>
            </div>
            <div class="profile-meta">
              ${meta.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
            </div>
          </article>
        `;
      })
      .join("");
  }

  function slugify(value, fallback = "item") {
    const normalized = String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
    return normalized || fallback;
  }

  function splitDelimitedLines(value) {
    const seen = new Set();
    return String(value || "")
      .split(/[\r\n,]+/)
      .map((item) => item.trim())
      .filter((item) => {
        if (!item || seen.has(item)) {
          return false;
        }
        seen.add(item);
        return true;
      });
  }

  function serializeDelimitedLines(values) {
    return Array.isArray(values) ? values.filter(Boolean).join("\n") : "";
  }

  function normalizeSlotLabelMap(rawMap) {
    const source = rawMap && typeof rawMap === "object" ? rawMap : {};
    const normalized = {};
    Object.entries(source).forEach(([rawKey, rawValue]) => {
      const slotNumber = Number.parseInt(rawKey, 10);
      const label = String(rawValue || "").trim();
      if (!Number.isNaN(slotNumber) && slotNumber >= 0 && label) {
        normalized[slotNumber] = label;
      }
    });
    return normalized;
  }

  function normalizeSlotSizeMap(rawMap) {
    const source = rawMap && typeof rawMap === "object" ? rawMap : {};
    const normalized = {};
    const allowedSizes = new Set(["2230", "2242", "2260", "2280", "22110"]);
    Object.entries(source).forEach(([rawKey, rawValue]) => {
      const slotNumber = Number.parseInt(rawKey, 10);
      const sizeLabel = String(rawValue || "").trim();
      if (!Number.isNaN(slotNumber) && slotNumber >= 0 && allowedSizes.has(sizeLabel)) {
        normalized[slotNumber] = sizeLabel;
      }
    });
    return normalized;
  }

  function parseSlotLabelsText(value) {
    const parsed = {};
    String(value || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((line) => {
        const match = line.match(/^(\d+)\s*[:=]\s*(.+)$/);
        if (!match) {
          return;
        }
        const slotNumber = Number.parseInt(match[1], 10);
        const label = String(match[2] || "").trim();
        if (!Number.isNaN(slotNumber) && slotNumber >= 0 && label) {
          parsed[slotNumber] = label;
        }
      });
    return parsed;
  }

  function serializeSlotLabelsText(slotLabels) {
    return Object.entries(normalizeSlotLabelMap(slotLabels))
      .sort((left, right) => Number(left[0]) - Number(right[0]))
      .map(([slotNumber, label]) => `${slotNumber}=${label}`)
      .join("\n");
  }

  function parseSlotSizesText(value) {
    const parsed = {};
    const allowedSizes = new Set(["2230", "2242", "2260", "2280", "22110"]);
    String(value || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((line) => {
        const match = line.match(/^(\d+)\s*[:=]\s*(\d{4,5})$/);
        if (!match) {
          return;
        }
        const slotNumber = Number.parseInt(match[1], 10);
        const sizeLabel = String(match[2] || "").trim();
        if (!Number.isNaN(slotNumber) && slotNumber >= 0 && allowedSizes.has(sizeLabel)) {
          parsed[slotNumber] = sizeLabel;
        }
      });
    return parsed;
  }

  function serializeSlotSizesText(slotSizes) {
    return Object.entries(normalizeSlotSizeMap(slotSizes))
      .sort((left, right) => Number(left[0]) - Number(right[0]))
      .map(([slotNumber, sizeLabel]) => `${slotNumber}=${sizeLabel}`)
      .join("\n");
  }

  function getStorageViewTemplate(templateId) {
    return state.storageViewTemplates.find((template) => template.id === templateId) || null;
  }

  function storageViewAddValueForProfile(profileId) {
    return `profile:${profileId}`;
  }

  function storageViewAddProfileId(value) {
    const normalized = String(value || "");
    return normalized.startsWith("profile:") ? normalized.slice("profile:".length) : "";
  }

  function storageViewTemplateOptionsHtml() {
    if (!state.storageViewTemplates.length) {
      return '<option value="">No templates loaded</option>';
    }
    return state.storageViewTemplates
      .map((template) => `<option value="${escapeHtml(template.id)}">${escapeHtml(template.label)}</option>`)
      .join("");
  }

  function storageViewAddHelpText() {
    const systemId = currentStorageViewSystemId();
    const hiddenProfiles = hiddenLiveChassisProfiles();
    if (!systemId) {
      return "Storage views stay attached to one system, so internal carrier cards and boot media do not need to become separate systems. Live SES enclosures still show up on their own later, and a saved chassis view is only needed when you want a curated layout for that hardware.";
    }
    if (state.liveEnclosuresLoading) {
      return `Checking live discovered enclosures on ${systemId} so duplicate saved chassis layouts can stay out of the add list.`;
    }
    if (state.liveEnclosuresError) {
      return `Unable to inspect live discovered enclosures on ${systemId} right now, so the full saved chassis layout list is still shown. Virtual/internal templates are unaffected.`;
    }
    if (hiddenProfiles.length) {
      const labels = hiddenProfiles
        .slice(0, 3)
        .map((profile) => profile.label)
        .join(", ");
      const suffix = hiddenProfiles.length > 3 ? ", and more" : "";
      return `Live discovered enclosures on ${systemId} already cover ${labels}${suffix}, so those duplicate saved chassis layouts are hidden here. Generic and internal layouts stay available for hardware that is not auto-discovered.`;
    }
    return `Live discovered enclosures already auto-populate on ${systemId}. Add a storage view here only when you want a saved chassis layout that is not already auto-discovered, or a virtual internal disk group attached to this host.`;
  }

  function storageViewAddOptionsHtml() {
    const chassisProfiles = addableStorageViewProfiles();
    const virtualTemplates = state.storageViewTemplates.filter((template) => template.kind !== "ses_enclosure");
    const groups = [];
    if (chassisProfiles.length) {
      groups.push(`
        <optgroup label="Saved Chassis Views">
          ${chassisProfiles
            .map((profile) => {
              const pinned = profile.id === currentPinnedProfileId() ? " (Pinned)" : "";
              return `<option value="${escapeHtml(storageViewAddValueForProfile(profile.id))}">${escapeHtml(profile.label + pinned)}</option>`;
            })
            .join("")}
        </optgroup>
      `);
    }
    if (virtualTemplates.length) {
      groups.push(`
        <optgroup label="Virtual And Internal Views">
          ${virtualTemplates
            .map((template) => `<option value="${escapeHtml(template.id)}">${escapeHtml(template.label)}</option>`)
            .join("")}
        </optgroup>
      `);
    }
    return groups.join("") || '<option value="">No layouts loaded</option>';
  }

  function storageViewAddDefaultValue() {
    const pinnedProfile = currentPinnedProfile();
    if (pinnedProfile && liveEnclosureMatchesForProfile(pinnedProfile.id).length === 0) {
      return storageViewAddValueForProfile(pinnedProfile.id);
    }
    const firstProfile = addableStorageViewProfiles()[0];
    if (firstProfile) {
      return storageViewAddValueForProfile(firstProfile.id);
    }
    return state.storageViewTemplates.find((template) => template.kind !== "ses_enclosure")?.id || state.storageViewTemplates[0]?.id || "";
  }

  function storageViewProfileOptionsHtml() {
    const options = ['<option value="">Follow current live profile</option>'].concat(
      orderedStorageViewProfiles().map((profile) => `<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.label)}</option>`)
    );
    return options.join("");
  }

  function renderStorageViewTemplateOptions() {
    const addOptionsHtml = storageViewAddOptionsHtml();
    const templateOptionsHtml = storageViewTemplateOptionsHtml();
    const profileOptionsHtml = storageViewProfileOptionsHtml();
    if (elements.setupStorageViewTemplate) {
      const selectedValue = elements.setupStorageViewTemplate.value || storageViewAddDefaultValue();
      elements.setupStorageViewTemplate.innerHTML = addOptionsHtml;
      const availableValues = new Set(
        [...elements.setupStorageViewTemplate.options].map((option) => option.value)
      );
      elements.setupStorageViewTemplate.value = availableValues.has(selectedValue) ? selectedValue : storageViewAddDefaultValue();
      elements.setupStorageViewTemplate.disabled = elements.setupStorageViewTemplate.options.length === 0;
    }
    if (elements.setupStorageViewTemplateSelect) {
      const selectedValue = elements.setupStorageViewTemplateSelect.value || getSelectedStorageView()?.template_id || state.storageViewTemplates[0]?.id || "";
      elements.setupStorageViewTemplateSelect.innerHTML = templateOptionsHtml;
      elements.setupStorageViewTemplateSelect.value = getStorageViewTemplate(selectedValue)?.id || state.storageViewTemplates[0]?.id || "";
      elements.setupStorageViewTemplateSelect.disabled = !state.storageViewTemplates.length;
    }
    if (elements.setupStorageViewProfile) {
      const selectedValue = elements.setupStorageViewProfile.value || getSelectedStorageView()?.profile_id || "";
      elements.setupStorageViewProfile.innerHTML = profileOptionsHtml;
      const availableValues = new Set(
        [...elements.setupStorageViewProfile.options].map((option) => option.value)
      );
      elements.setupStorageViewProfile.value = availableValues.has(selectedValue) ? selectedValue : "";
      elements.setupStorageViewProfile.disabled = !state.profiles.length;
    }
    if (elements.setupStorageViewAddButton) {
      elements.setupStorageViewAddButton.disabled = !elements.setupStorageViewTemplate?.options.length;
    }
  }

  function nextStorageViewOrder() {
    const highest = state.storageViews.reduce((current, storageView) => {
      const order = Number(storageView?.order) || 0;
      return Math.max(current, order);
    }, 0);
    return highest > 0 ? highest + 10 : 10;
  }

  function uniqueStorageViewId(baseId, excludeId = "") {
    const normalizedBase = slugify(baseId, "storage-view");
    let candidate = normalizedBase;
    let suffix = 2;
    const existingIds = new Set(
      state.storageViews
        .map((storageView) => storageView?.id)
        .filter((storageViewId) => storageViewId && storageViewId !== excludeId)
    );
    while (existingIds.has(candidate)) {
      candidate = `${normalizedBase}-${suffix}`;
      suffix += 1;
    }
    return candidate;
  }

  function cloneStorageView(storageView) {
    return JSON.parse(JSON.stringify(storageView || {}));
  }

  function normalizeStorageView(rawView, index = 0, seenIds = null) {
    const template = getStorageViewTemplate(rawView?.template_id) || getStorageViewTemplate("manual-4") || state.storageViewTemplates[0] || null;
    const baseId = rawView?.id || rawView?.label || template?.default_id || `storage-view-${index + 1}`;
    let storageViewId = slugify(baseId, `storage-view-${index + 1}`);
    if (seenIds instanceof Set) {
      let uniqueId = storageViewId;
      let suffix = 2;
      while (seenIds.has(uniqueId)) {
        uniqueId = `${storageViewId}-${suffix}`;
        suffix += 1;
      }
      storageViewId = uniqueId;
      seenIds.add(storageViewId);
    }

    const defaultRender = template?.default_render || {};
    const defaultBinding = template?.default_binding || {};
    const render = {
      show_in_main_ui: rawView?.render?.show_in_main_ui ?? defaultRender.show_in_main_ui ?? true,
      show_in_admin_ui: rawView?.render?.show_in_admin_ui ?? defaultRender.show_in_admin_ui ?? true,
      default_collapsed: rawView?.render?.default_collapsed ?? defaultRender.default_collapsed ?? false,
    };
    const binding = {
      mode: rawView?.binding?.mode || defaultBinding.mode || "auto",
      enclosure_ids: Array.isArray(rawView?.binding?.enclosure_ids) ? rawView.binding.enclosure_ids.filter(Boolean) : Array.isArray(defaultBinding.enclosure_ids) ? defaultBinding.enclosure_ids.filter(Boolean) : [],
      pool_names: Array.isArray(rawView?.binding?.pool_names) ? rawView.binding.pool_names.filter(Boolean) : Array.isArray(defaultBinding.pool_names) ? defaultBinding.pool_names.filter(Boolean) : [],
      serials: Array.isArray(rawView?.binding?.serials) ? rawView.binding.serials.filter(Boolean) : Array.isArray(defaultBinding.serials) ? defaultBinding.serials.filter(Boolean) : [],
      pcie_addresses: Array.isArray(rawView?.binding?.pcie_addresses) ? rawView.binding.pcie_addresses.filter(Boolean) : Array.isArray(defaultBinding.pcie_addresses) ? defaultBinding.pcie_addresses.filter(Boolean) : [],
      device_names: Array.isArray(rawView?.binding?.device_names) ? rawView.binding.device_names.filter(Boolean) : Array.isArray(defaultBinding.device_names) ? defaultBinding.device_names.filter(Boolean) : [],
    };
    const slotLabels = normalizeSlotLabelMap(rawView?.layout_overrides?.slot_labels);
    const slotSizes = normalizeSlotSizeMap(rawView?.layout_overrides?.slot_sizes);
    return {
      id: storageViewId,
      label: String(rawView?.label || template?.default_label || storageViewId).trim(),
      kind: rawView?.kind || template?.kind || "manual",
      template_id: rawView?.template_id || template?.id || "manual-4",
      profile_id: rawView?.kind === "ses_enclosure" || rawView?.template_id === "ses-auto"
        ? (rawView?.profile_id || "")
        : "",
      enabled: rawView?.enabled !== false,
      order: Number(rawView?.order) || (index + 1) * 10,
      render,
      binding,
      layout_overrides:
        Object.keys(slotLabels).length || Object.keys(slotSizes).length
          ? { slot_labels: slotLabels, slot_sizes: slotSizes }
          : null,
    };
  }

  function normalizeStorageViews(rawViews) {
    const seenIds = new Set();
    return (Array.isArray(rawViews) ? rawViews : [])
      .map((storageView, index) => normalizeStorageView(storageView, index, seenIds))
      .sort((left, right) => (Number(left.order) || 0) - (Number(right.order) || 0));
  }

  function createStorageViewFromTemplate(templateId) {
    const template = getStorageViewTemplate(templateId) || state.storageViewTemplates[0] || null;
    if (!template) {
      return null;
    }
    const slotLabels = normalizeSlotLabelMap(template.default_slot_labels);
    return normalizeStorageView(
      {
        id: uniqueStorageViewId(template.default_id || template.label),
        label: template.default_label || template.label,
        kind: template.kind,
        template_id: template.id,
        enabled: true,
        order: nextStorageViewOrder(),
        render: template.default_render || {},
        binding: template.default_binding || {},
        layout_overrides: Object.keys(slotLabels).length ? { slot_labels: slotLabels, slot_sizes: {} } : null,
      },
      state.storageViews.length
    );
  }

  function existingSavedChassisView(profileId = "") {
    return state.storageViews.find(
      (storageView) => storageView.kind === "ses_enclosure" && String(storageView.profile_id || "") === String(profileId || "")
    ) || null;
  }

  function defaultChassisViewLabel(profile) {
    const baseLabel = String(profile?.panel_title || profile?.label || "Chassis").trim();
    if (!baseLabel) {
      return "Saved Chassis";
    }
    return baseLabel;
  }

  function createChassisStorageView(profileId) {
    const template = getStorageViewTemplate("ses-auto");
    const profile = getProfileById(profileId);
    if (!template || !profile) {
      return null;
    }
    const label = defaultChassisViewLabel(profile);
    return normalizeStorageView(
      {
        id: uniqueStorageViewId(label),
        label,
        kind: "ses_enclosure",
        template_id: template.id,
        profile_id: profile.id,
        enabled: true,
        order: nextStorageViewOrder(),
        render: template.default_render || {},
        binding: {
          ...(template.default_binding || {}),
          mode: "auto",
        },
      },
      state.storageViews.length
    );
  }

  function getSelectedStorageView() {
    return state.storageViews.find((storageView) => storageView.id === state.selectedStorageViewId) || null;
  }

  function ensureStorageViewSelection() {
    const selected = getSelectedStorageView();
    if (selected) {
      return selected;
    }
    state.selectedStorageViewId = state.storageViews[0]?.id || "";
    return getSelectedStorageView();
  }

  function buildSequentialLayout(rows, columns, slotCount) {
    const safeRows = Math.max(1, Number(rows) || 1);
    const safeColumns = Math.max(1, Number(columns) || 1);
    const safeSlotCount = Math.max(1, Number(slotCount) || safeRows * safeColumns);
    const layout = [];
    let slotNumber = 0;
    for (let rowIndex = 0; rowIndex < safeRows; rowIndex += 1) {
      const row = [];
      for (let columnIndex = 0; columnIndex < safeColumns; columnIndex += 1) {
        if (slotNumber < safeSlotCount) {
          row.push(slotNumber);
          slotNumber += 1;
        } else {
          row.push(null);
        }
      }
      layout.push(row);
    }
    return layout;
  }

  function storageViewProfile(storageView, { fallbackToPinned = true } = {}) {
    const explicitProfile = getProfileById(storageView?.profile_id);
    if (explicitProfile) {
      return explicitProfile;
    }
    if (storageView?.kind === "ses_enclosure" && fallbackToPinned) {
      return previewProfile();
    }
    return null;
  }

  function buildStorageViewRows(storageView) {
    const template = getStorageViewTemplate(storageView?.template_id);
    const selectedProfile = storageViewProfile(storageView);
    if (storageView?.kind === "ses_enclosure" && selectedProfile) {
      return buildProfileRows(selectedProfile);
    }
    if (Array.isArray(template?.slot_layout) && template.slot_layout.length) {
      return template.slot_layout;
    }
    return buildSequentialLayout(template?.rows || 1, template?.columns || 1, template?.slot_count || 1);
  }

  function storageViewKindLabel(storageView) {
    if (storageView?.kind === "ses_enclosure") {
      return "Saved Chassis View";
    }
    return "Virtual Storage View";
  }

  function storageViewSlotLabel(storageView, slotValue) {
    const template = getStorageViewTemplate(storageView?.template_id);
    const selectedProfile = storageViewProfile(storageView);
    const overrides = storageView?.layout_overrides?.slot_labels || {};
    const templateLabels = template?.default_slot_labels || {};
    if (overrides[slotValue] || overrides[String(slotValue)]) {
      return overrides[slotValue] || overrides[String(slotValue)];
    }
    if (templateLabels[slotValue] || templateLabels[String(slotValue)]) {
      return templateLabels[slotValue] || templateLabels[String(slotValue)];
    }
    if (storageView?.kind === "ses_enclosure" && selectedProfile) {
      return String(slotValue).padStart(2, "0");
    }
    return `Slot ${Number(slotValue) + 1}`;
  }

  function storageViewSlotSize(storageView, slotValue) {
    const overrides = storageView?.layout_overrides?.slot_sizes || {};
    return overrides[slotValue] || overrides[String(slotValue)] || "2280";
  }

  function orderedStorageViewSlotIndices(storageView) {
    const rows = buildStorageViewRows(storageView);
    const visibleSlots = rows.flat().filter((slotValue) => Number.isInteger(slotValue));
    if (storageView?.kind === "ses_enclosure") {
      return visibleSlots;
    }
    return [...visibleSlots].sort((left, right) => Number(left) - Number(right));
  }

  function storageViewPreviewBindingValue(storageView, slotValue) {
    const orderedSlots = orderedStorageViewSlotIndices(storageView);
    const logicalIndex = orderedSlots.findIndex((candidate) => Number(candidate) === Number(slotValue));
    if (logicalIndex < 0) {
      return "";
    }
    const preferredDevice = storageView?.binding?.device_names?.[logicalIndex];
    if (preferredDevice) {
      return preferredDevice;
    }
    const preferredSerial = storageView?.binding?.serials?.[logicalIndex];
    if (preferredSerial) {
      return preferredSerial;
    }
    const preferredAddress = storageView?.binding?.pcie_addresses?.[logicalIndex];
    if (preferredAddress) {
      return preferredAddress;
    }
    return "";
  }

  function collectStorageViewMeta(storageView) {
    const template = getStorageViewTemplate(storageView?.template_id);
    const selectedProfile = storageViewProfile(storageView);
    const previewRows = buildStorageViewRows(storageView);
    const visibleSlots = previewRows.flat().filter((slotValue) => Number.isInteger(slotValue)).length;
    const profileChip = storageView?.kind === "ses_enclosure" && selectedProfile
      ? `profile: ${selectedProfile.label}${storageView?.profile_id ? "" : " (live fallback)"}`
      : null;
    return [
      storageView ? storageViewKindLabel(storageView) : null,
      template?.label || null,
      `${visibleSlots} slots`,
      storageView?.binding?.mode ? `binding: ${storageView.binding.mode}` : null,
      profileChip,
      storageView?.kind === "nvme_carrier" ? "slot 1 nearest PCIe edge" : null,
      template?.supports_auto_discovery ? "auto-discovery" : null,
      template?.supports_led ? "LED capable" : null,
    ].filter(Boolean);
  }

  function buildStorageViewPreviewCell(storageView, slotValue) {
    const slotLabel = storageViewSlotLabel(storageView, slotValue);
    const slotSize = storageViewSlotSize(storageView, slotValue);
    const bindingValue = storageViewPreviewBindingValue(storageView, slotValue);
    if (storageView?.kind === "nvme_carrier") {
      return `
        <div class="profile-preview-cell">
          <div class="storage-view-device storage-view-device--nvme" data-slot-size="${escapeHtml(slotSize)}">
            <div class="storage-view-device-content">
              <span class="storage-view-preview-slot-label">${escapeHtml(slotLabel)}</span>
              <span class="storage-view-preview-slot-index">${escapeHtml(slotSize)}</span>
              <span class="storage-view-binding-summary">${escapeHtml(bindingValue || "Unbound preview slot")}</span>
            </div>
          </div>
        </div>
      `;
    }
    return `
      <div class="profile-preview-cell">
        <span class="storage-view-preview-slot-label">${escapeHtml(slotLabel)}</span>
        <span class="storage-view-preview-slot-index">slot ${escapeHtml(String(slotValue))}</span>
        ${bindingValue ? `<span class="storage-view-binding-summary">${escapeHtml(bindingValue)}</span>` : ""}
      </div>
    `;
  }

  function renderStorageViewList() {
    if (!elements.setupStorageViewList || !elements.setupStorageViewCount) {
      return;
    }
    ensureStorageViewSelection();
    elements.setupStorageViewCount.textContent = `${state.storageViews.length} view${state.storageViews.length === 1 ? "" : "s"}`;
    elements.setupStorageViewList.innerHTML = state.storageViews
      .map((storageView) => {
        const template = getStorageViewTemplate(storageView.template_id);
        const classes = ["storage-view-card"];
        if (storageView.id === state.selectedStorageViewId) {
          classes.push("is-selected");
        }
        if (!storageView.enabled) {
          classes.push("is-disabled");
        }
        const summary = collectStorageViewMeta(storageView);
        return `
          <button class="${classes.join(" ")}" type="button" data-storage-view-id="${escapeHtml(storageView.id)}">
            <div class="storage-view-card-header">
              <div>
                <h4>${escapeHtml(storageView.label || storageView.id)}</h4>
                <p>${escapeHtml(template?.summary || "Storage view definition.")}</p>
              </div>
              <span class="badge">${escapeHtml(storageView.enabled ? "Enabled" : "Disabled")}</span>
            </div>
            <div class="profile-preview-meta">
              ${summary.map((item) => `<span class="meta-chip">${escapeHtml(item)}</span>`).join("")}
            </div>
          </button>
        `;
      })
      .join("");
  }

  function syncStorageViewEditorFromState() {
    const storageView = ensureStorageViewSelection();
    if (!elements.setupStorageViewEditor || !elements.setupStorageViewEmpty || !elements.setupStorageViewTemplateBadge) {
      return;
    }
    if (!storageView) {
      elements.setupStorageViewEditor.classList.add("hidden");
      elements.setupStorageViewEmpty.classList.remove("hidden");
      elements.setupStorageViewTemplateBadge.textContent = "No view selected";
      if (elements.setupStorageViewHelp) {
        elements.setupStorageViewHelp.textContent = storageViewAddHelpText();
      }
      return;
    }

    const template = getStorageViewTemplate(storageView.template_id);
    const selectedProfile = storageViewProfile(storageView, { fallbackToPinned: false });
    elements.setupStorageViewEditor.classList.remove("hidden");
    elements.setupStorageViewEmpty.classList.add("hidden");
    elements.setupStorageViewTemplateBadge.textContent = template?.label || storageView.template_id;
    if (elements.setupStorageViewLabel) {
      elements.setupStorageViewLabel.value = storageView.label || "";
    }
    if (elements.setupStorageViewId) {
      elements.setupStorageViewId.value = storageView.id || "";
    }
    if (elements.setupStorageViewTemplateSelect) {
      elements.setupStorageViewTemplateSelect.value = template?.id || state.storageViewTemplates[0]?.id || "";
    }
    if (elements.setupStorageViewProfile) {
      elements.setupStorageViewProfile.value = selectedProfile?.id || storageView.profile_id || "";
      elements.setupStorageViewProfile.disabled = storageView.kind !== "ses_enclosure" || !state.profiles.length;
    }
    if (elements.setupStorageViewBindingMode) {
      elements.setupStorageViewBindingMode.value = storageView.binding?.mode || "auto";
    }
    if (elements.setupStorageViewOrder) {
      elements.setupStorageViewOrder.value = String(Number(storageView.order) || 10);
    }
    if (elements.setupStorageViewEnabled) {
      elements.setupStorageViewEnabled.checked = storageView.enabled !== false;
    }
    if (elements.setupStorageViewShowMain) {
      elements.setupStorageViewShowMain.checked = storageView.render?.show_in_main_ui !== false;
    }
    if (elements.setupStorageViewShowAdmin) {
      elements.setupStorageViewShowAdmin.checked = storageView.render?.show_in_admin_ui !== false;
    }
    if (elements.setupStorageViewCollapsed) {
      elements.setupStorageViewCollapsed.checked = Boolean(storageView.render?.default_collapsed);
    }
    if (elements.setupStorageViewEnclosureIds) {
      elements.setupStorageViewEnclosureIds.value = serializeDelimitedLines(storageView.binding?.enclosure_ids);
    }
    if (elements.setupStorageViewPoolNames) {
      elements.setupStorageViewPoolNames.value = serializeDelimitedLines(storageView.binding?.pool_names);
    }
    if (elements.setupStorageViewSerials) {
      elements.setupStorageViewSerials.value = serializeDelimitedLines(storageView.binding?.serials);
    }
    if (elements.setupStorageViewPcieAddresses) {
      elements.setupStorageViewPcieAddresses.value = serializeDelimitedLines(storageView.binding?.pcie_addresses);
    }
    if (elements.setupStorageViewDeviceNames) {
      elements.setupStorageViewDeviceNames.value = serializeDelimitedLines(storageView.binding?.device_names);
    }
    if (elements.setupStorageViewSlotLabels) {
      elements.setupStorageViewSlotLabels.value = serializeSlotLabelsText(storageView.layout_overrides?.slot_labels);
    }
    if (elements.setupStorageViewSlotSizes) {
      elements.setupStorageViewSlotSizes.value = serializeSlotSizesText(storageView.layout_overrides?.slot_sizes);
      elements.setupStorageViewSlotSizes.disabled = storageView.kind !== "nvme_carrier";
    }
    if (elements.setupStorageViewHelp) {
      elements.setupStorageViewHelp.textContent = template?.notes
        || storageViewAddHelpText();
    }
    if (elements.setupStorageViewEditorHelp) {
      const duplicateLiveEnclosures = storageView.kind === "ses_enclosure"
        ? liveEnclosureMatchesForProfile(storageView.profile_id)
        : [];
      elements.setupStorageViewEditorHelp.textContent = storageView.kind === "ses_enclosure"
        ? (duplicateLiveEnclosures.length
          ? `This saved chassis view duplicates the live discovered enclosure${duplicateLiveEnclosures.length === 1 ? "" : "s"} ${duplicateLiveEnclosures.map((enclosure) => enclosure.label).join(", ")}. The live hardware already auto-populates separately, so keep this only if you still want a curated overlay.`
          : storageView.profile_id
            ? "This saved chassis view keeps its own profile-backed layout while the real enclosure still appears separately in runtime discovery."
            : "This legacy saved chassis view follows the current live profile until you pin a specific saved chassis layout here.")
        : (template?.summary || "The template defines the physical shape. Binding hints decide how disks or enclosures should land inside that shape later.");
    }
    if (elements.setupStorageViewMoveUpButton) {
      elements.setupStorageViewMoveUpButton.disabled = state.storageViews[0]?.id === storageView.id;
    }
    if (elements.setupStorageViewMoveDownButton) {
      elements.setupStorageViewMoveDownButton.disabled = state.storageViews[state.storageViews.length - 1]?.id === storageView.id;
    }
  }

  function renderStorageViewPreview() {
    const storageView = ensureStorageViewSelection();
    if (
      !elements.setupStorageViewPreviewSummary
      || !elements.setupStorageViewPreviewGrid
      || !elements.setupStorageViewPreviewMeta
      || !elements.setupStorageViewKindBadge
    ) {
      return;
    }
    if (!storageView) {
      elements.setupStorageViewKindBadge.textContent = "No view";
      elements.setupStorageViewPreviewSummary.textContent = "Add a storage view to see its template preview here.";
      elements.setupStorageViewPreviewGrid.innerHTML = "";
      elements.setupStorageViewPreviewMeta.innerHTML = "";
      return;
    }

    const template = getStorageViewTemplate(storageView.template_id);
    const selectedProfile = storageViewProfile(storageView);
    const previewRows = buildStorageViewRows(storageView);
    const columnCount = Math.max(1, ...previewRows.map((row) => (Array.isArray(row) ? row.length : 0)));
    elements.setupStorageViewKindBadge.textContent = storageViewKindLabel(storageView);
    elements.setupStorageViewPreviewSummary.textContent =
      storageView.kind === "ses_enclosure" && selectedProfile
        ? (storageView.profile_id
          ? `Using the saved chassis layout ${selectedProfile.label} as the preview shape for this saved chassis view.`
          : `Using the current live profile ${selectedProfile.label} as the preview shape until this saved chassis view pins its own layout.`)
        : template?.summary || "Storage view preview for the selected template.";
    elements.setupStorageViewPreviewGrid.style.gridTemplateColumns = `repeat(${columnCount}, minmax(0, 1fr))`;
    elements.setupStorageViewPreviewGrid.classList.toggle("is-nvme-carrier", storageView.kind === "nvme_carrier");
    elements.setupStorageViewPreviewGrid.innerHTML = previewRows
      .flat()
      .map((slotValue) => {
        if (slotValue === null || slotValue === undefined) {
          return '<div class="profile-preview-cell is-gap">Gap</div>';
        }
        return buildStorageViewPreviewCell(storageView, slotValue);
      })
      .join("");
    elements.setupStorageViewPreviewMeta.innerHTML = collectStorageViewMeta(storageView)
      .map((item) => `<span class="meta-chip">${escapeHtml(item)}</span>`)
      .join("");
  }

  function renderStorageViews() {
    renderStorageViewTemplateOptions();
    renderStorageViewList();
    syncStorageViewEditorFromState();
    renderStorageViewPreview();
    renderStorageViewCandidates();
  }

  function replaceStorageViewState(rawViews, preferredId = "") {
    state.storageViews = normalizeStorageViews(rawViews);
    state.selectedStorageViewId = preferredId || state.storageViews[0]?.id || "";
    if (!state.storageViews.some((storageView) => storageView.id === state.selectedStorageViewId)) {
      state.selectedStorageViewId = state.storageViews[0]?.id || "";
    }
    renderStorageViews();
  }

  function updateSelectedStorageView(mutator) {
    const selected = getSelectedStorageView();
    if (!selected) {
      return;
    }
    mutator(selected);
    const selectedId = state.selectedStorageViewId;
    state.storageViews = normalizeStorageViews(state.storageViews);
    state.selectedStorageViewId = state.storageViews.find((storageView) => storageView.id === selectedId)?.id || state.storageViews[0]?.id || "";
    renderStorageViews();
  }

  function saveStorageViewEditorToState() {
    updateSelectedStorageView((storageView) => {
      const previousId = storageView.id;
      storageView.label = elements.setupStorageViewLabel?.value?.trim() || storageView.label;
      storageView.id = uniqueStorageViewId(elements.setupStorageViewId?.value?.trim() || storageView.label, previousId);
      storageView.template_id = elements.setupStorageViewTemplateSelect?.value || storageView.template_id;
      storageView.kind = getStorageViewTemplate(storageView.template_id)?.kind || storageView.kind || "manual";
      storageView.profile_id = storageView.kind === "ses_enclosure"
        ? (elements.setupStorageViewProfile?.value || "")
        : "";
      storageView.order = Number(elements.setupStorageViewOrder?.value) || storageView.order || nextStorageViewOrder();
      storageView.enabled = Boolean(elements.setupStorageViewEnabled?.checked);
      storageView.render = {
        show_in_main_ui: Boolean(elements.setupStorageViewShowMain?.checked),
        show_in_admin_ui: Boolean(elements.setupStorageViewShowAdmin?.checked),
        default_collapsed: Boolean(elements.setupStorageViewCollapsed?.checked),
      };
      storageView.binding = {
        mode: elements.setupStorageViewBindingMode?.value || "auto",
        enclosure_ids: splitDelimitedLines(elements.setupStorageViewEnclosureIds?.value),
        pool_names: splitDelimitedLines(elements.setupStorageViewPoolNames?.value),
        serials: splitDelimitedLines(elements.setupStorageViewSerials?.value),
        pcie_addresses: splitDelimitedLines(elements.setupStorageViewPcieAddresses?.value),
        device_names: splitDelimitedLines(elements.setupStorageViewDeviceNames?.value),
      };
      const slotLabels = parseSlotLabelsText(elements.setupStorageViewSlotLabels?.value);
      const slotSizes = parseSlotSizesText(elements.setupStorageViewSlotSizes?.value);
      storageView.layout_overrides =
        Object.keys(slotLabels).length || Object.keys(slotSizes).length
          ? { slot_labels: slotLabels, slot_sizes: slotSizes }
          : null;
    });
  }

  function currentStorageViewSystemId() {
    const explicitId = state.loadedSystemId || elements.setupSystemId?.value?.trim() || state.selectedExistingSystemId || "";
    return getSystemById(explicitId)?.id || "";
  }

  function resetLiveEnclosureState() {
    state.liveEnclosures = [];
    state.liveEnclosuresLoading = false;
    state.liveEnclosuresSystemId = null;
    state.liveEnclosuresError = null;
  }

  async function fetchLiveEnclosures({ force = false, quiet = false } = {}) {
    const systemId = currentStorageViewSystemId();
    if (!systemId) {
      resetLiveEnclosureState();
      renderStorageViews();
      return;
    }
    state.liveEnclosuresLoading = true;
    state.liveEnclosuresError = null;
    state.liveEnclosuresSystemId = systemId;
    renderStorageViews();
    try {
      const params = new URLSearchParams({ system_id: systemId });
      if (force) {
        params.set("force", "true");
      }
      const payload = await fetchJson(`/api/admin/storage-views/live-enclosures?${params.toString()}`);
      state.liveEnclosures = Array.isArray(payload.enclosures) ? payload.enclosures : [];
      state.liveEnclosuresSystemId = payload.system_id || systemId;
      if (!quiet) {
        setBanner(`Loaded ${state.liveEnclosures.length} discovered live enclosure${state.liveEnclosures.length === 1 ? "" : "s"} for ${state.liveEnclosuresSystemId}.`, "success");
      }
    } catch (error) {
      state.liveEnclosures = [];
      state.liveEnclosuresSystemId = systemId;
      state.liveEnclosuresError = error.message || String(error);
      if (!quiet) {
        setBanner(`Unable to inspect live enclosures: ${error.message || error}`, "error");
      }
    } finally {
      state.liveEnclosuresLoading = false;
      renderStorageViews();
    }
  }

  function candidateBindingAlreadyAttached(candidate, storageView = getSelectedStorageView()) {
    if (!candidate || !storageView) {
      return false;
    }
    const recommended = candidate.recommended_binding || {};
    const serials = new Set(Array.isArray(storageView.binding?.serials) ? storageView.binding.serials : []);
    const pcieAddresses = new Set(Array.isArray(storageView.binding?.pcie_addresses) ? storageView.binding.pcie_addresses : []);
    const deviceNames = new Set(Array.isArray(storageView.binding?.device_names) ? storageView.binding.device_names : []);
    const serialsAttached = (recommended.serials || []).every((value) => serials.has(value));
    const pcieAttached = (recommended.pcie_addresses || []).every((value) => pcieAddresses.has(value));
    const devicesAttached = (recommended.device_names || []).every((value) => deviceNames.has(value));
    return serialsAttached && pcieAttached && devicesAttached;
  }

  function storageViewCandidateOwner(candidate) {
    return state.storageViews.find((storageView) => candidateBindingAlreadyAttached(candidate, storageView)) || null;
  }

  function visibleStorageViewCandidates(storageView = getSelectedStorageView()) {
    if (!storageView) {
      return [];
    }
    return state.storageViewCandidates.filter((candidate) => {
      const owner = storageViewCandidateOwner(candidate);
      return !owner || owner.id === storageView.id;
    });
  }

  function mergeDistinctValues(existingValues, additionalValues) {
    return Array.from(new Set([...(Array.isArray(existingValues) ? existingValues : []), ...(Array.isArray(additionalValues) ? additionalValues : [])].filter(Boolean)));
  }

  function applyStorageViewCandidate(candidate) {
    if (!candidate) {
      return;
    }
    updateSelectedStorageView((storageView) => {
      const recommended = candidate.recommended_binding || {};
      storageView.binding = {
        ...storageView.binding,
        mode: storageView.binding?.mode === "auto" ? "hybrid" : (storageView.binding?.mode || "hybrid"),
        serials: mergeDistinctValues(storageView.binding?.serials, recommended.serials),
        pcie_addresses: mergeDistinctValues(storageView.binding?.pcie_addresses, recommended.pcie_addresses),
        device_names: mergeDistinctValues(storageView.binding?.device_names, recommended.device_names),
      };
    });
  }

  function applyAllStorageViewCandidates() {
    const selectedStorageView = getSelectedStorageView();
    if (!selectedStorageView) {
      setBanner("Select a storage view first so the candidate bindings know where to land.", "error");
      return;
    }
    const availableCandidates = visibleStorageViewCandidates(selectedStorageView);
    if (!availableCandidates.length) {
      setBanner("No unmapped inventory candidates are loaded yet.", "error");
      return;
    }
    availableCandidates.forEach((candidate) => {
      if (!candidateBindingAlreadyAttached(candidate, selectedStorageView)) {
        applyStorageViewCandidate(candidate);
      }
    });
    setBanner(`Added ${availableCandidates.length} unmapped inventory candidates to ${selectedStorageView.label}.`, "success");
  }

  function renderStorageViewCandidates() {
    if (!elements.setupStorageViewCandidatesList || !elements.setupStorageViewCandidatesHelp || !elements.setupStorageViewCandidatesAddAllButton) {
      return;
    }
    const selectedStorageView = getSelectedStorageView();
    const systemId = currentStorageViewSystemId();
    const availableCandidates = visibleStorageViewCandidates(selectedStorageView);
    const claimedElsewhereCount = state.storageViewCandidates.length - availableCandidates.length;
    elements.setupStorageViewCandidatesAddAllButton.disabled =
      !selectedStorageView || !availableCandidates.some((candidate) => !candidateBindingAlreadyAttached(candidate, selectedStorageView));
    if (!selectedStorageView) {
      elements.setupStorageViewCandidatesHelp.textContent = "Select a storage view first, then you can attach live unmapped inventory candidates to it.";
      elements.setupStorageViewCandidatesList.innerHTML = "";
      return;
    }
    if (!systemId) {
      elements.setupStorageViewCandidatesHelp.textContent = "Load a saved system first so the admin sidecar can inspect live inventory and suggest unmapped candidates.";
      elements.setupStorageViewCandidatesList.innerHTML = "";
      return;
    }
    if (state.storageViewCandidatesLoading) {
      elements.setupStorageViewCandidatesHelp.textContent = `Inspecting live inventory on ${systemId} for disks that are not already sitting in mapped slots...`;
      elements.setupStorageViewCandidatesList.innerHTML = "";
      return;
    }
    if (!availableCandidates.length) {
      if (selectedStorageView.kind === "ses_enclosure") {
        elements.setupStorageViewCandidatesHelp.textContent = `This saved chassis view mirrors a live enclosure. The discovered enclosure auto-populates separately, and candidate shortcuts are usually only needed for virtual internal views on ${systemId}.`;
        elements.setupStorageViewCandidatesList.innerHTML = "";
        return;
      }
      if (claimedElsewhereCount > 0) {
        elements.setupStorageViewCandidatesHelp.textContent = `All currently discovered unmapped candidates are already attached to other saved storage views on ${systemId}, so this view is intentionally not re-offering them.`;
      } else {
        elements.setupStorageViewCandidatesHelp.textContent = `No unmapped inventory candidates were found for ${systemId}. That usually means everything visible is already tied to a slot, or this host needs a manual binding for the next internal group.`;
      }
      elements.setupStorageViewCandidatesList.innerHTML = "";
      return;
    }
    elements.setupStorageViewCandidatesHelp.textContent = selectedStorageView.kind === "ses_enclosure"
      ? `These candidates come from live inventory on ${systemId}, but this saved chassis view already mirrors a separately discovered live enclosure. Candidate shortcuts are usually more useful for virtual internal views.`
      : claimedElsewhereCount > 0
        ? `These candidates come from live inventory on ${systemId}, exclude disks already sitting in mapped slots, and also hide disks already claimed by a different saved storage view.`
        : `These candidates come from live inventory on ${systemId} and exclude disks already sitting in mapped slots. Use them as a safer shortcut for internal NVMe or boot-device views.`;
    elements.setupStorageViewCandidatesList.innerHTML = availableCandidates
      .map((candidate) => {
        const attached = candidateBindingAlreadyAttached(candidate, selectedStorageView);
        const deviceSummary = Array.isArray(candidate.device_names) && candidate.device_names.length
          ? candidate.device_names.join(", ")
          : "No device names surfaced";
        return `
          <article class="storage-view-candidate-card${attached ? " is-attached" : ""}">
            <div class="storage-view-candidate-header">
              <div>
                <h5>${escapeHtml(candidate.label || candidate.candidate_id || "Inventory candidate")}</h5>
                <p>${escapeHtml(candidate.description || "Live unmapped inventory candidate.")}</p>
              </div>
              <span class="badge">${attached ? "Attached" : "Unmapped"}</span>
            </div>
            <div class="storage-view-candidate-body">
              <div class="profile-preview-meta">
                ${[
                  candidate.serial ? `serial ${candidate.serial}` : null,
                  candidate.pool_name ? `pool ${candidate.pool_name}` : null,
                  candidate.transport_address ? candidate.transport_address : null,
                  candidate.bus ? candidate.bus : null,
                ].filter(Boolean).map((item) => `<span class="meta-chip">${escapeHtml(item)}</span>`).join("")}
              </div>
              <p>${escapeHtml(deviceSummary)}</p>
            </div>
            <div class="storage-view-candidate-actions">
              <button class="button secondary small" type="button" data-storage-view-candidate-id="${escapeHtml(candidate.candidate_id || "")}" ${attached ? "disabled" : ""}>${attached ? "Already Added" : "Add Hints"}</button>
            </div>
          </article>
        `;
      })
      .join("");
  }

  async function fetchStorageViewCandidates({ force = false, quiet = false } = {}) {
    const systemId = currentStorageViewSystemId();
    if (!systemId) {
      state.storageViewCandidates = [];
      state.storageViewCandidatesSystemId = null;
      state.storageViewCandidatesLoading = false;
      renderStorageViewCandidates();
      return;
    }
    state.storageViewCandidatesLoading = true;
    renderStorageViewCandidates();
    try {
      const params = new URLSearchParams({ system_id: systemId });
      if (force) {
        params.set("force", "true");
      }
      const payload = await fetchJson(`/api/admin/storage-views/candidates?${params.toString()}`);
      state.storageViewCandidates = Array.isArray(payload.candidates) ? payload.candidates : [];
      state.storageViewCandidatesSystemId = payload.system_id || systemId;
      if (!quiet) {
        setBanner(`Loaded ${state.storageViewCandidates.length} unmapped inventory candidate${state.storageViewCandidates.length === 1 ? "" : "s"} for ${state.storageViewCandidatesSystemId}.`, "success");
      }
    } catch (error) {
      state.storageViewCandidates = [];
      state.storageViewCandidatesSystemId = systemId;
      if (!quiet) {
        setBanner(`Unable to load unmapped inventory candidates: ${error.message || error}`, "error");
      }
    } finally {
      state.storageViewCandidatesLoading = false;
      renderStorageViewCandidates();
    }
  }

  function addStorageView(templateId) {
    const profileId = storageViewAddProfileId(templateId);
    if (profileId) {
      const existingView = existingSavedChassisView(profileId);
      if (existingView) {
        state.selectedStorageViewId = existingView.id;
        renderStorageViews();
        setBanner(`Selected existing saved chassis view ${existingView.label}.`, "success");
        return;
      }
      const duplicateLiveEnclosures = liveEnclosureMatchesForProfile(profileId);
      if (duplicateLiveEnclosures.length) {
        const labels = duplicateLiveEnclosures.map((enclosure) => enclosure.label).join(", ");
        setBanner(`Skipped duplicate saved chassis view. ${labels} already auto-populate as discovered live enclosure${duplicateLiveEnclosures.length === 1 ? "" : "s"}.`, "info");
        renderStorageViews();
        return;
      }
    }

    const storageView = profileId
      ? createChassisStorageView(profileId)
      : createStorageViewFromTemplate(templateId);
    if (!storageView) {
      if (profileId) {
        setBanner("That saved chassis layout is not available right now.", "error");
      }
      return;
    }
    state.storageViews = normalizeStorageViews([...state.storageViews, storageView]);
    state.selectedStorageViewId = storageView.id;
    renderStorageViews();
    if (profileId) {
      const profile = getProfileById(profileId);
      setBanner(`Added saved chassis view ${storageView.label} from ${profile?.label || profileId}.`, "success");
    }
  }

  function deleteSelectedStorageView() {
    const selectedId = state.selectedStorageViewId;
    if (!selectedId) {
      return;
    }
    state.storageViews = state.storageViews.filter((storageView) => storageView.id !== selectedId);
    state.selectedStorageViewId = state.storageViews[0]?.id || "";
    renderStorageViews();
  }

  function duplicateSelectedStorageView() {
    const selected = getSelectedStorageView();
    if (!selected) {
      return;
    }
    const duplicated = cloneStorageView(selected);
    duplicated.id = uniqueStorageViewId(`${selected.id}-copy`);
    duplicated.label = `${selected.label} Copy`;
    duplicated.order = nextStorageViewOrder();
    state.storageViews = normalizeStorageViews([...state.storageViews, duplicated]);
    state.selectedStorageViewId = duplicated.id;
    renderStorageViews();
  }

  function moveSelectedStorageView(direction) {
    const selected = getSelectedStorageView();
    if (!selected) {
      return;
    }
    const ordered = [...state.storageViews].sort((left, right) => (Number(left.order) || 0) - (Number(right.order) || 0));
    const currentIndex = ordered.findIndex((storageView) => storageView.id === selected.id);
    if (currentIndex < 0) {
      return;
    }
    const targetIndex = currentIndex + direction;
    if (targetIndex < 0 || targetIndex >= ordered.length) {
      return;
    }
    const currentOrder = ordered[currentIndex].order;
    ordered[currentIndex].order = ordered[targetIndex].order;
    ordered[targetIndex].order = currentOrder;
    state.storageViews = normalizeStorageViews(ordered);
    state.selectedStorageViewId = selected.id;
    renderStorageViews();
  }

  function platformSetupCopy(platform) {
    switch (String(platform || "core").toLowerCase()) {
      case "scale":
        return "TrueNAS SCALE usually combines the middleware websocket path with Linux-side SSH enrichment for SMART detail, SES, and slot actions.";
      case "linux":
        return "Generic Linux setups are usually SSH-heavy, so pinning a trusted profile and SSH command set matters more than API auth here.";
      case "quantastor":
        return "Quantastor normally uses API user/password auth, with SSH reserved for the richer shared-slot and SES details.";
      default:
        return "TrueNAS CORE usually wants an API key, with SSH as the optional fallback for enclosure mapping and LED control.";
    }
  }

  function defaultCommands(platform) {
    const key = String(platform || "core").toLowerCase();
    const commands = state.platformDefaults?.[key]?.ssh_commands;
    return Array.isArray(commands) ? commands : [];
  }

  function syncPlatformHelp() {
    if (!elements.setupPlatformHelp || !elements.setupPlatform) {
      return;
    }
    elements.setupPlatformHelp.textContent = platformSetupCopy(elements.setupPlatform.value);
  }

  function maybeLoadRecommendedCommands(force = false) {
    if (!elements.setupSshCommands || !elements.setupPlatform) {
      return;
    }
    const currentValue = elements.setupSshCommands.value.trim();
    if (!force && currentValue) {
      return;
    }
    elements.setupSshCommands.value = defaultCommands(elements.setupPlatform.value).join("\n");
    scheduleSudoersPreviewRefresh(0);
  }

  function collectSetupCommands() {
    return String(elements.setupSshCommands?.value || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function collectBootstrapSudoCommands() {
    return collectSetupCommands().filter((line) => /^sudo\b/i.test(line));
  }

  function collectSudoersPreviewPayload() {
    const bootstrapEnabled = bootstrapEnabledForSession();
    return {
      platform: elements.setupPlatform?.value || "core",
      service_user: elements.setupSshUser?.value?.trim() || "jbodmap",
      install_sudo_rules: bootstrapEnabled && Boolean(elements.setupBootstrapInstallSudo?.checked),
      sudo_commands: bootstrapEnabled ? collectBootstrapSudoCommands() : [],
    };
  }

  function renderSudoersPreview(preview = {}) {
    if (
      !elements.setupBootstrapSudoersName
      || !elements.setupBootstrapSudoersDetail
      || !elements.setupBootstrapSudoersPreview
    ) {
      return;
    }
    const payload = collectSudoersPreviewPayload();
    const serviceUser = String(preview.service_user || payload.service_user || "jbodmap").trim() || "jbodmap";
    const filename = String(preview.filename || `truenas-jbod-ui-${serviceUser}`).trim();
    const detail = String(preview.detail || "").trim();
    const content = String(preview.content || "").trimEnd();

    elements.setupBootstrapSudoersName.textContent = filename;
    elements.setupBootstrapSudoersDetail.textContent =
      detail
      || "This is the exact command-limited sudoers content bootstrap would write for the final service account.";
    elements.setupBootstrapSudoersPreview.textContent =
      content || "# No sudoers preview available.\n";
  }

  function scheduleSudoersPreviewRefresh(delay = 180) {
    if (!elements.setupBootstrapSudoersPreview) {
      return;
    }
    if (state.sudoersPreviewTimerId) {
      window.clearTimeout(state.sudoersPreviewTimerId);
    }
    state.sudoersPreviewTimerId = window.setTimeout(() => {
      state.sudoersPreviewTimerId = null;
      void refreshSudoersPreview();
    }, Math.max(0, delay));
  }

  async function refreshSudoersPreview() {
    if (!elements.setupBootstrapSudoersPreview) {
      return;
    }
    const payload = collectSudoersPreviewPayload();
    if (!bootstrapEnabledForSession()) {
      renderSudoersPreview({
        service_user: payload.service_user,
        detail: "Enable One-Time Bootstrap when you want to preview the exact sudoers file for a bootstrap run.",
        content: "# One-Time Bootstrap is disabled for this edit session.\n# Enable it to preview the command-limited sudoers file.\n",
      });
      return;
    }
    const requestSeq = (state.sudoersPreviewRequestSeq || 0) + 1;
    state.sudoersPreviewRequestSeq = requestSeq;
    try {
      const result = await fetchJson("/api/admin/system-setup/sudoers-preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (requestSeq !== state.sudoersPreviewRequestSeq) {
        return;
      }
      renderSudoersPreview({
        ...result,
        service_user: payload.service_user,
      });
    } catch (error) {
      if (requestSeq !== state.sudoersPreviewRequestSeq) {
        return;
      }
      renderSudoersPreview({
        service_user: payload.service_user,
        detail: `Unable to build the sudoers preview right now: ${error.message || error}`,
        content: `# Unable to build the sudoers preview\n# ${error.message || error}\n`,
      });
    }
  }

  function normalizeConnectionHost(value) {
    const rawValue = String(value || "").trim();
    if (!rawValue) {
      return "";
    }
    let normalized = rawValue.replace(/^(https?|ssh):\/\//i, "");
    normalized = normalized.split("/")[0];
    if (normalized.startsWith("[") && normalized.includes("]")) {
      return normalized.slice(1, normalized.indexOf("]"));
    }
    const colonCount = (normalized.match(/:/g) || []).length;
    if (colonCount === 1) {
      return normalized.split(":")[0];
    }
    return normalized;
  }

  function suggestedConnectionHost() {
    const explicitSshHost = normalizeConnectionHost(elements.setupSshHost?.value);
    if (explicitSshHost) {
      return explicitSshHost;
    }
    return normalizeConnectionHost(elements.setupTruenasHost?.value);
  }

  function collectTlsServerName() {
    return elements.setupTlsServerName?.value?.trim() || "";
  }

  function suggestedTlsServerNames() {
    const names = new Set();
    const leaf = state.tlsInspection?.leaf || {};
    (Array.isArray(leaf.san_dns) ? leaf.san_dns : []).forEach((value) => {
      const cleaned = String(value || "").trim();
      if (cleaned) {
        names.add(cleaned);
      }
    });
    const subject = String(leaf.subject || "");
    const match = subject.match(/(?:^|,)CN=([^,]+)/i);
    if (match?.[1]) {
      names.add(match[1].trim());
    }
    return Array.from(names);
  }

  function normalizeKeyMode(value) {
    return value === "generate" || value === "manual" ? value : "reuse";
  }

  function normalizeKeyName(value) {
    return String(value || "")
      .trim()
      .replace(/[^a-zA-Z0-9._-]+/g, "-")
      .replace(/^[._-]+|[._-]+$/g, "")
      .toLowerCase()
      .slice(0, 128);
  }

  function suggestedKeyName() {
    const idCandidate = normalizeKeyName(elements.setupSystemId?.value);
    const labelCandidate = normalizeKeyName(elements.setupSystemLabel?.value);
    const hostCandidate = normalizeKeyName(
      String(elements.setupTruenasHost?.value || "")
        .replace(/^https?:\/\//i, "")
        .split("/")[0]
        .split(":")[0]
    );
    if (idCandidate) {
      return `id_${idCandidate}`;
    }
    if (labelCandidate) {
      return `id_${labelCandidate}`;
    }
    if (hostCandidate) {
      return `id_${hostCandidate}`;
    }
    return "id_truenas";
  }

  function getSshKeyByName(name) {
    return state.sshKeys.find((key) => key.name === name) || null;
  }

  function renderSshKeyOptions(preferredName = "") {
    if (!elements.setupSshExistingKey) {
      return;
    }
    if (!state.sshKeys.length) {
      elements.setupSshExistingKey.innerHTML = '<option value="">No SSH keys found under config/ssh</option>';
      elements.setupSshExistingKey.value = "";
      return;
    }
    const options = state.sshKeys.map((key) => {
      const label = `${key.name} (${key.algorithm || "ssh"})`;
      return `<option value="${escapeHtml(key.name)}">${escapeHtml(label)}</option>`;
    });
    elements.setupSshExistingKey.innerHTML = options.join("");
    const selectedName = preferredName && getSshKeyByName(preferredName) ? preferredName : state.sshKeys[0].name;
    elements.setupSshExistingKey.value = selectedName;
  }

  function applySelectedKey() {
    if (!elements.setupSshKeyPath || normalizeKeyMode(elements.setupSshKeyMode?.value) !== "reuse") {
      return;
    }
    const selectedKey = getSshKeyByName(elements.setupSshExistingKey?.value);
    if (!selectedKey) {
      return;
    }
    elements.setupSshKeyPath.value = selectedKey.runtime_private_path || selectedKey.private_path || elements.setupSshKeyPath.value;
  }

  function syncKeyHelp() {
    if (!elements.setupSshKeyHelp) {
      return;
    }
    if (!elements.setupSshEnabled?.checked) {
      elements.setupSshKeyHelp.textContent = "SSH key controls unlock when SSH enrichment is enabled for this system.";
      return;
    }
    if (state.sshKeysLoading) {
      elements.setupSshKeyHelp.textContent = "Loading SSH key pairs from config/ssh...";
      return;
    }
    const mode = normalizeKeyMode(elements.setupSshKeyMode?.value);
    if (mode === "reuse") {
      const selectedKey = getSshKeyByName(elements.setupSshExistingKey?.value);
      if (!selectedKey) {
        elements.setupSshKeyHelp.textContent = "No reusable keys were found yet. Generate one here or switch to a manual path.";
        return;
      }
      elements.setupSshKeyHelp.textContent = `Using ${selectedKey.runtime_private_path || selectedKey.private_path} (${selectedKey.fingerprint}).`;
      return;
    }
    if (mode === "generate") {
      const generatedName = normalizeKeyName(elements.setupGenerateKeyName?.value) || suggestedKeyName();
      elements.setupSshKeyHelp.textContent = `New Ed25519 key pairs are written under config/ssh and become available at /run/ssh immediately. Suggested name: ${generatedName}.`;
      return;
    }
    elements.setupSshKeyHelp.textContent = `Manual mode leaves the key path editable. Current path: ${elements.setupSshKeyPath?.value || "/run/ssh/id_truenas"}.`;
  }

  function syncKeyMode() {
    const mode = normalizeKeyMode(elements.setupSshKeyMode?.value);
    if (elements.setupSshKeyMode) {
      elements.setupSshKeyMode.value = mode;
    }
    if (elements.setupReuseKeyPanel) {
      elements.setupReuseKeyPanel.classList.toggle("hidden", mode !== "reuse");
    }
    if (elements.setupGenerateKeyPanel) {
      elements.setupGenerateKeyPanel.classList.toggle("hidden", mode !== "generate");
    }
    if (elements.setupManualKeyPanel) {
      elements.setupManualKeyPanel.classList.toggle("hidden", mode !== "manual");
    }
    if (elements.setupSshKeyPath) {
      elements.setupSshKeyPath.readOnly = mode !== "manual";
    }
    if (elements.setupGenerateKeyName && mode === "generate" && !elements.setupGenerateKeyName.value.trim()) {
      elements.setupGenerateKeyName.value = suggestedKeyName();
    }
    renderSshKeyOptions(elements.setupSshExistingKey?.value || "");
    applySelectedKey();
    syncKeyHelp();
  }

  function syncSshFields() {
    const enabled = Boolean(elements.setupSshEnabled?.checked);
    document.querySelectorAll("[data-ssh-field]").forEach((field) => {
      field.disabled = !enabled;
    });
    if (elements.setupRefreshKeysButton) {
      elements.setupRefreshKeysButton.disabled = !enabled;
    }
    if (elements.setupGenerateKeyButton) {
      elements.setupGenerateKeyButton.disabled = !enabled;
    }
    if (elements.setupLoadRecommendedButton) {
      elements.setupLoadRecommendedButton.disabled = !enabled;
    }
    if (enabled && elements.setupSshHost && !elements.setupSshHost.value.trim()) {
      elements.setupSshHost.value = suggestedConnectionHost();
    }
    if (enabled && elements.setupSshUser && !elements.setupSshUser.value.trim()) {
      elements.setupSshUser.value = "jbodmap";
    }
    if (enabled && elements.setupBootstrapHost && !elements.setupBootstrapHost.value.trim()) {
      elements.setupBootstrapHost.value = suggestedConnectionHost();
    }
    syncBootstrapFields();
    syncKeyMode();
    syncKeyHelp();
  }

  function bootstrapEnabledForSession() {
    return Boolean(elements.setupSshEnabled?.checked && elements.setupBootstrapEnabled?.checked);
  }

  function syncBootstrapFields() {
    const sshEnabled = Boolean(elements.setupSshEnabled?.checked);
    const bootstrapEnabled = bootstrapEnabledForSession();
    document.querySelectorAll("[data-bootstrap-field]").forEach((field) => {
      field.disabled = !bootstrapEnabled;
    });
    if (elements.setupBootstrapFields) {
      elements.setupBootstrapFields.classList.toggle("is-disabled", sshEnabled && !bootstrapEnabled);
    }
    if (elements.setupBootstrapResult) {
      if (!sshEnabled) {
        elements.setupBootstrapResult.textContent = "Enable SSH enrichment first if you want to use one-time bootstrap.";
      } else if (!bootstrapEnabled) {
        elements.setupBootstrapResult.textContent = "Bootstrap is off by default for saved systems. Enable it only when you intend to run one-time service-account setup.";
      }
    }
  }

  function syncBackupControls() {
    if (elements.backupPackaging) {
      const encryptEnabled = Boolean(elements.backupEncryptToggle?.checked);
      const currentPackaging = elements.backupPackaging.value || state.backupDefaults.packaging || "tar.zst";
      if (encryptEnabled) {
        if (!state.backupForced7z && currentPackaging && currentPackaging !== "7z") {
          state.backupLastPlainPackaging = currentPackaging;
        }
        elements.backupPackaging.value = "7z";
        elements.backupPackaging.disabled = true;
        state.backupForced7z = true;
      } else {
        elements.backupPackaging.disabled = false;
        if (state.backupForced7z && state.backupLastPlainPackaging && state.backupLastPlainPackaging !== "7z") {
          elements.backupPackaging.value = state.backupLastPlainPackaging;
        }
        state.backupForced7z = false;
      }
    }
    if (elements.backupExportPassphrase) {
      elements.backupExportPassphrase.disabled = !Boolean(elements.backupEncryptToggle?.checked);
    }
    if (elements.backupExportRestartToggle) {
      const stopEnabled = Boolean(elements.backupExportStopToggle?.checked);
      elements.backupExportRestartToggle.disabled = !stopEnabled;
      if (!stopEnabled) {
        elements.backupExportRestartToggle.checked = false;
      }
    }
    if (elements.backupImportRestartToggle) {
      const stopEnabled = Boolean(elements.backupImportStopToggle?.checked);
      elements.backupImportRestartToggle.disabled = !stopEnabled;
      if (!stopEnabled) {
        elements.backupImportRestartToggle.checked = false;
      }
    }
  }

  function readSelectedImportFile() {
    return elements.backupImportFile?.files?.[0] || null;
  }

  function resetSetupForm() {
    state.loadedSystemId = null;
    state.selectedProfileId = "";
    state.tlsInspection = null;
    state.storageViews = [];
    state.storageViewCandidates = [];
    state.storageViewCandidatesSystemId = null;
    state.storageViewCandidatesLoading = false;
    resetLiveEnclosureState();
    state.selectedStorageViewId = "";
    if (elements.setupSystemLabel) {
      elements.setupSystemLabel.value = "";
    }
    if (elements.setupSystemId) {
      elements.setupSystemId.value = "";
    }
    if (elements.setupPlatform) {
      elements.setupPlatform.value = "core";
    }
    if (elements.setupProfile) {
      elements.setupProfile.value = "";
    }
    if (elements.setupMakeDefault) {
      elements.setupMakeDefault.checked = false;
    }
    if (elements.setupTruenasHost) {
      elements.setupTruenasHost.value = "";
    }
    if (elements.setupVerifySsl) {
      elements.setupVerifySsl.checked = true;
    }
    if (elements.setupTlsCaBundlePath) {
      elements.setupTlsCaBundlePath.value = "";
    }
    if (elements.setupTlsServerName) {
      elements.setupTlsServerName.value = "";
    }
    if (elements.setupEnclosureFilter) {
      elements.setupEnclosureFilter.value = "";
    }
    if (elements.setupApiKey) {
      elements.setupApiKey.value = "";
    }
    if (elements.setupApiUser) {
      elements.setupApiUser.value = "";
    }
    if (elements.setupApiPassword) {
      elements.setupApiPassword.value = "";
    }
    if (elements.setupSshEnabled) {
      elements.setupSshEnabled.checked = false;
    }
    if (elements.setupSshHost) {
      elements.setupSshHost.value = "";
    }
    if (elements.setupSshUser) {
      elements.setupSshUser.value = "";
    }
    if (elements.setupSshPort) {
      elements.setupSshPort.value = "22";
    }
    if (elements.setupSshKeyMode) {
      elements.setupSshKeyMode.value = "reuse";
    }
    if (elements.setupSshKeyPath) {
      elements.setupSshKeyPath.value = "/run/ssh/id_truenas";
    }
    if (elements.setupSshPassword) {
      elements.setupSshPassword.value = "";
    }
    if (elements.setupSshSudoPassword) {
      elements.setupSshSudoPassword.value = "";
    }
    if (elements.setupSshKnownHosts) {
      elements.setupSshKnownHosts.value = "/app/data/known_hosts";
    }
    if (elements.setupSshStrictHostKey) {
      elements.setupSshStrictHostKey.checked = true;
    }
    if (elements.setupSshCommands) {
      elements.setupSshCommands.value = defaultCommands("core").join("\n");
    }
    if (elements.setupBootstrapHost) {
      elements.setupBootstrapHost.value = "";
    }
    if (elements.setupBootstrapEnabled) {
      elements.setupBootstrapEnabled.checked = false;
    }
    if (elements.setupBootstrapUser) {
      elements.setupBootstrapUser.value = "root";
    }
    if (elements.setupBootstrapPassword) {
      elements.setupBootstrapPassword.value = "";
    }
    if (elements.setupBootstrapKeyPath) {
      elements.setupBootstrapKeyPath.value = "";
    }
    if (elements.setupBootstrapSudoPassword) {
      elements.setupBootstrapSudoPassword.value = "";
    }
    if (elements.setupBootstrapInstallSudo) {
      elements.setupBootstrapInstallSudo.checked = true;
    }
    if (elements.setupTlsCaFile) {
      elements.setupTlsCaFile.value = "";
    }
    if (elements.setupTlsCaFileLabel) {
      elements.setupTlsCaFileLabel.textContent = "No file selected";
    }
    if (elements.setupTlsImportResult) {
      elements.setupTlsImportResult.textContent = "Import a PEM CA or certificate chain here when your TrueNAS or Quantastor host uses a private CA.";
    }
    if (elements.setupBootstrapResult) {
      elements.setupBootstrapResult.textContent = "Bootstrap is off by default for saved systems. Enable it only when you intend to run one-time service-account setup.";
    }
    if (elements.setupResult) {
      elements.setupResult.textContent = "Saving here updates the mounted config file; restart the read UI after a new system is added so it picks the new list up cleanly.";
    }
    syncPlatformHelp();
    syncVerifySslHelp();
    syncTlsServerNameHelp();
    renderTlsServerNameSuggestions();
    syncTlsTrustStatus();
    renderProfileOptions();
    renderProfilePreview();
    renderProfileCatalog();
    renderStorageViews();
    renderTlsInspection();
    syncSshFields();
    updateCreateButton();
    renderExistingSystems();
    scheduleSudoersPreviewRefresh(0);
  }

  function loadSystemIntoForm(system) {
    if (!system) {
      return;
    }
    state.loadedSystemId = system.id || null;
    state.selectedExistingSystemId = system.id || state.selectedExistingSystemId;
    state.selectedProfileId = system.default_profile_id || "";
    replaceStorageViewState(system.storage_views || []);
    if (elements.setupSystemLabel) {
      elements.setupSystemLabel.value = system.label || system.id || "";
    }
    if (elements.setupSystemId) {
      elements.setupSystemId.value = system.id || "";
    }
    if (elements.setupPlatform) {
      elements.setupPlatform.value = system.platform || "core";
    }
    renderProfileOptions();
    if (elements.setupProfile) {
      elements.setupProfile.value = state.selectedProfileId;
    }
    if (elements.setupMakeDefault) {
      elements.setupMakeDefault.checked = Boolean(system.is_default);
    }
    if (elements.setupTruenasHost) {
      elements.setupTruenasHost.value = system.truenas_host || "";
    }
    if (elements.setupVerifySsl) {
      elements.setupVerifySsl.checked = Boolean(system.verify_ssl);
    }
    if (elements.setupTlsCaBundlePath) {
      elements.setupTlsCaBundlePath.value = system.tls_ca_bundle_path || "";
    }
    if (elements.setupTlsServerName) {
      elements.setupTlsServerName.value = system.tls_server_name || "";
    }
    if (elements.setupEnclosureFilter) {
      elements.setupEnclosureFilter.value = system.enclosure_filter || "";
    }
    if (elements.setupApiKey) {
      elements.setupApiKey.value = system.api_key || "";
    }
    if (elements.setupApiUser) {
      elements.setupApiUser.value = system.api_user || "";
    }
    if (elements.setupApiPassword) {
      elements.setupApiPassword.value = system.api_password || "";
    }
    if (elements.setupSshEnabled) {
      elements.setupSshEnabled.checked = Boolean(system.ssh_enabled);
    }
    if (elements.setupSshHost) {
      elements.setupSshHost.value = system.ssh_host || "";
    }
    if (elements.setupSshUser) {
      elements.setupSshUser.value = system.ssh_user || "";
    }
    if (elements.setupSshPort) {
      elements.setupSshPort.value = String(system.ssh_port || 22);
    }
    if (elements.setupSshPassword) {
      elements.setupSshPassword.value = system.ssh_password || "";
    }
    if (elements.setupSshSudoPassword) {
      elements.setupSshSudoPassword.value = system.ssh_sudo_password || "";
    }
    if (elements.setupSshKnownHosts) {
      elements.setupSshKnownHosts.value = system.ssh_known_hosts_path || "/app/data/known_hosts";
    }
    if (elements.setupSshStrictHostKey) {
      elements.setupSshStrictHostKey.checked = system.ssh_strict_host_key_checking !== false;
    }
    if (elements.setupSshCommands) {
      elements.setupSshCommands.value = Array.isArray(system.ssh_commands) ? system.ssh_commands.join("\n") : "";
    }
    if (elements.setupBootstrapHost) {
      elements.setupBootstrapHost.value = "";
    }
    if (elements.setupBootstrapEnabled) {
      elements.setupBootstrapEnabled.checked = false;
    }
    if (elements.setupBootstrapPassword) {
      elements.setupBootstrapPassword.value = "";
    }
    if (elements.setupBootstrapKeyPath) {
      elements.setupBootstrapKeyPath.value = "";
    }
    if (elements.setupBootstrapSudoPassword) {
      elements.setupBootstrapSudoPassword.value = "";
    }
    state.tlsInspection = null;
    if (elements.setupTlsCaFile) {
      elements.setupTlsCaFile.value = "";
    }
    if (elements.setupTlsCaFileLabel) {
      elements.setupTlsCaFileLabel.textContent = "No file selected";
    }
    if (elements.setupTlsImportResult) {
      elements.setupTlsImportResult.textContent = system.tls_ca_bundle_path
        ? `Current custom TLS trust bundle: ${system.tls_ca_bundle_path}`
        : "Import a PEM CA or certificate chain here when your TrueNAS or Quantastor host uses a private CA.";
    }
    syncTlsTrustStatus();
    const matchingKey = state.sshKeys.find((key) =>
      [key.runtime_private_path, key.private_path].filter(Boolean).includes(system.ssh_key_path || "")
    );
    if (matchingKey && elements.setupSshKeyMode && elements.setupSshExistingKey) {
      elements.setupSshKeyMode.value = "reuse";
      renderSshKeyOptions(matchingKey.name);
      elements.setupSshExistingKey.value = matchingKey.name;
      applySelectedKey();
    } else {
      if (elements.setupSshKeyMode) {
        elements.setupSshKeyMode.value = "manual";
      }
      if (elements.setupSshKeyPath) {
        elements.setupSshKeyPath.value = system.ssh_key_path || "/run/ssh/id_truenas";
      }
    }
    syncPlatformHelp();
    syncVerifySslHelp();
    syncTlsServerNameHelp();
    renderTlsServerNameSuggestions();
    renderProfilePreview();
    renderProfileCatalog();
    renderStorageViews();
    renderTlsInspection();
    syncSshFields();
    updateCreateButton();
    renderExistingSystems();
    scheduleSudoersPreviewRefresh(0);
    if (elements.setupResult) {
      elements.setupResult.textContent = `Loaded ${system.label || system.id}. Save with the same system id to update it in place, or change the id to create a copy.`;
    }
    void fetchLiveEnclosures({ quiet: true });
    void fetchStorageViewCandidates({ quiet: true });
  }

  function collectSetupPayload() {
    const sshEnabled = Boolean(elements.setupSshEnabled?.checked);
    const sshHost = normalizeConnectionHost(elements.setupSshHost?.value) || null;
    const sshUser = elements.setupSshUser?.value?.trim() || (sshEnabled ? "jbodmap" : null);
    const normalizedSystemId = elements.setupSystemId?.value?.trim() || null;
    return {
      system_id: normalizedSystemId,
      label: elements.setupSystemLabel?.value?.trim() || "",
      platform: elements.setupPlatform?.value || "core",
      truenas_host: elements.setupTruenasHost?.value?.trim() || "",
      api_key: elements.setupApiKey?.value?.trim() || null,
      api_user: elements.setupApiUser?.value?.trim() || null,
      api_password: elements.setupApiPassword?.value || null,
      verify_ssl: Boolean(elements.setupVerifySsl?.checked),
      tls_ca_bundle_path: elements.setupTlsCaBundlePath?.value?.trim() || null,
      tls_server_name: collectTlsServerName() || null,
      enclosure_filter: elements.setupEnclosureFilter?.value?.trim() || null,
      ssh_enabled: sshEnabled,
      ssh_host: sshHost,
      ssh_port: Number(elements.setupSshPort?.value) || 22,
      ssh_user: sshUser,
      ssh_key_path: elements.setupSshKeyPath?.value?.trim() || null,
      ssh_password: elements.setupSshPassword?.value || null,
      ssh_sudo_password: elements.setupSshSudoPassword?.value || null,
      ssh_known_hosts_path: elements.setupSshKnownHosts?.value?.trim() || null,
      ssh_strict_host_key_checking: Boolean(elements.setupSshStrictHostKey?.checked),
      ssh_commands: collectSetupCommands(),
      default_profile_id: elements.setupProfile?.value || null,
      storage_views: state.storageViews
        .slice()
        .sort((left, right) => (Number(left.order) || 0) - (Number(right.order) || 0))
        .map((storageView) => ({
          id: storageView.id,
          label: storageView.label,
          kind: storageView.kind,
          template_id: storageView.template_id,
          profile_id: storageView.profile_id || null,
          enabled: Boolean(storageView.enabled),
          order: Number(storageView.order) || 10,
          render: {
            show_in_main_ui: Boolean(storageView.render?.show_in_main_ui),
            show_in_admin_ui: Boolean(storageView.render?.show_in_admin_ui),
            default_collapsed: Boolean(storageView.render?.default_collapsed),
          },
          binding: {
            mode: storageView.binding?.mode || "auto",
            enclosure_ids: Array.isArray(storageView.binding?.enclosure_ids) ? storageView.binding.enclosure_ids : [],
            pool_names: Array.isArray(storageView.binding?.pool_names) ? storageView.binding.pool_names : [],
            serials: Array.isArray(storageView.binding?.serials) ? storageView.binding.serials : [],
            pcie_addresses: Array.isArray(storageView.binding?.pcie_addresses) ? storageView.binding.pcie_addresses : [],
            device_names: Array.isArray(storageView.binding?.device_names) ? storageView.binding.device_names : [],
          },
          layout_overrides:
            (storageView.layout_overrides?.slot_labels && Object.keys(storageView.layout_overrides.slot_labels).length)
            || (storageView.layout_overrides?.slot_sizes && Object.keys(storageView.layout_overrides.slot_sizes).length)
              ? {
                  slot_labels: storageView.layout_overrides?.slot_labels || {},
                  slot_sizes: storageView.layout_overrides?.slot_sizes || {},
                }
              : null,
        })),
      replace_existing: Boolean(state.loadedSystemId && normalizedSystemId === state.loadedSystemId),
      make_default: Boolean(elements.setupMakeDefault?.checked),
    };
  }

  function resolveBootstrapServiceKey() {
    const mode = normalizeKeyMode(elements.setupSshKeyMode?.value);
    if (mode === "generate") {
      throw new Error("Generate the SSH key pair first, then run the one-time bootstrap.");
    }
    if (mode === "reuse") {
      const selectedKey = getSshKeyByName(elements.setupSshExistingKey?.value);
      if (!selectedKey?.name) {
        throw new Error("Choose an existing SSH key pair before running the bootstrap.");
      }
      return {
        service_key_name: selectedKey.name,
        service_key_path: selectedKey.runtime_private_path || selectedKey.private_path || null,
        service_public_key: selectedKey.public_key || null,
      };
    }
    const manualPath = elements.setupSshKeyPath?.value?.trim() || "";
    if (!manualPath) {
      throw new Error("Enter the SSH key path that should be installed for the final service account.");
    }
    return {
      service_key_name: null,
      service_key_path: manualPath,
      service_public_key: null,
    };
  }

  function collectBootstrapPayload() {
    if (!bootstrapEnabledForSession()) {
      throw new Error("Enable One-Time Bootstrap before running bootstrap on this host.");
    }
    const setupPayload = collectSetupPayload();
    if (!setupPayload.ssh_enabled) {
      throw new Error("Enable SSH enrichment first so the final service-account details are defined.");
    }
    const bootstrapHost =
      normalizeConnectionHost(elements.setupBootstrapHost?.value) || setupPayload.ssh_host || suggestedConnectionHost();
    if (!bootstrapHost) {
      throw new Error("Enter an SSH host before running the one-time bootstrap.");
    }
    return {
      platform: setupPayload.platform,
      host: bootstrapHost,
      port: setupPayload.ssh_port || 22,
      bootstrap_user: elements.setupBootstrapUser?.value?.trim() || "root",
      bootstrap_password: elements.setupBootstrapPassword?.value || null,
      bootstrap_sudo_password: elements.setupBootstrapSudoPassword?.value || null,
      bootstrap_key_path: elements.setupBootstrapKeyPath?.value?.trim() || null,
      bootstrap_known_hosts_path: setupPayload.ssh_known_hosts_path || "/app/data/known_hosts",
      bootstrap_strict_host_key_checking: Boolean(setupPayload.ssh_strict_host_key_checking),
      timeout_seconds: 15,
      service_user: setupPayload.ssh_user || "jbodmap",
      service_shell: "/bin/sh",
      install_sudo_rules: Boolean(elements.setupBootstrapInstallSudo?.checked),
      sudo_commands: collectBootstrapSudoCommands(),
      ...resolveBootstrapServiceKey(),
    };
  }

  async function readJsonResponse(response) {
    try {
      return await response.json();
    } catch (error) {
      return null;
    }
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, options);
    const payload = await readJsonResponse(response);
    if (!response.ok || (payload && payload.ok === false)) {
      throw new Error(payload?.detail || `Request failed with ${response.status}`);
    }
    return payload || {};
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

  function resolveDownloadFilename(response, fallbackName) {
    const contentDisposition = response.headers.get("Content-Disposition") || "";
    const match = contentDisposition.match(/filename="([^"]+)"/i);
    return match ? match[1] : fallbackName;
  }

  function collectTlsTargetHost() {
    return elements.setupTruenasHost?.value?.trim() || "";
  }

  function readTextFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error || new Error("Unable to read the selected PEM file."));
      reader.readAsText(file);
    });
  }

  async function inspectTlsCertificate() {
    const host = collectTlsTargetHost();
    if (!host) {
      setBanner("Enter the HTTPS host first so the admin sidecar knows which certificate to inspect.", "error");
      return;
    }
    if (elements.setupInspectTlsButton) {
      elements.setupInspectTlsButton.disabled = true;
    }
    if (elements.setupTlsInspectionResult) {
      elements.setupTlsInspectionResult.textContent = `Inspecting the presented TLS certificate details for ${host}...`;
    }
    try {
      const payload = await fetchJson("/api/admin/tls/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host,
          timeout_seconds: 10,
          tls_server_name: collectTlsServerName() || null,
        }),
      });
      state.tlsInspection = payload.inspection || null;
      renderTlsInspection();
      syncTlsServerNameHelp();
      renderTlsServerNameSuggestions();
      if (elements.setupTlsInspectionResult) {
        elements.setupTlsInspectionResult.textContent = `Inspected ${payload.inspection?.host || host}. Compare the fingerprints before trusting or importing anything.`;
      }
      setBanner(`Fetched the presented TLS certificate details for ${payload.inspection?.host || host}.`, "success");
    } catch (error) {
      if (elements.setupTlsInspectionResult) {
        elements.setupTlsInspectionResult.textContent = `TLS inspection failed: ${error.message || error}`;
      }
      setBanner(`TLS inspection failed: ${error.message || error}`, "error");
    } finally {
      if (elements.setupInspectTlsButton) {
        elements.setupInspectTlsButton.disabled = false;
      }
    }
  }

  async function trustRemoteTlsCertificate() {
    const host = collectTlsTargetHost();
    if (!host) {
      setBanner("Enter the HTTPS host first so the admin sidecar knows which remote certificate material to save for verified connections.", "error");
      return;
    }
    if (elements.setupTrustRemoteTlsButton) {
      elements.setupTrustRemoteTlsButton.disabled = true;
    }
    if (elements.setupTlsImportResult) {
      elements.setupTlsImportResult.textContent = `Saving the presented remote certificate material for ${host}...`;
    }
    try {
      const payload = await fetchJson("/api/admin/tls/trust-remote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host,
          timeout_seconds: 10,
          system_id: elements.setupSystemId?.value?.trim() || null,
          bundle_name: suggestedTlsBundleName(),
          tls_server_name: collectTlsServerName() || null,
        }),
      });
      state.tlsInspection = payload.inspection || state.tlsInspection;
      if (elements.setupTlsCaBundlePath) {
        elements.setupTlsCaBundlePath.value = payload.bundle_path || "";
      }
      if (elements.setupVerifySsl) {
        elements.setupVerifySsl.checked = true;
      }
      renderTlsInspection();
      syncVerifySslHelp();
      syncTlsServerNameHelp();
      renderTlsServerNameSuggestions();
      const trusted = syncTlsTrustStatus(payload.validation || null);
      const validationDetail = buildTlsValidationSuggestion(payload.validation);
      if (elements.setupTlsImportResult) {
        elements.setupTlsImportResult.textContent = trusted
          ? `Saved the presented remote certificate material to ${payload.bundle_path} and verified it for ${payload.validation?.host || host}.`
          : `Saved the presented remote certificate material to ${payload.bundle_path}, but the validation check still needs attention. ${validationDetail || ""}`.trim();
      }
      setBanner(
        trusted
          ? `Saved and verified the presented remote certificate material for ${host}.`
          : `Saved the presented remote certificate material for ${host}, but it has not validated cleanly yet.`
      , trusted ? "success" : "info");
    } catch (error) {
      syncTlsTrustStatus();
      if (elements.setupTlsImportResult) {
        elements.setupTlsImportResult.textContent = `Saving the remote certificate material failed: ${error.message || error}`;
      }
      setBanner(`Saving the remote certificate material failed: ${error.message || error}`, "error");
    } finally {
      if (elements.setupTrustRemoteTlsButton) {
        elements.setupTrustRemoteTlsButton.disabled = false;
      }
    }
  }

  async function importTlsBundle() {
    const file = elements.setupTlsCaFile?.files?.[0] || null;
    if (!file) {
      setBanner("Choose a PEM bundle before importing it into the local trust store.", "error");
      return;
    }
    if (elements.setupTlsImportCaButton) {
      elements.setupTlsImportCaButton.disabled = true;
    }
    if (elements.setupTlsImportResult) {
      elements.setupTlsImportResult.textContent = `Importing ${file.name} into the local TLS trust store...`;
    }
    try {
      const pemText = await readTextFile(file);
      const payload = await fetchJson("/api/admin/tls/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          pem_text: pemText,
          host: collectTlsTargetHost() || null,
          system_id: elements.setupSystemId?.value?.trim() || null,
          bundle_name: suggestedTlsBundleName(),
          tls_server_name: collectTlsServerName() || null,
        }),
      });
      if (elements.setupTlsCaBundlePath) {
        elements.setupTlsCaBundlePath.value = payload.bundle_path || "";
      }
      if (elements.setupVerifySsl) {
        elements.setupVerifySsl.checked = true;
      }
      syncVerifySslHelp();
      const trusted = syncTlsTrustStatus(payload.validation || null);
      const validationDetail = buildTlsValidationSuggestion(payload.validation);
      if (elements.setupTlsImportResult) {
        elements.setupTlsImportResult.textContent = trusted
          ? `Imported ${payload.certificate_count} certificate${payload.certificate_count === 1 ? "" : "s"} into ${payload.bundle_path} and verified it for ${payload.validation?.host || collectTlsTargetHost() || "this host"}.`
          : `Imported ${payload.certificate_count} certificate${payload.certificate_count === 1 ? "" : "s"} into ${payload.bundle_path}. ${validationDetail || defaultTlsTrustDetail({ bundlePath: payload.bundle_path })}`.trim();
      }
      setBanner(
        trusted
          ? `Imported and verified PEM trust bundle from ${file.name}.`
          : `Imported PEM trust bundle from ${file.name}.`
      , trusted ? "success" : "info");
    } catch (error) {
      syncTlsTrustStatus();
      if (elements.setupTlsImportResult) {
        elements.setupTlsImportResult.textContent = `TLS bundle import failed: ${error.message || error}`;
      }
      setBanner(`TLS bundle import failed: ${error.message || error}`, "error");
    } finally {
      if (elements.setupTlsImportCaButton) {
        elements.setupTlsImportCaButton.disabled = false;
      }
    }
  }

  async function refreshState({ quiet = false } = {}) {
    if (state.refreshInFlight) {
      return;
    }
    state.refreshInFlight = true;
    if (elements.refreshStateButton) {
      elements.refreshStateButton.disabled = true;
    }
    if (!quiet) {
      setBanner("Refreshing admin sidecar state...");
    }
    try {
      const payload = await fetchJson("/api/admin/state");
      state.admin = payload.admin || {};
      state.systems = Array.isArray(payload.systems) ? payload.systems : [];
      state.defaultSystemId = payload.default_system_id || null;
      state.profiles = Array.isArray(payload.profiles) ? payload.profiles : [];
      state.storageViewTemplates = Array.isArray(payload.storage_view_templates) ? payload.storage_view_templates : [];
      state.platformDefaults = payload.setup_platform_defaults || {};
      state.sshKeys = Array.isArray(payload.ssh_keys) ? payload.ssh_keys : [];
      state.runtime = payload.runtime || { available: false, detail: null, containers: [] };
      state.backupDefaults = payload.backup_defaults || state.backupDefaults;
      state.paths = payload.paths || state.paths;
      renderAll();
      if (state.loadedSystemId) {
        void fetchLiveEnclosures({ quiet: true });
        void fetchStorageViewCandidates({ quiet: true });
      }
      if (!quiet) {
        setBanner("Admin sidecar state refreshed.", "success");
      }
    } catch (error) {
      setBanner(`Unable to refresh admin state: ${error.message || error}`, "error");
    } finally {
      state.refreshInFlight = false;
      if (elements.refreshStateButton) {
        elements.refreshStateButton.disabled = false;
      }
    }
  }

  async function runRuntimeAction(containerKey, action) {
    const verb =
      action === "stop"
        ? "Stopping"
        : action === "restart"
          ? "Restarting"
          : "Starting";
    setBanner(`${verb} ${containerKey} container...`);
    try {
      const payload = await fetchJson(`/api/admin/runtime/containers/${encodeURIComponent(containerKey)}/${action}`, {
        method: "POST",
      });
      state.runtime = payload.runtime || state.runtime;
      renderRuntimeCards();
      setBanner(`${verb} ${containerKey} container completed.`, "success");
    } catch (error) {
      setBanner(`Container ${action} failed: ${error.message || error}`, "error");
    }
  }

  async function exportBackup() {
    const encrypt = Boolean(elements.backupEncryptToggle?.checked);
    const passphrase = readOptionalSecretValue(elements.backupExportPassphrase);
    const packaging = elements.backupPackaging?.value || "tar.zst";
    if (encrypt && !passphrase) {
      setBanner("Enter a passphrase before exporting an encrypted backup.", "error");
      return;
    }
    if (elements.backupExportButton) {
      elements.backupExportButton.disabled = true;
    }
    if (elements.backupExportResult) {
      elements.backupExportResult.textContent = "Preparing backup bundle...";
    }
    try {
      const stopServices = Boolean(elements.backupExportStopToggle?.checked);
      const restartServices = Boolean(elements.backupExportRestartToggle?.checked);
      const response = await fetch(
        `/api/admin/backup/export?stop_services=${String(stopServices)}&restart_services=${String(restartServices)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            encrypt,
            passphrase,
            packaging,
          }),
        }
      );
      if (!response.ok) {
        const payload = await readJsonResponse(response);
        throw new Error(payload?.detail || `Request failed with ${response.status}`);
      }
      const blob = await response.blob();
      const objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      const actualPackaging = response.headers.get("X-Backup-Packaging") || packaging;
      anchor.download = resolveDownloadFilename(
        response,
        `jbod-system-backup${actualPackaging === "tar.zst" ? ".tar.zst" : actualPackaging === "tar.gz" ? ".tar.gz" : `.${actualPackaging}`}`
      );
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(objectUrl);
      const stopped = response.headers.get("X-Admin-Stopped-Containers") || "none";
      const restarted = response.headers.get("X-Admin-Restarted-Containers") || "none";
      if (elements.backupExportResult) {
        elements.backupExportResult.textContent = `Exported ${actualPackaging}. Stopped: ${stopped}. Restarted: ${restarted}.`;
      }
      setBanner(`Full backup exported as ${actualPackaging}.`, "success");
      await refreshState({ quiet: true });
    } catch (error) {
      if (elements.backupExportResult) {
        elements.backupExportResult.textContent = `Export failed: ${error.message || error}`;
      }
      setBanner(`Full backup export failed: ${error.message || error}`, "error");
    } finally {
      if (elements.backupExportButton) {
        elements.backupExportButton.disabled = false;
      }
    }
  }

  async function importBackup() {
    const file = readSelectedImportFile();
    const passphrase = readOptionalSecretValue(elements.backupImportPassphrase);
    if (!file) {
      setBanner("Choose a backup file before importing.", "error");
      return;
    }
    if (elements.backupImportButton) {
      elements.backupImportButton.disabled = true;
    }
    if (elements.backupImportResult) {
      elements.backupImportResult.textContent = `Importing ${file.name}...`;
    }
    try {
      const stopServices = Boolean(elements.backupImportStopToggle?.checked);
      const restartServices = Boolean(elements.backupImportRestartToggle?.checked);
      const response = await fetch(
        `/api/admin/backup/import?stop_services=${String(stopServices)}&restart_services=${String(restartServices)}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/octet-stream",
            ...(passphrase !== null
              ? { "X-Backup-Passphrase-Base64": encodeUtf8Base64(passphrase) }
              : {}),
          },
          body: await file.arrayBuffer(),
        }
      );
      const payload = await readJsonResponse(response);
      if (!response.ok || payload?.ok === false) {
        throw new Error(payload?.detail || `Request failed with ${response.status}`);
      }
      state.systems = Array.isArray(payload.systems) ? payload.systems : state.systems;
      state.defaultSystemId = payload.default_system_id || state.defaultSystemId;
      if (elements.backupImportResult) {
        const stopped = Array.isArray(payload.stopped_containers) ? payload.stopped_containers.join(", ") || "none" : "none";
        const restarted = Array.isArray(payload.restarted_containers) ? payload.restarted_containers.join(", ") || "none" : "none";
        elements.backupImportResult.textContent = `Imported ${file.name}. Stopped: ${stopped}. Restarted: ${restarted}.`;
      }
      setBanner(`Full backup imported from ${file.name}.`, "success");
      await refreshState({ quiet: true });
    } catch (error) {
      if (elements.backupImportResult) {
        elements.backupImportResult.textContent = `Import failed: ${error.message || error}`;
      }
      setBanner(`Full backup import failed: ${error.message || error}`, "error");
    } finally {
      if (elements.backupImportButton) {
        elements.backupImportButton.disabled = false;
      }
    }
  }

  async function loadSshKeys({ quiet = false } = {}) {
    state.sshKeysLoading = true;
    syncKeyHelp();
    try {
      const payload = await fetchJson("/api/admin/ssh-keys");
      state.sshKeys = Array.isArray(payload.keys) ? payload.keys : [];
      syncKeyMode();
      if (!quiet) {
        setBanner("SSH key list refreshed.", "success");
      }
    } catch (error) {
      setBanner(`Unable to refresh SSH keys: ${error.message || error}`, "error");
    } finally {
      state.sshKeysLoading = false;
      syncKeyHelp();
    }
  }

  async function generateSshKey() {
    const desiredName = normalizeKeyName(elements.setupGenerateKeyName?.value) || suggestedKeyName();
    if (!desiredName) {
      setBanner("Enter a key name before generating a new SSH key pair.", "error");
      return;
    }
    if (elements.setupGenerateKeyName) {
      elements.setupGenerateKeyName.value = desiredName;
    }
    setBanner(`Generating SSH key pair ${desiredName}...`);
    try {
      const payload = await fetchJson("/api/admin/ssh-keys/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: desiredName }),
      });
      state.sshKeys = Array.isArray(payload.keys) ? payload.keys : state.sshKeys;
      renderSshKeyOptions(payload.key?.name || desiredName);
      if (elements.setupSshKeyMode) {
        elements.setupSshKeyMode.value = "reuse";
      }
      syncKeyMode();
      if (elements.setupSshExistingKey && payload.key?.name) {
        elements.setupSshExistingKey.value = payload.key.name;
      }
      applySelectedKey();
      setBanner(`SSH key pair ${desiredName} generated.`, "success");
    } catch (error) {
      setBanner(`SSH key generation failed: ${error.message || error}`, "error");
    }
  }

  async function bootstrapServiceAccount() {
    let payload;
    try {
      payload = collectBootstrapPayload();
    } catch (error) {
      setBanner(error.message || String(error), "error");
      if (elements.setupBootstrapResult) {
        elements.setupBootstrapResult.textContent = error.message || String(error);
      }
      return;
    }

    if (elements.setupBootstrapButton) {
      elements.setupBootstrapButton.disabled = true;
    }
    if (elements.setupBootstrapResult) {
      elements.setupBootstrapResult.textContent = `Bootstrapping ${payload.service_user} on ${payload.host}...`;
    }

    try {
      const result = await fetchJson("/api/admin/system-setup/bootstrap", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (elements.setupSshEnabled) {
        elements.setupSshEnabled.checked = true;
      }
      if (elements.setupSshHost && !elements.setupSshHost.value.trim()) {
        elements.setupSshHost.value = payload.host;
      }
      if (elements.setupBootstrapHost) {
        elements.setupBootstrapHost.value = payload.host;
      }
      if (elements.setupSshUser) {
        elements.setupSshUser.value = result.service_user || payload.service_user;
      }
      if (elements.setupSshPassword) {
        elements.setupSshPassword.value = "";
      }
      if (elements.setupSshSudoPassword) {
        elements.setupSshSudoPassword.value = "";
      }
      if (elements.setupBootstrapPassword) {
        elements.setupBootstrapPassword.value = "";
      }
      if (elements.setupBootstrapSudoPassword) {
        elements.setupBootstrapSudoPassword.value = "";
      }
      syncSshFields();
      maybeLoadRecommendedCommands();
      scheduleSudoersPreviewRefresh(0);
      if (elements.setupBootstrapResult) {
        const sudoState = result.sudo_rules_installed ? `Sudoers: ${result.sudoers_path || "installed"}.` : "Sudo rules skipped.";
        elements.setupBootstrapResult.textContent = `${result.detail || `Provisioned ${result.service_user || payload.service_user}.`} ${sudoState}`;
      }
      if (elements.setupResult) {
        elements.setupResult.textContent = `Bootstrap finished for ${result.service_user || payload.service_user}. Save the system entry when you are ready to persist the final key-based connection details.`;
      }
      setBanner(`Bootstrap complete for ${result.service_user || payload.service_user}.`, "success");
    } catch (error) {
      if (elements.setupBootstrapResult) {
        elements.setupBootstrapResult.textContent = `Bootstrap failed: ${error.message || error}`;
      }
      setBanner(`Bootstrap failed: ${error.message || error}`, "error");
    } finally {
      if (elements.setupBootstrapButton) {
        elements.setupBootstrapButton.disabled = false;
      }
    }
  }

  async function createSystem() {
    const payload = collectSetupPayload();
    if (!payload.label || !payload.truenas_host) {
      setBanner("System label and host are required before saving.", "error");
      return;
    }
    if (elements.setupCreateButton) {
      elements.setupCreateButton.disabled = true;
    }
    if (elements.setupResult) {
      elements.setupResult.textContent = `Creating system entry for ${payload.label}...`;
    }
    try {
      const result = await fetchJson("/api/admin/system-setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.loadedSystemId = result.system?.id || state.loadedSystemId;
      state.selectedExistingSystemId = result.system?.id || state.selectedExistingSystemId;
      state.defaultSystemId = result.default_system_id || state.defaultSystemId;
      if (elements.setupResult) {
        elements.setupResult.textContent = result.detail || `${result.updated_existing ? "Updated" : "Created"} ${result.system?.label || payload.label}. Restart the read UI to load the updated config cleanly.`;
      }
      updateCreateButton();
      setBanner(`${result.updated_existing ? "Updated" : "Created"} system ${result.system?.label || payload.label}.`, "success");
      await refreshState({ quiet: true });
      void fetchStorageViewCandidates({ quiet: true });
    } catch (error) {
      if (elements.setupResult) {
        elements.setupResult.textContent = `System setup failed: ${error.message || error}`;
      }
      setBanner(`System setup failed: ${error.message || error}`, "error");
    } finally {
      if (elements.setupCreateButton) {
        elements.setupCreateButton.disabled = false;
      }
    }
  }

  function renderAll() {
    updateAdminMeta();
    renderBackupPaths();
    renderRuntimeCards();
    renderExistingSystems();
    renderProfileOptions();
    renderProfilePreview();
    renderProfileCatalog();
    renderStorageViews();
    syncPlatformHelp();
    syncVerifySslHelp();
    syncTlsServerNameHelp();
    renderTlsServerNameSuggestions();
    renderTlsTrustStatus();
    renderTlsInspection();
    syncBackupControls();
    syncSshFields();
    updateCreateButton();
    scheduleSudoersPreviewRefresh(0);
  }

  function bindEvents() {
    elements.refreshStateButton?.addEventListener("click", () => {
      void refreshState();
    });

    elements.runtimeCards?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-runtime-action]");
      if (!button) {
        return;
      }
      void runRuntimeAction(button.dataset.containerKey, button.dataset.runtimeAction);
    });

    elements.backupEncryptToggle?.addEventListener("change", syncBackupControls);
    elements.backupPackaging?.addEventListener("change", () => {
      if (!elements.backupEncryptToggle?.checked && elements.backupPackaging?.value && elements.backupPackaging.value !== "7z") {
        state.backupLastPlainPackaging = elements.backupPackaging.value;
      }
    });
    elements.backupExportStopToggle?.addEventListener("change", syncBackupControls);
    elements.backupImportStopToggle?.addEventListener("change", syncBackupControls);
    elements.backupExportButton?.addEventListener("click", () => {
      void exportBackup();
    });
    elements.backupImportPickButton?.addEventListener("click", () => {
      elements.backupImportFile?.click();
    });
    elements.backupImportFile?.addEventListener("change", () => {
      const file = readSelectedImportFile();
      if (elements.backupImportFileLabel) {
        elements.backupImportFileLabel.textContent = file ? file.name : "No file selected";
      }
    });
    elements.backupImportButton?.addEventListener("click", () => {
      void importBackup();
    });

    elements.setupPlatform?.addEventListener("change", () => {
      syncPlatformHelp();
      maybeLoadRecommendedCommands();
      updateCreateButton();
      scheduleSudoersPreviewRefresh();
    });
    elements.setupVerifySsl?.addEventListener("change", () => {
      syncVerifySslHelp();
      syncTlsTrustStatus();
    });
    elements.setupTlsCaBundlePath?.addEventListener("input", () => {
      syncVerifySslHelp();
      syncTlsTrustStatus();
    });
    elements.setupTlsServerName?.addEventListener("input", () => {
      syncVerifySslHelp();
      syncTlsServerNameHelp();
      renderTlsServerNameSuggestions();
      syncTlsTrustStatus();
    });
    elements.setupTlsServerNameSuggestions?.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-tls-server-name]") : null;
      if (!button || !elements.setupTlsServerName) {
        return;
      }
      elements.setupTlsServerName.value = button.dataset.tlsServerName || "";
      syncVerifySslHelp();
      syncTlsServerNameHelp();
      renderTlsServerNameSuggestions();
      syncTlsTrustStatus();
    });
    elements.setupInspectTlsButton?.addEventListener("click", () => {
      void inspectTlsCertificate();
    });
    elements.setupTrustRemoteTlsButton?.addEventListener("click", () => {
      void trustRemoteTlsCertificate();
    });
    elements.setupTlsCaPickButton?.addEventListener("click", () => {
      elements.setupTlsCaFile?.click();
    });
    elements.setupTlsCaFile?.addEventListener("change", () => {
      const file = elements.setupTlsCaFile?.files?.[0] || null;
      if (elements.setupTlsCaFileLabel) {
        elements.setupTlsCaFileLabel.textContent = file ? file.name : "No file selected";
      }
    });
    elements.setupTlsImportCaButton?.addEventListener("click", () => {
      void importTlsBundle();
    });
    elements.setupProfile?.addEventListener("change", () => {
      state.selectedProfileId = elements.setupProfile?.value || "";
      renderProfilePreview();
      renderProfileCatalog();
      renderStorageViews();
    });
    elements.profileCatalog?.addEventListener("click", (event) => {
      const card = event.target.closest("[data-profile-id]");
      if (!card || !elements.setupProfile) {
        return;
      }
      state.selectedProfileId = card.dataset.profileId || "";
      elements.setupProfile.value = state.selectedProfileId;
      renderProfilePreview();
      renderProfileCatalog();
      renderStorageViews();
    });
    elements.setupStorageViewAddButton?.addEventListener("click", () => {
      addStorageView(elements.setupStorageViewTemplate?.value || state.storageViewTemplates[0]?.id || "");
    });
    elements.setupStorageViewList?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-storage-view-id]");
      if (!button) {
        return;
      }
      state.selectedStorageViewId = button.dataset.storageViewId || "";
      renderStorageViews();
    });
    elements.setupStorageViewCandidatesList?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-storage-view-candidate-id]");
      if (!button) {
        return;
      }
      const candidate = state.storageViewCandidates.find((item) => item.candidate_id === button.dataset.storageViewCandidateId);
      if (!candidate) {
        return;
      }
      applyStorageViewCandidate(candidate);
    });
    elements.setupStorageViewCandidatesRefreshButton?.addEventListener("click", () => {
      void fetchStorageViewCandidates({ force: true });
    });
    elements.setupStorageViewCandidatesAddAllButton?.addEventListener("click", applyAllStorageViewCandidates);
    elements.setupStorageViewMoveUpButton?.addEventListener("click", () => {
      moveSelectedStorageView(-1);
    });
    elements.setupStorageViewMoveDownButton?.addEventListener("click", () => {
      moveSelectedStorageView(1);
    });
    elements.setupStorageViewDuplicateButton?.addEventListener("click", duplicateSelectedStorageView);
    elements.setupStorageViewRemoveButton?.addEventListener("click", deleteSelectedStorageView);
    [
      elements.setupStorageViewLabel,
      elements.setupStorageViewId,
      elements.setupStorageViewTemplateSelect,
      elements.setupStorageViewProfile,
      elements.setupStorageViewBindingMode,
      elements.setupStorageViewOrder,
      elements.setupStorageViewEnabled,
      elements.setupStorageViewShowMain,
      elements.setupStorageViewShowAdmin,
      elements.setupStorageViewCollapsed,
      elements.setupStorageViewEnclosureIds,
      elements.setupStorageViewPoolNames,
      elements.setupStorageViewSerials,
      elements.setupStorageViewPcieAddresses,
      elements.setupStorageViewDeviceNames,
      elements.setupStorageViewSlotLabels,
      elements.setupStorageViewSlotSizes,
    ].forEach((field) => {
      if (!field) {
        return;
      }
      const eventName = field.matches("input[type='checkbox'], select") ? "change" : "input";
      field.addEventListener(eventName, () => {
        saveStorageViewEditorToState();
      });
    });
    elements.existingSystemSelect?.addEventListener("change", () => {
      state.selectedExistingSystemId = elements.existingSystemSelect?.value || "";
      renderExistingSystems();
    });
    elements.existingSystemLoadButton?.addEventListener("click", () => {
      loadSystemIntoForm(getSystemById(elements.existingSystemSelect?.value || state.selectedExistingSystemId));
    });
    elements.existingSystemResetButton?.addEventListener("click", resetSetupForm);
    elements.currentSystemsList?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-existing-system-id]");
      if (!button) {
        return;
      }
      state.selectedExistingSystemId = button.dataset.existingSystemId || "";
      if (elements.existingSystemSelect) {
        elements.existingSystemSelect.value = state.selectedExistingSystemId;
      }
      renderExistingSystems();
    });

    elements.setupSshEnabled?.addEventListener("change", () => {
      if (elements.setupSshEnabled?.checked && !state.sshKeys.length) {
        void loadSshKeys({ quiet: true });
      }
      if (elements.setupSshEnabled?.checked) {
        maybeLoadRecommendedCommands();
      }
      syncSshFields();
      scheduleSudoersPreviewRefresh();
    });
    elements.setupSshUser?.addEventListener("input", () => {
      scheduleSudoersPreviewRefresh();
    });
    elements.setupBootstrapEnabled?.addEventListener("change", () => {
      syncBootstrapFields();
      scheduleSudoersPreviewRefresh();
    });
    elements.setupBootstrapInstallSudo?.addEventListener("change", () => {
      scheduleSudoersPreviewRefresh();
    });
    elements.setupSshCommands?.addEventListener("input", () => {
      scheduleSudoersPreviewRefresh();
    });
    elements.setupSshKeyMode?.addEventListener("change", syncKeyMode);
    elements.setupSshExistingKey?.addEventListener("change", () => {
      applySelectedKey();
      syncKeyHelp();
    });
    elements.setupRefreshKeysButton?.addEventListener("click", () => {
      void loadSshKeys();
    });
    elements.setupGenerateKeyButton?.addEventListener("click", () => {
      void generateSshKey();
    });
    elements.setupBootstrapButton?.addEventListener("click", () => {
      void bootstrapServiceAccount();
    });
    elements.setupGenerateKeyName?.addEventListener("input", syncKeyHelp);
    elements.setupSshKeyPath?.addEventListener("input", syncKeyHelp);
    elements.setupTruenasHost?.addEventListener("change", () => {
      state.tlsInspection = null;
      syncTlsServerNameHelp();
      renderTlsServerNameSuggestions();
      syncTlsTrustStatus();
      renderTlsInspection();
      if (elements.setupTlsInspectionResult) {
        elements.setupTlsInspectionResult.textContent = "This fetches the presented leaf certificate, and the full chain when the runtime exposes it, without trusting anything first so you can review SHA-256 and SHA-1 fingerprints before importing.";
      }
      const suggestedHost = suggestedConnectionHost();
      if (elements.setupSshEnabled?.checked && elements.setupSshHost && !elements.setupSshHost.value.trim()) {
        elements.setupSshHost.value = suggestedHost;
      }
      if (elements.setupBootstrapHost && !elements.setupBootstrapHost.value.trim()) {
        elements.setupBootstrapHost.value = suggestedHost;
      }
      if (elements.setupSshKeyMode?.value === "generate" && elements.setupGenerateKeyName && !normalizeKeyName(elements.setupGenerateKeyName.value)) {
        elements.setupGenerateKeyName.value = suggestedKeyName();
      }
      syncKeyHelp();
    });
    elements.setupSshHost?.addEventListener("change", () => {
      if (elements.setupBootstrapHost && !elements.setupBootstrapHost.value.trim()) {
        elements.setupBootstrapHost.value = suggestedConnectionHost();
      }
    });
    [elements.setupSystemLabel, elements.setupSystemId].forEach((field) => {
      field?.addEventListener("input", () => {
        if (elements.setupSshKeyMode?.value === "generate" && elements.setupGenerateKeyName && !normalizeKeyName(elements.setupGenerateKeyName.value)) {
          elements.setupGenerateKeyName.value = suggestedKeyName();
        }
        syncKeyHelp();
        updateCreateButton();
        renderExistingSystems();
      });
    });
    elements.setupLoadRecommendedButton?.addEventListener("click", () => {
      maybeLoadRecommendedCommands(true);
    });
    elements.setupCreateButton?.addEventListener("click", () => {
      void createSystem();
    });
  }

  if (elements.backupExportStopToggle) {
    elements.backupExportStopToggle.checked = Boolean(state.backupDefaults.stop_services);
  }
  if (elements.backupExportRestartToggle) {
    elements.backupExportRestartToggle.checked = Boolean(state.backupDefaults.restart_services);
  }
  if (elements.backupImportStopToggle) {
    elements.backupImportStopToggle.checked = Boolean(state.backupDefaults.import_stop_services);
  }
  if (elements.backupImportRestartToggle) {
    elements.backupImportRestartToggle.checked = Boolean(state.backupDefaults.import_restart_services);
  }
  if (elements.backupPackaging) {
    elements.backupPackaging.value = state.backupDefaults.packaging || "tar.zst";
    if (elements.backupPackaging.value !== "7z") {
      state.backupLastPlainPackaging = elements.backupPackaging.value;
    }
  }
  if (elements.setupPlatform) {
    elements.setupPlatform.value = "core";
  }
  if (elements.setupSshKeyMode) {
    elements.setupSshKeyMode.value = "reuse";
  }
  if (elements.setupGenerateKeyName) {
    elements.setupGenerateKeyName.value = suggestedKeyName();
  }
  if (elements.setupBootstrapUser && !elements.setupBootstrapUser.value.trim()) {
    elements.setupBootstrapUser.value = "root";
  }
  if (elements.setupBootstrapInstallSudo) {
    elements.setupBootstrapInstallSudo.checked = true;
  }
  if (elements.setupBootstrapEnabled) {
    elements.setupBootstrapEnabled.checked = false;
  }

  bindEvents();
  renderAll();
  maybeLoadRecommendedCommands();
  startCountdownTimer();
})();
