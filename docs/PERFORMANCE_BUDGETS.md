# Performance budgets

This repository uses public, deterministic modeled fixtures for 60-slot and 347-slot scales. They do not use local configuration, history databases, parser captures, hardware names, addresses, or credentials.

## Fixture contract

`tests/perf_fixtures.py` fixes the fixture timestamp and ordering. Each case has the exact requested slot count. Every slot has one modeled event and two samples for each of six history metrics, for 12 metric samples per slot. The tests serialize through `InventorySnapshot`, `HistoryStore.list_scope_history`, the history scope route shape, and `SnapshotExportService`.

The checked artifact is `docs/performance-baseline-v1.json`. It records `fixture_version`, `modeled: true`, measured stable counts and bytes, and the comparison policy. `MODELED_THRESHOLDS` in `tests/perf_fixtures.py` is the authoritative source for hard ceilings; tests consume that mapping directly and the generated artifact records the values used for each case.

## Blocking budgets

The gate compares structural, cardinality, query, and cache invariants exactly. This includes slot counts, database connection and `SELECT` counts, history calls, template and ZIP work, cache entry counts, and the configured cache maximum.

Byte measurements use a symmetric bounded-drift policy: the allowed difference from the reviewed baseline is 10% or 4,096 bytes, whichever is larger. Hard ceilings always apply, even when a measurement remains inside its drift band. A payload that crosses a ceiling therefore fails immediately, and a large regression cannot be hidden by refreshing ordinary asset measurements.

Export measurements report the pre-inline HTML document bytes and the inlined static asset bytes separately, as well as the complete HTML and retained-cache totals. Normal `app.js`, `style.css`, or offline-image edits inside the bounded band do not require baseline regeneration. Growth beyond the band still fails, while the complete export remains subject to its hard ceiling.

The three export caches use a shared 32 MiB logical payload budget by default. Accounting uses UTF-8 bytes for rendered HTML, raw bytes for ZIP archives, and compact JSON bytes for retained snapshot, history, SMART, and export metadata. The gate does not use `tracemalloc` or `sys.getsizeof`.

Global LRU eviction runs after per-cache entry limits and TTL cleanup. A cache hit refreshes access order without extending TTL. Oversized entries are returned but not cached, and they do not evict usable entries already inside the budget. HTML-only work does not build or retain ZIP bytes.

Prometheus exposes `snapshot_export_cache_entries`, `snapshot_export_cache_bytes`, request outcomes, eviction reasons, and oversized-entry rejections. Labels are limited to the fixed cache names `history`, `render`, and `zip`; cache keys and payload content are never labels.

Wall-clock durations are report-only. Shared CI runner timing cannot fail this gate. Stable cardinality, query, cache, and byte regressions can. The deterministic unittest suite runs the baseline check once; CI does not invoke the same check again outside that suite.

Run the repeatable report-only cache and latency benchmark with:

```bash
python scripts/benchmark_snapshot_export_cache.py --slots 60 347 --iterations 3
```

The command prints median end-to-end elapsed time and current cache-byte accounting. Do not copy its host-dependent timing into the deterministic baseline or turn it into a CI threshold.

## Check and refresh

Run the normal check with:

```bash
python scripts/build_perf_baseline.py --check
```

When an intentional fixture, schema, policy, or meaningful payload-shape change needs new reviewed measurements, inspect the diff and then refresh atomically with:

```bash
python scripts/build_perf_baseline.py --write
python scripts/build_perf_baseline.py --check
```

Review fixture and baseline changes together. Do not raise a ceiling only to make CI pass. Explain why the payload or cache contract changed and retain useful headroom.

## Active performance semantics

- Selection and focus updates must not rebuild a stable enclosure grid.
- Parser and SAS diagnostic payloads remain bounded before they reach API,
  browser, or diagnostic surfaces.
- Browser history caches remain bounded by age, entry count, and bytes.
- Refresh paths preserve cached state while it is valid and invalidate only the
  affected scope.

The deterministic baseline above covers its named inventory, history, export,
query, and cache metrics. Other performance-sensitive paths require focused
contract tests and measured limits rather than permissive placeholder baselines.
