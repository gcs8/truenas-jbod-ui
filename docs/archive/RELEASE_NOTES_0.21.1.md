# Release Notes - v0.21.1

Date: `2026-06-12`

`v0.21.1` is a narrow post-publish hotfix for the `v0.21.0` history-noise cleanup. During the post-GHCR deployment refresh, the long-running source stack exposed one more false-event class: two UNVR Pro slots briefly disappeared from an inventory snapshot and reappeared on the next pass, which produced paired `Identity Change` and `Topology Change` rows even though the disks had not actually moved or changed identity.

## Fixed

- History event generation now treats present/absent slot flaps as state-only transitions for durable event history.
- `Identity Change`, `Topology Change`, and multipath-change rows are no longer emitted solely because a slot crosses the present/absent boundary during a transient incomplete snapshot.
- Stable present-to-present identity changes still produce identity events, so real disk replacement without an intermediate empty observation remains visible.
- The long-running source stack was cleaned again after a SQLite backup; the 8 post-`v0.21.0` UNVR Pro presence-flap identity/topology rows were removed while preserving state-change rows.

## Validation

- Full Python unit discovery passed `513` tests with `4` skipped after adding the presence-flap regression.
- JavaScript syntax/npm gates passed.
- A local `0.21.1` Docker image booted UI, history, and admin sidecars with `/livez` and `/healthz` returning `status=ok` and UI version `0.21.1`.
- The `.138` long-running source stack was rebuilt from the hotfix source, cleaned, and refreshed; after a recovery fast refresh it reported `status=ok`, `last_error=null`, and `0` same-day `slot_identity_changed`/`slot_topology_changed` rows.

## Upgrade Note

Deploy `v0.21.1` instead of `v0.21.0` for the release candidate. Existing databases that already received the post-`v0.21.0` presence-flap rows require the same surgical cleanup pattern used in the release handoff; do not delete state-change rows or metrics.
