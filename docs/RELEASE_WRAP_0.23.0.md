# Release wrap: v0.23.0

Date: `2026-09-08`

## Scope

v0.23.0 consolidates the post-v0.22.2 release line. It includes the reviewed platform, security, history, backup, restore, deployment, documentation, and operator-workflow changes recorded in `CHANGELOG.md`.

Release preparation branch: `release/v0.23.0`.

Release source input commit: `c7c9ca2741916c875be58e3f4e51768d79f6623d`.

Public-demo artifact commit: `50ec0d6a4738d7d2cd2045951354c79edfdaa86e`.

Validated executable candidate commit: `f66ab1e9336d41dca0528065fd3d5d0116cbf35d`.

Validated executable candidate tree: `3fe79ee429f5453269dacf99f52130f88c3ba1ef`.

Validated Linux QA image: `sha256:6a2b54ce2b25a2053ecceaf97a110311103b2d56ee7b5e2bf2195ba0d0e83af9`.

Tag: `v0.23.0`, published 2026-09-09 as a non-draft, non-prerelease GitHub
release: https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.23.0.

Validated against `docs/RELEASE_CHECKLIST.md`.

Changelog coverage: pass (136 PRs)

The generated GitHub release-note set includes 139 pull requests: the 136 changelog-governed pull requests plus 3 dependency pull requests. All 139 currently map to `.github/release.yml` categories; 121 historical pull requests were backfilled with the same conventional-title labels used by the current automatic label workflow.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Release preparation used `release/v0.23.0`; tag `v0.23.0` was published from source commit `dbcd2ab31e9a46083693cf08802434ac8cfc1e43` | Pass |  |
| Python unit and syntax gates | yes | Canonical full source gate passed at executable commit `f66ab1e9336d41dca0528065fd3d5d0116cbf35d`: 2,350 Python tests, compileall, bounded Ruff, diff hygiene, performance baseline, and checked public-demo artifact | Pass |  |
| JavaScript syntax gates | yes | All configured JavaScript syntax gates and 242 JavaScript unit tests passed in the canonical full source gate | Pass |  |
| Docker build and health gates | yes | Source-labeled image `sha256:6a2b54ce2b25a2053ecceaf97a110311103b2d56ee7b5e2bf2195ba0d0e83af9` was built from `f66ab1e9336d41dca0528065fd3d5d0116cbf35d`; UI, history, and admin health passed in matrix and restored-stack runs | Pass |  |
| Optional-sidecar runtime matrix | yes | Five variants passed: UI-only, UI+history, admin-only, UI+admin, and UI+history+admin; four alias cycles, four scoped mapping cycles, and one admin setup cycle completed with cleanup | Pass |  |
| Full Playwright/browser gates | yes | Current executable candidate passed 9 restored-stack admin/private browser tests; unchanged UI/browser lanes previously passed 22 live tests with 2 fixture-dependent skips and 11 offline/saved-view tests | Pass |  |
| Feature-specific live API/UI gates | yes | Synthetic operator QA passed system switching, enclosure and saved-view selection, alias save/clear, scoped mapping roundtrip, encrypted FULL export/import, restart survival, and served-origin pencil cleanup | Pass |  |
| Local release perf harnesses | yes | Five no-record iterations passed for all main UI and history workflows, including forced/cached inventory, SMART batch and chunked prefetch, mapping roundtrip, snapshot estimate, estimated counts, and exact counts | Pass |  |
| Linux QA restore gate | yes | Egress-blocked restore of encrypted all-ten-group 7z backup SHA-256 `b72d1c78eb90e77eaa3ec68ae57708a088b543abe98dcfb74a182555b02634cd` passed with exact source/image provenance, aggregate-count reconciliation, two-system metadata, restart survival, and zero restart failures | Pass |  |
| Restored Linux QA perf harnesses | yes | Restored-stack UI and history no-record harnesses passed all workflows over the retained internal network, including mapping import roundtrip, snapshot estimate, and exact history counts | Pass |  |
| Snapshot/export/offline artifact gate | yes | Redacted Force ZIP export produced one offline HTML member; Chromium rendered 60 slots with no network requests, console errors, private endpoints, host paths, credential assignments, or long bare hex values. Later candidate changes were limited to backup/restore state handling | Pass |  |
| Docs/wiki/public-demo gate | yes | Current full source gate found the deterministic public demo publishable at 2,070,273 raw bytes and 930,591 gzip bytes; 47 populated and 13 empty bays, adjacent slots 42 and 43 in one synthetic `spares` group, privacy scans, and two exact-byte screenshot reviews passed | Pass |  |
| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |
| GHCR publish verification | yes | [Publish GHCR workflow run 34293966354](https://github.com/gcs8/truenas-jbod-ui/actions/runs/34293966354) completed successfully for release source `dbcd2ab31e9a46083693cf08802434ac8cfc1e43`; package tags `0.23.0`, `v0.23.0`, and `latest` resolve to manifest `sha256:567d42025850a048186cf35adb2c97c071d27fa93f3071ec0eb3ddd71dd9df17` | Pass |  |
| Deployment refresh/sniff tests | yes | Private deployment qualification remains unverified; it still requires a verified encrypted FULL backup, immutable digest activation, health checks, sniff tests, and rollback evidence | Blocked |  |
| Post-release reopen | yes | Development continued on `main` after the published tag and release | Pass |  |

## Current evidence

- GitHub published tag and release `v0.23.0` on 2026-09-09 from source commit
  `dbcd2ab31e9a46083693cf08802434ac8cfc1e43`.
- GHCR workflow run 34293966354 published package tags `0.23.0`, `v0.23.0`,
  and `latest` on manifest
  `sha256:567d42025850a048186cf35adb2c97c071d27fa93f3071ec0eb3ddd71dd9df17`.
- GitHub Pages deployment 6340637703 published the static public demo from
  repository commit `a98917a4ba21aef06adab7bf7adc2c4bf0663f05`. The external
  wiki still needs a separate synchronized publication.
- The validated executable candidate is commit `f66ab1e9336d41dca0528065fd3d5d0116cbf35d`, tree `3fe79ee429f5453269dacf99f52130f88c3ba1ef`, and Linux QA image `sha256:6a2b54ce2b25a2053ecceaf97a110311103b2d56ee7b5e2bf2195ba0d0e83af9`.
- The encrypted FULL restore included config, runtime overrides, profiles, mappings, SAS Fabric aliases, slot cache, history, SSH keys, TLS trust, and known hosts. Inspection reported all ten groups present, no absent groups, and exact aggregate-count reconciliation after import.
- Restore remediation preserved existing path modes, uses descriptor-owned exclusive copies through ownership, mode, and fsync, records segmented-hot sibling ownership only after successful exclusive creation, and assigns new shared runtime paths mode `0660` for files and `0770` for directories under the existing `${APP_GID}` trust boundary.
- The disposable restore stack, synthetic appliance, backup archive, passphrase, raw evidence, temporary scripts, networks, and reserved ports were removed. `switch-explorer` remained running with its original container and image identities.
- Production-derived input was limited to aggregate structure from the fixed cached UI endpoints: 47 populated bays, 13 empty bays, two adjacent spare slots sharing one pool/vdev/class group, a 2-slot boot view with 2 matches, and a 4-slot NVMe view with 4 matches.
- The checked fixture remains `provenance: synthetic`. Production device names, serials, addresses, hostnames, measurements, credentials, history rows, and raw payloads were not copied into the repository.
- Public screenshots visibly report v0.23.0 and synthetic provenance. Exact image hashes, sizes, dimensions, source revision, and source artifact fingerprint are recorded in `docs/PUBLIC_SCREENSHOT_REVIEW.md` and `docs/images/screenshots/manifest.json`.
- `.github/release.yml` category reconciliation covers Breaking changes, Security, Features, Fixes, Performance, Documentation, Dependencies, and Internal with zero unmatched release-note pull requests.

## Hold boundary

The public `v0.23.0` tag, GitHub release, GHCR package, and Pages demo are
published. Private deployment qualification remains unverified, and the external
wiki remains a separate publication gate. The beginner installation stays pinned
to `v0.22.2` until issue #463 records lifecycle, retention, and rollback
qualification for a newer default.
