from __future__ import annotations

import re
from collections import Counter
from typing import Any

from app.services.sas_diagnostics.common import (
    FAULT_FAMILY_LABELS,
    fault_family_likely_layer,
    fault_family_priority,
    fault_family_severity,
    severity_rank,
)
from app.services.sas_diagnostics.lsi_loginfo import decode_lsi_loginfo
from app.services.sas_diagnostics.scsi import (
    decode_scsi_cdb_message,
    decode_scsi_sense_event,
    decode_scsi_status_value,
)


DEFAULT_EVENT_TABLE_PAGE_SIZE = 25

FREEBSD_ERRNO_SOURCE = {
    "name": "FreeBSD intro(2) errno list",
    "url": "https://man.freebsd.org/cgi/man.cgi?apropos=0&manpath=freebsd&query=intro&sektion=2",
}

FREEBSD_ERRNO_LABELS = {
    5: ("EIO", "Input/output error"),
}


def new_mpr_event_summary() -> dict[str, Any]:
    return {
        "event_count": 0,
        "error_count": 0,
        "retry_count": 0,
        "sense_count": 0,
        "ioc_terminated_count": 0,
        "devices": set(),
        "targets": set(),
        "loginfo_counts": Counter(),
        "sense_counts": Counter(),
        "cam_status_counts": Counter(),
        "fault_family_counts": Counter(),
        "operation_counts": Counter(),
        "finding_records": {},
        "decoded_records": [],
        "recent_events": [],
    }


def decode_mpr_dmesg_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "")
    message = str(event.get("message") or "")
    if event_type == "cdb":
        return decode_scsi_cdb_message(message)
    if event_type == "scsi_sense":
        return decode_scsi_sense_event(event)
    if event_type == "cam_status":
        return _decode_cam_status_event(message)
    if event_type == "cam_error":
        return _decode_cam_error_event(message)
    if event_type == "scsi_status":
        return _decode_scsi_status_event(message)
    if event_type == "retry":
        return {
            "label": "Command retry scheduled",
            "family": "retry",
            "likely_layer": "OS retry path",
            "description": "The OS is retrying a command after transport or sense data indicated failure.",
        }
    if event_type == "ioc_terminated":
        loginfo = str(event.get("loginfo") or "").lower()
        decoded_loginfo = decode_lsi_loginfo(loginfo)
        if decoded_loginfo:
            return decoded_loginfo
        return {
            "label": "Controller terminated SCSI IO",
            "family": "controller_terminated_io",
            "likely_layer": "HBA path or downstream SAS fabric",
            "description": "The HBA reported an IO terminated before normal completion.",
            "loginfo": loginfo or None,
        }
    return {}


def make_decoded_event_record(event: dict[str, Any], *, event_id: str, sequence: int) -> dict[str, Any]:
    decoded = decode_mpr_dmesg_event(event)
    record: dict[str, Any] = {
        "id": event_id,
        "event_id": event_id,
        "sequence": sequence,
        "source": event.get("source"),
        "controller": event.get("controller"),
        "device": event.get("device"),
        "target": event.get("target"),
        "message": event.get("message"),
        "event_type": event.get("event_type"),
        "severity": event.get("severity") or "info",
        "raw_line": event.get("line"),
        "decoded": decoded,
    }
    for key in ("bus", "lun", "smid", "loginfo", "asc", "reason", "timestamp_raw"):
        if event.get(key) is not None:
            record[key] = event.get(key)
    for key in (
        "label",
        "family",
        "likely_layer",
        "description",
        "operation",
        "direction",
        "opcode",
        "service_action",
        "cdb_hex",
        "byte_count",
        "lba",
        "transfer_blocks",
        "allocation_length",
        "parameter_list_length",
        "sense_key",
        "sense_label",
        "asc_label",
        "log_page_code",
        "log_page",
        "log_page_control",
        "log_page_control_label",
        "log_save_parameters",
        "log_page_source",
        "log_subpage_code",
        "sas_phy_log_concepts",
        "vendor",
        "source_attribution",
        "decode_confidence",
        "decode_source",
        "decoder_note",
        "reported_operation",
        "cam_status",
        "scsi_status",
        "scsi_status_code",
        "cam_error_code",
        "cam_retry_state",
        "errno_name",
        "errno_label",
        "service_action_label",
    ):
        if decoded.get(key) is not None:
            record[key] = decoded.get(key)
    if not record.get("label"):
        record["label"] = event.get("reason") or event.get("message") or "Kernel event"
    if record.get("event_type") == "scsi_sense" and record.get("family"):
        record["severity"] = fault_family_severity(str(record["family"]))
    if not record.get("likely_layer") and record.get("family"):
        record["likely_layer"] = fault_family_likely_layer(str(record["family"]))
    fingerprint = _finding_fingerprint(record)
    if fingerprint:
        record["fingerprint"] = fingerprint
    return {key: value for key, value in record.items() if value is not None}


