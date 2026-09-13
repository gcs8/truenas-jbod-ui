# Release Notes - v0.23.0

v0.23.0 is the first release after the v0.22.x segmented-history series. It combines the post-v0.22.2 platform, security, backup, history, deployment, and operator-workflow work into one reviewed release.

## Highlights

- The main and admin UIs run without application authentication by default for short-lived LAN use. Basic authentication, explicit browser origins, and private CA verification remain opt-in hardening controls.
- The default Compose stack runs the UI and history services as non-root with read-only image filesystems after a one-time ownership preflight.
- Segmented history now supports crash-safe later-generation rotation, interrupted-restore recovery, packaged lifecycle tools, bounded queries, and backup-readable immutable segments.
- Backup inspection and restore use bounded archive processing, encryption provenance, single-use server receipts, streamed staging, and measured low-heap duplicate-key tracking.
- Dell MD1280 shelves have validated full-chassis and per-drawer profiles. TrueNAS, QuantaStor, Linux, ESXi, mapping, Storage Fabric, and diagnostics paths received focused correctness and privacy fixes.
- The static public demo is generated from schema-validated synthetic data. Its aggregate layout follows the latest production shape, including two synthetic spares in one `spares` group, without copying production identifiers or measurements.
- Release notes now use `.github/release.yml` categories. Historical post-v0.22.2 pull requests were backfilled with the same conventional-title labels used by the current automatic workflow.

## Upgrade notes

- Existing `network` authentication mode now treats network reachability as authorization. Anyone who can reach the published main or admin port can use its controls. Keep those ports limited to trusted users or configure Basic authentication and exact public origins.
- New storage connections default to certificate verification disabled. Existing explicit settings remain unchanged. Enable verification and supply a trusted CA bundle when the appliance certificate is not already trusted.
- The hardened Compose file requires a one-time ownership preflight before first start. Stop the stack and run `scripts/prepare_nonroot_bind_mounts.py` first in dry-run mode, then with `--apply`, using the configured `APP_UID` and `APP_GID`.
- Do not run the generic ownership helper recursively over an existing segmented-history tree. Follow the bounded repair procedure in the Backup, Restore, and Debug Bundles guide so immutable segments remain non-writable.
- Rebuild or repull the complete image instead of upgrading dependencies in place.

## Release process

The v0.23.0 tag and GitHub release were published on 2026-09-09 after the
recorded pre-tag checks. The GHCR package and Pages demo are also published.
Private deployment qualification remains unverified, and the external Wiki still
needs a synchronized publication. The release wrap records those remaining gates;
the beginner installation stays pinned to v0.22.2 until lifecycle qualification
supports a newer default.
