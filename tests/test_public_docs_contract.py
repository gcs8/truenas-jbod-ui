from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import unittest

import yaml

from scripts.check_public_docs import same_repository_main_path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_WIKI_PAGES = {
    "wiki/Admin-UI-and-System-Setup.md",
    "wiki/Advanced-Configuration.md",
    "wiki/Architecture-and-Services.md",
    "wiki/Backup-Restore-and-Debug-Bundles.md",
    "wiki/Demo-and-Offline-Workflows.md",
    "wiki/Docker-and-GHCR-Deployment.md",
    "wiki/Generic-Linux-Setup.md",
    "wiki/Heat-Map-Mode.md",
    "wiki/History-Maintenance-and-Recovery.md",
    "wiki/History-and-Snapshot-Export.md",
    "wiki/Home.md",
    "wiki/Live-Enclosures-and-Storage-Views.md",
    "wiki/Operations-Logging-and-Metrics.md",
    "wiki/Profiles-and-Custom-Layouts.md",
    "wiki/Public-Demo-Site.md",

    "wiki/Quantastor-Setup.md",
    "wiki/Quick-Start.md",
    "wiki/SSH-Setup-and-Sudo.md",
    "wiki/Troubleshooting.md",
    "wiki/TrueNAS-CORE-Setup.md",
    "wiki/TrueNAS-SCALE-Setup.md",
    "wiki/Visual-Tour.md",
    "wiki/_Sidebar.md",
}


