from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml


RULES_PATH = Path(__file__).resolve().parents[1] / "prometheus" / "rules" / "truenas-jbod-ui-alerts-v1.yml"
CI_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
OPERATIONS_DOC_PATH = (
    Path(__file__).resolve().parents[1] / "wiki" / "Operations-Logging-and-Metrics.md"
)
EXPECTED_ALERTS = {
    "TrueNASJBODUIServiceUnavailable",
    "TrueNASJBODUIHistoryCollectorStale",
    "TrueNASJBODUIHistoryCollectionFailures",
    "TrueNASJBODUIHistoryBackoffExhausted",
    "TrueNASJBODUISmartFailureEvidence",
    "TrueNASJBODUIHighTemperature",
}
ALERT_METRICS = {
    "truenas_jbod_ui_history_collection_interval_seconds",
    "truenas_jbod_ui_history_collection_consecutive_failures",
    "truenas_jbod_ui_history_collection_failure_backoff_seconds",
    "truenas_jbod_ui_history_collection_failure_backoff_max_seconds",
    "truenas_jbod_ui_history_smart_failure_evidence_disks",
    "truenas_jbod_ui_history_max_temperature_celsius",
    "truenas_jbod_ui_history_smart_evidence_timestamp_seconds",
}
EXPECTED_RULE_DEFAULTS = {
    "TrueNASJBODUIServiceUnavailable": (
        "min by (deployment, job, truenas_jbod_ui_monitor) "
        '(up{truenas_jbod_ui_monitor="required"}) == 0',
        "5m",
    ),
    "TrueNASJBODUIHistoryCollectorStale": (
        "max by (deployment, service) "
        "((time() - truenas_jbod_ui_history_last_success_timestamp_seconds"
        '{service="enclosure-history"}) > bool '
        "(2 * truenas_jbod_ui_history_collection_interval_seconds"
        '{service="enclosure-history"})) > 0',
        "5m",
    ),
    "TrueNASJBODUIHistoryCollectionFailures": (
        "max by (deployment, service) "
        '(truenas_jbod_ui_history_collection_consecutive_failures{service="enclosure-history"}) >= 3',
        "5m",
    ),
    "TrueNASJBODUIHistoryBackoffExhausted": (
        "max by (deployment, service) "
        '((truenas_jbod_ui_history_collection_consecutive_failures{service="enclosure-history"} > 0) '
        "and "
        '(truenas_jbod_ui_history_collection_failure_backoff_seconds{service="enclosure-history"} '
        ">= truenas_jbod_ui_history_collection_failure_backoff_max_seconds"
        '{service="enclosure-history"})) > 0',
        "5m",
    ),
    "TrueNASJBODUISmartFailureEvidence": (
        "max by (deployment, service) "
        '(truenas_jbod_ui_history_smart_failure_evidence_disks{service="enclosure-history"}) > 0',
        "5m",
    ),
    "TrueNASJBODUIHighTemperature": (
        "max by (deployment, service) "
        '(truenas_jbod_ui_history_max_temperature_celsius{service="enclosure-history"}) >= 55',
        "15m",
    ),
}
FORBIDDEN_TEMPLATE_LABELS = {
    "system_id",
    "enclosure_id",
    "slot",
    "serial",
    "device_name",
    "instance",
}


