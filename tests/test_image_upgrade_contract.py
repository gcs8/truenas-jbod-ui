"""Synthetic deployment contracts, not Docker acceptance evidence."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from scripts import update_immutable_deployment as deployment
from tests import test_immutable_deployment as fixtures
from tests.test_immutable_deployment import FakeRuntime, NEW_IMAGE_ID


class ImageUpgradeTests(unittest.TestCase):
    make_root = fixtures.ImmutableDeploymentTests.make_root
    make_spec = fixtures.ImmutableDeploymentTests.make_spec

    def test_default_preserves_custom_compose_bytes_and_identity_through_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            custom = b'# operator choices\r\nservices:\r\n  enclosure-ui:\r\n    user: "0:0"\r\n'
            (root / 'compose.yaml').write_bytes(custom)
            before = {name: ((root / name).read_bytes(), (root / name).stat().st_ino)
                      for name in ('compose.yaml', 'docker-compose.nonroot.yml')}
            runtime = FakeRuntime(root)
            deployment.update_deployment(self.make_spec(root), run=runtime.run,
                                         download=runtime.download, probe=runtime.probe)
            self.assertEqual(runtime.downloads, [])
            self.assertEqual(deployment.validate_receipt(root)['replace_compose'], False)
            deployment.rollback_deployment(root, run=runtime.run, probe=runtime.probe)
            for name, (data, inode) in before.items():
                self.assertEqual((root / name).read_bytes(), data)
                self.assertEqual((root / name).stat().st_ino, inode)

    def test_explicit_compose_replacement_is_recorded_and_rolled_back(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            previous = (root / 'compose.yaml').read_bytes()
            runtime = FakeRuntime(root)
            spec = replace(self.make_spec(root), replace_compose=True)
            deployment.update_deployment(spec, run=runtime.run, download=runtime.download, probe=runtime.probe)
            self.assertEqual(len(runtime.downloads), 2)
            self.assertNotEqual((root / 'compose.yaml').read_bytes(), previous)
            self.assertTrue(deployment.validate_receipt(root)['replace_compose'])
            deployment.rollback_deployment(root, run=runtime.run, probe=runtime.probe)
            self.assertEqual((root / 'compose.yaml').read_bytes(), previous)

    def test_starting_converges_during_activation_and_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime = FakeRuntime(root)
            remaining = {}
            def run(command, **kwargs):
                result = runtime.run(command, **kwargs)
                if 'up' in command:
                    remaining.update({'container-enclosure-ui': 2, 'container-enclosure-history': 1})
                if command[:2] == ['docker', 'inspect'] and 'State.Health' in command[-2]:
                    count = remaining.get(command[-1], 0)
                    if count:
                        remaining[command[-1]] -= 1
                        return 'starting'
                return result
            with mock.patch.object(deployment.time, 'sleep'):
                result = deployment.update_deployment(self.make_spec(root), run=run,
                                                      download=runtime.download, probe=runtime.probe)
                self.assertEqual(result['status'], 'active')
                self.assertEqual(sum('up' in cmd for cmd in runtime.commands), 1)
                deployment.rollback_deployment(root, run=run, probe=runtime.probe)
            self.assertEqual(deployment.validate_receipt(root)['status'], 'rolled_back')

    def test_starting_forever_is_deadline_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime = FakeRuntime(root)
            deployment.update_deployment(self.make_spec(root), run=runtime.run,
                                         download=runtime.download, probe=runtime.probe)
            runtime.health = 'starting'
            with mock.patch.object(deployment.time, 'monotonic', side_effect=range(0, 10000, 30)), \
                 mock.patch.object(deployment.time, 'sleep') as sleep:
                with self.assertRaisesRegex(deployment.DeploymentError, 'timed out.*starting'):
                    deployment.verify_deployment(root, run=runtime.run, probe=runtime.probe)
            self.assertLessEqual(sleep.call_count, 60)

    def test_health_read_after_deadline_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime = FakeRuntime(root)
            deployment.update_deployment(self.make_spec(root), run=runtime.run,
                                         download=runtime.download, probe=runtime.probe)
            with mock.patch.object(deployment.time, 'monotonic', side_effect=[0, 121]):
                with self.assertRaisesRegex(deployment.DeploymentError, 'timed out'):
                    deployment.verify_deployment(root, run=runtime.run, probe=runtime.probe)

    def test_readiness_rechecks_identity_and_restart_state(self):
        for fault in ('restart', 'image', 'health', 'exit', 'container'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                root = self.make_root(temp)
                runtime = FakeRuntime(root)
                changed = False
                def probe(url):
                    nonlocal changed
                    if runtime.active_image_id == NEW_IMAGE_ID:
                        changed = True
                def run(command, **kwargs):
                    result = runtime.run(command, **kwargs)
                    if changed and runtime.active_image_id == NEW_IMAGE_ID:
                        if fault == 'restart' and command[-2] == '{{.RestartCount}}':
                            return '1'
                        if fault == 'image' and command[-2] == '{{.Image}}':
                            return 'sha256:' + 'f' * 64
                        if fault == 'health' and 'State.Health' in command[-2]:
                            return 'unhealthy'
                        if fault == 'exit' and command[-2] == '{{.State.Status}}':
                            return 'exited'
                        if fault == 'container' and 'ps' in command and '-q' in command:
                            return 'replacement-' + command[-1]
                    return result
                # FakeRuntime understands the original ID; simulate inspect for a replacement.
                original_run = run
                def inspect_run(command, **kwargs):
                    if command[:2] == ['docker', 'inspect'] and command[-1].startswith('replacement-'):
                        command = [*command[:-1], command[-1].replace('replacement-', 'container-', 1)]
                    return original_run(command, **kwargs)
                with self.assertRaisesRegex(deployment.DeploymentError, 'automatic rollback completed'):
                    deployment.update_deployment(self.make_spec(root), run=inspect_run,
                                                 download=runtime.download, probe=probe)
                self.assertEqual(deployment.validate_receipt(root)['status'], 'rolled_back')

    def test_image_only_preflight_uses_live_relative_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime = FakeRuntime(root)
            def run(command, **kwargs):
                if command[:2] == ['docker', 'compose'] and 'config' in command:
                    files = [Path(command[i + 1]) for i, arg in enumerate(command) if arg == '-f']
                    self.assertTrue(all(path.parent == root for path in files),
                                    'image-only preflight must resolve operator relative files in place')
                return runtime.run(command, **kwargs)
            deployment.update_deployment(self.make_spec(root), run=run,
                                         download=runtime.download, probe=runtime.probe)

    def test_canceled_manual_rollback_cannot_leave_active_success_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime = FakeRuntime(root)
            deployment.update_deployment(self.make_spec(root), run=runtime.run,
                                         download=runtime.download, probe=runtime.probe)
            def canceled_probe(url):
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                deployment.rollback_deployment(root, run=runtime.run, probe=canceled_probe)
            receipt = deployment.validate_receipt(root)
            self.assertEqual(receipt['status'], 'prepared')
            self.assertIsNone(receipt['result'])

    def test_failed_activation_waits_for_rollback_health(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime = FakeRuntime(root, fail_activation=True)
            rollback_reads = 0
            def run(command, **kwargs):
                nonlocal rollback_reads
                result = runtime.run(command, **kwargs)
                if 'up' in command:
                    rollback_reads = 2
                if command[:2] == ['docker', 'inspect'] and 'State.Health' in command[-2] and rollback_reads:
                    rollback_reads -= 1
                    return 'starting'
                return result
            with mock.patch.object(deployment.time, 'sleep'):
                with self.assertRaisesRegex(deployment.DeploymentError, 'automatic rollback completed'):
                    deployment.update_deployment(self.make_spec(root), run=run,
                                                 download=runtime.download, probe=runtime.probe)
            self.assertEqual(deployment.validate_receipt(root)['status'], 'rolled_back')
            self.assertEqual(rollback_reads, 0)

    def test_image_only_rollback_refuses_operator_compose_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime = FakeRuntime(root)
            deployment.update_deployment(self.make_spec(root), run=runtime.run,
                                         download=runtime.download, probe=runtime.probe)
            changed = b'# later operator edit\nservices: {}\n'
            (root / 'compose.yaml').write_bytes(changed)
            with self.assertRaisesRegex(deployment.DeploymentError, 'image-only rollback Compose changed'):
                deployment.rollback_deployment(root, run=runtime.run, probe=runtime.probe)
            self.assertEqual((root / 'compose.yaml').read_bytes(), changed)

    def test_legacy_replacement_receipt_remains_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime = FakeRuntime(root)
            deployment.update_deployment(replace(self.make_spec(root), replace_compose=True),
                                         run=runtime.run, download=runtime.download, probe=runtime.probe)
            receipt = deployment.validate_receipt(root)
            del receipt['replace_compose']
            deployment._write_receipt(root / deployment.RECEIPT_DIR_NAME, receipt)
            deployment.rollback_deployment(root, run=runtime.run, probe=runtime.probe)
            self.assertEqual(deployment.validate_receipt(root)['status'], 'rolled_back')
            self.assertIn('previous base', (root / 'compose.yaml').read_text())

    def test_base_root_compatibility_and_opt_in_full_hardening(self):
        base = yaml.safe_load(Path('docker-compose.yml').read_text())['services']
        overlay = yaml.safe_load(Path('docker-compose.nonroot.yml').read_text())['services']
        for name in ('enclosure-ui', 'enclosure-history', 'enclosure-admin'):
            self.assertEqual(base[name]['user'], '0:0')
            for key in ('read_only', 'cap_drop', 'cap_add', 'security_opt', 'tmpfs'):
                self.assertNotIn(key, base[name])
            hardened = {**base[name], **overlay[name]}
            self.assertTrue(hardened['read_only'])
            self.assertEqual(hardened['cap_drop'], ['ALL'])
            self.assertEqual(hardened['security_opt'], ['no-new-privileges:true'])
            self.assertEqual(hardened['tmpfs'], ['/tmp'])
        self.assertIn('./config:/app/config:ro', base['enclosure-ui']['volumes'])
        self.assertIn('./config:/app/config:ro', overlay['enclosure-ui']['volumes'])
        self.assertEqual(overlay['enclosure-admin']['cap_add'], ['CHOWN', 'FOWNER'])
        self.assertEqual(base['enclosure-backup']['network_mode'], 'none')
        self.assertTrue(base['enclosure-backup']['read_only'])


if __name__ == '__main__':
    unittest.main()


class ImmutableRetentionCheckTests(unittest.TestCase):
    """#400: the immutable update consumes only aggregate disk-retention totals."""

    make_root = fixtures.ImmutableDeploymentTests.make_root
    make_spec = fixtures.ImmutableDeploymentTests.make_spec
    INVENTORY_URL = "http://127.0.0.1:8080/api/inventory"

    @staticmethod
    def inventory(source, rendered, duplicate, unplaced):
        return {
            "summary": {
                "disk_count": source,
                "source_disk_count": source,
                "rendered_unique_disk_count": rendered,
                "duplicate_disk_view_count": duplicate,
                "unplaced_disk_count": unplaced,
            },
            # Identifiers the check must never copy into the receipt.
            "slots": [{"serial": "SYNTH-SERIAL-0001", "device_name": "da0"}],
        }

    def update(self, root, responses):
        runtime = FakeRuntime(root)
        answers = list(responses)
        spec = replace(self.make_spec(root), inventory_url=self.INVENTORY_URL)

        def fetch_json(url):
            self.assertEqual(url, self.INVENTORY_URL)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        return runtime, lambda: deployment.update_deployment(
            spec, run=runtime.run, download=runtime.download, probe=runtime.probe, fetch_json=fetch_json)

    def test_healthy_totals_activate_and_record_counts_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            _, update = self.update(root, [self.inventory(6, 6, 2, 0), self.inventory(6, 6, 0, 0)])
            result = update()
            receipt = deployment.validate_receipt(root)
            self.assertEqual(receipt["status"], "active")
            self.assertEqual(receipt["retention"]["before"]["source_disk_count"], 6)
            self.assertEqual(receipt["retention"]["after"]["unplaced_disk_count"], 0)
            self.assertEqual(result["retention"], receipt["retention"])
            receipt_text = (root / deployment.RECEIPT_DIR_NAME / "receipt.json").read_text(encoding="utf-8")
            self.assertNotIn("SYNTH-SERIAL", receipt_text)
            self.assertNotIn("da0", receipt_text)

    def test_unplaced_disk_rolls_back_automatically(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime, update = self.update(root, [self.inventory(6, 6, 0, 0), self.inventory(6, 5, 0, 1)])
            with self.assertRaisesRegex(deployment.DeploymentError, "automatic rollback completed") as caught:
                update()
            self.assertIn("1 of 6 source disks", str(caught.exception.__cause__))
            receipt = deployment.validate_receipt(root)
            self.assertEqual(receipt["status"], "rolled_back")
            self.assertEqual(receipt["retention"]["after"]["unplaced_disk_count"], 1)
            self.assertEqual(runtime.active_image_id, fixtures.OLD_IMAGE_ID)

    def test_source_disk_loss_across_update_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            _, update = self.update(root, [self.inventory(6, 6, 0, 0), self.inventory(5, 5, 0, 0)])
            with self.assertRaises(deployment.DeploymentError) as caught:
                update()
            self.assertIn("source disks fell from 6 to 5", str(caught.exception.__cause__))

    def test_predecessor_without_totals_still_gates_the_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            _, update = self.update(root, [{"summary": {"disk_count": 6}}, self.inventory(6, 6, 0, 0)])
            update()
            receipt = deployment.validate_receipt(root)
            self.assertIsNone(receipt["retention"]["before"])
            self.assertEqual(receipt["status"], "active")

    def test_multipath_collapse_with_zero_unplaced_activates(self):
        # The supported multipath fixture shape: 5 source records, 4 logical
        # disks, 1 duplicate view, nothing unplaced.
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            _, update = self.update(root, [self.inventory(5, 4, 1, 0), self.inventory(5, 4, 1, 0)])
            update()
            self.assertEqual(deployment.validate_receipt(root)["status"], "active")

    def test_unreadable_baseline_stops_before_any_change(self):
        for failure in (
            deployment.DeploymentError("inventory retention check could not read the inventory"),
            {"no": "summary"},
            {"summary": {**self.inventory(6, 6, 0, 0)["summary"], "unplaced_disk_count": "0"}},
        ):
            with self.subTest(failure=repr(failure)[:60]), tempfile.TemporaryDirectory() as temp:
                root = self.make_root(temp)
                env_before = (root / ".env").read_bytes()
                runtime, update = self.update(root, [failure, self.inventory(6, 6, 0, 0)])
                with self.assertRaises(deployment.DeploymentError) as caught:
                    update()
                self.assertNotIn("rollback", str(caught.exception))
                self.assertFalse((root / deployment.RECEIPT_DIR_NAME).exists())
                self.assertEqual((root / ".env").read_bytes(), env_before)
                self.assertFalse(any(command[:2] == ("docker", "pull") for command in runtime.commands))

    def test_candidate_without_totals_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            _, update = self.update(root, [self.inventory(6, 6, 0, 0), {"summary": {"disk_count": 6}}])
            with self.assertRaises(deployment.DeploymentError) as caught:
                update()
            self.assertIn("predates retention totals", str(caught.exception.__cause__))

    def test_inventory_url_must_be_loopback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            spec = replace(self.make_spec(root), inventory_url="http://192.0.2.10:8080/api/inventory")
            with self.assertRaisesRegex(deployment.DeploymentError, "inventory URL must use an explicit loopback"):
                deployment.update_deployment(spec, run=FakeRuntime(root).run)

    def test_receipts_without_retention_remain_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(temp)
            runtime = FakeRuntime(root)
            deployment.update_deployment(self.make_spec(root), run=runtime.run,
                                         download=runtime.download, probe=runtime.probe)
            self.assertNotIn("retention", deployment.validate_receipt(root))

    def test_cli_forwards_inventory_url(self):
        args = deployment._build_parser().parse_args([
            "update", "/srv/jbod", "--project-name=jbod", "--source-revision=" + "0" * 40,
            "--expected-image=ghcr.io/gcs8/truenas-jbod-ui@sha256:" + "0" * 64,
            "--candidate-tag=ghcr.io/gcs8/truenas-jbod-ui:v1", "--compose=docker-compose.yml=live.yml",
            "--service=app", "--health-url=http://127.0.0.1/healthz",
            "--inventory-url=http://127.0.0.1:8080/api/inventory",
        ])
        self.assertEqual(args.inventory_url, "http://127.0.0.1:8080/api/inventory")


class ImageOnlyUpgradeSmokeContractTests(unittest.TestCase):
    """#399/#463: CI upgrades the previous public release by image pin only."""

    ROOT = Path(__file__).resolve().parents[1]

    def load_smoke(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "run_image_upgrade_smoke", self.ROOT / "scripts" / "run_image_upgrade_smoke.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_ci_job_upgrades_the_pinned_public_previous_release(self):
        workflow = yaml.safe_load((self.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        job = workflow["jobs"]["image-upgrade-smoke"]
        self.assertEqual(job["needs"], "route")
        self.assertIn("needs.route.outputs.run == 'true'", job["if"])
        self.assertRegex(job["env"]["PREVIOUS_IMAGE"], r"^ghcr\.io/gcs8/truenas-jbod-ui@sha256:[0-9a-f]{64}$")
        self.assertEqual(job["env"]["PREVIOUS_VERSION"], "0.22.2")
        commands = "\n".join(str(step.get("run", "")) for step in job["steps"])
        self.assertIn("tests/fixtures/compose/v0.22.2.yml", commands)
        self.assertIn('--candidate-revision "$GITHUB_SHA"', commands)
        self.assertIn('SOURCE_COMMIT="$GITHUB_SHA"', commands)
        self.assertRegex(commands, r"registry:2@sha256:[0-9a-f]{64}")
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                self.assertFalse(step["with"]["persist-credentials"])

    def test_upgrade_changes_only_the_image_pin_between_phases(self):
        import inspect
        smoke = self.load_smoke()
        source = (self.ROOT / "scripts" / "run_image_upgrade_smoke.py").read_text(encoding="utf-8")
        # The operator procedure: set JBOD_UI_IMAGE, pull, up -d. No ownership
        # repair, no down, no Compose replacement, no migration command.
        phases = [inspect.getsource(function) for function in (
            smoke.seed_previous_release, smoke.upgrade_and_rollback, smoke.segmented_catalog_upgrade,
            smoke.interrupted_migration, smoke.kill_during_startup, smoke.check_runtime,
            *(value for value in vars(smoke.Deployment).values() if inspect.isfunction(value)))]
        for forbidden in ("chown", "prepare_nonroot_bind_mounts", "migrate_segmented_history", '"down"',
                          "HARDENED_OWNERSHIP_PREP"):
            for phase in phases:
                with self.subTest(forbidden=forbidden, phase=phase.split("(", 1)[0]):
                    self.assertNotIn(forbidden, phase)
        # The only ownership change is the documented one-time step that adopts
        # the overlay, and it runs in prepare_root, before the first start.
        prep = source.split("HARDENED_OWNERSHIP_PREP = (", 1)[1].split("\n)\n", 1)[0]
        self.assertEqual(source.count("chown"), prep.count("chown"))
        self.assertIn("HARDENED_OWNERSHIP_PREP", inspect.getsource(smoke.prepare_root))
        calls = []
        deployment = smoke.Deployment(Path("/nonexistent"))
        deployment.compose = lambda *args, **kwargs: calls.append(args) or ""
        deployment.pull_and_up()
        self.assertEqual(calls, [("pull",), ("up", "-d", "--wait", "--wait-timeout", "300")])

    def test_compose_fixture_is_the_exact_previous_release_file(self):
        fixture = yaml.safe_load((self.ROOT / "tests" / "fixtures" / "compose" / "v0.22.2.yml").read_text(encoding="utf-8"))
        for service in ("enclosure-ui", "enclosure-history"):
            with self.subTest(service=service):
                self.assertEqual(fixture["services"][service]["user"], "0:0")
                self.assertEqual(fixture["services"][service]["image"], "${JBOD_UI_IMAGE:-ghcr.io/gcs8/truenas-jbod-ui:latest}")

    def test_published_support_matrix_names_the_check_that_backs_it(self):
        workflow = yaml.safe_load((self.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        guide = (self.ROOT / "wiki" / "Upgrading.md").read_text(encoding="utf-8")
        matrix = guide.split("## What is tested", 1)[1].split("\n## ", 1)[0]
        self.assertIn(workflow["jobs"]["image-upgrade-smoke"]["name"], " ".join(matrix.split()))
        self.assertIn(workflow["jobs"]["image-upgrade-scenarios"]["name"], " ".join(matrix.split()))
        for scenario in ("hardened", "interrupted-migration", "segmented-catalog"):
            self.assertIn(f"--scenario {scenario}", " ".join(matrix.split()))
        self.assertIn("scripts/run_image_upgrade_smoke.py", matrix)
        self.assertIn(f"v{workflow['jobs']['image-upgrade-smoke']['env']['PREVIOUS_VERSION']}", matrix)
        self.assertIn("Not tested yet", matrix)

    def test_expected_history_view_matches_the_seed_shape(self):
        smoke = self.load_smoke()
        view = smoke.expected_history_view({1: 2, 4: 1})
        self.assertEqual(view["events"], [[1, "disk_inserted", "SYNTH-0001", "SYNTH-0001"],
                                          [4, "disk_inserted", "SYNTH-0004", "SYNTH-0004"]])
        self.assertEqual(view["samples"], [[1, 30, "SYNTH-0001"], [1, 31, "SYNTH-0001"], [4, 30, "SYNTH-0004"]])
        source = (self.ROOT / "scripts" / "run_image_upgrade_smoke.py").read_text(encoding="utf-8")
        segmented = source.split("def segmented_catalog_upgrade", 1)[1].split("\n\ndef ", 1)[0]
        candidate_read = segmented.index("history_api_view((1, 2, 3, 4))")
        rollback = segmented.index("deployment.set_image(args.previous_image)")
        self.assertLess(candidate_read, rollback)
        self.assertEqual(segmented.count("history_api_view((1, 2, 3, 4))"), 2)

    def test_seed_and_read_scripts_compile(self):
        smoke = self.load_smoke()
        for name, script in (
            ("seed_ui", smoke.seed_script(smoke.SEED_UI, (1, 2))),
            ("seed_history", smoke.seed_script(smoke.SEED_HISTORY, (1,), metrics_per_slot=2)),
            ("read_ui", smoke.READ_UI),
            ("read_history", smoke.READ_HISTORY),
        ):
            with self.subTest(script=name):
                compile(script, name, "exec")

    def test_schema_evidence_reports_equal_versions_as_compatibility_not_a_transition(self):
        smoke = self.load_smoke()
        predecessor = {"user_version": 1, "current_schema": 1}
        candidate = {"user_version": 1, "current_schema": 1}

        evidence = smoke.schema_evidence(predecessor, candidate)

        self.assertEqual(
            evidence,
            {"compatibility": "ok", "before": "1", "after": "1", "transition": "none"},
        )
        self.assertEqual(
            smoke.schema_evidence(predecessor, {"user_version": 2, "current_schema": 2}),
            {"compatibility": "ok", "before": "1", "after": "2", "transition": "1->2"},
        )
        with self.assertRaisesRegex(smoke.SmokeError, "predecessor schema is not current"):
            smoke.schema_evidence({"user_version": 0, "current_schema": 1}, candidate)
        with self.assertRaisesRegex(smoke.SmokeError, "candidate schema is not current"):
            smoke.schema_evidence(predecessor, {"user_version": 0, "current_schema": 1})

    def test_upgrade_evidence_and_docs_do_not_claim_an_equal_schema_was_migrated(self):
        from history_service.store import SCHEMA

        released_schema = (
            self.ROOT / "tests" / "fixtures" / "history_released_schemas" / "v0.22.2.sql"
        ).read_text(encoding="utf-8")
        self.assertEqual(SCHEMA, released_schema, "update the evidence when the candidate schema changes")
        source = (self.ROOT / "scripts" / "run_image_upgrade_smoke.py").read_text(encoding="utf-8")
        upgrade = source.split("def upgrade_and_rollback", 1)[1].split("\ndef ", 1)[0]
        interrupted = source.split("def interrupted_migration", 1)[1].split("\ndef ", 1)[0]
        guide = (self.ROOT / "wiki" / "Upgrading.md").read_text(encoding="utf-8")
        matrix = guide.split("## What is tested", 1)[1].split("\n## ", 1)[0]
        scripts_guide = (self.ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
        changelog = (self.ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        for text in (upgrade, interrupted, matrix, scripts_guide):
            self.assertNotIn("schema migrated", text)
            self.assertNotIn("finish the migration", text)
        self.assertIn("schema_compatibility=ok", upgrade)
        self.assertIn("schema_before={schema['before']}", upgrade)
        self.assertIn("schema_after={schema['after']}", upgrade)
        self.assertIn("schema_transition={schema['transition']}", upgrade)
        self.assertIn("schema_compatibility=ok", interrupted)
        self.assertIn("schema_before={schema['before']}", interrupted)
        self.assertIn("schema_after={schema['after']}", interrupted)
        self.assertIn("schema_transition={schema['transition']}", interrupted)
        self.assertIn("does not exercise a schema transition", matrix)
        self.assertIn("(#637)", changelog.split("## Unreleased", 1)[1].split("\n## ", 1)[0])


class UpgradeScenarioContractTests(unittest.TestCase):
    """#399/#463: hardened, interrupted-migration and segmented-catalog upgrades."""

    ROOT = Path(__file__).resolve().parents[1]
    load_smoke = ImageOnlyUpgradeSmokeContractTests.load_smoke

    def workflow(self):
        return yaml.safe_load((self.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))

    def test_scenario_job_runs_each_scenario_as_its_own_step_from_the_pinned_release(self):
        workflow = self.workflow()
        required = workflow["jobs"]["image-upgrade-smoke"]
        job = workflow["jobs"]["image-upgrade-scenarios"]
        self.assertEqual(job["needs"], "route")
        self.assertIn("needs.route.outputs.run == 'true'", job["if"])
        self.assertEqual(job["env"], required["env"])
        runs = [str(step.get("run", "")) for step in job["steps"]]
        hardened = [command for command in runs if "--scenario hardened" in command]
        interrupted = [command for command in runs if "--scenario interrupted-migration" in command]
        segmented = [command for command in runs if "--scenario segmented-catalog" in command]
        self.assertEqual(len(hardened), 1)
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(len(segmented), 1)
        self.assertIn("--nonroot-fixture tests/fixtures/compose/v0.22.2.nonroot.yml", hardened[0])
        for command in hardened + interrupted + segmented:
            self.assertIn("--compose-fixture tests/fixtures/compose/v0.22.2.yml", command)
            self.assertIn('--candidate-revision "$GITHUB_SHA"', command)
        # The required job stays the fast base scenario.
        self.assertNotIn("--scenario", "\n".join(str(step.get("run", "")) for step in required["steps"]))

    def test_nonroot_fixture_is_the_exact_v0_22_2_overlay(self):
        import hashlib
        # `git show v0.22.2:docker-compose.nonroot.yml`, byte for byte.
        data = (self.ROOT / "tests" / "fixtures" / "compose" / "v0.22.2.nonroot.yml").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         "7b9e6604b402ac02d9a73993fda8032d0d71ee3c521788ce45f24cd31db76154")
        overlay = yaml.safe_load(data)
        for service in ("enclosure-ui", "enclosure-history"):
            self.assertEqual(overlay["services"][service]["user"], "${APP_UID:-10001}:${APP_GID:-10001}")

    def test_hardened_ownership_step_is_the_documented_one(self):
        smoke = self.load_smoke()
        guide = (self.ROOT / "wiki" / "Troubleshooting.md").read_text(encoding="utf-8")
        section = guide.split("## A non-root container gets permission denied", 1)[1].split("\n## ", 1)[0]
        block = section.split("```bash\n", 1)[1].split("```", 1)[0].splitlines()
        # The documented block is: down, the preparation, then up with the overlay.
        self.assertEqual(block[0], "docker compose down")
        self.assertEqual(block[-1], "docker compose -f docker-compose.yml -f docker-compose.nonroot.yml up -d")
        self.assertEqual(tuple(block[1:-1]), smoke.HARDENED_OWNERSHIP_PREP)
        self.assertEqual(smoke.HARDENED_FILES, ("docker-compose.yml", "docker-compose.nonroot.yml"))

    def test_hardened_scenario_runs_every_compose_command_with_the_overlay(self):
        smoke = self.load_smoke()
        deployment = smoke.Deployment(Path("/nonexistent"), smoke.HARDENED_FILES)
        self.assertEqual(deployment.file_args(),
                         ["-f", "docker-compose.yml", "-f", "docker-compose.nonroot.yml"])
        calls = []
        with mock.patch.object(smoke, "run", side_effect=lambda command, **kwargs: calls.append(command) or ""):
            deployment.compose("pull")
            deployment.compose("up", "-d")
        self.assertEqual(calls[0][:6], ["docker", "compose", "-f", "docker-compose.yml",
                                        "-f", "docker-compose.nonroot.yml"])
        self.assertEqual(calls[1][6:], ["up", "-d"])
        self.assertEqual(smoke.Deployment(Path("/nonexistent")).file_args(), [])

    def test_kill_phases_are_the_history_startup_migration_steps_in_order(self):
        import inspect
        from history_service.store import HistoryStore
        from tests import test_history_released_schema_upgrades as released

        smoke = self.load_smoke()
        body = inspect.getsource(HistoryStore._initialize_schema)
        positions = [body.index(f"self.{phase}(connection)") for phase in smoke.KILL_PHASES]
        self.assertEqual(positions, sorted(positions))
        # Every runtime kill point is one of #603's unit-level kill seams; only
        # the batched backfill, which v0.22.2 state never enters, is left out.
        unit_methods = {method for method, _ in released.KILL_PHASES.values()}
        self.assertLessEqual(set(smoke.KILL_PHASES), unit_methods)
        self.assertEqual(unit_methods - set(smoke.KILL_PHASES), {"_backfill_disk_identity_batch"})
        for phase in smoke.KILL_PHASES:
            self.assertIsInstance(inspect.getattr_static(HistoryStore, phase), staticmethod)

    def test_kill_hook_pauses_the_real_service_startup(self):
        smoke = self.load_smoke()
        script = smoke.kill_hook_script("_ensure_identity_indexes")
        compile(script, "kill_hook", "exec")
        self.assertIn("import history_service.main", script)
        self.assertIn(smoke.KILL_MARKER, script)
        self.assertIn("signal.pause()", script)
        with self.assertRaises(ValueError):
            smoke.kill_hook_script("_not_a_phase")
        for name, template in (("end_state", smoke.END_STATE), ("reference", smoke.UNINTERRUPTED_REFERENCE)):
            with self.subTest(script=name):
                compile(smoke.end_state_script("/reference/history.db", template), name, "exec")
        source = (self.ROOT / "scripts" / "run_image_upgrade_smoke.py").read_text(encoding="utf-8")
        self.assertIn('"docker", "kill", "--signal", "KILL"', source)
        self.assertNotIn("time.sleep", source.split("def kill_during_startup", 1)[1].split("\ndef ", 1)[0])

    def test_end_state_matches_an_uninterrupted_upgrade_of_the_same_database(self):
        import json
        import sqlite3
        import subprocess
        import sys
        import tempfile
        from contextlib import closing

        from history_service.store import HistoryStore
        from tests import test_history_released_schema_upgrades as released

        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            released.build_released_database(path, "v0.22.2", stamp_like_release=True)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(f"UPDATE slot_events SET system_id = '{smoke.SMOKE_SYSTEM}'")
                connection.commit()
            HistoryStore(str(path))

            def read() -> dict:
                output = subprocess.run([sys.executable, "-c", smoke.end_state_script(str(path))],
                                        capture_output=True, text=True, check=True, cwd=self.ROOT)
                return json.loads(output.stdout.strip().splitlines()[-1])

            first = read()
            self.assertEqual(first["integrity"], "ok")
            self.assertTrue(first["counters_match"])
            self.assertEqual(first, read())
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(f"DELETE FROM slot_events WHERE rowid = "
                                   f"(SELECT MIN(rowid) FROM slot_events WHERE system_id = '{smoke.SMOKE_SYSTEM}')")
                connection.commit()
            self.assertNotEqual(read()["rows"], first["rows"])

    def test_segmented_catalog_setting_survives_image_pin_changes(self):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            deployment = smoke.Deployment(Path(directory))
            deployment.set_image("previous")
            deployment.set_runtime_environment(
                "HISTORY_SEGMENT_CATALOG_PATH",
                "/app/history/segments/catalog.json",
            )
            deployment.set_image("candidate")
            values = dict(
                line.split("=", 1)
                for line in (Path(directory) / ".env").read_text(encoding="utf-8").splitlines()
                if line
            )
        self.assertEqual(values["JBOD_UI_IMAGE"], "candidate")
        self.assertEqual(
            values["HISTORY_SEGMENT_CATALOG_PATH"],
            "/app/history/segments/catalog.json",
        )

    def test_segmented_scenario_compares_exact_catalog_identity_across_upgrade_and_rollback(self):
        import inspect

        smoke = self.load_smoke()
        self.assertIn("segmented-catalog", smoke.SCENARIOS)
        for name, script in (
            ("create_segmented_catalog", smoke.CREATE_SEGMENTED_CATALOG),
            ("read_segmented_identity", smoke.READ_SEGMENTED_IDENTITY),
        ):
            with self.subTest(script=name):
                compile(script, name, "exec")
        source = inspect.getsource(smoke.segmented_catalog_upgrade)
        self.assertIn("seed_segmented_catalog", source)
        self.assertIn("after_identity == before_identity", source)
        self.assertIn("rolled_identity == before_identity", source)
        self.assertIn("history_api_view((1, 2, 3))", source)
        self.assertIn("history_api_view((1, 2, 3, 4))", source)
        self.assertIn("catalog_identity=ok", source)

    def test_hardened_scenario_requires_the_overlay_fixture(self):
        smoke = self.load_smoke()
        base = ["--root", "/nonexistent", "--compose-fixture", "a", "--config-fixture", "b",
                "--previous-image", "p", "--previous-version", "1", "--candidate-image", "c",
                "--candidate-version", "2", "--candidate-revision", "r"]
        self.assertEqual(smoke.build_parser().parse_args(base).scenario, "base")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            smoke.main([*base, "--scenario", "hardened"])
