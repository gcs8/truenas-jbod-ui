from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from starlette.templating import Jinja2Templates

from app import __version__
from app.config import Settings
from app.models.domain import InventorySnapshot
from app.services.history_backend import HistoryBackendClient


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
EXPORT_HISTORY_CONCURRENCY = 4
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


@dataclass(slots=True)
class RenderedSnapshotExport:
    filename: str
    html: str
    size_bytes: int
    snapshot: InventorySnapshot
    history_cache: dict[str, dict[str, Any]]
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
    def __init__(self, snapshot: InventorySnapshot, history_cache: dict[str, dict[str, Any]]) -> None:
        self.serial_values: list[str] = []
        self.partial_identifier_values: list[str] = []
        self.system_aliases = self._build_system_aliases(snapshot)
        self.enclosure_aliases = self._build_enclosure_aliases(snapshot)
        self._collect_known_values(snapshot.model_dump(mode="json"))
        self._collect_known_values(history_cache)
        self.serial_suffix_counts = Counter(self._serial_suffix(value) for value in self.serial_values if self._serial_suffix(value))
        self.token_replacements = self._build_token_replacements()

    def redact_snapshot(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        redacted = self.redact_object(snapshot.model_dump(mode="json"))
        return InventorySnapshot.model_validate(redacted)

    def redact_history_cache(self, history_cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return self.redact_object(history_cache)

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

    async def build_enclosure_snapshot_export(
        self,
        *,
        request: Request,
        snapshot: InventorySnapshot,
        selected_slot: int | None,
        history_window_hours: int | None,
        io_chart_mode: str,
        redact_sensitive: bool = False,
        packaging: str = "auto",
        allow_oversize: bool = False,
    ) -> PackagedSnapshotExport:
        rendered = await self.build_enclosure_snapshot_html(
            request=request,
            snapshot=snapshot,
            selected_slot=selected_slot,
            history_window_hours=history_window_hours,
            io_chart_mode=io_chart_mode,
            redact_sensitive=redact_sensitive,
            requested_packaging=packaging,
        )
        html_bytes = rendered.html.encode("utf-8")
        html_size_bytes = len(html_bytes)
        normalized_packaging = self._normalize_packaging(packaging)
        zip_bytes = self._build_zip_archive(rendered.filename, html_bytes)
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

    async def build_enclosure_snapshot_html(
        self,
        *,
        request: Request,
        snapshot: InventorySnapshot,
        selected_slot: int | None,
        history_window_hours: int | None,
        io_chart_mode: str,
        redact_sensitive: bool = False,
        requested_packaging: str = "auto",
    ) -> RenderedSnapshotExport:
        normalized_slot = self._normalize_selected_slot(snapshot, selected_slot)
        normalized_window_hours = self._normalize_history_window_hours(history_window_hours)
        normalized_chart_mode = "average" if io_chart_mode == "average" else "total"
        generated_at = datetime.now(timezone.utc)

        history_cache = await self._collect_slot_histories(snapshot)
        snapshot_for_export = snapshot
        history_cache_for_export = history_cache
        if redact_sensitive:
            redactor = SnapshotRedactor(snapshot, history_cache)
            snapshot_for_export = redactor.redact_snapshot(snapshot)
            history_cache_for_export = redactor.redact_history_cache(history_cache)

        tracked_slots = sum(1 for payload in history_cache_for_export.values() if payload.get("available"))
        metric_sample_count = sum(
            len(samples)
            for payload in history_cache_for_export.values()
            for samples in (payload.get("metrics") or {}).values()
        )
        history_available = tracked_slots > 0

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
            "selected_slot": normalized_slot,
            "io_chart_mode": normalized_chart_mode,
            "requested_packaging": self._normalize_packaging(requested_packaging),
            "redaction": "redacted" if redact_sensitive else "full",
            "offline": True,
            "size_limit_bytes": self.size_limit_bytes,
            "size_limit_label": format_bytes(self.size_limit_bytes),
        }
        history_summary = {
            "counts": {
                "tracked_slots": tracked_slots,
                "metric_sample_count": metric_sample_count,
            },
            "collector": {
                "last_completed_at": generated_at.isoformat(),
            },
        }

        context = {
            "request": request,
            "snapshot": snapshot_for_export,
            "settings": self.settings,
            "initial_snapshot_json": json.dumps(snapshot_for_export.model_dump(mode="json")),
            "history_configured": history_available,
            "snapshot_mode": True,
            "snapshot_export_meta": export_meta,
            "snapshot_export_meta_json": json.dumps(export_meta),
            "preloaded_history_json": json.dumps(history_cache_for_export),
            "preloaded_history_summary_json": json.dumps(history_summary),
            "initial_selected_slot_json": json.dumps(normalized_slot),
            "initial_history_timeframe_hours_json": json.dumps(normalized_window_hours),
            "initial_history_panel_open_json": json.dumps(bool(normalized_slot is not None and history_available)),
            "initial_history_io_chart_mode_json": json.dumps(normalized_chart_mode),
        }

        template = self.templates.env.get_template("index.html")
        html = template.render(context)
        html = self._inline_static_assets(request, html)

        filename = self._build_filename(snapshot_for_export, generated_at)
        return RenderedSnapshotExport(
            filename=filename,
            html=html,
            size_bytes=len(html.encode("utf-8")),
            snapshot=snapshot_for_export,
            history_cache=history_cache_for_export,
            history_available=history_available,
            export_meta=export_meta,
            history_summary=history_summary,
        )

    async def _collect_slot_histories(self, snapshot: InventorySnapshot) -> dict[str, dict[str, Any]]:
        if not self.history_backend.configured:
            return {}

        system_id = snapshot.selected_system_id
        enclosure_id = snapshot.selected_enclosure_id
        semaphore = asyncio.Semaphore(EXPORT_HISTORY_CONCURRENCY)

        async def fetch(slot_number: int) -> tuple[str, dict[str, Any]]:
            async with semaphore:
                payload = await self.history_backend.get_slot_history(slot_number, system_id, enclosure_id)
            return self._build_history_cache_key(system_id, enclosure_id, slot_number), payload

        results = await asyncio.gather(*(fetch(slot.slot) for slot in snapshot.slots))
        return {key: payload for key, payload in results}

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
