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
from collections import Counter
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
# that chunk (SMP_PHYS_PER_COMMAND PHYs), and the collector sends an
# expander's chunks one planning round at a time, so the first chunk that does
# not answer ends that expander's read for the refresh: a hung expander costs
# about one call timeout per read, not one per chunk. timeout(1) sends SIGTERM
# (sudo relays it to camcontrol), then SIGKILL after the grace.
SMP_CALL_TIMEOUT_SECONDS = 2
SMP_CALL_KILL_AFTER_SECONDS = 1
# Route discovery is complete; these budgets only limit what one refresh
# reads. Larger fabrics are read in turn (see select_counter_reads()).
SMP_MAX_TARGETS_PER_REFRESH = 8
# Hard ceiling on sudo camcontrol calls in one refresh, forced or not: the
# one-PHY permission probe plus every full read. One full read of the largest
# expander (255 PHYs) always fits alongside the probe.
SMP_MAX_CALLS_PER_REFRESH = 512
# One SSH command reads at most this many PHYs of one expander. The SSH
# command timeout (SSHConfig.timeout_seconds, 15 s by default) covers a whole
# command, so a chunk must fit inside it: 4 x (2 s + 1 s kill grace) = 12 s
# when every call is killed. A chunk that still runs past the deadline loses
# only its own PHYs (#1088 review). Calls, not chunks, count against
# SMP_MAX_CALLS_PER_REFRESH.
SMP_PHYS_PER_COMMAND = 4
# Hard ceiling on counter SSH commands (round trips) in one refresh: the
# probe, plus at most one partial chunk per expander on top of the calls
# divided into full chunks. select_counter_reads() enforces it too.
SMP_MAX_COMMANDS_PER_REFRESH = (
    1 + SMP_MAX_TARGETS_PER_REFRESH + (SMP_MAX_CALLS_PER_REFRESH - 1) // SMP_PHYS_PER_COMMAND
)
# Output cap for one counter command, enforced by SSHProbe instead of its
# general 4 MiB limit. A chunk's real answer is a few hundred bytes per PHY
# (a 28-byte frame or a short error, hex-dumped by od), so 64 KiB is ample,
# and a whole refresh retains at most 136 x 64 KiB = 8.5 MiB of counter output
# whatever the server sends (#1088 security review).
SMP_MAX_OUTPUT_BYTES_PER_COMMAND = 64 * 1024
# Admissible output of one chunk: a short header plus at most this much per
# PHY (a hex-dumped 28-byte frame is about 100 bytes, a hex-dumped camcontrol
# or sudo message a few hundred). Larger output is rejected unparsed, and its
# PHYs read as an error (#1088 security review).
SMP_OUTPUT_HEADER_BYTES = 256
SMP_MAX_OUTPUT_BYTES_PER_PHY = 4096
# What the collector keeps between refreshes: decoded, validated rows only,
# never raw output. Each row has a fixed shape (PHY, four bounded counters,
# a state and a reason token from fixed sets), an expander keeps at most one
# row per PHY (255), and all kept readings together hold at most this many
# expanders and PHY rows; the least recently read are dropped first and read
# as not read until their next turn (#1088 security review).
SMP_MAX_RETAINED_EXPANDERS = 64
SMP_MAX_RETAINED_PHYS = 4096
# One expander's counters are read in full at most once per this interval,
# whatever asked for the refresh (the Refresh button and ?force=true too).
# They are cumulative error counts, so a reading minutes old is current
# enough; inside the interval the last reading is shown with its read time.
SMP_COUNTER_READ_INTERVAL_SECONDS = 15 * 60
# A refused capability is asked again only after this long (or after the app
# restarts, or with one probe call on a forced refresh), not on every refresh.
SMP_CAPABILITY_RETRY_SECONDS = 6 * 60 * 60

