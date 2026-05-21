# Release Notes - v0.20.2

Date: `2026-05-21`

`v0.20.2` is a corrective public release for the release process itself.

It keeps the `v0.20.1` Storage Fabric runtime behavior intact, preserves the
already-published `v0.20.1` audit trail, and ships the release checklist
hardening needed before the next normal feature or maintenance release.

## Changed

- `docs/RELEASE_CHECKLIST.md` is now the mandatory release gate for every
  tagged release.
- Release-specific QA documents are now addenda only; they cannot replace the
  global release checklist.
- Every future release wrap must include an item-by-item checklist evidence
  table before the tag, GitHub release, GHCR publish, wiki sync, public demo
  refresh, or deployment refresh.
- `scripts/validate_release_wrap.py` now checks the required release-wrap
  evidence rows before a release can ship.
- `docs/RELEASE_WRAP_0.20.1.md` now records the post-publish audit of the
  `v0.20.1` process gap and the decision not to delete, overwrite, or retag
  public artifacts for a process-only remediation.

## Runtime Impact

- No new Storage Fabric feature behavior is introduced in this patch.
- The app/package version is bumped to `0.20.2` so GHCR, live `/livez`, and
  runtime cards can clearly show the corrected release.

## Release Discipline

This release is intentionally small, but it is still subject to the full
release checklist. The release wrap records the completed gates and any
concrete `N/A` decisions.
