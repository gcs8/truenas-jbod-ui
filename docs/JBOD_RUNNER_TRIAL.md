# Native singleton runner trial

This successor qualifies only synthetic Python/Node and storage smoke. Invoke
`python3 scripts/jbod_runner_smoke.py --profile native` explicitly. Missing or
unknown CLI profiles fail. `--profile full` still requires real sandboxed Chrome;
it does not downgrade on a browser error. The frozen predecessor is unchanged.

Native requires Python 3.12 and 3.14, Node 24, npm, gh, Git, curl, tar and
sha256sum. It installs nothing. Both interpreters execute child SSL/SQLite imports,
scratch fsync and readback, SQLite WAL/commit/rollback/reopen/integrity and cleanup,
and all five semantic needs cases. Connections close before scratch removal.
UID 1001, zero effective capabilities and NoNewPrivs remain mandatory.

The native receipt names `profile=native`, `python=pass`, `node=pass`,
`storage=pass`, `needs=pass`, and `browser=not_qualified`. Source-file SHA-256
values identify the actual companion bytes. `source_revision=unpublished` is
honest for local source mounts; it is not a dispatch commit. The external runtime
receipt supplies inspected image identity and hardening. The `qualifies` helper
recognizes only native synthetic receipts. It is not authentication, an image
attestation or a CI router. It rejects browser, full, all-ci and arc-qualified scope.

Only the owner-only main dispatch singleton uses the literal
`jbod-native-trial` label. Source SHA must match the dispatched workflow SHA and
`JBOD_NATIVE_TRIAL_SHA`, with immutable repository ID and actor/triggering-actor
checks, pinned checkout, no persisted credentials, 15-minute timeout and one
non-cancelling concurrency group. No ordinary CI variable or browser selector is
changed. Existing required browser and private-QA gates remain required.

## Approval and remaining gates

- Owner approved Python/Node-only initial trial plus storage checks. Browser
  acceptance remains pending on a separately reviewed VM-backed lane. No shared
  seccomp/AppArmor/capability relaxation or sandbox bypass is authorized.
- The parent supplied successful selected-repository App access verification.
  Public fork approval policy is `all_external_contributors`; never approve
  external fork execution. Re-read both boundaries before any registration.
  Job expressions do not constrain fork-edited YAML.
- Independent review of this exact source freeze, built image, security and
  publication side effects is required. No commit, push, PR, registry push,
  deployment, dispatch or live activation occurs in this candidate task.
- Before publication, refresh destination main and reconcile this trial-only
  allowlist without changing ordinary workflow bytes. Re-run the contracts.
  Publish workflow and companions together on default main only after explicit
  approval. Approve the resulting commit, never a temporary tree or base SHA.
  Existing branch-push CI may spend hosted minutes even with runner variables
  unset. Audit all trigger side effects and required checks before publication.
- The inactive infrastructure values deliberately retain their old base image.
  They are not deployable native qualification: select the independently reviewed
  published successor digest in a new freeze before activation. No local Docker
  tag or image ID is a pullable registry reference.
- Docker smoke is not k3s acceptance. Require actual containerd RuntimeDefault,
  unchanged pod controls, approved CNI egress/DNS enforcement, disk-backed
  emptyDir storage semantics, cleanup and measured idle/capacity/pressure gates.
  Keep min0/max1 and the old fleet reservation. Do not link the active graph yet.
- Full #500 Python partition/dependency/promtool/coverage/failure propagation,
  full repository tests and ordinary CI migration remain separate gates. This
  synthetic smoke does not prove full application or browser readiness.
- Browser work needs sandbox-enabled raw Chrome and Playwright, explicit
  `chromiumSandbox: true`, renderer/runtime evidence and cleanup on an approved
  VM-backed lane. Merely retaining Chrome unused in the native image proves none
  of those requirements.

## Bounded source verification

Run from this successor with `JBOD_INFRA_VALUES` set to its inactive sibling's
`clusters/home-prod/charts/gha-runner-scale-set/jbod-values.yaml`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  tests.test_jbod_runner_trial tests.test_ci_contract tests.test_dev_check -q
ruff check --no-cache scripts/ci_unittest.py scripts/jbod_runner_smoke.py \
  scripts/dev_check.py tests/test_jbod_runner_trial.py tests/test_ci_contract.py \
  --select E4,E7,E9,F
git diff --check
```

This bounded infrastructure-contract suite is not `dev_check.py --safe` or a
release gate. No application source changed and no live application data is used.
