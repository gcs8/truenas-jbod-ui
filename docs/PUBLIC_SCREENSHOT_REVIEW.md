# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The capture script used `file://`, UTC, fixed desktop viewports, reduced motion, and no network request.

Source revision: `9b2f9d930ae2f021aa5a937bd64b2f779483ba29`

Source artifact SHA-256: `c86aacc684bf5291487f1246914232a46f2a7c162ba0f2b81f2a01f3f4116bb7`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4237 | 977906 | `4b5071a91b392bf314411db6dcfd83de7dd1ce702434f7ac45971d488f8214f5` | PASS |
| `public-demo-history.png` | 1920 by 5015 | 1093463 | `31eeb3de13367e76852ba1e585f77217e7b2defe2217596c6d79210cd5d46e7d` | PASS |

The exact pixels were checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. The review also covered snapshot/live wording, selector clipping, panel overlap, page overflow, canvas-edge artifacts, count grammar, and the consistency of current, history, rate, power-on, and annualized values.

The overview image selects slot 57's special-class mirror. It does not depict the spare group. The fixture and browser gates separately verify that slots 42 and 43 render in the single `demo-capacity > spares > spare` group.

Mobile and tablet layouts are unsupported and are not part of this screenshot review.

No publication blocker remained. Source revision and Build ID are intentionally visible public-repository provenance, not fixture data or credentials.

The byte checker independently verifies PNG framing, dimensions, sizes, hashes, docs/Wiki byte equality, artifact identity, source revision, and the `PASS` fields recorded here.

Final pixel verdict: `PASS`
