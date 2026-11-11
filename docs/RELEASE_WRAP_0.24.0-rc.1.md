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
| GHCR publish verification | yes | No tag or registry publication in phase A. Fixed rc.1 tags and `dev` will require separately admitted publication and immutable registry readback; `latest` remains excluded | Blocked |  |
| Deployment refresh/sniff tests | yes | No rc.1 deployment in phase A. The existing beta twin was temporarily parked for disposable QA, then restored with its original IDs/image and independently read back healthy. This is recovery, not rc.1 adoption. Published-image helper deployment and readback belong to phase B | Blocked |  |
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

Archive observations occurred before work and at two idle checkpoints after
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

## Phase-A boundary

The final wrap update changes documentation only. Its changed-input proof and
focused release/document tests are recorded separately before readiness; all
runtime, browser and image results remain attributed to `8888083d` above.
Exact-head CI and Codex results belong to the final PR head, not this earlier
runtime commit. Read those external results before parent admission.

Do not merge, tag, publish, deploy or push the wiki from this phase. No
production installation was contacted. The existing stable channel and public
demo remain unchanged. Phase B must independently admit the final release PR,
then perform its authorized publication, helper deployment and twin crawl.
