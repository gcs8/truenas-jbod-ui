# v0.8.0 History Sidecar Plan

## Goal

Add optional historical lookback without turning the main JBOD UI into a full
monitoring stack.

The intended `v0.8.0` direction is:

- keep the main UI fast and inventory-focused
- make historical collection optional in Docker Compose
- collect only a small set of useful slot metrics
- record slot-change events only when something meaningful changes

## Recommendation

Use a lightweight SQLite-backed sidecar service rather than InfluxDB,
Prometheus, or MySQL.

Why this is the best fit for the current app:

- the sample rate is low
- the metric set is small and mostly integer counters
- slot history has a strong relational / event-log shape
- deployment should stay close to the current single-compose-file workflow
- operators should be able to inspect or back up the history file directly

## What To Collect

Fast cadence, default every `5` minutes:

- `temperature_c`

Slow cadence, default every `6` hours but operator-tunable:

- `bytes_read`
- `bytes_written`
- `annualized_bytes_written`
- `power_on_hours`

Change-only events:

- slot state changes such as present / empty / fault / identify
- identity changes such as serial, device, model, or persistent ID drift
- topology changes such as pool or vdev movement

That event model should be enough to answer practical questions like:

- when did this bay start running hotter than usual?
- when did this disk move to a different slot?
- when did someone replace or reshuffle a drive?
- when did a disk change pool or vdev role?

## Architecture

### 1. Separate collector container

The sidecar should call the main UI API instead of talking to TrueNAS directly.

That keeps:

- API credentials in one place
- SSH behavior in one place
- platform-specific SMART probing in one place
- the optional history service from becoming a second inventory implementation

### 2. SQLite schema

Keep two main tables:

- `metric_samples`
- `slot_events`

And one small current-state table:

- `slot_state_current`

This gives us:

- efficient slot timelines
- easy “latest state” comparisons between polls
- a stable future path for embedded UI panels or a dedicated history page

### 3. UX direction

Short term:

- keep the history sidecar on its own optional port with a tiny status page and
  JSON endpoints

Preferred medium term:

- add a `History` button in the main slot-detail panel when a history backend is
  configured and healthy
- open a slot-scoped history drawer or a dedicated history route inside the main
  app

That avoids forcing a second full standalone UI before we know what operators
actually need.

## Why Not Prometheus / Influx / MySQL

Prometheus:

- great at scrape-based metrics
- awkward for change-only slot identity history unless we bolt on another store
- more “ops stack” than this project currently wants

InfluxDB:

- valid for time-series metrics
- heavier than necessary for this sample volume
- still wants a second solution for rich change-event history

MySQL / MariaDB:

- works technically
- bigger deployment and maintenance footprint than SQLite for one small optional
  sidecar

SQLite:

- zero extra infrastructure
- stable for small append-heavy datasets
- easy backup / inspection story
- fits the repo’s existing “simple local persistence on bind mounts” philosophy

## Rollout

Phase 1 in this branch:

- optional Compose profile
- SQLite-backed sidecar collector
- per-slot event log
- per-slot metric sampling endpoints

Phase 2:

- main UI health check for the history backend
- slot-detail `History` button
- compact charts and recent-event timeline in the main UI

Phase 3:

- retention settings
- downsampling / rollups if operators keep long histories
- optional export of slot event timelines

