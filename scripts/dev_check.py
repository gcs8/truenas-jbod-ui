#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import os
import re
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
    "scripts/capture_public_demo_screenshots.js",
)
CI_SOURCE_GATES = frozenset(
    {
        "bounded-ruff",
        "diff-hygiene",
        "javascript-syntax",
        "javascript-unit-tests",
        "performance-baseline",
        "prometheus-rules",
        "python-compileall",
        "python-unittest",
    }
)
CI_SOURCE_GATE_STEPS: Mapping[str, str] = {
    "bounded-ruff": "Run bounded Ruff rules",
    "diff-hygiene": "Check patch whitespace",
    "javascript-syntax": "Check JavaScript syntax",
    "javascript-unit-tests": "Run deterministic JavaScript unit tests",
    "performance-baseline": "Run deterministic unittest suite",
    "prometheus-rules": "Validate starter Prometheus alert rules",
    "python-compileall": "Compile Python source",
    "python-unittest": "Run deterministic unittest suite",
}
CI_SOURCE_GATE_WORKFLOW_COMMANDS: Mapping[str, str] = {
    "bounded-ruff": (
        "ruff check app admin_service history_service scripts tests --select E4,E7,E9,F"
    ),
    "diff-hygiene": "\n".join(
        (
            "set -euo pipefail",
            'if [ "${GITHUB_EVENT_NAME}" = "pull_request" ] && [ -n "${PR_BASE_SHA:-}" ]; then',
            'git diff --check "${PR_BASE_SHA}...HEAD"',
            'elif [ -n "${BEFORE_SHA:-}" ] && [ "${BEFORE_SHA}" != '
            '"0000000000000000000000000000000000000000" ] && git cat-file -e '
            '"${BEFORE_SHA}^{commit}" 2>/dev/null; then',
            'git diff --check "${BEFORE_SHA}..HEAD"',
            "elif git rev-parse --verify HEAD^ >/dev/null 2>&1; then",
            'git diff --check "HEAD^..HEAD"',
            "else",
            "git diff --check",
            "fi",
        )
    ),
    "javascript-syntax": "\n".join(
        (
            "set -euo pipefail",
            "shopt -s nullglob",
            "node --check app/static/app.js",
            "node --check app/static/sas_fabric_view.js",
            "node --check admin_service/static/admin.js",
            "node --check history_service/static/dashboard.js",
            "node --check scripts/capture_public_demo_screenshots.js",
            "specs=(qa/*.spec.js)",
            "if [ ${#specs[@]} -eq 0 ]; then",
            'echo "No QA spec files found under qa/*.spec.js"',
            "exit 1",
            "fi",
            'for spec in "${specs[@]}"; do',
            'node --check "$spec"',
            "done",
        )
    ),
    "javascript-unit-tests": "npm run test:unit",
    "performance-baseline": 'python -m unittest discover -s tests -p "test_*.py" -v',
    "prometheus-rules": "\n".join(
        (
            "promtool check rules prometheus/rules/truenas-jbod-ui-alerts-v1.yml",
            "python -m unittest tests.test_prometheus_alert_rules -v",
        )
    ),
    "python-compileall": (
        "python -m compileall app admin_service history_service scripts tests"
    ),
    "python-unittest": 'python -m unittest discover -s tests -p "test_*.py" -v',
}
CI_SOURCE_GATE_MARKER = re.compile(
    r"^(?P<indent> *)# dev-check-source-gate: (?P<gate>[a-z0-9-]+)\s*$",
    re.MULTILINE,
)
WINDOWS_PORTABLE_TEST_MODULES = (
    "tests.test_admin_command_state",
    "tests.test_admin_config",
    "tests.test_admin_maintenance",
    "tests.test_admin_secret_models",
    "tests.test_changelog_entry_gate",
    "tests.test_ci_contract",
    "tests.test_config_example",
    "tests.test_dev_check",
    "tests.test_ghcr_release_contract",
    "tests.test_history_backend",
    "tests.test_history_backend_bounds",
    "tests.test_history_config_contract",
    "tests.test_history_operation_bounds",
    "tests.test_logging_config",
    "tests.test_parsers",
    "tests.test_profile_builder",
    "tests.test_profiles",
    "tests.test_prometheus_alert_rules",
    "tests.test_public_doc_privacy",
    "tests.test_public_demo_deployment",
    "tests.test_public_demo_deterministic",
    "tests.test_public_demo_fixture",
    "tests.test_public_demo_history_consistency",
    "tests.test_public_demo_provenance",
    "tests.test_public_docs_contract",
    "tests.test_public_screenshots",
    "tests.test_quantastor_api",
    "tests.test_release_changelog_coverage",
    "tests.test_release_status",
    "tests.test_release_wrap_validator",
    "tests.test_ssh_probe",
    "tests.test_tls_trust",
    "tests.test_truenas_ws",
    "tests.test_wiki_drift_verifier",
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
            "tests.test_disk_inventory_sync",
            "tests.test_disk_inventory_sync_grants",
            "tests.test_enclosure_aliases",
            "tests.test_enclosure_option_labels",
            "tests.test_history_routes",
            "tests.test_app_history_body_bound",
            "tests.test_app_history_bounds",
            "tests.test_history_bulk_bounds",
            "tests.test_history_refresh_bounds",
            "tests.test_history_service",
            "tests.test_inventory",
            "tests.test_inventory_registry_routes",
            "tests.test_mapping_routes",
            "tests.test_metrics",
            "tests.test_perf",
            "tests.test_perf_budgets",
            "tests.test_platform_parity_fixtures",
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
            "tests.test_esxi_host_prep",
            "tests.test_immutable_deployment",
            "tests.test_mapping_store",
            "tests.test_nonroot_migration",
            "tests.test_perf_harness",
            "tests.test_private_qa_restore",
            "tests.test_process_secrets",
        ),
    ),
    WindowsExclusion(
        category="Bash syntax tooling",
        reason=(
            "this suite invokes bash -n and Windows may expose only the WSL launcher "
            "without an installed Bash runtime"
        ),
        modules=("tests.test_bash_ci_contract",),
    ),
)


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    missing_tool: str | None = None
    ci_gate: str | None = None


