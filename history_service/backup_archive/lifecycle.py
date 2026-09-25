"""Retention and grooming for catalogued backup artifacts.

Rules are per (backup class, location): keep the newest ``keep_count`` copies
and/or copies younger than ``max_age``. A copy past either limit is a
candidate, oldest first. The guarantees, in order:

* Only catalogued artifacts are ever deleted; objects a location holds without
  a catalog record are reported by :func:`reconcile` and never touched.
* Preserved (pinned) artifacts are never deleted and do not count toward
  ``keep_count``.
* The newest verified copy of a class at a location is never deleted, even when
  a limit says it should be (it is listed in ``GroomingPlan.guarded``).
* Unverified copies (partial or failed readback) do not count toward
  ``keep_count``; once older than the grace period they become candidates of
  kind ``"unverified"``, listed separately from retention deletions.
* A (class, location) pair without a rule is left alone entirely.

``LifecycleManager.plan(now)`` is a dry run. ``apply(plan, resolver)`` deletes
exactly the planned items, tombstoning each in the catalog, and stops at the
first unexpected error with the partial progress in the result.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from .catalog import (
    LOCAL_LOCATION,
    ArtifactCatalog,
    ArtifactRecord,
    CatalogError,
    utc,
    validate_backup_class,
    validate_location,
    validate_object_name,
)

DEFAULT_UNVERIFIED_GRACE = timedelta(hours=24)
DEFAULT_ACTOR = "lifecycle"

PlanKind = Literal["retention", "unverified"]


class GroomableTarget(Protocol):
    """The part of ``ArchiveTarget`` grooming needs.

    ``delete`` may either return quietly (the transport targets in
    ``transport.py`` are idempotent) or raise ``FileNotFoundError`` when the
    object is already gone; only the latter is reported as ``already_missing``.
    Any other exception is treated as unexpected and stops ``apply``.
    """

    def list(self, prefix: str = "") -> list[Any]: ...

    def delete(self, name: str) -> None: ...


TargetResolver = Callable[[str], GroomableTarget]


@dataclass(frozen=True, slots=True)
class RetentionRule:
    backup_class: str
    location: str
    keep_count: int | None = None
    max_age: timedelta | None = None

    def __post_init__(self) -> None:
        validate_backup_class(self.backup_class)
        validate_location(self.location)
        if self.keep_count is not None and (type(self.keep_count) is not int or self.keep_count < 0):
            raise ValueError("keep_count must be a non-negative integer or None.")
        if self.max_age is not None and (
            not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0)
        ):
            raise ValueError("max_age must be a positive timedelta or None.")


@dataclass(frozen=True, slots=True)
class PlanItem:
    record: ArtifactRecord
    reason: str
    kind: PlanKind


@dataclass(frozen=True, slots=True)
class GroomingPlan:
    """A dry-run preview: what would be deleted, and what a limit hit but the guard kept."""

    generated_at: datetime
    items: tuple[PlanItem, ...]
    guarded: tuple[PlanItem, ...] = ()

    @property
    def retention_items(self) -> tuple[PlanItem, ...]:
        return tuple(item for item in self.items if item.kind == "retention")

    @property
    def unverified_items(self) -> tuple[PlanItem, ...]:
        return tuple(item for item in self.items if item.kind == "unverified")


@dataclass(frozen=True, slots=True)
class ApplyResult:
    deleted: tuple[PlanItem, ...]
    already_missing: tuple[PlanItem, ...]
    failed: PlanItem | None = None
    error: str | None = None
    not_attempted: tuple[PlanItem, ...] = ()

    @property
    def complete(self) -> bool:
        return self.failed is None and not self.not_attempted


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    location: str
    missing: tuple[ArtifactRecord, ...]
    uncatalogued: tuple[Any, ...]
    size_mismatch: tuple[tuple[ArtifactRecord, Any], ...] = ()
    tombstoned_but_present: tuple[Any, ...] = ()

    @property
    def clean(self) -> bool:
        return not (
            self.missing or self.uncatalogued or self.size_mismatch or self.tombstoned_but_present
        )


def _describe(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds}s"


@dataclass
class LifecycleManager:
    catalog: ArtifactCatalog
    rules: Iterable[RetentionRule] = ()
    unverified_grace: timedelta = DEFAULT_UNVERIFIED_GRACE
    _rules: dict[tuple[str, str], RetentionRule] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.unverified_grace, timedelta) or self.unverified_grace < timedelta(0):
            raise ValueError("unverified_grace must be a non-negative timedelta.")
        self._rules = {}
        for rule in self.rules:
            key = (rule.backup_class, rule.location)
            if key in self._rules:
                raise ValueError(f"Duplicate retention rule for {key[0]} at {key[1]}.")
            self._rules[key] = rule
        self.rules = tuple(self._rules.values())

    # -- planning ------------------------------------------------------------

    def _plan_group(
        self, rule: RetentionRule, records: list[ArtifactRecord], now: datetime
    ) -> tuple[list[PlanItem], list[PlanItem]]:
        items: list[PlanItem] = []
        guarded: list[PlanItem] = []
        verified = [record for record in records if record.verified]
        newest_verified = max(
            verified, key=lambda record: (record.created_at, record.artifact_id), default=None
        )
        # Newest first so the index is the copy's rank for keep_count.
        counted = sorted(
            (record for record in verified if not record.preserved),
            key=lambda record: (record.created_at, record.artifact_id),
            reverse=True,
        )
        for rank, record in enumerate(counted, start=1):
            reasons: list[str] = []
            if rule.keep_count is not None and rank > rule.keep_count:
                reasons.append(f"beyond keep_count {rule.keep_count} (newest #{rank})")
            if rule.max_age is not None and now - record.created_at > rule.max_age:
                reasons.append(
                    f"older than max_age {_describe(rule.max_age)} "
                    f"(age {_describe(now - record.created_at)})"
                )
            if not reasons or self.catalog.is_deletion_claimed(record.artifact_id):
                continue
            item = PlanItem(record=record, reason="; ".join(reasons), kind="retention")
            if newest_verified is not None and record.artifact_id == newest_verified.artifact_id:
                guarded.append(item)
            else:
                items.append(item)
        for record in records:
            if record.verified or record.preserved or self.catalog.is_deletion_claimed(record.artifact_id):
                continue
            age = now - record.created_at
            if age > self.unverified_grace:
                items.append(
                    PlanItem(
                        record=record,
                        reason=(
                            f"unverified for longer than grace {_describe(self.unverified_grace)} "
                            f"(age {_describe(age)})"
                        ),
                        kind="unverified",
                    )
                )
        return items, guarded

    def plan(self, now: datetime) -> GroomingPlan:
        """Dry run: every deletion grooming would make at ``now``, oldest first."""

        now = utc(now)
        items: list[PlanItem] = []
        guarded: list[PlanItem] = []
        for (backup_class, location), rule in sorted(self._rules.items()):
            records = self.catalog.list(backup_class=backup_class, location=location)
            group_items, group_guarded = self._plan_group(rule, records, now)
            items.extend(group_items)
            guarded.extend(group_guarded)
        order = lambda item: (item.record.created_at, item.record.artifact_id)  # noqa: E731
        return GroomingPlan(
            generated_at=now, items=tuple(sorted(items, key=order)), guarded=tuple(sorted(guarded, key=order))
        )

    # -- applying ------------------------------------------------------------

    def apply(
        self,
        plan: GroomingPlan,
        target_resolver: TargetResolver,
        *,
        actor: str = DEFAULT_ACTOR,
        now: Callable[[], datetime] | None = None,
    ) -> ApplyResult:
        """Delete exactly ``plan.items`` in order, tombstoning each one.

        Before each deletion the catalog atomically claims the record: it must
        still equal the planned record (so a copy pinned after planning is not
        deleted) and must not have become the newest verified copy, and while
        the claim is held it cannot be pinned. An object already gone from its
        location is tombstoned as missing. Any other failure releases the claim
        and stops the run.
        """

        clock = now or (lambda: datetime.now(timezone.utc))
        deleted: list[PlanItem] = []
        missing: list[PlanItem] = []
        targets: dict[str, GroomableTarget] = {}
        items = list(plan.items)
        for index, item in enumerate(items):
            record = item.record

            def stop(message: str) -> ApplyResult:
                return ApplyResult(
                    deleted=tuple(deleted),
                    already_missing=tuple(missing),
                    failed=item,
                    error=message,
                    not_attempted=tuple(items[index + 1 :]),
                )

            try:
                if record.location not in targets:
                    targets[record.location] = target_resolver(record.location)
                target = targets[record.location]
                self.catalog.claim_deletion(record, actor=actor, now=clock())
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                return stop(f"{type(exc).__name__}: {exc}")
            gone = False
            try:
                target.delete(record.name)
            except FileNotFoundError:
                gone = True
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                self.catalog.release_deletion(record.artifact_id)
                return stop(f"{type(exc).__name__}: {exc}")
            try:
                reason = f"{item.kind}: {item.reason}"
                if gone:
                    reason += "; object was already missing"
                self.catalog.record_deletion(
                    record.artifact_id, reason=reason, actor=actor, now=clock(), expected=record
                )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                return stop(f"{type(exc).__name__}: {exc}")
            (missing if gone else deleted).append(item)
        return ApplyResult(deleted=tuple(deleted), already_missing=tuple(missing))


def reconcile(catalog: ArtifactCatalog, target: GroomableTarget, location: str) -> ReconcileReport:
    """Compare the catalog for ``location`` with what ``target.list()`` holds. Never deletes."""

    location = validate_location(location)
    records = catalog.list(location=location)
    objects = list(target.list())
    by_name = {obj.name: obj for obj in objects}
    catalogued_names = {record.name for record in records}
    tombstoned_names = {tombstone.record.name for tombstone in catalog.tombstones(location=location)}
    missing = tuple(record for record in records if record.name not in by_name)
    size_mismatch = tuple(
        (record, by_name[record.name])
        for record in records
        if record.name in by_name and by_name[record.name].size != record.size
    )
    extra = sorted(
        (obj for obj in objects if obj.name not in catalogued_names), key=lambda obj: obj.name
    )
    return ReconcileReport(
        location=location,
        missing=missing,
        uncatalogued=tuple(obj for obj in extra if obj.name not in tombstoned_names),
        size_mismatch=size_mismatch,
        tombstoned_but_present=tuple(obj for obj in extra if obj.name in tombstoned_names),
    )


@dataclass(frozen=True, slots=True)
class LocalObject:
    name: str
    size: int
    modified: datetime | None


class LocalBackupDirectory:
    """The ``local`` location: files under the configured backup directory.

    Names are relative POSIX paths confined to ``root``: absolute paths, ``..``
    and backslashes are refused, and a name whose resolved parent leaves the
    root (through a symlinked directory) or that is not a regular file is
    refused rather than followed. Listing skips symlinks.
    """

    provider = "filesystem"
    location = LOCAL_LOCATION

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(str(self.root))

    def _path(self, name: str) -> Path:
        try:
            validate_object_name(name)
        except CatalogError as exc:
            raise ValueError(str(exc)) from exc
        path = self.root.joinpath(*PurePosixPath(name).parts)
        parent = path.parent.resolve(strict=False)
        if parent != self.root and self.root not in parent.parents:
            raise ValueError("Artifact path escapes the backup directory.")
        return parent / path.name

    def delete(self, name: str) -> None:
        path = self._path(name)
        metadata = os.lstat(path)  # FileNotFoundError when already gone
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Artifact path is not a regular file.")
        os.unlink(path)

    def list(self, prefix: str = "") -> list[LocalObject]:
        objects: list[LocalObject] = []
        for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(directory, d)))
            for filename in sorted(filenames):
                path = Path(directory, filename)
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                name = path.relative_to(self.root).as_posix()
                if not name.startswith(prefix):
                    continue
                objects.append(
                    LocalObject(
                        name=name,
                        size=metadata.st_size,
                        modified=datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc),
                    )
                )
        return objects


def local_target_resolver(
    backup_dir: str | os.PathLike[str], remote: Callable[[str], GroomableTarget] | None = None
) -> TargetResolver:
    """Resolve ``local`` to the backup directory and anything else through ``remote``."""

    local = LocalBackupDirectory(backup_dir)

    def resolve(location: str) -> GroomableTarget:
        if location == LOCAL_LOCATION:
            return local
        if remote is None:
            raise LookupError(f"No archive target is configured for location {location!r}.")
        return remote(location)

    return resolve
