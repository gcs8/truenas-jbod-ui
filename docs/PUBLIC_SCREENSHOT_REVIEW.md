# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The pinned Linux capture used `file://`, UTC, a 1920 by 1080 viewport, reduced motion, and no network request. GitHub Actions run `34811222108` captured each image twice and produced byte-identical copies.

Source revision: `b2e78c1ae4694e1df5c36f946404532ba4bd53f4`

Source artifact SHA-256: `9409e159a87f9d78f8d7d8afdb3518be637e295c4feb55585d65dd229b9e35de`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4092 | 1099561 | `6b0949d78424a8e2bcb4d0080ccbb7bef0525a212f1be69fb71df1fa85d893d0` | PASS |
| `public-demo-history.png` | 1920 by 4852 | 1226073 | `127e205d138eec86af1069fd05bdb503daabed3d154dd022835854878453dc40` | PASS |

Both exact PNGs were loaded and inspected with vision tooling before setting the manifest attestations. The review checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. None were visible. Source revision and Build ID are intentional public-repository provenance.

Neither image showed unintended overlap, clipping, missing assets, corrupted text, or canvas-edge artifacts. Narrow bay labels use intentional ellipses. Full selected-slot identifiers remain readable in Slot Details. Equal-height cards leave unused space but do not hide or collide with content.

The selected slot is 57. Its device, serial, persistent ID, health, temperature, pool, vdev, and special-class topology agree across the enclosure, Slot Details, Topology Context, Calibration Mapping, and history panel. The map contains every bay from 00 through 59 exactly once. Its 47 populated and 13 empty bays match Mapping Health.

The checked-in fixture preserves the spare group at slots 42 and 43. Both bays appear populated in the images. Their narrow tile labels are ellipsized, so the spare class is not readable from the screenshot alone; the fixture and public-demo contract tests establish that grouping. The selected special-class peer group at slots 57 through 59 is visibly consistent.

The history image shows five samples from January 8 through January 15, 2026. Its latest, minimum, and maximum temperatures match the chart. Read/write totals, seven-day deltas, rates, capture time, and selected identity agree with the overview. The one aggregate event does not claim to belong to the selected slot.

The frozen, synthetic, offline wording is explicit. Live refresh, calibration, backup, and LED actions are unavailable as expected. No pixel-review blocker remained. This review does not authorize publication by itself; the exact-byte checker still verifies framing, dimensions, sizes, hashes, docs/Wiki byte identity, artifact identity, source revision, and the `PASS` fields.

Mobile and tablet layouts are unsupported and are not part of this review.

Final pixel verdict: `PASS`
