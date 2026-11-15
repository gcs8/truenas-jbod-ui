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
* A retention deletion never relies on a kept copy that was not just checked
  (#1188). Before the first retention deletion of a (class, location) pair,
  ``apply`` re-reads the copies that justify it (the newest verified copy and
  the ``keep_count`` newest counted copies) and compares size and SHA-256 with
  the catalog. A missing or mismatching keeper is marked unverified, the pair
  is replanned and only planned items that are still due are deleted. When a
  keeper cannot be checked, that location is refused with a named reason.

``LifecycleManager.plan(now)`` is a dry run that reads only the catalog.
``apply(plan, resolver)`` deletes at most the planned items, tombstoning each
in the catalog. A location whose
target cannot be opened, or whose deletion reports a connection/timeout
failure, is skipped (recorded in ``ApplyResult.failed_locations``) while the
other locations are still groomed; any other unexpected error stops the run
with the partial progress in the result.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterable, Mapping
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
from .errors import describe_error

DEFAULT_UNVERIFIED_GRACE = timedelta(hours=24)
DEFAULT_ACTOR = "lifecycle"
_DIGEST_CHUNK = 1024 * 1024

PlanKind = Literal["retention", "unverified"]


class KeeperCheckUnavailable(RuntimeError):
    """A kept copy's payload could not be checked, so no deletion may rely on it.

    ``reason`` is a fixed code shown in grooming status (never provider text):
    ``keeper-unverifiable`` (the target offers no way to read the bytes back),
    ``keeper-unreadable`` (a local copy exists but cannot be read) or
    ``keeper-readback-failed`` (a remote readback did not complete).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"Kept copy could not be checked ({reason}).")
        self.reason = reason


class GroomableTarget(Protocol):
    """The part of ``ArchiveTarget`` grooming needs.

    ``delete`` may either return quietly (the transport targets in
    ``transport.py`` are idempotent) or raise ``FileNotFoundError`` when the
    object is already gone; only the latter is reported as ``already_missing``.
    Connection and timeout errors make that location unavailable. Any other
    exception is treated as unexpected and stops ``apply``.
    """

    def list(self, prefix: str = "") -> list[Any]: ...

    def delete(self, name: str) -> None: ...


TargetResolver = Callable[[str], GroomableTarget]
# Reads a kept copy back and returns its (size, sha256). Raises
# FileNotFoundError when the copy is gone and KeeperCheckUnavailable when it
# cannot be checked; reading stops once more than the recorded size arrived.
PayloadDigest = Callable[[ArtifactRecord, GroomableTarget], tuple[int, str]]


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
    failed_locations: Mapping[str, str] = field(default_factory=dict)
    # Keepers whose payload was missing or did not match, now marked unverified.
    downgraded: tuple[ArtifactRecord, ...] = ()
    # Planned items left in place because the replan after a downgrade keeps them.
    kept_after_replan: tuple[PlanItem, ...] = ()

    @property
    def complete(self) -> bool:
        return self.failed is None and not self.not_attempted

    def location_complete(self, location: str) -> bool:
        """True when every planned item for ``location`` was handled."""

        if location in self.failed_locations:
            return False
        if self.failed is not None and self.failed.record.location == location:
            return False
        return not any(item.record.location == location for item in self.not_attempted)


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
    # How apply() reads a kept copy back; None means local_payload_digest,
    # which checks the local backup directory and refuses every other target.
    payload_digest: PayloadDigest | None = None
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

    # -- keeper checks -------------------------------------------------------

    def _keepers(
        self, rule: RetentionRule | None, records: list[ArtifactRecord], pending: set[str]
    ) -> list[ArtifactRecord]:
        """The copies that justify deleting ``pending``: guard copy plus keep_count slots."""

        order = lambda record: (record.created_at, record.artifact_id)  # noqa: E731
        verified = [
            record
            for record in records
            if record.verified
            and record.artifact_id not in pending
            and not self.catalog.is_deletion_claimed(record.artifact_id)
        ]
        keepers: dict[str, ArtifactRecord] = {}
        newest = max(verified, key=order, default=None)
        if newest is not None:
            keepers[newest.artifact_id] = newest
        if rule is not None and rule.keep_count:
            counted = sorted((r for r in verified if not r.preserved), key=order, reverse=True)
            for record in counted[: rule.keep_count]:
                keepers[record.artifact_id] = record
        return sorted(keepers.values(), key=order, reverse=True)

    def _keeper_intact(self, record: ArtifactRecord, target: GroomableTarget) -> bool:
        digest = self.payload_digest or local_payload_digest
        try:
            size, sha256 = digest(record, target)
        except FileNotFoundError:
            return False
        return size == record.size and sha256 == record.sha256

    def _confirm_keepers(
        self,
        backup_class: str,
        location: str,
        pending: set[str],
        target: GroomableTarget,
        planned_at: datetime,
        downgraded: list[ArtifactRecord],
    ) -> set[str]:
        """Check the keepers of one pair; return the planned ids still safe to delete.

        A keeper that is missing or does not match is marked unverified (and
        appended to ``downgraded``) and the pair is replanned; the loop ends
        because every pass downgrades at least one verified record or confirms
        all keepers. Each keeper is read at most once per run.
        ``KeeperCheckUnavailable`` and connection errors propagate so the
        caller refuses the location.
        """

        rule = self._rules.get((backup_class, location))
        intact: dict[str, bool] = {}
        while True:
            records = self.catalog.list(backup_class=backup_class, location=location)
            bad: list[ArtifactRecord] = []
            for keeper in self._keepers(rule, records, pending):
                if keeper.artifact_id not in intact:
                    intact[keeper.artifact_id] = self._keeper_intact(keeper, target)
                if not intact[keeper.artifact_id]:
                    bad.append(keeper)
            if not bad:
                return pending
            for keeper in bad:
                downgraded.append(self.catalog.mark_unverified(keeper.artifact_id))
            if rule is None:
                return set()
            replanned, _guarded = self._plan_group(
                rule, self.catalog.list(backup_class=backup_class, location=location), planned_at
            )
            # Never add a deletion the reviewed plan did not contain.
            pending = pending & {item.record.artifact_id for item in replanned if item.kind == "retention"}

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
        location is tombstoned as missing. When a location's target cannot be
        resolved, or deletion reports a connection/timeout failure, that
        location's items are skipped and the others continue. Any other failure
        releases the claim and stops the run.

        Before the first retention deletion of each (class, location) pair the
        pair's keepers are read back (see ``_confirm_keepers``). A downgraded
        keeper can only shrink the set of deletions; a keeper that cannot be
        checked refuses the whole location with a ``KeeperCheckUnavailable``
        reason in ``failed_locations``.
        """

        clock = now or (lambda: datetime.now(timezone.utc))
        deleted: list[PlanItem] = []
        missing: list[PlanItem] = []
        targets: dict[str, GroomableTarget] = {}
        failed_locations: dict[str, str] = {}
        first_failure: tuple[PlanItem, str] | None = None
        skipped: list[PlanItem] = []
        downgraded: list[ArtifactRecord] = []
        kept: list[PlanItem] = []
        confirmed: dict[tuple[str, str], set[str]] = {}
        items = list(plan.items)
        for index, item in enumerate(items):
            record = item.record

            def stop(message: str) -> ApplyResult:
                return ApplyResult(
                    deleted=tuple(deleted),
                    already_missing=tuple(missing),
                    failed=item,
                    error=message,
                    not_attempted=tuple(skipped + items[index + 1 :]),
                    failed_locations=failed_locations,
                    downgraded=tuple(downgraded),
                    kept_after_replan=tuple(kept),
                )

            if record.location in failed_locations:
                skipped.append(item)
                continue
            if record.location not in targets:
                try:
                    targets[record.location] = target_resolver(record.location)
                except Exception as exc:  # noqa: BLE001 - reported per location, not swallowed
                    failed_locations[record.location] = describe_error(exc)
                    first_failure = first_failure or (item, failed_locations[record.location])
                    skipped.append(item)
                    continue
            target = targets[record.location]
            if item.kind == "retention":
                group = (record.backup_class, record.location)
                if group not in confirmed:
                    pending = {
                        entry.record.artifact_id
                        for entry in items
                        if entry.kind == "retention"
                        and (entry.record.backup_class, entry.record.location) == group
                    }
                    try:
                        confirmed[group] = self._confirm_keepers(
                            *group, pending, target, plan.generated_at, downgraded
                        )
                    except KeeperCheckUnavailable as exc:
                        message = str(exc)
                    except (ConnectionError, TimeoutError) as exc:
                        message = describe_error(exc)
                    except Exception as exc:  # noqa: BLE001 - refuse the location, never delete unchecked
                        message = f"{KeeperCheckUnavailable('keeper-check-failed')} {describe_error(exc)}"
                    else:
                        message = None
                    if message is not None:
                        failed_locations[record.location] = message
                        first_failure = first_failure or (item, message)
                        skipped.append(item)
                        continue
                if record.artifact_id not in confirmed[group]:
                    kept.append(item)
                    continue
            try:
                self.catalog.claim_deletion(record, actor=actor, now=clock())
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                return stop(describe_error(exc))
            gone = False
            try:
                target.delete(record.name)
            except FileNotFoundError:
                gone = True
            except (ConnectionError, TimeoutError) as exc:
                self.catalog.release_deletion(record.artifact_id)
                message = describe_error(exc)
                failed_locations[record.location] = message
                first_failure = first_failure or (item, message)
                skipped.append(item)
                continue
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                self.catalog.release_deletion(record.artifact_id)
                return stop(describe_error(exc))
            try:
                reason = f"{item.kind}: {item.reason}"
                if gone:
                    reason += "; object was already missing"
                self.catalog.record_deletion(
                    record.artifact_id, reason=reason, actor=actor, now=clock(), expected=record
                )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                return stop(describe_error(exc))
            (missing if gone else deleted).append(item)
        failed, error = first_failure or (None, None)
        return ApplyResult(
            deleted=tuple(deleted),
            already_missing=tuple(missing),
            failed=failed,
            error=error,
            not_attempted=tuple(entry for entry in skipped if entry is not failed),
            failed_locations=failed_locations,
            downgraded=tuple(downgraded),
            kept_after_replan=tuple(kept),
        )


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

    def digest(self, name: str, *, limit: int) -> tuple[int, str]:
        """(size, sha256) of a regular file, read in chunks; stops once ``limit`` is exceeded.

        Raises ``FileNotFoundError`` when the file is gone, ``ValueError`` for
        a path that escapes the root or is not a regular file, and ``OSError``
        when it cannot be opened or read (a symlink is refused, not followed).
        """

        path = self._path(name)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Artifact path is not a regular file.")
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(descriptor, _DIGEST_CHUNK):
                size += len(chunk)
                if size > limit:
                    break
                digest.update(chunk)
            return size, digest.hexdigest()
        finally:
            os.close(descriptor)

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


def local_payload_digest(record: ArtifactRecord, target: GroomableTarget) -> tuple[int, str]:
    """Default ``PayloadDigest``: hash a copy in the local backup directory.

    Any other target cannot be content-verified here and is refused, so a
    caller that grooms remote locations must supply its own bounded readback.
    """

    if not isinstance(target, LocalBackupDirectory):
        raise KeeperCheckUnavailable("keeper-unverifiable")
    try:
        return target.digest(record.name, limit=record.size)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise KeeperCheckUnavailable("keeper-unreadable") from exc


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
