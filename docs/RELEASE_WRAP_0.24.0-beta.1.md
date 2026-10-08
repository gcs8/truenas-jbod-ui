# Release wrap: v0.24.0-beta.1

Date: `2026-10-08`

## Scope

v0.24.0-beta.1 is a prerelease of v0.24.0. It contains every pull request
merged after `v0.23.0`, through #908, and publishes the `dev` image tag only.
`latest` stays on `v0.23.0`.

Release preparation branch: `release/v0.24.0-beta.1` (PR #897), merged into
`main` with a merge commit. The tag goes on that `main` merge commit.

Validated release-branch commit: `b0656fbe175d2963f6bffe1763793d36d132254d`.
Later commits change only this wrap, the restore guide and a code comment in
the QA restore controller; none of them ship in the image.

Validated Linux QA image: `sha256:c6702aeb699808fb2d847628d92df51f3c8f2f0354ec2af30e995022936109ae`,
built on the Linux QA host from that commit with
`org.opencontainers.image.revision` set to it.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section.

Changelog coverage: pass (219 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `release/v0.24.0-beta.1` at `b0656fbe175d2963f6bffe1763793d36d132254d` contains `main` at `a161782147c306aea5def97403840acfc98f31ce` (#902-#908) and the prerelease version bump; `## v0.24.0-beta.1 - 2026-10-08` covers 219 PRs | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `b0656fb`: 4,114 Python tests OK (6 skipped), compileall, bounded Ruff, diff hygiene, test-count manifest, performance baseline and checked public-demo artifact passed. Locally `promtool` is absent, so the alert-rule gate ran in CI only. CI on `b0656fb`: 24 checks passed, 11 routing skips, 0 failed | Pass |  |
| JavaScript syntax gates | yes | Same full source gate: syntax checks for app, admin, history, SAS Fabric and every `qa/*.spec.js`, plus 1,161 JavaScript unit tests, 0 failed | Pass |  |
| Docker build and health gates | yes | QA image `sha256:c6702aeb699808fb2d847628d92df51f3c8f2f0354ec2af30e995022936109ae` built from `b0656fb` with a matching revision label; reports `0.24.0-beta.1`. UI, history and admin `/livez` and `/healthz` passed in the matrix and every restore drill. CI production container, image-only upgrade and hardened upgrade smokes passed | Pass |  |
| Optional-sidecar runtime matrix | yes | `scripts/run_compose_runtime_matrix.py` on the Linux QA host at `0d204c8`; the image inputs, the matrix script, `docker-compose.yml` and the config fixture are identical at `b0656fb`. 7 variants passed (UI only, UI+history, admin only, UI+admin, UI+history+admin, scheduler disabled, scheduler enabled); 4 alias cycles, 4 mapping cycles, 1 admin setup, 1 scheduler backup; no containers left | Pass |  |
| Full Playwright/browser gates | yes | Live read-only restore at `b0656fb`: 16 passed, 2 skipped (history refresh actions and auto-refresh-after-switch need fixtures the restored data lacks) for `qa/ui-switching.spec.js` and `qa/esxi-smoke.spec.js`; 16 passed, 1 skipped for `qa/private-restore.spec.js` and `qa/admin-operations.spec.js`. CI ran the fixture-only specs, `qa/admin-operations.spec.js` and `qa/public-demo.spec.js` | Pass |  |
| Feature-specific live API/UI gates | yes | Live read-only run at `b0656fb` against all 8 saved systems: release sweep across every system and enclosure view, ESXi carrier view with StorCLI missing on the host, manual refresh with focus restore, saved-chassis and boot-device views, SAS Fabric label and slot-mapping pencil cycles with cleanup, history drawer from enclosure and storage views | Pass |  |
| Local release perf harnesses | yes | Against the live restored stack at `b0656fb`, after a completed full history pass: `run_perf_harness.py`, 8 workflows passed (forced and cached inventory, storage views, health, history status, SMART batch, chunked SMART prefetch, snapshot estimate); `run_history_perf_harness.py`, 5 workflows passed including exact counts. `build_perf_baseline.py --check` passed in the source gate | Pass |  |
| Linux QA restore gate | yes | At `b0656fb`: default `tar.zst` round trip (a FULL export with no `packaging` field came out `tar.zst` and restored with exact counts, restart survival and verified cleanup) and legacy `7z` round trip of the production backup (export source `0.22.2`) both passed. Receipts and deviations under "Restore evidence" | Pass |  |
| Restored Linux QA perf harnesses | yes | Live read-only restore of the production `7z` FULL backup at `b0656fb`: app harness 8/8 and history harness 5/5. Both egress-blocked `tar.zst` restores at `b0656fb` passed the history harness | Pass |  |
| Snapshot/export/offline artifact gate | yes | Redacted Auto and Force ZIP exports from a restored stack at `b0656fb` rendered 60 slots each in Chromium with network blocked: offline badge shown, 0 external requests, 0 console errors, 0 private IPv4 addresses, 0 host paths. Details under "Snapshot export evidence" | Pass |  |
| Docs/wiki/public-demo gate | yes | `check_public_docs.py` passed (26 documents, 195 wiki links, 0 prose patterns); `check_public_screenshots.py` passed; `check_public_demo_artifact.py` found the demo publishable (2,095,850 raw bytes); `validate_release_wrap.py v0.24.0-beta.1 --public-demo-only` passed. Public demo deferred: prerelease. `wiki/` changed in 23 files since `v0.23.0` | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Recorded after the GitHub prerelease publishes `v0.24.0-beta.1`, `0.24.0-beta.1` and `dev` | Blocked |  |
| Deployment refresh/sniff tests | yes | Recorded after the `dev` image is deployed to the Linux QA host for beta testing. Production stays on v0.22.2 for this prerelease | Blocked |  |
| Post-release reopen | yes | Blocked on the deployment gate | Blocked |  |

## Restore evidence

All drills ran on the Linux QA host with `scripts/run_private_qa_restore.py`
at `b0656fb` (image `sha256:c6702aeb699808fb2d847628d92df51f3c8f2f0354ec2af30e995022936109ae`).
Sanitized receipts stay on that host; raw payloads were not copied out.

**Legacy round trip (`7z`), live read-only.** The encrypted production FULL
backup (export source app `0.22.2`, SHA-256
`79bc19e037719add0f8bf6887151875b30a6cc28946404c323f90a7b19ccdc71`):
inspection `7z`, encrypted, schema 2, 8 groups present (the source has no
runtime overrides or SAS Fabric aliases); import restored the history
database and 8 systems with 0 restart failures; counts reconciled with 13 new
history events, the 13 "Not Installed" bays the v0.23.0 #189 fix reports as
empty; restored rows survived the restart; the SAS Fabric label and
slot-mapping pencil cycles cleaned up. The controller requested a full
history pass and waited for it (it completed with forced inventory) before
any browser or performance check. Production was stopped from 09:02 to 09:28
UTC so only one collector polled the appliances, and came back healthy on
v0.22.2.

**Default-format round trip (`tar.zst`), egress-blocked.** Production runs
v0.22.2, whose FULL default is `7z`, so the beta made the default-format
archive. An earlier beta-made `tar.zst` (SHA-256
`efb83ad50a83cfd13350f8464dbbba3ecb0830b95eba4bbdba91b63c98ce3439`, export
source `0.24.0-beta.1`) restored with exact counts and was exported FULL with
no `packaging` field. That export came out encrypted `tar.zst`, schema 2,
345,661,026 bytes, SHA-256
`d99f0a014f951de48f30799c2059af6bc7e72fc5a373e88080f9beaac0d21c12`; 2
services stopped and restarted with 0 restart failures. Restoring it:
inspection `tar.zst`, encrypted, export source `0.24.0-beta.1`, no absent
groups; import restored the history database and 8 systems; counts matched
exactly before writes, after writes and after restart; restored rows survived
the restart; offline browser and history performance checks passed; cleanup
was verified.

Deviations:

- The default-format archive came from the beta, not from the long-running
  deployment, because that deployment cannot export `tar.zst` yet.
- The legacy `7z` round trip ran in live read-only mode. In egress-blocked
  mode its post-restart exact count can fail (tracked history slots 326 to
  336, seen at `c70d877`): the synthetic demo system needs no network, and
  since 87ec6f6 the beta records its 10 empty storage-view bays. See known
  issues.
- Earlier live runs on this branch (04:03-04:23, 05:18-05:51, 06:35-06:57 and
  08:35-08:57 UTC) found the QA-harness problems fixed here: specs racing a
  slow live refresh, an ESXi check that assumed StorCLI, a collector still
  owing its first full pass, and a perf client that did not honour the app's
  503 `Retry-After`.

## Snapshot export evidence

From the egress-blocked restored stack at `b0656fb`, with redaction on: the
estimate, the Auto export (HTML, 880,780 bytes) and the Force ZIP export
(132,441 bytes, one HTML member) answered HTTP 200 and reported partial
redaction. Chromium with every network request blocked rendered 60 slots in
each artifact, showed the offline badge, and logged no console errors.

The privacy scan found no private IPv4 addresses and no host paths. Its 2
credential-pattern hits are the app's own sign-in field code, also present in
v0.23.0. The 120 long hex values are mapping-revision digests.

## Known issues carried into the beta

- The ESXi host without StorCLI still shows bays without drive models. The app
  says StorCLI is missing; installing Broadcom's ESXi 8 StorCLI package and
  rebooting the host fixes it. This predates the beta.
- A JBOD-only MegaRAID controller logs `No VD's have been configured` as an
  invalid controller block. Drive data is unaffected. This predates the beta.
- A cleared manual mapping's note can come back on its bay from the slot-detail
  cache. A QA drill's transient note showed up this way in a later export.
  This predates the beta.
- A forced inventory can answer HTTP 503 "busy" with `Retry-After: 1` while a
  background refresh holds the snapshot. The browser UI shows it as a failed
  refresh; the perf harness now retries up to three times.
- An egress-blocked restore of a pre-beta backup can gain tracked history slots
  once, from a synthetic system that needs no network, which breaks that
  drill's exact-count check depending on collector timing.
- After a restore or restart, history reports failed passes until the main UI
  has a trusted inventory, and its next full pass can be an hour away. On slow
  appliances a full pass takes several minutes.

## Hold boundary

This wrap authorizes the `v0.24.0-beta.1` prerelease and its `dev` image only.
It does not move `latest`, deploy to production, or publish the external wiki.
