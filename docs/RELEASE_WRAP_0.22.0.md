# Release Wrap - v0.22.0

Date: `2026-08-30`

## Scope

`v0.22.0` is the normal feature and hardening release after `v0.21.2`. It contains the closed post-v0.21.2 audit/remediation backlog: performance and cache bounds, deployment and container hardening, CI expansion, backup/admin/history integrity, observability, and operator-trust fixes.

Release branch: `release/v0.22.0` from `origin/main` commit `084fe99625a2b7654718b7b159a9184c2fde7c1d`.

Release commit: pending final pre-tag evidence and merge.

Tag: `v0.22.0` pending.

Validated against `docs/RELEASE_CHECKLIST.md`.

`HANDOFF.md` and `TODO.md` are absent from this repository; the live issue tracker and release checklist are the available sources of task state.

## Checklist Evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `origin/main` at `084fe99625a2b7654718b7b159a9184c2fde7c1d`; open issues `0`; open PRs `0`; release worktree branch `release/v0.22.0`; target version changed only in the release-prep worktree | Pass |  |
| Python unit and syntax gates | yes | Pending final release-candidate Python 3.12/3.14 full and targeted suites | Blocked |  |
| JavaScript syntax gates | yes | Pending final release-candidate Node syntax and unit suites | Blocked |  |
| Docker build and health gates | yes | Pending isolated Docker build and UI/history/admin health on `h0073` (`Codex-dev-test-target`, `10.13.37.138`) | Blocked |  |
| Optional-sidecar runtime matrix | yes | Pending UI-only, UI+history, UI+admin, and full-stack matrix on isolated release ports | Blocked |  |
| Full Playwright/browser gates | yes | Pending full browser suite against the isolated restored development stack | Blocked |  |
| Feature-specific live API/UI gates | yes | Pending Storage Fabric trust-state, mapping, platform, admin, and no-console-error checks against the isolated development stack | Blocked |  |
| Local release perf harnesses | yes | Pending serial main/history harnesses against the isolated release-candidate stack | Blocked |  |
| Linux QA restore gate | yes | Pending full-data admin export/import validation on the isolated development host; source deviation from the old Windows example will be recorded exactly | Blocked |  |
| Restored Linux QA perf harnesses | yes | Pending serial main/history harnesses after restored collector state is idle | Blocked |  |
| Snapshot/export/offline artifact gate | yes | Pending estimate/download/offline browser smoke plus checked-in public-demo verification | Blocked |  |
| Docs/wiki/public-demo gate | yes | Release notes, changelog, roadmap, wiki current-version copy, and this wrap are drafted; final stale-version and public artifact checks are pending | Blocked |  |
| GHCR publish verification | yes | Requires published GitHub Release and release-triggered workflow digest/tag/source verification | Blocked |  |
| Deployment refresh/sniff tests | yes | Requires published digest, isolated development acceptance/teardown, verified FULL encrypted production admin-sidecar backup, immutable production activation, convergence, and rollback receipt evidence | Blocked |  |
| Post-release reopen | yes | Requires completed GHCR and deployment gates before reopening the next development version | Blocked |  |

## Planned development QA contract

- Target: `h0073` - `Codex-dev-test-target` - `10.13.37.138`.
- Fresh isolated directory and Compose project; no reuse of stale containers or receipts.
- Fresh port range `18280/18281/18282`; unrelated listeners on `18080/18081` remain untouched.
- Existing approved full-data QA state is copied into a new isolated source directory. A current candidate admin export/import cycle proves the restore path before acceptance.
- The candidate stack is stopped after acceptance. No JBOD-UI listener or running candidate container remains on the development host.

## Validator status

Current expected command:

- `/tmp/hermes-verify-issue47-py312/bin/python scripts/validate_release_wrap.py 0.22.0 --phase pre-tag --allow-blocked`

The strict pre-tag command must remain blocked until every pre-publish row is `Pass` or justified `N/A`:

- `/tmp/hermes-verify-issue47-py312/bin/python scripts/validate_release_wrap.py 0.22.0 --phase pre-tag`

## Remaining work

1. Complete source, Docker, optional-sidecar, browser, live feature, performance, restore, snapshot/export, and docs gates.
2. Replace every pre-publish Blocked row with exact evidence and pass the strict pre-tag validator.
3. Merge the reviewed release-prep commit, tag it, publish the GitHub release, and verify the GHCR digest/source.
4. Run published-digest development acceptance and stop the development stack.
5. Take and verify one FULL encrypted production admin-sidecar backup, deploy the immutable digest once, and verify convergence and rollback evidence.
6. Reopen the next development version and pass the final release-wrap validator.
