# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The pinned Linux capture used `file://`, UTC, a 1920 by 1080 viewport, reduced motion, and no network request. GitHub Actions run `35245478228` captured each image twice and produced byte-identical copies.

Source revision: `1f40e1fd64ee53b0705904a32246d89cd01962e8`

Source artifact SHA-256: `c828afc68ebf878cf2261f50a5cd96f94f7057915c6f0aedf61af8a792dc4822`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4092 | 1099955 | `1630def93e525fd20a38cb2c1e44277afc50858efcb31e156c5cdcc92e17b068` | PASS |
| `public-demo-history.png` | 1920 by 4852 | 1226672 | `7bd2f983457639a1d6fd6870d9e9220244d6df92f80bc4e68ed4cc081085ef48` | PASS |

These are the recaptured bytes for the #529 rebuild on top of #538; only the Source revision and Build ID cards changed content. Both exact PNGs were loaded and inspected with vision tooling. The review checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. None were visible. Source revision and Build ID are intentional public-repository provenance.

Neither image showed unintended overlap, clipping, missing assets, corrupted text, or canvas-edge artifacts. Narrow bay labels use intentional ellipses. Full selected-slot identifiers remain readable in Slot Details. Equal-height cards leave unused space but do not hide or collide with content.

The selected slot is 57. Its device, serial, persistent ID, health, temperature, pool, vdev, and special-class topology agree across the enclosure, Slot Details, Topology Context, Calibration Mapping, and history panel. The map contains every bay from 00 through 59 exactly once. Its 47 populated and 13 empty bays match Mapping Health.

The checked-in fixture preserves the spare group at slots 42 and 43. Both bays appear populated in the images. Their narrow tile labels are ellipsized, so the spare class is not readable from the screenshot alone; the fixture and public-demo contract tests establish that grouping. The selected special-class peer group at slots 57 through 59 is visibly consistent.

The history image shows five samples from January 8 through January 15, 2026. Its latest, minimum, and maximum temperatures match the chart. Read/write totals, seven-day deltas, rates, capture time, and selected identity agree with the overview. The one aggregate event does not claim to belong to the selected slot.

The frozen, synthetic, offline wording is explicit. Live refresh, calibration, backup, and LED actions are unavailable as expected. The lane's inspection found no blocker in the pixels themselves; the sign-off below is still outstanding. This review does not authorize publication by itself; the exact-byte checker still verifies framing, dimensions, sizes, hashes, docs/Wiki byte identity, artifact identity, source revision, and the `PASS` fields.

Mobile and tablet layouts are unsupported and are not part of this review.

Final pixel verdict: `PASS`

Ryoko inspected both exact PNGs bound above. The complete visual review found no unintended clipping, overlap, missing assets, corrupted text, exposed private material, or inconsistent fixture data. An independent pixel comparison against the previously approved pair confined every changed pixel to the Source Revision and Build ID cards. The manifest records this exact-byte approval.
