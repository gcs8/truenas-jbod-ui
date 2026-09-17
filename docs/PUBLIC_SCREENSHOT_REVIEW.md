# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The pinned Linux capture used `file://`, UTC, a 1920 by 1080 viewport, reduced motion, and no network request. GitHub Actions run `35244766869` captured each image twice and produced byte-identical copies.

Source revision: `d8b6913a869d5855a75271b2807882e33178a834`

Source artifact SHA-256: `8501574912ba152b9ce6eead9ddc4e31954c4c83dd3e53eeeb754709a149e28a`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4092 | 1100120 | `10835307f24c039c58fab81167786493e1681377a27d5abe70ccf54645902d27` | PENDING |
| `public-demo-history.png` | 1920 by 4852 | 1226484 | `89ce1ba3e107705a93311fb5bb3de50128f0643f1424d427d95815b0754800ef` | PENDING |

These are the recaptured bytes for the #538 rebuild; only the Source revision and Build ID cards changed content. Both exact PNGs were loaded and inspected with vision tooling. The review checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. None were visible. Source revision and Build ID are intentional public-repository provenance.

Neither image showed unintended overlap, clipping, missing assets, corrupted text, or canvas-edge artifacts. Narrow bay labels use intentional ellipses. Full selected-slot identifiers remain readable in Slot Details. Equal-height cards leave unused space but do not hide or collide with content.

The selected slot is 57. Its device, serial, persistent ID, health, temperature, pool, vdev, and special-class topology agree across the enclosure, Slot Details, Topology Context, Calibration Mapping, and history panel. The map contains every bay from 00 through 59 exactly once. Its 47 populated and 13 empty bays match Mapping Health.

The checked-in fixture preserves the spare group at slots 42 and 43. Both bays appear populated in the images. Their narrow tile labels are ellipsized, so the spare class is not readable from the screenshot alone; the fixture and public-demo contract tests establish that grouping. The selected special-class peer group at slots 57 through 59 is visibly consistent.

The history image shows five samples from January 8 through January 15, 2026. Its latest, minimum, and maximum temperatures match the chart. Read/write totals, seven-day deltas, rates, capture time, and selected identity agree with the overview. The one aggregate event does not claim to belong to the selected slot.

The frozen, synthetic, offline wording is explicit. Live refresh, calibration, backup, and LED actions are unavailable as expected. The lane's inspection found no blocker in the pixels themselves; the sign-off below is still outstanding. This review does not authorize publication by itself; the exact-byte checker still verifies framing, dimensions, sizes, hashes, docs/Wiki byte identity, artifact identity, source revision, and the `PASS` fields.

Mobile and tablet layouts are unsupported and are not part of this review.

Final pixel verdict: `PENDING`

The recapture above was produced by an automated lane, which may not record `PASS`. The observations in this record are the lane's inspection notes, not the sign-off. A human reviewer has to look at the two exact PNGs and, only then, set `pixel_review` to `PASS` here and in `docs/images/screenshots/manifest.json`. Until that happens `scripts/check_public_screenshots.py` fails on purpose.
