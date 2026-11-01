# Release wrap: v0.24.0-beta.4

Date: `2026-11-01`

## Scope

v0.24.0-beta.4 is the fourth prerelease of v0.24.0. It is rebuilt from current
`main` and adds the 50 pull requests merged after `v0.24.0-beta.3`. It
supersedes the earlier beta.4 candidate in #967 (`9478b11`/`f9e605f`), which
was 95 commits behind `main` and is not tagged. GHCR gets the fixed
`v0.24.0-beta.4` and `0.24.0-beta.4` tags and moves `dev` to it; `latest`
stays on `v0.23.0`.

Release preparation branch: `release/v0.24.0-beta.4-r2`. It is merged into
`main` with a merge commit, never squashed, so the validated commit stays
reachable from the tag, which goes on that `main` merge commit.

Base: `main` at `a743e9f19a47f4f9b7c3266060cf74093afa2d37`, which includes the
CORE multipath bay attribution fix (#1060, for #1059). The branch was first cut
from `main` at `401d536b075f0ebb37c2706465ea6d38c3a2d768` (release commit
`95ba5befb4cc448c5da3c49085216333cde276c2`) and then took current `main` with
the merge commit `857d3ca4`.

Validated release-branch commit: `d6325c021c30edc033544af55a7234fa28e27842`
(`main` at `a743e9f1` plus the version bump, changelog cut, release notes and
the two release-metadata tests). Later commits change only this wrap. #1060
changed image inputs, so every runtime gate below was rerun on this commit;
no runtime evidence from `95ba5be` is carried over.

Validated Linux QA image:
`sha256:7e29be4f41e72c776c91b01c025fdc2d57cb3af1f875e79fab94ffdce7a89ff0`, built
once on the Linux QA host from that commit with
`org.opencontainers.image.revision` set to it. It reports `0.24.0-beta.4`, and
every QA run below used that image ID.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section.

Changelog coverage: pass (50 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `release/v0.24.0-beta.4-r2` at `d6325c021c30edc033544af55a7234fa28e27842` is `main` at `a743e9f19a47f4f9b7c3266060cf74093afa2d37` (merged in with `857d3ca4`, no force push) plus the prerelease version bump; `## v0.24.0-beta.4 - 2026-11-01` covers every PR merged since `v0.24.0-beta.3`, including #1060; `check_release_changelog_coverage.py v0.24.0-beta.3` passed with the external wiki commit `788e8459c68c9406533514459870cd63906aacb2` verified as a branch tip | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `d6325c0` (exit 0, first run): 4,726 Python tests OK (6 skipped), compileall, bounded Ruff, diff hygiene, test-count manifest, performance baseline and checked public-demo artifact passed. Locally `promtool` is absent, so the alert-rule gate runs in CI only | Pass |  |
| JavaScript syntax gates | yes | Same full source gate at `d6325c0`: syntax checks for app, admin, history, SAS Fabric, the screenshot script and every `qa/*.spec.js`, plus 1,337 JavaScript unit tests (1,332 passed, 5 skipped, 0 failed) | Pass |  |
| Docker build and health gates | yes | QA image `sha256:7e29be4f41e7` built from `d6325c0` with a matching revision label; reports `0.24.0-beta.4`. UI, history and admin `/livez` and `/healthz` passed in every matrix variant and restore drill. The image's writer-fence entrypoint started every service in each run | Pass |  |
| Optional-sidecar runtime matrix | yes | `scripts/run_compose_runtime_matrix.py` on the Linux QA host at `d6325c0`: 8 variants passed (including the non-root UI variant); 5 alias cycles, 5 mapping cycles, 1 admin setup, 1 scheduler disabled, 1 scheduler backup, 2 journal reads; no containers left | Pass |  |
| Full Playwright/browser gates | yes | Live read-only restore at `d6325c0`: 16 passed, 2 skipped for `qa/ui-switching.spec.js` and `qa/esxi-smoke.spec.js` (the history refresh actions need fixtures the restored data lacks; auto-refresh after a switch needs UI timing); 16 passed, 1 skipped for `qa/private-restore.spec.js` and `qa/admin-operations.spec.js` in each restore. Locally at `d6325c0`: the 8 fixture-only specs 274 passed, `qa/admin-operations.spec.js` 15 passed, `qa/public-demo.spec.js` against a current-source demo build 9 passed | Pass |  |
| Feature-specific live API/UI gates | yes | Disposable CORE VM (TrueNAS 13.0-U6.8) through the candidate's admin routes at `d6325c0`: all bootstrap, SMART-grant and TLS checks passed and the task account was removed (see "Fix checks"). Read-only SSH-only collection on a CORE host with SES shelves, in the candidate image `sha256:7e29be4f41e7`: every read succeeded and the independent comparator (its own parsers, no app code) confirmed pool, vdev and class for every occupied bay, on two identical runs. Storage-view fixes #956, #959, #963 and #966 exercised in Chromium on populated views of a live read-only restored stack in the same image, with that stack's own admin service (see "Storage-view checks"); the degraded-pool state for #956 used the repo's fixture generator because it cannot be produced read-only. 0 console errors | Pass |  |
| Local release perf harnesses | yes | Against the live restored stack at `d6325c0`, after a completed full history pass: `run_perf_harness.py`, 8 workflows passed (forced and cached inventory, storage views, health, history status, SMART batch, chunked SMART prefetch, snapshot estimate); `run_history_perf_harness.py`, 5 workflows passed including exact counts. `build_perf_baseline.py --check` passed in the source gate | Pass |  |
| Linux QA restore gate | yes | At `d6325c0`: default `tar.zst` round trip (the beta.3-made default archive restored with exact counts; a FULL export with no `packaging` field came out `tar.zst`; that export, made by `0.24.0-beta.4`, restored with exact counts, restart survival and verified cleanup) and the legacy `7z` round trip of a `0.22.2` backup in live read-only mode. Blocked: the explicit `packaging: "7z"` FULL export with the full path set, from a stack restored with exact counts, failed with HTTP 400 "Portable 7z backup operation timed out." The export runs single-threaded LZMA2 under a fixed 600-second limit (unchanged since v0.10.0), and the restored history is too large to finish in time, so there was no `7z` archive to restore. Receipts under "Restore evidence" | Blocked |  |
| Restored Linux QA perf harnesses | yes | The live read-only restore of the `0.22.2` `7z` backup at `d6325c0` passed the app and history harnesses; both egress-blocked `tar.zst` restores passed the history harness | Pass |  |
| Snapshot/export/offline artifact gate | yes | Redacted Auto and Force ZIP exports from two restored stacks at `d6325c0` rendered all 60 slots in Chromium with network blocked: offline badge shown, 0 external requests, 0 console errors, 0 private IPv4 addresses, 0 host paths. The 2 credential-pattern hits are the app's own sign-in field code | Pass |  |
| Docs/wiki/public-demo gate | yes | `check_public_docs.py` passed (26 documents, 195 wiki links, 0 prose patterns); `check_public_screenshots.py` passed; `check_public_demo_artifact.py` found the demo publishable (2,095,850 raw bytes); `validate_release_wrap.py 0.24.0-beta.4 --public-demo-only` passed. Public demo deferred: prerelease. `wiki/` changed in 5 files since `v0.24.0-beta.3` (`Admin-UI-and-System-Setup.md`, `SSH-Setup-and-Sudo.md`, `Troubleshooting.md`, `TrueNAS-CORE-Setup.md`, `Upgrading.md`) | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Recorded after the GitHub prerelease publishes `v0.24.0-beta.4`, `0.24.0-beta.4` and `dev` | Blocked |  |
| Deployment refresh/sniff tests | yes | Recorded after the `dev` image is deployed to a QA host for beta testing | Blocked |  |
| Post-release reopen | yes | Blocked on the deployment gate | Blocked |  |

## Restore evidence

All drills ran on a disposable Linux QA host with
`scripts/run_private_qa_restore.py` at `d6325c0`, image
`sha256:7e29be4f41e72c776c91b01c025fdc2d57cb3af1f875e79fab94ffdce7a89ff0`. Archive digests, system
names, counts and timings stay in the restricted QA receipts.

**Default-format round trip (`tar.zst`), egress-blocked.** The beta.3-made
default-format archive restored with exact counts and every saved system.
Inspection reported `tar.zst`, encrypted, schema 2, no absent groups. A
redacted snapshot export rendered offline. A FULL export with no `packaging`
field came out encrypted `tar.zst`, schema 2, with 0 restart failures.
Restoring that export: inspection `tar.zst`, encrypted, export source
`0.24.0-beta.4`, no absent groups; import restored the history database and
every saved system; counts matched exactly; restored rows survived the
restart; the SAS Fabric label pencil cycle cleaned up; offline browser and
history performance checks passed; cleanup was verified.

**Legacy round trip (`7z`), live read-only.** An encrypted FULL backup from a
`0.22.2` deployment. Inspection reported `7z`, encrypted, schema 2. It was
missing only the runtime-overrides and SAS Fabric alias groups, which that
release did not export. Import restored the history database and every saved
system with 0 restart failures. Counts reconciled, with history growth limited
to new events. Restored rows survived the restart, and the SAS Fabric label and
slot-mapping pencil cycles cleaned up. The live browser, app performance,
history live pass and history performance checks passed. The QA host's
long-running beta deployment was stopped during the run so only one collector
polled the appliances. It came back healthy on the same container IDs with 0
restarts.

**Explicit `7z` FULL export round trip: failed.** On the same Linux QA host and
image, the beta.3-made default archive was restored again with exact counts
and restart survival, then the admin API was asked for an encrypted FULL export
with `packaging: "7z"` and all ten backup groups. After about 11 minutes the
export answered HTTP 400 "Portable 7z backup operation timed out." and wrote no
archive, so no `7z` restore could run. The `7z` writer uses one thread and a fixed
600-second limit. Both are unchanged since v0.10.0, so this is not new in this
beta, but production-sized history no longer fits in the limit. The stack was
torn down with no containers, networks or volumes left, and the long-running
beta deployment came back on the same container IDs.

## Fix checks

Run on the owner-admitted disposable CORE VM (TrueNAS 13.0-U6.8) at
`d6325c0`, through the candidate's own admin service and HTTP routes, with
strict host-key checking against a pinned key:

- #1055: bootstrap for BMC/IPMI and ESXi was refused before any key lookup or
  connection, and the IPMI sudo preview is disabled.
- #957, #960, #1025: a Home directory that is not a folder on a pool was refused
  before connecting. A missing account with no Home directory was refused with
  no account, group or home created. With a Home directory on the pool,
  bootstrap created the account through `user.create`; the middleware readback
  matched home, shell `/bin/sh`, disabled password, SMB off, the installed key
  and the full 25-entry command policy. Re-running it for the existing account
  with a different Home directory kept the original home and reported no
  account created.
- #957: as the new service account, `sudo -n -l` (list mode only) allowed the
  6 SMART read forms and refused the 5 forms with self-tests, setting changes,
  extra arguments or path traversal.
- #965, #1054: on loopback TLS endpoints, inspection flagged an expired and a
  not-yet-valid certificate and trust-remote refused to pin either; a valid one
  was pinned. The main UI pointed at the expired endpoint named the failed
  certificate check instead of calling the API unreachable.
- Cleanup: the task account, its group and both candidate homes were removed;
  a fresh session confirmed none remain.

Read-only on a CORE host with SES shelves, in the candidate image
`sha256:7e29be4f41e7` at `d6325c0` (API deliberately absent, so the page has
only SSH data; #955, #958, #1022, #1060):

- Every read succeeded. Bays mapped over SSH, each shelf kept its own id and
  profile, the "turn on SSH" and "no drive bays" warnings were absent, and the
  page named SSH as the source of the bays.
- An independent comparator with its own parsers for `gmultipath list`,
  `glabel status` and `zpool status` (no application code) joined each
  occupied bay to its multipath group, or to its own disk labels when it has
  none, and confirmed pool, vdev and class for every occupied bay on two
  identical runs. On `95ba5be` (before #1060) the same comparator found two
  bays with another geom's label and vdev; both are now confirmed, and every
  label the page shows sits on that bay's own disk in the raw `glabel` rows.
- Storage views on that host have no occupied bays, so the storage-view fixes
  were checked on other restored systems (next section).

## Storage-view checks

In Chromium against a live read-only restore of the production backup, in
image `sha256:7e29be4f41e7` at `d6325c0`, choosing systems whose views have
occupied slots. Admin checks used that restored stack's own admin service, and
its saves changed only that disposable stack's configuration:

- #956: on a CORE system, every occupied NVMe-carrier and SATADOM slot was drawn
  healthy, and the detail panel and tooltip showed `Vdev` and `Health ONLINE`.
  No pool warning appeared. A DEGRADED pool cannot be made read-only, so the
  degraded state ran as a fixture: the real inventory code with the repo's
  synthetic degraded pool, rendered by
  `scripts/build_current_source_browser_fixture.py` with the candidate's
  `app.js`. The degraded member was drawn as Fault with `Health DEGRADED`, and
  the page warned `Pool flashpool is DEGRADED.`. The ONLINE control had neither.
- #959: on a SCALE system with a populated generated chassis view, choosing a
  layout and saving set the system layout. The generated Primary Chassis
  stayed unsaved and unpinned, and the main page still called it "Chassis". A
  pinned saved chassis on another system still reads "Saved view". After the
  Primary Chassis was deleted and saved, it stayed deleted through a reload and
  another save. The live shelf still showed its occupied bays.
- #963: on CORE, new passive 2-slot and AOC-SLG4-2H8M2 views started with no
  ESXi RAID IDs and were shown on the main page. Bound by serial to real NVMe
  drives, each showed 2 of 2 slots occupied and healthy. On an ESXi system, the
  same AOC template filled in the `C0 x4`/`C1 x4` and `13:0`/`13:1` IDs, and its
  live AOC enclosure still showed both drives.
- #966: Add a view listed the AOC card and the FatTwin rear 2-bay group once, as
  views only. The Chassis layout list left both out, while the ESXi system kept
  its saved AOC layout as `Current:`. The drive list read "Drives on <system>
  that are not in a chassis bay or another view:" under "Drives for this view",
  and showed no bare `UNKNOWN`.

## Deviations

- The first candidate (`95ba5be`, before #1060) failed the feature-specific
  gate: the independent comparator found two CORE multipath bays showing
  another geom's label and vdev (#1059). #1060 fixed it on `main`, the release
  branch took `main`, and every gate in this wrap was rerun on `d6325c0`.
- `dev_check.py --full` ran with a short private temp folder because two tests
  bind a Unix socket and the default scratch path is too long for one. Earlier
  rounds showed this is a workstation path-length limit, not a source issue.
  The `d6325c0` run passed the first time.
- As for earlier betas, the QA host's long-running beta deployment uses the
  same fixed container names as the restore controller and the Compose
  matrix. It was stopped and its containers renamed aside for each run, then
  renamed back and started on the same container IDs.

- The storage-view browser checks needed a few harness fixes (a hidden order
  field, the carrier claim order, and committing typed serials). No product
  code changed. One check read a cached snapshot taken while that CORE system's
  API was briefly down. It was rerun after a forced read with the API back.

## Known issues carried into the beta

- A forced refresh without an enclosure selection can still answer 503 "busy"
  with `Retry-After: 1` when another request publishes newer topology while it
  builds. It fails closed by design (#946). Refresh Now in the browser always
  names the enclosure.
- A system whose boot disk has no storage view (for example a virtual SCALE
  boot disk) reports it as unplaced in the system-wide retention check, so
  `--inventory-url` on that system rolls back until a boot-device view is added.
  This is by design.
- The ESXi host without StorCLI still shows bays without drive models. This
  predates the beta.
- After a restore or restart, history reports failed passes until the main UI
  has a trusted inventory, and its next full pass can be an hour away.
- The segmented-history `docker compose run --entrypoint ...` runbook commands
  and other `--entrypoint` overrides do not hold the writer fence (#1057).
  Stop every writer before running the non-root ownership helper with them.

## Hold boundary

This wrap does not authorize a tag while any pre-publish row is `Blocked`. Once
it passes, it authorizes the `v0.24.0-beta.4` prerelease and its `dev` image
only. It does not move `latest`, deploy to production, or publish the external
wiki.
