# Release wrap: v0.24.0-rc.2

Date: `2026-11-21`

Release mode: full
Previous release tag: `v0.24.0-rc.1`

## Scope

Release preparation branch: `release/v0.24.0-rc.2`.
Base: `main` at `445605526747d2546c97f0e29b64307a27b84e49`.
Release cut: pending the additive release commit.

This recut includes PR #1460's evidence-aware ESXi browser observer without
rewriting release-branch history. Repository tooling recollected all 83
fragments and preserved every historical release section. Release notes describe
the changes since rc.1 and summarize the v0.24.0 line.

Validated against `docs/RELEASE_CHECKLIST.md`, including its prerelease rules.
Raw restore, appliance, resource and visual evidence remains private.
Preparation does not authorize merge, tagging, publication or deployment.
No narrow-beta deferrals apply. Carried results retain their original source
identity and are not represented as fresh execution on this successor.

Changelog coverage: pass (125 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Approved main merged additively. All 83 fragments and historical release sections preserved; pre-commit coverage includes 125 required PRs. Exact-cut coverage and resolver qualification pending. | Blocked | |
| Python unit and syntax gates | yes | Fresh full native source gate required on the clean committed recut. Earlier counts are historical only. | Blocked | |
| JavaScript syntax gates | yes | Fresh complete native syntax and unit gates required. | Blocked | |
| Docker build and health gates | yes | Fresh exact-cut image build and runtime identity required. | Blocked | |
| Optional-sidecar runtime matrix | yes | Prior eight-variant pass may be carried only after complete shipped and controlling-input identity proof. | Blocked | |
| Full Playwright/browser gates | yes | Fresh full fixture/Admin/demo browser run required because browser inputs changed. Legacy live browser, full read-only crawl and human-view remain required. | Blocked | |
| Feature-specific live API/UI gates | yes | Prior CORE and independent read-only comparator results require carried-evidence identity proof. Fresh storage-view, refresh/write-identity, telemetry and census gates remain required. | Blocked | |
| Local release perf harnesses | yes | Prior supplemental pair may be retained with input proof, but omitted mapping-import workflows do not qualify complete stock workflows and CSV trails. Missing full acceptance remains required. | Blocked | |
| Linux QA restore gate | yes | Prior separate fresh default tar.zst and explicit 7z FULL round trips require identity-backed carries. Fresh complete legacy live restore required with the corrected ESXi observer. | Blocked | |
| Restored Linux QA perf harnesses | yes | Complete three-iteration app/history workflows, exact labels and CSV trails required. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Prior isolated export-source snapshot may be carried with input proof. Fresh restored live snapshot required. | Blocked | |
| Docs/wiki/public-demo gate | yes | Source diff, release metadata, privacy, documentation and screenshot-manifest checks required. Public demo deferred: prerelease. No public screenshot or external wiki bytes changed. | Blocked | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Publication is outside this preparation phase. | Blocked | |
| Deployment refresh/sniff tests | yes | Deployment is outside this preparation phase. | Blocked | |
| Post-release reopen | yes | Publication, deployment and final validation must precede reopen. | Blocked | |

## Qualification boundary

The preceding cut passed all eight matrix variants and both independent fresh
FULL format round trips. Its legacy restore stopped at an unconditional ESXi
occupant-caption assertion. PR #1460 corrects that observer by choosing its
contract from scoped occupancy evidence and source warnings, retaining populated
identity and physical-order checks. Historical failure and recovery receipts
remain retained. This diagnosis does not substitute for a successful fresh drill.

The candidate must prove every shipped Dockerfile input identical to the prior
cut before authorized evidence is carried. Browser evidence is not carried.
Every retained gate must also prove unchanged controlling scripts and inputs.
The local performance supplement is not promoted to full acceptance.

The PR remains draft. Pre-tag validation stays blocked until every pre-publish
row passes. No ready event, independent-review request, merge, tag, publication,
deployment or external wiki action has occurred in this round.
