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
| Docker build and health gates | yes | Frozen-source image built successfully from 5643c6b174f423e8f08a37c0e528c2fb8b4958fe. Local QA image ID: sha256:b5b1aa1bab2b5d2b1db9c47ab5dff702e80f8c00789ffbd3b190b253c7c616cd. OCI revision and application version match the cut. The first UI-only matrix variant became healthy and recovered after its intentional restart, but its subsequent mapping cycle failed. The complete runtime matrix remains unqualified. This image is not published. | Blocked | |
| Optional-sidecar runtime matrix | yes | The maintained controller returned native exit 1 in the first UI-only variant. Its initial slot-mapping POST required HTTP 200 but received 409 at scripts/run_compose_runtime_matrix.py:1003. The response body was not retained, so the conflict category is unknown. No assertion was relaxed and no retry was run. Later variants are unexecuted. The existing test installation was restored and independently verified. | Blocked | |
| Full Playwright/browser gates | yes | All four exact-source fixture CI legs passed. The completed independent per-file run returned native exit 0 for all 16 files: 14 fixture files with 556 passed, Admin with 15 passed, and the generated demo with nine passed. Total 580 passed, zero skipped. Every native summary and exit was retained. The earlier interrupted run remains historical evidence. Restored checks, full read-only crawl and human-view qualification remain pending. | Blocked | |
| Feature-specific live API/UI gates | yes | The first CORE invocation passed ten of eleven cases with independently verified account/group/home cleanup; its loopback observer used stale warning wording. Nine observer unit tests now pass. A separate loopback-only replay on the same frozen cut returned HTTP 200 and passed the exact certificate-verification text, fixed expiry category and not-unreachable checks. Fixed-field classification was retained before runtime removal. No appliance connection or second account cycle occurred. Together these receipts complete all eleven CORE cases. A fresh private comparator copy updates only the stale version pin to this candidate. Its 17 unit tests and five positive/negative version-and-path controls passed with networking disabled and no credentials mounted. One actual read-only collection then passed independent membership comparison for all occupied placements, with zero unconfirmed placements or failed initial/enrichment reads. The earlier version-refusal receipt is preserved. Storage views, refresh/write identity, telemetry and census remain unrun after the matrix failure. | Blocked | |
| Local release perf harnesses | yes | Both full three-iteration harnesses and CSV trails pending. | Blocked | |
| Linux QA restore gate | yes | Fresh default tar.zst and explicit 7z FULL round trips and the legacy live-read-only restore remain unrun. The sequence stopped at the first Compose matrix mapping cycle before a restore controller was invoked. | Blocked | |
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
was built with verified source/version identity. Its runtime admission was
then held by a requirement to keep the existing installation running.

Round four superseded that admission boundary with explicit brief
stop/rename/restore permission. The independent browser run completed with
all 16 files passing before any runtime transition. The unchanged comparator
then refused the candidate because its first assertion still pins rc.1 rather
than rc.2. Offline execution of that exact assertion confirmed the version
mismatch with networking disabled and no credentials mounted. That assertion
precedes configuration loading and appliance transport. The comparator's
fixed-field failure is preserved; no membership result or comparator appliance
read is claimed. No harness correction or comparator replay was performed.

Round four stopped before parking the existing installation. Its original
full container IDs and image IDs remained unchanged, both healthy with zero
restarts. No matrix or restore runtime was created during that round.

Round five corrected the private comparator's version pin after offline
positive and negative admission controls, preserving every read-only, endpoint,
credential and command-budget guard. Its single actual collection passed.
The existing test installation was then briefly stopped and renamed under the
inherited recovery handler. The first UI-only Compose variant became healthy
and completed its alias persistence cycle, but its initial slot-mapping save
received HTTP 409 instead of the required 200. The maintained repository
controller is unchanged. The native failure and traceback are retained; the
conflict response body was not retained, so no specific cause is asserted.

The sequence stopped immediately and restored the original containers. A
separate readback confirmed the original full IDs, names and image, running
and healthy with zero restarts. Disposable containers, networks and volumes
were absent, all task listeners were closed and no task credential-copy or
worker processes remained. No restore, format round trip, performance, feature,
crawl or human-view phase ran. The PR remains draft and those gates remain
Blocked. No independent-review request, merge, tag, publication or deployment
occurred. The delayed appliance snapshot was taken after two idle telemetry
observations and more than five minutes after independent recovery readback.
No new alert-check mail or alert-count increase was observed. Retained total,
internal and QA-origin session counts were unchanged, so the conditional
additional settled snapshot was not required. Per-session creation and
credential-type metadata is retained privately. These are net observations,
not claims about load causation.

[CI run 44936144754](https://github.com/gcs8/truenas-jbod-ui/actions/runs/44936144754)
completed successfully on `5643c6b174f423e8f08a37c0e528c2fb8b4958fe`, with all
24 jobs successful. Both CodeQL languages passed. The release-metadata
selection ran 173 tests with one skip and no failures. No independent review
was requested while full qualification remained blocked. This evidence-only
follow-up changes the wrap, not the source that ran those checks. Its own
exact-head CI completed successfully in run 44938543582 with all 24 jobs
successful; both CodeQL languages passed in run 44938543491. The next
evidence-only head, `32baabf1a124e9b2b1f844db271561a26e9aa638`, also has
24 successful jobs in run 44940582388 and both successful CodeQL language
jobs in run 44940582390. These checks do not clear the unexecuted runtime
rows. Any subsequent evidence-only commit requires its own exact-head CI.

Stable promotion, external wiki publication and production deployment remain
outside this phase.