@dataclass(frozen=True)
class Skip:
    name: str
    reason: str
    ci_gate: str | None = None


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
    ci_gate: str | None = None,
) -> Check:
    resolved = _resolve_tool(tool, find_executable, platform)
    if resolved is None:
        return Check(name, (tool, *args), missing_tool=tool, ci_gate=ci_gate)
    return Check(name, (resolved, *args), ci_gate=ci_gate)


def _normalize_ci_source_gate_command(lines: Sequence[str]) -> str:
    return "\n".join(line.strip() for line in lines if line.strip())


def _ci_source_gate_marker_step(
    lines: Sequence[str],
    marker_index: int,
    indent: str,
) -> tuple[str, str] | None:
    for line in reversed(lines[:marker_index]):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_indent = line[: len(line) - len(line.lstrip(" "))]
        if len(line_indent) < len(indent):
            if stripped != "steps:":
                return None
            break
    else:
        return None

    step_index = marker_index + 1
    while step_index < len(lines):
        next_marker = CI_SOURCE_GATE_MARKER.fullmatch(lines[step_index])
        if next_marker is None or next_marker.group("indent") != indent:
            break
        step_index += 1
    if step_index >= len(lines):
        return None

    step_line = lines[step_index]
    step_name_match = re.match(
        rf"^{re.escape(indent)}- name:\s*(?P<name>.+?)\s*$",
        step_line,
    )
    if step_name_match is None:
        return None

    run_prefix = f"{indent}  run"
    for run_index, line in enumerate(lines[step_index + 1 :], start=step_index + 1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            line_indent = line[: len(line) - len(line.lstrip(" "))]
            if len(line_indent) <= len(indent):
                break
        run_match = re.match(
            rf"^{re.escape(indent)}  run\s*:\s*(?P<command>.*?)\s*$",
            line,
        )
        if not line.startswith(run_prefix) or run_match is None:
            continue

        command = run_match.group("command")
        if command in {"|", "|-"}:
            block_lines = []
            run_indent = len(indent) + 2
            for block_line in lines[run_index + 1 :]:
                block_stripped = block_line.strip()
                block_indent = len(block_line) - len(block_line.lstrip(" "))
                if block_stripped and block_indent <= run_indent:
                    break
                block_lines.append(block_line)
            command = _normalize_ci_source_gate_command(block_lines)
        elif command.startswith(">") or not command:
            return None

        return step_name_match.group("name"), command
    return None


def _read_ci_source_gate_matches(root: Path) -> tuple[str, tuple[re.Match[str], ...]]:
    workflow_path = root / ".github" / "workflows" / "ci.yml"
    try:
        workflow = workflow_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"Cannot read CI source gate contract: {workflow_path}") from exc
    matches = tuple(CI_SOURCE_GATE_MARKER.finditer(workflow))
    if not matches:
        raise PlanError("CI source gate contract is missing")
    duplicate_gates = sorted(
        gate
        for gate, count in Counter(match.group("gate") for match in matches).items()
        if count > 1
    )
    if duplicate_gates:
        raise PlanError(f"CI source gate marker is duplicated: {','.join(duplicate_gates)}")
    return workflow, matches


