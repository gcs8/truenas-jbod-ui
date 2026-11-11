# Release wrap: v0.24.0-rc.1

Date: `2026-11-11`

Release mode: full
Previous release tag: `v0.24.0-beta.8`

## Scope

Release preparation branch: `release/v0.24.0-rc.1`.
Base: `main` at `2fd82ffd0f8ffecbeedac35f257b5a52369fd2ed`.
The candidate includes #1160, #1159 and #1163 plus the beta.8 wrap #1162.
Every beta.8-deferred gate is required. No Deferred result is permitted.

Release commit and QA image: pending qualification.
Tag `v0.24.0-rc.1` is not created. No release, registry publication,
deployment, production action or external wiki publication is authorized
by this phase-A record. A later phase requires separate admission.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Pending full candidate qualification. | Blocked |  |
| Python unit and syntax gates | yes | Pending full candidate qualification. | Blocked |  |
| JavaScript syntax gates | yes | Pending full candidate qualification. | Blocked |  |
| Docker build and health gates | yes | Pending full candidate qualification. | Blocked |  |
| Optional-sidecar runtime matrix | yes | Pending full candidate qualification. | Blocked |  |
| Full Playwright/browser gates | yes | Pending full candidate qualification. | Blocked |  |
| Feature-specific live API/UI gates | yes | Pending full candidate qualification. | Blocked |  |
| Local release perf harnesses | yes | Pending full candidate qualification. | Blocked |  |
| Linux QA restore gate | yes | Pending full candidate qualification. | Blocked |  |
| Restored Linux QA perf harnesses | yes | Pending full candidate qualification. | Blocked |  |
| Snapshot/export/offline artifact gate | yes | Pending full candidate qualification. | Blocked |  |
| Docs/wiki/public-demo gate | yes | Pending full candidate qualification. | Blocked |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Pending separately authorized post-publish phase. | Blocked |  |
| Deployment refresh/sniff tests | yes | Pending separately authorized post-publish phase. | Blocked |  |
| Post-release reopen | yes | Pending separately authorized post-publish phase. | Blocked |  |
