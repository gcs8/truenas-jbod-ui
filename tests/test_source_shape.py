"""Source-shape checks: file-size ceilings and recorded cross-package imports.

Both checks stop the numbers from getting worse while large modules are split,
and make each split lock in its gain. The lists below only move down: when a
file shrinks or an import goes away, the test asks for the entry to be lowered
or removed. Raising a ceiling or recording a new import is a maintainer
decision that a reviewer reads in the diff.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --- File-size ceilings -------------------------------------------------------

SIZE_SCOPE_DIRS = ("admin_service", "app", "history_service", "qa", "scripts", "tests")
SIZE_SUFFIXES = (".py", ".js", ".css", ".html")
SKIPPED_DIR_NAMES = frozenset({"__pycache__", "node_modules", "vendor"})
MINIFIED_SUFFIXES = (".min.js", ".min.css")
DEFAULT_LINE_LIMIT = 3000
CEILING_STEP = 250
MAX_CEILING_ROOM = 750

# Every in-scope file over DEFAULT_LINE_LIMIT, with its ceiling computed once as
# (lines // 250 + 2) * 250. Lower an entry when the file shrinks; remove it when
# the file is at or under the default.
# One blank line between entries, so two pull requests that lower different
# entries merge without a conflict.
SIZE_CEILINGS: dict[str, int] = {
    "admin_service/static/admin.js": 9750,

    "app/services/inventory.py": 18250,

    "app/services/parsers.py": 5000,

    "app/services/sas_fabric.py": 3500,

    "app/services/snapshot_export.py": 3500,

    "app/static/app.js": 13750,

    "app/static/sas_fabric_view.js": 5000,

    "app/static/style.css": 7500,

    "history_service/store.py": 5500,

    "history_service/system_backup.py": 8000,

    "tests/test_admin_service.py": 6500,

    "tests/test_backup_integration.py": 4250,

    "tests/test_history_service.py": 11750,

    "tests/test_image_upgrade_contract.py": 3750,

    "tests/test_inventory.py": 27250,

    "tests/test_mapping_store.py": 4500,

    "tests/test_parsers.py": 5500,

    "tests/test_sas_fabric.py": 7000,

    "tests/test_segmented_history_reader.py": 4000,

    "tests/test_snapshot_export.py": 4000,

    "tests/test_system_backup.py": 10000,
}

# --- Cross-package imports ----------------------------------------------------

IMPORT_PACKAGES = ("admin_service", "app", "history_service")

# Per (importer, imported) package pair, every file that imports the other
# package today. Remove a file when its import goes away.
CROSS_PACKAGE_IMPORTS: dict[tuple[str, str], tuple[str, ...]] = {
    ("admin_service", "app"): (
        "admin_service/config.py",
        "admin_service/main.py",
        "admin_service/route_support.py",
        "admin_service/routes.py",
        "admin_service/services/account_bootstrap.py",
        "admin_service/services/esxi_host_prep.py",
        "admin_service/services/maintenance.py",
        "admin_service/services/runtime_control.py",
        "admin_service/services/tls_trust.py",
    ),
    ("admin_service", "history_service"): (
        "admin_service/route_support.py",
        "admin_service/routes.py",
        "admin_service/services/maintenance.py",
    ),
    ("app", "history_service"): (
        "app/history_routes.py",
        "app/metrics.py",
        "app/routes.py",
        "app/services/backup_health.py",
        "app/services/config_change_journal.py",
        "app/services/history_backend.py",
        "app/services/snapshot_export.py",
    ),
    ("history_service", "app"): (
        "history_service/backup_archive/editor.py",
        "history_service/backup_archive/policy.py",
        "history_service/backup_archive/transport.py",
        "history_service/backup_scheduler/main.py",
        "history_service/collector.py",
        "history_service/config.py",
        "history_service/domain.py",
        "history_service/main.py",
        "history_service/refresh_auth.py",
        "history_service/scheduled_backup_main.py",
        "history_service/sqlite_temp.py",
        "history_service/startup.py",
        "history_service/store.py",
        "history_service/system_backup.py",
    ),
}


def _walk(root: Path, top: str, suffixes: tuple[str, ...]) -> list[tuple[str, Path]]:
    base = root / top
    if not base.is_dir():
        return []
    found = []
    for path in base.rglob("*"):
        relative = path.relative_to(root)
        if any(part in SKIPPED_DIR_NAMES for part in relative.parts[:-1]):
            continue
        if path.is_file() and path.name.endswith(suffixes) and not path.name.endswith(MINIFIED_SUFFIXES):
            found.append((relative.as_posix(), path))
    return sorted(found)


def count_lines(path: Path) -> int:
    data = path.read_bytes()
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def ceiling_for(lines: int) -> int:
    return (lines // CEILING_STEP + 2) * CEILING_STEP


def size_violations(
    root: Path,
    ceilings: dict[str, int],
    scope_dirs: tuple[str, ...] = SIZE_SCOPE_DIRS,
    default: int = DEFAULT_LINE_LIMIT,
) -> list[str]:
    """Every file over its limit and every list entry that should move down."""

    sizes = {name: count_lines(path) for top in scope_dirs for name, path in _walk(root, top, SIZE_SUFFIXES)}
    violations = []
    for name, lines in sorted(sizes.items()):
        if name not in ceilings and lines > default:
            violations.append(
                f"{name}: {lines} lines, over the {default}-line default. Split it: move code into a new"
                f" module; adding a ceiling to SIZE_CEILINGS is a maintainer decision."
            )
    for name, ceiling in sorted(ceilings.items()):
        lines = sizes.get(name)
        if lines is None:
            violations.append(f"{name}: listed in SIZE_CEILINGS but not found; remove its entry.")
        elif lines <= default:
            violations.append(
                f"{name}: {lines} lines, at or under the {default}-line default; remove its entry from SIZE_CEILINGS."
            )
        elif lines > ceiling:
            violations.append(
                f"{name}: {lines} lines, over its ceiling of {ceiling}. Split it: move code into a new module;"
                f" raising a ceiling is a maintainer decision."
            )
        elif ceiling - lines >= MAX_CEILING_ROOM:
            violations.append(
                f"{name}: {lines} lines, ceiling {ceiling} is {ceiling - lines} above its size;"
                f" lower it to {ceiling_for(lines)} in SIZE_CEILINGS."
            )
    return violations


def cross_package_imports(
    root: Path, packages: tuple[str, ...] = IMPORT_PACKAGES
) -> dict[tuple[str, str], dict[str, int]]:
    """Map (importer, imported) to {file: first line} for each absolute import of another package."""

    found: dict[tuple[str, str], dict[str, int]] = {}
    for importer in packages:
        for name, path in _walk(root, importer, (".py",)):
            for node in ast.walk(ast.parse(path.read_bytes(), filename=name)):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    imported = module.split(".")[0]
                    if imported in packages and imported != importer:
                        lines = found.setdefault((importer, imported), {})
                        lines[name] = min(lines.get(name, node.lineno), node.lineno)
    return found


def import_violations(
    root: Path,
    recorded: dict[tuple[str, str], tuple[str, ...]],
    packages: tuple[str, ...] = IMPORT_PACKAGES,
) -> list[str]:
    """Every unrecorded cross-package import and every recorded one that is gone."""

    found = cross_package_imports(root, packages)
    violations = []
    for (importer, imported), files in sorted(found.items()):
        listed = set(recorded.get((importer, imported), ()))
        for name, line in sorted(files.items()):
            if name not in listed:
                violations.append(
                    f"{name}:{line}: new import of {imported} from {importer}, not recorded in"
                    f" CROSS_PACKAGE_IMPORTS[({importer!r}, {imported!r})]. Keep the code inside"
                    f" {importer}; recording a new cross-package import is a maintainer decision."
                )
    for (importer, imported), files in sorted(recorded.items()):
        for name in sorted(files):
            if name not in found.get((importer, imported), {}):
                violations.append(
                    f"{name}: recorded as importing {imported} but no longer does; remove its entry from"
                    f" CROSS_PACKAGE_IMPORTS[({importer!r}, {imported!r})]."
                )
    return violations


# --- Tests --------------------------------------------------------------------


class RepositoryShapeTests(unittest.TestCase):
    def test_file_sizes_stay_under_their_ceilings(self) -> None:
        violations = size_violations(ROOT, SIZE_CEILINGS)
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_cross_package_imports_match_the_record(self) -> None:
        violations = import_violations(ROOT, CROSS_PACKAGE_IMPORTS)
        self.assertEqual(violations, [], "\n" + "\n".join(violations))


class _TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_lines(self, relative: str, count: int) -> None:
        self.write(relative, "x = 1\n" * count)

    def only(self, violations: list[str]) -> str:
        self.assertEqual(len(violations), 1, violations)
        return violations[0]


class SizeCeilingCheckTests(_TreeCase):
    def check(self, ceilings: dict[str, int]) -> list[str]:
        return size_violations(self.root, ceilings, scope_dirs=("pkg",), default=100)

    def test_unlisted_file_over_the_default_fails(self) -> None:
        self.write_lines("pkg/big.py", 101)
        self.write_lines("pkg/edge.py", 100)
        violation = self.only(self.check({}))
        self.assertIn("pkg/big.py: 101 lines", violation)
        self.assertIn("100-line default", violation)
        self.assertIn("move code into a new module", violation)
        self.assertIn("maintainer decision", violation)

    def test_file_exactly_at_its_ceiling_passes(self) -> None:
        self.write_lines("pkg/listed.js", 750)
        self.assertEqual(self.check({"pkg/listed.js": 750}), [])

    def test_file_over_its_ceiling_fails(self) -> None:
        self.write_lines("pkg/listed.css", 751)
        violation = self.only(self.check({"pkg/listed.css": 750}))
        self.assertIn("pkg/listed.css: 751 lines, over its ceiling of 750", violation)
        self.assertIn("raising a ceiling is a maintainer decision", violation)

    def test_listed_file_that_is_missing_fails(self) -> None:
        violation = self.only(self.check({"pkg/gone.py": 500}))
        self.assertIn("pkg/gone.py", violation)
        self.assertIn("not found", violation)
        self.assertIn("remove its entry", violation)

    def test_listed_file_at_or_under_the_default_fails(self) -> None:
        self.write_lines("pkg/small.html", 100)
        violation = self.only(self.check({"pkg/small.html": 300}))
        self.assertIn("pkg/small.html: 100 lines, at or under the 100-line default", violation)
        self.assertIn("remove its entry", violation)

    def test_ceiling_with_too_much_room_fails(self) -> None:
        self.write_lines("pkg/shrunk.py", 250)
        self.write_lines("pkg/roomy.py", 251)
        violations = self.check({"pkg/shrunk.py": 1000, "pkg/roomy.py": 1000})
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("pkg/shrunk.py: 250 lines, ceiling 1000 is 750 above its size", violations[0])
        self.assertIn("lower it to 750", violations[0])

    def test_out_of_scope_minified_and_vendored_files_are_skipped(self) -> None:
        skipped = ("pkg/data.json", "pkg/lib.min.js", "pkg/vendor/lib.js", "pkg/__pycache__/m.py", "other/big.py")
        for relative in skipped:
            self.write_lines(relative, 101)
        self.assertEqual(self.check({}), [])

    def test_last_line_without_newline_is_counted(self) -> None:
        self.write("pkg/tail.py", "x = 1\n" * 100 + "y = 2")
        violation = self.only(self.check({}))
        self.assertIn("pkg/tail.py: 101 lines", violation)


class ImportBoundaryCheckTests(_TreeCase):
    PACKAGES = ("alpha", "beta")

    def found(self) -> dict[tuple[str, str], dict[str, int]]:
        return cross_package_imports(self.root, self.PACKAGES)

    def test_each_absolute_import_form_is_found_with_its_line(self) -> None:
        self.write("alpha/a.py", "import os\nimport beta.mod\n")
        self.write("alpha/b.py", "from beta import x\n")
        self.write("alpha/sub/c.py", "\nfrom beta.mod import x\n")
        self.write("alpha/d.py", "import beta\n")
        self.assertEqual(
            self.found(),
            {("alpha", "beta"): {"alpha/a.py": 2, "alpha/b.py": 1, "alpha/sub/c.py": 2, "alpha/d.py": 1}},
        )

    def test_imports_inside_functions_and_type_checking_are_found(self) -> None:
        self.write("beta/f.py", "def load():\n    from alpha.mod import x\n    return x\n")
        self.write("beta/t.py", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import alpha\n")
        self.assertEqual(self.found(), {("beta", "alpha"): {"beta/f.py": 2, "beta/t.py": 3}})

    def test_relative_same_package_and_third_party_imports_are_ignored(self) -> None:
        self.write("alpha/r.py", "from . import beta\nfrom .beta import x\nfrom .. import beta\n")
        self.write("alpha/s.py", "import alpha.mod\nfrom alpha import y\nimport betamax\nfrom gamma import beta\n")
        self.assertEqual(self.found(), {})

    def test_unrecorded_import_fails_with_file_line_and_package(self) -> None:
        self.write("alpha/listed.py", "import beta\n")
        self.write("alpha/new.py", "x = 1\n\nfrom beta.mod import y\n")
        violations = import_violations(self.root, {("alpha", "beta"): ("alpha/listed.py",)}, self.PACKAGES)
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("alpha/new.py:3: new import of beta", violations[0])
        self.assertIn("not recorded", violations[0])

    def test_recorded_file_without_the_import_fails(self) -> None:
        self.write("alpha/clean.py", "import os\n")
        violations = import_violations(
            self.root, {("alpha", "beta"): ("alpha/clean.py", "alpha/gone.py")}, self.PACKAGES
        )
        self.assertEqual(len(violations), 2, violations)
        self.assertIn("alpha/clean.py: recorded as importing beta but no longer does", violations[0])
        self.assertIn("remove its entry", violations[0])
        self.assertIn("alpha/gone.py", violations[1])

    def test_matching_record_passes(self) -> None:
        self.write("alpha/a.py", "import beta\n")
        self.write("beta/b.py", "from alpha import x\n")
        recorded = {("alpha", "beta"): ("alpha/a.py",), ("beta", "alpha"): ("beta/b.py",)}
        self.assertEqual(import_violations(self.root, recorded, self.PACKAGES), [])


if __name__ == "__main__":
    unittest.main()
