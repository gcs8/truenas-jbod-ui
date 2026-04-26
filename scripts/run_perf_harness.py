from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(slots=True)
class RunResult:
    name: str
    duration_ms: float
    stage_totals_ms: dict[str, float] = field(default_factory=dict)
    request_count: int = 0


@dataclass(slots=True)
class RequestMetrics:
    stage_totals_ms: dict[str, float] = field(default_factory=dict)
    request_count: int = 0


@dataclass(slots=True)
class GitContext:
    branch: str | None
    commit: str | None
    dirty: bool | None


@dataclass(slots=True)
class ApiResponse:
    data: dict[str, Any]
    headers: dict[str, str]


class ApiClient:
    def __init__(self, base_url: str, *, system_id: str | None = None, enclosure_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.system_id = system_id
        self.enclosure_id = enclosure_id

    def _build_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        merged = dict(params or {})
        if self.system_id:
            merged["system_id"] = self.system_id
        if self.enclosure_id:
            merged["enclosure_id"] = self.enclosure_id
        query = urlencode({key: value for key, value in merged.items() if value is not None})
        return f"{self.base_url}{path}" + (f"?{query}" if query else "")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.get_json_with_headers(path, params=params).data

    def get_json_with_headers(self, path: str, params: dict[str, Any] | None = None) -> ApiResponse:
        request = Request(self._build_url(path, params=params), method="GET")
        with urlopen(request, timeout=120) as response:
            return ApiResponse(
                data=json.loads(response.read().decode("utf-8")),
                headers={key.lower(): value for key, value in response.headers.items()},
            )

    def post_json(self, path: str, payload: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.post_json_with_headers(path, payload, params=params).data

    def post_json_with_headers(
        self,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> ApiResponse:
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            self._build_url(path, params=params),
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=120) as response:
            return ApiResponse(
                data=json.loads(response.read().decode("utf-8")),
                headers={key.lower(): value for key, value in response.headers.items()},
            )


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return ordered[index]


def summarize(results: list[RunResult]) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = {}
    grouped_stage_totals: dict[str, dict[str, list[float]]] = {}
    grouped_request_counts: dict[str, list[int]] = {}
    for result in results:
        grouped.setdefault(result.name, []).append(result.duration_ms)
        grouped_request_counts.setdefault(result.name, []).append(result.request_count)
        if result.stage_totals_ms:
            workflow_stage_totals = grouped_stage_totals.setdefault(result.name, {})
            for label, value in result.stage_totals_ms.items():
                workflow_stage_totals.setdefault(label, []).append(value)
    summary: list[dict[str, Any]] = []
    for name, durations in grouped.items():
        stage_summary = [
            {
                "label": label,
                "avg_ms": round(statistics.fmean(values), 1),
                "max_ms": round(max(values), 1),
                "iterations": len(values),
            }
            for label, values in sorted(
                grouped_stage_totals.get(name, {}).items(),
                key=lambda item: statistics.fmean(item[1]),
                reverse=True,
            )
        ]
        summary.append(
            {
                "name": name,
                "iterations": len(durations),
                "avg_requests": round(statistics.fmean(grouped_request_counts.get(name, [0])), 1),
                "min_ms": round(min(durations), 1),
                "avg_ms": round(statistics.fmean(durations), 1),
                "p50_ms": round(percentile(durations, 0.50), 1),
                "p95_ms": round(percentile(durations, 0.95), 1),
                "max_ms": round(max(durations), 1),
                "stage_summary": stage_summary,
            }
        )
    summary.sort(key=lambda item: (item["avg_ms"], item["name"]))
    return summary


def parse_server_timing(header_value: str | None) -> dict[str, float]:
    if not header_value:
        return {}
    metrics: dict[str, float] = {}
    for segment in header_value.split(","):
        parts = [part.strip() for part in segment.split(";") if part.strip()]
        if not parts:
            continue
        metric_name = parts[0]
        label = metric_name
        duration_ms: float | None = None
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key == "desc":
                label = value.strip().strip('"')
            elif key == "dur":
                try:
                    duration_ms = float(value)
                except ValueError:
                    duration_ms = None
        if duration_ms is not None:
            metrics[label] = duration_ms
    return metrics


def build_request_metrics(response: ApiResponse) -> RequestMetrics:
    stage_totals = parse_server_timing(response.headers.get("server-timing"))
    stage_totals.pop("total", None)
    return RequestMetrics(
        stage_totals_ms=stage_totals,
        request_count=1,
    )


def merge_request_metrics(*metrics: RequestMetrics) -> RequestMetrics:
    stage_totals: dict[str, float] = {}
    request_count = 0
    for metric in metrics:
        request_count += metric.request_count
        for label, value in metric.stage_totals_ms.items():
            stage_totals[label] = round(stage_totals.get(label, 0.0) + value, 1)
    return RequestMetrics(stage_totals_ms=stage_totals, request_count=request_count)


def run_timed(name: str, fn: Callable[[], RequestMetrics | None]) -> RunResult:
    started = time.perf_counter()
    metrics = fn() or RequestMetrics()
    return RunResult(
        name=name,
        duration_ms=(time.perf_counter() - started) * 1000,
        stage_totals_ms=metrics.stage_totals_ms,
        request_count=metrics.request_count,
    )


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "run"


def detect_git_context(repo_root: Path) -> GitContext:
    def run_git(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None
        return completed.stdout.strip()

    branch = run_git("rev-parse", "--abbrev-ref", "HEAD")
    commit = run_git("rev-parse", "--short", "HEAD")
    dirty_output = run_git("status", "--porcelain")
    dirty = None if dirty_output is None else bool(dirty_output)
    return GitContext(branch=branch, commit=commit, dirty=dirty)


def compare_summary(
    current_summary: list[dict[str, Any]],
    baseline_summary: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not baseline_summary:
        return []

    baseline_by_name = {item["name"]: item for item in baseline_summary}
    comparisons: list[dict[str, Any]] = []
    for item in current_summary:
        baseline = baseline_by_name.get(item["name"])
        if baseline is None:
            continue
        avg_delta_ms = round(item["avg_ms"] - baseline["avg_ms"], 1)
        p95_delta_ms = round(item["p95_ms"] - baseline["p95_ms"], 1)
        avg_delta_pct = None if baseline["avg_ms"] == 0 else round((avg_delta_ms / baseline["avg_ms"]) * 100, 1)
        p95_delta_pct = None if baseline["p95_ms"] == 0 else round((p95_delta_ms / baseline["p95_ms"]) * 100, 1)
        comparisons.append(
            {
                "name": item["name"],
                "baseline_avg_ms": baseline["avg_ms"],
                "current_avg_ms": item["avg_ms"],
                "avg_delta_ms": avg_delta_ms,
                "avg_delta_pct": avg_delta_pct,
                "baseline_p95_ms": baseline["p95_ms"],
                "current_p95_ms": item["p95_ms"],
                "p95_delta_ms": p95_delta_ms,
                "p95_delta_pct": p95_delta_pct,
            }
        )
    return comparisons


def load_baseline_payload(record_dir: Path | None, baseline: str | None) -> dict[str, Any] | None:
    baseline_path: Path | None = None
    if baseline:
        if baseline == "latest":
            if record_dir is None:
                return None
            baseline_path = record_dir / "latest.json"
        else:
            baseline_path = Path(baseline)
    elif record_dir is not None:
        baseline_path = record_dir / "latest.json"

    if baseline_path is None or not baseline_path.exists():
        return None
    return json.loads(baseline_path.read_text(encoding="utf-8"))


def append_history_csv(record_dir: Path, payload: dict[str, Any]) -> None:
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
        "system_id",
        "enclosure_id",
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
            existing_fieldnames = reader.fieldnames or []
            if existing_fieldnames != fieldnames:
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
                    "system_id": payload["system_id"],
                    "enclosure_id": payload["enclosure_id"],
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

    run_json_path = runs_dir / f"{run_stem}.json"
    run_md_path = runs_dir / f"{run_stem}.md"
    latest_json_path = record_dir / "latest.json"
    latest_md_path = record_dir / "latest.md"
    history_jsonl_path = record_dir / "history.jsonl"

    return {
        "run_json": str(run_json_path),
        "run_md": str(run_md_path),
        "latest_json": str(latest_json_path),
        "latest_md": str(latest_md_path),
        "history_jsonl": str(history_jsonl_path),
        "history_csv": str(record_dir / "history.csv"),
    }


def write_history_files(record_dir: Path, payload: dict[str, Any]) -> dict[str, str]:
    artifacts = build_artifact_paths(record_dir, payload)
    persisted_payload = dict(payload)
    persisted_payload["artifacts"] = artifacts
    markdown = render_markdown(persisted_payload)
    rendered_json = json.dumps(persisted_payload, indent=2)

    run_json_path = Path(artifacts["run_json"])
    run_md_path = Path(artifacts["run_md"])
    latest_json_path = Path(artifacts["latest_json"])
    latest_md_path = Path(artifacts["latest_md"])
    history_jsonl_path = Path(artifacts["history_jsonl"])

    run_json_path.write_text(rendered_json, encoding="utf-8")
    run_md_path.write_text(markdown, encoding="utf-8")
    latest_json_path.write_text(rendered_json, encoding="utf-8")
    latest_md_path.write_text(markdown, encoding="utf-8")
    with history_jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(persisted_payload))
        handle.write("\n")
    append_history_csv(record_dir, persisted_payload)

    return artifacts


def render_markdown(
    payload: dict[str, Any],
) -> str:
    summary = payload["summary"]
    lines = [
        "# Perf Harness Summary",
        "",
        f"- Recorded At: `{payload['recorded_at']}`",
        f"- Run Label: `{payload['label']}`",
        f"- Base URL: `{payload['base_url']}`",
        f"- System ID: `{payload['system_id'] or 'default'}`",
        f"- Enclosure ID: `{payload['enclosure_id'] or 'default'}`",
        f"- Branch: `{payload['git'].get('branch') or 'unknown'}`",
        f"- Commit: `{payload['git'].get('commit') or 'unknown'}`",
        f"- Worktree Dirty: `{payload['git'].get('dirty')}`",
        f"- SMART Batch Max Concurrency: `{payload.get('smart_batch_max_concurrency') or 'default'}`",
        f"- SMART Prefetch Chunk Size: `{payload.get('smart_prefetch_chunk_size') or 'default'}`",
        f"- SMART Prefetch Batch Concurrency: `{payload.get('smart_prefetch_batch_concurrency') or 'default'}`",
        "",
        "| Workflow | Iterations | Avg requests | Min ms | Avg ms | P50 ms | P95 ms | Max ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    stage_sections: list[str] = []
    for item in summary:
        lines.append(
            f"| {item['name']} | {item['iterations']} | {item['avg_requests']} | {item['min_ms']} | {item['avg_ms']} | "
            f"{item['p50_ms']} | {item['p95_ms']} | {item['max_ms']} |"
        )
        if item.get("stage_summary"):
            stage_sections.append(f"### {item['name']} stages")
            for stage in item["stage_summary"][:5]:
                stage_sections.append(
                    f"  - `{stage['label']}` avg `{stage['avg_ms']} ms` max `{stage['max_ms']} ms`"
                )
            stage_sections.append("")

    if stage_sections:
        lines.extend(["", "## Stage Rollups", ""])
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
                f"| {item['name']} | {item['avg_delta_ms']} | {avg_delta_pct} | {item['p95_delta_ms']} | {p95_delta_pct} |"
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
    parser = argparse.ArgumentParser(description="Run a lightweight read-only performance harness against the app API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="Base URL for the running app.")
    parser.add_argument("--system-id", default=None, help="Optional system id query parameter.")
    parser.add_argument("--enclosure-id", default=None, help="Optional enclosure id query parameter.")
    parser.add_argument("--iterations", type=int, default=3, help="Iterations per workflow.")
    parser.add_argument("--smart-slot-count", type=int, default=8, help="How many slots to include in the smart batch probe.")
    parser.add_argument(
        "--smart-batch-max-concurrency",
        type=int,
        default=None,
        help="Optional max_concurrency override to send with smart batch requests.",
    )
    parser.add_argument(
        "--smart-prefetch-chunk-size",
        type=int,
        default=24,
        help="Chunk size to use when simulating browser SMART prefetch.",
    )
    parser.add_argument(
        "--smart-prefetch-batch-concurrency",
        type=int,
        default=2,
        help="Parallel chunk request count to use when simulating browser SMART prefetch.",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json", help="Output format.")
    parser.add_argument("--output", default=None, help="Optional file to write instead of stdout.")
    parser.add_argument("--label", default=None, help="Optional label for the saved run.")
    parser.add_argument(
        "--skip-snapshot-export-estimate",
        action="store_true",
        help="Skip the snapshot export estimate workflow when focusing on live read-path timings.",
    )
    parser.add_argument(
        "--record-dir",
        default=str(REPO_ROOT / "data" / "perf"),
        help="Directory for latest/history perf artifacts.",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Optional JSON file to compare against, or 'latest' for the record dir latest.json.",
    )
    parser.add_argument("--no-record", action="store_true", help="Do not write latest/history artifacts.")
    args = parser.parse_args()

    client = ApiClient(args.base_url, system_id=args.system_id, enclosure_id=args.enclosure_id)
    results: list[RunResult] = []

    inventory = client.get_json("/api/inventory", params={"force": "true"})
    client.get_json("/api/storage-views")
    mapping_bundle = client.get_json("/api/mappings/export")
    history_status_available = False
    try:
        client.get_json("/api/history/status")
    except Exception:
        history_status_available = False
    else:
        history_status_available = True
    slots = inventory.get("slots") or []
    mappings = mapping_bundle.get("mappings") or []
    probe_slots = [slot.get("slot") for slot in slots[: max(1, args.smart_slot_count)] if isinstance(slot, dict) and slot.get("slot") is not None]
    selected_slot = probe_slots[0] if probe_slots else None

    def inventory_force() -> RequestMetrics:
        return build_request_metrics(client.get_json_with_headers("/api/inventory", params={"force": "true"}))

    def inventory_cached() -> RequestMetrics:
        return build_request_metrics(client.get_json_with_headers("/api/inventory"))

    def storage_views_cached() -> RequestMetrics:
        return build_request_metrics(client.get_json_with_headers("/api/storage-views"))

    def history_status() -> RequestMetrics:
        return build_request_metrics(client.get_json_with_headers("/api/history/status"))

    def health_cached() -> RequestMetrics:
        return build_request_metrics(client.get_json_with_headers("/healthz"))

    def request_smart_batch(slots_to_fetch: list[int]) -> RequestMetrics:
        response = client.post_json_with_headers(
            "/api/slots/smart-batch",
            {
                "slots": slots_to_fetch,
                "max_concurrency": (
                    min(args.smart_batch_max_concurrency, len(slots_to_fetch))
                    if args.smart_batch_max_concurrency
                    else None
                ),
            },
        )
        return build_request_metrics(response)

    def smart_batch() -> RequestMetrics:
        if not probe_slots:
            return RequestMetrics()
        return request_smart_batch(probe_slots)

    def smart_prefetch_chunked() -> RequestMetrics:
        if not probe_slots:
            return RequestMetrics()
        chunk_size = max(1, args.smart_prefetch_chunk_size)
        chunks = [probe_slots[index : index + chunk_size] for index in range(0, len(probe_slots), chunk_size)]
        worker_count = max(1, min(args.smart_prefetch_batch_concurrency, len(chunks)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(request_smart_batch, chunk) for chunk in chunks]
            return merge_request_metrics(*(future.result() for future in futures))

    def snapshot_estimate() -> RequestMetrics:
        response = client.post_json_with_headers(
            "/api/export/enclosure-snapshot/estimate",
            {
                "selected_slot": selected_slot,
                "history_window_hours": 24,
                "history_panel_open": bool(selected_slot is not None),
                "io_chart_mode": "total",
                "redact_sensitive": False,
                "packaging": "auto",
                "allow_oversize": False,
            },
        )
        return build_request_metrics(response)

    def mappings_import_roundtrip() -> RequestMetrics:
        if mappings:
            return RequestMetrics()
        return build_request_metrics(client.post_json_with_headers("/api/mappings/import", mapping_bundle))

    workflows: list[tuple[str, Any]] = [
        ("inventory_force", inventory_force),
        ("inventory_cached", inventory_cached),
        ("storage_views_cached", storage_views_cached),
        ("health_cached", health_cached),
    ]
    if history_status_available:
        workflows.append(("history_status", history_status))
    if probe_slots:
        workflows.append(("smart_batch", smart_batch))
        if len(probe_slots) > 1:
            workflows.append(("smart_prefetch_chunked", smart_prefetch_chunked))
    if not mappings:
        workflows.append(("mappings_import_roundtrip", mappings_import_roundtrip))
    if not args.skip_snapshot_export_estimate:
        workflows.append(("snapshot_export_estimate", snapshot_estimate))

    for _ in range(max(1, args.iterations)):
        for name, fn in workflows:
            results.append(run_timed(name, fn))

    summary = summarize(results)
    git_context = detect_git_context(REPO_ROOT)
    record_dir = None if args.no_record else Path(args.record_dir)
    if record_dir is not None:
        record_dir.mkdir(parents=True, exist_ok=True)
    baseline_payload = load_baseline_payload(record_dir, args.baseline)
    comparison = compare_summary(summary, baseline_payload.get("summary") if baseline_payload else None)
    payload = {
        "run_id": f"{int(time.time() * 1000)}",
        "recorded_at": utc_timestamp(),
        "label": args.label or git_context.branch or "manual-run",
        "base_url": args.base_url,
        "system_id": args.system_id,
        "enclosure_id": args.enclosure_id,
        "probe_slots": probe_slots,
        "mapping_count": len(mappings),
        "iterations": max(1, args.iterations),
        "smart_batch_max_concurrency": args.smart_batch_max_concurrency,
        "smart_prefetch_chunk_size": args.smart_prefetch_chunk_size,
        "smart_prefetch_batch_concurrency": args.smart_prefetch_batch_concurrency,
        "git": {
            "branch": git_context.branch,
            "commit": git_context.commit,
            "dirty": git_context.dirty,
        },
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

    markdown = render_markdown(payload)
    if record_dir is not None:
        payload["artifacts"] = write_history_files(record_dir, payload)
        markdown = render_markdown(payload)

    rendered = json.dumps(payload, indent=2) if args.format == "json" else markdown

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
