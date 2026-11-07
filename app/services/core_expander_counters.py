"""CORE SAS expander PHY error counters over SMP (#941): a discovered capability.

The app reads REPORT PHY ERROR LOG through one fixed `camcontrol smpcmd` shape
when the SSH account may run it, and says the counters are not available when
sudo refuses. Nothing here changes an expander: the request bytes are fixed
apart from the PHY number, and the sudo grant admits only that shape.

Sources (FreeBSD releng/13.0, the base of TrueNAS CORE 13.0):
https://github.com/freebsd/freebsd-src/tree/releng/13.0
* sbin/camcontrol/camcontrol.c smpcmd(): `-r len fmt` encodes the request,
  `-R len -` writes the raw response to stdout. Sizes exclude the CRC.
* lib/libcam/scsi_cmdparse.c next_field(): empty field names (`{}`) separate
  hex byte fields, so the whole request is one whitespace-free argument.
* sys/dev/mpr/mpr_sas.c mprsas_action_smpio(): an SMP request sent to a pass
  device goes to that device itself when it is an SMP target, otherwise to its
  parent expander. A pass device of any end device behind an expander therefore
  reaches that expander.

REPORT PHY ERROR LOG (SPL-4 r10 9.4.3.11, Table 336), cross-checked against
https://github.com/doug-gilbert/smp_utils/blob/5ff2d5c638896332041804d0bfa95252304081ea/src/smp_rep_phy_err_log.c
main(): function 0x11, allocated response length 6, request length 2, PHY at
byte 9; 12 request and 28 response bytes without CRC; four big-endian uint32
counters at 12/16/20/24 that saturate at FFFFFFFFh. Rows use the counter keys
of the per-PHY SasPortView model; Linux sas_phy sysfs names map onto them.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

CAMCONTROL = "/sbin/camcontrol"

# One sudo rule for the whole command shape: any CAM pass device, any PHY from
# 00h through FEh, every other request byte fixed. sudo 1.9.10+ treats an
# argument list wrapped in ^...$ as an anchored POSIX ERE (CORE 13.0 ships
# 1.9.12). The literal braces sit in bracket expressions and there is no `\`,
# `,`, `:` or `=`, which the TrueNAS middleware would backslash-escape when it
# writes sudoers. No other SMP function, request or response length, extra
# argument, stdin request or option can match. sudo joins argv with spaces
# before matching; re-splitting this string at its spaces either sends the
# same request or leaves camcontrol without its -R option, which it refuses
# before sending anything (smpcmd() checks both -r and -R first).
# Things that must move together: CONTRIBUTING.md.
# The PHY group starts after a digit, not after `]`, so the rule never contains
# `](`, which Markdown link checks read as a link in the generated docs.
SMP_PHY_ERROR_LOG_SUDO_GRANT = (
    f"{CAMCONTROL} ^smpcmd pass[0-9]+ -r 12 "
    "40[{][}]11[{][}]06[{][}]02[{][}]00[{][}]00[{][}]00[{][}]00[{][}]00"
    "([{][}][0-9a-e][0-9a-f]|[{][}]f[0-9a-e])"
    "[{][}]00[{][}]00 -R 28 -$"
)

SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX = "smp phy error log "
SMP_PHY_ERROR_LOG_MARKER = "tnj-smp-phy-error-log"
# Per call and per refresh bounds. One call that gives no answer at all stops
# that expander's batch, so a hung expander costs about one timeout.
SMP_CALL_TIMEOUT_SECONDS = 2
SMP_MAX_TARGETS_PER_REFRESH = 8
# A refused capability is asked again only after this long (or after the app
# restarts), not on every refresh.
SMP_CAPABILITY_RETRY_SECONDS = 6 * 60 * 60

PERMISSION_DENIED_MESSAGE = "Expander counters not available (permission not granted)."
NO_ROUTE_MESSAGE = "Expander counters not available (no CAM pass device reaches this expander)."
NO_ANSWER_MESSAGE = "Expander counters not available (the expander did not return a readable error log)."
NOT_READ_MESSAGE = "Expander counters not read yet."

CAPABILITY_GRANTED = "granted"
CAPABILITY_DENIED = "permission_denied"

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

_PASS_DEVICE = re.compile(r"pass[0-9]+")
_SAS_ADDRESS = re.compile(r"[0-9a-f]{16}")
_PERMISSION_TEXT = ("a password is required", "is not allowed to execute", "not in the sudoers", "sudo:")


# --- command shape -----------------------------------------------------------

def _pass_device(device: object) -> str:
    if not isinstance(device, str) or _PASS_DEVICE.fullmatch(device) is None:
        raise ValueError("A literal CAM passN target is required; no paths/options")
    return device


def _phy_number(phy: object) -> int:
    if type(phy) is not int or not 0 <= phy <= 254:
        raise ValueError("A literal phy integer from 0 through 254 is required")
    return phy


def phy_error_log_request_format(phy: int) -> str:
    """The libcam constant format for one REPORT PHY ERROR LOG request."""
    request = bytes((0x40, SMP_FUNCTION_REPORT_PHY_ERROR_LOG, 6, 2, 0, 0, 0, 0, 0, _phy_number(phy), 0, 0))
    return "{}".join(f"{byte:02x}" for byte in request)


def phy_error_log_argv(device: str, phy: int) -> tuple[str, ...]:
    """The exact argv sudo admits for one PHY of the expander behind ``device``."""
    device = _pass_device(device)
    fmt = phy_error_log_request_format(phy)
    return (CAMCONTROL, "smpcmd", device, "-r", "12", fmt, "-R", "28", "-")


@dataclass(frozen=True)
class SmpTarget:
    """One expander and the CAM pass device whose SMP requests reach it."""

    unit: int
    expander_sas_address: str
    device: str
    num_phys: int

    @property
    def identity(self) -> str:
        return f"{self.unit}:{self.expander_sas_address}:{self.device}"


def _hex_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{1,8}", text):
        return None
    return int(text, 16)


def parse_camcontrol_pass_devices(output: str) -> dict[tuple[str, int], str]:
    """Map (controller, CAM target) to the pass device on that target.

    A controller/target pair that lists two different pass devices is dropped:
    it cannot name one route.
    """
    routes: dict[tuple[str, int], str] = {}
    conflicts: set[tuple[str, int]] = set()
    controller: str | None = None
    for line in str(output or "").splitlines():
        stripped = line.strip()
        bus = re.match(r"^scbus-?\d+\s+on\s+(?P<controller>[A-Za-z]+\d+)\s+bus\s+\d+:", stripped)
        if bus:
            controller = bus.group("controller")
            continue
        match = re.search(r"\btarget\s+(?P<target>\d+)\s+lun\s+\S+\s+\((?P<devices>[^)]*)\)", stripped)
        if not match or controller is None:
            continue
        devices = [item.strip() for item in match.group("devices").split(",")]
        passes = [item for item in devices if _PASS_DEVICE.fullmatch(item)]
        key = (controller, int(match.group("target")))
        for device in passes:
            if key in conflicts:
                break
            if routes.get(key, device) != device:
                routes.pop(key, None)
                conflicts.add(key)
                break
            routes[key] = device
    return routes


def discover_smp_targets(outputs: Mapping[str, str]) -> list[SmpTarget]:
    """Find each expander the CORE collector saw and a pass device that reaches it.

    ``outputs`` is keyed by canonical command (``mprutil -u N show expanders``,
    ``mprutil -u N show devices``, ``camcontrol devlist -v``). Only commands the
    app already runs are used, so discovery needs no extra grant. A device row
    whose parent handle is the expander, whose handle an expander PHY reports
    as attached, or the expander's own row ties the expander to a CAM target;
    CAM maps that target to a pass device. The lowest such target wins so the
    choice is stable between refreshes.
    """
    # Imported here: the parsers module imports this one for its output key.
    from app.services.sas_fabric_parsers import parse_mpr_devices, parse_mpr_expanders

    routes = parse_camcontrol_pass_devices(outputs.get("camcontrol devlist -v", ""))
    targets: list[SmpTarget] = []
    units = sorted(
        int(match.group("unit"))
        for command in outputs
        if (match := re.fullmatch(r"mprutil -u (?P<unit>\d+) show expanders", command))
    )
    for unit in units:
        controller = f"mpr{unit}"
        devices = parse_mpr_devices(outputs.get(f"mprutil -u {unit} show devices", ""))
        for expander in parse_mpr_expanders(outputs.get(f"mprutil -u {unit} show expanders", "")):
            address = str(expander.get("sas_address") or "").lower()
            handle = _hex_int(expander.get("dev_handle"))
            try:
                num_phys = int(str(expander.get("num_phys") or ""))
            except ValueError:
                continue
            if not _SAS_ADDRESS.fullmatch(address) or handle is None or not 1 <= num_phys <= 255:
                continue
            attached = {_hex_int(phy.get("dev_handle")) for phy in expander.get("phys") or []} - {None, 0}
            candidates: list[tuple[int, str]] = []
            for device in devices:
                own = str(device.get("sas_address") or "").lower() == address
                child = _hex_int(device.get("parent")) == handle or _hex_int(device.get("handle")) in attached
                # A cascaded expander's own SMP target would answer for itself.
                if child and not own and "SMP" in str(device.get("device") or "").upper():
                    continue
                target_text = str(device.get("target") or "")
                if not (own or child) or not target_text.isdigit():
                    continue
                route = routes.get((controller, int(target_text)))
                if route:
                    candidates.append((int(target_text), route))
            if candidates:
                targets.append(SmpTarget(unit, address, min(candidates)[1], num_phys))
    targets.sort(key=lambda item: (item.unit, item.expander_sas_address))
    return targets[:SMP_MAX_TARGETS_PER_REFRESH]


def build_phy_error_log_command(target: SmpTarget, phys: Sequence[int] | None = None) -> str:
    """One POSIX sh command line that reads ``phys`` of ``target`` in order.

    Each call is the exact granted argv with a literal PHY, bounded by
    timeout(1). Both output streams are hex-dumped so the raw frame survives
    text transport and sudo's or camcontrol's message stays with its PHY. A
    call that prints nothing (timed out) ends the batch for this expander.
    """
    device = _pass_device(target.device)
    if not _SAS_ADDRESS.fullmatch(target.expander_sas_address) or type(target.unit) is not int or target.unit < 0:
        raise ValueError("A validated expander identity is required")
    selected = list(range(min(target.num_phys, 255))) if phys is None else [_phy_number(phy) for phy in phys]
    if not selected:
        raise ValueError("At least one PHY is required")
    template = phy_error_log_request_format(0).split("{}")
    template[9] = "$p"
    fmt = "{}".join(template)
    phy_words = " ".join(f"{phy:02x}" for phy in selected)
    # The PHY range keeps a one-PHY permission probe and the full read of the
    # same expander apart as two outputs.
    header = f"{SMP_PHY_ERROR_LOG_MARKER}:{target.identity}:{selected[0]:02x}-{selected[-1]:02x}"
    return (
        f"echo {header} ; "
        f"for p in {phy_words}; do echo \"@phy=$p\"; "
        f"o=$(/usr/bin/timeout {SMP_CALL_TIMEOUT_SECONDS} sudo -n {CAMCONTROL} smpcmd {device} "
        f"-r 12 \"{fmt}\" -R 28 - 2>&1 | /usr/bin/od -An -tx1 -v); "
        "if [ -z \"$o\" ]; then echo @no-response; break; fi; "
        "printf '%s\\n' \"$o\"; done; echo @end"
    )


def smp_output_key(command_or_identity: str) -> str | None:
    """Canonical output key for a built command, or None for anything else."""
    first = str(command_or_identity or "").split(" ", 2)
    if len(first) >= 2 and first[0] == "echo" and first[1].startswith(f"{SMP_PHY_ERROR_LOG_MARKER}:"):
        return SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX + first[1][len(SMP_PHY_ERROR_LOG_MARKER) + 1:]
    return None


# --- response decoding -------------------------------------------------------

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

    The function result is checked before length because an expander may
    return a header-only frame for a failed function; that is reported as its
    result, never decoded as zeros. Only SAS-2+ frames with RESPONSE LENGTH 06h
    are admitted. Reserved bytes are ignored; the change count is not returned.
    """
    _phy_number(phy)
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


