from __future__ import annotations

import re
from typing import Any

from app.services.sas_diagnostics.common import fault_family_likely_layer


LSI_LOGINFO_SOURCE = {
    "name": "baruch/lsi_decode_loginfo",
    "url": "https://github.com/baruch/lsi_decode_loginfo",
    "license": "MIT",
}

# Broadcom/LSI loginfo field names and nested table shapes are derived from
# baruch/lsi_decode_loginfo (MIT, Copyright 2014 Baruch Even) and the Linux
# mpt2sas/mpt3sas headers it cites. This module ports selected tables into a
# typed app-local decoder rather than vendoring the script.
LSI_LOGINFO_TYPE_NAMES = {
    0x00000000: ("NONE", "none"),
    0x10000000: ("SCSI", "scsi"),
    0x20000000: ("FC", "fc"),
    0x30000000: ("SAS", "sas"),
    0x40000000: ("iSCSI", "iscsi"),
}

LSI_SAS_ORIGIN_NAMES = {
    0x00000000: ("IOP", "iop"),
    0x01000000: ("PL", "physical_layer"),
    0x02000000: ("IR", "integrated_raid"),
}

LSI_SAS_IOP_CODES = {
    0x00010000: ("IOP_LOGINFO_CODE_BOOT", "Boot", "controller_configuration", ""),
    0x00030000: ("IOP_LOGINFO_CODE_CONFIG_INVALID_PAGE", "Config invalid page", "controller_configuration", ""),
    0x00040000: ("IOP_LOGINFO_CODE_DIAG_MSG_ERROR", "Diagnostic message error", "controller_configuration", ""),
    0x00050000: ("IOP_LOGINFO_CODE_TASK_TERMINATED", "Task terminated", "controller_terminated_io", "Associated with Task Abort"),
    0x00060000: ("IOP_LOGINFO_CODE_ENCL_MGMT", "Enclosure management", "ses_enclosure", ""),
    0x00070000: ("IOP_LOGINFO_CODE_TARGET", "Target mode", "controller_terminated_io", ""),
    0x00080000: ("IOP_LOGINFO_CODE_LOG_TIMESTAMP_EVENT", "Log timestamp event", "controller_configuration", ""),
}

LSI_SAS_IOP_INVALID_PAGE_DETAILS = {
    0x00000100: ("IOP_LOGINFO_CODE_CONFIG_INVALID_PAGE_RT", "Route Table Entry not found", "controller_configuration"),
    0x00000200: ("IOP_LOGINFO_CODE_CONFIG_INVALID_PAGE_PN", "Invalid Page Number", "controller_configuration"),
    0x00000300: ("IOP_LOGINFO_CODE_CONFIG_INVALID_PAGE_FORM", "Invalid FORM", "controller_configuration"),
    0x00000400: ("IOP_LOGINFO_CODE_CONFIG_INVALID_PAGE_PT", "Invalid Page Type", "controller_configuration"),
    0x00000500: ("IOP_LOGINFO_CODE_CONFIG_INVALID_PAGE_DNM", "Device Not Mapped", "controller_configuration"),
    0x00000600: ("IOP_LOGINFO_CODE_CONFIG_INVALID_PAGE_PERSIST", "Persistent Page not found", "controller_configuration"),
    0x00000700: ("IOP_LOGINFO_CODE_CONFIG_INVALID_PAGE_DEFAULT", "Default Page not found", "controller_configuration"),
}

