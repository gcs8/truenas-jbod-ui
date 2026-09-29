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
- [ ] I did not rebuild `public-demo/**` or recapture screenshots unless this is a
      release pull request. The demo is rebuilt once per release with
      `python scripts/build_public_demo.py --output public-demo/index.html --source-revision <full-source-commit>`
      (see `docs/RELEASE_CHECKLIST.md`, "Public demo rebuild").
- [ ] If README, Wiki, demo, or screenshot files changed, I ran
      `python scripts/check_public_docs.py`,
      `python scripts/check_public_screenshots.py`, and the relevant Playwright
      tests. Screenshot pixel review is bound to the exact manifest hashes.
