# Public Demo Artifact

This folder holds the generated static demo entry point for GitHub Pages or any
plain static host. The demo is built from live-derived TN Core sample data
through the same offline snapshot exporter used by the app, with critical disk
identifiers scrambled consistently across the artifact.

Published site:

- https://gcs8.github.io/truenas-jbod-ui/

Build it with:

```powershell
python scripts/build_public_demo.py --output public-demo/index.html
```

Check that the committed artifact is current with:

```powershell
python scripts/build_public_demo.py --output public-demo/index.html --check
```

Check that the committed artifact is safe to publish with:

```powershell
python scripts/check_public_demo_artifact.py public-demo
```

The GitHub Pages workflow deploys this directory as-is. GitHub-hosted runners
do not rebuild the demo from live data; they smoke-test the checked-in
`public-demo/index.html` file.
