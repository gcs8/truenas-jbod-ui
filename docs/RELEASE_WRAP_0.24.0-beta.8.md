# Release wrap: v0.24.0-beta.8

Date: `2026-11-11`

Release mode: narrow beta
Previous release tag: `v0.24.0-beta.7`

## Scope

Release preparation branch: `release/v0.24.0-beta.8`.
Base: `main` at `22f4cfcf5ff2822b561525f776ae074924e9f779`.
Previous fully qualified beta: `v0.24.0-beta.7` at
`690a2d55236daea8a500965a455ab6d045520578`.

The source delta is front-end assets, tests, reviewed QA tooling and docs,
plus the validator's byte-checked release-version metadata exception.
#1153 is the only user-visible change: state-chip clearance from slot
numbers, heat values, LEDs and latches. #1152 records the preceding release;
#1155 adds narrow-beta release tooling, not runtime behavior.

Validated against `docs/RELEASE_CHECKLIST.md`, subject to owner-approved
D-TNJ-20261111-NARROWBETA. Long-gate deferrals are not passes or waivers.

Runtime-qualified source: `5dc8609d370f0fa78982b7062d6f596de97a06c1`.
Candidate image: `sha256:333f3fc29df6589dc3aef1747a693241452c8f163ebac8669b67d5ce7acb72d7`.
OCI source revision: `5dc8609d370f0fa78982b7062d6f596de97a06c1`.
Platform manifest: `sha256:2dd8f3a2de9ae4e007a491a64974b7dd15244fa65fb6cdc11ceb71867d74285b`.
Image config: `sha256:86274702693c3e8e97c748dc1327222a760a9ff483602fedb2540997a84d0710`.
This is a local QA image, not a published GHCR artifact.

Changelog coverage: pass (2 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Merged main `22f4cfcf` without rewriting release-branch history. Whole delta from beta.7 is narrow-eligible, including the explicitly reviewed validator tool and version-only metadata. Changelog coverage passed for 2 PRs; #1153 is the sole user-visible change, #1155 Internal. No operator action beyond the normal image update. | Pass |  |
| Python unit and syntax gates | yes | FAILED: self-referential validator test, fix pending on main. `dev_check.py --full` at `5dc8609d` completed 5107 tests, 6 skipped, with two failing subtests of `test_existing_release_wraps_are_unchanged_by_narrow_beta_rules`, both for the first narrow beta.8 wrap. Compileall and Ruff passed. Native `FINAL: FAIL`, exit 1. Do not promote until main's test correction is merged and the full gate reruns. | Blocked |  |
| JavaScript syntax gates | yes | The completed full source gate passed every JS syntax check. JS unit summary: 1550 tests, 1545 passed, 5 skipped, 0 failed. | Pass |  |
| Docker build and health gates | yes | Built the pinned source above. OCI revision and app version matched. A synthetic, network-none container returned `/livez` status ok/version 0.24.0-beta.8 and `/healthz` status ok, with 0 restarts. Smoke container removal verified; no existing deployment was changed. Native build/smoke exit 0. | Pass |  |
| Optional-sidecar runtime matrix | yes | Deferred to the next rc/final full run. Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Deferred |  |
| Full Playwright/browser gates | yes | Remote fixture checks at `5dc8609d`: heatmap-temperature-bands 46 passed; selected UniFi chip/narrow-layout cases 29 passed. ui-coverage first run: 10 passed, 1 transport-closed failure after the connection aged about 15 minutes; only that storage-view-first test reran on a fresh connection and passed. Original failure retained, not relabelled as a green whole-file run. Complete-file exact-head CI remains pending. Supplemental UniFi captures: 28 passed. Human-view review covered every face family, heat off/temperature/long Power-On Hours, at 390/1600/1920/3840 using unscaled native crops. Full UI crawl: Deferred to the next rc/final full run. | Pass |  |
| Feature-specific live API/UI gates | yes | Required narrow parts passed on synthetic HTTP fixtures and shipping candidate templates/assets, with no appliance contact: chip placement, protected text/LED/latch clearance, per-bay and G2 fallbacks, all face families and all four widths. Native visual review found no chip obstruction in the complete bays inspected. CORE VM checks and Archive comparator: Deferred to the next rc/final full run. | Pass |  |
| Local release perf harnesses | yes | Deferred to the next rc/final full run. Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Deferred |  |
| Linux QA restore gate | yes | Legacy live restore and default tar.zst/explicit 7z round trips: Deferred to the next rc/final full run. Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Deferred |  |
| Restored Linux QA perf harnesses | yes | Deferred to the next rc/final full run. Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Deferred |  |
| Snapshot/export/offline artifact gate | yes | Candidate ui-coverage built its offline HTML through the real SnapshotExportService/public-demo fixture builder, not the older checked-in demo. Offline default/about/heatmap/healthy and empty bay selection/history/connections completed at 1600/3840, with the absent fault bay explicitly not applicable. This is synthetic exporter/browser evidence, not a live export or native file-scheme check. Live archive-format round trips are separately Deferred. | Pass |  |
| Docs/wiki/public-demo gate | yes | Public docs PASS: 26 documents, 197 wiki links, 0 prose patterns. Checked-in screenshot manifest and exact-byte review PASS. Release notes rendered; metadata, source diff and whitespace reviewed. wiki/ unchanged from beta.7. Checked-in demo artifact publishable; public demo deferred: prerelease. No public screenshots or wiki publication was performed. | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Not run in phase A; no publish authorized | Blocked |  |
| Deployment refresh/sniff tests | yes | Not run in phase A; no deployment authorized | Blocked |  |
| Post-release reopen | yes | Blocked on publication and authorized deployment qualification | Blocked |  |

## Visual and transport boundaries

The 390 px layout is best effort, not newly supported. Narrow hardware rows
retain their internal horizontal scroll, so partial neighboring bays in a
capture are not complete-bay acceptance evidence. Native crop indexes record
literal source rectangles and PNG hashes. Initial drawer and ENVR crops cut
off a bay edge; corrected crops of the same source PNGs were inspected instead.
An ENVR Core 390 px off-mode pool label is vertically clipped, without a chip
collision. That best-effort rendering limitation remains visible for maintainer
review; no general claim of a defect-free whole UI is made.

The fixture crawl reported 0 unknown errors and 0 blocked requests or admitted
writes. Its retained findings include known #1110 and #1103 cases, an intentional
503 storage-view refusal with its expected console diagnostic, and a synthetic
history fixture gap. Successful fresh sessions read back zero remaining owned
contexts and disconnected their transport. The failed session lost transport
before cleanup readback; independent Browserless process/slot release is not
proved by those receipts.

## Deferred scope

- Compose matrix: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Legacy live restore: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Default `tar.zst` and explicit `7z` round trips: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Performance harnesses: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- CORE VM checks: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Archive comparator: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Full 42-chunk crawl: Deferred to rc/final per D-TNJ-20261111-NARROWBETA

## Hold boundary

The parent is correcting the validator compatibility test through a separate
PR to main. Do not edit that test on this release branch. After the correction
merges, merge main without rewriting history, rerun `dev_check.py --full`,
reconcile evidence and pass the pre-tag validator before pushing or readying
the PR. Exact-head CI and Codex remain outstanding. This wrap does not assert
release readiness.

No merge, tag, publish, deployment or external wiki publication in phase A.
Production remains outside scope. No earlier release's passing rows are
carried forward as fresh beta.8 execution evidence.
