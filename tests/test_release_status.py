from __future__ import annotations

import asyncio
import io
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.release_status import ReleaseStatusService, describe_release_status


class ReleaseStatusTests(unittest.TestCase):
    def test_v0222_release_metadata_is_aligned(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        package = json.loads((repository / "package.json").read_text(encoding="utf-8"))
        package_lock = json.loads((repository / "package-lock.json").read_text(encoding="utf-8"))
        changelog = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
        roadmap = (repository / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
        wiki_home = (repository / "wiki" / "Home.md").read_text(encoding="utf-8")
        release_notes = (repository / "docs" / "RELEASE_NOTES_0.22.2.md").read_text(encoding="utf-8")

        from app import __version__

        self.assertEqual(__version__, "0.22.2")
        self.assertEqual(package["version"], "0.22.2")
        self.assertEqual(package_lock["version"], "0.22.2")
        self.assertEqual(package_lock["packages"][""]["version"], "0.22.2")
        self.assertIn("## Unreleased", changelog)
        self.assertLess(changelog.index("## Unreleased"), changelog.index("## v0.22.2"))
        self.assertIn("## v0.22.2 - 2026-09-01", changelog)
        self.assertIn("# Release Notes - v0.22.2", release_notes)
        self.assertIn("issue #124", release_notes)

        release_url = "https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.22.2"
        for current_doc in (roadmap, wiki_home):
            with self.subTest(document=current_doc[:40]):
                self.assertIn("v0.22.2", current_doc)
                self.assertIn("latest published release", current_doc)
                self.assertIn("2026-09-01", current_doc)
                self.assertIn(release_url, current_doc)
                self.assertNotIn("v0.22.1` is the latest published release", current_doc)

    def test_post_v0222_roadmap_reconciles_completed_follow_up_work(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        roadmap = (repository / "docs" / "ROADMAP.md").read_text(encoding="utf-8")

        for marker in (
            "Issue #119 closed",
            "PR #121 merged",
            "579e3bf641872d842af3639ed7bdb084c9b75aff",
            "Issue #124 closed",
            "#162",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, roadmap)

        self.assertNotIn("issue #119 and draft PR #121 remains", roadmap)
        self.assertNotIn("publication also remains v0.22.3 work", roadmap)

    def test_roadmap_does_not_claim_absent_v011_plan_is_preserved_locally(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        roadmap = (repository / "docs" / "ROADMAP.md").read_text(encoding="utf-8")

        self.assertNotRegex(
            roadmap,
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

    def test_release_status_service_reports_error_when_initial_refresh_fails(self) -> None:
        service = ReleaseStatusService(current_version="0.15.0-dev")

        with patch("app.services.release_status.urllib.request.urlopen", side_effect=OSError("offline")):
            snapshot = asyncio.run(service.refresh(force=True))

        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["summary"], "Release check unavailable")
        self.assertIn("offline", snapshot["error"])
