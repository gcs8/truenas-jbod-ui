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
    def test_absent_record_reports_unknown_previous_even_for_fresh_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"

            notice = upgrade_notice.current_notice(data_dir, version="0.23.0")
            self.assertIsNotNone(notice)
            self.assertEqual(notice["previous"], "")
            self.assertIn("Previous version unknown", notice["text"])
            self.assertNotIn("Updated to", notice["text"])
            self.assertEqual(upgrade_notice.current_notice(data_dir, version="0.23.0"), notice)
            self.assertTrue(
                upgrade_notice.dismiss_notice(
                    data_dir,
                    notice_version="0.23.0",
                    version="0.23.0",
                )
            )
            self.assertIsNone(upgrade_notice.current_notice(data_dir, version="0.23.0"))

    def test_uninstrumented_install_does_not_infer_upgrade_from_existing_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            mapping_path = data_dir / "slot_mappings.json"
            mapping_path.write_text('{"synthetic": "untouched"}', encoding="utf-8")
            before = mapping_path.read_bytes()
            notice = upgrade_notice.current_notice(data_dir, version="0.23.0")
            self.assertIsNotNone(notice)
            self.assertEqual(notice["previous"], "")
            self.assertIn("Running v0.23.0. Previous version unknown.", notice["text"])
            self.assertEqual(mapping_path.read_bytes(), before)

    def test_invalid_record_shapes_report_unknown_previous(self) -> None:
        for record in [[], {}, {"last_seen_version": None}, {"last_seen_version": 23}]:
            with self.subTest(record=record), tempfile.TemporaryDirectory() as temp_dir:
                data_dir = Path(temp_dir)
                upgrade_notice.state_path(data_dir).write_text(json.dumps(record), encoding="utf-8")
                notice = upgrade_notice.current_notice(data_dir, version="0.23.0")
                self.assertIsNotNone(notice)
                self.assertEqual(notice["previous"], "")
                self.assertIn("Previous version unknown", notice["text"])

    def test_pending_text_tracks_auth_mode_changes_without_persisting_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            network = upgrade_notice.current_notice(data_dir, version="0.23.0", auth_mode="network")
            before = upgrade_notice.state_path(data_dir).read_bytes()
            basic = upgrade_notice.current_notice(data_dir, version="0.23.0", auth_mode="basic")
            self.assertIn("In network mode", network["text"])
            self.assertIn("Sign-in is required", basic["text"])
            self.assertEqual(upgrade_notice.state_path(data_dir).read_bytes(), before)

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
                        "Updated to v0.23.0. In network mode, anyone who can reach this port can change bay "
                        "assignments and lights. See Optional authentication on the Advanced "
                        "Configuration wiki page to add a sign-in."
                    ),
                },
            )
            self.assertEqual(second, first)
            self.assertEqual(read_state(data_dir)["notice"], {"version": "0.23.0", "previous": "0.22.2"})

            self.assertTrue(
                upgrade_notice.dismiss_notice(
                    data_dir,
                    notice_version="0.23.0",
                    version="0.23.0",
                )
            )

            self.assertIsNone(upgrade_notice.current_notice(data_dir, version="0.23.0"))
            self.assertEqual(read_state(data_dir), {"last_seen_version": "0.23.0"})

    def test_stale_page_cannot_dismiss_a_newer_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            upgrade_notice.current_notice(data_dir, version="0.23.0")
            newer = upgrade_notice.current_notice(data_dir, version="0.24.0")

            with self.assertRaises(upgrade_notice.UpgradeNoticeVersionConflict):
                upgrade_notice.dismiss_notice(
                    data_dir,
                    notice_version="0.23.0",
                    version="0.24.0",
                )

            self.assertEqual(upgrade_notice.current_notice(data_dir, version="0.24.0"), newer)
            self.assertTrue(
                upgrade_notice.dismiss_notice(
                    data_dir,
                    notice_version="0.24.0",
                    version="0.24.0",
                )
            )
            self.assertIsNone(upgrade_notice.current_notice(data_dir, version="0.24.0"))

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

    def test_unreadable_record_reports_unknown_previous(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            upgrade_notice.state_path(data_dir).write_text("{not json", encoding="utf-8")

            notice = upgrade_notice.current_notice(data_dir, version="0.23.0")
            self.assertIsNotNone(notice)
            self.assertIn("Previous version unknown", notice["text"])
            self.assertEqual(notice["previous"], "")

    def test_invalid_utf8_record_reports_unknown_previous_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            upgrade_notice.state_path(data_dir).write_bytes(b"\xff")

            notice = upgrade_notice.current_notice(data_dir, version="0.23.0")

            self.assertIsNotNone(notice)
            self.assertEqual(notice["previous"], "")
            self.assertIn("Running v0.23.0. Previous version unknown.", notice["text"])
            self.assertNotIn("Updated to", notice["text"])
            self.assertEqual(read_state(data_dir), {
                "last_seen_version": "0.23.0",
                "notice": {"version": "0.23.0", "previous": ""},
            })
            self.assertEqual(upgrade_notice.current_notice(data_dir, version="0.23.0"), notice)
            self.assertTrue(
                upgrade_notice.dismiss_notice(
                    data_dir,
                    notice_version="0.23.0",
                    version="0.23.0",
                )
            )
            self.assertIsNone(upgrade_notice.current_notice(data_dir, version="0.23.0"))

    def test_basic_mode_notice_uses_selected_policy_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            notice = upgrade_notice.current_notice(Path(temp_dir), version="0.23.0", auth_mode="basic")
            self.assertIn("Sign-in is required to change bay assignments and lights", notice["text"])
            self.assertNotIn("anyone who can reach", notice["text"])
            self.assertNotIn("synthetic-passphrase", notice["text"])

    def test_dismiss_reports_an_unwritable_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            with patch.object(upgrade_notice, "_write_state", return_value=False):
                self.assertFalse(
                    upgrade_notice.dismiss_notice(
                        data_dir,
                        notice_version="0.23.0",
                        version="0.23.0",
                    )
                )


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
            upgrade_notice.current_notice(data_dir, version=__version__)
            app = build_app(auth_mode="network")
            with patch.object(app_main, "get_settings", return_value=settings):
                status, _headers, _body = asyncio.run(
                    invoke_asgi(
                        app,
                        "/api/upgrade-notice/dismiss",
                        method="POST",
                        body=json.dumps({"version": __version__}).encode("utf-8"),
                    )
                )
                cross_site, _headers, _body = asyncio.run(
                    invoke_asgi(
                        app,
                        "/api/upgrade-notice/dismiss",
                        method="POST",
                        origin="https://attacker.example",
                        body=json.dumps({"version": __version__}).encode("utf-8"),
                    )
                )

            self.assertEqual(status, 200)
            self.assertEqual(cross_site, 403)
            self.assertEqual(read_state(data_dir), {"last_seen_version": __version__})

    def test_stale_page_dismissal_is_rejected_without_clearing_current_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            settings = Settings(
                app=AppConfig(),
                paths=PathConfig(mapping_file=str(data_dir / "slot_mappings.json")),
            )
            current = upgrade_notice.current_notice(data_dir, version=__version__)
            app = build_app(auth_mode="network")

            with patch.object(app_main, "get_settings", return_value=settings):
                status, _headers, body = asyncio.run(
                    invoke_asgi(
                        app,
                        "/api/upgrade-notice/dismiss",
                        method="POST",
                        body=b'{"version":"0.22.2"}',
                    )
                )

            self.assertEqual(status, 409)
            self.assertIn("newer upgrade notice", json.loads(body)["detail"])
            self.assertEqual(upgrade_notice.current_notice(data_dir, version=__version__), current)

    def test_dismiss_requires_sign_in_in_basic_mode(self) -> None:
        app = build_app(auth_mode="basic", public_origin="http://ui.example.test:8080")

        status, _headers, _body = asyncio.run(invoke_asgi(app, "/api/upgrade-notice/dismiss", method="POST"))

        self.assertEqual(status, 401)

    def test_dismiss_fails_closed_when_the_record_cannot_be_written(self) -> None:
        app = build_app(auth_mode="network")
        with patch.object(upgrade_notice, "dismiss_notice", return_value=False):
            status, _headers, body = asyncio.run(
                invoke_asgi(
                    app,
                    "/api/upgrade-notice/dismiss",
                    method="POST",
                    body=json.dumps({"version": __version__}).encode("utf-8"),
                )
            )

        self.assertEqual(status, 503)
        self.assertIn("not writable", json.loads(body)["detail"])


if __name__ == "__main__":
    unittest.main()
