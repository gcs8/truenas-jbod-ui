# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `6c79e68c237a4bda5edf655ca23970270dd855ee`.

This additive cut includes PR #1456's synthetic live-source Compose matrix
correction without rewriting release-branch history. The repository tooling
recollected all 81 fragments and preserved every historical release section.
Release notes describe the changes since rc.1 and summarize the v0.24.0 line.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.
Raw restore, appliance, resource and visual evidence remains private.
Preparation does not authorize merge, tagging, publication or deployment.
No narrow-beta deferrals apply. Prior evidence is retained under its original
source identity, not represented as fresh execution on this successor.

Changelog coverage: pass (123 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main revision merged additively. All 81 fragments and historical release sections preserved; coverage includes 123 required PRs. Exact-cut channel resolver verification pending. | Blocked | |
| Python unit and syntax gates | yes | Full native source gate on the new committed release cut pending. Predecessor results are historical only. | Blocked | |
| JavaScript syntax gates | yes | Exact-cut native syntax and JavaScript unit gates pending. | Blocked | |
| Docker build and health gates | yes | Exact-cut image build, OCI revision, application version and complete runtime health matrix pending. No image has been published. | Blocked | |
| Optional-sidecar runtime matrix | yes | PR #1456 supplies a synthetic live occupancy source for verifiable mapping writes. Fresh execution of every variant pending. No expected status or product admission check is weakened. | Blocked | |
| Full Playwright/browser gates | yes | Predecessor fixture, Admin and generated-demo evidence has 580 passed and zero skipped across 16 native per-file runs. Carry requires the complete shipped-path and browser-input identity proof. Restored browser, read-only crawl and human-view gates pending. | Blocked | |
| Feature-specific live API/UI gates | yes | Predecessor CORE has eleven completed cases and independent account, group and home cleanup. Predecessor independent read-only membership comparator passed with zero unconfirmed placements and no failed initial or enrichment reads. Carry requires complete shipped-path identity proof. Fresh remaining feature gates pending. | Blocked | |
| Local release perf harnesses | yes | Both full three-iteration harnesses and CSV trails pending. | Blocked | |
| Linux QA restore gate | yes | Fresh default tar.zst and explicit 7z FULL export and restore round trips, plus legacy live-read-only restore pending on the new cut and image. | Blocked | |
| Restored Linux QA perf harnesses | yes | Both full three-iteration restored-stack harnesses and CSV trails pending. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Source and restored-stack estimate, Auto HTML, ZIP and offline browser acceptance pending. | Blocked | |
| Docs/wiki/public-demo gate | yes | Exact-cut source diff, metadata, privacy, documentation, screenshot-manifest and prerelease demo checks pending. Public demo deferred: prerelease. | Blocked | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Publication is outside this preparation phase. | Blocked | |
| Deployment refresh/sniff tests | yes | Deployment is outside this preparation phase. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Qualification history and phase boundaries

The first cut stopped at test assumptions corrected by PR #1454. The second
cut was `5643c6b174f423e8f08a37c0e528c2fb8b4958fe`. Its full native source
gate and exact-source CI passed. Its CORE controller completed ten appliance
cases before a stale loopback warning matcher refused the changed sanitized
wording. A tested observer-only replay completed the eleventh case without a
second appliance-account cycle. The source-bound image built successfully and
all 16 fixture, Admin and generated-demo browser files passed.

The fourth round stopped before runtime startup at a stale private comparator
version guard. The fifth round corrected only that pin after network-disabled,
credential-free positive and negative controls. One actual read-only collection
then passed. Its delayed observation found no new alert-check mail and unchanged
alert, mail-job and retained-session counts after idle telemetry. These are net
observations, not load-causation claims.

The fifth round briefly parked the existing test installation under its
recovery handler. The first Compose variant completed health and alias
persistence checks, then its initial mapping POST received HTTP 409 instead of
200. The response body was not retained. Independent restoration readback proved
the original container IDs, names and image, healthy with zero restarts, and
absence of disposable resources, listeners, credential copies and workers.
The original Compose files, data and image were not edited. Later gates did not
run. The preserved failure is not a pass. PR #1456 subsequently corrected the
repository matrix's unreachable occupancy fixture on main.

The sixth round must prove every image-copied product path and supporting build
input identical to the second cut before carrying CORE, fixture-browser and
comparator results. Every source, CI and image gate is rerun, along with the
corrected matrix and every remaining runtime gate. Any shipped difference stops
that carry. All unexecuted rows remain Blocked, and the PR remains draft until
all pre-publish gates qualify. No readiness event or independent-review request
has occurred for this cut.

Stable promotion, external wiki publication and production deployment remain
outside this phase.
