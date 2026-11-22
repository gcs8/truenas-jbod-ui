# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `b232fbd47c75eb1d6f083ddddf19a83dcb90504f`.
Release cut: pending the additive release commit.

This recut includes the Topology accessible-name correction and performance
observer readiness correction from PRs #1464 and #1462. It preserves release
branch history, all 85 collected fragments and every historical release section.
Release notes describe the changes since rc.1 and summarize the v0.24.0 line.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.
Raw restore, appliance, resource and visual evidence remains private.
Preparation does not authorize merge, tagging, publication or deployment.
No narrow-beta deferrals apply. Carried results retain their original source
identity and are not represented as fresh execution on this successor.

Changelog coverage: pass (127 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main merged additively. All 85 fragments and historical release sections preserved; pre-commit coverage includes 127 required PRs. Exact-cut coverage and resolver qualification pending. | Blocked | |
| Python unit and syntax gates | yes | Fresh full native source gate required on the clean committed recut. Earlier counts are historical only. | Blocked | |
| JavaScript syntax gates | yes | Fresh complete native syntax and unit gates required. | Blocked | |
| Docker build and health gates | yes | Fresh exact-cut image build and runtime identity required. | Blocked | |
| Optional-sidecar runtime matrix | yes | Prior eight-variant pass requires backend and controlling-input proof for the single browser-script shipped delta. | Blocked | |
| Full Playwright/browser gates | yes | Fresh full fixture/Admin/demo browser run, complete legacy live browser, full read-only crawl and human-view required. Topology unpinned, pinned bundle and pinned lane require desktop review and SVG-title checks. | Blocked | |
| Feature-specific live API/UI gates | yes | CORE and independent read-only comparator results require original-receipt identity proof. Fresh storage-view, refresh/write-identity, telemetry and census gates remain required. | Blocked | |
| Local release perf harnesses | yes | Complete prior stock app/history pair may be carried with original receipt and backend identity proof. | Blocked | |
| Linux QA restore gate | yes | Prior separate fresh default tar.zst and explicit 7z FULL round trips and offline drills require identity-backed carries. Fresh complete legacy live restore required. | Blocked | |
| Restored Linux QA perf harnesses | yes | Complete three-iteration app/history workflows, readiness timeout above the refresh limit, exact labels and CSV trails required. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Fresh restored Auto HTML and explicit ZIP exports and offline browser acceptance required. | Blocked | |
| Docs/wiki/public-demo gate | yes | Source diff, release metadata, privacy, documentation and screenshot-manifest checks required. Public demo deferred: prerelease. No public screenshot or external wiki bytes changed. | Blocked | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Publication is outside this preparation phase. | Blocked | |
| Deployment refresh/sniff tests | yes | Deployment is outside this preparation phase. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Qualification boundary

Earlier native failures and recovery receipts remain retained. The prior complete
legacy restore passed. Restored performance and new Topology accessibility
findings required corrections before this recut; diagnosis is not fresh acceptance.
The only admitted shipped delta from the preceding cut is
`app/static/sas_fabric_topology.js`. Backend-only carries must bind the original
receipts and controlling inputs, and state that the changed script is not loaded.
Browser evidence is not carried. All required live gates run on the new image.

The PR remains draft. Pre-tag validation stays blocked until every pre-publish
row passes. No ready event, independent-review request, merge, tag, publication,
deployment or external wiki action has occurred in this round.
