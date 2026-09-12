# Release Wrap - v0.14.0

Date: `2026-04-26`

## Status

`0.14.0` is validated on the release-prep branch and looks ready for the final
cut mechanics.

The product slice itself now feels locked: the first-pass ESXi path is in, the
admin-side ESXi guardrails are honest, the read-path responsiveness cleanup is
landed, and the refreshed screenshot/docs pass is underway. The remaining work
is the actual closeout sequence: final commit review, merge, tag, publish, and
reopen on the next dev branch.

## What This Cycle Locked In

- first-pass read-only ESXi inventory on the validated Supermicro
  `AOC-SLG4-2H8M2` path, using SSH `esxcli` plus StorCLI JSON
- a photo-backed live AOC carrier view that maps physical RAID members `13:0`
  and `13:1` onto the board image
- admin-side ESXi setup guardrails that keep the recommended saved SSH user on
  `root` and disable the Linux bootstrap/sudoers flow
- stale-cache-first switching restored for normal read paths, plus a
  lightweight `/livez` route and cached `/healthz` dependency reporting
- narrower cache invalidation for slot mutations and a non-blocking Quantastor
  LED verify follow-up so current identify truth no longer drags every switch

## Current Release-Prep Snapshot

- app and browser-QA package metadata are now bumped to `0.14.0` for the
  release commit
- current release-prep work is isolated on:
  - `codex/v0.14.0-release-prep-2026-04-26`
- release-facing references and tracked screenshots are being refreshed to
  `v0.14.0`
- broad release validation currently reads:
  - Python `unittest`: `139` passed
  - Playwright smoke: `15` passed / `1` skipped
  - focused ESXi/admin smoke: `5` passed
  - local perf harness label: `release-candidate-esxi-prep`
- known remaining perf caveat is still the local Windows `history_status` path,
  not a new branch-wide switching regression

## What Still Needs To Happen Before The Cut

- finish the release-facing screenshot/docs refresh and review the final diff
- merge, tag, publish, and republish the checked-in `wiki/` tree if it changed
- reopen the repo on the next `-dev` kickoff branch after the tag lands

## What Rolls Beyond This Tag

These items still look like later work, not blockers for the `0.14.0` cut:

- deciding whether the local ESXi dev entry should stay on root auth or move
  toward a narrower key/service-account model
- the remaining shared `ses_enclosure` geometry cleanup
- broader second-pass builder editing beyond the current preset/matrix path
- deeper tuning of the local Windows `history_status` path
