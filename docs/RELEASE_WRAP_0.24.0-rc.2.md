# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `255ebfa1b708b3267a91cb67cae3f853fdd2bfb8`.

This candidate collects the changelog fragments, release metadata and the
changes since rc.1. Qualification must run afresh. No predecessor runtime
receipt is claimed for this candidate and no narrow-beta deferrals apply.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.
Raw restore, appliance, resource and visual evidence remains private.
Source preparation is not authorization to merge, tag, publish or deploy.

Changelog coverage: pass (121 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Exact-base main CI and CodeQL passed before the cut. Release metadata, all 79 collected fragment entries, historical section preservation and coverage of 121 required PRs passed. Publish-step resolution retains the exact rc.2 aliases and dev, never latest. | Pass | |
| Python unit and syntax gates | yes | Full source gate on clean commit 48569022f1171ed7eeaa06bd1a92a182557a6c26 returned exit 1 and FINAL: FAIL. Python ran 6,995 tests with seven skips and two failures: the changelog collector's checked-in Unreleased-body assertion after collection, and the backup wrong-passphrase/damage test's leftover import-bundle assertion. Compileall and Ruff passed. Both CI Python shard-1 jobs also failed the collector assertion. No correction or rerun was made. | Blocked | |
| JavaScript syntax gates | yes | Native full source run at 48569022 passed all declared script and spec syntax checks. JavaScript unit summary: 2,076 total, 2,071 passed, five skipped, zero failed or cancelled. This does not clear the failed Python source gate. | Pass | |
| Docker build and health gates | yes | Candidate image not yet built or exercised. | Blocked | |
| Optional-sidecar runtime matrix | yes | Complete candidate-image matrix pending. | Blocked | |
| Full Playwright/browser gates | yes | Fixture groups, Admin, demo, restored browser checks and full read-only crawl pending. | Blocked | |
| Feature-specific live API/UI gates | yes | CORE VM, independent comparator, storage views, refresh/write identity, telemetry, census and human-view evidence pending. Persisted CORE account/grant checks require specific authorization before execution. | Blocked | |
| Local release perf harnesses | yes | Both full three-iteration harnesses and CSV trails pending. | Blocked | |
| Linux QA restore gate | yes | Fresh default tar.zst and explicit 7z FULL round trips and the legacy live-read-only restore pending. | Blocked | |
| Restored Linux QA perf harnesses | yes | Both full three-iteration restored-stack harnesses and CSV trails pending. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Source and restored-stack estimate, Auto HTML, ZIP and offline browser acceptance pending. | Blocked | |
| Docs/wiki/public-demo gate | yes | Source diff, metadata, privacy, documentation, screenshot-manifest and prerelease demo checks pending. Public demo deferred: prerelease. | Blocked | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Publication is outside this preparation phase. | Blocked | |
| Deployment refresh/sniff tests | yes | Deployment is outside this preparation phase. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Phase boundaries

Every pre-publish row must pass before release readiness. An unexecuted gate
is Blocked, not Pass or Deferred. Qualification stopped at the failed source
gate. No candidate QA image was built and no live drill ran. The release PR
remains draft. Full source execution is attributed to commit
`48569022f1171ed7eeaa06bd1a92a182557a6c26`, tree
`335a4c93ce19e6ab752d5993b3f7b5f1bb92f701`. This wrap-only evidence update is
not a new source or runtime qualification.

[CI run 44928813095](https://github.com/gcs8/truenas-jbod-ui/actions/runs/44928813095)
failed on that exact source commit: 19 successful jobs and five failed jobs.
The failures are Python shard 1 on both versions, their two aggregate jobs
and the downstream public-demo aggregate because Python source failed. All
four fixture-browser legs passed, but they do not replace full local and
restored-stack browser qualification. Both CodeQL languages passed. No
independent review was requested while qualification was blocked.

Stable promotion, external wiki publication and production deployment are
outside this phase.
