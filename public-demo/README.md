# Public demo artifact

This directory contains the generated static demo served by GitHub Pages. The
demo uses only the schema-validated fixture at
`tests/fixtures/public_demo/public_demo.json`. Every system, enclosure, disk,
identifier, metric, and event in that fixture was invented for this repository.
The normal build never reads operator config, local history, caches, logs, or a
live system.

Published site:

- https://gcs8.github.io/truenas-jbod-ui/

## Regenerate and verify

A clean checkout can reproduce the artifact:

```bash
python scripts/build_public_demo.py --output public-demo/index.html
python scripts/build_public_demo.py --output public-demo/index.html --check
python scripts/check_public_demo_artifact.py public-demo
PUBLIC_DEMO_ARTIFACT=public-demo/index.html npx playwright test qa/public-demo.spec.js
```

The builder records SHA-256 fingerprints for the complete semantic input graph
in the artifact. The checker verifies those fingerprints, the source version,
embedded JavaScript and CSS, privacy rules, removed-code rules, and size limits.
Change fixture data in the checked-in JSON, not in generated HTML. Do not edit
`public-demo/index.html` by hand.

Running the builder locally does not publish anything. Pull requests and pushes
to `main` run the verification job but do not deploy. Publishing the exact
checked-in directory to GitHub Pages requires a separately approved
`workflow_dispatch` run of `.github/workflows/publish-public-demo.yml`. Commit,
push, merge, Pages publication, and public readback remain separate approval and
verification gates.

Local-history conversion is not part of normal generation. If maintainers add a
future conversion tool, it must require explicit opt-in, write the bounded
public fixture, and stop before artifact regeneration or publication so the
fixture bytes can be reviewed first.
