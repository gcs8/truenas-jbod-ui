# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `fdb624ed477f3e2e81fd4233290545911a37724d`.

This cut adds a merge commit to the existing release branch without rewriting
history. It recollects the complete main-branch changelog and fragments with
the repository tooling. All 80 fragment entries and the historical release
sections were preserved. Release notes describe the changes since rc.1 and
summarize the v0.24.0 line since stable.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.
Raw restore, appliance, resource and visual evidence remains private.
Source preparation is not authorization to merge, tag, publish or deploy.
No predecessor runtime receipt is claimed and no narrow-beta deferrals apply.

Changelog coverage: pass (122 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Exact-base main CI completed with 23 successful jobs and the expected push-only changelog skip; both CodeQL languages passed before the cut. All 80 fragments and historical section preservation passed; coverage includes 122 required PRs. The workflow's own tag resolver emits the two exact rc.2 aliases and dev, never latest; its 22 contract tests passed. | Pass | |
| Python unit and syntax gates | yes | Full source gate on clean committed 5643c6b174f423e8f08a37c0e528c2fb8b4958fe returned native exit 0 and FINAL: PASS. Python ran 6,995 tests with seven skips and no failures. Compileall, Ruff, whitespace, performance baseline and the checked-in demo check passed. Local promtool was unavailable and explicitly skipped; the exact-source CI jobs installed pinned promtool and passed. | Pass | |
| JavaScript syntax gates | yes | The same full source run passed all declared script/spec syntax checks. JavaScript unit summary: 2,076 total, 2,071 passed, five skipped, zero failed or cancelled. | Pass | |
| Docker build and health gates | yes | Frozen-source image built successfully from 5643c6b174f423e8f08a37c0e528c2fb8b4958fe. Local QA image ID: sha256:b5b1aa1bab2b5d2b1db9c47ab5dff702e80f8c00789ffbd3b190b253c7c616cd. OCI revision and application version match the cut. Runtime health remains pending because isolated container-name admission is blocked. This image is not published. | Blocked | |
| Optional-sidecar runtime matrix | yes | The unchanged controller refuses admission because the existing test installation occupies its fixed container names. The installation must remain running and unchanged. No matrix runtime started and no guard was bypassed. | Blocked | |
| Full Playwright/browser gates | yes | All four exact-source fixture CI legs passed. The separate round-two fixture run was intentionally interrupted and is not a complete browser result. A fresh per-file fixture/Admin/demo run is active with distinct output; no completed local group result is claimed yet. Restored checks, full read-only crawl and human-view qualification remain pending. | Blocked | |
| Feature-specific live API/UI gates | yes | The first CORE invocation passed ten of eleven cases with independently verified account/group/home cleanup; its loopback observer used stale warning wording. Nine observer unit tests now pass. A separate loopback-only replay on the same frozen cut returned HTTP 200 and passed the exact certificate-verification text, fixed expiry category and not-unreachable checks. Fixed-field classification was retained before runtime removal. No appliance connection or second account cycle occurred. Together these receipts complete all eleven CORE cases. Comparator, storage views, refresh/write identity, telemetry and census remain unrun. | Blocked | |
| Local release perf harnesses | yes | Both full three-iteration harnesses and CSV trails pending. | Blocked | |
| Linux QA restore gate | yes | Fresh default tar.zst and explicit 7z FULL round trips and the legacy live-read-only restore remain unrun. The unchanged restore controller independently refuses admission while the existing test installation occupies its fixed container names. | Blocked | |
| Restored Linux QA perf harnesses | yes | Both full three-iteration restored-stack harnesses and CSV trails pending. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Source and restored-stack estimate, Auto HTML, ZIP and offline browser acceptance pending. | Blocked | |
| Docs/wiki/public-demo gate | yes | Source diff, metadata, privacy, documentation, screenshot-manifest and prerelease demo checks pending. Public demo deferred: prerelease. | Blocked | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Publication is outside this preparation phase. | Blocked | |
| Deployment refresh/sniff tests | yes | Deployment is outside this preparation phase. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Qualification history and phase boundaries

The first cut stopped at two test-fixture failures after changelog collection
and during encrypted-import cleanup checks. PR #1454 corrected those test
assumptions on main. Its changes are included here; the original failed
results remain historical evidence, not passes for this successor.

Every pre-publish row must pass before release readiness. An unexecuted gate
is Blocked, not Pass or Deferred. Round two stopped at the failed CORE
controller assertion. Round three corrected the observer and passed only the
loopback UI case without repeating appliance mutation. The candidate image
was built with verified source/version identity. Matrix and restore
admission remain blocked by fixed container-name collisions under the
requirement to keep the existing test installation running and unchanged.
The PR remains draft. No restored stack or comparator ran, and no appliance
observation deltas were measured. The existing installation was never
parked, renamed, replaced or changed.

[CI run 44936144754](https://github.com/gcs8/truenas-jbod-ui/actions/runs/44936144754)
completed successfully on `5643c6b174f423e8f08a37c0e528c2fb8b4958fe`, with all
24 jobs successful. Both CodeQL languages passed. The release-metadata
selection ran 173 tests with one skip and no failures. No independent review
was requested while full qualification remained blocked. This evidence-only
follow-up changes the wrap, not the source that ran those checks. Its own
exact-head CI completed successfully in run 44938543582 with all 24 jobs
successful; both CodeQL languages passed in run 44938543491. Any subsequent
evidence-only commit requires its own exact-head CI.

Stable promotion, external wiki publication and production deployment remain
outside this phase.
