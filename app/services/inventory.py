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
    extract_enclosure_slot_candidates,
    format_bytes,
    merge_slot_candidate_maps,
    normalize_device_name,
    normalize_lookup_keys,
    normalize_text,
    parse_pool_query_topology,
    parse_smart_test_results,
    parse_smartctl_summary,
    parse_ssh_outputs,
)
from app.services.ssh_probe import SSHProbe
from app.services.truenas_ws import TrueNASAPIError, TrueNASRawData, TrueNASWebsocketClient

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        elif slot_view.led_backend == "ssh":
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
            return SmartSummaryView(available=False, message="No SMART-capable device path is available for this slot.")

        cache_key = f"{self.system.id}|{'|'.join(candidates)}"
        cache_until = self._smart_cache_until.get(cache_key, datetime.min.replace(tzinfo=timezone.utc))
        cached = self._smart_cache.get(cache_key)
        if cached and utcnow() < cache_until:
            return cached

        last_error: str | None = None
        for candidate in candidates:
            try:
                payload = await self.truenas_client.fetch_disk_smartctl(candidate, ["-a", "-j"])
            except TrueNASAPIError as exc:
                last_error = str(exc)
                continue

            summary = SmartSummaryView.model_validate(parse_smartctl_summary(payload))
            summary.temperature_c = summary.temperature_c or slot_view.temperature_c
            summary.last_test_type = slot_view.last_smart_test_type
            summary.last_test_status = slot_view.last_smart_test_status
            summary.last_test_lifetime_hours = slot_view.last_smart_test_lifetime_hours
            if (
                summary.power_on_hours is not None
                and summary.last_test_lifetime_hours is not None
                and summary.power_on_hours >= summary.last_test_lifetime_hours
            ):
                summary.last_test_age_hours = summary.power_on_hours - summary.last_test_lifetime_hours

            self._smart_cache[cache_key] = summary
            self._smart_cache_until[cache_key] = utcnow() + timedelta(minutes=5)
            return summary

        return SmartSummaryView(
            available=False,
            temperature_c=slot_view.temperature_c,
            last_test_type=slot_view.last_smart_test_type,
            last_test_status=slot_view.last_smart_test_status,
            last_test_lifetime_hours=slot_view.last_smart_test_lifetime_hours,
            message=last_error or "SMART summary is unavailable for this slot.",
        )

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

        if not raw_data.enclosures:
            warnings.append(
                "TrueNAS API returned no enclosure rows. API-only mode can still show disk and pool metadata, "
                "but physical slot mapping on this system will require SSH enrichment or manual calibration."
            )

        slots, available_enclosures, selected_meta = self._correlate(
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
            ssh_slot_hint_count=len(ssh_data.ses_slot_candidates),
        )
        return InventorySnapshot(
            slots=slots,
            refresh_interval_seconds=self.settings.app.refresh_interval_seconds,
            selected_system_id=self.system.id,
            selected_system_label=self.system.label,
            warnings=warnings,
            last_updated=utcnow(),
            generated_at=utcnow(),
            systems=[SystemOption(id=system.id, label=system.label or system.id) for system in self.settings.systems],
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
    ) -> tuple[list[SlotView], list[EnclosureOption], dict[str, str | None]]:
        slot_count = self.settings.layout.slot_count
        api_candidates, api_selected_meta = extract_enclosure_slot_candidates(
            raw_data.enclosures,
            self.system.truenas.enclosure_filter,
            slot_count,
            self.settings.layout.api_slot_number_base,
            selected_enclosure_id,
        )
        selected_meta = self._merge_enclosure_meta(ssh_data.ses_selected_meta, api_selected_meta)
        available_enclosures = self._build_enclosure_options(raw_data, selected_meta)
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
        disk_records = self._build_disk_records(
            raw_data.disks,
            ssh_data,
            raw_data.disk_temperatures,
            parse_smart_test_results(raw_data.smart_test_results),
        )

        disks_by_key: dict[str, DiskRecord] = {}
        disks_by_slot: dict[tuple[str | None, int], DiskRecord] = {}
        for disk in disk_records:
            for key in disk.lookup_keys:
                disks_by_key[key] = disk
            if disk.slot is not None:
                disks_by_slot[(disk.enclosure_id, disk.slot)] = disk
                disks_by_slot[(None, disk.slot)] = disk

        slot_views: list[SlotView] = []

        for slot in range(slot_count):
            row_index = slot // self.settings.layout.columns
            column_index = slot % self.settings.layout.columns
            candidate = slot_candidates.get(slot, {})
            enclosure_id = selected_meta.get("id") or normalize_text(candidate.get("enclosure_id"))
            mapping = self.mapping_store.get_mapping(self.system.id, enclosure_id, slot)
            disk = self._resolve_disk_for_slot(slot, enclosure_id, mapping, disks_by_key, disks_by_slot, candidate, ssh_data)
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

        return slot_views, available_enclosures, selected_meta

    def _build_enclosure_options(
        self,
        raw_data: TrueNASRawData,
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
            target_pair = (target_device, target_element if isinstance(target_element, int) else None)
            if target_pair in seen_ses_targets or not target_pair[0] or target_pair[1] is None:
                continue
            seen_ses_targets.add(target_pair)
            ses_targets.append(
                {
                    "ses_device": target_pair[0],
                    "ses_element_id": target_pair[1],
                }
            )
        if not ses_targets and ses_device and ses_element_id is not None:
            ses_targets.append(
                {
                    "ses_device": ses_device,
                    "ses_element_id": ses_element_id,
                }
            )

        api_led_supported = bool(enclosure_id and enclosure_id in api_enclosure_ids)
        ssh_led_supported = bool(self.system.ssh.enabled and ses_targets)
        if api_led_supported:
            led_supported = True
            led_backend = "api"
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
            enclosure_label=enclosure_meta.get("label"),
            enclosure_name=normalize_text(raw_slot_status.get("enclosure_name")) or enclosure_meta.get("name"),
            present=present,
            state=state,
            identify_active=identify_active,
            device_name=device_name,
            serial=serial,
            model=model,
            size_bytes=size_bytes,
            size_human=size_human,
            gptid=gptid or normalize_text(raw_slot_status.get("gptid_hint")) or (mapping.gptid if mapping else None),
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
                        (gptid or normalize_text(raw_slot_status.get("gptid_hint")) or "") or "",
                        (disk.pool_name if disk else "") or "",
                        (zpool.vdev_name if zpool else "") or "",
                        (zpool.vdev_class if zpool else "") or "",
                        normalize_text(raw_slot_status.get("enclosure_name")) or "",
                        notes or "",
                    ],
                )
            ).lower(),
            raw_status=raw_slot_status,
        )

    async def _set_slot_led_over_ssh(self, slot_view: SlotView, action: LedAction) -> None:
        if not self.system.ssh.enabled:
            raise TrueNASAPIError("SSH fallback is disabled, so LED control cannot use sesutil locate.")
        ses_targets = slot_view.ssh_ses_targets or []
        if not ses_targets and slot_view.ssh_ses_device and slot_view.ssh_ses_element_id is not None:
            ses_targets = [
                {
                    "ses_device": slot_view.ssh_ses_device,
                    "ses_element_id": slot_view.ssh_ses_element_id,
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
            if not target_device or not isinstance(target_element, int):
                continue
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
    def _smart_candidate_devices(slot_view: SlotView) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        if slot_view.multipath:
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
