from __future__ import annotations

import asyncio
import html
import os
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app import __version__
from app.logging_config import configure_service_logging
from app.metrics import install_metrics
from app.services.release_status import ReleaseStatusService
from history_service.collector import HistoryCollector
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
async def index() -> HTMLResponse:
    status = collector.status()
    counts = store.counts()
    scopes = store.list_scopes()
    return HTMLResponse(render_dashboard(status, counts, scopes, app_version=__version__, release_status=get_release_status_service().snapshot()))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    payload = {
        "status": "ok" if not collector.last_error else "degraded",
        **collector.status(),
        **store.counts(),
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
async def overview() -> dict[str, object]:
    return {
        "collector": collector.status(),
        "counts": store.counts(),
        "scopes": store.list_scopes(),
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
    event_limit: int = Query(default=12, ge=1, le=1000),
) -> dict[str, object]:
    histories = store.list_scope_history(
        system_id,
        enclosure_id,
        slots=slots,
        event_limit=event_limit,
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
    counts: dict[str, int],
    scopes: list[dict[str, object]],
    *,
    app_version: str,
    release_status: dict[str, object] | None = None,
) -> str:
    release_payload = release_status or {}
    release_summary = html.escape(str(release_payload.get("summary") or "Checking releases..."))
    latest_url = str(release_payload.get("latest_url") or "").strip()
    release_note_markup = (
        f"<a class='note-link' href='{html.escape(latest_url)}' target='_blank' rel='noopener'>{release_summary}</a>"
        if latest_url
        else f"<span class='note-text'>{release_summary}</span>"
    )
    rows = []
    for scope in scopes:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(scope.get('system_label') or scope.get('system_id') or 'unknown'))}</td>"
            f"<td>{html.escape(str(scope.get('enclosure_label') or scope.get('enclosure_id') or 'default'))}</td>"
            f"<td>{html.escape(str(scope.get('tracked_slots') or 0))}</td>"
            f"<td>{html.escape(str(scope.get('event_count') or 0))}</td>"
            f"<td>{html.escape(str(scope.get('metric_sample_count') or 0))}</td>"
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
    </style>
  </head>
  <body>
    <main>
      <h1>{html.escape(settings.app_name)}</h1>
      <p>
        Optional sidecar for slot history and lightweight SMART snapshots. This stays separate from
        the main UI so collection can be enabled or disabled without changing the core dashboard.
      </p>

      <section class="grid">
        <div class="card">
          <div class="label">Collector</div>
          <div class="value {'status-ok' if status.get('collector_running') else 'status-error'}">
            {html.escape('Running' if status.get('collector_running') else 'Stopped')}
          </div>
        </div>
        <div class="card">
          <div class="label">Tracked Slots</div>
          <div class="value">{counts.get('tracked_slots', 0)}</div>
        </div>
        <div class="card">
          <div class="label">Slot Events</div>
          <div class="value">{counts.get('event_count', 0)}</div>
        </div>
        <div class="card">
          <div class="label">Metric Samples</div>
          <div class="value">{counts.get('metric_sample_count', 0)}</div>
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
          Source: <code>{html.escape(str(status.get('source_base_url') or 'unknown'))}</code><br>
          Database: <code>{html.escape(str(status.get('sqlite_path') or 'unknown'))}</code><br>
          Last inventory pass: <code>{html.escape(str(status.get('last_inventory_at') or 'never'))}</code><br>
          Last fast metrics pass: <code>{html.escape(str(status.get('last_fast_metrics_at') or 'never'))}</code><br>
          Last slow metrics pass: <code>{html.escape(str(status.get('last_slow_metrics_at') or 'never'))}</code><br>
          Last backup snapshot: <code>{html.escape(str(status.get('last_backup_at') or 'never'))}</code><br>
          Last error: <code>{html.escape(str(status.get('last_error') or 'none'))}</code>
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
          <tbody>
            {scope_markup}
          </tbody>
        </table>
      </section>
    </main>
  </body>
</html>"""
