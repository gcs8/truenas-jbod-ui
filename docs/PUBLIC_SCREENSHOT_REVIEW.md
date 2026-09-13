# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The capture script used `file://`, UTC, fixed desktop viewports, reduced motion, and no network request.

Source revision: `e216f95c32446e21b3a27253c09be5b7e4a16b0f`

Source artifact SHA-256: `6921490bbf79df5f8eecb3e30f6ab376759d61f38ba254f8964c45d671f3f519`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4104 | 1123930 | `c01a9c1ed228bb7942cf43129d5b6b2ce23fd72b99e3c3495691ed372293a0f2` | PASS |
| `public-demo-history.png` | 1920 by 4860 | 1254272 | `6eabf96beb3c996357943b2913c5f1e96d914b4154715a58cd9033a1263b3911` | PASS |

Both exact PNGs were loaded and inspected with vision tooling before setting the two manifest review attestations. No additional crops were needed. The review covered private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers; none were visible. Source revision and Build ID are intentional public-repository provenance, not credentials.

Neither image showed unintended panel overlap, page overflow, selector clipping, or canvas-edge artifacts. Narrow bay labels use intentional ellipses; full selected-slot identifiers remain readable in Slot Details. The tall details column leaves unused space without collisions.

The frozen/offline and at-capture wording is explicit. Disabled refresh and calibration controls retain live-oriented labels, a minor presentation ambiguity rather than a claim of live capability. Their disabled state is covered separately by browser checks. No source or UI changes were made for this observation.

Both images select slot 57's special-class mirror, not the spare group. Selected device, serial, persistent ID, topology peers, capture timestamps, and mapping counts agree across panels. The history image repeats the detail values and shows the preserved temperature range, five samples, seven-day deltas and rates. Lifetime annualized values are distinct from the seven-day rate; power-on days are displayed as completed days. Aggregate event and slot/view counts have broader scope than the selected slot and are not independently established by pixels alone.

Mobile and tablet layouts are unsupported and are not part of this screenshot review.

No pixel-review blocker remained. This review is not authorization for publication. The byte checker independently verifies PNG framing, dimensions, sizes, hashes, docs/Wiki byte equality, artifact identity, source revision, and the `PASS` fields recorded here.

Final pixel verdict: `PASS`
