"""Read and write the ``backups`` section of ``config.yaml`` for the admin editor (#573).

Secrets stay file-only, as everywhere else in this app: a target names each
credential by a ``*_file`` path, and the scheduler reads the file when it opens
the target. The editor therefore

* never returns a ``*_file`` path to the browser. It reports only whether the
  setting is configured and whether the file is present and private;
* accepts a new ``*_file`` path only as an absolute path (the operator puts
  the secret file in place with the host shell, as for
  ``BACKUP_ARCHIVE_PASSPHRASE_FILE``); an omitted secret keeps its current
  value, and ``null`` clears it;
* refuses any inline credential key, the same as ``ArchiveTargetSettings.from_mapping``.

Values set in the environment win over ``config.yaml``. The editor reports them
as locked, with the variable name, and a save cannot change them.

A save is validated with the scheduler's own loader (``policy_from_section``)
against the current environment, written atomically (temp file, fsync,
``os.replace``) under the admin's config write lock, and guarded by the
revision (SHA-256 of ``config.yaml``) the editor was loaded from.
"""

from __future__ import annotations

import copy
import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from app.config_errors import ConfigurationError
from history_service.backup_archive.policy import (
    ENV_POLICY_OVERRIDES,
    TARGETS_ENV,
    ConfigClassPolicy,
    FullClassPolicy,
    policy_from_section,
)
from history_service.backup_archive.settings import PROVIDERS, ArchiveTargetSettings

SECRET_FILE_FIELDS = (
    "password_file",
    "private_key_file",
    "private_key_passphrase_file",
    "access_key_id_file",
    "secret_access_key_file",
)
TARGET_FIELDS = tuple(field.name for field in fields(ArchiveTargetSettings))
TARGET_PLAIN_FIELDS = tuple(name for name in TARGET_FIELDS if name not in SECRET_FILE_FIELDS)
CLASS_FIELDS = {
    "config": tuple(ConfigClassPolicy.model_fields),
    "full": tuple(FullClassPolicy.model_fields),
}
# The scheduler container mounts ./config/backup-secrets at this path; the admin
# container sees the same folder under its config directory.
SCHEDULER_SECRET_ROOT = PurePosixPath("/run/backup-secrets")
MAX_TARGETS = 32
RESTART_COMMAND = "docker compose --profile backup-scheduler restart enclosure-backup-scheduler"


