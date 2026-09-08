# Public demo product brief

## Purpose

The public demo lets a prospective operator inspect the enclosure UI without connecting a storage system. It is a single self-contained HTML file served by GitHub Pages. The file also opens directly from disk.

The demo answers a narrow question: does the physical enclosure view, slot detail, saved-view selector, history panel, heat map, and read-only fabric summary make sense before installation?

## Audience

The intended reader runs or evaluates direct-attached storage and wants to see the operator workflow first. The demo is also the public fixture used by maintainers for screenshot, accessibility, desktop-layout, and Pages publication checks.

It is not a remote administration console. Do not use it to judge whether a specific chassis, controller, disk, multipath layout, or account policy is supported.

This is a desktop operator application. Mobile and tablet layouts are unsupported. A page that happens to render on a phone or tablet is incidental behavior, not a product capability, compatibility promise, or release gate.

## Included interactions

The checked artifact supports these offline actions:

- select the synthetic 60-bay enclosure and two saved or virtual views;
- select populated and empty slots with pointer or keyboard input;
- inspect model, serial, pool, temperature, and topology fields;
- open the preloaded history panel and change its metric or time window;
- turn on heat-map mode and scrub the preloaded timeline;
- open the read-only Storage Fabric summary that explains why live refresh is unavailable.

The artifact makes no API request. Refresh, setup, export, disk-sync, mapping, locator, and LED controls are absent, disabled, or hidden. Browser tests fail if an offline interaction contacts another origin or exposes an actionable live-only control.

## Deliberate omissions

The demo excludes all credentials, target addresses, SSH material, operator configuration, history databases, logs, caches, backup data, and live discovery results. It also excludes current target health, write authorization, and support claims for any private deployment.

A successful demo check proves only that the checked public fixture renders and behaves as documented. It does not prove a production deployment, live TrueNAS response, or hardware compatibility.

## Fixture strategy

`tests/fixtures/public_demo/public_demo.json` is the only data input. Its schema allows one synthetic TrueNAS CORE system, a synthetic 60-bay enclosure, deterministic disks and gaps, two synthetic views, preloaded history, and a bounded fabric summary. Identifiers use explicit `demo-*` and `DEMO-*` forms.

`app/services/public_demo_fixture.py` rejects unknown fields and values that do not match the fixture contract. `scripts/build_public_demo.py` ignores ambient app configuration and local runtime state. `scripts/public_demo_inputs.py` declares the complete source graph. The generated artifact embeds a hash for every declared input.

## Source and build identity

The banner shows two different identities:

- `Source revision` is the full Git commit that contains the declared demo inputs.
- `Build ID` is a deterministic SHA-256 fingerprint of that revision and the declared input hashes.

The leading source-parity manifest also records the artifact hash and the combined source/output hash. `scripts/check_public_demo_artifact.py` checks every field, requires the recorded source revision to be an ancestor of the current checkout, and rejects any declared input changed after that revision.

The repository uses two commits when generator inputs change. The first commit freezes the source graph. The next commit adds the artifact generated with `--source-revision` set to that first commit. This avoids claiming that a file contains the Git commit that already contains the same file.

## Screenshot provenance

`scripts/capture_public_demo_screenshots.js` opens only `public-demo/index.html` through `file://`. It blocks and records non-file requests, uses fixed desktop viewports and reduced motion, and writes two PNGs:

- `public-demo-overview.png`
- `public-demo-history.png`

`docs/images/screenshots/manifest.json` records the source artifact hash, source revision, image dimensions, byte counts, and SHA-256 values. The Wiki copies must be byte-identical. The capture script leaves pixel review at `PENDING`; a reviewer changes it to `PASS` only after inspecting the exact recorded bytes.

[`PUBLIC_SCREENSHOT_REVIEW.md`](PUBLIC_SCREENSHOT_REVIEW.md) records the exact
hashes and final pixel decision for the checked-in images.

## Supported desktop browser and layout matrix

The supported public-demo browser is the current stable Chrome channel used by GitHub's hosted Ubuntu runner. The artifact is also checked with bundled Playwright Chromium during local development when Chrome is unavailable. Firefox and WebKit are not release gates for this static demo.

The browser suite checks these literal viewports:

| Viewport | Purpose |
|---|---|
| 1280 by 720 | Short desktop layout |
| 1920 by 1080 | Standard wide desktop layout |

The same suite checks the 640-CSS-pixel layout equivalent of a 1280-pixel desktop window at 200 percent browser zoom, keyboard focus, accessible names, reduced-motion preference, visible focus, page overflow, same-origin requests, console errors, file mode, and a Pages-style `/truenas-jbod-ui/` subpath. It does not test or claim phone or tablet support. The product makes no promise for obsolete browsers or script-disabled mode.

The artifact has no local asset or API dependency to fail at runtime. The static
checker rejects a new `src`, stylesheet, media, or frame reference, and the
browser gate requires zero file-mode requests. Source-parity and deployment
tests reject malformed, missing, changed, oversized, or non-HTML artifact bytes
before publication.

## Build and local verification

Use a committed source revision when generator inputs changed:

```bash
SOURCE_COMMIT="$(git rev-parse HEAD)"
python3 scripts/build_public_demo.py \
  --source-revision "$SOURCE_COMMIT" \
  --output public-demo/index.html
python3 scripts/check_public_demo_artifact.py public-demo
```

The screenshot and documentation gates are separate:

```bash
node scripts/capture_public_demo_screenshots.js
python3 scripts/check_public_screenshots.py
python3 scripts/check_public_docs.py
```

Do not set screenshot pixel review to `PASS` before reviewing the exact PNG hashes.

## Publication and readback

A pull request and a push to `main` verify bytes but do not publish Pages. A maintainer must run `.github/workflows/publish-public-demo.yml` with `workflow_dispatch` after the reviewed commit merges.

The workflow records `${GITHUB_SHA}` and the checked artifact SHA-256 before upload. After deployment, `scripts/check_public_demo_deployment.py` fetches the anonymous Pages URL with bounded retries and requires an exact byte match. A Chrome check then loads the deployed subpath, reloads it, confirms visible provenance, and rejects failed or cross-origin requests.

The Wiki has its own manual publication gate. Pages success does not publish Wiki changes, and Wiki success does not publish Pages.

## Revert and republish

If Pages readback fails, stop. Do not edit the deployed artifact in place.

1. Keep the failed deployment run and exact hashes as evidence.
2. Revert the bad source or artifact commit through a reviewed pull request.
3. Run the local artifact, docs, screenshot, and browser gates against the revert candidate.
4. Merge only after fresh exact-head checks.
5. Run the Pages workflow manually at the new `main` commit.
6. Require the post-deploy exact-byte and browser jobs to pass.

The release checklist includes a local revert simulation. It materializes the chosen known-good commit in a temporary worktree, runs the artifact checker there, and removes the worktree. The simulation proves that the selected repository state can rebuild and verify. It does not change Pages.
