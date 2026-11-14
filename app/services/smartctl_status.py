"""smartctl exit status bits (smartctl(8), "RETURN VALUES").

smartctl's exit status is a bit mask. Only bits 0 and 1 mean the command did
not read the device; the others report what it read. Bit 2 is a SMART/ATA
command or checksum problem with output still printed, and bits 3-7 are drive
health, which must stay visible on the SMART result rather than being reported
as a failed SSH command (#1171).
"""

from __future__ import annotations

import posixpath
import shlex

SMARTCTL_EXIT_BITS: tuple[tuple[int, str], ...] = (
    (0, "command_line_error"),
    (1, "device_open_failed"),
    (2, "smart_command_failed"),
    (3, "smart_status_failing"),
    (4, "prefail_attributes_at_threshold"),
    (5, "attributes_past_threshold"),
    (6, "error_log_has_errors"),
    (7, "self_test_log_has_errors"),
)
# Bits 0-1: the command line did not parse or the device did not open.
SMARTCTL_FAILURE_MASK = 0b0000_0011
# Bits 3-4: the drive reports itself failing now.
SMARTCTL_FAILING_MASK = 0b0001_1000


def _mask(exit_code: int | None) -> int | None:
    """The exit status as an 8-bit smartctl mask, or None when it is not one."""
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and 0 <= exit_code <= 255:
        return exit_code
    return None


def smartctl_exit_failed(exit_code: int | None) -> bool:
    """True when smartctl did not read the device (or the status is not a smartctl mask)."""
    mask = _mask(exit_code)
    return mask is None or bool(mask & SMARTCTL_FAILURE_MASK)


def smartctl_exit_flags(exit_code: int | None) -> list[str]:
    """Names of the bits set in a smartctl exit status, lowest bit first."""
    mask = _mask(exit_code)
    if mask is None:
        return []
    return [name for bit, name in SMARTCTL_EXIT_BITS if mask & (1 << bit)]


def smartctl_reports_failing(exit_code: int | None) -> bool:
    mask = _mask(exit_code)
    return mask is not None and bool(mask & SMARTCTL_FAILING_MASK)


def smartctl_device_path(command: str) -> str | None:
    """The device a ``[sudo ...] smartctl ... /dev/X`` command reads, else None."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    index = 0
    if tokens and posixpath.basename(tokens[0]) == "sudo":
        index = 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 1
    if index >= len(tokens) or posixpath.basename(tokens[index]) != "smartctl":
        return None
    arguments = tokens[index + 1:]
    if not arguments or not arguments[-1].startswith("/dev/"):
        return None
    return arguments[-1]
