from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from collections import Counter
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
    SmartBatchItem,
    SmartSummaryView,
    SlotState,
    SlotView,
    SourceStatus,
    SystemOption,
)
from app.services.mapping_store import MappingStore
from app.services.profile_registry import (
    ProfileRegistry,
    UNIFI_UNVR_FRONT_4_PROFILE_ID,
    UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
)
from app.services.quantastor_api import QuantastorRESTClient
from app.services.parsers import (
    ParsedSSHData,
    ZpoolMember,
    build_slot_candidates_from_ses_enclosures,
    canonicalize_ssh_command,
    extract_nvme_controller_name,
    extract_enclosure_slot_candidates,
    format_bytes,
    merge_slot_candidate_maps,
    normalize_device_name,
    normalize_hex_identifier,
    normalize_lookup_keys,
    normalize_text,
    parse_pool_query_topology,
    parse_nvme_id_ctrl_summary,
    parse_nvme_id_ns_summary,
    parse_nvme_smart_log_summary,
    parse_smart_test_results,
    parse_smartctl_text_enrichment,
    parse_smartctl_summary,
    parse_ssh_outputs,
    shift_hex_identifier,
)
from app.services.ssh_probe import SSHProbe
from app.services.truenas_ws import TrueNASAPIError, TrueNASRawData, TrueNASWebsocketClient

logger = logging.getLogger(__name__)
HCTL_NAME_REGEX = re.compile(r"^\d+:\d+:\d+:\d+$")
UNIFI_GPIO_LED_PROFILE_IDS = {
    UNIFI_UNVR_FRONT_4_PROFILE_ID,
    UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
}


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


def infer_slot_count_from_layout(layout_rows: list[list[int]], fallback: int | None = None) -> int:
    if layout_rows:
        return max(slot for row in layout_rows for slot in row) + 1
    return fallback or 0


def parse_size_to_bytes(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    text = normalize_text(str(value) if value is not None else None)
    if not text:
        return None
    match = re.match(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTPE]?)(?:i?B?)$", text, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group("number"))
    unit = match.group("unit").upper()
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}[unit]
    return int(number * (1000 ** power))


def build_lunid_aliases(value: str | None, platform: str) -> set[str]:
    aliases: set[str] = set()
    normalized = normalize_hex_identifier(value)
    if normalized:
        aliases.add(normalized)

    # CORE shelves have mostly matched on exact lunid or +1 shifted SAS hints.
    # On the user's SCALE host, the rear SSD enclosure exposes AES SAS addresses
    # that can differ from disk.query lunids by up to two hex counts, so we keep
    # the match window intentionally small but a little wider there.
    deltas = (1,) if platform not in {"scale", "quantastor"} else (-2, -1, 1, 2)
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
        if leaf_lowered.startswith("eui."):
            return leaf, "EUI64"

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
    smart_devices: list[str]
    lookup_keys: set[str]


