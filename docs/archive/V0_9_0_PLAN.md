# v0.9.0 Plan

Planning baseline for the first stabilization-focused release after `0.8.0`.

## Goal

Use `v0.9.0` to improve confidence before `1.0.0` or `0.10.0` by adding
reasonable performance observability, repeatable slowdown detection, and a
small batch of broadly reusable enclosure profiles.

This should help us answer:

- where time is actually going during inventory and slot workflows
- whether a change made the app slower between releases
- which chassis layouts from the Quantastor reference set are broad enough to
  deserve first-class profile treatment

## Recommendation

Focus `v0.9.0` on two workstreams:

- opt-in performance instrumentation and comparison harness
- low-risk expansion of reusable chassis and enclosure profiles

Why this is the best next step:

- the app already has basic refresh, caching, and rotating log-file settings,
  but it does not yet have a real performance harness
- current write paths appear to rebuild snapshots more than once per action, so
  there is likely low-hanging fruit once we can measure it
- big shelves still feel slow when switching back to a previously visited
  system because the app is paying cold inventory and SMART costs again instead
  of leaning harder on cacheable slot identity and detail fields
- the Quantastor reference files provide a rich layout catalog that can be
  mined for overlapping shapes without importing vendor images directly
- this is the right kind of pre-`1.0` work: fewer guesses, fewer regressions,
  and a stronger base for later feature releases

## Workstream 1: Opt-In Performance Observability

Target:

- add performance visibility that can be enabled during development or through
  environment/config flags
- keep the default runtime simple and safe when performance tracing is off
- make slow paths visible without needing to attach a debugger blindly

Good first deliverables:

- request-level timing for key API routes
- staged timing inside snapshot builds, including:
  - TrueNAS websocket collection
  - SSH diagnostics
  - Quantastor REST / CLI / SES enrichment
  - profile resolution
  - snapshot export collection when used
- slow-operation logging with clear thresholds
- lightweight request correlation ids or operation labels in logs
- optional timing summaries written to logs or a small debug artifact

Suggested toggles:

- `.env` or config switches for performance timing
- separate thresholds for "collect timings" and "warn when slow"
- an option to keep extra instrumentation dev-only

## Workstream 2: Repeatable Perf Harness

Target:

- compare key workflows across branches and releases
- catch obvious regressions before they ship
- keep the harness small enough that it actually gets used
- add a small browser smoke layer for real switch/render regressions the HTTP
  harness cannot see on its own

Good first candidates:

- scripted benchmark pass for:
  - inventory snapshot build
  - slot-detail / SMART summary fetch
  - mapping save / clear
  - LED identify / clear
  - history-backed snapshot export
- stable JSON or Markdown output that can be compared over time
- one simple "baseline versus current branch" workflow
- release-checklist integration so perf checks become part of normal release
  prep instead of a one-off exercise
- lightweight Playwright coverage for:
  - browser-visible system switch completion
  - browser-visible enclosure switch completion where multiple views exist
  - selected-slot detail reset on scope change
  - no immediate auto-refresh after a manual switch
  - history drawer open/load on a selected slot
  - snapshot export dialog estimate rendering
- artifact-first browser reporting, with timing thresholds staying advisory
  until a few baseline runs prove they are stable

## Workstream 3: Low-Hanging Perf Cleanup

Target:

- use the new instrumentation to remove obvious self-inflicted costs first
- prefer simple wins over speculative rewrites

Known early suspects:

- duplicate forced snapshot rebuilds after write actions
- repeated inventory refreshes across route and service layers
- avoidable repeated SSH / API work during one operator action
- large-system switch-back latency caused by treating stable slot identity and
  stable SMART detail as fully cold every time
- any expensive parsing or merge step that dominates snapshot build time once
  measured

Rules:

- measure first when possible
- keep correctness ahead of micro-optimization
- prefer trimming duplicate work before adding new infrastructure
- when caching, prefer standalone-safe local state in the main UI over making
  the optional history sidecar a hard dependency

Current `v0.9.0` perf direction inside this workstream:

