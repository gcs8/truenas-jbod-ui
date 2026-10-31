"""Synthetic deployment contracts, not Docker acceptance evidence."""
from dataclasses import replace
from pathlib import Path
import contextlib
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
            # #911: the check asks for totals across every enclosure and view.
            self.assertEqual(url, self.INVENTORY_URL + "?retention_scope=system")
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

    def test_a_busy_system_check_is_retried_instead_of_rolling_back(self):
        # Another client's system-wide check holds the per-system lock; the
        # route answers 503 with Retry-After, which is not a failed candidate.
        busy = deployment.InventoryBusy(retry_after_seconds=5)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(deployment.time, "sleep") as sleep:
            root = self.make_root(temp)
            _, update = self.update(root, [busy, self.inventory(6, 6, 0, 0), busy, busy, self.inventory(6, 6, 0, 0)])
            update()
            receipt = deployment.validate_receipt(root)
        self.assertEqual(receipt["status"], "active")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 5, 5])

    def test_a_check_that_stays_busy_fails_after_a_bounded_wait(self):
        busy = deployment.InventoryBusy(retry_after_seconds=30)
        attempts = deployment.RETENTION_BUSY_MAX_WAIT_SECONDS // 30 + 1
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(deployment.time, "sleep") as sleep:
            root = self.make_root(temp)
            runtime, update = self.update(
                root, [self.inventory(6, 6, 0, 0), *[busy] * attempts],
            )
            with self.assertRaisesRegex(deployment.DeploymentError, "automatic rollback completed") as caught:
                update()
            receipt = deployment.validate_receipt(root)
        self.assertIn("stayed busy", str(caught.exception.__cause__))
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), deployment.RETENTION_BUSY_MAX_WAIT_SECONDS)
        self.assertEqual(runtime.active_image_id, fixtures.OLD_IMAGE_ID)

    def test_only_a_503_with_retry_after_counts_as_busy(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/busy"):
                    self.send_response(503)
                    self.send_header("Retry-After", "5")
                elif self.path.startswith("/huge"):
                    self.send_response(503)
                else:
                    self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": false, "detail": "SYNTH-DETAIL"}')

            def log_message(self, *args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(deployment.InventoryBusy) as caught:
                deployment._default_fetch_json(f"{base}/busy")
            self.assertEqual(caught.exception.retry_after_seconds, 5)
            for path in ("/huge", "/error"):
                with self.subTest(path=path):
                    with self.assertRaises(deployment.DeploymentError) as other:
                        deployment._default_fetch_json(f"{base}{path}")
                    self.assertNotIsInstance(other.exception, deployment.InventoryBusy)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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

    def test_retention_url_asks_for_system_wide_totals(self):
        for given, expected in (
            ("http://127.0.0.1:8080/api/inventory",
             "http://127.0.0.1:8080/api/inventory?retention_scope=system"),
            ("http://127.0.0.1:8080/api/inventory?system_id=archive-core",
             "http://127.0.0.1:8080/api/inventory?system_id=archive-core&retention_scope=system"),
            ("http://127.0.0.1:8080/api/inventory?retention_scope=enclosure&force=true",
             "http://127.0.0.1:8080/api/inventory?force=true&retention_scope=system"),
        ):
            with self.subTest(given=given):
                self.assertEqual(deployment._system_retention_url(given), expected)

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
            smoke.readonly_state, smoke.install_readonly_history_mount,
            *(value for value in vars(smoke.Deployment).values() if inspect.isfunction(value)))]
        for forbidden in ("chown", "prepare_nonroot_bind_mounts", "migrate_segmented_history", '"down"',
                          "HARDENED_OWNERSHIP_PREP"):
            for phase in phases:
                with self.subTest(forbidden=forbidden, phase=phase.split("(", 1)[0]):
                    self.assertNotIn(forbidden, phase)
        # The only ownership change is the documented one-time step that adopts
        # the overlay, and it runs in prepare_root, before the first start.
        prep = source.split("HARDENED_OWNERSHIP_PREP = (", 1)[1].split("\n)\n", 1)[0]
        # A diagnostic may reject chown without executing an ownership repair.
        self.assertEqual(source.count("sudo chown "), prep.count("sudo chown "))
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
        for scenario in ("hardened", "interrupted-migration", "segmented-catalog", "unavailable-backup-storage"):
            self.assertIn(f"--scenario {scenario}", " ".join(matrix.split()))
        self.assertIn("scripts/run_image_upgrade_smoke.py", matrix)
        self.assertIn(f"v{workflow['jobs']['image-upgrade-smoke']['env']['PREVIOUS_VERSION']}", matrix)
        self.assertIn("Not tested yet", matrix)

    def test_support_matrix_does_not_list_ci_checked_storage_faults_as_untested(self):
        # #463: readonly-state and enospc-state run in CI; the table must say so.
        import re
        workflow = yaml.safe_load((self.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["image-upgrade-scenarios"]["steps"]
        ci_scenarios = {name for step in steps for name in re.findall(r"--scenario (\S+)", step.get("run", ""))}
        self.assertTrue({"readonly-state", "enospc-state"} <= ci_scenarios)
        guide = (self.ROOT / "wiki" / "Upgrading.md").read_text(encoding="utf-8")
        matrix = guide.split("## What is tested", 1)[1].split("\n## ", 1)[0]
        # Every scenario CI runs is named, so a tested cell cannot outlive its check.
        for scenario in sorted(ci_scenarios):
            self.assertIn(f"--scenario {scenario}", " ".join(matrix.split()))
        rows = {}
        for line in matrix.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
            if line.startswith("| ") and len(cells) == 3:
                rows[cells[0]] = cells[1:]
        tested, untested = rows["Local state"]
        self.assertIn("read-only", tested)
        self.assertIn("full", tested)
        self.assertIn("HTTP 503", tested)
        self.assertNotIn("read-only or full disks", untested)
        # Bounds the CI runs do not cover stay listed as untested.
        for bound in ("Network filesystems", "config or UI data directory", "data blocks"):
            self.assertIn(bound, untested)
        recovery_tested, recovery_untested = rows["Recovery"]
        self.assertIn("fault is removed by hand", recovery_tested)
        self.assertNotIn("a full disk", recovery_untested)

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

    def test_read_history_resolves_the_predecessor_schema_from_the_released_store(self):
        # READ_HISTORY runs inside the v0.22.2 image, whose store predates
        # CURRENT_SCHEMA_VERSION. Evaluate the probe's own lookup against the
        # constants that release actually defines.
        import ast
        import subprocess
        from types import SimpleNamespace

        import history_service.store as current_store

        smoke = self.load_smoke()
        lookup = smoke.READ_HISTORY.split("current_schema = ", 1)[1].split("\nprint(", 1)[0]

        # The schema-version constants v0.22.2's history_service/store.py
        # defines. Pinned so shallow and --no-tags checkouts can run this test;
        # cross-checked against the tag whenever the checkout has it.
        v0222_schema_constants = {"DISK_IDENTITY_BACKFILL_USER_VERSION": 1}

        tagged = subprocess.run(
            ["git", "show", "v0.22.2:history_service/store.py"],
            capture_output=True, text=True, check=False, cwd=self.ROOT,
        )
        if tagged.returncode == 0:
            names = {}
            for node in ast.parse(tagged.stdout).body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id.isupper():
                            names[target.id] = node.value.value
            self.assertNotIn("CURRENT_SCHEMA_VERSION", names)
            for name, value in v0222_schema_constants.items():
                self.assertEqual(names.get(name), value, name)

        released = SimpleNamespace(**v0222_schema_constants)
        self.assertFalse(hasattr(released, "CURRENT_SCHEMA_VERSION"))
        self.assertEqual(eval(lookup, {"store_module": released}), 1)
        self.assertEqual(eval(lookup, {"store_module": current_store}),
                         current_store.CURRENT_SCHEMA_VERSION)
        self.assertEqual(eval(lookup, {"store_module": SimpleNamespace()}), None)

        predecessor = {"user_version": 1, "current_schema": eval(lookup, {"store_module": released})}
        candidate = {"user_version": 1, "current_schema": current_store.CURRENT_SCHEMA_VERSION}
        self.assertEqual(smoke.schema_evidence(predecessor, candidate)["transition"], "none")

    def test_operator_index_count_matches_real_released_startup(self):
        import sqlite3
        from contextlib import closing

        from history_service.store import CURRENT_SCHEMA_VERSION, HistoryStore
        from tests.test_history_released_schema_upgrades import build_released_database

        # v0.22.2's _ensure_identity_indexes adds these outside its SCHEMA.
        # Cover both the released SQL fixture and an already-started release.
        identity_indexes = {
            "idx_slot_state_disk_identity": "slot_state_current (disk_identity_key)",
            "idx_metric_samples_disk_identity":
                "metric_samples (disk_identity_key, metric_name, observed_at DESC)",
        }
        chronological_indexes = {
            "idx_slot_events_scope_chronological":
                "slot_events (system_id, enclosure_key, slot, julianday(observed_at) DESC, id DESC)",
            "idx_metric_samples_slot_chronological":
                "metric_samples (system_id, enclosure_key, slot, julianday(observed_at) DESC, id DESC)",
            "idx_metric_samples_scope_chronological":
                "metric_samples (system_id, enclosure_key, slot, metric_name, julianday(observed_at) DESC, id DESC)",
            "idx_metric_samples_disk_chronological":
                "metric_samples (disk_identity_key, julianday(observed_at) DESC, id DESC)",
            "idx_metric_samples_disk_metric_chronological":
                "metric_samples (disk_identity_key, metric_name, julianday(observed_at) DESC, id DESC)",
        }

        def state(path):
            with closing(sqlite3.connect(path)) as connection:
                return {
                    "definitions": connection.execute(
                        "SELECT type, name, tbl_name, sql FROM sqlite_master "
                        "WHERE type != 'index' ORDER BY type, name"
                    ).fetchall(),
                    "indexes": dict(connection.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
                    )),
                    "rows": {
                        table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                        for (table,) in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    },
                    "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
                    "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
                }

        guide = (self.ROOT / "wiki" / "Upgrading.md").read_text(encoding="utf-8")
        released_schema = (
            self.ROOT / "tests" / "fixtures" / "history_released_schemas" / "v0.22.2.sql"
        ).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            for already_started in (False, True):
                with self.subTest(released_identity_indexes=already_started):
                    path = Path(directory) / f"history-{already_started}.sqlite3"
                    build_released_database(path, "v0.22.2", stamp_like_release=True)
                    if already_started:
                        with closing(sqlite3.connect(path)) as connection, connection:
                            for name, target in identity_indexes.items():
                                connection.execute(f"CREATE INDEX {name} ON {target}")
                    before = state(path)

                    # No startup mocks: the constructor runs version admission,
                    # SCHEMA, identity indexes, backfill and counter synchronization.
                    HistoryStore(str(path), recover_unreadable_database=False)
                    after = state(path)
                    for key in ("definitions", "rows", "user_version", "quick_check"):
                        self.assertEqual(after[key], before[key], key)
                    self.assertEqual(after["user_version"], CURRENT_SCHEMA_VERSION)
                    self.assertEqual(after["quick_check"], "ok")
                    self.assertEqual(
                        {name: after["indexes"].get(name) for name in before["indexes"]},
                        before["indexes"],
                    )
                    added = set(after["indexes"]) - set(before["indexes"])
                    expected = dict(chronological_indexes)
                    if not already_started:
                        expected.update(identity_indexes)
                    self.assertEqual(added, set(expected))
                    for name, target in expected.items():
                        self.assertEqual(" ".join(after["indexes"][name].split()),
                                         f"CREATE INDEX {name} ON {target}")
                    added_chronological = {name for name in added if name.endswith("_chronological")}
                    self.assertEqual(added_chronological, set(chronological_indexes))
                    self.assertEqual(len(added_chronological), 5)
                    self.assertEqual(self.load_smoke().schema_evidence(
                        {"user_version": before["user_version"], "current_schema": 1},
                        {"user_version": after["user_version"], "current_schema": CURRENT_SCHEMA_VERSION},
                    )["transition"], "none")

                    HistoryStore(str(path), recover_unreadable_database=False)
                    self.assertEqual(state(path), after, "restart must preserve the complete state")
                    with closing(sqlite3.connect(path)) as connection:
                        connection.executescript(released_schema)
                    self.assertEqual(state(path), after, "released SQL must still accept the database")
                    self.assertIn("Startup adds five chronological indexes", guide,
                                  f"real startup added {sorted(added_chronological)}")

    def test_upgrade_evidence_and_docs_do_not_claim_an_equal_schema_was_migrated(self):
        from history_service.store import CURRENT_SCHEMA_VERSION

        # The real-constructor test above qualifies the complete startup delta;
        # executing SCHEMA alone omits the two disk chronological indexes.
        self.assertEqual(CURRENT_SCHEMA_VERSION, 1, "update the qualification when the logical schema version changes")
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
        self.assertIn("Executing the schema SQL alone does not qualify startup", matrix)
        self.assertIn("exact bytes without reindexing", matrix)
        self.assertIn("headroom on production-scale databases remain unqualified", matrix)
        self.assertIn("chronological indexes", matrix)
        self.assertIn("chronological indexes", scripts_guide)
        # Any section: #637 moves from Unreleased into its release and stays there.
        self.assertRegex(changelog, r"\(#637(?:, #\d+)*\)")


class ReadOnlyStateScenarioTests(unittest.TestCase):
    """#463: maintained driver regressions, not real-container evidence."""

    ROOT = Path(__file__).resolve().parents[1]
    load_smoke = ImageOnlyUpgradeSmokeContractTests.load_smoke

    def test_readonly_state_is_dispatched_and_invoked_in_the_existing_report_only_job(self):
        from types import SimpleNamespace

        smoke = self.load_smoke()
        self.assertIn("readonly-state", smoke.SCENARIOS)
        with mock.patch.object(smoke, "readonly_state", return_value="qualified") as scenario:
            args = SimpleNamespace(scenario="readonly-state")
            self.assertEqual(smoke.smoke(args), "qualified")
            scenario.assert_called_once_with(args)
        workflow = yaml.safe_load((self.ROOT / ".github/workflows/ci.yml").read_text())
        job = workflow["jobs"]["image-upgrade-scenarios"]
        runs = [step["run"] for step in job["steps"] if "--scenario readonly-state" in step.get("run", "")]
        self.assertEqual(len(runs), 1)
        for argument in (
            '--root "$RUNNER_TEMP/image-upgrade-readonly"',
            '--compose-fixture tests/fixtures/compose/v0.22.2.yml',
            '--config-fixture tests/fixtures/ci-smoke-config.yaml',
            '--previous-image "$PREVIOUS_IMAGE"',
            '--previous-version "$PREVIOUS_VERSION"',
            '--candidate-image "$CANDIDATE_IMAGE"',
            '--candidate-revision "$GITHUB_SHA"',
        ):
            self.assertIn(argument, runs[0])
        self.assertEqual(job["timeout-minutes"], 30)
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertNotIn("--scenario readonly-state", str(workflow["jobs"]["image-upgrade-smoke"]))

    def scenario_args(self, root):
        from types import SimpleNamespace
        return SimpleNamespace(root=root, compose_fixture=Path("compose-fixture"),
                               config_fixture=Path("config-fixture"), previous_image="previous",
                               previous_version="1", candidate_image="candidate", candidate_version="2",
                               candidate_revision="revision", scenario="readonly-state")

    def test_readonly_lifecycle_preserves_bytes_and_recovers_only_after_explicit_remount(self):
        self.exercise_lifecycle()

    def test_readonly_lifecycle_refuses_lost_replaced_or_fresh_history(self):
        for changed in ({}, {"history.db": [0, "fresh"]}, {"history.db.broken-1": [20, "original"]}):
            with self.subTest(changed=changed):
                self.exercise_lifecycle(changed=changed)

    def test_readonly_lifecycle_refuses_a_replaced_container_even_without_a_restart_count(self):
        self.exercise_lifecycle(replaced=True)

    def exercise_lifecycle(self, *, changed=None, replaced=False):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.scenario_args(root)
            deployment = mock.Mock(spec=smoke.Deployment)
            deployment.files = ()
            deployment.root = root
            before = {"history.db": [20, "original"]}
            ui = {"mappings": [["shelf-a", 1, "SYNTH-0001", "synthetic"]]}
            history = {"counts": {"slot_state_current": 3, "slot_events": 3, "metric_samples": 12},
                       "integrity": "ok"}
            deployment.run_python.return_value = before

            def read(service, script):
                if script == smoke.READ_HISTORY_TREE:
                    return before if changed is None else changed
                if script == smoke.READONLY_WRITE_PROBE:
                    return {"errno": 30}
                return ui if service == "enclosure-ui" else history

            deployment.exec_python.side_effect = read
            with (
                mock.patch.object(smoke, "Deployment", return_value=deployment),
                mock.patch.object(smoke, "prepare_root"),
                mock.patch.object(smoke, "seed_previous_release", return_value=(ui, history, {1: 4, 2: 4, 3: 4})),
                mock.patch.object(smoke, "sha256", return_value="config"),
                mock.patch.object(smoke, "wait_for_history_liveness"),
                mock.patch.object(smoke, "check_readonly_runtime", return_value={"started": "unchanged"},
                                  side_effect=[{"started": "unchanged"}, {"started": "replacement"}]
                                  if replaced else None) as check,
                mock.patch.object(smoke, "check_runtime") as healthy,
                mock.patch.object(smoke, "history_api_view", return_value=smoke.expected_history_view({1: 4, 2: 4, 3: 4})),
                mock.patch.object(smoke.time, "sleep") as sleep,
            ):
                if changed is not None or replaced:
                    reason = "container identity changed" if replaced else "history bytes or entries changed"
                    with self.assertRaisesRegex(smoke.SmokeError, reason):
                        smoke.readonly_state(args)
                    healthy.assert_not_called()
                    deployment.pull_and_up.assert_not_called()
                else:
                    result = smoke.readonly_state(args)
                    self.assertIn("scenario=readonly-state", result)
                    self.assertIn("history_health=down", result)
                    self.assertIn("history_bytes=unchanged", result)
                    self.assertIn("writable_recovery=ok", result)
                    self.assertEqual(check.call_count, 11)
                    self.assertEqual(sleep.call_args_list, [mock.call(2)] * 10)
                    healthy.assert_called_once_with(deployment, "candidate", "2", "revision")
                    deployment.pull_and_up.assert_called_once_with()
                    self.assertEqual(deployment.files, ())
                deployment.set_image.assert_called_once_with("candidate")
                calls = deployment.compose.call_args_list
                self.assertEqual(calls[:3], [mock.call("stop", *smoke.SERVICES), mock.call("pull"),
                                             mock.call("up", "-d")])
                if changed is None and not replaced:
                    self.assertEqual(calls[3:], [mock.call("stop", *smoke.SERVICES)])

    def test_readonly_runtime_distinguishes_local_refusal_from_liveness_or_remote_degradation(self):
        import copy
        import json

        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = smoke.Deployment(root)
            args = self.scenario_args(root)
            baseline = {"State": {"Status": "running", "StartedAt": "start"}, "Image": "image-id",
                        "RestartCount": 0, "Mounts": [{"Destination": "/app/history", "Source": str(root / "history"),
                                                       "Type": "bind", "RW": False}]}
            for fault in (None, "restart", "remounted", "wrong-source", "other-image", "exited",
                          "healthy-history", "remote-degraded", "wrong-reason", "active-api", "wrong-version"):
                with self.subTest(fault=fault):
                    state = copy.deepcopy(baseline)
                    responses = {
                        f"http://127.0.0.1:{smoke.UI_PORT}/livez": (200, {"status": "ok", "version": "2"}),
                        f"http://127.0.0.1:{smoke.UI_PORT}/healthz": (200, {"status": "ok"}),
                        f"http://127.0.0.1:{smoke.HISTORY_PORT}/livez": (200, {"status": "ok", "version": "2"}),
                        f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz": (503, {
                            "status": "down", "detail": "History storage is unavailable; see the service logs.",
                            "reason": "Cannot write to /app/history (owned by uid 0, running as uid 0)."}),
                        f"http://127.0.0.1:{smoke.HISTORY_PORT}/api/history/overview": (503, {
                            "detail": "History storage is unavailable; see the service logs."}),
                    }
                    if fault == "restart":
                        state["RestartCount"] = 1
                    elif fault == "remounted":
                        state["Mounts"][0]["RW"] = True
                    elif fault == "wrong-source":
                        state["Mounts"][0]["Source"] = str(root / "fresh-history")
                    elif fault == "other-image":
                        state["Image"] = "other"
                    elif fault == "exited":
                        state["State"]["Status"] = "exited"
                    elif fault in {"healthy-history", "remote-degraded"}:
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz"] = (
                            200, {"status": "ok" if fault == "healthy-history" else "degraded"})
                    elif fault == "wrong-reason":
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz"][1]["reason"] = "other failure"
                    elif fault == "active-api":
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/api/history/overview"] = (200, {})
                    elif fault == "wrong-version":
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/livez"][1]["version"] = "1"

                    def run(command, **kwargs):
                        if command[:3] == ["docker", "image", "inspect"]:
                            return "revision" if "Labels" in command[4] else "image-id"
                        return json.dumps(state)

                    with (mock.patch.object(smoke, "run", side_effect=run),
                          mock.patch.object(deployment, "compose", return_value="container"),
                          mock.patch.object(smoke, "get_json", side_effect=lambda url, **kwargs: responses[url])):
                        if fault is None:
                            self.assertEqual(set(smoke.check_readonly_runtime(deployment, args)), set(smoke.SERVICES))
                        else:
                            with self.assertRaises(smoke.SmokeError):
                                smoke.check_readonly_runtime(deployment, args)

    def test_readonly_probes_hash_binary_bytes_and_require_erofs_not_permissions(self):
        import contextlib
        import errno
        import io
        import json

        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "history.db").write_bytes(b"\xff\x00synthetic")

            def execute(template):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(compile(template.replace("/app/history", str(root)), "probe", "exec"), {})
                return json.loads(output.getvalue())

            before = execute(smoke.READ_HISTORY_TREE)
            self.assertIn("history.db", before)
            self.assertEqual(execute(smoke.READ_HISTORY_TREE), before)
            (root / "history.db").write_bytes(b"\xfe\x00synthetic")
            self.assertNotEqual(execute(smoke.READ_HISTORY_TREE), before)
            with mock.patch("os.open", side_effect=OSError(errno.EROFS, "read-only filesystem")):
                self.assertEqual(execute(smoke.READONLY_WRITE_PROBE), {"errno": errno.EROFS})
            for number in (errno.EACCES, errno.ENOSPC):
                with mock.patch("os.open", side_effect=OSError(number, "other fault")):
                    with self.assertRaises(OSError):
                        execute(smoke.READONLY_WRITE_PROBE)
            with self.assertRaisesRegex(RuntimeError, "required history mount is writable"):
                execute(smoke.READONLY_WRITE_PROBE)
            self.assertEqual(set(root.iterdir()), {root / "history.db"})

    def test_error_bodies_are_opt_in_and_preserve_the_actual_http_refusal(self):
        import io
        import urllib.error
        from email.message import Message

        smoke = self.load_smoke()
        for read_body in (False, True):
            error = urllib.error.HTTPError("http://127.0.0.1/healthz", 503, "unavailable", Message(),
                                           io.BytesIO(b'{"status":"down","reason":"Cannot write to /app/history"}'))
            with mock.patch.object(smoke.urllib.request, "urlopen", side_effect=error):
                status, payload = smoke.get_json("http://127.0.0.1/healthz", read_error_body=read_body)
            self.assertEqual(status, 503)
            self.assertEqual(payload, {"status": "down", "reason": "Cannot write to /app/history"} if read_body else {})

    def test_readonly_startup_wait_is_bounded_and_does_not_accept_another_release(self):
        smoke = self.load_smoke()
        with (mock.patch.object(smoke, "get_json", side_effect=OSError("not listening")),
              mock.patch.object(smoke.time, "monotonic", side_effect=[0, 1, 60]),
              mock.patch.object(smoke.time, "sleep") as sleep):
            with self.assertRaisesRegex(smoke.SmokeError, "within 60 seconds"):
                smoke.wait_for_history_liveness("2")
            sleep.assert_called_once_with(1)
        with mock.patch.object(smoke, "get_json", return_value=(200, {"status": "ok", "version": "1"})):
            with self.assertRaisesRegex(smoke.SmokeError, "live candidate"):
                smoke.wait_for_history_liveness("2")
        with mock.patch.object(smoke, "get_json", return_value=(200, {"status": "ok", "version": "2"})):
            smoke.wait_for_history_liveness("2")

    def test_readonly_cli_cleans_up_on_both_success_and_qualification_failure(self):
        smoke = self.load_smoke()
        for failure in (None, smoke.SmokeError("synthetic refusal contract failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "docker-compose.yml").write_text("services: {}")
                smoke.install_readonly_history_mount(smoke.Deployment(root))
                argv = ["--scenario", "readonly-state", "--root", str(root),
                        "--compose-fixture", "fixture", "--config-fixture", "config-fixture",
                        "--previous-image", "previous", "--previous-version", "1", "--candidate-image", "candidate",
                        "--candidate-version", "2", "--candidate-revision", "revision"]
                with (mock.patch.object(smoke, "smoke", side_effect=failure, return_value="synthetic result"),
                      mock.patch.object(smoke.subprocess, "run") as run,
                      mock.patch("sys.stdout"), mock.patch("sys.stderr")):
                    self.assertEqual(smoke.main(argv), 1 if failure else 0)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(commands[-2:], [
                    ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.readonly.yml",
                     "down", "--volumes", "--remove-orphans"], ["sudo", "rm", "-rf", str(root)],
                ])

    def test_readonly_mount_is_the_only_fault_and_cleanup_uses_its_compose_chain(self):
        smoke = self.load_smoke()
        self.assertTrue(callable(getattr(smoke, "readonly_state", None)),
                        "the runtime scenario must exist, not only a static validator")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docker-compose.yml").write_text("services: {}")
            smoke.install_readonly_history_mount(smoke.Deployment(root))
            overlay = yaml.safe_load((root / "docker-compose.readonly.yml").read_text())
            self.assertEqual(overlay, {"services": {"enclosure-history": {
                "volumes": ["./history:/app/history:ro"],
            }}})
            with mock.patch.object(smoke.subprocess, "run") as run:
                smoke.cleanup(root)
            self.assertEqual(run.call_args_list[0].args[0], [
                "docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.readonly.yml",
                "down", "--volumes", "--remove-orphans",
            ])
            self.assertEqual(run.call_args_list[1].args[0], ["sudo", "rm", "-rf", str(root)])


class EnospcStateScenarioTests(unittest.TestCase):
    """#463: maintained driver regressions, not real-container evidence."""

    ROOT = Path(__file__).resolve().parents[1]
    load_smoke = ImageOnlyUpgradeSmokeContractTests.load_smoke

    def test_enospc_state_is_dispatched_and_invoked_in_the_existing_report_only_job(self):
        from types import SimpleNamespace

        smoke = self.load_smoke()
        self.assertIn("enospc-state", smoke.SCENARIOS)
        with mock.patch.object(smoke, "enospc_state", return_value="qualified") as scenario:
            args = SimpleNamespace(scenario="enospc-state")
            self.assertEqual(smoke.smoke(args), "qualified")
            scenario.assert_called_once_with(args)
        workflow = yaml.safe_load((self.ROOT / ".github/workflows/ci.yml").read_text())
        job = workflow["jobs"]["image-upgrade-scenarios"]
        runs = [step["run"] for step in job["steps"] if "--scenario enospc-state" in step.get("run", "")]
        self.assertEqual(len(runs), 1)
        for argument in (
            '--root "$RUNNER_TEMP/image-upgrade-enospc"',
            '--compose-fixture tests/fixtures/compose/v0.22.2.yml',
            '--config-fixture tests/fixtures/ci-smoke-config.yaml',
            '--previous-image "$PREVIOUS_IMAGE"',
            '--previous-version "$PREVIOUS_VERSION"',
            '--candidate-image "$CANDIDATE_IMAGE"',
            '--candidate-revision "$GITHUB_SHA"',
        ):
            self.assertIn(argument, runs[0])
        self.assertEqual(job["timeout-minutes"], 30)
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertNotIn("--scenario enospc-state", str(workflow["jobs"]["image-upgrade-smoke"]))

    def scenario_args(self, root):
        from types import SimpleNamespace
        return SimpleNamespace(root=root, compose_fixture=Path("compose-fixture"),
                               config_fixture=Path("config-fixture"), previous_image="previous",
                               previous_version="1", candidate_image="candidate", candidate_version="2",
                               candidate_revision="revision", scenario="enospc-state")

    def test_enospc_lifecycle_preserves_bytes_and_recovers_only_after_explicit_freeing(self):
        self.exercise_lifecycle()

    def test_enospc_lifecycle_refuses_lost_replaced_or_fresh_history(self):
        for changed in ({}, {"history.db": [0, "fresh"]}, {"history.db.broken-1": [20, "original"]}):
            with self.subTest(changed=changed):
                self.exercise_lifecycle(changed=changed)

    def test_enospc_lifecycle_refuses_a_replaced_container_even_without_a_restart_count(self):
        self.exercise_lifecycle(replaced=True)

    def test_enospc_recovery_rejects_invalid_integrity_or_lost_counts(self):
        for recovered in ({"integrity": "corrupt", "counts": {
                "slot_state_current": 3, "slot_events": 3, "metric_samples": 12}},
                {"integrity": "ok", "counts": {}}):
            with self.subTest(recovered=recovered):
                self.exercise_lifecycle(recovered=recovered)

    def test_enospc_startup_wait_has_fault_specific_bounded_diagnostics(self):
        smoke = self.load_smoke()
        with (mock.patch.object(smoke, "get_json", side_effect=OSError("not listening")),
              mock.patch.object(smoke.time, "monotonic", side_effect=[0, 1, 60]),
              mock.patch.object(smoke.time, "sleep") as sleep):
            with self.assertRaisesRegex(smoke.SmokeError, "ENOSPC history startup.*within 60 seconds"):
                smoke.wait_for_history_liveness("2", fault="ENOSPC")
            sleep.assert_called_once_with(1)
        with mock.patch.object(smoke, "get_json", return_value=(200, {"status": "ok", "version": "1"})):
            with self.assertRaisesRegex(smoke.SmokeError, "ENOSPC history /livez.*live candidate"):
                smoke.wait_for_history_liveness("2", fault="ENOSPC")
        with mock.patch.object(smoke, "get_json", return_value=(200, {"status": "ok", "version": "2"})):
            smoke.wait_for_history_liveness("2", fault="ENOSPC")

    def exercise_lifecycle(self, *, changed=None, replaced=False, recovered=None):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.scenario_args(root)
            deployment = mock.Mock(spec=smoke.Deployment)
            deployment.files = ()
            deployment.root = root
            sequence = mock.Mock()
            sequence.attach_mock(deployment.run_python, "run_python")
            sequence.attach_mock(deployment.pull_and_up, "pull_and_up")
            before = {"history.db": [20, "original"]}
            ui = {"mappings": [["shelf-a", 1, "SYNTH-0001", "synthetic"]]}
            history = {"counts": {"slot_state_current": 3, "slot_events": 3, "metric_samples": 12},
                       "integrity": "ok"}
            deployment.run_python.side_effect = lambda service, script: {
                smoke.READ_HISTORY_TREE: before,
                smoke.FILL_ENOSPC_HISTORY: {"errno": 28},
                smoke.CLEAR_ENOSPC_HISTORY: {"ok": True},
            }[script]
            written = False
            recovered_history = history if recovered is None else recovered

            def read(service, script):
                nonlocal written
                if script == smoke.READ_HISTORY_TREE:
                    return before if changed is None else changed
                if script == smoke.ENOSPC_WRITE_PROBE:
                    return {"errno": 28}
                if script == smoke.seed_script(smoke.SEED_HISTORY, (4,), metrics_per_slot=1):
                    written = True
                    return {"ok": True}
                if script == smoke.READ_HISTORY:
                    return ({"integrity": "ok", "counts": {
                        "slot_state_current": 4, "slot_events": 4, "metric_samples": 13,
                    }} if written else recovered_history)
                self.assertEqual((service, script), ("enclosure-ui", smoke.READ_UI))
                return ui

            deployment.exec_python.side_effect = read
            with (
                mock.patch.object(smoke, "Deployment", return_value=deployment),
                mock.patch.object(smoke, "prepare_root"),
                mock.patch.object(smoke, "install_enospc_history_mount") as install,
                mock.patch.object(smoke, "seed_previous_release", return_value=(ui, history, {1: 4, 2: 4, 3: 4})),
                mock.patch.object(smoke, "sha256", return_value="config"),
                mock.patch.object(smoke, "wait_for_history_liveness") as wait,
                mock.patch.object(smoke, "check_enospc_runtime", return_value={"started": "unchanged"},
                                  side_effect=[{"started": "unchanged"}, {"started": "replacement"}] + [{"started": "unchanged"}] * 9
                                  if replaced else None) as check,
                mock.patch.object(smoke, "check_runtime") as healthy,
                mock.patch.object(smoke, "history_api_view", side_effect=[
                    smoke.expected_history_view({1: 4, 2: 4, 3: 4}),
                    smoke.expected_history_view({1: 4, 2: 4, 3: 4, 4: 1})]),
                mock.patch.object(smoke.time, "sleep") as sleep,
            ):
                if changed is not None or replaced:
                    reason = "container identity changed" if replaced else "history bytes or entries changed"
                    with self.assertRaisesRegex(smoke.SmokeError, reason):
                        smoke.enospc_state(args)
                    healthy.assert_not_called()
                    deployment.pull_and_up.assert_not_called()
                elif recovered is not None:
                    with self.assertRaisesRegex(smoke.SmokeError, "writable recovery lost predecessor history"):
                        smoke.enospc_state(args)
                else:
                    result = smoke.enospc_state(args)
                    self.assertIn("scenario=enospc-state", result)
                    self.assertIn("history_health=down", result)
                    self.assertIn("history_bytes=unchanged", result)
                    self.assertIn("writable_recovery=ok", result)
                    self.assertEqual(check.call_count, 11)
                    self.assertEqual(sleep.call_args_list, [mock.call(2)] * 10)
                    install.assert_called_once_with(deployment)
                    wait.assert_called_once_with("2", fault="ENOSPC")
                    healthy.assert_called_once_with(deployment, "candidate", "2", "revision")
                    deployment.pull_and_up.assert_called_once_with()
                    self.assertEqual(deployment.files, ())
                    self.assertLess(sequence.mock_calls.index(mock.call.run_python(
                        "enclosure-history", smoke.CLEAR_ENOSPC_HISTORY)),
                        sequence.mock_calls.index(mock.call.pull_and_up()))
                deployment.set_image.assert_called_once_with("candidate")
                calls = deployment.compose.call_args_list
                self.assertEqual(calls[:3], [mock.call("stop", *smoke.SERVICES), mock.call("pull"),
                                             mock.call("up", "-d")])
                if changed is None and not replaced and recovered is None:
                    self.assertEqual(calls[3:], [mock.call("stop", *smoke.SERVICES),
                                                mock.call("rm", "-f", *smoke.SERVICES)])
                    self.assertEqual(deployment.run_python.call_args_list, [
                        mock.call("enclosure-history", smoke.FILL_ENOSPC_HISTORY),
                        mock.call("enclosure-history", smoke.READ_HISTORY_TREE),
                        mock.call("enclosure-history", smoke.CLEAR_ENOSPC_HISTORY),
                    ])
                    deployment.exec_python.assert_any_call("enclosure-history",
                        smoke.seed_script(smoke.SEED_HISTORY, (4,), metrics_per_slot=1))
                    self.assertEqual(deployment.exec_python.call_args_list[-1],
                                     mock.call("enclosure-history", smoke.READ_HISTORY))

    def test_enospc_runtime_distinguishes_local_refusal_from_liveness_or_remote_degradation(self):
        import copy
        import json
        import sqlite3
        from types import SimpleNamespace

        from history_service.startup import HistoryStartupError, open_history_store_with_retries

        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = smoke.Deployment(root)
            args = self.scenario_args(root)
            baseline = {"State": {"Status": "running", "StartedAt": "start"}, "Image": "image-id",
                        "RestartCount": 0, "Mounts": [{"Destination": "/app/history", "Source": str(root / "history"),
                                                       "Type": "bind", "RW": True}]}
            for fault in (None, "restart", "remounted", "wrong-source", "other-image", "exited",
                          "healthy-history", "remote-degraded", "wrong-reason", "active-api", "wrong-version",
                          "ownership-only", "missing-remedy", "chown-remedy", "missing-capacity-diagnosis",
                          "wrong-remedy-volume", "missing-inode-remedy", "product-inode-full"):
                with self.subTest(fault=fault):
                    state = copy.deepcopy(baseline)
                    responses = {
                        f"http://127.0.0.1:{smoke.UI_PORT}/livez": (200, {"status": "ok", "version": "2"}),
                        f"http://127.0.0.1:{smoke.UI_PORT}/healthz": (200, {"status": "ok"}),
                        f"http://127.0.0.1:{smoke.HISTORY_PORT}/livez": (200, {"status": "ok", "version": "2"}),
                        f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz": (503, {
                            "status": "down", "detail": "History storage is unavailable; see the service logs.",
                            "reason": "Cannot write to /app/history because the filesystem is full. "
                                      "On the Docker host, free space or inodes on the volume containing ./history, "
                                      "then restart the history service."}),
                        f"http://127.0.0.1:{smoke.HISTORY_PORT}/api/history/overview": (503, {
                            "detail": "History storage is unavailable; see the service logs."}),
                    }
                    if fault == "restart":
                        state["RestartCount"] = 1
                    elif fault == "remounted":
                        state["Mounts"][0]["RW"] = False
                    elif fault == "wrong-source":
                        state["Mounts"][0]["Source"] = str(root / "fresh-history")
                    elif fault == "other-image":
                        state["Image"] = "other"
                    elif fault == "exited":
                        state["State"]["Status"] = "exited"
                    elif fault in {"healthy-history", "remote-degraded"}:
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz"] = (
                            200, {"status": "ok" if fault == "healthy-history" else "degraded"})
                    elif fault == "wrong-reason":
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz"][1]["reason"] = "other failure"
                    elif fault == "ownership-only":
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz"][1]["reason"] = (
                            "Cannot write to /app/history (owned by uid 0, running as uid 10001). "
                            "On the Docker host run: sudo chown -R 10001:10001 ./history")
                    elif fault == "missing-remedy":
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz"][1]["reason"] = (
                            "Cannot write to /app/history because the filesystem is full.")
                    elif fault == "chown-remedy":
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz"][1]["reason"] += (
                            " Run sudo chown -R 10001:10001 ./history.")
                    elif fault in {"missing-capacity-diagnosis", "wrong-remedy-volume", "missing-inode-remedy"}:
                        old, new = {
                            "missing-capacity-diagnosis": ("filesystem is full", "storage is unavailable"),
                            "wrong-remedy-volume": ("./history", "./data"),
                            "missing-inode-remedy": ("free space or inodes", "free space"),
                        }[fault]
                        payload = responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz"][1]
                        payload["reason"] = payload["reason"].replace(old, new)
                    elif fault == "product-inode-full":
                        error = sqlite3.OperationalError("unable to open database file")
                        space = SimpleNamespace(f_bavail=1024, f_files=256, f_favail=0)
                        with mock.patch("app.services.storage_writability.os.statvfs", return_value=space, create=True):
                            with self.assertRaises(HistoryStartupError) as raised:
                                open_history_store_with_retries(
                                    mock.Mock(side_effect=error), directory="/app/history", attempts=1)
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/healthz"][1]["reason"] = raised.exception.reason
                    elif fault == "active-api":
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/api/history/overview"] = (200, {})
                    elif fault == "wrong-version":
                        responses[f"http://127.0.0.1:{smoke.HISTORY_PORT}/livez"][1]["version"] = "1"

                    def run(command, **kwargs):
                        if command[:3] == ["docker", "image", "inspect"]:
                            return "revision" if "Labels" in command[4] else "image-id"
                        return json.dumps(state)

                    with (mock.patch.object(smoke, "run", side_effect=run),
                          mock.patch.object(deployment, "compose", return_value="container"),
                          mock.patch.object(smoke, "get_json", side_effect=lambda url, **kwargs: responses[url])):
                        if fault in (None, "product-inode-full"):
                            self.assertEqual(set(smoke.check_enospc_runtime(deployment, args)), set(smoke.SERVICES))
                        else:
                            with self.assertRaises(smoke.SmokeError):
                                smoke.check_enospc_runtime(deployment, args)

    def test_enospc_probes_hash_binary_bytes_and_require_enospc_not_permissions_or_erofs(self):
        import contextlib
        import errno
        import io
        import json

        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "history.db").write_bytes(b"\xff\x00synthetic")

            def execute(template):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(compile(template.replace("/app/history", str(root)), "probe", "exec"), {})
                return json.loads(output.getvalue())

            before = execute(smoke.READ_HISTORY_TREE)
            self.assertIn("history.db", before)
            self.assertEqual(execute(smoke.READ_HISTORY_TREE), before)
            (root / "history.db").write_bytes(b"\xfe\x00synthetic")
            self.assertNotEqual(execute(smoke.READ_HISTORY_TREE), before)
            with mock.patch("os.open", side_effect=OSError(errno.ENOSPC, "full filesystem")):
                self.assertEqual(execute(smoke.ENOSPC_WRITE_PROBE), {"errno": errno.ENOSPC})
            for number in (errno.EACCES, errno.EROFS):
                with mock.patch("os.open", side_effect=OSError(number, "other fault")):
                    with self.assertRaises(OSError):
                        execute(smoke.ENOSPC_WRITE_PROBE)
            with self.assertRaisesRegex(RuntimeError, "required history mount has free space"):
                execute(smoke.ENOSPC_WRITE_PROBE)
            self.assertEqual(set(root.iterdir()), {root / "history.db"})

    def test_enospc_cli_cleans_up_on_both_success_and_qualification_failure(self):
        smoke = self.load_smoke()
        for failure in (None, smoke.SmokeError("synthetic refusal contract failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "docker-compose.yml").write_text("services: {}")
                with mock.patch.object(smoke, "run"):
                    smoke.install_enospc_history_mount(smoke.Deployment(root))
                argv = ["--scenario", "enospc-state", "--root", str(root),
                        "--compose-fixture", "fixture", "--config-fixture", "config-fixture",
                        "--previous-image", "previous", "--previous-version", "1", "--candidate-image", "candidate",
                        "--candidate-version", "2", "--candidate-revision", "revision"]
                with (mock.patch.object(smoke, "smoke", side_effect=failure, return_value="synthetic result"),
                      mock.patch.object(smoke.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
                      mock.patch.object(smoke.os.path, "ismount", return_value=True),
                      mock.patch("sys.stdout"), mock.patch("sys.stderr")):
                    self.assertEqual(smoke.main(argv), 1 if failure else 0)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(commands[-3:], [
                    ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.enospc.yml",
                     "down", "--volumes", "--remove-orphans"],
                    ["sudo", "umount", str(root / "history")], ["sudo", "rm", "-rf", str(root)],
                ])

    def test_enospc_fixture_is_bounded_and_cleanup_unmounts_before_removing_it(self):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docker-compose.yml").write_text("services: {}")
            deployment = smoke.Deployment(root)
            with mock.patch.object(smoke, "run") as run:
                smoke.install_enospc_history_mount(deployment)
            self.assertEqual(run.call_args_list, [mock.call([
                "sudo", "mount", "-t", "tmpfs", "-o", "size=8m,nr_inodes=256", "tmpfs", str(root / "history")])])
            self.assertEqual(deployment.files, ("docker-compose.yml", "docker-compose.enospc.yml"))
            overlay = yaml.safe_load((root / "docker-compose.enospc.yml").read_text())
            self.assertEqual(overlay, {"services": {"enclosure-history": {
                "volumes": ["./history:/app/history:rw"],
            }}})
            with (mock.patch.object(smoke.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
                  mock.patch.object(smoke.os.path, "ismount", return_value=True)):
                smoke.cleanup(root)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands, [
                ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.enospc.yml",
                 "down", "--volumes", "--remove-orphans"],
                ["sudo", "umount", str(root / "history")], ["sudo", "rm", "-rf", str(root)],
            ])
            # A failed unmount must leave the root intact instead of traversing a live mount.
            with (mock.patch.object(smoke.subprocess, "run", side_effect=[
                    mock.Mock(returncode=0), mock.Mock(returncode=1), mock.Mock(returncode=0)]) as run,
                  mock.patch.object(smoke.os.path, "ismount", return_value=True)):
                with self.assertRaises(smoke.SmokeError):
                    smoke.cleanup(root)
                self.assertEqual(run.call_count, 2)

    def test_enospc_fill_requires_a_small_filesystem_and_restores_only_its_filler(self):
        import contextlib
        import errno
        import io
        import json
        import os
        from types import SimpleNamespace

        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retained = root / "history.db"
            retained.write_bytes(b"\xff\x00synthetic")
            open_file = os.open

            def execute(script):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(compile(script.replace("/app/history", str(root)), "probe", "exec"), {})
                return json.loads(output.getvalue())

            small = SimpleNamespace(f_frsize=4096, f_blocks=2048, f_files=256)
            for oversized in (SimpleNamespace(f_frsize=4096, f_blocks=2049, f_files=256),
                              SimpleNamespace(f_frsize=4096, f_blocks=2048, f_files=257)):
                with mock.patch("os.statvfs", return_value=oversized):
                    with self.assertRaisesRegex(RuntimeError, "bounded fixture"):
                        execute(smoke.FILL_ENOSPC_HISTORY)
                self.assertEqual(list(root.iterdir()), [retained])

            def limited_open(path, *args, **kwargs):
                if len(list(root.rglob("*"))) >= 5:
                    raise OSError(errno.ENOSPC, "synthetic inode quota")
                return open_file(path, *args, **kwargs)

            with (mock.patch("os.statvfs", return_value=small),
                  mock.patch("os.open", side_effect=limited_open)):
                self.assertEqual(execute(smoke.FILL_ENOSPC_HISTORY), {"errno": errno.ENOSPC})
            self.assertGreater(len(list(root.rglob("*"))), 1)
            self.assertEqual(execute(smoke.CLEAR_ENOSPC_HISTORY), {"ok": True})
            self.assertEqual(list(root.iterdir()), [retained])
            self.assertEqual(retained.read_bytes(), b"\xff\x00synthetic")
            for number in (errno.EROFS, errno.EACCES):
                with (mock.patch("os.statvfs", return_value=small),
                      mock.patch("os.open", side_effect=OSError(number, "other fault"))):
                    with self.assertRaises(OSError):
                        execute(smoke.FILL_ENOSPC_HISTORY)
                execute(smoke.CLEAR_ENOSPC_HISTORY)
            with mock.patch("os.statvfs", return_value=small):
                with self.assertRaisesRegex(RuntimeError, "did not exhaust"):
                    execute(smoke.FILL_ENOSPC_HISTORY)
                execute(smoke.CLEAR_ENOSPC_HISTORY)


class UnavailableBackupStorageScenarioTests(unittest.TestCase):
    """#463: maintained driver regressions, not real-container evidence."""

    ROOT = Path(__file__).resolve().parents[1]
    load_smoke = ImageOnlyUpgradeSmokeContractTests.load_smoke

    def test_backup_target_readback_requires_the_recovered_copy_shipped_after_the_restore(self):
        from datetime import datetime, timedelta, timezone

        smoke = self.load_smoke()
        base = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        baseline_at = base
        refused_one_at = base + timedelta(hours=1)
        refused_two_at = base + timedelta(minutes=65)
        recovery_at = base + timedelta(minutes=70)
        # The scenario's host-side instants: the fault goes in before the
        # refused builds and is restored before the recovery build.
        injection_ts = base.timestamp() + 3599.5
        restore_ts = refused_two_at.timestamp() + 30.5

        def artifact(at):
            return "config/jbod-scheduled-backup-" + at.strftime("%Y%m%dT%H%M%SZ") + "-0123abcd.7z"

        digests = {name: "d" + str(index) for index, name in enumerate([
            artifact(baseline_at), artifact(refused_one_at), artifact(refused_two_at), artifact(recovery_at)])}
        digests["config/.scheduled-backup.lock"] = "bookkeeping"

        def digests_of(path):
            return digests["config/" + Path(path).name]

        def fixture(local_names, remote_names):
            def run_without_sudo(command, **kwargs):
                if command[:2] == ["sudo", "find"]:
                    names = remote_names if "backup-targets" in command[2] else local_names
                    return "\n".join(names) + "\n"
                raise AssertionError(command)
            return mock.patch.object(smoke, "run", side_effect=run_without_sudo)

        lock = "config/.scheduled-backup.lock"
        all_local = [artifact(baseline_at), artifact(refused_one_at), artifact(refused_two_at),
                     artifact(recovery_at), lock]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (fixture(all_local, [artifact(baseline_at)]),
                  mock.patch.object(smoke, "sha256", side_effect=digests_of)):
                # The recovery artifact missing remotely is refused: everything
                # built at/after the restore instant must ship byte-identically.
                with self.assertRaisesRegex(smoke.SmokeError, "must be shipped byte-identically"):
                    smoke.check_backup_target_readback(root, window_start=injection_ts, window_end=restore_ts)
            with (fixture(all_local, [artifact(baseline_at), artifact(recovery_at)]),
                  mock.patch.object(smoke, "sha256", side_effect=digests_of)):
                # The documented shape holds: the target is the local archive
                # minus the refusal-window artifacts, plus the recovered copy.
                shipped = smoke.check_backup_target_readback(root, window_start=injection_ts, window_end=restore_ts)
            self.assertEqual(set(shipped), {artifact(baseline_at), artifact(recovery_at)})

    def test_backup_target_status_wait_requires_a_coherent_refusal_snapshot(self):
        smoke = self.load_smoke()

        # Records exactly as the released writer persists them. Target failure
        # record: RunRecord.as_dict plus the writer's label merge (the shape
        # fork CI dumped). Class records: RunRecord.as_dict
        # (history_service/backup_scheduler/service.py:145-146) with
        # at = _iso(started) from _run_class entry (:808) - the archive BUILD
        # time, strictly BEFORE the target record's at, which _ship takes
        # inside its per-target loop (:1132) after the build.
        def target_failure_record(at):
            return {"artifact_id": None, "at": at,
                    "detail": ("ConfigurationError: Configuration error: Filesystem archive target "
                               "'smoke-nas': Filesystem archive target root must resolve through "
                               "directories only."),
                    "label": "smoke-nas", "ok": False}

        def class_record(at, detail):
            return {"at": at, "ok": True, "detail": detail, "artifact_id": "artifact-" + at[:19]}

        def snapshot(target_at, failure_record, class_at, class_detail):
            return {"schema_version": 1,
                    "classes": {"config": class_record(class_at, class_detail)},
                    "targets": {"smoke-nas": failure_record}}

        ship_at = "2026-10-24T22:43:01.541158+00:00"
        baseline_build_at = "2026-10-24T22:42:40.112000+00:00"
        refused_build_at = "2026-10-24T22:43:00.900000+00:00"
        # Intermediate write: _ship persisted the fresh target failure beside
        # the previous (baseline) class record, _record_class has not run.
        incoherent = snapshot(ship_at, target_failure_record(ship_at), baseline_build_at, None)
        # The coherent pair the scheduler ends with: the class record is the
        # refused run's OWN record, so its at predates the target record's at.
        coherent = snapshot(ship_at, target_failure_record(ship_at), refused_build_at,
                            "1 remote copy failure(s)")
        with (mock.patch.object(smoke, "read_backup_archive_status", return_value=incoherent),
              mock.patch.object(smoke.time, "monotonic", side_effect=[0, 30, 90]),
              mock.patch.object(smoke.time, "sleep") as sleep):
            # A fresh target failure beside a stale class record is the
            # intermediate write between _ship and _record_class: keep polling.
            with self.assertRaisesRegex(smoke.SmokeError, "never reported"):
                smoke.wait_for_backup_target_status(Path("/nonexistent"), want_ok=False)
            self.assertEqual(sleep.call_args_list, [mock.call(2)])
        with (mock.patch.object(smoke, "read_backup_archive_status", side_effect=[incoherent, coherent]),
              mock.patch.object(smoke.time, "sleep") as sleep):
            self.assertEqual(smoke.wait_for_backup_target_status(Path("/nonexistent"), want_ok=False), coherent)
            self.assertEqual(sleep.call_args_list, [mock.call(2)])
        with mock.patch.object(smoke, "read_backup_archive_status", return_value=coherent):
            # The first poll may already see the resolved pair: the refused
            # class record's detail shape is what makes it coherent, never an
            # at comparison against the target record it predates.
            self.assertEqual(smoke.wait_for_backup_target_status(Path("/nonexistent"), want_ok=False), coherent)

    def test_unavailable_backup_storage_is_dispatched_and_invoked_in_the_existing_report_only_job(self):
        from types import SimpleNamespace

        smoke = self.load_smoke()
        self.assertIn("unavailable-backup-storage", smoke.SCENARIOS)
        for name, script in (
            ("config_change", smoke.config_change_script("upgrade-smoke-backup-before")),
            ("journal_append", smoke.journal_append_script("upgrade-smoke-backup-before")),
        ):
            with self.subTest(script=name):
                compile(script, name, "exec")
        self.assertIn("save_mapping", smoke.CONFIG_CHANGE_PROBE)
        self.assertIn('journal.append("mapping.save", SUBJECT)', smoke.BACKUP_JOURNAL_APPEND)
        # Every sibling dispatch target answers too, so routing this scenario to
        # one of them fails on the returned value rather than on a missing argument.
        siblings = ("enospc_state", "readonly_state", "interrupted_migration", "segmented_catalog_upgrade",
                    "upgrade_and_rollback")
        with contextlib.ExitStack() as stack:
            for sibling in siblings:
                stack.enter_context(mock.patch.object(smoke, sibling, return_value=f"routed to {sibling}"))
            scenario = stack.enter_context(
                mock.patch.object(smoke, "unavailable_backup_storage", return_value="qualified"))
            args = SimpleNamespace(scenario="unavailable-backup-storage")
            self.assertEqual(smoke.smoke(args), "qualified")
            scenario.assert_called_once_with(args)
        workflow = yaml.safe_load((self.ROOT / ".github/workflows/ci.yml").read_text())
        job = workflow["jobs"]["image-upgrade-scenarios"]
        runs = [step["run"] for step in job["steps"] if "--scenario unavailable-backup-storage" in step.get("run", "")]
        self.assertEqual(len(runs), 1)
        for argument in (
            '--root "$RUNNER_TEMP/image-upgrade-backup"',
            '--compose-fixture tests/fixtures/compose/v0.22.2.yml',
            '--config-fixture tests/fixtures/ci-smoke-config.yaml',
            '--previous-image "$PREVIOUS_IMAGE"',
            '--previous-version "$PREVIOUS_VERSION"',
            '--candidate-image "$CANDIDATE_IMAGE"',
            '--candidate-revision "$GITHUB_SHA"',
        ):
            self.assertIn(argument, runs[0])
        self.assertEqual(job["timeout-minutes"], 30)
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertNotIn("--scenario unavailable-backup-storage", str(workflow["jobs"]["image-upgrade-smoke"]))

    def scenario_args(self, root):
        from types import SimpleNamespace
        return SimpleNamespace(root=root, compose_fixture=Path("compose-fixture"),
                               config_fixture=Path("config-fixture"), previous_image="previous",
                               previous_version="1", candidate_image="candidate", candidate_version="2",
                               candidate_revision="revision", scenario="unavailable-backup-storage")

    def test_unavailable_backup_storage_lifecycle_preserves_bytes_and_recovers_only_after_explicit_removal(self):
        self.exercise_lifecycle()

    def test_unavailable_backup_storage_lifecycle_refuses_lost_replaced_or_fresh_history(self):
        for changed in ({}, {"history.db": [0, "fresh"]}, {"history.db.broken-1": [20, "original"]}):
            with self.subTest(changed=changed):
                self.exercise_lifecycle(changed=changed)

    def test_unavailable_backup_storage_lifecycle_refuses_a_replaced_scheduler_container(self):
        self.exercise_lifecycle(replaced=True)

    def test_unavailable_backup_storage_refuses_an_unisolated_target_failure(self):
        for refusal_detail in (None, "unrelated failure"):
            with self.subTest(refusal_detail=refusal_detail):
                self.exercise_lifecycle(refusal_detail=refusal_detail)

    def test_unavailable_backup_storage_lifecycle_refuses_drifted_config_bytes(self):
        self.exercise_lifecycle(config_drift=True)

    def test_unavailable_backup_storage_history_guard_does_not_depend_on_an_idle_collector(self):
        # The released history service writes on its own clock while it runs:
        # _run_loop starts a background pass after startup_grace_seconds (20 s,
        # history_service/config.py:74) and repeats it on the poll or failure
        # backoff interval (history_service/collector.py:888-1006, config.py:67
        # and :71), and a pass may run retention (collector.py:630, :1232).
        # Model that writer as landing at any step while the service is up.
        self.exercise_lifecycle(history_writer="collector")

    def test_unavailable_backup_storage_history_guard_still_catches_a_write_during_the_refusal(self):
        # A write that does not come from the history service (the scheduler
        # mounts ./history read-write) must still fail the unchanged-bytes guard.
        self.exercise_lifecycle(history_writer="other")

    def exercise_lifecycle(self, *, changed=None, replaced=False, refusal_detail="1 remote copy failure(s)",
                           config_drift=False, history_writer=None):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.scenario_args(root)
            deployment = mock.Mock(spec=smoke.Deployment)
            deployment.files = ()
            deployment.root = root
            before = {"history.db": [20, "original"], "segments": [4, "sidecar"]}
            ui = {"mappings": [["shelf-a", 1, "SYNTH-0001", "synthetic"]]}
            history = {"counts": {"slot_state_current": 3, "slot_events": 3, "metric_samples": 12},
                       "integrity": "ok"}
            refused = {"schema_version": 1, "classes": {
                "config": {"at": "t", "ok": True, "detail": refusal_detail, "artifact_id": "artifact-during"}},
                "targets": {"smoke-nas": {"at": "2026-01-01T00:01:00+00:00", "ok": False, "detail": (
                    "ConfigurationError: Configuration error: Filesystem archive target 'smoke-nas': "
                    "Filesystem archive target root must resolve through directories only.")}}}
            recovered_status = {"schema_version": 1, "classes": {}, "targets": {
                "smoke-nas": {"at": "2026-01-01T00:02:00+00:00", "ok": True, "detail": None,
                              "artifact_id": "artifact-after"}}}
            shipped = {"schema_version": 1, "classes": {}, "targets": {
                "smoke-nas": {"at": "2026-01-01T00:00:00+00:00", "ok": True, "detail": None,
                              "artifact_id": "artifact-before"}}}
            identity = {"enclosure-backup-scheduler": ["container", "start"]}
            replacement = {"enclosure-backup-scheduler": ["container-2", "start-2"]}
            tree_reads = []
            ui_reads = []
            sha_calls = []
            order = []
            history_service = {"running": False, "writes": 0}

            def history_writer_step():
                # "collector": only the live history service writes; "other":
                # a writer outside it writes whether or not the service runs.
                if history_writer == "other" or (history_writer == "collector" and history_service["running"]):
                    history_service["writes"] += 1

            def history_tree():
                if not history_service["writes"]:
                    return before
                return {**before, "history.db": [20 + history_service["writes"], "written"]}

            def compose(*command, **kwargs):
                services = [part for part in command[1:] if not part.startswith("-")]
                if command[0] == "stop" and (not services or "enclosure-history" in services):
                    history_service["running"] = False
                elif command[0] in ("up", "start", "restart") and (
                        not services or "enclosure-history" in services):
                    history_service["running"] = True
                return ""

            def pull_and_up():
                history_service["running"] = True

            deployment.compose.side_effect = compose
            deployment.pull_and_up.side_effect = pull_and_up

            def sha_digest(path):
                sha_calls.append(path)
                # First call: the seeded capture. Later calls: drifted only
                # when this entry drives the config-bytes refusal branch.
                if len(sha_calls) == 1 or not config_drift:
                    return "digest-seeded"
                return "digest-drifted"

            def run_python(service, script):
                if script == smoke.READ_HISTORY_TREE:
                    tree_reads.append(script)
                    # A live writer may land just before and just after any read.
                    history_writer_step()
                    if len(tree_reads) == 1 or changed is None:
                        tree = history_tree()
                    else:
                        tree = changed
                    history_writer_step()
                    return tree
                raise AssertionError(f"unexpected one-off probe: {script[:60]!r}")

            def read(service, script):
                history_writer_step()
                if script == smoke.READ_HISTORY:
                    return history
                if script == smoke.READ_UI:
                    ui_reads.append(script)
                    notes = ("upgrade-smoke-backup-during" if len(ui_reads) == 1
                             else "upgrade-smoke-backup-after")
                    return {"mappings": ui["mappings"] + [["shelf-a", 4, "SYNTH-0004", notes]]}
                if "save_mapping" in script:
                    return {"ok": True}
                if "ChangeJournal" in script:
                    return {"change_id": "chg_synthetic"}
                raise AssertionError(f"unexpected probe: {service} {script[:60]!r}")

            deployment.run_python.side_effect = run_python
            deployment.exec_python.side_effect = read

            def wait(root_arg, *, want_ok, after=""):
                if want_ok and not after:
                    return shipped
                if not want_ok:
                    return refused
                return recovered_status

            with (
                mock.patch.object(smoke, "Deployment", return_value=deployment),
                mock.patch.object(smoke, "prepare_root"),
                mock.patch.object(smoke, "install_backup_secrets") as bootstrap,
                mock.patch.object(smoke, "install_backup_scheduler_with_target") as install,
                mock.patch.object(smoke, "seed_previous_release",
                                  side_effect=lambda *args, **kwargs:
                                      order.append("seed") or (ui, history, {1: 4, 2: 4, 3: 4})),
                mock.patch.object(smoke, "sha256", side_effect=sha_digest),
                mock.patch.object(smoke, "wait_for_backup_target_status", side_effect=wait) as wait_mock,
                mock.patch.object(smoke, "make_backup_target_unavailable", return_value=1000.0) as fault,
                mock.patch.object(smoke, "make_backup_target_available", return_value=2000.0) as restore,
                mock.patch.object(smoke, "check_backup_target_readback", return_value={"config/after.7z": "d"}) as readback,
                mock.patch.object(smoke, "check_runtime") as healthy,
                mock.patch.object(smoke, "history_api_view",
                                  return_value=smoke.expected_history_view({1: 4, 2: 4, 3: 4})),
                mock.patch.object(smoke.time, "sleep", side_effect=lambda seconds: history_writer_step()) as sleep,
            ):
                bootstrap.side_effect = lambda *args, **kwargs: order.append("bootstrap") or mock.DEFAULT
                if replaced:
                    # The probe runs once for the identity and once per bounded
                    # observation step; every later reading is the replacement,
                    # so the loop's equality against the first reading fails.
                    readings = [identity] + [replacement] * 10
                else:
                    readings = [identity] * 11
                with mock.patch.object(smoke, "check_backup_scheduler_runtime", side_effect=readings) as probe:
                    if replaced:
                        with self.assertRaisesRegex(smoke.SmokeError, "scheduler identity changed"):
                            smoke.unavailable_backup_storage(args)
                        self.assertEqual(fault.call_count, 1)
                        restore.assert_not_called()
                        readback.assert_not_called()
                        self.assertEqual(wait_mock.call_count, 2)
                        self.assertEqual(healthy.call_count, 1)
                    elif changed is not None or history_writer == "other":
                        with self.assertRaisesRegex(smoke.SmokeError, "history bytes or entries changed"):
                            smoke.unavailable_backup_storage(args)
                        self.assertEqual(fault.call_count, 1)
                        restore.assert_not_called()
                        readback.assert_not_called()
                        self.assertEqual(wait_mock.call_count, 2)
                        self.assertEqual(healthy.call_count, 1)
                    elif refusal_detail != "1 remote copy failure(s)":
                        with self.assertRaisesRegex(smoke.SmokeError, "isolate the remote copy failures"):
                            smoke.unavailable_backup_storage(args)
                        self.assertEqual(fault.call_count, 1)
                        restore.assert_not_called()
                        readback.assert_not_called()
                        self.assertEqual(wait_mock.call_count, 2)
                        self.assertEqual(healthy.call_count, 1)
                    elif config_drift:
                        with self.assertRaisesRegex(smoke.SmokeError, "config changed during the backup-target refusal"):
                            smoke.unavailable_backup_storage(args)
                        self.assertEqual(fault.call_count, 1)
                        restore.assert_not_called()
                        readback.assert_not_called()
                        self.assertEqual(wait_mock.call_count, 2)
                        self.assertEqual(healthy.call_count, 1)
                        self.assertEqual([path.resolve() for path in sha_calls],
                                         [root.resolve() / "config/config.yaml"] * 2,
                                         "the invariant limb must compare the seeded config digest, not a stub")
                    else:
                        try:
                            result = smoke.unavailable_backup_storage(args)
                        except smoke.SmokeError as exc:
                            self.fail(f"the qualification refused a healthy run: {exc}")
                        self.assertTrue(history_service["running"],
                                        "the history service must be running again before the recovery checks")
                        self.assertIn("scenario=unavailable-backup-storage", result)
                        self.assertIn("target_refusals=1", result)
                        self.assertIn("target_refusal=ConfigurationError", result)
                        self.assertIn("target_recovery=ok", result)
                        self.assertIn("backup_local_kept=ok", result)
                        self.assertIn("target_readback=ok", result)
                        self.assertIn("history_bytes=unchanged", result)
                        self.assertEqual(probe.call_count, 11)
                        self.assertEqual(sleep.call_args_list, [mock.call(2)] * 10)
                        install.assert_called_once_with(deployment)
                        fault.assert_called_once_with(root)
                        restore.assert_called_once_with(root)
                        readback.assert_called_once_with(
                            root,
                            window_start=1000.0,
                            window_end=2000.0,
                        )
                        self.assertEqual(healthy.call_count, 2)
                        for call in healthy.call_args_list:
                            self.assertEqual(call.args, (deployment, "candidate", "2", "revision"))
                        deployment.pull_and_up.assert_called_once_with()
                # The sudo bootstrap is CI-proven only: mocked here (as the
                # enospc mount is), called once with the deployment root, and
                # strictly before seeding, so the pre-seed ordering stays
                # pinned executably in the harness as well as by source text.
                bootstrap.assert_called_once_with(root)
                self.assertEqual(order, ["bootstrap", "seed"])
                self.assertFalse((root / "config" / "backup-secrets").exists(),
                                 "the harness must not run the real sudo bootstrap inside the temporary root")
                waits = [(call.kwargs["want_ok"], call.kwargs.get("after", ""))
                         for call in wait_mock.call_args_list]
                self.assertEqual(waits, [(True, ""), (False, "2026-01-01T00:00:00+00:00"),
                                         (True, "2026-01-01T00:01:00+00:00")][:len(waits)])
                deployment.set_image.assert_called_once_with("candidate")
                # The history service is stopped for the snapshot and the
                # refusal window, then started again unless the run failed first.
                failed_before_restart = (replaced or changed is not None or history_writer == "other"
                                         or refusal_detail != "1 remote copy failure(s)")
                self.assertEqual(deployment.compose.call_args_list,
                                 [mock.call("stop", *smoke.SERVICES), mock.call("pull"),
                                  mock.call("stop", "enclosure-history")]
                                 + ([] if failed_before_restart else [
                                     mock.call("up", "-d", "--wait", "--wait-timeout", "300", "enclosure-history",
                                               timeout=420)]))
                journal_notes = [call.args[1] for call in deployment.exec_python.call_args_list
                                 if "ChangeJournal" in call.args[1]]
                self.assertEqual(journal_notes, [smoke.journal_append_script(note)
                                                 for note in ("upgrade-smoke-backup-before",
                                                              "upgrade-smoke-backup-during",
                                                              "upgrade-smoke-backup-after")][:len(journal_notes)])

    def test_backup_target_status_wait_is_bounded_and_refuses_stale_or_missing_records(self):
        import itertools

        smoke = self.load_smoke()
        stale = {"schema_version": 1, "classes": {}, "targets": {
            "smoke-nas": {"at": "2026-01-01T00:00:00+00:00", "ok": True, "detail": None, "artifact_id": "a"}}}
        fresh = {"schema_version": 1, "classes": {}, "targets": {
            "smoke-nas": {"at": "2026-01-01T00:09:00+00:00", "ok": True, "detail": None, "artifact_id": "b"}}}
        with (mock.patch.object(smoke, "read_backup_archive_status", side_effect=[stale, fresh]),
              mock.patch.object(smoke.time, "sleep") as sleep):
            self.assertEqual(smoke.wait_for_backup_target_status(
                Path("/nonexistent"), want_ok=True, after="2026-01-01T00:01:00+00:00"), fresh)
            self.assertEqual(sleep.call_args_list, [mock.call(2)])
        with (mock.patch.object(smoke, "read_backup_archive_status", return_value=stale),
              mock.patch.object(smoke.time, "monotonic", side_effect=itertools.count(0, 30)),
              mock.patch.object(smoke.time, "sleep")):
            with self.assertRaisesRegex(smoke.SmokeError, "within 90 seconds"):
                smoke.wait_for_backup_target_status(Path("/nonexistent"), want_ok=False)
        with (mock.patch.object(smoke, "read_backup_archive_status", side_effect=smoke.SmokeError("missing")),
              mock.patch.object(smoke.time, "monotonic", side_effect=itertools.count(0, 30)),
              mock.patch.object(smoke.time, "sleep") as sleep):
            with self.assertRaisesRegex(smoke.SmokeError, "within 90 seconds"):
                smoke.wait_for_backup_target_status(Path("/nonexistent"), want_ok=True)
            self.assertEqual(sleep.call_args_list, [mock.call(2)] * 2)

    def test_backup_target_status_wait_survives_malformed_reads_until_the_deadline(self):
        import itertools
        import json

        smoke = self.load_smoke()
        torn = json.JSONDecodeError("Expecting value", "backup-archive.json", 0)
        with (mock.patch.object(smoke, "read_backup_archive_status", side_effect=torn),
              mock.patch.object(smoke.time, "monotonic", side_effect=itertools.count(0, 30)),
              mock.patch.object(smoke.time, "sleep") as sleep):
            # A malformed or torn read is "no fresh status yet", never a failed
            # qualification: the poll keeps going until the bounded deadline,
            # whose message names the last record seen.
            with self.assertRaisesRegex(smoke.SmokeError, "never reported"):
                smoke.wait_for_backup_target_status(Path("/nonexistent"), want_ok=True)
            self.assertEqual(sleep.call_args_list, [mock.call(2)] * 2)

    def test_backup_archive_status_reader_refuses_malformed_payloads(self):
        import json

        smoke = self.load_smoke()
        good = {"schema_version": 1, "classes": {}, "targets": {}}
        for payload in ({}, [], "not an object",
                        {"schema_version": 2, "classes": {}, "targets": {}},
                        {"schema_version": 1, "classes": {}},
                        {"schema_version": 1, "targets": {}},
                        {"schema_version": 1, "classes": [], "targets": {}},
                        {"schema_version": 1, "classes": {}, "targets": "smoke-nas"}):
            with self.subTest(payload=payload):
                with mock.patch.object(smoke, "run", return_value=json.dumps(payload)):
                    with self.assertRaisesRegex(smoke.SmokeError, "unexpected shape"):
                        smoke.read_backup_archive_status(Path("/nonexistent"))
        with mock.patch.object(smoke, "run", return_value=json.dumps(good)):
            self.assertEqual(smoke.read_backup_archive_status(Path("/nonexistent")), good)

    def test_backup_target_refusal_pins_the_scheduler_diagnosis_and_rejects_sibling_faults(self):
        smoke = self.load_smoke()
        detail = ("ConfigurationError: Configuration error: Filesystem archive target 'smoke-nas': "
                  "Filesystem archive target root must resolve through directories only.")

        def status_for(target_record, class_record=None):
            return {"schema_version": 1,
                    "classes": {"config": class_record if class_record is not None else {
                        "at": "t", "ok": True, "detail": "1 remote copy failure(s)", "artifact_id": "a"}},
                    "targets": {"smoke-nas": target_record}}

        smoke.check_backup_target_refusal(status_for({"at": "t", "ok": False, "detail": detail}))
        # The per-run count is the scheduler's own bookkeeping; any count and
        # the multi-failure plural form must both pass the shape pin.
        smoke.check_backup_target_refusal(status_for(
            {"at": "t", "ok": False, "detail": detail},
            class_record={"at": "t", "ok": True, "detail": "2 remote copy failure(s)", "artifact_id": "a"}))
        sibling_contamination = detail.replace(
            "must resolve through directories only",
            "must resolve through directories only; the filesystem is full")
        mutants = (
            status_for({"at": "t", "ok": True, "detail": None}),
            status_for({"at": "t", "ok": False, "detail": None}),
            status_for({"at": "t", "ok": False, "detail": detail.replace("ConfigurationError: ", "")}),
            status_for({"at": "t", "ok": False, "detail": detail.replace(
                "Filesystem archive target 'smoke-nas': ", "")}),
            status_for({"at": "t", "ok": False, "detail": detail.replace(
                "must resolve through directories only", "something else happened")}),
            status_for({"at": "t", "ok": False, "detail": sibling_contamination}),
            status_for({"at": "t", "ok": False, "detail": detail}, class_record={
                "at": "t", "ok": False, "detail": "1 remote copy failure(s)", "artifact_id": "a"}),
            status_for({"at": "t", "ok": False, "detail": detail}, class_record={
                "at": "t", "ok": True, "detail": None, "artifact_id": None}),
            status_for({"at": "t", "ok": False, "detail": detail}, class_record={
                "at": "t", "ok": True, "detail": "1 remote copy failures", "artifact_id": "a"}),
            status_for({"at": "t", "ok": False, "detail": detail}, class_record={
                "at": "t", "ok": True, "detail": "unrelated failure", "artifact_id": "a"}),
            status_for({"at": "t", "ok": False, "detail": detail}, class_record={
                "at": "t", "ok": True, "detail": "remote copy failure(s)", "artifact_id": "a"}),
        )
        for mutant in mutants:
            with self.subTest(mutant=repr(mutant["targets"]["smoke-nas"])[:100]):
                with self.assertRaises(smoke.SmokeError):
                    smoke.check_backup_target_refusal(mutant)
        recovered = smoke.check_backup_target_recovery({"schema_version": 1, "classes": {}, "targets": {
            "smoke-nas": {"at": "t", "ok": True, "detail": None, "artifact_id": "b"}}})
        self.assertEqual(recovered["artifact_id"], "b")
        for mutant in (
            {"schema_version": 1, "classes": {}, "targets": {
                "smoke-nas": {"at": "t", "ok": False, "detail": detail}}},
            {"schema_version": 1, "classes": {}, "targets": {
                "smoke-nas": {"at": "t", "ok": True, "detail": None, "artifact_id": None}}},
            {"schema_version": 1, "classes": {}, "targets": {
                "smoke-nas": {"at": "t", "ok": True, "detail": "leftover", "artifact_id": "b"}}},
        ):
            with self.subTest(mutant=repr(mutant)[:100]):
                with self.assertRaises(smoke.SmokeError):
                    smoke.check_backup_target_recovery(mutant)

    def test_backup_scheduler_runtime_probe_refuses_restarts_or_replacement(self):
        import copy
        import json

        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = smoke.Deployment(root)
            baseline = {"State": {"Status": "running", "StartedAt": "start"}, "Image": "image-id",
                        "RestartCount": 0}
            for fault in (None, "restarted", "exited"):
                with self.subTest(fault=fault):
                    state = copy.deepcopy(baseline)
                    if fault == "restarted":
                        state["RestartCount"] = 1
                    elif fault == "exited":
                        state["State"]["Status"] = "exited"

                    def run(command, **kwargs):
                        return json.dumps(state)

                    with (mock.patch.object(smoke, "run", side_effect=run),
                          mock.patch.object(deployment, "compose", return_value="container")):
                        if fault is None:
                            self.assertEqual(smoke.check_backup_scheduler_runtime(deployment),
                                             {"enclosure-backup-scheduler": ["container", "start"]})
                        else:
                            with self.assertRaises(smoke.SmokeError):
                                smoke.check_backup_scheduler_runtime(deployment)

    def test_readback_sha256_is_binary_safe_and_hashes_exact_bytes(self):
        import hashlib

        smoke = self.load_smoke()
        payload = bytes(range(256))
        with (mock.patch.object(smoke, "run", return_value=payload),
              mock.patch.object(smoke, "run_bytes", return_value=payload, create=True)):
            try:
                digest = smoke.sha256(Path("/nonexistent"))
            except (UnicodeDecodeError, AttributeError) as error:
                # The pre-fix seam decoded or re-encoded the bytes as text;
                # either failure mode is the same binary-safety defect.
                self.fail(f"sha256 is not binary-safe: {error!r}")
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())

    def test_backup_target_readback_attribute_missing_artifacts_to_the_refusal_window(self):
        from datetime import datetime, timedelta, timezone

        smoke = self.load_smoke()
        base = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        baseline_at = base
        refused_one_at = base + timedelta(hours=1)
        refused_two_at = base + timedelta(minutes=65)
        recovery_at = base + timedelta(minutes=70)

        def artifact(at):
            return "config/jbod-scheduled-backup-" + at.strftime("%Y%m%dT%H%M%SZ") + "-0123abcd.7z"

        local_names = [artifact(baseline_at), artifact(refused_one_at), artifact(refused_two_at),
                       artifact(recovery_at), "config/.scheduled-backup.lock"]
        remote_names = [artifact(baseline_at), artifact(recovery_at)]
        digests = {name: "d" + name[-13] for name in local_names + remote_names}

        def find_listing(sub_root):
            return "\n".join(remote_names if "backup-targets" in sub_root else local_names) + "\n"

        def run_without_sudo(command, **kwargs):
            if command[:2] == ["sudo", "find"]:
                return find_listing(command[2])
            raise AssertionError(command)

        def digests_of(path):
            return digests["config/" + Path(path).name]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (mock.patch.object(smoke, "run", side_effect=run_without_sudo),
                  mock.patch.object(smoke, "sha256", side_effect=digests_of)):
                # Two refusal-window artifacts missing remotely is exactly the
                # contract: the target is the local archive minus the window.
                # The window starts at the raw host injection instant — the
                # refused build (12:00:00) predates its own refusal record
                # (12:00:01) because the artifact-name stamp is the archive
                # build time, so the refused record's `at` must never bound it.
                injection_ts = base.timestamp() + 3599.5
                shipped = smoke.check_backup_target_readback(
                    root, window_start=injection_ts, window_end=recovery_at.isoformat())
            self.assertEqual(set(shipped), {artifact(baseline_at), artifact(recovery_at)})
            with (mock.patch.object(smoke, "run", side_effect=run_without_sudo),
                  mock.patch.object(smoke, "sha256", side_effect=digests_of),
                  self.assertRaisesRegex(smoke.SmokeError, "not attributable to the refusal window")):
                # A window that starts after the first refusal must refuse its
                # artifact: the attribution is by timestamp, not by "any gap".
                smoke.check_backup_target_readback(
                    root, window_start=refused_two_at.isoformat(), window_end=recovery_at.isoformat())
            with (mock.patch.object(smoke, "run", side_effect=run_without_sudo),
                  mock.patch.object(smoke, "sha256", side_effect=digests_of),
                  self.assertRaisesRegex(smoke.SmokeError, "must be shipped byte-identically")):
                # An artifact at/after the window end is post-restore: missing
                # remotely it is the unshipped recovery, never an allowed gap.
                smoke.check_backup_target_readback(
                    root, window_start=baseline_at.isoformat(), window_end=refused_two_at.isoformat())
            with (mock.patch.object(smoke, "run", side_effect=lambda command, **kwargs:
                                        find_listing(command[2]) if "backup-targets" in command[2]
                                        else "\n".join(remote_names) + "\n"),
                  mock.patch.object(smoke, "sha256", side_effect=digests_of),
                  self.assertRaisesRegex(smoke.SmokeError, "produced no unshipped artifact")):
                # Nothing missing at all: the target already holds every local artifact.
                smoke.check_backup_target_readback(
                    root, window_start=refused_one_at.isoformat(), window_end=recovery_at.isoformat())
            with (mock.patch.object(smoke, "run", side_effect=lambda command, **kwargs:
                                        "config/.scheduled-backup.lock\n"),
                  mock.patch.object(smoke, "sha256", side_effect=digests_of),
                  self.assertRaisesRegex(smoke.SmokeError, "must hold only shipped archive artifacts")):
                smoke.check_backup_target_readback(
                    root, window_start=baseline_at.isoformat(), window_end=recovery_at.isoformat())

    def test_backup_target_readback_requires_byte_identical_local_twins_on_disk(self):
        import contextlib
        import subprocess
        from datetime import datetime, timedelta, timezone

        smoke = self.load_smoke()
        base = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        refusal_at = base + timedelta(minutes=1)
        recovery_at = base + timedelta(minutes=2)

        def artifact(at):
            return "jbod-scheduled-backup-" + at.strftime("%Y%m%dT%H%M%SZ") + "-0123abcd.7z"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backup-targets" / "remote" / "config").mkdir(parents=True)
            (root / "backups" / "archive" / "config").mkdir(parents=True)
            (root / "backups" / "archive" / "config" / ".scheduled-backup.lock").write_bytes(b"bookkeeping")
            (root / "backup-targets" / "remote" / "config" / artifact(base)).write_bytes(b"\x01synthetic")
            (root / "backups" / "archive" / "config" / artifact(base)).write_bytes(b"\x01synthetic")
            (root / "backups" / "archive" / "config" / artifact(refusal_at)).write_bytes(b"\x02synthetic")
            (root / "backup-targets" / "remote" / "config" / artifact(recovery_at)).write_bytes(b"\x03synthetic")
            (root / "backups" / "archive" / "config" / artifact(recovery_at)).write_bytes(b"\x03synthetic")

            def run_without_sudo(command, **kwargs):
                if command[:2] == ["sudo", "find"]:
                    result = subprocess.run(["find", command[2], "-type", "f", "-printf", "%P\n"],
                                            capture_output=True, text=True, check=True)
                    return result.stdout
                raise AssertionError(command)

            def run_bytes_without_sudo(command, **kwargs):
                if command[:2] == ["sudo", "cat"]:
                    result = subprocess.run(["cat", command[2]], capture_output=True, check=True)
                    return result.stdout
                raise AssertionError(command)

            def readback_mocks():
                stack = contextlib.ExitStack()
                stack.enter_context(mock.patch.object(smoke, "run", side_effect=run_without_sudo))
                stack.enter_context(mock.patch.object(smoke, "run_bytes", side_effect=run_bytes_without_sudo,
                                                      create=True))
                return stack

            with readback_mocks():
                shipped = smoke.check_backup_target_readback(
                    root, window_start=refusal_at.timestamp() - 0.5, window_end=recovery_at.isoformat())
            self.assertEqual(set(shipped), {"config/" + artifact(base), "config/" + artifact(recovery_at)})
            (root / "backup-targets" / "remote" / "config" / artifact(recovery_at)).write_bytes(b"\x04drifted")
            with readback_mocks(), self.assertRaisesRegex(smoke.SmokeError, "does not byte-match"):
                smoke.check_backup_target_readback(
                    root, window_start=refusal_at.isoformat(), window_end=recovery_at.isoformat())
            (root / "backup-targets" / "remote" / "config" / artifact(recovery_at)).write_bytes(b"\x03synthetic")
            orphan = "jbod-scheduled-backup-" + (base + timedelta(minutes=3)).strftime("%Y%m%dT%H%M%SZ") + "-0123abcd.7z"
            (root / "backup-targets" / "remote" / "config" / orphan).write_bytes(b"\x05synthetic")
            with readback_mocks(), self.assertRaisesRegex(smoke.SmokeError, "does not byte-match"):
                smoke.check_backup_target_readback(
                    root, window_start=refusal_at.isoformat(), window_end=recovery_at.isoformat())
            (root / "backup-targets" / "remote" / "config" / orphan).unlink()
            (root / "backup-targets" / "remote" / "config" / artifact(base)).unlink()
            (root / "backup-targets" / "remote" / "config" / artifact(recovery_at)).unlink()
            with readback_mocks(), self.assertRaisesRegex(smoke.SmokeError, "no shipped backup bytes"):
                smoke.check_backup_target_readback(
                    root, window_start=refusal_at.isoformat(), window_end=recovery_at.isoformat())
            (root / "backups" / "archive" / "config" / artifact(refusal_at)).unlink()
            (root / "backup-targets" / "remote" / "config" / artifact(base)).write_bytes(b"\x01synthetic")
            (root / "backup-targets" / "remote" / "config" / artifact(recovery_at)).write_bytes(b"\x03synthetic")
            with readback_mocks(), self.assertRaisesRegex(smoke.SmokeError, "produced no unshipped artifact"):
                smoke.check_backup_target_readback(
                    root, window_start=refusal_at.isoformat(), window_end=recovery_at.isoformat())
            (root / "backups" / "archive" / "config" / artifact(refusal_at)).write_bytes(b"\x02synthetic")
            later_refusal = base + timedelta(seconds=90)
            (root / "backups" / "archive" / "config" / artifact(later_refusal)).write_bytes(b"\x07synthetic")
            with readback_mocks(), self.assertRaisesRegex(smoke.SmokeError, "not attributable to the refusal window"):
                # With a second in-window artifact, the early one's stamp before
                # the window start is what the attribution pin rejects.
                smoke.check_backup_target_readback(
                    root, window_start=(refusal_at + timedelta(seconds=1)).isoformat(),
                    window_end=recovery_at.isoformat())

    def test_backup_target_fixture_is_bounded_and_cleanup_uses_its_compose_chain(self):
        smoke = self.load_smoke()
        self.assertTrue(callable(getattr(smoke, "unavailable_backup_storage", None)),
                        "the runtime scenario must exist, not only a static validator")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docker-compose.yml").write_text("services: {}")
            deployment = smoke.Deployment(root)
            with mock.patch.object(smoke, "run") as run:
                smoke.install_backup_scheduler_with_target(deployment)
            self.assertEqual(deployment.files, ("docker-compose.yml", "docker-compose.unavailable-backup.yml"))
            overlay = yaml.safe_load((root / "docker-compose.unavailable-backup.yml").read_text())
            service = overlay["services"]["enclosure-backup-scheduler"]
            self.assertEqual(service["image"], "${JBOD_UI_IMAGE:-ghcr.io/gcs8/truenas-jbod-ui:latest}")
            self.assertEqual(service["command"], ["python", "-m", "history_service.backup_scheduler.main"])
            self.assertEqual(service["user"], "0:0")
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
            self.assertEqual(service["group_add"], ["10001"])
            self.assertEqual(service["environment"]["BACKUP_CONFIG_ENABLED"], "true")
            self.assertEqual(service["environment"]["BACKUP_CONFIG_DEBOUNCE_SECONDS"], "2")
            self.assertEqual(service["environment"]["BACKUP_CONFIG_MAX_DELAY_SECONDS"], "10")
            self.assertEqual(service["environment"]["APP_GID"], "10001")
            self.assertEqual(service["environment"]["SCHEDULED_BACKUP_PASSPHRASE_FILE"],
                             "/app/config/backup-secrets/backup-passphrase")
            self.assertEqual(service["environment"]["BACKUP_TARGETS_JSON"],
                             '[{"target_id":"smoke-nas","provider":"filesystem","root":"/app/backups/targets/remote"}]')
            self.assertIn("./backup-targets:/app/backups/targets", service["volumes"])
            self.assertIn("./backup-journal:/app/backup-journal", service["volumes"])
            self.assertIn("./backup-status:/app/backup-status", service["volumes"])
            self.assertIn("./backup-api:/app/backup-api", service["volumes"])
            self.assertIn("./config:/app/config:ro", service["volumes"])
            self.assertIn("./data:/app/data:ro", service["volumes"])
            self.assertIn("./history:/app/history", service["volumes"])
            self.assertIn("./backups:/app/backups", service["volumes"])
            self.assertIn("./config/ssh:/run/ssh:ro", service["volumes"])
            self.assertEqual(service["restart"], "unless-stopped")
            self.assertIn("scheduler.sock", service["healthcheck"]["test"][3])
            privileged = [call.args[0] for call in run.call_args_list]
            self.assertEqual(privileged[:2], [
                ["sudo", "chown", "0:10001", str(root / "backup-status")],
                ["sudo", "chmod", "2750", str(root / "backup-status")],
            ])
            self.assertEqual(privileged[2:7], [
                ["sudo", "install", "-d", "-o", "0", "-g", "0", "-m", "0755", str(root / "backups")],
                ["sudo", "install", "-d", "-o", "0", "-g", "0", "-m", "0755", str(root / "backup-journal")],
                ["sudo", "install", "-d", "-o", "0", "-g", "0", "-m", "2750", str(root / "backup-api")],
                ["sudo", "install", "-d", "-o", "0", "-g", "0", "-m", "0755", str(root / "backup-targets")],
                ["sudo", "install", "-d", "-o", "0", "-g", "0", "-m", "0755", str(root / "backup-targets" / "remote")],
            ])
            self.assertFalse((root / "config" / "backup-secrets").exists(),
                             "the post-seed helper must not touch the hardened config dir")
            with mock.patch.object(smoke, "run") as run:
                smoke.make_backup_target_unavailable(root)
                smoke.make_backup_target_available(root)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands, [
                ["sudo", "mv", str(root / "backup-targets" / "remote"),
                 str(root / "backup-targets" / "remote.held")],
                ["sudo", "install", "-m", "0644", "-o", "0", "-g", "0", "/dev/null",
                 str(root / "backup-targets" / "remote")],
                ["sudo", "rm", str(root / "backup-targets" / "remote")],
                ["sudo", "mv", str(root / "backup-targets" / "remote.held"),
                 str(root / "backup-targets" / "remote")],
            ])
            with (mock.patch.object(smoke.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
                  mock.patch.object(smoke.os.path, "ismount", return_value=False)):
                smoke.cleanup(root)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands, [
                ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.unavailable-backup.yml",
                 "down", "--volumes", "--remove-orphans"],
                ["sudo", "rm", "-rf", str(root)],
            ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docker-compose.yml").write_text("services: {}")
            (root / "docker-compose.unavailable-backup.yml").write_text("services: {}")
            with mock.patch.object(smoke.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                smoke.cleanup(root)
            self.assertEqual(run.call_args_list[0].args[0][:6],
                             ["docker", "compose", "-f", "docker-compose.yml",
                              "-f", "docker-compose.unavailable-backup.yml"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(smoke, "run") as run:
                smoke.install_backup_secrets(root)
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["sudo", "install", "-d", "-o", "0", "-g", "0", "-m", "0700",
                 str(root / "config" / "backup-secrets")],
                ["sudo", "install", "-m", "0600", "-o", "0", "-g", "0",
                 str(root / "backup-passphrase.tmp"), str(root / "config" / "backup-secrets" / "backup-passphrase")],
            ])
            self.assertFalse((root / "backup-passphrase.tmp").exists(),
                             "the staging copy must not survive the install")

    def test_backup_passphrase_staging_file_is_private_from_creation(self):
        import os
        import re
        import stat

        smoke = self.load_smoke()
        real_open = os.open
        created = []
        staged = []

        def recording_open(path, flags, mode=0o777, *args, **kwargs):
            descriptor = real_open(path, flags, mode, *args, **kwargs)
            created.append((Path(path).name, flags, stat.S_IMODE(os.fstat(descriptor).st_mode)))
            return descriptor

        def fake_run(command, **kwargs):
            if command[:4] == ["sudo", "install", "-m", "0600"]:
                staged.append(Path(command[-2]).read_text(encoding="ascii"))
            return ""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # umask 0 shows the requested mode itself: a world-readable request
            # would be visible here even though a later chmod could repair it.
            previous_umask = os.umask(0)
            try:
                with (mock.patch.object(smoke.os, "open", side_effect=recording_open),
                      mock.patch.object(smoke, "run", side_effect=fake_run)):
                    smoke.install_backup_secrets(root)
            finally:
                os.umask(previous_umask)
            self.assertEqual(created, [("backup-passphrase.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)],
                             "the passphrase staging file must be created exclusively with mode 0600")
            self.assertEqual(len(staged), 1)
            self.assertRegex(staged[0], re.compile(r"\A[0-9a-f]{32}\n\Z"))
            self.assertFalse((root / "backup-passphrase.tmp").exists())

    def test_backup_secrets_bootstrap_precedes_seeding_and_the_overlay_joins_after(self):
        import inspect

        smoke = self.load_smoke()
        source = inspect.getsource(smoke.unavailable_backup_storage)
        # The secrets bootstrap must run while the deployment tree is still the
        # invoking user's (before the released writers harden ./config), the
        # seed phase must not see the scheduler, and the overlay must join the
        # compose chain only after seeding (v0.22.2 has no scheduler module).
        self.assertIn("install_backup_secrets(root)", source)
        self.assertLess(source.index("install_backup_secrets(root)"),
                        source.index("seed_previous_release(deployment, args)"))
        self.assertLess(source.index("seed_previous_release(deployment, args)"),
                        source.index('deployment.compose("stop", *SERVICES)'))
        self.assertLess(source.index('deployment.compose("stop", *SERVICES)'),
                        source.index("install_backup_scheduler_with_target(deployment)"))
        self.assertNotIn("install_backup_scheduler_with_target", source[:source.index("seed_previous_release")])
        self.assertNotIn("UNAVAILABLE_BACKUP_FILES", inspect.getsource(smoke.install_backup_secrets))
        self.assertNotIn("backup-secrets", inspect.getsource(smoke.install_backup_scheduler_with_target))

    def test_unavailable_backup_cli_cleans_up_on_both_success_and_qualification_failure(self):
        smoke = self.load_smoke()
        for failure in (None, smoke.SmokeError("synthetic refusal contract failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "docker-compose.yml").write_text("services: {}")
                with mock.patch.object(smoke, "run"):
                    smoke.install_backup_scheduler_with_target(smoke.Deployment(root))
                argv = ["--scenario", "unavailable-backup-storage", "--root", str(root),
                        "--compose-fixture", "fixture", "--config-fixture", "config-fixture",
                        "--previous-image", "previous", "--previous-version", "1", "--candidate-image", "candidate",
                        "--candidate-version", "2", "--candidate-revision", "revision"]
                with (mock.patch.object(smoke, "smoke", side_effect=failure, return_value="synthetic result"),
                      mock.patch.object(smoke.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
                      mock.patch("sys.stdout"), mock.patch("sys.stderr")):
                    self.assertEqual(smoke.main(argv), 1 if failure else 0)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(commands[-2:], [
                    ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.unavailable-backup.yml",
                     "down", "--volumes", "--remove-orphans"], ["sudo", "rm", "-rf", str(root)],
                ])


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
        enospc = [command for command in runs if "--scenario enospc-state" in command]
        self.assertEqual(len(enospc), 1)
        backup = [command for command in runs if "--scenario unavailable-backup-storage" in command]
        self.assertEqual(len(backup), 1)
        self.assertIn("--nonroot-fixture tests/fixtures/compose/v0.22.2.nonroot.yml", hardened[0])
        for command in hardened + interrupted + segmented + enospc + backup:
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

    def test_segmented_catalog_cutoff_survives_midnight_rounding(self):
        # Since v0.23.0 the migration rounds its cutoff down to midnight UTC.
        # The rounded cutoff must still fall after every row seeded just before.
        import contextlib
        import io
        import sys
        import types
        from datetime import datetime, timezone

        from history_service.segment_sealer import normalize_history_cutoff

        smoke = self.load_smoke()
        captured = {}

        def migrate_segmented_history(**kwargs):
            captured.update(kwargs)
            return {"apply": True, "segment": {"segment_id": "segment-0001"}}

        stub = types.ModuleType("history_service.segment_migration")
        stub.migrate_segmented_history = migrate_segmented_history
        seeded_by = datetime.now(timezone.utc)
        with mock.patch.dict(sys.modules, {"history_service.segment_migration": stub}):
            with contextlib.redirect_stdout(io.StringIO()):
                exec(compile(smoke.CREATE_SEGMENTED_CATALOG, "create_segmented_catalog", "exec"), {})
        rounded = datetime.fromisoformat(normalize_history_cutoff(captured["cutoff"]))
        self.assertGreater(rounded, seeded_by)

    def test_hardened_scenario_requires_the_overlay_fixture(self):
        smoke = self.load_smoke()
        base = ["--root", "/nonexistent", "--compose-fixture", "a", "--config-fixture", "b",
                "--previous-image", "p", "--previous-version", "1", "--candidate-image", "c",
                "--candidate-version", "2", "--candidate-revision", "r"]
        self.assertEqual(smoke.build_parser().parse_args(base).scenario, "base")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            smoke.main([*base, "--scenario", "hardened"])

    def test_clean_host_admission_refuses_existing_root_and_missing_consent(self):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "jbod-clean-host-fixture"
            with self.assertRaises(smoke.SmokeError):
                smoke.admit_clean_host_root(root, consent=False)
            self.assertFalse(root.exists())
            root.mkdir()
            marker = root / "keep"
            marker.write_text("untouched")
            with self.assertRaises(smoke.SmokeError):
                smoke.admit_clean_host_root(root, consent=True)
            self.assertEqual(marker.read_text(), "untouched")

    def test_clean_host_admission_refuses_symlink_and_unbounded_names(self):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / "link").symlink_to(parent, target_is_directory=True)
            for root in (parent / "other", parent / "link" / "jbod-clean-host-fixture"):
                with self.subTest(root=root), self.assertRaises(smoke.SmokeError):
                    smoke.admit_clean_host_root(root, consent=True)

    def test_clean_host_resource_census_never_calls_inspect_absence(self):
        smoke = self.load_smoke()
        for failure in range(4):
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if len(calls) - 1 == failure:
                    raise smoke.SmokeError("daemon unavailable")
                return ""

            with mock.patch.object(smoke, "run", side_effect=run):
                with self.assertRaises(smoke.SmokeError):
                    smoke.clean_host_absent("owned", {})
            self.assertEqual(len(calls), 4)
            self.assertTrue(all("ls" in command for command in calls))

    def test_clean_host_resource_census_refuses_reserved_names_and_residuals(self):
        smoke = self.load_smoke()
        for index in range(4):
            output = ["", "", "", ""]
            output[index] = "truenas-jbod-admin" if index == 0 else "residual"
            with mock.patch.object(smoke, "run", side_effect=output):
                with self.assertRaises(smoke.SmokeError):
                    smoke.clean_host_absent("owned", {})

    def test_clean_host_teardown_failure_still_checks_absence_and_retains_files(self):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "recovery.7z"
            archive.write_bytes(b"retain")
            deployment = smoke.Deployment(root)
            deployment.compose = mock.Mock(side_effect=smoke.SmokeError("down failed"))
            with mock.patch.object(smoke, "clean_host_absent") as census:
                with self.assertRaises(smoke.SmokeError):
                    smoke.clean_host_down(deployment, "owned", {})
            census.assert_called_once()
            self.assertEqual(archive.read_bytes(), b"retain")

    def test_clean_host_unknown_cleanup_retains_recovery_after_successful_down(self):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "key-input").write_text("synthetic")
            deployment = smoke.Deployment(root)
            deployment.compose = mock.Mock(return_value="")
            with mock.patch.object(smoke, "clean_host_absent", side_effect=smoke.SmokeError("unknown")):
                with self.assertRaises(smoke.SmokeError):
                    smoke.clean_host_down(deployment, "owned", {})
            self.assertTrue((root / "key-input").is_file())
            deployment.compose.assert_called_once()
            command = deployment.compose.call_args.args
            deployment.set_image("synthetic-image")
            self.assertIn("COMPOSE_PROFILES=history", (root / ".env").read_text())
            # Explicit --profile flags replace the .env profile selection.
            # Check both supplied Compose documents, without claiming a Docker run.
            profiles = {command[index + 1] for index, value in enumerate(command[:-1])
                        if value == "--profile"}
            for fixture in ("tests/fixtures/compose/v0.22.2.yml", "docker-compose.yml"):
                services = yaml.safe_load((self.ROOT / fixture).read_text())["services"]
                selected = {name for name, service in services.items()
                            if not service.get("profiles") or profiles.intersection(service["profiles"])}
                self.assertTrue({"enclosure-ui", "enclosure-history", "enclosure-admin"} <= selected)
            self.assertEqual(command[-2:], ("down", "--volumes"))

    def test_clean_host_http_restore_uses_encrypted_inspection_receipt(self):
        smoke = self.load_smoke()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "recovery.7z"
            archive.write_bytes(b"encrypted fixture")
            calls = []

            def post(path, body, headers):
                calls.append((path, body, headers))
                if path.endswith("inspect"):
                    return {"ok": True, "encrypted": True, "encryption_mode": "encrypted",
                            "inspection_receipt": "single-use", "present_groups": list(smoke.CLEAN_HOST_GROUPS),
                            "selected_groups": list(smoke.CLEAN_HOST_GROUPS), "absent_groups": []}
                return {"ok": True, "included_groups": list(smoke.CLEAN_HOST_GROUPS)}

            with mock.patch.object(smoke, "clean_host_post", side_effect=post):
                smoke.clean_host_import(archive, "synthetic recovery input")
            self.assertEqual(len(calls), 2)
            self.assertIn("stop_services=true&restart_services=false", calls[1][0])
            self.assertEqual(calls[1][2]["X-Backup-Inspection-Receipt"], "single-use")
            self.assertEqual(calls[1][2]["X-Backup-Expected-Encryption"], "encrypted")
            self.assertEqual(calls[0][1], calls[1][1])

    def test_clean_host_refuses_plaintext_missing_group_and_missing_receipt_before_import(self):
        smoke = self.load_smoke()
        good = {"ok": True, "encrypted": True, "encryption_mode": "encrypted",
                "inspection_receipt": "single-use", "present_groups": list(smoke.CLEAN_HOST_GROUPS),
                "selected_groups": list(smoke.CLEAN_HOST_GROUPS), "absent_groups": []}
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "recovery.7z"
            archive.write_bytes(b"synthetic")
            for changed in ({"encrypted": False}, {"present_groups": []}, {"inspection_receipt": ""}):
                with self.subTest(changed=changed):
                    with mock.patch.object(smoke, "clean_host_post", return_value={**good, **changed}) as post:
                        with self.assertRaises(smoke.SmokeError):
                            smoke.clean_host_import(archive, "synthetic")
                    self.assertEqual(post.call_count, 1)

    def test_clean_host_restore_oracle_rejects_count_byte_and_identity_changes(self):
        smoke = self.load_smoke()
        before = {"config_sha256": "a", "mapping_sha256": "b", "history_rows": {"events": "c"}}
        self.assertIsNone(smoke.check_clean_host_identity(before, dict(before)))
        for field in before:
            with self.subTest(field=field), self.assertRaises(smoke.SmokeError):
                smoke.check_clean_host_identity(before, {**before, field: "changed"})

    def test_clean_host_is_dispatched_without_legacy_unconditional_cleanup(self):
        smoke = self.load_smoke()
        argv = ["--root", "/nonexistent", "--compose-fixture", "a", "--config-fixture", "b",
                "--previous-image", "p", "--previous-version", "1", "--candidate-image", "c",
                "--candidate-version", "2", "--candidate-revision", "r", "--scenario", "clean-host-restore"]
        with mock.patch.object(smoke, "clean_host_restore", return_value="ok") as scenario:
            with mock.patch.object(smoke, "cleanup", side_effect=AssertionError("legacy cleanup")):
                self.assertEqual(smoke.main(argv), 0)
        scenario.assert_called_once()

    def test_clean_host_orchestration_erases_source_and_retains_recovery_on_faults(self):
        import io
        import shutil
        from types import SimpleNamespace

        smoke = self.load_smoke()
        for fault in (None, "import", "partial-start", "cleanup-unknown"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "jbod-clean-host-fixture"
                events = []
                identity = {"history_rows": {"slot_events": [3, "synthetic-digest"]}}

                class Transport(smoke.CleanHostDeployment):
                    def compose(self, *command, **kwargs):
                        events.append(command)
                        return ""

                    def run_python(self, service, script, extra=()):
                        return identity if script == smoke.READ_CLEAN_HISTORY else {"config_sha256": "a", "mapping_sha256": "b"}

                    def exec_python(self, service, script):
                        if script == smoke.READ_HISTORY:
                            return {"integrity": "ok", "counts": {
                                "slot_state_current": 3, "slot_events": 3, "metric_samples": 12}}
                        return self.run_python(service, script)

                def prepare(path, compose, config):
                    if path.name == "target":
                        self.assertNotEqual(config.read_bytes(), args.config_fixture.read_bytes())
                    path.mkdir()
                    (path / "docker-compose.yml").write_text("synthetic transport only")
                    (path / "must-not-survive").write_text("source" if path.name == "source" else "target")

                def run(command, **kwargs):
                    if command[:3] == ["sudo", "rm", "-rf"]:
                        shutil.rmtree(command[-1])
                    elif fault == "cleanup-unknown" and (root / "target").exists() and "ls" in command:
                        raise smoke.SmokeError("unknown cleanup")
                    return ""

                def seed(deployment, args):
                    deployment.set_image(args.previous_image)
                    if fault == "partial-start":
                        raise smoke.SmokeError("partial startup")

                def post(path, body, headers):
                    self.assertFalse((root / "source").exists())
                    self.assertTrue((root / "target").is_dir())
                    self.assertEqual(set((root / "recovery").iterdir()), {
                        root / "recovery/backup.7z", root / "recovery/operator-key-input"})
                    if fault == "import":
                        raise smoke.SmokeError("import refused")
                    if path.endswith("inspect"):
                        return {"ok": True, "encrypted": True, "encryption_mode": "encrypted",
                                "inspection_receipt": "single-use", "selected_groups": list(smoke.CLEAN_HOST_GROUPS),
                                "present_groups": list(smoke.CLEAN_HOST_GROUPS), "absent_groups": []}
                    return {"ok": True, "included_groups": list(smoke.CLEAN_HOST_GROUPS)}

                args = SimpleNamespace(root=root, disposable_clean_host_restore=True,
                    compose_fixture=self.ROOT / "tests/fixtures/compose/v0.22.2.yml",
                    config_fixture=self.ROOT / "tests/fixtures/ci-smoke-config.yaml",
                    previous_image="predecessor", previous_version="0.22.2", candidate_image="candidate",
                    candidate_version="current", candidate_revision="a" * 40)
                opener = mock.Mock()
                opener.open.return_value = io.BytesIO(b"7z\xbc\xaf\x27\x1csynthetic-transport")
                with mock.patch.object(smoke, "CleanHostDeployment", Transport), \
                     mock.patch.object(smoke, "prepare_root", side_effect=prepare), \
                     mock.patch.object(smoke, "run", side_effect=run), \
                     mock.patch.object(smoke, "seed_previous_release", side_effect=seed), \
                     mock.patch.object(smoke, "check_runtime"), \
                     mock.patch.object(smoke, "history_api_view", return_value=smoke.expected_history_view({1: 4, 2: 4, 3: 4})), \
                     mock.patch.object(smoke, "clean_host_post", side_effect=post), \
                     mock.patch.object(smoke.urllib.request, "build_opener", return_value=opener):
                    if fault:
                        with self.assertRaises(smoke.SmokeError):
                            smoke.clean_host_restore(args)
                        self.assertTrue((root / "recovery/operator-key-input").is_file())
                        if fault != "partial-start":
                            self.assertTrue((root / "recovery/backup.7z").is_file())
                    else:
                        self.assertIn("cleanup=verified", smoke.clean_host_restore(args))
                        self.assertFalse(root.exists())
                        restart = events.index(("restart", *smoke.SERVICES))
                        self.assertEqual(events[restart + 1], ("up", "-d", "--wait", "--wait-timeout", "300"))
