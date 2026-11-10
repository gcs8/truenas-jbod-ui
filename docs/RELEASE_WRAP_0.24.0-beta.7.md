# Release wrap: v0.24.0-beta.7

Date: `2026-11-09`

## Scope

v0.24.0-beta.7 is the seventh prerelease of v0.24.0. It is cut from current
`main` and adds the pull requests merged after `v0.24.0-beta.6`. GHCR gets the
fixed `v0.24.0-beta.7` and `0.24.0-beta.7` tags and moves `dev` to it;
`latest` stays on `v0.23.0`.

Release preparation branch: `release/v0.24.0-beta.7`. It is merged into
`main` with a merge commit, never squashed, so the validated commit stays
reachable from the tag, which goes on that `main` merge commit.

Base: `main` at `65076010bbfec53901f98f22c6e1765f8569a4df`.

Validated release-branch commit: pending.

Validated Linux QA image: pending.

Validated against `docs/RELEASE_CHECKLIST.md`, including its "Prereleases"
section.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Pending qualification | Blocked |  |
| Python unit and syntax gates | yes | Pending qualification | Blocked |  |
| JavaScript syntax gates | yes | Pending qualification | Blocked |  |
| Docker build and health gates | yes | Pending qualification | Blocked |  |
| Optional-sidecar runtime matrix | yes | Pending qualification | Blocked |  |
| Full Playwright/browser gates | yes | Pending qualification | Blocked |  |
| Feature-specific live API/UI gates | yes | Pending qualification | Blocked |  |
| Local release perf harnesses | yes | Pending qualification | Blocked |  |
| Linux QA restore gate | yes | Pending qualification | Blocked |  |
| Restored Linux QA perf harnesses | yes | Pending qualification | Blocked |  |
| Snapshot/export/offline artifact gate | yes | Pending qualification | Blocked |  |
| Docs/wiki/public-demo gate | yes | Pending qualification | Blocked |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | Recorded after the GitHub prerelease publishes `v0.24.0-beta.7`, `0.24.0-beta.7` and `dev` | Blocked |  |
| Deployment refresh/sniff tests | yes | Recorded after the `dev` image is deployed to the beta test hosts | Blocked |  |
| Post-release reopen | yes | Blocked on the deployment gate | Blocked |  |

## Hold boundary

This wrap does not authorize a tag while any pre-publish row is `Blocked`. Once
it passes, it authorizes the `v0.24.0-beta.7` prerelease and its `dev` image
only. It does not move `latest`, deploy to production, or publish the external
wiki.