PERMISSION_DENIED_MESSAGE = "Expander counters not available (permission not granted)."
NO_ROUTE_MESSAGE = "Expander counters not available (no CAM pass device reaches this expander)."
NO_ANSWER_MESSAGE = "Expander counters not available (the expander did not return a readable error log)."
NOT_READ_MESSAGE = "Expander counters not read yet."
HUNG_MESSAGE = (
    "Expander counters not available (the expander did not answer; its read stopped "
    "and is tried again after the read interval)."
)
REJECTED_OUTPUT_MESSAGE = (
    "Expander counters not available (the expander's answer was malformed or too large and was discarded)."
)
# Why a read stopped before its last chunk, as shown after "read stopped at PHY N".
STOP_REASON_LABELS = {
    "no_response": "no answer",
    "rejected_output": "answer rejected",
    "command_failed": "command failed",
    "permission_denied": "permission not granted",
}
READ_BUDGET_MESSAGE = (
    f"Expander counters not read this refresh (read budget of {SMP_MAX_TARGETS_PER_REFRESH} expanders "
    f"and {SMP_MAX_CALLS_PER_REFRESH} PHY reads per refresh; expanders are read in turn)."
)
READ_BUDGET_REASON = "read_budget"

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
_LOGGED_IDENTITY = re.compile(rf"({SMP_PHY_ERROR_LOG_MARKER}:[0-9]+:)([0-9a-f]{{4}})[0-9a-f]{{8}}([0-9a-f]{{4}})(?=:)")
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


def mask_smp_identity(text: str) -> str:
    """Shorten expander SAS addresses in counter-read markers for logs.

    Same first-four...last-four form the snapshot export uses for partial
    identifiers; the unit, pass device and PHY range stay readable.
    """
    return _LOGGED_IDENTITY.sub(r"\1\2...\3", str(text or ""))


