from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_perf_harness import (  # noqa: E402
    RequestMetrics,
    RunResult,
    compare_summary,
    detect_git_context,
    load_baseline_payload,
    slugify,
    summarize,
    utc_timestamp,
)


@dataclass(slots=True)
class HistoryApiResponse:
    data: Any
    headers: dict[str, str]


class HistoryApiClient:
    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _build_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        return f"{self.base_url}{path}" + (f"?{query}" if query else "")

    def get_text(self, path: str, params: dict[str, Any] | None = None) -> HistoryApiResponse:
        request = Request(self._build_url(path, params=params), method="GET")
        with urlopen(request, timeout=self.timeout) as response:
            return HistoryApiResponse(
                data=response.read().decode("utf-8", errors="replace"),
                headers={key.lower(): value for key, value in response.headers.items()},
            )

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> HistoryApiResponse:
        response = self.get_text(path, params=params)
        return HistoryApiResponse(data=json.loads(str(response.data)), headers=response.headers)


def run_timed(name: str, fn: Callable[[], RequestMetrics | None]) -> RunResult:
    started = time.perf_counter()
    metrics = fn() or RequestMetrics()
    return RunResult(
        name=name,
        duration_ms=(time.perf_counter() - started) * 1000,
        stage_totals_ms=metrics.stage_totals_ms,
        request_count=metrics.request_count,
    )


def collector_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    collector = payload.get("collector")
    if isinstance(collector, dict):
        return collector
    return payload


def collector_stage_totals_from_payload(payload: dict[str, Any] | None) -> dict[str, float]:
    collector = collector_from_payload(payload)
    timings = collector.get("collection_stage_timings") or []
    totals: dict[str, float] = {}
    if not isinstance(timings, list):
        return totals
    for entry in timings:
        if not isinstance(entry, dict):
            continue
        stage = entry.get("stage")
        duration = entry.get("duration_ms")
        if not stage:
            continue
        try:
            duration_ms = float(duration)
        except (TypeError, ValueError):
            continue
        label = f"collector.{stage}"
        totals[label] = round(totals.get(label, 0.0) + duration_ms, 1)
    return totals


def metrics_from_payload(payload: dict[str, Any] | None) -> RequestMetrics:
    return RequestMetrics(stage_totals_ms=collector_stage_totals_from_payload(payload), request_count=1)


def format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        size = 0.0
    size = max(0.0, size)
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


