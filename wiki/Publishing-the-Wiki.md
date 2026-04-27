# Publishing the Wiki

This repo keeps a GitHub-wiki-ready copy of the pages in the `wiki/` folder so
they can be maintained like normal project docs first.

## Basic Publish Flow

Clone the GitHub wiki repo:

```bash
git clone <your-github-repo>.wiki.git repo-wiki
```

Copy the page set in:

```bash
cp wiki/*.md repo-wiki/
mkdir -p repo-wiki/images
cp wiki/images/* repo-wiki/images/
```

Commit and push:

```bash
cd repo-wiki
git add .
git commit -m "Refresh wiki pages"
git push
```

## Recommended Maintainer Workflow

- treat the repo `wiki/` folder as the source of truth
- treat `wiki/images/` as the source of truth for wiki-embedded screenshots
- review changes in normal PRs
- publish to GitHub Wiki after the docs look right

## Refresh Screenshots Before A Release-Oriented Publish

If the release changed operator-facing flows, regenerate the tracked screenshot
set before you copy `wiki/images/` into the GitHub wiki repo.

From the repo root in PowerShell:

```powershell
$env:SCREENSHOT_TAG='v0.15.0'
.\.venv\Scripts\python.exe scripts\capture_readme_screenshots.py
.\.venv\Scripts\python.exe scripts\capture_history_export_screenshots.py
.\.venv\Scripts\python.exe scripts\capture_release_workflow_screenshots.py
```

That refreshes the repo screenshots under `docs/images/screenshots/` and the
wiki-facing copies under `wiki/images/`.

## Good Times To Refresh The Wiki

- after a release
- after a new platform guide lands
- after a profile system change
- after a setup flow becomes simpler or safer

For the full repo release flow, use:

- [`docs/RELEASE_CHECKLIST.md`](../docs/RELEASE_CHECKLIST.md)
