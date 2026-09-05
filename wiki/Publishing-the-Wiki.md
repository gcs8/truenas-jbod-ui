# Publishing the wiki

The `wiki/` directory is the reviewed source for GitHub Wiki pages. The files
under `wiki/images/` are the reviewed source for wiki images.

Publishing is an owner-approved action.
The verification command does not publish or change the external wiki.
It reads committed Git objects and compares the root Markdown pages plus the
complete `images/` tree byte for byte.

## Verify an external wiki commit

Supply full 40-character commit IDs for both repositories. A local external-wiki
checkout keeps the check deterministic and does not need credentials:

```bash
repo_commit="$(git rev-parse HEAD)"
wiki_commit="$(git -C repo-wiki rev-parse HEAD)"
python scripts/verify_wiki_drift.py \
  --repository . \
  --repository-commit "$repo_commit" \
  --wiki-source repo-wiki \
  --external-wiki-commit "$wiki_commit"
```

The source may also be the public Git URL. The verifier clones it into a
temporary directory and deletes that clone when the check finishes:

```bash
python scripts/verify_wiki_drift.py \
  --repository . \
  --repository-commit "$repo_commit" \
  --wiki-source https://github.com/gcs8/truenas-jbod-ui.wiki.git \
  --external-wiki-commit "$wiki_commit"
```

A passing check prints an exact receipt:

```text
Wiki drift verification: PASS
Repository commit: <sha>
External wiki commit: <sha>
Compared files: <count>
```

Missing, extra, or changed page and image bytes fail the check. Missing Git
sources, abbreviated or malformed commit IDs, non-regular files, and unreadable
objects fail closed as errors. Uncommitted files in either checkout are ignored
because the verifier reads only the named commits.

## Owner publish flow

1. Freeze and review the repository commit that will supply `wiki/`.
2. Clone the GitHub wiki repository into `repo-wiki`.
3. Run the verifier against its current commit. If it reports drift, review that
   exact diff before copying files.
4. Replace the root Markdown pages and `images/` tree in `repo-wiki` with the
   files from the frozen repository commit. Remove stale pages and images rather
   than leaving extra files behind.
5. Commit the external wiki change locally and run the verifier against the new
   external commit. It must pass before push.
6. The owner approves and pushes that exact external commit.
7. Run the verifier again with the public Git URL and the pushed commit. Record
   its four-line receipt in the release wrap.

The verifier never runs `git add`, `git commit`, or `git push`. Publication stays
manual even when the release gate reports drift.

## Refresh screenshots before a release-oriented publish

If the release changed operator-facing flows, regenerate the tracked screenshot
set before copying `wiki/images/` into the GitHub wiki repository.

From the repository root in PowerShell, use the release tag being prepared:

```powershell
$env:SCREENSHOT_TAG='vX.Y.Z'
.\.venv\Scripts\python.exe scripts\capture_readme_screenshots.py
.\.venv\Scripts\python.exe scripts\capture_history_export_screenshots.py
.\.venv\Scripts\python.exe scripts\capture_release_workflow_screenshots.py
```

This refreshes the repository screenshots under `docs/images/screenshots/` and
the wiki copies under `wiki/images/`.

For the full release flow, use
[`docs/RELEASE_CHECKLIST.md`](../docs/RELEASE_CHECKLIST.md).
