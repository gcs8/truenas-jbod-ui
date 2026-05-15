from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app import __version__
from app.logging_config import configure_service_logging
from app.metrics import install_metrics
from app.services.release_status import ReleaseStatusService
from history_service.collector import HistoryCollectionAlreadyRunning, HistoryCollector
from history_service.config import get_history_settings
from history_service.store import HistoryStore

configure_service_logging(
    log_level=os.getenv("APP_LOG_LEVEL", "INFO"),
    log_format=os.getenv("LOG_FORMAT", "text"),
    service_name="enclosure-history",
)
settings = get_history_settings()
store = HistoryStore(settings.sqlite_path)
collector = HistoryCollector(settings, store)
logger = logging.getLogger(__name__)
refresh_lock = asyncio.Lock()
SLOT_HISTORY_METRIC_LIMITS: dict[str, int] = {
    "temperature_c": 96,
    "bytes_read": 60,
    "bytes_written": 60,
    "annualized_bytes_written": 60,
    "power_on_hours": 60,
}


@lru_cache
def get_release_status_service() -> ReleaseStatusService:
    return ReleaseStatusService(
        current_version=__version__,
        enabled=settings.release_check_enabled,
        repo_full_name=settings.release_check_repo,
        interval_seconds=settings.release_check_interval_seconds,
        timeout_seconds=settings.release_check_timeout_seconds,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    release_task = asyncio.create_task(get_release_status_service().run_periodic_refresh())
    await collector.start()
    try:
        yield
    finally:
        if release_task is not None and not release_task.done():
            release_task.cancel()
            try:
                await release_task
            except asyncio.CancelledError:
                pass
        await collector.stop()


app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
install_metrics(app, service_name="enclosure-history", version=__version__)


@app.get("/", response_class=HTMLResponse)
async def index(exact_counts: bool = Query(default=False)) -> HTMLResponse:
    status = collector.status()
    counts = store.counts() if exact_counts else store.estimated_counts()
    scopes = store.list_scopes(include_activity_counts=exact_counts)
    return HTMLResponse(
        render_dashboard(
            status,
            counts,
            scopes,
            app_version=__version__,
            release_status=get_release_status_service().snapshot(),
            database_size_bytes=store.database_size_bytes(),
        )
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    collector_status = collector.status()
    payload = {
        "status": "ok" if not collector.last_error else "degraded",
        "collector": collector_status,
        "database_size_bytes": store.database_size_bytes(),
        **collector_status,
    }
    return JSONResponse(payload, status_code=200)


@app.get("/livez")
async def livez() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
        },
        status_code=200,
    )


@app.get("/api/history/overview")
async def overview(exact_counts: bool = Query(default=False)) -> dict[str, object]:
    return {
        "collector": collector.status(),
        "counts": store.counts() if exact_counts else store.estimated_counts(),
        "counts_exact": exact_counts,
        "database": {
            "size_bytes": store.database_size_bytes(),
        },
        "scopes": store.list_scopes(include_activity_counts=exact_counts),
    }


@app.post("/api/history/refresh", response_model=None)
async def refresh_history(mode: str = Query(default="fast")) -> dict[str, object] | JSONResponse:
    normalized_mode = mode.lower().strip()
    if normalized_mode not in {"fast", "full"}:
        raise HTTPException(status_code=400, detail="mode must be 'fast' or 'full'")
    if refresh_lock.locked():
        raise HTTPException(status_code=409, detail="History refresh already running.")
    if collector.collection_running:
        payload = await overview(exact_counts=False)
        return JSONResponse(
            {
                "ok": False,
                "mode": normalized_mode,
                "detail": "History collection already running.",
                **payload,
            },
            status_code=409,
        )
    try:
        async with refresh_lock:
            await collector.run_once(
                force_fast=True,
                force_slow=normalized_mode == "full",
                include_due_intervals=False,
                cached_root_only=normalized_mode == "fast",
            )
    except HistoryCollectionAlreadyRunning:
        payload = await overview(exact_counts=False)
        return JSONResponse(
            {
                "ok": False,
                "mode": normalized_mode,
                "detail": "History collection already running.",
                **payload,
            },
            status_code=409,
        )
    except Exception as exc:  # noqa: BLE001 - report manual collection failures as structured API errors.
        logger.exception("Manual history %s refresh failed", normalized_mode)
        collector.last_error = str(exc)
        try:
            payload = await overview(exact_counts=False)
        except Exception:  # noqa: BLE001 - keep the original refresh failure visible even if summary loading also fails.
            logger.exception("Manual history %s refresh failed while loading summary payload", normalized_mode)
            payload = {
                "collector": collector.status(),
                "counts": {},
                "counts_exact": False,
                "scopes": [],
            }
        return JSONResponse(
            {
                "ok": False,
                "mode": normalized_mode,
                "detail": f"History {normalized_mode} refresh failed: {exc}",
                **payload,
            },
            status_code=500,
        )
    payload = await overview(exact_counts=False)
    return {
        "ok": True,
        "mode": normalized_mode,
        "detail": "History full refresh completed." if normalized_mode == "full" else "History fast refresh completed.",
        **payload,
    }


