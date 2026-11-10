# Release wrap: v0.24.0-beta.7

Date: `2026-11-10`

## Scope

v0.24.0-beta.7 is the seventh prerelease of v0.24.0. It includes the changes
merged after `v0.24.0-beta.6`, including the refresh/read, crawler and layout
corrections in #1147, #1148 and #1149.

Release preparation branch: `release/v0.24.0-beta.7`. The intended release
flow merges this branch into `main` with a merge commit, never squashed, so
the qualified source remains reachable from the eventual tag.

Base: `main` at `65076010bbfec53901f98f22c6e1765f8569a4df`.

Validated source and runtime commit:
`1f8b66d021d5fab3691fed4d8c78fce40c0359f5`.

Validated Linux QA image:
`sha256:a3fc5ff601d9b69abd95527b81d91efef3502f93cd6073648f9ab8e6ad096550`.
Linux/amd64 platform manifest:
`sha256:7d0a174338907df60cfef4e2127bbed62cac13e40a4f17b1f7f1b6a73515fafa`.

This wrap is a documentation-only follow-up. Runtime results belong to the
commit and image above, not to an unbuilt image of the later wrap commit.
The image-input diff is checked separately before PR readiness.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section. Raw private restore, appliance, resource and visual evidence is
retained in the maintainer's restricted handoff, not in this repository.

