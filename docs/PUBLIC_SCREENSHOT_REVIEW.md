# Public screenshot exact-byte review

The two screenshots were captured from the synthetic public demo using the repository capture script, file URLs, UTC and fixed desktop viewports. The capture rejected external network requests.

Source revision: `f05f8062512820bcddf89becb162d2591b71fbb5`

Source artifact SHA-256: `e875d9d5ede286a41d94fdaa95e0e4f3aa717092347b7a487ff13676ffa4c95b`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---|---:|---|---|
| `public-demo-overview.png` | 1920 by 4104 | 1123607 | `df72191d7c2e178afd88da1657dd3765eadbfc62e3702353e46983d5d896c187` | PASS |
| `public-demo-history.png` | 1920 by 4860 | 1253926 | `baf8a665579debc5b633f3588db92ad1d2d115d80d77d56593e60891b68e43bd` | PASS |

Both exact images were visually reviewed. No blocking text overlap, clipped panels or unexplained missing content was found. Frozen, offline and synthetic disclosures are visible. The history image contains populated metric summaries and charts. Unavailable Fabric and multipath information has explicit explanations.

Minor presentation limitations remain: long details create excess vertical whitespace, hashes wrap narrowly, and bay labels are small. These are not claims of new regressions or live runtime health. No layout redesign was included in this history-response fix.

The overview selects slot 57. Spare-group behavior remains covered by the separate fixture/browser tests, not by this selected-disk screenshot. Mobile and tablet layouts are unsupported.

The checker verifies PNG framing, dimensions, sizes, hashes, docs/Wiki byte equality, source artifact identity and this review record. Final pixel verdict: `PASS`. No site or external Wiki publication was performed.
