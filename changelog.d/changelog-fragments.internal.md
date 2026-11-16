A pull request now records its changelog entry as one file under
`changelog.d/` instead of editing `CHANGELOG.md`, so open pull requests no
longer conflict there. `scripts/changelog_fragments.py collect` writes the
entries into `CHANGELOG.md` when a release is prepared, numbering each from the
commit that merged it, and the release tools read the fragments for unreleased
work.
