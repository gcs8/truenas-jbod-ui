# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. GitHub Actions run `36049972945` used the pinned Playwright `v1.63.0-jammy` container, `file://`, UTC, a 1920 by 1080 viewport, reduced motion, and no network request. The job captured each image twice and produced byte-identical copies.

Source revision: `2288c324d3df1baf2cfeeb4d60d60c816e837cb8`

Source artifact SHA-256: `505568b3660bef9baa2f1fb144d615b761f9edb0bec7b64b47d08de79c9d646a`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4092 | 1099132 | `a5b71d60b647717f4d03b6788de7f17a0f5125f84b25482b3578ba0618f628ca` | PASS |
| `public-demo-history.png` | 1920 by 4852 | 1226115 | `5d4bd72304218b3680ee787a22cf77ac6862f641afb5012b9d9e6602720422f2` | PASS |

These exact images accompany the provenance-only public-demo refresh that rebinds the artifact to main commit `2288c32`; the previously recorded revision was a #552 branch commit the squash merge did not carry onto main. Every declared demo input has the same blob as the previously approved source revision. The generated artifact changes only its source-parity manifest, visible Source revision, and visible Build ID.

Both PNGs were loaded and inspected with Hermes `vision_analyze`. The review checked the changed provenance area for complete values, private data, clipping, overlap, corruption, and malformed text. The Source revision is the complete 40-character public Git commit. The Build ID is the complete 64-character deterministic input-manifest fingerprint. Neither value contains a credential, private path, private address, or personal identifier.

An RGB comparison against the previously approved pair found exactly 9,338 changed pixels and 27,968 changed color channels in each image. Both difference bounding boxes were `x=962..1819, y=270..570`. Every pixel outside that box is unchanged. Visual inspection found the changed pixels confined to the intentional Source revision and Build ID text. No new layout, asset, selected-slot, history, or content change appears in either image.

The selected slot remains 57. Its device, serial, persistent ID, health, temperature, pool, vdev, and special-class topology remain consistent across the enclosure, Slot Details, Topology Context, Calibration Mapping, and history panel. The map still contains every bay from 00 through 59 exactly once, with 47 populated and 13 empty bays. The frozen, synthetic, offline wording remains explicit.

Final pixel verdict: `PASS`

Ryoko signs off the two exact PNGs and hashes recorded above. This approval applies only to those bytes. Any recapture resets `pixel_review` to `PENDING` and requires a new named review.
