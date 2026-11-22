# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `3d17ab6970913991d46a4383e26df1bb52e79b08`.
Release cut: `8e2f425ba654a4b54321a0006d719ff3315a7e3a`.
Qualification outcome: **Pass** for phase A. Publication and deployment remain separate.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.

This additive recut includes the Topology accessible-name correction #1467
and QA-only forced-read readiness correction #1466. All 87 collected fragments
and historical release sections were preserved. No release history was rewritten.

Changelog coverage: pass (129 PRs)

The sole shipped delta from `8c9ace94ec82638cc89d516841d9ba161daf3428` is
`app/static/sas_fabric_topology.js`. Per-path objects and SHA-256 values,
including all 166 copied image files, prove that scope. All other bytes and all
modes, owners and symlink targets match. Backend-only carries below do not load
that script and keep their original executed revisions, images and receipts.
Browser evidence is fresh, not carried.

Fresh local QA image:
`sha256:eeec72a46a8e03031b02588b038377c23f091c5fa8995428c1fd2e9426b93bb2`.
Its OCI revision is the release cut above and version is `0.24.0-rc.2`.
It is not a registry publication or deployment.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main merged additively; 87 fragments and historical sections preserved. Coverage 129 PRs. Exact shipped identity passes. Actual resolver emits fixed version aliases and dev only; 22 resolver contract tests pass. | Pass | |
| Python unit and syntax gates | yes | Native dev_check --full on clean exact release cut: 7,048 tests run, seven skipped, no failures; compile/source gates and FINAL PASS. Start/end commit and tree match. Exact-cut CI covers the local Prometheus-tool skip. | Pass | |
| JavaScript syntax gates | yes | Full maintained syntax checks; 2,108 unit tests total, 2,103 passed, five skipped, zero failed/cancelled/todo. | Pass | |
| Docker build and health gates | yes | Fresh exact-cut image, OCI revision/version and 166-file proof pass. Disposable services reached healthy authenticated runtime. Original test installation independently recovered on original full container IDs, names and image, healthy with zero restarts; task resources absent. Not deployment acceptance. | Pass | |
| Optional-sidecar runtime matrix | yes | Carried from 6150cce10e1e9af4300efcf2a786235266a14246: all eight declared variants, native complete marker and original receipt hashes verified. Backend/controller inputs unchanged; sole shipped browser-script delta is not loaded. | Pass | |
| Full Playwright/browser gates | yes | Fresh 16-file fixture/Admin/generated-demo run: 602 passed, zero skipped/failed. Full read-only crawl: 42/42 chunks native zero and safety ok; 40 targets, 1,021 records, 1,732 captures across 390/1600/1920/3840. Zero unknown errors, new error identities, blocked requests or admitted writes. Actual human-view and Topology title DOM proof pass; native hover appearance remains for owner. | Pass | |
| Feature-specific live API/UI gates | yes | CORE 11/11 and comparator 51/51 carried with original receipts/input identity. Four preceding feature drivers and immediate census behavior carried from 8c9ace94ec82638cc89d516841d9ba161daf3428. Fresh pre-crawl/end-of-hold segment-tree files match byte/metadata. Bounded read-only observation closes with zero alert-check mail, alert, mail-job or retained-session delta. Sole browser-script delta is not loaded by carried backend gates. | Pass | |
| Local release perf harnesses | yes | Complete stock local app/history pair carried from b3dc216ab4cd93d361cf5b52124e224af76969fd: exact labels, three iterations each, CSV trails and all mapping-import cycles. Original receipts and backend/controller identity verified. Same disposable restored stack supplied local/restored label sets; not independent installations. Explicit carry across the later QA observer correction, not claimed observer-byte-identical execution. | Pass | |
| Linux QA restore gate | yes | Separate tar.zst and explicit 7z FULL round trips and both offline drills carried from 6150cce10e1e9af4300efcf2a786235266a14246. Complete preceding legacy live drill including performance carried with identity from 8c9ace94ec82638cc89d516841d9ba161daf3428. Native full setup repeated to instantiate this disposable stack without bypassing mandatory checks; counts, restart survival, both pencil cycles/cleanup, offline browser 16 passed/one explicit skip, live browser 16 passed/two explicit skips, app/history performance pass. Optional seed absences retain original provenance. Final independent cleanup/recovery pass. | Pass | |
| Restored Linux QA perf harnesses | yes | Fresh full stock app/history pair passes with exact required labels, three iterations each, latest JSON and CSV artifacts, all three empty-bundle mapping preview/apply cycles and empty-bundle readback. Readiness budget 330 seconds in actual nested child and full pair. Supplemental omitted-workflow output is not substituted. | Pass | |
| Snapshot/export/offline artifact gate | yes | Restored estimate, Auto HTML and explicit ZIP exports and both offline renders carried with original receipt/input proof from 8c9ace94ec82638cc89d516841d9ba161daf3428. Native setup also repeated this path: HTTP 200, zero external requests and console errors. Private partially redacted artifacts not published. | Pass | |
| Docs/wiki/public-demo gate | yes | Release notes, source diff, metadata, privacy, public documentation, screenshot manifest and prerelease policy checks pass. Public demo deferred: prerelease. No public screenshot or external wiki bytes changed. Wrap-only final head requires its own exact-head CI. | Pass | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Not authorized in this preparation phase. No image published. | Blocked | |
| Deployment refresh/sniff tests | yes | Not authorized in this preparation phase. No installation adopted the candidate. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Crawl and human-view disposition

