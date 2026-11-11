# Release wrap: v0.24.0-beta.8

Date: `2026-11-11`

Release mode: narrow beta
Previous release tag: `v0.24.0-beta.7`

## Scope

Release preparation branch: `release/v0.24.0-beta.8`.
Base: `main` at `560194dcc266ec16259baac67ba67810c88db864`.
Previous fully qualified beta: `v0.24.0-beta.7` at
`690a2d55236daea8a500965a455ab6d045520578`.

The source delta is front-end assets, tests, reviewed QA tooling and docs,
plus the validator's byte-checked release-version metadata exception.
#1153 is the only user-visible change: state-chip clearance from slot
numbers, heat values, LEDs and latches. #1152 records the preceding release;
#1155 adds narrow-beta release tooling, not runtime behavior. #1156 corrects
the compatibility test to compare only pre-mode wraps with the legacy rules.

Validated against `docs/RELEASE_CHECKLIST.md`, subject to owner-approved
D-TNJ-20261111-NARROWBETA. Long-gate deferrals are not passes or waivers.

Full source-gate commit: `f6b303ea352ded06a5970cef077c68d6abf7b97c`.
Runtime-qualified source: `5dc8609d370f0fa78982b7062d6f596de97a06c1`.
Candidate image: `sha256:333f3fc29df6589dc3aef1747a693241452c8f163ebac8669b67d5ce7acb72d7`.
OCI source revision: `5dc8609d370f0fa78982b7062d6f596de97a06c1`.
Platform manifest: `sha256:2dd8f3a2de9ae4e007a491a64974b7dd15244fa65fb6cdc11ceb71867d74285b`.
Image config: `sha256:86274702693c3e8e97c748dc1327222a760a9ff483602fedb2540997a84d0710`.
This is a local QA image, not a published GHCR artifact.

Changelog coverage: pass (2 PRs)

#1156 is listed under Internal; its no-changelog label excludes it from the
coverage gate's required PR count.

## Unchanged-input proof

`git diff --name-only 5dc8609d f6b303ea352ded06a5970cef077c68d6abf7b97c`
returned exactly:

```text
CHANGELOG.md
docs/RELEASE_WRAP_0.24.0-beta.8.md
tests/test_release_wrap_validator.py
tests/unittest_test_counts.json
```

