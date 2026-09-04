#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


ROOT = Path(__file__).resolve().parents[1]
FIXED_JAVASCRIPT_PATHS = (
    "app/static/app.js",
    "app/static/sas_fabric_view.js",
    "admin_service/static/admin.js",
    "history_service/static/dashboard.js",
)
WINDOWS_PORTABLE_TEST_MODULES = (
    "tests.test_admin_command_state",
    "tests.test_admin_config",
    "tests.test_admin_maintenance",
    "tests.test_admin_secret_models",
    "tests.test_ci_contract",
    "tests.test_dev_check",
    "tests.test_esxi_host_prep",
    "tests.test_ghcr_release_contract",
    "tests.test_history_backend",
    "tests.test_logging_config",
    "tests.test_mapping_store",
    "tests.test_parsers",
    "tests.test_profile_builder",
    "tests.test_profiles",
    "tests.test_prometheus_alert_rules",
    "tests.test_public_doc_privacy",
    "tests.test_quantastor_api",
    "tests.test_release_status",
    "tests.test_release_wrap_validator",
    "tests.test_ssh_probe",
    "tests.test_tls_trust",
    "tests.test_truenas_ws",
)


@dataclass(frozen=True)
class WindowsExclusion:
    category: str
    reason: str
    modules: tuple[str, ...]


WINDOWS_EXCLUSIONS = (
    WindowsExclusion(
        category="fcntl-dependent history/backup import graph",
        reason=(
            "history_service.scheduled_backup imports fcntl and its transitive history, "
            "backup, app, and route suites require POSIX locking"
        ),
        modules=(
            "tests.test_admin_auth",
            "tests.test_admin_runtime_routes",
            "tests.test_admin_service",
            "tests.test_admin_ttl",
            "tests.test_enclosure_aliases",
            "tests.test_enclosure_option_labels",
            "tests.test_history_routes",
            "tests.test_history_service",
            "tests.test_inventory",
            "tests.test_mapping_routes",
            "tests.test_metrics",
            "tests.test_perf",
            "tests.test_perf_budgets",
            "tests.test_platform_parity_fixtures",
            "tests.test_public_demo_fixture",
            "tests.test_read_ui_auth",
            "tests.test_route_contracts",
            "tests.test_sas_fabric",
            "tests.test_scheduled_backup",
            "tests.test_script_json",
            "tests.test_segment_migration",
            "tests.test_segment_rotation",
            "tests.test_segment_sealer",
            "tests.test_segmented_history",
            "tests.test_segmented_history_reader",
            "tests.test_segmented_restore_recovery",
            "tests.test_slot_bounds_routes",
            "tests.test_slot_detail_store",
            "tests.test_snapshot_export",
            "tests.test_system_backup",
        ),
    ),
    WindowsExclusion(
        category="POSIX filesystem and identity semantics",
        reason=(
            "these suites assert POSIX ownership, permission bits, links, file descriptors, "
            "or process identity that Windows does not implement equivalently"
        ),
        modules=(
            "tests.test_account_bootstrap",
            "tests.test_compose_runtime_matrix",
            "tests.test_container_contract",
            "tests.test_immutable_deployment",
            "tests.test_nonroot_migration",
            "tests.test_perf_harness",
            "tests.test_private_qa_restore",
            "tests.test_process_secrets",
        ),
    ),
)


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    missing_tool: str | None = None


@dataclass(frozen=True)
class Skip:
    name: str
    reason: str


@dataclass(frozen=True)
class Plan:
    checks: tuple[Check, ...]
    skips: tuple[Skip, ...] = ()


class PlanError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[object]]
ExecutableFinder = Callable[[str], str | None]


def _tracked_test_modules(root: Path) -> set[str]:
    return {f"tests.{path.stem}" for path in (root / "tests").glob("test_*.py")}


def _windows_test_check(root: Path, python_executable: str) -> tuple[Check, tuple[Skip, ...]]:
    discovered = _tracked_test_modules(root)
    portable = set(WINDOWS_PORTABLE_TEST_MODULES)
    excluded = {
        module
        for exclusion in WINDOWS_EXCLUSIONS
        for module in exclusion.modules
    }
    classified = portable | excluded
    if discovered != classified or portable & excluded:
        unclassified = sorted(discovered - classified)
        missing = sorted(classified - discovered)
        overlap = sorted(portable & excluded)
        details = []
        if unclassified:
            details.append(f"unclassified={','.join(unclassified)}")
        if missing:
            details.append(f"missing={','.join(missing)}")
        if overlap:
            details.append(f"overlap={','.join(overlap)}")
        raise PlanError(f"Windows test classification is stale ({'; '.join(details)})")

    check = Check(
        "Python unittest (Windows portable suite)",
        (python_executable, "-m", "unittest", "-v", *WINDOWS_PORTABLE_TEST_MODULES),
    )
    skips = tuple(
        Skip(
            f"Windows exclusion: {exclusion.category}",
            f"{exclusion.reason}; excluded suites: {', '.join(exclusion.modules)}",
        )
        for exclusion in WINDOWS_EXCLUSIONS
    )
    return check, skips


def _resolve_tool(tool: str, find_executable: ExecutableFinder, platform: str) -> str | None:
    """Return the dispatchable path for ``tool``, or ``None`` when it is not installed.

    ``subprocess.run(..., shell=False)`` on Windows only appends ``.exe`` when it resolves a
    bare command name, so ``npm`` (shipped as ``npm.cmd``) raises ``FileNotFoundError``.
    Resolving through ``shutil.which`` first, with an explicit ``.cmd`` fallback for the
    Node tool shims, keeps the gate dispatchable on both platforms.
    """
    resolved = find_executable(tool)
    if resolved is None and platform.startswith("win"):
        resolved = find_executable(f"{tool}.cmd")
    return resolved


