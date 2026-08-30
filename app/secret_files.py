from __future__ import annotations

import os
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
