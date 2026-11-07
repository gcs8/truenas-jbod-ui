"""Inactive, pure #941 command/policy draft; NOT a collector or grant installer.

Only FreeBSD releng/13.2 source semantics are qualified, not any CORE host:
https://github.com/freebsd/freebsd-src/tree/f0cf0b8266eef39b13917f7bed808daf6d6a2d3e
* sbin/camcontrol/camcontrol.c: smpcmd(), smpphylist()
* sbin/camcontrol/camcontrol.8: smpcmd request/response exclude CRC
* lib/libcam/scsi_cmdparse.c: next_field(), do_encode(); hex byte fields
  separated by empty field names ({}) need no whitespace or variable arguments.
* lib/libcam/cam_cdbparse.3: constant hex values default to single bytes.

REPORT PHY ERROR LOG request layout and response counters:
https://github.com/doug-gilbert/smp_utils/blob/5ff2d5c638896332041804d0bfa95252304081ea/src/smp_rep_phy_err_log.c
main(): function 0x11, allocated response length 6, request length 2, phy byte 9;
12 request / 28 response bytes excluding CRC. smp_utils is NOT required or
admitted by this draft. No installed binary or host transport is verified here.

The response decoder and delta helper below are equally inert: pure functions
over caller-supplied bytes and rows, with no call site, collector, history or
UI consumer. Response layout is SPL-4 r10 9.4.3.11 Table 336, cross-checked
against the smp_utils main() offsets above (change count at 4, phy at 9,
four big-endian uint32 counters at 12/16/20/24). Rows use the counter keys of
the existing per-phy SasPortView model; the Linux sas_phy sysfs names are
mapped onto the same keys (scsi_transport_sas.c sas_phy_linkerror_attr, "%u"):
https://github.com/torvalds/linux/blob/v6.8/drivers/scsi/scsi_transport_sas.c
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


CAMCONTROL = "/sbin/camcontrol"


def _pass_device(device: object) -> str:
    if not isinstance(device, str) or re.fullmatch(r"pass[0-9]+", device) is None:
        raise ValueError("A literal CAM passN target is required; no paths/options")
    return device


def phy_error_log_argv(device: str, phy: int) -> tuple[str, ...]:
    """Build only a source-derived read request; never execute it.

    Empty field names keep the entire constant format in one whitespace-free
    argv token. No stdin request, arbitrary opcode, reset or clear is possible.
    This builder does not establish that device is an SMP target on a host.
    """
    device = _pass_device(device)
    if type(phy) is not int or not 0 <= phy <= 254:
        raise ValueError("A literal phy integer from 0 through 254 is required")
    request = bytes((0x40, 0x11, 6, 2, 0, 0, 0, 0, 0, phy, 0, 0))
    fmt = "{}".join(f"{byte:02x}" for byte in request)
    return (CAMCONTROL, "smpcmd", device, "-r", "12", fmt, "-R", "28", "-")


def draft_readonly_policy(
    argv: tuple[str, ...] | list[str],
    *,
    freebsd_release: str | None = None,
    tool_path: str | None = None,
    sudo_version: str | None = None,
    opt_in: bool = False,
) -> dict[str, object]:
    """Return disabled data, or an exact inactive candidate for supplied evidence.

    Evidence is caller-supplied, not discovered/attested. An opt-in requests a
    SOURCE DRAFT, never authorizes execution/application. Only the inspected
    13.2 source family and sudo 1.9.10+ in the 1.9 series are admitted. An actual
    CORE build/source match, binary path, SMP-capable target/HBA, permissions,
    middleware and owner approval remain independent gates. Do not feed this
    result to bootstrap or treat it as an installed sudo rule.
    """
    result: dict[str, object] = {
        "state": "disabled",
        "enabled": False,
        "host_validated": False,
        "capability": None,
        "argv": (),
        "candidate_grant": None,
    }
    if type(opt_in) is not bool:
        raise ValueError("opt_in must be an explicit bool")
    if not opt_in:
        return result
    if freebsd_release != "13.2" or tool_path != CAMCONTROL:
        raise ValueError("Missing or unqualified FreeBSD release/tool evidence")
    version = re.fullmatch(r"1[.]9[.]([0-9]+)(p[0-9]+)?", sudo_version or "")
    if version is None or int(version[1]) < 10:
        raise ValueError("Explicit sudo 1.9.10+ evidence in the 1.9 series required")
    if not isinstance(argv, (tuple, list)) or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("An exact argv sequence, not a shell command, is required")
    command = tuple(argv)
    capability = None
    if len(command) == 3 and command[:2] == (CAMCONTROL, "smpphylist"):
        _pass_device(command[2])
        capability = "phy_topology_only"
    elif len(command) == 9 and command[:2] == (CAMCONTROL, "smpcmd"):
        # Recognize only our canonical constant format, not the libcam grammar.
        fields = command[5].split("{}")
        if len(fields) == 12 and re.fullmatch(r"[0-9a-f][0-9a-f]", fields[9]):
            phy = int(fields[9], 16)
            if command == phy_error_log_argv(command[2], phy):
                capability = "expander_phy_error_log"
    if capability is None:
        raise ValueError("Unsupported command: only exact read-only draft shapes admitted")
    # Exact target/phy, anchored arguments, no sudo wildcard. Literal braces
    # use ERE character classes rather than backslashes the CORE middleware
    # would escape. No whitespace inside any argv token avoids re-grouping.
    arguments = " ".join(command[1:]).replace("{", "[{]").replace("}", "[}]")
    result.update(
        state="inactive_draft",
        capability=capability,
        argv=command,
        candidate_grant=f"{CAMCONTROL} ^{arguments}$",
    )
    return result


# --- REPORT PHY ERROR LOG response decoding (inert) -------------------------

SMP_FRAME_TYPE_RESPONSE = 0x41
SMP_FUNCTION_REPORT_PHY_ERROR_LOG = 0x11
# Response length in dwords after the 4-byte header, excluding CRC (SAS-2+).
PHY_ERROR_LOG_RESPONSE_LENGTH = 6
PHY_ERROR_LOG_FRAME_BYTES = 4 + 4 * PHY_ERROR_LOG_RESPONSE_LENGTH  # 28, no CRC
COUNTER_SATURATED = 0xFFFFFFFF

COUNTER_FIELDS = (
    "invalid_dword_count",
    "running_disparity_error_count",
    "loss_of_dword_synchronization_count",
    "phy_reset_problem_count",
)
_COUNTER_OFFSETS = dict(zip(COUNTER_FIELDS, (12, 16, 20, 24)))
# Linux /sys/class/sas_phy/<phy>/<attribute> name -> shared per-phy row key.
SYSFS_COUNTER_ATTRIBUTES = {
    "invalid_dword_count": "invalid_dword_count",
    "running_disparity_error_count": "running_disparity_error_count",
    "loss_of_dword_sync_count": "loss_of_dword_synchronization_count",
    "phy_reset_problem_count": "phy_reset_problem_count",
}


class SmpResponseError(ValueError):
    """A REPORT PHY ERROR LOG response that must not be read as counters.

    ``reason`` is one of frame_type_error, frame_length, frame_type, function,
    function_result, response_length or phy_identifier.
    """

    def __init__(self, reason: str, message: str, *, function_result: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.function_result = function_result


class CounterSampleError(ValueError):
    """A per-phy counter sample that cannot be compared safely."""


def decode_phy_error_log_response(frame: object, *, phy: int) -> dict[str, int]:
    """Decode one CRC-excluded response for the requested phy, or raise.

    Validation order puts the function result before length checks because an
    expander may return a header-only frame for a failed function; that is
    reported as its result, never decoded as zeros. Only SAS-2+ frames with
    RESPONSE LENGTH 06h are admitted; the SAS-1.1 zero-length form is refused.
    Reserved bytes are ignored and the expander change count is not returned.
    """
    if type(phy) is not int or not 0 <= phy <= 254:
        raise ValueError("A literal requested phy integer from 0 through 254 is required")
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise SmpResponseError("frame_type_error", "Response must be bytes, not decoded or text data")
    data = bytes(frame)
    if len(data) < 4:
        raise SmpResponseError("frame_length", f"Response has {len(data)} bytes; no SMP header")
    if data[0] != SMP_FRAME_TYPE_RESPONSE:
        raise SmpResponseError("frame_type", f"SMP frame type {data[0]:02X}h is not a response (41h)")
    if data[1] != SMP_FUNCTION_REPORT_PHY_ERROR_LOG:
        raise SmpResponseError("function", f"Function {data[1]:02X}h is not REPORT PHY ERROR LOG (11h)")
    if data[2] != 0:
        raise SmpResponseError(
            "function_result",
            f"Function result {data[2]:02X}h is not SMP FUNCTION ACCEPTED",
            function_result=data[2],
        )
    if len(data) != PHY_ERROR_LOG_FRAME_BYTES:
        raise SmpResponseError(
            "frame_length",
            f"Response has {len(data)} bytes; exactly {PHY_ERROR_LOG_FRAME_BYTES} without CRC are required",
        )
    if data[3] != PHY_ERROR_LOG_RESPONSE_LENGTH:
        raise SmpResponseError(
            "response_length",
            f"Response length {data[3]:02X}h is not {PHY_ERROR_LOG_RESPONSE_LENGTH:02X}h",
        )
    if data[9] != phy:
        raise SmpResponseError("phy_identifier", f"Response phy {data[9]} does not match requested phy {phy}")
    row: dict[str, int] = {"phy": phy}
    for field, offset in _COUNTER_OFFSETS.items():
        # Saturated counters stay FFFFFFFFh; they never wrap.
        row[field] = int.from_bytes(data[offset:offset + 4], "big")
    return row


def _counter_sample(sample: object, label: str) -> dict[int, dict[str, int | None]]:
    if isinstance(sample, (str, bytes, bytearray, Mapping)) or not isinstance(sample, Sequence):
        raise CounterSampleError(f"{label} sample must be a sequence of per-phy rows")
    rows: dict[int, dict[str, int | None]] = {}
    for row in sample:
        if not isinstance(row, Mapping):
            raise CounterSampleError(f"{label} sample rows must be mappings")
        phy = row.get("phy")
        if type(phy) is not int or not 0 <= phy <= 255:
            raise CounterSampleError(f"{label} sample has a missing or malformed phy identifier")
        if phy in rows:
            raise CounterSampleError(f"{label} sample repeats phy {phy}")
        counters: dict[str, int | None] = {}
        for field in COUNTER_FIELDS:
            if field not in row:
                raise CounterSampleError(f"{label} phy {phy} omits {field}; use None when unreported")
            value = row[field]
            if value is not None and (type(value) is not int or not 0 <= value <= COUNTER_SATURATED):
                raise CounterSampleError(f"{label} phy {phy} {field} is not an unsigned 32-bit count")
            counters[field] = value
        rows[phy] = counters
    return rows


def phy_counter_deltas(previous: object, current: object) -> list[dict[str, object]]:
    """Return per-phy increments between two samples of one expander.

    Each row keeps the shared per-phy keys, with a numeric increment or None,
    plus ``counter_states`` per counter:
    * ``delta``: current >= previous, increment is current - previous;
    * ``reset``: current < previous, so the counter was cleared; the value is
      the count since the reset (a lower bound), never a negative number;
    * ``saturated``: current is FFFFFFFFh, increment unknown (None);
    * ``unavailable``: current value not reported (None);
    * ``no_baseline``: previous value or phy not reported (None).
    Phys present only in ``previous`` are dropped. Rows are ordered by phy.
    The caller must supply samples from the same expander in time order.
    """
    before = _counter_sample(previous, "previous")
    after = _counter_sample(current, "current")
    result: list[dict[str, object]] = []
    for phy in sorted(after):
        row: dict[str, object] = {"phy": phy}
        states: dict[str, str] = {}
        baseline = before.get(phy)
        for field in COUNTER_FIELDS:
            now = after[phy][field]
            then = None if baseline is None else baseline[field]
            if now is None:
                states[field], row[field] = "unavailable", None
            elif now == COUNTER_SATURATED:
                states[field], row[field] = "saturated", None
            elif then is None:
                states[field], row[field] = "no_baseline", None
            elif now < then:
                states[field], row[field] = "reset", now
            else:
                states[field], row[field] = "delta", now - then
        row["counter_states"] = states
        result.append(row)
    return result
