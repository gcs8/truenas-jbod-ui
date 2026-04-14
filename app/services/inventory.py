from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app import __version__
from app.config import Settings, SystemConfig
from app.models.domain import (
    EnclosureOption,
    InventorySnapshot,
    InventorySummary,
    LedAction,
    MappingBundle,
    ManualMapping,
    MultipathMember,
    MultipathView,
    SmartSummaryView,
    SlotState,
    SlotView,
    SourceStatus,
    SystemOption,
)
from app.services.mapping_store import MappingStore
from app.services.parsers import (
    ParsedSSHData,
    build_slot_candidates_from_ses_enclosures,
    extract_enclosure_slot_candidates,
    format_bytes,
    merge_slot_candidate_maps,
    normalize_device_name,
    normalize_hex_identifier,
    normalize_lookup_keys,
    normalize_text,
    parse_pool_query_topology,
    parse_smart_test_results,
    parse_smartctl_text_enrichment,
    parse_smartctl_summary,
    parse_ssh_outputs,
    shift_hex_identifier,
)
from app.services.ssh_probe import SSHProbe
from app.services.truenas_ws import TrueNASAPIError, TrueNASRawData, TrueNASWebsocketClient

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_layout_rows(rows: int, columns: int, slot_count: int) -> list[list[int]]:
    layout_rows: list[list[int]] = []
    for row_index in reversed(range(rows)):
        start = row_index * columns
        row_slots = [slot for slot in range(start, start + columns) if slot < slot_count]
        if row_slots:
            layout_rows.append(row_slots)
    return layout_rows


def build_lunid_aliases(value: str | None, platform: str) -> set[str]:
    aliases: set[str] = set()
    normalized = normalize_hex_identifier(value)
    if normalized:
        aliases.add(normalized)

    # CORE shelves have mostly matched on exact lunid or +1 shifted SAS hints.
    # On the user's SCALE host, the rear SSD enclosure exposes AES SAS addresses
    # that can differ from disk.query lunids by up to two hex counts, so we keep
    # the match window intentionally small but a little wider there.
    deltas = (1,) if platform != "scale" else (-2, -1, 1, 2)
    for delta in deltas:
        shifted = shift_hex_identifier(value, delta)
        if shifted:
            aliases.add(shifted)
    return aliases


def resolve_persistent_id(*candidates: str | None) -> tuple[str | None, str | None]:
    for candidate in candidates:
        value = normalize_text(candidate)
        if not value:
            continue

        lowered = value.lower()
        leaf = value.rsplit("/", 1)[-1]
        leaf_lowered = leaf.lower()

        if "/dev/disk/by-partuuid/" in lowered:
            return leaf, "PARTUUID"
        if "/dev/disk/by-id/" in lowered:
            if leaf_lowered.startswith("wwn-"):
                return leaf, "WWN"
            return leaf, "Disk ID"
        if lowered.startswith("gptid/") or "/gptid/" in lowered:
            return value, "GPTID"
        if leaf_lowered.startswith("wwn-"):
            return leaf, "WWN"

        return value, None

    return None, None


@dataclass(slots=True)
class DiskRecord:
    raw: dict[str, Any]
    device_name: str | None
    path_device_name: str | None
    multipath_name: str | None
    multipath_member: str | None
    serial: str | None
    model: str | None
    size_bytes: int | None
    identifier: str | None
    health: str | None
    pool_name: str | None
    lunid: str | None
    bus: str | None
    temperature_c: int | None
    last_smart_test_type: str | None
    last_smart_test_status: str | None
    last_smart_test_lifetime_hours: int | None
    logical_block_size: int | None
    physical_block_size: int | None
    enclosure_id: str | None
    slot: int | None
    lookup_keys: set[str]


