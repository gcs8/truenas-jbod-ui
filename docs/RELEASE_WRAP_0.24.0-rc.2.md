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
| Scope and branch | yes | Version cut and collected changelog await final metadata verification. | Blocked | |
| Python unit and syntax gates | yes | Full source gate not yet executed on the release commit. | Blocked | |
| JavaScript syntax gates | yes | Full source gate not yet executed on the release commit. | Blocked | |
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
is Blocked, not Pass or Deferred. Exact-head CI and independent review remain
separate from local qualification. Stable promotion, external wiki publication
and production deployment are outside this phase.
