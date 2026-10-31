"""Operator-facing error text for backup status and API responses (#737).

Backup status files and the scheduler API are shared with the admin UI and
``/healthz``. A remote provider (SFTP, FTP, SMB, S3, NFS) or a client library
can put anything in an exception message: server banners, reply text, paths,
account names. Only text this application wrote is shown. For any other error
the class name is shown, plus the local ``strerror`` text for an ``OSError``
that has an errno. Raw provider text is never copied into status.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DETAIL_CHARS = 300

_SOURCE_ROOT = str(Path(__file__).resolve().parents[2]) + os.sep
_APP_PACKAGES = frozenset({"app", "history_service", "admin_service"})
_INSTALLED_DIRS = ("site-packages", "dist-packages")


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
    traceback = exc.__traceback__
    if traceback is None:
        return False
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    return _is_source_file(traceback.tb_frame.f_code.co_filename)


def _clean(text: str) -> str:
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def describe_error(exc: BaseException, limit: int = DEFAULT_DETAIL_CHARS) -> str:
    """Short, bounded operator-facing text with no raw provider data.

    The message is kept for an exception type defined here, or for any type
    the application packages raised themselves. An ``OSError`` with an errno shows only
    its local ``strerror`` (no filename or peer text). Anything a client
    library raised shows only its class name.
    """

    if _is_app_type(exc):
        message = _clean(str(exc))
    elif isinstance(exc, OSError) and isinstance(exc.errno, int) and exc.errno > 0:
        message = _clean(os.strerror(exc.errno))
    elif _raised_in_source(exc):
        message = _clean(str(exc))
    else:
        message = ""
    name = type(exc).__name__
    text = f"{name}: {message}" if message else name
    return text[:limit]
