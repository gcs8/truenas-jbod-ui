from __future__ import annotations

import io
import hashlib
import json
import re
import time
import zipfile
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any

from fastapi import Request
from starlette.templating import Jinja2Templates

from app import __version__
from app.config import Settings
from app.models.domain import InventorySnapshot
from app.perf import add_perf_metadata, perf_stage
from app.services.history_backend import HistoryBackendClient


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_EXPORT_SIZE_LIMIT_BYTES = 24 * 1024 * 1024
IPV4_PATTERN = re.compile(r"(?<![\dA-Fa-f:])(?P<ip>(?:\d{1,3}\.){3}\d{1,3})(?![\dA-Fa-f:])")
IPV6_PATTERN = re.compile(r"(?<![:\w])(?P<ip>(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4})(?![:\w])")
SERIAL_PATH_KEYS = {"serial"}
PARTIAL_ID_PATH_KEYS = {
    "gptid",
    "logical_unit_id",
    "lunid",
    "sas_address",
    "attached_sas_address",
    "namespace_eui64",
    "namespace_nguid",
    "uuid",
    "enclosure_identifier",
}
ROLLUP_INTERVAL_CHOICES_SECONDS = (
    300,
    900,
    1800,
    3600,
    7200,
    10800,
    14400,
    21600,
    43200,
    86400,
    172800,
    604800,
)
TEMPERATURE_METRIC_NAMES = {"temperature_c"}


@dataclass(slots=True)
class RenderedSnapshotExport:
    cache_key: str
    filename: str
    html: str
    size_bytes: int
    snapshot: InventorySnapshot
    history_cache: dict[str, dict[str, Any]]
    smart_summary_cache: dict[str, dict[str, Any]]
    history_available: bool
    export_meta: dict[str, Any]
    history_summary: dict[str, Any]


@dataclass(slots=True)
class PackagedSnapshotExport:
    filename: str
    content: bytes
    media_type: str
    size_bytes: int
    html_size_bytes: int
    packaging: str
    redaction: str
    size_limit_bytes: int


@dataclass(slots=True)
class SnapshotExportCacheEntry:
    stored_at_monotonic: float
    value: Any


EXPORT_HISTORY_CACHE: OrderedDict[str, SnapshotExportCacheEntry] = OrderedDict()
EXPORT_RENDER_CACHE: OrderedDict[str, SnapshotExportCacheEntry] = OrderedDict()
EXPORT_ZIP_CACHE: OrderedDict[str, SnapshotExportCacheEntry] = OrderedDict()


class SnapshotExportTooLargeError(RuntimeError):
    def __init__(self, *, html_size_bytes: int, archive_size_bytes: int | None, size_limit_bytes: int) -> None:
        self.html_size_bytes = html_size_bytes
        self.archive_size_bytes = archive_size_bytes
        self.size_limit_bytes = size_limit_bytes
        parts = [
            f"Snapshot export exceeds the default {format_bytes(size_limit_bytes)} size target.",
            f"HTML: {format_bytes(html_size_bytes)}.",
        ]
        if archive_size_bytes is not None:
            parts.append(f"ZIP: {format_bytes(archive_size_bytes)}.")
        parts.append("Use Force ZIP or Allow oversize to continue.")
        super().__init__(" ".join(parts))


