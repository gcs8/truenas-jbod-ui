"""One-time notice shown in the main UI after the app has been updated.

The app remembers the last version it ran as a small JSON file next to the
saved bay assignments. When the version changes, a notice is recorded and
stays until someone dismisses it, so a restart or a second browser does not
lose it. A fresh install (no file yet) records the version silently.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app import __version__


logger = logging.getLogger(__name__)

STATE_FILENAME = "last_seen_version.json"

# Releases whose upgrade notes deserve a sentence in the UI itself. Keys are
# exact version strings; the generic "Updated to vX.Y.Z." text is used otherwise.
VERSION_NOTICES: dict[str, str] = {
    "0.23.0": (
        "Updated to v0.23.0. Anyone who can reach this port can change bay "
        "assignments and lights. See Optional authentication on the Advanced "
        "Configuration wiki page to add a sign-in."
    ),
}


def notice_text(version: str) -> str:
    return VERSION_NOTICES.get(version, f"Updated to v{version}.")


def state_path(data_dir: Path) -> Path:
    return data_dir / STATE_FILENAME


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("Unable to read %s: %s", path, exc)
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        logger.warning("Ignoring unreadable version record at %s", path)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(path)
    except OSError as exc:
        logger.warning("Unable to record the app version at %s: %s", path, exc)
        return False
    return True


def current_notice(data_dir: Path, *, version: str = __version__) -> dict[str, str] | None:
    """Record the running version and return the pending notice, if any.

    Called when the main page is rendered. The first call after a version
    change stores a notice; later calls return the same notice until
    ``dismiss_notice`` clears it. The notice is only about the version that
    is running now: a stale record for an older version is replaced.
    """
    path = state_path(data_dir)
    state = _read_state(path)
    last_seen = state.get("last_seen_version")
    pending = state.get("notice") if isinstance(state.get("notice"), dict) else None

    if not isinstance(last_seen, str) or not last_seen:
        # Fresh install: remember the version, nothing to announce.
        _write_state(path, {"last_seen_version": version})
        return None

    if last_seen != version:
        pending = {"version": version, "previous": last_seen}
        _write_state(path, {"last_seen_version": version, "notice": pending})
    elif pending is not None and pending.get("version") != version:
        pending = None
        _write_state(path, {"last_seen_version": version})

    if pending is None:
        return None
    return {
        "version": version,
        "previous": str(pending.get("previous") or ""),
        "text": notice_text(version),
    }


def dismiss_notice(data_dir: Path, *, version: str = __version__) -> bool:
    """Clear the pending notice. Returns True when the record was updated."""
    path = state_path(data_dir)
    state = _read_state(path)
    if "notice" not in state and state.get("last_seen_version") == version:
        return True
    return _write_state(path, {"last_seen_version": version})
