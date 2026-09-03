# Release wrap: v0.22.2

Date: `2026-09-01`

## Scope

v0.22.2 is a SemVer patch on published v0.22.1. It releases the reviewed segmented-history architecture already merged to `main`, adds fail-closed scheduled-FULL-gated retention with durable backup authorization, preserves restore ownership for non-root runtime consumers, makes scheduled-backup status safely readable across the non-root Compose services, fixes immutable deployment candidate validation, and carries the dependency versions already present on `main`.

Release preparation used `fix/v0.22.2-segmented-lifecycle`. The published tag
was cut from `main`.

Release candidate and source commit:
`6473d05f46d8344146cbbd7d0cdbf44487613a3c`. This tagged commit is the
published release authority; earlier pre-publish candidate identities remain
historical validation evidence only.

Tag: `v0.22.2`, published 2026-09-01 as a non-draft, non-prerelease GitHub
release: https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.22.2.

Development resumed on `main` after the tag. The application version remains
`0.22.2` until the next release version is selected.

At release time, PR #120 was closed because `main` already contained its
`websockets==17.0.1` and `cryptography==50.0.0` updates. Issue #119 and draft PR
#121 remained v0.22.3 work pending real-shelf evidence. Crash-safe
later-generation segment publication remained tracked in issue #124 for v0.22.3
because the initial migration journal does not cover a later hot, segment, and
catalog replacement transaction.

Post-release reconciliation: Issue #119 closed after real-shelf validation and
PR #121 merged as `579e3bf641872d842af3639ed7bdb084c9b75aff`. Issue #124 closed
after crash-safe later-generation rotation landed in #162. The paragraph above
records release-time state only.

Validated against `docs/RELEASE_CHECKLIST.md`.

## Checklist evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | Release preparation used `fix/v0.22.2-segmented-lifecycle`; tag `v0.22.2` was published from `main` source commit `6473d05f46d8344146cbbd7d0cdbf44487613a3c` | Pass |  |
| Python unit and syntax gates | yes | Status-sharing RED regressions fail on `0700`/`0600` and pass on `2750`/`0640`; full Python 3.12 and 3.14 each pass 1,099 tests with 4 declared skips; Ruff, compileall, and diff hygiene pass | Pass |  |
| JavaScript syntax gates | yes | 130 Node unit tests pass; application, admin, public-demo, Playwright config, and final `qa/ui-switching.spec.js` syntax checks pass | Pass |  |
| Docker build and health gates | yes | The successor release candidate was built from the reviewed staged source; UI, history, and admin were healthy with zero restarts and returned HTTP 200 on `/livez` | Pass |  |
| Optional-sidecar runtime matrix | yes | The previous exact image passed isolated UI-only, UI+history, UI+admin, and all-service operation. The successor repeated the affected all-service plus one-shot-backup path, then removed every task-owned container and verified its temporary listeners closed | Pass |  |
| Full Playwright/browser gates | yes | Admin 7/7 passed; offline/public demo 6 passed with 4 expected interaction-only skips; final live UI suite passed 15 with 2 fixture-dependent skips; ESXi smoke skipped because the QA config has no saved ESXi system | Pass |  |
| Feature-specific live API/UI gates | yes | The successor rejected a `1000:1000 2750` status directory, accepted `1000:10001 2750`, published complete `1000:10001 0640` success evidence, byte-bound the archive size and SHA-256, allowed both non-root readers, rejected five incomplete-success shapes in the built image, and passed independent file-backed preflight with `history_db` selected and present | Pass |  |
| Local release perf harnesses | yes | The corrected main harness completed three serial iterations including mapping preview/confirmation; the history harness completed three serial iterations against the restored segmented store. SHA-256-bound artifacts were retained in task-owned temporary storage on the isolated QA target | Pass |  |
| Linux QA restore gate | yes | The previous candidate's encrypted schema-v2 FULL restore recovered expected hot/segmented counts, catalog and segment hashes, runtime ownership, and SQLite integrity with no rollback debris; restore code is unchanged by the successor | Pass |  |
| Restored Linux QA perf harnesses | yes | Restored main and history harnesses passed serially with labels `release-candidate-linux-qa-restore` and `release-candidate-history-linux-qa-restore`; artifact SHA-256 values were recorded and stack cleanup passed | Pass |  |
| Snapshot/export/offline artifact gate | yes | Offline snapshot and checked-in public-demo browser checks passed; snapshot estimate dialog passed in the final live suite | Pass |  |
| Docs/wiki/public-demo gate | yes | Version alignment, changelog, v0.22.2 release notes/wrap, segmented operations, setgid status setup, release checklist, and wiki consistency are updated; source contract tests pass | Pass |  |
| GHCR publish verification | yes | [Publish GHCR workflow run 33505635256](https://github.com/gcs8/truenas-jbod-ui/actions/runs/33505635256) completed successfully for source `6473d05f46d8344146cbbd7d0cdbf44487613a3c`; anonymous registry readback returned `ghcr.io/gcs8/truenas-jbod-ui@sha256:4bfa37a4c40a058055aef384194f98248722eca25f7fb429d6a5a34446d647a7`, and the linux/amd64 image label carries the same source revision | Pass |  |
| Deployment refresh/sniff tests | yes | The private deployment receipt was validated and runtime convergence was reverified against `v0.22.2`: the expected and running digests matched, required services had zero restarts, and health probes passed. Private deployment identifiers are not retained here | Pass |  |
| Post-release reopen | yes | Development resumed on `main` after the tag; `CHANGELOG.md` now carries an `Unreleased` section while application version `0.22.2` remains unchanged | Pass |  |

## Safety boundary

- The published OCI index digest is
  `sha256:4bfa37a4c40a058055aef384194f98248722eca25f7fb429d6a5a34446d647a7`
  and its linux/amd64 source label matches the release commit.
- Bounded production readback retained only the release version, digest/source
  match, service health outcome, and the intentionally stopped admin-sidecar
  state. No private deployment identifiers are recorded here.
- Segmented retention edits only the writable hot database. It never edits immutable segments.
- Missing, unreadable, malformed, stale, failed, history-excluding, history-absent, artifact-incomplete, symlinked, oversized, or writable scheduled-backup status blocks retention.
- The backup UID owns a setgid `2750` status directory with the explicitly configured app GID and publishes atomic `0640` status files. UI/history can read but cannot alter backup evidence.
- The hot SQLite database persists `ready`, `claimed`, and `consumed` backup authorization. Completed passes cannot repeat after restart, failed passes remain retryable, catch-up can continue, and interrupted claims require a newer backup.
- Pre-release backup diagnostics that reached the bounded 7z cap failed closed.
  The final production outcome is summarized above without retaining private
  backup or deployment receipt details.

## Validator status

Final validation command:

- `python3 scripts/validate_release_wrap.py 0.22.2`

## Remaining work

No v0.22.2 release gates remain. Subsequent source and documentation changes
belong under `Unreleased` and do not change the published tag or image.
