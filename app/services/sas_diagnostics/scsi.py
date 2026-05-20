from __future__ import annotations

import re
from typing import Any

from app.services.sas_diagnostics.common import fault_family_likely_layer


T10_OPCODE_SOURCE = {
    "name": "INCITS T10 SCSI Operation Codes numeric list",
    "url": "https://www.t10.org/lists/op-num.htm",
}

T10_ASC_ASCQ_SOURCE = {
    "name": "INCITS T10 SCSI ASC/ASCQ numeric list",
    "url": "https://www.t10.org/lists/asc-num.htm",
}

T10_LOG_SENSE_SOURCE = {
    "name": "INCITS T10 SPC/SBC LOG SENSE page assignments",
    "url": "https://www.t10.org/lists/1spc-lst.htm",
}

T10_STATUS_SOURCE = {
    "name": "INCITS T10 SCSI status codes list",
    "url": "https://www.t10.org/lists/2status.htm",
}

KERNEL_MESSAGE_SOURCE = {
    "name": "FreeBSD CAM/MPR kernel message",
    "url": None,
}


# Generic SCSI/SBC/SPC command names. Keep this table standards-shaped and
# source-scoped; product-specific interpretation belongs outside this module.
SCSI_CDB_OPCODE_NAMES = {
    0x00: "TEST UNIT READY",
    0x03: "REQUEST SENSE",
    0x04: "FORMAT UNIT",
    0x07: "REASSIGN BLOCKS",
    0x08: "READ(6)",
    0x0A: "WRITE(6)",
    0x12: "INQUIRY",
    0x15: "MODE SELECT(6)",
    0x1A: "MODE SENSE(6)",
    0x1B: "START STOP UNIT",
    0x1C: "RECEIVE DIAGNOSTIC RESULTS",
    0x1D: "SEND DIAGNOSTIC",
    0x1E: "PREVENT ALLOW MEDIUM REMOVAL",
    0x23: "READ FORMAT CAPACITIES",
    0x25: "READ CAPACITY(10)",
    0x28: "READ(10)",
    0x2A: "WRITE(10)",
    0x2B: "SEEK(10)",
    0x2E: "WRITE AND VERIFY(10)",
    0x2F: "VERIFY(10)",
    0x34: "PRE-FETCH(10)",
    0x35: "SYNCHRONIZE CACHE(10)",
    0x36: "LOCK UNLOCK CACHE(10)",
    0x37: "READ DEFECT DATA(10)",
    0x38: "MEDIUM SCAN",
    0x3B: "WRITE BUFFER",
    0x3C: "READ BUFFER",
    0x3E: "READ LONG(10)",
    0x3F: "WRITE LONG(10)",
    0x41: "WRITE SAME(10)",
    0x42: "UNMAP",
    0x48: "SANITIZE",
    0x4C: "LOG SELECT",
    0x4D: "LOG SENSE",
    0x55: "MODE SELECT(10)",
    0x56: "RESERVE(10)",
    0x57: "RELEASE(10)",
    0x5A: "MODE SENSE(10)",
    0x5E: "PERSISTENT RESERVE IN",
    0x5F: "PERSISTENT RESERVE OUT",
    0x7E: "EXTENDED CDB",
    0x7F: "VARIABLE LENGTH CDB",
    0x83: "THIRD-PARTY COPY OUT",
    0x84: "THIRD-PARTY COPY IN",
    0x85: "ATA PASS-THROUGH(16)",
    0x88: "READ(16)",
    0x89: "COMPARE AND WRITE",
    0x8A: "WRITE(16)",
    0x8B: "ORWRITE",
    0x8C: "READ ATTRIBUTE",
    0x8D: "WRITE ATTRIBUTE",
    0x8E: "WRITE AND VERIFY(16)",
    0x8F: "VERIFY(16)",
    0x90: "PRE-FETCH(16)",
    0x91: "SYNCHRONIZE CACHE(16)",
    0x93: "WRITE SAME(16)",
    0x94: "ZBC OUT",
    0x95: "ZBC IN",
    0x9A: "WRITE STREAM(16)",
    0x9B: "READ BUFFER(16)",
    0x9C: "WRITE ATOMIC(16)",
    0x9D: "SERVICE ACTION BIDIRECTIONAL",
    0x9E: "SERVICE ACTION IN(16)",
    0x9F: "SERVICE ACTION OUT(16)",
    0xA0: "REPORT LUNS",
    0xA1: "ATA PASS-THROUGH(12)",
    0xA2: "SECURITY PROTOCOL IN",
    0xA3: "MAINTENANCE IN",
    0xA4: "MAINTENANCE OUT",
    0xA5: "MOVE MEDIUM",
    0xA7: "MOVE MEDIUM ATTACHED",
    0xA8: "READ(12)",
    0xA9: "SERVICE ACTION OUT(12)",
    0xAA: "WRITE(12)",
    0xAB: "SERVICE ACTION IN(12)",
    0xAE: "WRITE AND VERIFY(12)",
    0xAF: "VERIFY(12)",
    0xB5: "SECURITY PROTOCOL OUT",
    0xB6: "SEND VOLUME TAG",
    0xB7: "READ DEFECT DATA(12)",
    0xB8: "READ ELEMENT STATUS",
    0xBA: "REDUNDANCY GROUP (IN)",
    0xBB: "REDUNDANCY GROUP (OUT)",
    0xBC: "SPARE (IN)",
    0xBD: "SPARE (OUT)",
    0xBE: "VOLUME SET (IN)",
    0xBF: "VOLUME SET (OUT)",
}