def record_mpr_event_summary(summary: dict[str, Any], event: dict[str, Any], record: dict[str, Any]) -> None:
    summary["event_count"] += 1
    if record.get("severity") == "error":
        summary["error_count"] += 1
    event_type = str(event.get("event_type") or "message")
    if event_type == "retry":
        summary["retry_count"] += 1
    if event_type == "scsi_sense":
        summary["sense_count"] += 1
        if event.get("reason"):
            summary["sense_counts"][event["reason"]] += 1
    if event_type == "ioc_terminated":
        summary["ioc_terminated_count"] += 1
    if event_type == "cam_status":
        cam_status = str(event.get("message") or "").split(":", 1)[-1].strip()
        if cam_status:
            summary["cam_status_counts"][cam_status] += 1
    if event.get("device"):
        summary["devices"].add(event["device"])
    if event.get("target"):
        summary["targets"].add(event["target"])
    if event.get("loginfo"):
        summary["loginfo_counts"][event["loginfo"]] += 1
    family = str(record.get("family") or "")
    if family:
        summary["fault_family_counts"][family] += 1
        _record_finding(summary, record)
    operation = str(record.get("operation") or "")
    if operation:
        summary["operation_counts"][operation] += 1
    compact_event = {
        key: value
        for key, value in event.items()
        if key
        in {
            "event_id",
            "controller",
            "device",
            "target",
            "message",
            "event_type",
            "severity",
            "loginfo",
            "asc",
            "reason",
            "timestamp_raw",
        }
        and value
    }
    summary["recent_events"].append(compact_event)
    summary["recent_events"] = summary["recent_events"][-12:]
    summary["decoded_records"].append(record)


def finalize_mpr_event_summary(summary: dict[str, Any]) -> dict[str, Any]:
    fault_family_counts = dict(summary["fault_family_counts"])
    top_findings = _top_mpr_findings(summary)
    primary_fault = top_findings[0] if top_findings else None
    decoded_records = list(summary["decoded_records"])
    return {
        "event_count": summary["event_count"],
        "error_count": summary["error_count"],
        "retry_count": summary["retry_count"],
        "sense_count": summary["sense_count"],
        "ioc_terminated_count": summary["ioc_terminated_count"],
        "devices": sorted(summary["devices"]),
        "targets": sorted(summary["targets"], key=lambda value: int(value) if str(value).isdigit() else str(value)),
        "loginfo_counts": dict(summary["loginfo_counts"]),
        "sense_counts": dict(summary["sense_counts"]),
        "cam_status_counts": dict(summary["cam_status_counts"]),
        "fault_family_counts": fault_family_counts,
        "operation_counts": dict(summary["operation_counts"]),
        "top_findings": top_findings,
        "primary_fault": primary_fault,
        "operator_summary": _mpr_operator_summary(summary, primary_fault),
        "recent_events": list(summary["recent_events"]),
        "decoded_records": decoded_records,
        "event_table": {
            "schema_version": 1,
            "total_count": len(decoded_records),
            "page_size": DEFAULT_EVENT_TABLE_PAGE_SIZE,
            "rows": decoded_records,
        },
    }


def _record_finding(summary: dict[str, Any], record: dict[str, Any]) -> None:
    fingerprint = record.get("fingerprint")
    if not fingerprint:
        return
    findings = summary["finding_records"]
    finding = findings.setdefault(
        fingerprint,
        {
            "fingerprint": fingerprint,
            "family": record.get("family"),
            "label": record.get("label") or FAULT_FAMILY_LABELS.get(str(record.get("family") or "")),
            "severity": record.get("severity") or fault_family_severity(str(record.get("family") or "")),
            "likely_layer": record.get("likely_layer"),
            "count": 0,
            "controllers": set(),
            "devices": set(),
            "targets": set(),
            "loginfo": record.get("loginfo"),
            "asc": record.get("asc"),
            "asc_label": record.get("asc_label"),
            "opcode": record.get("opcode"),
            "operation": record.get("operation"),
            "last_event_id": record.get("event_id"),
        },
    )
    finding["count"] += 1
    finding["last_event_id"] = record.get("event_id")
    for key in ("controllers", "devices", "targets"):
        singular = key[:-1]
        if record.get(singular):
            finding[key].add(record[singular])


def _decode_cam_status_event(message: str) -> dict[str, Any]:
    status = _message_value_after_colon(message)
    lowered = status.lower()
    if lowered == "ccb request completed with an error":
        label = "CAM completed command with an error"
        description = "The FreeBSD CAM layer saw the CCB complete with a failed command status."
    elif lowered == "scsi status error":
        label = "CAM reported SCSI status error"
        description = "The FreeBSD CAM layer reported that target SCSI status, not clean transport completion, drove the failure."
    else:
        label = f"CAM status: {status}" if status else "CAM transport status"
        description = "The FreeBSD CAM layer reported a non-clean command status."
    return {
        "label": label,
        "family": "cam_error",
        "likely_layer": "OS CAM transport layer",
        "description": description,
        "cam_status": status or None,
    }


