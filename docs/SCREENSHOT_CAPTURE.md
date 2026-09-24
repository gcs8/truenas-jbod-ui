# Recapturing the public demo screenshots

`docs/images/screenshots/manifest.json` binds the two public-demo PNGs to the
checked-in demo artifact byte for byte. Any pull request that changes a declared
demo input eventually needs a fresh capture, and the fresh bytes have to come
from one pinned environment. This page is the only supported way to produce
them.

## Why the capture is Linux only

The demo page asks for `"Segoe UI", Tahoma, Geneva, Verdana, sans-serif` and
`"Cascadia Mono", SFMono-Regular, Consolas, monospace`. None of those faces ship
on Linux, so every glyph comes from the fontconfig fallback. A capture on
Windows or macOS uses different faces, different rasterisation, and therefore a
different full-page height and different PNG bytes. Measured on the same
artifact, a Windows capture produced pages 133 px and 155 px taller and PNGs
about 13 percent smaller than the committed images. Those bytes are not wrong,
they are just from another machine, and committing them republishes the public
screenshots in one contributor's font rendering.

So the capture runs on `ubuntu-latest` inside the Playwright container image,
never on a workstation.

## What is pinned

| Thing | Pin |
|---|---|
| Container image | `mcr.microsoft.com/playwright:v1.62.1-jammy@sha256:b3251f7ff1a9fa559a28d1c67eaa15fc1a9800f7845e82756caea7842967f615` |
| Playwright package | `package-lock.json`, installed with `npm ci --ignore-scripts` |
| Browser build | the Chromium inside that image |
| Fonts | the font packages inside that image, listed in the run log |
| Demo artifact | the checked-in `public-demo/index.html` at the dispatched ref |
| Capture settings | `scripts/capture_public_demo_screenshots.js`: `file://` only, 1920 by 1080 viewport, dark scheme, UTC, reduced motion, pointer moved off controls, focus cleared, two settled animation frames |

The image is pinned by digest, not by tag alone. A tag in a registry can be
moved to new bytes, and a moved tag would change the fonts or the Chromium build
without changing anything in this repository. The workflow keeps the
human-readable tag in a comment, in the `CONTAINER_TAG` variable, and in the run
log, and `tests/test_ci_contract.py` fails if the `container.image` pin loses its
`@sha256:` digest. Record a new digest without Docker:

```bash
curl -sSI -H "Accept: application/vnd.oci.image.index.v1+json" \
  https://mcr.microsoft.com/v2/playwright/manifests/v1.62.1-jammy |
  grep -i docker-content-digest
```

The tag list is at `https://mcr.microsoft.com/v2/playwright/tags/list`.

The container tag and the locked Playwright version must match. The job reads
the version out of `package-lock.json` and fails if the tag has drifted, because
`npm ci --ignore-scripts` does not download browsers and relies on the ones
already in the image. When Dependabot raises `@playwright/test`, raise the tag in
`.github/workflows/capture-public-demo-screenshots.yml` in the same pull
request.

The job prints the fallback family that `fc-match` resolves for `sans-serif` and
`monospace`, plus every installed font package and its version. That output is
informational: `fc-match` answers a fontconfig question, and the renderer is free
to pick something else for the glyphs that end up in the PNG.

The families that decide the pixels come from Chromium itself.
`scripts/report_public_demo_platform_fonts.js` loads the demo artifact over
`file://`, waits for the page to render, and asks the DevTools protocol
(`CSS.getPlatformFontsForNode`) which platform fonts were used for a body text
node and for a monospace node. It prints each family with its glyph count and
writes `platform-fonts.json` into the artifact. `EXPECTED_SANS_FALLBACK` and
`EXPECTED_MONO_FALLBACK` are compared against the family with the most glyphs in
that report, not against `fc-match`.

Both are pinned in the workflow `env` block, and the job fails when the family
Chromium used no longer matches:

| Variable | Pinned family |
|---|---|
| `EXPECTED_SANS_FALLBACK` | `Liberation Sans` |
| `EXPECTED_MONO_FALLBACK` | `WenQuanYi Zen Hei Mono` |

Qualification run `34734456858` recorded both under the container digest pinned
above; #516 moved them into the workflow. Do not edit either value to make a red
capture green: a mismatch means the image, its fonts, or the Chromium build
changed, so the pixels changed too. Re-qualify instead - dispatch a
`qualification_only` run against the new pin, read the dominant families out of
its `platform-fonts.json`, and update the workflow `env` block, this table, and
the container digest in one pull request. `tests/test_ci_contract.py` reads the
families out of the workflow and fails if this table drifts from them.

## Dispatching the workflow

