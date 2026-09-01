# Release wrap: v0.22.2

Date: `2026-09-01`

## Scope

v0.22.2 is a SemVer patch on published v0.22.1. It releases the reviewed segmented-history architecture already merged to `main`, adds fail-closed scheduled-FULL-gated retention with durable backup authorization, preserves restore ownership for non-root runtime consumers, makes scheduled-backup status safely readable across the non-root Compose services, fixes immutable deployment candidate validation, and carries the dependency versions already present on `main`.

Release branch: `fix/v0.22.2-segmented-lifecycle`, rebased onto `origin/main` commit `2f4da463f6f33933d457593a061fa79050200b30`.

Release candidate commit: pending final candidate review and commit.

Previous broad QA runtime evidence used source identity `d423be58476542895542cd0f334cdaaa43a12eeb24f266aa92235f6019f604f3`. Blocker remediation tightened successful-artifact validation, required the configured application GID for shared status publication, corrected the mapping preview/confirmation performance path, and updated tests and release evidence. The affected successor runtime was built and tested from exact staged identity `26412788d7d76faba1af98807eeed971f623a89449db704da97ffab150f8f72f`. Only this evidence document changed afterward; application, Compose, test, and runtime files did not.

Tag: `v0.22.2` pending.

PR #120 is closed because current `main` already contains its `websockets==17.0.1` and `cryptography==50.0.0` updates. Issue #119 and draft PR #121 target v0.22.3 unless their required real-shelf evidence arrives before the v0.22.2 freeze. Crash-safe later-generation segment publication is tracked in issue #124 for v0.22.3 because the initial migration journal does not cover a later hot, segment, and catalog replacement transaction.

Validated against `docs/RELEASE_CHECKLIST.md`.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Isolated branch `fix/v0.22.2-segmented-lifecycle` is rebased onto current `main`; scope is scheduled-FULL-gated segmented retention, legacy snapshot suppression, restore ownership, non-root status sharing, deployment `.env` staging, version/docs, and release evidence | Pass |  |
| Python unit and syntax gates | yes | Status-sharing RED regressions fail on `0700`/`0600` and pass on `2750`/`0640`; full Python 3.12 and 3.14 each pass 1,099 tests with 4 declared skips; Ruff, compileall, and diff hygiene pass | Pass |  |
| JavaScript syntax gates | yes | 130 Node unit tests pass; application, admin, public-demo, Playwright config, and final `qa/ui-switching.spec.js` syntax checks pass | Pass |  |
| Docker build and health gates | yes | Successor image ID `sha256:64d2c39ae89e14b6b7c1747243eb44fc62fd9238ff0de7fec0754fa7c9e61c8c` was built from staged identity `26412788...f72f`; UI, history, and admin were healthy with zero restarts and returned HTTP 200 on `/livez` | Pass |  |
| Optional-sidecar runtime matrix | yes | The previous exact image passed isolated UI-only, UI+history, UI+admin, and all-service operation. The successor repeated the affected all-service plus one-shot-backup path, then removed every task-owned container and closed ports 28080 through 28082 | Pass |  |
| Full Playwright/browser gates | yes | Admin 7/7 passed; offline/public demo 6 passed with 4 expected interaction-only skips; final live UI suite passed 15 with 2 fixture-dependent skips; ESXi smoke skipped because the QA config has no saved ESXi system | Pass |  |
| Feature-specific live API/UI gates | yes | The successor rejected a `1000:1000 2750` status directory, accepted `1000:10001 2750`, published complete `1000:10001 0640` success evidence, byte-bound the archive size and SHA-256, allowed both non-root readers, rejected five incomplete-success shapes in the built image, and passed independent file-backed preflight with `history_db` selected and present | Pass |  |
| Local release perf harnesses | yes | The corrected main harness completed three serial iterations including mapping preview/confirmation; the history harness completed three serial iterations against the restored segmented store. SHA-256-bound artifacts were retained in task-owned temporary storage on the isolated QA target | Pass |  |
| Linux QA restore gate | yes | The previous candidate's encrypted schema-v2 FULL restore recovered expected hot/segmented counts, catalog and segment hashes, runtime ownership, and SQLite integrity with no rollback debris; restore code is unchanged by the successor | Pass |  |
| Restored Linux QA perf harnesses | yes | Restored main and history harnesses passed serially with labels `release-candidate-linux-qa-restore` and `release-candidate-history-linux-qa-restore`; artifact SHA-256 values were recorded and stack cleanup passed | Pass |  |
| Snapshot/export/offline artifact gate | yes | Offline snapshot and checked-in public-demo browser checks passed; snapshot estimate dialog passed in the final live suite | Pass |  |
| Docs/wiki/public-demo gate | yes | Version alignment, changelog, v0.22.2 release notes/wrap, segmented operations, setgid status setup, release checklist, and wiki consistency are updated; source contract tests pass | Pass |  |
| GHCR publish verification | yes | Requires merged release commit, v0.22.2 GitHub Release, release workflow, full immutable image reference, and exact source revision | Blocked |  |
| Deployment refresh/sniff tests | yes | Requires published-digest development smoke and cleanup, then a fresh independently verified production FULL backup before any production update | Blocked |  |
| Post-release reopen | yes | Requires completed GHCR and deployment evidence before opening the next development version | Blocked |  |

## Safety boundary

- Production remains on the current segmented digest until every pre-publish gate passes and a fresh encrypted FULL backup completes and is independently verified.
- Development acceptance used the approved isolated Linux QA target. The task-owned JBOD UI stack was removed afterward and ports 28080 through 28082 were verified closed. Existing listeners on ports 18080 and 18081 remained untouched.
- Segmented retention edits only the writable hot database. It never edits immutable segments.
- Missing, unreadable, malformed, stale, failed, history-excluding, history-absent, artifact-incomplete, symlinked, oversized, or writable scheduled-backup status blocks retention.
- The backup UID owns a setgid `2750` status directory with the explicitly configured app GID and publishes atomic `0640` status files. UI/history can read but cannot alter backup evidence.
- The hot SQLite database persists `ready`, `claimed`, and `consumed` backup authorization. Completed passes cannot repeat after restart, failed passes remain retryable, catch-up can continue, and interrupted claims require a newer backup.
- One documented-sequence QA backup succeeded and restored correctly. Two extra diagnostic attempts reached the 10-minute 7z cap and failed closed; production activation remains impossible unless its mandatory fresh FULL backup succeeds and is independently verified.
- PR #121 remains draft. Synthetic fixtures do not replace real-shelf validation.

## Validator status

Current shape check:

- `python3 scripts/validate_release_wrap.py 0.22.2 --phase pre-tag --allow-blocked`

The strict pre-tag command must not pass until every pre-publish row is `Pass` or has a justified `N/A` result:

- `python3 scripts/validate_release_wrap.py 0.22.2 --phase pre-tag`

## Remaining work

1. Freeze and complete the targeted I2/I4/I7/I8 exact-candidate rereview.
2. Commit and update PR #147, then merge only after exact-head CI passes.
3. Complete the strict pre-tag wrap and publish v0.22.2.
4. Verify the GHCR digest and source revision.
5. Complete the scheduled production observation, then take and independently verify a fresh production FULL backup before any production update.
