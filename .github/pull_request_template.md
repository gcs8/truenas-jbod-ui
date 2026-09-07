## Summary

-

## Validation

- [ ] I ran the relevant checks from `CONTRIBUTING.md`.
- [ ] I used only synthetic, sanitized, or public fixture data in committed changes.

## Public demo and publication

- [ ] I checked whether this pull request changes `public-demo/**`. A pull request
      and later push to `main` verify those bytes but do not deploy GitHub Pages.
      Publication requires a separately approved `workflow_dispatch` run of
      `.github/workflows/publish-public-demo.yml`.
- [ ] If demo inputs changed, I regenerated `public-demo/index.html` only with
      `python scripts/build_public_demo.py --output public-demo/index.html` and
      reviewed the fixture and generated-byte boundaries separately.
