# Public demo artifact

`index.html` is the checked-in GitHub Pages artifact. It is a frozen,
self-contained snapshot built from `tests/fixtures/public_demo/public_demo.json`
and the source graph in `scripts/public_demo_inputs.py`.

The fixture is synthetic. The build does not read app configuration, credentials,
history databases, caches, logs, or live systems.

## Build

If a generator input changed, commit that source first and pass its full commit:

```bash
SOURCE_COMMIT="$(git rev-parse HEAD)"
python3 scripts/build_public_demo.py \
  --source-revision "$SOURCE_COMMIT" \
  --output public-demo/index.html
```

The banner and leading manifest record that source revision plus a separate
deterministic Build ID. The Build ID fingerprints the declared input hashes and
must not be described as a Git commit.

## Verify

```bash
python3 scripts/build_public_demo.py \
  --output public-demo/index.html \
  --check
python3 scripts/check_public_demo_artifact.py public-demo
python3 scripts/check_public_docs.py
python3 scripts/check_public_screenshots.py
```

The complete browser command also needs a current-source synthetic fixture:

```bash
slot_focus_artifact="$(mktemp "${TMPDIR:-/tmp}/truenas-jbod-ui-slot-focus-XXXXXX.html")"
trap 'rm -f -- "$slot_focus_artifact"' EXIT
python3 scripts/build_current_source_browser_fixture.py --output "$slot_focus_artifact"
PUBLIC_DEMO_ARTIFACT=public-demo/index.html \
SLOT_FOCUS_ARTIFACT="$slot_focus_artifact" \
npx playwright test qa/public-demo.spec.js --retries=0
```

The tests cover offline file mode, a Pages-style subpath, Chrome-compatible
interaction, phone through wide-desktop layouts, keyboard focus, reduced motion,
zoom, forbidden live controls, console errors, and unexpected requests.

## Publish

A local build does not publish anything. Pull requests and pushes to `main`
verify the artifact but do not deploy it. A
maintainer must run `.github/workflows/publish-public-demo.yml` with
`workflow_dispatch`.

After deployment, the workflow fetches the anonymous Pages URL and requires an
exact byte match with `public-demo/index.html`. It then loads the published
subpath in Chrome and rejects failed or cross-origin requests.

If readback fails, revert through a reviewed pull request and republish the new
`main` commit. Do not patch Pages in place.

See `docs/PUBLIC_DEMO_PRODUCT_BRIEF.md` for the fixture, identity, screenshot,
browser, publication, and rollback contract.