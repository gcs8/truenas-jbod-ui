from __future__ import annotations


FAULT_FAMILY_LABELS = {
    "sas_protocol": "SAS protocol error",
    "link_loss": "SAS link loss",
    "timeout": "SAS timeout",
    "bus_reset": "SCSI bus reset",
    "controller_terminated_io": "Controller terminated IO",
    "controller_configuration": "Controller configuration error",
    "sas_transport": "SAS transport error",
    "device_path_exception": "Device/path exception",
    "ses_enclosure": "SES/enclosure management error",
    "logical_unit_communication": "Logical unit communication error",
    "target_failure": "Target/device failure",
    "protection_error": "Block protection error",
    "data_buffer_error": "Data buffer transfer error",
    "pcie_fabric": "PCIe fabric error",
    "cam_error": "CAM transport error",
    "scsi_status": "SCSI status",
    "retry": "Command retry",
    "write_error": "Write error",
    "read_error": "Read error",
    "write_io": "Write IO affected",
    "read_io": "Read IO affected",
    "capacity_query": "Capacity query",
    "log_sense": "LOG SENSE diagnostic page",
    "maintenance": "SCSI maintenance command",
    "aborted_command": "Aborted command",
    "scsi_sense": "SCSI sense event",
    "scsi_command": "SCSI command",
}


def fault_family_priority(family: str) -> int:
    priorities = {
        "link_loss": 0,
        "sas_protocol": 1,
        "timeout": 2,
        "sas_transport": 3,
        "controller_terminated_io": 4,
        "device_path_exception": 5,
        "ses_enclosure": 6,
        "logical_unit_communication": 7,
        "target_failure": 8,
        "pcie_fabric": 9,
        "data_buffer_error": 10,
        "controller_configuration": 11,
        "cam_error": 12,
        "bus_reset": 13,
        "aborted_command": 14,
        "protection_error": 15,
        "scsi_sense": 16,
        "scsi_status": 17,
        "retry": 18,
        "write_error": 19,
        "read_error": 20,
        "write_io": 30,
        "read_io": 31,
        "log_sense": 32,
        "capacity_query": 33,
        "maintenance": 34,
        "scsi_command": 35,
    }
    return priorities.get(family, 20)


def fault_family_severity(family: str) -> str:
    if family in {
        "sas_protocol",
        "link_loss",
        "timeout",
        "controller_terminated_io",
        "controller_configuration",
        "sas_transport",
        "ses_enclosure",
        "logical_unit_communication",
        "target_failure",
        "pcie_fabric",
        "data_buffer_error",
        "write_error",
        "read_error",
    }:
        return "error"
    if family in {
        "cam_error",
        "bus_reset",
        "scsi_status",
        "retry",
        "aborted_command",
        "device_path_exception",
        "protection_error",
    }:
        return "warning"
    return "info"


def severity_rank(severity: str | None) -> int:
    return {"error": 0, "warning": 1, "info": 2}.get(str(severity or "info").lower(), 3)


def fault_family_likely_layer(family: str) -> str:
    if family in {"sas_protocol", "link_loss", "timeout", "sas_transport"}:
        return "SAS link/path"
    if family == "controller_terminated_io":
        return "HBA path or downstream fabric"
    if family == "controller_configuration":
        return "HBA firmware/configuration"
    if family == "ses_enclosure":
        return "SES/enclosure management path"
    if family == "logical_unit_communication":
        return "Target communication path"
    if family == "target_failure":
        return "SCSI target/device"
    if family == "pcie_fabric":
        return "Host PCIe fabric or endpoint"
    if family == "data_buffer_error":
        return "SCSI data buffer transfer"
    if family == "protection_error":
        return "Block protection metadata"
    if family in {"write_error", "read_error"}:
        return "Target media or command data path"
    if family == "bus_reset":
        return "SCSI/SAS recovery"
    if family in {"write_io", "read_io"}:
        return "Workload IO context"
    if family == "cam_error":
        return "FreeBSD CAM transport layer"
    if family == "scsi_status":
        return "SCSI target status"
    if family == "log_sense":
        return "SCSI diagnostic log page"
    return "SCSI/SAS fabric"
