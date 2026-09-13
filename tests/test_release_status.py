from __future__ import annotations

import asyncio
import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.release_status import ReleaseStatusService, describe_release_status


class ReleaseStatusTests(unittest.TestCase):
    def test_v0230_release_metadata_is_aligned(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        package = json.loads((repository / "package.json").read_text(encoding="utf-8"))
        package_lock = json.loads((repository / "package-lock.json").read_text(encoding="utf-8"))
        changelog = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
        roadmap = (repository / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
        wiki_home = (repository / "wiki" / "Home.md").read_text(encoding="utf-8")
        release_notes = (repository / "docs" / "RELEASE_NOTES_0.23.0.md").read_text(encoding="utf-8")

        from app import __version__

        self.assertEqual(__version__, "0.23.0")
        self.assertEqual(package["version"], "0.23.0")
        self.assertEqual(package_lock["version"], "0.23.0")
        self.assertEqual(package_lock["packages"][""]["version"], "0.23.0")
        self.assertIn("## v0.23.0 - 2026-09-08", changelog)
        self.assertLess(changelog.index("## v0.23.0"), changelog.index("## v0.22.2"))
        self.assertIn("## v0.22.2 - 2026-09-01", changelog)
        self.assertIn("# Release Notes - v0.23.0", release_notes)
        self.assertIn("two synthetic spares", release_notes)

        self.assertIn("`v0.23.0` is the latest published release", roadmap)
        self.assertIn("2026-09-09", roadmap)
        self.assertIn("https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.23.0", roadmap)
        self.assertIn("docs/archive/ROADMAP_HISTORY.md", roadmap)
        self.assertNotIn("v0.22.2` is the latest published release", roadmap)

        release_url = "https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.23.0"
        normalized_wiki_home = " ".join(wiki_home.split())
        self.assertIn("`v0.23.0` is the latest published release", wiki_home)
        self.assertIn("2026-09-09", wiki_home)
        self.assertIn(release_url, wiki_home)
        self.assertIn("beginner installation remains pinned to `v0.22.2`", normalized_wiki_home)
        self.assertNotIn("`v0.22.2` is the latest published release", wiki_home)

    def test_roadmap_is_short_and_points_at_the_archived_history(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        roadmap = (repository / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
        history = repository / "docs" / "archive" / "ROADMAP_HISTORY.md"

        self.assertLessEqual(len(roadmap.splitlines()), 60)
        self.assertTrue(history.is_file())
        for stale in ("Current status: shipped", "HANDOFF.md", "TODO.md", "V0_3_X_PLAN"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, roadmap)

    def test_post_v0222_roadmap_history_reconciles_completed_follow_up_work(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        history = (repository / "docs" / "archive" / "ROADMAP_HISTORY.md").read_text(encoding="utf-8")

        for marker in (
            "Issue #119 closed",
            "PR #121 merged",
            "579e3bf641872d842af3639ed7bdb084c9b75aff",
            "Issue #124 closed",
            "#162",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, history)

        self.assertNotIn("issue #119 and draft PR #121 remains", history)
        self.assertNotIn("publication also remains v0.22.3 work", history)

    def test_roadmap_does_not_claim_absent_v011_plan_is_preserved_locally(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        for relative in ("docs/ROADMAP.md", "docs/archive/ROADMAP_HISTORY.md"):
            text = (repository / relative).read_text(encoding="utf-8")
            with self.subTest(document=relative):
                self.assertNotRegex(
                    text,
                    r"artifacts/deferred-docs/V0_11_0_PLAN\.md|preserved locally",
                )

    def test_unreleased_changelog_records_selected_post_v0222_changes(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        changelog = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = changelog.split("## v0.22.2", maxsplit=1)[0]

        for heading in ("### Added", "### Changed", "### Fixed"):
            with self.subTest(heading=heading):
                self.assertIn(heading, unreleased)

        for marker in (
            "#121",
            "SATA",
            "#157",
            "MD1280",
            "#161",
            "legacy",
            "#162",
            "crash-safe",
            "#171",
            "joined SES",
            "82e49a05f5e820d3360998d2590dfe33a1e5bad7",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, unreleased)

    def test_segment_permission_upgrade_note_uses_the_bounded_repair_procedure(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        changelog = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = changelog.split("## v0.22.2", maxsplit=1)[0]
        normalized = " ".join(unreleased.split())

        self.assertNotIn("same preflight helper", unreleased)
        self.assertIn("Do not run the generic ownership helper over an existing segmented-history tree", normalized)
        self.assertIn("wiki/Backup-Restore-and-Debug-Bundles.md#optional-scheduled-state-backups", unreleased)
        self.assertIn("`0750`", unreleased)
        self.assertIn("`0640`", unreleased)

    def test_nonroot_upgrade_note_quiesces_and_uses_the_configured_identity(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        changelog = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = changelog.split("## v0.22.2", maxsplit=1)[0]
        normalized = " ".join(unreleased.split())

        self.assertIn("docker compose down", normalized)
        self.assertIn('app_uid="${APP_UID:-10001}"', normalized)
        self.assertIn('app_gid="${APP_GID:-10001}"', normalized)
        self.assertIn('--uid "$app_uid" --gid "$app_gid"', normalized)
        self.assertNotIn("--uid 10001 --gid 10001", normalized)

    def test_network_mode_upgrade_note_names_every_write_control(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        changelog = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = changelog.split("## v0.22.2", maxsplit=1)[0]

        self.assertIn("system locator", unreleased)
        self.assertIn("Storage Fabric alias", unreleased)

    def test_segment_repair_covers_writable_and_immutable_history_paths(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "wiki" / "Backup-Restore-and-Debug-Bundles.md").read_text(encoding="utf-8")
        normalized = " ".join(text.split())

        for marker in ("docker compose down", "history root", "history.db", "`0770`", "`0660`", "`0750`", "`0640`"):
            with self.subTest(marker=marker):
                self.assertIn(marker, normalized)
        self.assertIn("Do not recursively relax rollback snapshots", normalized)

    def test_describe_release_status_reports_update_available_for_older_build(self) -> None:
        status, summary = describe_release_status("0.14.0", "v0.14.1")

        self.assertEqual(status, "update-available")
        self.assertEqual(summary, "Update available: v0.14.1")

    def test_describe_release_status_reports_dev_build_for_newer_dev_version(self) -> None:
        status, summary = describe_release_status("0.15.0-dev", "v0.14.1")

        self.assertEqual(status, "dev-build")
        self.assertEqual(summary, "Dev build · latest stable v0.14.1")

    def test_release_status_service_refresh_populates_latest_release_payload(self) -> None:
        payload = {
            "tag_name": "v0.14.1",
            "name": "v0.14.1",
            "html_url": "https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.14.1",
            "published_at": "2026-04-26T00:00:00Z",
        }
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))

        service = ReleaseStatusService(current_version="0.14.1")
        with patch("app.services.release_status.urllib.request.urlopen", return_value=response):
            snapshot = asyncio.run(service.refresh(force=True))

        self.assertEqual(snapshot["status"], "current")
        self.assertEqual(snapshot["summary"], "Latest tagged release")
        self.assertEqual(snapshot["latest_tag"], "v0.14.1")
        self.assertEqual(snapshot["latest_url"], payload["html_url"])

    def test_periodic_refresh_restarts_on_a_new_event_loop(self) -> None:
        service = ReleaseStatusService(current_version="0.14.1")
        now = 1000.0

        async def lifespan() -> None:
            nonlocal now
            loop = asyncio.get_running_loop()
            loop.slow_callback_duration = float("inf")
            baseline = asyncio.all_tasks()

            async def settle() -> None:
                for _ in range(12):
                    await asyncio.sleep(0)

            async def fetch(function):
                return function()

            with (
                patch.object(loop, "time", side_effect=lambda: now),
                patch("app.services.release_status.monotonic", side_effect=lambda: now),
                patch("app.services.release_status.asyncio.to_thread", side_effect=fetch),
                patch.object(service, "_fetch_latest_release", return_value={"tag_name": "v0.14.1"}) as network,
            ):
                worker = asyncio.create_task(service.run_periodic_refresh())
                try:
                    await settle()
                    # Surface a worker crash instead of masking it with cancellation.
                    if worker.done():
                        await worker
                    before = network.call_count
                    now = service._next_refresh_at
                    await settle()
                    self.assertEqual(network.call_count, before + 1)
                    self.assertEqual(service.snapshot()["status"], "current")
                    self.assertFalse(worker.done())
                finally:
                    worker.cancel()
                    if not worker.done():
                        with self.assertRaises(asyncio.CancelledError):
                            await worker
                    await settle()
                    self.assertEqual(asyncio.all_tasks(), baseline)
                    self.assertFalse([timer for timer in loop._scheduled if not timer.cancelled()])

        # Reuse the instance, as the process-wide cached getters do on restart.
        asyncio.run(lifespan())
        asyncio.run(lifespan())

    def test_release_status_service_reports_error_when_initial_refresh_fails(self) -> None:
        service = ReleaseStatusService(current_version="0.15.0-dev")

        with patch("app.services.release_status.urllib.request.urlopen", side_effect=OSError("offline")):
            snapshot = asyncio.run(service.refresh(force=True))

        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["summary"], "Release check unavailable")
        self.assertIn("offline", snapshot["error"])


class ReleaseRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        utc_clock = patch(
            "app.services.release_status._utc_now",
            side_effect=lambda: datetime.fromtimestamp(self.now, timezone.utc),
        )
        utc_clock.start()
        self.addCleanup(utc_clock.stop)
        self.clock = patch("app.services.release_status.monotonic", side_effect=lambda: self.now, create=True)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.network = patch("app.services.release_status.urllib.request.urlopen", side_effect=OSError("offline"))
        self.urlopen = self.network.start()
        self.addCleanup(self.network.stop)
        self.service = ReleaseStatusService(current_version="0.14.1")

    def succeed(self) -> None:
        response = MagicMock()
        response.__enter__.side_effect = lambda: io.BytesIO(json.dumps({
            "tag_name": "v0.14.1",
            "html_url": "https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.14.1",
        }).encode())
        self.urlopen.side_effect = None
        self.urlopen.return_value = response

    async def test_initial_failure_retries_at_one_minute_not_one_day(self) -> None:
        self.assertEqual((await self.service.refresh())["status"], "error")
        self.now += 59
        await self.service.refresh()
        self.assertEqual(self.urlopen.call_count, 1)
        self.succeed()
        self.now += 1
        self.assertEqual((await self.service.refresh())["status"], "current")
        self.assertEqual(self.urlopen.call_count, 2)

    async def test_repeated_failures_back_off_and_cap_at_one_hour(self) -> None:
        await self.service.refresh()
        for calls, delay in enumerate((60, 300, 3600, 3600), start=1):
            with self.subTest(delay=delay, calls=calls):
                self.now += delay - 1
                await self.service.refresh()
                self.assertEqual(self.urlopen.call_count, calls)
                self.now += 1
                await self.service.refresh()
                self.assertEqual(self.urlopen.call_count, calls + 1)

    async def test_success_resets_backoff_and_restores_normal_interval(self) -> None:
        await self.service.refresh()
        self.now += 60
        await self.service.refresh()
        self.now += 300
        self.succeed()
        good = await self.service.refresh()
        self.assertEqual(good["status"], "current")
        self.now += 86399
        await self.service.refresh()
        self.assertEqual(self.urlopen.call_count, 3)
        self.now += 1
        self.urlopen.side_effect = OSError("offline again")
        self.assertEqual(await self.service.refresh(), good)
        self.assertEqual(self.urlopen.call_count, 4)
        self.now += 59
        self.assertEqual(await self.service.refresh(), good)
        self.assertEqual(self.urlopen.call_count, 4)
        self.now += 1
        self.succeed()
        self.assertEqual((await self.service.refresh())["status"], "current")
        self.assertEqual(self.urlopen.call_count, 5)

    async def test_periodic_loop_uses_failure_delays_then_success_interval(self) -> None:
        delays = []

        async def wait():
            delay = self.service._next_refresh_at - self.now
            delays.append(delay)
            self.now += delay
            if len(delays) == 2:
                self.succeed()
            if len(delays) == 3:
                raise asyncio.CancelledError
            raise asyncio.TimeoutError

        with patch.object(self.service._deadline_changed, "wait", side_effect=wait):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.run_periodic_refresh()
        self.assertEqual(delays, [60, 300, 86400])
        self.assertEqual(self.urlopen.call_count, 3)

    async def _settle_periodic(self) -> None:
        # Bounded event-loop turns drain ready callbacks, without wall-clock waits.
        for _ in range(12):
            await asyncio.sleep(0)

    async def _check_forced_refresh_wakeup(self, *, failure: bool) -> None:
        self.succeed()
        loop = asyncio.get_running_loop()
        # Virtual time jumps are not real slow callbacks.
        loop.slow_callback_duration = float("inf")
        baseline = asyncio.all_tasks()

        async def fetch(function):
            return function()

        with (
            patch.object(loop, "time", side_effect=lambda: self.now),
            patch("app.services.release_status.asyncio.to_thread", side_effect=fetch),
        ):
            periodic = asyncio.create_task(self.service.run_periodic_refresh())
            try:
                await self._settle_periodic()
                self.assertEqual(self.urlopen.call_count, 1)
                good = self.service.snapshot()
                old_deadline = self.service._next_refresh_at
                self.now += 10
                if failure:
                    self.urlopen.side_effect = OSError("forced failure")
                await self.service.refresh(force=True)
                if failure:
                    self.assertEqual(self.service.snapshot(), good)
                await self._settle_periodic()
                deadline = self.service._next_refresh_at
                self.assertEqual(deadline, self.now + (60 if failure else 86400))
                # The active timer must move in either direction, not just the
                # stored deadline. Inspect only live timers on this isolated loop.
                timers = [timer.when() for timer in loop._scheduled if not timer.cancelled()]
                self.assertIn(deadline, timers)
                self.assertNotIn(old_deadline, timers)
                self.succeed()
                self.now = deadline - 1
                await self._settle_periodic()
                self.assertEqual(self.urlopen.call_count, 2)
                self.now = deadline
                await self._settle_periodic()
                self.assertEqual(self.urlopen.call_count, 3)
                self.assertEqual(self.service.snapshot()["status"], "current")
            finally:
                periodic.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await periodic
                await self._settle_periodic()
                self.assertEqual(asyncio.all_tasks(), baseline)
                self.assertFalse([timer for timer in loop._scheduled if not timer.cancelled()])

    async def test_forced_failure_interrupts_existing_day_wait(self) -> None:
        await self._check_forced_refresh_wakeup(failure=True)

    async def test_forced_success_replaces_existing_day_wait(self) -> None:
        await self._check_forced_refresh_wakeup(failure=False)

    async def test_deadline_update_before_wait_registration_is_not_lost(self) -> None:
        self.succeed()
        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = float("inf")
        baseline = asyncio.all_tasks()
        event = self.service._deadline_changed
        original_wait = event.wait
        registrations = 0

        async def fetch(function):
            return function()

        async def wait():
            nonlocal registrations
            registrations += 1
            if registrations == 1:
                # Force an update after the timer is chosen but before the
                # event has registered its waiter. The notification must latch.
                self.now += 10
                self.urlopen.side_effect = OSError("registration race")
                await self.service.refresh(force=True)
            await original_wait()

        with (
            patch.object(loop, "time", side_effect=lambda: self.now),
            patch.object(event, "wait", side_effect=wait),
            patch("app.services.release_status.asyncio.to_thread", side_effect=fetch),
        ):
            periodic = asyncio.create_task(self.service.run_periodic_refresh())
            try:
                await self._settle_periodic()
                self.assertEqual(self.urlopen.call_count, 2)
                self.assertEqual(registrations, 2)
                self.assertEqual(
                    [timer.when() for timer in loop._scheduled if not timer.cancelled()],
                    [1070.0],
                )
                # Multiple updates before the periodic task resumes coalesce
                # to the latest deadline. Cancel while a wakeup is pending.
                self.succeed()
                await self.service.refresh(force=True)
                self.urlopen.side_effect = OSError("pending wake cancellation")
                await self.service.refresh(force=True)
            finally:
                periodic.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await periodic
                await self._settle_periodic()
                self.assertEqual(asyncio.all_tasks(), baseline)
                self.assertFalse(event._waiters)
                self.assertFalse([timer for timer in loop._scheduled if not timer.cancelled()])

    async def test_disabled_checks_never_fetch_or_sleep_even_when_forced(self) -> None:
        service = ReleaseStatusService(current_version="0.14.1", enabled=False)
        with patch("app.services.release_status.asyncio.sleep") as sleep:
            await service.run_periodic_refresh()
            self.assertEqual((await service.refresh(force=True))["status"], "disabled")
        sleep.assert_not_called()
        self.urlopen.assert_not_called()

    async def test_concurrent_due_refreshes_share_one_attempt(self) -> None:
        await self.service.refresh()
        self.now += 60
        started = asyncio.Event()
        finish = asyncio.Event()

        async def fetch(function):
            started.set()
            await finish.wait()
            return function()

        self.succeed()
        with patch("app.services.release_status.asyncio.to_thread", side_effect=fetch) as worker:
            first = asyncio.create_task(self.service.refresh(force=True))
            await started.wait()
            second = asyncio.create_task(self.service.refresh())
            await asyncio.sleep(0)
            finish.set()
            results = await asyncio.gather(first, second)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["status"], "current")

    async def test_retry_delay_starts_after_failed_attempt_finishes(self) -> None:
        def slow_failure(*args, **kwargs):
            self.now += 5
            raise OSError("offline")

        self.urlopen.side_effect = slow_failure
        await self.service.refresh()
        self.now += 59
        await self.service.refresh()
        self.assertEqual(self.urlopen.call_count, 1)
        self.now += 1
        await self.service.refresh()
        self.assertEqual(self.urlopen.call_count, 2)

    async def test_force_bypasses_deadline_but_success_keeps_interval_floor(self) -> None:
        self.service = ReleaseStatusService(current_version="0.14.1", interval_seconds=1)
        await self.service.refresh()
        self.succeed()
        self.assertEqual((await self.service.refresh(force=True))["status"], "current")
        self.now += 3599
        await self.service.refresh()
        self.assertEqual(self.urlopen.call_count, 2)
        self.now += 1
        await self.service.refresh()
        self.assertEqual(self.urlopen.call_count, 3)
