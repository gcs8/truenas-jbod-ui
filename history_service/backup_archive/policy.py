"""Backup class policy and archive target settings (config.yaml + environment).

Everything is off by default. The ``backups`` section of ``config.yaml``
supplies the policy; environment variables override single values (and
``BACKUP_TARGETS_JSON`` replaces the whole target list)::

    backups:
      config:                    # config-only backups taken after config changes
        enabled: false
        debounce_seconds: 30     # quiet period after the last change
        max_delay_seconds: 600   # cap, so a steady stream of edits still gets backed up
        local_keep: 30
        remote_keep: 90          # null = no count limit
        remote_max_age_days: null
      full:                      # config + history database on a cron schedule
        enabled: false
        schedule: "0 3 * * *"
        archive_format: tar.zst  # or 7z: portable (older app versions, plain 7-Zip) but slow
        local_keep: 14
        remote_keep: null
        remote_max_age_days: 90
      targets:
        - target_id: office-nas
          label: Office NAS
          provider: sftp
          hostname: nas.example.test
          username: backup
          root: /srv/backups/jbod
          known_hosts_path: /run/backup-secrets/archive_known_hosts
          private_key_file: /run/backup-secrets/archive_sftp_key

Target credentials are only ever ``*_file`` paths (see ``settings.py``).
Validation problems raise :class:`app.config_errors.ConfigurationError` with one
plain sentence per problem that names the key and where it came from, never the
value.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.config_errors import ConfigurationError, describe_validation_error
from history_service.backup_archive.cron import CronError, CronSchedule
from history_service.backup_archive.settings import (
    ArchiveConfigError,
    ArchiveTargetSettings,
    filesystem_roots_overlap,
)

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
DEFAULT_LOCAL_ARCHIVE_ROOT = "/app/backups/archive"

# Environment variable -> (class, field). Values are parsed by the model.
ENV_POLICY_OVERRIDES: dict[str, tuple[str, str]] = {
    "BACKUP_CONFIG_ENABLED": ("config", "enabled"),
    "BACKUP_CONFIG_DEBOUNCE_SECONDS": ("config", "debounce_seconds"),
    "BACKUP_CONFIG_MAX_DELAY_SECONDS": ("config", "max_delay_seconds"),
    "BACKUP_CONFIG_LOCAL_KEEP": ("config", "local_keep"),
    "BACKUP_CONFIG_REMOTE_KEEP": ("config", "remote_keep"),
    "BACKUP_CONFIG_REMOTE_MAX_AGE_DAYS": ("config", "remote_max_age_days"),
    "BACKUP_FULL_ENABLED": ("full", "enabled"),
    "BACKUP_FULL_SCHEDULE": ("full", "schedule"),
    "BACKUP_FULL_ARCHIVE_FORMAT": ("full", "archive_format"),
    "BACKUP_FULL_LOCAL_KEEP": ("full", "local_keep"),
    "BACKUP_FULL_REMOTE_KEEP": ("full", "remote_keep"),
    "BACKUP_FULL_REMOTE_MAX_AGE_DAYS": ("full", "remote_max_age_days"),
}
TARGETS_ENV = "BACKUP_TARGETS_JSON"
_TARGET_UI_KEYS = ("label", "enabled")


class _ClassPolicyBase(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    local_keep: int = Field(default=7, ge=1, le=10_000)
    remote_keep: int | None = Field(default=None, ge=1, le=100_000)
    remote_max_age_days: int | None = Field(default=None, ge=1, le=36_500)

    @field_validator("remote_keep", "remote_max_age_days", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
            return None
        return value


class ConfigClassPolicy(_ClassPolicyBase):
    local_keep: int = Field(default=30, ge=1, le=10_000)
    debounce_seconds: int = Field(default=30, ge=0, le=86_400)
    max_delay_seconds: int = Field(default=600, ge=1, le=604_800)

    @model_validator(mode="after")
    def _delay_covers_debounce(self) -> ConfigClassPolicy:
        if self.max_delay_seconds < self.debounce_seconds:
            raise ValueError(
                "backups.config.max_delay_seconds must be at least backups.config.debounce_seconds."
            )
        return self


class FullClassPolicy(_ClassPolicyBase):
    # One daily FULL per day for two weeks. This replacement policy keeps the
    # owner-approved 14-copy window when scheduler FULLs supersede the history
    # sidecar's duplicate daily/weekly/monthly copies (#455).
    local_keep: int = Field(default=14, ge=1, le=10_000)
    schedule: str = "0 3 * * *"
    # tar.zst (TJBENC02) is the default (#397): much faster for multi-GiB
    # history. Older app versions and plain 7-Zip cannot read it; 7z keeps
    # the portable format for operators who need that.
    archive_format: Literal["7z", "tar.zst"] = "tar.zst"

    @field_validator("schedule")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        try:
            return CronSchedule.parse(value).expression
        except CronError as exc:
            raise ValueError(f"is not a cron schedule ({exc})") from exc


class _PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    config: ConfigClassPolicy = Field(default_factory=ConfigClassPolicy)
    full: FullClassPolicy = Field(default_factory=FullClassPolicy)
    targets: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ArchiveTarget:
    """One configured remote target plus its UI-facing label and switch."""

    settings: ArchiveTargetSettings
    label: str
    enabled: bool = True

    @property
    def target_id(self) -> str:
        return self.settings.target_id


@dataclass(frozen=True, slots=True)
class BackupPolicy:
    config: ConfigClassPolicy = field(default_factory=ConfigClassPolicy)
    full: FullClassPolicy = field(default_factory=FullClassPolicy)
    targets: tuple[ArchiveTarget, ...] = ()

    @property
    def any_enabled(self) -> bool:
        return self.config.enabled or self.full.enabled

    def class_policy(self, backup_class: str) -> _ClassPolicyBase:
        return self.config if backup_class == "config" else self.full

    def enabled_targets(self) -> tuple[ArchiveTarget, ...]:
        return tuple(target for target in self.targets if target.enabled)

    def target(self, target_id: str) -> ArchiveTarget | None:
        return next((target for target in self.targets if target.target_id == target_id), None)

    def max_age(self, backup_class: str) -> timedelta | None:
        days = self.class_policy(backup_class).remote_max_age_days
        return None if days is None else timedelta(days=days)


def local_archive_root_from_environment(environ: Mapping[str, str]) -> str:
    return str(environ.get("BACKUP_ARCHIVE_DIR") or "").strip() or DEFAULT_LOCAL_ARCHIVE_ROOT


def validate_filesystem_target_roots(
    targets: Iterable[ArchiveTarget],
    local_archive_root: str | os.PathLike[str],
) -> None:
    """Fail closed when a filesystem target aliases or overlaps local storage."""

    problems: list[str] = []
    for target in targets:
        if target.settings.provider != "filesystem":
            continue
        try:
            overlaps = filesystem_roots_overlap(local_archive_root, target.settings.root)
        except ArchiveConfigError as exc:
            problems.append(f"Filesystem archive target {target.target_id!r}: {exc}")
            continue
        if overlaps:
            problems.append(
                f"Filesystem archive target {target.target_id!r} must not overlap the local archive root."
            )
    if problems:
        raise ConfigurationError(problems)


def config_backups_enabled_from_environment(yaml_backups: Mapping[str, Any] | None = None) -> bool:
    """Cheap check used by the journal hooks: env wins, then ``backups.config.enabled``."""

    raw = os.getenv("BACKUP_CONFIG_ENABLED")
    if raw is not None and raw.strip():
        return raw.strip().lower() in TRUE_VALUES
    if isinstance(yaml_backups, Mapping):
        section = yaml_backups.get("config")
        if isinstance(section, Mapping):
            return section.get("enabled") is True
    return False


def _read_backups_section(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError([f"{config_path} could not be read as YAML ({type(exc).__name__})."]) from None
    if not isinstance(loaded, dict):
        return {}
    section = loaded.get("backups")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigurationError([f"backups in {config_path} must be a mapping of keys to values."])
    return section


def load_backup_policy(
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> BackupPolicy:
    """Load and validate the policy. Raises ``ConfigurationError`` on any problem."""

    env = os.environ if environ is None else environ
    path = Path(config_path or env.get("APP_CONFIG_PATH") or "/app/config/config.yaml")
    return policy_from_section(_read_backups_section(path), source=str(path), environ=env)


def policy_from_section(
    section: Mapping[str, Any],
    *,
    source: str,
    environ: Mapping[str, str],
) -> BackupPolicy:
    """Validate one ``backups`` mapping (plus environment overrides) into a policy.

    ``source`` names where the mapping came from in problem sentences.
    """

    env = environ
    path = source
    document: dict[str, Any] = {
        "config": dict(section.get("config") or {}) if isinstance(section.get("config", {}), dict) else section.get("config"),
        "full": dict(section.get("full") or {}) if isinstance(section.get("full", {}), dict) else section.get("full"),
        "targets": section.get("targets", []),
    }
    for key in set(section) - {"config", "full", "targets"}:
        document[key] = section[key]
    env_sources: dict[tuple[str, str], str] = {}
    for env_name, (class_name, field_name) in ENV_POLICY_OVERRIDES.items():
        raw = env.get(env_name)
        if raw is None or raw == "":
            continue
        if isinstance(document.get(class_name), dict):
            document[class_name][field_name] = raw.strip()
            env_sources[(class_name, field_name)] = env_name
    targets_source = f"backups.targets in {path}"
    raw_targets = env.get(TARGETS_ENV)
    if raw_targets is not None and raw_targets.strip():
        try:
            document["targets"] = json.loads(raw_targets)
        except json.JSONDecodeError:
            raise ConfigurationError([f"{TARGETS_ENV} in the environment must be valid JSON."]) from None
        targets_source = f"{TARGETS_ENV} in the environment"

    def resolve(location: tuple[int | str, ...]) -> tuple[str, str] | None:
        if len(location) >= 2 and (location[0], location[1]) in env_sources:
            return env_sources[(str(location[0]), str(location[1]))], "the environment"
        if location and location[0] == "targets":
            return targets_source.split(" in ", 1)[0], targets_source.split(" in ", 1)[1]
        return "backups." + ".".join(str(part) for part in location), str(path)

    try:
        parsed = _PolicyDocument.model_validate(document)
    except ValidationError as exc:
        problems = describe_validation_error(exc, resolve_location=resolve, default_source=path)
        raise ConfigurationError(problems) from None
    targets = _parse_targets(parsed.targets, targets_source)
    policy = BackupPolicy(config=parsed.config, full=parsed.full, targets=targets)
    validate_filesystem_target_roots(targets, local_archive_root_from_environment(env))
    return policy


def _parse_targets(items: list[Any], source: str) -> tuple[ArchiveTarget, ...]:
    subject, where = source.split(" in ", 1)
    problems: list[str] = []
    targets: list[ArchiveTarget] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        label_prefix = f"{subject}[{index}] in {where}"
        if not isinstance(item, Mapping):
            problems.append(f"{label_prefix} must be a mapping of keys to values.")
            continue
        values = dict(item)
        label = values.pop("label", None)
        enabled = values.pop("enabled", True)
        if label is not None and not isinstance(label, str):
            problems.append(f"{label_prefix}: label must be text.")
            continue
        if not isinstance(enabled, bool):
            problems.append(f"{label_prefix}: enabled must be true or false.")
            continue
        try:
            settings = ArchiveTargetSettings.from_mapping(values)
        except ArchiveConfigError as exc:
            problems.append(f"{label_prefix}: {exc}")
            continue
        except TypeError:
            problems.append(f"{label_prefix}: target settings have the wrong shape.")
            continue
        if settings.target_id in seen or settings.target_id == "local":
            problems.append(f"{label_prefix}: target_id must be unique and must not be 'local'.")
            continue
        seen.add(settings.target_id)
        targets.append(ArchiveTarget(settings=settings, label=(label or settings.target_id).strip()[:80], enabled=enabled))
    if problems:
        raise ConfigurationError(problems)
    return tuple(targets)


__all__ = [
    "DEFAULT_LOCAL_ARCHIVE_ROOT",
    "ENV_POLICY_OVERRIDES",
    "TARGETS_ENV",
    "ArchiveTarget",
    "BackupPolicy",
    "ConfigClassPolicy",
    "FullClassPolicy",
    "config_backups_enabled_from_environment",
    "load_backup_policy",
    "local_archive_root_from_environment",
    "policy_from_section",
    "validate_filesystem_target_roots",
]
