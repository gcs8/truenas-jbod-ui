# Release wrap: v0.24.0-beta.6

Date: `2026-11-07`

## Scope

v0.24.0-beta.6 is the sixth prerelease of v0.24.0. It is cut from current
`main` and adds the 7 pull requests merged after `v0.24.0-beta.5` that need a
changelog line (#1081, #1083, #1084, #1085, #1086, #1090, #1093), plus the
Renovate and dependency updates #1073, #1074 and #1080 and the beta.5 wrap
follow-up #1079. GHCR gets the fixed `v0.24.0-beta.6` and `0.24.0-beta.6` tags
and moves `dev` to it; `latest` stays on `v0.23.0`.

Release preparation branch: `release/v0.24.0-beta.6`. It is merged into
`main` with a merge commit, never squashed, so the validated commit stays
reachable from the tag, which goes on that `main` merge commit.

Base: `main` at `a89a864832401e08e45128aebb30903fc52ae25d`, which includes the
history index-build memory fix (#1093, for #1092). The branch was first cut
from `main` at `05869875d69e892df66ff52dead812f6ed5ca9f3` (release commit
`dc1281bfcb30af6e7fcb4b2288c39d227beaa573`) and then took current `main` with
the merge commit `4b21e830`.

Validated release-branch commit: `170967f436d57b67743b3223d8bfe0e01da75793`
(`main` at `a89a8648` plus the version bump, changelog cut, release notes and
the release-metadata tests). Later commits change only this wrap. #1093
changed image inputs, so every runtime gate below was rerun on this commit;
no runtime evidence from `dc1281b` is carried over.

Validated Linux QA image:
`sha256:9828f9e068b138844b12d5982728d750f1058deb875ce0a49abbd90440866bd5`, built
once on the Linux QA host from that commit with
`org.opencontainers.image.revision` set to it, on the digest-pinned base
`python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54`.
It reports `0.24.0-beta.6`, and every QA run below used that image ID.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section.

Changelog coverage: pass (7 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `release/v0.24.0-beta.6` at `170967f436d57b67743b3223d8bfe0e01da75793` is `main` at `a89a864832401e08e45128aebb30903fc52ae25d` (merged in with `4b21e830`, no force push) plus the prerelease version bump; `## v0.24.0-beta.6 - 2026-11-07` covers every PR merged since `v0.24.0-beta.5`, including #1093 and its upgrade note; `check_release_changelog_coverage.py v0.24.0-beta.5` passed with the external wiki commit `788e8459c68c9406533514459870cd63906aacb2` verified as a branch tip | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `170967f` (exit 0, first run): 4,891 Python tests OK (6 skipped), compileall, bounded Ruff, diff hygiene, test-count manifest, performance baseline and checked public-demo artifact passed. Locally `promtool` is absent, so the alert-rule gate runs in CI only | Pass |  |
| JavaScript syntax gates | yes | Same full source gate at `170967f`: syntax checks for app, admin, history, SAS Fabric, the screenshot script and every `qa/*.spec.js`, plus 1,362 JavaScript unit tests (1,357 passed, 5 skipped, 0 failed) | Pass |  |
| Docker build and health gates | yes | QA image `sha256:9828f9e068b1` built from `170967f` on the digest-pinned `python:3.12-slim` base with a matching revision label; reports `0.24.0-beta.6`. UI, history and admin `/livez` and `/healthz` passed in every matrix variant and restore drill. The image's writer-fence entrypoint started every service in each run | Pass |  |
| Optional-sidecar runtime matrix | yes | `scripts/run_compose_runtime_matrix.py` on the Linux QA host at `170967f`: 8 variants passed (including the non-root UI variant); 5 alias cycles, 5 mapping cycles, 1 admin setup, 1 scheduler disabled, 1 scheduler backup, 2 journal reads; no containers left | Pass |  |
| Full Playwright/browser gates | yes | Live read-only restore at `170967f`: 16 passed, 2 skipped for `qa/ui-switching.spec.js` and `qa/esxi-smoke.spec.js` (the history refresh actions need fixtures the restored data lacks; auto-refresh after a switch needs UI timing); 16 passed, 1 skipped for `qa/private-restore.spec.js` and `qa/admin-operations.spec.js` in each restore (live, `tar.zst` and `7z`). Locally at `170967f`: the 9 fixture-only specs (including the new `qa/chassis-row-overflow.spec.js`) 280 passed, `qa/admin-operations.spec.js` 15 passed, `qa/public-demo.spec.js` against a current-source demo build 9 passed | Pass |  |
| Feature-specific live API/UI gates | yes | Disposable CORE VM (TrueNAS 13.0-U6.8) through the candidate's admin routes at `170967f`: all bootstrap, SMART-grant and TLS checks passed and the task account was removed. Read-only SSH-only collection on a CORE host with SES shelves, in the candidate image `sha256:9828f9e068b1`: every read succeeded and the independent comparator confirmed pool, vdev and class for every occupied bay. On a live read-only restored stack in the same image: #1081, #1086 and #1083 checked as described in "Fix checks"; storage-view checks (#956, #959, #963, #966) passed with 0 browser console errors and 0 HTTP 5xx on their one allowed unchanged rerun (see "Deviations"); UniFi faces and #1067 labels unchanged from beta.5; middleware limits, peaks and appliance alert and session readbacks as in "Fix checks". Full-page renders of the touched pages at 390, 1600, 1920 and 3840 pixels wide were reviewed | Pass |  |
| Local release perf harnesses | yes | Against the live restored stack at `170967f`, after a completed full history pass: `run_perf_harness.py` and `run_history_perf_harness.py` passed (the restore controller's `app_performance` and `history_performance` checks). `build_perf_baseline.py --check` passed in the source gate | Pass |  |
| Linux QA restore gate | yes | At `170967f`: default `tar.zst` round trip (the beta.3-made default archive restored with exact counts; a FULL export with no `packaging` field came out `tar.zst`; that export, made by `0.24.0-beta.6`, restored with exact counts, restart survival and verified cleanup); explicit `packaging: "7z"` FULL export round trip (the same restored stack exported `7z` with all ten groups within the size-scaled deadline, and that export, made by `0.24.0-beta.6`, restored with exact counts, restart survival and verified cleanup); and the legacy `7z` round trip of a `0.22.2` backup in live read-only mode, with the history container well under its default 1 GiB memory limit while it built the upgrade's background indexes (#1093). Receipts under "Restore evidence" | Pass |  |
| Restored Linux QA perf harnesses | yes | The live read-only restore of the `0.22.2` `7z` backup at `170967f` passed the app and history harnesses; the egress-blocked `tar.zst` and `7z` restores passed the history harness | Pass |  |
| Snapshot/export/offline artifact gate | yes | Redacted Auto and Force ZIP exports from two restored stacks at `170967f` rendered all 60 slots in Chromium with network blocked: offline badge shown, 0 external requests, 0 console errors, 0 private IPv4 addresses, 0 host paths. The 2 credential-pattern hits are the app's own sign-in field code | Pass |  |
| Docs/wiki/public-demo gate | yes | At `170967f`: `check_public_docs.py` passed (26 documents, 197 wiki links, 0 prose patterns); `check_public_screenshots.py` passed; `check_public_demo_artifact.py` found the demo publishable (2,095,850 raw bytes); `validate_release_wrap.py 0.24.0-beta.6 --public-demo-only` passed. Public demo deferred: prerelease. `wiki/` changed in 1 file since `v0.24.0-beta.5` (`Upgrading.md`, #1093) | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Recorded after the GitHub prerelease publishes `v0.24.0-beta.6`, `0.24.0-beta.6` and `dev` | Blocked |  |
| Deployment refresh/sniff tests | yes | Recorded after the `dev` image is deployed to the beta test hosts | Blocked |  |
| Post-release reopen | yes | Blocked on the deployment gate | Blocked |  |

## Restore evidence

All drills ran on a disposable Linux QA host with
`scripts/run_private_qa_restore.py` at `170967f`, image
`sha256:9828f9e068b138844b12d5982728d750f1058deb875ce0a49abbd90440866bd5`.
Archive digests, system names, counts and timings stay in the restricted QA
receipts.

**Default-format round trip (`tar.zst`), egress-blocked.** The beta.3-made
default-format archive restored with exact counts and every saved system.
Inspection reported `tar.zst`, encrypted, schema 2, no absent groups. A
redacted snapshot export rendered offline. A FULL export with no `packaging`
field came out encrypted `tar.zst`, schema 2, with 0 restart failures.
Restoring that export: inspection `tar.zst`, encrypted, export source
`0.24.0-beta.6`, no absent groups; import restored the history database and
every saved system; counts matched exactly; restored rows survived the
restart; the SAS Fabric label pencil cycle cleaned up; offline browser and
history performance checks passed; cleanup was verified. It passed on the
first run.

**Explicit `7z` FULL export round trip, egress-blocked.** The host had well
over the 24 GiB free that the export round trips need before they started.
From the same restored stack, the admin API was asked for an encrypted FULL
export with `packaging: "7z"` and all ten backup groups. The admin log and the
metrics gauge both reported the size-scaled create deadline (#1062), far above
the time the `7z` step took (the step finished within about a tenth of it);
there were no timeout log lines. The export came out encrypted `7z`, schema 2,
with 0 restart failures. Restoring that export: inspection `7z`, encrypted,
export source `0.24.0-beta.6`, no absent groups; import restored the history
database and every saved system; counts matched exactly; restored rows
survived the restart; the SAS Fabric label pencil cycle cleaned up; offline
browser and history performance checks passed; cleanup was verified. It
passed on the first run.

**Legacy round trip (`7z`), live read-only.** An encrypted FULL backup from a
`0.22.2` deployment. Inspection reported `7z`, encrypted, schema 2. It was
missing only the runtime-overrides and SAS Fabric alias groups, which that
release did not export. Import restored the history database and every saved
system with 0 restart failures. After the import restart the history service
built the five background indexes of the `0.22.2` upgrade in under two
minutes. A host-side sampler read the history container's cgroup every two
seconds: its anonymous memory peaked at about 131 MiB and its non-reclaimable
usage (what `docker stats` shows) at about 539 MiB, against the default 1 GiB
`HISTORY_MEM_LIMIT`, with no OOM kill. Counts reconciled, with history growth
limited to new events. Restored rows survived the restart, and the SAS Fabric
label and slot-mapping pencil cycles cleaned up. The live browser, app
performance, history live pass and history performance checks passed. A
redacted snapshot export from this stack also rendered offline. A second live
restore of the same backup, made for the storage-view rerun, passed the same
checks with the same memory profile.

The QA host's long-running beta deployment was stopped during each run so only
one collector polled the appliances. It came back healthy on the same
container IDs with 0 restarts, and no QA containers were left.

## Fix checks

Run on the owner-admitted disposable CORE VM (TrueNAS 13.0-U6.8) at
`170967f`, through the candidate's own admin service and HTTP routes, with
strict host-key checking against a pinned key: bootstrap refusal for BMC/IPMI
and ESXi (#1055), the Home directory and missing-account rules and
`user.create` readback (#957, #960, #1025), the SMART sudo allow/deny list in
list mode only (#957), and expired, not-yet-valid and valid loopback TLS
certificates in inspection, trust-remote and the main UI (#965, #1054) all
passed. The task account, its group and both candidate homes were removed; a
fresh session confirmed none remain.

Read-only on a CORE host with SES shelves, in the candidate image
`sha256:9828f9e068b1` at `170967f` (API deliberately absent, so the page has
only SSH data): every read succeeded, each shelf kept its own id and profile,
and the independent comparator (its own parsers, no application code)
confirmed pool, vdev and class for every occupied bay. It ran the same read
commands as for beta.5.

On the live read-only restored stack in the same image:

- #1081: every 24-bay single-row 2.5" front (two enclosures and a saved
  chassis view) at 1600 pixels overflows its bezel and now scrolls from bay
  00: at scroll start bay 00 sits inside the scroll box and is the element
  under its own centre, a click selects it, the last bay is reachable at the
  end, and no bay is unreachable. At 1920 pixels the row fits. On the beta.5
  test deployment the same fronts at 1600 pixels left bay 00 past the start
  edge, where neither scrolling nor a click could reach it.
- #1086: the CORE Storage Fabric API kept the HBA PHY table and returned
  `reported_lanes` only on the HBA-to-expander and expander cascade links, null
  on every other link. Each carried eight PHY pairs, no PHY twice, matching
  source and target lists and a lane count equal to the pairs; a link whose
  lanes report different rates has a null summary rate, as designed. The
  Storage Fabric page loaded. No new appliance command was run.
- #1083: on this segmented history, `/api/admin/history/systems` returned
  `total_rows` for every saved system, all non-zero and each equal to the sum
  of its per-table counts. The restored data has no removed-system history, so
  the admin page's removed-system list showed none; the row counts it would
  show come from the same summary.
- UniFi and other non-TrueNAS systems: the UNVR and UNVR Pro faces showed the
  same disk in every bay as on the beta.5 test deployment (slot, device and
  serial hash), and the API, SSH and bay-source labels (#1067) read as in
  beta.5.
- The main UI and Admin `/metrics` were read every few seconds through both
  live restores and every live check. For the CORE appliance the call budget
  was 4, split UI 2, Admin 1 (history reads through the UI); each service's
  `calls_in_flight_peak` stayed at its own limit and the most calls seen in
  flight at once across services was 3. No middleware busy or backoff lines
  were logged.
- Before the first live phase and after the last one, read-only snapshots of
  the CORE appliance's alerts, alert mails and API sessions were taken through
  the twin's saved key. The QA window added no "Failed to check for alert"
  mail and no retained API session from the QA host. Active alerts were the
  same pre-existing ones before and after.

Full-page renders of the pages beta.6 touches (the 24-bay single-row fronts,
the CORE Storage Fabric page and the admin page with its history cleanup
section) were captured in remote Chromium at 390, 1600, 1920 and 3840 pixels
wide and reviewed. Bay 00 is visible at scroll start at every width; nothing
new renders wrong. Pre-existing and unchanged in this release: at 390 pixels
the enclosure panel title wraps one letter per line beside its buttons, and
the admin page is wider than the viewport.

## Storage-view checks

In Chromium against a live read-only restore of the production backup, in
image `sha256:9828f9e068b1` at `170967f`, with the beta.5 driver unchanged.
Admin checks used that restored stack's own admin service, and its saves
changed only that disposable stack's configuration. #956 (healthy CORE NVMe
carrier and SATADOM slots with `Vdev` and `Health ONLINE`), #959 (generated
SCALE chassis stays unsaved and unpinned; saved views keep their labels; a
deleted Primary Chassis stays deleted), #963 (CORE passive and AOC views
without ESXi RAID IDs, bound by serial; the ESXi AOC template keeps its IDs)
and #966 (view-only templates listed once, layout list excludes them, drive
list wording) all passed on the rerun described under "Deviations". Across
both runs, the main page and the admin page logged 0 console errors and
received 0 HTTP 5xx responses.

## Deviations

- The first candidate (`dc1281b`, before #1093) failed the Linux QA restore
  gate: the restored history container was killed by its 1 GiB memory limit
  while it built the `0.22.2` upgrade's background indexes, on both of its
  runs. The same drill on the beta.5 image peaked at the same memory, so the
  cause predates beta.6. Its export round trips did not run for lack of QA
  host disk. #1093 fixed the memory on `main`, the QA host disk was grown, and
  every gate in this wrap was rerun on `170967f`.
- The storage-view driver's first run on `170967f` failed one check: the CORE
  drive list in the admin storage-view editor said it could not list drives,
  because a CORE middleware read timed out while the admin service held its
  single call. Every other check in that run passed, with 0 console errors
  and 0 HTTP 5xx. The driver saves to the restored stack's configuration, so
  its one allowed unchanged rerun used a second, freshly restored live stack
  from the same backup and image; all checks passed. The same read timeouts
  were logged in the passing run and in beta.5's passing run, so the failure
  is intermittent and timing-dependent; its cause is not isolated.
- `/api/admin/history/systems` and `/api/admin/history/orphaned` take minutes
  on this restored history (the admin service's per-system summary scan,
  unchanged in this release). One check's client gave up after five minutes;
  a direct read of the same route completed and gave the counts above.
- `dev_check.py --full` ran with a short private temp folder because two tests
  bind a Unix socket and the default scratch path is too long for one.
- As for earlier betas, the QA host's long-running beta deployment uses the
  same fixed container names as the restore controller and the Compose
  matrix. It was stopped and its containers renamed aside for each run, then
  renamed back and started on the same container IDs.
- The restored `0.22.2` configuration predates the UniFi host-key pins, so the
  twin's two UniFi host-key lines were added to the disposable restored
  stack's known-hosts file before the UniFi checks. No product code changed
  for the checks.
- The clean-host restore qualification (#1085, #1090) passed on a disposable
  host at `05869875` before this cut and, as directed, was not rerun.

## Known issues carried into the beta

- A forced refresh without an enclosure selection can still answer 503 "busy"
  with `Retry-After: 1` when another request publishes newer topology while it
  builds. It fails closed by design (#946). Refresh Now in the browser always
  names the enclosure.
- The ESXi host without StorCLI still shows bays without drive models. This
  predates the beta.
- After a restore or restart, history reports failed passes until the main UI
  has a trusted inventory, and its next full pass can be an hour away.
- The segmented-history `docker compose run --entrypoint ...` runbook commands
  and other `--entrypoint` overrides do not hold the writer fence (#1057).
  Stop every writer before running the non-root ownership helper with them.
- API sessions that earlier versions left on a CORE 13 appliance stay listed
  until its middleware restarts.
- On a large history the admin service's history summaries (row counts and
  removed-system history) take minutes to load.
- While a CORE appliance's middleware is slow, the admin storage-view editor
  can say it could not list drives; reselecting the view retries.

## Hold boundary

This wrap does not authorize a tag while any pre-publish row is `Blocked`. Once
it passes, it authorizes the `v0.24.0-beta.6` prerelease and its `dev` image
only. It does not move `latest`, deploy to production, or publish the external
wiki.
