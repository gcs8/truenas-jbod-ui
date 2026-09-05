from __future__ import annotations

import argparse
import errno
import os
import resource
import stat
from pathlib import Path

APP_RECURSIVE_ROOTS = ("data", "history", "logs")
APP_CONFIG_PATHS = ("config.yaml", "ssh", "tls")
MAX_ENTRIES = 100_000
MAX_TREE_DEPTH = 256
DESCRIPTOR_RESERVE = 64
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
_STAT_SUPPORTS_NOFOLLOW = os.stat in os.supports_follow_symlinks


class OwnershipEntries(list[tuple[Path, os.stat_result]]):
    def __init__(
        self,
        root: Path,
        root_metadata: os.stat_result,
        entries: list[tuple[Path, os.stat_result]],
    ) -> None:
        super().__init__(entries)
        self.root = root
        self.root_metadata = root_metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight or apply bounded non-root bind-mount ownership.")
    parser.add_argument("root", type=Path, help="Deployment directory containing the known runtime roots.")
    parser.add_argument("--uid", type=int, default=10001)
    parser.add_argument("--gid", type=int, default=10001)
    parser.add_argument("--apply", action="store_true", help="Apply ownership after a complete safe preflight.")
    return parser.parse_args()


def _require_descriptor_support() -> None:
    required_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required_flags):
        raise RuntimeError("descriptor-bound ownership migration is unsupported on this platform")
    if not _OPEN_SUPPORTS_DIR_FD or not _STAT_SUPPORTS_DIR_FD:
        raise RuntimeError("descriptor-relative ownership migration is unsupported on this platform")
    if not _STAT_SUPPORTS_NOFOLLOW:
        raise RuntimeError("no-follow ownership inspection is unsupported on this platform")


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_entry(
    path: Path,
    metadata: os.stat_result,
    *,
    directory_only: bool = False,
    check_stale: bool = True,
) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"refusing symlink runtime entry: {path}")
    if directory_only:
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"runtime root is not a directory: {path}")
    elif not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
        raise ValueError(f"refusing non-file runtime entry: {path}")
    if check_stale and (
        path.name.endswith((".restore", ".tmp"))
        or (path.name.startswith(".") and ".restore-" in path.name)
    ):
        raise ValueError(f"stale replacement artifact: {path}")


def _entry_flags(metadata: os.stat_result) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if stat.S_ISDIR(metadata.st_mode):
        flags |= os.O_DIRECTORY
    return flags


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except Exception as close_error:
        raise RuntimeError("runtime descriptor cleanup failed") from close_error


