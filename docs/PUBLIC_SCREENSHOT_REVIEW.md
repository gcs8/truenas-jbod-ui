# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The pinned Linux capture used `file://`, UTC, a 1920 by 1080 viewport, reduced motion, and no network request. GitHub Actions run `35277884579` captured each image twice and produced byte-identical copies.

Source revision: `e567a65ffd5483a6574b664815ce5d0fae462317`

Source artifact SHA-256: `f5d4a54aaede6ab9867c8301bb81585f0326dbb885782df58e437954d40b86bb`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4092 | 1097758 | `ee3cffff77cd9e27bd6682eb8fab55b5e1e058bcae2f9733dbacd5dabf8e71ab` | PASS |
| `public-demo-history.png` | 1920 by 4852 | 1224675 | `675b9521eeabfb8d4a5219adcbaa2cd772d956ea98805cc65fe06ab8642e2d0d` | PASS |

These are the recaptured bytes for the #524 rebuild after merging main, which brought in #538, #529, #539, #540 and #541. `app/models/domain.py` gained `SlotView.identity_state`, so the embedded snapshot payloads carry one extra field per slot; no rendered card reads that field, so only the Source revision and Build ID cards changed visible content. Both exact PNGs were loaded and inspected by the lane that produced them. These are the lane's inspection notes, not the sign-off. The review checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. None were visible. Source revision and Build ID are intentional public-repository provenance.

Neither image showed unintended overlap, clipping, missing assets, corrupted text, or canvas-edge artifacts. Narrow bay labels use intentional ellipses. Full selected-slot identifiers remain readable in Slot Details. Equal-height cards leave unused space but do not hide or collide with content.

The selected slot is 57. Its device, serial, persistent ID, health, temperature, pool, vdev, and special-class topology agree across the enclosure, Slot Details, Topology Context, Calibration Mapping, and history panel. The map contains every bay from 00 through 59 exactly once. Its 47 populated and 13 empty bays match Mapping Health.

The checked-in fixture preserves the spare group at slots 42 and 43. Both bays appear populated in the images. Their narrow tile labels are ellipsized, so the spare class is not readable from the screenshot alone; the fixture and public-demo contract tests establish that grouping. The selected special-class peer group at slots 57 through 59 is visibly consistent.

The history image shows five samples from January 8 through January 15, 2026. Its latest, minimum, and maximum temperatures match the chart. Read/write totals, seven-day deltas, rates, capture time, and selected identity agree with the overview. The one aggregate event does not claim to belong to the selected slot.

The frozen, synthetic, offline wording is explicit. Live refresh, calibration, backup, and LED actions are unavailable as expected. Ryoko inspected both exact PNGs with Hermes `vision_analyze`. The complete visual review found no unintended clipping, overlap, missing assets, corrupted text, exposed private material, or inconsistent fixture data. An independent RGB pixel comparison against the previously approved pair found 9,521 changed pixels in each image, all inside the provenance-card region at x=962..1819 and y=270..570; every pixel outside that region was identical. The Source revision and Build ID cards are fully visible, readable, contained, and non-overlapping.

Mobile and tablet layouts are unsupported and are not part of this review.

Final pixel verdict: `PASS`

Ryoko signs off the two exact PNGs and hashes recorded above. This approval applies only to those bytes. Any recapture resets `pixel_review` to `PENDING` and requires a new named review.
