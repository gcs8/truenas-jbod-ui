"""Backup keeper comments say no more than the catalogue flag means (#1293).

The protected keeper is the newest copy whose ``verified`` catalogue flag is
set, which records its last completed check. For an FTP-style target that can
be the size check at upload, with no readback. The page, the catalogue refusal
and the changelog already say so; the implementation comments must not call
that copy the "newest verified copy". The stronger, separate guarantee (a
fresh size and SHA-256 read of the keeper before a retention deletion) stays.

Source text and synthetic local-directory targets only.
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
import tokenize
import unittest
from pathlib import Path

from history_service.backup_archive.lifecycle import LifecycleManager

import tests.test_backup_integration as backup_fixtures
from tests.test_backup_list_assurance import SizeOnlyTarget

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "history_service/backup_archive/lifecycle.py",
    "history_service/backup_archive/catalog.py",
    "history_service/backup_scheduler/service.py",
)
OVERCLAIM = re.compile(r"\bnewest\s+verified\b", re.IGNORECASE)


def _prose(path: Path) -> str:
    """Every comment and docstring in ``path``, whitespace folded."""

    source = path.read_text(encoding="utf-8")
    parts = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                parts.append(doc)
    return " ".join(" ".join(parts).split())


def _docstring(path: Path, name: str) -> str:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return " ".join((ast.get_docstring(node) or "").split())
    raise AssertionError(f"{name} not found in {path}")


class KeeperCommentWordingTests(unittest.TestCase):
    def test_comments_do_not_call_the_keeper_the_newest_verified_copy(self) -> None:
        for module in MODULES:
            with self.subTest(module=module):
                found = OVERCLAIM.findall(_prose(ROOT / module))
                self.assertEqual(found, [], f"{module} still describes the keeper as newest verified")

    def test_the_fresh_keeper_readback_guarantee_is_still_documented(self) -> None:
        guarantees = _prose(ROOT / MODULES[0])
        self.assertRegex(guarantees, r"``apply`` reads back the newest .{0,80}? and compares size and SHA-256 with the catalog")

    def test_keeper_docstrings_name_the_catalogue_flag(self) -> None:
        self.assertIn("flag", _docstring(ROOT / MODULES[0], "_newest_keeper"))
        self.assertIn("flag", _docstring(ROOT / MODULES[1], "mark_unverified"))

    def test_scanner_sees_a_split_overclaim(self) -> None:
        # Control: the check reads comments and docstrings across line breaks.
        sample = '"""The newest\n    verified copy."""\n# newest verified kept copy\nx = "newest verified"\n'
        text = " ".join(
            [
                tok.string
                for tok in tokenize.generate_tokens(io.StringIO(sample).readline)
                if tok.type == tokenize.COMMENT
            ]
            + [ast.get_docstring(ast.parse(sample), clean=False) or ""]
        )
        self.assertEqual(len(OVERCLAIM.findall(" ".join(text.split()))), 2)  # not the string literal


class SizeOnlyKeeperSelectionTests(backup_fixtures.SchedulerTestBase):
    """The keeper is chosen by the catalogue flag, before any readback."""

    def open_target(self, settings):
        @contextlib.contextmanager
        def opened():
            yield SizeOnlyTarget(self.remote_root / settings.target_id)

        return opened()

    def test_a_size_only_copy_is_the_keeper_before_any_readback(self) -> None:
        scheduler = self.make({"full": {"enabled": True}, "targets": [backup_fixtures.TARGET]})
        scheduler.run_now("full")
        remote = next(r for r in scheduler.catalog.list() if r.location == "nas")
        row = next(item for item in scheduler.library()["artifacts"] if item["id"] == remote.artifact_id)
        self.assertTrue(remote.verified)
        self.assertEqual(row["upload_check"], "size")
        self.assertIsNone(row["last_verify"])

        manager = LifecycleManager(scheduler.catalog, ())
        keeper = manager._newest_keeper(scheduler.catalog.list(backup_class="full", location="nas"), set())
        self.assertEqual(keeper.artifact_id, remote.artifact_id)


if __name__ == "__main__":
    unittest.main()
