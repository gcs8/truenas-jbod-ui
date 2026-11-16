from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

MAX_SECRET_FILE_BYTES = 65_536


def load_secret_environment_value(env_name: str) -> str | None:
    file_env_name = f"{env_name}_FILE"
    secret_file = os.getenv(file_env_name)
    if secret_file is None:
        return os.getenv(env_name)

    secret_path = Path(secret_file)
    initial_metadata = None
    try:
        initial_metadata = secret_path.lstat()
    except OSError:
        pass
    if initial_metadata is None or not stat.S_ISREG(initial_metadata.st_mode):
        raise ValueError(f"{file_env_name} must reference a readable regular file.")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = None
    try:
        descriptor = os.open(secret_path, flags)
    except OSError:
        pass
    if descriptor is None:
        raise ValueError(f"{file_env_name} must reference a readable regular file.")

    metadata = None
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        pass
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise ValueError(f"{file_env_name} must reference a readable regular file.")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise ValueError(f"{file_env_name} must not reference a group- or world-writable file.")

    payload = None
    handle = None
    try:
        handle = os.fdopen(descriptor, "rb")
    except OSError:
        pass
    if handle is not None:
        try:
            payload = handle.read(MAX_SECRET_FILE_BYTES + 1)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass
    else:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if payload is None:
        raise ValueError(f"{file_env_name} must reference a readable regular file.")

    if len(payload) > MAX_SECRET_FILE_BYTES:
        raise ValueError(f"{file_env_name} must not exceed {MAX_SECRET_FILE_BYTES} bytes.")
    value = None
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if value is None:
        raise ValueError(f"{file_env_name} must contain valid UTF-8 text.")
    if "\x00" in value:
        raise ValueError(f"{file_env_name} must not contain NUL characters.")

    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith("\n"):
        return value[:-1]
    return value


class ReplacedDurabilityUncertainError(OSError):
    """The replacement is already visible, but the directory fsync failed.

    Raised only after ``os.replace`` has published the new file. A caller
    must not treat it as proof that the old file survived, and must not undo
    work that the visible file now depends on.
    """


_STAGING_MODE = 0o600
_STAGING_ATTEMPTS = 16


def _create_staging_file(path: Path) -> tuple[Path, int]:
    """A new staging file beside ``path`` under a name only this call uses."""
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(_STAGING_ATTEMPTS):
        candidate = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
        try:
            return candidate, os.open(candidate, flags, _STAGING_MODE)
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not create a staging file beside {path.name}.")


def write_text_atomically(path: Path, text: str) -> int:
    """Replace ``path`` with ``text`` through a fsynced staging file.

    Each call stages under its own unpredictable name, created exclusively
    with mode 0o600, so a config file that holds credentials never passes
    through the umask default and two writers never share a staging file.
    Once the content is written the staging file takes the existing file's
    mode (0o600 for a new file) and is renamed into place. Returns that mode.

    Any failure or interruption before the rename leaves ``path`` as it was
    and removes this call's staging name, and no other file. A failure after
    the rename (the directory sync) raises ReplacedDurabilityUncertainError:
    the new file is already in place and its durability is uncertain.
    """

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = _STAGING_MODE
    temp_path, descriptor = _create_staging_file(path)
    handle = None
    try:
        handle = os.fdopen(descriptor, "wb")
        if hasattr(os, "fchmod"):
            # os.open applies the umask; keep the staging file exactly 0600.
            os.fchmod(handle.fileno(), _STAGING_MODE)
        handle.write(text.encode("utf-8"))
        handle.flush()
        if hasattr(os, "fchmod"):
            # Complete: now take the destination's mode for publication.
            os.fchmod(handle.fileno(), mode)
        os.fsync(handle.fileno())
        if os.name != "posix":
            # Windows cannot rename a file that is still open.
            handle.close()
        os.replace(temp_path, path)
    except BaseException:
        # Nothing was published: remove this call's staging copy (it can
        # hold credentials). Best effort: a failed unlink is not reported
        # over the original failure.
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if handle is not None:
            handle.close()
        else:
            os.close(descriptor)
    if os.name == "posix":
        # fsync the directory so the rename itself survives a power loss.
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            # Replacement has already happened. Do not claim either success
            # or that the old bytes survived a failed durability barrier.
            raise ReplacedDurabilityUncertainError(
                exc.errno, "File replaced; directory durability is uncertain."
            ) from exc
    return mode
