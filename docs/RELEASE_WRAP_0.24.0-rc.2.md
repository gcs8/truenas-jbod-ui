# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `b232fbd47c75eb1d6f083ddddf19a83dcb90504f`.
Release cut: `8c9ace94ec82638cc89d516841d9ba161daf3428`.
Qualification outcome: **Blocked**, not release-ready.

This additive recut includes the Topology accessibility correction #1464 and
performance-observer readiness correction #1462. All 85 collected fragments and
every historical release section were preserved. No release history was rewritten.
Release notes describe changes since rc.1 and summarize the v0.24.0 line.

Changelog coverage: pass (127 PRs)

The only shipped change from `b3dc216ab4cd93d361cf5b52124e224af76969fd` is
`app/static/sas_fabric_topology.js`. Per-path objects and SHA-256 values, plus
all 166 copied image files, were checked. All other shipped bytes and all modes,
owners and symlink targets match. Backend-only carries below do not load that
browser script and retain their original executed source and image identities.
Browser evidence is fresh, not carried.

Fresh local QA image:
`sha256:b0e87af0cc0c1f107341c6b1f5ecb5c772ef72d0a71bc1d551006e94f38dcedd`.
Its OCI source revision is the release-cut source above, and its application
version is `0.24.0-rc.2`. It is not a registry publication or deployment.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main merged additively; 85 fragments and historical sections preserved. Coverage passes for 127 PRs. Actual publication resolver emits the fixed version aliases and dev only; 22 resolver contract tests pass. | Pass | |
| Python unit and syntax gates | yes | Native dev_check --full on the clean release cut: 7,044 tests run, seven skipped, no failures; compile and source gates pass, final PASS. Start/end commit and tree match. Exact-cut CI supplies the local Prometheus-tool skip. | Pass | |
| JavaScript syntax gates | yes | Complete native syntax checks and 2,088 unit tests total: 2,083 passed, five skipped, zero failed, cancelled or todo. | Pass | |
| Docker build and health gates | yes | Fresh exact-cut image, version/revision and copied-file proof pass. Disposable restore services reached healthy runtime and authenticated browser acceptance. Original test installation recovered on the same container IDs/image, healthy with zero restarts; task resources absent. Not deployment acceptance. | Pass | |
| Optional-sidecar runtime matrix | yes | Carried from 6150cce10e1e9af4300efcf2a786235266a14246: all eight declared native variants, complete success marker and original receipt hashes verified. Backend and controlling inputs unchanged; the sole shipped Topology browser-script delta is not loaded by this gate. | Pass | |
| Full Playwright/browser gates | yes | Fresh complete 16-file fixture/Admin/generated-demo run: 594 passed, zero skipped/failed. Legacy restored browser: offline 16 passed/one explicit skip; live 16 passed/two explicit skips. Full 42-chunk live crawl and live human-view not started because the subsequent native app-performance command failed. Synthetic Topology controls are preparation only, not live visual acceptance. | Blocked | |
| Feature-specific live API/UI gates | yes | CORE 11/11 and independent read-only comparator 51/51 carried with original receipts and backend/input proof; the sole Topology script delta is not loaded. Fresh held-runtime feature and census checks not started after legacy performance failure. Bounded observation closed with no new alert-check mail or retained-session increase. | Blocked | |
| Local release perf harnesses | yes | Complete prior local app/history pair carried from b3dc216ab4cd93d361cf5b52124e224af76969fd: exact labels, three iterations each, CSV trails and all mapping-import cycles. Original receipts verified. Same restored disposable stack supplied both local and restored labels. Backend unchanged; Topology browser script not loaded. The later observer correction does not imply fresh execution of this explicitly carried pair. | Pass | |
| Linux QA restore gate | yes | Fresh default tar.zst and explicit 7z FULL round trips and both offline drills carried from 6150cce10e1e9af4300efcf2a786235266a14246 with original complete receipts and backend/controller-input proof. The fresh legacy live drill failed in browser-and-performance: run_perf_harness.py forced GET /api/inventory returned HTTP 503. Full drill aggregate/restart/pencil acceptance is not inferred from earlier phases. Cleanup verified. | Blocked | |
| Restored Linux QA perf harnesses | yes | Full restored three-iteration app/history pair not started. Private caller prepared explicit 330-second readiness and failure capture, but the earlier legacy single-iteration app command failed on a forced inventory read. No retry or exception granted. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Fresh restored Auto HTML and explicit ZIP export/browser checks not started after the legacy failure. Earlier snapshots are historical evidence only. | Blocked | |
| Docs/wiki/public-demo gate | yes | Release notes, source diff, metadata, public-text privacy, documentation and screenshot-manifest checks pass. Public demo deferred: prerelease. No public screenshot or external wiki bytes changed. Failure and evidence limitations remain explicit. | Pass | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Not authorized in this preparation phase. No image published. | Blocked | |
| Deployment refresh/sniff tests | yes | Not authorized in this preparation phase. No installation adopted the candidate. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Failure and evidence limits

The legacy live browser group completed successfully. Its following native
app-performance command failed at `scripts/run_perf_harness.py:985`, through
`ApiClient._open_json:278`, on a forced inventory read returning HTTP 503.
The exact traceback and service logs are retained privately. The response body
was not retained by that native path. There was no failed browser assertion,
and no failure-time PNG or scoped response capture. Synthetic control captures
and earlier successful browser images are not substituted for missing evidence.
No request was replayed to reconstruct it. Cause remains unknown.

The disposable runtime was removed and the original test installation recovered
with independently verified IDs, image, health and zero restarts. The bounded
read-only observation recorded no alert, mail or retained-session delta. These
recovery results do not clear the failed release gate.

The native source and exact-cut CI remain valid at the stated cut: CI Preflight
run 45015563049 passed all 24 expected jobs, and CodeQL run 45015563005 passed
both language jobs. This follow-up changes release evidence only.

No narrow-beta deferrals apply. The PR remains draft, with five pre-publish rows
Blocked. No readiness event, independent-review request, merge, tag, publication,
deployment or external wiki action occurred in this round.
