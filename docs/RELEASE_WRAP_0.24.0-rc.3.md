# Release wrap: v0.24.0-rc.3

Date: `2026-11-27`

Release mode: full
Previous release tag: `v0.24.0-rc.2`

## Scope

Release branch: `release/v0.24.0-rc.3`.
Base: `main` at `62649c3ec9e8b393adf6ef2184ffd4b1c8a7b0d9`.
The tag, registry publication and deployment are outside phase A. No
qualification result is carried forward from rc.2. The release follows
`docs/RELEASE_CHECKLIST.md`, including its prerelease rules.

Changelog coverage: pass (20 PRs)

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Final cut identity and resolver verification pending. | Blocked | |
| Python unit and syntax gates | yes | Full exact-cut source run pending. | Blocked | |
| JavaScript syntax gates | yes | Full exact-cut source run pending. | Blocked | |
| Docker build and health gates | yes | Exact-cut image and disposable runtime checks pending. | Blocked | |
| Optional-sidecar runtime matrix | yes | All eight variants pending. | Blocked | |
| Full Playwright/browser gates | yes | Fixture, Admin, demo, live read-only crawl and human-view runs pending. | Blocked | |
| Feature-specific live API/UI gates | yes | CORE, comparator, storage, retry and census checks pending. | Blocked | |
| Local release perf harnesses | yes | App/history pair pending. | Blocked | |
| Linux QA restore gate | yes | Legacy live restore, both format round trips and both offline restore drills pending. | Blocked | |
| Restored Linux QA perf harnesses | yes | Restored app/history pair pending. | Blocked | |
| Snapshot/export/offline artifact gate | yes | Offline and restored export checks pending. | Blocked | |
| Docs/wiki/public-demo gate | yes | Source review and public artifact checks pending; public demo deferred: prerelease. | Blocked | |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked | |
| GHCR publish verification | yes | Not part of phase A. | Blocked | |
| Deployment refresh/sniff tests | yes | Not part of phase A. | Blocked | |
| Post-release reopen | yes | Final release validation must precede reopen. | Blocked | |