SCSI_SERVICE_ACTION_NAMES = {
    (0x5E, 0x00): "READ KEYS",
    (0x5E, 0x01): "READ RESERVATION",
    (0x5E, 0x02): "REPORT CAPABILITIES",
    (0x5E, 0x03): "READ FULL STATUS",
    (0x5F, 0x00): "REGISTER",
    (0x5F, 0x01): "RESERVE",
    (0x5F, 0x02): "RELEASE",
    (0x5F, 0x03): "CLEAR",
    (0x5F, 0x04): "PREEMPT",
    (0x5F, 0x05): "PREEMPT AND ABORT",
    (0x5F, 0x06): "REGISTER AND IGNORE EXISTING KEY",
    (0x5F, 0x07): "REGISTER AND MOVE",
    (0x84, 0x00): "COPY STATUS",
    (0x84, 0x01): "RECEIVE DATA",
    (0x84, 0x03): "OPERATING PARAMETERS",
    (0x84, 0x04): "FAILED SEGMENT DETAILS",
    (0x9E, 0x10): "READ CAPACITY(16)",
    (0x9E, 0x11): "READ LONG(16)",
    (0x9E, 0x12): "GET LBA STATUS",
    (0x9F, 0x11): "WRITE LONG(16)",
    (0xA3, 0x05): "REPORT DEVICE IDENTIFIER",
    (0xA3, 0x0A): "REPORT TARGET PORT GROUPS",
    (0xA3, 0x0B): "REPORT ALIASES",
    (0xA3, 0x0C): "REPORT SUPPORTED OPERATION CODES",
    (0xA3, 0x0D): "REPORT SUPPORTED TASK MANAGEMENT FUNCTIONS",
    (0xA3, 0x0E): "REPORT PRIORITY",
    (0xA3, 0x0F): "REPORT TIMESTAMP",
    (0xA4, 0x06): "SET DEVICE IDENTIFIER",
    (0xA4, 0x0A): "SET TARGET PORT GROUPS",
    (0xA4, 0x0B): "CHANGE ALIASES",
    (0xA4, 0x0E): "SET PRIORITY",
    (0xA4, 0x0F): "SET TIMESTAMP",
}

SCSI_SERVICE_ACTION_OPCODES = {opcode for opcode, _ in SCSI_SERVICE_ACTION_NAMES} | {
    0x9D,
    0x9E,
    0x9F,
    0xA3,
    0xA4,
    0xA9,
    0xAB,
}

SCSI_SENSE_KEY_LABELS = {
    "NO SENSE": "No specific sense key",
    "RECOVERED ERROR": "Recovered target error",
    "NOT READY": "Device not ready",
    "MEDIUM ERROR": "Disk media error",
    "HARDWARE ERROR": "Hardware reported an error",
    "ILLEGAL REQUEST": "Unsupported or invalid command",
    "UNIT ATTENTION": "Device state changed",
    "DATA PROTECT": "Data protection prevented the command",
    "BLANK CHECK": "Blank or unwritten medium",
    "COPY ABORTED": "Copy operation aborted",
    "ABORTED COMMAND": "Command aborted below the OS",
    "VOLUME OVERFLOW": "Volume overflow",
    "MISCOMPARE": "Compare operation mismatch",
}

