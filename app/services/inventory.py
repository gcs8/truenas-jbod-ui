from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.models.domain import (
    InventorySnapshot,
    InventorySummary,
    LedAction,
    ManualMapping,
    SlotState,
    SlotView,
    SourceStatus,
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
    serial: str | None
    model: str | None
    size_bytes: int | None
    identifier: str | None
    health: str | None
    pool_name: str | None
    enclosure_id: str | None
    slot: int | None
    lookup_keys: set[str]


class InventoryService:
    def __init__(
        self,
        settings: Settings,
        truenas_client: TrueNASWebsocketClient,
        ssh_probe: SSHProbe,
        mapping_store: MappingStore,
    ) -> None:
        self.settings = settings
        self.truenas_client = truenas_client
        self.ssh_probe = ssh_probe
        self.mapping_store = mapping_store
        self._cache: InventorySnapshot | None = None
        self._cache_until = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = asyncio.Lock()

    async def get_snapshot(self, force_refresh: bool = False) -> InventorySnapshot:
        async with self._lock:
            now = utcnow()
            if not force_refresh and self._cache and now < self._cache_until:
                return self._cache

            snapshot = await self._build_snapshot()
            self._cache = snapshot
            self._cache_until = now + timedelta(seconds=self.settings.app.cache_ttl_seconds)
            return snapshot

    async def set_slot_led(self, slot: int, action: LedAction) -> None:
        snapshot = await self.get_snapshot(force_refresh=True)
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

        await self.get_snapshot(force_refresh=True)

    async def save_mapping(self, slot: int, payload: dict[str, Any]) -> ManualMapping:
        snapshot = await self.get_snapshot(force_refresh=True)
        slot_view = next((item for item in snapshot.slots if item.slot == slot), None)
        enclosure_id = slot_view.enclosure_id if slot_view else None
        mapping = ManualMapping(slot=slot, enclosure_id=enclosure_id, **payload)
        saved = self.mapping_store.save_mapping(mapping)
        await self.get_snapshot(force_refresh=True)
        return saved

    async def clear_mapping(self, slot: int) -> bool:
        snapshot = await self.get_snapshot(force_refresh=True)
        slot_view = next((item for item in snapshot.slots if item.slot == slot), None)
        enclosure_id = slot_view.enclosure_id if slot_view else None
        cleared = self.mapping_store.clear_mapping(enclosure_id, slot)
        await self.get_snapshot(force_refresh=True)
        return cleared

    async def _build_snapshot(self) -> InventorySnapshot:
        warnings: list[str] = []
        ssh_data = ParsedSSHData()
        sources = {
            "api": SourceStatus(enabled=True, ok=False, message=None),
            "ssh": SourceStatus(enabled=self.settings.ssh.enabled, ok=not self.settings.ssh.enabled, message=None),
        }

        try:
            raw_data = await self.truenas_client.fetch_all()
            sources["api"] = SourceStatus(enabled=True, ok=True, message="TrueNAS API reachable.")
        except Exception as exc:
            logger.exception("Failed to fetch TrueNAS API data")
            raw_data = TrueNASRawData(enclosures=[], disks=[], pools=[])
            sources["api"] = SourceStatus(enabled=True, ok=False, message=str(exc))
            warnings.append("TrueNAS API is unreachable. Slot details may be partial or unavailable.")

        if self.settings.ssh.enabled:
            try:
                command_results = await self.ssh_probe.run_commands()
                outputs = {item.command: item.stdout for item in command_results if item.ok}
                failures = [item for item in command_results if not item.ok]
                ssh_data = parse_ssh_outputs(
                    outputs,
                    self.settings.layout.slot_count,
                    self.settings.truenas.enclosure_filter,
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

        slots = self._correlate(raw_data, ssh_data, warnings)
        selected_slot = next((slot for slot in slots if slot.enclosure_id), slots[0] if slots else None)
        summary = InventorySummary(
            disk_count=len(raw_data.disks),
            pool_count=len(raw_data.pools),
            enclosure_count=len(raw_data.enclosures),
            mapped_slot_count=sum(1 for slot in slots if slot.device_name),
            manual_mapping_count=len(self.mapping_store.load_all()),
            ssh_slot_hint_count=len(ssh_data.ses_slot_candidates),
        )
        return InventorySnapshot(
            slots=slots,
            refresh_interval_seconds=self.settings.app.refresh_interval_seconds,
            warnings=warnings,
            last_updated=utcnow(),
            generated_at=utcnow(),
            selected_enclosure_id=selected_slot.enclosure_id if selected_slot else None,
            selected_enclosure_label=selected_slot.enclosure_label if selected_slot else None,
            selected_enclosure_name=selected_slot.enclosure_name if selected_slot else None,
            sources=sources,
            summary=summary,
        )

    def _correlate(
        self,
        raw_data: TrueNASRawData,
        ssh_data: ParsedSSHData,
        warnings: list[str],
    ) -> list[SlotView]:
        slot_count = self.settings.layout.slot_count
        api_candidates, selected_meta = extract_enclosure_slot_candidates(
            raw_data.enclosures,
            self.settings.truenas.enclosure_filter,
            slot_count,
            self.settings.layout.api_slot_number_base,
        )
        api_enclosure_ids = {
            enclosure_id
            for enclosure_id in (
                normalize_text(candidate.get("enclosure_id")) for candidate in api_candidates.values()
            )
            if enclosure_id
        }
        if selected_meta.get("id"):
            api_enclosure_ids.add(selected_meta["id"])
        slot_candidates = merge_slot_candidate_maps(ssh_data.ses_slot_candidates, api_candidates)
        selected_meta = self._merge_enclosure_meta(ssh_data.ses_selected_meta, selected_meta)
        api_topology_members = parse_pool_query_topology(raw_data.pools)
        disk_records = self._build_disk_records(raw_data.disks, ssh_data)

        disks_by_key: dict[str, DiskRecord] = {}
        disks_by_slot: dict[tuple[str | None, int], DiskRecord] = {}
        for disk in disk_records:
            for key in disk.lookup_keys:
                disks_by_key[key] = disk
            if disk.slot is not None:
                disks_by_slot[(disk.enclosure_id, disk.slot)] = disk
                disks_by_slot[(None, disk.slot)] = disk

        manual_mappings = self.mapping_store.load_all()
        slot_views: list[SlotView] = []

        for slot in range(slot_count):
            row_index = slot // self.settings.layout.columns
            column_index = slot % self.settings.layout.columns
            candidate = slot_candidates.get(slot, {})
            enclosure_id = normalize_text(candidate.get("enclosure_id")) or selected_meta.get("id")
            mapping = manual_mappings.get(f"{enclosure_id or 'default'}:{slot}") or manual_mappings.get(f"default:{slot}")
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

        return slot_views

    def _build_disk_records(self, disks: list[dict[str, Any]], ssh_data: ParsedSSHData) -> list[DiskRecord]:
        records: list[DiskRecord] = []
        for disk in disks:
            device_name = normalize_device_name(
                disk.get("devname") or disk.get("name") or disk.get("device") or disk.get("disk")
            )
            serial = normalize_text(disk.get("serial") or disk.get("serial_lunid") or disk.get("lunid"))
            model = normalize_text(disk.get("model"))
            size_bytes = disk.get("size") if isinstance(disk.get("size"), int) else None
            identifier = normalize_text(disk.get("identifier"))
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

            multipath_name = normalize_text(disk.get("multipath_name"))
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
                    serial=serial,
                    model=model,
                    size_bytes=size_bytes,
                    identifier=identifier,
                    health=health,
                    pool_name=pool_name,
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
        enclosure_id = normalize_text(raw_slot_status.get("enclosure_id")) or enclosure_meta.get("id")
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
        ssh_led_supported = bool(self.settings.ssh.enabled and ses_targets)
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
            if enclosure_id and self.settings.ssh.enabled:
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
        if not self.settings.ssh.enabled:
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