def run_checker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/check_public_docs.py", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class PublicDocsContractTests(unittest.TestCase):
    def test_platform_api_first_examples_keep_tls_verification_opt_in(self) -> None:
        for relative_path in (
            "wiki/TrueNAS-CORE-Setup.md",
            "wiki/TrueNAS-SCALE-Setup.md",
            "wiki/Quantastor-Setup.md",
        ):
            guide = (ROOT / relative_path).read_text(encoding="utf-8")
            initial_setup = guide.split("## 2.", maxsplit=1)[0]
            with self.subTest(guide=relative_path):
                self.assertIn("verify_ssl: false", initial_setup)
                self.assertNotIn("verify_ssl: true", initial_setup)

    def test_admin_guide_does_not_describe_the_main_ui_as_read_only(self) -> None:
        guide = (ROOT / "wiki/Admin-UI-and-System-Setup.md").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("read-only enclosure UI", guide)
        self.assertIn("main enclosure UI", guide)

    def test_architecture_guide_states_the_reachability_boundary_plainly(self) -> None:
        guide = (ROOT / "wiki/Architecture-and-Services.md").read_text(
            encoding="utf-8"
        )
        normalized = " ".join(guide.split())

        self.assertNotRegex(guide, r"(?i)trusted (?:network|LAN)")
        self.assertIn("Anyone who can reach an enabled service", normalized)
        self.assertIn("Basic authentication", normalized)

    def test_readme_is_a_short_human_facing_entry_point(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertLessEqual(len(readme.splitlines()), 150)
        for required in (
            "https://gcs8.github.io/truenas-jbod-ui/",
            "public-demo-overview.png",
            "public-demo-history.png",
            "TRUENAS_HOST=https://truenas.example.test",
            "TRUENAS_API_KEY=replace-with-your-api-key",
            "TRUENAS_PLATFORM=core",
            "TRUENAS_VERIFY_SSL=false",
            "docker compose up -d",
            "wiki/Quick-Start.md",
        ):
            with self.subTest(required=required):
                self.assertIn(required, readme)

        for internal_note in (
            "Desktop support contract",
            "documentation inventory",
            "review baseline",
            "source revision",
            "release gate",
            "current `main` source build",
            "next release",
            "ADMIN_PUBLIC_ORIGIN",
            "APP_PUBLIC_ORIGIN",
            "ADMIN_AUTH_MODE",
            "TRUENAS_TLS_CA_BUNDLE_PATH",
        ):
            with self.subTest(internal_note=internal_note):
                self.assertNotIn(internal_note, readme)

    def test_repository_has_exact_readme_and_wiki_document_set(self) -> None:
        actual = {path.relative_to(ROOT).as_posix() for path in (ROOT / "wiki").glob("*.md")}

        self.assertEqual(actual, EXPECTED_WIKI_PAGES)
        self.assertTrue((ROOT / "README.md").is_file())
        self.assertEqual(len(actual), 23)
        self.assertEqual(len(actual) + 1, 24)
        self.assertFalse((ROOT / "wiki/Publishing-the-Wiki.md").exists())
        self.assertTrue((ROOT / "docs/PUBLISHING_THE_WIKI.md").is_file())

    def test_clean_checkout_docs_checker_passes(self) -> None:
        result = run_checker()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("24 documents", result.stdout)
        self.assertIn("local links", result.stdout)
        self.assertIn("YAML examples", result.stdout)
        self.assertIn("command paths", result.stdout)
        self.assertIn("configuration keys", result.stdout)
        self.assertIn("prose patterns", result.stdout)

    def test_documentation_inventory_is_archived_and_publishing_guide_names_the_gate(self) -> None:
        self.assertFalse((ROOT / "docs/DOCUMENTATION_INVENTORY.md").exists())
        self.assertTrue((ROOT / "docs/archive/DOCUMENTATION_INVENTORY.md").is_file())

        publishing = (ROOT / "docs/PUBLISHING_THE_WIKI.md").read_text(encoding="utf-8")
        self.assertIn("scripts/check_public_docs.py", publishing)

    def test_every_live_reference_document_is_linked_from_the_wiki_or_contributing(self) -> None:
        linkable = (
            (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
            + "".join((ROOT / page).read_text(encoding="utf-8") for page in sorted(EXPECTED_WIKI_PAGES))
            + (ROOT / "README.md").read_text(encoding="utf-8")
        )
        current_release_files = {"RELEASE_NOTES_0.23.0.md", "RELEASE_WRAP_0.23.0.md"}
        unlinked = [
            path.name
            for path in sorted((ROOT / "docs").glob("*.md"))
            if path.name not in current_release_files and f"docs/{path.name}" not in linkable
        ]
        self.assertEqual(unlinked, [])

    def test_quick_start_is_ca_optional_and_advanced_docs_cover_verification(self) -> None:
        quick_start = (ROOT / "wiki/Quick-Start.md").read_text(encoding="utf-8")
        advanced = (ROOT / "wiki/Advanced-Configuration.md").read_text(encoding="utf-8")
        operations = (ROOT / "wiki/Operations-Logging-and-Metrics.md").read_text(encoding="utf-8")

        self.assertIn("TRUENAS_VERIFY_SSL=false", quick_start)
        self.assertNotIn("TRUENAS_TLS_CA_BUNDLE_PATH", quick_start)
        self.assertIn("TRUENAS_VERIFY_SSL=true", advanced)
        self.assertIn("TRUENAS_TLS_CA_BUNDLE_PATH=/app/config/tls/truenas-ca.pem", advanced)
        self.assertIn("TRUENAS_TLS_SERVER_NAME=truenas.example.test", advanced)
        self.assertIn("trusted, isolated logging network", operations)
        self.assertIn("authenticated and encrypted", operations)

    def test_all_yaml_examples_parse(self) -> None:
        count = 0
        for relative_path in ("README.md", *sorted(EXPECTED_WIKI_PAGES)):
            text = (ROOT / relative_path).read_text(encoding="utf-8")
            parts = text.split("```yaml\n")
            for part in parts[1:]:
                block, separator, _rest = part.partition("\n```")
                self.assertTrue(separator, relative_path)
                yaml.safe_load(block)
                count += 1
        self.assertGreaterEqual(count, 20)

    def test_external_link_check_has_a_bounded_online_mode(self) -> None:
        help_result = run_checker("--help")

        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--check-external", help_result.stdout)
        self.assertIn("--external-timeout-seconds", help_result.stdout)
        self.assertIn("--external-max-attempts", help_result.stdout)

    def test_same_repository_main_links_validate_candidate_paths_without_traversal(self) -> None:
        self.assertEqual(
            same_repository_main_path(
                "https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/PUBLIC_DEMO_PRODUCT_BRIEF.md"
            ),
            Path("docs/PUBLIC_DEMO_PRODUCT_BRIEF.md"),
        )
        self.assertIsNone(
            same_repository_main_path(
                "https://github.com/another-owner/truenas-jbod-ui/blob/main/docs/PUBLIC_DEMO_PRODUCT_BRIEF.md"
            )
        )
        self.assertIsNone(
            same_repository_main_path(
                "https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/../private.txt"
            )
        )

    def test_public_demo_brief_records_product_browser_and_rollback_contracts(self) -> None:
        brief = (ROOT / "docs/PUBLIC_DEMO_PRODUCT_BRIEF.md").read_text(encoding="utf-8")
        checklist = (ROOT / "docs/RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

        for phrase in (
            "## Audience",
            "## Included interactions",
            "## Deliberate omissions",
            "## Fixture strategy",
            "## Source and build identity",
            "## Screenshot provenance",
            "## Supported desktop browser and layout matrix",
            "## Publication and readback",
            "## Revert and republish",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, brief)
        self.assertIn("Mobile and tablet layouts are unsupported", brief)
        self.assertIn("git worktree add --detach", checklist)
        self.assertIn("python3 scripts/check_public_demo_artifact.py public-demo", checklist)
        self.assertIn("byte-readback and browser jobs", checklist)

    def test_public_demo_pages_are_for_visitors(self) -> None:
        public_demo = (ROOT / "wiki/Public-Demo-Site.md").read_text(encoding="utf-8")
        visual_tour = (ROOT / "wiki/Visual-Tour.md").read_text(encoding="utf-8")
        sidebar = (ROOT / "wiki/_Sidebar.md").read_text(encoding="utf-8")
        publishing_guide = (ROOT / "docs/PUBLISHING_THE_WIKI.md").read_text(encoding="utf-8")

        for page in (public_demo, visual_tour):
            for maintainer_term in (
                "manifest",
                "pixel review",
                "source revision",
                "source-revision",
                "publication",
                "publish-public-demo.yml",
                "workflow_dispatch",
                "contract",
            ):
                with self.subTest(page=page.splitlines()[0], maintainer_term=maintainer_term):
                    self.assertNotIn(maintainer_term, page.lower())

        self.assertNotIn("Publishing-the-Wiki", sidebar)
        self.assertIn("python scripts/verify_wiki_drift.py", publishing_guide)
        self.assertIn("complete `images/` tree byte for byte", publishing_guide)
        self.assertIn("must pass before push", publishing_guide)
        self.assertIn("public Git URL and the pushed commit", publishing_guide)


if __name__ == "__main__":
    unittest.main()
