"use strict";

(() => {
  const status = document.getElementById("history-refresh-status");
  const fastButton = document.getElementById("history-refresh-fast");
  const fullButton = document.getElementById("history-refresh-full");
  const collectorBanner = document.getElementById("collector-activity-banner");
  const buttons = [fastButton, fullButton].filter(Boolean);

  function readInitialOverview() {
    const bootstrap = document.getElementById("history-dashboard-bootstrap");
    if (!bootstrap) {
      return null;
    }
    return JSON.parse(bootstrap.textContent || "null");
  }

  function formatDuration(totalSeconds) {
    const seconds = Math.max(0, Number(totalSeconds || 0));
    const minutes = Math.floor(seconds / 60);
    const remainder = Math.floor(seconds % 60);
    if (minutes <= 0) {
      return `${remainder}s`;
    }
    return `${minutes}m ${remainder}s`;
  }

  function formatCount(value, estimated = false) {
    if (value === null || value === undefined) {
      return "deferred";
    }
    return `${estimated ? "~" : ""}${value}`;
  }

  function formatBytes(value) {
    let size = Math.max(0, Number(value || 0));
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let unit = units[0];
    for (const candidate of units) {
      unit = candidate;
      if (size < 1024 || candidate === units[units.length - 1]) {
        break;
      }
      size /= 1024;
    }
    return unit === "B" ? `${Math.trunc(size)} B` : `${size.toFixed(1)} ${unit}`;
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) {
      element.textContent = value;
    }
  }

  function statusValue(value, fallback = "never") {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function collectionInventoryLabel(value) {
    if (value === true) {
      return "forced";
    }
    if (value === false) {
      return "cached";
    }
    return "not recorded";
  }

  function collectionDurationLabel(value) {
    return Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)}s` : "not recorded";
  }

  function backoffLabel(seconds) {
    const remaining = Number(seconds || 0);
    return remaining > 0 ? `${Math.ceil(remaining)}s remaining` : "inactive";
  }

  function renderCollectorStatus(payload) {
    const collector = payload?.collector || payload || {};
    const stateValue = document.getElementById("collector-state-value");
    if (stateValue) {
      stateValue.textContent = collector.collector_running ? "Running" : "Stopped";
      stateValue.classList.toggle("status-ok", Boolean(collector.collector_running));
      stateValue.classList.toggle("status-error", !collector.collector_running);
    }
    const currentCollection = collector.collection_running
      ? `${collector.collection_kind || "background"} for ${formatDuration(collector.collection_elapsed_seconds)}: ${collector.collection_activity || "working"}`
      : "not running";
    setText("status-current-collection", currentCollection);
    setText("status-source-base-url", statusValue(collector.source_base_url, "unknown"));
    setText("status-sqlite-path", statusValue(collector.sqlite_path, "unknown"));
    setText("status-last-inventory-at", statusValue(collector.last_inventory_at));
    setText("status-last-fast-metrics-at", statusValue(collector.last_fast_metrics_at));
    setText("status-last-slow-metrics-at", statusValue(collector.last_slow_metrics_at));
    setText("status-last-backup-at", statusValue(collector.last_backup_at));
    setText("status-last-retention-at", statusValue(collector.last_retention_at));
    setText("status-last-retention-duration", collectionDurationLabel(collector.last_retention_duration_seconds));
    setText("status-last-retention-rows-removed", String(collector.last_retention_rows_removed || 0));
    setText("status-last-retention-has-more", collector.last_retention_has_more ? "yes" : "no");
    setText("status-last-retention-error", statusValue(collector.last_retention_error, "none"));
    setText("status-last-collection-duration", collectionDurationLabel(collector.last_collection_duration_seconds));
    setText("status-last-background-overrun", collectionDurationLabel(collector.last_background_overrun_seconds));
    setText("status-last-collection-inventory", collectionInventoryLabel(collector.last_collection_inventory_forced));
    setText("status-next-collection-at", statusValue(collector.next_collection_at, "not scheduled"));
    setText("status-background-failures", String(collector.background_consecutive_failures || 0));
    setText("status-background-backoff", backoffLabel(collector.background_backoff_seconds_remaining));
    setText("status-background-backoff-until", statusValue(collector.background_backoff_until, "not active"));
    setText("status-last-error", statusValue(collector.last_error, "none"));
  }

  function renderOverview(payload) {
    if (!payload) {
      return;
    }
    renderCollectorStatus(payload);
    const counts = payload.counts || {};
    const countsExact = Boolean(payload.counts_exact);
    setText("tracked-slots-value", statusValue(counts.tracked_slots, "0"));
    setText("slot-events-value", formatCount(counts.event_count, !countsExact));
    setText("metric-samples-value", formatCount(counts.metric_sample_count, !countsExact));
    setText("metric-rollups-value", formatCount(counts.metric_rollup_count, !countsExact));
    setText("db-size-value", formatBytes(payload.database?.size_bytes ?? payload.database_size_bytes));
    renderScopes(payload.scopes || []);
  }

  function renderScopes(scopes) {
    const body = document.getElementById("tracked-scopes-body");
    if (!body) {
      return;
    }
    const rows = Array.isArray(scopes) ? scopes : [];
    if (!rows.length) {
      body.innerHTML = "<tr><td colspan='6'>No slot history has been collected yet.</td></tr>";
      return;
    }
    body.replaceChildren(...rows.map((scope) => {
      const row = document.createElement("tr");
      const cells = [
        statusValue(scope.system_label || scope.system_id, "unknown"),
        statusValue(scope.enclosure_label || scope.enclosure_id, "default"),
        statusValue(scope.tracked_slots, "0"),
        formatCount(scope.event_count),
        formatCount(scope.metric_sample_count),
        statusValue(scope.last_seen_at, "never"),
      ];
      for (const value of cells) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      }
      return row;
    }));
  }

  function renderCollectorBanner(payload) {
    if (!collectorBanner) {
      return;
    }
    const collector = payload?.collector || payload || {};
    if (collector.collection_running) {
      const kind = collector.collection_kind || "background";
      const activity = collector.collection_activity || "working";
      const elapsed = formatDuration(collector.collection_elapsed_seconds);
      collectorBanner.textContent = `History ${kind} collection running for ${elapsed}: ${activity}.`;
      collectorBanner.hidden = false;
      return;
    }
    const backoffRemaining = Number(collector.background_backoff_seconds_remaining || 0);
    if (backoffRemaining > 0) {
      collectorBanner.textContent = `History background collection is backed off for ${formatDuration(backoffRemaining)} after repeated failures.`;
      collectorBanner.hidden = false;
      return;
    }
    collectorBanner.hidden = true;
    collectorBanner.textContent = "";
  }

  async function pollCollectorStatus() {
    try {
      const response = await fetch("/healthz", { cache: "no-store" });
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      renderCollectorBanner(payload);
      renderCollectorStatus(payload);
    } catch (_error) {
      // Keep the current banner state if a transient poll fails.
    }
  }

  async function pollOverviewStatus() {
    try {
      const response = await fetch("/api/history/overview", { cache: "no-store" });
      if (!response.ok) {
        return;
      }
      renderOverview(await response.json());
    } catch (_error) {
      // The cheap health poll still keeps the live collector status moving.
    }
  }

  async function runRefresh(mode) {
    buttons.forEach((button) => button.disabled = true);
    if (status) {
      status.textContent = mode === "full"
        ? "Running full history refresh..."
        : "Running fast history refresh...";
    }
    try {
      const response = await fetch(`/api/history/refresh?mode=${encodeURIComponent(mode)}`, {
        method: "POST",
      });
      const body = await response.text();
      let payload = {};
      try {
        payload = body ? JSON.parse(body) : {};
      } catch (_error) {
        payload = { detail: body || `HTTP ${response.status}` };
      }
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.detail || `Refresh failed with ${response.status}`);
      }
      if (status) {
        status.textContent = payload.detail || "History refresh completed.";
      }
      renderOverview(payload);
      buttons.forEach((button) => button.disabled = false);
    } catch (error) {
      if (status) {
        status.textContent = `Refresh failed: ${error.message || error}`;
      }
      buttons.forEach((button) => button.disabled = false);
    }
  }

  const initialCollectorStatus = readInitialOverview();
  renderCollectorBanner(initialCollectorStatus);
  window.setInterval(pollCollectorStatus, 2000);
  window.setInterval(pollOverviewStatus, 10000);
  window.__HISTORY_DASHBOARD_POLL = {
    pollCollectorStatus,
    pollOverviewStatus,
  };
  fastButton?.addEventListener("click", () => runRefresh("fast"));
  fullButton?.addEventListener("click", () => runRefresh("full"));
})();
