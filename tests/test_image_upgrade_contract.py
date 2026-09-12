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
        self.assertIn('./config:/app/config', base['enclosure-ui']['volumes'])
        self.assertIn('./config:/app/config:ro', overlay['enclosure-ui']['volumes'])
        self.assertEqual(overlay['enclosure-admin']['cap_add'], ['CHOWN', 'FOWNER'])
        self.assertEqual(base['enclosure-backup']['network_mode'], 'none')
        self.assertTrue(base['enclosure-backup']['read_only'])


if __name__ == '__main__':
    unittest.main()