SCSI_ASC_ASCQ_LABELS = {
    (0x00, 0x00): "No additional sense information",
    (0x00, 0x06): "I/O process terminated",
    (0x00, 0x16): "Operation in progress",
    (0x00, 0x1D): "ATA pass through information available",
    (0x03, 0x00): "Peripheral device write fault",
    (0x03, 0x01): "No write current",
    (0x03, 0x02): "Excessive write errors",
    (0x04, 0x00): "Logical unit not ready",
    (0x04, 0x01): "Logical unit is becoming ready",
    (0x04, 0x02): "Logical unit not ready, initializing command required",
    (0x04, 0x03): "Logical unit not ready, manual intervention required",
    (0x04, 0x05): "Logical unit not ready, rebuild in progress",
    (0x04, 0x06): "Logical unit not ready, recalculation in progress",
    (0x04, 0x07): "Logical unit not ready, operation in progress",
    (0x04, 0x09): "Logical unit not ready, self-test in progress",
    (0x04, 0x0A): "Logical unit not accessible, asymmetric access state transition",
    (0x04, 0x0B): "Logical unit not accessible, target port in standby state",
    (0x04, 0x0C): "Logical unit not accessible, target port in unavailable state",
    (0x04, 0x1A): "Logical unit not ready, START STOP UNIT command in progress",
    (0x04, 0x1B): "Logical unit not ready, sanitize in progress",
    (0x04, 0x20): "Logical unit not ready, logical unit reset required",
    (0x04, 0x21): "Logical unit not ready, hard reset required",
    (0x04, 0x22): "Logical unit not ready, power cycle required",
    (0x05, 0x00): "Logical unit does not respond to selection",
    (0x08, 0x00): "Logical unit communication failure",
    (0x08, 0x01): "Logical unit communication timeout",
    (0x08, 0x02): "Logical unit communication parity error",
    (0x08, 0x03): "Logical unit communication CRC error",
    (0x08, 0x04): "Unreachable copy target",
    (0x09, 0x00): "Track following error",
    (0x09, 0x01): "Tracking servo failure",
    (0x09, 0x02): "Focus servo failure",
    (0x09, 0x03): "Spindle servo failure",
    (0x09, 0x04): "Head select fault",
    (0x0B, 0x00): "Warning",
    (0x0B, 0x01): "Warning - specified temperature exceeded",
    (0x0B, 0x02): "Warning - enclosure degraded",
    (0x0B, 0x03): "Warning - background self-test failed",
    (0x0B, 0x04): "Warning - background pre-scan detected medium error",
    (0x0B, 0x05): "Warning - background medium scan detected medium error",
    (0x0B, 0x06): "Warning - non-volatile cache now volatile",
    (0x0C, 0x00): "Write error",
    (0x0C, 0x01): "Write error, recovered with auto reallocation",
    (0x0C, 0x02): "Write error, auto reallocation failed",
    (0x0C, 0x03): "Write error, recommend reassignment",
    (0x0C, 0x07): "Write error, recovery needed",
    (0x0C, 0x08): "Write error, recovery failed",
    (0x0C, 0x0C): "Write error, unexpected unsolicited data",
    (0x0C, 0x0D): "Write error, not enough unsolicited data",
    (0x0C, 0x0E): "Multiple write errors",
    (0x0E, 0x00): "Invalid information unit",
    (0x0E, 0x01): "Information unit too short",
    (0x0E, 0x02): "Information unit too long",
    (0x0E, 0x03): "Invalid field in command information unit",
    (0x10, 0x00): "ID CRC or ECC error",
    (0x10, 0x01): "Logical block guard check failed",
    (0x10, 0x02): "Logical block application tag check failed",
    (0x10, 0x03): "Logical block reference tag check failed",
    (0x11, 0x00): "Unrecovered read error",
    (0x14, 0x00): "Recorded entity not found",
    (0x14, 0x01): "Record not found",
    (0x14, 0x05): "Record not found, recommend reassignment",
    (0x15, 0x00): "Random positioning error",
    (0x15, 0x01): "Mechanical positioning error",
    (0x15, 0x02): "Positioning error detected by read of medium",
    (0x16, 0x00): "Data synchronization mark error",
    (0x17, 0x00): "Recovered data with no error correction applied",
    (0x17, 0x01): "Recovered data with retries",
    (0x17, 0x02): "Recovered data with positive head offset",
    (0x17, 0x03): "Recovered data with negative head offset",
    (0x17, 0x05): "Recovered data using previous sector id",
    (0x17, 0x06): "Recovered data without ECC, data auto-reallocated",
    (0x17, 0x07): "Recovered data without ECC, recommend reassignment",
    (0x17, 0x08): "Recovered data without ECC, recommend rewrite",
    (0x18, 0x00): "Recovered data with error correction applied",
    (0x18, 0x01): "Recovered data with error correction and retries applied",
    (0x18, 0x02): "Recovered data, data auto-reallocated",
    (0x18, 0x05): "Recovered data, recommend reassignment",
    (0x18, 0x06): "Recovered data, recommend rewrite",
    (0x19, 0x00): "Defect list error",
    (0x19, 0x01): "Defect list not available",
    (0x19, 0x02): "Defect list error in primary list",
    (0x19, 0x03): "Defect list error in grown list",
    (0x1A, 0x00): "Parameter list length error",
    (0x1B, 0x00): "Synchronous data transfer error",
    (0x20, 0x00): "Invalid command operation code",
    (0x21, 0x00): "Logical block address out of range",
    (0x24, 0x00): "Invalid field in CDB",
    (0x25, 0x00): "Logical unit not supported",
    (0x27, 0x00): "Write protected",
    (0x27, 0x01): "Hardware write protected",
    (0x27, 0x02): "Logical unit software write protected",
    (0x27, 0x07): "Space allocation failed write protect",
    (0x28, 0x00): "Not ready to ready change, medium may have changed",
    (0x28, 0x01): "Import or export element accessed",
    (0x29, 0x00): "Power on, reset, or bus device reset occurred",
    (0x29, 0x01): "Power on occurred",
    (0x29, 0x02): "SCSI bus reset occurred",
    (0x29, 0x03): "Bus device reset function occurred",
    (0x29, 0x04): "Device internal reset",
    (0x29, 0x05): "Transceiver mode changed to single-ended",
    (0x29, 0x06): "Transceiver mode changed to LVD",
    (0x29, 0x07): "I_T nexus loss occurred",
    (0x2A, 0x01): "Mode parameters changed",
    (0x2A, 0x02): "Log parameters changed",
    (0x2A, 0x03): "Reservations preempted",
    (0x2A, 0x04): "Reservations released",
    (0x2A, 0x05): "Registrations preempted",
    (0x2A, 0x06): "Asymmetric access state changed",
    (0x2A, 0x07): "Implicit asymmetric access state transition failed",
    (0x2A, 0x09): "Capacity data has changed",
    (0x2A, 0x0A): "Error history I_T nexus cleared",
    (0x2A, 0x0B): "Error history snapshot released",
    (0x2C, 0x00): "Command sequence error",
    (0x2F, 0x00): "Commands cleared by another initiator",
    (0x31, 0x00): "Medium format corrupted",
    (0x31, 0x01): "Format command failed",
    (0x31, 0x02): "Zoned formatting failed due to spare linking",
    (0x32, 0x00): "No defect spare location available",
    (0x32, 0x01): "Defect list update failure",
    (0x3A, 0x00): "Medium not present",
    (0x3E, 0x00): "Logical unit has not self-configured yet",
    (0x3E, 0x01): "Logical unit failure",
    (0x3E, 0x02): "Timeout on logical unit",
    (0x3E, 0x03): "Logical unit failed self-test",
    (0x3E, 0x04): "Logical unit unable to update self-test log",
    (0x3F, 0x00): "Target operating conditions have changed",
    (0x3F, 0x01): "Microcode has been changed",
    (0x3F, 0x03): "Inquiry data has changed",
    (0x3F, 0x04): "Component device attached",
    (0x3F, 0x05): "Device identifier changed",
    (0x3F, 0x06): "Redundancy group created or modified",
    (0x3F, 0x07): "Redundancy group deleted",
    (0x3F, 0x08): "Spare created or modified",
    (0x3F, 0x09): "Spare deleted",
    (0x3F, 0x0A): "Volume set created or modified",
    (0x3F, 0x0B): "Volume set deleted",
    (0x3F, 0x0E): "Reported LUNs data has changed",
    (0x3F, 0x0F): "Echo buffer overwritten",
    (0x3F, 0x16): "Microcode has been changed without reset",
    (0x34, 0x00): "Enclosure failure",
    (0x35, 0x00): "Enclosure services failure",
    (0x35, 0x01): "Unsupported enclosure function",
    (0x35, 0x02): "Enclosure services unavailable",
    (0x40, 0x00): "RAM failure",
    (0x41, 0x00): "Data path failure",
    (0x42, 0x00): "Power-on or self-test failure",
    (0x43, 0x00): "Message error",
    (0x44, 0x00): "Internal target failure",
    (0x45, 0x00): "Select or reselect failure",
    (0x46, 0x00): "Unsuccessful soft reset",
    (0x47, 0x00): "SCSI parity error",
    (0x47, 0x01): "Data phase CRC error detected",
    (0x47, 0x02): "SCSI parity error detected during ST data phase",
    (0x47, 0x03): "Information unit iuCRC error detected",
    (0x47, 0x04): "Asynchronous information protection error detected",
    (0x47, 0x05): "Protocol service CRC error",
    (0x47, 0x06): "PHY test function in progress",
    (0x48, 0x00): "Initiator detected error message received",
    (0x49, 0x00): "Invalid message error",
    (0x4A, 0x00): "Command phase error",
    (0x4B, 0x00): "Data phase error",
    (0x4B, 0x01): "Invalid target port transfer tag received",
    (0x4B, 0x02): "Too much write data",
    (0x4B, 0x03): "ACK/NAK timeout",
    (0x4B, 0x04): "NAK received",
    (0x4B, 0x05): "Data offset error",
    (0x4B, 0x06): "Initiator response timeout",
    (0x4B, 0x07): "Connection lost",
    (0x4B, 0x08): "Data-in buffer overflow, data buffer size",
    (0x4B, 0x09): "Data-in buffer overflow, data buffer descriptor area",
    (0x4B, 0x0A): "Data-in buffer error",
    (0x4B, 0x0B): "Data-out buffer overflow, data buffer size",
    (0x4B, 0x0C): "Data-out buffer overflow, data buffer descriptor area",
    (0x4B, 0x0D): "Data-out buffer error",
    (0x4B, 0x0E): "PCIe fabric error",
    (0x4B, 0x0F): "PCIe completion timeout",
    (0x4B, 0x10): "PCIe completer abort",
    (0x4B, 0x11): "PCIe poisoned TLP received",
    (0x4B, 0x12): "PCIe ECRC check failed",
    (0x4B, 0x13): "PCIe unsupported request",
    (0x4B, 0x14): "PCIe ACS violation",
    (0x4B, 0x15): "PCIe TLP prefix blocked",
    (0x4C, 0x00): "Logical unit failed self-configuration",
    (0x55, 0x00): "System resource failure",
    (0x55, 0x01): "System buffer full",
    (0x55, 0x03): "Insufficient resources",
    (0x55, 0x0B): "Insufficient power for operation",
    (0x5B, 0x00): "Log exception",
    (0x5B, 0x01): "Threshold condition met",
    (0x5B, 0x02): "Log counter at maximum",
    (0x5B, 0x03): "Log list codes exhausted",
    (0x5E, 0x00): "Low power condition on",
    (0x5E, 0x01): "Idle condition activated by timer",
    (0x5E, 0x02): "Standby condition activated by timer",
    (0x5E, 0x03): "Idle condition activated by command",
    (0x5E, 0x04): "Standby condition activated by command",
    (0x5D, 0x00): "Failure prediction threshold exceeded",
    (0x5D, 0x01): "Media failure prediction threshold exceeded",
    (0x5D, 0x02): "Logical unit failure prediction threshold exceeded",
    (0x5D, 0x03): "Spare area exhaustion prediction threshold exceeded",
    (0x5D, 0x10): "Hardware impending failure, general hard drive failure",
    (0x5D, 0x11): "Hardware impending failure, drive error rate too high",
    (0x5D, 0x12): "Hardware impending failure, data error rate too high",
    (0x5D, 0x13): "Hardware impending failure, seek error rate too high",
    (0x5D, 0x14): "Hardware impending failure, too many block reassigns",
    (0x5D, 0x15): "Hardware impending failure, access times too high",
    (0x5D, 0x16): "Hardware impending failure, start unit times too high",
    (0x5D, 0x17): "Hardware impending failure, channel parametrics",
    (0x5D, 0x18): "Hardware impending failure, controller detected",
    (0x5D, 0x19): "Hardware impending failure, throughput performance",
    (0x5D, 0x1A): "Hardware impending failure, seek time performance",
    (0x5D, 0x1B): "Hardware impending failure, spin-up retry count",
    (0x5D, 0x1C): "Hardware impending failure, drive calibration retry count",
    (0x67, 0x00): "Configuration failure",
    (0x68, 0x00): "Logical unit not configured",
    (0x69, 0x00): "Data loss on logical unit",
    (0x6B, 0x00): "State change has occurred",
    (0x6B, 0x01): "A redundancy level got better",
    (0x6B, 0x02): "A redundancy level got worse",
    (0x6C, 0x00): "Rebuild failure occurred",
    (0x6D, 0x00): "Recalculate failure occurred",
}

