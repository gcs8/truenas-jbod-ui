# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `b232fbd47c75eb1d6f083ddddf19a83dcb90504f`.
Release cut: `8c9ace94ec82638cc89d516841d9ba161daf3428`.
Qualification outcome: **Blocked**, not release-ready.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.

This additive recut includes the Topology accessibility correction #1464 and
performance-observer readiness correction #1462. All 85 collected fragments and
every historical release section were preserved. No release history was rewritten.
Release notes describe changes since rc.1 and summarize the v0.24.0 line.

Changelog coverage: pass (127 PRs)

The only shipped change from `b3dc216ab4cd93d361cf5b52124e224af76969fd` is
`app/static/sas_fabric_topology.js`. Per-path objects and SHA-256 values, plus
all 166 copied image files, were checked. All other shipped bytes and all modes,
owners and symlink targets match. Backend-only carries below do not load that
browser script and retain their original executed source and image identities.
Browser evidence is fresh, not carried.

Fresh local QA image:
`sha256:b0e87af0cc0c1f107341c6b1f5ecb5c772ef72d0a71bc1d551006e94f38dcedd`.
Its OCI source revision is the release-cut source above, and its application
version is `0.24.0-rc.2`. It is not a registry publication or deployment.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main merged additively; 85 fragments and historical sections preserved. Coverage passes for 127 PRs. Actual publication resolver emits the fixed version aliases and dev only; 22 resolver contract tests pass. | Pass | |
| Python unit and syntax gates | yes | Native dev_check --full on the clean release cut: 7,044 tests run, seven skipped, no failures; compile and source gates pass, final PASS. Start/end commit and tree match. Exact-cut CI supplies the local Prometheus-tool skip. | Pass | |
| JavaScript syntax gates | yes | Complete native syntax checks and 2,088 unit tests total: 2,083 passed, five skipped, zero failed, cancelled or todo. | Pass | |
| Docker build and health gates | yes | Fresh exact-cut image, version/revision and copied-file proof pass. Disposable restore services reached healthy runtime and authenticated browser acceptance. Original test installation recovered on the same container IDs/image, healthy with zero restarts; task resources absent. Not deployment acceptance. | Pass | |
| Optional-sidecar runtime matrix | yes | Carried from 6150cce10e1e9af4300efcf2a786235266a14246: all eight declared native variants, complete success marker and original receipt hashes verified. Backend and controlling inputs unchanged; the sole shipped Topology browser-script delta is not loaded by this gate. | Pass | |
| Full Playwright/browser gates | yes | Retained exact-cut 16-file fixture/Admin/generated-demo run: 594 passed, zero skipped/failed, not rerun. Fresh complete legacy restored browser: offline 16 passed/one explicit skip; live 16 passed/two explicit skips. Read-only crawl stopped at chunk 19 of the 42-chunk plan: 18 complete chunks passed; the next chunk found one unknown serious Topology path-node label-in-name mismatch, selector .kind-path, at 1600 px. Exact finding, PNG and scoped HTTP 200 API response were retained before recovery. Partial human-view does not clear full coverage. | Blocked | |
| Feature-specific live API/UI gates | yes | CORE 11/11 and independent read-only comparator 51/51 carried with original receipts and backend/input proof; the sole Topology script delta is not loaded. Fresh storage-view, concurrent-refresh/save, read-retry and counter/census feature drivers pass. Immediate census before/after files match in bytes and metadata; final end-of-hold segment-tree check was not reached after crawl stop. Bounded observation closed with no new alert-check mail, alert change or retained-session increase; mail-job count increased by one, cause unassigned. | Blocked | |
| Local release perf harnesses | yes | Complete prior local app/history pair carried from b3dc216ab4cd93d361cf5b52124e224af76969fd: exact labels, three iterations each, CSV trails and all mapping-import cycles. Original receipts verified. Same restored disposable stack supplied both local and restored labels. Backend unchanged; Topology browser script not loaded. The later observer correction does not imply fresh execution of this explicitly carried pair. | Pass | |
| Linux QA restore gate | yes | Default tar.zst and explicit 7z FULL round trips and both offline drills carried from 6150cce10e1e9af4300efcf2a786235266a14246 with original complete receipts and backend/controller-input proof. Fresh complete legacy live restore passes inspection, import, aggregate counts, restart survival, both pencil cycles, offline/live browsers and app/history performance. The seed legitimately lacks optional runtime overrides and fabric aliases. Final independent disposable-resource cleanup and original-installation recovery pass. | Pass | |
| Restored Linux QA perf harnesses | yes | The legacy single-iteration app/history checks and a supplemental three-iteration CSV pair pass with 330-second app readiness. The supplemental app run omits mapping import, and its history label is not the mandatory stock label. The complete default-workflow pair with exact labels and all mapping-import cycles was scheduled after crawl and was not reached. Not substituted by the supplemental pair. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Fresh restored snapshot estimate, Auto HTML export and explicit ZIP export returned HTTP 200. Both extracted offline artifacts rendered with the offline badge, zero external requests and zero console errors. Private partially redacted artifacts were not published. Native export and browser exits both zero. | Pass | |
| Docs/wiki/public-demo gate | yes | Release notes, source diff, metadata, public-text privacy, documentation and screenshot-manifest checks pass. Public demo deferred: prerelease. No public screenshot or external wiki bytes changed. Failure and evidence limitations remain explicit. | Pass | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Not authorized in this preparation phase. No image published. | Blocked | |
| Deployment refresh/sniff tests | yes | Not authorized in this preparation phase. No installation adopted the candidate. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Continuation outcome and evidence limits