class SnapshotRedactor:
    def __init__(
        self,
        snapshot: InventorySnapshot,
        history_cache: dict[str, dict[str, Any]],
        smart_summary_cache: dict[str, dict[str, Any]],
    ) -> None:
        self.serial_values: list[str] = []
        self.partial_identifier_values: list[str] = []
        self.system_aliases = self._build_system_aliases(snapshot)
        self.enclosure_aliases = self._build_enclosure_aliases(snapshot)
        self._collect_known_values(snapshot.model_dump(mode="json"))
        self._collect_known_values(history_cache)
        self._collect_known_values(smart_summary_cache)
        self.serial_suffix_counts = Counter(self._serial_suffix(value) for value in self.serial_values if self._serial_suffix(value))
        self.token_replacements = self._build_token_replacements()

    def redact_snapshot(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        redacted = self.redact_object(snapshot.model_dump(mode="json"))
        return InventorySnapshot.model_validate(redacted)

    def redact_history_cache(self, history_cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return self.redact_object(history_cache)

    def redact_smart_summary_cache(self, smart_summary_cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return self.redact_object(smart_summary_cache)

    def redact_object(self, value: Any, path: tuple[Any, ...] = ()) -> Any:
        if isinstance(value, dict):
            return {key: self.redact_object(item, path + (key,)) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact_object(item, path + (index,)) for index, item in enumerate(value)]
        if isinstance(value, str):
            return self._redact_string(value, path)
        return value

    def _collect_known_values(self, value: Any, path: tuple[Any, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                next_path = path + (key,)
                if key == "details_json" and isinstance(item, str):
                    try:
                        parsed = json.loads(item)
                    except json.JSONDecodeError:
                        parsed = None
                    if parsed is not None:
                        self._collect_known_values(parsed, next_path + ("parsed",))
                self._collect_known_values(item, next_path)
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                self._collect_known_values(item, path + (index,))
            return
        if not isinstance(value, str):
            return

        bucket = self._classify_path(path)
        normalized = value.strip()
        if not normalized:
            return
        if bucket == "serial":
            self.serial_values.append(normalized)
        elif bucket == "partial_id":
            self.partial_identifier_values.append(normalized)

    def _classify_path(self, path: tuple[Any, ...]) -> str | None:
        if not path:
            return None
        leaf = str(path[-1]).lower()
        parent = str(path[-2]).lower() if len(path) >= 2 else ""
        grandparent = str(path[-3]).lower() if len(path) >= 3 else ""

        if leaf in SERIAL_PATH_KEYS:
            return "serial"
        if leaf in PARTIAL_ID_PATH_KEYS:
            return "partial_id"
        if leaf in {"selected_system_id", "selected_system_label", "system_id"}:
            return "system"
        if leaf in {"selected_enclosure_id", "selected_enclosure_label", "selected_enclosure_name", "enclosure_id", "enclosure_label", "enclosure_name"}:
            return "enclosure"
        if parent == "systems" and leaf in {"id", "label"}:
            return "system"
        if parent == "enclosures" and leaf in {"id", "label", "name"}:
            return "enclosure"
        if grandparent == "systems" and leaf in {"id", "label"}:
            return "system"
        if grandparent == "enclosures" and leaf in {"id", "label", "name"}:
            return "enclosure"
        return None

    @staticmethod
    def _build_aliases(values: list[str], prefix: str) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for value in values:
            if value in aliases:
                continue
            aliases[value] = f"{prefix}-{len(aliases) + 1:02d}"
        return aliases

    @classmethod
    def _build_system_aliases(cls, snapshot: InventorySnapshot) -> dict[str, str]:
        groups: list[list[str]] = []
        for system in snapshot.systems:
            groups.append([system.id, system.label])
        if snapshot.selected_system_id or snapshot.selected_system_label:
            groups.append([snapshot.selected_system_id or "", snapshot.selected_system_label or ""])
        return cls._build_group_aliases(groups, "host")

    @classmethod
    def _build_enclosure_aliases(cls, snapshot: InventorySnapshot) -> dict[str, str]:
        groups: list[list[str]] = []
        for enclosure in snapshot.enclosures:
            groups.append([enclosure.id, enclosure.label, enclosure.name or ""])
        if snapshot.selected_enclosure_id or snapshot.selected_enclosure_label or snapshot.selected_enclosure_name:
            groups.append(
                [
                    snapshot.selected_enclosure_id or "",
                    snapshot.selected_enclosure_label or "",
                    snapshot.selected_enclosure_name or "",
                ]
            )
        for slot in snapshot.slots:
            groups.append([slot.enclosure_id or "", slot.enclosure_label or "", slot.enclosure_name or ""])
        return cls._build_group_aliases(groups, "enc")

    @staticmethod
    def _build_group_aliases(groups: list[list[str]], prefix: str) -> dict[str, str]:
        aliases: dict[str, str] = {}
        alias_index = 0
        for group in groups:
            tokens = [token.strip() for token in group if token and token.strip()]
            if not tokens:
                continue
            existing_alias = next((aliases[token] for token in tokens if token in aliases), None)
            if existing_alias is None:
                alias_index += 1
                existing_alias = f"{prefix}-{alias_index:02d}"
            for token in tokens:
                aliases[token] = existing_alias
        return aliases

    def _build_token_replacements(self) -> list[tuple[str, str]]:
        replacements: dict[str, str] = {}
        for value, alias in self.system_aliases.items():
            replacements[value] = alias
        for value, alias in self.enclosure_aliases.items():
            replacements[value] = alias
        for value in self.serial_values:
            replacements[value] = self._mask_serial(value)
        for value in self.partial_identifier_values:
            replacements[value] = self._mask_partial_identifier(value)
        return sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)

    def _redact_string(self, value: str, path: tuple[Any, ...]) -> str:
        normalized = value.strip()
        if not normalized:
            return value

        bucket = self._classify_path(path)
        if bucket == "system":
            return self.system_aliases.get(normalized, normalized)
        if bucket == "enclosure":
            return self.enclosure_aliases.get(normalized, normalized)
        if bucket == "serial":
            return self._mask_serial(normalized)
        if bucket == "partial_id":
            return self._mask_partial_identifier(normalized)

        redacted = value
        for original, replacement in self.token_replacements:
            if original and original in redacted:
                redacted = redacted.replace(original, replacement)
        redacted = IPV4_PATTERN.sub(lambda match: self._mask_ipv4(match.group("ip")), redacted)
        redacted = IPV6_PATTERN.sub(lambda match: self._mask_ipv6(match.group("ip")), redacted)
        return redacted

    @staticmethod
    def _serial_suffix(value: str) -> str:
        cleaned = re.sub(r"\s+", "", value.strip())
        return cleaned[-4:].upper() if cleaned else ""

    def _mask_serial(self, value: str) -> str:
        cleaned = re.sub(r"\s+", "", value.strip())
        if not cleaned:
            return value
        suffix = cleaned[-4:]
        if len(cleaned) <= 4:
            return f"...{suffix}"
        if self.serial_suffix_counts.get(suffix.upper(), 0) > 1 and len(cleaned) > 6:
            return f"{cleaned[:2]}...{suffix}"
        return f"...{suffix}"

    @staticmethod
    def _mask_partial_identifier(value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return value
        if len(cleaned) <= 8:
            return f"{cleaned[:2]}...{cleaned[-2:]}"
        return f"{cleaned[:4]}...{cleaned[-4:]}"

    @staticmethod
    def _mask_ipv4(value: str) -> str:
        parts = value.split(".")
        if len(parts) != 4:
            return value
        return f"x.x.x.{parts[-1]}"

    @staticmethod
    def _mask_ipv6(value: str) -> str:
        parts = value.split(":")
        if len(parts) < 2:
            return value
        tail = [part for part in parts if part][-2:]
        return f"x:x:{':'.join(tail)}" if tail else "x:x"


class SnapshotExportService:
    def __init__(
        self,
        settings: Settings,
        history_backend: HistoryBackendClient,
        templates: Jinja2Templates,
        *,
        size_limit_bytes: int = DEFAULT_EXPORT_SIZE_LIMIT_BYTES,
    ) -> None:
        self.settings = settings
        self.history_backend = history_backend
        self.templates = templates
        self.size_limit_bytes = size_limit_bytes
        self._history_cache = EXPORT_HISTORY_CACHE
        self._render_cache = EXPORT_RENDER_CACHE
        self._zip_cache = EXPORT_ZIP_CACHE

    async def build_enclosure_snapshot_export(
        self,
        *,
        request: Request,
        snapshot: InventorySnapshot,
        smart_summary_cache: dict[str, dict[str, Any]] | None = None,
        selected_slot: int | None,
        history_window_hours: int | None,
        history_panel_open: bool = False,
        io_chart_mode: str,
        redact_sensitive: bool = False,
        packaging: str = "auto",
        allow_oversize: bool = False,
    ) -> PackagedSnapshotExport:
        with perf_stage("snapshot_export.render_html"):
            rendered = await self.build_enclosure_snapshot_html(
                request=request,
                snapshot=snapshot,
                smart_summary_cache=smart_summary_cache,
                selected_slot=selected_slot,
                history_window_hours=history_window_hours,
                history_panel_open=history_panel_open,
                io_chart_mode=io_chart_mode,
                redact_sensitive=redact_sensitive,
                requested_packaging=packaging,
            )
        html_bytes = rendered.html.encode("utf-8")
        html_size_bytes = len(html_bytes)
        normalized_packaging = self._normalize_packaging(packaging)
        with perf_stage("snapshot_export.build_zip_archive"):
            zip_bytes = self._build_zip_archive_cached(rendered, html_bytes)
        zip_filename = f"{Path(rendered.filename).stem}.zip"
        zip_size_bytes = len(zip_bytes)

        if normalized_packaging == "html":
            if html_size_bytes > self.size_limit_bytes and not allow_oversize:
                raise SnapshotExportTooLargeError(
                    html_size_bytes=html_size_bytes,
                    archive_size_bytes=zip_size_bytes,
                    size_limit_bytes=self.size_limit_bytes,
                )
            return PackagedSnapshotExport(
                filename=rendered.filename,
                content=html_bytes,
                media_type="text/html; charset=utf-8",
                size_bytes=html_size_bytes,
                html_size_bytes=html_size_bytes,
                packaging="html",
                redaction=rendered.export_meta["redaction"],
                size_limit_bytes=self.size_limit_bytes,
            )

        if normalized_packaging == "zip":
            if zip_size_bytes > self.size_limit_bytes and not allow_oversize:
                raise SnapshotExportTooLargeError(
                    html_size_bytes=html_size_bytes,
                    archive_size_bytes=zip_size_bytes,
                    size_limit_bytes=self.size_limit_bytes,
                )
            return PackagedSnapshotExport(
                filename=zip_filename,
                content=zip_bytes,
                media_type="application/zip",
                size_bytes=zip_size_bytes,
                html_size_bytes=html_size_bytes,
                packaging="zip",
                redaction=rendered.export_meta["redaction"],
                size_limit_bytes=self.size_limit_bytes,
            )

        if html_size_bytes <= self.size_limit_bytes:
            return PackagedSnapshotExport(
                filename=rendered.filename,
                content=html_bytes,
                media_type="text/html; charset=utf-8",
                size_bytes=html_size_bytes,
                html_size_bytes=html_size_bytes,
                packaging="html",
                redaction=rendered.export_meta["redaction"],
                size_limit_bytes=self.size_limit_bytes,
            )

        if zip_size_bytes <= self.size_limit_bytes or allow_oversize:
            return PackagedSnapshotExport(
                filename=zip_filename,
                content=zip_bytes,
                media_type="application/zip",
                size_bytes=zip_size_bytes,
                html_size_bytes=html_size_bytes,
                packaging="zip",
                redaction=rendered.export_meta["redaction"],
                size_limit_bytes=self.size_limit_bytes,
            )

        raise SnapshotExportTooLargeError(
            html_size_bytes=html_size_bytes,
            archive_size_bytes=zip_size_bytes,
            size_limit_bytes=self.size_limit_bytes,
        )

    async def estimate_enclosure_snapshot_export(
        self,
        *,
        request: Request,
        snapshot: InventorySnapshot,
        smart_summary_cache: dict[str, dict[str, Any]] | None = None,
        selected_slot: int | None,
        history_window_hours: int | None,
        history_panel_open: bool = False,
        io_chart_mode: str,
        redact_sensitive: bool = False,
        packaging: str = "auto",
        allow_oversize: bool = False,
    ) -> dict[str, Any]:
        with perf_stage("snapshot_export.estimate.render_html"):
            rendered = await self.build_enclosure_snapshot_html(
                request=request,
                snapshot=snapshot,
                smart_summary_cache=smart_summary_cache,
                selected_slot=selected_slot,
                history_window_hours=history_window_hours,
                history_panel_open=history_panel_open,
                io_chart_mode=io_chart_mode,
                redact_sensitive=redact_sensitive,
                requested_packaging=packaging,
            )
        html_bytes = rendered.html.encode("utf-8")
        html_size_bytes = len(html_bytes)
        with perf_stage("snapshot_export.estimate.measure_zip"):
            zip_size_bytes = len(self._build_zip_archive_cached(rendered, html_bytes))
        normalized_packaging = self._normalize_packaging(packaging)
        auto_packaging = self._determine_auto_packaging(html_size_bytes, zip_size_bytes)
        effective_packaging = self._determine_estimated_packaging(
            requested_packaging=normalized_packaging,
            auto_packaging=auto_packaging,
            allow_oversize=allow_oversize,
        )
        selected_size_bytes = self._size_for_packaging(effective_packaging, html_size_bytes, zip_size_bytes)
        selected_within_limit = selected_size_bytes is not None and selected_size_bytes <= self.size_limit_bytes
        selected_allowed = bool(selected_size_bytes is not None and (selected_within_limit or allow_oversize))
        return {
            "ok": True,
            "scope_label": rendered.export_meta.get("scope_label"),
            "selected_slot": rendered.export_meta.get("selected_slot"),
            "html_size_bytes": html_size_bytes,
            "html_size_label": format_bytes(html_size_bytes),
            "html_within_limit": html_size_bytes <= self.size_limit_bytes,
            "zip_size_bytes": zip_size_bytes,
            "zip_size_label": format_bytes(zip_size_bytes),
            "zip_within_limit": zip_size_bytes <= self.size_limit_bytes,
            "selected_packaging": normalized_packaging,
            "effective_packaging": effective_packaging,
            "selected_size_bytes": selected_size_bytes,
            "selected_size_label": format_bytes(selected_size_bytes) if selected_size_bytes is not None else None,
            "selected_within_limit": selected_within_limit,
            "selected_allowed": selected_allowed,
            "auto_packaging": auto_packaging,
            "auto_within_limit": auto_packaging in {"html", "zip"},
            "size_limit_bytes": self.size_limit_bytes,
            "size_limit_label": format_bytes(self.size_limit_bytes),
            "redaction": rendered.export_meta.get("redaction"),
            "redaction_label": rendered.export_meta.get("redaction_label"),
            "downsampling_label": rendered.export_meta.get("downsampling_label"),
            "downsampling_note": rendered.export_meta.get("downsampling_note"),
            "metric_sample_count": rendered.export_meta.get("metric_sample_count"),
            "event_count": rendered.export_meta.get("event_count"),
            "allow_oversize": allow_oversize,
        }

    async def build_enclosure_snapshot_html(
        self,
        *,
        request: Request,
        snapshot: InventorySnapshot,
        smart_summary_cache: dict[str, dict[str, Any]] | None = None,
        selected_slot: int | None,
        history_window_hours: int | None,
        history_panel_open: bool = False,
        io_chart_mode: str,
        redact_sensitive: bool = False,
        requested_packaging: str = "auto",
    ) -> RenderedSnapshotExport:
        normalized_slot = self._normalize_selected_slot(snapshot, selected_slot)
        normalized_window_hours = self._normalize_history_window_hours(history_window_hours)
        normalized_chart_mode = "average" if io_chart_mode == "average" else "total"
        render_cache_key = self._build_render_cache_key(
            snapshot=snapshot,
            smart_summary_cache=smart_summary_cache,
            selected_slot=normalized_slot,
            history_window_hours=normalized_window_hours,
            history_panel_open=history_panel_open,
            io_chart_mode=normalized_chart_mode,
            redact_sensitive=redact_sensitive,
        )
        cached_render = self._get_cached_value(self._render_cache, render_cache_key)
        if cached_render is not None:
            add_perf_metadata(
                snapshot_export_render_cache="hit",
                snapshot_export_render_cache_key=render_cache_key[:12],
                snapshot_export_render_cache_entries=len(self._render_cache),
            )
            return cached_render

        add_perf_metadata(
            snapshot_export_render_cache="miss",
            snapshot_export_render_cache_key=render_cache_key[:12],
            snapshot_export_render_cache_entries=len(self._render_cache),
        )
        generated_at = datetime.now(timezone.utc)

        with perf_stage("snapshot_export.collect_slot_histories", slot_count=len(snapshot.slots)):
            raw_history_cache = await self._collect_slot_histories(snapshot)
        base_smart_summary_cache = {
            str(slot_number): summary
            for slot_number, summary in (smart_summary_cache or {}).items()
        }
        template = self.templates.env.get_template("index.html")
        rendered_candidate: RenderedSnapshotExport | None = None
        for strategy in self._build_downsampling_strategies():
            with perf_stage(
                "snapshot_export.prepare_history_cache",
                target_points_per_series=strategy["target_points_per_series"],
                max_events_per_slot=strategy["max_events_per_slot"],
            ):
                history_cache_for_export, downsampling_meta = self._prepare_history_cache_for_export(
                    raw_history_cache,
                    history_window_hours=normalized_window_hours,
                    reference_time=generated_at,
                    target_points_per_series=strategy["target_points_per_series"],
                    max_events_per_slot=strategy["max_events_per_slot"],
                )
            smart_summary_cache_for_export = dict(base_smart_summary_cache)
            snapshot_for_export = snapshot
            if redact_sensitive:
                redactor = SnapshotRedactor(snapshot, history_cache_for_export, smart_summary_cache_for_export)
                snapshot_for_export = redactor.redact_snapshot(snapshot)
                history_cache_for_export = redactor.redact_history_cache(history_cache_for_export)
                history_cache_for_export = self._rekey_history_cache(history_cache_for_export)
                smart_summary_cache_for_export = redactor.redact_smart_summary_cache(smart_summary_cache_for_export)

            tracked_slots = sum(1 for payload in history_cache_for_export.values() if payload.get("available"))
            metric_sample_count = sum(
                len(samples)
                for payload in history_cache_for_export.values()
                for samples in (payload.get("metrics") or {}).values()
            )
            smart_summary_count = sum(1 for payload in smart_summary_cache_for_export.values() if payload)
            event_count = sum(len(payload.get("events") or []) for payload in history_cache_for_export.values())
            history_available = tracked_slots > 0
            redaction_level = "partial" if redact_sensitive else "none"
            redaction_label = "Partial" if redact_sensitive else "None"
            redaction_note = (
                "Host aliases and partial identifier masking applied"
                if redact_sensitive
                else "Original identifiers included"
            )

            export_meta = {
                "generated_at": generated_at.isoformat(),
                "app_version": __version__,
                "scope_kind": "enclosure",
                "scope_label": snapshot_for_export.selected_enclosure_label or snapshot_for_export.selected_enclosure_id or "Current Enclosure",
                "system_label": snapshot_for_export.selected_system_label,
                "history_window_hours": normalized_window_hours,
                "history_window_label": self._format_history_window_label(normalized_window_hours),
                "history_available": history_available,
                "tracked_slots": tracked_slots,
                "metric_sample_count": metric_sample_count,
                "smart_summary_count": smart_summary_count,
                "event_count": event_count,
                "selected_slot": normalized_slot,
                "io_chart_mode": normalized_chart_mode,
                "redaction": redaction_level,
                "redaction_label": redaction_label,
                "redaction_note": redaction_note,
                "downsampling_label": downsampling_meta["label"],
                "downsampling_note": downsampling_meta["note"],
                "offline": True,
                "size_limit_bytes": self.size_limit_bytes,
                "size_limit_label": format_bytes(self.size_limit_bytes),
            }
            history_summary = {
                "counts": {
                    "tracked_slots": tracked_slots,
                    "metric_sample_count": metric_sample_count,
                    "smart_summary_count": smart_summary_count,
                    "event_count": event_count,
                },
                "collector": {
                    "last_completed_at": generated_at.isoformat(),
                },
            }

            context = {
                "request": request,
                "snapshot": snapshot_for_export,
                "storage_view_runtime": {"system_id": snapshot_for_export.selected_system_id, "system_label": snapshot_for_export.selected_system_label, "views": []},
                "settings": self.settings,
                "initial_snapshot_json": json.dumps(snapshot_for_export.model_dump(mode="json")),
                "initial_storage_view_runtime_json": json.dumps(
                    {
                        "system_id": snapshot_for_export.selected_system_id,
                        "system_label": snapshot_for_export.selected_system_label,
                        "views": [],
                    }
                ),
                "history_configured": history_available,
                "snapshot_mode": True,
                "snapshot_export_meta": export_meta,
                "snapshot_export_meta_json": json.dumps(export_meta),
                "preloaded_history_json": json.dumps(history_cache_for_export),
                "preloaded_smart_summary_json": json.dumps(smart_summary_cache_for_export),
                "preloaded_history_summary_json": json.dumps(history_summary),
                "initial_selected_slot_json": json.dumps(normalized_slot),
                "initial_history_timeframe_hours_json": json.dumps(normalized_window_hours),
                "initial_history_panel_open_json": json.dumps(
                    bool(history_panel_open and normalized_slot is not None and history_available)
                ),
                "initial_history_io_chart_mode_json": json.dumps(normalized_chart_mode),
            }

            with perf_stage("snapshot_export.render_template"):
                html = self._inline_static_assets(request, template.render(context))
            filename = self._build_filename(snapshot_for_export, generated_at)
            rendered_candidate = RenderedSnapshotExport(
                cache_key=render_cache_key,
                filename=filename,
                html=html,
                size_bytes=len(html.encode("utf-8")),
                snapshot=snapshot_for_export,
                history_cache=history_cache_for_export,
                smart_summary_cache=smart_summary_cache_for_export,
                history_available=history_available,
                export_meta=export_meta,
                history_summary=history_summary,
            )
            if rendered_candidate.size_bytes <= self.size_limit_bytes:
                break

        if rendered_candidate is None:
            raise RuntimeError("Unable to render enclosure snapshot export.")
        self._store_cached_value(self._render_cache, render_cache_key, rendered_candidate)
        add_perf_metadata(snapshot_export_render_cache_entries=len(self._render_cache))
        return rendered_candidate

    @staticmethod
    def _build_downsampling_strategies() -> list[dict[str, int | None]]:
        return [
            {"target_points_per_series": None, "max_events_per_slot": None},
            {"target_points_per_series": 96, "max_events_per_slot": 50},
            {"target_points_per_series": 48, "max_events_per_slot": 25},
            {"target_points_per_series": 24, "max_events_per_slot": 10},
        ]

    def _prepare_history_cache_for_export(
        self,
        raw_history_cache: dict[str, dict[str, Any]],
        *,
        history_window_hours: int | None,
        reference_time: datetime,
        target_points_per_series: int | None,
        max_events_per_slot: int | None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        prepared_cache: dict[str, dict[str, Any]] = {}
        rollup_seconds_used = 0
        metric_rollup_applied = False
        event_trim_applied = False

        for cache_key, payload in raw_history_cache.items():
            metrics: dict[str, list[dict[str, Any]]] = {}
            for metric_name, raw_samples in (payload.get("metrics") or {}).items():
                filtered_samples = self._filter_history_samples_for_window(
                    raw_samples,
                    history_window_hours=history_window_hours,
                    reference_time=reference_time,
                )
                rolled_samples, rollup_seconds, changed = self._downsample_metric_samples(
                    metric_name,
                    filtered_samples,
                    target_points_per_series=target_points_per_series,
                )
                metrics[metric_name] = rolled_samples
                metric_rollup_applied = metric_rollup_applied or changed
                rollup_seconds_used = max(rollup_seconds_used, rollup_seconds or 0)

            filtered_events = self._filter_history_events_for_window(
                payload.get("events") or [],
                history_window_hours=history_window_hours,
                reference_time=reference_time,
            )
            exported_events = filtered_events
            if max_events_per_slot is not None and len(filtered_events) > max_events_per_slot:
                exported_events = filtered_events[-max_events_per_slot:]
                event_trim_applied = True

            exported_payload = dict(payload)
            exported_payload["metrics"] = metrics
            exported_payload["events"] = exported_events
            exported_payload["sample_counts"] = {
                metric_name: len(samples)
                for metric_name, samples in metrics.items()
            }
            exported_payload["latest_values"] = {
                metric_name: self._history_sample_value(samples[-1]) if samples else None
                for metric_name, samples in metrics.items()
            }
            prepared_cache[cache_key] = exported_payload

        return prepared_cache, self._build_downsampling_meta(
            history_window_hours=history_window_hours,
            rollup_seconds=rollup_seconds_used or None,
            metric_rollup_applied=metric_rollup_applied,
            event_trim_applied=event_trim_applied,
            max_events_per_slot=max_events_per_slot,
        )

    def _build_downsampling_meta(
        self,
        *,
        history_window_hours: int | None,
        rollup_seconds: int | None,
        metric_rollup_applied: bool,
        event_trim_applied: bool,
        max_events_per_slot: int | None,
    ) -> dict[str, str]:
        if not metric_rollup_applied and not event_trim_applied:
            return {
                "label": "None",
                "note": "No rollups or downsampling applied",
            }

        note_parts: list[str] = []
        if history_window_hours is not None:
            note_parts.append(f"Filtered to the exported {self._format_history_window_label(history_window_hours)} window")
        if metric_rollup_applied and rollup_seconds:
            label = f"{self._format_rollup_interval_label(rollup_seconds)} rollups"
            note_parts.append(f"Metric samples grouped into {label.lower()}")
        else:
            label = "Event cap"
        if event_trim_applied and max_events_per_slot is not None:
            note_parts.append(f"Recent events limited to {max_events_per_slot} per slot")

        return {
            "label": label,
            "note": ". ".join(note_parts) + ".",
        }

    def _filter_history_samples_for_window(
        self,
        samples: list[dict[str, Any]],
        *,
        history_window_hours: int | None,
        reference_time: datetime,
    ) -> list[dict[str, Any]]:
        ordered = sorted(
            samples,
            key=lambda sample: self._history_timestamp(sample) or datetime.min.replace(tzinfo=timezone.utc),
        )
        if history_window_hours is None:
            return ordered
        cutoff = reference_time.timestamp() - (history_window_hours * 3600)
        before_cutoff: list[dict[str, Any]] = []
        within_window: list[dict[str, Any]] = []
        for sample in ordered:
            timestamp = self._history_timestamp(sample)
            if timestamp is None:
                continue
            if timestamp.timestamp() < cutoff:
                before_cutoff.append(sample)
            else:
                within_window.append(sample)
        if before_cutoff:
            return [before_cutoff[-1], *within_window]
        return within_window

    def _filter_history_events_for_window(
        self,
        events: list[dict[str, Any]],
        *,
        history_window_hours: int | None,
        reference_time: datetime,
    ) -> list[dict[str, Any]]:
        ordered = sorted(
            events,
            key=lambda event: self._history_timestamp(event) or datetime.min.replace(tzinfo=timezone.utc),
        )
        if history_window_hours is None:
            return ordered
        cutoff = reference_time.timestamp() - (history_window_hours * 3600)
        return [
            event
            for event in ordered
            if (timestamp := self._history_timestamp(event)) is not None and timestamp.timestamp() >= cutoff
        ]

    def _downsample_metric_samples(
        self,
        metric_name: str,
        samples: list[dict[str, Any]],
        *,
        target_points_per_series: int | None,
    ) -> tuple[list[dict[str, Any]], int | None, bool]:
        ordered = [
            sample for sample in samples
            if self._history_timestamp(sample) is not None and self._history_sample_value(sample) is not None
        ]
        if target_points_per_series is None or len(ordered) <= target_points_per_series:
            return ordered, None, False

        first_timestamp = self._history_timestamp(ordered[0])
        last_timestamp = self._history_timestamp(ordered[-1])
        if first_timestamp is None or last_timestamp is None:
            return ordered, None, False
        span_seconds = max(1, int((last_timestamp - first_timestamp).total_seconds()))
        bucket_seconds = self._select_rollup_interval_seconds(span_seconds, target_points_per_series)
        buckets: dict[int, list[dict[str, Any]]] = {}
        for sample in ordered:
            timestamp = self._history_timestamp(sample)
            if timestamp is None:
                continue
            bucket_key = int(timestamp.timestamp()) // bucket_seconds
            buckets.setdefault(bucket_key, []).append(sample)

        aggregated = [
            self._aggregate_metric_bucket(metric_name, bucket_samples)
            for _, bucket_samples in sorted(buckets.items())
        ]
        return aggregated, bucket_seconds, len(aggregated) < len(ordered)

    def _aggregate_metric_bucket(self, metric_name: str, bucket_samples: list[dict[str, Any]]) -> dict[str, Any]:
        representative = dict(bucket_samples[-1])
        representative["rolled_up"] = True
        representative["rollup_count"] = len(bucket_samples)
        if metric_name in TEMPERATURE_METRIC_NAMES:
            numeric_values = [
                float(value)
                for sample in bucket_samples
                if (value := self._history_sample_value(sample)) is not None
            ]
            if numeric_values:
                averaged_value = round(sum(numeric_values) / len(numeric_values))
                representative["value"] = averaged_value
                if "value_integer" in representative:
                    representative["value_integer"] = averaged_value
                if "value_real" in representative:
                    representative["value_real"] = None
        return representative

    @staticmethod
    def _history_sample_value(sample: dict[str, Any]) -> Any:
        if "value" in sample:
            return sample.get("value")
        if sample.get("value_integer") is not None:
            return sample.get("value_integer")
        return sample.get("value_real")

    @staticmethod
    def _history_timestamp(item: dict[str, Any]) -> datetime | None:
        observed_at = item.get("observed_at")
        if not observed_at:
            return None
        try:
            return datetime.fromisoformat(str(observed_at))
        except ValueError:
            return None

    @staticmethod
    def _select_rollup_interval_seconds(span_seconds: int, target_points_per_series: int) -> int:
        desired_interval = max(60, ceil(span_seconds / max(1, target_points_per_series)))
        for candidate in ROLLUP_INTERVAL_CHOICES_SECONDS:
            if candidate >= desired_interval:
                return candidate
        return desired_interval

    @staticmethod
    def _format_rollup_interval_label(interval_seconds: int) -> str:
        if interval_seconds % 86400 == 0:
            days = interval_seconds // 86400
            return f"~{days}d"
        if interval_seconds % 3600 == 0:
            hours = interval_seconds // 3600
            return f"~{hours}h"
        if interval_seconds % 60 == 0:
            minutes = interval_seconds // 60
            return f"~{minutes}m"
        return f"~{interval_seconds}s"

    async def _collect_slot_histories(self, snapshot: InventorySnapshot) -> dict[str, dict[str, Any]]:
        if not self.history_backend.configured:
            return {}

        system_id = snapshot.selected_system_id
        enclosure_id = snapshot.selected_enclosure_id
        slot_numbers = [slot.slot for slot in snapshot.slots]
        history_cache_key = self._build_history_snapshot_cache_key(snapshot)
        cached_history = self._get_cached_value(self._history_cache, history_cache_key)
        if cached_history is not None:
            add_perf_metadata(
                snapshot_export_history_cache="hit",
                snapshot_export_history_cache_key=history_cache_key[:12],
                snapshot_export_history_cache_entries=len(self._history_cache),
            )
            return cached_history

        add_perf_metadata(
            snapshot_export_history_cache="miss",
            snapshot_export_history_cache_key=history_cache_key[:12],
            snapshot_export_history_cache_entries=len(self._history_cache),
        )
        get_status = getattr(self.history_backend, "get_status", None)
        if callable(get_status):
            status = await get_status()
            if not status.get("available"):
                detail = status.get("detail") or "History backend is unavailable."
                return {
                    self._build_history_cache_key(system_id, enclosure_id, slot.slot): self._build_unavailable_history_payload(
                        slot=slot.slot,
                        system_id=system_id,
                        enclosure_id=enclosure_id,
                        detail=detail,
                    )
                    for slot in snapshot.slots
                }

        scope_history = await self.history_backend.get_scope_history(
            system_id=system_id,
            enclosure_id=enclosure_id,
            slots=slot_numbers,
        )
        payload = {
            self._build_history_cache_key(system_id, enclosure_id, slot_number): scope_history.get(
                slot_number,
                self._build_unavailable_history_payload(
                    slot=slot_number,
                    system_id=system_id,
                    enclosure_id=enclosure_id,
                    detail="History backend did not return data for this slot.",
                ),
            )
            for slot_number in slot_numbers
        }
        self._store_cached_value(self._history_cache, history_cache_key, payload)
        add_perf_metadata(snapshot_export_history_cache_entries=len(self._history_cache))
        return payload

    @staticmethod
    def _build_unavailable_history_payload(
        *,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
        detail: str,
    ) -> dict[str, Any]:
        return {
            "configured": True,
            "available": False,
            "detail": detail,
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": {},
            "events": [],
            "sample_counts": {},
            "latest_values": {},
        }

    def _build_zip_archive_cached(self, rendered: RenderedSnapshotExport, html_content: bytes) -> bytes:
        cache_key = f"{rendered.cache_key}|zip"
        cached_zip = self._get_cached_value(self._zip_cache, cache_key)
        if cached_zip is not None:
            add_perf_metadata(
                snapshot_export_zip_cache="hit",
                snapshot_export_zip_cache_entries=len(self._zip_cache),
            )
            return cached_zip

        add_perf_metadata(
            snapshot_export_zip_cache="miss",
            snapshot_export_zip_cache_entries=len(self._zip_cache),
        )
        zip_bytes = self._build_zip_archive(rendered.filename, html_content)
        self._store_cached_value(self._zip_cache, cache_key, zip_bytes)
        add_perf_metadata(snapshot_export_zip_cache_entries=len(self._zip_cache))
        return zip_bytes

    def _cache_enabled(self) -> bool:
        return (
            self.settings.app.export_cache_ttl_seconds > 0
            and self.settings.app.export_cache_max_entries > 0
        )

    def _get_cached_value(
        self,
        cache: OrderedDict[str, SnapshotExportCacheEntry],
        cache_key: str,
    ) -> Any | None:
        if not self._cache_enabled():
            cache.clear()
            return None
        self._evict_stale_cache_entries(cache)
        entry = cache.get(cache_key)
        if entry is None:
            return None
        cache.move_to_end(cache_key)
        return entry.value

    def _store_cached_value(
        self,
        cache: OrderedDict[str, SnapshotExportCacheEntry],
        cache_key: str,
        value: Any,
    ) -> None:
        if not self._cache_enabled():
            return
        self._evict_stale_cache_entries(cache)
        cache[cache_key] = SnapshotExportCacheEntry(
            stored_at_monotonic=time.monotonic(),
            value=value,
        )
        cache.move_to_end(cache_key)
        while len(cache) > self.settings.app.export_cache_max_entries:
            cache.popitem(last=False)

    def _evict_stale_cache_entries(self, cache: OrderedDict[str, SnapshotExportCacheEntry]) -> None:
        if not self._cache_enabled():
            cache.clear()
            return
        ttl_seconds = self.settings.app.export_cache_ttl_seconds
        now = time.monotonic()
        stale_keys = [
            key
            for key, entry in cache.items()
            if now - entry.stored_at_monotonic > ttl_seconds
        ]
        for key in stale_keys:
            cache.pop(key, None)

    @staticmethod
    def _build_snapshot_signature(snapshot: InventorySnapshot) -> str:
        payload = snapshot.model_dump(
            mode="json",
            exclude={"generated_at", "last_updated"},
        )
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(rendered.encode("utf-8")).hexdigest()

    @staticmethod
    def _smart_summary_cache_fingerprint(smart_summary_cache: dict[str, dict[str, Any]] | None) -> str:
        normalized_cache = {
            str(slot): summary
            for slot, summary in (smart_summary_cache or {}).items()
        }
        rendered = json.dumps(normalized_cache, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(rendered.encode("utf-8")).hexdigest()

    def _build_render_cache_key(
        self,
        *,
        snapshot: InventorySnapshot,
        smart_summary_cache: dict[str, dict[str, Any]] | None,
        selected_slot: int | None,
        history_window_hours: int | None,
        history_panel_open: bool,
        io_chart_mode: str,
        redact_sensitive: bool,
    ) -> str:
        return "|".join(
            [
                "render",
                self._build_snapshot_signature(snapshot),
                f"smart={self._smart_summary_cache_fingerprint(smart_summary_cache)}",
                f"slot={selected_slot if selected_slot is not None else 'none'}",
                f"window={history_window_hours if history_window_hours is not None else 'all'}",
                f"panel={'open' if history_panel_open else 'closed'}",
                f"io={io_chart_mode}",
                f"redact={'partial' if redact_sensitive else 'none'}",
            ]
        )

    def _build_history_snapshot_cache_key(self, snapshot: InventorySnapshot) -> str:
        return f"history|{self._build_snapshot_signature(snapshot)}"

    def _inline_static_assets(self, request: Request, html: str) -> str:
        inline_css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        inline_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        stylesheet_href = str(request.url_for("static", path="style.css"))
        script_src = str(request.url_for("static", path="app.js"))

        html = self._replace_once(
            html,
            f'<link rel="stylesheet" href="{stylesheet_href}">',
            f"<style>\n{inline_css}\n</style>",
        )
        html = self._replace_once(
            html,
            f'<script src="{script_src}" defer></script>',
            f"<script>\n{inline_js}\n</script>",
        )
        return html

    @staticmethod
    def _build_zip_archive(html_filename: str, html_content: bytes) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr(html_filename, html_content)
        return buffer.getvalue()

    @classmethod
    def _rekey_history_cache(cls, history_cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        rekeyed: dict[str, dict[str, Any]] = {}
        for original_key, payload in history_cache.items():
            slot_number = payload.get("slot")
            if not isinstance(slot_number, int):
                rekeyed[original_key] = payload
                continue
            cache_key = cls._build_history_cache_key(
                payload.get("system_id"),
                payload.get("enclosure_id"),
                slot_number,
            )
            rekeyed[cache_key] = payload
        return rekeyed

    def _determine_auto_packaging(self, html_size_bytes: int, zip_size_bytes: int) -> str:
        if html_size_bytes <= self.size_limit_bytes:
            return "html"
        if zip_size_bytes <= self.size_limit_bytes:
            return "zip"
        return "oversize"

    @staticmethod
    def _size_for_packaging(packaging: str, html_size_bytes: int, zip_size_bytes: int) -> int | None:
        if packaging == "html":
            return html_size_bytes
        if packaging == "zip":
            return zip_size_bytes
        return None

    @staticmethod
    def _determine_estimated_packaging(
        *,
        requested_packaging: str,
        auto_packaging: str,
        allow_oversize: bool,
    ) -> str | None:
        if requested_packaging == "html":
            return "html"
        if requested_packaging == "zip":
            return "zip"
        if auto_packaging in {"html", "zip"}:
            return auto_packaging
        if allow_oversize:
            return "zip"
        return None

    @staticmethod
    def _replace_once(content: str, needle: str, replacement: str) -> str:
        if needle not in content:
            fallback = needle.replace("http://testserver", "")
            if fallback in content:
                return content.replace(fallback, replacement, 1)
            raise RuntimeError(f"Unable to inline expected asset tag: {needle}")
        return content.replace(needle, replacement, 1)

    @staticmethod
    def _normalize_packaging(packaging: str | None) -> str:
        return "zip" if packaging == "zip" else "html" if packaging == "html" else "auto"

    @staticmethod
    def _normalize_selected_slot(snapshot: InventorySnapshot, selected_slot: int | None) -> int | None:
        if selected_slot is None:
            return None
        valid_slots = {slot.slot for slot in snapshot.slots}
        return selected_slot if selected_slot in valid_slots else None

    @staticmethod
    def _normalize_history_window_hours(history_window_hours: int | None) -> int | None:
        if history_window_hours is None:
            return None
        try:
            numeric_value = int(history_window_hours)
        except (TypeError, ValueError):
            return 24
        return numeric_value if numeric_value > 0 else None

    @staticmethod
    def _build_history_cache_key(system_id: str | None, enclosure_id: str | None, slot_number: int) -> str:
        system_part = system_id or "system"
        enclosure_part = enclosure_id or "all-enclosures"
        return f"{system_part}|{enclosure_part}|{slot_number}"

    @staticmethod
    def _format_history_window_label(history_window_hours: int | None) -> str:
        if history_window_hours is None:
            return "All"
        if history_window_hours == 24:
            return "24h"
        if history_window_hours < 24:
            return f"{history_window_hours}h"
        if history_window_hours == 24 * 365:
            return "1y"
        if history_window_hours % 24 == 0:
            return f"{history_window_hours // 24}d"
        return f"{history_window_hours}h"

    @staticmethod
    def _build_filename(snapshot: InventorySnapshot, generated_at: datetime) -> str:
        parts = [
            "jbod-snapshot",
            snapshot.selected_system_label or snapshot.selected_system_id or "system",
            snapshot.selected_enclosure_label or snapshot.selected_enclosure_id or "enclosure",
            generated_at.strftime("%Y%m%dT%H%M%SZ"),
        ]
        normalized = [
            re.sub(r"[^a-z0-9]+", "-", part.strip().lower()).strip("-")
            for part in parts
            if part and part.strip()
        ]
        return f"{'-'.join(normalized)}.html"


def format_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ["KiB", "MiB", "GiB"]
    value = float(size_bytes)
    for unit in units:
        value /= 1024.0
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
    return f"{size_bytes} B"
