"""One fixed, unprivileged Linux SAS transport survey. No remote writes."""

from __future__ import annotations

import shlex

SAS_TRANSPORT_KEY = "sas transport sysfs"
# Keep this script self-contained: it runs with the host's standard-library
# Python, not the application environment. Tests execute these exact bytes on
# an invented sysfs tree, substituting only ROOT.
SAS_TRANSPORT_SCRIPT = r"""
import json
import os
import re
import signal

ROOT = "/sys"
MAX_ROWS = 4096
MAX_DIRECTORY = 2048
MAX_VISITS = 32768
MAX_BYTES = 2 * 1024 * 1024
MAX_VALUE = 512
FIELDS = {
    "sas_host": (),
    "scsi_host": ("proc_name", "board_name", "version_fw", "host_sas_address"),
    "sas_expander": ("vendor_id", "product_id", "product_rev", "level"),
    "sas_port": ("num_phys",),
    "sas_phy": ("phy_identifier", "negotiated_linkrate", "minimum_linkrate", "maximum_linkrate", "sas_address", "device_type", "invalid_dword_count", "running_disparity_error_count", "loss_of_dword_sync_count", "phy_reset_problem_count"),
    "sas_device": ("sas_address", "phy_identifier", "bay_identifier", "enclosure_identifier", "device_type"),
    "sas_end_device": (),
}
NAMES = {
    "sas_host": r"host[0-9]+", "scsi_host": r"host[0-9]+",
    "sas_expander": r"expander-[0-9]+:[0-9]+",
    "sas_port": r"port-[0-9]+(?::[0-9]+){1,2}",
    "sas_phy": r"phy-[0-9]+(?::[0-9]+){1,2}",
    "sas_device": r"(?:expander|end_device)-[0-9]+(?::[0-9]+){1,2}",
    "sas_end_device": r"end_device-[0-9]+(?::[0-9]+){1,2}",
}
visits = 0
read_bytes = 0

class Refused(Exception):
    pass

def alarm(signum, frame):
    raise Refused("time_budget")

signal.signal(signal.SIGALRM, alarm)
signal.alarm(10)

def listing(path, optional=False):
    global visits
    result = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                visits += 1
                if visits > MAX_VISITS or len(result) >= MAX_DIRECTORY:
                    raise Refused("entry_budget")
                if len(entry.name) > 128:
                    raise Refused("name_budget")
                result.append(entry.name)
    except FileNotFoundError:
        if not optional:
            raise Refused("directory_not_read")
    except OSError:
        raise Refused("directory_not_read")
    return sorted(result)

def resolved(path):
    value = os.path.realpath(path, strict=True)
    if len(value) > 4096 or not value.startswith(ROOT + "/devices/"):
        raise Refused("path_not_read")
    return value

def object_path(entry, kind, name):
    device = entry + "/device"
    if os.path.lexists(device):
        return resolved(device)
    value = resolved(entry)
    suffix = "/" + kind + "/" + name
    return value[:-len(suffix)] if value.endswith(suffix) else value

def read(path):
    global read_bytes
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            value = stream.read(MAX_VALUE + 1)
        read_bytes += len(value)
        if read_bytes > MAX_BYTES:
            raise Refused("byte_budget")
        if len(value) > MAX_VALUE:
            return None
        return value.decode("ascii").strip()
    except (OSError, UnicodeError):
        # EINVAL on an unlinked PHY, EACCES and missing attributes all mean
        # not read. Do not replace any of them with measured zero.
        return None

def public_path(path):
    return "/sys" + path[len(ROOT):]

def collect():
    rows = []
    hosts = []
    output_bytes = 0
    for kind in FIELDS:
        names = hosts if kind == "scsi_host" else listing(ROOT + "/class/" + kind, optional=True)
        if kind == "sas_host":
            hosts = names
        for name in names:
            if not re.fullmatch(NAMES[kind], name):
                raise Refused("invalid_entry")
            if len(rows) >= MAX_ROWS:
                raise Refused("row_budget")
            entry = ROOT + "/class/" + kind + "/" + name
            if kind == "scsi_host" and not os.path.lexists(entry):
                continue
            try:
                obj = object_path(entry, kind, name)
            except OSError:
                raise Refused("path_not_read")
            attrs = {field: read(entry + "/" + field) for field in FIELDS[kind]}
            links = {}
            if kind == "sas_port":
                # phys and remote objects are immediate children only. This
                # includes the kernel's explicit upstream backlink.
                for child in listing(obj):
                    if re.fullmatch(r"(?:phy|expander|end_device)-[0-9]+(?::[0-9]+){1,2}|host[0-9]+", child):
                        links[child] = public_path(resolved(obj + "/" + child))
            elif kind == "sas_phy" and os.path.lexists(obj + "/port"):
                links["port"] = public_path(resolved(obj + "/port"))
            elif kind == "sas_end_device":
                # Two fixed SCSI levels, never a recursive walk or shell glob.
                for target in listing(obj):
                    if not re.fullmatch(r"target[0-9]+:[0-9]+:[0-9]+", target):
                        continue
                    for lun in listing(obj + "/" + target):
                        if not re.fullmatch(r"[0-9]+:[0-9]+:[0-9]+:[0-9]+", lun):
                            continue
                        block = obj + "/" + target + "/" + lun + "/block"
                        for disk in listing(block, optional=True):
                            if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", disk):
                                links[disk] = public_path(resolved(block + "/" + disk))
            row = {"kind": kind, "name": name, "path": public_path(obj), "attrs": attrs, "links": links}
            output_bytes += len(json.dumps(row, separators=(",", ":")).encode("utf-8")) + 1
            if output_bytes > MAX_BYTES - 128:
                raise Refused("output_budget")
            rows.append(row)
    return {"version": 1, "complete": True, "rows": rows}

try:
    result = collect()
    output = json.dumps(result, separators=(",", ":"))
    if len(output.encode("utf-8")) > MAX_BYTES:
        raise Refused("output_budget")
except (Refused, OSError) as error:
    reason = str(error) if isinstance(error, Refused) else "not_read"
    output = json.dumps({"version": 1, "complete": False, "reason": reason, "rows": []})
finally:
    signal.alarm(0)
print(output)
""".strip()

SAS_TRANSPORT_COMMAND = "/usr/bin/python3 -I -c " + shlex.quote(SAS_TRANSPORT_SCRIPT)
