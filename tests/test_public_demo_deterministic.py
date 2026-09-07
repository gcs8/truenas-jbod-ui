from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from pydantic import ValidationError

from app import __version__


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = Path("tests/fixtures/public_demo/public_demo.json")
LOCAL_PYTHON_PACKAGES = {"admin_service", "app", "history_service", "scripts"}
EXPECTED_INPUT_PATHS = {
    FIXTURE_PATH,
    Path("app/__init__.py"),
    Path("app/config.py"),
    Path("app/logging_config.py"),
    Path("app/main.py"),
    Path("app/metrics.py"),
    Path("app/models/domain.py"),
    Path("app/perf.py"),
    Path("app/request_context.py"),
    Path("app/script_json.py"),
    Path("app/secret_files.py"),
    Path("app/slot_layout.py"),
    Path("app/services/history_backend.py"),
    Path("app/services/history_status.py"),
    Path("app/services/profile_registry.py"),
    Path("app/services/public_demo_fixture.py"),
    Path("app/services/snapshot_export.py"),
    Path("app/services/storage_view_templates.py"),
    Path("app/services/storage_views.py"),
    Path("scripts/build_public_demo.py"),
    Path("scripts/public_demo_inputs.py"),
    Path("scripts/public_demo_source_parity.py"),
    Path("app/static/app.js"),
    Path("app/static/style.css"),
    Path("app/templates/base.html"),
    Path("app/templates/index.html"),
    Path("history_service/operation_bounds.py"),
    Path("history_service/scheduled_backup.py"),
    Path("app/static/images/aoc-slg4-2h8m2.jpg"),
    Path("app/static/images/hyper-m2-gen3-card.png"),
    Path("app/static/images/satadom-ml-3ie3-v2.png"),
}


def run_checker(demo_dir: Path, *, source_root: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "scripts/check_public_demo_artifact.py", str(demo_dir)]
    if source_root is not None:
        command.extend(("--source-root", str(source_root)))
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def recursive_local_python_inputs(entrypoint: Path) -> set[Path]:
    def resolve_module(module_name: str) -> Path | None:
        module_path = ROOT.joinpath(*module_name.split("."))
        for candidate in (module_path.with_suffix(".py"), module_path / "__init__.py"):
            if candidate.is_file():
                return candidate
        return None

    def module_name(path: Path) -> str:
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    pending = [ROOT / entrypoint]
    discovered: set[Path] = set()
    while pending:
        source_path = pending.pop()
        relative_path = source_path.relative_to(ROOT)
        if relative_path in discovered:
            continue
        discovered.add(relative_path)
        current_module = module_name(source_path)
        package = current_module if source_path.name == "__init__.py" else current_module.rsplit(".", 1)[0]
        parsed = ast.parse(source_path.read_text(encoding="utf-8"), filename=relative_path.as_posix())
        candidates: list[str] = []
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            base = node.module or ""
            if node.level:
                package_parts = package.split(".") if package else []
                keep = max(0, len(package_parts) - (node.level - 1))
                base = ".".join(package_parts[:keep] + ([base] if base else []))
            if base:
                candidates.append(base)
                candidates.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        for candidate in candidates:
            if candidate.split(".", 1)[0] not in LOCAL_PYTHON_PACKAGES:
                continue
            resolved = resolve_module(candidate)
            if resolved is not None:
                pending.append(resolved)
    return discovered


def workflow_paths_for_event(workflow: str, event_name: str) -> set[str]:
    lines = workflow.splitlines()
    event_header = f"  {event_name}:"
    start = lines.index(event_header)
    paths_start = next(index for index in range(start + 1, len(lines)) if lines[index] == "    paths:")
    paths: set[str] = set()
    for line in lines[paths_start + 1 :]:
        if line.startswith("  ") and not line.startswith("      "):
            break
        if line.startswith('      - "') and line.endswith('"'):
            paths.add(line.removeprefix('      - "').removesuffix('"'))
    return paths