def _bind_child(
    parent_descriptor: int,
    name: str,
    path: Path,
    *,
    expected: os.stat_result | None = None,
    directory_only: bool = False,
    check_stale: bool = True,
) -> tuple[int, os.stat_result]:
    inspected = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    _validate_entry(
        path,
        inspected,
        directory_only=directory_only,
        check_stale=check_stale,
    )
    if expected is not None and not _same_inode(inspected, expected):
        raise ValueError(f"runtime entry changed before descriptor binding: {path}")

    try:
        descriptor = os.open(name, _entry_flags(inspected), dir_fd=parent_descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            try:
                current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            except OSError:
                current = None
            if current is not None and stat.S_ISLNK(current.st_mode):
                raise ValueError(f"refusing symlink runtime entry: {path}") from exc
            if current is None or not _same_inode(current, inspected):
                raise ValueError(f"runtime entry changed during descriptor binding: {path}") from exc
        raise
    try:
        opened = os.fstat(descriptor)
        if not _same_inode(opened, inspected):
            raise ValueError(f"runtime entry changed during descriptor binding: {path}")
        if expected is not None and not _same_inode(opened, expected):
            raise ValueError(f"runtime entry changed during ownership migration: {path}")
        if (
            check_stale
            and path.name.startswith(".history-replacement-")
            and stat.S_ISDIR(opened.st_mode)
        ):
            with os.scandir(descriptor) as directory:
                if next(directory, None) is not None:
                    raise ValueError(f"stale replacement artifact: {path}")
    except BaseException:
        _close_descriptor(descriptor)
        raise
    return descriptor, opened


def _open_approved_root(root: Path) -> tuple[Path, int, os.stat_result]:
    _require_descriptor_support()
    unresolved_root = Path(root)
    if ".." in unresolved_root.parts:
        raise ValueError(f"refusing parent traversal in deployment root: {root}")
    absolute_root = Path(os.path.abspath(os.fspath(unresolved_root)))
    descriptor = os.open("/", os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    current_path = Path("/")
    try:
        for component in absolute_root.parts[1:]:
            current_path /= component
            next_descriptor, _ = _bind_child(
                descriptor,
                component,
                current_path,
                directory_only=True,
                check_stale=False,
            )
            previous_descriptor = descriptor
            descriptor = next_descriptor
            _close_descriptor(previous_descriptor)
        root_metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise ValueError(f"runtime root is not a directory: {absolute_root}")
    except BaseException:
        _close_descriptor(descriptor)
        raise
    return absolute_root, descriptor, root_metadata


def _append_entry(
    entries: list[tuple[Path, os.stat_result]],
    seen: set[Path],
    path: Path,
    metadata: os.stat_result,
) -> None:
    if path in seen:
        return
    entries.append((path, metadata))
    seen.add(path)
    if len(entries) > MAX_ENTRIES:
        raise ValueError("runtime ownership preflight exceeded its bounded entry limit")


def _open_tree_directory(
    root_descriptor: int,
    runtime_root: Path,
    root_metadata: os.stat_result,
    chain: tuple[tuple[str, os.stat_result], ...],
) -> tuple[int, os.stat_result, bool]:
    descriptor = root_descriptor
    descriptor_owned = False
    current_path = runtime_root
    current_metadata = os.fstat(descriptor)
    if not _same_inode(current_metadata, root_metadata):
        raise ValueError(f"runtime entry changed during ownership migration: {runtime_root}")
    try:
        for name, expected in chain:
            current_path /= name
            next_descriptor, current_metadata = _bind_child(
                descriptor,
                name,
                current_path,
                expected=expected,
                directory_only=True,
            )
            previous_descriptor = descriptor
            previous_owned = descriptor_owned
            descriptor = next_descriptor
            descriptor_owned = True
            if previous_owned:
                _close_descriptor(previous_descriptor)
    except BaseException:
        if descriptor_owned:
            _close_descriptor(descriptor)
        raise
    return descriptor, current_metadata, descriptor_owned


def _append_tree(
    entries: list[tuple[Path, os.stat_result]],
    seen: set[Path],
    runtime_root: Path,
    root_descriptor: int,
    root_metadata: os.stat_result,
) -> None:
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError(f"runtime root is not a directory: {runtime_root}")

    pending: list[
        tuple[
            Path,
            os.stat_result,
            tuple[tuple[str, os.stat_result], ...] | None,
        ]
    ] = [(runtime_root, root_metadata, ())]
    while pending:
        current_path, expected, chain = pending.pop()
        if chain is None:
            _append_entry(entries, seen, current_path, expected)
            continue
        if len(chain) > MAX_TREE_DEPTH:
            raise ValueError("runtime ownership preflight exceeded its bounded depth limit")

        descriptor, metadata, descriptor_owned = _open_tree_directory(
            root_descriptor,
            runtime_root,
            root_metadata,
            chain,
        )
        try:
            _append_entry(entries, seen, current_path, metadata)
            with os.scandir(descriptor) as directory:
                names = sorted((entry.name for entry in directory), reverse=True)
            for name in names:
                candidate = current_path / name
                child_descriptor, child_metadata = _bind_child(descriptor, name, candidate)
                try:
                    if len(entries) + len(pending) + 1 > MAX_ENTRIES:
                        raise ValueError("runtime ownership preflight exceeded its bounded entry limit")
                    if stat.S_ISDIR(child_metadata.st_mode):
                        child_chain = chain + ((name, child_metadata),)
                        if len(child_chain) > MAX_TREE_DEPTH:
                            raise ValueError(
                                "runtime ownership preflight exceeded its bounded depth limit"
                            )
                        pending.append((candidate, child_metadata, child_chain))
                    else:
                        pending.append((candidate, child_metadata, None))
                finally:
                    _close_descriptor(child_descriptor)
        finally:
            if descriptor_owned:
                _close_descriptor(descriptor)


def _bind_optional_child(
    parent_descriptor: int,
    name: str,
    path: Path,
) -> tuple[int, os.stat_result] | None:
    try:
        inspected = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _validate_entry(path, inspected)
    return _bind_child(
        parent_descriptor,
        name,
        path,
        expected=inspected,
    )


def inventory(root: Path) -> OwnershipEntries:
    absolute_root, root_descriptor, root_metadata = _open_approved_root(root)
    entries: list[tuple[Path, os.stat_result]] = []
    seen: set[Path] = set()
    try:
        config_root = absolute_root / "config"
        config_bound = _bind_optional_child(root_descriptor, "config", config_root)
        if config_bound is not None:
            config_descriptor, config_metadata = config_bound
            try:
                if not stat.S_ISDIR(config_metadata.st_mode):
                    raise ValueError(f"runtime root is not a directory: {config_root}")
                _append_entry(entries, seen, config_root, config_metadata)
                for name in APP_CONFIG_PATHS:
                    candidate = config_root / name
                    candidate_bound = _bind_optional_child(config_descriptor, name, candidate)
                    if candidate_bound is None:
                        continue
                    candidate_descriptor, candidate_metadata = candidate_bound
                    try:
                        if stat.S_ISDIR(candidate_metadata.st_mode):
                            _append_tree(
                                entries,
                                seen,
                                candidate,
                                candidate_descriptor,
                                candidate_metadata,
                            )
                        else:
                            _append_entry(entries, seen, candidate, candidate_metadata)
                    finally:
                        _close_descriptor(candidate_descriptor)
            finally:
                _close_descriptor(config_descriptor)

        for name in APP_RECURSIVE_ROOTS:
            runtime_root = absolute_root / name
            runtime_bound = _bind_optional_child(root_descriptor, name, runtime_root)
            if runtime_bound is None:
                continue
            runtime_descriptor, runtime_metadata = runtime_bound
            try:
                _append_tree(
                    entries,
                    seen,
                    runtime_root,
                    runtime_descriptor,
                    runtime_metadata,
                )
            finally:
                _close_descriptor(runtime_descriptor)
    finally:
        _close_descriptor(root_descriptor)
    return OwnershipEntries(absolute_root, root_metadata, entries)


def _target_mode(metadata: os.stat_result) -> int:
    return 0o770 if stat.S_ISDIR(metadata.st_mode) else 0o660


def _close_opened_descriptors(opened: list[tuple[int, Path, os.stat_result]]) -> None:
    close_failures: list[Exception] = []
    for descriptor, _, _ in reversed(opened):
        try:
            os.close(descriptor)
        except Exception as exc:
            close_failures.append(exc)
    if close_failures:
        raise RuntimeError("runtime descriptor cleanup failed") from close_failures[0]


def apply_ownership(
    entries: OwnershipEntries,
    *,
    uid: int,
    gid: int,
) -> None:
    if not isinstance(entries, OwnershipEntries):
        raise TypeError("ownership entries must come from descriptor-bound inventory")
    descriptor_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    required_descriptors = len(entries) + 1
    if descriptor_limit != resource.RLIM_INFINITY and required_descriptors > max(
        descriptor_limit - DESCRIPTOR_RESERVE,
        0,
    ):
        raise ValueError("runtime ownership apply exceeds the available descriptor budget")

    root, root_descriptor, current_root_metadata = _open_approved_root(entries.root)
    if not _same_inode(current_root_metadata, entries.root_metadata):
        _close_descriptor(root_descriptor)
        raise ValueError(f"deployment root changed during ownership migration: {root}")

    opened: list[tuple[int, Path, os.stat_result]] = [
        (root_descriptor, root, current_root_metadata)
    ]
    descriptors_by_relative_path = {Path(): root_descriptor}
    changed: list[tuple[int, os.stat_result]] = []
    try:
        for path, expected in entries:
            relative = path.relative_to(root)
            if not relative.parts or relative.parts[0] not in {
                "config",
                *APP_RECURSIVE_ROOTS,
            }:
                raise ValueError(f"runtime entry is outside approved roots: {path}")
            try:
                parent_descriptor = descriptors_by_relative_path[relative.parent]
            except KeyError as exc:
                raise ValueError(f"runtime entry parent was not descriptor-bound: {path}") from exc
            descriptor, metadata = _bind_child(
                parent_descriptor,
                relative.name,
                path,
                expected=expected,
            )
            opened.append((descriptor, path, metadata))
            if stat.S_ISDIR(metadata.st_mode):
                descriptors_by_relative_path[relative] = descriptor

        for descriptor, path, metadata in opened[1:]:
            current = os.fstat(descriptor)
            if not _same_inode(current, metadata):
                raise ValueError(f"runtime descriptor changed before ownership migration: {path}")

        try:
            for descriptor, _, metadata in opened[1:]:
                desired_mode = _target_mode(metadata)
                if (
                    metadata.st_uid == uid
                    and metadata.st_gid == gid
                    and stat.S_IMODE(metadata.st_mode) == desired_mode
                ):
                    continue
                changed.append((descriptor, metadata))
                os.fchown(descriptor, uid, gid)
                os.fchmod(descriptor, desired_mode)
        except Exception:
            rollback_failures: list[Exception] = []
            for descriptor, metadata in reversed(changed):
                try:
                    os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
                    os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
                except Exception as exc:  # pragma: no cover - catastrophic host failure
                    rollback_failures.append(exc)
            if rollback_failures:
                raise RuntimeError("ownership migration failed and rollback was incomplete") from rollback_failures[0]
            raise
    finally:
        _close_opened_descriptors(opened)


def main() -> int:
    args = parse_args()
    entries = inventory(args.root)
    if args.apply:
        if os.geteuid() != 0:
            raise PermissionError("--apply requires root so rollback can restore every original owner")
        apply_ownership(entries, uid=args.uid, gid=args.gid)
    print(f"preflight=ok entries={len(entries)} apply={str(args.apply).lower()} uid={args.uid} gid={args.gid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
