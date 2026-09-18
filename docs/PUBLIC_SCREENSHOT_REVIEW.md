# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The pinned Linux capture used `file://`, UTC, a 1920 by 1080 viewport, reduced motion, and no network request. GitHub Actions run `35346046672` captured each image twice and produced byte-identical copies.

Source revision: `bcd76680c7b9e671bf721b8598339714fc77e401`

Source artifact SHA-256: `00c63e2d6f815ce2c5f0c0335649302d0cdf4bf8582650be4f7e1d680400a276`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4092 | 1098276 | `2c23377851effc224a8f7ab4d941dd1f874a4caa7077ed8129a9237314585ca3` | PASS |
| `public-demo-history.png` | 1920 by 4852 | 1224916 | `1a104caa5f5aa7cf30680e6e54cb617e6dd78c80ca23bd44530a1cf63c812d24` | PASS |

These are the exact recaptured bytes for the disk-retention accounting successor to #547 after rebasing onto current main. The source adds identifier-free aggregate inventory fields named `source_disk_count`, `rendered_unique_disk_count`, `duplicate_disk_view_count`, and `unplaced_disk_count`, plus truthful virtual-inventory fallback wording. The aggregate fields are part of the inventory summary API and are not separate human-facing cards in this fixture. This review therefore checks that the generated public artifact remains complete, synthetic, and visually coherent; it does not claim the screenshots alone prove the API values.

Both PNGs were loaded and inspected with Hermes `vision_analyze`. The review checked for private addresses, hostnames, paths, credentials, key material, personal information, and non-demo device identifiers. None were visible. Source revision and Build ID are intentional public-repository provenance.

Neither image showed unintended overlap, clipping, missing assets, corrupted text, or canvas-edge artifacts. Narrow bay labels use intentional ellipses. Full selected-slot identifiers remain readable in Slot Details. Equal-height cards leave unused space but do not hide or collide with content.

The selected slot is 57. Its device, serial, persistent ID, health, temperature, pool, vdev, and special-class topology agree across the enclosure, Slot Details, Topology Context, Calibration Mapping, and history panel. The map contains every bay from 00 through 59 exactly once. Its 47 populated and 13 empty bays match Mapping Health.

The checked-in fixture preserves the spare group at slots 42 and 43. Both bays appear populated in the images. Their narrow tile labels are ellipsized, so the spare class is not readable from the screenshot alone; the fixture and public-demo contract tests establish that grouping. The selected special-class peer group at slots 57 through 59 is visibly consistent.

The history image shows five samples from January 8 through January 15, 2026. Its latest, minimum, and maximum temperatures match the chart. Read/write totals, seven-day deltas, rates, capture time, and selected identity agree with the overview. The one aggregate event does not claim to belong to the selected slot.

An RGB comparison against the previously approved pair found 9,400 changed pixels in each image. The overview difference bounding box was `x=962..1834, y=270..3478`; the history difference bounding box was `x=962..1834, y=270..4234`. Visual inspection of amplified difference images found the substantive changes confined to the Source revision and Build ID text blocks. Each image also had one tiny isolated antialiasing speck at the far-right edge, with no coherent application component or layout change.

The frozen, synthetic, offline wording is explicit. Live refresh, calibration, backup, and LED actions are unavailable as expected. Mobile and tablet layouts are unsupported and are not part of this review.

Final pixel verdict: `PASS`

Ryoko signs off the two exact PNGs and hashes recorded above. This approval applies only to those bytes. Any recapture resets `pixel_review` to `PENDING` and requires a new named review.
