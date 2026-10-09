# Release wrap: v0.24.0-beta.2

Date: `2026-10-09`

## Scope

v0.24.0-beta.2 is the second prerelease of v0.24.0. It adds the seven pull
requests merged after `v0.24.0-beta.1`: #912, #913, #916, #917, #918, #919 and
#920. GHCR gets the fixed `v0.24.0-beta.2` and `0.24.0-beta.2` tags and moves
`dev` to it; `latest` stays on `v0.23.0`.

Release preparation branch: `release/v0.24.0-beta.2` (PR #922). It is merged
into `main` with a merge commit, never squashed, so the validated commit stays
reachable from the tag, which goes on that `main` merge commit.

Validated release-branch commit: `ff3594edf7ea7dee9fc7cbdfe17444dec27f2a85`.
The only later commit adds this wrap, which does not ship in the image.

Validated Linux QA image: `sha256:5514c634766568998617b8e1ff575ed5ae454bdd1d68789a6a6fbd5fabbe2e9b`,
built on the Linux QA host from that commit with
`org.opencontainers.image.revision` set to it. It reports `0.24.0-beta.2`.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section.

Changelog coverage: pass (7 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `release/v0.24.0-beta.2` at `ff3594edf7ea7dee9fc7cbdfe17444dec27f2a85` is `main` at `e0aae2731b1a00ea2d7fdd9f9b748b6479d25c71` (#912-#920) plus the prerelease version bump; `## v0.24.0-beta.2 - 2026-10-09` covers 7 PRs; `check_release_changelog_coverage.py v0.24.0-beta.1` passed with the external wiki commit `788e8459c68c9406533514459870cd63906aacb2` verified as a branch tip | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `ff3594e`: 4,158 Python tests OK (6 skipped), compileall, bounded Ruff, diff hygiene, test-count manifest, performance baseline and checked public-demo artifact passed. A first run under a long temporary path failed two Unix-socket fixtures with `AF_UNIX path too long`; the rerun with a short `TMPDIR` passed everything. Locally `promtool` is absent, so the alert-rule gate ran in CI only. CI on `ff3594e`: 25 checks passed, 11 routing skips, 0 failed, including both Python versions across all 8 shards | Pass |  |
| JavaScript syntax gates | yes | Same full source gate at `ff3594e`: syntax checks for app, admin, history, SAS Fabric and every `qa/*.spec.js`, plus 1,166 JavaScript unit tests, 0 failed | Pass |  |
| Docker build and health gates | yes | QA image `sha256:5514c634766568998617b8e1ff575ed5ae454bdd1d68789a6a6fbd5fabbe2e9b` built from `ff3594e` with a matching revision label; reports `0.24.0-beta.2`. UI, history and admin `/livez` and `/healthz` passed in the matrix and every restore drill. CI production container, image-only upgrade and hardened upgrade smokes passed on `ff3594e` | Pass |  |
| Optional-sidecar runtime matrix | yes | `scripts/run_compose_runtime_matrix.py` on the Linux QA host at `ff3594e`: 7 variants passed (UI only, UI+history, admin only, UI+admin, UI+history+admin, scheduler disabled, scheduler enabled); 4 alias cycles, 4 mapping cycles, 1 admin setup, 1 scheduler backup; no containers left | Pass |  |
| Full Playwright/browser gates | yes | Live read-only restore at `ff3594e`: 16 passed, 2 skipped (history refresh actions and auto-refresh-after-switch need fixtures the restored data lacks) for `qa/ui-switching.spec.js` and `qa/esxi-smoke.spec.js`; 16 passed, 1 skipped for `qa/private-restore.spec.js` and `qa/admin-operations.spec.js`. CI ran the fixture-only specs, `qa/admin-operations.spec.js` and `qa/public-demo.spec.js` | Pass |  |
| Feature-specific live API/UI gates | yes | Live read-only run at `ff3594e` against every saved system: release sweep across every system and enclosure view, ESXi carrier view, manual refresh with focus restore, saved-chassis and boot-device views, SAS Fabric label and slot-mapping pencil cycles with cleanup, history drawer from enclosure and storage views. Each fix in scope was also checked on the Linux QA host's long-running deployment before release; see "Fix checks" | Pass |  |
| Local release perf harnesses | yes | Against the live restored stack at `ff3594e`, after a completed full history pass: `run_perf_harness.py`, 8 workflows passed (forced and cached inventory, storage views, health, history status, SMART batch, chunked SMART prefetch, snapshot estimate); `run_history_perf_harness.py`, 5 workflows passed including exact counts. `build_perf_baseline.py --check` passed in the source gate | Pass |  |
| Linux QA restore gate | yes | At `ff3594e`: default `tar.zst` round trip (a FULL export with no `packaging` field came out `tar.zst` and restored with exact counts, restart survival and verified cleanup) and legacy `7z` round trip of a `0.22.2` backup both passed. Receipts and deviations under "Restore evidence" | Pass |  |
| Restored Linux QA perf harnesses | yes | Live read-only restore of a `0.22.2` `7z` FULL backup at `ff3594e`: app harness 8/8 and history harness 5/5. Both egress-blocked `tar.zst` restores at `ff3594e` passed the history harness | Pass |  |
| Snapshot/export/offline artifact gate | yes | Redacted Auto and Force ZIP exports from two restored stacks at `ff3594e` (egress-blocked and live read-only) rendered every slot in Chromium with network blocked: offline badge shown, 0 external requests, 0 console errors, 0 private IPv4 addresses, 0 host paths. Details under "Snapshot export evidence" | Pass |  |
| Docs/wiki/public-demo gate | yes | `check_public_docs.py` passed (26 documents, 195 wiki links, 0 prose patterns); `check_public_screenshots.py` passed; `check_public_demo_artifact.py` found the demo publishable (2,095,850 raw bytes); `validate_release_wrap.py v0.24.0-beta.2 --public-demo-only` passed. Public demo deferred: prerelease. `wiki/` changed in 1 file since `v0.24.0-beta.1` (`wiki/Upgrading.md`, #918) | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Recorded after the GitHub prerelease publishes `v0.24.0-beta.2`, `0.24.0-beta.2` and `dev` | Blocked |  |
| Deployment refresh/sniff tests | yes | Recorded after the `dev` image is deployed to a QA host for beta testing | Blocked |  |
| Post-release reopen | yes | Blocked on the deployment gate | Blocked |  |

## Restore evidence

All drills ran on a disposable Linux QA host with
`scripts/run_private_qa_restore.py` at `ff3594e` (image
`sha256:5514c634766568998617b8e1ff575ed5ae454bdd1d68789a6a6fbd5fabbe2e9b`).
Archive digests, system names, counts and timings stay in the restricted QA
receipts on that host.

**Legacy round trip (`7z`), live read-only.** An encrypted FULL backup from a
`0.22.2` deployment: inspection reported `7z`, encrypted, schema 2, with every
group the source had; import restored the history database and every saved
system with 0 restart failures; counts reconciled, with history growth limited
to the same 13 empty-bay corrections v0.23.0 #189 makes that beta.1's run
recorded; restored rows survived the restart; the SAS Fabric label and
slot-mapping pencil cycles cleaned up. The controller confirmed a completed
full history pass with forced inventory before any browser or performance
check. The QA host's long-running beta deployment was stopped during the run
so only one collector polled the appliances, and came back healthy.

**Default-format round trip (`tar.zst`), egress-blocked.** The beta.1-made
default-format archive restored with exact counts, then exported FULL with no
`packaging` field. That export came out encrypted `tar.zst`, schema 2, with 0
restart failures. Restoring it: inspection `tar.zst`, encrypted, export source
`0.24.0-beta.2`, no absent groups; import restored the history database and
every saved system; counts matched exactly before writes, after writes and
after restart; restored rows survived the restart; offline browser and history
performance checks passed; cleanup was verified.

Deviations:

- The default-format source archive came from beta.1, because `0.22.2` cannot
  export `tar.zst`; the round trip's second leg was made by beta.2.
- The legacy `7z` round trip ran in live read-only mode, as for beta.1.
- The QA host's long-running beta deployment uses the same fixed container
  names as the restore controller and the Compose matrix, so it was stopped
  and its containers renamed aside for the drills, then renamed back and
  started on the same container IDs. A first attempt ran the matrix before
  that and stopped at its reserved-name preflight; a second, piped over SSH
  stdin, lost the rest of its script to the matrix's `docker compose`
  commands after the matrix passed. Neither left containers behind. The
  recorded run ran the script from a file on the host.

## Snapshot export evidence

From the egress-blocked and the live read-only restored stacks at `ff3594e`,
with redaction on: the estimate, the Auto export (HTML) and the Force ZIP
export (one HTML member) answered HTTP 200 and reported partial redaction.
Chromium with every network request blocked rendered every slot in each
artifact, showed the offline badge, and logged no console errors.

The privacy scan found no private IPv4 addresses and no host paths. Its 2
credential-pattern hits are the app's own sign-in field code, also present in
v0.23.0. Its long hex values are mapping-revision digests.

## Fix checks

Each fix in scope ran on the Linux QA host's long-running deployment, first as
a combined branch image and then as the `main` image at `e0aae27`, before this
release:

- #917: the upgrade helper's `--inventory-url` disk check upgraded that
  deployment on the first try with 0 unplaced disks, where it had rolled back
  before. A multi-enclosure TrueNAS CORE system reports every disk placed.
- #918: in the live read-only restore at `ff3594e`, the restored `0.22.2`
  production history database (about 4.7 GB) started without the new indexes,
  logged that 5 would be built in the background, and built them one at a
  time in 0.3 to 25.9 seconds each, about 90 seconds in all, before the
  controller's full history pass and the browser and performance checks.
- #916: after a forced refresh of a system's rear enclosure, opening the
  system without choosing an enclosure still lands on its front default.
- #919: no cleared mapping note remained in the slot-detail cache.
- #912, #913, #920: the 2.5" sleds, state icons and Connections list render
  as intended on the affected chassis.

## Known issues carried into the beta

- #921: on a QuantaStor HA cluster, distinct encrypted multipath disks that
  share a `dm-N` alias on one node are counted as one disk by the system-wide
  retention check. Placement is unaffected, but a missing disk among them may
  not be noticed. The upgrade helper does not roll back because of it.
- A system whose boot disk has no storage view (for example a virtual SCALE
  boot disk) reports it as unplaced in the system-wide retention check, so
  `--inventory-url` on that system rolls back until a boot-device view is
  added for it. This is by design: the check counts every disk the system
  reports.
- The ESXi host without StorCLI still shows bays without drive models, and a
  JBOD-only MegaRAID controller logs `No VD's have been configured`. Both
  predate the beta.
- A forced inventory can answer HTTP 503 "busy" with `Retry-After: 1` while a
  background refresh holds the snapshot. The upgrade helper's disk check
  retries it for up to 120 seconds (#917); the browser UI still shows it as a
  failed refresh.
- After a restore or restart, history reports failed passes until the main UI
  has a trusted inventory, and its next full pass can be an hour away.

## Hold boundary

This wrap authorizes the `v0.24.0-beta.2` prerelease and its `dev` image only.
It does not move `latest`, deploy to production, or publish the external wiki.
