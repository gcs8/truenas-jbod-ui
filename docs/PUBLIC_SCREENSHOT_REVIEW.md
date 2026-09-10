# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. The capture script used `file://`, UTC, fixed desktop viewports, reduced motion, and no network request. Both exact PNG files were inspected with image-review tooling before setting the manifest review fields to PASS.

Source revision: `c456648fbfb0535eb5edf95a1a3fa62148b4c346`

Source artifact SHA-256: `8faa27d5af1ebc415f1023a69f6eb531609561ee397ec793ff95b91b1db73711`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 4104 | 1124091 | `72550bb35c93305d0e4433a87bdf0e88665b33fe2b8cca4fee54b13048b999be` | PASS |
| `public-demo-history.png` | 1920 by 4860 | 1254295 | `18c5e6842cb4f90fc4dbe30dea4038c7ba2eab82a45a502ff20b1380a97a300c` | PASS |

The images show synthetic demo identities, frozen/offline disclosures, disabled live actions, and capture-scoped health information. No visible private addresses, credentials, local paths, or non-demo hardware identifiers were found. No unintended page-edge clipping, panel overlap, or control collision was visible. Selected slot 57 is consistent across details, history, topology, and calibration. The enclosure occupancy agrees with the 47 populated and 13 empty summary.

The overview image selects slot 57's special-class mirror; it does not depict the spare group. Storage Fabric data is explicitly unavailable in this offline artifact. The history image shows preloaded synthetic metrics, not live collection.

Visible caveats: these full-page images need native-resolution or zoomable presentation; small bay labels are intentionally ellipsized, secondary text is muted, and long provenance hashes wrap. The tall detail column leaves unused space. Health badges in isolated crops could look live, so retain the synthetic/offline context when presenting them. These are presentation limitations, not new clipping or provenance failures; no layout or source changes were made during artifact review.

Mobile and tablet layouts are unsupported and are not part of this screenshot review. Source revision and Build ID are intentional repository provenance, not fixture data or credentials. Their presence does not authorize publication of this local candidate.

The byte checker independently verifies PNG framing, dimensions, sizes, hashes, docs/Wiki byte equality, artifact identity, source revision, and the PASS fields recorded here. Dimensions and byte counts above come from the generated manifest, not visual estimates.

Final pixel verdict: `PASS`

This verdict covers native-resolution desktop reference use. Independent combined-candidate review and publication approval remain separate gates.
