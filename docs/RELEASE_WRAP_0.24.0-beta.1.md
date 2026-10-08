# Release wrap: v0.24.0-beta.1

Date: `2026-10-08`

## Scope

v0.24.0-beta.1 is a prerelease of v0.24.0. It contains every pull request
merged after `v0.23.0`, through #908, and publishes the `dev` image tag only.
`latest` stays on `v0.23.0`.

Release preparation branch: `release/v0.24.0-beta.1` (PR #897), merged into
`main` with a merge commit. The tag goes on that `main` merge commit.

Validated release-branch commit: `c70d87770b30dcf8e9791826217c13802904744f`.
Later commits add this wrap and loosen one changelog test; neither ships in
the image.

Validated Linux QA image: `sha256:cd7347963ff7c2270c79b32b0ff4244d5b1b0d52de1b7cdfb3feda99aec9e926`,
built on the Linux QA host from that commit with
`org.opencontainers.image.revision` set to it.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section.

Changelog coverage: pass (219 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `release/v0.24.0-beta.1` at `c70d87770b30dcf8e9791826217c13802904744f` contains `main` at `a161782147c306aea5def97403840acfc98f31ce` (#902-#908) and the prerelease version bump; `## v0.24.0-beta.1 - 2026-10-08` covers 219 PRs | Pass |  |
| Python unit and syntax gates | yes | `python scripts/dev_check.py --full` at `c70d877`: 4,112 Python tests OK (6 skipped), compileall, bounded Ruff, diff hygiene, test-count manifest, performance baseline and checked public-demo artifact passed. Locally `promtool` is absent, so the alert-rule gate ran in CI only. CI on `c70d877`: 24 checks passed, 11 routing skips, 0 failed | Pass |  |
| JavaScript syntax gates | yes | Same full source gate: syntax checks for app, admin, history, SAS Fabric and every `qa/*.spec.js`, plus 1,161 JavaScript unit tests, 0 failed | Pass |  |
| Docker build and health gates | yes | QA image `sha256:cd7347963ff7c2270c79b32b0ff4244d5b1b0d52de1b7cdfb3feda99aec9e926` built from `c70d877` with a matching revision label; reports `0.24.0-beta.1`. UI, history and admin `/livez` and `/healthz` passed in the matrix and both restore drills. CI production container, image-only upgrade and hardened upgrade smokes passed | Pass |  |
| Optional-sidecar runtime matrix | yes | `scripts/run_compose_runtime_matrix.py` on the Linux QA host against an image with identical runtime inputs (`0d204c8`; the later commits change only QA scripts, tests and docs): 7 variants passed (UI only, UI+history, admin only, UI+admin, UI+history+admin, scheduler disabled, scheduler enabled); 4 alias cycles, 4 mapping cycles, 1 admin setup, 1 scheduler backup; no containers left | Pass |  |
| Full Playwright/browser gates | yes | Live read-only restores at `4bfba4e` and `c70d877`, each: 16 passed, 2 skipped (history refresh actions and auto-refresh-after-switch need fixtures the restored data lacks) for `qa/ui-switching.spec.js` and `qa/esxi-smoke.spec.js`; 16 passed, 1 skipped for `qa/private-restore.spec.js` and `qa/admin-operations.spec.js`. CI ran the fixture-only specs, `qa/admin-operations.spec.js` and `qa/public-demo.spec.js` | Pass |  |
| Feature-specific live API/UI gates | yes | Live read-only runs against all 8 saved systems: release sweep across every system and enclosure view, ESXi carrier view with StorCLI missing on the host, manual refresh with focus restore, saved-chassis and boot-device views, SAS Fabric label and slot-mapping pencil cycles with cleanup, history drawer from enclosure and storage views | Pass |  |
| Local release perf harnesses | yes | Against the live restored stack at `4bfba4e`: `run_perf_harness.py`, 8 workflows passed (forced and cached inventory, storage views, health, history status, SMART batch, chunked SMART prefetch, snapshot estimate); `run_history_perf_harness.py`, 5 workflows passed including exact counts. `build_perf_baseline.py --check` passed in the source gate at `c70d877` | Pass |  |
| Linux QA restore gate | yes | Default `tar.zst` round trip at `c70d877`: a FULL export with no `packaging` field came out `tar.zst` and restored with exact counts, restart survival and verified cleanup. Legacy `7z` round trip of the production backup (export source `0.22.2`) passed at `4bfba4e`, whose image inputs match `c70d877`. Receipts and deviations under "Restore evidence" | Pass |  |
| Restored Linux QA perf harnesses | yes | Restores of the production `7z` FULL backup (export source app `0.22.2`): app harness 8/8 at `4bfba4e` after the collector finished a live pass; history harness 5/5 at `4bfba4e` and at `c70d877`, where it now also waits for that pass. Both egress-blocked `tar.zst` restores at `c70d877` passed the history harness | Pass |  |
| Snapshot/export/offline artifact gate | yes | Redacted Auto and Force ZIP exports from the live restored stack rendered 60 slots each in Chromium with network blocked: offline badge shown, 0 external requests, 0 console errors, 0 private IPv4 addresses, 0 host paths. Details under "Snapshot export evidence" | Pass |  |
| Docs/wiki/public-demo gate | yes | `check_public_docs.py` passed (26 documents, 195 wiki links, 0 prose patterns); `check_public_screenshots.py` passed; `check_public_demo_artifact.py` found the demo publishable (2,095,850 raw bytes); `validate_release_wrap.py v0.24.0-beta.1 --public-demo-only` passed. Public demo deferred: prerelease. `wiki/` changed in 23 files since `v0.23.0` | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Recorded after the GitHub prerelease publishes `v0.24.0-beta.1`, `0.24.0-beta.1` and `dev` | Blocked |  |
| Deployment refresh/sniff tests | yes | Recorded after the `dev` image is deployed to the Linux QA host for beta testing. Production stays on v0.22.2 for this prerelease | Blocked |  |
| Post-release reopen | yes | Blocked on the deployment gate | Blocked |  |

## Restore evidence

All drills ran on the Linux QA host with `scripts/run_private_qa_restore.py`.
Sanitized receipts stay on that host; raw payloads were not copied out.

**Default-format round trip (`tar.zst`).** Production runs v0.22.2, whose FULL
default is `7z`, so the beta made the default-format archive: the production
`7z` was restored into an egress-blocked beta stack, then exported FULL with no
`packaging` field. The export came out encrypted `tar.zst`, schema 2,
345,661,061 bytes, SHA-256
`a93d2b441d7521e3e5e63ad5dd49dc184cbd004cab4162e3c9ba0fb556b54068`; 2
services stopped and restarted with 0 restart failures. Restoring it at
`c70d877` (image `sha256:cd7347963ff7c2270c79b32b0ff4244d5b1b0d52de1b7cdfb3feda99aec9e926`): inspection reported `tar.zst`,
encrypted, export-source app `0.24.0-beta.1`, no absent groups; import
restored the history database and 8 systems; aggregate counts matched exactly
before writes, after writes and after restart; restored rows survived the
restart; offline browser and history performance checks passed; cleanup was
verified. The earlier beta-made archive (SHA-256 `efb83ad50a83cfd13350f8464dbbba3ecb0830b95eba4bbdba91b63c98ce3439`) also restored
at `c70d877` with exact counts.

**Legacy round trip (`7z`).** The encrypted production FULL backup (export
source app `0.22.2`, SHA-256 `79bc19e037719add0f8bf6887151875b30a6cc28946404c323f90a7b19ccdc71`)
passed a live read-only restore at `4bfba4e` (image `sha256:3ea8f1fe8c8929162cf438995939346ab2b4765159512f6c5ef24d26bddb02a5`):
inspection `7z`, encrypted, schema 2, 8 groups present (the source has no
runtime overrides or SAS Fabric aliases); import restored history and 8
systems; counts reconciled with 13 new history events, the 13 "Not Installed"
bays the v0.23.0 #189 fix reports as empty; restored rows survived the
restart; the SAS Fabric label and slot-mapping pencil cycles cleaned up.

Production was stopped during each live run so only one collector polled the
appliances: 04:03-04:23, 05:18-05:51 and 06:35-06:57 UTC on 2026-10-08. It came
back healthy on v0.22.2 each time.

Deviations:

- The default-format archive came from the beta, not from the long-running
  deployment, because that deployment cannot export `tar.zst` yet.
- The passing `7z` receipt is from `4bfba4e`. `c70d877` changes only the QA
  controller, its tests and docs, none of which ship in the image. At `c70d877`
  the egress-blocked `7z` drill failed its post-restart exact count (tracked
  history slots 326 to 336). The 10 extra slots match the synthetic demo
  system's 10 empty storage-view bays: that system needs no network, and the
  beta records empty storage views since 87ec6f6. A second live `7z` run at
  `c70d877` passed restore, counts and both
  browser suites, then failed when the app performance harness got one HTTP
  503 "busy" answer to a forced inventory. Both are QA-harness issues listed
  below.

## Snapshot export evidence

From the live restored stack at `4bfba4e`, with redaction on: the estimate,
the Auto export and the Force ZIP export answered HTTP 200. The ZIP was
151,748 bytes with one HTML member and reported partial redaction. Chromium
with every network request blocked rendered 60 slots in each artifact, showed
the offline badge, and logged no console errors.

The privacy scan found no private IPv4 addresses and no host paths. Its 2
credential-pattern hits are the app's own sign-in field code, also present in
v0.23.0. The 121 long hex values are 120 mapping-revision digests and one QA
nonce.

## Known issues carried into the beta

- The ESXi host without StorCLI still shows bays without drive models. The app
  says StorCLI is missing; installing Broadcom's ESXi 8 StorCLI package and
  rebooting the host fixes it. This predates the beta.
- A JBOD-only MegaRAID controller logs `No VD's have been configured` as an
  invalid controller block. Drive data is unaffected. This predates the beta.
- A cleared manual mapping's note can come back on its bay from the slot-detail
  cache. The QA drill's own transient note showed up this way in a later
  export. This predates the beta.
- A forced inventory can answer HTTP 503 "busy, retry in 1 s" while a
  background refresh holds the snapshot. The app performance harness does not
  retry, so one such answer fails a run.
- An egress-blocked restore of a pre-beta backup can gain tracked history slots
  once, from a synthetic system that needs no network, which breaks that
  drill's exact-count check depending on collector timing.
- After a restore or restart, history reports failed passes until the main UI
  has a trusted inventory. On slow appliances the first full pass can take
  several minutes.

## Hold boundary

This wrap authorizes the `v0.24.0-beta.1` prerelease and its `dev` image only.
It does not move `latest`, deploy to production, or publish the external wiki.
