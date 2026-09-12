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
| Capture settings | `scripts/capture_public_demo_screenshots.js`: `file://` only, 1920 by 1080 viewport, dark scheme, UTC, reduced motion |

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
that report, not against `fc-match`. Both are empty until the first dispatched
run records them; set them in the workflow `env` block afterwards, so a later
image change fails the job instead of quietly changing the pixels.

## Running it before the workflow is merged

A `workflow_dispatch` workflow cannot be dispatched while it exists only on a
pull request branch. GitHub's documentation is explicit:

- "This event will only trigger a workflow run if the workflow file exists on
  the default branch."
- "To trigger the `workflow_dispatch` event, your workflow must be in the
  default branch."
- "On the GitHub UI, the "Run workflow" button will be present if the workflow
  file exists on the default branch. Once a workflow has run at least once, you
  can dispatch it against any branch or tag via the GitHub API or GitHub CLI."

So `gh workflow run capture-public-demo-screenshots.yml --ref <branch>` does not
work from this pull request branch alone: `--ref` chooses which ref is checked
out and which version of the workflow file runs, but the trigger itself is only
registered from the copy on the default branch, and the "Run workflow" button
does not appear at all. While the file lives only on the branch, GitHub has no
registered `workflow_dispatch` trigger for it and rejects the dispatch; the same
applies to the REST endpoint `POST
/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches`, whose docs say
"You must configure your GitHub Actions workflow to run when the
workflow_dispatch webhook event occurs."

This has not been tested by dispatching, and there is no way to test it without
first putting the file on a default branch somewhere. Two ways to get a first
run, for the owners to choose between:

1. **Fork.** Push the branch to a fork and make it the fork's default branch.
   The workflow is then dispatchable there, against any ref of that fork, and
   nothing lands in this repository until the owners are satisfied.
2. **Merge the workflow first as a no-op tooling change.** The workflow is
   dispatch-only, has `permissions: contents: read`, and no `push`,
   `pull_request`, or `schedule` trigger, so merging it changes no gate and
   starts no run. Dispatch comes afterwards, deliberately.

Neither option should be read as a recommendation to merge in order to find out
whether the workflow works. Option 2 is a decision to accept the file on main
before its first run; option 1 keeps that decision open.

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
   restoring the checkout between the runs. If any PNG differs between run 1 and
   run 2, the job fails. That comparison is the reproducibility proof, and a
   failure means the environment is not deterministic yet, not that the images
   are wrong.
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

## What the human still approves

Everything that decides publication:

- Look at the two downloaded PNGs. The pixel review checklist is in
  [`PUBLIC_SCREENSHOT_REVIEW.md`](PUBLIC_SCREENSHOT_REVIEW.md): private
  addresses, hostnames, paths, key material, non-demo identifiers, clipping,
  overlap, overflow, wording, and value consistency.
- Only then change `pixel_review` from `PENDING` to `PASS`. No script and no
  workflow may write `PASS`, and nobody may edit a hash in `manifest.json` by
  hand to make a gate green.

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