def _tool_check(
    name: str,
    tool: str,
    args: tuple[str, ...],
    *,
    find_executable: ExecutableFinder,
    platform: str,
) -> Check:
    resolved = _resolve_tool(tool, find_executable, platform)
    if resolved is None:
        return Check(name, (tool, *args), missing_tool=tool)
    return Check(name, (resolved, *args))


def _qa_spec_paths(root: Path) -> tuple[str, ...]:
    paths = tuple(
        path.relative_to(root).as_posix()
        for path in sorted((root / "qa").glob("*.spec.js"))
    )
    if not paths:
        raise PlanError("No QA spec files found under qa/*.spec.js")
    return paths


def build_plan(
    mode: str,
    *,
    platform: str = sys.platform,
    root: Path = ROOT,
    python_executable: str = sys.executable,
    environment: Mapping[str, str] = os.environ,
    find_executable: ExecutableFinder | None = None,
) -> Plan:
    if mode not in {"safe", "full"}:
        raise PlanError(f"Unsupported validation mode: {mode}")

    if find_executable is None:
        find_executable = shutil.which

    skips: list[Skip] = []
    if platform.startswith("win"):
        python_check, windows_skips = _windows_test_check(root, python_executable)
        skips.extend(windows_skips)
    else:
        python_check = Check(
            "Python unittest (full discovery)",
            (
                python_executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_*.py",
                "-v",
            ),
        )

    checks = [
        python_check,
        Check(
            "Python compileall",
            (
                python_executable,
                "-m",
                "compileall",
                "app",
                "admin_service",
                "history_service",
                "scripts",
                "tests",
            ),
        ),
        Check(
            "Bounded Ruff",
            (
                python_executable,
                "-m",
                "ruff",
                "check",
                "app",
                "admin_service",
                "history_service",
                "scripts",
                "tests",
                "--select",
                "E4,E7,E9,F",
            ),
        ),
    ]
    checks.extend(
        _tool_check(
            f"JavaScript syntax: {path}",
            "node",
            ("--check", path),
            find_executable=find_executable,
            platform=platform,
        )
        for path in (*FIXED_JAVASCRIPT_PATHS, *_qa_spec_paths(root))
    )
    checks.extend(
        (
            _tool_check(
                "Git diff hygiene",
                "git",
                ("diff", "--check"),
                find_executable=find_executable,
                platform=platform,
            ),
            _tool_check(
                "JavaScript unit tests",
                "npm",
                ("run", "test:unit"),
                find_executable=find_executable,
                platform=platform,
            ),
            Check(
                "Performance baseline",
                (python_executable, "scripts/build_perf_baseline.py", "--check"),
            ),
        )
    )

    requested_promtool = environment.get("PROMTOOL_BINARY", "promtool")
    promtool = _resolve_tool(requested_promtool, find_executable, platform)
    if promtool is None:
        skips.append(
            Skip(
                "Prometheus alert rules",
                "promtool is not available; install it or set PROMTOOL_BINARY to run this gate",
            )
        )
    else:
        checks.append(
            Check(
                "Prometheus alert rules",
                (
                    promtool,
                    "check",
                    "rules",
                    "prometheus/rules/truenas-jbod-ui-alerts-v1.yml",
                ),
            )
        )

    return Plan(tuple(checks), tuple(skips))


def run_plan(
    plan: Plan,
    *,
    root: Path = ROOT,
    runner: Runner = subprocess.run,
    output: TextIO = sys.stdout,
) -> int:
    results: list[tuple[str, str]] = []
    for check in plan.checks:
        if check.missing_tool is not None:
            print(f"MISS  {check.name}: {check.missing_tool} was not found", file=output, flush=True)
            results.append(
                (
                    check.name,
                    f"FAIL  {check.name}: tool not found: {check.missing_tool}; "
                    "install it or add it to PATH",
                )
            )
            continue
        print(f"RUN   {check.name}: {' '.join(check.argv)}", file=output, flush=True)
        try:
            completed = runner(check.argv, cwd=root, check=False, shell=False)
        except OSError as exc:
            results.append((check.name, f"FAIL  {check.name}: {type(exc).__name__}: {exc}"))
        else:
            if completed.returncode == 0:
                results.append((check.name, f"PASS  {check.name}"))
            else:
                results.append((check.name, f"FAIL  {check.name} (exit {completed.returncode})"))

    print("\nValidation summary", file=output)
    print("------------------", file=output)
    for _name, result in results:
        print(result, file=output)
    for skip in plan.skips:
        print(f"SKIP  {skip.name}: {skip.reason}", file=output)

    failed = any(result.startswith("FAIL") for _name, result in results)
    print(f"FINAL: {'FAIL' if failed else 'PASS'}", file=output)
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the authoritative platform-aware source validation gates."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--safe", dest="mode", action="store_const", const="safe")
    mode.add_argument("--full", dest="mode", action="store_const", const="full")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = build_plan(args.mode)
    except PlanError as exc:
        print("Validation summary")
        print("------------------")
        print(f"FAIL  validation plan: {exc}")
        print("FINAL: FAIL")
        return 1
    return run_plan(plan)


if __name__ == "__main__":
    raise SystemExit(main())
