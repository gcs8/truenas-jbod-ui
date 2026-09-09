# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The capture script used `file://`, UTC, fixed desktop viewports, reduced motion, and no network request.

Source revision: `8fcbd03c165aac894a98885dd21efebd84d17dd8`

Source artifact SHA-256: `29a87a3a35986e9f3d91dac5dadc0c2e6fa0bd6fa0d26ce662200d0133cdf383`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4237 | 977896 | `6f77ac7ee3fc7162513778b113dff431d255d9a51acd2184788e1e0a3114fe13` | PASS |
| `public-demo-history.png` | 1920 by 5015 | 1093513 | `f2b26ce83023bc14124b902bf4e0a3e172f26c7edcee83ea53a5dddd944f446a` | PASS |

The exact pixels were checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. The review also covered snapshot/live wording, selector clipping, panel overlap, page overflow, canvas-edge artifacts, count grammar, and the consistency of current, history, rate, power-on, and annualized values.

The overview image selects slot 57's special-class mirror. It does not depict the spare group. The fixture and browser gates separately verify that slots 42 and 43 render in the single `demo-capacity > spares > spare` group.

Mobile and tablet layouts are unsupported and are not part of this screenshot review.

No publication blocker remained. Source revision and Build ID are intentionally visible public-repository provenance, not fixture data or credentials.

The byte checker independently verifies PNG framing, dimensions, sizes, hashes, docs/Wiki byte equality, artifact identity, source revision, and the `PASS` fields recorded here.

Final pixel verdict: `PASS`