def _decode_scsi_status_event(message: str) -> dict[str, Any]:
    status = _message_value_after_colon(message)
    return decode_scsi_status_value(status)


def _decode_cam_error_event(message: str) -> dict[str, Any]:
    match = re.search(r"Error\s+(?P<code>\d+)\s*,\s*(?P<state>.+)$", message, re.IGNORECASE)
    if not match:
        return {
            "label": f"CAM error: {message.strip()}" if message.strip() else "CAM error",
            "family": "cam_error",
            "likely_layer": "FreeBSD CAM transport layer",
            "description": "The FreeBSD CAM layer reported a command error.",
        }
    code = int(match.group("code"))
    state = match.group("state").strip()
    errno_name, errno_label = FREEBSD_ERRNO_LABELS.get(code, (None, None))
    label_head = f"CAM {errno_name}" if errno_name else f"CAM error {code}"
    decoded: dict[str, Any] = {
        "label": f"{label_head}: {state}",
        "family": "cam_error",
        "likely_layer": "FreeBSD CAM transport layer",
        "description": "The FreeBSD CAM layer reported an OS-level command failure for this disk/path.",
        "cam_error_code": code,
        "cam_retry_state": state,
        "decode_confidence": "observed",
        "decode_source": "freebsd_cam_errno",
        "source_attribution": dict(FREEBSD_ERRNO_SOURCE),
    }
    if errno_name:
        decoded["errno_name"] = errno_name
        decoded["errno_label"] = errno_label
        decoded["description"] = (
            f"The FreeBSD CAM layer reported {errno_name} ({errno_label}) and {state.lower()} for this disk/path."
        )
    else:
        decoded["decoder_note"] = "CAM reported a numeric error code that is not in the current local errno table."
    return decoded


def _message_value_after_colon(message: str) -> str:
    if ":" not in message:
        return message.strip()
    return message.split(":", 1)[1].strip()


def _finding_fingerprint(record: dict[str, Any]) -> str | None:
    family = record.get("family")
    if not family:
        return None
    parts = [
        str(family),
        str(record.get("label") or ""),
        str(record.get("loginfo") or ""),
        str(record.get("asc") or ""),
        str(record.get("opcode") or ""),
        str(record.get("service_action") or ""),
        str(record.get("operation") or ""),
        str(record.get("controller") or ""),
        str(record.get("device") or ""),
        str(record.get("target") or ""),
    ]
    return "|".join(part for part in parts if part)


def _top_mpr_findings(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for finding in summary["finding_records"].values():
        family = str(finding.get("family") or "")
        rows.append(
            {
                "fingerprint": finding["fingerprint"],
                "family": family,
                "label": finding.get("label") or FAULT_FAMILY_LABELS.get(family, family.replace("_", " ").title()),
                "count": finding.get("count", 0),
                "severity": finding.get("severity") or fault_family_severity(family),
                "likely_layer": finding.get("likely_layer") or fault_family_likely_layer(family),
                "affected": {
                    "controllers": sorted(finding["controllers"]),
                    "devices": sorted(finding["devices"]),
                    "targets": sorted(finding["targets"], key=lambda value: int(value) if str(value).isdigit() else str(value)),
                },
                "loginfo": finding.get("loginfo"),
                "asc": finding.get("asc"),
                "asc_label": finding.get("asc_label"),
                "opcode": finding.get("opcode"),
                "operation": finding.get("operation"),
                "last_event_id": finding.get("last_event_id"),
            }
        )
    rows.sort(
        key=lambda item: (
            severity_rank(item.get("severity")),
            -int(item.get("count") or 0),
            fault_family_priority(str(item.get("family") or "")),
            str(item.get("label") or ""),
        )
    )
    return rows[:6]


def _mpr_operator_summary(summary: dict[str, Any], primary_fault: dict[str, Any] | None) -> str | None:
    if not summary.get("event_count"):
        return None
    devices = sorted(summary["devices"])
    targets = sorted(summary["targets"], key=lambda value: int(value) if str(value).isdigit() else str(value))
    scope = ""
    if devices:
        scope = f" on {', '.join(devices[:4])}"
        if len(devices) > 4:
            scope += f" +{len(devices) - 4}"
    elif targets:
        scope = f" on target {', '.join(targets[:4])}"
        if len(targets) > 4:
            scope += f" +{len(targets) - 4}"
    if primary_fault:
        return f"{primary_fault['label']}{scope}: {summary['error_count']} errors, {summary['retry_count']} retries"
    return f"Kernel reported {summary['event_count']} MPR/CAM events{scope}"