LSI_SAS_IR_CODES = {
    0x00010000: ("IR_LOGINFO_RAID_ACTION_ERROR", "RAID action error", "device_path_exception"),
    0x00010001: ("IR_LOGINFO_VOLUME_CREATE_INVALID_LENGTH", "Volume create invalid length", "device_path_exception"),
    0x00010002: ("IR_LOGINFO_VOLUME_CREATE_DUPLICATE", "Volume create duplicate", "device_path_exception"),
    0x00010003: ("IR_LOGINFO_VOLUME_CREATE_NO_SLOTS", "Volume create no slots", "device_path_exception"),
    0x00010020: ("IR_LOGINFO_PHYSDISK_CREATE_TOO_MANY_DISKS", "Physical disk create too many disks", "device_path_exception"),
    0x00010023: ("IR_LOGINFO_PHYSDISK_CREATE_BUS_TID_INVALID", "Physical disk bus/target invalid", "device_path_exception"),
    0x00010030: ("IR_LOGINFO_COMPAT_ERROR_RAID_DISABLED", "Compatibility error, RAID disabled", "device_path_exception"),
    0x00010031: ("IR_LOGINFO_COMPAT_ERROR_INQUIRY_FAILED", "Compatibility error, inquiry failed", "device_path_exception"),
    0x0001003A: ("IR_LOGINFO_COMPAT_ERROR_PHYS_DISK_NOT_FOUND", "Physical disk not found", "link_loss"),
    0x00020000: ("IR_LOGINFO_CODE_UNUSED2", "Unused IR code", "device_path_exception"),
}

LSI_SAS_PL_CODES = {
    0x00010000: ("PL_LOGINFO_CODE_OPEN_FAILURE", "Open failure", "sas_transport", "see SUB_CODE_OPEN_FAIL"),
    0x00020000: ("PL_LOGINFO_CODE_INVALID_SGL", "Invalid SGL", "controller_terminated_io", ""),
    0x00030000: (
        "PL_LOGINFO_CODE_WRONG_REL_OFF_OR_FRAME_LENGTH",
        "Wrong relative offset or frame length",
        "sas_transport",
        "",
    ),
    0x00040000: ("PL_LOGINFO_CODE_FRAME_XFER_ERROR", "Frame transfer error", "sas_transport", ""),
    0x00050000: ("PL_LOGINFO_CODE_TX_FM_CONNECTED_LOW", "TX frame connected low", "sas_transport", ""),
    0x00060000: ("PL_LOGINFO_CODE_SATA_NON_NCQ_RW_ERR_BIT_SET", "SATA non-NCQ read/write error", "device_path_exception", ""),
    0x00070000: ("PL_LOGINFO_CODE_SATA_READ_LOG_RECEIVE_DATA_ERR", "SATA read-log receive-data error", "device_path_exception", ""),
    0x00080000: ("PL_LOGINFO_CODE_SATA_NCQ_FAIL_ALL_CMDS_AFTR_ERR", "SATA NCQ failed all commands after error", "device_path_exception", ""),
    0x00090000: ("PL_LOGINFO_CODE_SATA_ERR_IN_RCV_SET_DEV_BIT_FIS", "SATA error in received Set Device Bit FIS", "device_path_exception", ""),
    0x000A0000: ("PL_LOGINFO_CODE_RX_FM_INVALID_MESSAGE", "RX frame invalid message", "sas_protocol", ""),
    0x000B0000: ("PL_LOGINFO_CODE_RX_CTX_MESSAGE_VALID_ERROR", "RX context message valid error", "sas_protocol", ""),
    0x000C0000: ("PL_LOGINFO_CODE_RX_FM_CURRENT_FRAME_ERROR", "RX frame current-frame error", "sas_protocol", ""),
    0x000D0000: ("PL_LOGINFO_CODE_SATA_LINK_DOWN", "SATA link down", "link_loss", ""),
    0x000E0000: ("PL_LOGINFO_CODE_DISCOVERY_SATA_INIT_W_IOS", "Discovery SATA init with IOs", "device_path_exception", ""),
    0x000F0000: ("PL_LOGINFO_CODE_CONFIG_ERROR", "Configuration error", "controller_configuration", ""),
    0x00100000: ("PL_LOGINFO_CODE_DSCVRY_SATA_INIT_TIMEOUT", "Discovery SATA init timeout", "timeout", ""),
    0x00110000: ("PL_LOGINFO_CODE_RESET", "Reset", "device_path_exception", "See Sub-Codes below"),
    0x00120000: ("PL_LOGINFO_CODE_ABORT", "Abort", "sas_transport", "See Sub-Codes below"),
    0x00130000: ("PL_LOGINFO_CODE_IO_NOT_YET_EXECUTED", "IO not yet executed", "controller_terminated_io", "Associated with Task Abort"),
    0x00140000: ("PL_LOGINFO_CODE_IO_EXECUTED", "IO executed before abort", "controller_terminated_io", "Associated with Task Abort"),
    0x00150000: ("PL_LOGINFO_CODE_PERS_RESV_OUT_NOT_AFFIL_OWNER", "Persistent reservation owner mismatch", "device_path_exception", ""),
    0x00160000: ("PL_LOGINFO_CODE_OPEN_TXDMA_ABORT", "Open TXDMA abort", "sas_transport", ""),
    0x00170000: ("PL_LOGINFO_CODE_IO_DEVICE_MISSING_DELAY_RETRY", "IO device missing delay retry", "link_loss", "I-T Nexus Loss"),
    0x00180000: ("PL_LOGINFO_CODE_IO_CANCELLED_DUE_TO_R_ERR", "IO cancelled due to receive error", "sas_transport", ""),
    0x00200000: ("PL_LOGINFO_CODE_ENCL_MGMT_ERR", "Enclosure management error", "ses_enclosure", ""),
}