SCSI_STATUS_LABELS = {
    "GOOD": "GOOD",
    "CHECK CONDITION": "CHECK CONDITION",
    "CONDITION MET": "CONDITION MET",
    "BUSY": "BUSY",
    "RESERVATION CONFLICT": "RESERVATION CONFLICT",
    "TASK SET FULL": "TASK SET FULL",
    "ACA ACTIVE": "ACA ACTIVE",
    "TASK ABORTED": "TASK ABORTED",
}

SCSI_STATUS_CODES = {
    0x00: "GOOD",
    0x02: "CHECK CONDITION",
    0x04: "CONDITION MET",
    0x08: "BUSY",
    0x18: "RESERVATION CONFLICT",
    0x28: "TASK SET FULL",
    0x30: "ACA ACTIVE",
    0x40: "TASK ABORTED",
}

LOG_SENSE_PAGE_NAMES = {
    0x00: "Supported Log Pages",
    0x02: "Write Error Counter",
    0x03: "Read Error Counter",
    0x05: "Verify Error Counter",
    0x0D: "Temperature",
    0x0E: "Start-Stop Cycle Counter",
    0x10: "Self-Test Results",
    0x15: "Background Scan Results",
    0x18: "Protocol-Specific Port",
    0x19: "General Statistics and Performance",
    0x1A: "Power Condition Transitions",
    0x2F: "Informational Exceptions",
    0x3F: "All Log Pages",
}

