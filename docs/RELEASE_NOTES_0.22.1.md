# Release Notes - v0.22.1

Date: `2026-08-30`

`v0.22.1` is a narrow maintenance patch on top of `v0.22.0`. It fixes the FULL encrypted backup and restore path for history databases larger than the v0.22.0 in-memory restore limits.

## Fixed

- Encrypted 7z imports extract and validate `manifest.json` before payload extraction, then keep payload members file-backed through SHA-256 verification, SQLite integrity checking, and transactional staging.
- The 7z `history_db` allowance is 4 GiB per member and 6 GiB across expanded members. The larger bound applies only when the manifest declares the canonical, selected, present `history_db` group and member; ZIP, TAR, and non-history 7z members retain the existing limits.
- Transaction staging copies file-backed members without loading them into Python memory.
- 7z extraction workspaces remain available through validation and transaction staging, then are removed before live activation. Cleanup failures prevent activation and fail closed with a fixed error.

## Regression coverage

The focused suite covers:

- a sparse, valid SQLite history member sized beyond the observed production database;
- chunked SHA-256 verification without `Path.read_bytes()`;
- file-to-file transaction staging;
- encrypted 7z export and import with extraction-root cleanup;
- rejection of oversized non-history members;
- rejection of forged or noncanonical `history_db` members before payload extraction;
- cleanup-failure handling;
- sabotage of the path handoff back to byte materialization.

## Upgrade note

Upgrade from `v0.22.0` before using the admin sidecar to create or restore a FULL encrypted backup whose history database exceeds 1.5 GiB. The patch does not intentionally change inventory collection, Storage Fabric presentation, history retention, deployment routing, or public-demo data.

## Validation status

The release will not be tagged until `docs/RELEASE_WRAP_0.22.1.md` passes the strict pre-tag validator and the published candidate completes a FULL encrypted development restore using a history database larger than the observed production database.
