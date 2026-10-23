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
"""
from __future__ import annotations

import re


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
