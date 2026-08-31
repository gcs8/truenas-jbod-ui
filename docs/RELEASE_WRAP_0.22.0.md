# Release Wrap - v0.22.0

Date: `2026-08-30`

## Scope

`v0.22.0` is the normal feature and hardening release after `v0.21.2`. It contains the closed post-v0.21.2 audit/remediation backlog: performance and cache bounds, deployment and container hardening, CI expansion, backup/admin/history integrity, observability, and operator-trust fixes.

Release branch: `release/v0.22.0` from `origin/main` commit `084fe99625a2b7654718b7b159a9184c2fde7c1d`.

Release candidate commit: `c9e532d1faa472b106e9ffc2df78b3a35afb116c`; release commit pending final evidence update and merge.

Tag: `v0.22.0` pending.

Validated against `docs/RELEASE_CHECKLIST.md`.

`HANDOFF.md` and `TODO.md` are absent from this repository; the live issue tracker and release checklist are the available sources of task state.

## Checklist Evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `origin/main` at `084fe99625a2b7654718b7b159a9184c2fde7c1d`; open issues `0`; draft release PR `#117`; release worktree branch `release/v0.22.0`; candidate `c9e532d1faa472b106e9ffc2df78b3a35afb116c` | Pass |  |
| Python unit and syntax gates | yes | Python 3.12 and 3.14 each passed `984` tests with `4` skips; backup-focused module passed `116` tests; Ruff, compileall, dependency checks, and `promtool 3.5.0` passed | Pass |  |
| JavaScript syntax gates | yes | `npm run test:unit` passed `130/130`; changed and canonical JavaScript syntax checks passed | Pass |  |
| Docker build and health gates | yes | Exact candidate built as local image `sha256:e8e06c7b64fe97328c1af6de2c24c24a1b7ed0c4f710c97dd0488dbdd64c8a1c`; UI, history, and admin reported healthy `0.22.0`, zero restarts, and matching OCI revision | Pass |  |
| Optional-sidecar runtime matrix | yes | Exact candidate passed UI-only, UI+history, UI+admin, and full-stack phases on isolated ports; all three services were stopped afterward | Pass |  |
| Full Playwright/browser gates | yes | Canonical five-file run passed `31` tests with `4` expected skips after one bounded rerun for transient post-restore appliance refresh saturation; focused recovery rerun passed `2/2` before the canonical pass | Pass |  |
| Feature-specific live API/UI gates | yes | Final browser suite covered platform switching, ESXi enriched-or-truthful-fallback evidence, mapping, Storage Fabric, history, admin, snapshot, and console-error checks; direct history refresh added metrics without identity/topology churn | Pass |  |
| Local release perf harnesses | yes | Final candidate main harness passed three serial iterations; forced inventory averaged `13192.3 ms`, cached workflows stayed below `30 ms`, and snapshot estimate averaged `149.9 ms` | Pass |  |
| Linux QA restore gate | yes | FULL encrypted admin export/import selected all `10` restore-grade groups; wrong-passphrase rejection, deliberate cache mutation, exact post-export fingerprints, logical history counts, service restart, and archive/passphrase cleanup passed | Pass |  |
| Restored Linux QA perf harnesses | yes | Final candidate history harness passed estimated and exact-count modes; estimated overview averaged `9.2 ms` and exact overview averaged `12896.2 ms` | Pass |  |
| Snapshot/export/offline artifact gate | yes | Final Playwright run passed snapshot estimate/dialog and offline/public-demo coverage; checked-in public demo artifact validation passed | Pass |  |
| Docs/wiki/public-demo gate | yes | Release notes, changelog, roadmap, wiki current-version copy, release wrap, stale-version checks, performance baseline, and checked-in public artifact checks passed | Pass |  |
| GHCR publish verification | yes | Requires published GitHub Release and release-triggered workflow digest/tag/source verification | Blocked |  |
| Deployment refresh/sniff tests | yes | Requires published digest, isolated development acceptance/teardown, verified FULL encrypted production admin-sidecar backup, immutable production activation, convergence, and rollback receipt evidence | Blocked |  |
| Post-release reopen | yes | Requires completed GHCR and deployment gates before reopening the next development version | Blocked |  |

## Planned development QA contract

- Target: `h0073` - `Codex-dev-test-target`; the maintainer-only inventory address is intentionally omitted.
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

1. Pass the strict pre-tag validator and merge PR `#117`.
2. Tag the merge commit, publish the GitHub release, and verify the GHCR digest/source.
3. Run published-digest development sniff tests and stop the development stack again.
4. Take and verify one FULL encrypted production admin-sidecar backup, deploy the immutable digest once, and verify convergence and rollback evidence.
5. Reopen the next development version and pass the final release-wrap validator.
