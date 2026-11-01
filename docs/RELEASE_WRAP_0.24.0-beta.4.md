# Release wrap: v0.24.0-beta.4

Date: `2026-11-01`

## Scope

v0.24.0-beta.4 is the fourth prerelease of v0.24.0. It is rebuilt from current
`main` and adds the 49 pull requests merged after `v0.24.0-beta.3`. It
supersedes the earlier beta.4 candidate in #967 (`9478b11`/`f9e605f`), which
was 95 commits behind `main` and is not tagged. GHCR gets the fixed
`v0.24.0-beta.4` and `0.24.0-beta.4` tags and moves `dev` to it; `latest`
stays on `v0.23.0`.

Release preparation branch: `release/v0.24.0-beta.4-r2`. It is merged into
`main` with a merge commit, never squashed, so the validated commit stays
reachable from the tag, which goes on that `main` merge commit.

Base: `main` at `401d536b075f0ebb37c2706465ea6d38c3a2d768`.

Validated release-branch commit: `95ba5befb4cc448c5da3c49085216333cde276c2`
(`main` plus the version bump, changelog cut, release notes and the two
release-metadata tests). Later commits change only this wrap.

Validated Linux QA image: linux/amd64 manifest
`sha256:d0a1bab0d81e5b3155778ff8293e83b20b37a34560cb38061cb3c68cebbf4b32`, built
on the Linux QA host from that commit with `org.opencontainers.image.revision`
set to it. It reports `0.24.0-beta.4`. The image was rebuilt for each QA run;
the image index IDs (`sha256:b19988157a86945493aa462e2e16976d4c475b5967fcdb86960380d57a9a6b9f`,
`sha256:9010f7bf42be48d5f857775ab21600a55a8ae38d1b451a27532188cdd7a3f5c5`,
`sha256:acfb5b02549ed8c4f9d6176dd3d566e53f87567dc89e4ba47c69e70f5c9a7d2a`,
`sha256:d21a69f5e9e1b816f6a049a813a5e32015ebca4652b81ab962f0265577cfc3d5`)
all point at that same platform manifest.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section.

