# Release wrap: v0.22.2

Date: `2026-08-31`

## Scope

v0.22.2 is a SemVer patch on published v0.22.1. It releases the reviewed segmented-history architecture already merged to `main`, adds fail-closed scheduled-FULL-gated retention with durable backup authorization, fixes immutable deployment candidate validation, and carries the dependency versions already present on `main`.

Release branch: `fix/v0.22.2-segmented-lifecycle` from `origin/main` commit `7e1c60084c366f6f4cc1d01a3e6b62b3acea2d36`.

Release candidate commit: pending.

Tag: `v0.22.2` pending.

PR #120 is closed because current `main` already contains its `websockets==17.0.1` and `cryptography==50.0.0` updates. Issue #119 and draft PR #121 target v0.22.3 unless their required real-shelf evidence arrives before the v0.22.2 freeze. Crash-safe later-generation segment publication is tracked in issue #124 for v0.22.3 because the initial migration journal does not cover a later hot, segment, and catalog replacement transaction.

Validated against `docs/RELEASE_CHECKLIST.md`.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Isolated branch `fix/v0.22.2-segmented-lifecycle` starts from segmented-history merge `7e1c60084c366f6f4cc1d01a3e6b62b3acea2d36`; scope is scheduled-FULL-gated segmented retention, legacy snapshot suppression, deployment `.env` staging, version/docs, and release evidence | Pass |  |
| Python unit and syntax gates | yes | B1/B2 RED regressions reproduced missing-group and restart-reuse failures; 365 affected tests pass; full Python 3.12 and 3.14 each pass 1,086 tests with 4 declared skips; Ruff, compileall, dependency consistency, and deterministic baseline checks pass | Pass |  |
| JavaScript syntax gates | yes | `npm ci --ignore-scripts`, 130 Node unit tests, and syntax checks for application/admin/public-demo JavaScript pass | Pass |  |
| Docker build and health gates | yes | Exact-candidate Docker build, image identity, and service health remain required | Blocked |  |
| Optional-sidecar runtime matrix | yes | Exact-candidate UI-only, UI+history, UI+admin, and all-service matrix remains required | Blocked |  |
| Full Playwright/browser gates | yes | Main and admin browser suites remain required on exact candidate bytes | Blocked |  |
| Feature-specific live API/UI gates | yes | Scheduled schema-v2 FULL status, stale/failure refusal, authorized hot retention, hot-plus-segment reads, and deployment-helper staging smoke remain required | Blocked |  |
| Local release perf harnesses | yes | Main and history performance harnesses remain required | Blocked |  |
| Linux QA restore gate | yes | Encrypted schema-v2 export, mutation, restore, and hot-plus-segment query drill on history larger than production remains required | Blocked |  |
| Restored Linux QA perf harnesses | yes | Restored-stack main and history performance harnesses remain required | Blocked |  |
| Snapshot/export/offline artifact gate | yes | Snapshot estimate/download and offline browser smoke remain required | Blocked |  |
| Docs/wiki/public-demo gate | yes | Version alignment, changelog, v0.22.2 release notes/wrap, roadmap, segmented operations, release checklist, and wiki consistency pass source tests; static public demo passes 1 test with 4 interaction-only skips | Pass |  |
| GHCR publish verification | yes | Requires merged release commit, v0.22.2 GitHub Release, release workflow, full immutable image reference, and exact source revision | Blocked |  |
| Deployment refresh/sniff tests | yes | Requires published-digest development smoke and cleanup, then a fresh independently verified production FULL backup before any production update | Blocked |  |
| Post-release reopen | yes | Requires completed GHCR and deployment evidence before opening the next development version | Blocked |  |

## Safety boundary

- Production remains on the current segmented digest until every pre-publish gate passes and a fresh encrypted FULL backup completes and is independently verified.
- Development acceptance uses h0073 and stops the task-owned JBOD UI stack afterward. Existing listeners on ports 18080 and 18081 remain untouched.
- Segmented retention edits only the writable hot database. It never edits immutable segments.
- Missing, malformed, stale, failed, history-excluding, history-absent, symlinked, oversized, or writable scheduled-backup status blocks retention.
- The hot SQLite database persists `ready`, `claimed`, and `consumed` backup authorization. Completed passes cannot repeat after restart, failed passes remain retryable, catch-up can continue, and interrupted claims require a newer backup.
- PR #121 remains draft. Synthetic fixtures do not replace real-shelf validation.

## Validator status

Current shape check:

- `/tmp/hermes-verify-issue47-py312/bin/python scripts/validate_release_wrap.py 0.22.2 --phase pre-tag --allow-blocked`

The strict pre-tag command must not pass until every pre-publish row is `Pass` or has a justified `N/A` result:

- `/tmp/hermes-verify-issue47-py312/bin/python scripts/validate_release_wrap.py 0.22.2 --phase pre-tag`

## Remaining work

1. Complete the targeted successor exact-candidate review for the B1/B2 remediation.
2. Open and merge the exact-head lifecycle PR only after required CI passes.
3. Build and restore-test the release candidate on h0073, then stop the task-owned stack.
4. Complete the strict pre-tag wrap and publish v0.22.2.
5. Verify the GHCR digest and source revision.
6. Take and independently verify a fresh production FULL backup before any production update.
