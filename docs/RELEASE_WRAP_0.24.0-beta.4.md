# Release wrap: v0.24.0-beta.4

Date: `2026-10-12`

## Scope

v0.24.0-beta.4 is the fourth prerelease of v0.24.0. It adds the nine pull
requests merged after `v0.24.0-beta.3`: #955, #956, #957, #958, #959, #960,
#963, #965 and #966. GHCR gets the fixed `v0.24.0-beta.4` and `0.24.0-beta.4`
tags and moves `dev` to it; `latest` stays on `v0.23.0`.

Release preparation branch: `release/v0.24.0-beta.4` (PR #967). It is merged
into `main` with a merge commit, never squashed, so the validated commit stays
reachable from the tag, which goes on that `main` merge commit.

Validated release-branch commit: `9478b115ab95d8eddb18ccd3a930a71ed16b5b16`.
The only later commit adds this wrap.

Validated Linux QA image: `sha256:bcb5a8fb7d8276471d7b2d2fda12a6de0677d7cecd87133008bc754488cd27b7`, built on the Linux QA host
from that commit with `org.opencontainers.image.revision` set to it. It reports
`0.24.0-beta.4`.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section.

Changelog coverage: pass (13 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `release/v0.24.0-beta.4` at `9478b115ab95d8eddb18ccd3a930a71ed16b5b16` is `main` at `f51b0f0a92a4f0a13cbc966264d9b2d2e2b1869f` (#955-#966) plus the prerelease version bump; `## v0.24.0-beta.4 - 2026-10-12` covers 13 numbers (9 PRs and the 4 issues their commits name); `check_release_changelog_coverage.py v0.24.0-beta.3` passed with the external wiki commit `788e8459c68c9406533514459870cd63906aacb2` verified as a branch tip | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `9478b11`: `FINAL: PASS`; 4,339 Python tests OK (6 skipped), compileall, bounded Ruff, diff hygiene, test-count manifest, performance baseline and checked public-demo artifact passed. Locally `promtool` is absent, so the alert-rule gate ran in CI only. CI on the PR head: 43 checks passed, 1 routing skip, 0 failed, including both Python versions across all 8 shards | Pass |  |
| JavaScript syntax gates | yes | Same full source gate at `9478b11`: syntax checks for app, admin, history, SAS Fabric and every `qa/*.spec.js`, plus 1,268 JavaScript unit tests: 1,263 passed, 0 failed, 5 skipped. CI JavaScript syntax and npm lock passed | Pass |  |
| Docker build and health gates | yes | QA image built from `9478b11` with a matching revision label; reports `0.24.0-beta.4`. UI, history and admin `/livez` and `/healthz` passed in the matrix and every restore drill. CI production container, image-only upgrade and hardened upgrade smokes passed | Pass |  |
| Optional-sidecar runtime matrix | yes | `scripts/run_compose_runtime_matrix.py` on the Linux QA host at `9478b11`: 7 variants passed; 4 alias cycles, 4 mapping cycles, 1 admin setup, 1 scheduler disabled, 1 scheduler backup; no containers left | Pass |  |
| Full Playwright/browser gates | yes | Live read-only restore at `9478b11`: 16 passed, 2 skipped for the live browser suite, including the release sweep and manual refresh with focus restore. The 2 skips are history refresh actions, which need fixtures the restored data lacks, and auto-refresh after a switch, which needs UI timing. Offline and admin suite: 16 passed, 1 skipped. CI admin clean-room browser QA passed | Pass |  |
| Feature-specific live API/UI gates | yes | Live read-only run at `9478b11` against every saved system: release sweep across every system and enclosure view, manual refresh with focus restore on a two-shelf system, SAS Fabric label and slot-mapping pencil cycles with cleanup, history drawer from enclosure and storage views. The CORE-over-SSH fixes in scope need a CORE host read over SSH; see "Fix checks" | Pass |  |
| Local release perf harnesses | yes | Against the live restored stack at `9478b11`, after a completed full history pass: `run_perf_harness.py`, 8 workflows passed (forced and cached inventory, storage views, health, history status, SMART batch, chunked SMART prefetch, snapshot estimate); `run_history_perf_harness.py`, 5 workflows passed including exact counts. `build_perf_baseline.py --check` passed in the source gate | Pass |  |
| Linux QA restore gate | yes | At `9478b11`: default `tar.zst` round trip (the beta.3-made default archive restored with exact counts; a FULL export with no `packaging` field came out `tar.zst`; that export restored with exact counts, restart survival and verified cleanup) and the legacy `7z` round trip of a `0.22.2` backup in live read-only mode. Receipts and deviations under "Restore evidence" | Pass |  |
| Restored Linux QA perf harnesses | yes | Both egress-blocked `tar.zst` restores at `9478b11` passed the history harness; the live read-only restore of the `0.22.2` `7z` backup passed the app and history harnesses | Pass |  |
| Snapshot/export/offline artifact gate | yes | Redacted Auto and Force ZIP exports from two restored stacks at `9478b11` rendered all 60 slots in Chromium with network blocked: offline badge shown, 0 external requests, 0 console errors, 0 private IPv4 addresses, 0 host paths. The 2 credential-pattern hits are the app's own sign-in field code | Pass |  |
| Docs/wiki/public-demo gate | yes | `check_public_docs.py` passed (26 documents, 195 wiki links, 0 prose patterns); `check_public_screenshots.py` passed; `check_public_demo_artifact.py` found the demo publishable (2,095,850 raw bytes); `validate_release_wrap.py v0.24.0-beta.4 --public-demo-only` passed. Public demo deferred: prerelease. `wiki/` changed in 3 files since `v0.24.0-beta.3` (`Admin-UI-and-System-Setup.md`, `SSH-Setup-and-Sudo.md`, `TrueNAS-CORE-Setup.md`) | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Recorded after the GitHub prerelease publishes `v0.24.0-beta.4`, `0.24.0-beta.4` and `dev` | Blocked |  |
| Deployment refresh/sniff tests | yes | Recorded after the `dev` image is deployed to a QA host for beta testing | Blocked |  |
| Post-release reopen | yes | Blocked on the deployment gate | Blocked |  |

## Restore evidence

All drills ran on a disposable Linux QA host with
`scripts/run_private_qa_restore.py` at `9478b11`. Archive digests, system
names, counts and timings stay in the restricted QA receipts on that host.

**Default-format round trip (`tar.zst`), egress-blocked.** The beta.3-made
default-format archive restored with exact counts and 8 systems. A redacted
snapshot export rendered offline. A FULL export with no `packaging` field came
out encrypted `tar.zst`, schema 2, with 0 restart failures. Restoring that
export: inspection `tar.zst`, encrypted, export source `0.24.0-beta.4`, no
absent groups; import restored the history database and every saved system;
counts matched exactly; restored rows survived the restart; offline browser and
history performance checks passed; cleanup was verified.

**Legacy round trip (`7z`), live read-only.** An encrypted FULL backup from a
`0.22.2` deployment. Inspection reported `7z`, encrypted, schema 2. It was
missing only the runtime-overrides and SAS Fabric alias groups, which that
release did not export. Import restored the history database and every saved
system with 0 restart failures. Counts reconciled, with history growth limited
to 13 events. Restored rows survived the restart, and the SAS Fabric label and
slot-mapping pencil cycles cleaned up. The live browser, app performance,
history live pass and history performance checks passed. The QA host's
long-running beta deployment was stopped during the run so only one collector
polled the appliances. It came back healthy with 0 restarts.

Deviations:

- The two drills that keep their stack running for snapshot export (the first
  `tar.zst` restore and the live read-only restore) report cleanup as not
  verified in their receipts, because the controller leaves the stack up by
  request. The QA script then took each stack down and confirmed 0 containers,
  networks and volumes left for the project and the runtime root removed.
- As for beta.3, the QA host's long-running beta deployment uses the same fixed
  container names as the restore controller and the Compose matrix. It was
  stopped and its containers renamed aside for the drills, then renamed back
  and started on the same container IDs.
- The live read-only drill logged 3 SSH channel-open refusals from appliances
  during its refresh passes (beta.3's drill logged 9). Every refresh, browser
  and performance check that depended on them passed.

## Fix checks

The fixes in scope are covered by Python and JavaScript unit tests that failed
before each fix. Their live checks need a TrueNAS CORE host read over SSH, which the
Linux QA host's restored systems do not include. They are scheduled on the
beta test host that onboards a CORE system after the image deploys:

- #955, #958: separate backplanes and AHCI SGPIO ports draw as their own
  shelves, pool names resolve without the API, and the page names the real
  source of its bays.
- #957, #960: bootstrap creates a missing CORE account through `user.create`
  with a home under `/mnt`, refuses one with no Home directory, and stores
  SMART grants that `sudo -n -l` still allows after the middleware saves them.
- #965: an expired CORE certificate is refused for pinning, and the main page
  names the failed certificate check.
- #956, #959, #963, #966: storage-view health, the generated Primary Chassis,
  the M.2 card templates and the admin view picker on the same host.

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

## Hold boundary

This wrap authorizes the `v0.24.0-beta.4` prerelease and its `dev` image only.
It does not move `latest`, deploy to production, or publish the external wiki.
