#!/usr/bin/env python3
"""Run one shard of the Python unittest suite, or verify the shard results in CI.

CI splits ``python -m unittest discover -s tests -p "test_*.py"`` across parallel
jobs by test module. The split is the explicit ``SHARDS`` table below: every
``tests/test_*.py`` module belongs to exactly one shard, and both ``run`` and
``verify`` refuse to proceed when a module on disk is unassigned or assigned
twice, so a new test file cannot fall outside every shard. ``tests.test_unittest_shards``
asserts the same partition and that ``.github/workflows/ci.yml`` runs every shard.

    python scripts/run_test_shard.py run --shard 1 --results-dir shard-results
    python scripts/run_test_shard.py verify --results-dir shard-results --python-version 3.14
    python scripts/run_test_shard.py list

``run`` imports the shard's modules the way discovery does (``tests`` is the
top-level directory, so a module is ``test_x``, not ``tests.test_x``) and writes
``shard-<id>.json`` with the outcome and per-module discovered test counts.
``verify`` requires one successful result for every shard of the expected Python
version and compares those counts with the reviewed manifest at
``tests/unittest_test_counts.json``. Skipped tests still count because the
contract records discovery, not platform-dependent outcomes.

    python scripts/run_test_shard.py check-counts
    python scripts/run_test_shard.py update-counts  # review the manifest diff

Local full runs keep using discovery (``scripts/dev_check.py --safe``); the shard
table only serves CI. Rebalance it from a per-test timing run when a shard
drifts well above the others.
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Run as a script, sys.path[0] is scripts/; discovery under `python -m unittest`
# had the working directory (the repository root) there instead, which is what
# `from app ...` and `from tests import heap_probe` in the test modules rely on.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = ROOT / "tests"
TEST_MODULE_PATTERN = "test_*.py"
TEST_COUNT_MANIFEST = TESTS_DIR / "unittest_test_counts.json"
TEST_COUNT_MANIFEST_SCHEMA = 1
RESULT_PREFIX = "shard-"
RESULT_SUFFIX = ".json"

# Balanced from a per-test timing run on ubuntu-latest (2026-09-24, Python 3.12
# and 3.14 averaged, with the slow-test fixes of #569 applied). The backup and
# rotation suites, the two heaviest, sit on different shards. Keep each tuple
# sorted; the guard test rejects a module listed twice or not at all.
SHARDS: dict[str, tuple[str, ...]] = {
    "1": (
        "test_account_bootstrap",
        "test_admin_config",
        "test_changelog_entry_gate",
        "test_dev_check",
        "test_disk_retention_accounting",
        "test_heap_probe",
        "test_history_backup_coupling",
        "test_history_config_contract",
        "test_history_diagnostics",
        "test_history_health_states",
        "test_history_released_schema_upgrades",
        "test_jbod_runner_trial",
        "test_mapping_store",
        "test_metrics",
        "test_perf",
        "test_perf_harness",
        "test_platform_parity_fixtures",
        "test_private_qa_restore",
        "test_process_secrets",
        "test_profile_builder",
        "test_prometheus_alert_rules",
        "test_public_demo_provenance",
        "test_public_screenshots",
        "test_segment_sealer",
        "test_snapshot_export",
        "test_ssh_failure_contexts",
        "test_ssh_session_reuse",
        "test_storage_view_smart_batch",
        "test_system_backup",
        "test_system_setup_api_dialect",
        "test_truenas_ws",
    ),
    "2": (
        "test_admin_runtime_routes",
        "test_admin_safety",
        "test_backup_integration",
        "test_ci_contract",
        "test_compose_runtime_matrix",
        "test_config_example",
        "test_enclosure_option_labels",
        "test_full_backup_benchmark",
        "test_history_backend",
        "test_history_operation_bounds",
        "test_history_refresh_bounds",
        "test_image_upgrade_contract",
        "test_immutable_deployment",
        "test_logging_config",
        "test_parsers",
        "test_public_demo_deployment",
        "test_public_demo_history_consistency",
        "test_release_status",
        "test_script_json",
        "test_script_platform_guards",
        "test_scripts_help",
        "test_segment_rotation",
        "test_segmented_history",
        "test_segmented_history_reader",
        "test_segmented_restore_recovery",
        "test_unittest_shards",
        "test_upgrade_notice",
    ),
    "3": (
        "test_admin_command_state",
        "test_admin_error_correlation",
        "test_app_history_body_bound",
        "test_app_history_bounds",
        "test_backup_archive_journal",
        "test_backup_archive_lifecycle",
        "test_backup_archive_transport",
        "test_container_contract",
        "test_enclosure_aliases",
        "test_history_backend_bounds",
        "test_history_bulk_bounds",
        "test_history_runtime_damage_pause",
        "test_mapping_routes",
        "test_nonroot_migration",
        "test_public_demo_deterministic",
        "test_public_demo_fixture",
        "test_quantastor_api",
        "test_read_ui_auth",
        "test_release_wrap_validator",
        "test_sas_fabric",
        "test_scheduled_backup",
        "test_segment_migration",
        "test_slot_bounds_routes",
        "test_startup_config",
        "test_startup_migration_recovery",
        "test_startup_writability",
        "test_tls_trust",
        "test_wiki_drift_verifier",
    ),
    "4": (
        "test_admin_auth",
        "test_admin_maintenance",
        "test_admin_secret_models",
        "test_admin_service",
        "test_admin_ttl",
        "test_bash_ci_contract",
        "test_disk_inventory_sync",
        "test_disk_inventory_sync_grants",
        "test_esxi_host_prep",
        "test_ghcr_release_contract",
        "test_history_routes",
        "test_history_schema_version_gate",
        "test_history_service",
        "test_inventory",
        "test_inventory_registry_routes",
        "test_nonroot_cli",
        "test_perf_budgets",
        "test_profiles",
        "test_public_doc_privacy",
        "test_public_docs_contract",
        "test_release_changelog_coverage",
        "test_route_contracts",
        "test_settings_reload",
        "test_slot_detail_store",
        "test_smart_grid_io",
        "test_ssh_probe",
        "test_truenas_ws_jsonrpc",
        "test_ui_health_and_admin_probe",
    ),
}


def discovered_modules(tests_dir: Path = TESTS_DIR) -> tuple[str, ...]:
    """Module names discovery would collect from ``tests_dir``."""

    return tuple(sorted(path.stem for path in tests_dir.glob(TEST_MODULE_PATTERN) if path.is_file()))


def partition_problems(
    shards: dict[str, tuple[str, ...]] = SHARDS,
    tests_dir: Path = TESTS_DIR,
) -> list[str]:
    """Every module on disk in exactly one shard, and nothing assigned that is not on disk."""

    problems: list[str] = []
    assigned = Counter(module for modules in shards.values() for module in modules)
    on_disk = set(discovered_modules(tests_dir))
    for module, count in sorted(assigned.items()):
        if count > 1:
            problems.append(f"{module} is assigned to {count} shards")
        if module not in on_disk:
            problems.append(f"{module} is assigned but tests/{module}.py does not exist")
    for module in sorted(on_disk - set(assigned)):
        problems.append(f"tests/{module}.py is not assigned to any shard")
    for shard_id, modules in shards.items():
        if tuple(sorted(modules)) != tuple(modules):
            problems.append(f"shard {shard_id} is not sorted")
        if not modules:
            problems.append(f"shard {shard_id} is empty")
    return problems


def result_path(results_dir: Path, shard_id: str) -> Path:
    return results_dir / f"{RESULT_PREFIX}{shard_id}{RESULT_SUFFIX}"


def python_version_label() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def build_suite_and_counts(
    modules: tuple[str, ...],
    tests_dir: Path = TESTS_DIR,
    *,
    fail_on_discovery_errors: bool = False,
) -> tuple[unittest.TestSuite, dict[str, int]]:
    """Load modules in discovery form and retain each module's discovered count."""

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    counts: dict[str, int] = {}
    for module in modules:
        error_count = len(loader.errors)
        module_suite = loader.discover(str(tests_dir), pattern=f"{module}.py")
        if fail_on_discovery_errors and len(loader.errors) > error_count:
            raise ValueError(f"{module} failed discovery; fix imports before updating counts")
        counts[module] = module_suite.countTestCases()
        suite.addTests(module_suite)
    return suite, counts


