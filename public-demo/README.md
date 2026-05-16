# Public Demo Artifact

This folder holds the generated static demo entry point for GitHub Pages or any
plain static host. The demo is built from synthetic sample data through the same
offline snapshot exporter used by the app.

Build it with:

```powershell
python scripts/build_public_demo.py --output public-demo/index.html
```

Check that the committed artifact is current with:

```powershell
python scripts/build_public_demo.py --output public-demo/index.html --check
```
