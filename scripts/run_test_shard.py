#!/usr/bin/env python3
"""Run one shard of the Python unittest suite, or verify the shard results in CI.

CI splits ``python -m unittest discover -s tests -p "test_*.py"`` across parallel
jobs by test module. The split is the explicit ``SHARDS`` table below: every
``tests/test_*.py`` module belongs to exactly one shard, and both ``run`` and
``verify`` refuse to proceed when a module on disk is unassigned or assigned
twice, so a new test file cannot fall outside every shard. ``tests.test_unittest_shards``
asserts the same partition and that ``.github/workflows/ci.yml`` runs every shard.

    python scripts/run_test_shard.py run --shard 1 --results-dir shard-results
    python scripts/run_test_shard.py discover --output inventory.json
    python scripts/run_test_shard.py verify --results-dir shard-results \\
        --python-version 3.14 --inventory inventory.json
    python scripts/run_test_shard.py compare --base base.json --candidate inventory.json
    python scripts/run_test_shard.py list

``run`` imports the shard's modules the way discovery does (``tests`` is the
top-level directory, so a module is ``test_x``, not ``tests.test_x``) and writes
``shard-<id>-attempt-<attempt>.json`` in CI. The receipt lists every test
identity (``case.id()``) the shard collected, started and completed.
``--run-attempt`` defaults to ``SHARD_RUN_ATTEMPT``; without either, local runs
retain the unversioned ``shard-<id>.json`` name.

``discover`` builds the expected set independently of ``SHARDS``: it loads every
``tests/test_*.py`` module on disk, cross-checks that against one unfiltered
``discover`` of the directory, and refuses import errors, module-level
``SkipTest`` (also in a nested test package the unfiltered pass reaches), a
module with no tests, a duplicate identity and an identity owned by another
module. The inventory is generated in CI and never committed.

``verify`` recursively selects the highest numeric attempt for each shard,
allowing unaffected shards from earlier attempts of a failed-only rerun;
ambiguous or malformed receipts fail closed. Every latest receipt must be green,
bound to the inventory's commit, workflow run, Python version and shard table,
and must have collected, started and completed exactly the inventory identities
of its modules. Across shards every identity must have completed exactly once,
so an omitted, repeated or substituted test fails with its identity named.
Skipped tests still count as completed: the contract is about which tests were
scheduled, not platform-dependent outcomes.

``compare`` lists identities present in a base inventory but missing from the
candidate (removed or renamed tests) and those that are new. Removals fail
unless ``--removals-approved`` is given; CI passes it only when the pull request
carries the ``tests-removed-ok`` label. This is a guard against unnoticed
removal, not a trust boundary: it runs in the pull request's own workflow, with
this script and the test code from the pull request, so a pull request that
edits ``.github/workflows/ci.yml``, this script or the test code it imports can
switch it off. Such an edit needs the same reviewer attention as a removal.

Local full runs keep using discovery (``scripts/dev_check.py --safe``); the shard
table only serves CI. Rebalance it from a per-test timing run when a shard
drifts well above the others.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
# Run as a script, sys.path[0] is scripts/; discovery under `python -m unittest`
# had the working directory (the repository root) there instead, which is what
# `from app ...` and `from tests import heap_probe` in the test modules rely on.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = ROOT / "tests"
TEST_MODULE_PATTERN = "test_*.py"
RESULT_PREFIX = "shard-"
RESULT_SUFFIX = ".json"
IDENTITY_SCHEMA = 2
INVENTORY_KIND = "inventory"
RECEIPT_KIND = "shard"
INVENTORY_PROFILE = "linux-full"
BINDING_FIELDS = ("python_version", "source_commit", "run_id", "partition_fingerprint")
OUTCOME_COUNTERS = ("failures", "errors", "skipped", "expected_failures", "unexpected_successes")
REMOVAL_APPROVAL_LABEL = "tests-removed-ok"
LISTED_IDS = 25

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
        "test_history_deferred_indexes",
        "test_history_diagnostics",
        "test_history_failure_log_fields",
        "test_history_fresh_deadline",
        "test_history_health_states",
        "test_history_released_schema_upgrades",
        "test_history_smart_metric_failure",
        "test_jbod_runner_trial",
        "test_mapping_store",
        "test_mapping_store_writer_lock",
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
        "test_ssh_connection_keep",
        "test_ssh_failure_contexts",
        "test_ssh_session_reuse",
        "test_storage_view_smart_batch",
        "test_system_backup",
        "test_system_setup_api_dialect",
        "test_system_setup_field_ownership",
        "test_system_setup_legacy_add",
        "test_truenas_ws",
    ),
    "2": (
        "test_admin_runtime_behavior_request_shape",
        "test_admin_runtime_routes",
        "test_admin_safety",
        "test_admin_upload_staging_event_loop",
        "test_background_refresh_busy_routes",
        "test_background_refresh_deadlines",
        "test_backup_integration",
        "test_ci_browser_legs",
        "test_ci_contract",
        "test_combined_shelf_upgrade",
        "test_compose_runtime_matrix",
        "test_config_example",
        "test_core_counter_markers",
        "test_demo_create_durability",
        "test_enclosure_option_labels",
        "test_full_backup_benchmark",
        "test_history_backend",
        "test_history_operation_bounds",
        "test_history_refresh_bounds",
        "test_image_upgrade_contract",
        "test_image_upgrade_smoke_safety",
        "test_immutable_deployment",
        "test_incomplete_refresh_status",
        "test_log_noise",
        "test_logging_config",
        "test_parsers",
        "test_public_demo_deployment",
        "test_public_demo_history_consistency",
        "test_release_status",
        "test_sas_cdb_malformed_tokens",
        "test_sas_decoder_byte_tokens",
        "test_sas_loginfo_width",
        "test_script_json",
        "test_script_platform_guards",
        "test_scripts_help",
        "test_segment_census",
        "test_segment_rotation",
        "test_segmented_history",
        "test_segmented_history_reader",
        "test_segmented_restore_recovery",
        "test_unittest_identities",
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
        "test_backup_retention_keeper_budget",
        "test_container_contract",
        "test_core_fabric_legacy_controller",
        "test_enclosure_aliases",
        "test_history_activation_marker_startup",
        "test_history_backend_bounds",
        "test_history_bulk_bounds",
        "test_history_runtime_damage_pause",
        "test_ipmi_duplicate_bays",
        "test_mapping_routes",
        "test_nonroot_migration",
        "test_persisted_smart_binding",
        "test_persisted_smart_refresh",
        "test_public_demo_deterministic",
        "test_public_demo_fixture",
        "test_quantastor_api",
        "test_quantastor_pool_device_scope",
        "test_quantastor_storage_view_claims",
        "test_read_ui_auth",
        "test_release_wrap_evidence_rows",
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
        "test_atomic_write",
        "test_bash_ci_contract",
        "test_disk_inventory_sync",
        "test_disk_inventory_sync_grants",
        "test_esxi_host_prep",
        "test_esxi_topology_alias_scope",
        "test_ghcr_release_contract",
        "test_history_routes",
        "test_history_schema_version_gate",
        "test_history_service",
        "test_inventory",
        "test_inventory_nested_slot_zero",
        "test_inventory_registry_routes",
        "test_nonroot_cli",
        "test_perf_budgets",
        "test_profiles",
        "test_public_doc_privacy",
        "test_public_docs_contract",
        "test_release_changelog_coverage",
        "test_restore_history_schema_gate",
        "test_route_contracts",
        "test_settings_reload",
        "test_slot_detail_store",
        "test_smart_grid_io",
        "test_smart_json_shapes",
        "test_smart_unread_consumers",
        "test_ssh_probe",
        "test_truenas_middleware_limits",
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


def partition_fingerprint(shards: dict[str, tuple[str, ...]] = SHARDS) -> str:
    """Stable digest of the shard table that receipts and inventories are bound to."""

    canonical = json.dumps({shard: list(modules) for shard, modules in shards.items()}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_commit(root: Path = ROOT) -> str:
    """The checked-out commit, or ``unknown`` outside a Git checkout."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def run_binding(
    root: Path = ROOT, shards: dict[str, tuple[str, ...]] = SHARDS
) -> dict[str, str]:
    """Fields every receipt and inventory of one CI run must share."""

    return {
        "python_version": python_version_label(),
        "source_commit": source_commit(root),
        "run_id": os.environ.get("GITHUB_RUN_ID") or "local",
        "partition_fingerprint": partition_fingerprint(shards),
    }


