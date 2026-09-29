# Public demo site plan

Date: `2026-05-15`

Status: completed and superseded by
[`PUBLIC_DEMO_PRODUCT_BRIEF.md`](../PUBLIC_DEMO_PRODUCT_BRIEF.md).

The first static Pages path shipped in `v0.19.0`. That release used a scrubbed
live-derived sample. Current `main` no longer uses that input path. It builds
`public-demo/index.html` only from the checked synthetic fixture at
`tests/fixtures/public_demo/public_demo.json`.

The current product brief owns the active contract for:

- intended audience and offline interactions;
- deliberate omissions and privacy boundaries;
- source revision and deterministic Build ID;
- fixture-only screenshots and exact-image review;
- Chrome, viewport, zoom, focus, reduced-motion, and routing checks;
- manual Pages publication, exact-byte readback, and rollback.

Historical release notes and changelog entries still describe what shipped at
their dates. Do not use them as current build instructions.

Active operator guidance lives in:

- [`README.md`](../../README.md)
- [`public-demo/README.md`](../../public-demo/README.md)
- [`wiki/Public-Demo-Site.md`](../../wiki/Public-Demo-Site.md)
- [`wiki/Demo-and-Offline-Workflows.md`](../../wiki/Demo-and-Offline-Workflows.md)
- [`docs/RELEASE_CHECKLIST.md`](../RELEASE_CHECKLIST.md)