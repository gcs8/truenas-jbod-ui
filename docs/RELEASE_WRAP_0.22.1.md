# Release Wrap - v0.22.1

Date: `2026-08-30`

## Scope

`v0.22.1` is a narrow SemVer patch on top of published `v0.22.0`. It extracts and validates the encrypted 7z manifest before payload extraction, then grants the large file-backed path only to the canonical, selected, present `history_db` group and member. This lets the required production FULL backup cover the observed 3,240,521,728-byte history database without loading that member into Python memory.

Release branch: `hotfix/v0.22.1-large-backup-streaming` from `origin/main` commit `ab6b54fc6bd0211a5a626568c8fd55fe853ca6f6`.

Release candidate commit: pending.

Tag: `v0.22.1` pending.

Validated against `docs/RELEASE_CHECKLIST.md`.

## Checklist Evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Isolated worktree branch `hotfix/v0.22.1-large-backup-streaming` starts from published v0.22.0 merge commit `ab6b54fc6bd0211a5a626568c8fd55fe853ca6f6`; scope is limited to two-phase manifest-first 7z validation, file-backed large-history backup/restore, regression tests, version metadata, and release documentation | Pass |  |
| Python unit and syntax gates | yes | Python 3.12 and 3.14 each passed `998` tests with `4` skips after the manifest-binding fix; the focused backup module passed `130` tests; Ruff, compileall, and release-wrap validation passed | Pass |  |
| JavaScript syntax gates | yes | Deterministic Node unit tests passed `130/130`; canonical JavaScript syntax checks passed | Pass |  |
| Docker build and health gates | yes | Exact-candidate image build and three-service health remain required | Blocked |  |
| Optional-sidecar runtime matrix | yes | Exact-candidate UI/history/admin matrix remains required | Blocked |  |
| Full Playwright/browser gates | yes | Final-byte public-demo smoke passed `1` test with `4` expected skips, and admin clean-room browser QA passed `7/7`; the full live-stack browser suite remains required | Blocked |  |
| Feature-specific live API/UI gates | yes | Real 7z FULL encrypted export, wrong-passphrase rejection, mutation, file-backed restore, and post-restore verification with a history database larger than production remain required | Blocked |  |
| Local release perf harnesses | yes | Exact-candidate performance harnesses remain required | Blocked |  |
| Linux QA restore gate | yes | Development FULL encrypted restore using a history database larger than 3,240,521,728 bytes remains required | Blocked |  |
| Restored Linux QA perf harnesses | yes | Restored-stack performance harnesses remain required | Blocked |  |
| Snapshot/export/offline artifact gate | yes | Exact-candidate snapshot, export, and checked-in public artifact validation remain required | Blocked |  |
| Docs/wiki/public-demo gate | yes | Version metadata, release notes, changelog, roadmap, repo-local wiki, and release wrap identify v0.22.1; release-wrap tests and full Python suites passed; checked-in public demo validation passed at `7,179,043` raw bytes and `1,587,980` gzip bytes | Pass |  |
| GHCR publish verification | yes | Requires merged release commit, v0.22.1 GitHub Release, release-triggered workflow, and converged v0.22.1/0.22.1/latest digest verification | Blocked |  |
| Deployment refresh/sniff tests | yes | Requires published-digest development sniff and teardown, then verified production FULL encrypted backup and one immutable production deployment with convergence and rollback receipts | Blocked |  |
| Post-release reopen | yes | Requires completed GHCR and deployment gates before reopening the next development version | Blocked |  |

## Safety boundary

- Production remains unchanged until a FULL encrypted backup completes and is independently verified.
- Development acceptance uses `Codex-dev-test-target` and stops the JBOD-UI stack afterward.
- The patch does not broaden ZIP/TAR or non-history member limits.
- The compressed request limit remains 256 MiB, the archive member-count and compression-ratio checks remain active, and the 7z extraction tree remains private and bounded.

## Validator status

Current shape check:

- `/tmp/hermes-verify-issue47-py312/bin/python scripts/validate_release_wrap.py 0.22.1 --phase pre-tag --allow-blocked`

The strict pre-tag command must not pass until every pre-publish row above is `Pass` or justified `N/A`:

- `/tmp/hermes-verify-issue47-py312/bin/python scripts/validate_release_wrap.py 0.22.1 --phase pre-tag`

## Remaining work

1. Complete the targeted security rereview and open the v0.22.1 PR.
2. Build the exact PR candidate and complete development FULL restore, browser, and performance acceptance with a database larger than production.
3. Update the release evidence, require exact-head green CI, and merge the reviewed PR.
4. Publish and verify the immutable GHCR digest, then run the published-digest development smoke and stop the stack.
5. Take and verify the production FULL encrypted backup before the one allowed production deployment.
