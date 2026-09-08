# Public demo site

The public demo is a static, read-only copy of the enclosure UI:

- [Open the public demo](https://gcs8.github.io/truenas-jbod-ui/)

It opens without Docker, a TrueNAS account, an API key, or an SSH connection.
The checked-in file also works directly from disk.

## What is included

The demo uses one deterministic synthetic TrueNAS CORE fixture. It has a 60-bay
top-loading enclosure, two saved or virtual views, preloaded slot history, a
heat-map timeline, and a read-only fabric summary.

Visitors can select slots, inspect disk and topology fields, switch views, open
history, and scrub the heat-map timeline. The page starts with no selected bay
so the full enclosure is visible first.

## What is omitted

The artifact has no target addresses, credentials, SSH material, operator
configuration, history database, logs, cache, backup, or live discovery result.
It cannot refresh a host, change mappings, run setup, operate a locator, sync a
disk inventory, or call an admin route. Those controls are absent, disabled, or
hidden in snapshot mode.

The demo is not a support claim for a chassis, controller, disk, or multipath
layout. It does not replace the Docker application or its history and admin
sidecars.

## Provenance

`tests/fixtures/public_demo/public_demo.json` is the only data input.
`app/services/public_demo_fixture.py` validates the fixture schema and rejects
unknown fields. `scripts/public_demo_inputs.py` lists the complete generator
source graph.

The banner shows two identities:

- `Source revision` is the full Git commit for the declared demo inputs.
- `Build ID` is a deterministic SHA-256 fingerprint of that revision and the
  input hashes.

The leading source-parity manifest records those values plus the artifact and
combined source/output hashes. `scripts/check_public_demo_artifact.py` rejects a
missing input, changed hash, stale source revision, private value, or size-budget
failure.

## Build and verify

When generator inputs change, commit them before generating the artifact:

```bash
SOURCE_COMMIT="$(git rev-parse HEAD)"
python3 scripts/build_public_demo.py \
  --source-revision "$SOURCE_COMMIT" \
  --output public-demo/index.html
python3 scripts/check_public_demo_artifact.py public-demo
```

The screenshot and documentation checks run separately:

```bash
node scripts/capture_public_demo_screenshots.js
python3 scripts/check_public_screenshots.py
python3 scripts/check_public_docs.py
```

The capture script uses only the checked artifact through `file://`. It leaves
the exact-image review status at `PENDING` until someone inspects the recorded
PNG hashes.

## Browser contract

The supported public browser is the current stable Chrome channel on GitHub's
hosted Ubuntu runner. The Playwright suite also runs with local bundled Chromium
when Chrome is unavailable. It covers file mode, a Pages-style subpath, reload,
back and forward navigation, keyboard focus, reduced motion, a 200-percent
desktop zoom-equivalent layout, 1280- and 1920-pixel desktop widths, console
errors, and unexpected requests.

Mobile and tablet layouts are unsupported. Incidental rendering on those
devices is not a public-demo capability or compatibility claim.

Firefox and WebKit are not release gates for this demo.

## Publication

A pull request and a push to `main` verify the checked bytes but do not publish
Pages. A maintainer must run `.github/workflows/publish-public-demo.yml` with
`workflow_dispatch` after merge.

The workflow records the GitHub source SHA and checked artifact SHA-256 before
upload. After deployment it fetches the anonymous Pages URL with bounded retries
and requires an exact byte match. A Chrome check then reloads the deployed route
and rejects failed or cross-origin requests.

Wiki publication is a different manual gate. Publishing one does not publish the
other.

## Rollback

If readback fails, do not patch the deployed page. Revert the bad source or
artifact commit through a reviewed pull request, verify the known-good artifact
locally, merge it with fresh exact-head checks, and run the Pages workflow again.
The post-deploy byte and browser jobs must pass on the new `main` commit.

The full fixture, browser, screenshot, publication, and rollback contract is in
the [public demo product brief](https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/PUBLIC_DEMO_PRODUCT_BRIEF.md).

## Related pages

- [[Quick Start|Quick-Start]]
- [[Visual Tour|Visual-Tour]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
- [[Architecture and Services|Architecture-and-Services]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]]