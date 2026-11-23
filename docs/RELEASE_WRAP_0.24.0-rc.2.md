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
| GHCR publish verification | yes | [Release workflow](https://github.com/gcs8/truenas-jbod-ui/actions/runs/45070295617) completed on merge `5d485296313ef94f70cf6b915323ff38f1d5016d`. Annotated `v0.24.0-rc.2` targets that merge; its tree equals qualified head `c41b77f105eaba91f2e608d38c26108ff60bba80`. GitHub prerelease is published, not draft. Fixed version aliases, `dev`, `sha-5d48529` and the full source-SHA alias resolve to `ghcr.io/gcs8/truenas-jbod-ui@sha256:283fa755b23971efe23c7d2d198184a9abfc9fa80ea90aa8b68dba345a7bce5f`, with the exact merge revision and version labels. SHA aliases copy identical release-built manifest bytes, not another build. Stable/latest pointers remain v0.23.0. | Pass | |
| Deployment refresh/sniff tests | yes | First authorized validation installation adopted the published digest through the tagged image-only helper; update and independent verify returned zero. Healthy fenced services, unchanged Compose inputs, retained placement and intact durable history were read back. API request/status parity and the delayed idle observation passed. Full deployed-state crawl, delayed idle observation and independent final readback pass. Secondary-installation acceptance is blocked by host unreachability before staging; no backup/helper action or rollback occurred there. No complete deployment-gate claim. | Blocked | |
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

## Phase B publication and exact-merge CI

The published source is merge `5d485296313ef94f70cf6b915323ff38f1d5016d`.
Its tree, `73a6961b1ba38c05e75dac6b5b38227da8b1be08`, equals the qualified
release head's tree. The release workflow built the published image above;
it is distinct from the local phase-A QA image.

[Main CI run 45068384634](https://github.com/gcs8/truenas-jbod-ui/actions/runs/45068384634)
failed on attempt one. Python 3.14 shard 1 ran 1,846 tests with two skips and
one error in `test_trickled_headers_and_chunk_metadata_obey_whole_response_deadline`,
headers/status-200 subtest. The post-close `recv(1)` assertion received
`ConnectionResetError` rather than EOF. The accepted test-only diagnosis is
RST rather than FIN when a client closes with unread bytes. The failed native
result remains retained; it is not relabelled a pass. A separate test correction
is outside this immutable release cut.

The two Python summaries and public-demo aggregate also failed downstream.
The Python 3.12 summary had verified all 7,048 test identities exactly once;
its final matrix-result predicate failed because of the 3.14 shard, not a
separately observed 3.12 test failure.

The four failed jobs were rerun under the authorized continuation. Attempt
two completed successfully with 23 successful jobs and one expected Changelog
skip. Nineteen successful executions were retained from attempt one; only the
four failed jobs executed again. GitHub assigns new job IDs in the attempt-two
view even to retained jobs, so execution provenance was reconciled by original
start/completion timestamps and conclusions, not job IDs alone. No application
source, release tree, test assertion or deadline changed in this lane.
[Main CodeQL](https://github.com/gcs8/truenas-jbod-ui/actions/runs/45068384579)
completed successfully for both languages on the same merge, attempt one.

| Main CI job | Attempt-two result | Execution evidence |
| --- | --- | --- |
| Python unittest shard (3.14, 1) | success | rerun on attempt 2 |
| Changelog entry | skipped | expected skip; not executed |
| Fixture browser specs (leg 3) | success | successful execution retained from attempt 1 |
| Python unittest shard (3.14, 4) | success | successful execution retained from attempt 1 |
| Hardened, interrupted and segmented upgrade smoke | success | successful execution retained from attempt 1 |
| Python unittest shard (3.14, 3) | success | successful execution retained from attempt 1 |
| Python unittest shard (3.14, 2) | success | successful execution retained from attempt 1 |
| Fixture browser specs (leg 2) | success | successful execution retained from attempt 1 |
| Fixture browser specs (leg 1) | success | successful execution retained from attempt 1 |
| Production container smoke | success | successful execution retained from attempt 1 |
| Diff hygiene | success | successful execution retained from attempt 1 |
| Python unittest shard (3.12, 2) | success | successful execution retained from attempt 1 |
| Fixture browser specs (leg 4) | success | successful execution retained from attempt 1 |
| Admin clean-room browser QA | success | successful execution retained from attempt 1 |
| Python unittest shard (3.12, 4) | success | successful execution retained from attempt 1 |
| JavaScript syntax and npm lock | success | successful execution retained from attempt 1 |
| Run routing | success | successful execution retained from attempt 1 |
| Image-only upgrade smoke | success | successful execution retained from attempt 1 |
| Bounded Ruff | success | successful execution retained from attempt 1 |
| Python unittest shard (3.12, 3) | success | successful execution retained from attempt 1 |
| Python unittest shard (3.12, 1) | success | successful execution retained from attempt 1 |
| Python compile and unittest (3.12) | success | rerun on attempt 2 |
| Python compile and unittest (3.14) | success | rerun on attempt 2 |
| Checked-in public demo artifact | success | rerun on attempt 2 |

## Phase B deployed-state crawl and issue disposition

The first authorized validation installation adopted the release-built digest
through the tagged helper, with update and independent verify both returning
zero. Backup checksums and predecessor recovery receipt were retained. Placement
counts and identities matched the admitted predecessor, with no duplicate or
unplaced regression, and the helper reported intact durable history. The two fenced
services were healthy, with unchanged Compose inputs and no restart or OOM.

The complete examination request/status multiset matches the predecessor,
including its existing validation refusals, with no transport exception or
HTTP 5xx.
This is parity, not a claim that every endpoint returned success. Initial
retained logs were not empty or wholly clean. Final combined output contains
no traceback headers or access-log 5xx, but does include platform remote-error
records and a history-pass failure. Its structured event identifies `smart_read` and
`source_error_reply` with `HistorySourceError`; the native final helper verifies
healthy, unpaused history with no recovery required. The retained rc.1 accepted crawl already contains `TrueNASAPIError`
exception records; its initial post-adoption observation also contains a
`HistorySourceError` pass failure. These are pre-existing failure families,
not first seen in rc.2. Old logging lacks the current method/category and
stage/kind attribution, so exact cause equivalence or an equal-duration error
rate is not claimed. The later rc.1 crawl's absence of a history-pass failure
does not erase that initial baseline failure. An initial
stdout-only log projection was rejected because container stderr held the actual
log lines; retained combined output supplied the corrected classification.

The full deployed-state read-only crawl completed 42 chunks in 42 attempts,
with native exit zero, the Admin chunk last, and independent Admin absence
readback after cleanup. It covered 40 targets and 1,021 records: 850 done,
130 not-applicable, two previewed and 39 listed. Listed states were not
exercised. Native captures total 1,832 across 390/1600/1920/3840. No unknown
errors, blocked requests or admitted writes were reported. All 42 native logs,
exit receipts, safety predicates and exact capture identities were reconciled.
There are 458 captured target/state pairs, exactly one capture at each width.

The delayed post-crawl observation passed at least 30 minutes after the last chunk,
while the middleware was idle in two consecutive samples. Retained sessions
and alerts were unchanged, and no new failed-alert mail was recorded. The net
retained mail-job count changed during the crawl window; this is not a count
of new sends and its cause is not attributed. A separate post-adoption
delayed observation also passed. No session-only rollback was performed.

Retained middleware observations include failed Admin reads kept as missing
observations. Directly observed UI peaks remain within their reported budgets.
History and Admin emitted no direct-call limiter series, not a measured zero
load. History memory sampling recorded no OOM kills. Charged cgroup peak reached
the configured limit and max events occurred; it is explicitly not described
as wholly below the limit. Detailed deployment measurements remain in restricted
evidence. The rc.1 final readback already reached the same charged-memory
limit with no OOM; at-limit pressure is not first seen in rc.2. The sampled
anonymous-memory peak is higher in rc.2, but the observation lengths, active
coverage and live data differ. This is an explicit measured increase, not a
controlled regression or leak diagnosis. rc.1 max-event accounting is not
available in the cited scalar receipt. Memory sampling covered history only. Final UI/history readback retains
the original post-adoption container IDs, exact image, zero restarts, two fence
locks and unchanged configuration/helper/receipt hashes. Temporary Admin and
operation-owned key-copy directories are absent; owned samplers exited zero.

All 310 findings retain existing issue classifications: 160 main-page
label-in-name occurrences under #1110; 72 heat-spread errors, 32 contrast
errors and 46 healthy-temperature alarm warnings under #1103. Compared with
phase A, the error-occurrence multiset includes 20 additional known heat-spread
and six additional known main-page label occurrences. This is not described as
zero added error identities. State eligibility and live data differ between
the deployed installation and disposable qualification stack. Topology itself
has no findings under the narrowed rule.

Actual deployed Topology human-view covered unpinned, pinned-node,
pinned-bundle and reported-lane states at 1600 and 3840. Native SVG title
metadata remains present; native hover appearance is owner-only and is not
claimed. Direct accepted-baseline comparisons preserve the existing sticky
trace-band overlay, scroll boundaries and source wrapping. The missing proxy
sign-in block shifts the deployed content vertically and changes which graph
region intersects that existing band. No new independent geometry defect was
identified. Main face families and readable History/Admin captures were also
reviewed; screenshot slices do not establish hidden content or write behavior.

The release's 129 covered PRs yielded 137 distinct actual-issue references;
three required follow-ups were added. All 140 received exact-readback release
notices. No blanket closure was performed: issue-specific negative boundaries,
restore workflows and owner checks are not established by a generic read-only
crawl. Follow-ups #1461, #1463 and #1465 remain open. Three further observations
were posted and read back on #1103, #1110 and #1114; these also remain open.

## Phase B acceptance remains partial

The second authorized validation target is **Blocked: host unreachable;
owner to check**. Its recorded external address is explicitly authorized;
absence from the homelab inventory is not an authorization blocker.

The inherited coordination procedure read the shared-service checkpoint and
completed its local process scan. Bounded SSH connection attempts for the
remote scan and service-age read timed out, native status 255, before remote
authentication or staging. Their retained receipt-completion timestamps are
`2026-11-23T00:52:35Z` and `2026-11-23T00:52:50Z`; these are local log-file
completion anchors, not packet-capture timestamps. Independent reachability
checks confirmed the target unavailable.

No directory or script was staged there; backup, helper update/verify, browser
examination and Admin authentication checks were not started. There is nothing
to roll back. No further retry, network, route or VPN change was made. This
branch requires the owner to restore reachability before a later authorized
continuation. The first installation's verified result is preserved and was
not replayed.

This is a FULL-mode wrap with explicit Blocked gates, not a completed release
acceptance or a narrow-mode deferral. No final promotion, stable-tag movement,
production activation or external wiki publication is claimed.
