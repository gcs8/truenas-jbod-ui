from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PR_LABEL_WORKFLOW = ROOT / ".github" / "workflows" / "pr-labels.yml"


class BashCIContractTests(unittest.TestCase):
    def test_pr_label_workflow_uses_a_bash_safe_conventional_title_pattern(self) -> None:
        workflow = yaml.safe_load(PR_LABEL_WORKFLOW.read_text(encoding="utf-8"))
        script = workflow["jobs"]["label"]["steps"][0]["run"]

        self.assertIn("conventional_pattern=", script)
        self.assertIn('[[ "$title" =~ $conventional_pattern ]]', script)
        subprocess.run(["bash", "-n"], input=script.encode("utf-8"), check=True)


if __name__ == "__main__":
    unittest.main()
