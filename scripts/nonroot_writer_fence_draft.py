"""INACTIVE DRAFT: owner-dependent orchestration model for issue #884.

No concrete backend, CLI apply path, helper import, ownership syscall, service
hook or automatic activation is provided. The existing one-shot helper's
stop-EVERY-writer precondition remains unchanged. This module alone is NOT a
writer fence. Its local single-use lock only protects coordinator state.

Selected draft option: an external authority owns a declared deployment and ALL
writer launch/restart paths, including host jobs, descendants and writers that
already hold filesystem descriptors. It must atomically acquire exclusive
ownership and close admission BEFORE returning a lease; retain both throughout
inventory/mutation and after any exception, interrupt, caller exit or lost
connection; and independently enumerate/prove the complete writer set. Partial
acquisition failure must retain any closed admissions, never silently reopen.
Stopping means verified termination/drain of every writer and descendant, not a
service-stop acknowledgement. Inspection must be fresh, bounded and fail closed
on unavailable evidence. A PID alone is not a writer identity.

These are backend obligations, NOT enforcement implemented by this state
machine. Evidence consistency checks cannot authenticate a dishonest backend,
discover omitted host jobs or stop out-of-band writers. A real integration must
prove continuous admission authority (including process/backend death), stable
root/instance binding and independently complete inventory; booleans or an
operator's declaration alone do not prove it. No such integration is supplied.

Success intentionally leaves admission CLOSED and ownership HELD. There is no
release/restart API: recovery/reopening requires a separately designed owner
protocol after mutation/rollback completion is proved. Callbacks are trusted,
synchronous, bounded fixture operations; they must not defer work beyond run().
The optional callback checkpoint detects reported drift, not a TOCTOU fix. It
cannot replace the backend's continuous fence. This draft does not implement
rollback; mutation/rollback must finish under the same held backend authority.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Protocol, TypeVar

CORE_ROLES = frozenset({"ui", "history", "admin", "backup", "backup-scheduler"})
ROLES = CORE_ROLES | {"host-job"}
Plan = TypeVar("Plan")


class FenceRefused(RuntimeError):
    """No safe transition can be established from the available evidence."""


class Phase(Enum):
    NEW = "new"
    ACQUIRING = "acquiring"
    STOPPING = "stopping"
    INVENTORY = "inventory"
    MUTATION = "mutation"
    COMPLETE_HELD = "complete-held"
    FAILED = "failed"


@dataclass(frozen=True)
class Writer:
    name: str
    role: str
    owner: str


@dataclass(frozen=True)
class Deployment:
    key: str
    owner: str
    root: Path
    writers: tuple[Writer, ...]
    # Required, including an explicitly empty tuple. Never infer no host jobs.
    host_jobs: tuple[str, ...]


@dataclass(frozen=True)
class Observation:
    writer: Writer
    state: str  # Only "running"/"stopped" are verifiable in this draft.
    identity: str  # Backend-bound managed slot/instance generation, NOT just PID.


@dataclass(frozen=True)
class Evidence:
    deployment: Deployment
    lease_id: str
    root_identity: tuple[int, int]  # Backend-bound device/inode, not path alone.
    exclusive: bool
    admission_closed: bool
    inventory_complete: bool
    observations: tuple[Observation, ...]


class Lease(Protocol):
    """External continuously enforced authority, retained even on caller death.

    inspect() reports all actual writers, NOT merely echoes the declaration.
    stop() acts only on owned identities and waits/drains with a bounded timeout.
    No release method is intentionally available to this draft coordinator.
    """

    def inspect(self) -> Evidence: ...

    def stop(self, writer: Writer) -> None: ...


class Backend(Protocol):
    def acquire(self, deployment: Deployment) -> Lease:
        """Atomically claim the bound root and close all writer admission.

        Refuse unsupported authority/completeness. Failure must not reopen any
        partially closed gate. A helper-local/advisory lock cannot satisfy this.
        """
        ...


def _nonempty(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_deployment(deployment: Deployment) -> None:
    if not _nonempty(deployment.key) or not _nonempty(deployment.owner):
        raise FenceRefused("explicit deployment identity and owner required")
    if not isinstance(deployment.root, Path) or not deployment.root.is_absolute() or ".." in deployment.root.parts:
        raise FenceRefused("absolute unambiguous deployment root required")
    if not isinstance(deployment.writers, tuple) or not isinstance(deployment.host_jobs, tuple):
        raise FenceRefused("explicit immutable writer and host-job declarations required")
    names = [writer.name for writer in deployment.writers]
    if len(names) != len(set(names)):
        raise FenceRefused("duplicate writer declaration")
    for writer in deployment.writers:
        if not _nonempty(writer.name) or writer.role not in ROLES or writer.owner != deployment.owner:
            raise FenceRefused("missing, unsupported or unowned writer declaration")
    if not CORE_ROLES.issubset({writer.role for writer in deployment.writers}):
        raise FenceRefused("all core writer roles must be explicitly declared")
    hosts = {writer.name for writer in deployment.writers if writer.role == "host-job"}
    if len(deployment.host_jobs) != len(set(deployment.host_jobs)) or set(deployment.host_jobs) != hosts:
        raise FenceRefused("host jobs must exactly match declared host writers")


class Coordinator:
    """Single-use synchronous state machine; no default backend or live effects."""

    def __init__(self, deployment: Deployment, backend: Backend | None = None):
        self._deployment = deployment
        self._backend = backend
        self._phase = Phase.NEW
        self._claim_lock = Lock()  # State/reentry only; NEVER the all-writer fence.
        self._lease: Lease | None = None
        self._initial: Evidence | None = None

    @property
    def phase(self) -> Phase:
        return self._phase

    def _check(self, *, stopped: bool) -> None:
        if self._lease is None:
            raise FenceRefused("backend lease unavailable")
        evidence = self._lease.inspect()
        if evidence.deployment != self._deployment:
            raise FenceRefused("backend deployment ownership/binding mismatch")
        if any(flag is not True for flag in (
            evidence.exclusive, evidence.admission_closed, evidence.inventory_complete,
        )):
            raise FenceRefused("exclusive all-writer admission authority is unproven")
        if not _nonempty(evidence.lease_id):
            raise FenceRefused("lease identity unavailable")
        if (
            not isinstance(evidence.root_identity, tuple)
            or len(evidence.root_identity) != 2
            or any(type(part) is not int or part < 0 for part in evidence.root_identity)
        ):
            raise FenceRefused("bound root identity unavailable")
        observations = evidence.observations
        if len(observations) != len(self._deployment.writers) or {
            observation.writer for observation in observations
        } != set(self._deployment.writers):
            raise FenceRefused("missing, duplicate, unowned or undeclared writers")
        for observation in observations:
            if not _nonempty(observation.identity) or observation.state not in {"running", "stopped"}:
                raise FenceRefused("writer state or instance identity unverifiable")
            if stopped and observation.state != "stopped":
                raise FenceRefused("writer termination not verified")
        if self._initial is None:
            self._initial = evidence
        elif (
            evidence.lease_id != self._initial.lease_id
            or evidence.root_identity != self._initial.root_identity
            or {item.writer: item.identity for item in observations}
            != {item.writer: item.identity for item in self._initial.observations}
        ):
            raise FenceRefused("deployment, lease or writer identity changed")

    def _checkpoint(self) -> None:
        if self._phase not in {Phase.INVENTORY, Phase.MUTATION}:
            raise FenceRefused("checkpoint outside inventory/mutation window")
        try:
            self._check(stopped=True)
        except BaseException:
            # A callback must not swallow failed evidence and recover authority.
            self._phase = Phase.FAILED
            raise

    def run(
        self,
        inventory: Callable[[Path, Callable[[], None]], Plan],
        mutate: Callable[[Plan, Callable[[], None]], None],
    ) -> None:
        with self._claim_lock:
            if self._phase is not Phase.NEW:
                raise FenceRefused("coordinator is single-use")
            self._phase = Phase.ACQUIRING
        try:
            _validate_deployment(self._deployment)
            if self._backend is None:
                raise FenceRefused("inactive draft requires an explicit enforcing backend")
            self._lease = self._backend.acquire(self._deployment)
            self._check(stopped=False)
            self._phase = Phase.STOPPING
            for writer in self._deployment.writers:
                self._lease.stop(writer)
            self._check(stopped=True)
            self._phase = Phase.INVENTORY
            plan = inventory(self._deployment.root, self._checkpoint)
            self._checkpoint()
            self._phase = Phase.MUTATION
            mutate(plan, self._checkpoint)
            self._checkpoint()
            self._phase = Phase.COMPLETE_HELD
        except BaseException:
            # Do not release, restart, hide interrupts or imply rollback succeeded.
            self._phase = Phase.FAILED
            raise
