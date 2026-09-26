from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PR_LABEL_WORKFLOW = ROOT / ".github" / "workflows" / "pr-labels.yml"


class BashCIContractTests(unittest.TestCase):
    def run_ci_routing_script(self, gh_responses: list[str]) -> tuple[subprocess.CompletedProcess[bytes], str, int, list[str]]:
        workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
        script = workflow["jobs"]["route"]["steps"][0]["run"]

        with tempfile.TemporaryDirectory() as raw_temp_dir:
            temp_dir = Path(raw_temp_dir)
            bin_dir = temp_dir / "bin"
            bin_dir.mkdir()
            output_path = temp_dir / "github-output"
            gh_state_path = temp_dir / "gh-state"
            sleep_calls_path = temp_dir / "sleep-calls"

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["FAKE_GH_STATE"])
call_index = int(state_path.read_text(encoding="utf-8")) if state_path.exists() else 0
state_path.write_text(str(call_index + 1), encoding="utf-8")
responses = json.loads(os.environ["FAKE_GH_RESPONSES"])
response = responses[call_index]
if response == "error":
    sys.exit(1)
print(response)
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_sleep = bin_dir / "sleep"
            fake_sleep.write_text(
                """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

with Path(os.environ["FAKE_SLEEP_CALLS"]).open("a", encoding="utf-8") as handle:
    handle.write(sys.argv[1] + "\\n")
""",
                encoding="utf-8",
            )
            fake_sleep.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "BRANCH_NAME": "fix/635-ci-routing-race",
                    "EVENT_NAME": "push",
                    "FAKE_GH_RESPONSES": json.dumps(gh_responses),
                    "FAKE_GH_STATE": str(gh_state_path),
                    "FAKE_SLEEP_CALLS": str(sleep_calls_path),
                    "GH_REPO": "example/project",
                    "GITHUB_OUTPUT": str(output_path),
                    "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
                }
            )
            result = subprocess.run(
                ["bash"],
                cwd=ROOT,
                env=env,
                input=script.encode("utf-8"),
                capture_output=True,
                check=False,
            )
            output = output_path.read_text(encoding="utf-8")
            gh_calls = int(gh_state_path.read_text(encoding="utf-8"))
            sleep_calls = (
                sleep_calls_path.read_text(encoding="utf-8").splitlines()
                if sleep_calls_path.exists()
                else []
            )
            return result, output, gh_calls, sleep_calls

    def test_pr_label_workflow_uses_a_bash_safe_conventional_title_pattern(self) -> None:
        workflow = yaml.safe_load(PR_LABEL_WORKFLOW.read_text(encoding="utf-8"))
        script = workflow["jobs"]["label"]["steps"][0]["run"]

        self.assertIn("conventional_pattern=", script)
        self.assertIn('[[ "$title" =~ $conventional_pattern ]]', script)
        subprocess.run(["bash", "-n"], input=script.encode("utf-8"), check=True)

    def test_push_routing_rechecks_zero_and_defers_if_pull_request_appears(self) -> None:
        result, output, gh_calls, sleep_calls = self.run_ci_routing_script(["0", "1"])

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(output, "run=false\n")
        self.assertEqual(gh_calls, 2)
        self.assertEqual(sleep_calls, ["15"])

    def test_push_routing_rechecks_zero_then_runs_if_still_zero(self) -> None:
        result, output, gh_calls, sleep_calls = self.run_ci_routing_script(["0", "0"])

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(output, "run=true\n")
        self.assertEqual(gh_calls, 2)
        self.assertEqual(sleep_calls, ["15"])

    def test_push_routing_does_not_recheck_a_visible_pull_request(self) -> None:
        result, output, gh_calls, sleep_calls = self.run_ci_routing_script(["1"])

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(output, "run=false\n")
        self.assertEqual(gh_calls, 1)
        self.assertEqual(sleep_calls, [])

    def test_push_routing_runs_full_ci_when_the_first_lookup_fails(self) -> None:
        result, output, gh_calls, sleep_calls = self.run_ci_routing_script(["error"])

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(output, "run=true\n")
        self.assertEqual(gh_calls, 1)
        self.assertEqual(sleep_calls, [])

    def test_push_routing_runs_full_ci_when_the_recheck_fails(self) -> None:
        result, output, gh_calls, sleep_calls = self.run_ci_routing_script(["0", "error"])

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(output, "run=true\n")
        self.assertEqual(gh_calls, 2)
        self.assertEqual(sleep_calls, ["15"])


if __name__ == "__main__":
    unittest.main()
