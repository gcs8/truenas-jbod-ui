"""The shipped upgrade notes must describe commands the shipped image has.

A published-image operator never clones the repository, so an upgrade note may
only name a command that the image, Compose, or the host shell provides. These
tests read the released v0.23.0 section of `CHANGELOG.md` and the matching
release notes and hold them to that, to a rollback path, and to a breaking
changes list that agrees with the rest of the section (#TBD, #430).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
RELEASE_NOTES = ROOT / "docs" / "RELEASE_NOTES_0.23.0.md"
DOCKERFILE = ROOT / "Dockerfile"
REPO_ONLY_HELPER = "prepare_nonroot_bind_mounts.py"


def release_section(version: str) -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.index(f"## {version}")
    rest = text[start + 1 :]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def subsection(section: str, heading: str) -> str:
    start = section.index(f"### {heading}")
    rest = section[start + 1 :]
    end = rest.find("\n### ")
    return rest if end < 0 else rest[:end]


def bullets(block: str) -> list[str]:
    found: list[str] = []
    for line in block.splitlines():
        if line.startswith("- "):
            found.append(line[2:].strip())
        elif found and line.startswith("  ") and line.strip():
            found[-1] = f"{found[-1]} {line.strip()}"
    return found


class UpgradeNotesContractTests(unittest.TestCase):
    def test_the_ownership_helper_is_still_absent_from_the_image(self) -> None:
        # The premise of the other tests: the helper is repository-only, so no
        # published-image instruction may depend on it.
        self.assertNotIn(REPO_ONLY_HELPER, DOCKERFILE.read_text(encoding="utf-8"))

    def test_released_upgrade_notes_never_require_the_repository_only_helper(self) -> None:
        self.assertNotIn(REPO_ONLY_HELPER, release_section("v0.23.0"))
        self.assertNotIn(REPO_ONLY_HELPER, RELEASE_NOTES.read_text(encoding="utf-8"))

    def test_released_upgrade_notes_carry_a_rollback_path(self) -> None:
        notes = subsection(release_section("v0.23.0"), "Upgrade notes")

        self.assertIn("Rolling back", notes)
        self.assertIn("JBOD_UI_IMAGE", notes)
        self.assertIn("Backup-Restore-and-Debug-Bundles", notes)

    def test_released_breaking_changes_name_one_authentication_outcome(self) -> None:
        breaking = bullets(subsection(release_section("v0.23.0"), "Breaking changes"))
        authentication = [
            bullet
            for bullet in breaking
            if re.search(r"authentic|public origin", bullet, re.IGNORECASE)
        ]

        self.assertEqual(len(authentication), 1, breaking)
        self.assertIn("#392", authentication[0])
        self.assertNotIn("(#201)", "\n".join(breaking))
        self.assertNotIn("(#245 and #246)", "\n".join(breaking))

    def test_one_feature_is_one_added_bullet(self) -> None:
        added = "\n".join(bullets(subsection(release_section("v0.23.0"), "Added")))

        self.assertNotIn("(#359)", added)
        self.assertIn("(#357 and #359)", added)


if __name__ == "__main__":
    unittest.main()
