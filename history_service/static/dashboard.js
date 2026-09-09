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

  const READ_TIMEOUT_MS = 10000;
  const REFRESH_TIMEOUT_MS = 120000;
  let generation = 0;
  let sequence = 0;
  let collectorSequence = 0;
  let refreshRunning = false;
  let refreshUnknown = false;
  const healthPoll = { url: "/healthz", delay: 2000, timer: null, active: null };
  const overviewPoll = { url: "/api/history/overview", delay: 10000, timer: null, active: null };

  // Bound the entire operation, including body consumption. Abort alone is not
  // a settlement guarantee (and does not undo a POST on the server).
  function boundedRequest(url, options, timeout, consume) {
    const controller = new AbortController();
    let rejectCanceled;
    const canceled = new Promise((_resolve, reject) => { rejectCanceled = reject; });
    function cancel() {
      controller.abort();
      rejectCanceled(new Error("Request canceled or timed out"));
    }
    const timer = window.setTimeout(cancel, timeout);
    const work = (async () => {
      const response = await fetch(url, { ...options, signal: controller.signal });
      if (controller.signal.aborted) throw new Error("Request canceled");
      const payload = await consume(response);
      if (controller.signal.aborted) throw new Error("Request canceled");
      return payload;
    })();
    return {
      cancel,
      promise: Promise.race([work, canceled]).finally(() => window.clearTimeout(timer)),
    };
  }

  function markCollectorStale(reason) {
    setText("history-collector-freshness", `Collector status stale: ${reason}. Showing last known values.`);
    const stateValue = document.getElementById("collector-state-value");
    if (stateValue) {
      if (!stateValue.textContent.startsWith("Stale")) {
        stateValue.textContent = `Stale (last known: ${stateValue.textContent.trim()})`;
      }
      stateValue.classList.toggle("status-ok", false);
      stateValue.classList.toggle("status-error", false);
    }
    const current = document.getElementById("status-current-collection");
    if (current && !current.textContent.startsWith("Stale")) {
      current.textContent = `Stale (last known: ${current.textContent})`;
    }
    if (collectorBanner) {
      collectorBanner.hidden = false;
      collectorBanner.textContent = "Collector activity is stale; current activity is unknown.";
    }
  }

  function markOverviewStale(reason) {
    setText("history-overview-freshness", `Overview stale: ${reason}. Showing last known counts and scopes.`);
  }

  function acceptCollector(payload, ticket) {
    // Health and overview share collector fields. A late older response (or
    // failure) must not replace a newer observation from either endpoint.
    if (ticket < collectorSequence) return;
    collectorSequence = ticket;
    renderCollectorStatus(payload);
    renderCollectorBanner(payload);
    setText("history-collector-freshness", "Collector status checked successfully.");
  }

  function schedulePoll(channel, delay = channel.delay) {
    window.clearTimeout(channel.timer);
    channel.timer = null;
    if (document.hidden || refreshRunning || channel.active) return;
    channel.timer = window.setTimeout(() => pollStatus(channel), delay);
  }

  function pollStatus(channel) {
    if (document.hidden || refreshRunning) return Promise.resolve();
    if (channel.active) return channel.active.promise;
    window.clearTimeout(channel.timer);
    channel.timer = null;
    const epoch = generation;
    const ticket = ++sequence;
    const request = boundedRequest(channel.url, { cache: "no-store" }, READ_TIMEOUT_MS, async (response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    });
    channel.active = request;
    request.promise = request.promise.then((payload) => {
      if (epoch !== generation || document.hidden) return;
      if (!payload || typeof payload !== "object" || Array.isArray(payload)
          || typeof (payload.collector || payload).collector_running !== "boolean") {
        throw new Error("Invalid dashboard response");
      }
      acceptCollector(payload, ticket);
      if (channel === overviewPoll) {
        renderOverview(payload);
        setText("history-overview-freshness", "Overview checked successfully.");
      }
    }).catch(() => {
      if (epoch !== generation || document.hidden) return;
      if (ticket >= collectorSequence) {
        collectorSequence = ticket;
        markCollectorStale("latest check failed or timed out");
      }
      if (channel === overviewPoll) markOverviewStale("latest check failed or timed out");
    }).finally(() => {
      if (channel.active !== request) return;
      channel.active = null;
      schedulePoll(channel);
    });
    return request.promise;
  }

  function pollCollectorStatus() {
    return pollStatus(healthPoll);
  }

  function pollOverviewStatus() {
    return pollStatus(overviewPoll);
  }

  function pauseReads() {
    generation += 1;
    for (const channel of [healthPoll, overviewPoll]) {
      window.clearTimeout(channel.timer);
      channel.timer = null;
      channel.active?.cancel();
      channel.active = null;
    }
  }

  function resumeReads() {
    if (document.hidden || refreshRunning) return;
    pollCollectorStatus();
    pollOverviewStatus();
  }

  async function runRefresh(mode) {
    if (refreshRunning || refreshUnknown || !buttons.length) return;
    refreshRunning = true;
    pauseReads();
    const epoch = generation;
    const ticket = ++sequence;
    buttons.forEach((button) => button.disabled = true);
    markCollectorStale("manual refresh in progress");
    markOverviewStale("manual refresh in progress");
    if (status) {
      status.textContent = mode === "full"
        ? "Running full history refresh..."
        : "Running fast history refresh...";
    }
    try {
      const request = boundedRequest("/api/history/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      }, REFRESH_TIMEOUT_MS, async (response) => {
        const body = await response.text();
        let payload;
        try {
          payload = JSON.parse(body);
        } catch (_error) {
          // An unreadable success response does not prove a write failed.
          if (response.ok) throw new Error("Unreadable refresh response");
          payload = { detail: body || `HTTP ${response.status}` };
        }
        if (!response.ok || payload?.ok === false) {
          const error = new Error(payload?.detail || `Refresh failed with ${response.status}`);
          // Only the refresh handler's mode-bound failure envelope confirms
          // a completed collection failure. Generic 5xx/gateway errors do not.
          const applicationFailure = response.status === 500
            && payload?.ok === false
            && payload.mode === mode
            && payload.detail === `History ${mode} refresh failed; see service logs.`
            && typeof payload.collector?.collector_running === "boolean"
            && typeof payload.collector?.collection_running === "boolean"
            && payload.collector.last_error === payload.detail;
          error.confirmedFailure = response.status < 500 || applicationFailure;
          throw error;
        }
        if (payload?.ok !== true) throw new Error("Unconfirmed refresh response");
        return payload;
      });
      const payload = await request.promise;
      if (status) status.textContent = payload.detail || "History refresh completed.";
      // A visibility change invalidates UI data, not the write's outcome.
      if (epoch === generation && !document.hidden) {
        acceptCollector(payload, ticket);
        renderOverview(payload);
        setText("history-overview-freshness", "Overview checked successfully.");
      }
    } catch (error) {
      refreshUnknown = !error.confirmedFailure;
      if (status) {
        status.textContent = refreshUnknown
          ? "Refresh outcome unknown: collection may still be running. Verify collector status before reloading this page to enable another refresh. No automatic retry was sent."
          : `Refresh failed: ${error.message || error}`;
      }
    } finally {
      refreshRunning = false;
      buttons.forEach((button) => button.disabled = refreshUnknown);
      resumeReads();
    }
  }

  const initialCollectorStatus = readInitialOverview();
  renderCollectorBanner(initialCollectorStatus);
  if (document.hidden) {
    markCollectorStale("polling paused while page is hidden");
    markOverviewStale("polling paused while page is hidden");
  } else {
    schedulePoll(healthPoll);
    schedulePoll(overviewPoll);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      pauseReads();
      markCollectorStale("polling paused while page is hidden");
      markOverviewStale("polling paused while page is hidden");
    } else {
      resumeReads();
    }
  });
  window.__HISTORY_DASHBOARD_POLL = {
    pollCollectorStatus,
    pollOverviewStatus,
  };
  fastButton?.addEventListener("click", () => runRefresh("fast"));
  fullButton?.addEventListener("click", () => runRefresh("full"));
})();