def _hex_dump_bytes(lines: Sequence[str]) -> bytes | None:
    values: list[int] = []
    for line in lines:
        for word in line.split():
            if not re.fullmatch(r"[0-9a-f]{2}", word):
                return None
            values.append(int(word, 16))
    return bytes(values)


def _unknown_row(phy: int, state: str, reason: str) -> dict[str, Any]:
    return {"phy": phy, **{field: None for field in COUNTER_FIELDS}, "counter_state": state, "counter_reason": reason}


def classify_phy_output(phy: int, payload: bytes | None) -> dict[str, Any]:
    """One PHY's result: measured counters, refused permission, or unknown.

    Unknown counters are None, never zero.
    """
    if payload is None:
        return _unknown_row(phy, "unknown", "unreadable_output")
    if not payload:
        return _unknown_row(phy, "unknown", "no_response")
    if len(payload) == PHY_ERROR_LOG_FRAME_BYTES and payload[0] == SMP_FRAME_TYPE_RESPONSE:
        try:
            row: dict[str, Any] = dict(decode_phy_error_log_response(payload, phy=phy))
        except SmpResponseError as exc:
            return _unknown_row(phy, "unknown", exc.reason)
        row["counter_state"] = "measured"
        row["counter_reason"] = None
        return row
    text = payload.decode("utf-8", errors="replace").lower()
    if any(needle in text for needle in _PERMISSION_TEXT):
        return _unknown_row(phy, CAPABILITY_DENIED, "sudo_refused")
    if len(payload) >= 4 and payload[0] == SMP_FRAME_TYPE_RESPONSE:
        try:
            decode_phy_error_log_response(payload, phy=phy)
        except SmpResponseError as exc:
            return _unknown_row(phy, "unknown", exc.reason)
    return _unknown_row(phy, "unknown", "command_failed")


