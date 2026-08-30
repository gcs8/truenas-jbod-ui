from __future__ import annotations

import argparse
import os
import resource
import stat
from pathlib import Path

APP_RECURSIVE_ROOTS = ("data", "history", "logs")
APP_CONFIG_PATHS = ("config.yaml", "ssh", "tls")
MAX_ENTRIES = 100_000
DESCRIPTOR_RESERVE = 64


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight or apply bounded non-root bind-mount ownership.")
    parser.add_argument("root", type=Path, help="Deployment directory containing the known runtime roots.")
    parser.add_argument("--uid", type=int, default=10001)
    parser.add_argument("--gid", type=int, default=10001)
    parser.add_argument("--apply", action="store_true", help="Apply ownership after a complete safe preflight.")
    return parser.parse_args()


def open_verified(path: Path, expected: os.stat_result) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if stat.S_ISDIR(expected.st_mode):
        flags |= getattr(os, "O_DIRECTORY", 0)
    elif not stat.S_ISREG(expected.st_mode):
        raise ValueError(f"refusing non-file runtime entry: {path}")
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError(f"runtime entry changed during preflight: {path}")
    except BaseException:
        try:
            os.close(descriptor)
        except Exception as close_error:
            raise RuntimeError("runtime descriptor cleanup failed") from close_error
        raise
    return descriptor


def _inspect_entry(path: Path) -> os.stat_result:
    metadata = os.stat(path, follow_symlinks=False)
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"refusing symlink runtime entry: {path}")
    if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
        raise ValueError(f"refusing non-file runtime entry: {path}")
    if path.name.endswith((".restore", ".tmp")) or (
        path.name.startswith(".") and ".restore-" in path.name
    ):
        raise ValueError(f"stale replacement artifact: {path}")
    if path.name.startswith(".history-replacement-") and stat.S_ISDIR(metadata.st_mode):
        with os.scandir(path) as directory:
            if next(directory, None) is not None:
                raise ValueError(f"stale replacement artifact: {path}")
    return metadata


def _append_tree(
    entries: list[tuple[Path, os.stat_result]],
    seen: set[Path],
    runtime_root: Path,
) -> None:
    root_metadata = _inspect_entry(runtime_root)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError(f"runtime root is not a directory: {runtime_root}")
    def raise_walk_error(error: OSError) -> None:
        raise error

    for dirpath, dirnames, filenames in os.walk(
        runtime_root,
        topdown=True,
        onerror=raise_walk_error,
        followlinks=False,
    ):
        current = Path(dirpath)
        for candidate in (
            current,
            *(current / item for item in dirnames),
            *(current / item for item in filenames),
        ):
            if candidate in seen:
                continue
            entries.append((candidate, _inspect_entry(candidate)))
            seen.add(candidate)
            if len(entries) > MAX_ENTRIES:
                raise ValueError("runtime ownership preflight exceeded its bounded entry limit")


def inventory(root: Path) -> list[tuple[Path, os.stat_result]]:
    entries: list[tuple[Path, os.stat_result]] = []
    seen: set[Path] = set()
    config_root = root / "config"
    try:
        config_metadata = _inspect_entry(config_root)
    except FileNotFoundError:
        config_metadata = None
    if config_metadata is not None:
        if not stat.S_ISDIR(config_metadata.st_mode):
            raise ValueError(f"runtime root is not a directory: {config_root}")
        entries.append((config_root, config_metadata))
        seen.add(config_root)
        for name in APP_CONFIG_PATHS:
            candidate = config_root / name
            try:
                candidate_metadata = os.stat(candidate, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(candidate_metadata.st_mode):
                _append_tree(entries, seen, candidate)
            else:
                if candidate not in seen:
                    entries.append((candidate, _inspect_entry(candidate)))
                    seen.add(candidate)
    for name in APP_RECURSIVE_ROOTS:
        runtime_root = root / name
        try:
            os.stat(runtime_root, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _append_tree(entries, seen, runtime_root)
    return entries


def _verify_path_identity(path: Path, expected: os.stat_result) -> None:
    current = os.stat(path, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError(f"runtime entry changed during ownership migration: {path}")


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
    entries: list[tuple[Path, os.stat_result]],
    *,
    uid: int,
    gid: int,
) -> None:
    descriptor_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if descriptor_limit != resource.RLIM_INFINITY and len(entries) > max(
        descriptor_limit - DESCRIPTOR_RESERVE,
        0,
    ):
        raise ValueError("runtime ownership apply exceeds the available descriptor budget")

    opened: list[tuple[int, Path, os.stat_result]] = []
    changed: list[tuple[int, os.stat_result]] = []
    try:
        for path, metadata in entries:
            opened.append((open_verified(path, metadata), path, metadata))
        for _, path, metadata in opened:
            _verify_path_identity(path, metadata)

        try:
            for descriptor, _, metadata in opened:
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
            for _, path, metadata in opened:
                _verify_path_identity(path, metadata)
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
    root = args.root.resolve(strict=True)
    entries = inventory(root)
    if args.apply:
        if os.geteuid() != 0:
            raise PermissionError("--apply requires root so rollback can restore every original owner")
        apply_ownership(entries, uid=args.uid, gid=args.gid)
    print(f"preflight=ok entries={len(entries)} apply={str(args.apply).lower()} uid={args.uid} gid={args.gid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