LOG_SENSE_PAGE_CONTROL_LABELS = {
    0x00: "Current cumulative values",
    0x01: "Current threshold values",
    0x02: "Default cumulative values",
    0x03: "Default threshold values",
}

SAS_PHY_LOG_PARAMETERS = [
    {"code": "0x0001", "name": "Invalid dword count"},
    {"code": "0x0002", "name": "Running disparity error count"},
    {"code": "0x0003", "name": "Loss of dword synchronization count"},
    {"code": "0x0004", "name": "PHY reset problem count"},
]

SES_ELEMENT_TYPE_LABELS = {
    0x00: "Unspecified",
    0x01: "Device slot",
    0x02: "Power supply",
    0x03: "Cooling",
    0x04: "Temperature sensor",
    0x05: "Door",
    0x06: "Audible alarm",
    0x07: "Enclosure services controller electronics",
    0x08: "SCC controller electronics",
    0x09: "Nonvolatile cache",
    0x0B: "Uninterruptible power supply",
    0x0C: "Display",
    0x0D: "Key pad entry",
    0x0E: "Enclosure",
    0x0F: "SCSI port/transceiver",
    0x10: "Language",
    0x11: "Communication port",
    0x12: "Voltage sensor",
    0x13: "Current sensor",
    0x14: "SCSI target port",
    0x15: "SCSI initiator port",
    0x17: "Array device slot",
    0x18: "SAS expander",
    0x19: "SAS connector",
}

AES_CONCEPTS = {
    "descriptor": "Additional Element Status descriptors attach element-specific identity.",
    "array_device_slot": "Array device AES can carry SAS address, phy, and bay-index hints.",
    "expander": "Expander AES can connect element identity to the SAS fabric.",
    "connector": "Connector AES can describe external/internal cable attachment points.",
}


def decode_scsi_cdb_message(message: str) -> dict[str, Any]:
    match = re.search(
        r"(?P<operation>[A-Z][A-Z0-9 _/-]*(?:\([A-Z0-9]+\))?)\.\s+CDB:\s+(?P<cdb>[0-9a-fA-F ]+)",
        message,
    )
    if not match:
        return {}
    cdb_bytes = [int(token, 16) for token in match.group("cdb").split() if re.fullmatch(r"[0-9a-fA-F]{2}", token)]
    reported_operation = match.group("operation").strip()
    opcode = cdb_bytes[0] if cdb_bytes else None
    operation_from_table = opcode in SCSI_CDB_OPCODE_NAMES if opcode is not None else False
    operation = SCSI_CDB_OPCODE_NAMES.get(opcode, reported_operation) if opcode is not None else reported_operation
    service_action = None
    service_action_known = False
    if opcode in SCSI_SERVICE_ACTION_OPCODES and len(cdb_bytes) > 1:
        service_action = cdb_bytes[1] & 0x1F
        service_action_operation = SCSI_SERVICE_ACTION_NAMES.get((opcode, service_action))
        if service_action_operation:
            operation = service_action_operation
            service_action_known = True

    family = _cdb_fault_family(operation)
    confidence = "standard" if operation_from_table else "observed"
    decoder_note = None
    if opcode in SCSI_SERVICE_ACTION_OPCODES and service_action is not None and not service_action_known:
        confidence = "standard-partial"
        decoder_note = "Opcode is standard, but this service action is not in the current local lookup table."
    elif not operation_from_table:
        decoder_note = "Kernel message supplied the operation name; opcode is not in the current local standards table."
    decoded: dict[str, Any] = {
        "label": operation,
        "family": family,
        "operation": operation,
        "reported_operation": reported_operation,
        "direction": _cdb_direction(operation),
        "cdb_hex": " ".join(f"{value:02x}" for value in cdb_bytes),
        "byte_count": len(cdb_bytes),
        "likely_layer": fault_family_likely_layer(family),
        "description": f"{operation} was active when the path error was reported.",
        "decode_confidence": confidence,
        "decode_source": "t10_scsi_operation_codes" if operation_from_table else "kernel_message",
        "source_attribution": dict(T10_OPCODE_SOURCE if operation_from_table else KERNEL_MESSAGE_SOURCE),
    }
    if decoder_note:
        decoded["decoder_note"] = decoder_note
    if opcode is not None:
        decoded["opcode"] = f"0x{opcode:02x}"
    if service_action is not None:
        decoded["service_action"] = f"0x{service_action:02x}"
        if service_action_known:
            decoded["service_action_label"] = operation

    address = _decode_cdb_address(cdb_bytes)
    if address:
        decoded.update(address)
    lengths = _decode_cdb_lengths(cdb_bytes)
    if lengths:
        decoded.update(lengths)
    if opcode == 0x4D:
        decoded.update(_decode_log_sense_cdb(cdb_bytes))
    return decoded


