# Release wrap: v0.24.0-beta.8

Date: `2026-11-11`

## Scope

Release preparation branch: `release/v0.24.0-beta.8`.
Base: `main` at `4242fa5884be70048d4aae0be302dfd53f7643d2`.
Previous fully qualified beta: `v0.24.0-beta.7` at
`690a2d55236daea8a500965a455ab6d045520578`.

The entire source delta is front-end assets, tests, QA tooling and docs.
The user-visible change is #1153, state-chip clearance from slot numbers,
heat values, LEDs and latches. #1152 records the preceding release.

Validated against `docs/RELEASE_CHECKLIST.md`, subject to owner-approved
D-TNJ-20261111-NARROWBETA. Its long-gate deferrals are not passes or waivers.
All rows begin Blocked. The current validator admits no deferred status;
check this policy representation before expensive qualification.

Release source commit: the pinned phase-A PR head, not yet runtime-qualified.
Candidate image digest and revision: not built.
Changelog coverage: pass (1 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Release branch cut from main `4242fa5884be70048d4aae0be302dfd53f7643d2`; metadata and qualification pending | Blocked |  |
| Python unit and syntax gates | yes | Not run: `dev_check.py --full` pending | Blocked |  |
| JavaScript syntax gates | yes | Not run: full source gate pending | Blocked |  |
| Docker build and health gates | yes | Not run: candidate image build pending; no service deployment authorized in phase A | Blocked |  |
| Optional-sidecar runtime matrix | yes | Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Blocked |  |
| Full Playwright/browser gates | yes | Narrow fixture specs and human-view screenshots pending; full 42-chunk crawl: Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Blocked |  |
| Feature-specific live API/UI gates | yes | Narrow fixture-only chip checks pending; CORE VM checks and Archive comparator: Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Blocked |  |
| Local release perf harnesses | yes | Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Blocked |  |
| Linux QA restore gate | yes | Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Blocked |  |
| Restored Linux QA perf harnesses | yes | Deferred to rc/final per D-TNJ-20261111-NARROWBETA | Blocked |  |
| Snapshot/export/offline artifact gate | yes | Not run in phase A; no snapshot/export behavior changed, disposition pending | Blocked |  |
| Docs/wiki/public-demo gate | yes | Source review and privacy checks pending; public demo deferred: prerelease | Blocked |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Not run in phase A; no publish authorized | Blocked |  |
| Deployment refresh/sniff tests | yes | Not run in phase A; no deployment authorized | Blocked |  |
| Post-release reopen | yes | Blocked on publication and authorized deployment qualification | Blocked |  |

## Deferred scope

- Compose matrix: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Legacy live restore: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Default `tar.zst` and explicit `7z` round trips: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Performance harnesses: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- CORE VM checks: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Archive comparator: Deferred to rc/final per D-TNJ-20261111-NARROWBETA
- Full 42-chunk crawl: Deferred to rc/final per D-TNJ-20261111-NARROWBETA

## Hold boundary

No merge, tag, publish, deployment or external wiki publication in phase A.
Production remains outside scope. No earlier release's passing rows are
carried forward as fresh beta.8 execution evidence.
