# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `6c79e68c237a4bda5edf655ca23970270dd855ee`.
Release cut: `404dd296bc55e5f0536435afc40afeefab72f8fa`.

This additive merge includes PR #1456's synthetic live-source Compose matrix
correction without rewriting release-branch history. Repository tooling
recollected all 81 fragments and preserved every historical release section.
Release notes describe the changes since rc.1 and summarize the v0.24.0 line.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.
Raw restore, appliance, resource and visual evidence remains private.
Preparation does not authorize merge, tagging, publication or deployment.
No narrow-beta deferrals apply. Carried results retain their original source
identity and are not represented as fresh execution on this successor.

Changelog coverage: pass (123 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main revision merged additively. All 81 fragments and historical release sections preserved; exact-cut coverage includes 123 required PRs. The actual workflow tag resolver emits the two exact rc.2 aliases and dev, never latest; all 22 publish-contract tests passed. | Pass | |
| Python unit and syntax gates | yes | Full native source gate on clean committed 404dd296bc55e5f0536435afc40afeefab72f8fa returned exit 0 and FINAL: PASS. Python ran 7,008 tests with seven skipped and no failures. Compileall, bounded Ruff, whitespace, performance baseline and checked-in demo checks passed. Local promtool was explicitly skipped as unavailable; exact-cut CI installed the pinned tool and passed. Start and end source commit and tree matched. | Pass | |
| JavaScript syntax gates | yes | The same full native source run passed all declared script and browser-spec syntax checks. JavaScript reported 2,076 total, 2,071 passed, five skipped, zero failed, zero cancelled and zero todo. | Pass | |
| Docker build and health gates | yes | Fresh exact-cut image build returned native 0 with matching OCI revision and application version. QA image sha256:59e4bb7097c8af86d9acdf3b98cce292580e6025f7205e27f76de1e0197071ec is not published. Its digest differs from the predecessor, but every copied product file, mode and owner matches. The first complete variant passed; the second was healthy but failed its mapping-clear contract. Full runtime health qualification remains incomplete. | Blocked | |
| Optional-sidecar runtime matrix | yes | Native exit 1. The complete ui-only variant passed, including alias and mapping persistence, restart, clear and journal readback. In ui-history, mapping save and persisted restart readback completed, then DELETE /api/slots/0/mapping returned HTTP 503 instead of 200 at scripts/run_compose_runtime_matrix.py:1141. The body was omitted, so its category is unknown. Remaining variants did not run. No assertion was relaxed, no retry ran, and independent original-installation recovery passed. | Blocked | |
| Full Playwright/browser gates | yes | Carried from 5643c6b1 with complete shipped-path and browser-input hash equality: 16 native per-file runs, 580 passed, zero skipped, covering fixtures, Admin and generated demo. Restored browser checks, full read-only crawl and human-view review did not start after the matrix failure. | Blocked | |
| Feature-specific live API/UI gates | yes | CORE eleven-case qualification and independent cleanup, plus the independent read-only membership comparator, are Carried from 5643c6b1 with shipped-tree identity proof. Comparator had zero unconfirmed placements and no failed initial or enrichment reads. It was not rerun. Fresh storage-view, refresh/write-identity, telemetry and census gates did not start. | Blocked | |
| Local release perf harnesses | yes | Both full three-iteration harnesses and CSV trails unrun after the matrix failure. | Blocked | |
| Linux QA restore gate | yes | Fresh default tar.zst and explicit 7z FULL export and restore round trips, plus legacy live-read-only restore, unrun after the matrix failure. No restore controller started. | Blocked | |
| Restored Linux QA perf harnesses | yes | Both full three-iteration restored-stack harnesses and CSV trails unrun after the matrix failure. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Source and restored-stack estimate, Auto HTML, ZIP and offline browser acceptance unrun after the matrix failure. | Blocked | |
| Docs/wiki/public-demo gate | yes | Exact-cut source and metadata diff reviewed. Public privacy, documentation, screenshot-manifest and prerelease demo checks are recorded with the evidence follow-up. Public demo deferred: prerelease. No public screenshot or external wiki bytes changed. | Pass | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Publication is outside this preparation phase. | Blocked | |
| Deployment refresh/sniff tests | yes | Deployment is outside this preparation phase. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Carried evidence and input identity

| Qualification | Disposition | Original source | Evidence boundary |
| --- | --- | --- | --- |
| CORE appliance and loopback cases | Carried | 5643c6b174f423e8f08a37c0e528c2fb8b4958fe | Eleven completed cases, independent account, group and home cleanup. No appliance rerun. |
| Fixture, Admin and generated-demo browser | Carried | 5643c6b174f423e8f08a37c0e528c2fb8b4958fe | Every per-file native exit passed; 580 passed and zero skipped. Browser specs, configuration, dependencies, fixture builder and demo inputs are identical. |
| Independent read-only membership comparator | Carried | 5643c6b174f423e8f08a37c0e528c2fb8b4958fe | All occupied placements confirmed, zero unconfirmed placements or failed reads. Its closed window had unchanged alert, mail-job and retained-session counts and zero new alert-check mail. No new collection. |

Every Dockerfile COPY source, both requirements files, Dockerfile and
`.dockerignore` have identical Git tree or blob objects in the original and
successor cuts. This includes the complete application, history and Admin
trees, their static assets and templates, copied history tools and example
configuration. Per-path hashes and the complete changed-path manifest are
retained privately. An independent network-disabled, read-only inspection of
both images also matched all 166 copied files by bytes, mode, UID, GID and
symlink target. Image IDs and rootfs layer digests differ; no equality of
rebuilt image metadata is claimed.

## Native failure and recovery

Earlier cuts and their failed observers remain historical evidence. PR #1454
corrected the first cut's test assumptions. A tested loopback-only observer
replay completed the second cut's eleventh CORE case without repeating its
appliance-account cycle. The independent browser run passed before subsequent
runtime attempts. A private comparator version-pin correction passed offline
positive and negative controls before its one actual collection.

The fifth round's initial mapping save received HTTP 409. PR #1456 then
corrected the repository matrix's unreachable occupancy fixture. The sixth
round reran the complete native source gate, exact-head CI and image build,
proved product identity, and executed that corrected matrix. The first complete
variant passed. The second failed at mapping clear after the UI restart,
returning 503. The controller retained no response body for this status, so
neither its category nor its root cause is established by this receipt.

The runner stopped immediately. Its inherited recovery handler restored the
original container names, IDs and image, and independent readback confirmed
both running and healthy with zero restarts. Successful container, network and
volume censuses found no disposable resources. All task listeners were closed,
credential-copy directories and owned helper processes were absent, and the
frozen source remained clean. Original Compose files, data and image were not
edited. No restore, export, performance, feature, crawl or human-view phase ran.
The new read-only monitoring window closed after two consecutive idle telemetry
observations and a post snapshot more than five minutes after independent
recovery readback. No new alert-check mail appeared; alert and mail-job counts
did not increase, and retained sessions decreased. No additional settled
snapshot was required. Per-session creation and credential-type metadata is
retained privately. These are net observations, not load-causation claims, and
they do not substitute for the failed matrix.

[Exact-cut CI run 44951647760](https://github.com/gcs8/truenas-jbod-ui/actions/runs/44951647760)
completed with all 24 jobs successful. Both CodeQL language jobs passed in
[run 44951647717](https://github.com/gcs8/truenas-jbod-ui/actions/runs/44951647717).
These results are attributed to the release cut, not to a later evidence-only
commit. Every evidence-only successor requires its own exact-head CI.

The PR remains draft. Pre-tag validation remains blocked by unqualified runtime
rows. No readiness event or independent-review request occurred. No merge,
tag, publication, deployment or external wiki action occurred. Stable promotion
and production actions remain outside this phase.
