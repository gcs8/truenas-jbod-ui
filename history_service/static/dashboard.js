"use strict";

(() => {
  const status = document.getElementById("history-refresh-status");
  const fastButton = document.getElementById("history-refresh-fast");
  const fullButton = document.getElementById("history-refresh-full");
  const collectorBanner = document.getElementById("collector-activity-banner");
  const buttons = [fastButton, fullButton].filter(Boolean);
  const NOT_COUNTED_TITLE = "Not counted, to keep this page quick on a large history";

  localizeTimestamps();

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

  function formatCount(value) {
    if (value === null || value === undefined) {
      return "—";
    }
    return String(value);
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

  function relativeTimeLabel(date, now) {
    const deltaSeconds = Math.round((date.getTime() - now) / 1000);
    const magnitude = Math.abs(deltaSeconds);
    let amount;
    if (magnitude < 45) {
      return deltaSeconds <= 0 ? "just now" : "in under a minute";
    }
    if (magnitude < 3600) {
      amount = `${Math.round(magnitude / 60)} min`;
    } else if (magnitude < 86400) {
      amount = `${Math.round(magnitude / 3600)} h`;
    } else {
      amount = `${Math.round(magnitude / 86400)} d`;
    }
    return deltaSeconds < 0 ? `${amount} ago` : `in ${amount}`;
  }

  // Timestamps arrive as ISO-8601 UTC; show them in the viewer's local time like
  // the main UI does, with the age alongside so the row reads at a glance.
  function formatTimestamp(value, fallback = "never", now = Date.now()) {
    if (value === null || value === undefined || value === "") {
      return fallback;
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    const local = date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    return `${local} (${relativeTimeLabel(date, now)})`;
  }

  function setTimestamp(id, value, fallback = "never") {
    const element = document.getElementById(id);
    if (!element) {
      return;
    }
    element.textContent = formatTimestamp(value, fallback);
    if (value) {
      element.dataset.timestamp = String(value);
    } else {
      delete element.dataset.timestamp;
    }
  }

  function localizeTimestamps() {
    for (const element of document.querySelectorAll("[data-timestamp]")) {
      element.textContent = formatTimestamp(element.dataset.timestamp, element.textContent);
    }
  }

  function collectionInventoryLabel(value) {
    if (value === true) {
      return "fresh inventory";
    }
    if (value === false) {
      return "cached inventory";
    }
    return "—";
  }

  function collectionDurationLabel(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
      return "—";
    }
    return `${Number(value).toFixed(1)}s`;
  }

  function overrunLabel(value) {
    const seconds = Number(value);
    return Number.isFinite(seconds) && seconds > 0 ? `${seconds.toFixed(1)}s` : "no";
  }

  function retryLabel(collector) {
    const failures = Number(collector.background_consecutive_failures || 0);
    const remaining = Number(collector.background_backoff_seconds_remaining || 0);
    const streak = failures > 0 ? ` (${failures} ${failures === 1 ? "failure" : "failures"} so far)` : "";
    if (remaining > 0) {
      return `in ${formatDuration(remaining)}${streak}`;
    }
    return failures > 0 ? `now${streak}` : "no";
  }

  function collectorStateLabel(collector) {
    if (!collector.collector_running) {
      return "Stopped";
    }
    return collector.collector_starting ? "Starting" : "Running";
  }

  function renderCollectorStatus(payload) {
    const collector = payload?.collector || payload || {};
    const stateValue = document.getElementById("collector-state-value");
    if (stateValue) {
      stateValue.textContent = collectorStateLabel(collector);
      stateValue.classList.toggle("status-ok", Boolean(collector.collector_running));
      stateValue.classList.toggle("status-error", !collector.collector_running);
    }
    const currentCollection = collector.collection_running
      ? `${collector.collection_kind || "background"} for ${formatDuration(collector.collection_elapsed_seconds)}: ${collector.collection_activity || "working"}`
      : "no";
    setText("status-current-collection", currentCollection);
    setTimestamp("status-last-inventory-at", collector.last_inventory_at);
    setTimestamp("status-last-fast-metrics-at", collector.last_fast_metrics_at);
    setTimestamp("status-last-slow-metrics-at", collector.last_slow_metrics_at);
    setTimestamp("status-last-backup-at", collector.last_backup_at);
    setTimestamp("status-last-retention-at", collector.last_retention_at);
    setText("status-last-retention-duration", collectionDurationLabel(collector.last_retention_duration_seconds));
    setText("status-last-retention-rows-removed", String(collector.last_retention_rows_removed || 0));
    setText("status-last-retention-has-more", collector.last_retention_has_more ? "yes" : "no");
    setText("status-last-retention-error", statusValue(collector.last_retention_error, "none"));
    setText("status-last-collection-duration", collectionDurationLabel(collector.last_collection_duration_seconds));
    setText("status-last-background-overrun", overrunLabel(collector.last_background_overrun_seconds));
    setText("status-last-collection-inventory", collectionInventoryLabel(collector.last_collection_inventory_forced));
    setTimestamp("status-next-collection-at", collector.next_collection_at, "not scheduled");
    setText("status-background-retry", retryLabel(collector));
    setText("status-last-error", statusValue(collector.last_error, "none"));
  }

  function renderOverview(payload) {
    if (!payload) {
      return;
    }
    renderCollectorStatus(payload);
    const counts = payload.counts || {};
    setText("tracked-slots-value", statusValue(counts.tracked_slots, "0"));
    setText("slot-events-value", formatCount(counts.event_count));
    setText("metric-samples-value", formatCount(counts.metric_sample_count));
    setText("metric-rollups-value", formatCount(counts.metric_rollup_count));
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
        { text: statusValue(scope.system_label || scope.system_id, "unknown") },
        { text: statusValue(scope.enclosure_label || scope.enclosure_id, "default") },
        { text: statusValue(scope.tracked_slots, "0") },
        { text: formatCount(scope.event_count), counted: scope.event_count !== null && scope.event_count !== undefined },
        { text: formatCount(scope.metric_sample_count), counted: scope.metric_sample_count !== null && scope.metric_sample_count !== undefined },
        { text: formatTimestamp(scope.last_seen_at), timestamp: scope.last_seen_at },
      ];
      for (const value of cells) {
        const cell = document.createElement("td");
        cell.textContent = value.text;
        if (value.counted === false) {
          cell.title = NOT_COUNTED_TITLE;
        }
        if (value.timestamp) {
          cell.dataset.timestamp = String(value.timestamp);
        }
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
      collectorBanner.textContent = `History collection paused for ${formatDuration(backoffRemaining)} after repeated failures.`;
      collectorBanner.hidden = false;
      return;
    }
    collectorBanner.hidden = true;
    collectorBanner.textContent = "";
  }

  // After a 429 only the Full button waits; the label counts down so the user
  // knows when to try again without watching the status line.
  let cooldownTimer = null;
  function startFullRefreshCooldown(seconds) {
    if (!fullButton) {
      return;
    }
    window.clearInterval(cooldownTimer);
    let remaining = Math.max(0, Math.ceil(Number(seconds) || 0));
    const tick = () => {
      if (remaining <= 0) {
        window.clearInterval(cooldownTimer);
        cooldownTimer = null;
        fullButton.textContent = "Full refresh";
        fullButton.disabled = false;
        return;
      }
      fullButton.disabled = true;
      fullButton.textContent = `Full refresh (${formatDuration(remaining)})`;
      remaining -= 1;
    };
    tick();
    if (remaining > 0) {
      cooldownTimer = window.setInterval(tick, 1000);
    }
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
      const response = await fetch("/api/history/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      const body = await response.text();
      let payload = {};
      try {
        payload = body ? JSON.parse(body) : {};
      } catch (_error) {
        payload = { detail: body || `HTTP ${response.status}` };
      }
      if (!response.ok || payload.ok === false) {
        const error = new Error(payload.detail || `Refresh failed with ${response.status}`);
        error.retryAfterSeconds = response.status === 429 ? Number(payload.retry_after_seconds) || 0 : 0;
        throw error;
      }
      if (status) {
        status.textContent = payload.detail || "History refresh completed.";
      }
      renderOverview(payload);
      buttons.forEach((button) => button.disabled = false);
    } catch (error) {
      if (status) {
        status.textContent = error.retryAfterSeconds ? error.message : `Refresh failed: ${error.message || error}`;
      }
      buttons.forEach((button) => button.disabled = false);
      if (error.retryAfterSeconds) {
        startFullRefreshCooldown(error.retryAfterSeconds);
      }
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
