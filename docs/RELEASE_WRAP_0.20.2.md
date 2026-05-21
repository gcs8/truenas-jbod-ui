# Release Wrap - v0.20.2

Date: `2026-05-21`

## Scope

`v0.20.2` is a corrective public release for the release process itself.

The release keeps `v0.20.1` Storage Fabric runtime behavior intact and ships
the global release-gate hardening that prevents future tags from skipping or
silently narrowing `docs/RELEASE_CHECKLIST.md`.

Included changes:

- app/package metadata bump to `0.20.2`
- `docs/RELEASE_CHECKLIST.md` as mandatory source of truth for every tag
- release-wrap evidence-table requirements for every future release
- `scripts/validate_release_wrap.py` plus regression coverage
- `docs/RELEASE_WRAP_0.20.1.md` post-publish checklist audit
- `docs/RELEASE_NOTES_0.20.2.md`

No new Storage Fabric platform enrichment, UI workflow, or write-capable action
is introduced by this patch.

## Checklist Evidence

Validated against `docs/RELEASE_CHECKLIST.md`.

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | pending | Blocked |  |
| Python unit and syntax gates | yes | pending | Blocked |  |
| JavaScript syntax gates | yes | pending | Blocked |  |
| Docker build and health gates | yes | pending | Blocked |  |
| Optional-sidecar runtime matrix | yes | pending | Blocked |  |
| Full Playwright/browser gates | yes | pending | Blocked |  |
| Feature-specific live API/UI gates | yes | pending | Blocked |  |
| Local release perf harnesses | yes | pending | Blocked |  |
| Linux QA restore gate | yes | pending | Blocked |  |
| Restored Linux QA perf harnesses | yes | pending | Blocked |  |
| Snapshot/export/offline artifact gate | yes | pending | Blocked |  |
| Docs/wiki/public-demo gate | yes | pending | Blocked |  |
| GHCR publish verification | yes | pending | Blocked |  |
| Deployment refresh/sniff tests | yes | pending | Blocked |  |
| Post-release reopen | yes | pending | Blocked |  |

## Publish Result

Pending until the checklist evidence table is complete.

## Notes

- `v0.20.1` is intentionally left intact. Deleting, overwriting, or retagging a
  public release would make the operator/audit trail less trustworthy.
- `0.20.1.1` was not used as the app/package version because the repo metadata
  uses SemVer-compatible versions; `0.20.2` is the SemVer-safe corrective patch.
