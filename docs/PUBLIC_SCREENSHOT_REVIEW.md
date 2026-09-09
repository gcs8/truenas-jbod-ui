# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The capture script used `file://`, UTC, fixed desktop viewports, reduced motion, and no network request.

Source revision: `76d0c06814b3245fcb6e5509ada05d8be794c151`

Source artifact SHA-256: `364a3bfe1f85609efeeef6ff2682c8d19eed3a3b818602557f8bc3d2db68ab16`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4237 | 978403 | `5cabffaadc0cc2b4dabf55f6edddd460318acb5f66b4862168e59fde021c9dad` | PASS |
| `public-demo-history.png` | 1920 by 5015 | 1094061 | `3f9d78519f804371852d904ae905aaf7214fa4a40b0e6c8519cbfe22ac5eec16` | PASS |

The exact pixels were checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. The review also covered snapshot/live wording, selector clipping, panel overlap, page overflow, canvas-edge artifacts, count grammar, and the consistency of current, history, rate, power-on, and annualized values.

The overview image selects slot 57's special-class mirror. It does not depict the spare group. The fixture and browser gates separately verify that slots 42 and 43 render in the single `demo-capacity > spares > spare` group.

Mobile and tablet layouts are unsupported and are not part of this screenshot review.

No publication blocker remained. Source revision and Build ID are intentionally visible public-repository provenance, not fixture data or credentials.

The byte checker independently verifies PNG framing, dimensions, sizes, hashes, docs/Wiki byte equality, artifact identity, source revision, and the `PASS` fields recorded here.

Final pixel verdict: `PASS`