def decode_scsi_sense_event(event: dict[str, Any]) -> dict[str, Any]:
    reason = str(event.get("reason") or "").strip()
    sense_key = str(event.get("sense_key") or event.get("sense") or "").upper()
    asc = str(event.get("asc") or "").lower()
    asc_tuple = parse_asc_ascq(asc)
    asc_label = SCSI_ASC_ASCQ_LABELS.get(asc_tuple) if asc_tuple else None
    family = _sense_fault_family(reason, sense_key, asc_tuple)
    sense_label = SCSI_SENSE_KEY_LABELS.get(sense_key)
    label = reason or asc_label or sense_label or "SCSI sense event"
    confidence = "standard" if asc_label else "observed" if reason or sense_label else "unconfirmed"
    decoder_note = None
    if asc_tuple and not asc_label:
        decoder_note = "ASC/ASCQ was observed in kernel output but is not in the current local lookup table."
        if not reason:
            label = f"Unconfirmed ASC/ASCQ {asc}"
    decoded = {
        "label": label,
        "family": family,
        "sense_key": sense_key,
        "sense_label": sense_label,
        "asc": asc,
        "asc_label": asc_label,
        "likely_layer": _sense_likely_layer(family),
        "description": _sense_description(label, family),
        "decode_confidence": confidence,
        "decode_source": "t10_scsi_asc_ascq" if asc_label else "kernel_message",
        "source_attribution": dict(T10_ASC_ASCQ_SOURCE if asc_label else KERNEL_MESSAGE_SOURCE),
    }
    if decoder_note:
        decoded["decoder_note"] = decoder_note
    return decoded


def parse_asc_ascq(value: str) -> tuple[int, int] | None:
    parts = [part.strip() for part in str(value or "").split(",", 1)]
    if len(parts) != 2:
        return None
    try:
        return int(parts[0], 16), int(parts[1], 16)
    except ValueError:
        return None


def decode_scsi_status_value(value: str) -> dict[str, Any]:
    raw_status = str(value or "").strip()
    if not raw_status:
        return {
            "label": "SCSI status event",
            "family": "scsi_status",
            "likely_layer": "SCSI target status",
            "description": "The target returned a SCSI status response for this command.",
        }

    status_code = None
    normalized = raw_status.upper().replace("_", " ").replace("-", " ")
    numeric_match = re.fullmatch(r"(?:0x)?(?P<value>[0-9a-fA-F]{1,2})h?", raw_status)
    if numeric_match:
        status_code = int(numeric_match.group("value"), 16)
        normalized = SCSI_STATUS_CODES.get(status_code, normalized)
    label = SCSI_STATUS_LABELS.get(normalized)
    if label:
        family = "aborted_command" if label == "TASK ABORTED" else "scsi_status"
        description = "The target returned a standard SCSI status response for this command."
        if label == "CHECK CONDITION":
            description = "The target returned Check Condition; following sense data should explain the failure."
        elif label in {"BUSY", "TASK SET FULL"}:
            description = "The target or command queue could not accept the command at that moment."
        elif label == "RESERVATION CONFLICT":
            description = "The command conflicted with a SCSI reservation or persistent reservation state."
        elif label == "TASK ABORTED":
            description = "The target reported that the task was aborted."
        display_label = _display_scsi_status_label(label)
        decoded: dict[str, Any] = {
            "label": f"SCSI status: {display_label}",
            "family": family,
            "likely_layer": fault_family_likely_layer(family) if family != "scsi_status" else "SCSI target status",
            "description": description,
            "scsi_status": display_label,
            "decode_confidence": "standard",
            "decode_source": "t10_scsi_status",
            "source_attribution": dict(T10_STATUS_SOURCE),
        }
        if status_code is not None:
            decoded["scsi_status_code"] = f"0x{status_code:02x}"
        return decoded

    return {
        "label": f"SCSI status: {raw_status}",
        "family": "scsi_status",
        "likely_layer": "SCSI target status",
        "description": "The target returned a SCSI status response that is not in the current local status-code table.",
        "scsi_status": raw_status,
        "decode_confidence": "observed",
        "decode_source": "kernel_message",
        "source_attribution": dict(KERNEL_MESSAGE_SOURCE),
        "decoder_note": "Kernel message supplied the SCSI status text; value is not in the current local status-code table.",
    }


def _display_scsi_status_label(label: str) -> str:
    return " ".join("ACA" if token == "ACA" else token.title() for token in label.split())


def _cdb_fault_family(operation: str) -> str:
    direction = _cdb_direction(operation)
    if direction == "write":
        return "write_io"
    if direction == "read":
        return "read_io"
    upper = operation.upper()
    if upper.startswith("READ CAPACITY"):
        return "capacity_query"
    if upper == "LOG SENSE":
        return "log_sense"
    if "PERSISTENT RESERVE" in upper or upper in {
        "READ KEYS",
        "READ RESERVATION",
        "REPORT CAPABILITIES",
        "READ FULL STATUS",
        "REGISTER",
        "RESERVE",
        "RELEASE",
        "CLEAR",
        "PREEMPT",
        "PREEMPT AND ABORT",
        "REGISTER AND IGNORE EXISTING KEY",
        "REGISTER AND MOVE",
    }:
        return "maintenance"
    if "DIAGNOSTIC" in upper or "ENCLOSURE" in upper:
        return "ses_enclosure"
    if "MAINTENANCE" in upper:
        return "maintenance"
    return "scsi_command"


