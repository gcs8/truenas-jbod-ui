# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The capture script used `file://`, UTC, fixed desktop viewports, reduced motion, and no network request.

Source revision: `ba5497bd4c57d94b55a75677ae18800d743135a6`

Source artifact SHA-256: `c924b1d6d40a9a52ac7cf94c44f114b63317da45edc59155361bec3876f342ce`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4104 | 1123527 | `22ebb96c3a8b9005015e1538df9edea56dd817e73bc0d142b094667fa9461d25` | PASS |
| `public-demo-history.png` | 1920 by 4860 | 1253884 | `38623889865f669ada1fddc143f7a0c17b5ced06b4f621a23120ceaae9286cd8` | PASS |

The exact pixels were checked for private addresses, hostnames, paths, credentials, key material, and non-demo device identifiers. The review also covered snapshot/live wording, selector clipping, panel overlap, page overflow, canvas-edge artifacts, count grammar, and the consistency of current, history, rate, power-on, and annualized values.

The overview image selects slot 57's special-class mirror. It does not depict the spare group. The fixture and browser gates separately verify that slots 42 and 43 render in the single `demo-capacity > spares > spare` group.

Mobile and tablet layouts are unsupported and are not part of this screenshot review.

Both exact images were inspected. No visible private identifiers, panel overlap,
or uncontrolled overflow were found. Existing desktop presentation caveats remain:
bay labels are abbreviated, full-page images need full-resolution viewing, and
topology colors are separate from health indicators. Disabled refresh and mapping
controls remain visible alongside explicit frozen/offline notices. Artifact-wide
summary and event totals are not selected-bay totals. These observations do not
reopen the accepted layout or change the synthetic snapshot contract.

No publication blocker remained. Source revision and Build ID are intentionally visible public-repository provenance, not fixture data or credentials.

The byte checker independently verifies PNG framing, dimensions, sizes, hashes, docs/Wiki byte equality, artifact identity, source revision, and the `PASS` fields recorded here.

Final pixel verdict: `PASS`