def append_history_perf_csv(record_dir: Path, payload: dict[str, Any]) -> None:
    history_path = record_dir / "history.csv"
    comparison_by_name = {item["name"]: item for item in payload.get("comparison") or []}
    fieldnames = [
        "recorded_at",
        "run_id",
        "label",
        "branch",
        "commit",
        "dirty",
        "base_url",
        "workflow",
        "iterations",
        "avg_requests",
        "min_ms",
        "avg_ms",
        "p50_ms",
        "p95_ms",
        "max_ms",
        "baseline_run_id",
        "baseline_label",
        "avg_delta_ms",
        "avg_delta_pct",
        "p95_delta_ms",
        "p95_delta_pct",
    ]
    write_mode = "a"
    existing_rows: list[dict[str, Any]] = []
    if history_path.exists():
        with history_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if (reader.fieldnames or []) != fieldnames:
                existing_rows = list(reader)
                write_mode = "w"
    else:
        write_mode = "w"

    with history_path.open(write_mode, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_mode == "w":
            writer.writeheader()
            for row in existing_rows:
                writer.writerow({fieldname: row.get(fieldname) for fieldname in fieldnames})
        for item in payload["summary"]:
            comparison = comparison_by_name.get(item["name"], {})
            writer.writerow(
                {
                    "recorded_at": payload["recorded_at"],
                    "run_id": payload["run_id"],
                    "label": payload["label"],
                    "branch": payload["git"].get("branch"),
                    "commit": payload["git"].get("commit"),
                    "dirty": payload["git"].get("dirty"),
                    "base_url": payload["base_url"],
                    "workflow": item["name"],
                    "iterations": item["iterations"],
                    "avg_requests": item.get("avg_requests"),
                    "min_ms": item["min_ms"],
                    "avg_ms": item["avg_ms"],
                    "p50_ms": item["p50_ms"],
                    "p95_ms": item["p95_ms"],
                    "max_ms": item["max_ms"],
                    "baseline_run_id": (payload.get("baseline") or {}).get("run_id"),
                    "baseline_label": (payload.get("baseline") or {}).get("label"),
                    "avg_delta_ms": comparison.get("avg_delta_ms"),
                    "avg_delta_pct": comparison.get("avg_delta_pct"),
                    "p95_delta_ms": comparison.get("p95_delta_ms"),
                    "p95_delta_pct": comparison.get("p95_delta_pct"),
                }
            )


def build_artifact_paths(record_dir: Path, payload: dict[str, Any]) -> dict[str, str]:
    runs_dir = record_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    label_slug = slugify(payload["label"])
    run_stem = f"{payload['recorded_at'].replace(':', '').replace('-', '').replace('Z', 'z').replace('T', '_')}_{label_slug}"
    return {
        "run_json": str(runs_dir / f"{run_stem}.json"),
        "run_md": str(runs_dir / f"{run_stem}.md"),
        "latest_json": str(record_dir / "latest.json"),
        "latest_md": str(record_dir / "latest.md"),
        "history_jsonl": str(record_dir / "history.jsonl"),
        "history_csv": str(record_dir / "history.csv"),
    }


def write_history_perf_files(record_dir: Path, payload: dict[str, Any]) -> dict[str, str]:
    artifacts = build_artifact_paths(record_dir, payload)
    persisted_payload = dict(payload)
    persisted_payload["artifacts"] = artifacts
    rendered_json = json.dumps(persisted_payload, indent=2)
    rendered_markdown = render_markdown(persisted_payload)

    Path(artifacts["run_json"]).write_text(rendered_json, encoding="utf-8")
    Path(artifacts["run_md"]).write_text(rendered_markdown, encoding="utf-8")
    Path(artifacts["latest_json"]).write_text(rendered_json, encoding="utf-8")
    Path(artifacts["latest_md"]).write_text(rendered_markdown, encoding="utf-8")
    with Path(artifacts["history_jsonl"]).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(persisted_payload))
        handle.write("\n")
    append_history_perf_csv(record_dir, persisted_payload)
    return artifacts


