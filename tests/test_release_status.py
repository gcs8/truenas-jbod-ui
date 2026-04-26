from __future__ import annotations

import asyncio
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from app.services.release_status import ReleaseStatusService, describe_release_status


class ReleaseStatusTests(unittest.TestCase):
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
