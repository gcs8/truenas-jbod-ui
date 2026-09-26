"""Apply config.yaml, runtime-overrides.yaml and profiles.yaml edits without a restart (#432).

The main UI keeps one *settings generation* at a time: the validated settings
plus the objects built from them (the inventory registry, the history client,
the snapshot exporter). Every request is pinned to the generation that was
current when it arrived, so a request that is running while the files change
sees either the old settings or the new ones, never a mix.

``ConfigReloader`` checks the watched files before non-static HTTP requests,
no more often than once per ``interval``, comparing (mtime, size, inode). A
change is read and validated in a worker thread. Valid settings replace the
current generation; invalid ones are ignored, the old settings stay in use,
and ``problem`` carries one plain sentence for the log, ``/healthz`` and the
page. Settings that only a new process can apply (``RESTART_ONLY_SETTINGS``)
keep their running values.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

import yaml

from app.config import (
    Settings,
    config_watch_paths,
    get_settings as load_cached_settings,
    load_settings,
    replace_settings,
    restart_only_changes,
    with_running_restart_only_settings,
)
from app.config_errors import ConfigurationError


logger = logging.getLogger(__name__)

DEFAULT_RELOAD_CHECK_SECONDS = 2.0
ComponentT = TypeVar("ComponentT")
FileSignature = tuple[tuple[str, tuple[int, int, int] | None], ...]
_MISSING = object()


class SettingsGeneration:
    """One validated settings object and the components built from it."""

    def __init__(
        self,
        number: int,
        settings: Settings,
        inherited: dict[str, Any] | None = None,
    ) -> None:
        self.number = number
        self.settings = settings
        self._lock = threading.RLock()
        self._components: dict[str, Any] = {}
        # The newest built instance of each component from earlier
        # generations, so a successor can reuse its caches. Dropped once this
        # generation builds its own, so old generations are not kept alive.
        self._inherited: dict[str, Any] = dict(inherited or {})

    def component(
        self,
        name: str,
        factory: Callable[[SettingsGeneration], ComponentT],
    ) -> ComponentT:
        value = self._components.get(name, _MISSING)
        if value is not _MISSING:
            return value  # type: ignore[return-value]
        with self._lock:
            value = self._components.get(name, _MISSING)
            if value is _MISSING:
                value = factory(self)
                self._components[name] = value
                self._inherited.pop(name, None)
        return value  # type: ignore[return-value]

    def inherited(self, name: str) -> Any | None:
        with self._lock:
            return self._inherited.get(name)

    def successor_inheritance(self) -> dict[str, Any]:
        with self._lock:
            return {**self._inherited, **self._components}


class SettingsRuntime:
    """The current settings generation, and the one pinned to this request."""

    def __init__(self, loader: Callable[[], Settings] = load_cached_settings) -> None:
        self._loader = loader
        self._lock = threading.Lock()
        self._current: SettingsGeneration | None = None
        self._pinned: ContextVar[SettingsGeneration | None] = ContextVar(
            "settings_generation",
            default=None,
        )

    def current(self) -> SettingsGeneration:
        """The newest generation. Follows ``app.config.get_settings()``.

        A new generation starts whenever the cached settings object changes,
        whether the reloader installed it or a caller cleared the cache.
        """
        settings = self._loader()
        generation = self._current
        if generation is not None and generation.settings is settings:
            return generation
        with self._lock:
            generation = self._current
            if generation is None or generation.settings is not settings:
                generation = SettingsGeneration(
                    (generation.number + 1) if generation is not None else 1,
                    settings,
                    inherited=generation.successor_inheritance() if generation is not None else None,
                )
                self._current = generation
            return generation

    def active(self) -> SettingsGeneration:
        """The generation pinned to the running request, else the newest one."""
        return self._pinned.get() or self.current()

    @contextmanager
    def pin(self, generation: SettingsGeneration | None = None) -> Iterator[SettingsGeneration]:
        pinned = generation or self.current()
        token = self._pinned.set(pinned)
        try:
            yield pinned
        finally:
            self._pinned.reset(token)


def _stat_one(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def file_signature(paths: tuple[Path, ...]) -> FileSignature:
    """(mtime, size, inode) per watched file; ``None`` for a missing file."""
    return tuple((str(path), _stat_one(path)) for path in paths)


# The reload warning is shown to anyone who can reach the main UI (/healthz,
# the page, /api/inventory), so it never carries file content: a YAML parser
# error quotes the offending line, which may hold a credential.
PUBLIC_RELOAD_FAILURE = (
    "Config change not applied: the edited configuration is not valid, so the main UI "
    "keeps the previous settings. The main UI log names the setting or line to fix."
)
PUBLIC_RELOAD_MISSING_CONFIG = (
    "Config change not applied: config.yaml is missing, so the main UI keeps the previous settings. "
    "Restore the file or restart the main UI to use first-start defaults."
)


def describe_reload_failure(exc: BaseException) -> str:
    """Log-only detail for a rejected config file, without any file content."""
    if isinstance(exc, ConfigurationError) and exc.problems:
        # Built from key names and requirements only (pydantic input is
        # excluded), the same sentences start-up logs.
        return "; ".join(exc.problems[:5]) + (f" (and {len(exc.problems) - 5} more)" if len(exc.problems) > 5 else "")
    mark = getattr(exc, "problem_mark", None)
    if isinstance(exc, yaml.YAMLError) and mark is not None:
        return (
            f"YAML syntax error in {getattr(mark, 'name', 'the config file')} at line "
            f"{int(getattr(mark, 'line', 0)) + 1}, column {int(getattr(mark, 'column', 0)) + 1}."
        )
    return f"{type(exc).__name__} while reading the config files."


class ConfigReloader:
    """Rate-limit request-triggered checks and swap in valid config changes."""

    def __init__(
        self,
        runtime: SettingsRuntime,
        *,
        interval_seconds: float = DEFAULT_RELOAD_CHECK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        loader: Callable[[], Settings] | None = None,
        on_applied: Callable[[Settings, Settings], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._clock = clock
        self._loader = loader
        self._on_applied = on_applied
        self._baseline: FileSignature | None = None
        self._rejected: FileSignature | None = None
        self._next_check_at = 0.0
        self._inflight: asyncio.Task[bool] | None = None
        self.problem: str | None = None
        self.restart_pending: tuple[str, ...] = ()

    def prime(self, settings: Settings | None = None) -> None:
        """Record the files the running settings were read from."""
        running = settings or self.runtime.current().settings
        self._baseline = file_signature(config_watch_paths(running))
        self._next_check_at = self._clock() + self.interval_seconds

    def problems(self) -> list[str]:
        return [self.problem] if self.problem else []

    async def maybe_reload(self) -> None:
        """Check the files if the last check is older than the interval."""
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            await asyncio.shield(inflight)
            return
        now = self._clock()
        if now < self._next_check_at:
            return
        self._next_check_at = now + self.interval_seconds
        task = asyncio.ensure_future(self.check_now())
        self._inflight = task
        try:
            await asyncio.shield(task)
        finally:
            if self._inflight is task and task.done():
                self._inflight = None

    async def check_now(self) -> bool:
        """Check the files once. Returns True when new settings were applied."""
        try:
            return await self._check()
        except Exception:  # noqa: BLE001 - a reload must never take the main UI down.
            logger.exception("Config reload check failed; the main UI keeps its current settings.")
            return False

    async def _check(self) -> bool:
        running = self.runtime.current().settings
        paths = config_watch_paths(running)
        if self._baseline is None:
            self._baseline = await asyncio.to_thread(file_signature, paths)
            return False
        signature = await asyncio.to_thread(file_signature, paths)
        if signature == self._baseline:
            # The files are back to exactly what is running (for example a
            # config.yaml moved away and restored): nothing to apply, and any
            # warning about the rejected state no longer holds.
            if self.problem or self._rejected is not None:
                logger.info("Config files match the running settings again; cleared the reload warning.")
                self.problem = None
                self._rejected = None
            return False
        if signature == self._rejected:
            return False
        baseline_by_path = dict(self._baseline)
        signature_by_path = dict(signature)
        primary_config = str(paths[0])
        if baseline_by_path.get(primary_config) is not None and signature_by_path.get(primary_config) is None:
            logger.warning(
                "Config change not applied; the main UI keeps the previous settings because the config file is missing."
            )
            self.problem = PUBLIC_RELOAD_MISSING_CONFIG
            self._rejected = signature
            return False
        try:
            loaded, after = await asyncio.to_thread(self._load, paths, running)
        except Exception as exc:  # noqa: BLE001 - invalid edits keep the old settings.
            logger.warning(
                "Config change not applied; the main UI keeps the previous settings. %s",
                describe_reload_failure(exc),
            )
            self.problem = PUBLIC_RELOAD_FAILURE
            self._rejected = signature
            return False
        if after != signature:
            # Still being written: read it again on the next check.
            return False
        return self._apply(loaded, signature)

    def _load(self, paths: tuple[Path, ...], running: Settings) -> tuple[Settings, FileSignature]:
        loaded = (
            load_settings(running_restart_only=running)
            if self._loader is None
            else self._loader()
        )
        return loaded, file_signature(paths)

    def _apply(self, loaded: Settings, signature: FileSignature) -> bool:
        running = self.runtime.current().settings
        self._baseline = signature
        self._rejected = None
        if self.problem:
            logger.info("Config is valid again; the main UI applied it.")
        self.problem = None
        restart_keys = tuple(restart_only_changes(running, loaded))
        if restart_keys and restart_keys != self.restart_pending:
            logger.warning(
                "Config change to %s needs a main UI restart to take effect; "
                "the other changes were applied.",
                ", ".join(restart_keys),
            )
        self.restart_pending = restart_keys
        effective = with_running_restart_only_settings(running, loaded)
        if effective == running:
            return False
        replace_settings(effective)
        generation = self.runtime.current()
        logger.info(
            "Applied config changes without a restart (settings generation %d).",
            generation.number,
        )
        if self._on_applied is not None:
            try:
                self._on_applied(running, effective)
            except Exception:  # noqa: BLE001 - the swap already happened; report and keep serving.
                logger.exception("Config reload follow-up failed.")
        return True


class ConfigReloadMiddleware:
    """Check for config changes, then pin the request to one settings generation."""

    def __init__(self, app: Any, *, reloader: ConfigReloader, skip_prefixes: tuple[str, ...] = ("/static/",)) -> None:
        self.app = app
        self.reloader = reloader
        self.skip_prefixes = skip_prefixes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or str(scope.get("path") or "").startswith(self.skip_prefixes):
            await self.app(scope, receive, send)
            return
        await self.reloader.maybe_reload()
        with self.reloader.runtime.pin():
            await self.app(scope, receive, send)


def install_config_reload(
    app: Any,
    runtime: SettingsRuntime,
    startup_settings: Settings,
    *,
    on_applied: Callable[[Any, Settings, Settings], None] | None = None,
) -> ConfigReloader:
    """Watch the config files for ``app`` and pin each request to one generation."""

    def applied(before: Settings, after: Settings) -> None:
        if on_applied is not None:
            on_applied(app, before, after)

    reloader = ConfigReloader(runtime, on_applied=applied)
    reloader.prime(startup_settings)
    app.state.config_reloader = reloader
    app.add_middleware(ConfigReloadMiddleware, reloader=reloader)
    return reloader


def config_reload_problems(request: Any = None, *, include_restart_notice: bool = True) -> list[str]:
    """Plain warnings about config edits the running main UI has not applied.

    An invalid edit is a health problem (``/healthz`` reports it as degraded).
    A valid edit to a restart-only setting is only a page notice.
    """
    app_state = getattr(getattr(request, "app", None), "state", None)
    reloader = getattr(app_state, "config_reloader", None)
    if not isinstance(reloader, ConfigReloader):
        return []
    problems = reloader.problems()
    if include_restart_notice and reloader.restart_pending:
        problems.append(
            "Config change to "
            + ", ".join(reloader.restart_pending)
            + " is saved but needs a main UI restart to take effect."
        )
    return problems