Changelog coverage: pass (15 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Qualified source `1f8b66d0` contains `main` at `65076010` and the prerelease cut. The dated beta.7 CHANGELOG section, release notes and upgrade instructions cover every required merged PR since beta.6; the coverage checker passed | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `1f8b66d0`, exit 0, `FINAL: PASS`: 5,077 Python tests run, six skipped, no failures. Compileall, bounded Ruff, test-count manifest, diff hygiene and performance baseline passed. Local `promtool` is unavailable; CI owns that check | Pass |  |
| JavaScript syntax gates | yes | Same full source gate: all declared JavaScript and browser-spec syntax checks passed; 1,545 unit tests, 1,540 passed, five skipped, zero failed | Pass |  |
| Docker build and health gates | yes | Built the pinned QA image from `1f8b66d0`, verified version `0.24.0-beta.7` and matching OCI revision. UI/history/admin health and restart checks passed in the matrix and complete legacy restore; default memory limits were retained | Pass |  |
| Optional-sidecar runtime matrix | yes | Candidate-image Compose matrix passed all eight variants, including non-root, alias/mapping, admin setup, scheduler and cross-user journal cases; task resources removed | Pass |  |
| Full Playwright/browser gates | yes | At `1f8b66d0`, 13 fixture-only specs passed 382 tests, admin operations passed 15, current-source demo passed nine. Complete legacy restore ran the full mandatory browser set: offline 16 passed/one skipped, live 16 passed/two skipped. Skips retain their fixture/timing reasons, not passes. Full read-only candidate crawl and cross-family human-view inspection passed with only known residual findings; no excluded disk-sync state or HA saved-view gap | Pass |  |
| Feature-specific live API/UI gates | yes | Candidate CORE VM bootstrap/grant/TLS checks and independent read-only membership comparator passed once each, with cleanup. Storage-view cases, concurrent reload/Refresh/save/export estimate, changed-disk 409 refusal, rendered expander counter inspectors, and informational read-only census passed. Controlled retryable-refusal browser checks recovered through the shipping SMART/history UI. Segment metadata and bytes stayed unchanged. Middleware role maxima stayed within the combined budget; bounded alert/mail/session comparisons found no new alert-check mail or accumulated retained-session growth | Pass |  |
| Local release perf harnesses | yes | As in the preceding qualification procedure, both maintained harnesses ran serially against the exact-image local restored stack after a full history pass; controller `app_performance` and `history_performance` passed, one iteration each. Private JSON results are retained; the established controller uses `--no-record`, so it does not create the checklist's separate three-iteration local CSV trail. This is the same scoped procedure used by beta.6, not a claim that the example CSV commands ran. Source performance baseline passed | Pass |  |
| Linux QA restore gate | yes | Current-image legacy encrypted schema-2 `0.22.2` `7z` restore passed inspection, import, reconciliation, restart survival, pencil cleanup, all browser/perf phases and independent final teardown. The historical source legitimately lacks optional runtime-overrides and SAS-alias files. Separate current-writer default `tar.zst` and explicit FULL `7z` round trips passed at `9e53b272`/image `fb418714`; those format receipts are reused under the r3 plan after proving backup/history/controller/Compose image-build inputs for that pipeline unchanged. Both exports were made by `0.24.0-beta.7`, detected their requested format, had no absent groups, and passed counts, restart and cleanup. Refresh/UI-dependent legacy browser/perf and snapshot paths were rerun on `1f8b66d0` | Pass |  |
| Restored Linux QA perf harnesses | yes | Both harnesses reran on the current legacy-restored image after a complete history pass, one iteration each, with successful native results and retained private JSON evidence. The unaffected egress-blocked format-drill history-perf results remain attributed to `9e53b272` | Pass |  |
| Snapshot/export/offline artifact gate | yes | Current-image redacted Auto HTML and Force ZIP exports passed size/packaging checks and offline Chromium rendering with no external requests, console errors, private IPv4 addresses or host paths. The known credential-pattern matches are sign-in-field code, not exported credentials | Pass |  |
| Docs/wiki/public-demo gate | yes | Reviewed CHANGELOG, release notes, wrap and changed wiki sources. `check_public_docs.py`, `check_public_screenshots.py`, `check_public_demo_artifact.py`, release-note rendering and prerelease `--public-demo-only` validation passed. The checked-in public demo remains unchanged; current-source browser rendering passed separately. Public demo rebuild deferred for this prerelease. External wiki publication remains owner-only | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Not run in phase A. Future prerelease publication must verify fixed beta.7 tags and `dev`; `latest` must remain on the stable release | Blocked |  |
| Deployment refresh/sniff tests | yes | Not run in phase A. The QA outage ended with the original twin containers healthy on the same IDs and image; this was restoration, not a beta.7 deployment. New-image twin and test-VM adoption need separate phase-B authorization | Blocked |  |
| Post-release reopen | yes | Blocked on publication and authorized deployment qualification | Blocked |  |

## Coverage and remaining findings

The read-only crawl included all configured systems, enclosure and displayed
saved-view scopes, Fabric, history and admin at 1600, 1920 and 3840 px, plus
390 px best effort. Every selected chunk ended safety-clean, with no admitted
writes, blocked requests, capture failures or unexplained error findings.
Disabled disk-sync controls were explicitly not applicable, not removed from
the inventory. HA saved views executed without the predecessor binding gap.
Live write, LED and appliance-load states were not enabled; fixture coverage
is separate and does not claim live actuation acceptance.

The historical seed needed its existing QA-only UniFi host-key pin-copy step.
After that precondition, only the affected UniFi chunks were repeated, then
substituted into the qualified native merge. Initial unknown-data captures and
all supplemental diagnostics remain retained, not silently discarded.

Known findings remain under #1110, #1103 and #1114. The grouped warnings are
healthy-temperature alarm and small-range spread; error-level accessible-name,
contrast and narrow-view overlap findings retain their known-issue attribution.
The unchanged 390 px state-chip/slot-number contact documented in #1149 was
confirmed and retained as a known best-effort-width limitation. This is not a
claim of universal chip clearance or a clean mobile UI. No new product code
was added during release QA.

The resource check distinguishes sampled anonymous memory and Docker-style
working set from total charged memory. Anonymous/working-set samples stayed
below the shipped limit with zero OOM kills; reclaimable file-cache charge
touched the limit. SQLite temporary work used the history-volume temp
directory, not the system temp directory. Exact measurements and nonzero
optional-source/backoff log categories are in restricted receipts; no blanket
clean-log claim is made.

## Hold boundary

Phase A stops before merge, tag, GitHub release, GHCR tag pushes, deployment
or external wiki publication. A passing pre-tag validator qualifies the
candidate record; it does not grant those actions. The later approved release
flow must publish the fixed beta.7 tags and `dev` without moving `latest`, and
must qualify each authorized deployment separately. Production remains out
of scope.
