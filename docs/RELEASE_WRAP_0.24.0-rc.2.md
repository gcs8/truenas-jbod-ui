# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `177cc0b70c87d33a02aa71d52d8f0241493ca5e7`.
Release cut: `6150cce10e1e9af4300efcf2a786235266a14246`.

This additive merge includes PR #1458's Compose matrix restart-readiness
correction without rewriting release-branch history. Repository tooling
recollected all 82 fragments and preserved every historical release section.
Release notes describe the changes since rc.1 and summarize the v0.24.0 line.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.
Raw restore, appliance, resource and visual evidence remains private.
Preparation does not authorize merge, tagging, publication or deployment.
No narrow-beta deferrals apply. Carried results retain their original source
identity and are not represented as fresh execution on this successor.

Changelog coverage: pass (124 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main merged additively. All 82 fragments and historical release sections preserved; exact-cut coverage includes 124 required PRs. The actual workflow resolver emits only the two exact rc.2 aliases and dev; all 22 publish-contract tests passed. | Pass | |
| Python unit and syntax gates | yes | Full native dev_check --full on clean committed 6150cce10e1e9af4300efcf2a786235266a14246 returned exit 0 and FINAL: PASS. Python ran 7,019 tests with seven skipped and no failures. Compileall, bounded Ruff, whitespace, performance baseline and checked-in demo passed. Local promtool was explicitly unavailable; exact-cut CI installed its pinned tool and passed. Start and end commit and tree matched. | Pass | |
| JavaScript syntax gates | yes | The same full native source run passed all maintained script and browser-spec syntax checks. JavaScript reported 2,076 total, 2,071 passed, five skipped, zero failed, zero cancelled and zero todo. | Pass | |
| Docker build and health gates | yes | Fresh exact-cut build returned native 0 with matching OCI revision and application version. QA image sha256:dd117752e3f31c777883aa99de58df8f8934713b7673dda6d6bd5d1ad4f666ad is not published. All eight matrix variants passed health, authentication, identity, runtime-user and isolation contracts. Copied product files match the predecessor despite different image metadata. | Pass | |
| Optional-sidecar runtime matrix | yes | All eight exact-source variants and the native complete success marker passed: UI only, UI/history, Admin initial setup, UI/Admin, all three services, scheduler disabled, scheduler enabled backup and hardened nonroot UI. Five alias cycles, five mapping save/readback/restart/readback/clear cycles, two journal-read cycles, one Admin setup and both scheduler contracts passed. No assertion was relaxed. | Pass | |
| Full Playwright/browser gates | yes | Fixture/Admin/demo 580 passed and zero skipped across 16 files are Carried with complete product and browser-input identity. Each of three offline restore groups had 16 passed and one skipped. Legacy live browser had 15 passed, two skipped and one failed at qa/esxi-smoke.spec.js:89. The full read-only crawl and human-view did not start after this failure. | Blocked | |
| Feature-specific live API/UI gates | yes | CORE eleven-case qualification with independent cleanup and the independent read-only comparator are Carried with shipped-input identity. Comparator had zero unconfirmed placements and zero failed reads. Fresh storage-view, refresh/write-identity, telemetry and census gates did not start after the legacy browser failure. | Blocked | |
| Local release perf harnesses | yes | A supplemental three-iteration pair ran on the isolated export source, but its app harness omits mapping import. Mandatory complete stock workflows, exact labels and CSV trails did not run. The supplement is not substituted for full acceptance. | Blocked | |
| Linux QA restore gate | yes | Both fresh encrypted schema-v2 FULL round trips passed separately: default packaging-omitted tar.zst and explicit 7z, exported from the same isolated current-image API and complete path set. Both target drills passed inspect/import/counts/restart/browser/perf and independent cleanup, with no absent groups. The separate legacy live restore failed its ESXi browser assertion; the whole restore gate remains blocked. | Blocked | |
| Restored Linux QA perf harnesses | yes | The required complete three-iteration app and history pair did not run after the legacy browser failure. One-iteration restore-controller history measurements are supplemental only. | Blocked | |
| Snapshot/export/offline artifact gate | yes | The isolated export source passed estimate, HTML/ZIP export and offline-render checks. The mandatory restored live snapshot path did not run after the browser failure, so the complete row remains blocked. | Blocked | |
| Docs/wiki/public-demo gate | yes | Source, release metadata and public-safe evidence diff reviewed. Privacy, public-doc, screenshot-manifest and prerelease demo checks run with the evidence follow-up. Public demo deferred: prerelease. No public screenshot or external wiki bytes changed. | Pass | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Publication is outside this preparation phase. | Blocked | |
| Deployment refresh/sniff tests | yes | Deployment is outside this preparation phase. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Carried evidence and input identity

| Qualification | Disposition | Original source | Evidence boundary |
| --- | --- | --- | --- |
| CORE appliance and loopback cases | Carried | 5643c6b174f423e8f08a37c0e528c2fb8b4958fe | Eleven completed cases and independent account, group and home cleanup. No appliance rerun. |
| Fixture, Admin and generated-demo browser | Carried | 5643c6b174f423e8f08a37c0e528c2fb8b4958fe | Every original native per-file exit passed, 580 passed and zero skipped. Browser specs, configuration, dependencies, fixture builder and demo inputs are identical. |
| Independent read-only membership comparator | Carried | 5643c6b174f423e8f08a37c0e528c2fb8b4958fe | Every occupied placement confirmed, zero unconfirmed placements or failed reads. Its original closed monitoring window remains retained. No new comparator collection. |

Every Dockerfile COPY source, both requirements files, Dockerfile and
`.dockerignore` have identical Git tree or blob objects to both earlier cuts
5643c6b174f423e8f08a37c0e528c2fb8b4958fe and
404dd296bc55e5f0536435afc40afeefab72f8fa. This includes the complete application,
history and Admin trees, static assets, templates, copied tools and example
configuration. All browser inputs also match. Per-path hashes, complete changed
paths and ten original native evidence hashes are retained privately.
Independent network-disabled, read-only image inspection matched all 166 copied
files by bytes, mode, UID, GID and symlink target. Rebuilt image IDs differ;
no equality of image metadata is claimed. The repaired matrix ran fresh.

## Native failure and verified recovery

Historical failures and their source-bound receipts remain retained. The new
restart-readiness matrix passed every variant. Both fresh format round trips
then passed independently. The legacy live restore reached its browser phase:
offline browser passed, while the live group failed its ESXi assertion at
qa/esxi-smoke.spec.js:89. The first carrier tile was expected to contain
`13:1`; it instead showed a slot-numbered storage-view card with unknown
occupancy. The enclosure selection, carrier image and tile count assertions
preceded this failure. Its underlying cause is not established by this receipt.
No retry or product/controller edit ran, and the failed gate is not waived
because shipped product bytes match earlier cuts.

The failure-shaped restore receipt reports browser-and-performance failure,
verified cleanup and no retained stack. It omits aggregate, restart and pencil
summaries, so none is inferred from incomplete execution. The owner runner
stopped and restored the original container names, IDs and image. Independent
readback proved both running and healthy with zero restarts, successful empty
container/network/volume censuses, all task listeners closed, no credential-copy
directories or owned helpers, all task runtimes absent and clean frozen source.
Original Compose files, data and image were not edited.

A fresh read-only monitoring window surrounded this runtime attempt. Its post
snapshot was taken at an idle checkpoint more than five minutes after recovery.
Alert totals, active alerts, mail jobs and total/internal/QA-origin retained
sessions had zero net change; new alert-check mail was zero. No session count
rose, so no extra settled snapshot was required. Per-session creation/type rows
and fresh call bounds are retained privately. These net observations do not
prove load causation or clear the browser failure.

Partial-run resource observations retained UI and Admin role peaks within their
budgets and no observed history OOM kills. Six metric reads failed, and one
partial cgroup sample made the inherited aggregate reporter refuse. A separate
complete-sample report preserves that gap; it does not claim full runtime/load
qualification or change the native result.

[Exact-cut CI run 44961036439](https://github.com/gcs8/truenas-jbod-ui/actions/runs/44961036439)
passed all 24 jobs. Both CodeQL languages passed in
[run 44961036281](https://github.com/gcs8/truenas-jbod-ui/actions/runs/44961036281).
These qualify the cut, not a later evidence-only successor. Such a successor
requires its own exact-head CI.

The PR remains draft. Pre-tag validation remains blocked by the unqualified
runtime rows. No ready event or independent-review request occurred. No merge,
tag, publication, deployment or external wiki action occurred. Parent review
owns diagnosis and any new qualification attempt.