Changelog coverage: pass (49 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `release/v0.24.0-beta.4-r2` at `95ba5befb4cc448c5da3c49085216333cde276c2` is `main` at `401d536b075f0ebb37c2706465ea6d38c3a2d768` plus the prerelease version bump; `## v0.24.0-beta.4 - 2026-11-01` covers every PR merged since `v0.24.0-beta.3`; `check_release_changelog_coverage.py v0.24.0-beta.3` passed with the external wiki commit `788e8459c68c9406533514459870cd63906aacb2` verified as a branch tip | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `95ba5be`: 4,719 Python tests OK (6 skipped), compileall, bounded Ruff, diff hygiene, test-count manifest, performance baseline and checked public-demo artifact passed. Locally `promtool` is absent, so the alert-rule gate runs in CI only. Two earlier local runs failed on the workstation's test environment, not the source; see "Deviations" | Pass |  |
| JavaScript syntax gates | yes | Same full source gate at `95ba5be`: syntax checks for app, admin, history, SAS Fabric, the screenshot script and every `qa/*.spec.js`, plus 1,337 JavaScript unit tests (1,332 passed, 5 skipped, 0 failed) | Pass |  |
| Docker build and health gates | yes | QA image built from `95ba5be` with a matching revision label; reports `0.24.0-beta.4`. UI, history and admin `/livez` and `/healthz` passed in every matrix variant and restore drill. The image's new writer-fence entrypoint started every service in each run | Pass |  |
| Optional-sidecar runtime matrix | yes | `scripts/run_compose_runtime_matrix.py` on the Linux QA host at `95ba5be`: 8 variants passed (including the non-root UI variant); 5 alias cycles, 5 mapping cycles, 1 admin setup, 1 scheduler disabled, 1 scheduler backup, 2 journal reads; no containers left | Pass |  |
| Full Playwright/browser gates | yes | Live read-only restore at `95ba5be`: 16 passed, 2 skipped for `qa/ui-switching.spec.js` and `qa/esxi-smoke.spec.js` (the history refresh actions need fixtures the restored data lacks; auto-refresh after a switch needs UI timing); 16 passed, 1 skipped for `qa/private-restore.spec.js` and `qa/admin-operations.spec.js`. Locally at `95ba5be`: the 8 fixture-only specs 274 passed, `qa/admin-operations.spec.js` 15 passed, `qa/public-demo.spec.js` against a current-source demo build 9 passed | Pass |  |
| Feature-specific live API/UI gates | yes | Disposable CORE VM (TrueNAS 13.0-U6.8) through the candidate's admin routes at `95ba5be`: all bootstrap, SMART-grant and TLS checks passed and the task account was removed (see "Fix checks"). Read-only SSH-only collection on a CORE host with SES shelves, in the candidate image: all 22 reads succeeded, but the independent pool-membership comparison did not pass (4 occupied bays not confirmed by its join). Storage views on that host were not exercised | Blocked |  |
| Local release perf harnesses | yes | Against the live restored stack at `95ba5be`, after a completed full history pass: `run_perf_harness.py`, 8 workflows passed (forced and cached inventory, storage views, health, history status, SMART batch, chunked SMART prefetch, snapshot estimate); `run_history_perf_harness.py`, 5 workflows passed including exact counts. `build_perf_baseline.py --check` passed in the source gate | Pass |  |
| Linux QA restore gate | yes | At `95ba5be`: default `tar.zst` round trip (the beta.3-made default archive restored with exact counts; a FULL export with no `packaging` field came out `tar.zst`; that export, made by `0.24.0-beta.4`, restored with exact counts, restart survival and verified cleanup) and the legacy `7z` round trip of a `0.22.2` backup in live read-only mode. Receipts and deviations under "Restore evidence" | Pass |  |
| Restored Linux QA perf harnesses | yes | The live read-only restore of the `0.22.2` `7z` backup at `95ba5be` passed the app and history harnesses; the egress-blocked `tar.zst` restore passed the history harness | Pass |  |
| Snapshot/export/offline artifact gate | yes | Redacted Auto and Force ZIP exports from two restored stacks at `95ba5be` rendered all 60 slots in Chromium with network blocked: offline badge shown, 0 external requests, 0 console errors, 0 private IPv4 addresses, 0 host paths. The 2 credential-pattern hits are the app's own sign-in field code | Pass |  |
| Docs/wiki/public-demo gate | yes | `check_public_docs.py` passed (26 documents, 195 wiki links, 0 prose patterns); `check_public_screenshots.py` passed; `check_public_demo_artifact.py` found the demo publishable (2,095,850 raw bytes); `validate_release_wrap.py 0.24.0-beta.4 --public-demo-only` passed. Public demo deferred: prerelease. `wiki/` changed in 5 files since `v0.24.0-beta.3` (`Admin-UI-and-System-Setup.md`, `SSH-Setup-and-Sudo.md`, `Troubleshooting.md`, `TrueNAS-CORE-Setup.md`, `Upgrading.md`) | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Recorded after the GitHub prerelease publishes `v0.24.0-beta.4`, `0.24.0-beta.4` and `dev` | Blocked |  |
| Deployment refresh/sniff tests | yes | Recorded after the `dev` image is deployed to a QA host for beta testing | Blocked |  |
| Post-release reopen | yes | Blocked on the deployment gate | Blocked |  |

## Restore evidence

All drills ran on a disposable Linux QA host with
`scripts/run_private_qa_restore.py` at `95ba5be`. Archive digests, system
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

## Fix checks

Run on the owner-admitted disposable CORE VM (TrueNAS 13.0-U6.8) at
`95ba5be`, through the candidate's own admin service and HTTP routes, with
strict host-key checking against a pinned key:

- #1055: bootstrap for BMC/IPMI and ESXi was refused before any key lookup or
  connection, and the IPMI sudo preview is disabled.
- #957, #960, #1025: a Home directory outside `/mnt/<pool>/` was refused
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

Read-only on a CORE host with SES shelves, in the candidate image (API
deliberately absent, so the page has only SSH data; #955, #958, #1022):

- All 22 reads succeeded. Bays mapped over SSH, each shelf kept its own id and
  profile, the "turn on SSH" and "no drive bays" warnings were absent, and the
  page named SSH as the source of the bays.
- The independent membership check joins each occupied bay to its multipath
  group and compares pool, vdev and class. 4 occupied bays were not
  confirmed: 2 disks whose own GPT label is not on their multipath provider,
  and 2 disks with no multipath group. A diagnostic join on each disk's own
  labels matched the app for all 4, but that diagnostic shares the app's
  parsers and is not an accepted oracle. This row stays `Blocked` until the
  comparison passes or the owner decides.
- Storage views on that host, and the CORE fixes through the browser, were not
  exercised: the host has no populated saved views (#956, #959, #963, #966).

## Deviations

- The first two local `dev_check.py --full` runs at `95ba5be` failed in the
  workstation's test environment. Run 1: one test that asserts the temp folder
  is unchanged saw a file another process created in the shared temp folder.
  Run 2, with a private temp folder: two tests could not bind a Unix socket
  because that folder's path was too long. Run 3, with a short private temp
  folder, passed. No source changed between the runs.
- The first default-format drill failed at its browser phase: the QA host did
  not have the Chromium build that the Playwright release pinned on `main`
  needs, so no browser launched. After installing it, the second and third
  runs passed the restore, snapshot and offline checks, but the export
  free-space check refused their FULL export because the QA host disk was
  short of space. That refusal is the intended behavior. Unused QA and
  pull-request images and the build cache were removed from the QA host, and
  the fourth run passed every phase. The recorded results are from that run.
- As for earlier betas, the QA host's long-running beta deployment uses the
  same fixed container names as the restore controller and the Compose
  matrix. It was stopped and its containers renamed aside for each run, then
  renamed back and started on the same container IDs.

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