@app.get("/api/history/slots/{slot}/events")
async def slot_events(
    slot: int,
    system_id: str = Query(...),
    enclosure_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, object]:
    return {
        "events": store.list_slot_events(system_id, enclosure_id, slot, limit=limit),
    }


@app.get("/api/history/slots/{slot}/metrics")
async def slot_metrics(
    slot: int,
    system_id: str = Query(...),
    enclosure_id: str | None = Query(default=None),
    metric_name: str | None = Query(default=None),
    since: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, object]:
    return {
        "samples": store.list_metric_samples(
            system_id,
            enclosure_id,
            slot,
            metric_name=metric_name,
            limit=limit,
            since=since,
        ),
    }


@app.get("/api/history/slots/{slot}/bundle")
async def slot_history_bundle(
    slot: int,
    system_id: str = Query(...),
    enclosure_id: str | None = Query(default=None),
    since: str | None = Query(default=None),
    event_limit: int = Query(default=12, ge=1, le=1000),
) -> dict[str, object]:
    return store.get_slot_history_bundle(
        system_id,
        enclosure_id,
        slot,
        event_limit=event_limit,
        metric_limits=SLOT_HISTORY_METRIC_LIMITS,
        since=since,
    )


@app.get("/api/history/scopes/slots")
async def scope_slot_history(
    system_id: str = Query(...),
    enclosure_id: str | None = Query(default=None),
    slots: list[int] = Query(default=[]),
    since: str | None = Query(default=None),
    event_limit: int = Query(default=12, ge=1, le=1000),
) -> dict[str, object]:
    histories = store.list_scope_history(
        system_id,
        enclosure_id,
        slots=slots,
        event_limit=event_limit,
        since=since,
        metric_limits={
            **SLOT_HISTORY_METRIC_LIMITS,
        },
    )
    return {
        "histories": {
            str(slot): {
                "slot": slot,
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "events": payload.get("events", []),
                "metrics": payload.get("metrics", {}),
                "sample_counts": payload.get("sample_counts", {}),
                "latest_values": payload.get("latest_values", {}),
            }
            for slot, payload in histories.items()
        }
    }