def discover_smp_targets(outputs: Mapping[str, str]) -> list[SmpTarget]:
    """Find each expander the CORE collector saw and a pass device that reaches it.

    ``outputs`` is keyed by canonical command (``mprutil -u N show expanders``,
    ``mprutil -u N show devices``, ``camcontrol devlist -v``). Only commands the
    app already runs are used, so discovery needs no extra grant. A device row
    whose parent handle is the expander, whose handle an expander PHY reports
    as attached, or the expander's own row ties the expander to a CAM target;
    CAM maps that target to a pass device. The lowest such target wins so the
    choice is stable between refreshes.

    The kernel sends an end device's SMP request to that device's current
    parent, so a populated parent is authoritative. A device whose parent names
    one expander while another expander's PHY table lists it, or whose missing
    parent leaves two expanders claiming it, is no route at all: the tables
    disagree (a topology change or a reused handle) and the read could reach
    the wrong expander. Every expander with a route is returned; the per
    refresh read budget is applied by select_counter_reads(), not here.
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
        targets.extend(_unit_smp_targets(
            f"mpr{unit}",
            unit,
            parse_mpr_expanders(outputs.get(f"mprutil -u {unit} show expanders", "")),
            parse_mpr_devices(outputs.get(f"mprutil -u {unit} show devices", "")),
            routes,
        ))
    targets.sort(key=lambda item: (item.unit, item.expander_sas_address))
    return targets


def _unit_smp_targets(
    controller: str,
    unit: int,
    expanders: Sequence[Mapping[str, Any]],
    devices: Sequence[Mapping[str, Any]],
    routes: Mapping[tuple[str, int], str],
) -> list[SmpTarget]:
    """Routes for one controller's expanders, with work linear in the rows.

    Rows are indexed once by handle, parent and address, and only the first
    255 PHY rows of an expander are read, so a large or hostile listing
    cannot make correlation quadratic. Two expander rows with one handle or
    address, or two device rows with one handle, cannot name one route and
    are dropped (fail closed).
    """
    expander_rows: list[tuple[Mapping[str, Any], str, int | None, list[int]]] = []
    for expander in expanders:
        attached = [
            handle for phy in list(expander.get("phys") or [])[:255]
            if (handle := _hex_int(phy.get("dev_handle")))
        ]
        expander_rows.append(
            (expander, str(expander.get("sas_address") or "").lower(), _hex_int(expander.get("dev_handle")), attached)
        )
    # Device handle -> the expanders whose PHY table lists it as attached.
    claimed_by: dict[int, set[int]] = {}
    for _expander, _address, expander_handle, attached in expander_rows:
        if expander_handle is not None:
            for attached_handle in attached:
                claimed_by.setdefault(attached_handle, set()).add(expander_handle)
    handle_rows = Counter(handle for _e, _a, handle, _p in expander_rows if handle is not None)
    address_rows = Counter(address for _e, address, _h, _p in expander_rows)

    rows: list[tuple[str, int | None, int | None, Mapping[str, Any]]] = []
    for device in devices:
        rows.append((
            str(device.get("sas_address") or "").lower(),
            _hex_int(device.get("parent")) or None,
            _hex_int(device.get("handle")),
            device,
        ))
    device_handle_rows = Counter(handle for _a, _p, handle, _d in rows if handle is not None)
    by_address: dict[str, list[int]] = {}
    by_parent: dict[int, list[int]] = {}
    by_handle: dict[int, int] = {}
    for index, (address, parent, handle, _device) in enumerate(rows):
        by_address.setdefault(address, []).append(index)
        if parent is not None:
            by_parent.setdefault(parent, []).append(index)
        if handle is not None and device_handle_rows[handle] == 1:
            by_handle[handle] = index

    targets: list[SmpTarget] = []
    for expander, address, handle, attached in expander_rows:
        try:
            num_phys = int(str(expander.get("num_phys") or ""))
        except ValueError:
            continue
        if not _SAS_ADDRESS.fullmatch(address) or handle is None or not 1 <= num_phys <= 255:
            continue
        if handle_rows[handle] > 1 or address_rows[address] > 1:
            continue
        indexes = {*by_address.get(address, ()), *by_parent.get(handle, ())}
        indexes.update(by_handle[item] for item in attached if item in by_handle)
        attached_set = set(attached)
        candidates: list[tuple[int, str]] = []
        for index in indexes:
            device_address, parent, device_handle, device = rows[index]
            if device_handle is not None and device_handle_rows[device_handle] > 1:
                continue
            own = device_address == address
            child = parent == handle or device_handle in attached_set
            # A cascaded expander's own SMP target would answer for itself.
            if child and not own and "SMP" in str(device.get("device") or "").upper():
                continue
            claims = claimed_by.get(device_handle, set()) if device_handle else set()
            if child and not own and (
                (parent is not None and claims - {parent}) or (parent is None and len(claims) > 1)
            ):
                # Contradictory parent/attachment evidence: fail closed.
                continue
            target_text = str(device.get("target") or "")
            if not (own or child) or not target_text.isdigit():
                continue
            route = routes.get((controller, int(target_text)))
            if route:
                candidates.append((int(target_text), route))
        if candidates:
            targets.append(SmpTarget(unit, address, min(candidates)[1], num_phys))
    return targets


def select_counter_reads(
    targets: Sequence[SmpTarget],
    last_read: Mapping[tuple[int, str], float],
    now: float,
    call_budget: int,
    command_budget: int = SMP_MAX_COMMANDS_PER_REFRESH,
) -> list[SmpTarget]:
    """The expanders one refresh reads in full.

    An expander read less than SMP_COUNTER_READ_INTERVAL_SECONDS ago (by the
    monotonic times in ``last_read``) is not due, whatever asked for the
    refresh. Due expanders go least recently read first (never read first,
    then discovery order), up to SMP_MAX_TARGETS_PER_REFRESH expanders,
    ``call_budget`` PHY calls and ``command_budget`` SSH commands (chunks of
    SMP_PHYS_PER_COMMAND PHYs), so every expander gets its turn. The result
    keeps discovery order.
    """
    order = {target.identity: index for index, target in enumerate(targets)}
    due = [
        target for target in targets
        if now - last_read.get((target.unit, target.expander_sas_address), float("-inf"))
        >= SMP_COUNTER_READ_INTERVAL_SECONDS
    ]
    due.sort(key=lambda target: (
        last_read.get((target.unit, target.expander_sas_address), float("-inf")), order[target.identity],
    ))
    chosen: list[SmpTarget] = []
    calls = 0
    commands = 0
    for target in due:
        cost = min(target.num_phys, 255)
        chunks = -(-cost // SMP_PHYS_PER_COMMAND)
        if len(chosen) >= SMP_MAX_TARGETS_PER_REFRESH:
            break
        if calls + cost > call_budget or commands + chunks > command_budget:
            continue
        chosen.append(target)
        calls += cost
        commands += chunks
    return sorted(chosen, key=lambda target: order[target.identity])


def smp_output_calls(output_key: str | None) -> int:
    """How many PHY calls an output key's range stands for (0 for other keys)."""
    match = re.search(r":([0-9a-f]{2})-([0-9a-f]{2})$", str(output_key or ""))
    if not match or not str(output_key).startswith(SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX):
        return 0
    return max(0, int(match.group(2), 16) - int(match.group(1), 16)) + 1


