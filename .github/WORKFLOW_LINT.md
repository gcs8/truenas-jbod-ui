# Workflow lint

`workflow-lint.yml` runs on pull requests to `main`, pushes to `main` that
change `.github/**` or `tests/test_ci_contract.py`, and manual dispatch. Every
invocation scans **all** `.yml` and `.yaml` files under `.github/workflows`,
not just the changed files. The job uses a GitHub-hosted Ubuntu runner, a
read-only contents token, no injected secrets, and no application services.

The gate uses Python 3.12's standard library. Its focused tests run before
acquisition. No pip, npm, remote installer, or container is needed to install
the scanners. Each fixed official release archive is SHA-256 checked before
only its executable member is read. Downloads and reports go in the runner's
temporary directory, never in the checkout.

## Tool pins

The URLs and digests are maintained in `.github/scripts/workflow_lint.py`.
They were checked against the official GitHub release asset `digest` metadata.

| Tool | Release | Linux x86-64 archive SHA-256 |
| --- | --- | --- |
| actionlint | [1.7.12](https://github.com/rhysd/actionlint/releases/tag/v1.7.12) | `8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8` |
| ShellCheck | [0.11.0](https://github.com/koalaman/shellcheck/releases/tag/v0.11.0) | `8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198` |
| zizmor | [1.33.1](https://github.com/zizmorcore/zizmor/releases/tag/v1.33.1) | `3e6a301c7a820a2501915f5a9de8c0486da82bdec7965f068477538a98743d43` |

The actionlint archive also matches the official
`actionlint_1.7.12_checksums.txt` manifest. That manifest's SHA-256 is
`433028cf0ba3c42163ea1a668dedce30fcdbe84fe912b1a5e288c006eab8a4f5`.
Checkout and Python setup reuse the repository's full action commit pins.

Actionlint receives an explicit, nonempty `-shellcheck` executable path after
all three tool versions are checked. Missing ShellCheck fails the job instead
of silently losing shell coverage. `.github/actionlint.yaml` only registers
the existing dispatch-only trial's custom runner label. It suppresses no
rules. Two per-command SC2016 comments in the image-digest summary explain
literal Markdown backticks in single-quoted printf format strings. The
format strings and publication behavior remain unchanged.

Zizmor runs with `--offline --no-config --no-ignores --strict-collection`,
`--persona=regular`, and the explicit `--format=json-v1` output contract.
Offline mode does not run online audits such as action-repository lookup.
There is no GitHub API token passed to the scanner. Pedantic and auditor
personas are not enabled. Native finding exits 11 through 14 retain their
meaning; the gate never uses `--no-exit-codes` or a severity cutoff.

## Reviewed findings

`.github/workflow-lint-baseline.json` contains three existing findings, not
rule-wide ignores. Each entry requires a reason, native rule, path, one-based
primary line and column, severity, confidence, and the SHA-256 of the complete
workflow. Unknown fields, duplicate JSON keys or identities, malformed reports,
scanner errors, severity/exit mismatches, new findings and stale entries fail.
Changes anywhere in an accepted workflow require fresh review. Binding only
the `pull_request_target` trigger line would not catch a newly added checkout
or executable step elsewhere in the labeler.

The current dispositions are:

- `adhoc-packages` in `dependency-audit.yml`, line 56. The existing npm bootstrap
  pins version 10.9.8 and disables lifecycle scripts, but is not integrity-locked.
  Its low-severity package-resolution risk remains accepted for this exact
  audit-only step. This is not described as a false positive.
- `dangerous-triggers` in `pr-labels.yml`, line 12. The labeler needs its
  intentional label-write token but never checks out or runs pull-request
  code. The title and number enter through environment variables and label
  names come from a fixed mapping. The full workflow hash protects that review
  boundary. The scanner's high severity is preserved in the baseline.
- `template-injection` in `publish-public-demo.yml`, line 149. This informational,
  low-confidence finding concerns a Pages URL produced by the pinned
  `deploy-pages` action. Readback is dispatch-only after deployment, not a
  pull-request code path. This lint change leaves its publication and readback
  semantics intact and accepts only the exact existing source.

There is no auto-refresh or blanket-ignore command. To change an entry,
inspect the new native report and the **whole** affected workflow, explain
why retaining that finding is justified, then update its exact identity and
source hash. Remove entries whose findings were corrected. A concrete secret
or exploitable defect needs private maintainer escalation, not an accepted
baseline. Tool upgrades require checking official metadata, updating the pins
and explicit output contract, reviewing all findings, and running the focused
tests and actual native gate again.

## Local use

Use a project Python 3.12 interpreter and fresh temporary tool/report paths:

```bash
python .github/scripts/workflow_lint.py --install-tools "$TMPDIR/workflow-lint-tools"
python -m unittest discover -s .github/tests -p 'test_workflow_lint.py' -v
python .github/scripts/workflow_lint.py --tools-dir "$TMPDIR/workflow-lint-tools" --report-dir "$TMPDIR/workflow-lint-reports"
python -m unittest tests.test_ci_contract -v
```

Both output directories must be new for that invocation. The report directory
keeps each native JSON report and stderr plus `receipt.json` with the full
selected file list, native exit codes, accepted finding counts and gate result.
Missing or malformed JSON is never treated as an empty finding set. A clean
current gate means actionlint has no findings and zizmor has exactly the three
reviewed findings. It does not mean zizmor itself returned zero findings.

SBOMs, signing, provenance, Scorecard, security policy files, code ownership,
Compose lint and scheduled issue writers are outside this workflow's scope.
