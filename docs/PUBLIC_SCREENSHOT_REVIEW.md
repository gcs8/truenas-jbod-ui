# Public screenshot exact-byte review

The repository capture script rendered two synthetic public-demo screenshots with file URLs, UTC, fixed desktop viewports and external requests rejected.

Source revision: `b637e8719d2b3c8f35e2f4168aeaa4e6a9b3a66a`

Source artifact SHA-256: `62efa185c25e1e738cbdbea307b0be84c8ab1093fdab1d5a172717a2b763cc68`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---|---:|---|---|
| `public-demo-overview.png` | 1920 by 4104 | 1123719 | `efc00625b9b32334247b2616d33caf8845d413a7672d78ec5a2188e1ce95e0e5` | PASS |
| `public-demo-history.png` | 1920 by 4860 | 1254051 | `225ceac8dfdd3ef27f5672d86e0dc4859c758658d07bc71faa88ab2c24c61890` | PASS |

Both exact images were visually reviewed. No blocking overlap, viewport clipping or missing sections was found. Frozen, offline and synthetic disclosures remain visible. The history screenshot contains populated metric summaries and charts. Missing Fabric or multipath data is explicitly explained rather than presented as success.

Existing presentation limitations remain: the long details column and equal-height panels leave excess vertical whitespace; compact bay labels are ellipsized. This patch does not redesign the layout. Source revision and build identifiers are public provenance.

The overview selects slot 57. Spare-group behavior is verified by separate fixture/browser tests, not by this selected-disk screenshot. Mobile and tablet layouts are unsupported. This record is static pixel QA, not live or interactive acceptance.

The checker binds PNG sizes, dimensions, hashes, docs/Wiki copies, source artifact and this review. Final pixel verdict: `PASS`. No public site or external Wiki was published.