def result_path(results_dir: Path, shard_id: str, run_attempt: int | None = None) -> Path:
    attempt = "" if run_attempt is None else f"-attempt-{run_attempt}"
    return results_dir / f"{RESULT_PREFIX}{shard_id}{attempt}{RESULT_SUFFIX}"


def python_version_label() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _iter_cases(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def _loader_placeholder(case: unittest.TestCase) -> bool:
    """unittest's stand-ins for an import error or a module-level SkipTest."""

    return type(case).__module__ == "unittest.loader"


def build_suite_and_ids(
    modules: tuple[str, ...], tests_dir: Path = TESTS_DIR
) -> tuple[unittest.TestSuite, list[str]]:
    """Load modules in discovery form; return the suite and its collected identities.

    Identities are read before the run because ``TestSuite`` drops each test
    after running it.
    """

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module in modules:
        suite.addTests(loader.discover(str(tests_dir), pattern=f"{module}.py"))
    return suite, [case.id() for case in _iter_cases(suite)]


def build_suite(modules: tuple[str, ...], tests_dir: Path = TESTS_DIR) -> unittest.TestSuite:
    """Load ``modules`` exactly as ``unittest discover -s <tests_dir>`` would."""

    return build_suite_and_ids(modules, tests_dir)[0]


class DiscoveryError(ValueError):
    """Full discovery cannot produce a trustworthy expected identity set."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _first_line(text: str) -> str:
    lines = [line for line in str(text).strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else "unknown error"


def _listed(ids, limit: int = LISTED_IDS) -> str:
    ids = list(ids)
    shown = ", ".join(ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, and {len(ids) - limit} more"


def collect_inventory_ids(tests_dir: Path = TESTS_DIR) -> dict[str, list[str]]:
    """Discover every test identity under ``tests_dir``, independently of ``SHARDS``.

    Each module on disk is loaded alone so its identities can be attributed to
    it, and the union is cross-checked against one unfiltered discovery of the
    directory, so neither a nested test root nor a ``load_tests`` hook that
    depends on the pattern can silently change the expected set.
    """

    problems: list[str] = []
    modules = discovered_modules(tests_dir)
    if not modules:
        raise DiscoveryError([f"no {TEST_MODULE_PATTERN} modules under {tests_dir}"])
    loader = unittest.TestLoader()
    inventory: dict[str, list[str]] = {}
    for module in modules:
        errors_before = len(loader.errors)
        cases = list(_iter_cases(loader.discover(str(tests_dir), pattern=f"{module}.py")))
        for error in loader.errors[errors_before:]:
            problems.append(f"{module} failed discovery ({_first_line(error)})")
        if any(_loader_placeholder(case) for case in cases):
            if len(loader.errors) == errors_before:
                problems.append(f"{module} skipped itself at import; its real tests cannot be listed")
            cases = [case for case in cases if not _loader_placeholder(case)]
        ids = [case.id() for case in cases]
        if not ids and len(loader.errors) == errors_before:
            problems.append(f"{module} collected no tests")
        foreign = sorted({test_id for test_id in ids if test_id.split(".", 1)[0] != module})
        if foreign:
            problems.append(f"{module} collected tests owned by another module: {_listed(foreign)}")
        repeated = sorted(test_id for test_id, count in Counter(ids).items() if count > 1)
        if repeated:
            problems.append(f"{module} collected duplicate test identities: {_listed(repeated)}")
        inventory[module] = sorted(ids)

    union = Counter(test_id for ids in inventory.values() for test_id in ids)
    across = sorted(
        test_id for test_id, count in union.items()
        if count > 1 and sum(test_id in ids for ids in inventory.values()) > 1
    )
    if across:
        problems.append(f"test identities collected by more than one module: {_listed(across)}")

    # The unfiltered pass also reaches modules the per-module pass cannot (a
    # nested test package). A placeholder there is a module that skipped itself
    # or failed to import: report it by name instead of dropping it, or that
    # module would be neither listed nor run while every check passed.
    full_loader = unittest.TestLoader()
    full: Counter = Counter()
    placeholder_problems: list[str] = []
    for case in _iter_cases(full_loader.discover(str(tests_dir), pattern=TEST_MODULE_PATTERN)):
        if not _loader_placeholder(case):
            full[case.id()] += 1
            continue
        name = getattr(case, "_testMethodName", None) or case.id()
        if name in inventory:
            continue  # a top-level module, already reported above
        exception = getattr(case, "_exception", None)
        if exception is None:
            placeholder_problems.append(f"{name} skipped itself at import; its real tests cannot be listed")
        else:
            placeholder_problems.append(f"{name} failed discovery ({_first_line(str(exception))})")
    if full_loader.errors and not placeholder_problems and not problems:
        placeholder_problems.append(
            f"unfiltered discovery failed ({_first_line(full_loader.errors[0])})"
        )
    problems.extend(placeholder_problems)
    if full != union and not problems:
        only_full = sorted((full - union).elements())
        only_modules = sorted((union - full).elements())
        problems.append(
            "unfiltered discovery disagrees with per-module discovery "
            f"(only unfiltered: {_listed(only_full) or '-'}; "
            f"only per-module: {_listed(only_modules) or '-'})"
        )
    if problems:
        raise DiscoveryError(problems)
    return inventory


def build_inventory(
    tests_dir: Path = TESTS_DIR, *, binding: dict[str, str] | None = None
) -> dict[str, object]:
    """The independent expected identity set for one Python version and commit."""

    modules = collect_inventory_ids(tests_dir)
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT") or "local"
    return {
        "schema": IDENTITY_SCHEMA,
        "kind": INVENTORY_KIND,
        "profile": INVENTORY_PROFILE,
        **(binding if binding is not None else run_binding(tests_dir.parent)),
        "run_attempt": attempt,
        "python_full_version": platform.python_version(),
        "platform": sys.platform,
        "modules": modules,
        "ids": sorted(test_id for ids in modules.values() for test_id in ids),
    }


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _is_id_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def validate_inventory(payload: object) -> dict[str, object]:
    """Strictly validate an inventory; raise ``ValueError`` naming the first defect."""

    if not isinstance(payload, dict):
        raise ValueError("inventory is not an object")
    if payload.get("schema") != IDENTITY_SCHEMA or payload.get("kind") != INVENTORY_KIND:
        raise ValueError(f"expected schema {IDENTITY_SCHEMA} kind {INVENTORY_KIND!r}")
    if payload.get("profile") != INVENTORY_PROFILE:
        raise ValueError(f"expected profile {INVENTORY_PROFILE!r}")
    for field in BINDING_FIELDS:
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError(f"{field} must be a non-empty string")
    modules = payload.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ValueError("modules must be a non-empty object")
    if list(modules) != sorted(modules):
        raise ValueError("module names must be sorted")
    for module, ids in modules.items():
        if not module or not _is_id_list(ids) or not ids:
            raise ValueError(f"{module}: identities must be a non-empty list of strings")
        if ids != sorted(set(ids)):
            raise ValueError(f"{module}: identities must be sorted and unique")
        if any(test_id.split(".", 1)[0] != module for test_id in ids):
            raise ValueError(f"{module}: lists an identity owned by another module")
    every = [test_id for ids in modules.values() for test_id in ids]
    if len(every) != len(set(every)):
        raise ValueError("an identity is listed under more than one module")
    if payload.get("ids") != sorted(every):
        raise ValueError("ids must be the sorted union of the module identities")
    return payload


def load_inventory(path: Path) -> dict[str, object]:
    return validate_inventory(
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    )


class IdentityRecordingResult(unittest.TextTestResult):
    """Record the identity of every test the runner actually starts and stops.

    Sub-tests report through ``addSubTest`` and never start another test, so a
    test with many sub-tests still appears once.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started_ids: list[str] = []
        self.completed_ids: list[str] = []

    def startTest(self, test: unittest.TestCase) -> None:
        self.started_ids.append(test.id())
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        self.completed_ids.append(test.id())


def run_shard(
    shard_id: str,
    *,
    shards: dict[str, tuple[str, ...]] = SHARDS,
    tests_dir: Path = TESTS_DIR,
    results_dir: Path | None = None,
    stream=None,
    run_attempt: int | None = None,
    binding: dict[str, str] | None = None,
) -> int:
    if run_attempt is not None and (type(run_attempt) is not int or run_attempt <= 0):
        print("run attempt must be a positive integer", file=sys.stderr)
        return 2
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
        if run_attempt is not None and result_path(results_dir, shard_id, run_attempt).exists():
            print(f"shard {shard_id}: attempt {run_attempt} already has a result", file=sys.stderr)
            return 2
        # Created before the run so a coverage data file pointed here by
        # COVERAGE_FILE has somewhere to land even when the run aborts early.
        results_dir.mkdir(parents=True, exist_ok=True)
    # `python -m unittest` enables "default" warnings unless -W was given.
    warnings = None if sys.warnoptions else "default"
    runner = unittest.TextTestRunner(
        stream=stream, verbosity=2, warnings=warnings, resultclass=IdentityRecordingResult
    )
    suite, collected_ids = build_suite_and_ids(modules, tests_dir)
    result = runner.run(suite)

    if results_dir is not None:
        payload = {
            "schema": IDENTITY_SCHEMA,
            "kind": RECEIPT_KIND,
            **(binding if binding is not None else run_binding(tests_dir.parent, shards)),
            "shard": shard_id,
            "modules": list(modules),
            "collected_ids": collected_ids,
            "started_ids": result.started_ids,
            "completed_ids": result.completed_ids,
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "was_successful": result.wasSuccessful(),
        }
        if run_attempt is not None:
            payload["run_attempt"] = run_attempt
        # CI receipts are immutable, including if another writer raced the check.
        mode = "w" if run_attempt is None else "x"
        with result_path(results_dir, shard_id, run_attempt).open(mode, encoding="utf-8") as target:
            target.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if result.wasSuccessful() else 1


def _counter_problems(label: str, expected: Counter, actual: Counter) -> list[str]:
    problems: list[str] = []
    missing = sorted((expected - actual).elements())
    extra = sorted((actual - expected).elements())
    if missing:
        problems.append(f"{label} is missing {len(missing)}: {_listed(missing)}")
    if extra:
        problems.append(f"{label} has {len(extra)} unexpected or repeated: {_listed(extra)}")
    return problems


def _select_latest_receipts(results_dir: Path, problems: list[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    attempts: dict[str, int] = {}
    seen: set[tuple[str, int]] = set()
    # Keep artifact directories separate in CI; never let download order decide
    # which receipt survives. Unversioned receipts count as attempt one.
    for path in sorted(results_dir.rglob(f"{RESULT_PREFIX}*{RESULT_SUFFIX}")):
        match = re.fullmatch(r"shard-([1-9][0-9]*)(?:-attempt-([1-9][0-9]*))?\.json", path.name)
        if match is None:
            problems.append(f"invalid result filename: {path.name}")
            continue
        shard_id, attempt_label = match.groups()
        attempt = int(attempt_label) if attempt_label is not None else 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
        except (OSError, ValueError) as exc:
            problems.append(f"shard {shard_id}: unreadable result ({exc})")
            continue
        if not isinstance(payload, dict):
            problems.append(f"shard {shard_id}: result is not an object")
            continue
        recorded_attempt = payload.get("run_attempt", 1 if attempt_label is None else None)
        if type(recorded_attempt) is not int or recorded_attempt != attempt:
            problems.append(f"shard {shard_id}: invalid or mismatched run_attempt in {path.name}")
            continue
        if payload.get("shard") != shard_id:
            problems.append(f"shard {shard_id}: receipt shard does not match {path.name}")
            continue
        identity = (shard_id, attempt)
        if identity in seen:
            problems.append(f"shard {shard_id}: ambiguous duplicate result for attempt {attempt}")
            continue
        seen.add(identity)
        if attempt > attempts.get(shard_id, 0):
            attempts[shard_id] = attempt
            found[shard_id] = payload
    return found


def verify_results(
    results_dir: Path,
    expected_python_version: str,
    inventory: dict[str, object],
    *,
    shards: dict[str, tuple[str, ...]] = SHARDS,
    tests_dir: Path = TESTS_DIR,
) -> list[str]:
    """Problems that must make the required check fail; empty means every shard passed."""

    problems = partition_problems(shards, tests_dir)
    try:
        validate_inventory(inventory)
    except ValueError as exc:
        return problems + [f"inventory: invalid ({exc})"]
    if inventory["python_version"] != expected_python_version:
        problems.append(
            f"inventory: built on Python {inventory['python_version']!r}, "
            f"expected {expected_python_version!r}"
        )
    if inventory["partition_fingerprint"] != partition_fingerprint(shards):
        problems.append("inventory: built for a different shard table")
    inventory_modules: dict[str, list[str]] = inventory["modules"]  # type: ignore[assignment]
    on_disk = set(discovered_modules(tests_dir))
    if set(inventory_modules) != on_disk:
        problems.append(
            "inventory: module list differs from tests on disk "
            f"(missing: {_listed(sorted(on_disk - set(inventory_modules))) or '-'}; "
            f"extra: {_listed(sorted(set(inventory_modules) - on_disk)) or '-'})"
        )
    if not results_dir.is_dir():
        return problems + [f"results directory is missing: {results_dir}"]
    found = _select_latest_receipts(results_dir, problems)

    for shard_id in sorted(set(found) - set(shards)):
        problems.append(f"shard {shard_id}: result present but the shard is not in the table")
    executed: Counter = Counter()
    owners: dict[str, list[str]] = {}
    for shard_id, modules in shards.items():
        payload = found.get(shard_id)
        if payload is None:
            problems.append(f"shard {shard_id}: no result (job failed before writing, was skipped, or never ran)")
            continue
        if payload.get("schema") != IDENTITY_SCHEMA or payload.get("kind") != RECEIPT_KIND:
            problems.append(
                f"shard {shard_id}: receipt is not schema {IDENTITY_SCHEMA}; it cannot prove "
                "which tests ran, so rerun the whole workflow"
            )
            continue
        if payload.get("python_version") != expected_python_version:
            problems.append(
                f"shard {shard_id}: ran on Python {payload.get('python_version')!r}, "
                f"expected {expected_python_version!r}"
            )
        for field in ("source_commit", "run_id", "partition_fingerprint"):
            if payload.get(field) != inventory[field]:
                problems.append(
                    f"shard {shard_id}: {field} {payload.get(field)!r} does not match the "
                    f"inventory ({inventory[field]!r})"
                )
        if payload.get("modules") != list(modules):
            problems.append(f"shard {shard_id}: module list differs from the table")
        id_lists = {key: payload.get(key) for key in ("collected_ids", "started_ids", "completed_ids")}
        bad_lists = sorted(key for key, value in id_lists.items() if not _is_id_list(value))
        if bad_lists:
            problems.append(f"shard {shard_id}: {', '.join(bad_lists)} must be lists of identities")
        else:
            expected_ids = Counter(
                test_id for module in modules for test_id in inventory_modules.get(module, [])
            )
            collected = Counter(id_lists["collected_ids"])
            problems.extend(
                _counter_problems(f"shard {shard_id}: collected set", expected_ids, collected)
            )
            for key in ("started_ids", "completed_ids"):
                problems.extend(
                    _counter_problems(
                        f"shard {shard_id}: {key.split('_')[0]} set", collected, Counter(id_lists[key])
                    )
                )
            for test_id in id_lists["completed_ids"]:
                executed[test_id] += 1
                owners.setdefault(test_id, []).append(shard_id)
        tests_run = payload.get("tests_run")
        if type(tests_run) is not int or tests_run <= 0:
            problems.append(f"shard {shard_id}: ran no tests")
        elif _is_id_list(id_lists["started_ids"]) and tests_run != len(id_lists["started_ids"]):
            problems.append(f"shard {shard_id}: tests_run differs from the started identities")
        for counter in OUTCOME_COUNTERS:
            value = payload.get(counter)
            if type(value) is not int or value < 0:
                problems.append(f"shard {shard_id}: invalid {counter} count")
            elif counter not in {"skipped", "expected_failures"} and value != 0:
                problems.append(f"shard {shard_id}: nonzero {counter} count")
        if payload.get("was_successful") is not True:
            problems.append(
                f"shard {shard_id}: not successful "
                f"(failures={payload.get('failures')}, errors={payload.get('errors')}, "
                f"unexpected successes={payload.get('unexpected_successes')})"
            )

    expected_all = set(inventory["ids"])  # type: ignore[arg-type]
    missing = sorted(expected_all - set(executed))
    if missing:
        problems.append(f"test identities that no shard completed ({len(missing)}): {_listed(missing)}")
    repeated = sorted(test_id for test_id, count in executed.items() if count > 1)
    if repeated:
        detail = [f"{test_id} (shards {', '.join(owners[test_id])})" for test_id in repeated]
        problems.append(f"test identities completed more than once ({len(repeated)}): {_listed(detail)}")
    unexpected = sorted(set(executed) - expected_all)
    if unexpected:
        problems.append(
            f"test identities completed but not discovered ({len(unexpected)}): {_listed(unexpected)}"
        )
    return problems


def compare_inventories(
    base: dict[str, object], candidate: dict[str, object]
) -> tuple[list[str], list[str], list[str]]:
    """Return (removed, added, problems) between two validated inventories."""

    problems: list[str] = []
    if base["python_version"] != candidate["python_version"]:
        problems.append(
            f"base inventory is for Python {base['python_version']!r}, "
            f"candidate for {candidate['python_version']!r}"
        )
    base_ids = set(base["ids"])  # type: ignore[arg-type]
    candidate_ids = set(candidate["ids"])  # type: ignore[arg-type]
    return sorted(base_ids - candidate_ids), sorted(candidate_ids - base_ids), problems


def _isolate_import_path(root: Path) -> None:
    """Import tests of ``root`` only, never this checkout's ``app`` or ``scripts``."""

    own = {str(ROOT), str(SCRIPTS_DIR), ""}
    sys.path[:] = [entry for entry in sys.path if entry not in own]
    sys.path.insert(0, str(root))


def _discover_command(root: Path, output: Path) -> int:
    root = root.resolve()
    if root != ROOT:
        _isolate_import_path(root)
    try:
        inventory = build_inventory(root / "tests", binding=run_binding(root))
    except DiscoveryError as exc:
        for problem in exc.problems:
            print(f"test discovery: {problem}", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as target:
        target.write(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    print(
        f"test discovery: {len(inventory['ids'])} tests in {len(inventory['modules'])} modules "
        f"at {inventory['source_commit']} on Python {inventory['python_full_version']}"
    )
    return 0


def _compare_command(base_path: Path, candidate_path: Path, approved: bool, summary: Path | None) -> int:
    try:
        base = load_inventory(base_path)
        candidate = load_inventory(candidate_path)
    except (OSError, ValueError) as exc:
        print(f"test removal check: cannot read inventories ({exc})", file=sys.stderr)
        return 2
    removed, added, problems = compare_inventories(base, candidate)
    for problem in problems:
        print(f"test removal check: {problem}", file=sys.stderr)
    if problems:
        return 2
    lines = [
        f"Test identities: {len(base['ids'])} at base {base['source_commit']}, "
        f"{len(candidate['ids'])} at candidate {candidate['source_commit']} "
        f"(Python {candidate['python_version']}).",
        f"Added: {len(added)}. Removed or renamed: {len(removed)}.",
    ]
    lines.extend(f"  removed: {test_id}" for test_id in removed)
    lines.extend(f"  added: {test_id}" for test_id in added)
    print("\n".join(lines))
    if summary is not None:
        with summary.open("a", encoding="utf-8") as target:
            target.write("\n".join(["### Test identities", "", *lines, ""]) + "\n")
    if removed and not approved:
        print(
            f"test removal check: {len(removed)} test(s) present at the base are gone. If that is "
            f"intended, a maintainer adds the `{REMOVAL_APPROVAL_LABEL}` label and reruns this job.",
            file=sys.stderr,
        )
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run or verify one CI shard of the Python unittest suite.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one shard's test modules")
    run.add_argument("--shard", required=True, choices=sorted(SHARDS), help="shard id from the SHARDS table")
    run.add_argument(
        "--run-attempt", type=int, default=os.environ.get("SHARD_RUN_ATTEMPT"),
        help="positive CI run attempt (default: SHARD_RUN_ATTEMPT); writes an immutable receipt",
    )
    run.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="directory that receives shard outcome JSON (attempt-scoped in CI; omit to skip)",
    )

    discover = commands.add_parser("discover", help="write the full discovered test-identity inventory")
    discover.add_argument("--output", type=Path, required=True, help="new JSON file to create")
    discover.add_argument(
        "--root", type=Path, default=ROOT,
        help="checkout whose tests/ to discover (default: this one); another root is imported in isolation",
    )

    verify = commands.add_parser("verify", help="require every discovered test to have run exactly once")
    verify.add_argument("--results-dir", type=Path, required=True, help="directory holding shard outcome JSON recursively")
    verify.add_argument(
        "--python-version",
        required=True,
        help="major.minor every result must have run on, for example 3.14",
    )
    verify.add_argument("--inventory", type=Path, required=True, help="inventory written by `discover`")

    compare = commands.add_parser("compare", help="list tests removed or added since a base inventory")
    compare.add_argument("--base", type=Path, required=True, help="inventory of the base commit")
    compare.add_argument("--candidate", type=Path, required=True, help="inventory of the candidate commit")
    compare.add_argument(
        "--removals-approved", action="store_true",
        help=f"report removals without failing (CI: the `{REMOVAL_APPROVAL_LABEL}` label)",
    )
    compare.add_argument("--summary", type=Path, default=None, help="append a Markdown summary here")

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
    if args.command == "run":
        return run_shard(args.shard, results_dir=args.results_dir, run_attempt=args.run_attempt)
    if args.command == "discover":
        return _discover_command(args.root, args.output)
    if args.command == "compare":
        return _compare_command(args.base, args.candidate, args.removals_approved, args.summary)
    try:
        inventory = load_inventory(args.inventory)
    except (OSError, ValueError) as exc:
        print(f"shard verify: cannot read inventory {args.inventory} ({exc})", file=sys.stderr)
        return 1
    problems = verify_results(args.results_dir, args.python_version, inventory)
    if problems:
        for problem in problems:
            print(f"shard verify: {problem}", file=sys.stderr)
        return 1
    print(
        f"shard verify: every shard passed on Python {args.python_version} ({', '.join(SHARDS)}); "
        f"each of the {len(inventory['ids'])} discovered tests completed exactly once"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
