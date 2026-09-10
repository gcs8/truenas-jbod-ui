# Public screenshot exact-byte review

The repository capture script rendered two synthetic public-demo screenshots with file URLs, UTC and fixed desktop viewports. It reported no unexpected network requests.

Source revision: `3b53f542ef13c7dfc9066b535b1134d13a8dd821`

Source artifact SHA-256: `0da26acd91c76d2f5c75ac5081675e2365bb25178a019025e04667856c3f9922`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---|---|---|---|
| `public-demo-overview.png` | 1920 by 4104 | 1123303 | `12f5409fd6c463643dbfae0f367d2951f9851ce4996785865b831a0fcfd21783` | PASS |
| `public-demo-history.png` | 1920 by 4860 | 1253375 | `bea3afb62e79e739f819aaf807bc84a5f37dcbafcacccad8d4940bd28415e6a0` | PASS |

Both exact PNGs received actual pixel inspection through vision tooling after capture. No blocking overlap, viewport clipping or missing sections was found. Frozen, offline and synthetic disclosures remain visible. The history screenshot contains populated metric summaries and charts. Missing Fabric and multipath data are explicitly explained. No apparent private identifiers or credentials were visible; source revision and build identifiers are public provenance, not invented disk identifiers.

Existing presentation limitations remain: the long details column and equal-height panels leave excess vertical whitespace; compact bay labels are dim and ellipsized; chart axes have sparse labels. The global event count and selected-bay empty event list refer to different scopes. This artifact refresh does not redesign the layout.

The overview selects slot 57. Spare-group behavior is verified by separate fixture/browser tests, not by this selected-disk screenshot. Mobile and tablet layouts are unsupported. This record is static pixel QA, not live or interactive acceptance.

The checker binds PNG sizes, dimensions, hashes, docs/Wiki copies, source artifact and this review. Final pixel verdict: `PASS`. No public site or external Wiki was published.