def build_phy_error_log_command(target: SmpTarget, phys: Sequence[int] | None = None) -> str:
    """One POSIX sh command line that reads ``phys`` of ``target`` in order.

    Each call is the exact granted argv with a literal PHY, bounded by
    timeout(1) with a SIGKILL fallback (``-k``; FreeBSD 10.3+ and GNU). Both
    output streams are hex-dumped so the raw frame survives text transport
    and sudo's or camcontrol's message stays with its PHY. A call that prints
    nothing (timed out) ends this command; full reads are split into chunks
    by build_phy_error_log_commands().
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
        f"o=$(/usr/bin/timeout -k {SMP_CALL_KILL_AFTER_SECONDS} {SMP_CALL_TIMEOUT_SECONDS} "
        f"sudo -n {CAMCONTROL} smpcmd {device} "
        f"-r 12 \"{fmt}\" -R 28 - 2>&1 | /usr/bin/od -An -tx1 -v); "
        "if [ -z \"$o\" ]; then echo @no-response; break; fi; "
        "printf '%s\\n' \"$o\"; done; echo @end"
    )


def build_phy_error_log_commands(target: SmpTarget) -> list[str]:
    """A full read of ``target``: one command per SMP_PHYS_PER_COMMAND PHYs.

    Each chunk has its own PHY range in its output key, so a chunk that the
    SSH command deadline cuts off loses only its own PHYs. A call with no
    answer still ends its chunk; the next chunk starts afresh.
    """
    phys = list(range(min(target.num_phys, 255)))
    return [
        build_phy_error_log_command(target, phys[start:start + SMP_PHYS_PER_COMMAND])
        for start in range(0, len(phys), SMP_PHYS_PER_COMMAND)
    ]


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
    # The marker's PHY range ("00-03"), whether the end marker was seen, and
    # whether parse_chunk_output() refused the output as a whole.
    phy_range: str | None = None
    ended: bool = False
    rejected: bool = False

    @property
    def unit(self) -> int | None:
        parts = (self.target_identity or "").split(":")
        return int(parts[0]) if len(parts) == 3 and parts[0].isdigit() else None

    @property
    def expander_sas_address(self) -> str | None:
        parts = (self.target_identity or "").split(":")
        return parts[1] if len(parts) == 3 else None

    @property
    def device(self) -> str | None:
        parts = (self.target_identity or "").split(":")
        return parts[2] if len(parts) == 3 else None

    @property
    def capability(self) -> str | None:
        """granted when sudo ran camcontrol for any PHY, refused when it never did."""
        if self.rejected:
            return None
        states = {row["counter_state"] for row in self.rows}
        not_run = {"sudo_refused", "no_response", "incomplete_output", "rejected_output"}
        if "measured" in states or any(row["counter_reason"] not in not_run for row in self.rows):
            return CAPABILITY_GRANTED
        if CAPABILITY_DENIED in states:
            return CAPABILITY_DENIED
        return None


def parse_phy_error_log_output(stdout: str) -> PhyErrorLogResult:
    """Parse the output of :func:`build_phy_error_log_command`."""
    identity: str | None = None
    phy_range: str | None = None
    ended = False
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
                phy_range = parts[3]
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
            ended = True
            break
        if current is not None and line:
            lines.append(line)
    else:
        # No end marker: the batch was cut short; the open PHY is unknown.
        if current is not None:
            rows.append(_unknown_row(current, "unknown", "incomplete_output"))
    return PhyErrorLogResult(identity, rows, phy_range, ended)


def chunk_output_span(output_key: str | None) -> tuple[str, list[int]] | None:
    """The expander identity and PHYs a counter output key names, or None."""
    text = str(output_key or "")
    if not text.startswith(SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX):
        return None
    parts = text[len(SMP_PHY_ERROR_LOG_OUTPUT_KEY_PREFIX):].split(":")
    if (
        len(parts) != 4 or not parts[0].isdigit() or not parts[0].isascii()
        or _SAS_ADDRESS.fullmatch(parts[1]) is None or _PASS_DEVICE.fullmatch(parts[2]) is None
        or re.fullmatch(r"[0-9a-f]{2}-[0-9a-f]{2}", parts[3]) is None
    ):
        return None
    first, last = (int(part, 16) for part in parts[3].split("-"))
    if not first <= last <= 254:
        return None
    return ":".join(parts[:3]), list(range(first, last + 1))


def parse_chunk_output(output_key: str | None, stdout: object) -> PhyErrorLogResult | None:
    """Parse one counter command's output against the key it was asked for.

    None when the key is not a counter key. Output larger than the chunk can
    legitimately print is refused unparsed; so is output whose marker names
    another expander or pass device, that reports a PHY outside the key's
    range, or that lacks its end marker. A refused output's PHYs read as
    ``unknown`` / ``rejected_output``: an error, never counters.
    """
    span = chunk_output_span(output_key)
    if span is None:
        return None
    identity, phys = span
    phy_range = f"{phys[0]:02x}-{phys[-1]:02x}"
    rejected = PhyErrorLogResult(
        identity, [_unknown_row(phy, "unknown", "rejected_output") for phy in phys], phy_range, False, True,
    )
    text = stdout if isinstance(stdout, str) else ""
    if len(text) > SMP_OUTPUT_HEADER_BYTES + SMP_MAX_OUTPUT_BYTES_PER_PHY * len(phys):
        return rejected
    parsed = parse_phy_error_log_output(text)
    if (
        parsed.target_identity != identity or not parsed.ended
        or any(row["phy"] not in phys for row in parsed.rows)
    ):
        return rejected
    return parsed


def chunk_stop(output_key: str | None, ok: bool, stdout: object, stderr: object = "") -> tuple[str, int] | None:
    """Why an expander's read must stop after this chunk, and at which PHY.

    A chunk past the SSH command deadline or with a call that gave no answer
    is ``no_response`` (the expander is hung); a refused output is
    ``rejected_output``; any other failed command is ``command_failed``; a
    chunk whose every call sudo refused is ``permission_denied``. None means
    the next chunk may run.
    """
    span = chunk_output_span(output_key)
    if span is None:
        return None
    first = span[1][0]
    if not ok:
        timed_out = "timed out" in str(stderr or "").lower()
        return ("no_response" if timed_out else "command_failed", first)
    parsed = parse_chunk_output(output_key, stdout)
    if parsed is None or parsed.rejected:
        return ("rejected_output", first)
    for row in parsed.rows:
        if row["counter_reason"] == "no_response":
            return ("no_response", row["phy"])
    if parsed.rows and all(row["counter_state"] == CAPABILITY_DENIED for row in parsed.rows):
        return ("permission_denied", first)
    return None


# --- kept readings -----------------------------------------------------------

_ROW_STATES = frozenset({"measured", "unknown", CAPABILITY_DENIED})
_ROW_REASONS = frozenset({
    None, "unreadable_output", "no_response", "incomplete_output", "sudo_refused", "command_failed",
    "rejected_output", "frame_type_error", "frame_length", "frame_type", "function", "function_result",
    "response_length", "phy_identifier",
})
# One kept row: (phy, four counters, state, reason).
KeptRow = tuple[int, int | None, int | None, int | None, int | None, str, str | None]


def compact_counter_row(row: Mapping[str, Any]) -> KeptRow | None:
    """A decoded row in the fixed kept shape, or None when it is not one."""
    phy = row.get("phy")
    if type(phy) is not int or not 0 <= phy <= 254:
        return None
    counters: list[int | None] = []
    for name in COUNTER_FIELDS:
        value = row.get(name)
        if value is not None and (type(value) is not int or not 0 <= value <= COUNTER_SATURATED):
            return None
        counters.append(value)
    state, reason = row.get("counter_state"), row.get("counter_reason")
    if state not in _ROW_STATES or reason not in _ROW_REASONS:
        return None
    return (phy, *counters, state, reason)


@dataclass(frozen=True)
class KeptCounterRead:
    """One expander's last reading, kept between refreshes (#1088 security review).

    ``rows`` are decoded rows in the fixed KeptRow shape, at most one per PHY,
    from the latest read that completed a chunk, read at ``read_at`` (UTC).
    ``stopped`` is (reason, PHY, UTC time) when the latest read ended early,
    for example because the expander did not answer.
    """

    device: str
    read_at: str | None
    rows: tuple[KeptRow, ...]
    stopped: tuple[str, int, str] | None = None

    def row_dicts(self) -> list[dict[str, Any]]:
        return [
            {"phy": row[0], **dict(zip(COUNTER_FIELDS, row[1:5])), "counter_state": row[5], "counter_reason": row[6]}
            for row in self.rows
        ]


def bound_kept_reads(
    reads: Mapping[tuple[int, str], KeptCounterRead],
    recency: Mapping[tuple[int, str], float],
) -> dict[tuple[int, str], KeptCounterRead]:
    """Drop the least recently read readings until the kept totals fit."""
    order = sorted(reads, key=lambda expander: recency.get(expander, float("-inf")))
    kept = dict(reads)
    rows = sum(len(read.rows) for read in kept.values())
    for expander in order:
        if len(kept) <= SMP_MAX_RETAINED_EXPANDERS and rows <= SMP_MAX_RETAINED_PHYS:
            break
        rows -= len(kept.pop(expander).rows)
    return kept


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
