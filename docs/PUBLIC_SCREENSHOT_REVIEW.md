# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The capture script used `file://`, UTC, fixed desktop viewports, reduced motion, and no network request.

Source revision: `9524960a2d3b11a64b984caf28ff190772b6b346`

Source artifact SHA-256: `611d52247fb9a0f90b663a72c13fec363b55c2a912c55b655b7a91618f16a759`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4237 | 977552 | `725a5d7699968721f25a3e3fc75b6f209babf682a889386ecc940cd06b9db242` | PASS |
| `public-demo-history.png` | 1920 by 5015 | 1093610 | `13ce301dd8d4e35b5c380842682b9c7e4693604ab02c68cba4e4470bd29e9c5d` | PASS |

The exact pixels were checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. The review also covered snapshot/live wording, selector clipping, panel overlap, page overflow, canvas-edge artifacts, count grammar, and the consistency of current, history, rate, power-on, and annualized values.

The overview image selects slot 57's special-class mirror. It does not depict the spare group. The fixture and browser gates separately verify that slots 42 and 43 render in the single `demo-capacity > spares > spare` group.

Mobile and tablet layouts are unsupported and are not part of this screenshot review.

No publication blocker remained. Source revision and Build ID are intentionally visible public-repository provenance, not fixture data or credentials.

The byte checker independently verifies PNG framing, dimensions, sizes, hashes, docs/Wiki byte equality, artifact identity, source revision, and the `PASS` fields recorded here.

Final pixel verdict: `PASS`