class DeterministicPublicDemoContractTests(unittest.TestCase):
    def test_clean_source_materialization_regenerates_without_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            materialized = Path(temp_dir) / "repo"
            for relative_dir in ("admin_service", "app", "history_service", "scripts"):
                shutil.copytree(
                    ROOT / relative_dir,
                    materialized / relative_dir,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            fixture_target = materialized / FIXTURE_PATH
            fixture_target.parent.mkdir(parents=True, exist_ok=True)
            if (ROOT / FIXTURE_PATH).exists():
                shutil.copy2(ROOT / FIXTURE_PATH, fixture_target)
            output = materialized / "public-demo" / "index.html"
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(materialized),
                "APP_CONFIG_PATH": str(materialized / "absent-config.yaml"),
                "PYTHONHASHSEED": "random",
            }
            result = subprocess.run(
                [sys.executable, "scripts/build_public_demo.py", "--output", str(output)],
                cwd=materialized,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            self.assertNotIn("history/history.db", result.stderr)

    def test_checked_artifact_rejects_source_version_drift(self) -> None:
        module = importlib.import_module("scripts.public_demo_inputs")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_root = temp_root / "source"
            demo_dir = temp_root / "public-demo"
            demo_dir.mkdir(parents=True)
            shutil.copy2(ROOT / "public-demo/index.html", demo_dir / "index.html")
            shutil.copy2(ROOT / "public-demo/.nojekyll", demo_dir / ".nojekyll")
            for relative_path in module.PUBLIC_DEMO_INPUT_PATHS:
                target = source_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative_path, target)
            version_file = source_root / "app/__init__.py"
            version_file.write_text(
                version_file.read_text(encoding="utf-8").replace(__version__, "0.22.3"),
                encoding="utf-8",
            )
            result = run_checker(demo_dir, source_root=source_root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact app version 0.22.2 does not match source 0.22.3", result.stderr)

    def test_shared_input_graph_is_complete_and_unique(self) -> None:
        module = importlib.import_module("scripts.public_demo_inputs")
        actual = tuple(module.PUBLIC_DEMO_INPUT_PATHS)

        self.assertEqual(set(actual), EXPECTED_INPUT_PATHS)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertEqual(actual, tuple(sorted(actual, key=lambda path: path.as_posix())))

    def test_shared_input_graph_covers_recursive_local_python_imports(self) -> None:
        module = importlib.import_module("scripts.public_demo_inputs")
        declared = set(module.PUBLIC_DEMO_INPUT_PATHS)
        imported = recursive_local_python_inputs(Path("scripts/build_public_demo.py"))

        self.assertEqual(imported - declared, set())

    def test_every_declared_input_mutation_invalidates_checked_artifact(self) -> None:
        module = importlib.import_module("scripts.public_demo_inputs")
        input_paths = tuple(module.PUBLIC_DEMO_INPUT_PATHS)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_root = temp_root / "source"
            demo_dir = temp_root / "public-demo"
            demo_dir.mkdir(parents=True)
            shutil.copy2(ROOT / "public-demo/index.html", demo_dir / "index.html")
            shutil.copy2(ROOT / "public-demo/.nojekyll", demo_dir / ".nojekyll")
            for relative_path in input_paths:
                target = source_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative_path, target)

            for relative_path in input_paths:
                changed = source_root / relative_path
                original = changed.read_bytes()
                changed.write_bytes(original + b"\x00")
                try:
                    result = run_checker(demo_dir, source_root=source_root)
                finally:
                    changed.write_bytes(original)
                with self.subTest(path=relative_path.as_posix()):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        f"source fingerprint mismatch: {relative_path.as_posix()}",
                        result.stderr,
                    )

    def test_publish_workflow_watches_every_declared_input(self) -> None:
        module = importlib.import_module("scripts.public_demo_inputs")
        workflow = (ROOT / ".github/workflows/publish-public-demo.yml").read_text(encoding="utf-8")

        expected_paths = {path.as_posix() for path in module.PUBLIC_DEMO_INPUT_PATHS}
        expected_paths.update(
            {
                ".github/workflows/publish-public-demo.yml",
                "public-demo/**",
                "qa/public-demo.spec.js",
                "scripts/build_current_source_browser_fixture.py",
                "scripts/check_public_demo_artifact.py",
            }
        )
        for event_name in ("pull_request", "push"):
            with self.subTest(event_name=event_name):
                self.assertEqual(workflow_paths_for_event(workflow, event_name), expected_paths)

    def test_pages_deploy_requires_manual_dispatch(self) -> None:
        workflow = (ROOT / ".github/workflows/publish-public-demo.yml").read_text(encoding="utf-8")

        self.assertNotIn("if: github.event_name != 'pull_request'", workflow)
        self.assertEqual(workflow.count("if: github.event_name == 'workflow_dispatch'"), 3)

    def test_docs_define_deterministic_regeneration_and_publication_boundaries(self) -> None:
        public_readme = (ROOT / "public-demo/README.md").read_text(encoding="utf-8")
        contributor_rails = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        release_checklist = (ROOT / "docs/RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
        pull_request_template = (ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")

        for document in (public_readme, contributor_rails, release_checklist):
            self.assertIn("tests/fixtures/public_demo/public_demo.json", document)
            self.assertIn("python scripts/build_public_demo.py --output public-demo/index.html", document)
            self.assertIn("workflow_dispatch", document)
        self.assertIn("does not publish", public_readme)
        self.assertIn("do not deploy", public_readme)
        self.assertIn("public-demo/**", pull_request_template)
        self.assertIn("do not deploy GitHub Pages", pull_request_template)

    def test_fixture_is_schema_bounded_and_rejects_unknown_fields(self) -> None:
        fixture_module = importlib.import_module("app.services.public_demo_fixture")
        fixture = fixture_module.load_public_demo_fixture(ROOT / FIXTURE_PATH)
        payload = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))
        payload["ambient_local_value"] = "must fail closed"

        self.assertEqual(fixture.schema_version, 1)
        self.assertEqual(fixture.provenance, "synthetic")
        with self.assertRaises(ValidationError):
            fixture_module.PublicDemoFixture.model_validate(payload)

    def test_fixture_uses_only_reviewable_synthetic_patterns(self) -> None:
        fixture_module = importlib.import_module("app.services.public_demo_fixture")
        fixture_module.load_public_demo_fixture(ROOT / FIXTURE_PATH)
        fixture_text = (ROOT / FIXTURE_PATH).read_text(encoding="utf-8")

        self.assertNotRegex(fixture_text, r"(?<![0-9])(?:10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))\.")
        self.assertNotRegex(fixture_text, r"(?i)\b(?:password|passwd|api[_-]?key|secret|token|private[_-]?key)\b")
        self.assertNotRegex(fixture_text, r"-----BEGIN [A-Z ]+PRIVATE KEY-----")
        self.assertNotRegex(fixture_text, r"(?i)\b(?:wwn|naa)\.[0-9a-f]{16,}\b")
        self.assertRegex(fixture_text, re.compile(r'"provenance": "synthetic"'))
        self.assertIn('"id": "demo-system"', fixture_text)

    def test_builder_ignores_ambient_operator_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            malformed_config = temp_root / "operator.yaml"
            malformed_config.write_text("systems: [\n", encoding="utf-8")
            outputs = (temp_root / "ambient.html", temp_root / "clean.html")
            results = []
            for output, config_path in zip(outputs, (malformed_config, temp_root / "missing.yaml"), strict=True):
                env = os.environ.copy()
                env["APP_CONFIG_PATH"] = str(config_path)
                env["PYTHONHASHSEED"] = "random"
                results.append(
                    subprocess.run(
                        [sys.executable, "scripts/build_public_demo.py", "--output", str(output)],
                        cwd=ROOT,
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                )

            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())


if __name__ == "__main__":
    unittest.main()
