"""Local trial/source contracts. No registration, dispatch, or runtime install."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from scripts import jbod_runner_smoke as smoke
import re

ROOT = Path(__file__).resolve().parents[1]
TRIAL = ROOT / '.github/workflows/jbod-runner-trial.yml'


def context():
    repo = {'id': 1208650488, 'full_name': 'gcs8/truenas-jbod-ui', 'fork': False}
    return {'github': {
        'repository': repo['full_name'], 'repository_id': str(repo['id']),
        'actor': 'gcs8', 'triggering_actor': 'gcs8',
        'event_name': 'pull_request', 'ref': 'refs/pull/1/merge',
        'event': {'deleted': False, 'pull_request': {
            'base': {'ref': 'main', 'repo': copy.deepcopy(repo)},
            'head': {'repo': copy.deepcopy(repo)},
            'user': {'login': 'gcs8', 'type': 'User'},
        }},
    }, 'vars': {'JBOD_CI_LINUX_RUNNER': 'jbod-native-trial'}}

def evaluate(expression, data):
    # Evaluate only checked-in policy, never event text as source. JSON values
    # are substituted as literals; no subprocess, action or checkout is used.
    source = expression.removeprefix('${{').removesuffix('}}').strip()
    def value(match):
        node = data
        for field in match[0].split('.'):
            node = node.get(field) if isinstance(node, dict) else None
        return repr(node)
    source = re.sub(r'\b(?:github|vars)\.[a-zA-Z0-9_.]+', value, source)
    source = source.replace('&&', ' and ').replace('||', ' or ')
    source = re.sub(r'\bfalse\b', 'False', source)
    source = re.sub(r'\btrue\b', 'True', source)
    return eval(source, {'__builtins__': {}}, {})


class RunnerTrialTests(unittest.TestCase):
    def test_cross_repository_infrastructure_label(self):
        # Explicit opt-in path; ordinary source tests do not assume a sibling repo.
        value = os.environ.get('JBOD_INFRA_VALUES')
        if not value:
            self.skipTest('set JBOD_INFRA_VALUES to reviewed infrastructure jbod-values.yaml')
        infra = yaml.safe_load(Path(value).read_text())
        workflow = yaml.safe_load(TRIAL.read_text())
        self.assertEqual(infra['runnerScaleSetName'], workflow['jobs']['smoke']['runs-on'])
        self.assertEqual(infra['githubConfigUrl'], 'https://github.com/gcs8/truenas-jbod-ui')
        self.assertEqual((infra['minRunners'], infra['maxRunners']), (0, 1))

    def test_trial_contract_and_admission(self):
        workflow = yaml.safe_load(TRIAL.read_text())
        # PyYAML YAML 1.1 treats the unquoted GitHub key `on` as True.
        events = workflow.get('on', workflow.get(True))
        self.assertEqual(set(events), {'workflow_dispatch'})
        self.assertEqual(set(events['workflow_dispatch']['inputs']), {'source_sha'})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(workflow['concurrency'], {'group': 'jbod-native-singleton-runner-trial', 'cancel-in-progress': False})
        self.assertEqual(set(workflow['jobs']), {'smoke'})
        job = workflow['jobs']['smoke']
        self.assertEqual(job['runs-on'], 'jbod-native-trial')
        self.assertEqual(job['timeout-minutes'], 15)
        self.assertNotIn('strategy', job)
        self.assertNotIn('needs', job)
        data = context()
        data['github'].update(event_name='workflow_dispatch', ref='refs/heads/main', sha='a' * 40)
        data['vars']['JBOD_NATIVE_TRIAL_SHA'] = 'a' * 40
        data['inputs'] = {'source_sha': 'a' * 40}
        # Reuse the bounded expression evaluator by treating inputs as vars.
        expr = job['if'].replace('inputs.source_sha', 'vars.input_sha')
        data['vars']['input_sha'] = data['inputs']['source_sha']
        self.assertTrue(evaluate(expr, data))
        for area, field, value in [('github', 'actor', 'outsider'), ('github', 'triggering_actor', 'outsider'),
                                   ('github', 'repository_id', '42'), ('github', 'repository', 'fork/repo'),
                                   ('github', 'event_name', 'pull_request'), ('github', 'ref', 'refs/heads/feature'),
                                   ('github', 'sha', 'b' * 40), ('vars', 'JBOD_NATIVE_TRIAL_SHA', ''),
                                   ('vars', 'input_sha', '')]:
            changed = copy.deepcopy(data)
            changed[area][field] = value
            with self.subTest(field=field):
                self.assertFalse(evaluate(expr, changed))
        steps = job['steps']
        for sha, expected in [('a' * 40, 0), ('main', 1), ('a' * 39, 1), ('A' * 40, 1), ('a' * 40 + '\n', 1)]:
            result = subprocess.run(['bash', '-c', steps[0]['run']], env={'SOURCE_SHA': sha})
            self.assertEqual(result.returncode, expected)
        checkout = steps[1]
        self.assertRegex(checkout['uses'], r'^actions/checkout@[0-9a-f]{40}$')
        self.assertEqual(checkout['with'], {'repository': 'gcs8/truenas-jbod-ui', 'ref': '${{ inputs.source_sha }}',
                                             'persist-credentials': False, 'fetch-depth': 1})
        self.assertIn('git rev-parse HEAD', steps[2]['run'])
        self.assertEqual(steps[3]['run'], 'python3 scripts/jbod_runner_smoke.py --profile native')
        self.assertEqual(len(steps), 4)
        self.assertNotRegex(TRIAL.read_text(), r'(?m)^\s*(?:sudo |pip install|npm ci|docker |kubectl )')

    def test_needs_only_cli_rejects_malformed_and_extra_results(self):
        for value in ('null', '[]', 'bad-json', '{"python-tests":null}',
                      '{"python-tests":{"result":"success"},"extra":{"result":"success"}}'):
            result = subprocess.run(
                [sys.executable, 'scripts/ci_unittest.py', '--check-needs'],
                cwd=ROOT, env={**os.environ, 'CI_NEEDS': value},
                capture_output=True, timeout=30)
            with self.subTest(value=value):
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, b'')

    def test_needs_helper_has_no_discovery_or_partition_code(self):
        import ast
        tree = ast.parse((ROOT / 'scripts/ci_unittest.py').read_text())
        self.assertEqual({node.name for node in tree.body if isinstance(node, ast.FunctionDef)},
                         {'all_success', 'main'})
        self.assertEqual({node.names[0].name for node in tree.body if isinstance(node, ast.Import)},
                         {'argparse', 'json', 'os'})
        result = subprocess.run([sys.executable, 'scripts/ci_unittest.py', '--partition', 'remainder'],
                                cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2)

    def test_runtime_identity_rejects_before_commands(self):
        from unittest.mock import patch
        with patch.object(smoke.os, 'getuid', return_value=0), patch.object(smoke, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'UID 1001'):
                smoke.main()
            run.assert_not_called()

    def test_trial_has_no_privileged_or_secondary_execution_lane(self):
        workflow = yaml.safe_load(TRIAL.read_text())
        job = workflow['jobs']['smoke']
        for key in ('services', 'container', 'uses', 'strategy', 'environment'):
            self.assertNotIn(key, job)
        self.assertNotIn('secrets.', TRIAL.read_text())

    def test_real_synthetic_scratch_and_semantic_needs(self):
        with tempfile.TemporaryDirectory() as temp:
            smoke.scratch_probe(temp)
            self.assertEqual(list(Path(temp).iterdir()), [])
        smoke.needs_probe(sys.executable)


class NativeProfileTests(unittest.TestCase):
    def test_profile_is_explicit_on_cli(self):
        result = subprocess.run([sys.executable, 'scripts/jbod_runner_smoke.py'], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--profile', result.stderr)

    def test_native_prerequisites_exclude_only_browser(self):
        self.assertTrue(hasattr(smoke, 'require_prerequisites'), 'explicit prerequisite profiles missing')
        from unittest.mock import patch
        commands = ('python3', 'python3.12', 'python3.14', 'node', 'npm', 'gh', 'git', 'curl', 'tar', 'sha256sum')
        for missing in commands:
            with self.subTest(missing=missing), patch.object(smoke.shutil, 'which', side_effect=lambda c: None if c == missing else '/bin/' + c):
                with self.assertRaisesRegex(RuntimeError, 'missing preinstalled prerequisite: ' + re.escape(missing)):
                    smoke.require_prerequisites('native')
        with patch.object(smoke.shutil, 'which', side_effect=lambda c: None if c == 'google-chrome' else '/bin/' + c):
            smoke.require_prerequisites('native')
            with self.assertRaisesRegex(RuntimeError, 'google-chrome'):
                smoke.require_prerequisites('full')
        with self.assertRaises(ValueError):
            smoke.require_prerequisites('unknown')

    def test_native_receipt_never_qualifies_browser_or_all_ci(self):
        self.assertTrue(hasattr(smoke, 'qualifies'), 'scope-bound receipt validator missing')
        receipt = {'profile': 'native', 'result': 'pass', 'python': 'pass', 'node': 'pass',
                   'storage': 'pass', 'needs': 'pass', 'browser': 'not_qualified'}
        self.assertTrue(smoke.qualifies(receipt, 'native-trial'))
        for scope in ('full', 'browser', 'all-ci', 'arc-qualified', '', None):
            self.assertFalse(smoke.qualifies(receipt, scope))
        for key in receipt:
            bad = dict(receipt)
            bad[key] = 'wrong'
            self.assertFalse(smoke.qualifies(bad, 'native-trial'))
        for key in receipt:
            bad = dict(receipt)
            del bad[key]
            self.assertFalse(smoke.qualifies(bad, 'native-trial'))

    def test_native_execution_does_not_probe_browser_and_propagates_errors(self):
        self.assertTrue(hasattr(smoke, 'probe_tools'), 'profile-specific tool probe missing')
        from unittest.mock import patch
        def run(args, **kwargs):
            self.assertNotIn('google-chrome', args)
            if args[0].startswith('python') and 'sys.version_info' in args[-1]:
                return args[0].removeprefix('python')
            return 'v24.0.0' if args[0] == 'node' else 'version'
        with patch.object(smoke, 'run', side_effect=run), patch.object(smoke, 'needs_probe'):
            versions = smoke.probe_tools('native')
            self.assertNotIn('google-chrome', versions)
        with patch.object(smoke, 'run', side_effect=RuntimeError('native failure')):
            with self.assertRaisesRegex(RuntimeError, 'native failure'):
                smoke.probe_tools('native')

    def test_native_only_workflow_scope(self):
        workflow = yaml.safe_load(TRIAL.read_text())
        self.assertEqual(workflow['jobs']['smoke']['runs-on'], 'jbod-native-trial')
        self.assertEqual(workflow['jobs']['smoke']['steps'][3]['run'],
                         'python3 scripts/jbod_runner_smoke.py --profile native')
        self.assertNotIn('JBOD_CI_LINUX_RUNNER', TRIAL.read_text())
        self.assertNotIn('continue-on-error', TRIAL.read_text())


if __name__ == '__main__':
    unittest.main()