def read_ci_source_gate_contract(root: Path) -> frozenset[str]:
    _, matches = _read_ci_source_gate_matches(root)
    return frozenset(match.group("gate") for match in matches)


def validate_ci_source_gate_contract(root: Path) -> None:
    workflow, matches = _read_ci_source_gate_matches(root)
    declared = frozenset(match.group("gate") for match in matches)
    if declared != CI_SOURCE_GATES:
        added = sorted(declared - CI_SOURCE_GATES)
        removed = sorted(CI_SOURCE_GATES - declared)
        raise PlanError(
            "CI source gate contract drift "
            f"(added={','.join(added) or '-'}; removed={','.join(removed) or '-'})"
        )

    if (
        frozenset(CI_SOURCE_GATE_STEPS) != CI_SOURCE_GATES
        or frozenset(CI_SOURCE_GATE_WORKFLOW_COMMANDS) != CI_SOURCE_GATES
    ):
        raise PlanError("CI source gate validator contract drift")

    lines = workflow.splitlines()
    for match in matches:
        marker_index = workflow.count("\n", 0, match.start())
        step = _ci_source_gate_marker_step(
            lines,
            marker_index,
            match.group("indent"),
        )
        if step is None:
            raise PlanError(
                "CI source gate marker is not attached to an executable workflow step: "
                f"{match.group('gate')}"
            )
        step_name, command = step
        gate = match.group("gate")
        expected_step_name = CI_SOURCE_GATE_STEPS[gate]
        if step_name != expected_step_name:
            raise PlanError(
                "CI source gate marker guards the wrong workflow step: "
                f"{gate}"
            )
        if command != CI_SOURCE_GATE_WORKFLOW_COMMANDS[gate]:
            raise PlanError(f"CI source gate command drift: {gate}")


def planned_ci_source_gates(plan: Plan) -> frozenset[str]:
    return frozenset(
        gate
        for item in (*plan.checks, *plan.skips)
        if (gate := item.ci_gate) is not None
    )


def _planned_ci_source_gate_commands(
    plan: Plan,
) -> Mapping[str, tuple[tuple[str, ...], ...]]:
    commands: dict[str, list[tuple[str, ...]]] = {}
    for check in plan.checks:
        if check.ci_gate is not None:
            commands.setdefault(check.ci_gate, []).append(check.argv[1:])
    for skip in plan.skips:
        if skip.ci_gate is not None:
            commands.setdefault(skip.ci_gate, []).append(("<skip>",))
    return {gate: tuple(sorted(argvs)) for gate, argvs in commands.items()}


