from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RELEASE_CHECK_REPO = "gcs8/truenas-jbod-ui"
DEFAULT_RELEASE_CHECK_INTERVAL_SECONDS = 86400
DEFAULT_RELEASE_CHECK_TIMEOUT_SECONDS = 5.0

_VERSION_RE = re.compile(r"^v?(?P<core>\d+(?:\.\d+){1,3})(?P<suffix>.*)$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _normalize_tag(tag: str | None) -> str | None:
    if tag is None:
        return None
    normalized = str(tag).strip()
    if not normalized:
        return None
    return normalized if normalized.startswith("v") else f"v{normalized}"


def _parse_version(value: str | None) -> tuple[tuple[int, ...], bool] | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    match = _VERSION_RE.match(normalized)
    if not match:
        return None
    core = tuple(int(part) for part in match.group("core").split("."))
    suffix = match.group("suffix").strip().lower()
    return core, bool(suffix)


def _compare_version_tuples(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    left_parts = left + (0,) * (width - len(left))
    right_parts = right + (0,) * (width - len(right))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


def describe_release_status(current_version: str, latest_tag: str | None) -> tuple[str, str]:
    latest_display = _normalize_tag(latest_tag)
    if not latest_display:
        return "unknown", "Latest release unavailable"

    current_normalized = str(current_version or "").strip()
    if not current_normalized:
        return "unknown", f"Latest stable {latest_display}"

    if current_normalized == latest_display or f"v{current_normalized}" == latest_display:
        return "current", "Latest tagged release"

    parsed_current = _parse_version(current_normalized)
    parsed_latest = _parse_version(latest_display)
    if not parsed_current or not parsed_latest:
        return "known", f"Latest stable {latest_display}"

    current_core, current_has_suffix = parsed_current
    latest_core, _ = parsed_latest
    comparison = _compare_version_tuples(current_core, latest_core)

    if comparison < 0:
        return "update-available", f"Update available: {latest_display}"
    if comparison == 0:
        if current_has_suffix:
            return "dev-build", f"Pre-release build for {latest_display}"
        return "current", "Latest tagged release"
    if current_has_suffix:
        return "dev-build", f"Dev build · latest stable {latest_display}"
    return "ahead", f"Ahead of latest tagged release {latest_display}"


class ReleaseStatusService:
    def __init__(
        self,
        *,
        current_version: str,
        repo_full_name: str = DEFAULT_RELEASE_CHECK_REPO,
        enabled: bool = True,
        interval_seconds: int = DEFAULT_RELEASE_CHECK_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_RELEASE_CHECK_TIMEOUT_SECONDS,
    ) -> None:
        self.current_version = current_version
        self.repo_full_name = repo_full_name.strip() or DEFAULT_RELEASE_CHECK_REPO
        self.enabled = bool(enabled)
        self.interval_seconds = max(3600, int(interval_seconds or DEFAULT_RELEASE_CHECK_INTERVAL_SECONDS))
        self.timeout_seconds = max(1.0, float(timeout_seconds or DEFAULT_RELEASE_CHECK_TIMEOUT_SECONDS))
        self._lock = asyncio.Lock()
        self._payload: dict[str, Any] = self._build_payload(
            status="disabled" if not self.enabled else "checking",
            summary="Release checks disabled." if not self.enabled else "Checking releases...",
            latest_tag=None,
            latest_name=None,
            latest_url=None,
            published_at=None,
            checked_at=None,
            error=None,
        )

    def snapshot(self) -> dict[str, Any]:
        return dict(self._payload)

    async def run_periodic_refresh(self) -> None:
        if not self.enabled:
            self._payload = self._build_payload(
                status="disabled",
                summary="Release checks disabled.",
                latest_tag=None,
                latest_name=None,
                latest_url=None,
                published_at=None,
                checked_at=None,
                error=None,
            )
            return

        while True:
            await self.refresh(force=True)
            await asyncio.sleep(self.interval_seconds)

    async def refresh(self, *, force: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return self.snapshot()

        async with self._lock:
            if not force:
                checked_at = self._payload.get("checked_at")
                if checked_at:
                    try:
                        checked_dt = datetime.fromisoformat(str(checked_at))
                    except ValueError:
                        checked_dt = None
                    if checked_dt and (_utc_now() - checked_dt).total_seconds() < self.interval_seconds:
                        return self.snapshot()

            checked_at_dt = _utc_now()
            try:
                latest = await asyncio.to_thread(self._fetch_latest_release)
            except Exception as exc:  # noqa: BLE001 - cached service should fail softly.
                logger.info("Release check failed for %s: %s", self.repo_full_name, exc)
                if self._payload.get("latest_tag"):
                    return self.snapshot()
                self._payload = self._build_payload(
                    status="error",
                    summary="Release check unavailable",
                    latest_tag=None,
                    latest_name=None,
                    latest_url=None,
                    published_at=None,
                    checked_at=checked_at_dt,
                    error=str(exc),
                )
                return self.snapshot()

            latest_tag = _normalize_tag(str(latest.get("tag_name") or "")) if latest.get("tag_name") else None
            status, summary = describe_release_status(self.current_version, latest_tag)
            self._payload = self._build_payload(
                status=status,
                summary=summary,
                latest_tag=latest_tag,
                latest_name=str(latest.get("name") or "").strip() or None,
                latest_url=str(latest.get("html_url") or "").strip() or None,
                published_at=self._normalize_timestamp(latest.get("published_at")),
                checked_at=checked_at_dt,
                error=None,
            )
            return self.snapshot()

    def _fetch_latest_release(self) -> dict[str, Any]:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo_full_name}/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"truenas-jbod-ui/{self.current_version}",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise ValueError("GitHub release response was not a JSON object.")
        return payload

    @staticmethod
    def _normalize_timestamp(value: Any) -> str | None:
        if value in {None, ""}:
            return None
        text = str(value).strip()
        return text or None

    def _build_payload(
        self,
        *,
        status: str,
        summary: str,
        latest_tag: str | None,
        latest_name: str | None,
        latest_url: str | None,
        published_at: str | None,
        checked_at: datetime | None,
        error: str | None,
    ) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "repo_full_name": self.repo_full_name,
            "status": status,
            "summary": summary,
            "latest_tag": latest_tag,
            "latest_name": latest_name,
            "latest_url": latest_url,
            "published_at": published_at,
            "checked_at": _isoformat(checked_at),
            "error": error,
        }
