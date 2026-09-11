# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The capture script used `file://`, UTC, fixed desktop viewports, reduced motion, and no network request.

Source revision: `1f5769c0364a3cd7bb1ec3cf7d711b78278c6f1a`

Source artifact SHA-256: `d495e4f84e0de139daca1889656f0ae07f46da8f8f9f1574f85850356f1c7609`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4216 | 971585 | `22f475ba55d820e927e3b8f0a13d92e06d51f70c0d34c85b435dee5ab310faf8` | PASS |
| `public-demo-history.png` | 1920 by 4994 | 1090033 | `865f60826c850e00afb4ad9e4a7d8ce6f25a846651943643d193ac0672f96a9c` | PASS |

The exact pixels were checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. The review also covered snapshot/live wording, selector clipping, panel overlap, page overflow, canvas-edge artifacts, count grammar, and the consistency of current, history, rate, power-on, and annualized values.

The overview image selects slot 57's special-class mirror. It does not depict the spare group. The fixture and browser gates separately verify that slots 42 and 43 render in the single `demo-capacity > spares > spare` group.

Mobile and tablet layouts are unsupported and are not part of this screenshot review.

No publication blocker remained. Source revision and Build ID are intentionally visible public-repository provenance, not fixture data or credentials.

The byte checker independently verifies PNG framing, dimensions, sizes, hashes, docs/Wiki byte equality, artifact identity, source revision, and the `PASS` fields recorded here.

Final pixel verdict: `PASS`