def validate_planned_ci_source_gate_commands(
    plan: Plan,
    *,
    root: Path,
    platform: str,
) -> None:
    python_unittest = (
        ("-m", "unittest", "-v", *WINDOWS_PORTABLE_TEST_MODULES)
        if platform.startswith("win")
        else ("-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v")
    )
    expected: dict[str, tuple[tuple[str, ...], ...]] = {
        "bounded-ruff": (
            (
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
        "diff-hygiene": (("diff", "--check"),),
        "javascript-syntax": tuple(
            sorted(("--check", path) for path in (*FIXED_JAVASCRIPT_PATHS, *_qa_spec_paths(root)))
        ),
        "javascript-unit-tests": (("run", "test:unit"),),
        "performance-baseline": (("scripts/build_perf_baseline.py", "--check"),),
        "prometheus-rules": (
            ("check", "rules", "prometheus/rules/truenas-jbod-ui-alerts-v1.yml"),
        ),
        "python-compileall": (
            ("-m", "compileall", "app", "admin_service", "history_service", "scripts", "tests"),
        ),
        "python-unittest": (python_unittest,),
    }
    actual = _planned_ci_source_gate_commands(plan)
    for gate in sorted(CI_SOURCE_GATES):
        commands = actual.get(gate)
        allowed = (expected[gate],)
        if gate == "prometheus-rules":
            allowed += ((("<skip>",),),)
        if commands not in allowed:
            raise PlanError(f"Local source gate command drift: {gate}")


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
    validate_ci_source_gate_contract(root)

    if find_executable is None:
        find_executable = shutil.which

    skips: list[Skip] = []
    if platform.startswith("win"):
        python_check, windows_skips = _windows_test_check(root, python_executable)
        python_check = Check(
            python_check.name,
            python_check.argv,
            missing_tool=python_check.missing_tool,
            ci_gate="python-unittest",
        )
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
            ci_gate="python-unittest",
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
            ci_gate="python-compileall",
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
            ci_gate="bounded-ruff",
        ),
    ]
    checks.extend(
        _tool_check(
            f"JavaScript syntax: {path}",
            "node",
            ("--check", path),
            find_executable=find_executable,
            platform=platform,
            ci_gate="javascript-syntax",
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
                ci_gate="diff-hygiene",
            ),
            _tool_check(
                "JavaScript unit tests",
                "npm",
                ("run", "test:unit"),
                find_executable=find_executable,
                platform=platform,
                ci_gate="javascript-unit-tests",
            ),
            Check(
                "Performance baseline",
                (python_executable, "scripts/build_perf_baseline.py", "--check"),
                ci_gate="performance-baseline",
            ),
        )
    )
    if mode == "full":
        checks.append(
            Check(
                "Checked-in public demo artifact",
                (python_executable, "scripts/check_public_demo_artifact.py", "public-demo"),
            )
        )

    requested_promtool = environment.get("PROMTOOL_BINARY", "promtool")
    promtool = _resolve_tool(requested_promtool, find_executable, platform)
    if promtool is None:
        skips.append(
            Skip(
                "Prometheus alert rules",
                "promtool is not available; install it or set PROMTOOL_BINARY to run this gate",
                ci_gate="prometheus-rules",
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
                ci_gate="prometheus-rules",
            )
        )

    plan = Plan(tuple(checks), tuple(skips))
    if planned_ci_source_gates(plan) != CI_SOURCE_GATES:
        raise PlanError("planned source gates do not exactly match the CI source gate contract")
    validate_planned_ci_source_gate_commands(
        plan,
        root=root,
        platform=platform,
    )
    return plan


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
