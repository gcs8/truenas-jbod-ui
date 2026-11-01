"""Hold the deployment writer fence for this container's whole life (#884).

The image entrypoint runs ``python -I /app/app/writer_fence.py -- <command>``. When the
deployment's ``writer-fence`` directory is mounted at ``/run/writer-fence``,
this takes a shared kernel ``flock`` on it, keeps that descriptor open across
``exec`` and then becomes the requested command. The lock therefore lasts until
the container's main process exits.

``scripts/prepare_nonroot_bind_mounts.py --apply`` takes the same lock
exclusively and non-blocking, so it refuses to start while a fenced container
runs, and a fenced container started during its run waits here until it ends.

No mount means an older Compose file or a bare ``docker run``: the command
still starts, with a one-line notice, because such a container was never
fenced. A mounted fence that cannot be opened or locked refuses to start.
Kernel locks are only shared when the helper and the containers use the same
Linux kernel and a local filesystem.
"""

from __future__ import annotations

import errno
import os
import signal
import sys
from pathlib import Path

# Keep in sync with the Compose mounts and WRITER_FENCE_NAME in
# scripts/prepare_nonroot_bind_mounts.py (CONTRIBUTING.md "Things that must
# move together"). Do not create this path in the image: an unmounted image
# directory would be a private lock that fences nothing.
DEFAULT_FENCE_DIR = Path("/run/writer-fence")
USAGE = "usage: python -m app.writer_fence [--fence-dir PATH] -- COMMAND [ARGS...]"


class UsageError(ValueError):
    pass


def parse_arguments(arguments: list[str]) -> tuple[Path, list[str]]:
    fence_dir = DEFAULT_FENCE_DIR
    remaining = list(arguments)
    while remaining and remaining[0] != "--":
        option = remaining.pop(0)
        if option == "--fence-dir" and remaining and remaining[0] != "--":
            fence_dir = Path(remaining.pop(0))
        elif option.startswith("--fence-dir=") and option != "--fence-dir=":
            fence_dir = Path(option.split("=", 1)[1])
        else:
            raise UsageError(f"unexpected argument: {option}")
    if not remaining or len(remaining) < 2:
        raise UsageError("a command after -- is required")
    return fence_dir, remaining[1:]


def _exit_while_waiting(signum: int, _frame: object) -> None:
    # PID 1 ignores default-disposition SIGTERM; let docker stop end the wait.
    raise SystemExit(128 + signum)


class LockingUnsupported(OSError):
    pass


# The filesystem behind the mount has no flock (for example NFS without a lock
# manager). Nothing can be fenced there, so start unfenced with a notice.
_UNSUPPORTED_LOCK_ERRNOS = frozenset({errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOSYS})


def hold_shared_fence(fence_dir: Path) -> int | None:
    """Return an inheritable descriptor holding LOCK_SH, or None when unmounted."""
    import fcntl

    try:
        descriptor = os.open(
            fence_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_LOCK_ERRNOS:
                raise LockingUnsupported(exc.errno, exc.strerror) from exc
            if not isinstance(exc, BlockingIOError):
                raise
            previous = signal.signal(signal.SIGTERM, _exit_while_waiting)
            try:
                print(
                    "writer fence: the non-root ownership helper holds the deployment fence; "
                    "waiting for it to finish before starting",
                    file=sys.stderr,
                    flush=True,
                )
                fcntl.flock(descriptor, fcntl.LOCK_SH)
            finally:
                signal.signal(signal.SIGTERM, previous)
        os.set_inheritable(descriptor, True)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def main(arguments: list[str] | None = None) -> int:
    try:
        fence_dir, command = parse_arguments(sys.argv[1:] if arguments is None else arguments)
    except UsageError as exc:
        print(f"{USAGE}\nwriter fence: {exc}", file=sys.stderr)
        return 2
    unfenced = "this container is not fenced against the non-root ownership helper"
    try:
        if hold_shared_fence(fence_dir) is None:
            print(f"writer fence: {fence_dir} is not mounted; {unfenced}", file=sys.stderr, flush=True)
    except LockingUnsupported as exc:
        # The helper cannot lock this filesystem either, so it refuses to run.
        print(
            f"writer fence: {fence_dir} does not support kernel locks ({exc.strerror}); {unfenced}",
            file=sys.stderr,
            flush=True,
        )
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        print(f"writer fence: cannot hold {fence_dir} ({reason}); refusing to start", file=sys.stderr)
        return 1
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"writer fence: cannot run {command[0]} ({exc.strerror or type(exc).__name__})", file=sys.stderr)
        return 127
    return 0  # pragma: no cover - execvp does not return


if __name__ == "__main__":
    raise SystemExit(main())
