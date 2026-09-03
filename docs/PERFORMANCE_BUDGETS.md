# Performance budgets

This repository uses public, deterministic modeled fixtures for 60-slot and 347-slot scales. They do not use local configuration, history databases, parser captures, hardware names, addresses, or credentials.

## Fixture contract

`tests/perf_fixtures.py` fixes the fixture timestamp and ordering. Each case has the exact requested slot count. Every slot has one modeled event and two samples for each of six history metrics, for 12 metric samples per slot. The tests serialize through `InventorySnapshot`, `HistoryStore.list_scope_history`, the history scope route shape, and `SnapshotExportService`.

The checked artifact is `docs/performance-baseline-v1.json`. It records `fixture_version`, `modeled: true`, measured stable counts and bytes, and the applicable ceilings.

## Blocking budgets

| Metric | 60 slots | 347 slots |
| --- | ---: | ---: |
| Inventory compact JSON | 98,304 bytes | 524,288 bytes |
| Scope-history compact JSON | 655,360 bytes | 3,145,728 bytes |
| Scope-history connections | 1 | 1 |
| Scope-history `SELECT` statements | 20 | 20 |
| Export HTML | 8,388,608 bytes | 12,582,912 bytes |
| Accounted export-cache bytes | 20,971,520 bytes | 33,554,432 bytes |
| History status calls across two identical exports | 1 | 1 |
| Scope-history calls across two identical exports | 1 | 1 |
| Per-slot history fallback calls | 0 | 0 |
| Template renders across two identical exports | 1 | 1 |
| ZIP builds and cache entries for HTML export | 0 | 0 |

The three export caches use a shared 32 MiB logical payload budget by default. Accounting uses UTF-8 bytes for rendered HTML, raw bytes for ZIP archives, and compact JSON bytes for retained snapshot, history, SMART, and export metadata. The gate does not use `tracemalloc` or `sys.getsizeof`.

Global LRU eviction runs after per-cache entry limits and TTL cleanup. A cache hit refreshes access order without extending TTL. Oversized entries are returned but not cached, and they do not evict usable entries already inside the budget. HTML-only work does not build or retain ZIP bytes.

Prometheus exposes `snapshot_export_cache_entries`, `snapshot_export_cache_bytes`, request outcomes, eviction reasons, and oversized-entry rejections. Labels are limited to the fixed cache names `history`, `render`, and `zip`; cache keys and payload content are never labels.

Wall-clock durations are report-only. Shared CI runner timing cannot fail this gate. Stable cardinality, query, cache, and byte regressions can.

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

When an intentional shape change needs new measurements, inspect the diff and then refresh atomically with:

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
