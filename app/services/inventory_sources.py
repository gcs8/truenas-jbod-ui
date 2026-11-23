"""Source collection and SSH command planning for ``InventoryService``.

``InventorySourceCollectionMixin`` holds the methods that summarize SSH
failures, plan the SSH, StorCLI and SMP counter commands of one refresh, handle
SES discovery admission and overlay merging, and collect one
``InventorySourceBundle`` from the platform API, SSH and the BMC.
``InventoryService`` inherits it, so these stay methods of the same instance
and class. Source TTLs, cache admission, publication, locks and generations
stay on ``InventoryService``.

This module does not import ``app.services.inventory``. That module binds
itself here with ``bind_facade``; the few names that stay defined there are
looked up on it at each use, so a replacement made on it still takes effect.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import ModuleType
from typing import Any, Iterable

from app.models.domain import SourceStatus
from app.perf import perf_stage
from app.services.core_expander_counters import (
    CAPABILITY_DENIED,
    CAPABILITY_GRANTED,
    SMP_CAPABILITY_RETRY_SECONDS,
    SMP_COUNTER_TIME_BUDGET_SECONDS,
    SMP_MAX_CALLS_PER_REFRESH,
    SMP_MAX_COMMANDS_PER_REFRESH,
    KeptCounterRead,
    bound_kept_reads,
    build_phy_error_log_command,
    build_phy_error_log_commands,
    chunk_output_span,
    chunk_stop,
    compact_counter_row,
    discover_smp_targets,
    parse_chunk_output,
    select_counter_reads,
    smp_output_calls,
    smp_output_key,
)
from app.services.parsers import (
    CORE_ZPOOL_STATUS_BY_DEVICE_OPTIONAL_COMMAND,
    ParsedSSHData,
    canonicalize_ssh_command,
    esxi_storcli_fallback_command,
    fixed_device_sg_ses_note,
    fixed_device_sg_ses_reads,
    normalize_text,
)
from app.services.sas_fabric import (
    CORE_DMIDECODE_SLOT_OPTIONAL_COMMAND,
    CORE_MPR_DMESG_EVENTS_COMMAND,
    CORE_MPR_SYSCTL_LOCATION_COMMAND,
    CORE_PCICONF_LV_OPTIONAL_COMMAND,
    build_core_mprutil_unit_commands,
)
from app.services.sas_fabric_parsers import MPR_REJECTED_CONTROLLER_REASON, admit_mpr_unit_number
from app.services.ssh_probe import (
    SSH_INTERNAL_ERROR_REASON,
    SSHCommandResult,
    SSHLocalFailure,
    ssh_failure_reason,
)
from app.services.supermicro_bmc import BMCInventory
from app.services.tls_context import certificate_verification_failure_reason
from app.services.truenas_ws import LocalServiceError, TrueNASAPIBusyError, TrueNASRawData, log_failure

# The same logger object as ``app.services.inventory.logger``.
logger = logging.getLogger("app.services.inventory")

SCALE_CONFIGURED_SG_SES_FAILURE_REGEX = re.compile(
    r"^SSH command failed \((?:SG SES additional element status|SG SES enclosure status); "
    r"[a-z_]+; exit \d+\)\.(?: .+)?$"
)
# Local failure categories that mean no SSH session reached the host.
SSH_LOGIN_FAILURE_REASONS = frozenset(
    {
        "auth_failed",
        "connection_refused",
        "destination_not_approved",
        "dns_failed",
        "host_key_rejected",
        "host_unreachable",
        "key_file_invalid",
        "key_file_missing",
        "key_file_unreadable",
        "protocol_error",
        "retry_backoff",
        "ssh_disabled",
        "ssh_transport_failed",
        "timeout",
    }
)
SSH_PARTIAL_INTERNAL_ERROR_STATUS = (
    f"SSH collection error ({SSH_INTERNAL_ERROR_REASON}). Some SSH reads are missing for this refresh."
)

LINUX_NVME_LIST_SUBSYS_COMMAND = (
    "/usr/sbin/nvme list-subsys -o json 2>/dev/null || "
    "/usr/bin/nvme list-subsys -o json 2>/dev/null || true"
)
# Reads the kernel enclosure-driver slot bindings without sudo. Every line is
# `<enclosure scsi id>|<sg name>|<slot attr>|<component name>|<block devices>`,
# which parse_enclosure_sysfs_map turns into per-bay device hints that keep
# working when AES pages cannot provide per-bay SAS addresses.
LINUX_ENCLOSURE_SYSFS_MAP_COMMAND = (
    "for c in /sys/class/enclosure/*/*; do "
    '[ -f "$c/slot" ] || continue; '
    'e="${c%/*}"; '
    "printf '%s|%s|%s|%s|%s\\n' "
    '"${e##*/}" '
    "\"$(ls \"$e/device/scsi_generic\" 2>/dev/null | tr '\\n' ' ')\" "
    "\"$(cat \"$c/slot\" 2>/dev/null)\" "
    '"${c##*/}" '
    "\"$(ls \"$c/device/block\" 2>/dev/null | tr '\\n' ' ')\"; "
    "done"
)

# What each SSH command is used for, reported beside a command failure in the
# debug output. `criticality` says what breaks without it: `topology` and
# `inventory` failures cost structure, `enrichment` failures only cost detail.
SSH_FAILURE_CONTEXTS_BY_COMMAND: dict[str, dict[str, str]] = {
    "dmesg mpr events": {
        "context": "sas_fabric_kernel_events",
        "context_label": "recent MPR/CAM event evidence",
        "criticality": "enrichment",
    },
    "pciconf -lv": {
        "context": "sas_fabric_pci_inventory",
        "context_label": "HBA PCI inventory",
        "criticality": "enrichment",
    },
    "dmidecode slot": {
        "context": "sas_fabric_pcie_slots",
        "context_label": "HBA slot labels",
        "criticality": "enrichment",
    },
    "mpr sysctl pci locations": {
        "context": "sas_fabric_pci_location",
        "context_label": "HBA PCI location corroboration",
        "criticality": "enrichment",
    },
    "lsscsi -g -t": {
        "context": "storage_fabric_linux_scsi_transport",
        "context_label": "Linux SCSI transport detail",
        "criticality": "enrichment",
    },
    "lsscsi -g": {
        "context": "storage_fabric_linux_scsi_sg",
        "context_label": "Linux SG device discovery",
        "criticality": "enrichment",
    },
    "lsblk -OJ": {
        "context": "linux_block_inventory",
        "context_label": "Linux block inventory",
        "criticality": "inventory",
    },
    "nvme list-subsys -o json": {
        "context": "storage_fabric_linux_nvme_subsystems",
        "context_label": "Linux NVMe subsystem detail",
        "criticality": "enrichment",
    },
    "enclosure sysfs map": {
        "context": "storage_fabric_linux_enclosure_map",
        "context_label": "Linux enclosure driver slot map",
        "criticality": "enrichment",
    },
    # Only fills the UniFi locate-light state; the bay map comes from lsblk.
    "gpio debug": {
        "context": "unifi_led_gpio_state",
        "context_label": "UniFi locate-light GPIO state",
        "criticality": "enrichment",
    },
    # Exact, so it wins over the "mprutil " prefix below.
    "mprutil show adapters": {
        "context": "sas_fabric_adapter_discovery",
        "context_label": "adapter discovery",
        "criticality": "topology",
    },
}

# Checked in order, after the exact commands and the mprutil unit pattern.
SSH_FAILURE_CONTEXTS_BY_PREFIX: tuple[tuple[str, dict[str, str]], ...] = (
    (
        # The output key of the CORE expander counter read (#941).
        "smp phy error log ",
        {
            "context": "sas_fabric_expander_phy_error_counters",
            "context_label": "expander PHY error counters",
            "criticality": "enrichment",
        },
    ),
    # These configured page reads remain required unless a successful SES
    # rediscovery supersedes their fixed, value-free failure messages below.
    (
        "sg_ses aes ",
        {
            "context": "storage_fabric_sg_ses_aes",
            "context_label": "SG SES additional element status",
            "criticality": "inventory",
        },
    ),
    (
        "sg_ses ec ",
        {
            "context": "storage_fabric_sg_ses_ec",
            "context_label": "SG SES enclosure status",
            "criticality": "inventory",
        },
    ),
    (
        "sg_ses join ",
        {
            "context": "storage_fabric_sg_ses_join",
            "context_label": "joined SG SES slot evidence",
            "criticality": "enrichment",
        },
    ),
    (
        "mprutil ",
        {
            "context": "sas_fabric_mprutil",
            "context_label": "mprutil topology evidence",
            "criticality": "topology",
        },
    ),
)

# Any unit token: the failure context names the controller only for a unit
# number the fabric admits (1 to 4 ASCII digits, `admit_mpr_unit_number`).
MPRUTIL_UNIT_COMMAND_PATTERN = re.compile(
    r"^mprutil -u (?P<unit>\S+) show (?P<subcommand>[a-z]+)$"
)

# The unnumbered form reads the one controller of a host that has no unit
# numbers. Its failure record names that controller by its fixed name, "mpr",
# so the fabric view still counts the controller as attempted and unread.
MPRUTIL_UNNUMBERED_READ_PATTERN = re.compile(
    r"mprutil show (?:adapter|all|devices|enclosures|expanders|iocfacts)"
)

MPRUTIL_SUBCOMMAND_LABELS: dict[str, str] = {
    "adapter": "controller adapter detail",
    "devices": "MPR device rows",
    "enclosures": "MPR enclosure rows",
    "expanders": "MPR expander rows",
    "iocfacts": "IOC facts",
    "all": "MPR all-command detail",
}

DEFAULT_SSH_FAILURE_CONTEXT: dict[str, str] = {
    "context": "ssh_command",
    "context_label": "SSH command",
    "criticality": "inventory",
}
SSH_COMMAND_FAILURE_REASON = "ssh_command_failed"


@dataclass(frozen=True, slots=True)
class SourcePublication:
    sequence: int = 0
    sg_cache_generation: int = 0


@dataclass(slots=True)
class InventorySourceBundle:
    raw_data: TrueNASRawData
    ssh_outputs: dict[str, str]
    ssh_collected: bool
    warnings: list[str]
    sources: dict[str, SourceStatus]
    scale_ses_data: ParsedSSHData
    quantastor_ses_data: ParsedSSHData
    ssh_failure_details: list[dict[str, Any]] = field(default_factory=list)
    bmc_inventory: BMCInventory | None = None
    parsed_ssh_data_by_enclosure: dict[str, ParsedSSHData] = field(default_factory=dict)
    publication: SourcePublication = field(default_factory=SourcePublication, repr=False, compare=False)

# --- Names that stay in app.services.inventory, looked up there at each use ---

_facade: ModuleType | None = None
# The host class, which ``_esxi_storcli_runtime_warning`` names literally.
# Nothing subclasses it, so the moved lines keep reading this one class.
InventoryService: Any = None


def bind_facade(module: ModuleType) -> None:
    """Called once at the end of ``app.services.inventory``, after ``InventoryService`` exists."""

    global _facade, InventoryService
    _facade = module
    InventoryService = module.InventoryService


def _drain_inventory_worker(worker: Any) -> Any:
    return _facade._drain_inventory_worker(worker)


def _certificate_verify_code_label(exc: BaseException) -> str | None:
    return _facade._certificate_verify_code_label(exc)


def _public_source_failure_message(label: str, exc: BaseException) -> str:
    return _facade._public_source_failure_message(label, exc)


class InventorySourceCollectionMixin:
    """Source collection and command planning, inherited by ``InventoryService``.

    What the methods read on the host instance or class and do not define here:

    - collaborators: ``system``, ``truenas_client``, ``ssh_probe``, ``bmc_service``;
    - methods: ``_collapse_quantastor_optional_ssh_backoff_warnings``,
      ``_fetch_quantastor_cli_overlay``, ``_fetch_quantastor_ses_overlay``,
      ``_fetch_scale_ses_overlay``, ``_quantastor_reported_host_warnings``,
      ``_scale_expected_ses_missing_failure``, ``_should_probe_unifi_gpio_debug``,
      and on the class ``_local_failure_sentence``;
    - state, initialized by the host's constructor: ``_esxi_storcli_swapped``,
      ``_smp_counter_capability``, ``_smp_counter_checked_at``,
      ``_smp_counter_last_read``, ``_smp_counter_reads``,
      ``_smp_counter_silent_probes``; and reset here by
      ``_reset_smp_counter_plan``: ``_smp_counter_capability_memo``,
      ``_smp_counter_planned``, ``_smp_counter_queue``,
      ``_smp_counter_started_at``, ``_smp_counter_stopped``.
    """

    @staticmethod
    def _admit_sg_ses_devices(values: Iterable[object]) -> tuple[list[str], int, int]:
        """SG device paths in the kernel's form, deduplicated, in numeric order.

        Returns the admitted devices (the lowest indexes, at most the limit),
        the number of values that are not an SG device path, and the number of
        devices beyond the limit. A value that is not admitted is counted,
        never kept or shown.
        """

        indexes: set[int] = set()
        rejected = 0
        for value in values:
            device = _facade.normalize_text(value) if isinstance(value, str) else None
            if not device:
                continue
            match = _facade.SG_SES_DEVICE_RE.fullmatch(device)
            if match is None:
                rejected += 1
                continue
            indexes.add(int(match.group(1)))
        ordered = sorted(indexes)
        limit = _facade.SG_SES_MAX_DISCOVERED_DEVICES
        return [f"/dev/sg{index}" for index in ordered[:limit]], rejected, max(0, len(ordered) - limit)

    @staticmethod
    def _split_sg_ses_discovery_output(stdout: str) -> tuple[list[str], int]:
        """Discovery lines without the first well-formed skipped-count line, and that count.

        A count line in any other form stays a line for admission, which
        counts it as not an SG device path.
        """

        lines: list[str] = []
        skipped: int | None = None
        for line in (stdout or "").splitlines():
            match = _facade.SG_SES_DISCOVERY_SKIPPED_RE.fullmatch(line.strip()) if skipped is None else None
            if match is not None:
                skipped = int(match.group(1))
                continue
            lines.append(line)
        return lines, skipped or 0

    @classmethod
    def _parse_sg_ses_discovery_devices(cls, stdout: str) -> list[str]:
        return cls._admit_sg_ses_devices(cls._split_sg_ses_discovery_output(stdout)[0])[0]

    @staticmethod
    def _build_sg_ses_discovery_command() -> str:
        # Only the elevated AES read needs sudo. Each node's peripheral type
        # and INQUIRY data are read from sysfs first, unprivileged. A node is
        # skipped only when both reads succeed and agree: the type is a storage
        # device; the INQUIRY data is at least the standard 36 bytes and as
        # long as its header says (additional length + 5); byte 0 equals the
        # type (peripheral qualifier 0, same type); and byte 6 has EncServ
        # clear. Anything short, failed or contradictory is read (#1314).
        # ``od -v`` prints every byte (no ``*`` for repeated lines), and the
        # split runs with globbing off.
        types = "|".join(_facade.SG_SES_NON_ENCLOSURE_TYPES)
        script = (
            "skipped=0; "
            "for dev in /dev/sg*; do "
            'id="/sys/class/scsi_generic/${dev##*/}/device"; '
            'if type=$(cat "$id/type" 2>/dev/null) && inq=$(od -An -tu1 -v -N260 "$id/inquiry" 2>/dev/null); then '
            f'case "$type" in {types}) '
            "set -f; set -- $inq; set +f; "
            'if [ $# -ge 36 ] && [ $# -ge $(($5 + 5)) ] && [ "$1" = "$type" ] && [ $(($7 & 64)) -eq 0 ]; then '
            "skipped=$((skipped + 1)); continue; fi;; "
            "esac; "
            "fi; "
            'if sudo -n /usr/bin/sg_ses -p aes "$dev" >/dev/null 2>&1; then echo "$dev"; fi; '
            "done; "
            'echo "skipped-not-enclosure $skipped"'
        )
        return _facade.shlex.join(["/bin/sh", "-lc", script])

    @staticmethod
    def _tag_ses_overlay(ssh_data: ParsedSSHData, host: str) -> None:
        tagged_host = _facade.normalize_text(host)
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
                payload["ses_targets"] = _facade._merge_control_targets(tagged_targets, [])

        for enclosure in ssh_data.ses_enclosures:
            enclosure.ssh_host = tagged_host
            for slot in enclosure.slots.values():
                tagged_targets: list[dict[str, Any]] = []
                for item in slot.control_targets:
                    if not isinstance(item, dict):
                        continue
                    tagged = dict(item)
                    tagged["ssh_host"] = tagged_host
                    tagged_targets.append(tagged)
                if tagged_targets:
                    slot.control_targets = _facade._merge_control_targets(tagged_targets, [])

    @staticmethod
    def _augment_ses_targets_from_redundant_hosts(
        authoritative: ParsedSSHData,
        overlays: list[ParsedSSHData],
    ) -> ParsedSSHData:
        authoritative_enclosures = list(authoritative.ses_enclosures)
        authoritative.ses_enclosures = _facade._merge_ses_enclosures(
            [
                *authoritative.ses_enclosures,
                *[
                    enclosure
                    for overlay in overlays
                    if overlay is not authoritative
                    for enclosure in overlay.ses_enclosures
                ],
            ]
        )
        for overlay in overlays:
            if overlay is authoritative:
                continue
            supplemental_candidates: dict[int, dict[str, Any]] = {}
            for slot, primary in authoritative.ses_slot_candidates.items():
                # A merged same-host enclosure can retain a slot on another SES path.
                primary_enclosure = next((enclosure for enclosure in authoritative_enclosures
                    if enclosure.enclosure_id == primary.get("enclosure_id")
                    and primary.get("ses_device") in [enclosure.ses_device, *enclosure.ses_devices]), None)
                if primary_enclosure is None:
                    continue
                for payload in overlay.ses_slot_candidates.values():
                    if not isinstance(payload, dict) or primary.get("ses_slot_number") is None:
                        continue
                    other_enclosure = next((enclosure for enclosure in overlay.ses_enclosures
                        if enclosure.enclosure_id == payload.get("enclosure_id")
                        and payload.get("ses_device") in [enclosure.ses_device, *enclosure.ses_devices]), None)
                    if (other_enclosure is None
                        or not _facade._ses_enclosures_share_identity(primary_enclosure, other_enclosure)
                        or primary.get("ses_slot_number") != payload.get("ses_slot_number")):
                        continue
                    supplemental_candidates[slot] = {
                        "ses_targets": payload.get("ses_targets", []),
                        "identify_active": payload.get("identify_active", False),
                    }
            if supplemental_candidates:
                authoritative.ses_slot_candidates = _facade.merge_slot_candidate_maps(
                    authoritative.ses_slot_candidates,
                    supplemental_candidates,
                )
        return authoritative

    @staticmethod
    def _merge_ses_overlay_data(base: ParsedSSHData, overlay: ParsedSSHData) -> ParsedSSHData:
        merged = _facade.ParsedSSHData(
            glabel=base.glabel,
            zpool_members=dict(base.zpool_members),
            zpool_device_members=dict(base.zpool_device_members),
            zpool_pool_states=dict(base.zpool_pool_states),
            multipath_info=dict(base.multipath_info),
            ses_slot_to_device=dict(base.ses_slot_to_device),
            camcontrol_models=dict(base.camcontrol_models),
            camcontrol_controllers=dict(base.camcontrol_controllers),
            camcontrol_peer_devices=dict(base.camcontrol_peer_devices),
            ses_slot_candidates=_facade.merge_slot_candidate_maps(base.ses_slot_candidates, overlay.ses_slot_candidates),
            ses_selected_meta=_facade.merge_enclosure_meta(base.ses_selected_meta, overlay.ses_selected_meta),
            ses_enclosures=_facade._merge_ses_enclosures([*base.ses_enclosures, *overlay.ses_enclosures]),
            linux_blockdevices=list(base.linux_blockdevices),
            linux_scsi_devices=list(base.linux_scsi_devices) + list(overlay.linux_scsi_devices),
            linux_mdadm_arrays=dict(base.linux_mdadm_arrays),
            linux_nvme_subsystems=dict(base.linux_nvme_subsystems),
            ubntstorage_disks=list(base.ubntstorage_disks),
            ubntstorage_spaces=list(base.ubntstorage_spaces),
            unifi_led_states=dict(base.unifi_led_states),
        )
        return merged

    @classmethod
    def _summarize_ssh_failures(
        cls,
        command_results: list[SSHCommandResult],
        outputs: dict[str, str],
    ) -> tuple[list[str], str]:
        failures = [item for item in command_results if not item.ok]
        if not failures:
            return [], "SSH probe completed."

        # When the SSH session fails before any command can start, Paramiko
        # produces the same synthetic startup failure for every configured
        # command. Collapse that fan-out into one user-facing warning.
        # Only a failure this process recorded itself can collapse; a remote
        # command's exit status, whatever it printed, stays per command.
        local_failures = [getattr(failure, "local_failure", None) for failure in failures]
        local_reasons = {
            local_failure.reason if isinstance(local_failure, SSHLocalFailure) else None
            for local_failure in local_failures
        }
        unique_exit_codes = {failure.exit_code for failure in failures}
        transport_level_failure = (
            not outputs
            and len(failures) == len(command_results)
            and len(local_reasons) == 1
            and None not in local_reasons
        )
        if transport_level_failure:
            reason = next(iter(local_reasons))
            if reason == SSH_INTERNAL_ERROR_REASON:
                return (
                    [
                        f"SSH collection stopped on an internal error ({reason}). "
                        "Bay details from SSH are missing for this refresh."
                    ],
                    f"SSH collection error ({reason}).",
                )
            if reason in SSH_LOGIN_FAILURE_REASONS:
                return (
                    [
                        f"Could not log in over SSH ({reason}).{cls._local_failure_sentence(reason)} "
                        "Bay details from SSH are missing for this refresh."
                    ],
                    f"SSH login failed ({reason}).",
                )

        normalized_outputs = {
            canonicalize_ssh_command(command): output
            for command, output in outputs.items()
        }
        failed_canonical_commands = [canonicalize_ssh_command(failure.command) for failure in failures]
        storcli_command_failures = [
            command
            for command in failed_canonical_commands
            if command.startswith("storcli ")
        ]
        storcli_commands_missing = (
            storcli_command_failures
            and len(storcli_command_failures) == len(failures)
            and unique_exit_codes == {127}
        )
        if storcli_commands_missing:
            vib_output = (normalized_outputs.get("esxcli software vib list") or "").lower()
            broadcom_stack_detected = any(
                token in vib_output
                for token in (
                    "lsi-mr3",
                    "megaraid",
                    "lsuv2-lsiv2-drivers-plugin",
                    "avago_bootbank_lsi-mr3",
                    "bcm_bootbank_vmware-storcli64",
                )
            )
            if broadcom_stack_detected:
                warning = (
                    "StorCLI is not installed on this ESXi host, so drive details behind the RAID "
                    "controller are missing. The Broadcom driver packages alone (lsi-mr3, "
                    "lsuv2-lsiv2-drivers-plugin) do not provide them; install the Broadcom StorCLI VIB."
                )
            else:
                warning = (
                    "StorCLI is not available on this ESXi host, so drive details behind the RAID "
                    "controller are missing for this refresh."
                )
            esxi_major = re.search(r"\bESXi (\d+)\.", normalized_outputs.get("vmware -v") or "")
            if esxi_major and int(esxi_major.group(1)) >= 8:
                warning = (
                    f"{warning} ESXi 8 needs Broadcom's ESXi 8 StorCLI package and a host reboot "
                    "after installing it; the app then reads it through `esxcli storcli`."
                )
            return ([warning], "StorCLI commands unavailable.")

        failure_details = cls._ssh_failure_details(command_results)
        enrichment_failures = [
            detail
            for detail in failure_details
            if detail.get("criticality") == "enrichment"
        ]
        if outputs and enrichment_failures and len(enrichment_failures) == len(failures):

            def contexts_text(details: list[dict[str, Any]]) -> str:
                affected_contexts = sorted(
                    {str(detail.get("context_label") or "Storage Fabric enrichment") for detail in details}
                )
                text = ", ".join(affected_contexts[:3])
                if len(affected_contexts) > 3:
                    text = f"{text}, +{len(affected_contexts) - 3} more"
                return text

            internal_details = [
                detail for detail in enrichment_failures if detail.get("reason") == SSH_INTERNAL_ERROR_REASON
            ]
            other_details = [
                detail for detail in enrichment_failures if detail.get("reason") != SSH_INTERNAL_ERROR_REASON
            ]
            if internal_details:
                internal_text = contexts_text(internal_details)
                optional_warnings = (
                    [
                        f"Some SSH commands failed ({contexts_text(other_details)}). The bay map still works; "
                        "see Debug for failure context."
                    ]
                    if other_details
                    else []
                )
                optional_warnings.append(
                    "SSH collection stopped on an internal error in optional reads "
                    f"({internal_text}; {SSH_INTERNAL_ERROR_REASON}). The bay map still works; "
                    "the service log has the details."
                )
                return (
                    optional_warnings,
                    f"SSH collection error in optional reads ({internal_text}; {SSH_INTERNAL_ERROR_REASON}). "
                    "The bay map is complete.",
                )
            context_text = contexts_text(enrichment_failures)
            return (
                [
                    f"Some SSH commands failed ({context_text}). The bay map still works; "
                    "see Debug for failure context."
                ],
                # The SSH chip's tooltip names the failed optional read (#1066).
                f"Some optional SSH reads failed ({context_text}). The bay map is complete.",
            )

        warnings: list[str] = []
        internal_error_seen = False
        for detail in failure_details:
            context_label = detail.get("context_label", "SSH command")
            exit_status = detail.get("exit_code")
            if exit_status is None:
                exit_status = "unknown"
            reason = detail.get("reason", SSH_COMMAND_FAILURE_REASON)
            if reason == SSH_INTERNAL_ERROR_REASON:
                internal_error_seen = True
                warnings.append(
                    f"SSH collection stopped on an internal error ({context_label}; {reason}). "
                    "Bay details from that read are missing for this refresh."
                )
                continue
            warnings.append(
                f"SSH command failed ({context_label}; {reason}; exit {exit_status}).{cls._local_failure_sentence(reason)}"
            )
        return (warnings, SSH_PARTIAL_INTERNAL_ERROR_STATUS if internal_error_seen else "Some SSH commands failed.")

    @classmethod
    def _ssh_failure_details(cls, command_results: list[SSHCommandResult]) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        for position, result in enumerate(command_results, start=1):
            if result.ok:
                continue
            canonical = canonicalize_ssh_command(result.command)
            context = cls._ssh_command_failure_context(canonical)
            if context.get("context") == DEFAULT_SSH_FAILURE_CONTEXT["context"]:
                context = {
                    **context,
                    "context": f"ssh_command_{position}",
                    "context_label": f"SSH command {position}",
                }
            details.append(
                {
                    "reason": ssh_failure_reason(result),
                    "exit_code": result.exit_code,
                    **context,
                }
            )
        return details

    @staticmethod
    def _ssh_command_failure_context(canonical_command: str) -> dict[str, str]:
        """Describe a failed command with fixed public failure context.

        `criticality` says what breaks without it: `topology` and `inventory`
        failures cost structure, `enrichment` failures only cost detail.
        """
        exact = SSH_FAILURE_CONTEXTS_BY_COMMAND.get(canonical_command)
        if exact is not None:
            return dict(exact)

        unit_match = MPRUTIL_UNIT_COMMAND_PATTERN.match(canonical_command)
        if unit_match:
            subcommand = unit_match.group("subcommand")
            unit = unit_match.group("unit")
            # A unit the fabric does not admit is a fixed reason, never a
            # copy of the token: it may be any length or script.
            controller = (
                {"controller": f"mpr{unit}"}
                if admit_mpr_unit_number(unit) is not None
                else {"controller_rejected": MPR_REJECTED_CONTROLLER_REASON}
            )
            return {
                "context": f"sas_fabric_mprutil_{subcommand}",
                "context_label": MPRUTIL_SUBCOMMAND_LABELS.get(
                    subcommand, f"mprutil {subcommand}"
                ),
                **controller,
                "criticality": "enrichment",
            }

        for prefix, context in SSH_FAILURE_CONTEXTS_BY_PREFIX:
            if canonical_command.startswith(prefix):
                if MPRUTIL_UNNUMBERED_READ_PATTERN.fullmatch(canonical_command):
                    return {**context, "controller": "mpr"}
                return dict(context)

        return dict(DEFAULT_SSH_FAILURE_CONTEXT)

    @staticmethod
    def _is_configured_sg_ses_page_probe_failure(message: str) -> bool:
        return bool(SCALE_CONFIGURED_SG_SES_FAILURE_REGEX.match(normalize_text(message) or ""))

    @classmethod
    def _suppress_scale_configured_sg_ses_failures(
        cls,
        warnings: list[str],
        ssh_failures: list[str],
    ) -> tuple[list[str], list[str]]:
        if not ssh_failures:
            return warnings, ssh_failures

        remaining_failures: list[str] = []
        suppressed_failures: list[str] = []
        for failure in ssh_failures:
            if cls._is_configured_sg_ses_page_probe_failure(failure):
                suppressed_failures.append(failure)
            else:
                remaining_failures.append(failure)

        if not suppressed_failures:
            return warnings, ssh_failures

        remaining_warnings = list(warnings)
        for failure in suppressed_failures:
            try:
                remaining_warnings.remove(failure)
            except ValueError:
                pass
        return remaining_warnings, remaining_failures

    @staticmethod
    def _esxi_storcli_runtime_warning(outputs: dict[str, str]) -> str | None:
        normalized_outputs = {
            canonicalize_ssh_command(command): output
            for command, output in outputs.items()
        }
        storcli_outputs = [
            output
            for command, output in normalized_outputs.items()
            if command.startswith("storcli ")
        ]
        if not storcli_outputs:
            return None

        storcli_text = "\n".join(storcli_outputs).lower()
        no_controller_visible = any(
            needle in storcli_text
            for needle in (
                '"number of controllers" : 0',
                "number of controllers = 0",
                "controller 0 not found",
                "no controller found",
            )
        )
        if not no_controller_visible:
            return None

        passthrough_enabled = InventoryService._extract_enabled_passthrough_addresses(
            normalized_outputs.get("esxcli hardware pci pcipassthru list") or ""
        )
        megaraid_pci_addresses = InventoryService._extract_megaraid_pci_addresses(
            normalized_outputs.get("lspci") or ""
        )
        megaraid_passthrough = [
            address
            for address in megaraid_pci_addresses
            if address in passthrough_enabled
        ]
        if megaraid_passthrough:
            address_list = ", ".join(megaraid_passthrough)
            return (
                "StorCLI is installed on this ESXi host, but the Broadcom MegaRAID controller is "
                f"currently configured for PCI passthrough ({address_list}). ESXi will not bind that "
                "device to lsi_mr3 or expose it to StorCLI until passthrough is disabled and the host "
                "is rebooted, so drive details behind the RAID controller are missing for this refresh."
            )

        vib_output = (normalized_outputs.get("esxcli software vib list") or "").lower()
        broadcom_stack_detected = any(
            token in vib_output
            for token in (
                "lsi-mr3",
                "megaraid",
                "lsuv2-lsiv2-drivers-plugin",
                "avago_bootbank_lsi-mr3",
                "bcm_bootbank_vmware-storcli64",
                "vmware-storcli64",
            )
        )
        if broadcom_stack_detected:
            return (
                "StorCLI is installed on this ESXi host, but it currently reports no visible "
                "MegaRAID controllers. Broadcom/LSI packages such as lsi-mr3, lsuv2-lsiv2-drivers-plugin, "
                "and vmware-storcli64 are present, but no compatible controller is being surfaced to StorCLI, "
                "so drive details behind the RAID controller are missing for this refresh."
            )
        return (
            "StorCLI is installed on this ESXi host, but it currently reports no visible storage "
            "controllers, so drive details behind the RAID controller are missing for this refresh."
        )

    @staticmethod
    def _extract_enabled_passthrough_addresses(output: str) -> list[str]:
        addresses: list[str] = []
        for raw_line in output.splitlines():
            match = re.search(
                r"(?im)^\s*([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])\s+true\b",
                raw_line,
            )
            if not match:
                continue
            address = match.group(1).lower()
            if address not in addresses:
                addresses.append(address)
        return addresses

    @staticmethod
    def _extract_megaraid_pci_addresses(output: str) -> list[str]:
        addresses: list[str] = []
        for raw_line in output.splitlines():
            if "megaraid" not in raw_line.lower():
                continue
            match = re.search(r"(?im)^\s*([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])\b", raw_line)
            if not match:
                continue
            address = match.group(1).lower()
            if address not in addresses:
                addresses.append(address)
        return addresses

    def _linux_storage_enrichment_probe_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        if self.system.truenas.platform not in {"scale", "linux"}:
            return []
        seen_commands = {canonicalize_ssh_command(item.command) for item in command_results}
        commands: list[str] = []
        if "lsblk -OJ" not in seen_commands:
            commands.append(
                "/usr/bin/lsblk --json --bytes --output "
                "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,HCTL,PKNAME,MOUNTPOINTS,FSTYPE,UUID,PARTUUID,LOG-SEC,PHY-SEC"
            )
        if "lsscsi -g -t" not in seen_commands:
            commands.append("/usr/bin/lsscsi -g -t")
        if "nvme list-subsys -o json" not in seen_commands:
            commands.append(LINUX_NVME_LIST_SUBSYS_COMMAND)
        if "enclosure sysfs map" not in seen_commands:
            commands.append(LINUX_ENCLOSURE_SYSFS_MAP_COMMAND)
        return commands

    def _ssh_inventory_enrichment_probe_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        seed_commands = self._core_mprutil_seed_probe_commands(command_results)
        if seed_commands:
            return seed_commands

        unit_commands = self._core_mprutil_unit_probe_commands(command_results)
        if unit_commands:
            return unit_commands

        commands: list[str] = []
        commands.extend(self._core_zpool_by_device_probe_commands(command_results))
        commands.extend(self._core_mpr_dmesg_probe_commands(command_results))
        commands.extend(self._core_pci_slot_probe_commands(command_results))
        commands.extend(self._core_expander_counter_probe_commands(command_results))
        if self._should_probe_unifi_gpio_debug(command_results):
            commands.append("cat /sys/kernel/debug/gpio")
        commands.extend(self._esxi_controller_diagnostic_commands(command_results))
        if any(result.ok for result in command_results):
            commands.extend(self._linux_storage_enrichment_probe_commands(command_results))
        # A fallback shares its parser key with the read it replaces, so it skips the canonical dedupe.
        return self._esxi_storcli_fallback_commands(command_results) + self._dedupe_unseen_ssh_commands(
            commands, command_results
        )

    @staticmethod
    def _storcli_answered(result: SSHCommandResult) -> bool:
        # ESXi 8 prints XML for a direct StorCLI call; only JSON is an answer.
        return result.ok and result.stdout.lstrip().startswith("{")

    def _esxi_storcli_initial_commands(self) -> list[str]:
        commands = list(self.system.ssh.commands)
        if not self._esxi_storcli_swapped:
            return commands
        return [esxi_storcli_fallback_command(command) or command for command in commands]

    def _esxi_storcli_fallback_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        """Retry each unanswered StorCLI read in the other ESXi form (binary <-> `esxcli storcli`)."""
        if self.system.truenas.platform != "esxi":
            return []
        vib_list = next(
            (result.stdout for result in command_results
             if result.ok and canonicalize_ssh_command(result.command) == "esxcli software vib list"),
            None,
        )
        if vib_list is not None and "storcli" not in vib_list.lower():
            return []  # No StorCLI package, so neither form can answer.
        attempts = Counter(canonicalize_ssh_command(result.command) for result in command_results)
        return [
            fallback
            for result in command_results
            if not self._storcli_answered(result)
            and attempts[canonicalize_ssh_command(result.command)] == 1
            and (fallback := esxi_storcli_fallback_command(result.command))
        ]

    def _resolve_esxi_storcli_forms(self, command_results: list[SSHCommandResult]) -> list[SSHCommandResult]:
        """Keep one result per StorCLI read: the form that answered, else a failed form so the refresh
        still warns (ESXi 8 before its reboot: the binary prints XML, the plugin is missing), else the first.

        The answering form is remembered, so the next refresh tries it first.
        """
        reads: dict[str, list[SSHCommandResult]] = {}
        for result in command_results:
            if esxi_storcli_fallback_command(result.command):
                reads.setdefault(canonicalize_ssh_command(result.command), []).append(result)
        dropped: set[int] = set()
        for attempts in reads.values():
            if len(attempts) < 2:
                continue
            keep = next((result for result in attempts if self._storcli_answered(result)), None) or next(
                (result for result in attempts if not result.ok), attempts[0]
            )
            dropped.update(id(result) for result in attempts if result is not keep)
            if self._storcli_answered(keep):
                self._esxi_storcli_swapped = keep.command not in self.system.ssh.commands
        return [result for result in command_results if id(result) not in dropped]

    @staticmethod
    def _dedupe_unseen_ssh_commands(
        commands: Iterable[str],
        command_results: list[SSHCommandResult],
    ) -> list[str]:
        seen_canonical = {canonicalize_ssh_command(item.command) for item in command_results}
        selected: list[str] = []
        selected_canonical: set[str] = set()
        for command in commands:
            canonical = canonicalize_ssh_command(command)
            if canonical in seen_canonical or canonical in selected_canonical:
                continue
            selected.append(command)
            selected_canonical.add(canonical)
        return selected

    def _esxi_controller_diagnostic_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        if self.system.truenas.platform != "esxi":
            return []
        outputs = {item.command: item.stdout for item in command_results if item.ok}
        if not self._esxi_storcli_runtime_warning(outputs):
            return []
        seen_commands = {canonicalize_ssh_command(item.command) for item in command_results}
        missing_commands: list[str] = []
        if "esxcli hardware pci pcipassthru list" not in seen_commands:
            missing_commands.append("esxcli hardware pci pcipassthru list 2>&1 || true")
        if "lspci" not in seen_commands:
            missing_commands.append("lspci 2>&1 || true")
        return missing_commands

    def _core_mprutil_seed_probe_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        if self.system.truenas.platform != "core":
            return []
        seen_commands = {canonicalize_ssh_command(item.command) for item in command_results}
        if "mprutil show adapters" in seen_commands:
            return []
        return ["sudo -n /usr/sbin/mprutil show adapters"]

    def _core_mprutil_unit_probe_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        if self.system.truenas.platform != "core":
            return []
        outputs = {
            canonicalize_ssh_command(item.command): item.stdout
            for item in command_results
            if item.ok
        }
        adapter_summary = outputs.get("mprutil show adapters")
        if not adapter_summary:
            return []
        seen_commands = {canonicalize_ssh_command(item.command) for item in command_results}
        return build_core_mprutil_unit_commands(adapter_summary, seen_commands)

    @staticmethod
    def _smp_counter_clock() -> float:
        return time.monotonic()

    @staticmethod
    def _smp_counter_wall_clock() -> datetime:
        return datetime.now(timezone.utc)

    def _reset_smp_counter_plan(self) -> None:
        """Forget the full reads planned for the current collection.

        ``_smp_counter_planned`` maps each chunk command sent this collection
        (by output key) to its expander, in the order sent;
        ``_smp_counter_queue`` holds each expander's chunks not sent yet, and
        ``_smp_counter_stopped`` the expanders whose read ended early (reason,
        PHY).
        """
        self._smp_counter_planned: dict[str, tuple[int, str]] = {}
        self._smp_counter_queue: dict[tuple[int, str], list[str]] = {}
        self._smp_counter_stopped: dict[tuple[int, str], tuple[str, int]] = {}
        self._smp_counter_capability_memo: dict[tuple[str, int, int], str | None] = {}
        self._smp_counter_started_at: float | None = None

    def _record_smp_counter_reads(self, command_results: list[SSHCommandResult]) -> None:
        """Keep each full read this collection ran, as decoded rows with a read time.

        Called once after a collection. Only chunks the planner sent for full
        reads count, so a one-PHY permission probe never replaces a kept full
        reading. Raw output is never kept: each chunk is validated against the
        key it was asked for, and an oversized or malformed one keeps only
        ``rejected_output`` rows for its PHYs (#1088 security review). A new
        read replaces an expander's kept rows with the chunks that completed;
        a chunk that failed (for example past the SSH command deadline) leaves
        its PHYs unread rather than mixing in an older reading, and a read
        that ended early records where and why. A read with no completed chunk
        keeps the previous rows. The kept readings stay within
        SMP_MAX_RETAINED_EXPANDERS and SMP_MAX_RETAINED_PHYS, least recently
        read dropped first.
        """
        planned = dict(getattr(self, "_smp_counter_planned", None) or {})
        stopped = dict(getattr(self, "_smp_counter_stopped", None) or {})
        self._reset_smp_counter_plan()
        if not planned:
            return
        read_at = self._smp_counter_wall_clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        by_key = {
            key: item for item in command_results if (key := smp_output_key(item.command)) in planned
        }
        chunks: dict[tuple[int, str], list[str]] = {}
        for key, expander in planned.items():
            chunks.setdefault(expander, []).append(key)
        kept = dict(getattr(self, "_smp_counter_reads", None) or {})
        for expander, keys in chunks.items():
            span = chunk_output_span(keys[0])
            if span is None:
                continue
            device = span[0].split(":")[2]
            stop = stopped.get(expander)
            rows: dict[int, Any] = {}
            completed = False
            for key in keys:
                item = by_key.get(key)
                if item is None:
                    continue
                if stop is None:
                    stop = chunk_stop(key, item.ok, item.stdout, timed_out=ssh_failure_reason(item) == "timeout")
                if not item.ok:
                    continue
                parsed = parse_chunk_output(key, item.stdout)
                compact = [compact_counter_row(row) for row in parsed.rows] if parsed is not None else [None]
                if parsed is None or None in compact:
                    parsed = parse_chunk_output(key, None)
                    compact = [compact_counter_row(row) for row in parsed.rows]
                for row in compact:
                    rows.setdefault(row[0], row)
                completed = True
            marker = (stop[0], stop[1], read_at) if stop else None
            previous = kept.get(expander)
            if completed:
                kept[expander] = KeptCounterRead(device, read_at, tuple(rows[phy] for phy in sorted(rows)), marker)
            elif previous is not None and previous.device == device:
                kept[expander] = KeptCounterRead(previous.device, previous.read_at, previous.rows, marker)
            elif marker is not None:
                kept[expander] = KeptCounterRead(device, None, (), marker)
        self._smp_counter_reads = bound_kept_reads(kept, getattr(self, "_smp_counter_last_read", None) or {})

    def expander_counter_reads(self) -> dict[tuple[int, str], KeptCounterRead]:
        """Kept readings by (unit, expander SAS address) for the fabric view."""
        return dict(getattr(self, "_smp_counter_reads", None) or {})

    def _record_smp_counter_capability(self, command_results: list[SSHCommandResult]) -> None:
        """Remember whether sudo ran the expander counter read in these results."""
        # Planning runs again after every round of commands; each output is
        # parsed for its capability once per collection.
        memo = getattr(self, "_smp_counter_capability_memo", None)
        if memo is None or len(memo) > 4 * SMP_MAX_COMMANDS_PER_REFRESH:
            memo = self._smp_counter_capability_memo = {}
        outcomes: set[str | None] = set()
        for item in command_results:
            key = smp_output_key(item.command)
            if not item.ok or key is None:
                continue
            memo_key = (key, len(item.stdout or ""), hash(item.stdout or ""))
            if memo_key not in memo:
                parsed = parse_chunk_output(key, item.stdout)
                memo[memo_key] = parsed.capability if parsed is not None else None
            outcomes.add(memo[memo_key])
        if CAPABILITY_GRANTED in outcomes:
            state = CAPABILITY_GRANTED
        elif CAPABILITY_DENIED in outcomes:
            state = CAPABILITY_DENIED
        else:
            return
        self._smp_counter_capability = state
        self._smp_counter_checked_at = self._smp_counter_clock()
        self._smp_counter_silent_probes = set()

    def retry_expander_counter_capability(self) -> None:
        """Ask sudo again on the next collection after a remembered refusal.

        A forced source refresh (the Refresh button, ``?force=true``) calls this,
        so an operator who just added the grant through the admin bootstrap
        does not wait out SMP_CAPABILITY_RETRY_SECONDS. The admin sidecar runs
        in another process and cannot reach this cache directly. Background
        and stale-cache refreshes do not call it.
        """
        if self._smp_counter_capability == CAPABILITY_DENIED:
            self._smp_counter_checked_at = float("-inf")

    def _smp_counter_read_commands(self, targets: list[Any], command_results: list[SSHCommandResult]) -> list[str]:
        """Full reads for the expanders due this refresh (#1088 review).

        Each expander is read at most once per SMP_COUNTER_READ_INTERVAL_SECONDS,
        forced refresh or not; the interval starts when the read is planned, so
        a read that fails or times out still counts. One refresh reads at most
        SMP_MAX_TARGETS_PER_REFRESH expanders, SMP_MAX_CALLS_PER_REFRESH PHY
        calls and SMP_MAX_COMMANDS_PER_REFRESH SSH commands, the probe
        included; each expander is read in chunks of SMP_PHYS_PER_COMMAND PHYs,
        one command each. Only each expander's first chunk is sent now; the
        rest follow one per planning round (_smp_counter_next_chunks()), so
        the first chunk that does not answer ends that expander's read. The
        probe's output alone does not count, since the full reads follow it
        in that refresh.
        """
        keys = {smp_output_key(item.command) for item in command_results} - {None}
        # The capability probe reads PHY 0 of one expander ("...:00-00").
        if {key for key in keys if not key.endswith(":00-00")}:
            return []
        now = self._smp_counter_clock()
        current = {(target.unit, target.expander_sas_address) for target in targets}
        # Expired read times still order the rotation (select_counter_reads
        # applies the interval); only departed expanders are dropped.
        last_read = {
            expander: planned_at
            for expander, planned_at in (getattr(self, "_smp_counter_last_read", None) or {}).items()
            if expander in current
        }
        self._smp_counter_reads = {
            expander: read
            for expander, read in (getattr(self, "_smp_counter_reads", None) or {}).items()
            if expander in current
        }
        self._reset_smp_counter_plan()
        self._smp_counter_started_at = now
        budget = SMP_MAX_CALLS_PER_REFRESH - sum(smp_output_calls(key) for key in keys)
        command_budget = SMP_MAX_COMMANDS_PER_REFRESH - len(keys)
        commands: list[str] = []
        for target in select_counter_reads(targets, last_read, now, budget, command_budget):
            expander = (target.unit, target.expander_sas_address)
            last_read[expander] = now
            first, *rest = build_phy_error_log_commands(target)
            self._smp_counter_planned[smp_output_key(first)] = expander
            self._smp_counter_queue[expander] = rest
            commands.append(first)
        self._smp_counter_last_read = last_read
        return commands

    def _smp_counter_next_chunks(self, command_results: list[SSHCommandResult]) -> list[str]:
        """The next chunk of each expander still being read this refresh.

        An expander's read ends at the first chunk that did not answer (a call
        with no answer, or past the SSH command deadline), whose output was
        rejected, that failed, or that sudo refused; its remaining chunks are
        not sent this refresh, so a hung expander costs about one call
        timeout. The read interval still applies to it. Once
        SMP_COUNTER_TIME_BUDGET_SECONDS have passed since the full reads
        started, no further chunk is sent (``time_budget``), so slow but
        answering expanders cannot push the collection past its deadline.
        """
        planned = self._smp_counter_planned
        started = getattr(self, "_smp_counter_started_at", None)
        if started is not None and self._smp_counter_clock() - started >= SMP_COUNTER_TIME_BUDGET_SECONDS:
            for expander, queue in self._smp_counter_queue.items():
                span = chunk_output_span(smp_output_key(queue[0])) if queue else None
                if span is not None and expander not in self._smp_counter_stopped:
                    self._smp_counter_stopped[expander] = ("time_budget", span[1][0])
                queue.clear()
            return []
        by_key = {
            key: item for item in command_results if (key := smp_output_key(item.command)) in planned
        }
        last_sent: dict[tuple[int, str], str] = {}
        for key, expander in planned.items():
            last_sent[expander] = key
        commands: list[str] = []
        for expander, queue in self._smp_counter_queue.items():
            if not queue or expander in self._smp_counter_stopped:
                continue
            item = by_key.get(last_sent.get(expander, ""))
            if item is None:
                continue
            stop = chunk_stop(
                last_sent[expander], item.ok, item.stdout, timed_out=ssh_failure_reason(item) == "timeout"
            )
            if stop is not None:
                self._smp_counter_stopped[expander] = stop
                queue.clear()
                continue
            command = queue.pop(0)
            planned[smp_output_key(command)] = expander
            commands.append(command)
        return commands

    def _core_expander_counter_probe_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        """Read expander PHY error counters when the account may (#941).

        Targets come from the mprutil and camcontrol reads already in this
        refresh. An unknown or long-ago refused capability is checked with one
        sudo call (PHY 0 of the first expander, or of the next one after a
        probe that got no answer) before any full read; a recent refusal is
        not retried unless a forced refresh asked for it. A full read of a
        granted capability renews it.
        """
        if self.system.truenas.platform != "core":
            return []
        planned = getattr(self, "_smp_counter_planned", None) or {}
        if planned and any(smp_output_key(item.command) in planned for item in command_results):
            # A later planning round of this collection's full reads.
            self._record_smp_counter_capability(command_results)
            if self._smp_counter_capability != CAPABILITY_GRANTED:
                return []
            return self._smp_counter_next_chunks(command_results)
        outputs = {
            canonicalize_ssh_command(item.command): item.stdout
            for item in command_results
            if item.ok
        }
        targets = discover_smp_targets(outputs)
        if not targets:
            return []
        self._record_smp_counter_capability(command_results)
        state = self._smp_counter_capability
        if state == CAPABILITY_GRANTED:
            return self._smp_counter_read_commands(targets, command_results)
        recent = self._smp_counter_clock() - self._smp_counter_checked_at < SMP_CAPABILITY_RETRY_SECONDS
        if state == CAPABILITY_DENIED and recent:
            return []
        current = {(target.unit, target.expander_sas_address) for target in targets}
        silent = {expander for expander in (getattr(self, "_smp_counter_silent_probes", None) or set()) if expander in current}
        probed = [key for item in command_results if (key := smp_output_key(item.command))]
        if probed:
            # The probe ran this refresh but proved nothing (no answer): the
            # next refresh probes another expander, so one silent expander
            # cannot keep the capability unknown (#1088 review).
            for key in probed:
                span = chunk_output_span(key)
                if span is not None:
                    unit, address, _device = span[0].split(":")
                    silent.add((int(unit), address))
            self._smp_counter_silent_probes = silent
            return []
        target = next((item for item in targets if (item.unit, item.expander_sas_address) not in silent), None)
        if target is None:
            # Every expander was silent once: start the turn again.
            silent, target = set(), targets[0]
        self._smp_counter_silent_probes = silent
        return [build_phy_error_log_command(target, [0])]

    def _core_zpool_by_device_probe_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        """Read pool members by device for a saved CORE command list that predates it (#948)."""
        if self.system.truenas.platform != "core":
            return []
        seen_commands = {canonicalize_ssh_command(item.command) for item in command_results}
        if "zpool status -LP" in seen_commands:
            return []
        return [CORE_ZPOOL_STATUS_BY_DEVICE_OPTIONAL_COMMAND]

    def _core_mpr_dmesg_probe_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        if self.system.truenas.platform != "core":
            return []
        has_messages_probe = any(
            canonicalize_ssh_command(item.command) == "dmesg mpr events"
            and "/var/log/messages" in item.command.lower()
            for item in command_results
        )
        if has_messages_probe:
            return []
        return [CORE_MPR_DMESG_EVENTS_COMMAND]

    def _core_pci_slot_probe_commands(self, command_results: list[SSHCommandResult]) -> list[str]:
        if self.system.truenas.platform != "core":
            return []
        seen_commands = {canonicalize_ssh_command(item.command) for item in command_results}
        commands: list[str] = []
        if "pciconf -lv" not in seen_commands:
            commands.append(CORE_PCICONF_LV_OPTIONAL_COMMAND)
        if "dmidecode slot" not in seen_commands:
            commands.append(CORE_DMIDECODE_SLOT_OPTIONAL_COMMAND)
        if "mpr sysctl pci locations" not in seen_commands:
            commands.append(CORE_MPR_SYSCTL_LOCATION_COMMAND)
        return commands

    async def _collect_inventory_source_bundle(self) -> InventorySourceBundle:
        warnings: list[str] = []
        api_enabled = self.system.truenas.platform not in {"linux", "esxi", "ipmi"}
        bmc_enabled = bool(self.system.bmc.enabled and self.bmc_service is not None)
        api_label = (
            "Quantastor API"
            if self.system.truenas.platform == "quantastor"
            else "ESXi API"
            if self.system.truenas.platform == "esxi"
            else "BMC / IPMI"
            if self.system.truenas.platform == "ipmi"
            else "TrueNAS API"
        )
        # #1066: say how disks are read where no platform API is used, rather
        # than naming an API (TrueNAS) the system does not have.
        unused_api_message = (
            "The ESXi API is not used; disks are read over SSH."
            if self.system.truenas.platform == "esxi"
            else "BMC-only systems have no platform API; disks are read from the BMC."
            if self.system.truenas.platform == "ipmi"
            else "Linux systems have no platform API; disks are read over SSH."
        )
        sources = {
            "api": SourceStatus(
                enabled=api_enabled,
                ok=not api_enabled,
                message=unused_api_message if not api_enabled else None,
            ),
            "ssh": SourceStatus(enabled=self.system.ssh.enabled, ok=not self.system.ssh.enabled, message=None),
            "bmc": SourceStatus(
                enabled=bmc_enabled,
                ok=not bmc_enabled,
                message=(
                    "BMC/IPMI is not configured for this system."
                    if not bmc_enabled
                    else None
                ),
            ),
        }

        raw_data = TrueNASRawData(
            enclosures=[],
            disks=[],
            pools=[],
            disk_temperatures={},
            smart_test_results=[],
        )
        ssh_outputs: dict[str, str] = {}
        ssh_collected = False
        ssh_failures: list[str] = []
        ssh_failure_details: list[dict[str, Any]] = []
        quantastor_cli_loaded = False
        quantastor_cli_failures: list[str] = []
        scale_ses_loaded = False
        scale_ses_data = ParsedSSHData()
        scale_ses_failures: list[str] = []
        quantastor_ses_loaded = False
        quantastor_ses_data = ParsedSSHData()
        quantastor_ses_failures: list[str] = []
        bmc_inventory: BMCInventory | None = None

        async def load_api_data() -> TrueNASRawData | None:
            if not api_enabled:
                return None
            with perf_stage("inventory.api.fetch_all", platform=self.system.truenas.platform):
                return await self.truenas_client.fetch_all()

        skipped_sg_ses_reads = fixed_device_sg_ses_reads(self.system.truenas.platform, list(self.system.ssh.commands))

        async def load_ssh_payload() -> tuple[dict[str, str], bool, list[str], list[dict[str, Any]], SourceStatus]:
            # A collection that ended early (cancelled) leaves no plan behind.
            self._reset_smp_counter_plan()
            with perf_stage("inventory.ssh.run_commands"):
                ssh_started = time.perf_counter()
                if self.system.truenas.platform == "esxi":
                    command_results = self._resolve_esxi_storcli_forms(
                        await self.ssh_probe.run_planned_commands(
                            self._ssh_inventory_enrichment_probe_commands,
                            initial_commands=self._esxi_storcli_initial_commands(),
                        )
                    )
                elif skipped_sg_ses_reads:
                    # Device discovery reads every enclosure; a saved /dev/sgN
                    # read can point at a disk or CD-ROM after a renumbering.
                    command_results = await self.ssh_probe.run_planned_commands(
                        self._ssh_inventory_enrichment_probe_commands,
                        initial_commands=[
                            command for command in self.system.ssh.commands
                            if command not in skipped_sg_ses_reads
                        ],
                    )
                else:
                    command_results = await self.ssh_probe.run_planned_commands(
                        self._ssh_inventory_enrichment_probe_commands
                    )
                logger.info(
                    "Inventory SSH refresh completed for system=%s platform=%s profile=%s: commands=%s failures=%s duration=%.3fs",
                    self.system.id,
                    self.system.truenas.platform,
                    self.system.default_profile_id or "auto",
                    len(command_results),
                    sum(1 for result in command_results if not result.ok),
                    time.perf_counter() - ssh_started,
                )
            self._record_smp_counter_reads(command_results)
            outputs = {item.command: item.stdout for item in command_results if item.ok}
            failure_messages, status_message = self._summarize_ssh_failures(command_results, outputs)
            failure_details = self._ssh_failure_details(command_results)
            # The bay map survives when only `enrichment` commands failed and
            # something answered; history records on this, not on `ok` (#927).
            required_ok = not failure_details or (
                bool(outputs) and all(detail.get("criticality") == "enrichment" for detail in failure_details)
            )
            return (
                outputs,
                True,
                failure_messages,
                failure_details,
                SourceStatus(
                    enabled=True,
                    ok=not failure_messages,
                    message=status_message,
                    required_ok=required_ok,
                ),
            )

        async def load_bmc_inventory() -> BMCInventory:
            if self.bmc_service is None:
                raise LocalServiceError("BMC / IPMI access is not configured for this system.")
            with perf_stage("inventory.bmc.fetch"):
                # Retain the executor Future: cancelling an asyncio wrapper
                # cannot stop this synchronous collector.
                worker = asyncio.get_running_loop().run_in_executor(None, copy_context().run, self.bmc_service.fetch_inventory)
                return await _drain_inventory_worker(worker)

        api_task = asyncio.create_task(load_api_data()) if api_enabled else None
        ssh_task = asyncio.create_task(load_ssh_payload()) if self.system.ssh.enabled else None
        bmc_task = asyncio.create_task(load_bmc_inventory()) if bmc_enabled else None

        tasks = {task for task in (api_task, ssh_task, bmc_task) if task is not None}
        pending = tasks.copy()
        cancelled = False
        while pending:
            try:
                _done, pending = await asyncio.wait(pending)
            except asyncio.CancelledError:
                if not cancelled:
                    cancelled = True
                    # Signal each sibling once. Repeated caller cancellation
                    # must not interrupt SSH/BMC's retained worker drainage.
                    for task in pending:
                        task.cancel()
        # Observe every settled outcome before awaiting individual sources.
        # A source can itself be cancelled without cancelling this collector;
        # propagating it must not abandon a later sibling's completed failure.
        for task in tasks:
            if not task.cancelled():
                task.exception()
        if cancelled:
            raise asyncio.CancelledError

        if api_task is not None:
            try:
                api_result = await api_task
            except Exception as exc:
                log_failure(logger, "Platform API collection", exc, level=logging.ERROR)
                certificate_failure = certificate_verification_failure_reason(exc)
                if certificate_failure is not None:
                    # #954: keep the failure actionable without publishing the
                    # verifier's free-form text. Reviewed verify codes recover
                    # stable categories such as expiry and name mismatch.
                    certificate_label = _certificate_verify_code_label(exc)
                    certificate_detail = f" ({certificate_label})" if certificate_label else ""
                    certificate_message = f"{api_label} certificate verification failed{certificate_detail}."
                    sources["api"] = SourceStatus(enabled=True, ok=False, message=certificate_message)
                    warnings.append(
                        f"{certificate_message} "
                        "Slot details may be partial or unavailable."
                    )
                elif isinstance(exc, TrueNASAPIBusyError):
                    # #1072: the appliance answered but is out of capacity, or
                    # the app is backing off after it was. Not "unreachable".
                    sources["api"] = SourceStatus(
                        enabled=True,
                        ok=False,
                        message=f"{api_label} is busy; collection will retry.",
                    )
                    warnings.append(
                        f"{api_label} is busy; the app is backing off before asking again. "
                        "Slot details may be partial until it answers."
                    )
                else:
                    # The status shows this service's own sentence or a fixed
                    # category; the log line above carries the structured facts.
                    public_message = _public_source_failure_message(api_label, exc)
                    sources["api"] = SourceStatus(
                        enabled=True,
                        ok=False,
                        message=public_message,
                    )
                    warnings.append(
                        f"{public_message} "
                        "Slot details may be partial or unavailable."
                    )
            else:
                if api_result is not None:
                    raw_data = api_result
                if raw_data.enclosure_query_failed:
                    sources["api"] = SourceStatus(
                        enabled=True,
                        ok=False,
                        message=f"{api_label} reachable with degraded enclosure data.",
                    )
                    warnings.append(
                        f"{api_label} could not list enclosures this time. Disks, pools, temperatures, and SMART data are current."
                    )
                else:
                    sources["api"] = SourceStatus(enabled=True, ok=True, message=f"{api_label} reachable.")

        if ssh_task is not None:
            try:
                ssh_outputs, ssh_collected, ssh_failures, ssh_failure_details, ssh_status = await ssh_task
            except Exception as exc:
                log_failure(logger, "SSH diagnostics collection", exc, level=logging.ERROR)
                sources["ssh"] = SourceStatus(
                    enabled=True,
                    ok=False,
                    message=_public_source_failure_message("SSH diagnostics", exc),
                    required_ok=False,
                )
                warnings.append("SSH is turned on but no command output came back.")
            else:
                sources["ssh"] = ssh_status
                warnings.extend(ssh_failures)
                if skipped_sg_ses_reads:
                    warnings.append(fixed_device_sg_ses_note(len(skipped_sg_ses_reads)))

        if bmc_task is not None:
            try:
                bmc_inventory = await bmc_task
            except Exception as exc:
                log_failure(logger, "BMC diagnostics collection", exc, level=logging.ERROR)
                sources["bmc"] = SourceStatus(
                    enabled=True,
                    ok=False,
                    message=_public_source_failure_message("BMC / IPMI diagnostics", exc),
                )
                warnings.append("BMC/IPMI is turned on but the BMC could not be read.")
            else:
                sources["bmc"] = SourceStatus(enabled=True, ok=True, message="BMC / IPMI inventory reachable.")
                warnings.extend(bmc_inventory.warnings)

        if self.system.truenas.platform == "scale" and ssh_collected:
            try:
                with perf_stage("inventory.scale.fetch_ses_overlay"):
                    scale_ses_data, scale_ses_failures = await self._fetch_scale_ses_overlay()
            except Exception as exc:  # noqa: BLE001 - the API data still stands.
                log_failure(logger, "SCALE SES diagnostics collection", exc, level=logging.ERROR)
                scale_ses_failures.append(
                    "TrueNAS SCALE SSH SES rediscovery failed unexpectedly. API disk and pool data is still being used."
                )
            else:
                scale_ses_loaded = bool(scale_ses_data.ses_enclosures)
                if not scale_ses_loaded and not scale_ses_failures and raw_data.enclosures:
                    # SES was expected: the TrueNAS API reports enclosures but
                    # SSH found none. Absence without that evidence stays quiet.
                    scale_ses_failures.append(self._scale_expected_ses_missing_failure())
            warnings.extend(scale_ses_failures)

            if scale_ses_loaded:
                warnings, ssh_failures = self._suppress_scale_configured_sg_ses_failures(warnings, ssh_failures)
            if scale_ses_loaded or scale_ses_failures:
                sources["ssh"] = SourceStatus(
                    enabled=True,
                    ok=not ssh_failures and not scale_ses_failures,
                    message=(
                        "SSH probe and SCALE SES rediscovery completed."
                        if not ssh_failures and not scale_ses_failures
                        else "SCALE SES rediscovery completed with some failures."
                    ),
                )

        if self.system.truenas.platform == "quantastor" and ssh_collected:
            try:
                with perf_stage("inventory.quantastor.fetch_cli_overlay"):
                    quantastor_cli_overlay, quantastor_cli_failures = await self._fetch_quantastor_cli_overlay(raw_data)
            except Exception as exc:  # noqa: BLE001 - the REST data still stands.
                log_failure(logger, "Quantastor CLI diagnostics collection", exc, level=logging.ERROR)
                quantastor_cli_failures.append(
                    "Quantastor SSH CLI enrichment failed unexpectedly. REST data is still being used."
                )
            else:
                raw_data.cli_disks = quantastor_cli_overlay.get("cli_disks", [])
                raw_data.cli_hw_disks = quantastor_cli_overlay.get("cli_hw_disks", [])
                raw_data.cli_hw_enclosures = quantastor_cli_overlay.get("cli_hw_enclosures", [])
                raw_data.cli_network_ports = quantastor_cli_overlay.get("cli_network_ports", [])
                quantastor_cli_loaded = any(
                    (
                        raw_data.cli_disks,
                        raw_data.cli_hw_disks,
                        raw_data.cli_hw_enclosures,
                        raw_data.cli_network_ports,
                    )
                )
            warnings.extend(quantastor_cli_failures)

            try:
                with perf_stage("inventory.quantastor.fetch_ses_overlay"):
                    quantastor_ses_data, quantastor_ses_failures = await self._fetch_quantastor_ses_overlay(raw_data)
            except Exception as exc:  # noqa: BLE001 - the REST and CLI data still stand.
                log_failure(logger, "Quantastor SES diagnostics collection", exc, level=logging.ERROR)
                quantastor_ses_failures.append(
                    "Quantastor SSH SES enrichment failed unexpectedly. Available REST and CLI data is still being used."
                )
            else:
                quantastor_ses_loaded = bool(quantastor_ses_data.ses_enclosures)
            # A setup failure such as a missing node address reaches both overlays; say it once.
            warnings.extend(failure for failure in quantastor_ses_failures if failure not in quantastor_cli_failures)
            # Reported node addresses that were not used are shown as counts, never echoed.
            warnings.extend(self._quantastor_reported_host_warnings(raw_data))

            # Failed attempts matter even when they contributed no overlay rows.
            if quantastor_cli_failures or quantastor_ses_failures:
                sources["ssh"] = SourceStatus(
                    enabled=True,
                    ok=False,
                    message="Quantastor CLI/SES enrichment completed with some failures.",
                )
            elif quantastor_cli_loaded:
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

        return InventorySourceBundle(
            raw_data=raw_data,
            ssh_outputs=ssh_outputs,
            ssh_collected=ssh_collected,
            warnings=self._collapse_quantastor_optional_ssh_backoff_warnings(warnings),
            ssh_failure_details=ssh_failure_details,
            sources=sources,
            scale_ses_data=scale_ses_data,
            quantastor_ses_data=quantastor_ses_data,
            bmc_inventory=bmc_inventory,
        )