The workflow is on `main` (#512), so it can be dispatched against any ref:

```bash
gh workflow run capture-public-demo-screenshots.yml -R gcs8/truenas-jbod-ui \
  --ref main -f ref=<full commit SHA> -f qualification_only=false
```

`--ref main` picks the workflow definition; the `ref` input picks the commit
that is checked out and captured. Always pass a full commit SHA as `ref`, never
a branch name.

## The first run is a tooling qualification

The first dispatched run exists to qualify the tooling, not to produce images for
the manifest. Give `ref` a specific full commit SHA on `main`, never a branch
name: a branch moves, and a capture that cannot be tied to immutable content
proves nothing later. Leave `qualification_only` at its default `true`. The run
then skips `proposed-manifest.json` and uploads
`public-demo-screenshot-qualification` instead of
`public-demo-screenshot-candidate`, so its output cannot be mistaken for a
candidate.

What that run does and does not establish:

- It shows that the container, the locked Playwright, the fonts, and the capture
  script produce the same bytes twice **in that environment**. That is
  repeatability, and it is all the two-capture comparison can prove.
- It does **not** show that those bytes match the currently approved screenshots,
  and it is not evidence for or against the committed PNGs. The approved images
  were captured elsewhere; comparing across environments is exactly the thing
  this page says not to do.
- It records the platform font families and the resolved image digest, which is
  what `EXPECTED_SANS_FALLBACK` and `EXPECTED_MONO_FALLBACK` are set from.

Only after that qualification, and only when a maintainer is deliberately
producing images to commit, is the workflow run with `qualification_only` set to
`false`.

## How a maintainer produces a candidate

1. Push the branch that changed the demo input and its rebuilt
   `public-demo/index.html`.
2. Run the **Capture Public Demo Screenshots** workflow with
   `workflow_dispatch`, set `ref` to the full commit SHA of that branch head, and
   set `qualification_only` to `false`.
3. The job checks out the ref, installs the locked dependencies, verifies the
   fonts, then runs `node scripts/capture_public_demo_screenshots.js` twice,
   restoring the checkout between the runs. After each scripted interaction,
   the capture moves the pointer off controls, clears focus, and waits for two
   animation frames. This removes transient hover and focus rendering without
   changing the application or rewriting captured pixels. If any PNG differs
   between run 1 and run 2, the job fails. That comparison is the
   reproducibility proof, and a failure means the environment is not
   deterministic yet, not that the images are wrong.
4. The job runs `python scripts/check_public_screenshots.py --report`, which
   prints the manifest entries the captured bytes would need. Report mode reads
   no manifest and writes no file, and it always records
   `"pixel_review": "PENDING"`.
5. Download the artifact: `public-demo-screenshot-candidate` when
   `qualification_only` was `false`, `public-demo-screenshot-qualification` when
   it was `true`. It holds the two PNGs, `run-1/` and `run-2/` for the
   comparison, `platform-fonts.json`, `capture.log`, and, on a candidate run,
   `proposed-manifest.json`. It also holds `sha256sums.txt` for checking the two
   top-level PNGs after download. Retention is 14 days.

The job builds no artifact, commits nothing, pushes nothing, and opens no pull
request. It runs with `permissions: contents: read`.

## What the reviewer approves

Everything that decides publication:

- Look at the two downloaded PNGs. The pixel review checklist is in
  [`PUBLIC_SCREENSHOT_REVIEW.md`](PUBLIC_SCREENSHOT_REVIEW.md): private
  addresses, hostnames, paths, key material, non-demo identifiers, clipping,
  overlap, overflow, wording, and value consistency.
- Only then change `pixel_review` from `PENDING` to `PASS`. `PASS` means a
  named reviewer inspected the exact hash-bound PNG bytes. The reviewer may be
  a person or disclosed vision tooling; a tool review is recorded as a tool
  review in `PUBLIC_SCREENSHOT_REVIEW.md` and never described as a human one.
  Two-run byte reproducibility is required but does not replace looking at the
  images. No script and no workflow may write `PASS`, and nobody may edit a
  hash in `manifest.json` by hand to make a gate green.

## How the result is committed

1. Copy the two PNGs from the artifact over `docs/images/screenshots/` and
   `wiki/images/`. Both copies must be byte-identical.
2. Copy the `images`, `source_artifact_sha256`, and `source_revision` values from
   `proposed-manifest.json` into `docs/images/screenshots/manifest.json`.
3. Update the table, the source revision, and the source artifact SHA-256 in
   `docs/PUBLIC_SCREENSHOT_REVIEW.md`, and set `pixel_review` to `PASS` in the
   manifest only after the review above.
4. Run the gates locally:

   ```bash
   python scripts/check_public_screenshots.py
   python scripts/check_public_docs.py
   ```

5. Commit the images, the manifest, and the review record together, on the same
   branch as the artifact rebuild.

## Merging a pull request that rebuilt the artifact

`public-demo/index.html` records the commit it was built from, and
`scripts/check_public_demo_artifact.py` requires that commit to be an ancestor
of `HEAD`. A squash merge copies the branch's content onto `main` as one new
commit and leaves the branch commits behind, so the recorded revision is no
longer reachable and the check fails on `main` with `recorded public demo
source revision is not a local commit` (the failure #559 repaired).

So a pull request that changes `public-demo/**`, `docs/images/screenshots/**`,
`wiki/images/**` or any path in `scripts/public_demo_inputs.py` is merged with a
**merge commit**, never squash or rebase. Every other pull request may be
squashed. If a squash happens anyway, rebuild with `--source-revision` set to a
commit on `main` whose declared inputs match the artifact, as #559 did.

The order inside such a pull request is fixed:

1. Merge current `main` into the branch and commit the source change.
2. `python scripts/build_public_demo.py --output public-demo/index.html
   --source-revision <full SHA of that source commit>` and commit the result.
3. Dispatch the capture against the artifact commit, review, and commit the
   images, manifest and review record as described above.
4. Merge with a merge commit. A later commit that changes a declared demo input
   or `public-demo/index.html` invalidates the capture and the review, and the
   chain starts again from step 1. Committing the reviewed images and review
   record in step 3, or merging a `main` that touches neither, does not.

Only one demo-input pull request should run this chain at a time: two branches
rebuilt from different sources cannot both merge without one of them starting
again.