def _cdb_direction(operation: str) -> str:
    upper = operation.upper()
    if upper.startswith("WRITE") or " WRITE" in upper:
        return "write"
    if upper.startswith("READ") or " READ" in upper:
        return "read"
    return "other"


def _decode_cdb_address(cdb_bytes: list[int]) -> dict[str, Any]:
    if len(cdb_bytes) >= 16 and cdb_bytes[0] in {0x88, 0x8A, 0x8E, 0x8F, 0x91, 0x93}:
        return {
            "lba": _int_from_bytes(cdb_bytes[2:10]),
            "transfer_blocks": _int_from_bytes(cdb_bytes[10:14]),
        }
    if len(cdb_bytes) >= 12 and cdb_bytes[0] in {0xA8, 0xAA, 0xAE, 0xAF}:
        return {
            "lba": _int_from_bytes(cdb_bytes[2:6]),
            "transfer_blocks": _int_from_bytes(cdb_bytes[6:10]),
        }
    if len(cdb_bytes) >= 10 and cdb_bytes[0] in {0x28, 0x2A, 0x2E, 0x2F, 0x35, 0x41}:
        return {
            "lba": _int_from_bytes(cdb_bytes[2:6]),
            "transfer_blocks": _int_from_bytes(cdb_bytes[7:9]),
        }
    if len(cdb_bytes) >= 6 and cdb_bytes[0] in {0x08, 0x0A}:
        transfer_blocks = cdb_bytes[4] or 256
        return {
            "lba": ((cdb_bytes[1] & 0x1F) << 16) | (cdb_bytes[2] << 8) | cdb_bytes[3],
            "transfer_blocks": transfer_blocks,
        }
    return {}


def _decode_cdb_lengths(cdb_bytes: list[int]) -> dict[str, Any]:
    if not cdb_bytes:
        return {}
    opcode = cdb_bytes[0]
    if opcode in {0x03, 0x12, 0x1A} and len(cdb_bytes) >= 5:
        return {"allocation_length": cdb_bytes[4]}
    if opcode in {0x15} and len(cdb_bytes) >= 5:
        return {"parameter_list_length": cdb_bytes[4]}
    if opcode in {0x4C, 0x55, 0x5F} and len(cdb_bytes) >= 9:
        return {"parameter_list_length": _int_from_bytes(cdb_bytes[7:9])}
    if opcode in {0x42} and len(cdb_bytes) >= 9:
        return {"parameter_list_length": _int_from_bytes(cdb_bytes[7:9])}
    if opcode in {0x4D, 0x5A, 0x5E, 0xA0} and len(cdb_bytes) >= 9:
        return {"allocation_length": _int_from_bytes(cdb_bytes[7:9])}
    if opcode in {0x84} and len(cdb_bytes) >= 14:
        return {"allocation_length": _int_from_bytes(cdb_bytes[10:14])}
    if opcode == 0xA3 and len(cdb_bytes) >= 10:
        return {"allocation_length": _int_from_bytes(cdb_bytes[6:10])}
    if opcode == 0xA4 and len(cdb_bytes) >= 10:
        return {"parameter_list_length": _int_from_bytes(cdb_bytes[6:10])}
    if opcode == 0x9E and len(cdb_bytes) >= 14:
        service_action = cdb_bytes[1] & 0x1F
        if service_action in {0x10, 0x12}:
            return {"allocation_length": _int_from_bytes(cdb_bytes[10:14])}
    return {}


def _decode_log_sense_cdb(cdb_bytes: list[int]) -> dict[str, Any]:
    if len(cdb_bytes) < 3:
        return {}
    page_code = cdb_bytes[2] & 0x3F
    page_control = (cdb_bytes[2] >> 6) & 0x03
    decoded: dict[str, Any] = {
        "log_page_code": f"0x{page_code:02x}",
        "log_page": LOG_SENSE_PAGE_NAMES.get(page_code, "Unknown log page"),
        "log_page_control": f"0x{page_control:x}",
        "log_page_control_label": LOG_SENSE_PAGE_CONTROL_LABELS[page_control],
        "log_save_parameters": bool(cdb_bytes[1] & 0x01),
        "log_page_source": dict(T10_LOG_SENSE_SOURCE),
    }
    if len(cdb_bytes) > 3:
        decoded["log_subpage_code"] = f"0x{cdb_bytes[3]:02x}"
    if page_code == 0x18:
        decoded["sas_phy_log_concepts"] = SAS_PHY_LOG_PARAMETERS
    return decoded


def _int_from_bytes(values: list[int]) -> int:
    result = 0
    for value in values:
        result = (result << 8) | value
    return result


