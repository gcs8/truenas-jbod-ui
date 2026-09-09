"""Synthetic regression coverage for destructive admin operations."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from fastapi import HTTPException
from app.config import Settings
from app.models.domain import DemoSystemRequest
from app.services.demo_system_factory import DemoSystemFactory
from admin_service.main import app
from history_service.store import HistoryStore


class DemoCollisionTests(unittest.TestCase):
    def test_collisions_preserve_both_files_even_with_stale_settings(self):
        for collision in ("system", "profile"):
            for replace in (False, True):
                with self.subTest(collision=collision, replace=replace), tempfile.TemporaryDirectory() as tmp:
                    config = Path(tmp) / "config.yaml"
                    profiles = Path(tmp) / "profiles.yaml"
                    config.write_text(yaml.safe_dump({"systems": [{"id": "demo-builder-lab", "label": "Keep", "truenas": {"host": "https://example.invalid"}}] if collision == "system" else []}))
                    profiles.write_text(yaml.safe_dump({"profiles": [{"id": "demo-builder-lab-chassis", "label": "Keep", "rows": 1, "columns": 1}] if collision == "profile" else []}))
                    before = (config.read_bytes(), profiles.read_bytes())
                    factory = DemoSystemFactory(str(config), str(profiles))
                    with self.assertRaisesRegex(ValueError, "already exists"):
                        factory.create_demo_system(DemoSystemRequest(replace_existing=replace), Settings(systems=[], profiles=[]))
                    self.assertEqual((config.read_bytes(), profiles.read_bytes()), before)

    def test_long_demo_id_cannot_overwrite_a_truncated_profile_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, profiles = Path(tmp) / "config.yaml", Path(tmp) / "profiles.yaml"
            config.write_text("systems: []\n")
            system_id = "a" * 256
            profiles.write_text(yaml.safe_dump({"profiles": [{"id": system_id, "label": "Keep", "rows": 1, "columns": 1}]}))
            before = config.read_bytes(), profiles.read_bytes()
            factory = DemoSystemFactory(str(config), str(profiles))
            with self.assertRaises(ValueError):
                factory.create_demo_system(DemoSystemRequest(system_id=system_id), Settings(systems=[], profiles=[]))
            self.assertEqual((config.read_bytes(), profiles.read_bytes()), before)

    def test_legacy_single_system_is_not_silently_displaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, profiles = Path(tmp) / "config.yaml", Path(tmp) / "profiles.yaml"
            config.write_text("truenas:\n  host: https://legacy.example.invalid\nsystems: []\n")
            profiles.write_text("profiles: []\n")
            before = config.read_bytes(), profiles.read_bytes()
            factory = DemoSystemFactory(str(config), str(profiles))
            with self.assertRaisesRegex(ValueError, "legacy"):
                factory.create_demo_system(DemoSystemRequest(), Settings(systems=[], profiles=[]))
            self.assertEqual((config.read_bytes(), profiles.read_bytes()), before)

    def test_failed_system_save_rolls_back_new_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, profiles = Path(tmp) / "config.yaml", Path(tmp) / "profiles.yaml"
            config.write_text("systems: []\n")
            profiles.write_text("# preserve bytes\nprofiles: []\n")
            factory = DemoSystemFactory(str(config), str(profiles))
            before = config.read_bytes(), profiles.read_bytes()
            with patch.object(factory.system_service, "save_system", side_effect=ValueError("synthetic failure")):
                with self.assertRaises(ValueError):
                    factory.create_demo_system(DemoSystemRequest(), Settings(systems=[], profiles=[]))
            self.assertEqual((config.read_bytes(), profiles.read_bytes()), before)


class PurgeProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = HistoryStore(str(Path(self.tmp.name) / "history.db"))
        # Alert rules participate in the existing history ownership lifecycle.
        with self.store._connect() as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(slot_events)")]
        self.assertIn("system_id", columns)
        self.settings = Settings(systems=[])
        self.preview = next(r.endpoint for r in app.routes if r.path == "/api/admin/history/orphaned")
        self.purge = next(r.endpoint for r in app.routes if r.path == "/api/admin/history/purge-orphaned")

    def seed(self, system_id):
        with self.store._connect() as connection:
            connection.execute("INSERT INTO slot_events (observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json) VALUES ('2026-01-01T00:00:00Z', ?, 'synthetic', 1, 'Bay 1', 'inserted', '{}')", (system_id,))

    def run_with_store(self, coroutine):
        with patch("admin_service.main.reload_app_settings", return_value=self.settings), patch("admin_service.main.get_history_store", return_value=self.store):
            return asyncio.run(coroutine)

    def test_purge_requires_preview_proof(self):
        self.seed("removed-one")
        with self.assertRaises(HTTPException) as caught:
            self.run_with_store(self.purge({}))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self.store.list_history_system_summaries())

    def test_changed_row_count_requires_new_preview(self):
        self.seed("removed-one")
        preview = json.loads(self.run_with_store(self.preview()).body)
        self.seed("removed-one")
        with self.assertRaises(HTTPException) as caught:
            self.run_with_store(self.purge({"preview_token": preview["purge_preview_token"], "confirm_irreversible": True}))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.store.list_history_system_summaries()[0]["total_rows"], 2)

    def test_adopted_history_is_preserved_when_old_preview_is_submitted(self):
        self.seed("removed-one")
        preview = json.loads(self.run_with_store(self.preview()).body)
        self.store.adopt_system_history("removed-one", "saved-target")
        with self.assertRaises(HTTPException) as caught:
            self.run_with_store(self.purge({"preview_token": preview["purge_preview_token"], "confirm_irreversible": True}))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.store.list_history_system_summaries()[0]["system_id"], "saved-target")

    def test_missing_explicit_confirmation_preserves_rows(self):
        self.seed("removed-one")
        preview = json.loads(self.run_with_store(self.preview()).body)
        with self.assertRaises(HTTPException) as caught:
            self.run_with_store(self.purge({"preview_token": preview["purge_preview_token"]}))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.store.list_history_system_summaries()[0]["total_rows"], 1)

    def test_preview_comparison_holds_sqlite_write_transaction(self):
        self.seed("removed-one")
        expected = self.store.list_history_system_summaries()
        original = self.store._list_history_system_summaries
        observations = []

        def observe(connection, **kwargs):
            observations.append(connection.in_transaction)
            return original(connection, **kwargs)

        with patch.object(self.store, "_list_history_system_summaries", side_effect=observe):
            self.store.purge_orphaned_history([], expected_summaries=expected)
        self.assertEqual(observations, [True])

    def test_changed_candidate_set_requires_repreview_then_exact_delete(self):
        self.seed("removed-one")
        preview = json.loads(self.run_with_store(self.preview()).body)
        self.assertIn("purge_preview_token", preview)
        self.seed("removed-two")
        with self.assertRaises(HTTPException) as caught:
            self.run_with_store(self.purge({"preview_token": preview["purge_preview_token"], "confirm_irreversible": True}))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(self.store.list_history_system_summaries()), 2)
        preview = json.loads(self.run_with_store(self.preview()).body)
        result = json.loads(self.run_with_store(self.purge({"preview_token": preview["purge_preview_token"], "confirm_irreversible": True})).body)
        self.assertEqual(sorted(result["summary"]["removed_system_ids"]), ["removed-one", "removed-two"])
        self.assertEqual(self.store.list_history_system_summaries(), [])
        with self.assertRaises(HTTPException):
            self.run_with_store(self.purge({"preview_token": preview["purge_preview_token"], "confirm_irreversible": True}))
