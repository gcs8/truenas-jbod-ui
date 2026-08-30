# Release Notes - v0.22.0

Date: `2026-08-30`

`v0.22.0` closes the performance, deployment-safety, CI, backup, and operator-trust cycle that followed `v0.21.2`. It keeps the existing platform and root-runtime compatibility while adding opt-in hardening and stronger release evidence.

## Changed

- Added an immutable deployment transaction that binds a source revision to a GHCR digest, records the exact Compose project/file/profile/service contract, verifies runtime convergence, and preserves automatic and operator-requested rollback evidence.
- Added opt-in non-root runtime support, bounded ownership migration, explicit history permission repair, scoped file-backed secrets, and safer container defaults without forcing existing root deployments to migrate.
- Added deterministic 60-slot and 347-slot performance budgets across inventory, history, query, export, and cache paths.
- Reduced inventory SSH fanout, repeated slot-detail loads, browser cache growth, full-grid rebuilds, and snapshot-export copy/memory work.
- Expanded CI to Python 3.12 and 3.14, Ruff, production container smoke, JavaScript/npm integrity, admin browser QA, public-demo artifact checks, and CodeQL for Python and JavaScript.
- Added bounded aggregate Prometheus metrics and six starter alert rules for required services, collector freshness/failure, retry backoff, SMART failures, and sustained high temperature.

## Fixed

- Mapping import now previews the exact scoped diff and rejects stale writes.
- Hidden tabs stop polling, dirty calibration edits are protected across navigation, and stale async responses cannot overwrite the active scope.
- Mapping health now separates matched, empty, unmatched, and unknown bays and uses non-color cues.
- Storage Fabric now labels retained evidence as `STALE`, enabled source failures or materially limited evidence as `PARTIAL`, and complete healthy evidence as `OK`.
- Admin runtime actions wait for observed convergence, classify unavailable health correctly, and always release controls after failures or cancellation.
- Backup and restore paths have stronger archive, manifest, size, integrity, and transactional rollback checks. Portable encrypted 7z passphrases no longer appear in child-process arguments, and bounded FULL backups now support history databases above 1 GiB with a 10-minute archive-operation budget.
- History retention, adoption/remap, permission publication, derived keys, and degraded-topology handling are covered by stronger lifecycle tests.
- Diagnostic payloads and event samples are byte/count bounded, and the checked-in public demo has explicit route, freshness, and size gates.

## Compatibility and upgrade notes

- Existing root-based Compose deployments remain supported. The non-root overlay is opt-in and should be tested against the deployment's bind-mount ownership before activation.
- Keep the admin sidecar on a trusted local/LAN path. Auto-stop limits exposure but is not authentication, and the Docker socket grants host-level container authority.
- Deploy the full immutable `ghcr.io/gcs8/truenas-jbod-ui@sha256:...` reference from the release workflow receipt. Version tags and `latest` are mutable pointers.
- This release does not add ESXi RAID-management actions, public admin exposure, or inferred physical topology beyond available evidence.

## Validation status

The candidate will not be tagged until `docs/RELEASE_WRAP_0.22.0.md` passes the repository's mandatory pre-tag validator. Post-publish GHCR, development/production deployment, rollback, and development-reopen evidence are recorded in that wrap after publication.