LSI_SAS_PL_SUBCODES = {
    0x00000100: ("PL_LOGINFO_SUB_CODE_OPEN_FAILURE", "Open failure", "sas_transport"),
    0x00000200: ("PL_LOGINFO_SUB_CODE_INVALID_SGL", "Invalid SGL", "controller_terminated_io"),
    0x00000300: ("PL_LOGINFO_SUB_CODE_WRONG_REL_OFF_OR_FRAME_LENGTH", "Wrong relative offset or frame length", "sas_transport"),
    0x00000400: ("PL_LOGINFO_SUB_CODE_FRAME_XFER_ERROR", "Frame transfer error", "sas_transport"),
    0x00000500: ("PL_LOGINFO_SUB_CODE_TX_FM_CONNECTED_LOW", "TX frame connected low", "sas_transport"),
    0x00000600: ("PL_LOGINFO_SUB_CODE_SATA_NON_NCQ_RW_ERR_BIT_SET", "SATA non-NCQ read/write error", "device_path_exception"),
    0x00000700: ("PL_LOGINFO_SUB_CODE_SATA_READ_LOG_RECEIVE_DATA_ERR", "SATA read-log receive-data error", "device_path_exception"),
    0x00000800: ("PL_LOGINFO_SUB_CODE_SATA_NCQ_FAIL_ALL_CMDS_AFTR_ERR", "SATA NCQ failed all commands after error", "device_path_exception"),
    0x00000900: ("PL_LOGINFO_SUB_CODE_SATA_ERR_IN_RCV_SET_DEV_BIT_FIS", "SATA error in received Set Device Bit FIS", "device_path_exception"),
    0x00000A00: ("PL_LOGINFO_SUB_CODE_RX_FM_INVALID_MESSAGE", "RX frame invalid message", "sas_protocol"),
    0x00000B00: ("PL_LOGINFO_SUB_CODE_RX_CTX_MESSAGE_VALID_ERROR", "RX context message valid error", "sas_protocol"),
    0x00000C00: ("PL_LOGINFO_SUB_CODE_RX_FM_CURRENT_FRAME_ERROR", "RX frame current-frame error", "sas_protocol"),
    0x00000D00: ("PL_LOGINFO_SUB_CODE_SATA_LINK_DOWN", "SATA link down", "link_loss"),
    0x00000E00: ("PL_LOGINFO_SUB_CODE_DISCOVERY_SATA_ERR", "Discovery SATA error", "device_path_exception"),
    0x00000F00: ("PL_LOGINFO_SUB_CODE_SECOND_OPEN", "Second open", "sas_transport"),
    0x00001000: ("PL_LOGINFO_SUB_CODE_DSCVRY_SATA_INIT_TIMEOUT", "Discovery SATA init timeout", "timeout"),
    0x00002000: ("PL_LOGINFO_SUB_CODE_BREAK_ON_SATA_CONNECTION", "Break on SATA connection", "link_loss"),
    0x00003000: ("PL_LOGINFO_SUB_CODE_BREAK_ON_STUCK_LINK", "Break on stuck link", "link_loss"),
    0x00004000: ("PL_LOGINFO_SUB_CODE_BREAK_ON_STUCK_LINK_AIP", "Break on stuck link AIP", "link_loss"),
    0x00005000: ("PL_LOGINFO_SUB_CODE_BREAK_ON_INCOMPLETE_BREAK_RCVD", "Break on incomplete break received", "sas_transport"),
}