class PolicyEditError(ValueError):
    """A save was refused. ``problems`` holds one plain sentence per problem."""

    def __init__(self, problems: list[str], *, conflict: bool = False) -> None:
        super().__init__(" ".join(problems))
        self.problems = problems
        self.conflict = conflict


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_config(config_path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError:
        return {}, b""
    loaded = yaml.safe_load(raw.decode("utf-8")) or {}
    if not isinstance(loaded, dict):
        raise PolicyEditError([f"{config_path.name} must contain a YAML mapping."])
    return loaded, raw


def _secret_file_state(value: Any, config_dir: Path) -> dict[str, Any]:
    """Say whether a secret file is set and usable, without revealing its path."""

    text = str(value or "").strip()
    if not text:
        return {"configured": False, "present": None}
    candidate = PurePosixPath(text)
    try:
        relative = candidate.relative_to(SCHEDULER_SECRET_ROOT)
    except ValueError:
        # Outside the shared secrets folder the admin cannot see the file.
        return {"configured": True, "present": None}
    if ".." in relative.parts:
        return {"configured": True, "present": False}
    local = config_dir / "backup-secrets" / Path(*relative.parts) if relative.parts else None
    if local is None:
        return {"configured": True, "present": False}
    try:
        metadata = os.lstat(local)
    except OSError:
        return {"configured": True, "present": False}
    private = stat.S_ISREG(metadata.st_mode) and not stat.S_IMODE(metadata.st_mode) & 0o077
    return {"configured": True, "present": bool(private)}


def _env_locks(environ: Mapping[str, str]) -> tuple[dict[str, dict[str, str]], str | None]:
    locks: dict[str, dict[str, str]] = {"config": {}, "full": {}}
    for env_name, (class_name, field_name) in ENV_POLICY_OVERRIDES.items():
        raw = environ.get(env_name)
        if raw is not None and raw != "":
            locks[class_name][field_name] = env_name
    raw_targets = environ.get(TARGETS_ENV)
    targets_lock = TARGETS_ENV if raw_targets is not None and raw_targets.strip() else None
    return locks, targets_lock


def load_editor_view(config_path: str | Path, environ: Mapping[str, str]) -> dict[str, Any]:
    """The editable backups document, with secrets reduced to present/missing."""

    path = Path(config_path)
    document, raw = _read_config(path)
    section = _mapping(document.get("backups"))
    locks, targets_lock = _env_locks(environ)
    classes: dict[str, Any] = {}
    for class_name, model in (("config", ConfigClassPolicy), ("full", FullClassPolicy)):
        stored = _mapping(section.get(class_name))
        defaults = model().model_dump()
        classes[class_name] = {
            "values": {name: stored.get(name, defaults[name]) for name in CLASS_FIELDS[class_name]},
            "locked": dict(locks[class_name]),
        }
    targets: list[dict[str, Any]] = []
    for item in section.get("targets") or []:
        if not isinstance(item, dict):
            continue
        values = {name: item[name] for name in TARGET_PLAIN_FIELDS if name in item}
        values["label"] = item.get("label")
        values["enabled"] = item.get("enabled", True)
        targets.append(
            {
                "values": values,
                "secrets": {name: _secret_file_state(item.get(name), path.parent) for name in SECRET_FILE_FIELDS},
            }
        )
    problems: list[str] = []
    try:
        policy_from_section(section, source=str(path), environ=environ)
    except ConfigurationError as exc:
        problems = list(exc.problems)
    return {
        "revision": _revision(raw),
        "classes": classes,
        "targets": targets,
        "targets_locked_by": targets_lock,
        "providers": list(PROVIDERS),
        "target_fields": list(TARGET_PLAIN_FIELDS),
        "secret_fields": list(SECRET_FILE_FIELDS),
        "problems": problems,
        "restart_command": RESTART_COMMAND,
    }


def _merge_class(
    class_name: str,
    incoming: Any,
    stored: Mapping[str, Any],
    locked: Mapping[str, str],
    problems: list[str],
) -> dict[str, Any]:
    merged = dict(stored)
    if incoming is None:
        return merged
    if not isinstance(incoming, Mapping):
        problems.append(f"backups.{class_name} must be a mapping of settings.")
        return merged
    for key, value in incoming.items():
        if key not in CLASS_FIELDS[class_name]:
            problems.append(f"backups.{class_name}.{key} is not a setting.")
            continue
        if key in locked:
            if stored.get(key) != value:
                problems.append(
                    f"backups.{class_name}.{key} is set by {locked[key]} in the environment; change it there."
                )
            continue
        merged[key] = value
    return merged


def _merge_target(
    index: int,
    incoming: Any,
    previous: Mapping[str, Any] | None,
    problems: list[str],
) -> dict[str, Any] | None:
    where = f"backups.targets[{index}]"
    if not isinstance(incoming, Mapping):
        problems.append(f"{where} must be a mapping of settings.")
        return None
    values = incoming.get("values")
    secrets = incoming.get("secrets") or {}
    if not isinstance(values, Mapping) or not isinstance(secrets, Mapping):
        problems.append(f"{where} must have values and secrets mappings.")
        return None
    allowed = set(TARGET_PLAIN_FIELDS) | {"label", "enabled"}
    merged: dict[str, Any] = {}
    for key, value in values.items():
        if key not in allowed:
            # Includes inline credentials and *_file paths sneaking in as values.
            problems.append(f"{where}.{key} cannot be set here.")
            continue
        if value is None or value == "":
            continue
        merged[key] = value
    for name in SECRET_FILE_FIELDS:
        if name not in secrets:
            if previous and previous.get(name):
                merged[name] = previous[name]
            continue
        change = secrets[name]
        if change is None:
            continue
        if not isinstance(change, str) or not change.strip():
            problems.append(f"{where}.{name} must be an absolute path to a secret file, or null to clear it.")
            continue
        text = change.strip()
        if not text.startswith("/") or ".." in PurePosixPath(text).parts or "\x00" in text:
            problems.append(f"{where}.{name} must be an absolute path to a secret file.")
            continue
        merged[name] = text
    for key in secrets:
        if key not in SECRET_FILE_FIELDS:
            problems.append(f"{where}.{key} is not a secret file setting.")
    return merged


def apply_editor_change(
    config_path: str | Path,
    payload: Mapping[str, Any],
    environ: Mapping[str, str],
    *,
    write_lock: Any,
    record_change: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Validate and atomically save an edited ``backups`` section.

    Raises :class:`PolicyEditError` (``conflict=True`` when the file changed
    since ``payload['revision']`` was read). Returns the fresh editor view.
    """

    path = Path(config_path)
    if not isinstance(payload, Mapping):
        raise PolicyEditError(["The request body must be a JSON object."])
    with write_lock:
        document, raw = _read_config(path)
        if payload.get("revision") != _revision(raw):
            raise PolicyEditError(
                ["config.yaml changed since this editor was opened. Reload and try again."],
                conflict=True,
            )
        existing = _mapping(document.get("backups"))
        section: dict[str, Any] = copy.deepcopy(existing)
        locks, targets_lock = _env_locks(environ)
        problems: list[str] = []
        classes = payload.get("classes") or {}
        if not isinstance(classes, Mapping):
            problems.append("classes must be a mapping.")
            classes = {}
        for class_name in ("config", "full"):
            stored = _mapping(section.get(class_name))
            section[class_name] = _merge_class(
                class_name, classes.get(class_name), stored, locks[class_name], problems
            )
        if "targets" in payload:
            incoming_targets = payload.get("targets")
            if targets_lock is not None:
                problems.append(f"Targets are set by {targets_lock} in the environment; change them there.")
            elif not isinstance(incoming_targets, list) or len(incoming_targets) > MAX_TARGETS:
                problems.append(f"targets must be a list of at most {MAX_TARGETS} targets.")
            else:
                previous_by_id = {
                    str(item.get("target_id")): item
                    for item in (existing.get("targets") or [])
                    if isinstance(item, dict)
                }
                merged_targets = []
                for index, item in enumerate(incoming_targets):
                    target_id = None
                    if isinstance(item, Mapping) and isinstance(item.get("values"), Mapping):
                        target_id = str(item["values"].get("target_id") or "")
                    merged = _merge_target(index, item, previous_by_id.get(target_id or ""), problems)
                    if merged is not None:
                        merged_targets.append(merged)
                section["targets"] = merged_targets
        if problems:
            raise PolicyEditError(problems)
        try:
            policy = policy_from_section(section, source="the edited backups section", environ=environ)
        except ConfigurationError as exc:
            raise PolicyEditError(list(exc.problems)) from None
        document["backups"] = section
        _write_atomically(path, document)
    if record_change is not None:
        subject = f"config={'on' if policy.config.enabled else 'off'} full={'on' if policy.full.enabled else 'off'} targets={len(policy.targets)}"
        record_change("backups.policy.save", subject)
    return load_editor_view(path, environ)


def _write_atomically(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.backups-edit.tmp")
    temporary.unlink(missing_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o640
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(document, handle, default_flow_style=False, sort_keys=False, allow_unicode=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        directory = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


__all__ = [
    "RESTART_COMMAND",
    "SECRET_FILE_FIELDS",
    "PolicyEditError",
    "apply_editor_change",
    "load_editor_view",
]
