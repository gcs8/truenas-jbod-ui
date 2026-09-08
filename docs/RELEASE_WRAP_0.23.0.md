# Release wrap: v0.23.0

Date: `2026-09-08`

## Scope

v0.23.0 consolidates the post-v0.22.2 release line. It includes the reviewed platform, security, history, backup, restore, deployment, documentation, and operator-workflow changes recorded in `CHANGELOG.md`.

Release preparation branch: `release/v0.23.0`.

Release source input commit: `c7c9ca2741916c875be58e3f4e51768d79f6623d`.

Public-demo artifact commit: `50ec0d6a4738d7d2cd2045951354c79edfdaa86e`.

Tag: `v0.23.0` pending. GitHub release pending.

Validated against `docs/RELEASE_CHECKLIST.md`.

Changelog coverage: pass (136 PRs)

The generated GitHub release-note set includes 139 pull requests: the 136 changelog-governed pull requests plus 3 dependency pull requests. All 139 currently map to `.github/release.yml` categories; 121 historical pull requests were backfilled with the same conventional-title labels used by the current automatic label workflow.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Isolated `release/v0.23.0` worktree starts from verified `main` commit `850236f568aa9486f0b6ab1a2d439c6edcf3ce44`; no tag, release, GHCR, deployment, or production mutation has occurred | Pass |  |
| Python unit and syntax gates | yes | Final-candidate full source gate and exact test count remain required after the wrap is complete | Blocked |  |
| JavaScript syntax gates | yes | Final-candidate JavaScript syntax and unit gates remain required | Blocked |  |
| Docker build and health gates | yes | Exact-candidate image build, OCI source label, and three-service health remain required | Blocked |  |
| Optional-sidecar runtime matrix | yes | Exact-candidate UI-only, UI+history, UI+admin, admin-only, and all-service matrix remains required | Blocked |  |
| Full Playwright/browser gates | yes | Synthetic public-demo Chromium suite passed 9 tests; offline snapshot, admin, and intentionally configured live-stack suites remain required | Blocked |  |
| Feature-specific live API/UI gates | yes | Read-only production inventory confirmed the safe aggregate demo shape; exact-candidate live operator workflows and acceptance remain required | Blocked |  |
| Local release perf harnesses | yes | Exact-candidate main and history performance harnesses remain required | Blocked |  |
| Linux QA restore gate | yes | Disposable Linux encrypted backup inspection/import and exact restored provenance remain required | Blocked |  |
| Restored Linux QA perf harnesses | yes | Serial main and history harnesses against the restored Linux QA stack remain required | Blocked |  |
| Snapshot/export/offline artifact gate | yes | Checked public demo and screenshots pass; final exact-candidate snapshot export and offline browser smoke remain required | Blocked |  |
| Docs/wiki/public-demo gate | yes | Public demo is deterministic and publishable at 2,070,273 raw bytes and 930,591 gzip bytes; 47 populated and 13 empty bays match the approved production aggregate; slots 42 and 43 use one synthetic `spares` group; two exact-byte screenshots passed visual review; final source/privacy checks remain required | Blocked |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki, public demo | Blocked |  |
| GHCR publish verification | yes | Requires the published v0.23.0 release workflow, immutable image digest, and exact source revision | Blocked |  |
| Deployment refresh/sniff tests | yes | Requires a verified encrypted FULL backup, private deployment receipt, immutable digest activation, health checks, and rollback evidence | Blocked |  |
| Post-release reopen | yes | Requires completed GHCR, deployment, and publication evidence before reopening development | Blocked |  |

## Current evidence

- Production-derived input was limited to aggregate structure from the fixed cached UI endpoints: 47 populated bays, 13 empty bays, two adjacent spare slots sharing one pool/vdev/class group, a 2-slot boot view with 2 matches, and a 4-slot NVMe view with 4 matches.
- The checked fixture remains `provenance: synthetic`. Production device names, serials, addresses, hostnames, measurements, credentials, history rows, and raw payloads were not copied into the repository.
- Public screenshots visibly report v0.23.0 and synthetic provenance. Exact image hashes, sizes, dimensions, source revision, and source artifact fingerprint are recorded in `docs/PUBLIC_SCREENSHOT_REVIEW.md` and `docs/images/screenshots/manifest.json`.
- `.github/release.yml` category reconciliation covers Breaking changes, Security, Features, Fixes, Performance, Documentation, Dependencies, and Internal with zero unmatched release-note pull requests.

## Hold boundary

Do not tag or publish v0.23.0 while any required pre-tag row remains Blocked. Operator acceptance is required after the disposable Linux restore candidate is available. GHCR publication, deployment, external Wiki/public-demo publication, and post-release reopen remain separate later gates.