- keep short-lived live snapshot and SMART caches for truly fresh reads
- allow read-only UI paths to reuse stale cached data quickly, then refresh in
  the background
- reuse one shared per-system source bundle across enclosure switches so the
  app can redraw a different chassis/profile view without redoing the full
  appliance fetch first
- persist stable slot facts and stable SMART fields locally so detail panes can
  stay useful even when a fresh poll is still running
- optionally warm all configured systems once at container startup so switching
  back to a known shelf is fast
- parallelize the major appliance inventory fetch groups when a real cold poll
  is required so first-hit latency drops instead of just relying on cache
- preserve warmed SMART summaries across system and enclosure switches instead
  of pruning them on each snapshot change
- treat history backend status as a short-lived global cache so the UI does not
  keep paying the same status round-trip during rapid view changes
- keep the last seen client-side snapshot per scope so switching back to a
  known system or enclosure can repaint immediately while the live refresh runs
- keep a shell-first render pass on deck as the next polish for truly cold
  switches that have never been seen before, where the static enclosure
  geometry can paint immediately and the live slot/state payload can hydrate
  after

## Workstream 4: Broader Reusable Profiles From Quantastor References

Reference inputs:

- `D:\qs_enclosure_layout.conf`
- `D:\enclosureviews`

Intent:

- use those files as geometry and naming references
- do not import the source images into this repo
- identify overlapping layouts that should become reusable app profiles instead
  of one-off vendor clones

Promising overlapping shapes from the reference set:

- `1 x 24` front-drive layouts
- `3 x 4` 12-bay layouts
- `4 x 15` 60-bay layouts
- `6 x 14` 84-bay layouts
- `8 x 14` 106-bay layouts
- `5 x 5` 25-bay layouts
- `5 x 12` 60-bay layouts

First landed batch:

- `generic-front-24-1x24`
- `generic-front-12-3x4`
- `generic-top-60-4x15`
- `generic-front-60-5x12`
- `generic-front-84-6x14`

Second landed batch:

- `generic-front-102-8x14`
- `generic-front-106-8x14`
- renderer support for explicit interior gap cells so center beams and sidecar
  voids do not need to be visually compressed into packed rows

Recommended approach:

- first inventory the reference set by shape and orientation
- pick a short list of broad profiles that cover many chassis families
- use the visuals only to validate geometry, slot order, latch edge, and face
  style hints
- keep vendor- or cage-specific variants for later unless they unlock real reuse
- use explicit gap cells for irregular `102` / `106`-bay shelves instead of
  packed-row approximations so those faces stay visually honest

## Suggested Technical Shape

### App And History Service

- keep instrumentation lightweight and mostly internal
- use the existing logging path and add structured timing context rather than
  inventing a full telemetry stack first
- make instrumentation easy to disable completely

### Native Profilers

- start with Python-friendly tooling and app-native timing first
- allow optional deeper profiling later if the simple harness points at a real
  need

This means:

- `cProfile`-style or middleware-level timing is in scope
- external profilers can be evaluated as helpers
- eBPF is explicitly optional research, not a required foundation for `v0.9.0`

## Scope Guardrails

`v0.9.0` should not become:

- a mandatory always-on telemetry release
- a Prometheus / OTEL / vendor-agent integration project
- an eBPF-only profiling effort
- a giant one-off import of every layout in the Quantastor reference file
- a new large storage-platform adapter milestone

## Deferred But Worth Tracking

- optional native profiler integration beyond the initial harness
- Linux-specific deep tracing if the first-pass harness still leaves blind spots
- richer profile metadata if the current schema proves too narrow for the next
  batch of layouts
- broader platform-adapter work after the perf baseline and reusable profile
  pass are in place

## Exit Criteria For v0.9.0

- developers can enable performance timing without code edits
- the app can surface or record useful timing for the main expensive workflows
- a repeatable branch-versus-baseline perf check exists
- at least one obvious measured slowdown or duplicate-work path is removed
- a small batch of new reusable enclosure profiles lands from the Quantastor
  reference set
- docs explain how to turn the perf harness on and how to use the new profiles

## Status

Planning direction updated on `2026-04-17` from the local `v0.9.0` branch.
