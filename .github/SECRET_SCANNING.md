# Secret scanning

`.github/workflows/secret-scan.yml` runs on every pull request targeting `main`,
Monday at 07:23 UTC, and manual dispatch. It uses a full checkout, including the
pull-request merge commit and its ancestry, and scans all reachable Git refs
with `gitleaks git --log-opts='--all --full-history -m'`. It is not a diff-only
check. Uncommitted working files and unreachable Git objects are outside this
history scan.

The workflow runs only on GitHub-hosted Ubuntu, with `contents: read` and no
persisted checkout credentials. The scanner is the free upstream CLI, not a
licensed action or service. Version, upstream checksum manifest, release archive,
archive members, executable digest, and reported version are pinned and checked
by `.github/scripts/secret_scan.py`. Updating the scanner requires reviewing and
updating those pins together.

Every native invocation uses `--redact=100` for both logs and reports, and
`--ignore-gitleaks-allow` disables inline bypass comments. An explicit config
selects the pinned scanner's built-in rules without repository-added path,
regex, or rule exclusions. Scanner diagnostics and JSON reports are never
uploaded. The helper keeps reports under a private temporary directory outside
the checkout, removes them before returning, and publishes only a status,
version, and finding count. Exit 0 means clean, exit 1 means unignored findings,
and exit 2 means acquisition, validation, Git, scanner, or report failure. All
nonzero exits fail the workflow. Error-level diagnostics also fail the scan,
even if the native process returns zero.

## Reviewed historical exceptions

`.gitleaksignore` contains only commit-specific native fingerprints of the form
`commit:path:rule:line`. Each has a separate preceding `# Reviewed ...` note
identifying the exact source context and reason. Whole-line comments are
supported by the pinned native parser. Inline comments are not used. The
validator rejects duplicate, malformed, unreviewed, and commit-independent
fingerprints. No baseline report is used.

Do not add an exception just because a match resembles a test value. Review its
exact tracked source at the fingerprint commit privately. A source hash must
match the named file at the demo's recorded revision. Legacy manifests without
a source revision must instead match the file at the fingerprint commit, and
the note must say so. Synthetic values must have unambiguous test context. Stop
and notify the maintainer privately if a finding could be real. Never copy the
value into an exception, issue, pull request, or log. New occurrences, including
the same value at a new commit or line, still fail until separately reviewed.

## Maintained verification

The workflow explicitly executes only `.github/tests/test_secret_scan.py`.
These offline tests use disposable Git repositories and runtime-assembled
synthetic values. They prove deleted secrets remain detectable through merged
pull-request ancestry, other reachable refs are scanned, verbose native output
and reports redact the entire synthetic value, inline bypass comments do not
work, and an exact commented fingerprint does not ignore a new occurrence.
They also exercise ignore validation, integrity rejection, shallow-checkout
refusal, and scanner/report failure handling. Workflow trigger, permission,
action-pin and checkout contracts live in `tests/test_ci_contract.py`.