The Dockerfile, build definitions and every copied input are unchanged.
Neither changed test file is copied into the image. The browser fixtures,
QA tooling, package locks, templates and static assets are also unchanged.
The final evidence amendment changes only this wrap. The original image and
browser receipts remain attributed to `5dc8609d`, not relabelled as fresh
execution on the successor. No image rebuild or browser replay was needed.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Merged main `560194dc` without rewriting release-branch history. Whole delta from beta.7 is narrow-eligible, including the explicitly reviewed validator tool and version-only metadata. Changelog coverage passed for 2 required PRs; #1153 is the sole user-visible change, #1155 and #1156 Internal. No operator action beyond the normal image update. | Pass |  |
| Python unit and syntax gates | yes | `dev_check.py --full` on clean committed `f6b303ea352ded06a5970cef077c68d6abf7b97c`, after `npm ci`, with a fresh short real private TMPDIR. Python: 5108 tests run in 626.504s, 6 skipped, no failures. Compileall and bounded Ruff passed. Native `FINAL: PASS`, exit 0; start/end commit and tree matched, clean tree preserved. Prometheus alert rules explicitly SKIP because promtool was absent. The earlier two failing validator-compatibility subtests at `5dc8609d` are retained as historical evidence; #1156 corrected their pre-mode baseline contract. | Pass |  |
| JavaScript syntax gates | yes | The full source gate at `f6b303ea` passed every JS syntax check. JS unit summary: 1550 tests, 1545 passed, 5 skipped, 0 failed. | Pass |  |
| Docker build and health gates | yes | Built `5dc8609d` as the pinned image above; the unchanged-input proof retains this result for the successor. OCI revision and app version matched that build source. A synthetic, network-none container returned `/livez` status ok/version 0.24.0-beta.8 and `/healthz` status ok, with 0 restarts. Smoke container removal verified; no existing deployment was changed. Native build/smoke exit 0. | Pass |  |
| Optional-sidecar runtime matrix | yes | Deferred to the next rc/final full run. Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Deferred |  |
| Full Playwright/browser gates | yes | Remote fixture checks at `5dc8609d`, retained by the unchanged-input proof: heatmap-temperature-bands 46 passed; selected UniFi chip/narrow-layout cases 29 passed. ui-coverage first run: 10 passed, 1 transport-closed failure after the connection aged about 15 minutes; only that storage-view-first test reran on a fresh connection and passed. Original failure retained, not relabelled as a green whole-file run. Complete-file exact-head CI remains a separate remote gate. Supplemental UniFi captures: 28 passed. Human-view review covered every face family, heat off/temperature/long Power-On Hours, at 390/1600/1920/3840 using unscaled native crops. Known open #1157 is pre-existing ENVR Core 390 px pool-label clipping, not a chip regression from #1153. Post-publish narrow read-only twin crawl completed all 64 planned units on the published image at all four widths. Native complete-bay review verified the reported #1150 and #1151 shapes; both issues now have released-and-verified comments and completed closures. Known findings and transport recovery remain explicit below. Full UI crawl: Deferred to the next rc/final full run. | Pass |  |
| Feature-specific live API/UI gates | yes | Required narrow parts passed at `5dc8609d` on synthetic HTTP fixtures and shipping candidate templates/assets, with no appliance contact; inputs unchanged by the proof above. Chip placement, protected text/LED/latch clearance, per-bay and G2 fallbacks, all face families and all four widths were exercised. Native visual review found no chip obstruction in the complete bays inspected. Known open #1157 remains visible in the human-view evidence. CORE VM checks and Archive comparator: Deferred to the next rc/final full run. | Pass |  |
| Local release perf harnesses | yes | Deferred to the next rc/final full run. Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Deferred |  |
| Linux QA restore gate | yes | Legacy live restore and default tar.zst/explicit 7z round trips: Deferred to the next rc/final full run. Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Deferred |  |
| Restored Linux QA perf harnesses | yes | Deferred to the next rc/final full run. Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Deferred |  |
| Snapshot/export/offline artifact gate | yes | Candidate ui-coverage at `5dc8609d` built its offline HTML through the real SnapshotExportService/public-demo fixture builder, not the older checked-in demo; these inputs are unchanged. Offline default/about/heatmap/healthy and empty bay selection/history/connections completed at 1600/3840, with the absent fault bay explicitly not applicable. This is synthetic exporter/browser evidence, not a live export or native file-scheme check. Live archive-format round trips are separately Deferred. | Pass |  |
| Docs/wiki/public-demo gate | yes | Public docs PASS: 26 documents, 197 wiki links, 0 prose patterns. Checked-in screenshot manifest and exact-byte review PASS. Release notes rendered; metadata, source diff and whitespace reviewed. wiki/ unchanged from beta.7. The full source gate at `f6b303ea` passed the checked-in public-demo artifact and performance baseline checks. Public demo deferred: prerelease. No public screenshots or wiki publication was performed. | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | [Publish GHCR Image run 42976813853](https://github.com/gcs8/truenas-jbod-ui/actions/runs/42976813853) completed successfully. `v0.24.0-beta.8`, `0.24.0-beta.8` and `dev` converge on `ghcr.io/gcs8/truenas-jbod-ui@sha256:6cf83c4211072d9f024b6f3acc0c1fd7d16ca82fcebcfb20f0d16f33520919e5`; OCI revision `1fc22cfc12d40d859d0a4e14106c2a155a34ecb2`, version `0.24.0-beta.8`. GitHub release is a prerelease, not a draft. GHCR latest and GitHub latest remain v0.23.0. | Pass |  |
| Deployment refresh/sniff tests | yes | Both authorized beta test installations adopted the published immutable digest through the tag-pinned helper, update and independent verify exit 0. Real private receipts, exact Compose projects/file order/profiles/services, pre-update rollback image `ghcr.io/gcs8/truenas-jbod-ui@sha256:37004ccfdbf14719cfd0d17d90c03e35f59ab489b6bfdec73ad14494cead5489`, running container IDs, receipt hashes, health/restarts, read-only writer fences and durable-state integrity were independently read back. Complete placement before/after matched. Existing Compose and operator override bytes/modes were retained; only the image pin changed. API response parity and the required browser examination passed; the second test install passed main/Fabric at 1920 and 3840 with raw stats persisted before assertions and no HTTP/console errors. The first install passed the revised idle-time session observation with unchanged alert and retained-session totals and no new failed-alert mail. The earlier session-count observation and verified rollback are preserved below. This Pass covers only the authorized beta installations, not production or a deferred restore/index-build rehearsal. | Pass |  |
| Post-release reopen | yes | Deferred until owner-authorized final promotion and external wiki publication. The immutable beta tag/image are unchanged; this evidence-only follow-up does not reopen the release source or claim final validation passed. | Blocked |  |

## Visual and transport boundaries

The 390 px layout is best effort, not newly supported. Narrow hardware rows
retain their internal horizontal scroll, so partial neighboring bays in a
capture are not complete-bay acceptance evidence. Native crop indexes record
literal source rectangles and PNG hashes. Initial drawer and ENVR crops cut
off a bay edge; corrected crops of the same source PNGs were inspected instead.
An ENVR Core 390 px off-mode pool label is vertically clipped, without a chip
collision. This is known open issue #1157, pre-existing and not a regression
from #1153. No general claim of a defect-free whole UI is made.

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

## Phase boundaries

Phase A qualified source and the QA image without publishing or deploying.
PR #1154 subsequently completed its exact-head CI and Codex gates. The
authorized phase-B publication and test-install results are recorded here,
not relabelled as phase-A execution. Earlier release results are not carried
forward as fresh beta.8 evidence. Production remains outside scope.


## Published identity and observation history

Annotated tag `v0.24.0-beta.8` remains on
`1fc22cfc12d40d859d0a4e14106c2a155a34ecb2`.
[GitHub prerelease](https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.24.0-beta.8).
The release PR was actually squash-merged, rather than preserving its
ancestry. Its tree `7b8b935940b5445e0a0980a73d70c13e0c9b5958` equals the
reviewed `302b5cd4b8d9959b1f42686e1eaaa6d000324588` tree. Exact merge-commit
push CI and CodeQL completed successfully before publication. Tree equality
is content evidence, not a new test execution. The published image is
distinct from the earlier local QA image identified above.

The first test-install observation overlapped an ordinary collection cycle
and showed an increased session count. That attempt was rolled back through
the helper and independently verified on beta.7. Read-only diagnosis later
found the window sessions gone. A fresh beta.8 adoption then passed the
revised per-session, idle-time post check, more than thirty minutes after
adoption. Alert totals and retained sessions matched the pre-snapshot, with
no new failed-alert mail. Both attempts remain in restricted evidence; the
initial observation is not silently changed into a pass.

The second installation used its existing privileged backup/staging pattern
after shared-host coordination cleared. Every staged source file was
individually SHA-verified before use, and its consistent SQLite backup passed
quick_check and checksum readback. The operator override and writer-fence
contract were preserved. No rollback was needed on this installation.

Raw topology, identifiers, measurements, screenshots, backup paths and
validated private deployment receipts remain in the restricted maintainer
handoff. The external wiki and public demo were not published or changed.
Final promotion and production actions still require the owner's decision.


## Completed narrow post-publish crawl

The fresh read-only main-page crawl completed 64 accepted units. It exercised
513 target/state combinations at 390, 1600, 1920 and 3840 px, producing 2052
accepted done records and full captures. Each target covered default,
temperature, all sixteen metric choices including Power-On Hours, and heat
on then off. The executed identity matrix had no duplicate or missing
state/width combination. This is touched-state coverage, not the deferred
full-application crawl.

The native merge exited 0 with safety ok, zero admitted writes, zero blocked
requests and zero new error-severity findings. Its 639 retained known errors
comprise 367 accessible-name mismatches under #1110, 135 heat-spread findings
and 135 heat-contrast findings under #1103, and two NVMe carrier overlaps
under #1114. The extra all-scope metric coverage observes the already-open
NVMe canvas defect; those instances are not silently dropped. No new issue
was filed. Live writes, LED actions, appliance-load and download states were
not enabled.

One unit lost its browser connection. Its fail-closed report and partial
captures remain preserved and excluded from the accepted merge. Only that
unit was retried on a fresh connection; already-completed units were not
repeated. The retry and all remaining units passed. Elapsed time does not
establish the cause of the disconnect, and independent remote-slot cleanup
is not claimed.

Native unscaled crops show the right-latched 3.5-inch front bays at 390 px
clear of the slot-number, LED and latch collision. A populated dense,
top-latched 2.5-inch bay at 3840 px shows a complete five-digit rotated hours
value clear of its check chip. These are actual published-image captures,
not placeholder-only proof. Representative enclosure and storage-view faces
were inspected with complete-bay boundaries; cropped neighbors and
intentionally scrolled-off overlays are not counted as complete-bay visual
evidence. Retained G2/ENVR and long-value synthetic matrices supplement
shapes or data not present in every live capture, without being relabelled
live hardware coverage. Some source warnings and unavailable metrics remain
visible. No general defect-free or complete hardware-telemetry claim is made.

Released-and-verified comments and completed issue state were independently
read back for [#1150](https://github.com/gcs8/truenas-jbod-ui/issues/1150#issuecomment-6795786182)
and [#1151](https://github.com/gcs8/truenas-jbod-ui/issues/1151#issuecomment-6795787243).
Issues #1157 and #1158 remain open. Issues awaiting production evidence were
not changed. Source changes merged after the beta tag are not part of this
immutable release and are not deployed by this documentation update.

The four allowlisted Deferred rows remain valid only for this narrow beta.
Every deferred long gate still belongs to the next rc/final full run. The
external wiki publication and post-release reopen rows remain Blocked.

The post-publish final-phase validator was run against the immutable release
commit above and external wiki tip
`788e8459c68c9406533514459870cd63906aacb2`. It exited 1 with exactly four
diagnostics: the Blocked post-release reopen row, the Blocked publication
row, the publication row requiring Pass after publication, and external
wiki content differing from repository `wiki/`. Browser, GHCR and beta
deployment rows no longer fail. This is a retained final-validation hold,
not a passing final release. Publishing the wiki or reopening for final is
outside this beta's authority.