def render_dashboard(
    status: dict[str, object],
    counts: dict[str, object],
    scopes: list[dict[str, object]],
    *,
    app_version: str,
    release_status: dict[str, object] | None = None,
    database_size_bytes: int = 0,
) -> str:
    counts_are_estimated = bool(counts.get("estimated"))

    def format_count(value: object, *, estimated: bool = False) -> str:
        if value is None:
            return "deferred"
        prefix = "~" if estimated else ""
        return f"{prefix}{value}"

    def format_bytes(value: int) -> str:
        size = float(max(0, value))
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        unit = units[0]
        for candidate in units:
            unit = candidate
            if size < 1024 or candidate == units[-1]:
                break
            size /= 1024
        if unit == "B":
            return f"{int(size)} B"
        return f"{size:.1f} {unit}"

    release_payload = release_status or {}
    release_summary = html.escape(str(release_payload.get("summary") or "Checking releases..."))
    latest_url = str(release_payload.get("latest_url") or "").strip()
    release_note_markup = (
        f"<a class='note-link' href='{html.escape(latest_url)}' target='_blank' rel='noopener'>{release_summary}</a>"
        if latest_url
        else f"<span class='note-text'>{release_summary}</span>"
    )
    backoff_seconds = int(status.get("background_backoff_seconds_remaining") or 0)
    backoff_label = f"{backoff_seconds}s remaining" if backoff_seconds > 0 else "inactive"
    last_duration = status.get("last_collection_duration_seconds")
    last_duration_label = f"{float(last_duration):.1f}s" if isinstance(last_duration, (int, float)) else "not recorded"
    last_inventory_forced = status.get("last_collection_inventory_forced")
    if last_inventory_forced is True:
        last_inventory_mode = "forced"
    elif last_inventory_forced is False:
        last_inventory_mode = "cached"
    else:
        last_inventory_mode = "not recorded"
    status_json = json.dumps(status).replace("</", "<\\/")
    overview_json = json.dumps(
        {
            "collector": status,
            "counts": counts,
            "counts_exact": not counts_are_estimated,
            "database": {
                "size_bytes": database_size_bytes,
            },
            "scopes": scopes,
        }
    ).replace("</", "<\\/")
    rows = []
    for scope in scopes:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(scope.get('system_label') or scope.get('system_id') or 'unknown'))}</td>"
            f"<td>{html.escape(str(scope.get('enclosure_label') or scope.get('enclosure_id') or 'default'))}</td>"
            f"<td>{html.escape(str(scope.get('tracked_slots') or 0))}</td>"
            f"<td>{html.escape(format_count(scope.get('event_count')))}</td>"
            f"<td>{html.escape(format_count(scope.get('metric_sample_count')))}</td>"
            f"<td>{html.escape(str(scope.get('last_seen_at') or 'never'))}</td>"
            "</tr>"
        )
    scope_markup = "\n".join(rows) or "<tr><td colspan='6'>No slot history has been collected yet.</td></tr>"

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(settings.app_name)}</title>
    <style>
      :root {{
        color-scheme: dark;
        --bg: #11161c;
        --panel: #18212b;
        --panel-2: #202b38;
        --text: #eef4fb;
        --muted: #98a7ba;
        --line: #314253;
        --accent: #7cd4a8;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Segoe UI", Tahoma, sans-serif;
        background: radial-gradient(circle at top, #203245, var(--bg) 58%);
        color: var(--text);
      }}
      main {{
        max-width: 1080px;
        margin: 0 auto;
        padding: 32px 20px 48px;
      }}
      h1, h2 {{ margin: 0 0 12px; }}
      p {{ color: var(--muted); line-height: 1.5; }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
        gap: 14px;
        margin: 24px 0;
      }}
      .card, table {{
        background: rgba(24, 33, 43, 0.92);
        border: 1px solid var(--line);
        border-radius: 16px;
      }}
      .card {{
        padding: 16px;
      }}
      .label {{
        color: var(--muted);
        font-size: 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}
      .value {{
        margin-top: 8px;
        font-size: 1.35rem;
        font-weight: 700;
      }}
      .note-text,
      .note-link {{
        display: block;
        margin-top: 8px;
        color: var(--muted);
        font-size: 0.82rem;
        line-height: 1.4;
      }}
      .note-link {{
        text-decoration: none;
      }}
      .note-link:hover {{
        color: var(--text);
        text-decoration: underline;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        overflow: hidden;
      }}
      th, td {{
        padding: 12px 14px;
        border-bottom: 1px solid var(--line);
        text-align: left;
      }}
      th {{
        background: rgba(32, 43, 56, 0.95);
        color: var(--muted);
        font-size: 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}
      tr:last-child td {{ border-bottom: 0; }}
      code {{
        color: var(--accent);
        font-family: Consolas, monospace;
      }}
      .status-ok {{ color: var(--accent); }}
      .status-error {{ color: #ff8b8b; }}
      .actions {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        align-items: center;
        margin: 20px 0 4px;
      }}
      button {{
        border: 1px solid #45607d;
        border-radius: 12px;
        background: #2c3d50;
        color: var(--text);
        padding: 10px 14px;
        cursor: pointer;
      }}
      button.secondary {{
        background: transparent;
        border-color: var(--line);
      }}
      button:disabled {{
        cursor: wait;
        opacity: 0.7;
      }}
      .action-status {{
        min-height: 1.4em;
        color: var(--muted);
        font-size: 0.88rem;
      }}
      .collector-banner {{
        margin: 18px 0 4px;
        padding: 12px 14px;
        border: 1px solid #6f5f24;
        border-radius: 12px;
        background: rgba(96, 76, 24, 0.34);
        color: #f5d789;
        font-size: 0.92rem;
      }}
      .collector-banner[hidden] {{
        display: none;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>{html.escape(settings.app_name)}</h1>
      <p>
        Optional sidecar for slot history and lightweight SMART snapshots. This stays separate from
        the main UI so collection can be enabled or disabled without changing the core dashboard.
      </p>
      <div class="actions" aria-label="History refresh actions">
        <button id="history-refresh-fast" type="button">Refresh Fast</button>
        <button id="history-refresh-full" class="secondary" type="button">Refresh Full</button>
        <span id="history-refresh-status" class="action-status" role="status" aria-live="polite"></span>
      </div>
      <div id="collector-activity-banner" class="collector-banner" role="status" aria-live="polite" hidden></div>

      <section class="grid">
        <div class="card">
          <div class="label">Collector</div>
          <div id="collector-state-value" class="value {'status-ok' if status.get('collector_running') else 'status-error'}">
            {html.escape('Running' if status.get('collector_running') else 'Stopped')}
          </div>
        </div>
        <div class="card">
          <div class="label">Tracked Slots</div>
          <div id="tracked-slots-value" class="value">{counts.get('tracked_slots', 0)}</div>
        </div>
        <div class="card">
          <div class="label">Slot Events</div>
          <div id="slot-events-value" class="value">{html.escape(format_count(counts.get('event_count', 0), estimated=counts_are_estimated))}</div>
        </div>
        <div class="card">
          <div class="label">Metric Samples</div>
          <div id="metric-samples-value" class="value">{html.escape(format_count(counts.get('metric_sample_count', 0), estimated=counts_are_estimated))}</div>
        </div>
        <div class="card">
          <div class="label">DB Size</div>
          <div id="db-size-value" class="value">{html.escape(format_bytes(database_size_bytes))}</div>
        </div>
        <div class="card">
          <div class="label">Version</div>
          <div class="value">{html.escape(app_version)}</div>
          {release_note_markup}
        </div>
      </section>

      <section class="card">
        <h2>Collector Status</h2>
        <p>
          Current collection: <code id="status-current-collection">not running</code><br>
          Source: <code id="status-source-base-url">{html.escape(str(status.get('source_base_url') or 'unknown'))}</code><br>
          Database: <code id="status-sqlite-path">{html.escape(str(status.get('sqlite_path') or 'unknown'))}</code><br>
          Last inventory pass: <code id="status-last-inventory-at">{html.escape(str(status.get('last_inventory_at') or 'never'))}</code><br>
          Last fast metrics pass: <code id="status-last-fast-metrics-at">{html.escape(str(status.get('last_fast_metrics_at') or 'never'))}</code><br>
          Last slow metrics pass: <code id="status-last-slow-metrics-at">{html.escape(str(status.get('last_slow_metrics_at') or 'never'))}</code><br>
          Last backup snapshot: <code id="status-last-backup-at">{html.escape(str(status.get('last_backup_at') or 'never'))}</code><br>
          Last collection duration: <code id="status-last-collection-duration">{html.escape(last_duration_label)}</code><br>
          Last collection inventory: <code id="status-last-collection-inventory">{html.escape(last_inventory_mode)}</code><br>
          Next background pass: <code id="status-next-collection-at">{html.escape(str(status.get('next_collection_at') or 'not scheduled'))}</code><br>
          Background failures: <code id="status-background-failures">{html.escape(str(status.get('background_consecutive_failures') or 0))}</code><br>
          Background backoff: <code id="status-background-backoff">{html.escape(backoff_label)}</code><br>
          Backoff until: <code id="status-background-backoff-until">{html.escape(str(status.get('background_backoff_until') or 'not active'))}</code><br>
          Last error: <code id="status-last-error">{html.escape(str(status.get('last_error') or 'none'))}</code>
        </p>
      </section>

      <section style="margin-top: 24px;">
        <h2>Tracked Scopes</h2>
        <table>
          <thead>
            <tr>
              <th>System</th>
              <th>Enclosure</th>
              <th>Tracked Slots</th>
              <th>Events</th>
              <th>Metric Samples</th>
              <th>Last Seen</th>
            </tr>
          </thead>
          <tbody id="tracked-scopes-body">
            {scope_markup}
          </tbody>
        </table>
      </section>
    </main>
    <script>
      (() => {{
        const status = document.getElementById("history-refresh-status");
        const fastButton = document.getElementById("history-refresh-fast");
        const fullButton = document.getElementById("history-refresh-full");
        const collectorBanner = document.getElementById("collector-activity-banner");
        const buttons = [fastButton, fullButton].filter(Boolean);
        const initialCollectorStatus = {status_json};
        const initialOverviewPayload = {overview_json};

        function formatDuration(totalSeconds) {{
          const seconds = Math.max(0, Number(totalSeconds || 0));
          const minutes = Math.floor(seconds / 60);
          const remainder = Math.floor(seconds % 60);
          if (minutes <= 0) {{
            return `${{remainder}}s`;
          }}
          return `${{minutes}}m ${{remainder}}s`;
        }}

        function formatCount(value, estimated = false) {{
          if (value === null || value === undefined) {{
            return "deferred";
          }}
          return `${{estimated ? "~" : ""}}${{value}}`;
        }}

        function formatBytes(value) {{
          let size = Math.max(0, Number(value || 0));
          const units = ["B", "KiB", "MiB", "GiB", "TiB"];
          let unit = units[0];
          for (const candidate of units) {{
            unit = candidate;
            if (size < 1024 || candidate === units[units.length - 1]) {{
              break;
            }}
            size /= 1024;
          }}
          return unit === "B" ? `${{Math.trunc(size)}} B` : `${{size.toFixed(1)}} ${{unit}}`;
        }}

        function setText(id, value) {{
          const element = document.getElementById(id);
          if (element) {{
            element.textContent = value;
          }}
        }}

        function statusValue(value, fallback = "never") {{
          return value === null || value === undefined || value === "" ? fallback : String(value);
        }}

        function collectionInventoryLabel(value) {{
          if (value === true) {{
            return "forced";
          }}
          if (value === false) {{
            return "cached";
          }}
          return "not recorded";
        }}

        function collectionDurationLabel(value) {{
          return Number.isFinite(Number(value)) ? `${{Number(value).toFixed(1)}}s` : "not recorded";
        }}

        function backoffLabel(seconds) {{
          const remaining = Number(seconds || 0);
          return remaining > 0 ? `${{Math.ceil(remaining)}}s remaining` : "inactive";
        }}

        function renderCollectorStatus(payload) {{
          const collector = payload?.collector || payload || {{}};
          const stateValue = document.getElementById("collector-state-value");
          if (stateValue) {{
            stateValue.textContent = collector.collector_running ? "Running" : "Stopped";
            stateValue.classList.toggle("status-ok", Boolean(collector.collector_running));
            stateValue.classList.toggle("status-error", !collector.collector_running);
          }}
          const currentCollection = collector.collection_running
            ? `${{collector.collection_kind || "background"}} for ${{formatDuration(collector.collection_elapsed_seconds)}}: ${{collector.collection_activity || "working"}}`
            : "not running";
          setText("status-current-collection", currentCollection);
          setText("status-source-base-url", statusValue(collector.source_base_url, "unknown"));
          setText("status-sqlite-path", statusValue(collector.sqlite_path, "unknown"));
          setText("status-last-inventory-at", statusValue(collector.last_inventory_at));
          setText("status-last-fast-metrics-at", statusValue(collector.last_fast_metrics_at));
          setText("status-last-slow-metrics-at", statusValue(collector.last_slow_metrics_at));
          setText("status-last-backup-at", statusValue(collector.last_backup_at));
          setText("status-last-collection-duration", collectionDurationLabel(collector.last_collection_duration_seconds));
          setText("status-last-collection-inventory", collectionInventoryLabel(collector.last_collection_inventory_forced));
          setText("status-next-collection-at", statusValue(collector.next_collection_at, "not scheduled"));
          setText("status-background-failures", String(collector.background_consecutive_failures || 0));
          setText("status-background-backoff", backoffLabel(collector.background_backoff_seconds_remaining));
          setText("status-background-backoff-until", statusValue(collector.background_backoff_until, "not active"));
          setText("status-last-error", statusValue(collector.last_error, "none"));
        }}

        function renderOverview(payload) {{
          if (!payload) {{
            return;
          }}
          renderCollectorStatus(payload);
          const counts = payload.counts || {{}};
          const countsExact = Boolean(payload.counts_exact);
          setText("tracked-slots-value", statusValue(counts.tracked_slots, "0"));
          setText("slot-events-value", formatCount(counts.event_count, !countsExact));
          setText("metric-samples-value", formatCount(counts.metric_sample_count, !countsExact));
          setText("db-size-value", formatBytes(payload.database?.size_bytes ?? payload.database_size_bytes));
          renderScopes(payload.scopes || []);
        }}

        function renderScopes(scopes) {{
          const body = document.getElementById("tracked-scopes-body");
          if (!body) {{
            return;
          }}
          const rows = Array.isArray(scopes) ? scopes : [];
          if (!rows.length) {{
            body.innerHTML = "<tr><td colspan='6'>No slot history has been collected yet.</td></tr>";
            return;
          }}
          body.replaceChildren(...rows.map((scope) => {{
            const row = document.createElement("tr");
            const cells = [
              statusValue(scope.system_label || scope.system_id, "unknown"),
              statusValue(scope.enclosure_label || scope.enclosure_id, "default"),
              statusValue(scope.tracked_slots, "0"),
              formatCount(scope.event_count),
              formatCount(scope.metric_sample_count),
              statusValue(scope.last_seen_at, "never"),
            ];
            for (const value of cells) {{
              const cell = document.createElement("td");
              cell.textContent = value;
              row.appendChild(cell);
            }}
            return row;
          }}));
        }}

        function renderCollectorBanner(payload) {{
          if (!collectorBanner) {{
            return;
          }}
          const collector = payload?.collector || payload || {{}};
          if (collector.collection_running) {{
            const kind = collector.collection_kind || "background";
            const activity = collector.collection_activity || "working";
            const elapsed = formatDuration(collector.collection_elapsed_seconds);
            collectorBanner.textContent = `History ${{kind}} collection running for ${{elapsed}}: ${{activity}}.`;
            collectorBanner.hidden = false;
            return;
          }}
          const backoffRemaining = Number(collector.background_backoff_seconds_remaining || 0);
          if (backoffRemaining > 0) {{
            collectorBanner.textContent = `History background collection is backed off for ${{formatDuration(backoffRemaining)}} after repeated failures.`;
            collectorBanner.hidden = false;
            return;
          }}
          collectorBanner.hidden = true;
          collectorBanner.textContent = "";
        }}

        async function pollCollectorStatus() {{
          try {{
            const response = await fetch("/healthz", {{ cache: "no-store" }});
            if (!response.ok) {{
              return;
            }}
            const payload = await response.json();
            renderCollectorBanner(payload);
            renderCollectorStatus(payload);
          }} catch (_error) {{
            // Keep the current banner state if a transient poll fails.
          }}
        }}

        async function pollOverviewStatus() {{
          try {{
            const response = await fetch("/api/history/overview", {{ cache: "no-store" }});
            if (!response.ok) {{
              return;
            }}
            renderOverview(await response.json());
          }} catch (_error) {{
            // The cheap health poll still keeps the live collector status moving.
          }}
        }}

        async function runRefresh(mode) {{
          buttons.forEach((button) => button.disabled = true);
          if (status) {{
            status.textContent = mode === "full"
              ? "Running full history refresh..."
              : "Running fast history refresh...";
          }}
          try {{
            const response = await fetch(`/api/history/refresh?mode=${{encodeURIComponent(mode)}}`, {{
              method: "POST",
            }});
            const body = await response.text();
            let payload = {{}};
            try {{
              payload = body ? JSON.parse(body) : {{}};
            }} catch (_error) {{
              payload = {{ detail: body || `HTTP ${{response.status}}` }};
            }}
            if (!response.ok || payload.ok === false) {{
              throw new Error(payload.detail || `Refresh failed with ${{response.status}}`);
            }}
            if (status) {{
              status.textContent = payload.detail || "History refresh completed.";
            }}
            renderOverview(payload);
            buttons.forEach((button) => button.disabled = false);
          }} catch (error) {{
            if (status) {{
              status.textContent = `Refresh failed: ${{error.message || error}}`;
            }}
            buttons.forEach((button) => button.disabled = false);
          }}
        }}

        renderCollectorBanner(initialCollectorStatus);
        renderOverview(initialOverviewPayload);
        window.setInterval(pollCollectorStatus, 2000);
        window.setInterval(pollOverviewStatus, 10000);
        window.__HISTORY_DASHBOARD_POLL = {{
          pollCollectorStatus,
          pollOverviewStatus,
        }};
        fastButton?.addEventListener("click", () => runRefresh("fast"));
        fullButton?.addEventListener("click", () => runRefresh("full"));
      }})();
    </script>
  </body>
</html>"""
