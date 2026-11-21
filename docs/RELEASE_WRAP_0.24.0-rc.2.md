# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `177cc0b70c87d33a02aa71d52d8f0241493ca5e7`.
Release cut: pending additive commit and exact-cut qualification.

This additive merge includes PR #1458's Compose matrix restart-readiness
correction without rewriting release-branch history. Repository tooling
recollected all 82 fragments and preserved every historical release section.
Release notes describe the changes since rc.1 and summarize the v0.24.0 line.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.
Raw restore, appliance, resource and visual evidence remains private.
Preparation does not authorize merge, tagging, publication or deployment.
No narrow-beta deferrals apply. Earlier native receipts remain historical until
complete product and observer-input identity is proved for this cut.

Changelog coverage: pass (124 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main merged additively. All 82 fragments and historical release sections preserved; coverage includes 124 required PRs. Exact-cut coverage and workflow resolver replay pending. | Blocked | |
| Python unit and syntax gates | yes | Full native source gate required on the clean committed successor. Earlier source runs do not qualify this exact cut. | Blocked | |
| JavaScript syntax gates | yes | Full native source and syntax checks required on the successor. | Blocked | |
| Docker build and health gates | yes | Fresh image build, exact OCI revision and version, and full runtime health matrix pending. No image is published or adopted. | Blocked | |
| Optional-sidecar runtime matrix | yes | All variants must run using the corrected restart-readiness producer. Historical matrix failures remain retained; no assertion is relaxed. | Blocked | |
| Full Playwright/browser gates | yes | Fixture, Admin and demo receipts require shipped-path and browser-input identity before carry. Restored browser checks, full read-only crawl and human-view remain mandatory. | Blocked | |
| Feature-specific live API/UI gates | yes | CORE eleven-case qualification and independent read-only comparator require input identity before carry. Storage-view, refresh/write-identity, telemetry and census gates pending. | Blocked | |
| Local release perf harnesses | yes | Both full three-iteration harnesses and CSV trails pending. | Blocked | |
| Linux QA restore gate | yes | Fresh default tar.zst and explicit 7z FULL export and restore round trips, plus legacy live-read-only restore, pending. | Blocked | |
| Restored Linux QA perf harnesses | yes | Both full three-iteration restored-stack harnesses and CSV trails pending. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Source and restored-stack estimate, Auto HTML, ZIP and offline browser acceptance pending. | Blocked | |
| Docs/wiki/public-demo gate | yes | Public privacy, docs, screenshot-manifest and prerelease demo checks pending. Public demo deferred: prerelease. No public screenshot or external wiki bytes changed. | Blocked | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Publication is outside this preparation phase. | Blocked | |
| Deployment refresh/sniff tests | yes | Deployment is outside this preparation phase. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Historical results and successor admission

Earlier cuts and their failed observers remain historical evidence. PR #1454
corrected the first cut's test assumptions. A tested loopback-only observer
replay completed the second cut's eleventh CORE case without repeating its
appliance-account cycle. Fixture, Admin and generated-demo browser runs passed.
The independent read-only membership comparator confirmed all 51 occupied
placements with zero failed reads. These results will be carried only after
complete shipped and observer-input identity is recorded.

The fifth round's initial mapping save returned HTTP 409. PR #1456 corrected
the matrix's unreachable occupancy fixture. The sixth round's ui-only variant
passed, but ui-history mapping clear after restart returned HTTP 503. PR #1458
corrects the synthetic producer's restart ordering and readiness. Neither
historical failure is relabelled as a pass. The complete matrix must rerun.

Earlier recovery restored the original container names, IDs and image with
running, healthy, zero-restart readback and no disposable resources. This new
run must independently prove recovery again, preserving original Compose
files, data and image. A fresh read-only alert, mail and retained-session
observation window surrounds the runtime gates; the comparator is not repeated.

The PR remains draft. Every mandatory pre-publish row must pass before readiness
or an independent-review request. No merge, tag, publication, deployment or
external wiki action is authorized here. Stable promotion remains out of scope.