LSI_SAS_PL_OPEN_FAILURE_DETAILS = {
    0x00000001: ("PL_LOGINFO_SUB_CODE_OPEN_FAILURE_NO_DEST_TIMEOUT", "No destination timeout", "timeout"),
    0x00000002: ("PL_LOGINFO_SUB_CODE_OPEN_FAILURE_SATA_NEG_RATE_2HI", "SATA negotiated rate too high", "device_path_exception"),
    0x00000003: ("PL_LOGINFO_SUB_CODE_OPEN_FAILURE_RATE_NOT_SUPPORTED", "Rate not supported", "device_path_exception"),
    0x00000004: ("PL_LOGINFO_SUB_CODE_OPEN_FAILURE_BREAK", "Open failure break", "sas_transport"),
    0x00000005: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_RES_INITIALIZE0", "Resource initialize failure 0", "device_path_exception"),
    0x00000006: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_RES_INITIALIZE1", "Resource initialize failure 1", "device_path_exception"),
    0x00000007: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_RES_STOP0", "Resource stop failure 0", "device_path_exception"),
    0x00000008: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_RES_STOP1", "Resource stop failure 1", "device_path_exception"),
    0x00000009: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_RETRY", "Open fail retry", "retry"),
    0x0000000A: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_BREAK", "Open fail break", "sas_transport"),
    0x0000000C: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_OPEN_TIMEOUT_EXP", "Open fail timeout waiting for expander", "timeout"),
    0x0000000E: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_DVTBLE_ACCSS_FAIL", "Device table access failure", "device_path_exception"),
    0x00000011: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_BAD_DEST", "Bad destination", "device_path_exception"),
    0x00000012: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_RATE_NOT_SUPP", "Rate not supported", "device_path_exception"),
    0x00000013: ("PL_LOGINFO_SUB_CODE_OPEN_FAIL_PROT_NOT_SUPP", "Protocol not supported", "device_path_exception"),
    0x00000014: ("PL_LOGINFO_SUB_CODE_OPEN_FAILURE_ABANDON0", "Open reject zone violation", "device_path_exception"),
    0x0000001A: ("PL_LOGINFO_SUB_CODE_OPEN_FAILURE_ORR_TIMEOUT", "Open reject retry timeout", "timeout"),
    0x0000001B: ("PL_LOGINFO_SUB_CODE_OPEN_FAILURE_PATH_BLOCKED", "Path blocked", "link_loss"),
    0x0000001C: ("PL_LOGINFO_SUB_CODE_OPEN_FAILURE_AWT_MAXED", "Arbitration wait timer maxed", "timeout"),
    0x00000020: ("PL_LOGINFO_SUB_CODE_TARGET_BUS_RESET", "Target bus reset", "bus_reset"),
    0x00000030: ("PL_LOGINFO_SUB_CODE_TRANSPORT_LAYER", "Transport layer", "sas_transport"),
    0x00000040: ("PL_LOGINFO_SUB_CODE_PORT_LAYER", "Port layer", "sas_transport"),
}