All 42 planned chunks completed without a browser reconnect or replay. Native
records are 802 done, 180 not-applicable and 39 listed. Listed write, LED and
appliance-load states remain unexercised under the read-only boundary; they are
not passes. Disabled synchronization states are not-applicable. The plan covers
all configured systems, discovered main scopes, saved HA views, Storage Fabric,
history and Admin. Metric-option expansion retains the stock first-scope rule.
There are 433 native captures per width.

Known findings remain explicit: 252 label-in-name occurrences under #1110,
36 heat-spread errors and 12 heat-spread warnings, 32 heat-contrast errors and
62 heat-alarm warnings under #1103. Exact identity comparison with the accepted
full baseline finds zero added error identities. Topology has zero unknown
errors under the narrowed rule; no new component defect was waived by issue tag.

Human review covers actual chassis and saved-view face families, history/Admin,
and each Topology backend family. All eight families have unpinned, pinned-node
and pinned-bundle captures at 1600/3840. Reported lanes exist in one family and
are captured at both widths; the other seven families expose no reported-lane
badge, so that state is explicitly not-applicable there. All observed node,
bundle and lane SVG groups retain their native title element. Native hover
appearance is left to the owner because headless browsers cannot show it.

Existing sticky trace-band overlap, viewport scrolling and retained blank room
match the accepted baseline and established layout behavior. Long source-identity
wrapping and generic controller wording in the inspector are unchanged, not new
regressions introduced by this accessible-name correction. No new visual class
requiring a product fix was found.

## Observations, recovery and evidence limits

The original test installation was parked and restored by its sole recovery
owner. Independent successful container/network/volume censuses are empty; ten
task listeners are closed, no helpers/key-copy directories/runtime remain, and
source is clean at the frozen cut. Original Compose files, data and image were
not changed.

The bounded observation includes idle post-readback after the five-minute
recovery gap. Alerts, mail jobs and retained sessions are unchanged; no new
alert-check mail. The preceding window's net mail-job increase was not an
alert-check failure. A single bounded read-only metadata diagnosis identifies
two successful sends with SMART warning/report signatures during that older
window. Its net retained-job count is not a count of newly started sends; absent
historical job identities prevent a stronger one-to-one causal attribution.
No raw mail subject or body was retained.

History anonymous memory peaked at 133.86 MiB and Docker's sampled memory at
164.90 MiB, with no observed OOM. Charged cgroup memory reached the configured
1 GiB limit, so it is not described as wholly below that limit. SQLite temporary
files were confined to the history temporary directory. Middleware role peaks
were two and one, a conservative combined three within the four-call budget.
Seven failed metric reads remain explicit among 4,633 observations.

One earlier local receipt collector rejected a successful native setup because
it retained the preceding cut literal. The original stop and verified recovery
remain in evidence. Test-first private-harness correction updated only that
literal, preserving wrong-cut/status/image refusals. A fresh runtime generation
then completed qualification on the same source/image. An offline duplicate-log
allocation defect was also corrected before that owner loaded, without changing
native outcomes, read-only enforcement or recovery. No product repair or safety
check relaxation occurred in this release lane.

Exact-cut CI Preflight run 45042241558 passes all 24 expected jobs and CodeQL
45042241503 both languages, attempt one. Final wrap-only CI and required review
are recorded separately on the current PR head before promotion. No narrow-beta
deferrals apply. No merge, tag, publication, deployment, stable-channel promotion
or external wiki action occurred in phase A.