def _sense_fault_family(reason: str, sense_key: str, asc: tuple[int, int] | None) -> str:
    lowered = f"{reason} {sense_key}".lower()
    if asc:
        asc_code, ascq = asc
        if asc_code == 0x03:
            return "write_error"
        if asc_code == 0x04:
            if ascq in {0x0A, 0x0B, 0x0C}:
                return "device_path_exception"
            if ascq in {0x20, 0x21, 0x22}:
                return "target_failure"
            return "target_failure"
        if asc_code == 0x08:
            return "logical_unit_communication"
        if asc_code == 0x09:
            return "target_failure"
        if asc_code == 0x0B:
            if ascq == 0x02:
                return "enclosure_warning"
            return "device_path_exception"
        if asc_code == 0x0C:
            return "write_error"
        if asc_code == 0x10:
            return "protection_error"
        if asc_code == 0x11:
            return "read_error"
        if asc_code == 0x14:
            return "device_path_exception"
        if asc_code in {0x15, 0x16}:
            return "target_failure"
        if asc_code in {0x17, 0x18}:
            return "recovered_data"
        if asc_code == 0x19:
            return "medium_format"
        if asc_code == 0x1B:
            return "sas_protocol"
        if asc_code == 0x27:
            return "write_protect"
        if asc_code in {0x28, 0x29, 0x2A, 0x2F, 0x6B}:
            if asc_code == 0x29:
                if ascq == 0x07:
                    return "link_loss"
                if ascq in {0x02, 0x03, 0x04}:
                    return "bus_reset"
                if ascq in {0x05, 0x06}:
                    return "sas_protocol"
            if asc_code == 0x2A and ascq == 0x07:
                return "device_path_exception"
            return "unit_attention"
        if asc_code in {0x31, 0x32}:
            return "medium_format"
        if asc_code in {0x34, 0x35}:
            return "ses_enclosure"
        if asc_code == 0x3E:
            if ascq == 0x02:
                return "timeout"
            return "target_failure"
        if asc_code == 0x3F:
            return "unit_attention"
        if asc_code in {0x40, 0x41, 0x42, 0x44}:
            return "target_failure"
        if asc_code in {0x45, 0x46, 0x47, 0x48, 0x49, 0x4A}:
            return "sas_protocol"
        if asc_code == 0x4B:
            if ascq in {0x03, 0x06, 0x0F}:
                return "timeout"
            if ascq == 0x07:
                return "link_loss"
            if 0x08 <= ascq <= 0x0D:
                return "data_buffer_error"
            if 0x0E <= ascq <= 0x15:
                return "pcie_fabric"
            return "sas_protocol"
        if asc_code == 0x4C:
            return "target_failure"
        if asc_code == 0x55:
            return "device_path_exception"
        if asc_code == 0x5B:
            return "log_exception"
        if asc_code == 0x5D:
            return "failure_prediction"
        if asc_code == 0x5E:
            return "power_condition"
        if asc_code in {0x67, 0x68, 0x69, 0x6C, 0x6D}:
            return "target_failure"
    if asc == (0x4B, 0x03):
        return "timeout"
    if asc == (0x4B, 0x04):
        return "sas_protocol"
    if asc == (0x4B, 0x07):
        return "link_loss"
    if "nak" in lowered:
        return "sas_protocol"
    if "connection lost" in lowered:
        return "link_loss"
    if "timeout" in lowered:
        return "timeout"
    if "bus reset" in lowered or "reset occurred" in lowered:
        return "bus_reset"
    if "aborted" in lowered:
        return "aborted_command"
    return "scsi_sense"


def _sense_likely_layer(family: str) -> str:
    if family in {"sas_protocol", "link_loss", "timeout"}:
        return "SAS path, cable, expander, or target port"
    if family == "bus_reset":
        return "SCSI/SAS bus recovery"
    if family == "aborted_command":
        return "Target or transport aborted command"
    if family == "logical_unit_communication":
        return "Target communication path"
    if family == "target_failure":
        return "SCSI target/device"
    if family == "medium_format":
        return "Target medium/defect management"
    if family == "failure_prediction":
        return "Target health prediction"
    if family == "recovered_data":
        return "Target media recovery"
    if family == "write_protect":
        return "Target write protection"
    if family == "log_exception":
        return "SCSI diagnostic log"
    if family == "power_condition":
        return "SCSI target power condition"
    if family == "unit_attention":
        return "SCSI target state change"
    if family == "enclosure_warning":
        return "SES/enclosure health"
    if family == "pcie_fabric":
        return "Host PCIe fabric or endpoint"
    if family == "data_buffer_error":
        return "SCSI data buffer transfer"
    if family in {"write_error", "read_error"}:
        return "Target media or command data path"
    if family == "ses_enclosure":
        return "SES/enclosure management path"
    if family == "protection_error":
        return "Block protection metadata"
    return "SCSI target or transport"


def _sense_description(label: str, family: str) -> str:
    if family == "sas_protocol":
        return f"{label} is a SAS protocol/link symptom, often path or signal related."
    if family == "link_loss":
        return "The OS reported that the link to this target was lost."
    if family == "timeout":
        return "The command waited too long for SAS acknowledgement or completion."
    if family == "bus_reset":
        return "The bus was reset while recovering from transport errors."
    if family == "logical_unit_communication":
        return f"{label} indicates the target stopped responding cleanly on the communication path."
    if family == "target_failure":
        return f"{label} points at a target/device-side failure reported through SCSI sense data."
    if family == "medium_format":
        return f"{label} points at target medium formatting or defect-list management."
    if family == "failure_prediction":
        return f"{label} is a target-reported health prediction condition."
    if family == "recovered_data":
        return f"{label} means the target recovered data after media or correction work."
    if family == "write_protect":
        return f"{label} means the target refused the command because write protection is active."
    if family == "log_exception":
        return f"{label} points at a SCSI diagnostic log threshold or counter condition."
    if family == "power_condition":
        return f"{label} reports a target power-condition transition."
    if family == "unit_attention":
        return f"{label} is a SCSI target state-change notification."
    if family == "enclosure_warning":
        return f"{label} is an enclosure/SES health warning reported through SCSI sense data."
    if family == "pcie_fabric":
        return f"{label} is a host PCIe fabric or endpoint error surfaced through SCSI sense data."
    if family == "data_buffer_error":
        return f"{label} indicates a data buffer transfer problem during the SCSI command."
    if family == "write_error":
        return f"{label} indicates the target reported a write-path error through SCSI sense data."
    if family == "read_error":
        return f"{label} indicates the target reported a read-path error through SCSI sense data."
    if family == "ses_enclosure":
        return f"{label} came from an enclosure services or SES-related sense condition."
    if family == "protection_error":
        return f"{label} indicates block protection metadata did not validate."
    return f"The target returned SCSI sense data: {label}."