def render_markdown(payload: dict[str, Any]) -> str:
    collector = payload.get("collector_snapshot") or {}
    counts = payload.get("overview_counts") or {}
    lines = [
        "# History Perf Harness Summary",
        "",
        f"- Recorded At: `{payload['recorded_at']}`",
        f"- Run Label: `{payload['label']}`",
        f"- Base URL: `{payload['base_url']}`",
        f"- Branch: `{payload['git'].get('branch') or 'unknown'}`",
        f"- Commit: `{payload['git'].get('commit') or 'unknown'}`",
        f"- Worktree Dirty: `{payload['git'].get('dirty')}`",
        f"- Exact Counts Included: `{payload.get('include_exact_counts')}`",
        "",
        "| Workflow | Iterations | Avg requests | Min ms | Avg ms | P50 ms | P95 ms | Max ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    stage_sections: list[str] = []
    for item in payload["summary"]:
        lines.append(
            f"| {item['name']} | {item['iterations']} | {item['avg_requests']} | {item['min_ms']} | "
            f"{item['avg_ms']} | {item['p50_ms']} | {item['p95_ms']} | {item['max_ms']} |"
        )
        if item.get("stage_summary"):
            stage_sections.append(f"### {item['name']} collector stages")
            for stage in item["stage_summary"][:8]:
                stage_sections.append(
                    f"- `{stage['label']}` avg `{stage['avg_ms']} ms` max `{stage['max_ms']} ms`"
                )
            stage_sections.append("")

    if collector:
        lines.extend(
            [
                "",
                "## Collector Snapshot",
                "",
                f"- Collector Running: `{collector.get('collector_running')}`",
                f"- Collection Running: `{collector.get('collection_running')}`",
                f"- Current Activity: `{collector.get('collection_activity') or 'none'}`",
                f"- Elapsed Seconds: `{collector.get('collection_elapsed_seconds') or 0}`",
                f"- Last Duration: `{collector.get('last_collection_duration_seconds') or 'not recorded'}`",
                f"- Last Inventory Forced: `{collector.get('last_collection_inventory_forced')}`",
                f"- Next Background Pass: `{collector.get('next_collection_at') or 'not scheduled'}`",
                f"- Background Failures: `{collector.get('background_consecutive_failures') or 0}`",
                f"- Last Error: `{collector.get('last_error') or 'none'}`",
                f"- DB Size: `{format_bytes(payload.get('database_size_bytes'))}`",
            ]
        )

    if counts:
        lines.extend(
            [
                "",
                "## Overview Counts",
                "",
                f"- Counts Exact: `{payload.get('overview_counts_exact')}`",
                f"- Tracked Slots: `{counts.get('tracked_slots', 0)}`",
                f"- Slot Events: `{counts.get('event_count', 0)}`",
                f"- Metric Samples: `{counts.get('metric_sample_count', 0)}`",
            ]
        )

    if stage_sections:
        lines.extend(["", "## Collector Stage Rollups", ""])
        lines.extend(stage_sections[:-1] if stage_sections[-1] == "" else stage_sections)

    if payload.get("comparison"):
        baseline = payload.get("baseline") or {}
        lines.extend(
            [
                "",
                "## Comparison",
                "",
                f"- Baseline Label: `{baseline.get('label') or 'previous latest'}`",
                f"- Baseline Run ID: `{baseline.get('run_id') or 'unknown'}`",
                "",
                "| Workflow | Avg delta ms | Avg delta % | P95 delta ms | P95 delta % |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for item in payload["comparison"]:
            avg_delta_pct = "n/a" if item["avg_delta_pct"] is None else item["avg_delta_pct"]
            p95_delta_pct = "n/a" if item["p95_delta_pct"] is None else item["p95_delta_pct"]
            lines.append(
                f"| {item['name']} | {item['avg_delta_ms']} | {avg_delta_pct} | "
                f"{item['p95_delta_ms']} | {p95_delta_pct} |"
            )

    if payload.get("artifacts"):
        lines.extend(
            [
                "",
                "## Artifacts",
                "",
                f"- Latest JSON: `{payload['artifacts']['latest_json']}`",
                f"- Latest Markdown: `{payload['artifacts']['latest_md']}`",
                f"- History CSV: `{payload['artifacts']['history_csv']}`",
                f"- History JSONL: `{payload['artifacts']['history_jsonl']}`",
            ]
        )

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a lightweight performance harness against the history sidecar.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081", help="Base URL for the running history sidecar.")
    parser.add_argument("--iterations", type=int, default=3, help="Iterations per workflow.")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--include-exact-counts",
        action="store_true",
        help="Also time /api/history/overview?exact_counts=true. This can be very slow on Windows bind mounts.",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json", help="Output format.")
    parser.add_argument("--output", default=None, help="Optional file to write instead of stdout.")
    parser.add_argument("--label", default=None, help="Optional label for the saved run.")
    parser.add_argument(
        "--record-dir",
        default=str(REPO_ROOT / "data" / "history-perf"),
        help="Directory for latest/history perf artifacts.",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Optional JSON file to compare against, or 'latest' for the record dir latest.json.",
    )
    parser.add_argument("--no-record", action="store_true", help="Do not write latest/history artifacts.")
    args = parser.parse_args()

    client = HistoryApiClient(args.base_url, timeout=args.timeout)
    results: list[RunResult] = []
    latest_healthz: dict[str, Any] | None = None
    latest_overview: dict[str, Any] | None = None

    def sidecar_livez() -> RequestMetrics:
        client.get_text("/livez")
        return RequestMetrics(request_count=1)

    def sidecar_healthz() -> RequestMetrics:
        nonlocal latest_healthz
        latest_healthz = client.get_json("/healthz").data
        return metrics_from_payload(latest_healthz)

    def dashboard_html() -> RequestMetrics:
        client.get_text("/")
        return RequestMetrics(request_count=1)

    def overview_estimated() -> RequestMetrics:
        nonlocal latest_overview
        latest_overview = client.get_json("/api/history/overview").data
        return metrics_from_payload(latest_overview)

    def overview_exact() -> RequestMetrics:
        nonlocal latest_overview
        latest_overview = client.get_json("/api/history/overview", params={"exact_counts": "true"}).data
        return metrics_from_payload(latest_overview)

    workflows: list[tuple[str, Callable[[], RequestMetrics | None]]] = [
        ("sidecar_livez", sidecar_livez),
        ("sidecar_healthz", sidecar_healthz),
        ("dashboard_html", dashboard_html),
        ("overview_estimated", overview_estimated),
    ]
    if args.include_exact_counts:
        workflows.append(("overview_exact", overview_exact))

    for _ in range(max(1, args.iterations)):
        for name, fn in workflows:
            results.append(run_timed(name, fn))

    if latest_healthz is None:
        latest_healthz = client.get_json("/healthz").data
    if latest_overview is None:
        latest_overview = client.get_json("/api/history/overview").data

    summary = summarize(results)
    git_context = detect_git_context(REPO_ROOT)
    record_dir = None if args.no_record else Path(args.record_dir)
    if record_dir is not None:
        record_dir.mkdir(parents=True, exist_ok=True)
    baseline_payload = load_baseline_payload(record_dir, args.baseline)
    comparison = compare_summary(summary, baseline_payload.get("summary") if baseline_payload else None)
    collector_snapshot = collector_from_payload(latest_overview) or collector_from_payload(latest_healthz)
    database_size_bytes = (
        ((latest_overview.get("database") or {}).get("size_bytes") if isinstance(latest_overview, dict) else None)
        or (latest_healthz or {}).get("database_size_bytes")
    )
    payload = {
        "run_id": f"{int(time.time() * 1000)}",
        "recorded_at": utc_timestamp(),
        "label": args.label or git_context.branch or "history-manual-run",
        "base_url": args.base_url,
        "iterations": max(1, args.iterations),
        "include_exact_counts": bool(args.include_exact_counts),
        "git": {
            "branch": git_context.branch,
            "commit": git_context.commit,
            "dirty": git_context.dirty,
        },
        "collector_snapshot": collector_snapshot,
        "overview_counts": latest_overview.get("counts") if isinstance(latest_overview, dict) else {},
        "overview_counts_exact": latest_overview.get("counts_exact") if isinstance(latest_overview, dict) else None,
        "database_size_bytes": database_size_bytes,
        "results": [
            {
                "name": item.name,
                "duration_ms": round(item.duration_ms, 1),
                "request_count": item.request_count,
                "stage_totals_ms": item.stage_totals_ms,
            }
            for item in results
        ],
        "summary": summary,
        "comparison": comparison,
        "baseline": (
            {
                "run_id": baseline_payload.get("run_id"),
                "label": baseline_payload.get("label"),
                "recorded_at": baseline_payload.get("recorded_at"),
            }
            if baseline_payload
            else None
        ),
        "artifacts": None,
    }

    rendered = render_markdown(payload) if args.format == "markdown" else json.dumps(payload, indent=2)
    if record_dir is not None:
        payload["artifacts"] = write_history_perf_files(record_dir, payload)
        rendered = render_markdown(payload) if args.format == "markdown" else json.dumps(payload, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