class PrometheusAlertRulesTests(unittest.TestCase):
    @staticmethod
    def _normalize_expression(expression: object) -> str:
        normalized = " ".join(str(expression).split())
        return re.sub(r"\s*([(),])\s*", r"\1", normalized)

    @staticmethod
    def _promtool_binary() -> str:
        requested = os.environ.get("PROMTOOL_BINARY") or "promtool"
        binary = shutil.which(requested)
        if not binary:
            raise unittest.SkipTest(
                "promtool is unavailable; scripts/dev_check.py reports this optional local gate as SKIP"
            )
        return binary

    def _load_rules(self) -> list[dict[str, Any]]:
        payload = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"groups"})
        self.assertEqual(len(payload["groups"]), 1)
        group = payload["groups"][0]
        self.assertEqual(group["name"], "truenas-jbod-ui.v1")
        return group["rules"]

    def test_starter_rules_cover_every_operator_critical_condition(self) -> None:
        rules = self._load_rules()
        by_name = {rule["alert"]: rule for rule in rules}

        self.assertEqual(set(by_name), EXPECTED_ALERTS)
        for alert_name, (expected_expression, expected_duration) in EXPECTED_RULE_DEFAULTS.items():
            self.assertEqual(
                self._normalize_expression(by_name[alert_name]["expr"]),
                self._normalize_expression(expected_expression),
            )
            self.assertEqual(str(by_name[alert_name]["for"]), expected_duration)
        for rule in rules:
            self.assertRegex(str(rule["for"]), r"^[1-9][0-9]*[smh]$")
            self.assertEqual(rule["labels"]["owner"], "operator-configure")
            self.assertIn(rule["labels"]["severity"], {"warning", "critical"})
            self.assertIn("summary", rule["annotations"])
            self.assertIn("description", rule["annotations"])

        self.assertIn('truenas_jbod_ui_monitor="required"', by_name["TrueNASJBODUIServiceUnavailable"]["expr"])
        self.assertIn("2 * truenas_jbod_ui_history_collection_interval_seconds", by_name["TrueNASJBODUIHistoryCollectorStale"]["expr"])
        self.assertIn("truenas_jbod_ui_history_collection_consecutive_failures", by_name["TrueNASJBODUIHistoryCollectionFailures"]["expr"])
        self.assertIn("truenas_jbod_ui_history_collection_failure_backoff_max_seconds", by_name["TrueNASJBODUIHistoryBackoffExhausted"]["expr"])
        self.assertIn("truenas_jbod_ui_history_smart_failure_evidence_disks", by_name["TrueNASJBODUISmartFailureEvidence"]["expr"])
        self.assertIn("truenas_jbod_ui_history_max_temperature_celsius", by_name["TrueNASJBODUIHighTemperature"]["expr"])
        self.assertIn(
            "min by (deployment, job, truenas_jbod_ui_monitor)",
            by_name["TrueNASJBODUIServiceUnavailable"]["expr"],
        )
        stale_expression = by_name["TrueNASJBODUIHistoryCollectorStale"]["expr"]
        self.assertEqual(stale_expression.count("max by (deployment, service)"), 1)
        self.assertIn("> bool", stale_expression)
        backoff_expression = by_name["TrueNASJBODUIHistoryBackoffExhausted"]["expr"]
        self.assertEqual(backoff_expression.count("max by (deployment, service)"), 1)
        self.assertIn("and", backoff_expression)
        for alert_name, rule in by_name.items():
            if alert_name != "TrueNASJBODUIServiceUnavailable":
                self.assertIn("by (deployment, service)", rule["expr"])

    def test_starter_rules_do_not_route_or_annotate_with_private_topology(self) -> None:
        rules = self._load_rules()

        for rule in rules:
            rendered = yaml.safe_dump(rule, sort_keys=True)
            expression = str(rule["expr"])
            for label_name in FORBIDDEN_TEMPLATE_LABELS:
                self.assertNotIn(f"$labels.{label_name}", rendered)
                self.assertNotRegex(expression, rf"\b{label_name}\s*=")
            self.assertNotRegex(rendered, r"(?:10\.|192\.168\.|172\.(?:1[6-9]|2[0-9]|3[01])\.)[0-9]")
            self.assertNotRegex(rendered, r"/(?:home|mnt|opt|var)/")

    def test_promtool_accepts_rules_and_rejects_malformed_promql(self) -> None:
        promtool = self._promtool_binary()
        valid = subprocess.run(
            [promtool, "check", "rules", str(RULES_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)

        malformed = RULES_PATH.read_text(encoding="utf-8").replace(
            ") >= 55",
            ")) >= 55",
            1,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            malformed_path = Path(temp_dir) / "malformed-rules.yml"
            malformed_path.write_text(malformed, encoding="utf-8")
            rejected = subprocess.run(
                [promtool, "check", "rules", str(malformed_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(rejected.returncode, 0)

    def test_ci_and_operations_docs_carry_the_rule_contract(self) -> None:
        ci_text = CI_PATH.read_text(encoding="utf-8")
        docs_text = OPERATIONS_DOC_PATH.read_text(encoding="utf-8")

        self.assertIn("python -m unittest tests.test_prometheus_alert_rules -v", ci_text)
        self.assertIn("promtool check rules prometheus/rules/truenas-jbod-ui-alerts-v1.yml", ci_text)
        self.assertIn('PROMTOOL_VERSION: "3.5.0"', ci_text)
        self.assertIn("prometheus-${PROMTOOL_VERSION}.linux-amd64.tar.gz", ci_text)
        self.assertIn("e811827af26d822afb09a4f28314f61b618b12cff5369835a67f674d8b46f39a", ci_text)
        self.assertIn("prometheus/rules/truenas-jbod-ui-alerts-v1.yml", docs_text)
        self.assertIn("Routing ownership", docs_text)
        self.assertIn("Disable or tune a rule", docs_text)
        for alert_name in EXPECTED_ALERTS:
            self.assertIn(f"`{alert_name}`", docs_text)
        for metric_name in ALERT_METRICS:
            self.assertIn(f"`{metric_name}`", docs_text)


if __name__ == "__main__":
    unittest.main()
