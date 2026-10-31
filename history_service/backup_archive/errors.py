"""Operator-facing error text for backup status and API responses (#737).

Backup status files and the scheduler API are shared with the admin UI and
``/healthz``. A remote provider (SFTP, FTP, SMB, S3, NFS) or a client library
can put anything in an exception message: server banners, reply text, paths,
account names. Only text this application wrote is shown. For any other error
the class name is shown, plus the local ``strerror`` text for an ``OSError``
that has an errno. Raw provider text is never copied into status.
"""

from __future__ import annotations

import dis
import os
import socket
import ssl
from pathlib import Path

DEFAULT_DETAIL_CHARS = 300

_SOURCE_ROOT = str(Path(__file__).resolve().parents[2]) + os.sep
_APP_PACKAGES = frozenset({"app", "history_service", "admin_service"})
_INSTALLED_DIRS = ("site-packages", "dist-packages")
_RAISE_OPCODES = frozenset(dis.opmap[name] for name in ("RAISE_VARARGS", "RERAISE") if name in dis.opmap)
# These carry library or resolver codes in ``errno``, not POSIX errno values.
_NON_POSIX_ERRNO = (ssl.SSLError, socket.gaierror, socket.herror)


def _is_source_file(filename: str) -> bool:
    # Only the shipped packages count; a vendored or virtualenv copy of a
    # client library below the checkout does not.
    path = os.path.abspath(filename)
    if not path.startswith(_SOURCE_ROOT):
        return False
    parts = Path(path[len(_SOURCE_ROOT):]).parts
    return bool(parts) and parts[0] in _APP_PACKAGES and not any(part in _INSTALLED_DIRS for part in parts)


def _is_app_type(exc: BaseException) -> bool:
    return (type(exc).__module__ or "").split(".", 1)[0] in _APP_PACKAGES


def _raised_in_source(exc: BaseException) -> bool:
    """True only for an explicit ``raise`` statement in an application file.

    A builtin call made from application code, such as ``int(reply)``, also
    ends its traceback in an application frame, but its message quotes the
    provider value, so the instruction must be the raise itself.
    """

    traceback = exc.__traceback__
    if traceback is None:
        return False
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    code = traceback.tb_frame.f_code
    if not _is_source_file(code.co_filename):
        return False
    offset = traceback.tb_lasti
    raw = code.co_code
    return 0 <= offset < len(raw) and raw[offset] in _RAISE_OPCODES


def _clean(text: str) -> str:
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def describe_error(exc: BaseException, limit: int = DEFAULT_DETAIL_CHARS) -> str:
    """Short, bounded operator-facing text with no raw provider data.

    The message is kept for an exception type defined in the application, or
    for a builtin type an application ``raise`` statement created. An
    ``OSError`` with a POSIX errno shows only its local ``strerror`` (no
    filename or peer text). Anything a client library or builtin call raised
    shows only its class name.
    """

    if _is_app_type(exc):
        message = _clean(str(exc))
    elif isinstance(exc, _NON_POSIX_ERRNO):
        message = ""
    elif isinstance(exc, OSError) and isinstance(exc.errno, int) and exc.errno > 0:
        message = _clean(os.strerror(exc.errno))
    elif _raised_in_source(exc):
        message = _clean(str(exc))
    else:
        message = ""
    name = type(exc).__name__
    text = f"{name}: {message}" if message else name
    return text[:limit]