LSI_SAS_PL_CONFIG_ERRORS = {
    0x00000001: ("PL_LOGINFO_CODE_CONFIG_PL_NOT_INITIALIZED", "PL not initialized", "controller_configuration"),
    0x00000100: ("PL_LOGINFO_CODE_CONFIG_INVALID_PAGE_PT", "Invalid Page Type", "controller_configuration"),
    0x00000200: ("PL_LOGINFO_CODE_CONFIG_INVALID_PAGE_NUM_PHYS", "Invalid Number of Phys", "controller_configuration"),
    0x00000300: ("PL_LOGINFO_CODE_CONFIG_INVALID_PAGE_NOT_IMP", "Case Not Handled", "controller_configuration"),
    0x00000400: ("PL_LOGINFO_CODE_CONFIG_INVALID_PAGE_NO_DEV", "No Device found", "device_path_exception"),
    0x00000500: ("PL_LOGINFO_CODE_CONFIG_INVALID_PAGE_FORM", "Invalid FORM", "controller_configuration"),
    0x00000600: ("PL_LOGINFO_CODE_CONFIG_INVALID_PAGE_PHY", "Invalid Phy", "controller_configuration"),
    0x00000700: ("PL_LOGINFO_CODE_CONFIG_INVALID_PAGE_NO_OWNER", "No Owner found", "controller_configuration"),
}

LSI_SAS_PL_ENCLOSURE_MGMT_ERRORS = {
    0x00000000: ("PL_LOGINFO_CODE_ENCL_MGMT_SMP_FRAME_FAILURE", "Can't get SMP Frame", "ses_enclosure"),
    0x00000010: ("PL_LOGINFO_CODE_ENCL_MGMT_SMP_READ_ERROR", "Error occurred on SMP Read", "ses_enclosure"),
    0x00000020: ("PL_LOGINFO_CODE_ENCL_MGMT_SMP_WRITE_ERROR", "Error occurred on SMP Write", "ses_enclosure"),
    0x00000040: ("PL_LOGINFO_CODE_ENCL_MGMT_NOT_SUPPORTED_ON_ENCL", "Enclosure management not available for this WWID", "ses_enclosure"),
    0x00000050: ("PL_LOGINFO_CODE_ENCL_MGMT_ADDR_MODE_NOT_SUPPORTED", "Address mode not supported", "ses_enclosure"),
    0x00000060: ("PL_LOGINFO_CODE_ENCL_MGMT_BAD_SLOT_NUM", "Invalid Slot Number in SEP message", "ses_enclosure"),
    0x00000070: ("PL_LOGINFO_CODE_ENCL_MGMT_SGPIO_NOT_PRESENT", "SGPIO not present/enabled", "ses_enclosure"),
    0x00000080: ("PL_LOGINFO_CODE_ENCL_MGMT_GPIO_NOT_CONFIGURED", "GPIO not configured", "ses_enclosure"),
    0x00000090: ("PL_LOGINFO_CODE_ENCL_MGMT_GPIO_FRAME_ERROR", "GPIO cannot allocate a frame", "ses_enclosure"),
    0x000000A0: ("PL_LOGINFO_CODE_ENCL_MGMT_GPIO_CONFIG_PAGE_ERROR", "GPIO failed config page request", "ses_enclosure"),
    0x000000B0: ("PL_LOGINFO_CODE_ENCL_MGMT_SES_FRAME_ALLOC_ERROR", "Can't get frame for SES command", "ses_enclosure"),
    0x000000C0: ("PL_LOGINFO_CODE_ENCL_MGMT_SES_IO_ERROR", "SES I/O execution error", "ses_enclosure"),
    0x000000D0: ("PL_LOGINFO_CODE_ENCL_MGMT_SES_RETRIES_EXHAUSTED", "SEP I/O retries exhausted", "ses_enclosure"),
    0x000000E0: ("PL_LOGINFO_CODE_ENCL_MGMT_SMP_FRAME_ALLOC_ERROR", "Can't get frame for SMP command", "ses_enclosure"),
    0x00000100: ("PL_LOGINFO_DA_SEP_NOT_PRESENT", "SEP not present when message received", "ses_enclosure"),
    0x00000101: ("PL_LOGINFO_DA_SEP_SINGLE_THREAD_ERROR", "SEP can only accept one message at a time", "ses_enclosure"),
    0x00000103: ("PL_LOGINFO_DA_SEP_RECEIVED_NACK_FROM_SLAVE", "SEP NACKed, it is busy", "ses_enclosure"),
    0x00000104: ("PL_LOGINFO_DA_SEP_DID_NOT_RECEIVE_ACK", "SEP did not receive ACK", "ses_enclosure"),
    0x00000105: ("PL_LOGINFO_DA_SEP_BAD_STATUS_HDR_CHKSUM", "SEP sent bad status header checksum", "ses_enclosure"),
    0x00000106: ("PL_LOGINFO_DA_SEP_STOP_ON_DATA", "SEP stopped while transferring data", "ses_enclosure"),
    0x00000107: ("PL_LOGINFO_DA_SEP_STOP_ON_SENSE_DATA", "SEP stopped while transferring sense data", "ses_enclosure"),
    0x0000010A: ("PL_LOGINFO_DA_SEP_CHKSUM_ERROR_AFTER_STOP", "SEP returned bad checksum after STOP", "ses_enclosure"),
    0x0000010C: ("PL_LOGINFO_DA_SEP_UNSUPPORTED_COMMAND", "SEP does not support CDB opcode", "ses_enclosure"),
}


