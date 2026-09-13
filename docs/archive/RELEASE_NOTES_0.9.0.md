# Release Notes - v0.9.0

Release date: April 17, 2026

## Summary

`0.9.0` is a stabilization release focused on performance visibility,
release-to-release slowdown detection, cache-first scope switching, and a
broader reusable profile catalog before the next major feature push.

The goal of this release is to make the app feel faster during normal operator
navigation and to make future regressions easier to catch before they ship.

## Highlights

- Opt-in performance instrumentation for inventory, SMART, and snapshot-export
  workflows, including staged timing and response `Server-Timing` headers
- Rolling read-only perf harness artifacts under `data/perf/` so branch and
  release comparisons can be repeated instead of reconstructed from memory
- Cache-first system and enclosure switching with client-side scope reuse,
  persistent slot-detail caching, warmed SMART summaries, and background
  refresh instead of forcing cold rebuilds on every view change
- Browser-visible `UI Timing` panel plus Playwright smoke coverage for system
  switches, enclosure switches, slot-detail reset, auto-refresh timing,
  history drawer load, export dialog rendering, and a release-style configured
  system sweep
- Reusable built-in generic profiles for common `1x24`, `3x4`, `4x15`, `5x12`,
  `6x14`, `102`, and `106` enclosure families
- Explicit chassis gap-cell rendering so center beams and sidecar voids can be
  shown honestly instead of compressed into fake packed rows
- Quantastor topology trust guardrails so transient middleware churn does not
  replace a trusted mirror view with fake `disk > data` topology or record that
  noise into history

## Operator Notes

- Previously visited systems and enclosure views should now repaint quickly
  from cache, with history and SMART data settling afterward instead of
  blocking the first visible switch.
- The optional history sidecar remains optional. The main UI still runs
  standalone without it.
- Quantastor topology changes now carry a much higher proof burden. If the
  authoritative pool-device feed is incomplete during a middleware restart or
  upgrade, the app should preserve the last trusted topology instead of
  presenting a flattened transient view.
- Background auto-refresh no longer re-fires immediately after a manual system
  or enclosure switch.

## Validation Snapshot

- Browser QA:
  - `npx playwright test`
  - Result: `7` passed, `1` skipped
- Perf harness:
  - `python scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate-0.9.0`
  - Artifacts:
    - `data/perf/latest.md`
    - `data/perf/latest.json`
    - `data/perf/history.csv`
    - `data/perf/history.jsonl`
- Release-candidate perf snapshot recorded at `2026-04-17T18:27:45Z`:
  - `inventory_cached` avg `24.2 ms`
  - `smart_batch` avg `15.3 ms`
  - `mappings_import_roundtrip` avg `1022.7 ms`
  - `inventory_force` avg `5688.9 ms`
  - `snapshot_export_estimate` avg `10574.5 ms`

## Deployment Notes

- App version is now `0.9.0`.
- Existing operators should review:
  - `.env.example`
  - `config/config.example.yaml`
  - `config/profiles.example.yaml`
- Release prep should include both:
  - the expanded unit/integration test pass
  - the Playwright browser sweep against a live app build

## Suggested GitHub Release Intro

`0.9.0` hardens the JBOD UI before the next feature-heavy release. It adds
real performance instrumentation, a repeatable slowdown harness, much faster
cache-first system switching, broader reusable enclosure profiles, and
guardrails that stop transient Quantastor middleware wobble from being treated
like a real topology rewrite.