def build_suite(modules: tuple[str, ...], tests_dir: Path = TESTS_DIR) -> unittest.TestSuite:
    """Load ``modules`` exactly as ``unittest discover -s <tests_dir>`` would."""

    return build_suite_and_counts(modules, tests_dir)[0]


def discovered_test_counts(
    modules: tuple[str, ...], tests_dir: Path = TESTS_DIR
) -> dict[str, int]:
    """Return sorted per-module counts; skips remain part of discovery."""

    _suite, counts = build_suite_and_counts(
        tuple(sorted(modules)), tests_dir, fail_on_discovery_errors=True
    )
    return counts


def load_test_count_manifest(path: Path = TEST_COUNT_MANIFEST) -> dict[str, int]:
    """Load and validate the reviewed per-module discovery-count manifest."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != TEST_COUNT_MANIFEST_SCHEMA:
        raise ValueError(f"expected schema {TEST_COUNT_MANIFEST_SCHEMA}")
    modules = payload.get("modules")
    if not isinstance(modules, dict):
        raise ValueError("modules must be an object")
    for module, count in modules.items():
        if not isinstance(module, str) or not module:
            raise ValueError("module names must be non-empty strings")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError(f"{module}: count must be a positive integer")
    if list(modules) != sorted(modules):
        raise ValueError("module names must be sorted")
    return modules


def write_test_count_manifest(
    path: Path = TEST_COUNT_MANIFEST,
    *,
    modules: tuple[str, ...] | None = None,
    tests_dir: Path = TESTS_DIR,
) -> dict[str, int]:
    """Regenerate the reviewed manifest; callers must review the resulting diff."""

    counts = discovered_test_counts(modules or discovered_modules(tests_dir), tests_dir)
    payload = {"schema": TEST_COUNT_MANIFEST_SCHEMA, "modules": counts}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return counts


def test_count_problems(expected: dict[str, int], actual: dict[str, int]) -> list[str]:
    """Describe every module/count change without depending on discovery order."""

    problems: list[str] = []
    for module in sorted(set(expected) | set(actual)):
        old = expected.get(module)
        new = actual.get(module)
        if old is None:
            problems.append(
                f"test-count manifest: {module} is new with {new} tests; update the manifest"
            )
        elif new is None:
            problems.append(
                f"test-count manifest: {module} was not reported (expected {old} tests)"
            )
        elif new < old:
            lost = old - new
            noun = "test" if lost == 1 else "tests"
            problems.append(
                f"test-count manifest: {module} lost {lost} {noun} (expected {old}, found {new})"
            )
        elif new > old:
            gained = new - old
            noun = "test" if gained == 1 else "tests"
            problems.append(
                f"test-count manifest: {module} gained {gained} {noun} "
                f"(expected {old}, found {new}); update the manifest"
            )
    return problems


def run_shard(
    shard_id: str,
    *,
    shards: dict[str, tuple[str, ...]] = SHARDS,
    tests_dir: Path = TESTS_DIR,
    results_dir: Path | None = None,
    stream=None,
) -> int:
    problems = partition_problems(shards, tests_dir)
    if problems:
        for problem in problems:
            print(f"shard partition: {problem}", file=sys.stderr)
        return 2
    if shard_id not in shards:
        print(f"unknown shard {shard_id!r}; known: {', '.join(shards)}", file=sys.stderr)
        return 2

    modules = shards[shard_id]
    if results_dir is not None:
        # Created before the run so a coverage data file pointed here by
        # COVERAGE_FILE has somewhere to land even when the run aborts early.
        results_dir.mkdir(parents=True, exist_ok=True)
    # `python -m unittest` enables "default" warnings unless -W was given.
    warnings = None if sys.warnoptions else "default"
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, warnings=warnings)
    suite, test_counts = build_suite_and_counts(modules, tests_dir)
    result = runner.run(suite)

    if results_dir is not None:
        payload = {
            "shard": shard_id,
            "python_version": python_version_label(),
            "modules": list(modules),
            "test_counts": test_counts,
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "was_successful": result.wasSuccessful(),
        }
        result_path(results_dir, shard_id).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0 if result.wasSuccessful() else 1


def verify_results(
    results_dir: Path,
    expected_python_version: str,
    *,
    shards: dict[str, tuple[str, ...]] = SHARDS,
    tests_dir: Path = TESTS_DIR,
    expected_counts: dict[str, int] | None = None,
) -> list[str]:
    """Problems that must make the required check fail; empty means every shard passed."""

    problems = partition_problems(shards, tests_dir)
    found: dict[str, dict] = {}
    if not results_dir.is_dir():
        return problems + [f"results directory is missing: {results_dir}"]
    compare_counts = True
    if expected_counts is None:
        try:
            expected_counts = load_test_count_manifest()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            problems.append(f"test-count manifest: cannot load {TEST_COUNT_MANIFEST} ({exc})")
            expected_counts = {}
            compare_counts = False
    actual_counts: dict[str, int] = {}
    for path in sorted(results_dir.glob(f"{RESULT_PREFIX}*{RESULT_SUFFIX}")):
        shard_id = path.name[len(RESULT_PREFIX) : -len(RESULT_SUFFIX)]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"shard {shard_id}: unreadable result ({exc})")
            continue
        if not isinstance(payload, dict):
            problems.append(f"shard {shard_id}: result is not an object")
            continue
        found[shard_id] = payload

    for shard_id in sorted(set(found) - set(shards)):
        problems.append(f"shard {shard_id}: result present but the shard is not in the table")
    for shard_id, modules in shards.items():
        payload = found.get(shard_id)
        if payload is None:
            problems.append(f"shard {shard_id}: no result (job failed before writing, was skipped, or never ran)")
            continue
        if payload.get("python_version") != expected_python_version:
            problems.append(
                f"shard {shard_id}: ran on Python {payload.get('python_version')!r}, "
                f"expected {expected_python_version!r}"
            )
        if payload.get("modules") != list(modules):
            problems.append(f"shard {shard_id}: module list differs from the table")
        shard_counts = payload.get("test_counts")
        if not isinstance(shard_counts, dict):
            problems.append(f"shard {shard_id}: test_counts is not an object")
        else:
            if set(shard_counts) != set(modules):
                problems.append(f"shard {shard_id}: test_counts module list differs from the table")
            for module, count in shard_counts.items():
                if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                    problems.append(f"shard {shard_id}: {module} has invalid test count {count!r}")
                elif module in modules:
                    actual_counts[module] = count
        if not isinstance(payload.get("tests_run"), int) or payload["tests_run"] <= 0:
            problems.append(f"shard {shard_id}: ran no tests")
        if payload.get("was_successful") is not True:
            problems.append(
                f"shard {shard_id}: not successful "
                f"(failures={payload.get('failures')}, errors={payload.get('errors')}, "
                f"unexpected successes={payload.get('unexpected_successes')})"
            )
    if compare_counts:
        problems.extend(test_count_problems(expected_counts, actual_counts))
    return problems


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run or verify one CI shard of the Python unittest suite.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one shard's test modules")
    run.add_argument("--shard", required=True, choices=sorted(SHARDS), help="shard id from the SHARDS table")
    run.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="directory that receives shard-<id>.json with the outcome (omit to skip)",
    )

    verify = commands.add_parser("verify", help="require one successful result per shard")
    verify.add_argument("--results-dir", type=Path, required=True, help="directory holding shard-<id>.json files")
    verify.add_argument(
        "--python-version",
        required=True,
        help="major.minor every result must have run on, for example 3.14",
    )

    commands.add_parser("check-counts", help="compare full discovery with the reviewed count manifest")
    commands.add_parser("update-counts", help="regenerate the count manifest for review")
    commands.add_parser("list", help="print the shard table")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "list":
        for shard_id, modules in SHARDS.items():
            print(f"shard {shard_id}: {len(modules)} modules")
            for module in modules:
                print(f"  {module}")
        problems = partition_problems()
        for problem in problems:
            print(f"shard partition: {problem}", file=sys.stderr)
        return 2 if problems else 0
    if args.command in {"check-counts", "update-counts"}:
        problems = partition_problems()
        if problems:
            for problem in problems:
                print(f"shard partition: {problem}", file=sys.stderr)
            return 2
        if args.command == "update-counts":
            try:
                counts = write_test_count_manifest()
            except ValueError as exc:
                print(f"test-count manifest: cannot update ({exc})", file=sys.stderr)
                return 1
            print(
                f"test-count manifest: wrote {len(counts)} modules / "
                f"{sum(counts.values())} tests to {TEST_COUNT_MANIFEST}"
            )
            return 0
        try:
            expected = load_test_count_manifest()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"test-count manifest: cannot load {TEST_COUNT_MANIFEST} ({exc})", file=sys.stderr)
            return 1
        try:
            actual = discovered_test_counts(discovered_modules())
        except ValueError as exc:
            print(f"test-count manifest: cannot check ({exc})", file=sys.stderr)
            return 1
        problems = test_count_problems(expected, actual)
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
        print(
            f"test-count manifest: full discovery matches "
            f"{len(actual)} modules / {sum(actual.values())} tests"
        )
        return 0
    if args.command == "run":
        return run_shard(args.shard, results_dir=args.results_dir)
    problems = verify_results(args.results_dir, args.python_version)
    if problems:
        for problem in problems:
            print(f"shard verify: {problem}", file=sys.stderr)
        return 1
    print(f"shard verify: every shard passed on Python {args.python_version} ({', '.join(SHARDS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
