# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. GitHub Actions run `35381404566` used the pinned Playwright `v1.63.0-jammy` container, `file://`, UTC, a 1920 by 1080 viewport, reduced motion, and no network request. The job captured each image twice and produced byte-identical copies.

Source revision: `9bb81237378e491eab9540a55d60951b5750723e`

Source artifact SHA-256: `87f51a5a844a355c76e6d0d958927806c14bf63178a5597c94d10afd08c874b8`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4092 | 1099842 | `521e7c1e26c817d33d183a0683d51dcc053495132fef85942b9b0996cf831662` | PASS |
| `public-demo-history.png` | 1920 by 4852 | 1226434 | `fe58baeddf202048ec1e451dfd48170034c336907c69e3e0237be79a58f8c150` | PASS |

These exact images accompany the provenance-only public-demo refresh for the current-source successor to #552. Every declared demo input has the same blob as the previously approved source revision. The generated artifact changes only its source-parity manifest, visible Source revision, and visible Build ID.

Both PNGs were loaded and inspected with Hermes `vision_analyze`. The review checked the changed provenance area for complete values, private data, clipping, overlap, corruption, and malformed text. The Source revision is the complete 40-character public Git commit. The Build ID is the complete 64-character deterministic input-manifest fingerprint. Neither value contains a credential, private path, private address, or personal identifier.

An RGB comparison against the previously approved pair found exactly 8,940 changed pixels and 26,789 changed color channels in each image. Both difference bounding boxes were `x=962..1819, y=270..570`. Every pixel outside that box is unchanged. Visual inspection found the changed pixels confined to the intentional Source revision and Build ID text. No new layout, asset, selected-slot, history, or content change appears in either image.

The selected slot remains 57. Its device, serial, persistent ID, health, temperature, pool, vdev, and special-class topology remain consistent across the enclosure, Slot Details, Topology Context, Calibration Mapping, and history panel. The map still contains every bay from 00 through 59 exactly once, with 47 populated and 13 empty bays. The frozen, synthetic, offline wording remains explicit.

Final pixel verdict: `PASS`

Ryoko signs off the two exact PNGs and hashes recorded above. This approval applies only to those bytes. Any recapture resets `pixel_review` to `PENDING` and requires a new named review.