@dataclass
class PhyErrorLogResult:
    target_identity: str | None
    rows: list[dict[str, Any]]

    @property
    def unit(self) -> int | None:
        parts = (self.target_identity or "").split(":")
        return int(parts[0]) if len(parts) == 3 and parts[0].isdigit() else None

    @property
    def expander_sas_address(self) -> str | None:
        parts = (self.target_identity or "").split(":")
        return parts[1] if len(parts) == 3 else None

    @property
    def capability(self) -> str | None:
        """granted when sudo ran camcontrol for any PHY, refused when it never did."""
        states = {row["counter_state"] for row in self.rows}
        not_run = {"sudo_refused", "no_response", "incomplete_output"}
        if "measured" in states or any(row["counter_reason"] not in not_run for row in self.rows):
            return CAPABILITY_GRANTED
        if CAPABILITY_DENIED in states:
            return CAPABILITY_DENIED
        return None


def parse_phy_error_log_output(stdout: str) -> PhyErrorLogResult:
    """Parse the output of :func:`build_phy_error_log_command`."""
    identity: str | None = None
    rows: list[dict[str, Any]] = []
    current: int | None = None
    lines: list[str] = []

    def flush(no_response: bool = False) -> None:
        nonlocal current, lines
        if current is not None:
            payload = b"" if no_response else _hex_dump_bytes(lines)
            rows.append(classify_phy_output(current, payload))
        current, lines = None, []

    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if line.startswith(f"{SMP_PHY_ERROR_LOG_MARKER}:") and identity is None:
            parts = line[len(SMP_PHY_ERROR_LOG_MARKER) + 1:].split(":")
            if (
                len(parts) == 4 and parts[0].isdigit() and _SAS_ADDRESS.fullmatch(parts[1])
                and _PASS_DEVICE.fullmatch(parts[2]) and re.fullmatch(r"[0-9a-f]{2}-[0-9a-f]{2}", parts[3])
            ):
                identity = ":".join(parts[:3])
            continue
        phy_marker = re.fullmatch(r"@phy=([0-9a-f]{2})", line)
        if phy_marker:
            flush()
            phy = int(phy_marker.group(1), 16)
            current = phy if phy <= 254 and all(row["phy"] != phy for row in rows) else None
            continue
        if line == "@no-response":
            flush(no_response=True)
            continue
        if line == "@end":
            flush()
            break
        if current is not None and line:
            lines.append(line)
    else:
        # No end marker: the batch was cut short; the open PHY is unknown.
        if current is not None:
            rows.append(_unknown_row(current, "unknown", "incomplete_output"))
    return PhyErrorLogResult(identity, rows)


def counter_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    measured = [row for row in rows if row.get("counter_state") == "measured"]
    return {
        "measured_phys": len(measured),
        "unknown_phys": len(rows) - len(measured),
        "phys_with_errors": sum(
            1 for row in measured if any(int(row.get(field) or 0) > 0 for field in COUNTER_FIELDS)
        ),
    }


# --- per-phy deltas ----------------------------------------------------------

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
