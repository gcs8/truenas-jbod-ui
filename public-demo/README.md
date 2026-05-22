# Public Demo Artifact

This folder holds the generated static demo entry point for GitHub Pages or any
plain static host. The checked-in demo is built from live-derived TN Core sample
data through the same offline snapshot exporter used by the app, with critical
disk identifiers scrambled consistently across the artifact.

Published site:

- https://gcs8.github.io/truenas-jbod-ui/

The local history database is release input, not a repository fixture. Clean
checkouts and CI validate the checked-in `public-demo/index.html` artifact; they
do not rebuild it from ignored `history/history.db`.

## Clean checkout / CI checks

Check that the committed artifact is safe to publish with:

```powershell
python scripts/check_public_demo_artifact.py public-demo
```

Smoke-test the checked-in static artifact with:

```powershell
$env:PUBLIC_DEMO_ARTIFACT = "public-demo/index.html"
npx playwright test qa/public-demo.spec.js
```

## Release-maintainer regeneration

Regenerating the demo requires a trusted local checkout with ignored
`history/history.db` release input. Do not copy that database into the repo.

Run the local-data generation tests explicitly:

```powershell
$env:PUBLIC_DEMO_LOCAL_HISTORY = "1"
python -m unittest tests.test_public_demo_fixture -v
```

Build or freshness-check the checked-in artifact with:

```powershell
python scripts/build_public_demo.py --output public-demo/index.html
python scripts/build_public_demo.py --output public-demo/index.html --check
python scripts/check_public_demo_artifact.py public-demo
```

To smoke-test a temporary artifact generated from local history, opt in
explicitly:

```powershell
$env:PUBLIC_DEMO_BUILD_FROM_HISTORY = "1"
npx playwright test qa/public-demo.spec.js
```

The GitHub Pages workflow deploys this directory as-is. GitHub-hosted runners
smoke-test the checked-in `public-demo/index.html` file rather than rebuilding
from live data.