class InventoryService:
    def __init__(
        self,
        settings: Settings,
        system: SystemConfig,
        truenas_client: TrueNASWebsocketClient | QuantastorRESTClient,
        ssh_probe: SSHProbe,
        mapping_store: MappingStore,
        profile_registry: ProfileRegistry,
    ) -> None:
        self.settings = settings
        self.system = system
        self.truenas_client = truenas_client
        self.ssh_probe = ssh_probe
        self.mapping_store = mapping_store
        self.profile_registry = profile_registry
        self._cache: dict[str, InventorySnapshot] = {}
        self._cache_until: dict[str, datetime] = {}
        self._smart_cache: dict[str, SmartSummaryView] = {}
        self._smart_cache_until: dict[str, datetime] = {}
        self._quantastor_preferred_ses_host: str | None = None
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
        elif slot_view.led_backend in {"ssh", "scale_sg_ses", "quantastor_sg_ses"}:
            await self._set_slot_led_over_ssh(slot_view, action)
        elif slot_view.led_backend == "unifi_fault":
            await self._set_unifi_slot_led_over_ssh(slot_view, action)
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

        if self.system.truenas.platform == "quantastor":
            summary = self._merge_smart_summary(slot_view, self._build_quantastor_smart_summary(slot_view))
            candidates = self._smart_candidate_devices(slot_view)
            if self.system.ssh.enabled and candidates and self._summary_needs_ssh_enrichment(summary):
                ssh_summary, _ssh_error = await self._fetch_smart_summary_over_ssh(
                    candidates,
                    hosts=self._build_quantastor_preferred_hosts(slot_view),
                )
                if ssh_summary is not None:
                    summary = self._merge_missing_smart_fields(
                        summary,
                        self._merge_smart_summary(slot_view, ssh_summary),
                    )
            self._smart_cache[f"{self.system.id}|quantastor|{slot}"] = summary
            self._smart_cache_until[f"{self.system.id}|quantastor|{slot}"] = utcnow() + timedelta(minutes=5)
            return summary

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

        if self.system.truenas.platform in {"scale", "linux"}:
            summary, error_message = await self._fetch_smart_summary_over_ssh(candidates)
            if summary is not None:
                summary = self._merge_smart_summary(slot_view, summary)
                self._smart_cache[cache_key] = summary
                self._smart_cache_until[cache_key] = utcnow() + timedelta(minutes=5)
                return summary

            return self._fallback_smart_summary(
                slot_view,
                error_message
                or (
                    "Detailed SMART JSON is not currently available through the SCALE API on this system."
                    if self.system.truenas.platform == "scale"
                    else "Detailed SMART data is not available for this Linux slot."
                ),
            )

        last_error: str | None = None
        api_summary: SmartSummaryView | None = None
        api_candidate: str | None = None
        for candidate in candidates:
            try:
                payload = await self.truenas_client.fetch_disk_smartctl(candidate, ["-a", "-j"])
            except TrueNASAPIError as exc:
                last_error = str(exc)
                continue

            api_summary = self._merge_smart_summary(
                slot_view,
                SmartSummaryView.model_validate(parse_smartctl_summary(payload)),
            )
            api_candidate = candidate
            break

        if api_summary is not None:
            if api_candidate and self._summary_needs_ssh_enrichment(api_summary):
                try:
                    enrichment_payload = await self.truenas_client.fetch_disk_smartctl(api_candidate, ["-x"])
                except TrueNASAPIError as exc:
                    last_error = str(exc)
                else:
                    api_summary = self._merge_missing_smart_fields(
                        api_summary,
                        SmartSummaryView.model_validate(
                            parse_smartctl_text_enrichment(enrichment_payload)
                        ),
                    )

            self._smart_cache[cache_key] = api_summary
            self._smart_cache_until[cache_key] = utcnow() + timedelta(minutes=5)
            return api_summary

        if self.system.ssh.enabled:
            ssh_summary, ssh_error = await self._fetch_smart_summary_over_ssh(candidates)
            if ssh_summary is not None:
                ssh_summary = self._merge_smart_summary(slot_view, ssh_summary)
                self._smart_cache[cache_key] = ssh_summary
                self._smart_cache_until[cache_key] = utcnow() + timedelta(minutes=5)
                return ssh_summary
            if ssh_error:
                last_error = ssh_error

        return self._fallback_smart_summary(
            slot_view,
            last_error or "SMART summary is unavailable for this slot.",
        )

    async def get_slot_smart_summaries(
        self,
        slots: list[int],
        selected_enclosure_id: str | None = None,
        max_concurrency: int = 4,
    ) -> list[SmartBatchItem]:
        snapshot = await self.get_snapshot(selected_enclosure_id=selected_enclosure_id)
        slot_lookup = {item.slot: item for item in snapshot.slots}
        ordered_slots: list[int] = []
        seen_slots: set[int] = set()
        for slot in slots:
            if slot in seen_slots or slot not in slot_lookup:
                continue
            seen_slots.add(slot)
            ordered_slots.append(slot)

        if not ordered_slots:
            return []

        semaphore = asyncio.Semaphore(max(1, max_concurrency))

        async def load_summary(slot: int) -> SmartBatchItem:
            async with semaphore:
                try:
                    summary = await self.get_slot_smart_summary(slot, selected_enclosure_id=selected_enclosure_id)
                except TrueNASAPIError as exc:
                    summary = self._fallback_smart_summary(slot_lookup.get(slot), str(exc))
                return SmartBatchItem(slot=slot, summary=summary)

        return await asyncio.gather(*(load_summary(slot) for slot in ordered_slots))

    async def _fetch_smart_summary_over_ssh(
        self,
        candidates: list[str],
        hosts: list[str] | None = None,
    ) -> tuple[SmartSummaryView | None, str | None]:
        if not self.system.ssh.enabled:
            return None, (
                "Detailed SMART JSON is not currently available through the SCALE API on this system, "
                "and SSH fallback is disabled."
            )

        last_error: str | None = None
        host_candidates = [normalize_text(host) for host in (hosts or []) if normalize_text(host)]
        if not host_candidates:
            host_candidates = [None]

        for target_host in host_candidates:
            for candidate in candidates:
                device_path = candidate if candidate.startswith("/dev/") else f"/dev/{candidate}"
                for smartctl_binary in self._smartctl_binary_candidates():
                    command = shlex.join(
                        [
                            "sudo",
                            "-n",
                            smartctl_binary,
                            "-x",
                            "-j",
                            device_path,
                        ]
                    )
                    result = await self._run_ssh_command(command, target_host)
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
                        last_error = (
                            f"{target_host}:{device_path}: {detail}"
                            if target_host
                            else f"{device_path}: {detail}"
                        )
                        if "command not found" in detail.lower():
                            continue
                        break

                    text_command = shlex.join(
                        [
                            "sudo",
                            "-n",
                            smartctl_binary,
                            "-x",
                            device_path,
                        ]
                    )
                    text_result = await self._run_ssh_command(text_command, target_host)
                    if text_result.stdout.strip():
                        summary = self._merge_missing_smart_fields(
                            summary,
                            SmartSummaryView.model_validate(
                                parse_smartctl_text_enrichment(text_result.stdout)
                            ),
                        )
                    if self.system.truenas.platform == "linux":
                        nvme_summary = await self._fetch_linux_nvme_enrichment_over_ssh(device_path, target_host)
                        if nvme_summary is not None:
                            summary = self._merge_missing_smart_fields(summary, nvme_summary)
                    if summary.available or summary.message != "SMART JSON parsing failed.":
                        if self.system.truenas.platform == "quantastor" and target_host:
                            self._quantastor_preferred_ses_host = target_host
                        return summary, None
                    last_error = (
                        f"{target_host}:{device_path}: {summary.message or 'SMART JSON parsing failed.'}"
                        if target_host
                        else f"{device_path}: {summary.message or 'SMART JSON parsing failed.'}"
                    )
                    break

        return None, last_error

    def _smartctl_binary_candidates(self) -> tuple[str, ...]:
        if self.system.truenas.platform == "core":
            return ("/usr/local/sbin/smartctl", "/usr/sbin/smartctl")
        return ("/usr/sbin/smartctl", "/usr/local/sbin/smartctl")

    async def _fetch_linux_nvme_enrichment_over_ssh(
        self,
        device_path: str,
        host: str | None = None,
    ) -> SmartSummaryView | None:
        if self.system.truenas.platform != "linux":
            return None

        device_name = normalize_device_name(device_path)
        controller_name = extract_nvme_controller_name(device_name)
        if not controller_name:
            return None

        controller_path = f"/dev/{controller_name}"
        namespace_path = device_path if device_path.startswith("/dev/") else f"/dev/{device_name}"
        summary: SmartSummaryView | None = None

        for command, parser in (
            (
                shlex.join(["sudo", "-n", "/usr/sbin/nvme", "smart-log", "-o", "json", controller_path]),
                parse_nvme_smart_log_summary,
            ),
            (
                shlex.join(["sudo", "-n", "/usr/sbin/nvme", "id-ctrl", "-o", "json", controller_path]),
                parse_nvme_id_ctrl_summary,
            ),
            (
                shlex.join(["sudo", "-n", "/usr/sbin/nvme", "id-ns", "-o", "json", namespace_path]),
                parse_nvme_id_ns_summary,
            ),
        ):
            result = await self._run_ssh_command(command, host)
            if not result.stdout.strip():
                continue
            parsed = SmartSummaryView.model_validate(parser(result.stdout))
            if summary is None:
                summary = parsed
            else:
                summary = self._merge_missing_smart_fields(summary, parsed)

        return summary

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
        api_enabled = self.system.truenas.platform != "linux"
        api_label = "Quantastor API" if self.system.truenas.platform == "quantastor" else "TrueNAS API"
        sources = {
            "api": SourceStatus(
                enabled=api_enabled,
                ok=not api_enabled,
                message=f"{api_label} disabled for this SSH-only Linux system." if not api_enabled else None,
            ),
            "ssh": SourceStatus(enabled=self.system.ssh.enabled, ok=not self.system.ssh.enabled, message=None),
        }

        raw_data = TrueNASRawData(
            enclosures=[],
            disks=[],
            pools=[],
            disk_temperatures={},
            smart_test_results=[],
        )
        ssh_available = False
        ssh_failures: list[str] = []
        quantastor_cli_loaded = False
        quantastor_cli_failures: list[str] = []
        quantastor_ses_loaded = False
        quantastor_ses_data = ParsedSSHData()
        quantastor_ses_failures: list[str] = []
        if api_enabled:
            try:
                raw_data = await self.truenas_client.fetch_all()
                sources["api"] = SourceStatus(enabled=True, ok=True, message=f"{api_label} reachable.")
            except Exception as exc:
                logger.exception("Failed to fetch platform API data")
                sources["api"] = SourceStatus(enabled=True, ok=False, message=str(exc))
                warnings.append(f"{api_label} is unreachable. Slot details may be partial or unavailable.")

        if self.system.ssh.enabled:
            try:
                command_results = await self.ssh_probe.run_commands()
                if self._should_probe_unifi_gpio_debug(command_results):
                    command_results.append(await self.ssh_probe.run_command("cat /sys/kernel/debug/gpio"))
                outputs = {item.command: item.stdout for item in command_results if item.ok}
                failures = [item for item in command_results if not item.ok]
                ssh_available = True
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
                    message = f"SSH command failed: {failure.command} (exit {failure.exit_code})"
                    warnings.append(message)
                    ssh_failures.append(message)
            except Exception as exc:
                logger.exception("Failed to collect SSH diagnostics")
                sources["ssh"] = SourceStatus(enabled=True, ok=False, message=str(exc))
                warnings.append("SSH mode is enabled but could not collect fallback command output.")

        if self.system.truenas.platform == "quantastor" and ssh_available:
            try:
                quantastor_ses_data, quantastor_ses_failures = await self._fetch_quantastor_ses_overlay()
            except Exception:
                logger.exception("Failed to collect Quantastor SES diagnostics")
                quantastor_ses_failures.append(
                    "Quantastor SSH SES enrichment failed unexpectedly. REST and CLI slot truth is still being used."
                )
            else:
                quantastor_ses_loaded = bool(quantastor_ses_data.ses_enclosures)
                warnings.extend(quantastor_ses_failures)

            try:
                quantastor_cli_overlay, quantastor_cli_failures = await self._fetch_quantastor_cli_overlay()
            except Exception:
                logger.exception("Failed to collect Quantastor CLI diagnostics")
                quantastor_cli_failures.append(
                    "Quantastor SSH CLI enrichment failed unexpectedly. REST data is still being used."
                )
            else:
                raw_data.cli_disks = quantastor_cli_overlay.get("cli_disks", [])
                raw_data.cli_hw_disks = quantastor_cli_overlay.get("cli_hw_disks", [])
                raw_data.cli_hw_enclosures = quantastor_cli_overlay.get("cli_hw_enclosures", [])
                quantastor_cli_loaded = any(
                    (
                        raw_data.cli_disks,
                        raw_data.cli_hw_disks,
                        raw_data.cli_hw_enclosures,
                    )
                )
                warnings.extend(quantastor_cli_failures)

            if quantastor_cli_loaded:
                sources["ssh"] = SourceStatus(
                    enabled=True,
                    ok=not ssh_failures and not quantastor_cli_failures and not quantastor_ses_failures,
                    message=(
                        "SSH probe, Quantastor CLI enrichment, and SES overlay completed."
                        if quantastor_ses_loaded and not ssh_failures and not quantastor_cli_failures and not quantastor_ses_failures
                        else "SSH probe and Quantastor CLI enrichment completed."
                        if not ssh_failures and not quantastor_cli_failures and not quantastor_ses_failures
                        else "Quantastor CLI/SES enrichment completed with some failures."
                    ),
                )
            elif quantastor_ses_loaded:
                sources["ssh"] = SourceStatus(
                    enabled=True,
                    ok=not ssh_failures and not quantastor_ses_failures,
                    message=(
                        "SSH probe and Quantastor SES overlay completed."
                        if not ssh_failures and not quantastor_ses_failures
                        else "Quantastor SES overlay completed with some failures."
                    ),
                )

        has_scale_linux_ses = self._has_scale_linux_ses(ssh_data)
        if api_enabled and not raw_data.enclosures:
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
            elif self.system.truenas.platform == "quantastor":
                warnings.append(
                    "Quantastor did not expose any storage-system rows through the REST API, so no enclosure-scoped "
                    "view can be rendered yet."
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
            quantastor_ses_data=quantastor_ses_data,
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
        selected_profile = self.profile_registry.resolve_for_enclosure(
            self.system,
            selected_option,
            fallback_label=selected_option.label if selected_option else selected_meta.get("label"),
            fallback_rows=len(layout_rows) if layout_rows else None,
            fallback_columns=layout_columns or None,
            fallback_slot_count=layout_slot_count or None,
            fallback_slot_layout=layout_rows or None,
        )
        selected_slot = None
        if resolved_enclosure_id:
            selected_slot = next((slot for slot in slots if slot.enclosure_id == resolved_enclosure_id), None)
        if selected_slot is None:
            selected_slot = next((slot for slot in slots if slot.enclosure_id), slots[0] if slots else None)

        if self.system.truenas.platform == "linux":
            disk_count = sum(1 for slot in slots if slot.device_name)
            pool_count = len({slot.pool_name for slot in slots if slot.pool_name})
            enclosure_count = len(available_enclosures)
            ssh_slot_hint_count = max(
                len((selected_profile.slot_hints if selected_profile else {}) or {}),
                sum(1 for slot in slots if slot.smart_device_names),
            )
        elif self.system.truenas.platform == "quantastor":
            disk_count = len(raw_data.disks)
            pool_count = len(raw_data.pools)
            enclosure_count = len(available_enclosures)
            ssh_slot_hint_count = 0
        else:
            disk_count = len(raw_data.disks)
            pool_count = len(raw_data.pools)
            enclosure_count = len(raw_data.enclosures)
            ssh_slot_hint_count = max(
                len(ssh_data.ses_slot_candidates),
                sum(len(enclosure.slots) for enclosure in ssh_data.ses_enclosures),
            )

        platform_context: dict[str, Any] = {}
        if self.system.truenas.platform == "quantastor":
            selected_system_id = selected_option.id if selected_option else resolved_enclosure_id
            platform_context = self._build_quantastor_platform_context(raw_data, selected_system_id)
            self._annotate_quantastor_slot_context(slots, raw_data, selected_system_id, platform_context)

        summary = InventorySummary(
            disk_count=disk_count,
            pool_count=pool_count,
            enclosure_count=enclosure_count,
            mapped_slot_count=sum(1 for slot in slots if slot.device_name),
            manual_mapping_count=self.mapping_store.count_for_system(self.system.id),
            ssh_slot_hint_count=ssh_slot_hint_count,
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
            selected_profile=selected_profile,
            platform_context=platform_context,
            sources=sources,
            summary=summary,
        )

    def _correlate(
        self,
        raw_data: TrueNASRawData,
        ssh_data: ParsedSSHData,
        warnings: list[str],
        selected_enclosure_id: str | None = None,
        quantastor_ses_data: ParsedSSHData | None = None,
    ) -> tuple[list[SlotView], list[EnclosureOption], dict[str, str | None], list[list[int]], int, int]:
        if self.system.truenas.platform == "linux":
            return self._correlate_linux_host(ssh_data, warnings, selected_enclosure_id)
        if self.system.truenas.platform == "quantastor":
            return self._correlate_quantastor(raw_data, warnings, selected_enclosure_id, quantastor_ses_data or ParsedSSHData())
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
        selected_profile = self.profile_registry.resolve_for_enclosure(
            self.system,
            selected_option,
            fallback_label=selected_option.label if selected_option else selected_meta.get("label"),
            fallback_rows=selected_option.rows if selected_option and selected_option.rows else self.settings.layout.rows,
            fallback_columns=selected_option.columns if selected_option and selected_option.columns else self.settings.layout.columns,
            fallback_slot_count=selected_option.slot_count if selected_option and selected_option.slot_count else slot_count,
            fallback_slot_layout=selected_option.slot_layout if selected_option else None,
        )
        layout_columns = (
            selected_profile.columns
            if selected_profile
            else selected_option.columns if selected_option and selected_option.columns
            else self.settings.layout.columns
        )
        layout_rows = (
            [list(row) for row in selected_profile.slot_layout]
            if selected_profile and selected_profile.slot_layout
            else selected_option.slot_layout if selected_option and selected_option.slot_layout
            else build_layout_rows(
                selected_option.rows if selected_option and selected_option.rows else self.settings.layout.rows,
                layout_columns,
                selected_option.slot_count if selected_option and selected_option.slot_count else slot_count,
            )
        )
        layout_slot_count = infer_slot_count_from_layout(
            layout_rows,
            selected_option.slot_count if selected_option and selected_option.slot_count else slot_count,
        )
        slot_positions = {
            slot_number: (row_index, column_index)
            for row_index, row in enumerate(layout_rows)
            for column_index, slot_number in enumerate(row)
        }
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
            row_index, column_index = slot_positions.get(slot, (slot // layout_columns, slot % layout_columns))
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
            layout_rows,
            layout_slot_count,
            layout_columns,
        )

    def _correlate_linux_host(
        self,
        ssh_data: ParsedSSHData,
        warnings: list[str],
        selected_enclosure_id: str | None,
    ) -> tuple[list[SlotView], list[EnclosureOption], dict[str, str | None], list[list[int]], int, int]:
        available_enclosures = self._build_linux_enclosure_options()
        selected_option = self._resolve_selected_enclosure_option(available_enclosures, selected_enclosure_id, {})
        if selected_option is None:
            warnings.append("No profile-backed Linux enclosure is configured for this host yet.")
            return [], [], {"id": None, "label": None, "name": None}, [], 0, 0

        selected_profile = self.profile_registry.resolve_for_enclosure(
            self.system,
            selected_option,
            fallback_label=selected_option.label,
            fallback_rows=selected_option.rows or self.settings.layout.rows,
            fallback_columns=selected_option.columns or self.settings.layout.columns,
            fallback_slot_count=selected_option.slot_count or self.settings.layout.slot_count,
            fallback_slot_layout=selected_option.slot_layout,
        )
        if selected_profile is None:
            warnings.append("This Linux host is missing an enclosure profile for rendering.")
            return [], available_enclosures, self._enclosure_option_meta(selected_option), [], 0, 0

        layout_rows = [list(row) for row in selected_profile.slot_layout]
        layout_slot_count = infer_slot_count_from_layout(layout_rows, selected_option.slot_count)
        layout_columns = selected_profile.columns
        slot_positions = {
            slot_number: (row_index, column_index)
            for row_index, row in enumerate(layout_rows)
            for column_index, slot_number in enumerate(row)
        }

        disk_records = self._build_linux_disk_records(ssh_data)
        disks_by_key: dict[str, DiskRecord] = {}
        disks_by_slot: dict[tuple[str | None, int], DiskRecord] = {}
        for disk in disk_records:
            for key in disk.lookup_keys:
                disks_by_key[key] = disk
            vendor_slot = disk.raw.get("vendor_slot")
            if isinstance(vendor_slot, int):
                disks_by_slot[(selected_option.id, vendor_slot)] = disk
                disks_by_slot[(None, vendor_slot)] = disk

        linux_topology_members = self._build_linux_topology_members(disk_records)
        slot_views: list[SlotView] = []
        selected_meta = self._enclosure_option_meta(selected_option)
        vendor_slot_candidates = self._build_linux_vendor_slot_candidates(ssh_data, selected_option)
        slot_hints = selected_profile.slot_hints or {}
        if not slot_hints and not vendor_slot_candidates:
            warnings.append(
                "This Linux profile does not define slot hints yet, so physical slot correlation will require manual mapping."
            )
        if selected_profile.id == UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID:
            warnings.append(
                "UniFi UNVR Pro LED control is experimental. The vendor-local SSH command path is responding and "
                "GPIO state changes are visible, but operator-visible bay validation is still pending."
            )

        for slot in range(layout_slot_count):
            row_index, column_index = slot_positions.get(slot, (slot // max(layout_columns, 1), slot % max(layout_columns, 1)))
            hint_values = [normalize_text(value) for value in slot_hints.get(slot, []) if normalize_text(value)]
            raw_slot_status = {
                "device_names": hint_values,
                "device_hint": hint_values[0] if hint_values else None,
                "enclosure_id": selected_option.id,
                "enclosure_label": selected_option.label,
                "enclosure_name": selected_option.name,
                "experimental_led": selected_profile.id == UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
            }
            vendor_candidate = vendor_slot_candidates.get(slot)
            if vendor_candidate:
                vendor_device_names = vendor_candidate.get("device_names", [])
                if isinstance(vendor_device_names, list) and vendor_device_names:
                    raw_slot_status["device_names"] = list(
                        dict.fromkeys(
                            [*vendor_device_names, *raw_slot_status.get("device_names", [])]
                        )
                    )
                    raw_slot_status["device_hint"] = vendor_candidate.get("device_hint") or raw_slot_status["device_names"][0]
                elif vendor_candidate.get("device_hint"):
                    raw_slot_status["device_hint"] = vendor_candidate.get("device_hint")
                raw_slot_status.update(
                    {
                        key: value
                        for key, value in vendor_candidate.items()
                        if key not in {"device_names", "device_hint"} and value is not None
                    }
                )
            mapping = self.mapping_store.get_mapping(self.system.id, selected_option.id, slot)
            disk = self._resolve_disk_for_slot(
                slot,
                selected_option.id,
                mapping,
                disks_by_key,
                disks_by_slot,
                {},
                raw_slot_status,
                ssh_data,
            )
            slot_view = self._build_slot_view(
                slot=slot,
                row_index=row_index,
                column_index=column_index,
                enclosure_meta=selected_meta,
                raw_slot_status=raw_slot_status,
                disk=disk,
                mapping=mapping,
                ssh_data=ssh_data,
                api_topology_members=linux_topology_members,
                api_enclosure_ids=set(),
            )
            if mapping and not disk:
                warnings.append(f"Manual mapping for slot {slot:02d} did not match any current Linux device.")
            slot_views.append(slot_view)

        return (
            slot_views,
            available_enclosures,
            selected_meta,
            layout_rows,
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

        selected_profile = self.profile_registry.resolve_for_enclosure(
            self.system,
            selected_option,
            fallback_label=selected_option.label,
            fallback_rows=selected_option.rows or self.settings.layout.rows,
            fallback_columns=selected_option.columns or self.settings.layout.columns,
            fallback_slot_count=selected_option.slot_count or self.settings.layout.slot_count,
            fallback_slot_layout=selected_option.slot_layout,
        )
        slot_count = (
            infer_slot_count_from_layout(selected_profile.slot_layout, selected_option.slot_count)
            if selected_profile
            else selected_option.slot_count or self.settings.layout.slot_count
        )
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

        columns = (
            selected_profile.columns
            if selected_profile
            else selected_option.columns or self.settings.layout.columns
        )
        layout_rows = (
            [list(row) for row in selected_profile.slot_layout]
            if selected_profile and selected_profile.slot_layout
            else selected_option.slot_layout or build_layout_rows(selected_option.rows or self.settings.layout.rows, columns, slot_count)
        )
        slot_count = infer_slot_count_from_layout(layout_rows, slot_count)
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

    def _correlate_quantastor(
        self,
        raw_data: TrueNASRawData,
        warnings: list[str],
        selected_enclosure_id: str | None,
        quantastor_ses_data: ParsedSSHData,
    ) -> tuple[list[SlotView], list[EnclosureOption], dict[str, str | None], list[list[int]], int, int]:
        available_enclosures = self._build_quantastor_enclosure_options(raw_data)
        preferred_enclosure_id = selected_enclosure_id or self._select_quantastor_default_enclosure_id(
            raw_data,
            available_enclosures,
        )
        selected_option = self._resolve_selected_enclosure_option(available_enclosures, preferred_enclosure_id, {})
        if selected_option is None:
            warnings.append("Quantastor did not return any storage-system views that can be rendered yet.")
            return [], [], {"id": None, "label": None, "name": None}, [], 0, 0

        selected_profile = self.profile_registry.resolve_for_enclosure(
            self.system,
            selected_option,
            fallback_label=selected_option.label,
            fallback_rows=selected_option.rows or self.settings.layout.rows,
            fallback_columns=selected_option.columns or self.settings.layout.columns,
            fallback_slot_count=selected_option.slot_count or self.settings.layout.slot_count,
            fallback_slot_layout=selected_option.slot_layout,
        )
        if selected_profile is None:
            warnings.append("This Quantastor view needs a chassis profile before it can be rendered.")
            return [], available_enclosures, self._enclosure_option_meta(selected_option), [], 0, 0

        warnings.extend(self._build_quantastor_cluster_warnings(raw_data, selected_option.id))

        layout_rows = [list(row) for row in selected_profile.slot_layout]
        layout_slot_count = infer_slot_count_from_layout(layout_rows, selected_option.slot_count)
        layout_columns = selected_profile.columns
        slot_positions = {
            slot_number: (row_index, column_index)
            for row_index, row in enumerate(layout_rows)
            for column_index, slot_number in enumerate(row)
        }

        disk_records = self._build_quantastor_disk_records(raw_data, selected_option.id)
        disks_by_key: dict[str, DiskRecord] = {}
        disks_by_slot: dict[tuple[str | None, int], DiskRecord] = {}
        disks_by_sas: dict[str, DiskRecord] = {}
        for disk in disk_records:
            for key in disk.lookup_keys:
                disks_by_key[key] = disk
            if disk.slot is not None:
                disks_by_slot[(selected_option.id, disk.slot)] = disk
                disks_by_slot[(None, disk.slot)] = disk
            for alias in self._disk_sas_aliases(disk):
                disks_by_sas[alias] = disk

        api_topology_members = self._build_quantastor_topology_members(raw_data, disk_records)
        selected_meta = self._enclosure_option_meta(selected_option)
        empty_ssh = ParsedSSHData()
        quantastor_ses_candidates = quantastor_ses_data.ses_slot_candidates if quantastor_ses_data.ses_slot_candidates else {}
        slot_views: list[SlotView] = []

        for slot in range(layout_slot_count):
            mapping = self.mapping_store.get_mapping(self.system.id, selected_option.id, slot)
            ses_candidate = quantastor_ses_candidates.get(slot, {})
            slot_hints = {
                "present": False,
                "enclosure_id": selected_option.id,
                "enclosure_label": selected_option.label,
                "enclosure_name": selected_option.name,
            }
            self._merge_quantastor_ses_candidate(slot_hints, ses_candidate)
            disk = self._resolve_disk_for_slot(
                slot,
                selected_option.id,
                mapping,
                disks_by_key,
                disks_by_slot,
                disks_by_sas,
                slot_hints,
                empty_ssh,
            )
            raw_slot_status = {
                "present": disk is not None,
                "status": disk.health if disk else "Empty",
                "device_hint": disk.path_device_name if disk else None,
                "device_names": list(dict.fromkeys(disk.smart_devices if disk else [])),
                "serial_hint": disk.serial if disk else None,
                "model_hint": disk.model if disk else None,
                "reported_size": format_bytes(disk.size_bytes) if disk and disk.size_bytes else None,
                "enclosure_id": selected_option.id,
                "enclosure_label": selected_option.label,
                "enclosure_name": selected_option.name,
                "identify_active": self._quantastor_bool(disk.raw.get("isBlinking")) if disk else False,
                "sas_address_hint": normalize_hex_identifier(disk.raw.get("sasAddress")) if disk else None,
                "disk_raw": disk.raw if disk else None,
            }
            self._merge_quantastor_ses_candidate(raw_slot_status, ses_candidate)

            slot_view = self._build_slot_view(
                slot=slot,
                row_index=slot_positions.get(slot, (slot // max(layout_columns, 1), slot % max(layout_columns, 1)))[0],
                column_index=slot_positions.get(slot, (slot // max(layout_columns, 1), slot % max(layout_columns, 1)))[1],
                enclosure_meta=selected_meta,
                raw_slot_status=raw_slot_status,
                disk=disk,
                mapping=mapping,
                ssh_data=empty_ssh,
                api_topology_members=api_topology_members,
                api_enclosure_ids=set(),
            )
            if mapping and not disk:
                warnings.append(f"Manual mapping for slot {slot:02d} did not match any current Quantastor disk.")
            slot_views.append(slot_view)

        return (
            slot_views,
            available_enclosures,
            selected_meta,
            layout_rows,
            layout_slot_count,
            layout_columns,
        )

    def _build_quantastor_enclosure_options(self, raw_data: TrueNASRawData) -> list[EnclosureOption]:
        profile = self.profile_registry.resolve_for_enclosure(
            self.system,
            None,
            fallback_label=self.system.label or "Quantastor Enclosure",
            fallback_rows=1,
            fallback_columns=24,
            fallback_slot_count=24,
        )
        slot_layout = [list(row) for row in profile.slot_layout] if profile else None
        rows = profile.rows if profile else None
        columns = profile.columns if profile else None
        slot_count = infer_slot_count_from_layout(slot_layout or [], 24 if profile else None)
        hw_disks = self._quantastor_hw_disk_rows(raw_data)
        hw_enclosures = self._quantastor_hw_enclosure_rows(raw_data)
        hardware_system_ids = {
            system_id
            for system_id in (
                normalize_text(str(item.get("storageSystemId")) if item.get("storageSystemId") is not None else None)
                for item in [*hw_disks, *hw_enclosures]
            )
            if system_id
        }

        options: list[EnclosureOption] = []
        for system_row in raw_data.systems:
            system_id = normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None)
            if not system_id:
                continue
            if hardware_system_ids and system_id not in hardware_system_ids:
                continue
            label = normalize_text(
                system_row.get("name")
                or system_row.get("hostname")
                or system_row.get("description")
                or system_id
            )
            options.append(
                EnclosureOption(
                    id=system_id,
                    label=label or system_id,
                    name=normalize_text(
                        str(system_row.get("description") or system_row.get("nodeId") or label)
                        if (system_row.get("description") or system_row.get("nodeId") or label) is not None
                        else None
                    ),
                    rows=rows,
                    columns=columns,
                    slot_count=slot_count,
                    slot_layout=slot_layout,
                )
            )
        return options

    def _build_quantastor_disk_records(self, raw_data: TrueNASRawData, selected_system_id: str | None) -> list[DiskRecord]:
        hw_slot_hints = self._build_quantastor_hw_slot_hints(raw_data, selected_system_id)
        cli_disk_hints = self._build_quantastor_cli_disk_hints(raw_data.cli_disks, selected_system_id)
        pool_slot_hints = self._build_quantastor_pool_slot_hints(raw_data, selected_system_id)
        pool_names = {
            normalize_text(str(pool.get("id")) if pool.get("id") is not None else None): normalize_text(
                str(pool.get("name") or pool.get("description") or pool.get("id"))
                if (pool.get("name") or pool.get("description") or pool.get("id")) is not None
                else None
            )
            for pool in raw_data.pools
            if normalize_text(str(pool.get("id")) if pool.get("id") is not None else None)
        }

        records: list[DiskRecord] = []
        for disk in raw_data.disks:
            owner_id = normalize_text(
                str(disk.get("storageSystemId") or disk.get("systemId") or disk.get("controllerId"))
                if (disk.get("storageSystemId") or disk.get("systemId") or disk.get("controllerId")) is not None
                else None
            )
            if selected_system_id and owner_id and owner_id != selected_system_id:
                continue

            device_name = normalize_device_name(
                disk.get("devicePath") or disk.get("deviceName") or disk.get("device") or disk.get("name")
            )
            path_device_name = device_name
            serial = normalize_text(disk.get("serialNumber") or disk.get("serial"))
            model = normalize_text(
                disk.get("model")
                or " ".join(
                    filter(
                        None,
                        [
                            normalize_text(disk.get("vendorId") or disk.get("vendor")),
                            normalize_text(disk.get("productId") or disk.get("product")),
                        ],
                    )
                )
            )
            size_bytes = (
                disk.get("size")
                if isinstance(disk.get("size"), int)
                else disk.get("sizeBytes")
                if isinstance(disk.get("sizeBytes"), int)
                else parse_size_to_bytes(disk.get("size"))
            )
            disk_id = normalize_text(str(disk.get("id")) if disk.get("id") is not None else None)
            identifier, _ = resolve_persistent_id(
                normalize_text(disk.get("wwn")),
                normalize_text(disk.get("eui64")),
                normalize_text(disk.get("devicePath")),
                disk_id,
            )
            health = normalize_text(disk.get("healthStatus") or disk.get("status"))
            pool_id = normalize_text(
                str(disk.get("storagePoolId") or disk.get("poolId"))
                if (disk.get("storagePoolId") or disk.get("poolId")) is not None
                else None
            )
            lookup_keys = set()
            for value in (
                disk_id,
                device_name,
                path_device_name,
                serial,
                model,
                identifier,
                disk.get("devicePath"),
                disk.get("name"),
                disk.get("wwn"),
                disk.get("eui64"),
                disk.get("scsiId"),
                disk.get("wwid"),
                disk.get("hwDiskId"),
                disk.get("storagePoolDeviceId"),
            ):
                lookup_keys.update(normalize_lookup_keys(str(value) if value is not None else None))
            hint = next((hw_slot_hints[key] for key in lookup_keys if key in hw_slot_hints), None)
            cli_hint = next((cli_disk_hints[key] for key in lookup_keys if key in cli_disk_hints), None)
            pool_hint = next((pool_slot_hints[key] for key in lookup_keys if key in pool_slot_hints), None)
            merged_raw = dict(disk)
            if hint and isinstance(hint.get("hw_raw"), dict):
                merged_raw = self._merge_quantastor_payloads(hint["hw_raw"], merged_raw)
                merged_raw["quantastor_hw_disk"] = hint["hw_raw"]
                merged_raw["quantastor_hw_disk_source"] = hint.get("source")
                for value in (
                    hint["hw_raw"].get("physicalDiskId"),
                    hint["hw_raw"].get("serialNum"),
                    hint["hw_raw"].get("serialNumber"),
                    hint["hw_raw"].get("sasAddress"),
                    hint["hw_raw"].get("id"),
                ):
                    lookup_keys.update(normalize_lookup_keys(str(value) if value is not None else None))
            if isinstance(cli_hint, dict):
                merged_raw = self._merge_quantastor_payloads(cli_hint, merged_raw)
                merged_raw["quantastor_cli_disk"] = cli_hint
                for value in (
                    cli_hint.get("id"),
                    cli_hint.get("hwDiskId"),
                    cli_hint.get("serialNumber"),
                    cli_hint.get("scsiId"),
                    cli_hint.get("wwid"),
                    cli_hint.get("devicePath"),
                    cli_hint.get("altDevicePath"),
                    cli_hint.get("multipathParentDiskId"),
                ):
                    lookup_keys.update(normalize_lookup_keys(str(value) if value is not None else None))
                if not device_name:
                    device_name = normalize_device_name(
                        cli_hint.get("devicePath") or cli_hint.get("altDevicePath") or cli_hint.get("name")
                    )
                    path_device_name = device_name
            if not pool_hint:
                pool_hint = next((pool_slot_hints[key] for key in lookup_keys if key in pool_slot_hints), None)
            if pool_hint and isinstance(pool_hint.get("pool_device_raw"), dict):
                merged_raw["quantastor_pool_device"] = pool_hint["pool_device_raw"]
            slot = pool_hint.get("slot") if pool_hint else hint.get("slot") if hint else None
            if not isinstance(slot, int):
                slot = self._extract_quantastor_slot(disk)
            smart_devices = self._build_quantastor_smart_devices(disk, merged_raw, cli_hint, hint, pool_hint)

            records.append(
                DiskRecord(
                    raw=merged_raw,
                    device_name=device_name,
                    path_device_name=path_device_name,
                    multipath_name=None,
                    multipath_member=None,
                    serial=serial,
                    model=model,
                    size_bytes=size_bytes,
                    identifier=identifier,
                    health=health,
                    pool_name=pool_names.get(pool_id),
                    lunid=normalize_text(disk.get("wwn") or disk.get("scsiId")),
                    bus=normalize_text(disk.get("transportType") or disk.get("protocol") or disk.get("mediaInterface")),
                    temperature_c=self._extract_quantastor_int(disk, "temperature", "currentTemperature", "currTemp"),
                    last_smart_test_type=None,
                    last_smart_test_status=None,
                    last_smart_test_lifetime_hours=None,
                    logical_block_size=self._extract_quantastor_int(disk, "logicalBlockSize", "sectorSize", "logSectorSize"),
                    physical_block_size=self._extract_quantastor_int(disk, "physicalBlockSize", "phySectorSize"),
                    enclosure_id=normalize_text(str(hint.get("enclosure_id")) if hint and hint.get("enclosure_id") is not None else None) or selected_system_id,
                    slot=slot,
                    smart_devices=smart_devices,
                    lookup_keys=lookup_keys,
                )
            )
        return records

    def _select_quantastor_default_enclosure_id(
        self,
        raw_data: TrueNASRawData,
        options: list[EnclosureOption],
    ) -> str | None:
        option_ids = [option.id for option in options if option.id]
        if not option_ids:
            return None

        option_rank = {option_id: index for index, option_id in enumerate(option_ids)}
        owner_counts: dict[str, int] = {}
        for pool in raw_data.pools:
            owner_id = next(
                (
                    normalize_text(str(pool.get(key)) if pool.get(key) is not None else None)
                    for key in ("activeStorageSystemId", "primaryStorageSystemId", "storageSystemId")
                    if normalize_text(str(pool.get(key)) if pool.get(key) is not None else None) in option_rank
                ),
                None,
            )
            if owner_id:
                owner_counts[owner_id] = owner_counts.get(owner_id, 0) + 1

        if owner_counts:
            return min(owner_counts, key=lambda owner_id: (-owner_counts[owner_id], option_rank[owner_id]))

        for system_row in raw_data.systems:
            if not self._quantastor_bool(system_row.get("isMaster")):
                continue
            system_id = normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None)
            if system_id in option_rank:
                return system_id

        return option_ids[0]

    def _build_quantastor_smart_devices(
        self,
        disk: dict[str, Any],
        merged_raw: dict[str, Any],
        cli_hint: dict[str, Any] | None,
        hw_hint: dict[str, Any] | None,
        pool_hint: dict[str, Any] | None,
    ) -> list[str]:
        direct_candidates: list[str] = []
        stable_candidates: list[str] = []
        fallback_candidates: list[str] = []
        seen: set[str] = set()

        def add_candidate(value: Any) -> None:
            text = normalize_text(str(value) if value is not None else None)
            if not text:
                return
            normalized = normalize_device_name(text)
            if not normalized:
                return
            key = normalized.lower()
            if key in seen:
                return
            seen.add(key)

            if re.match(r"^(?:sd|da|ada|nvd)\d+$", key):
                direct_candidates.append(normalized)
            elif key.startswith("disk/by-id/scsi-"):
                stable_candidates.append(normalized)
            else:
                fallback_candidates.append(normalized)

        pool_device = pool_hint.get("pool_device_raw") if isinstance(pool_hint, dict) else None
        physical_disk_obj = pool_device.get("physicalDiskObj") if isinstance(pool_device, dict) else None
        hw_raw = hw_hint.get("hw_raw") if isinstance(hw_hint, dict) else None

        for payload in (
            disk,
            merged_raw,
            cli_hint,
            hw_raw,
            pool_device,
            physical_disk_obj,
        ):
            if not isinstance(payload, dict):
                continue
            for value in (
                payload.get("altDevicePath"),
                payload.get("devicePath"),
                payload.get("deviceName"),
                payload.get("device"),
                payload.get("name"),
            ):
                add_candidate(value)

        return [*stable_candidates, *direct_candidates, *fallback_candidates]

    def _build_quantastor_hw_slot_hints(
        self,
        raw_data: TrueNASRawData,
        selected_system_id: str | None,
    ) -> dict[str, dict[str, Any]]:
        grouped_rows: dict[str, list[dict[str, Any]]] = {}
        row_source = "cli" if raw_data.cli_hw_disks else "api"
        for row in self._quantastor_hw_disk_rows(raw_data):
            slot = self._extract_quantastor_slot(row)
            if slot is None:
                continue
            group_key = (
                normalize_hex_identifier(row.get("sasAddress"))
                or normalize_text(row.get("serialNum") or row.get("serialNumber"))
                or normalize_text(str(row.get("physicalDiskId")) if row.get("physicalDiskId") is not None else None)
                or normalize_text(str(row.get("id")) if row.get("id") is not None else None)
            )
            if not group_key:
                continue
            grouped_rows.setdefault(group_key, []).append(row)

        hints: dict[str, dict[str, Any]] = {}
        for rows in grouped_rows.values():
            canonical_slot = self._select_quantastor_canonical_slot(rows)
            preferred_row = next(
                (
                    row
                    for row in rows
                    if normalize_text(str(row.get("storageSystemId")) if row.get("storageSystemId") is not None else None)
                    == selected_system_id
                    and self._extract_quantastor_slot(row) == canonical_slot
                ),
                None,
            )
            if preferred_row is None:
                preferred_row = next(
                    (
                        row
                        for row in rows
                        if self._extract_quantastor_slot(row) == canonical_slot
                        and len(str(row.get("slot") or "")) > 1
                    ),
                    None,
                )
            if preferred_row is None:
                preferred_row = next(
                    (
                        row
                        for row in rows
                        if normalize_text(str(row.get("storageSystemId")) if row.get("storageSystemId") is not None else None)
                        == selected_system_id
                    ),
                    None,
                )
            if preferred_row is None:
                preferred_row = rows[0]

            hint = {
                "slot": canonical_slot,
                "enclosure_id": normalize_text(
                    str(preferred_row.get("enclosureId")) if preferred_row.get("enclosureId") is not None else None
                ),
                "controller_id": normalize_text(
                    str(preferred_row.get("controllerId")) if preferred_row.get("controllerId") is not None else None
                ),
                "hw_raw": preferred_row,
                "source": row_source,
            }
            for row in rows:
                for value in (
                    row.get("physicalDiskId"),
                    row.get("serialNum"),
                    row.get("serialNumber"),
                    row.get("sasAddress"),
                    row.get("id"),
                ):
                    for key in normalize_lookup_keys(str(value) if value is not None else None):
                        hints[key] = hint
        return hints

    def _build_quantastor_cli_disk_hints(
        self,
        rows: list[dict[str, Any]],
        selected_system_id: str | None,
    ) -> dict[str, dict[str, Any]]:
        hints: dict[str, tuple[int, dict[str, Any]]] = {}
        for row in rows:
            score = self._score_quantastor_cli_disk_row(row, selected_system_id)
            for value in (
                row.get("id"),
                row.get("hwDiskId"),
                row.get("serialNumber"),
                row.get("scsiId"),
                row.get("wwid"),
                row.get("devicePath"),
                row.get("altDevicePath"),
                row.get("multipathParentDiskId"),
                row.get("name"),
            ):
                for key in normalize_lookup_keys(str(value) if value is not None else None):
                    current = hints.get(key)
                    if current is None or score > current[0]:
                        hints[key] = (score, row)
        return {key: value[1] for key, value in hints.items()}

    def _build_quantastor_pool_slot_hints(
        self,
        raw_data: TrueNASRawData,
        selected_system_id: str | None,
    ) -> dict[str, dict[str, Any]]:
        hints: dict[str, tuple[int, dict[str, Any]]] = {}
        for row in raw_data.pool_devices:
            slot = self._extract_quantastor_slot(row)
            if slot is None:
                continue
            owner_id = normalize_text(
                str(row.get("storageSystemId")) if row.get("storageSystemId") is not None else None
            )
            score = 0
            if selected_system_id and owner_id == selected_system_id:
                score += 8
            if self._quantastor_bool(row.get("isSpare")):
                score += 4
            hint = {
                "slot": slot,
                "storage_system_id": owner_id,
                "pool_device_raw": row,
            }
            for value in (
                row.get("physicalDiskId"),
                row.get("physicalDiskSerialNumber"),
                row.get("physicalDiskScsiId"),
                row.get("devicePath"),
                row.get("id"),
            ):
                for key in normalize_lookup_keys(str(value) if value is not None else None):
                    current = hints.get(key)
                    if current is None or score > current[0]:
                        hints[key] = (score, hint)
        return {key: value[1] for key, value in hints.items()}

    @staticmethod
    def _select_quantastor_canonical_slot(rows: list[dict[str, Any]]) -> int | None:
        slot_counter: Counter[int] = Counter()
        raw_widths: dict[int, int] = {}
        for row in rows:
            slot = InventoryService._extract_quantastor_slot(row)
            if slot is None:
                continue
            slot_counter[slot] += 1
            raw_widths[slot] = max(raw_widths.get(slot, 0), len(str(row.get("slot") or "")))
        if not slot_counter:
            return None
        return max(slot_counter, key=lambda slot: (slot_counter[slot], raw_widths.get(slot, 0), slot))

    def _build_quantastor_topology_members(
        self,
        raw_data: TrueNASRawData,
        disk_records: list[DiskRecord],
    ) -> dict[str, ZpoolMember]:
        pool_index = {
            normalize_text(str(pool.get("id")) if pool.get("id") is not None else None): pool
            for pool in raw_data.pools
            if normalize_text(str(pool.get("id")) if pool.get("id") is not None else None)
        }
        system_index = {
            normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None): normalize_text(
                str(system_row.get("name") or system_row.get("hostname") or system_row.get("id"))
                if (system_row.get("name") or system_row.get("hostname") or system_row.get("id")) is not None
                else None
            )
            for system_row in raw_data.systems
            if normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None)
        }

        members: dict[str, ZpoolMember] = {}
        for device in raw_data.pool_devices:
            pool_id = normalize_text(
                str(device.get("storagePoolId") or device.get("poolId"))
                if (device.get("storagePoolId") or device.get("poolId")) is not None
                else None
            )
            pool = pool_index.get(pool_id)
            if pool is None:
                continue

            pool_name = normalize_text(
                str(pool.get("name") or pool.get("description") or pool_id)
                if (pool.get("name") or pool.get("description") or pool_id) is not None
                else None
            ) or "Pool"
            member_number = device.get("number")
            vdev_name = normalize_text(device.get("raidGroupId") or device.get("name") or device.get("deviceName")) or (
                f"member-{member_number}" if isinstance(member_number, int) else "member"
            )
            vdev_class = normalize_text(
                device.get("class")
                or device.get("usageType")
                or device.get("role")
                or ("spare" if device.get("isSpare") or normalize_text(device.get("raidGroupId")) == "spares" else None)
            ) or "data"
            owner_label = self._quantastor_pool_owner_label(pool, system_index)
            topology_label = f"{pool_name} > {vdev_name} > {vdev_class}"
            if owner_label:
                topology_label = f"{topology_label} ({owner_label})"

            member = ZpoolMember(
                pool_name=pool_name,
                vdev_class=vdev_class,
                vdev_name=vdev_name,
                topology_label=topology_label,
                health=normalize_text(device.get("status") or pool.get("status") or pool.get("health")),
                raw_name=normalize_text(device.get("name") or device.get("devicePath")),
                raw_path=normalize_text(device.get("devicePath")),
            )
            for key in normalize_lookup_keys(device.get("physicalDiskId")):
                members[key] = member
            for key in normalize_lookup_keys(device.get("devicePath")):
                members[key] = member
            for key in normalize_lookup_keys(device.get("physicalDiskSerialNumber")):
                members[key] = member
            for key in normalize_lookup_keys(device.get("physicalDiskScsiId")):
                members[key] = member
            physical_disk_obj = device.get("physicalDiskObj")
            if isinstance(physical_disk_obj, dict):
                for value in (
                    physical_disk_obj.get("id"),
                    physical_disk_obj.get("serialNumber"),
                    physical_disk_obj.get("scsiId"),
                    physical_disk_obj.get("devicePath"),
                    physical_disk_obj.get("altDevicePath"),
                ):
                    for key in normalize_lookup_keys(str(value) if value is not None else None):
                        members[key] = member

        if members:
            return members

        for disk in disk_records:
            pool_id = normalize_text(
                str(disk.raw.get("storagePoolId") or disk.raw.get("poolId"))
                if (disk.raw.get("storagePoolId") or disk.raw.get("poolId")) is not None
                else None
            )
            pool = pool_index.get(pool_id)
            if pool is None:
                continue
            pool_name = normalize_text(
                str(pool.get("name") or pool.get("description") or pool_id)
                if (pool.get("name") or pool.get("description") or pool_id) is not None
                else None
            ) or "Pool"
            owner_label = self._quantastor_pool_owner_label(pool, system_index)
            topology_label = f"{pool_name} > disk > data"
            if owner_label:
                topology_label = f"{topology_label} ({owner_label})"
            member = ZpoolMember(
                pool_name=pool_name,
                vdev_class="data",
                vdev_name="disk",
                topology_label=topology_label,
                health=disk.health,
                raw_name=normalize_text(disk.raw.get("name") or disk.raw.get("devicePath")),
                raw_path=normalize_text(disk.raw.get("devicePath")),
            )
            for key in disk.lookup_keys:
                members[key] = member
        return members

    @staticmethod
    def _quantastor_pool_owner_label(pool: dict[str, Any], system_index: dict[str, str | None]) -> str | None:
        owner_id = normalize_text(
            str(pool.get("activeStorageSystemId") or pool.get("primaryStorageSystemId") or pool.get("storageSystemId"))
            if (pool.get("activeStorageSystemId") or pool.get("primaryStorageSystemId") or pool.get("storageSystemId")) is not None
            else None
        )
        if not owner_id:
            return None
        owner_name = system_index.get(owner_id) or owner_id
        return f"active on {owner_name}"

    @staticmethod
    def _quantastor_has_cluster_peers(raw_data: TrueNASRawData) -> bool:
        cluster_members: dict[str, set[str]] = {}
        for system_row in raw_data.systems:
            cluster_id = normalize_text(
                str(system_row.get("storageSystemClusterId"))
                if system_row.get("storageSystemClusterId") is not None
                else None
            )
            system_id = normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None)
            if cluster_id and system_id:
                cluster_members.setdefault(cluster_id, set()).add(system_id)
        return any(len(members) > 1 for members in cluster_members.values())

    def _build_quantastor_cluster_warnings(
        self,
        raw_data: TrueNASRawData,
        selected_system_id: str | None,
    ) -> list[str]:
        if not (raw_data.ha_groups or self._quantastor_has_cluster_peers(raw_data)):
            return []

        systems_by_id = {
            normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None): system_row
            for system_row in raw_data.systems
            if normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None)
        }
        hardware_system_ids = {
            system_id
            for system_id in (
                normalize_text(str(item.get("storageSystemId")) if item.get("storageSystemId") is not None else None)
                for item in [*self._quantastor_hw_disk_rows(raw_data), *self._quantastor_hw_enclosure_rows(raw_data)]
            )
            if system_id
        }

        selected_row = systems_by_id.get(selected_system_id)
        cluster_id = normalize_text(
            str(selected_row.get("storageSystemClusterId"))
            if isinstance(selected_row, dict) and selected_row.get("storageSystemClusterId") is not None
            else None
        )

        cluster_rows = [
            row
            for row in raw_data.systems
            if (
                cluster_id
                and normalize_text(
                    str(row.get("storageSystemClusterId")) if row.get("storageSystemClusterId") is not None else None
                )
                == cluster_id
            )
        ]
        node_rows = [
            row
            for row in cluster_rows
            if normalize_text(str(row.get("id")) if row.get("id") is not None else None) in hardware_system_ids
        ] or cluster_rows

        master_row = next((row for row in node_rows if self._quantastor_bool(row.get("isMaster"))), None)
        selected_label = normalize_text(
            str(selected_row.get("name") or selected_row.get("hostname") or selected_system_id)
            if isinstance(selected_row, dict) and (selected_row.get("name") or selected_row.get("hostname") or selected_system_id)
            else selected_system_id
        ) or "selected node"
        warnings: list[str] = []

        if master_row is not None:
            master_id = normalize_text(str(master_row.get("id")) if master_row.get("id") is not None else None)
            master_label = normalize_text(
                str(master_row.get("name") or master_row.get("hostname") or master_id)
                if (master_row.get("name") or master_row.get("hostname") or master_id) is not None
                else None
            ) or "unknown master"
            if selected_system_id and master_id and selected_system_id != master_id:
                warnings.append(
                    f"Quantastor HA detected. Cluster master is {master_label}; selected view is {selected_label}."
                )
            else:
                warnings.append(f"Quantastor HA detected. Cluster master is {master_label}.")
        else:
            warnings.append(
                "Quantastor HA groups were detected. This first-pass adapter renders storage-system-scoped views; "
                "shared-slot ownership overlays and IO-fencing context are still future work."
            )

        # Quantastor can return an aggregate cluster object alongside the real
        # node records. The aggregate may advertise broader policy defaults that
        # do not match the current node-level state shown in the appliance UI,
        # so prefer the hardware-backed node rows when deciding whether to warn.
        io_fencing_rows = node_rows or cluster_rows
        if any(self._quantastor_bool(row.get("disableIoFencing")) for row in io_fencing_rows):
            warnings.append(
                "Quantastor cluster metadata reports IO fencing is currently disabled."
            )

        return warnings

    def _build_quantastor_platform_context(
        self,
        raw_data: TrueNASRawData,
        selected_system_id: str | None,
    ) -> dict[str, Any]:
        systems_by_id = {
            normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None): system_row
            for system_row in raw_data.systems
            if normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None)
        }
        system_labels = self._quantastor_system_label_index(raw_data)
        selected_row = systems_by_id.get(selected_system_id)
        if selected_row is None:
            return {}

        cluster_id = normalize_text(
            str(selected_row.get("storageSystemClusterId"))
            if selected_row.get("storageSystemClusterId") is not None
            else None
        )
        cluster_rows = [
            row
            for row in raw_data.systems
            if (
                cluster_id
                and normalize_text(
                    str(row.get("storageSystemClusterId")) if row.get("storageSystemClusterId") is not None else None
                ) == cluster_id
            )
        ] or [selected_row]
        hardware_system_ids = {
            system_id
            for system_id in (
                normalize_text(str(item.get("storageSystemId")) if item.get("storageSystemId") is not None else None)
                for item in [*self._quantastor_hw_disk_rows(raw_data), *self._quantastor_hw_enclosure_rows(raw_data)]
            )
            if system_id
        }
        node_rows = [
            row
            for row in cluster_rows
            if normalize_text(str(row.get("id")) if row.get("id") is not None else None) in hardware_system_ids
        ] or cluster_rows
        master_row = next((row for row in node_rows if self._quantastor_bool(row.get("isMaster"))), None)
        io_fencing_rows = node_rows or cluster_rows
        selected_label = system_labels.get(selected_system_id) or selected_system_id
        peer_labels = [
            system_labels.get(system_id) or system_id
            for system_id in (
                normalize_text(str(row.get("id")) if row.get("id") is not None else None)
                for row in node_rows
            )
            if system_id and system_id != selected_system_id
        ]
        master_id = normalize_text(str(master_row.get("id")) if master_row and master_row.get("id") is not None else None)

        return {
            "cluster_id": cluster_id,
            "selected_view_id": selected_system_id,
            "selected_view_label": selected_label,
            "cluster_node_labels": [
                system_labels.get(system_id) or system_id
                for system_id in (
                    normalize_text(str(row.get("id")) if row.get("id") is not None else None)
                    for row in node_rows
                )
                if system_id
            ],
            "peer_labels": peer_labels,
            "master_system_id": master_id,
            "master_label": system_labels.get(master_id) or master_id,
            "selected_is_master": bool(selected_system_id and master_id and selected_system_id == master_id),
            "io_fencing_enabled": (
                None
                if not io_fencing_rows
                else not any(self._quantastor_bool(row.get("disableIoFencing")) for row in io_fencing_rows)
            ),
        }

    @staticmethod
    def _quantastor_system_label_index(raw_data: TrueNASRawData) -> dict[str, str]:
        labels: dict[str, str] = {}
        for system_row in raw_data.systems:
            system_id = normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None)
            if not system_id:
                continue
            labels[system_id] = (
                normalize_text(
                    str(system_row.get("name") or system_row.get("hostname") or system_id)
                    if (system_row.get("name") or system_row.get("hostname") or system_id) is not None
                    else None
                )
                or system_id
            )
        return labels

    def _build_quantastor_presence_hints(self, raw_data: TrueNASRawData) -> dict[str, set[str]]:
        hints: dict[str, set[str]] = {}
        rows = [*raw_data.disks, *raw_data.cli_disks, *self._quantastor_hw_disk_rows(raw_data)]
        for row in rows:
            owner_id = normalize_text(
                str(row.get("storageSystemId") or row.get("iofenceSystemId") or row.get("controllerId"))
                if (row.get("storageSystemId") or row.get("iofenceSystemId") or row.get("controllerId")) is not None
                else None
            )
            if not owner_id:
                continue
            for key in self._collect_quantastor_lookup_keys(row):
                hints.setdefault(key, set()).add(owner_id)
        return hints

    @staticmethod
    def _collect_quantastor_lookup_keys(payload: dict[str, Any] | None) -> set[str]:
        if not isinstance(payload, dict):
            return set()

        keys: set[str] = set()
        for value in (
            payload.get("id"),
            payload.get("storagePoolDeviceId"),
            payload.get("hwDiskId"),
            payload.get("physicalDiskId"),
            payload.get("serialNumber"),
            payload.get("serialNum"),
            payload.get("scsiId"),
            payload.get("wwid"),
            payload.get("devicePath"),
            payload.get("altDevicePath"),
            payload.get("name"),
            payload.get("sasAddress"),
            payload.get("vpd83Id"),
            payload.get("multipathParentDiskId"),
        ):
            keys.update(normalize_lookup_keys(str(value) if value is not None else None))
        return keys

    def _annotate_quantastor_slot_context(
        self,
        slot_views: list[SlotView],
        raw_data: TrueNASRawData,
        selected_system_id: str | None,
        platform_context: dict[str, Any],
    ) -> None:
        system_labels = self._quantastor_system_label_index(raw_data)
        presence_hints = self._build_quantastor_presence_hints(raw_data)
        pool_index = {
            normalize_text(str(pool.get("id")) if pool.get("id") is not None else None): pool
            for pool in raw_data.pools
            if normalize_text(str(pool.get("id")) if pool.get("id") is not None else None)
        }

        for slot_view in slot_views:
            raw_disk = slot_view.raw_status.get("disk_raw") if isinstance(slot_view.raw_status, dict) else None
            if not isinstance(raw_disk, dict):
                continue

            lookup_keys = self._collect_quantastor_lookup_keys(raw_disk)
            cli_disk = raw_disk.get("quantastor_cli_disk") if isinstance(raw_disk.get("quantastor_cli_disk"), dict) else None
            hw_disk = raw_disk.get("quantastor_hw_disk") if isinstance(raw_disk.get("quantastor_hw_disk"), dict) else None
            pool_device = raw_disk.get("quantastor_pool_device") if isinstance(raw_disk.get("quantastor_pool_device"), dict) else None
            physical_disk_obj = None
            if pool_device and isinstance(pool_device.get("physicalDiskObj"), dict):
                physical_disk_obj = pool_device.get("physicalDiskObj")

            lookup_keys.update(self._collect_quantastor_lookup_keys(cli_disk))
            lookup_keys.update(self._collect_quantastor_lookup_keys(hw_disk))
            lookup_keys.update(self._collect_quantastor_lookup_keys(pool_device))
            lookup_keys.update(self._collect_quantastor_lookup_keys(physical_disk_obj))

            visible_on_ids = sorted({system_id for key in lookup_keys for system_id in presence_hints.get(key, set())})
            visible_on_labels = [system_labels.get(system_id) or system_id for system_id in visible_on_ids if system_id]

            pool_id = normalize_text(
                str(
                    raw_disk.get("storagePoolId")
                    or raw_disk.get("poolId")
                    or (pool_device.get("storagePoolId") if pool_device else None)
                )
                if (
                    raw_disk.get("storagePoolId")
                    or raw_disk.get("poolId")
                    or (pool_device.get("storagePoolId") if pool_device else None)
                ) is not None
                else None
            )
            pool = pool_index.get(pool_id)
            pool_owner_id = normalize_text(
                str(
                    (pool.get("activeStorageSystemId") if isinstance(pool, dict) else None)
                    or (pool.get("primaryStorageSystemId") if isinstance(pool, dict) else None)
                    or (pool.get("storageSystemId") if isinstance(pool, dict) else None)
                    or (pool_device.get("storageSystemId") if pool_device else None)
                )
                if (
                    (pool.get("activeStorageSystemId") if isinstance(pool, dict) else None)
                    or (pool.get("primaryStorageSystemId") if isinstance(pool, dict) else None)
                    or (pool.get("storageSystemId") if isinstance(pool, dict) else None)
                    or (pool_device.get("storageSystemId") if pool_device else None)
                ) is not None
                else None
            )
            fence_owner_id = normalize_text(
                str(
                    raw_disk.get("iofenceSystemId")
                    or (cli_disk.get("iofenceSystemId") if cli_disk else None)
                    or (physical_disk_obj.get("iofenceSystemId") if physical_disk_obj else None)
                )
                if (
                    raw_disk.get("iofenceSystemId")
                    or (cli_disk.get("iofenceSystemId") if cli_disk else None)
                    or (physical_disk_obj.get("iofenceSystemId") if physical_disk_obj else None)
                ) is not None
                else None
            )
            presented_by_id = normalize_text(
                str(raw_disk.get("storageSystemId")) if raw_disk.get("storageSystemId") is not None else None
            )
            pool_device_node_id = normalize_text(
                str(pool_device.get("storageSystemId")) if pool_device and pool_device.get("storageSystemId") is not None else None
            )

            selected_label = platform_context.get("selected_view_label")
            pool_owner_label = system_labels.get(pool_owner_id) or pool_owner_id
            fence_owner_label = system_labels.get(fence_owner_id) or fence_owner_id
            presented_by_label = system_labels.get(presented_by_id) or presented_by_id
            pool_device_node_label = system_labels.get(pool_device_node_id) or pool_device_node_id

            notes: list[str] = []
            if selected_label and pool_owner_label and selected_label != pool_owner_label:
                notes.append(f"Pool is active on {pool_owner_label}, while this view is {selected_label}.")
            if selected_label and fence_owner_label and selected_label != fence_owner_label:
                notes.append(f"I/O fencing currently resolves to {fence_owner_label}.")

            slot_view.operator_context = {
                "selected_view_label": selected_label,
                "cluster_master_label": platform_context.get("master_label"),
                "presented_by_label": presented_by_label,
                "pool_owner_label": pool_owner_label,
                "fence_owner_label": fence_owner_label,
                "pool_device_node_label": pool_device_node_label,
                "visible_on_labels": visible_on_labels,
                "io_fencing_enabled": platform_context.get("io_fencing_enabled"),
                "is_remote_presentation": self._quantastor_bool(raw_disk.get("isRemote")),
                "selected_view_is_pool_owner": bool(selected_system_id and pool_owner_id and selected_system_id == pool_owner_id),
                "selected_view_is_fence_owner": bool(selected_system_id and fence_owner_id and selected_system_id == fence_owner_id),
                "ownership_revision": raw_disk.get("ownershipRevision"),
                "notes": notes,
            }

    @staticmethod
    def _quantastor_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return False

    def _build_quantastor_smart_summary(self, slot_view: SlotView) -> SmartSummaryView:
        raw_disk = slot_view.raw_status.get("disk_raw") if isinstance(slot_view.raw_status, dict) else None
        if not isinstance(raw_disk, dict):
            return self._fallback_smart_summary(
                slot_view,
                "Detailed Quantastor SMART drill-down is not wired for this slot yet.",
            )

        temperature_c = (
            self._extract_quantastor_int(raw_disk, "temperature", "currentTemperature", "currTemp")
            or self._extract_quantastor_temperature(raw_disk.get("driveTemp"))
            or slot_view.temperature_c
        )
        power_on_hours = self._extract_quantastor_int(raw_disk, "powerOnHours", "powerOnTimeHours", "powerOnTime")
        bytes_read = self._extract_quantastor_bytes(raw_disk, "bytesRead", "dataReadBytes", "logicalBytesRead")
        bytes_written = self._extract_quantastor_bytes(raw_disk, "bytesWritten", "dataWrittenBytes", "logicalBytesWritten")
        rotation_rate = self._extract_quantastor_int(raw_disk, "rotationRate", "rotationRateRpm", "rpm")
        if rotation_rate is None and raw_disk.get("isSsd") is True:
            rotation_rate = 0
        form_factor = normalize_text(raw_disk.get("formFactor") or raw_disk.get("diskFormFactor"))
        smart_health_status = normalize_text(raw_disk.get("smartHealthTest"))
        if smart_health_status and smart_health_status.startswith("[") and smart_health_status.endswith("]"):
            smart_health_status = smart_health_status[1:-1].strip() or smart_health_status
        transport_protocol = (
            normalize_text(raw_disk.get("transportType") or raw_disk.get("protocol") or raw_disk.get("mediaInterface"))
            or ("SAS" if normalize_text(raw_disk.get("sasAddress")) else None)
        )

        summary = SmartSummaryView(
            available=True,
            temperature_c=temperature_c,
            smart_health_status=smart_health_status,
            power_on_hours=power_on_hours,
            power_on_days=(power_on_hours // 24) if isinstance(power_on_hours, int) else None,
            logical_block_size=(
                self._extract_quantastor_int(raw_disk, "logicalBlockSize", "sectorSize", "logSectorSize", "blockSize")
                or slot_view.logical_block_size
            ),
            physical_block_size=(
                self._extract_quantastor_int(raw_disk, "physicalBlockSize", "phySectorSize", "blockSize")
                or slot_view.physical_block_size
            ),
            bytes_read=bytes_read,
            bytes_written=bytes_written,
            annualized_bytes_written=(
                int(bytes_written / max(power_on_hours / 8760, 1 / 8760))
                if isinstance(bytes_written, int) and isinstance(power_on_hours, int) and power_on_hours > 0
                else None
            ),
            endurance_remaining_percent=self._extract_quantastor_int(raw_disk, "ssdLifeLeft", "lifeLeftPercent"),
            media_errors=self._extract_quantastor_int(raw_disk, "mediumErrors", "mediaErrors", "readErrors", "writeErrors"),
            predictive_errors=self._extract_quantastor_int(raw_disk, "predictiveErrors", "errCountPredictive"),
            non_medium_errors=self._extract_quantastor_int(raw_disk, "errCountNonMedium", "nonMediumErrors"),
            uncorrected_read_errors=self._extract_quantastor_int(raw_disk, "errCountUncorrectedRead"),
            uncorrected_write_errors=self._extract_quantastor_int(raw_disk, "errCountUncorrectedWrite"),
            rotation_rate_rpm=rotation_rate,
            form_factor=form_factor,
            firmware_version=normalize_text(raw_disk.get("firmwareVersion") or raw_disk.get("revisionLevel")),
            trim_supported=(
                self._quantastor_bool(raw_disk.get("trimSupported"))
                if raw_disk.get("trimSupported") not in (None, "")
                else None
            ),
            transport_protocol=transport_protocol,
            logical_unit_id=normalize_text(raw_disk.get("wwn") or raw_disk.get("scsiId")) or slot_view.logical_unit_id,
            sas_address=normalize_hex_identifier(raw_disk.get("sasAddress")) or slot_view.sas_address,
            attached_sas_address=normalize_hex_identifier(
                raw_disk.get("attachedSasAddress")
                or slot_view.raw_status.get("attached_sas_address")
            ),
            message=(
                "Quantastor slot detail is first-pass and reflects the current appliance payload, "
                "supplemented with SSH CLI disk rows and smartctl when available."
                if raw_disk.get("quantastor_cli_disk") or raw_disk.get("quantastor_hw_disk_source") == "cli"
                else "Quantastor REST SMART detail is first-pass and reflects the current disk payload exposed by the appliance API."
            ),
        )
        if not any(
            value is not None
            for value in (
                summary.temperature_c,
                summary.smart_health_status,
                summary.power_on_hours,
                summary.bytes_read,
                summary.bytes_written,
                summary.rotation_rate_rpm,
                summary.trim_supported,
                summary.transport_protocol,
            )
        ):
            return self._fallback_smart_summary(
                slot_view,
                "Quantastor returned inventory for this disk, but no richer SMART counters in the current payload.",
            )
        return summary

    @staticmethod
    def _quantastor_hw_disk_rows(raw_data: TrueNASRawData) -> list[dict[str, Any]]:
        return raw_data.cli_hw_disks or raw_data.hw_disks

    @staticmethod
    def _quantastor_hw_enclosure_rows(raw_data: TrueNASRawData) -> list[dict[str, Any]]:
        return raw_data.cli_hw_enclosures or raw_data.hw_enclosures

    async def _fetch_quantastor_cli_overlay(self) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        overlay = {
            "cli_disks": [],
            "cli_hw_disks": [],
            "cli_hw_enclosures": [],
        }
        best_overlay = dict(overlay)
        best_score = 0
        best_host: str | None = None
        failures: list[str] = []
        target_specs = (
            ("disk-list", "disk inventory", "cli_disks"),
            ("hw-disk-list", "hardware disk inventory", "cli_hw_disks"),
            ("hw-enclosure-list", "hardware enclosure inventory", "cli_hw_enclosures"),
        )

        for host in self._build_quantastor_ssh_hosts():
            host_overlay = {
                "cli_disks": [],
                "cli_hw_disks": [],
                "cli_hw_enclosures": [],
            }
            host_failures: list[str] = []

            for subcommand, label, target in target_specs:
                result = await self._run_ssh_command(self._build_quantastor_cli_command(subcommand), host)
                if not result.ok:
                    detail = normalize_text(result.stderr) or normalize_text(result.stdout) or f"exit {result.exit_code}"
                    host_failures.append(f"Quantastor SSH CLI {label} failed on {host}: {detail}")
                    continue
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError:
                    host_failures.append(f"Quantastor SSH CLI {label} returned invalid JSON on {host}.")
                    continue
                rows = QuantastorRESTClient._ensure_list(payload)
                if not rows:
                    host_failures.append(f"Quantastor SSH CLI {label} returned no usable rows on {host}.")
                    continue
                host_overlay[target] = rows

            failures.extend(host_failures)
            host_score = sum(len(rows) for rows in host_overlay.values())
            if host_score > best_score:
                best_overlay = host_overlay
                best_score = host_score
                best_host = host

            populated_targets = sum(1 for rows in host_overlay.values() if rows)
            if populated_targets == len(target_specs) and host_score > 0:
                self._quantastor_preferred_ses_host = host
                return host_overlay, []

        if best_score <= 0:
            return overlay, failures
        self._quantastor_preferred_ses_host = best_host
        return best_overlay, []

    async def _fetch_quantastor_ses_overlay(self) -> tuple[ParsedSSHData, list[str]]:
        best_overlay = ParsedSSHData()
        best_score = 0
        best_host: str | None = None
        failures: list[str] = []
        preferred_host = normalize_text(self._quantastor_preferred_ses_host)

        for host in self._build_quantastor_ssh_hosts():
            device_discovery = await self._run_ssh_command(self._build_quantastor_ses_discovery_command(), host)
            if not device_discovery.ok:
                detail = normalize_text(device_discovery.stderr) or normalize_text(device_discovery.stdout) or (
                    f"exit {device_discovery.exit_code}"
                )
                failures.append(f"Quantastor SSH SES discovery failed on {host}: {detail}")
                continue

            devices = [
                normalize_text(line)
                for line in device_discovery.stdout.splitlines()
                if normalize_text(line) and normalize_text(line).startswith("/dev/sg")
            ]
            if not devices:
                failures.append(f"Quantastor SSH SES discovery found no usable sg_ses devices on {host}.")
                continue

            host_overlay = ParsedSSHData()
            host_score = 0
            host_failures: list[str] = []
            for device in devices:
                aes_command = shlex.join(["sudo", "-n", "/usr/bin/sg_ses", "-p", "aes", device])
                aes_result = await self._run_ssh_command(aes_command, host)
                if not aes_result.ok:
                    detail = normalize_text(aes_result.stderr) or normalize_text(aes_result.stdout) or (
                        f"exit {aes_result.exit_code}"
                    )
                    host_failures.append(f"Quantastor AES page probe failed on {host} {device}: {detail}")
                    continue

                outputs = {aes_command: aes_result.stdout}
                ec_command = shlex.join(["sudo", "-n", "/usr/bin/sg_ses", "-p", "ec", device])
                ec_result = await self._run_ssh_command(ec_command, host)
                if ec_result.ok:
                    outputs[ec_command] = ec_result.stdout

                parsed = parse_ssh_outputs(outputs, self.settings.layout.slot_count, None, None)
                score = sum(len(enclosure.slots) for enclosure in parsed.ses_enclosures)
                if score <= 0:
                    continue
                self._tag_quantastor_ses_overlay(parsed, host)
                if score > host_score:
                    host_overlay = parsed
                    host_score = score

            if host_failures:
                failures.extend(host_failures)
            if host_score > best_score:
                best_overlay = host_overlay
                best_score = host_score
                best_host = host
            if preferred_host and host == preferred_host and host_score > 0:
                self._quantastor_preferred_ses_host = preferred_host
                return host_overlay, []

        if best_score <= 0:
            return ParsedSSHData(), failures
        self._quantastor_preferred_ses_host = best_host
        return best_overlay, []

    def _build_quantastor_ssh_hosts(self) -> list[str]:
        hosts: list[str] = []
        for value in [self._quantastor_preferred_ses_host, self.system.ssh.host, *(self.system.ssh.extra_hosts or [])]:
            host = normalize_text(value)
            if host and host not in hosts:
                hosts.append(host)
        return hosts

    def _build_quantastor_preferred_hosts(self, slot_view: SlotView | None = None) -> list[str]:
        hosts: list[str] = []
        if slot_view:
            for target in slot_view.ssh_ses_targets:
                if not isinstance(target, dict):
                    continue
                host = normalize_text(target.get("ssh_host"))
                if host and host not in hosts:
                    hosts.append(host)
        for value in [self._quantastor_preferred_ses_host, self.system.ssh.host, *(self.system.ssh.extra_hosts or [])]:
            host = normalize_text(value)
            if host and host not in hosts:
                hosts.append(host)
        return hosts

    @staticmethod
    def _merge_quantastor_ses_candidate(target: dict[str, Any], ses_candidate: Any) -> None:
        if not isinstance(ses_candidate, dict):
            return
        if ses_candidate.get("descriptor"):
            target["descriptor"] = ses_candidate.get("descriptor")
        if ses_candidate.get("status"):
            target["status"] = ses_candidate.get("status")
        if ses_candidate.get("ses_device"):
            target["ses_device"] = ses_candidate.get("ses_device")
        if isinstance(ses_candidate.get("ses_element_id"), int):
            target["ses_element_id"] = ses_candidate.get("ses_element_id")
        if isinstance(ses_candidate.get("ses_targets"), list) and ses_candidate.get("ses_targets"):
            target["ses_targets"] = ses_candidate.get("ses_targets")
        if ses_candidate.get("sas_address_hint"):
            target["sas_address_hint"] = ses_candidate.get("sas_address_hint")
        if ses_candidate.get("sas_device_type"):
            target["sas_device_type"] = ses_candidate.get("sas_device_type")
        if ses_candidate.get("attached_sas_address"):
            target["attached_sas_address"] = ses_candidate.get("attached_sas_address")
        for field in (
            "ses_predicted_failure",
            "ses_disabled",
            "ses_hot_spare",
            "ses_do_not_remove",
            "ses_fault_sensed",
            "ses_fault_requested",
        ):
            if field in ses_candidate:
                target[field] = ses_candidate.get(field)
        if isinstance(ses_candidate.get("identify_active"), bool):
            target["identify_active"] = bool(target.get("identify_active")) or ses_candidate.get("identify_active")
        if isinstance(ses_candidate.get("present"), bool):
            target["present"] = ses_candidate.get("present")

    @staticmethod
    def _build_quantastor_ses_discovery_command() -> str:
        script = 'for dev in /dev/sg*; do if sudo -n /usr/bin/sg_ses -p aes "$dev" >/dev/null 2>&1; then echo "$dev"; fi; done'
        return shlex.join(["/bin/sh", "-lc", script])

    @staticmethod
    def _tag_quantastor_ses_overlay(ssh_data: ParsedSSHData, host: str) -> None:
        tagged_host = normalize_text(host)
        if not tagged_host:
            return

        for payload in ssh_data.ses_slot_candidates.values():
            targets = payload.get("ses_targets")
            if not isinstance(targets, list):
                continue
            tagged_targets: list[dict[str, Any]] = []
            for item in targets:
                if not isinstance(item, dict):
                    continue
                tagged = dict(item)
                tagged["ssh_host"] = tagged_host
                tagged_targets.append(tagged)
            if tagged_targets:
                payload["ses_targets"] = tagged_targets

        for enclosure in ssh_data.ses_enclosures:
            for slot in enclosure.slots.values():
                tagged_targets: list[dict[str, Any]] = []
                for item in slot.control_targets:
                    if not isinstance(item, dict):
                        continue
                    tagged = dict(item)
                    tagged["ssh_host"] = tagged_host
                    tagged_targets.append(tagged)
                if tagged_targets:
                    slot.control_targets = tagged_targets

    def _build_quantastor_cli_command(self, subcommand: str) -> str:
        args = ["/usr/bin/qs", subcommand, "--json"]
        server_spec = self._build_quantastor_cli_server_spec()
        if server_spec:
            args.append(f"--server={server_spec}")
        return shlex.join(args)

    def _build_quantastor_cli_server_spec(self) -> str | None:
        api_user = normalize_text(self.system.truenas.api_user)
        api_password = normalize_text(self.system.truenas.api_password)
        if api_user and api_password:
            return f"localhost,{api_user},{api_password}"
        return None

    def _score_quantastor_cli_disk_row(self, row: dict[str, Any], selected_system_id: str | None) -> int:
        score = 0
        owner_id = normalize_text(
            str(row.get("storageSystemId") or row.get("iofenceSystemId") or row.get("controllerId"))
            if (row.get("storageSystemId") or row.get("iofenceSystemId") or row.get("controllerId")) is not None
            else None
        )
        if selected_system_id and owner_id == selected_system_id:
            score += 8
        if normalize_text(str(row.get("storagePoolId")) if row.get("storagePoolId") is not None else None):
            score += 4
        if normalize_text(str(row.get("hwDiskId")) if row.get("hwDiskId") is not None else None):
            score += 3
        if normalize_text(str(row.get("multipathParentDiskId")) if row.get("multipathParentDiskId") is not None else None):
            score += 2
        device_path = normalize_text(row.get("devicePath"))
        if device_path and "/dev/disk/by-dmuuid/" not in device_path:
            score += 1
        return score

    @staticmethod
    def _merge_quantastor_payloads(preferred: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current)
        for key, value in preferred.items():
            if value in (None, "", [], {}):
                continue
            if merged.get(key) in (None, "", [], {}):
                merged[key] = value
        return merged

    @staticmethod
    def _extract_quantastor_slot(disk: dict[str, Any]) -> int | None:
        for key in ("slot", "slotNumber", "slotId", "enclosureSlotNumber", "bayNumber", "positionNumber"):
            value = disk.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text.isdigit():
                    numeric = int(text)
                    # Quantastor shared-slot payloads can mix literal zero-based slots
                    # like "0" and "12" with zero-padded single-digit slots like "01".
                    # Only the zero-padded single-digit form should be normalized down.
                    if len(text) > 1 and text.startswith("0") and numeric < 10:
                        return numeric - 1
                    return numeric
            if isinstance(value, int):
                return value
        return None

    @staticmethod
    def _extract_quantastor_int(payload: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    normalized = text.replace(",", "")
                    if re.fullmatch(r"-?\d+", normalized):
                        return int(normalized)
                    match = re.search(r"-?\d[\d,]*", text)
                    if match:
                        return int(match.group(0).replace(",", ""))
        return None

    @staticmethod
    def _extract_quantastor_temperature(value: Any) -> int | None:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            match = re.search(r"(-?\d+)", value)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _extract_quantastor_bytes(payload: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            parsed = parse_size_to_bytes(value)
            if parsed is not None:
                return parsed
        return None

    def _build_linux_enclosure_options(self) -> list[EnclosureOption]:
        profile = self.profile_registry.resolve_for_enclosure(
            self.system,
            None,
            fallback_label=self.system.label or "Linux Enclosure",
            fallback_rows=self.settings.layout.rows,
            fallback_columns=self.settings.layout.columns,
            fallback_slot_count=self.settings.layout.slot_count,
        )
        if profile is None:
            return []

        slot_count = infer_slot_count_from_layout(profile.slot_layout, profile.rows * profile.columns)
        enclosure_id = profile.id
        return [
            EnclosureOption(
                id=enclosure_id,
                label=profile.panel_title or profile.label,
                name=profile.label,
                profile_id=profile.id,
                rows=profile.rows,
                columns=profile.columns,
                slot_count=slot_count,
                slot_layout=profile.slot_layout,
            )
        ]

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
                    profile_id=enclosure.profile_id,
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

    @staticmethod
    def _extract_linux_vendor_slot(value: Any) -> int | None:
        if isinstance(value, int):
            return value - 1 if value > 0 else 0
        if isinstance(value, float) and value.is_integer():
            parsed = int(value)
            return parsed - 1 if parsed > 0 else 0
        if not isinstance(value, str):
            return None
        match = re.search(r"\d+", value.strip())
        if not match:
            return None
        parsed = int(match.group(0))
        return parsed - 1 if parsed > 0 else 0

    @staticmethod
    def _linux_vendor_slot_present(entry: dict[str, Any]) -> bool | None:
        healthy = normalize_text(entry.get("healthy") or entry.get("health"))
        state = normalize_text(entry.get("state"))
        if healthy and healthy.lower() == "none":
            return False
        if state and state.lower() in {"nodisk", "absent", "empty"}:
            return False
        if healthy or state:
            return True
        return None

    def _build_linux_vendor_slot_candidates(
        self,
        ssh_data: ParsedSSHData,
        selected_option: EnclosureOption,
    ) -> dict[int, dict[str, Any]]:
        candidates: dict[int, dict[str, Any]] = {}
        for entry in ssh_data.ubntstorage_disks:
            slot = self._extract_linux_vendor_slot(entry.get("slot"))
            if slot is None:
                continue
            node = normalize_device_name(entry.get("node"))
            present = self._linux_vendor_slot_present(entry)
            status = normalize_text(entry.get("healthy") or entry.get("state"))
            size_value = entry.get("size")
            device_names = [node] if node and present is not False else []
            candidates[slot] = {
                "vendor_slot": slot,
                "vendor_slot_number": slot + 1,
                "status": status,
                "value": status,
                "present": present,
                "device_names": device_names,
                "device_hint": device_names[0] if device_names else None,
                "serial_hint": normalize_text(entry.get("serial")),
                "model_hint": normalize_text(entry.get("model")),
                "reported_size": (
                    format_bytes(size_value)
                    if isinstance(size_value, int)
                    else normalize_text(str(size_value) if size_value is not None else None)
                ),
                "enclosure_id": selected_option.id,
                "enclosure_label": selected_option.label,
                "enclosure_name": selected_option.name,
                "vendor_raw": entry,
            }
        return candidates

    def _build_linux_disk_records(self, ssh_data: ParsedSSHData) -> list[DiskRecord]:
        controllers: dict[str, dict[str, Any]] = {}
        ubntstorage_by_node: dict[str, dict[str, Any]] = {}
        for entry in ssh_data.ubntstorage_disks:
            node = normalize_device_name(entry.get("node"))
            if node:
                ubntstorage_by_node[node.lower()] = entry
        for blockdevice in ssh_data.linux_blockdevices:
            device_name = normalize_text(blockdevice.get("name"))
            controller_name = extract_nvme_controller_name(device_name)
            if not controller_name:
                continue

            controller = controllers.setdefault(
                controller_name,
                {
                    "controller_name": controller_name,
                    "serial": None,
                    "model": None,
                    "size_bytes": 0,
                    "logical_block_size": None,
                    "physical_block_size": None,
                    "namespace_devices": [],
                    "lookup_keys": set(),
                    "primary_namespace": None,
                    "primary_rank": -1,
                    "top_array_path": None,
                    "top_array_name": None,
                    "top_mountpoint": None,
                    "top_role": None,
                    "identifier": None,
                    "transport_address": None,
                    "transport": None,
                },
            )

            controller["serial"] = controller["serial"] or normalize_text(blockdevice.get("serial"))
            controller["model"] = controller["model"] or normalize_text(blockdevice.get("model"))
            controller["logical_block_size"] = controller["logical_block_size"] or (
                blockdevice.get("log-sec") if isinstance(blockdevice.get("log-sec"), int) else None
            )
            controller["physical_block_size"] = controller["physical_block_size"] or (
                blockdevice.get("phy-sec") if isinstance(blockdevice.get("phy-sec"), int) else None
            )
            controller["size_bytes"] += parse_size_to_bytes(blockdevice.get("size")) or 0

            namespace_name = normalize_device_name(device_name)
            if namespace_name:
                controller["namespace_devices"].append(namespace_name)
                controller["lookup_keys"].update(normalize_lookup_keys(namespace_name))
            controller["lookup_keys"].update(normalize_lookup_keys(controller_name))
            for value in (
                blockdevice.get("serial"),
                blockdevice.get("model"),
                blockdevice.get("wwn"),
                blockdevice.get("ptuuid"),
            ):
                controller["lookup_keys"].update(normalize_lookup_keys(str(value) if value is not None else None))
            namespace_identifier = next(
                (
                    normalized
                    for normalized in (
                        normalize_text(str(blockdevice.get("wwn")) if blockdevice.get("wwn") is not None else None),
                        normalize_text(str(blockdevice.get("ptuuid")) if blockdevice.get("ptuuid") is not None else None),
                    )
                    if normalized
                ),
                None,
            )
            for identifier_candidate in (
                blockdevice.get("wwn"),
                blockdevice.get("ptuuid"),
            ):
                normalized_identifier = normalize_text(str(identifier_candidate) if identifier_candidate is not None else None)
                if normalized_identifier:
                    controller["identifier"] = controller["identifier"] or normalized_identifier

            controller["transport"] = controller["transport"] or normalize_text(blockdevice.get("tran"))
            subsystem_meta = ssh_data.linux_nvme_subsystems.get(controller_name.lower())
            if subsystem_meta:
                controller["transport_address"] = controller["transport_address"] or subsystem_meta.get("address")
                controller["transport"] = controller["transport"] or subsystem_meta.get("transport")
                controller["lookup_keys"].update(normalize_lookup_keys(subsystem_meta.get("address")))
                controller["lookup_keys"].update(normalize_lookup_keys(subsystem_meta.get("nqn")))

            descendants = self._collect_linux_descendants(blockdevice)
            raid_nodes = [node for node in descendants if str(node.get("type") or "").lower().startswith("raid")]
            mountpoints = [
                normalize_text(node.get("mountpoint"))
                for node in descendants
                if normalize_text(node.get("mountpoint"))
            ]
            top_array = raid_nodes[-1] if raid_nodes else None
            top_array_name = normalize_device_name(top_array.get("name")) if top_array else None
            top_array_path = normalize_text(top_array.get("path")) if top_array else None
            top_mountpoint = next(
                (mount for mount in mountpoints if mount not in {"/boot", "/boot/efi", "/"}),
                mountpoints[0] if mountpoints else None,
            )
            top_role = "system" if top_mountpoint in {"/", "/boot", "/boot/efi"} else "data"
            namespace_rank = self._linux_namespace_rank(top_mountpoint, top_array_name, namespace_name)
            if namespace_rank > controller["primary_rank"]:
                controller["primary_rank"] = namespace_rank
                controller["primary_namespace"] = namespace_name
                controller["top_array_name"] = top_array_name
                controller["top_array_path"] = top_array_path
                controller["top_mountpoint"] = top_mountpoint
                controller["top_role"] = top_role
                if namespace_identifier:
                    controller["identifier"] = namespace_identifier

        generic_records: list[DiskRecord] = []
        for blockdevice in ssh_data.linux_blockdevices:
            device_name = normalize_device_name(blockdevice.get("name"))
            if not device_name or extract_nvme_controller_name(device_name):
                continue
            if not re.match(r"^(sd|hd|vd|xvd)[a-z]+$", device_name):
                continue
            device_type = normalize_text(blockdevice.get("type"))
            if device_type and device_type.lower() != "disk":
                continue
            vendor_entry = ubntstorage_by_node.get(device_name.lower())

            descendants = self._collect_linux_descendants(blockdevice)
            raid_nodes = [node for node in descendants if str(node.get("type") or "").lower().startswith("raid")]
            mountpoints = [
                normalize_text(node.get("mountpoint"))
                for node in descendants
                if normalize_text(node.get("mountpoint"))
            ]
            top_array = raid_nodes[-1] if raid_nodes else None
            top_array_name = normalize_device_name(top_array.get("name")) if top_array else None
            top_array_path = normalize_text(top_array.get("path")) if top_array else None
            top_mountpoint = next(
                (mount for mount in mountpoints if mount not in {"/boot", "/boot/efi", "/"}),
                mountpoints[0] if mountpoints else None,
            )
            role = "system" if top_mountpoint in {"/", "/boot", "/boot/efi"} else "data"
            pool_name = top_mountpoint or top_array_name or "mdadm"
            identifier, _identifier_label = resolve_persistent_id(
                str(blockdevice.get("wwn")) if blockdevice.get("wwn") is not None else None,
                str(blockdevice.get("ptuuid")) if blockdevice.get("ptuuid") is not None else None,
            )
            vendor_slot = self._extract_linux_vendor_slot(vendor_entry.get("slot")) if vendor_entry else None
            lookup_keys: set[str] = set()
            for value in (
                device_name,
                blockdevice.get("path"),
                blockdevice.get("hctl"),
                blockdevice.get("serial"),
                blockdevice.get("model"),
                blockdevice.get("wwn"),
                blockdevice.get("ptuuid"),
                vendor_entry.get("node") if vendor_entry else None,
                vendor_entry.get("serial") if vendor_entry else None,
                vendor_entry.get("model") if vendor_entry else None,
            ):
                lookup_keys.update(normalize_lookup_keys(str(value) if value is not None else None))

            generic_records.append(
                DiskRecord(
                    raw={
                        "top_array_name": top_array_name,
                        "top_array_path": top_array_path,
                        "top_mountpoint": top_mountpoint,
                        "top_role": role,
                        "hctl": normalize_text(blockdevice.get("hctl")),
                        "transport": normalize_text(blockdevice.get("tran")),
                        "vendor_slot": vendor_slot,
                        "vendor_raw": vendor_entry,
                    },
                    device_name=device_name,
                    path_device_name=device_name,
                    multipath_name=None,
                    multipath_member=None,
                    serial=normalize_text(blockdevice.get("serial")) or normalize_text(vendor_entry.get("serial") if vendor_entry else None),
                    model=normalize_text(blockdevice.get("model")) or normalize_text(vendor_entry.get("model") if vendor_entry else None),
                    size_bytes=parse_size_to_bytes(blockdevice.get("size")),
                    identifier=identifier,
                    health=normalize_text(vendor_entry.get("healthy") if vendor_entry else None) or "ONLINE",
                    pool_name=pool_name,
                    lunid=None,
                    bus=(normalize_text(blockdevice.get("tran")) or "disk").upper(),
                    temperature_c=None,
                    last_smart_test_type=None,
                    last_smart_test_status=None,
                    last_smart_test_lifetime_hours=None,
                    logical_block_size=blockdevice.get("log-sec") if isinstance(blockdevice.get("log-sec"), int) else None,
                    physical_block_size=blockdevice.get("phy-sec") if isinstance(blockdevice.get("phy-sec"), int) else None,
                    enclosure_id=None,
                    slot=None,
                    smart_devices=[device_name],
                    lookup_keys=lookup_keys,
                )
            )

        records: list[DiskRecord] = []
        for controller_name, payload in controllers.items():
            namespace_devices = list(dict.fromkeys(payload["namespace_devices"]))
            primary_namespace = payload["primary_namespace"] or (namespace_devices[0] if namespace_devices else controller_name)
            smart_devices = [
                device
                for device in dict.fromkeys(
                    [primary_namespace, *namespace_devices]
                )
                if device
            ]
            top_array_name = payload["top_array_name"]
            top_mountpoint = payload["top_mountpoint"]
            role = payload["top_role"] or "data"
            pool_name = top_mountpoint or top_array_name or "mdadm"
            lookup_keys = set(payload["lookup_keys"])
            lookup_keys.update(normalize_lookup_keys(controller_name))
            lookup_keys.update(normalize_lookup_keys(primary_namespace))

            records.append(
                DiskRecord(
                    raw={
                        "controller_name": controller_name,
                        "namespace_devices": smart_devices,
                        "transport_address": payload["transport_address"],
                        "top_array_name": top_array_name,
                        "top_mountpoint": top_mountpoint,
                        "top_role": role,
                    },
                    device_name=controller_name,
                    path_device_name=primary_namespace,
                    multipath_name=None,
                    multipath_member=None,
                    serial=payload["serial"],
                    model=payload["model"],
                    size_bytes=payload["size_bytes"] or None,
                    identifier=payload["identifier"],
                    health="ONLINE",
                    pool_name=pool_name,
                    lunid=None,
                    bus=(payload["transport"] or "nvme").upper(),
                    temperature_c=None,
                    last_smart_test_type=None,
                    last_smart_test_status=None,
                    last_smart_test_lifetime_hours=None,
                    logical_block_size=payload["logical_block_size"],
                    physical_block_size=payload["physical_block_size"],
                    enclosure_id=None,
                    slot=None,
                    smart_devices=smart_devices or [primary_namespace],
                    lookup_keys=lookup_keys,
                )
            )

        records.extend(generic_records)
        return records

    @staticmethod
    def _collect_linux_descendants(node: dict[str, Any]) -> list[dict[str, Any]]:
        descendants: list[dict[str, Any]] = []
        children = node.get("children") if isinstance(node.get("children"), list) else []
        for child in children:
            if not isinstance(child, dict):
                continue
            descendants.append(child)
            descendants.extend(InventoryService._collect_linux_descendants(child))
        return descendants

    @staticmethod
    def _linux_namespace_rank(mountpoint: str | None, top_array_name: str | None, namespace_name: str | None) -> int:
        if mountpoint and mountpoint not in {"/", "/boot", "/boot/efi"}:
            return 4
        if top_array_name and top_array_name.startswith("md"):
            return 3
        if namespace_name and namespace_name.endswith("n1"):
            return 1
        return 2

    @staticmethod
    def _build_linux_topology_members(disk_records: list[DiskRecord]) -> dict[str, ZpoolMember]:
        members: dict[str, ZpoolMember] = {}
        for disk in disk_records:
            top_array_name = normalize_text(disk.raw.get("top_array_name"))
            top_mountpoint = normalize_text(disk.raw.get("top_mountpoint"))
            role = normalize_text(disk.raw.get("top_role")) or "data"
            if not top_array_name and not top_mountpoint:
                continue
            pool_name = top_mountpoint or "mdadm"
            topology_label = " > ".join(
                filter(
                    None,
                    [
                        pool_name,
                        top_array_name,
                        role,
                    ],
                )
            )
            member = ZpoolMember(
                pool_name=pool_name,
                vdev_class=role,
                vdev_name=top_array_name or disk.device_name,
                topology_label=topology_label,
                health=disk.health,
                raw_name=disk.device_name or "",
                raw_path=f"/dev/{disk.path_device_name or disk.device_name}" if (disk.path_device_name or disk.device_name) else None,
            )
            for value in (disk.device_name, disk.path_device_name, disk.identifier, *disk.smart_devices):
                for key in normalize_lookup_keys(value):
                    members[key] = member
        return members

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

            for candidate in dict.fromkeys(filter(None, [path_device_name, multipath_member, device_name])):
                for peer in ssh_data.camcontrol_peer_devices.get(candidate.lower(), []):
                    lookup_keys.update(normalize_lookup_keys(peer))

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
                    smart_devices=[
                        candidate
                        for candidate in dict.fromkeys(
                            filter(None, [path_device_name, multipath_member, device_name])
                        )
                    ],
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

        sas_address_hint = normalize_hex_identifier(raw_slot_status.get("sas_address_hint"))
        raw_present = raw_slot_status.get("present") if isinstance(raw_slot_status.get("present"), bool) else None
        sas_device_type = normalize_text(raw_slot_status.get("sas_device_type"))
        quantastor_ses_empty = (
            self.system.truenas.platform == "quantastor"
            and (
                normalize_text(raw_slot_status.get("ses_device"))
                or (isinstance(raw_slot_status.get("ses_targets"), list) and raw_slot_status.get("ses_targets"))
            )
            and (
                raw_present is False
                or sas_address_hint == "0"
                or (sas_device_type and "no sas device attached" in sas_device_type.lower())
            )
        )

        if self.system.truenas.platform == "quantastor" and sas_address_hint and sas_address_hint != "0":
            hinted = disks_by_sas.get(sas_address_hint)
            if hinted:
                return hinted

        if not quantastor_ses_empty:
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
        if not disk and self._is_placeholder_hint_device(device_name):
            device_name = None
        gptid = ssh_data.glabel.device_to_gptid.get(device_name.lower()) if device_name else None
        zpool = self._lookup_zpool_member(disk, device_name, gptid, ssh_data, api_topology_members)
        model = disk.model if disk else normalize_text(raw_slot_status.get("model_hint"))
        if not model and device_name:
            model = ssh_data.camcontrol_models.get(device_name.lower())
        multipath = self._build_multipath_view(disk, ssh_data)

        identify_active = bool(raw_slot_status.get("identify_active")) or self._status_contains(raw_slot_status, "identify", "led=locate")
        if not identify_active and slot in ssh_data.unifi_led_states:
            identify_active = ssh_data.unifi_led_states[slot]
        faulty = self._status_contains(raw_slot_status, "fault") or self._health_is_bad(
            disk.health if disk else None,
            zpool.health if zpool else None,
        )
        raw_present = raw_slot_status.get("present") if isinstance(raw_slot_status.get("present"), bool) else None
        quantastor_ses_empty = (
            self.system.truenas.platform == "quantastor"
            and raw_present is False
            and (
                normalize_text(raw_slot_status.get("ses_device"))
                or (isinstance(raw_slot_status.get("ses_targets"), list) and raw_slot_status.get("ses_targets"))
                or normalize_text(raw_slot_status.get("sas_device_type"))
                or normalize_hex_identifier(raw_slot_status.get("sas_address_hint")) == "0"
            )
        )
        empty = raw_present is False or (
            raw_present is None and not disk and self._status_contains(raw_slot_status, "empty", "not installed", "absent")
        )
        present = False if quantastor_ses_empty else (
            raw_present is True
            or disk is not None
            or self._status_contains(raw_slot_status, "ok", "installed", "ready", "present")
            or identify_active
            or faulty
        )

        if identify_active:
            state = SlotState.identify
        elif faulty:
            state = SlotState.fault
        elif quantastor_ses_empty or empty:
            state = SlotState.empty
        elif disk:
            state = SlotState.healthy
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
            disk.identifier if disk else None,
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
        seen_ses_targets: set[tuple[str | None, str | None, int | None]] = set()
        for item in raw_ses_targets:
            if not isinstance(item, dict):
                continue
            target_host = normalize_text(item.get("ssh_host"))
            target_device = normalize_text(item.get("ses_device"))
            target_element = item.get("ses_element_id")
            target_slot_number = item.get("ses_slot_number") if isinstance(item.get("ses_slot_number"), int) else None
            target_pair = (target_host, target_device, target_element if isinstance(target_element, int) else None)
            if target_pair in seen_ses_targets or not target_pair[1] or target_pair[2] is None:
                continue
            seen_ses_targets.add(target_pair)
            target_payload = {
                "ses_device": target_pair[1],
                "ses_element_id": target_pair[2],
                "ses_slot_number": target_slot_number,
            }
            if target_host:
                target_payload["ssh_host"] = target_host
            ses_targets.append(target_payload)
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
        unifi_vendor_slot_number = raw_slot_status.get("vendor_slot_number")
        unifi_fault_led_supported = bool(
            self.system.truenas.platform == "linux"
            and normalize_text(raw_slot_status.get("enclosure_id")) in UNIFI_GPIO_LED_PROFILE_IDS
            and self.system.ssh.enabled
            and isinstance(unifi_vendor_slot_number, int)
        )
        if self.system.truenas.platform == "quantastor" and self.system.ssh.enabled and ses_targets:
            led_supported = True
            led_backend = "quantastor_sg_ses"
            led_reason = None
        elif self.system.truenas.platform == "quantastor":
            led_supported = False
            led_backend = None
            led_reason = (
                "LED control is currently unavailable on this Quantastor cluster because the documented "
                "REST and CLI identify operations are being rejected, and no working SES enclosure path "
                "was discovered over SSH."
            )
        elif api_led_supported:
            led_supported = True
            led_backend = "api"
            led_reason = None
        elif scale_linux_ses_targets and self.system.ssh.enabled:
            led_supported = True
            led_backend = "scale_sg_ses"
            led_reason = None
        elif unifi_fault_led_supported:
            led_supported = True
            led_backend = "unifi_fault"
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
            smart_device_names=list(disk.smart_devices) if disk else [],
            serial=serial,
            model=model,
            size_bytes=size_bytes,
            size_human=size_human,
            gptid=persistent_id,
            persistent_id_label=persistent_id_label,
            pool_name=disk.pool_name if (disk and disk.pool_name) else zpool.pool_name if zpool else None,
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
            mapping_source=(
                mapping.source
                if mapping
                else "ssh"
                if ses_device or self.system.truenas.platform == "linux"
                else "api"
                if disk
                else "unknown"
            ),
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

    def _should_probe_unifi_gpio_debug(self, command_results: list[Any]) -> bool:
        if self.system.truenas.platform != "linux":
            return False
        if self.system.default_profile_id not in UNIFI_GPIO_LED_PROFILE_IDS:
            return False
        seen_commands = {canonicalize_ssh_command(item.command) for item in command_results}
        return "gpio debug" not in seen_commands

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
            target_host = normalize_text(target.get("ssh_host"))
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
            result = await self._run_ssh_command(command, target_host)
            if not result.ok:
                detail = result.stderr.strip() or result.stdout.strip() or "Unknown SSH LED error."
                target_label = f"{target_host}:{target_device}" if target_host else target_device
                failures.append(f"{target_label}:{target_element}: {detail}")

        if failures:
            raise TrueNASAPIError("SSH LED action failed: " + " | ".join(failures))

    async def _set_unifi_slot_led_over_ssh(self, slot_view: SlotView, action: LedAction) -> None:
        if not self.system.ssh.enabled:
            raise TrueNASAPIError("SSH fallback is disabled, so UniFi LED control cannot run.")

        vendor_slot_number = slot_view.raw_status.get("vendor_slot_number")
        if not isinstance(vendor_slot_number, int):
            raise TrueNASAPIError(
                slot_view.led_reason
                or f"Slot {slot_view.slot_label} is missing the UniFi vendor bay number required for LED control."
            )

        if action == LedAction.identify:
            toggle = "True"
        elif action == LedAction.clear:
            toggle = "False"
        else:
            raise TrueNASAPIError("UniFi SSH LED control currently supports identify on and clear/off only.")

        command = shlex.join(
            [
                "python3",
                "-c",
                f"from ustd.hwmon import sata_led_sm; sata_led_sm.set_fault({vendor_slot_number}, {toggle})",
            ]
        )
        result = await self._run_ssh_command(command)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown UniFi SSH LED error."
            raise TrueNASAPIError("SSH LED action failed: " + detail)

    async def _run_ssh_command(self, command: str, host: str | None = None) -> Any:
        target_host = normalize_text(host)
        if not target_host or target_host == normalize_text(self.system.ssh.host):
            return await self.ssh_probe.run_command(command)

        probe = SSHProbe(self.system.ssh.model_copy(update={"host": target_host}))
        return await probe.run_command(command)

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
        seen: set[str] = set()
        candidate_keys: list[str] = []

        for value in (
            gptid,
            device_name,
            *(disk.smart_devices if disk else []),
            disk.identifier if disk else None,
            str(disk.raw.get("zfs_guid")) if disk and disk.raw.get("zfs_guid") is not None else None,
            disk.serial if disk else None,
        ):
            for key in normalize_lookup_keys(value):
                if key not in seen:
                    seen.add(key)
                    candidate_keys.append(key)

        if disk:
            for key in sorted(disk.lookup_keys):
                lowered = key.lower()
                if lowered not in seen:
                    seen.add(lowered)
                    candidate_keys.append(lowered)

        for key in candidate_keys:
            member = ssh_data.zpool_members.get(key)
            if member:
                return member
            api_member = api_topology_members.get(key)
            if api_member:
                return api_member
        return None

    def _disk_sas_aliases(self, disk: DiskRecord) -> set[str]:
        aliases = build_lunid_aliases(disk.lunid, self.system.truenas.platform)
        raw_sources: list[dict[str, Any]] = []
        if isinstance(disk.raw, dict):
            raw_sources.append(disk.raw)
            for key in ("quantastor_hw_disk", "quantastor_cli_disk"):
                payload = disk.raw.get(key)
                if isinstance(payload, dict):
                    raw_sources.append(payload)

        for payload in raw_sources:
            for key in ("sasAddress", "portSasAddress", "scsiId", "wwid", "wwn"):
                aliases.update(
                    build_lunid_aliases(
                        str(payload.get(key)) if payload.get(key) is not None else None,
                        self.system.truenas.platform,
                    )
                )
        return aliases

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
    def _is_placeholder_hint_device(value: str | None) -> bool:
        return bool(value and HCTL_NAME_REGEX.fullmatch(value.strip()))

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

        for device in slot_view.smart_device_names or []:
            normalized = normalize_device_name(device)
            if normalized and not InventoryService._is_placeholder_hint_device(normalized) and normalized not in seen:
                seen.add(normalized)
                candidates.append(normalized)

        if slot_view.multipath:
            # Prefer ACTIVE member paths first so ad-hoc smartctl calls land on
            # the same physical leg the system is currently favoring.
            active_first = sorted(
                slot_view.multipath.members,
                key=lambda member: ((member.state or "").upper() != "ACTIVE", member.device_name),
            )
            for member in active_first:
                normalized = normalize_device_name(member.device_name)
                if normalized and not InventoryService._is_placeholder_hint_device(normalized) and normalized not in seen:
                    seen.add(normalized)
                    candidates.append(normalized)

            for fallback in (
                slot_view.multipath.path_device_name,
                slot_view.multipath.alternate_path_device,
            ):
                normalized = normalize_device_name(fallback)
                if normalized and not InventoryService._is_placeholder_hint_device(normalized) and normalized not in seen:
                    seen.add(normalized)
                    candidates.append(normalized)

        normalized_device = normalize_device_name(slot_view.device_name)
        if (
            normalized_device
            and not InventoryService._is_placeholder_hint_device(normalized_device)
            and normalized_device not in seen
        ):
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
                summary.warning_temperature_c,
                summary.critical_temperature_c,
                summary.smart_health_status,
                summary.last_test_type,
                summary.last_test_status,
                summary.last_test_lifetime_hours,
                summary.power_on_hours,
                summary.logical_block_size,
                summary.physical_block_size,
                summary.available_spare_percent,
                summary.available_spare_threshold_percent,
                summary.endurance_used_percent,
                summary.endurance_remaining_percent,
                summary.bytes_read,
                summary.bytes_written,
                summary.annualized_bytes_written,
                summary.estimated_lifetime_bytes_written,
                summary.estimated_remaining_bytes_written,
                summary.media_errors,
                summary.predictive_errors,
                summary.non_medium_errors,
                summary.uncorrected_read_errors,
                summary.uncorrected_write_errors,
                summary.unsafe_shutdowns,
                summary.rotation_rate_rpm,
                summary.form_factor,
                summary.firmware_version,
                summary.protocol_version,
                summary.namespace_eui64,
                summary.namespace_nguid,
                summary.read_cache_enabled,
                summary.writeback_cache_enabled,
                summary.trim_supported,
                summary.transport_protocol,
                summary.logical_unit_id,
                summary.sas_address,
                summary.attached_sas_address,
                summary.negotiated_link_rate,
            )
        )
        return summary

    @staticmethod
    def _summary_needs_ssh_enrichment(summary: SmartSummaryView) -> bool:
        return any(
            value is None
            for value in (
                summary.power_on_hours,
                summary.rotation_rate_rpm,
                summary.form_factor,
                summary.read_cache_enabled,
                summary.writeback_cache_enabled,
                summary.transport_protocol,
                summary.sas_address,
                summary.attached_sas_address,
                summary.negotiated_link_rate,
            )
        )

    @staticmethod
    def _merge_missing_smart_fields(
        primary: SmartSummaryView,
        supplement: SmartSummaryView,
    ) -> SmartSummaryView:
        for field_name in (
            "temperature_c",
            "warning_temperature_c",
            "critical_temperature_c",
            "smart_health_status",
            "last_test_type",
            "last_test_status",
            "last_test_lifetime_hours",
            "last_test_age_hours",
            "power_on_hours",
            "power_on_days",
            "logical_block_size",
            "physical_block_size",
            "available_spare_percent",
            "available_spare_threshold_percent",
            "endurance_used_percent",
            "endurance_remaining_percent",
            "bytes_read",
            "bytes_written",
            "annualized_bytes_written",
            "estimated_lifetime_bytes_written",
            "estimated_remaining_bytes_written",
            "media_errors",
            "predictive_errors",
            "non_medium_errors",
            "uncorrected_read_errors",
            "uncorrected_write_errors",
            "unsafe_shutdowns",
            "rotation_rate_rpm",
            "form_factor",
            "firmware_version",
            "protocol_version",
            "namespace_eui64",
            "namespace_nguid",
            "read_cache_enabled",
            "writeback_cache_enabled",
            "trim_supported",
            "transport_protocol",
            "logical_unit_id",
            "sas_address",
            "attached_sas_address",
            "negotiated_link_rate",
        ):
            if getattr(primary, field_name) is None and getattr(supplement, field_name) is not None:
                setattr(primary, field_name, getattr(supplement, field_name))

        primary.available = primary.available or supplement.available
        if not primary.message and supplement.message:
            primary.message = supplement.message
        return primary

    @staticmethod
    def _status_contains(raw_status: dict[str, Any], *needles: str) -> bool:
        scalar_values: list[str] = []
        for value in raw_status.values():
            if value is None:
                continue
            if isinstance(value, dict):
                scalar_values.extend(str(item).lower() for item in value.values() if not isinstance(item, (dict, list, tuple, set)) and item is not None)
                continue
            if isinstance(value, (list, tuple, set)):
                scalar_values.extend(str(item).lower() for item in value if not isinstance(item, (dict, list, tuple, set)) and item is not None)
                continue
            scalar_values.append(str(value).lower())
        haystack = " ".join(scalar_values)
        return any(needle.lower() in haystack for needle in needles)

    @staticmethod
    def _health_is_bad(*values: str | None) -> bool:
        bad_keywords = ("fault", "degrad", "fail", "unavail", "offline", "removed", "critical")
        for value in values:
            if value and any(keyword in value.lower() for keyword in bad_keywords):
                return True
        return False
