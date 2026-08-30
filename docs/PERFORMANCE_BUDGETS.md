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
| Logical retained bytes | 20,971,520 bytes | 33,554,432 bytes |
| History status calls across two identical exports | 1 | 1 |
| Scope-history calls across two identical exports | 1 | 1 |
| Per-slot history fallback calls | 0 | 0 |
| Template renders across two identical exports | 1 | 1 |
| ZIP builds and cache entries for HTML export | 0 | 0 |

Logical retained bytes are the unique UTF-8 HTML bytes plus compact history JSON and any ZIP-cache bytes. The gate does not use `tracemalloc` or `sys.getsizeof`.

Wall-clock durations are report-only. Shared CI runner timing cannot fail this gate. Stable cardinality, query, cache, and byte regressions can.

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

## Deferred work

Issue #36 owns zero-rebuild slot selection and focus preservation. Issue #55 owns parser and SAS diagnostic payload budgets. Issues #48 and #56 own browser history-cache and refresh behavior. This budget suite must not add permissive baselines for those unfinished paths.
