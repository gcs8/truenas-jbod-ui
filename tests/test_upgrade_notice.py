from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import __version__
from app import main as app_main
from app.config import AppConfig, PathConfig, Settings
from app.models.domain import (
    EnclosureOption,
    EnclosureProfileView,
    InventorySnapshot,
    StorageViewRuntimePayload,
)
from app.services import upgrade_notice
from tests.test_read_ui_auth import build_app, index_request, invoke_asgi


def read_state(data_dir: Path) -> dict:
    return json.loads(upgrade_notice.state_path(data_dir).read_text(encoding="utf-8"))


class UpgradeNoticeServiceTests(unittest.TestCase):
    def test_fresh_install_records_the_version_without_a_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"

            self.assertIsNone(upgrade_notice.current_notice(data_dir, version="0.23.0"))

            self.assertEqual(read_state(data_dir), {"last_seen_version": "0.23.0"})

    def test_version_change_produces_one_notice_that_persists_until_dismissed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            upgrade_notice.current_notice(data_dir, version="0.22.2")

            first = upgrade_notice.current_notice(data_dir, version="0.23.0")
            second = upgrade_notice.current_notice(data_dir, version="0.23.0")

            self.assertEqual(
                first,
                {
                    "version": "0.23.0",
                    "previous": "0.22.2",
                    "text": (
                        "Updated to v0.23.0. Anyone who can reach this port can change bay "
                        "assignments and lights. See Optional authentication on the Advanced "
                        "Configuration wiki page to add a sign-in."
                    ),
                },
            )
            self.assertEqual(second, first)
            self.assertEqual(read_state(data_dir)["notice"], {"version": "0.23.0", "previous": "0.22.2"})

            self.assertTrue(upgrade_notice.dismiss_notice(data_dir, version="0.23.0"))

            self.assertIsNone(upgrade_notice.current_notice(data_dir, version="0.23.0"))
            self.assertEqual(read_state(data_dir), {"last_seen_version": "0.23.0"})

    def test_unlisted_versions_get_the_generic_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            upgrade_notice.current_notice(data_dir, version="0.23.0")

            notice = upgrade_notice.current_notice(data_dir, version="0.24.0")

        self.assertEqual(notice["text"], "Updated to v0.24.0.")
        self.assertEqual(notice["previous"], "0.23.0")

    def test_stale_notice_for_another_version_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            upgrade_notice.state_path(data_dir).write_text(
                json.dumps({"last_seen_version": "0.24.0", "notice": {"version": "0.23.0", "previous": "0.22.2"}}),
                encoding="utf-8",
            )

            self.assertIsNone(upgrade_notice.current_notice(data_dir, version="0.24.0"))
            self.assertEqual(read_state(data_dir), {"last_seen_version": "0.24.0"})

    def test_unreadable_record_is_treated_as_a_fresh_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            upgrade_notice.state_path(data_dir).write_text("{not json", encoding="utf-8")

            self.assertIsNone(upgrade_notice.current_notice(data_dir, version="0.23.0"))
            self.assertEqual(read_state(data_dir), {"last_seen_version": "0.23.0"})

    def test_dismiss_reports_an_unwritable_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            with patch.object(upgrade_notice, "_write_state", return_value=False):
                self.assertFalse(upgrade_notice.dismiss_notice(data_dir, version="0.23.0"))


class UpgradeNoticeRouteTests(unittest.TestCase):
    def _context(self, app, *, upgrade_notice_payload=None, snapshot_mode=False):
        snapshot = InventorySnapshot(
            slots=[],
            refresh_interval_seconds=30,
            selected_system_id="system-a",
            selected_system_label="System A",
            selected_enclosure_id="enc-a",
            selected_enclosure_label="Shelf A",
            selected_profile=EnclosureProfileView(
                id="profile-a",
                label="Profile A",
                panel_title="Profile A",
                rows=1,
                columns=1,
                slot_layout=[[0]],
            ),
            enclosures=[EnclosureOption(id="enc-a", label="Shelf A", raw_label="Shelf A")],
        )
        return app_main.build_index_context(
            request=index_request(app),
            snapshot=snapshot,
            storage_view_runtime=StorageViewRuntimePayload(system_id="system-a", views=[]),
            settings=Settings(),
            history_configured=False,
            upgrade_notice_payload=upgrade_notice_payload,
            snapshot_mode=snapshot_mode,
        )

    def test_index_renders_one_dismissible_banner_only_when_a_notice_is_pending(self) -> None:
        app = build_app(auth_mode="network")
        payload = {"version": "0.23.0", "previous": "0.22.2", "text": "Updated to v0.23.0. Example notice."}

        with_notice = app_main.templates.get_template("index.html").render(
            self._context(app, upgrade_notice_payload=payload)
        )
        without_notice = app_main.templates.get_template("index.html").render(self._context(app))
        snapshot_copy = app_main.templates.get_template("index.html").render(
            self._context(app, upgrade_notice_payload=payload, snapshot_mode=True)
        )

        self.assertEqual(with_notice.count('id="upgrade-notice"'), 1)
        self.assertIn('data-notice-version="0.23.0"', with_notice)
        self.assertIn("Updated to v0.23.0. Example notice.", with_notice)
        self.assertIn('id="upgrade-notice-dismiss"', with_notice)
        self.assertLess(with_notice.index('id="upgrade-notice"'), with_notice.index('id="status-strip"'))
        self.assertNotIn('id="upgrade-notice"', without_notice)
        self.assertNotIn('id="upgrade-notice"', snapshot_copy)

    def test_index_records_the_version_and_dismiss_clears_the_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            settings = Settings(
                app=AppConfig(),
                paths=PathConfig(mapping_file=str(data_dir / "slot_mappings.json")),
            )
            upgrade_notice.current_notice(data_dir, version="0.22.2")
            app = build_app(auth_mode="network")
            with patch.object(app_main, "get_settings", return_value=settings):
                status, _headers, _body = asyncio.run(
                    invoke_asgi(app, "/api/upgrade-notice/dismiss", method="POST")
                )
                cross_site, _headers, _body = asyncio.run(
                    invoke_asgi(
                        app,
                        "/api/upgrade-notice/dismiss",
                        method="POST",
                        origin="https://attacker.example",
                    )
                )

            self.assertEqual(status, 200)
            self.assertEqual(cross_site, 403)
            self.assertEqual(read_state(data_dir), {"last_seen_version": __version__})

    def test_dismiss_requires_sign_in_in_basic_mode(self) -> None:
        app = build_app(auth_mode="basic", public_origin="http://ui.example.test:8080")

        status, _headers, _body = asyncio.run(invoke_asgi(app, "/api/upgrade-notice/dismiss", method="POST"))

        self.assertEqual(status, 401)

    def test_dismiss_fails_closed_when_the_record_cannot_be_written(self) -> None:
        app = build_app(auth_mode="network")
        with patch.object(upgrade_notice, "dismiss_notice", return_value=False):
            status, _headers, body = asyncio.run(invoke_asgi(app, "/api/upgrade-notice/dismiss", method="POST"))

        self.assertEqual(status, 503)
        self.assertIn("not writable", json.loads(body)["detail"])


if __name__ == "__main__":
    unittest.main()
