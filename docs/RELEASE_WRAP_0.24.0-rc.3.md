# Release wrap: v0.24.0-rc.3

Date: `2026-11-27`

Release mode: full
Previous release tag: `v0.24.0-rc.2`

## Scope

Release branch: `release/v0.24.0-rc.3`.
Base: `main` at `f7c1f77f8f9abbe97b156e757c987b8dee0b2cb4`.
The tag, registry publication and deployment are outside phase A. No
qualification result is carried forward from rc.2. The release follows
`docs/RELEASE_CHECKLIST.md`, including its prerelease rules.

Release cut: `8354ca490259909aa2297f922a40d0ef2f69edcb`.
Qualification outcome: **Pass** for phase A, subject to exact-head CI on this
wrap and the release PR review gate. No registry image or deployment was made.
Local QA image `sha256:ec5bb8770cae50a8b4557d1277d10204b218b6afc6e84f711479d743ad709eb0` is pinned to the cut.

Changelog coverage: pass (23 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Additive release cut; source and built-image manifests confirm that only the five approved UI assets/templates changed from the preceding rc.3 cut. Native changelog coverage is 23 required PRs; #1507 is also documented. Actual prerelease resolver emits only the fixed rc.3 version aliases and dev. | Pass | |
| Python unit and syntax gates | yes | Clean exact-cut dev_check --full: 7,280 run, 7 skipped, no failures; compile and source gates passed. Exact-cut CI passed all eight Python shards and both aggregates. | Pass | |
| JavaScript syntax gates | yes | Exact-cut maintained syntax and unit gates passed; 2,577 passed, 5 skipped, zero failures. | Pass | |
| Docker build and health gates | yes | Exact-cut image build and OCI revision/version verified. Disposable health checks and restart evidence passed. Test installation restored to original IDs and digest, healthy with zero restarts; task resources absent. Not deployment acceptance. | Pass | |
| Optional-sidecar runtime matrix | yes | Fresh exact-cut 8/8 Compose variants passed, including root-template and persistence coverage. | Pass | |
| Full Playwright/browser gates | yes | Fresh 16 fixture/Admin/demo specs on the cut: 607 passed, no skips or failures. Read-only crawl: 42/42; 40 targets, 1,021 states, 1,664 native captures across 390/1600/1920/3840; zero unknown errors, blocked requests or writes. Human-view inspected live topology at 1600/3840 with pins, inspectors and export dialog, plus prior exact-cut synthetic retry/cancel cases. | Pass | |
| Feature-specific live API/UI gates | yes | CORE 11/11 and membership comparator 53/53 carried from the earlier rc.3 cut with proven unchanged backend/controller inputs and verified immutable receipts. Four fresh feature controllers and fresh immediate census passed. Current pre/post-hold segment paths, metadata and file bytes match. Read-only appliance observation closed without new alert-check mail or retained-session increase. | Pass | |
| Local release perf harnesses | yes | Fresh exact-cut app/history stock pairs passed; three iterations each, native CSV/JSON and mapping readback. | Pass | |
| Linux QA restore gate | yes | Fresh FULL encrypted tar.zst and explicit 7z exports and both offline round trips passed. Fresh legacy live restore passed count and restart readback, offline and live browser, app/history perf and final cleanup. Later disposable-stack setup instantiates the same data for the crawl, not a repeat qualification claim. | Pass | |
| Restored Linux QA perf harnesses | yes | Fresh exact-cut app/history stock pairs passed; three iterations each, native CSV/JSON and mapping readback. Readiness timeout 330 seconds. | Pass | |
| Snapshot/export/offline artifact gate | yes | Fresh source and restored HTML/ZIP snapshot exports and native offline rendering passed. Private artifacts not published. Browser estimate latency is tracked separately in #1558. | Pass | |
| Docs/wiki/public-demo gate | yes | Release metadata, notes, docs/privacy checks and checked-in public demo evidence passed. Public demo deferred: prerelease. Exact external wiki publication is a separate owner gate. Wrap-only head requires its own CI. | Pass | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Not part of phase A. | Blocked | |
| Deployment refresh/sniff tests | yes | Not part of phase A. | Blocked | |
| Post-release reopen | yes | Final release validation must precede reopen. | Blocked | |

## Crawler and visual evidence

The live crawl used the pre-#1533 QA tooling at `f7c1f77f8f9abbe97b156e757c987b8dee0b2cb4` and a
private Playwright 1.64.1 client pin. This owner-approved choice did not
change the repository's dependencies, the shipped application, or the image.
The request guard and native findings remained enabled. Document retirement
follows the earlier crawler version; no export download was admitted. The
private proxy measured 54 estimate HTTP 200 responses from
0.007 to 98.646 seconds (median
0.205 seconds). Some screenshots show a pending result.
The known cold-start estimate latency is tracked for after rc.3 in [#1558].

Native read-only results: 768 done,
214 not applicable, and
39 listed. Listed writes and external actions
were not exercised. Native captures at 390/1600/1920/3840:
416, 416, 416, 416, respectively.
The original test installation was restored after the visual and tree checks.

[#1558]: https://github.com/gcs8/truenas-jbod-ui/issues/1558
