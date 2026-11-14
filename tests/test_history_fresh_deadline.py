"""Fresh SMART and forced enumeration reads get their own deadline (#1168).

The fake ``urlopen`` below stands in for a main UI that answers after a set
number of (simulated) seconds: it raises ``TimeoutError`` when the caller's
socket timeout is shorter than that latency, as ``urllib`` does, and returns
the payload otherwise. No real sleeping and no network.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
import urllib.parse
from typing import Any
from unittest.mock import Mock, patch

from pydantic import ValidationError

from history_service.collector import HistoryCollector, ScopeSnapshot
from history_service.config import ENV_OVERRIDES, HistorySettings
from history_service.diagnostics import HistorySourceError

LARGE_CORE_FRESH_SMART_SECONDS = 78.5  # observed worst case on a 51-bay CORE system


class _SlowMainUI:
    """Answers each request after ``latency(path, query)`` simulated seconds."""

    def __init__(self, latency: float | dict[str, float]) -> None:
        self._latency = latency
        self.requests: list[tuple[str, dict[str, list[str]], float | None]] = []

    def __call__(self, request, timeout=None):  # noqa: ANN001 - urllib signature
        parsed = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        self.requests.append((parsed.path, query, timeout))
        if isinstance(self._latency, dict):
            latency = self._latency.get(parsed.path, 0.1)
        else:
            latency = self._latency
        if timeout is not None and latency > timeout:
            raise TimeoutError("timed out")
        if parsed.path.endswith("smart-batch"):
            slots = json.loads(request.data)["slots"]
            body = {"summaries": [{"slot": slot, "summary": {"available": True}} for slot in slots]}
        elif parsed.path == "/api/storage-views":
            body = {"views": []}
        else:
            body = {"systems": [], "slots": []}
        return io.BytesIO(json.dumps(body).encode("utf-8"))


def _collector(**settings: Any) -> HistoryCollector:
    return HistoryCollector(HistorySettings(source_base_url="http://enclosure-ui:8000", **settings), Mock())


def _scope(enclosure_id: str = "enc-a") -> ScopeSnapshot:
    return ScopeSnapshot(
        system_id="core-a",
        system_label="Example CORE",
        enclosure_id=enclosure_id,
        enclosure_label="Front",
        snapshot={"slots": []},
    )


class FreshDeadlineSettingsTests(unittest.TestCase):
    def test_default_fresh_deadline_covers_the_observed_large_core_worst_case(self) -> None:
        settings = HistorySettings()
        self.assertEqual(settings.fresh_request_timeout_seconds, 120)
        self.assertGreater(settings.fresh_request_timeout_seconds, LARGE_CORE_FRESH_SMART_SECONDS)
        # The generic deadline is unchanged.
        self.assertEqual(settings.request_timeout_seconds, 45)

    def test_fresh_deadline_has_an_env_override(self) -> None:
        self.assertEqual(ENV_OVERRIDES.get("HISTORY_FRESH_REQUEST_TIMEOUT_SECONDS"), "fresh_request_timeout_seconds")

    def test_fresh_deadline_is_bounded_with_a_floor(self) -> None:
        self.assertEqual(HistorySettings(fresh_request_timeout_seconds=60).fresh_request_timeout_seconds, 60)
        self.assertEqual(HistorySettings(fresh_request_timeout_seconds=900).fresh_request_timeout_seconds, 900)
        for value in (20, 59, 901):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                HistorySettings(fresh_request_timeout_seconds=value)

    def test_env_example_and_compose_ship_the_fresh_deadline(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".env.example"), encoding="utf-8") as handle:
            self.assertIn("HISTORY_FRESH_REQUEST_TIMEOUT_SECONDS=120", handle.read())
        for name in ("docker-compose.yml", "docker-compose.dev.yml"):
            with self.subTest(file=name), open(os.path.join(root, name), encoding="utf-8") as handle:
                self.assertIn(
                    "HISTORY_FRESH_REQUEST_TIMEOUT_SECONDS: ${HISTORY_FRESH_REQUEST_TIMEOUT_SECONDS:-120}",
                    handle.read(),
                )


class FreshSmartDeadlineTests(unittest.TestCase):
    def _fetch(self, collector: HistoryCollector, ui: _SlowMainUI, *, force_fresh: bool, scope=None):
        with patch("history_service.collector.urllib.request.urlopen", side_effect=ui):
            return asyncio.run(collector._fetch_smart_summaries(scope or _scope(), [0, 1], force_fresh=force_fresh))

    def test_a_legacy_generic_timeout_does_not_cap_a_fresh_smart_batch(self) -> None:
        # Installs from before v0.17 still carry HISTORY_REQUEST_TIMEOUT_SECONDS=20.
        collector = _collector(request_timeout_seconds=20)
        ui = _SlowMainUI(LARGE_CORE_FRESH_SMART_SECONDS)

        summaries = self._fetch(collector, ui, force_fresh=True)

        self.assertEqual(sorted(summaries), [0, 1])
        self.assertEqual([timeout for _path, _query, timeout in ui.requests], [120])
        self.assertEqual(ui.requests[0][1].get("fresh"), ["true"])

    def test_a_fresh_smart_batch_past_the_deadline_gives_up_with_a_clear_error(self) -> None:
        collector = _collector(request_timeout_seconds=20)
        ui = _SlowMainUI(130)

        with self.assertRaises(HistorySourceError) as raised:
            self._fetch(collector, ui, force_fresh=True)

        self.assertEqual(raised.exception.kind, "source_timeout")
        self.assertIn("timed out after 120s", str(raised.exception))

    def test_a_larger_generic_timeout_still_raises_the_fresh_deadline(self) -> None:
        collector = _collector(request_timeout_seconds=300)
        ui = _SlowMainUI(200)

        self.assertEqual(sorted(self._fetch(collector, ui, force_fresh=True)), [0, 1])
        self.assertEqual(ui.requests[0][2], 300)

    def test_cached_smart_reads_keep_their_short_deadline(self) -> None:
        collector = _collector(request_timeout_seconds=20)
        ui = _SlowMainUI(1)

        self._fetch(collector, ui, force_fresh=False)

        self.assertEqual(ui.requests[0][2], 5)
        self.assertNotIn("fresh", ui.requests[0][1])


class FreshEnumerationDeadlineTests(unittest.TestCase):
    def test_forced_inventory_and_storage_view_reads_use_the_fresh_deadline(self) -> None:
        collector = _collector(request_timeout_seconds=20)
        ui = _SlowMainUI(60)

        with patch("history_service.collector.urllib.request.urlopen", side_effect=ui):
            asyncio.run(collector._fetch_inventory(force=True))
            asyncio.run(collector._fetch_inventory(system_id="core-a", force=True))
            asyncio.run(collector._fetch_storage_views(system_id="core-a", force=True))

        self.assertEqual([timeout for _path, _query, timeout in ui.requests], [120, 120, 120])
        self.assertTrue(all(query.get("force") == ["true"] for _path, query, _timeout in ui.requests))

    def test_unforced_enumeration_keeps_the_generic_deadline(self) -> None:
        collector = _collector(request_timeout_seconds=20)
        ui = _SlowMainUI(1)

        with patch("history_service.collector.urllib.request.urlopen", side_effect=ui):
            asyncio.run(collector._fetch_inventory(force=False))
            asyncio.run(collector._fetch_storage_views(system_id="core-a", force=False))

        self.assertEqual([timeout for _path, _query, timeout in ui.requests], [20, 20])


class AbandonedFreshReadTests(unittest.TestCase):
    """After a fresh read times out, the main UI is still working on it.

    The collector must not start a second fresh read for the same scope until
    that work can have finished (one more fresh deadline).
    """

    def test_no_second_fresh_smart_batch_for_a_scope_while_the_first_may_still_run(self) -> None:
        collector = _collector()
        ui = _SlowMainUI(500)
        clock = [1000.0]

        with (
            patch("history_service.collector.urllib.request.urlopen", side_effect=ui),
            patch("history_service.collector.time.monotonic", side_effect=lambda: clock[0]),
        ):
            with self.assertRaises(HistorySourceError):
                asyncio.run(collector._fetch_smart_summaries(_scope(), [0], force_fresh=True))
            self.assertEqual(len(ui.requests), 1)

            clock[0] += 60
            with self.assertRaises(HistorySourceError) as raised:
                asyncio.run(collector._fetch_smart_summaries(_scope(), [0], force_fresh=True))
            self.assertEqual(len(ui.requests), 1, "a duplicate fresh request was sent")
            self.assertIn("previous fresh read", str(raised.exception))
            self.assertIn("may still be running", str(raised.exception))

            # Another scope is not held back.
            with self.assertRaises(HistorySourceError):
                asyncio.run(collector._fetch_smart_summaries(_scope("enc-b"), [0], force_fresh=True))
            self.assertEqual(len(ui.requests), 2)

            # Cached reads for the held scope still go out.
            with self.assertRaises(HistorySourceError):
                asyncio.run(collector._fetch_smart_summaries(_scope(), [0], force_fresh=False))
            self.assertEqual(len(ui.requests), 3)

            # Once a whole fresh deadline has passed, the scope is read again.
            clock[0] += 61
            with self.assertRaises(HistorySourceError):
                asyncio.run(collector._fetch_smart_summaries(_scope(), [0], force_fresh=True))
            self.assertEqual(len(ui.requests), 4)

    def test_a_timed_out_forced_inventory_read_is_followed_by_a_cached_one(self) -> None:
        collector = _collector()
        clock = [1000.0]
        slow = _SlowMainUI(500)

        with (
            patch("history_service.collector.urllib.request.urlopen", side_effect=slow),
            patch("history_service.collector.time.monotonic", side_effect=lambda: clock[0]),
        ):
            with self.assertRaises(HistorySourceError):
                asyncio.run(collector._fetch_inventory(system_id="core-a", force=True))

        fast = _SlowMainUI(1)
        clock[0] += 30
        with (
            patch("history_service.collector.urllib.request.urlopen", side_effect=fast),
            patch("history_service.collector.time.monotonic", side_effect=lambda: clock[0]),
            self.assertLogs("history_service.collector", level="INFO") as logs,
        ):
            asyncio.run(collector._fetch_inventory(system_id="core-a", force=True))
            # A different system still gets its forced read.
            asyncio.run(collector._fetch_inventory(system_id="core-b", force=True))

        (path, query, timeout), (_path_b, query_b, timeout_b) = fast.requests
        self.assertEqual(path, "/api/inventory")
        self.assertNotIn("force", query)
        self.assertEqual(timeout, 45)
        self.assertEqual(query_b.get("force"), ["true"])
        self.assertEqual(timeout_b, 120)
        self.assertTrue(any("previous forced read" in line for line in logs.output))

    def test_a_successful_fresh_read_clears_the_hold(self) -> None:
        collector = _collector()
        clock = [1000.0]
        ui = _SlowMainUI({"/api/slots/smart-batch": 500})

        with (
            patch("history_service.collector.urllib.request.urlopen", side_effect=ui),
            patch("history_service.collector.time.monotonic", side_effect=lambda: clock[0]),
        ):
            with self.assertRaises(HistorySourceError):
                asyncio.run(collector._fetch_smart_summaries(_scope(), [0], force_fresh=True))
        clock[0] += 121
        fast = _SlowMainUI(1)
        with (
            patch("history_service.collector.urllib.request.urlopen", side_effect=fast),
            patch("history_service.collector.time.monotonic", side_effect=lambda: clock[0]),
        ):
            asyncio.run(collector._fetch_smart_summaries(_scope(), [0], force_fresh=True))
            asyncio.run(collector._fetch_smart_summaries(_scope(), [0], force_fresh=True))
        self.assertEqual(len(fast.requests), 2)


if __name__ == "__main__":
    unittest.main()
