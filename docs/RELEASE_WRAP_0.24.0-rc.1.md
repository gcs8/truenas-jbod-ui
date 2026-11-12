# Release wrap: v0.24.0-rc.1

Date: `2026-11-11`

Release mode: full
Previous release tag: `v0.24.0-beta.8`

## Scope

Release preparation branch: `release/v0.24.0-rc.1`, PR #1164.
Base: `main` at `2fd82ffd0f8ffecbeedac35f257b5a52369fd2ed`.
The candidate includes #1160, #1159 and #1163 plus the beta.8 wrap #1162.
Every beta.8-deferred gate ran afresh. No narrow-beta deferrals apply.

Qualified source and runtime commit:
`8888083d8d737565138fc1f148489408caea3bcf`.

Qualified local Linux QA image:
`sha256:cf2a54e162158eb9523817f45a2603faa95e40b351ae52921e701b7be354e860`.
Its OCI revision and application version match the qualified source and
`0.24.0-rc.1`. This is a locally built QA image, not a published registry image.
The Dockerfile pins its base by digest. The later wrap-only commit does not
claim a rebuilt image or a new execution of the runtime gates.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.
Exact private restore, resource, appliance, snapshot and visual receipts remain
in the maintainer's restricted handoff, not in this public repository.

Changelog coverage: pass (3 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Qualified source `8888083d` contains the stated main base and release metadata. The dated CHANGELOG section, whole-line summary since stable, beta.8 delta and upgrade notes cover the required merged PRs. Actual publish-workflow tag resolution emits the two fixed rc.1 tags plus `dev`, never `latest` | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `8888083d`, native exit 0 and `FINAL: PASS`: 5,108 Python tests run, six skipped, no failures. Compileall, bounded Ruff, test-count manifest, whitespace and performance baseline passed. Local promtool was unavailable; the required exact-head CI job owns that check. An earlier run failed two permission fixtures under the runner's restrictive umask; its environment failure is retained separately. The complete unchanged-candidate rerun used the normal test umask | Pass |  |
| JavaScript syntax gates | yes | Same full source gate: all declared script/spec syntax checks passed. JavaScript unit summary: 1,576 total, 1,571 passed, five skipped, zero failed | Pass |  |
| Docker build and health gates | yes | Fresh exact-source image, matching OCI revision and `/livez` version. UI, history and Admin health and restart checks passed in the full Compose matrix and complete restore drills. Shipped resource limits retained. Original QA-host twin IDs, image and healthy state were independently verified after restoration | Pass |  |
| Optional-sidecar runtime matrix | yes | Full eight-variant current-image matrix passed. Five alias cycles, five mapping cycles, Admin initial setup, disabled scheduler, enabled scheduled backup, two cross-user journal reads and non-root ownership checks passed. Complete native log inspected; all task containers, networks and volumes removed | Pass |  |
| Full Playwright/browser gates | yes | Thirteen fixture-only spec groups passed 526 tests, Admin operations passed 15 and current-source demo passed nine. All 15 native exits and logs verified, 550 passed with no skips. Fresh current-format restore browser runs each had 16 passed and one explicitly inapplicable synthetic-demo mutation skip. Legacy live smoke had 16 passed and two applicability skips. Source visual supplements passed 46 heatmap and 101 selected UniFi/chip/label cases. Full candidate read-only crawl passed all 42 chunks with 1,680 captures, 420 at each prescribed width, no unexplained error identities and no lost capture identities versus the previous full phase-A run. Native human-view review is documented privately; known residuals below are not called fixed | Pass |  |
| Feature-specific live API/UI gates | yes | Once-only CORE VM account/grant/TLS checks and independent read-only membership comparator passed, with verified cleanup. Six storage-view cases, concurrent reload/Refresh/save/export estimate with proved background overlap, repeated cached reads, exact changed-disk API/page 409 messages, note clear readback, rendered PHY inspectors and informational census passed. A candidate-image census with read-only mounts left segment bytes and metadata unchanged through the entire hold. Controlled SMART/history 503 retry UI recovered through real candidate responses. Per-role telemetry stayed within the admitted combined middleware budget. Idle delayed alert/mail/session observations found no new alert-check mail or net retained-session growth | Pass |  |
| Local release perf harnesses | yes | Both maintained harnesses ran serially on the exact-image local QA stack after collector settlement, three iterations each, with the checklist's `release-candidate` and `release-candidate-history` labels and retained JSON/Markdown/CSV trails. The full app run includes three real empty-bundle mapping preview/import round trips and empty-bundle readback. Earlier read-only-only and offline measurements remain separate, not substituted for the full workflow run | Pass |  |
| Linux QA restore gate | yes | Fresh current-writer FULL default-format and explicit `7z` exports from the same accepted isolated source API completed separately. Default request omitted packaging and inspected as encrypted schema-2 `tar.zst`; explicit FULL inspected as encrypted schema-2 `7z`. Both export-source versions are `0.24.0-rc.1`, with every selected group present. Each complete candidate-image drill passed import, exact aggregate counts, restart survival, browser/perf and independent cleanup. The separate legacy `0.22.2` encrypted `7z` live-read-only drill passed monotonic checkpoint counts, persisted-row restart survival and both pencil cleanup cycles. Its optional runtime-overrides and SAS-alias omissions are historical source omissions, not candidate export omissions. Kept live runtime was subsequently removed and original twin restored | Pass |  |
| Restored Linux QA perf harnesses | yes | Both maintained harnesses ran serially against the populated candidate-image restored Linux QA stack after collector settlement, three iterations each. Exact labels `release-candidate-linux-qa-restore` and `release-candidate-history-linux-qa-restore`; retained JSON/Markdown/CSV trails. App includes three stock mapping preview/import round trips, no skipped workflow. The earlier history label ordering error is retained only as supplemental evidence, not silently relabelled | Pass |  |
| Snapshot/export/offline artifact gate | yes | Fresh source and live restored-stack estimate, Auto HTML and explicit ZIP exports passed. Offline browser rendering showed the expected bay tiles and offline badge, no external requests or console errors, and no private IPv4 addresses or host paths. Two lexical credential-pattern matches are exact sign-in-field code already in shipping JavaScript, not exported secret values. Partial redaction remains explicit; raw artifacts remain private | Pass |  |
| Docs/wiki/public-demo gate | yes | CHANGELOG, release notes, wrap and public text reviewed. Public-doc, screenshot-manifest, checked-in public-demo, note rendering and prerelease demo-policy checks passed. Current-source demo browser rendering is separate from the unchanged checked-in stable public demo. No demo rebuild or external wiki push is required by this prerelease; owner-only wiki publication remains pending | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | [Release publication run](https://github.com/gcs8/truenas-jbod-ui/actions/runs/43104893636) completed successfully on merge `5e65a992afaa73ade4c9f926d113d615b6a0f85f`. Annotated tag `v0.24.0-rc.1` targets that merge, whose tree equals reviewed head `81a30d4b`. GHCR `v0.24.0-rc.1`, `0.24.0-rc.1` and `dev` resolve to `ghcr.io/gcs8/truenas-jbod-ui@sha256:f428f1c5a5886d91c6c74dd500c258c64dd0ee4e1df40ea21251941cb189cfe7`, with matching OCI revision/version. GitHub prerelease is published, not draft; both stable/latest pointers remain v0.23.0 | Pass |  |
| Deployment refresh/sniff tests | yes | Published-image, image-only helper update and independent verify passed on both authorized validation installations. Exact digest/revision/version, healthy UI/history and applicable Admin, zero restarts/OOM, writer-fence locks, preserved Compose inputs/operator overlay, retained disk placement and intact durable history were independently checked. Both prior images and private verified backups remain available; rollback was not required. API sweeps matched their accepted baselines. The deployed-twin full read-only crawl completed 42 chunks at all four prescribed widths with no new error identities. Delayed idle observation is recorded in the phase-B section below. Production remains outside this authorization | Pass |  |
| Post-release reopen | yes | Blocked until separately admitted publication, deployment and final-validator gates complete. Stable promotion and production remain outside this lane | Blocked |  |

## Coverage and residual findings

The full restored-stack read-only crawl used the shipping route guard and
current repository inventory. All configured systems, enclosure and displayed
saved-view scopes, Fabric, history and Admin were covered at 1600, 1920 and
3840 px, plus 390 px best effort. All three HA saved views completed. The
`main.disk-sync` state was retained and reported disabled controls explicitly
not applicable. Write, LED, appliance-load and heavy-download states were not
enabled. Listed actions are not executed actuation coverage.

All 42 native chunk results and the native merge were safety-clean. The merge
recorded 992 state records, 773 done, 180 not applicable and 39 listed, with
1,680 captures and no admitted writes or blocked requests. Candidate and prior
full phase-A target/state/width capture identity sets match exactly.

Known errors remain: 234 accessible-name findings under #1110, three narrow
control overlaps under #1114, 56 contrast and 32 small-range spread findings
under #1103. Warnings were 50 healthy-temperature alarm and 12 small-range
spread findings under #1103. Independent comparison found no new error
identity versus the retained full phase-A baseline. Frequency changes do not
mean those defects were fixed. Known residuals remain reportable limitations;
the governing phase-A procedure accepts only the absence of new errors, not a
universally defect-free UI.

Native source-fixture review covered all narrow UniFi face variants and the
shared chip-placement families. Critical long-hour bays were inspected at all
four widths, including dense 2.5-inch top-latched and 3.5-inch right-latched
faces. Complete bay labels retained vertical line height, with intentional
single-line ellipsis where constrained. Scrolled neighboring bays were not
counted as complete-bay acceptance. Live review covered the populated system
families, saved views, Fabric, history/Admin and grouped finding examples.
Large full-page images were retained for context but reviewed with bounded
native tiles/crops, not downscaled as evidence for tiny text.

The ESXi board view at 390 exposes only a scrolling viewport slice. That image
does not prove complete-bay readability; the supported-width capture and local
board fixture checks supply the separate acceptance evidence. Intentional
horizontal label ellipses on narrow non-UniFi cards are not the vertical
UniFi clipping fixed in this release. Minor inherited Admin badge wrapping
and known narrow card/scroll limitations remain visible. Final cross-family
parent admission is separate from this worker's executed pixel review.

## Resources, failures and custody

Anonymous memory and Docker-style working set stayed below the shipped limit,
with no OOM kills. Total charged memory including reclaimable file cache
reached the limit during startup work; it is not reported as anonymous memory
or as uniformly well below the limit. SQLite temporary work used the history
volume's `.sqlite-tmp`, not the system temp directory. Complete measurements,
optional-source errors, backoff categories and two restart-window telemetry
read failures are retained privately. No blanket clean-log claim is made.
The history collector uses the UI inventory HTTP path, so the sampled UI role
includes its appliance work; an empty history-service middleware metric list
is not a separately measured history-role peak.

Appliance observations occurred before work and at two idle checkpoints after
cleanup, more than five minutes apart. Alert class/level counts, job count and
net retained-session categories were unchanged, with no new alert-check mail.
One API-key session row appeared at each snapshot time, but raw `current` was
false; this record does not claim the row was marked the observer's current
session. Net retained counts do not prove a request rate or absence of every
transient session.

Harness-only failures are preserved separately: restrictive test umask,
network-share browser-output mode rejection, missing metadata-reader argv and
missing transport-preload environment. The latter three refused before their
claimed gate began. Corrected unchanged-source complete runs passed. No
product code was changed to make a gate pass.

## Phase-A attribution

The final wrap update changes documentation only. Its changed-input proof and
focused release/document tests are recorded separately before readiness; all
runtime, browser and image results remain attributed to `8888083d` above.
Exact-head CI and Codex results belong to the final PR head, not this earlier
runtime commit. Read those external results before parent admission.

The phase-A results above remain attributed to their executed source, not
to a later documentation commit. Phase B independently admitted the final
release PR and performed the authorized prerelease publication, helper
adoptions and deployed-twin crawl. Production, stable promotion and external
wiki publication remain outside this authorization.


## Phase-B publication and live acceptance

[PR #1164](https://github.com/gcs8/truenas-jbod-ui/pull/1164) merged as
`5e65a992afaa73ade4c9f926d113d615b6a0f85f`. Its tree equals the admitted
`81a30d4b8f000ec9ba10ea8fa4d7f77d499b4389` tree,
`afb92c16caf2d10c20ca0ca667c6c4fa70d481bd`. Exact-merge main CI and CodeQL
completed successfully before the annotated rc.1 tag and
[GitHub prerelease](https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.24.0-rc.1).
The fixed rc.1 image tags and `dev` share the immutable digest recorded above.
`latest`, GitHub latest and the checked-in public demo remain on their
accepted stable release. Publication and the first validation deployment were
not replayed when an external observer needed correction.

The deployed-twin crawl used the shipping inventory, generated per-state
reads, strict target bindings and request guard from the tagged source.
All 42 native chunk exits and the merge passed. There were 40 targets,
989 records, 818 done, 130 not applicable, two confirmation previews and
39 listed states. Exactly 442 captured target/state pairs each have all
four widths, 390/1600/1920/3840, for 1,768 captures with no duplicate or
missing capture identities. Every unit finished below seven minutes. Admin
started only for the final chunk and its prior absent state was restored
and independently verified. No retry, live app write, LED actuation,
appliance-load permission or heavy-download permission was used.

The 302 automated findings were known: 104 accessible-name errors under
#1110; 56 contrast errors and 56 small-range spread errors under #1103;
74 healthy-temperature alarm warnings and 12 spread warnings under #1103.
There were zero unexplained errors, blocked requests or admitted writes,
and no new product issue was filed. #1114 did not emit an automated overlap
finding in this run. Selected native storage-view crops showed intentional
horizontal scrolling, which is not proof that every affected layout and
scroll endpoint is fixed; that issue remains open.

Listed Admin/account/configuration/runtime/restore actions, history refresh,
mapping restore and the release notice were not executed. Not-applicable
states reflect absent capabilities, empty/fault/unknown selections, hidden
panels or disabled controls rather than successful interactions. Disk sync
had two confirmation previews and 14 disabled/not-applicable records. The
three HA saved views completed. The stock metric-option expansion remains
first-scope-only for each configured system; this full application crawl does
not claim every metric on every secondary view. The earlier broader metric
matrix is separate supplemental evidence, not substituted for these chunks.

Native-scale review covered the populated platform families, right/top/bottom
latched bays, selected SMART details, Fabric, history and Admin. On the
ESXi view, the 390-pixel screenshot is only a scrolling slice and does not
establish whole-bay readability. Exact-source synthetic supplements cover
all UniFi variants, ENVR Core normal/long labels, dense rotated long-hour
values and remaining face shapes. Those are layout fixtures, not physical
hardware qualification. No new number, heat-value, state-chip, LED, latch
collision or vertical label clipping was found in complete reviewed bays.

For #1158, an actual young stale-hit rendered green `STORAGE OK` with an
informational background-refresh message. The corrected observer reused the
repository guard and generated reads, including automatic selected-disk
SMART; raw results were saved before assertions. Its earlier hand-written
guard failure remains recorded as an observer failure, not a product defect.
For #1161, deployed application JavaScript matched the reviewed source and
populated SMART summary/details rendered during the crawl. Deliberately hung
request recovery is proved by the exact-source browser regression, not by
hanging a real appliance. ENVR Core label-height acceptance combines the
matching deployed stylesheet with the exact-source fixture review.

Private helper receipts retain installation identity, retention and recovery
metadata. The second installation preserved its operator overlay and changed
only the image pin, after shared-host coordination cleared. Its API and
1920/3840 browser checks passed with zero HTTP, console, failed-request or
blocked-request diagnostics. Existing degraded-pool and saved-order warnings
remain; neither the application VM upgrade nor this report changes hardware.

The crawl logs have no Traceback. Background collection retained known
source/SSH failures, two retryable SMART HTTP 503 responses and three SMART
timeout skips matching retained predecessor categories. These are not browser
crawl errors, but are not described as clean logs or repaired collection.
Anonymous history memory and middleware concurrency remained separately
measured; charged memory includes reclaimable cache and is not anonymous
memory. Final delayed idle observation and resource reconciliation are retained
privately. The post-crawl snapshot ran while middleware was idle and more
than 30 minutes after the final chunk. Retained QA-host, internal and total
session counts, alert distributions, failed-alert mail and mail-job totals
were unchanged; zero new failed-alert messages and no confirmation snapshot
were required. The renewed window had zero metric-read failures and zero
OOM kills.

Release comments were read back on #1157, #1158 and #1161, and each was
closed completed with the evidence boundaries above. #1103 and #1110 were
re-observed and remain open. #1114 received a non-reproduction/coverage-limit
comment and remains open. No issue waiting on production was closed.

The post-publish change is this wrap only. Canonical `dev_check.py --safe`
passed before the evidence edit, with a full before/after source manifest
excluding only this wrap: 5,108 Python tests run, six skipped, no failures;
JavaScript 1,576 total, 1,571 passed and five skipped. Local promtool remained
unavailable, explicitly skipped; exact-head CI supplies that check. Focused
release/document/privacy tests qualify the final wrap bytes. Exact-head CI,
clean Codex and gated-queue admission remain separate remote requirements.