The earlier legacy forced-inventory HTTP 503 hold is preserved in historical
receipts. Its response bytes and failure-time screenshot were not captured;
no replay reconstructed them. The authorized continuation uses QA-only
`scripts/run_perf_harness.py` from main commit
`7791936a0dafb7a74a0238ab079ef8dfc6d81e13`, blob
`6f39f7adf4caaab2237d20316b6457a72a037b9c`, with explicit 330-second readiness.
No product or image bytes changed. The complete legacy drill now passes;
its setup inventory request recovered one busy response within that budget.
The staged script is separately hash-bound; its relocated Git-context fields
are empty rather than mislabelled as a different executed source.

The read-only crawl then stopped on `axe-label-content-name-mismatch` for a
Topology path node, selector `.kind-path`, at 1600 px. The native impact is
serious: visible text must be part of the accessible name. Exact finding,
contemporaneous PNG and scoped HTTP 200 API response are retained privately.
This unknown finding is not covered by the narrowed historical issue rule.
No live retry, checker waiver or product fix occurred. Its cause requires
source and retained-evidence review; the screenshot alone does not determine
the accessible name.

Partial crawl: 18 complete chunks passed and the next failed, out of a planned
42. Retained reports contain 483 records and 834 captures, with one unknown
error, no blocked requests and no admitted writes. Three-state Topology
human-view captures at 1600 and 3840 px passed title-preservation and visible
link-name checks for completed targets. That is not full human-view acceptance.
Native hover tooltip appearance remains an owner visual check because
headless browsers cannot display it. The complete stock restored performance
pair and end-of-hold segment-tree proof were not reached.

The original test installation recovered on the same full container IDs,
names and image, healthy with zero restarts. Independent successful container,
network and volume censuses were empty; task listeners, helpers and runtime
were absent. The owner's native zero exit establishes recovery, not crawl
qualification. The bounded read-only observation recorded no new alert-check
mail, no alert change and no retained-session change; mail jobs increased by
one, with no causal attribution. Memory and middleware samples cover this
partial run only; no OOM was observed, and the conservative combined role peak
was three within the four-call budget.

Retained source and exact-cut CI remain valid: CI Preflight run 45015563049
passed all 24 expected jobs, and CodeQL run 45015563005 passed both language
jobs. Prior wrap-head CI also remains historical evidence. This follow-up
changes only release evidence and requires its own exact-head CI.

No narrow-beta deferrals apply. The PR remains draft with three pre-publish
rows Blocked. No readiness event, independent-review request, merge, tag,
publication, deployment or external wiki action occurred in this continuation.