def decode_lsi_loginfo(loginfo: str) -> dict[str, Any] | None:
    raw = str(loginfo or "").strip().lower().removeprefix("0x")
    if not raw or not re.fullmatch(r"[0-9a-f]+", raw):
        return None
    value = int(raw, 16)
    type_bits = value & 0xF0000000
    origin_bits = value & 0x0F000000
    code_bits = value & 0x00FF0000
    sub_code_bits = value & 0x0000FF00
    sub_detail_bits = value & 0x000000FF
    low_16_bits = value & 0x0000FFFF

    type_label, type_key = LSI_LOGINFO_TYPE_NAMES.get(type_bits, ("Unknown", "unknown"))
    origin_label, origin_key = LSI_SAS_ORIGIN_NAMES.get(origin_bits, ("Unknown", "unknown"))

    code_symbol = None
    code_label = "Unknown code"
    code_family = "controller_terminated_io"
    code_note = ""
    sub_symbol = None
    sub_label = None
    sub_family = None
    detail_symbol = None
    detail_label = None
    detail_family = None
    unparsed = value & 0x00FFFFFF
    decode_path = "generic"

    if type_key == "sas" and origin_key == "physical_layer":
        decode_path = "sas_physical_layer"
        code_symbol, code_label, code_family, code_note = LSI_SAS_PL_CODES.get(
            code_bits,
            (None, "Unknown SAS physical-layer code", "controller_terminated_io", ""),
        )
        unparsed &= ~0x00FF0000
        if code_bits in {0x00010000, 0x00110000, 0x00120000}:
            sub_symbol, sub_label, sub_family = LSI_SAS_PL_SUBCODES.get(
                sub_code_bits,
                (None, None, None),
            )
            if sub_symbol:
                unparsed &= ~0x0000FF00
            if sub_code_bits == 0x00000100 and sub_detail_bits:
                detail_symbol, detail_label, detail_family = LSI_SAS_PL_OPEN_FAILURE_DETAILS.get(
                    sub_detail_bits,
                    (None, None, None),
                )
                if detail_symbol:
                    unparsed &= ~0x000000FF
        elif code_bits == 0x000F0000:
            detail_symbol, detail_label, detail_family = LSI_SAS_PL_CONFIG_ERRORS.get(
                low_16_bits,
                (None, None, None),
            )
            if detail_symbol:
                unparsed &= ~0x0000FFFF
        elif code_bits == 0x00200000:
            detail_symbol, detail_label, detail_family = LSI_SAS_PL_ENCLOSURE_MGMT_ERRORS.get(
                low_16_bits,
                (None, None, None),
            )
            if detail_symbol:
                unparsed &= ~0x0000FFFF
    elif type_key == "sas" and origin_key == "iop":
        decode_path = "sas_iop"
        code_symbol, code_label, code_family, code_note = LSI_SAS_IOP_CODES.get(
            code_bits,
            (None, "Unknown SAS IOP code", "controller_terminated_io", ""),
        )
        unparsed &= ~0x00FF0000
        if code_bits == 0x00030000:
            detail_symbol, detail_label, detail_family = LSI_SAS_IOP_INVALID_PAGE_DETAILS.get(
                low_16_bits,
                (None, None, None),
            )
            if detail_symbol:
                unparsed &= ~0x0000FFFF
    elif type_key == "sas" and origin_key == "integrated_raid":
        decode_path = "sas_integrated_raid"
        code_symbol, code_label, code_family = LSI_SAS_IR_CODES.get(
            value & 0x00FFFFFF,
            (None, "Unknown integrated RAID code", "device_path_exception"),
        )
        if code_symbol:
            unparsed = 0

    family = detail_family or sub_family or code_family
    detail = detail_label or sub_label
    label = f"LSI {type_label}"
    if origin_label != "Unknown":
        label += f" {origin_label}"
    label += f" {code_label}"
    if detail and detail.lower() != code_label.lower():
        label += f": {detail}"

    components = [
        f"type {type_label}",
        f"origin {origin_label}",
        code_symbol or f"code 0x{code_bits:08x}",
    ]
    if sub_symbol:
        components.append(sub_symbol)
    if detail_symbol:
        components.append(detail_symbol)
    if detail_label and detail_label not in components:
        components.append(detail_label)
    if code_note:
        components.append(code_note)
    if unparsed:
        components.append(f"unparsed 0x{unparsed:08x}")
    confidence = "vendor-reference"
    decoder_note = None
    if type_key == "unknown":
        confidence = "unconfirmed"
        decoder_note = "Loginfo type is not in the current local LSI lookup table."
    elif type_key != "sas":
        confidence = "unconfirmed"
        decoder_note = "Loginfo type is known, but the current local decoder only expands SAS loginfo entries."
    elif origin_key == "unknown":
        confidence = "unconfirmed"
        decoder_note = "SAS loginfo origin is not in the current local LSI lookup table."
    elif not code_symbol:
        confidence = "unconfirmed"
        decoder_note = "SAS loginfo code is not in the current local LSI lookup table."
    elif unparsed:
        confidence = "vendor-reference-partial"
        decoder_note = "Reference code matched, but some low-order loginfo bits are not decoded by the current local table."

    decoded = {
        "label": label,
        "family": family,
        "likely_layer": fault_family_likely_layer(family),
        "description": f"Decoded Broadcom/LSI loginfo {raw}: {', '.join(components)}.",
        "loginfo": raw,
        "vendor": "Broadcom/LSI",
        "decode_confidence": confidence,
        "decode_source": "baruch_lsi_decode_loginfo",
        "source_attribution": dict(LSI_LOGINFO_SOURCE),
        "lsi_loginfo": {
            "type": type_key,
            "type_label": type_label,
            "origin": origin_key,
            "origin_label": origin_label,
            "decode_path": decode_path,
            "code": f"0x{code_bits:08x}",
            "code_symbol": code_symbol,
            "code_label": code_label,
            "sub_code": f"0x{sub_code_bits:08x}" if sub_code_bits else None,
            "sub_code_symbol": sub_symbol,
            "sub_code_label": sub_label,
            "detail_code": f"0x{sub_detail_bits:08x}" if sub_detail_bits else None,
            "detail_symbol": detail_symbol,
            "detail_label": detail_label,
            "unparsed": f"0x{unparsed:08x}" if unparsed else None,
        },
    }
    if decoder_note:
        decoded["decoder_note"] = decoder_note
    return decoded