class InventoryService:
    def __init__(
        self,
        settings: Settings,
        system: SystemConfig,
        truenas_client: TrueNASWebsocketClient,
        ssh_probe: SSHProbe,
        mapping_store: MappingStore,
    ) -> None:
        self.settings = settings
        self.system = system
        self.truenas_client = truenas_client
        self.ssh_probe = ssh_probe
        self.mapping_store = mapping_store
        self._cache: dict[str, InventorySnapshot] = {}
        self._cache_until: dict[str, datetime] = {}
        self._smart_cache: dict[str, SmartSummaryView] = {}
        self._smart_cache_until: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def get_snapshot(
        self,
        force_refresh: bool = False,
        selected_enclosure_id: str | None = None,
    ) -> InventorySnapshot:
        async with self._lock:
            now = utcnow()
            cache_key = selected_enclosure_id or "__default__"
            cached = self._cache.get(cache_key)
            cache_until = self._cache_until.get(cache_key, datetime.min.replace(tzinfo=timezone.utc))
            if not force_refresh and cached and now < cache_until:
                return cached

            snapshot = await self._build_snapshot(selected_enclosure_id=selected_enclosure_id)
            self._cache[cache_key] = snapshot
            self._cache_until[cache_key] = now + timedelta(seconds=self.settings.app.cache_ttl_seconds)
            return snapshot

    async def set_slot_led(
        self,
        slot: int,
        action: LedAction,
        selected_enclosure_id: str | None = None,
    ) -> None:
        snapshot = await self.get_snapshot(force_refresh=True, selected_enclosure_id=selected_enclosure_id)
        slot_view = next((item for item in snapshot.slots if item.slot == slot), None)
        if not slot_view:
            raise TrueNASAPIError(f"Slot {slot:02d} is not present in the current snapshot.")
        if not slot_view.led_supported or not slot_view.led_backend:
            raise TrueNASAPIError(
                slot_view.led_reason
                or f"LED control is not available for slot {slot:02d} on this system."
            )

        if slot_view.led_backend == "api":
            api_slot_number = slot + self.settings.layout.api_slot_number_base
            try:
                await self.truenas_client.set_slot_status(slot_view.enclosure_id or "", api_slot_number, action.value)
            except TrueNASAPIError:
                if slot_view.ssh_ses_device and slot_view.ssh_ses_element_id is not None:
                    await self._set_slot_led_over_ssh(slot_view, action)
                else:
                    raise
        elif slot_view.led_backend in {"ssh", "scale_sg_ses"}:
            await self._set_slot_led_over_ssh(slot_view, action)
        else:
            raise TrueNASAPIError(
                slot_view.led_reason
                or f"LED backend {slot_view.led_backend!r} is not supported for slot {slot:02d}."
            )

        await self.get_snapshot(force_refresh=True, selected_enclosure_id=selected_enclosure_id)

    async def save_mapping(
        self,
        slot: int,
        payload: dict[str, Any],
        selected_enclosure_id: str | None = None,
    ) -> ManualMapping:
        snapshot = await self.get_snapshot(force_refresh=True, selected_enclosure_id=selected_enclosure_id)
        slot_view = next((item for item in snapshot.slots if item.slot == slot), None)
        enclosure_id = slot_view.enclosure_id if slot_view else None
        mapping = ManualMapping(
            system_id=self.system.id,
            slot=slot,
            enclosure_id=enclosure_id,
            **payload,
        )
        saved = self.mapping_store.save_mapping(mapping)
        await self.get_snapshot(force_refresh=True, selected_enclosure_id=selected_enclosure_id)
        return saved

    async def clear_mapping(self, slot: int, selected_enclosure_id: str | None = None) -> bool:
        snapshot = await self.get_snapshot(force_refresh=True, selected_enclosure_id=selected_enclosure_id)
        slot_view = next((item for item in snapshot.slots if item.slot == slot), None)
        enclosure_id = slot_view.enclosure_id if slot_view else None
        cleared = self.mapping_store.clear_mapping(self.system.id, enclosure_id, slot)
        await self.get_snapshot(force_refresh=True, selected_enclosure_id=selected_enclosure_id)
        return cleared

    async def get_slot_smart_summary(
        self,
        slot: int,
        selected_enclosure_id: str | None = None,
    ) -> SmartSummaryView:
        snapshot = await self.get_snapshot(selected_enclosure_id=selected_enclosure_id)
        slot_view = next((item for item in snapshot.slots if item.slot == slot), None)
        if not slot_view:
            raise TrueNASAPIError(f"Slot {slot:02d} is not present in the current snapshot.")

        candidates = self._smart_candidate_devices(slot_view)
        if not candidates:
            return self._fallback_smart_summary(
                slot_view,
                "No SMART-capable device path is available for this slot.",
            )

        cache_key = f"{self.system.id}|{'|'.join(candidates)}"
        cache_until = self._smart_cache_until.get(cache_key, datetime.min.replace(tzinfo=timezone.utc))
        cached = self._smart_cache.get(cache_key)
        if cached and utcnow() < cache_until:
            return cached

        if self.system.truenas.platform == "scale":
            summary, error_message = await self._fetch_smart_summary_over_ssh(candidates)
            if summary is not None:
                summary = self._merge_smart_summary(slot_view, summary)
                self._smart_cache[cache_key] = summary
                self._smart_cache_until[cache_key] = utcnow() + timedelta(minutes=5)
                return summary

            return self._fallback_smart_summary(
                slot_view,
                error_message
                or "Detailed SMART JSON is not currently available through the SCALE API on this system.",
            )

        last_error: str | None = None
        for candidate in candidates:
            try:
                payload = await self.truenas_client.fetch_disk_smartctl(candidate, ["-a", "-j"])
            except TrueNASAPIError as exc:
                last_error = str(exc)
                continue

            summary = self._merge_smart_summary(
                slot_view,
                SmartSummaryView.model_validate(parse_smartctl_summary(payload)),
            )

            self._smart_cache[cache_key] = summary
            self._smart_cache_until[cache_key] = utcnow() + timedelta(minutes=5)
            return summary

        return self._fallback_smart_summary(
            slot_view,
            last_error or "SMART summary is unavailable for this slot.",
        )

    async def _fetch_smart_summary_over_ssh(
        self,
        candidates: list[str],
    ) -> tuple[SmartSummaryView | None, str | None]:
        if not self.system.ssh.enabled:
            return None, (
                "Detailed SMART JSON is not currently available through the SCALE API on this system, "
                "and SSH fallback is disabled."
            )

        last_error: str | None = None
        for candidate in candidates:
            device_path = candidate if candidate.startswith("/dev/") else f"/dev/{candidate}"
            command = shlex.join(
                [
                    "sudo",
                    "-n",
                    "/usr/sbin/smartctl",
                    "-x",
                    "-j",
                    device_path,
                ]
            )
            result = await self.ssh_probe.run_command(command)
            summary = None
            if result.stdout.strip():
                parsed = parse_smartctl_summary(result.stdout)
                candidate_summary = SmartSummaryView.model_validate(parsed)
                # smartctl commonly returns advisory non-zero exit codes even when the
                # JSON payload is intact and contains useful SMART data.
                if candidate_summary.available or candidate_summary.message != "SMART JSON parsing failed.":
                    summary = candidate_summary
            if summary is None:
                detail = result.stderr.strip() or result.stdout.strip() or "Unknown SSH smartctl error."
                last_error = f"{device_path}: {detail}"
                continue

            text_command = shlex.join(
                [
                    "sudo",
                    "-n",
                    "/usr/sbin/smartctl",
                    "-x",
                    device_path,
                ]
            )
            text_result = await self.ssh_probe.run_command(text_command)
            if text_result.stdout.strip():
                enrichment = parse_smartctl_text_enrichment(text_result.stdout)
                summary.read_cache_enabled = enrichment.get("read_cache_enabled")
                summary.writeback_cache_enabled = enrichment.get("writeback_cache_enabled")
            if summary.available or summary.message != "SMART JSON parsing failed.":
                return summary, None
            last_error = f"{device_path}: {summary.message or 'SMART JSON parsing failed.'}"

        return None, last_error

    async def export_mapping_bundle(self, selected_enclosure_id: str | None = None) -> MappingBundle:
        return MappingBundle(
            app_version=__version__,
            system_id=self.system.id,
            enclosure_id=selected_enclosure_id,
            mappings=self.mapping_store.list_mappings(self.system.id, selected_enclosure_id),
        )

    async def import_mapping_bundle(
        self,
        bundle: MappingBundle,
        selected_enclosure_id: str | None = None,
    ) -> int:
        rewritten: list[ManualMapping] = []
        for mapping in bundle.mappings:
            target_enclosure_id = selected_enclosure_id or mapping.enclosure_id
            rewritten.append(
                mapping.model_copy(
                    update={
                        "system_id": self.system.id,
                        "enclosure_id": target_enclosure_id,
                        "source": "import",
                    }
                )
            )

        saved_count = self.mapping_store.replace_mappings(self.system.id, selected_enclosure_id, rewritten)
        await self.get_snapshot(force_refresh=True, selected_enclosure_id=selected_enclosure_id)
        return saved_count

    async def _build_snapshot(self, selected_enclosure_id: str | None = None) -> InventorySnapshot:
        warnings: list[str] = []
        ssh_data = ParsedSSHData()
        sources = {
            "api": SourceStatus(enabled=True, ok=False, message=None),
            "ssh": SourceStatus(enabled=self.system.ssh.enabled, ok=not self.system.ssh.enabled, message=None),
        }

        try:
            raw_data = await self.truenas_client.fetch_all()
            sources["api"] = SourceStatus(enabled=True, ok=True, message="TrueNAS API reachable.")
        except Exception as exc:
            logger.exception("Failed to fetch TrueNAS API data")
            raw_data = TrueNASRawData(
                enclosures=[],
                disks=[],
                pools=[],
                disk_temperatures={},
                smart_test_results=[],
            )
            sources["api"] = SourceStatus(enabled=True, ok=False, message=str(exc))
            warnings.append("TrueNAS API is unreachable. Slot details may be partial or unavailable.")

        if self.system.ssh.enabled:
            try:
                command_results = await self.ssh_probe.run_commands()
                outputs = {item.command: item.stdout for item in command_results if item.ok}
                failures = [item for item in command_results if not item.ok]
                ssh_data = parse_ssh_outputs(
                    outputs,
                    self.settings.layout.slot_count,
                    self.system.truenas.enclosure_filter,
                    selected_enclosure_id,
                )
                sources["ssh"] = SourceStatus(
                    enabled=True,
                    ok=not failures,
                    message="SSH probe completed." if not failures else "Some SSH commands failed.",
                )
                for failure in failures:
                    warnings.append(f"SSH command failed: {failure.command} (exit {failure.exit_code})")
            except Exception as exc:
                logger.exception("Failed to collect SSH diagnostics")
                sources["ssh"] = SourceStatus(enabled=True, ok=False, message=str(exc))
                warnings.append("SSH mode is enabled but could not collect fallback command output.")

        has_scale_linux_ses = self._has_scale_linux_ses(ssh_data)
        if not raw_data.enclosures:
            if self.system.truenas.platform == "scale" and has_scale_linux_ses:
                warnings.append(
                    "TrueNAS SCALE did not return enclosure rows, so this view is using Linux SES AES page parsing "
                    "for slot mapping on the selected enclosure."
                )
            elif self.system.truenas.platform == "scale":
                warnings.append(
                    "TrueNAS SCALE did not return mappable enclosure rows. This first-pass SCALE mode can still "
                    "show disk and pool metadata, but physical slot mapping and LED control will require future "
                    "Linux enclosure support or manual calibration."
                )
            else:
                warnings.append(
                    "TrueNAS API returned no enclosure rows. API-only mode can still show disk and pool metadata, "
                    "but physical slot mapping on this system will require SSH enrichment or manual calibration."
                )

        slots, available_enclosures, selected_meta, layout_rows, layout_slot_count, layout_columns = self._correlate(
            raw_data,
            ssh_data,
            warnings,
            selected_enclosure_id=selected_enclosure_id,
        )
        option_by_id = {item.id: item for item in available_enclosures}
        resolved_enclosure_id = None
        if selected_enclosure_id and selected_enclosure_id in option_by_id:
            resolved_enclosure_id = selected_enclosure_id
        elif selected_meta.get("id") and selected_meta["id"] in option_by_id:
            resolved_enclosure_id = selected_meta["id"]
        elif available_enclosures:
            resolved_enclosure_id = available_enclosures[0].id

        selected_option = option_by_id.get(resolved_enclosure_id) if resolved_enclosure_id else None
        selected_slot = None
        if resolved_enclosure_id:
            selected_slot = next((slot for slot in slots if slot.enclosure_id == resolved_enclosure_id), None)
        if selected_slot is None:
            selected_slot = next((slot for slot in slots if slot.enclosure_id), slots[0] if slots else None)

        summary = InventorySummary(
            disk_count=len(raw_data.disks),
            pool_count=len(raw_data.pools),
            enclosure_count=len(raw_data.enclosures),
            mapped_slot_count=sum(1 for slot in slots if slot.device_name),
            manual_mapping_count=self.mapping_store.count_for_system(self.system.id),
            ssh_slot_hint_count=max(
                len(ssh_data.ses_slot_candidates),
                sum(len(enclosure.slots) for enclosure in ssh_data.ses_enclosures),
            ),
        )
        return InventorySnapshot(
            slots=slots,
            layout_rows=layout_rows,
            layout_slot_count=layout_slot_count,
            layout_columns=layout_columns,
            refresh_interval_seconds=self.settings.app.refresh_interval_seconds,
            selected_system_id=self.system.id,
            selected_system_label=self.system.label,
            selected_system_platform=self.system.truenas.platform,
            warnings=warnings,
            last_updated=utcnow(),
            generated_at=utcnow(),
            systems=[
                SystemOption(
                    id=system.id,
                    label=system.label or system.id,
                    platform=system.truenas.platform,
                )
                for system in self.settings.systems
            ],
            enclosures=available_enclosures,
            selected_enclosure_id=selected_option.id if selected_option else selected_slot.enclosure_id if selected_slot else None,
            selected_enclosure_label=selected_option.label if selected_option else selected_slot.enclosure_label if selected_slot else None,
            selected_enclosure_name=selected_option.name if selected_option else selected_slot.enclosure_name if selected_slot else None,
            sources=sources,
            summary=summary,
        )

    def _correlate(
        self,
        raw_data: TrueNASRawData,
        ssh_data: ParsedSSHData,
        warnings: list[str],
        selected_enclosure_id: str | None = None,
    ) -> tuple[list[SlotView], list[EnclosureOption], dict[str, str | None], list[list[int]], int, int]:
        if self.system.truenas.platform == "scale" and self._has_scale_linux_ses(ssh_data):
            return self._correlate_scale_linux(raw_data, ssh_data, warnings, selected_enclosure_id)

        slot_count = self.settings.layout.slot_count
        api_candidates, api_selected_meta = extract_enclosure_slot_candidates(
            raw_data.enclosures,
            self.system.truenas.enclosure_filter,
            slot_count,
            self.settings.layout.api_slot_number_base,
            selected_enclosure_id,
        )
        selected_meta = self._merge_enclosure_meta(ssh_data.ses_selected_meta, api_selected_meta)
        available_enclosures = self._build_enclosure_options(raw_data, ssh_data, selected_meta)
        api_enclosure_ids = {
            enclosure_id
            for enclosure_id in (
                normalize_text(candidate.get("enclosure_id")) for candidate in api_candidates.values()
            )
            if enclosure_id
        }
        if api_selected_meta.get("id"):
            api_enclosure_ids.add(api_selected_meta["id"])
        slot_candidates = merge_slot_candidate_maps(ssh_data.ses_slot_candidates, api_candidates)
        api_topology_members = parse_pool_query_topology(raw_data.pools)
        selected_option = self._resolve_selected_enclosure_option(available_enclosures, selected_enclosure_id, selected_meta)
        layout_rows_count = selected_option.rows if selected_option and selected_option.rows else self.settings.layout.rows
        layout_columns = selected_option.columns if selected_option and selected_option.columns else self.settings.layout.columns
        layout_slot_count = selected_option.slot_count if selected_option and selected_option.slot_count else slot_count
        disk_records = self._build_disk_records(
            raw_data.disks,
            ssh_data,
            raw_data.disk_temperatures,
            parse_smart_test_results(raw_data.smart_test_results),
        )

        disks_by_key: dict[str, DiskRecord] = {}
        disks_by_slot: dict[tuple[str | None, int], DiskRecord] = {}
        disks_by_sas: dict[str, DiskRecord] = {}
        for disk in disk_records:
            for key in disk.lookup_keys:
                disks_by_key[key] = disk
            if disk.slot is not None:
                disks_by_slot[(disk.enclosure_id, disk.slot)] = disk
                disks_by_slot[(None, disk.slot)] = disk
            for alias in build_lunid_aliases(disk.lunid, self.system.truenas.platform):
                disks_by_sas[alias] = disk

        slot_views: list[SlotView] = []

        for slot in range(layout_slot_count):
            row_index = slot // layout_columns
            column_index = slot % layout_columns
            candidate = slot_candidates.get(slot, {})
            enclosure_id = selected_meta.get("id") or normalize_text(candidate.get("enclosure_id"))
            mapping = self.mapping_store.get_mapping(self.system.id, enclosure_id, slot)
            disk = self._resolve_disk_for_slot(
                slot,
                enclosure_id,
                mapping,
                disks_by_key,
                disks_by_slot,
                disks_by_sas,
                candidate,
                ssh_data,
            )
            slot_view = self._build_slot_view(
                slot=slot,
                row_index=row_index,
                column_index=column_index,
                enclosure_meta=selected_meta,
                raw_slot_status=candidate,
                disk=disk,
                mapping=mapping,
                ssh_data=ssh_data,
                api_topology_members=api_topology_members,
                api_enclosure_ids=api_enclosure_ids,
            )
            if mapping and not disk:
                warnings.append(f"Manual mapping for slot {slot:02d} did not match any current disk.")
            slot_views.append(slot_view)

        return (
            slot_views,
            available_enclosures,
            selected_meta,
            build_layout_rows(layout_rows_count, layout_columns, layout_slot_count),
            layout_slot_count,
            layout_columns,
        )

    def _correlate_scale_linux(
        self,
        raw_data: TrueNASRawData,
        ssh_data: ParsedSSHData,
        warnings: list[str],
        selected_enclosure_id: str | None,
    ) -> tuple[list[SlotView], list[EnclosureOption], dict[str, str | None], list[list[int]], int, int]:
        available_enclosures = self._build_scale_linux_enclosure_options(ssh_data)
        selected_option = self._resolve_selected_enclosure_option(available_enclosures, selected_enclosure_id, {})
        if selected_option is None:
            return [], [], {"id": None, "label": None, "name": None}, [], 0, 0

        slot_count = selected_option.slot_count or self.settings.layout.slot_count
        ssh_candidates, ssh_meta = build_slot_candidates_from_ses_enclosures(
            ssh_data.ses_enclosures,
            slot_count,
            self.system.truenas.enclosure_filter,
            selected_option.id,
        )
        api_candidates, api_selected_meta = extract_enclosure_slot_candidates(
            raw_data.enclosures,
            self.system.truenas.enclosure_filter,
            slot_count,
            self.settings.layout.api_slot_number_base,
            None,
        )
        selected_meta = self._merge_enclosure_meta(self._enclosure_option_meta(selected_option), api_selected_meta)
        selected_meta = self._merge_enclosure_meta(selected_meta, ssh_meta)
        slot_candidates = merge_slot_candidate_maps(ssh_candidates, api_candidates)
        api_topology_members = parse_pool_query_topology(raw_data.pools)
        api_enclosure_ids: set[str] = set()
        disk_records = self._build_disk_records(
            raw_data.disks,
            ssh_data,
            raw_data.disk_temperatures,
            parse_smart_test_results(raw_data.smart_test_results),
        )

        disks_by_key: dict[str, DiskRecord] = {}
        disks_by_slot: dict[tuple[str | None, int], DiskRecord] = {}
        disks_by_sas: dict[str, DiskRecord] = {}
        for disk in disk_records:
            for key in disk.lookup_keys:
                disks_by_key[key] = disk
            if disk.slot is not None:
                disks_by_slot[(disk.enclosure_id, disk.slot)] = disk
                disks_by_slot[(None, disk.slot)] = disk
            if disk.lunid:
                for alias in build_lunid_aliases(disk.lunid, self.system.truenas.platform):
                    disks_by_sas[alias] = disk

        rows = selected_option.rows or self.settings.layout.rows
        columns = selected_option.columns or self.settings.layout.columns
        layout_rows = selected_option.slot_layout or build_layout_rows(rows, columns, slot_count)
        slot_positions = {
            slot_number: (row_index, column_index)
            for row_index, row in enumerate(layout_rows)
            for column_index, slot_number in enumerate(row)
        }
        slot_views: list[SlotView] = []

        for slot in range(slot_count):
            candidate = slot_candidates.get(slot, {})
            mapping = self.mapping_store.get_mapping(self.system.id, selected_option.id, slot)
            disk = self._resolve_disk_for_slot(
                slot,
                selected_option.id,
                mapping,
                disks_by_key,
                disks_by_slot,
                disks_by_sas,
                candidate,
                ssh_data,
            )
            slot_view = self._build_slot_view(
                slot=slot,
                row_index=slot_positions.get(slot, (slot // columns, slot % columns))[0],
                column_index=slot_positions.get(slot, (slot // columns, slot % columns))[1],
                enclosure_meta=selected_meta,
                raw_slot_status=candidate,
                disk=disk,
                mapping=mapping,
                ssh_data=ssh_data,
                api_topology_members=api_topology_members,
                api_enclosure_ids=api_enclosure_ids,
            )
            if mapping and not disk:
                warnings.append(f"Manual mapping for slot {slot:02d} did not match any current disk.")
            slot_views.append(slot_view)

        return (
            slot_views,
            available_enclosures,
            selected_meta,
            layout_rows,
            slot_count,
            columns,
        )

    def _build_enclosure_options(
        self,
        raw_data: TrueNASRawData,
        ssh_data: ParsedSSHData,
        selected_meta: dict[str, str | None],
    ) -> list[EnclosureOption]:
        options: list[EnclosureOption] = []
        seen_ids: set[str] = set()
        filter_text = normalize_text(self.system.truenas.enclosure_filter)
        filter_value = filter_text.lower() if filter_text else None

        for enclosure in raw_data.enclosures:
            enclosure_id = normalize_text(str(enclosure.get("id") or ""))
            if not enclosure_id:
                continue
            enclosure_name = normalize_text(str(enclosure.get("name") or ""))
            enclosure_label = normalize_text(str(enclosure.get("label") or ""))
            haystack = " ".join(filter(None, [enclosure_id, enclosure_name, enclosure_label])).lower()
            if filter_value and filter_value not in haystack:
                continue
            if enclosure_id in seen_ids:
                continue
            seen_ids.add(enclosure_id)
            options.append(
                EnclosureOption(
                    id=enclosure_id,
                    label=enclosure_label or enclosure_name or enclosure_id,
                    name=enclosure_name,
                )
            )

        if self.system.truenas.platform == "scale":
            for enclosure in self._build_scale_linux_enclosure_options(ssh_data):
                if enclosure.id in seen_ids:
                    continue
                seen_ids.add(enclosure.id)
                options.append(enclosure)

        selected_id = normalize_text(selected_meta.get("id"))
        if selected_id and selected_id not in seen_ids:
            options.append(
                EnclosureOption(
                    id=selected_id,
                    label=normalize_text(selected_meta.get("label")) or selected_id,
                    name=normalize_text(selected_meta.get("name")),
                )
            )

        return options

    def _build_scale_linux_enclosure_options(self, ssh_data: ParsedSSHData) -> list[EnclosureOption]:
        options: list[EnclosureOption] = []
        for enclosure in ssh_data.ses_enclosures:
            if not enclosure.enclosure_id:
                continue
            slot_count = max(enclosure.slots) + 1 if enclosure.slots else 0
            options.append(
                EnclosureOption(
                    id=enclosure.enclosure_id,
                    label=enclosure.enclosure_label or enclosure.enclosure_name or enclosure.enclosure_id,
                    name=enclosure.enclosure_name,
                    rows=enclosure.layout_rows,
                    columns=enclosure.layout_columns,
                    slot_count=slot_count,
                    slot_layout=enclosure.slot_layout,
                )
            )

        return sorted(
            options,
            key=lambda item: (
                0 if "front" in item.label.lower() else 1 if "rear" in item.label.lower() else 2,
                item.slot_count or 0,
                item.label,
            ),
        )

    @staticmethod
    def _enclosure_option_meta(option: EnclosureOption) -> dict[str, str | None]:
        return {
            "id": option.id,
            "label": option.label,
            "name": option.name,
        }

    @staticmethod
    def _resolve_selected_enclosure_option(
        options: list[EnclosureOption],
        selected_enclosure_id: str | None,
        selected_meta: dict[str, str | None],
    ) -> EnclosureOption | None:
        option_by_id = {item.id: item for item in options}
        if selected_enclosure_id and selected_enclosure_id in option_by_id:
            return option_by_id[selected_enclosure_id]
        selected_meta_id = normalize_text(selected_meta.get("id"))
        if selected_meta_id and selected_meta_id in option_by_id:
            return option_by_id[selected_meta_id]
        return options[0] if options else None

    @staticmethod
    def _has_scale_linux_ses(ssh_data: ParsedSSHData) -> bool:
        return any(
            enclosure.ses_device and enclosure.ses_device.startswith("/dev/sg")
            for enclosure in ssh_data.ses_enclosures
        )

    def _build_disk_records(
        self,
        disks: list[dict[str, Any]],
        ssh_data: ParsedSSHData,
        disk_temperatures: dict[str, int],
        smart_tests: dict[str, dict[str, Any]],
    ) -> list[DiskRecord]:
        records: list[DiskRecord] = []
        for disk in disks:
            device_name = normalize_device_name(
                disk.get("devname") or disk.get("name") or disk.get("device") or disk.get("disk")
            )
            path_device_name = normalize_device_name(disk.get("name"))
            multipath_name = normalize_text(disk.get("multipath_name"))
            multipath_member = normalize_device_name(disk.get("multipath_member"))
            serial = normalize_text(disk.get("serial") or disk.get("serial_lunid") or disk.get("lunid"))
            model = normalize_text(disk.get("model"))
            size_bytes = disk.get("size") if isinstance(disk.get("size"), int) else None
            logical_block_size = self._extract_logical_block_size(disk, size_bytes)
            physical_block_size = self._extract_physical_block_size(disk)
            identifier = normalize_text(disk.get("identifier"))
            temperature_c = self._lookup_disk_temperature(disk_temperatures, path_device_name, device_name, multipath_member)
            latest_test = self._lookup_smart_test(smart_tests, path_device_name, device_name, multipath_member)
            health = normalize_text(
                disk.get("status")
                or disk.get("health")
                or disk.get("smart_status")
                or disk.get("smartstatus")
            )
            pool_name = self._extract_pool_name(disk)
            enclosure_id, slot = self._extract_enclosure_slot(disk)
            lookup_keys = set()
            for value in (
                device_name,
                path_device_name,
                multipath_member,
                serial,
                model,
                identifier,
                disk.get("zfs_guid"),
                disk.get("name"),
                disk.get("devname"),
                disk.get("multipath_name"),
                disk.get("multipath_member"),
            ):
                lookup_keys.update(normalize_lookup_keys(str(value) if value is not None else None))

            if multipath_name:
                lookup_keys.update(normalize_lookup_keys(f"multipath/{multipath_name}"))

            if device_name:
                gptid = ssh_data.glabel.device_to_gptid.get(device_name.lower())
                if gptid:
                    lookup_keys.update(normalize_lookup_keys(gptid))

            lookup_keys.update(build_lunid_aliases(disk.get("lunid"), self.system.truenas.platform))

            records.append(
                DiskRecord(
                    raw=disk,
                    device_name=device_name,
                    path_device_name=path_device_name,
                    multipath_name=multipath_name,
                    multipath_member=multipath_member,
                    serial=serial,
                    model=model,
                    size_bytes=size_bytes,
                    identifier=identifier,
                    health=health,
                    pool_name=pool_name,
                    lunid=normalize_text(disk.get("lunid")),
                    bus=normalize_text(disk.get("bus")),
                    temperature_c=temperature_c,
                    last_smart_test_type=normalize_text(latest_test.get("description")) if latest_test else None,
                    last_smart_test_status=normalize_text(latest_test.get("status_verbose") or latest_test.get("status")) if latest_test else None,
                    last_smart_test_lifetime_hours=latest_test.get("lifetime") if latest_test else None,
                    logical_block_size=logical_block_size,
                    physical_block_size=physical_block_size,
                    enclosure_id=enclosure_id,
                    slot=slot,
                    lookup_keys=lookup_keys,
                )
            )
        return records

    def _extract_pool_name(self, disk: dict[str, Any]) -> str | None:
        pool = disk.get("pool")
        if isinstance(pool, str):
            return normalize_text(pool)
        if isinstance(pool, dict):
            return normalize_text(pool.get("name"))
        return normalize_text(disk.get("pool_name"))

    def _extract_enclosure_slot(self, disk: dict[str, Any]) -> tuple[str | None, int | None]:
        enclosure = disk.get("enclosure")
        enclosure_id = None
        raw_slot = None

        if isinstance(enclosure, dict):
            enclosure_id = normalize_text(enclosure.get("id") or enclosure.get("name"))
            raw_slot = enclosure.get("slot") or enclosure.get("number") or enclosure.get("slot_number")
        elif isinstance(enclosure, str):
            enclosure_id = normalize_text(enclosure)

        for key in ("enclosure_slot", "slot", "enclosure_slot_number"):
            if raw_slot is None and key in disk:
                raw_slot = disk.get(key)

        if isinstance(raw_slot, str) and raw_slot.isdigit():
            raw_slot = int(raw_slot)
        if isinstance(raw_slot, int):
            return enclosure_id, raw_slot - self.settings.layout.api_slot_number_base
        return enclosure_id, None

    def _resolve_disk_for_slot(
        self,
        slot: int,
        enclosure_id: str | None,
        mapping: ManualMapping | None,
        disks_by_key: dict[str, DiskRecord],
        disks_by_slot: dict[tuple[str | None, int], DiskRecord],
        disks_by_sas: dict[str, DiskRecord],
        raw_slot_status: dict[str, Any],
        ssh_data: ParsedSSHData,
    ) -> DiskRecord | None:
        if mapping:
            for candidate in (mapping.serial, mapping.gptid, mapping.device_name):
                if not candidate:
                    continue
                for key in normalize_lookup_keys(candidate):
                    disk = disks_by_key.get(key)
                    if disk:
                        return disk

        direct = disks_by_slot.get((enclosure_id, slot)) or disks_by_slot.get((None, slot))
        if direct:
            return direct

        for candidate in raw_slot_status.get("device_names", []) or []:
            normalized = normalize_device_name(candidate)
            if normalized:
                hinted = disks_by_key.get(normalized.lower())
                if hinted:
                    return hinted

        for candidate in (
            raw_slot_status.get("device_hint"),
            raw_slot_status.get("serial_hint"),
            raw_slot_status.get("gptid_hint"),
        ):
            for key in normalize_lookup_keys(str(candidate) if candidate is not None else None):
                hinted = disks_by_key.get(key)
                if hinted:
                    return hinted

        sas_address_hint = normalize_hex_identifier(raw_slot_status.get("sas_address_hint"))
        if sas_address_hint:
            hinted = disks_by_sas.get(sas_address_hint)
            if hinted:
                return hinted

        ssh_device_hint = ssh_data.ses_slot_to_device.get(slot)
        if ssh_device_hint:
            return disks_by_key.get(ssh_device_hint.lower())

        return None

    def _build_slot_view(
        self,
        slot: int,
        row_index: int,
        column_index: int,
        enclosure_meta: dict[str, str | None],
        raw_slot_status: dict[str, Any],
        disk: DiskRecord | None,
        mapping: ManualMapping | None,
        ssh_data: ParsedSSHData,
        api_topology_members: dict[str, Any],
        api_enclosure_ids: set[str],
    ) -> SlotView:
        device_names = raw_slot_status.get("device_names", []) if isinstance(raw_slot_status.get("device_names"), list) else []
        fallback_device = next(
            (normalized for normalized in (normalize_device_name(item) for item in device_names) if normalized),
            None,
        )
        device_name = (
            disk.device_name
            if disk
            else normalize_device_name(raw_slot_status.get("device_hint")) or fallback_device
        )
        gptid = ssh_data.glabel.device_to_gptid.get(device_name.lower()) if device_name else None
        zpool = self._lookup_zpool_member(disk, device_name, gptid, ssh_data, api_topology_members)
        model = disk.model if disk else normalize_text(raw_slot_status.get("model_hint"))
        if not model and device_name:
            model = ssh_data.camcontrol_models.get(device_name.lower())
        multipath = self._build_multipath_view(disk, ssh_data)

        identify_active = bool(raw_slot_status.get("identify_active")) or self._status_contains(raw_slot_status, "identify", "led=locate")
        faulty = self._status_contains(raw_slot_status, "fault") or self._health_is_bad(
            disk.health if disk else None,
            zpool.health if zpool else None,
        )
        raw_present = raw_slot_status.get("present") if isinstance(raw_slot_status.get("present"), bool) else None
        empty = raw_present is False or (not disk and self._status_contains(raw_slot_status, "empty", "not installed", "absent"))
        present = (
            disk is not None
            or raw_present is True
            or self._status_contains(raw_slot_status, "ok", "installed", "ready", "present")
            or identify_active
            or faulty
        )

        if identify_active:
            state = SlotState.identify
        elif faulty:
            state = SlotState.fault
        elif disk:
            state = SlotState.healthy
        elif empty:
            state = SlotState.empty
        elif mapping:
            state = SlotState.unmapped
        else:
            state = SlotState.unknown

        serial = disk.serial if disk else normalize_text(raw_slot_status.get("serial_hint"))
        if not serial and mapping:
            serial = mapping.serial
        size_bytes = disk.size_bytes if disk else None
        size_human = format_bytes(size_bytes) or normalize_text(raw_slot_status.get("reported_size"))
        notes = mapping.notes if mapping else None
        persistent_id, persistent_id_label = resolve_persistent_id(
            gptid,
            raw_slot_status.get("gptid_hint"),
            zpool.raw_path if zpool else None,
            zpool.raw_name if zpool else None,
            mapping.gptid if mapping else None,
        )
        enclosure_id = enclosure_meta.get("id") or normalize_text(raw_slot_status.get("enclosure_id"))
        ses_device = normalize_text(raw_slot_status.get("ses_device"))
        raw_ses_element_id = raw_slot_status.get("ses_element_id")
        ses_element_id = raw_ses_element_id if isinstance(raw_ses_element_id, int) else None
        raw_ses_targets = raw_slot_status.get("ses_targets") if isinstance(raw_slot_status.get("ses_targets"), list) else []
        ses_targets = []
        seen_ses_targets: set[tuple[str | None, int | None]] = set()
        for item in raw_ses_targets:
            if not isinstance(item, dict):
                continue
            target_device = normalize_text(item.get("ses_device"))
            target_element = item.get("ses_element_id")
            target_slot_number = item.get("ses_slot_number") if isinstance(item.get("ses_slot_number"), int) else None
            target_pair = (target_device, target_element if isinstance(target_element, int) else None)
            if target_pair in seen_ses_targets or not target_pair[0] or target_pair[1] is None:
                continue
            seen_ses_targets.add(target_pair)
            ses_targets.append(
                {
                    "ses_device": target_pair[0],
                    "ses_element_id": target_pair[1],
                    "ses_slot_number": target_slot_number,
                }
            )
        if not ses_targets and ses_device and ses_element_id is not None:
            ses_targets.append(
                {
                    "ses_device": ses_device,
                    "ses_element_id": ses_element_id,
                    "ses_slot_number": slot,
                }
            )

        api_led_supported = bool(enclosure_id and enclosure_id in api_enclosure_ids)
        scale_linux_ses_targets = bool(
            self.system.truenas.platform == "scale"
            and any(
                normalize_text(target.get("ses_device", "")).startswith("/dev/sg")
                for target in ses_targets
                if isinstance(target, dict) and normalize_text(target.get("ses_device", ""))
            )
        )
        ssh_led_supported = bool(self.system.ssh.enabled and ses_targets and not scale_linux_ses_targets)
        if api_led_supported:
            led_supported = True
            led_backend = "api"
            led_reason = None
        elif scale_linux_ses_targets and self.system.ssh.enabled:
            led_supported = True
            led_backend = "scale_sg_ses"
            led_reason = None
        elif ssh_led_supported:
            led_supported = True
            led_backend = "ssh"
            led_reason = None
        elif not enclosure_id and not ses_device:
            led_supported = False
            led_backend = None
            led_reason = "LED control unavailable because this slot has no API or SSH enclosure mapping."
        else:
            led_supported = False
            led_backend = None
            if enclosure_id and self.system.ssh.enabled:
                led_reason = (
                    "LED control unavailable because this slot did not expose the SES controller metadata "
                    "needed for SSH `sesutil locate`."
                )
            elif enclosure_id:
                led_reason = (
                    "LED control unavailable because this slot is mapped from SSH fallback data, "
                    "but TrueNAS API did not expose a matching enclosure id."
                )
            else:
                led_reason = "LED control unavailable because this slot is missing SES controller metadata."

        return SlotView(
            slot=slot,
            slot_label=f"{slot + self.settings.layout.slot_number_base:02d}",
            row_index=row_index,
            column_index=column_index,
            enclosure_id=enclosure_id,
            enclosure_label=normalize_text(raw_slot_status.get("enclosure_label")) or enclosure_meta.get("label"),
            enclosure_name=normalize_text(raw_slot_status.get("enclosure_name")) or enclosure_meta.get("name"),
            present=present,
            state=state,
            identify_active=identify_active,
            device_name=device_name,
            serial=serial,
            model=model,
            size_bytes=size_bytes,
            size_human=size_human,
            gptid=persistent_id,
            persistent_id_label=persistent_id_label,
            pool_name=disk.pool_name if disk else zpool.pool_name if zpool else None,
            vdev_name=zpool.vdev_name if zpool else None,
            vdev_class=zpool.vdev_class if zpool else None,
            topology_label=zpool.topology_label if zpool else None,
            health=disk.health if disk and disk.health else zpool.health if zpool else raw_slot_status.get("status"),
            multipath=multipath,
            temperature_c=disk.temperature_c if disk else None,
            last_smart_test_type=disk.last_smart_test_type if disk else None,
            last_smart_test_status=disk.last_smart_test_status if disk else None,
            last_smart_test_lifetime_hours=disk.last_smart_test_lifetime_hours if disk else None,
            logical_block_size=disk.logical_block_size if disk else None,
            physical_block_size=disk.physical_block_size if disk else None,
            logical_unit_id=disk.lunid if disk else None,
            sas_address=normalize_text(raw_slot_status.get("sas_address_hint")),
            enclosure_identifier=normalize_text(raw_slot_status.get("descriptor")),
            led_supported=led_supported,
            led_backend=led_backend,
            led_reason=led_reason,
            ssh_ses_device=ses_device,
            ssh_ses_element_id=ses_element_id,
            ssh_ses_targets=ses_targets,
            mapping_source=mapping.source if mapping else "ssh" if ses_device else "api" if disk else "unknown",
            notes=notes,
            search_text=" ".join(
                filter(
                    None,
                    [
                        f"{slot + self.settings.layout.slot_number_base:02d}",
                        device_name or "",
                        multipath.device_name if multipath else "",
                        " ".join(member.device_name for member in multipath.members) if multipath else "",
                        " ".join(filter(None, [member.state for member in multipath.members])) if multipath else "",
                        " ".join(filter(None, [member.controller_label for member in multipath.members])) if multipath else "",
                        serial or "",
                        model or "",
                        persistent_id or "",
                        (disk.pool_name if disk else "") or "",
                        (zpool.vdev_name if zpool else "") or "",
                        (zpool.vdev_class if zpool else "") or "",
                        normalize_text(raw_slot_status.get("sas_device_type")) or "",
                        normalize_text(raw_slot_status.get("sas_address_hint")) or "",
                        normalize_text(raw_slot_status.get("enclosure_name")) or "",
                        normalize_text(raw_slot_status.get("enclosure_label")) or "",
                        notes or "",
                    ],
                )
            ).lower(),
            raw_status=raw_slot_status,
        )

    async def _set_slot_led_over_ssh(self, slot_view: SlotView, action: LedAction) -> None:
        if not self.system.ssh.enabled:
            raise TrueNASAPIError("SSH fallback is disabled, so LED control cannot use enclosure control commands.")
        ses_targets = slot_view.ssh_ses_targets or []
        if not ses_targets and slot_view.ssh_ses_device and slot_view.ssh_ses_element_id is not None:
            ses_targets = [
                {
                    "ses_device": slot_view.ssh_ses_device,
                    "ses_element_id": slot_view.ssh_ses_element_id,
                    "ses_slot_number": slot_view.slot,
                }
            ]

        if not ses_targets:
            raise TrueNASAPIError(
                slot_view.led_reason
                or f"Slot {slot_view.slot_label} is missing SES controller metadata required for SSH LED control."
            )

        if action == LedAction.identify:
            locate_state = "on"
        elif action == LedAction.clear:
            locate_state = "off"
        else:
            raise TrueNASAPIError("SSH LED fallback currently supports identify on and clear/off only.")

        failures: list[str] = []
        for target in ses_targets:
            target_device = normalize_text(target.get("ses_device"))
            target_element = target.get("ses_element_id")
            target_slot_number = target.get("ses_slot_number")
            if not target_device or not isinstance(target_element, int):
                continue
            if target_device.startswith("/dev/sg"):
                if action == LedAction.identify:
                    sg_action = "--set=ident"
                elif action == LedAction.clear:
                    sg_action = "--clear=ident"
                else:
                    raise TrueNASAPIError("SCALE sg_ses LED control currently supports identify on and clear/off only.")

                target_slot = target_slot_number if isinstance(target_slot_number, int) else slot_view.slot
                command = shlex.join(
                    [
                        "sudo",
                        "-n",
                        "/usr/bin/sg_ses",
                        f"--dev-slot-num={target_slot}",
                        sg_action,
                        target_device,
                    ]
                )
            else:
                command = shlex.join(
                    [
                        "sudo",
                        "-n",
                        "/usr/sbin/sesutil",
                        "locate",
                        "-u",
                        target_device,
                        str(target_element),
                        locate_state,
                    ]
                )
            result = await self.ssh_probe.run_command(command)
            if not result.ok:
                detail = result.stderr.strip() or result.stdout.strip() or "Unknown SSH LED error."
                failures.append(f"{target_device}:{target_element}: {detail}")

        if failures:
            raise TrueNASAPIError("SSH LED action failed: " + " | ".join(failures))

    @staticmethod
    def _merge_enclosure_meta(
        base: dict[str, str | None],
        overlay: dict[str, str | None],
    ) -> dict[str, str | None]:
        merged = dict(base)
        for key, value in overlay.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            merged[key] = value
        return merged

    def _lookup_zpool_member(
        self,
        disk: DiskRecord | None,
        device_name: str | None,
        gptid: str | None,
        ssh_data: ParsedSSHData,
        api_topology_members: dict[str, Any],
    ):
        for value in (
            gptid,
            device_name,
            disk.identifier if disk else None,
            str(disk.raw.get("zfs_guid")) if disk and disk.raw.get("zfs_guid") is not None else None,
            disk.serial if disk else None,
        ):
            for key in normalize_lookup_keys(value):
                member = ssh_data.zpool_members.get(key)
                if member:
                    return member
                api_member = api_topology_members.get(key)
                if api_member:
                    return api_member
        return None

    def _build_multipath_view(
        self,
        disk: DiskRecord | None,
        ssh_data: ParsedSSHData,
    ) -> MultipathView | None:
        if not disk:
            return None

        multipath_name = disk.multipath_name
        if not multipath_name:
            return None

        multipath_device = f"multipath/{multipath_name}"
        parsed = ssh_data.multipath_info.get(multipath_device.lower())
        members: list[MultipathMember] = []

        if parsed:
            members = [
                MultipathMember(
                    device_name=member.device_name,
                    state=member.state,
                    mode=member.mode,
                    controller_label=member.controller_label or ssh_data.camcontrol_controllers.get(member.device_name.lower()),
                )
                for member in parsed.consumers
            ]
        else:
            fallback_devices = [
                device
                for device in dict.fromkeys(
                    filter(None, [disk.path_device_name, disk.multipath_member])
                )
            ]
            members = [
                MultipathMember(
                    device_name=device,
                    controller_label=ssh_data.camcontrol_controllers.get(device.lower()),
                )
                for device in fallback_devices
            ]

        return MultipathView(
            name=multipath_name,
            device_name=parsed.device_name if parsed else multipath_device,
            uuid=parsed.uuid if parsed else None,
            mode=parsed.mode if parsed else None,
            state=parsed.state if parsed else None,
            provider_state=parsed.provider_state if parsed else None,
            path_device_name=disk.path_device_name,
            alternate_path_device=disk.multipath_member,
            lunid=disk.lunid,
            bus=disk.bus,
            members=members,
        )

    @staticmethod
    def _lookup_disk_temperature(
        temperatures: dict[str, int],
        *device_names: str | None,
    ) -> int | None:
        for device_name in device_names:
            normalized = normalize_device_name(device_name)
            if not normalized:
                continue
            value = temperatures.get(normalized) or temperatures.get(normalized.lower())
            if isinstance(value, int):
                return value
        return None

    @staticmethod
    def _lookup_smart_test(
        tests: dict[str, dict[str, Any]],
        *device_names: str | None,
    ) -> dict[str, Any] | None:
        for device_name in device_names:
            normalized = normalize_device_name(device_name)
            if not normalized:
                continue
            payload = tests.get(normalized.lower())
            if payload:
                return payload
        return None

    @staticmethod
    def _extract_physical_block_size(disk: dict[str, Any]) -> int | None:
        sectorsize = disk.get("sectorsize")
        if isinstance(sectorsize, int) and sectorsize > 0:
            return sectorsize
        return None

    @staticmethod
    def _extract_logical_block_size(disk: dict[str, Any], size_bytes: int | None) -> int | None:
        blocks = disk.get("blocks")
        if isinstance(blocks, int) and blocks > 0 and isinstance(size_bytes, int) and size_bytes > 0:
            logical = size_bytes // blocks
            if logical > 0 and logical * blocks == size_bytes:
                return logical
        sectorsize = disk.get("sectorsize")
        if isinstance(sectorsize, int) and sectorsize > 0:
            return sectorsize
        return None

    @staticmethod
    def _smart_candidate_devices(slot_view: SlotView) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        if slot_view.multipath:
            # Prefer ACTIVE member paths first so ad-hoc smartctl calls land on
            # the same physical leg the system is currently favoring.
            active_first = sorted(
                slot_view.multipath.members,
                key=lambda member: ((member.state or "").upper() != "ACTIVE", member.device_name),
            )
            for member in active_first:
                normalized = normalize_device_name(member.device_name)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    candidates.append(normalized)

            for fallback in (
                slot_view.multipath.path_device_name,
                slot_view.multipath.alternate_path_device,
            ):
                normalized = normalize_device_name(fallback)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    candidates.append(normalized)

        normalized_device = normalize_device_name(slot_view.device_name)
        if normalized_device and normalized_device not in seen:
            seen.add(normalized_device)
            candidates.append(normalized_device)

        return candidates

    @staticmethod
    def _fallback_smart_summary(slot_view: SlotView, message: str) -> SmartSummaryView:
        return SmartSummaryView(
            available=any(
                value is not None
                for value in (
                    slot_view.temperature_c,
                    slot_view.last_smart_test_type,
                    slot_view.last_smart_test_status,
                    slot_view.last_smart_test_lifetime_hours,
                    slot_view.logical_block_size,
                    slot_view.physical_block_size,
                )
            ),
            temperature_c=slot_view.temperature_c,
            last_test_type=slot_view.last_smart_test_type,
            last_test_status=slot_view.last_smart_test_status,
            last_test_lifetime_hours=slot_view.last_smart_test_lifetime_hours,
            logical_block_size=slot_view.logical_block_size,
            physical_block_size=slot_view.physical_block_size,
            logical_unit_id=slot_view.logical_unit_id,
            sas_address=slot_view.sas_address,
            message=message,
        )

    @staticmethod
    def _merge_smart_summary(slot_view: SlotView, summary: SmartSummaryView) -> SmartSummaryView:
        summary.temperature_c = summary.temperature_c or slot_view.temperature_c
        summary.last_test_type = summary.last_test_type or slot_view.last_smart_test_type
        summary.last_test_status = summary.last_test_status or slot_view.last_smart_test_status
        summary.last_test_lifetime_hours = (
            summary.last_test_lifetime_hours or slot_view.last_smart_test_lifetime_hours
        )
        summary.logical_block_size = summary.logical_block_size or slot_view.logical_block_size
        summary.physical_block_size = summary.physical_block_size or slot_view.physical_block_size
        summary.logical_unit_id = summary.logical_unit_id or slot_view.logical_unit_id
        summary.sas_address = summary.sas_address or slot_view.sas_address
        if summary.power_on_hours is not None and summary.power_on_days is None:
            summary.power_on_days = summary.power_on_hours // 24
        if (
            summary.power_on_hours is not None
            and summary.last_test_lifetime_hours is not None
            and summary.last_test_age_hours is None
            and summary.power_on_hours >= summary.last_test_lifetime_hours
        ):
            summary.last_test_age_hours = summary.power_on_hours - summary.last_test_lifetime_hours
        summary.available = summary.available or any(
            value is not None
            for value in (
                summary.temperature_c,
                summary.last_test_type,
                summary.last_test_status,
                summary.last_test_lifetime_hours,
                summary.power_on_hours,
                summary.logical_block_size,
                summary.physical_block_size,
                summary.rotation_rate_rpm,
                summary.form_factor,
                summary.read_cache_enabled,
                summary.writeback_cache_enabled,
                summary.transport_protocol,
                summary.logical_unit_id,
                summary.sas_address,
                summary.attached_sas_address,
                summary.negotiated_link_rate,
            )
        )
        return summary

    @staticmethod
    def _status_contains(raw_status: dict[str, Any], *needles: str) -> bool:
        haystack = " ".join(str(value).lower() for value in raw_status.values() if value is not None)
        return any(needle.lower() in haystack for needle in needles)

    @staticmethod
    def _health_is_bad(*values: str | None) -> bool:
        bad_keywords = ("fault", "degrad", "fail", "unavail", "offline", "removed", "critical")
        for value in values:
            if value and any(keyword in value.lower() for keyword in bad_keywords):
                return True
        return False
