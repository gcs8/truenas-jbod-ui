# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `3d17ab6970913991d46a4383e26df1bb52e79b08`.
Qualification outcome: **Blocked**, fresh qualification in progress.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.

This additive recut includes the Topology accessible-name correction #1467
and QA-only forced-read readiness correction #1466. All 87 collected fragments
and every historical release section were preserved. No release history was
rewritten. Changelog coverage: pass (129 PRs)

The expected sole shipped delta from
`8c9ace94ec82638cc89d516841d9ba161daf3428` is
`app/static/sas_fabric_topology.js`. Exact per-path and image-file proof is
required before retaining backend-only evidence. Those gates do not load this
browser script. Browser qualification is fresh, not carried.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main merged additively; fragments and release history preserved. Coverage passes for 129 PRs. Exact shipped identity and publication resolver pending. | Blocked | |
| Python unit and syntax gates | yes | Fresh clean-cut dev_check --full required. | Blocked | |
| JavaScript syntax gates | yes | Fresh native syntax and complete unit gates required. | Blocked | |
| Docker build and health gates | yes | Fresh exact-cut image and copied-file identity required. | Blocked | |
| Optional-sidecar runtime matrix | yes | Prior eight-variant result retained; original receipts and controlling-input identity must be rebound. | Blocked | |
| Full Playwright/browser gates | yes | Fresh complete fixture/Admin/generated-demo run and all 42 read-only crawl chunks required. Human-view includes Topology unpinned and pinned node, bundle and lane at 1600 and 3840 px, with SVG title preservation. Native hover appearance remains for the owner because headless browsers cannot show it. | Blocked | |
| Feature-specific live API/UI gates | yes | Prior CORE, comparator, four feature drivers and immediate census results require receipt/input proof. Fresh end-of-hold tree proof and bounded observation remain mandatory. | Blocked | |
| Local release perf harnesses | yes | Prior complete stock local app/history pair retained pending original receipt/input proof; no claim of fresh execution. | Blocked | |
| Linux QA restore gate | yes | Prior complete legacy live drill, separate tar.zst and explicit 7z FULL round trips and both offline drills retained pending original receipt/input proof. | Blocked | |
| Restored Linux QA perf harnesses | yes | Fresh full stock app/history pair with exact labels, three iterations each, CSV artifacts and all mapping-import cycles required. Readiness budget is 330 seconds. Supplemental runs cannot substitute. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Prior restored estimate, Auto HTML and explicit ZIP exports and offline renders retained pending original receipt/input proof. Private artifacts are not published. | Blocked | |
| Docs/wiki/public-demo gate | yes | Release metadata and public-safety checks pending. Public demo deferred: prerelease. | Blocked | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Not authorized in this preparation phase. No image published. | Blocked | |
| Deployment refresh/sniff tests | yes | Not authorized in this preparation phase. No installation adopted the candidate. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Qualification boundary

The preceding crawl stopped at a serious Topology path-node label-in-name
finding. Exact finding, contemporaneous PNG and scoped API response remain
private. The original test installation was recovered on its original IDs and
image, healthy with zero restarts. That historical hold is not a fresh pass.

This recut requires fresh exact-byte checks, a new image and full browser run,
then all required held-runtime gates. Backend-only carries keep their original
executed revisions, images and native receipts. No narrow-beta deferrals apply.
The PR remains draft until every pre-publication row qualifies. No merge, tag,
publication, deployment, stable-channel promotion or external wiki action is
authorized in this phase.
