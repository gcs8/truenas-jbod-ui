"""No-install singleton probe. Synthetic scratch only; no application/live data."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          timeout=60, **kwargs).stdout.strip()


def scratch_probe(root):
    with tempfile.TemporaryDirectory(prefix='jbod-trial-', dir=root) as temp:
        path = Path(temp)
        blob = b'jbod-synthetic-scratch\n' * 32768
        with (path / 'scratch').open('wb') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        if (path / 'scratch').read_bytes() != blob:
            raise RuntimeError('scratch round trip failed')
        database = path / 'synthetic.sqlite'
        expected_rows = [(i, 'synthetic') for i in range(1000)]
        with closing(sqlite3.connect(database)) as db:
            if db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise RuntimeError('WAL unavailable')
            db.execute('CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)')
            db.executemany('INSERT INTO probe VALUES (?, ?)', expected_rows)
            db.commit()
            db.execute("UPDATE probe SET value='rollback'")
            db.rollback()
            if db.execute("SELECT count(*) FROM probe WHERE value='synthetic'").fetchone()[0] != 1000:
                raise RuntimeError('SQLite rollback failed')
        with closing(sqlite3.connect(database)) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite integrity check failed')
            try:
                reopened_rows = db.execute('SELECT id, value FROM probe ORDER BY id').fetchall()
            except sqlite3.DatabaseError as error:
                raise RuntimeError('SQLite committed rows unavailable after reopen') from error
            if reopened_rows != expected_rows:
                raise RuntimeError('SQLite committed rows changed after reopen')


def needs_probe(python):
    cases = [('success', 0), ('failure', 1), ('cancelled', 1), ('skipped', 1), (None, 1)]
    for status, expected in cases:
        needs = {} if status is None else {'python-tests': {'result': status}}
        result = subprocess.run([python, 'scripts/ci_unittest.py', '--check-needs'],
                                cwd=ROOT, env={**os.environ, 'CI_NEEDS': json.dumps(needs)},
                                text=True, capture_output=True, timeout=30)
        if result.returncode != expected:
            raise RuntimeError(f'needs result {status}: expected {expected}, got {result.returncode}')


def main(profile="full"):
    if os.getuid() != 1001:
        raise RuntimeError('trial requires reviewed runner UID 1001')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if status['CapEff'].strip() != '0' * 16 or status['NoNewPrivs'].strip() != '1':
        raise RuntimeError('runner hardening differs from reviewed contract')
    require_prerequisites(profile)
    versions = probe_tools(profile)
    receipt = {'profile': profile, 'result': 'pass', 'python': 'pass', 'node': 'pass',
               'storage': 'pass', 'needs': 'pass',
               'browser': 'not_qualified' if profile == 'native' else 'pass',
               'versions': versions, 'needs_cases_per_python': 5,
               'source_files_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                       for name in ('scripts/jbod_runner_smoke.py', 'scripts/ci_unittest.py')},
               'source_revision': os.environ.get('GITHUB_SHA', 'unpublished'),
               'storage_checks': 'synthetic scratch, fsync, WAL, rollback, reopen rows, integrity, cleanup'}
    print(json.dumps(receipt, sort_keys=True))


def require_prerequisites(profile):
    if profile not in ('native', 'full'):
        raise ValueError('unknown qualification profile')
    commands = ('python3', 'python3.12', 'python3.14', 'node', 'npm', 'gh', 'git', 'curl', 'tar', 'sha256sum')
    for command in commands + (('google-chrome',) if profile == 'full' else ()):
        if not shutil.which(command):
            raise RuntimeError(f'missing preinstalled prerequisite: {command}')


def qualifies(receipt, scope):
    # This is a synthetic-smoke contract, never an ordinary-CI routing decision.
    expected = {'profile': 'native', 'result': 'pass', 'python': 'pass', 'node': 'pass',
                'storage': 'pass', 'needs': 'pass', 'browser': 'not_qualified'}
    return (scope == 'native-trial' and isinstance(receipt, dict)
            and all(receipt.get(key) == value for key, value in expected.items()))


def probe_tools(profile):
    if profile not in ('native', 'full'):
        raise ValueError('unknown qualification profile')
    versions = {}
    for version in ('3.12', '3.14'):
        python = 'python' + version
        actual = run([python, '-c', 'import sys; print("%d.%d" % sys.version_info[:2])'])
        if actual != version:
            raise RuntimeError('wrong Python version')
        run([python, '-c', 'import sqlite3, subprocess, sys; subprocess.run([sys.executable, "-c", "import sqlite3, ssl"], check=True)'])
        run([python, '-c', 'from scripts.jbod_runner_smoke import scratch_probe; import os; scratch_probe(os.environ["RUNNER_TEMP"])'], cwd=ROOT)
        needs_probe(python)
        versions[python] = actual
    versions['node'] = run(['node', '--version'])
    if not versions['node'].startswith('v24.'):
        raise RuntimeError('Node 24 required')
    for command in ('npm', 'gh', 'git') + (('google-chrome',) if profile == 'full' else ()):
        versions[command] = run([command, '--version']).splitlines()[0]
    if profile == 'native':
        return versions
    # Actual headless startup loads Chrome's required shared libraries. Do not
    # add --no-sandbox or sudo to compensate for an incompatible runner image.
    with tempfile.TemporaryDirectory(prefix='jbod-chrome-', dir=os.environ['RUNNER_TEMP']) as temp:
        page = Path(temp) / 'probe.html'
        page.write_text('<title>jbod-trial</title><p>synthetic</p>')
        dom = run(['google-chrome', '--headless', '--disable-gpu', '--no-first-run',
                   '--disable-background-networking', '--disable-extensions',
                   '--user-data-dir=' + str(Path(temp) / 'profile'), '--dump-dom', page.as_uri()])
        if '<title>jbod-trial</title>' not in dom:
            raise RuntimeError('Chrome DOM smoke failed')
    return versions


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, choices=('native', 'full'))
    main(parser.parse_args().profile)
