# Roadmap

`v0.23.0` is the latest published release, published on 2026-09-09. See the
[GitHub release](https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.23.0),
[`docs/RELEASE_NOTES_0.23.0.md`](./RELEASE_NOTES_0.23.0.md), and
`CHANGELOG.md` for what shipped.

The milestone-by-milestone history from `v0.2` through `v0.22.2`, including the
older execution plans it links, lives in
[`docs/archive/ROADMAP_HISTORY.md`](./archive/ROADMAP_HISTORY.md). Those pages
are historical records, not active scope.

## Next lane

Development continues on `main` after the `v0.23.0` tag. The next lane should
stay practical and incremental:

- keep richer platform-native Storage Fabric enrichment in small validated
  slices
- keep weaker SAS/backplane/platform inferences visibly labeled instead of
  turning them into fake certainty

Open work is tracked in the GitHub issue list, and every merged change lands
under `## Unreleased` in `CHANGELOG.md` before the next release cut.

## Guiding principle

Keep the app focused on:

- slot identity
- LED identify control
- physical-disk situational awareness
- practical operator workflows across a small number of validated platforms

Avoid turning it into a full storage analytics or appliance-management suite.

## Longer-term ideas

- broader chassis-profile sharing and import/export
- safer, more portable Linux SES control rules
- additional platforms only after the adapter and profile boundaries are proven
- optional richer topology visualization once the current compact context view
  stops being enough
- richer public demo fixtures once the static site proves useful
