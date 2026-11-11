# Release Notes - v0.24.0-beta.8

v0.24.0-beta.8 is a testing prerelease after v0.24.0-beta.7. Its only
user-visible change is the state-chip correction in #1153.

## Highlights

- A bay's state chip no longer covers its slot number, heat-map value,
  LED or latch, including rotated values and dense face layouts.
- If no shared clear position fits, chips are placed per bay. A smaller
  marker or accessible-only state keeps the underlying content visible.
- The UI crawler now reports a chip painted over bay text.

## Before you test it

Upgrading from v0.24.0-beta.7 requires no operator action beyond the normal
image update. Compose files, settings, environment variables, data formats
and appliance commands are unchanged. If upgrading from an earlier version,
read its intervening upgrade notes in `CHANGELOG.md` too.

This beta will use fixed image tags `v0.24.0-beta.8`, `0.24.0-beta.8` and
`dev`; `latest` stays on v0.23.0. The stable in-app update channel is unchanged.
The public demo and its screenshots wait for the stable release.

## Qualification scope

This candidate follows the owner-approved narrow-beta policy
D-TNJ-20261111-NARROWBETA. Long gates are deferred, not waived, until the
v0.24.0 rc/final full qualification. The release wrap records executed gates
separately from deferred and blocked work.

## Release process

`docs/RELEASE_WRAP_0.24.0-beta.8.md` records the candidate's checklist evidence.
This source preparation does not mean the beta has been published or deployed.
