# Release wrap: v0.24.0-beta.5

Preparation date: `2026-11-02`
Published: `2026-11-03`

## Scope

v0.24.0-beta.5 is the fifth prerelease of v0.24.0. It is cut from current
`main` and adds the 5 pull requests merged after `v0.24.0-beta.4` (#1067,
#1069, #1070, #1076, #1078) plus the Dependabot Python update #1071. GHCR gets
the fixed `v0.24.0-beta.5` and `0.24.0-beta.5` tags and moves `dev` to it;
`latest` stays on `v0.23.0`.

Release preparation branch: `release/v0.24.0-beta.5`. The gated runner
squash-merged release PR #1077 into `main` as
`78c6efee117881a93d971eea65f74b4bc4b0f4e5`, and the published tag
`v0.24.0-beta.5` points to that commit. This differs from the planned
merge-commit flow. The squash commit and validated PR head
`75df0057c1b4ac187f0d4087a5e1d2ef13c3c1f2` have the identical tree
`4f3e250579488050842dcd6219073e1aab9695f7`. The diff from runtime-qualified
`d1e8ea23d0d69f250e7592ce6974c7a5d498cd7c` to the tag changes only this
wrap. The source-content proof preserves the earlier qualification's scope;
it does not claim the release-branch commits are ancestors of the squash.

Base: `main` at `615c2d9e4c8ba1ecaddf891372bf852929c85315`, which includes the
per-appliance middleware call budget split by service role (#1078, for
#1072). The branch was first cut from `main` at
`2a1d12454d401126e681e0a79062b1e70beb268b` (release commit
`ba229db549d324b2bd2e446f3ccc506b0a74cdca`) and then took current `main` with
the merge commit `d1e8ea23`.

Validated release-branch commit: `d1e8ea23d0d69f250e7592ce6974c7a5d498cd7c`
(`main` at `615c2d9e` plus the version bump, changelog cut, release notes and
the release-metadata tests). Later commits change only this wrap. #1078
changed image inputs, so every runtime gate below was rerun on this commit;
no runtime evidence from `ba229db` is carried over.

Validated Linux QA image:
`sha256:e7ac30dd659479bfef0733e068b954feed52da8cd808b9d5adaaa70ff9edcf68`, built
once on the Linux QA host from that commit with
`org.opencontainers.image.revision` set to it. It reports `0.24.0-beta.5`, and
every QA run below used that image ID.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section.

Changelog coverage: pass (5 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `release/v0.24.0-beta.5` at `d1e8ea23d0d69f250e7592ce6974c7a5d498cd7c` is `main` at `615c2d9e4c8ba1ecaddf891372bf852929c85315` (merged in with `d1e8ea23`, no force push) plus the prerelease version bump; `## v0.24.0-beta.5 - 2026-11-02` covers every PR merged since `v0.24.0-beta.4`, including #1078 and its upgrade note; `check_release_changelog_coverage.py v0.24.0-beta.4` passed with the external wiki commit `788e8459c68c9406533514459870cd63906aacb2` verified as a branch tip | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `d1e8ea2` (exit 0, first run): 4,814 Python tests OK (6 skipped), compileall, bounded Ruff, diff hygiene, test-count manifest, performance baseline and checked public-demo artifact passed. Locally `promtool` is absent, so the alert-rule gate runs in CI only | Pass |  |
| JavaScript syntax gates | yes | Same full source gate at `d1e8ea2`: syntax checks for app, admin, history, SAS Fabric, the screenshot script and every `qa/*.spec.js`, plus 1,362 JavaScript unit tests (1,357 passed, 5 skipped, 0 failed) | Pass |  |
| Docker build and health gates | yes | QA image `sha256:e7ac30dd6594` built from `d1e8ea2` with a matching revision label; reports `0.24.0-beta.5`. UI, history and admin `/livez` and `/healthz` passed in every matrix variant and restore drill. The image's writer-fence entrypoint started every service in each run | Pass |  |
| Optional-sidecar runtime matrix | yes | `scripts/run_compose_runtime_matrix.py` on the Linux QA host at `d1e8ea2`: 8 variants passed (including the non-root UI variant); 5 alias cycles, 5 mapping cycles, 1 admin setup, 1 scheduler disabled, 1 scheduler backup, 2 journal reads; no containers left | Pass |  |
| Full Playwright/browser gates | yes | Live read-only restore at `d1e8ea2`: 16 passed, 2 skipped for `qa/ui-switching.spec.js` and `qa/esxi-smoke.spec.js` (the history refresh actions need fixtures the restored data lacks; auto-refresh after a switch needs UI timing); 16 passed, 1 skipped for `qa/private-restore.spec.js` and `qa/admin-operations.spec.js` in each restore (live, `tar.zst` and `7z`). Locally at `d1e8ea2`: the 8 fixture-only specs 275 passed, `qa/admin-operations.spec.js` 15 passed, `qa/public-demo.spec.js` against a current-source demo build 9 passed | Pass |  |
| Feature-specific live API/UI gates | yes | Disposable CORE VM (TrueNAS 13.0-U6.8) through the candidate's admin routes at `d1e8ea2`: all bootstrap, SMART-grant and TLS checks passed and the task account was removed. Read-only SSH-only collection on a CORE host with SES shelves, in the candidate image `sha256:e7ac30dd6594`: every read succeeded and the independent comparator confirmed pool, vdev and class for every occupied bay. Storage-view fixes #956, #959, #963 and #966 exercised in Chromium on a live read-only restored stack in the same image: 0 browser console errors and 0 HTTP 5xx responses. #1069 and #1067 checked on the restored UniFi and ESXi systems, #1070 with a synthetic fixture, #1076/#1078 with the service metrics and appliance alert and session readbacks (see "Fix checks") | Pass |  |
| Local release perf harnesses | yes | Against the live restored stack at `d1e8ea2`, after a completed full history pass: `run_perf_harness.py` and `run_history_perf_harness.py` passed (the restore controller's `app_performance` and `history_performance` checks). `build_perf_baseline.py --check` passed in the source gate | Pass |  |
| Linux QA restore gate | yes | At `d1e8ea2`: default `tar.zst` round trip (the beta.3-made default archive restored with exact counts; a FULL export with no `packaging` field came out `tar.zst`; that export, made by `0.24.0-beta.5`, restored with exact counts, restart survival and verified cleanup); explicit `packaging: "7z"` FULL export round trip (the same restored stack exported `7z` with all ten groups within the size-scaled deadline, and that export, made by `0.24.0-beta.5`, restored with exact counts, restart survival and verified cleanup); and the legacy `7z` round trip of a `0.22.2` backup in live read-only mode. Receipts under "Restore evidence" | Pass |  |
| Restored Linux QA perf harnesses | yes | The live read-only restore of the `0.22.2` `7z` backup at `d1e8ea2` passed the app and history harnesses; the egress-blocked `tar.zst` and `7z` restores passed the history harness | Pass |  |
| Snapshot/export/offline artifact gate | yes | Redacted Auto and Force ZIP exports from two restored stacks at `d1e8ea2` rendered all 60 slots in Chromium with network blocked: offline badge shown, 0 external requests, 0 console errors, 0 private IPv4 addresses, 0 host paths. The 2 credential-pattern hits are the app's own sign-in field code | Pass |  |
| Docs/wiki/public-demo gate | yes | At `d1e8ea2`: `check_public_docs.py` passed (26 documents, 197 wiki links, 0 prose patterns); `check_public_screenshots.py` passed; `check_public_demo_artifact.py` found the demo publishable (2,095,850 raw bytes); `validate_release_wrap.py 0.24.0-beta.5 --public-demo-only` passed. Public demo deferred: prerelease. `wiki/` changed in 5 files since `v0.24.0-beta.4` (`Generic-Linux-Setup.md`, `Operations-Logging-and-Metrics.md`, `Profiles-and-Custom-Layouts.md`, `TrueNAS-CORE-Setup.md`, `TrueNAS-SCALE-Setup.md`) | Pass |  |
| Docs/wiki/public-demo publication | yes | Deferred: the owner chose to publish the external wiki with the v0.24.0 final release, not for this prerelease | Blocked |  |
| GHCR publish verification | yes | GitHub prerelease https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.24.0-beta.5 published 2026-11-03, not latest; https://github.com/gcs8/truenas-jbod-ui/actions/runs/41590279473 succeeded at source revision `78c6efee117881a93d971eea65f74b4bc4b0f4e5`. Package API readback confirms `v0.24.0-beta.5`, `0.24.0-beta.5` and `dev` converge on `ghcr.io/gcs8/truenas-jbod-ui@sha256:030b66f331a2bbf02767d5560f0bb26769a951e05b0879edca172d4e2b114f40`. Main CI Preflight https://github.com/gcs8/truenas-jbod-ui/actions/runs/41588569539 succeeded on the same source. GitHub latest remains `v0.23.0` | Pass |  |
| Deployment refresh/sniff tests | yes | Maintainer-reported result for the maintainer's beta test deployment: updated to beta.5, healthy, 0 restarts, retention totals unchanged, no new console errors or 5xx. This records the completed beta update, not full checklist qualification: validated private deployment receipt, exact Compose contract, pre-update rollback digest, running container image IDs and rollback result were not supplied for this follow-up. No fresh runtime readback was performed; production deployment is outside this prerelease's authorization | Blocked |  |
| Post-release reopen | yes | Documentation follow-up branch `docs/beta5-wrap-post-publish` starts from `main` at `78c6efee117881a93d971eea65f74b4bc4b0f4e5` and adds `## Unreleased`; app version stays `0.24.0-beta.5`. The checklist's After Release rule prevents marking reopen Pass until deployment evidence and final validation pass; private deployment qualification and owner wiki publication remain outstanding | Blocked |  |

## Restore evidence

All drills ran on a disposable Linux QA host with
`scripts/run_private_qa_restore.py` at `d1e8ea2`, image
`sha256:e7ac30dd659479bfef0733e068b954feed52da8cd808b9d5adaaa70ff9edcf68`. Archive digests, system
names, counts and timings stay in the restricted QA receipts.

**Default-format round trip (`tar.zst`), egress-blocked.** The beta.3-made
default-format archive restored with exact counts and every saved system.
Inspection reported `tar.zst`, encrypted, schema 2, no absent groups. A
redacted snapshot export rendered offline. A FULL export with no `packaging`
field came out encrypted `tar.zst`, schema 2, with 0 restart failures.
Restoring that export: inspection `tar.zst`, encrypted, export source
`0.24.0-beta.5`, no absent groups; import restored the history database and
every saved system; counts matched exactly; restored rows survived the
restart; the SAS Fabric label pencil cycle cleaned up; offline browser and
history performance checks passed; cleanup was verified. It passed on the
first run.

**Explicit `7z` FULL export round trip, egress-blocked.** The host had more
than 22 GiB free before the export round trips started. From the same
restored stack, the admin API was asked for an encrypted FULL export with
`packaging: "7z"` and all ten backup groups. The admin log and the metrics
gauge both reported the size-scaled create deadline (#1062), far above the
time the `7z` step actually took; there were no timeout log lines. The export
came out encrypted `7z`, schema 2, with 0 restart failures. Restoring that
export: inspection `7z`, encrypted, export source `0.24.0-beta.5`, no absent
groups; import restored the history database and every saved system; counts
matched exactly; restored rows survived the restart; the SAS Fabric label
pencil cycle cleaned up; offline browser and history performance checks
passed; cleanup was verified. It passed on the first run.

**Legacy round trip (`7z`), live read-only.** An encrypted FULL backup from a
`0.22.2` deployment. Inspection reported `7z`, encrypted, schema 2. It was
missing only the runtime-overrides and SAS Fabric alias groups, which that
release did not export. Import restored the history database and every saved
system with 0 restart failures. Counts reconciled, with history growth limited
to new events. Restored rows survived the restart, and the SAS Fabric label and
slot-mapping pencil cycles cleaned up. The live browser, app performance,
history live pass and history performance checks passed. A redacted snapshot
export from this stack also rendered offline.

The QA host's long-running beta deployment was stopped during the run so only
one collector polled the appliances. It came back healthy on the same
container IDs with 0 restarts, and no QA containers were left.

## Fix checks

Run on the owner-admitted disposable CORE VM (TrueNAS 13.0-U6.8) at
`d1e8ea2`, through the candidate's own admin service and HTTP routes, with
strict host-key checking against a pinned key: bootstrap refusal for BMC/IPMI
and ESXi (#1055), the Home directory and missing-account rules and
`user.create` readback (#957, #960, #1025), the SMART sudo allow/deny list in
list mode only (#957), and expired, not-yet-valid and valid loopback TLS
certificates in inspection, trust-remote and the main UI (#965, #1054) all
passed. The task account, its group and both candidate homes were removed; a
fresh session confirmed none remain.

Read-only on a CORE host with SES shelves, in the candidate image
`sha256:e7ac30dd6594` at `d1e8ea2` (API deliberately absent, so the page has
only SSH data): every read succeeded, each shelf kept its own id and profile,
and the independent comparator (its own parsers, no application code)
confirmed pool, vdev and class for every occupied bay.

#1076 and #1078, on the live read-only restored stack in the same image, with
the twin's saved configuration (its CORE appliance uses the default limit):

- The main UI and Admin `/metrics` were read every few seconds through the
  live restore, the UniFi checks and the storage-view checks. For the CORE
  appliance the call budget was 4, split UI 2, Admin 1 (history reads through
  the UI). Each service's `calls_in_flight_peak` stayed at its own limit (UI 2,
  Admin 1), so the app's combined peak was at most 3, under the budget of 4.
  The SCALE appliance showed budget 8, split 4 and 2, with peaks at those
  limits.
- No middleware busy or backoff lines were logged. Admin logged a few
  platform-API read timeouts during the storage-view checks' concurrent
  forced reads; those pages still loaded with 0 console errors and 0 HTTP 5xx.
- Before the first live phase and after the last one, read-only snapshots of
  the CORE appliance's alerts, alert mails and API sessions were taken through
  the twin's saved key. The QA window added no "Failed to check for alert"
  mail and no retained API session from the QA host. Active alerts were the
  same pre-existing ones before and after.

UniFi and other non-TrueNAS systems (#1067, #1069, #1070), on the same stack:

- #1069: the UNVR Front 4 and UNVR Pro Front 7 views were drawn as the real
  chassis at 1920 and 390 pixels wide. Bay numbers ran 1-4 and 1-7, the
  decoration was hidden from assistive technology and not focusable, and every
  bay showed the same disk as on the beta.4 twin (slot, device and serial
  hash).
- #1067: both UniFi systems read "API: not used", "SSH: partial" and name
  UniFi storage as the bay source; the ESXi system reads "ESXi API: not
  used".
- #1070: in the candidate image, the real Linux correlator fed synthetic disks
  carrying every automatic placement hint placed none of them on the four
  layout-only faces and raised the manual-mapping warning; the validated UNVR
  face placed all of them.

## Storage-view checks

In Chromium against a live read-only restore of the production backup, in
image `sha256:e7ac30dd6594` at `d1e8ea2`, with the beta.4 driver unchanged.
Admin checks used that restored stack's own admin service, and its saves
changed only that disposable stack's configuration. #956 (healthy CORE NVMe
carrier and SATADOM slots with `Vdev` and `Health ONLINE`), #959 (generated
SCALE chassis stays unsaved and unpinned; saved views keep their labels; a
deleted Primary Chassis stays deleted), #963 (CORE passive and AOC views
without ESXi RAID IDs, bound by serial; the ESXi AOC template keeps its IDs)
and #966 (view-only templates listed once, layout list excludes them, drive
list wording) all passed. Across the whole run, the main page and the admin
page logged 0 console errors and received 0 HTTP 5xx responses.

## Deviations

- The first candidate (`ba229db`, before #1078) failed the CORE appliance load
  gate: during its storage-view checks the appliance sent two "Failed to check
  for alert" mails, because each service process applied the full call limit
  on its own. Its explicit `7z` export was refused by the product's free-space
  check on a full QA host disk. #1078 fixed the limit on `main`; the QA host
  was cleared of old QA images and exports before this run.
- #1078 changed image inputs, so the release branch took `main` again and
  every gate in this wrap was rerun on `d1e8ea2`.
- The comparator's version check is pinned to the release under test. On this
  run its first two attempts stopped at that check before any appliance read,
  because the launcher pointed at the beta.4 copy of the harness; the complete
  run used the beta.5 copy, whose only change is that version string.
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

## Hold boundary

The tag, GitHub prerelease and GHCR image are published, and the maintainer's
beta test deployment was reported healthy on beta.5. Release closeout is not
complete: the private deployment qualification is not established here, the
external wiki is deferred to the v0.24.0 final release, and the dependent reopen gate
remains blocked. The public demo was not rebuilt or published for this
prerelease, as the checklist requires. This follow-up does not authorize
retagging, moving `latest`, production deployment or wiki publication.
