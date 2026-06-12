# Advanced Configuration

This page is for operators who want to tweak more than the defaults.

## Single-System vs Multi-System

The app supports:

- a simple single-system env-driven flow
- a richer multi-system YAML config

If you want the optional history sidecar or the offline snapshot export flow,
use:

- [[History and Snapshot Export|History-and-Snapshot-Export]]

If you want one app instance to manage multiple hosts, use `systems:` in
`config/config.yaml`.

## Good Multi-System Pattern

```yaml
default_system_id: archive-core

systems:
  - id: archive-core
    label: Archive CORE
    default_profile_id: supermicro-cse-946-top-60
    truenas:
      host: https://archive-core.example.local
      api_key: ""
      platform: core
      verify_ssl: true
    ssh:
      enabled: true
      host: archive-core.example.local
      user: jbodmap
      key_path: /run/ssh/id_truenas
      known_hosts_path: /app/data/known_hosts
      strict_host_key_checking: true
      commands:
        - /sbin/glabel status
        - /usr/local/sbin/zpool status -gP
        - gmultipath list
        - sudo -n /usr/sbin/sesutil map
        - sudo -n /usr/sbin/sesutil show

  - id: offsite-scale
    label: Offsite SCALE
    enclosure_profiles:
      "5003048001c1043f": supermicro-ssg-6048r-front-24
      "500304801e977aff": supermicro-ssg-6048r-rear-12
    truenas:
      host: https://offsite-scale.example.local
      api_key: ""
      platform: scale
      verify_ssl: true
    ssh:
      enabled: true
      host: offsite-scale.example.local
      user: jbodmap
      key_path: /run/ssh/id_truenas
      known_hosts_path: /app/data/known_hosts
      strict_host_key_checking: true
      commands:
        - /usr/sbin/zpool status -gP
        - /usr/bin/lsblk -o NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,HCTL
        - /usr/bin/lsscsi -g
        - sudo -n /usr/bin/sg_ses -p aes /dev/sg27
        - sudo -n /usr/bin/sg_ses -p aes /dev/sg38
        - sudo -n /usr/bin/sg_ses -p ec /dev/sg27
        - sudo -n /usr/bin/sg_ses -p ec /dev/sg38
```

## App Tuning Knobs

Useful app-level settings:

```yaml
app:
  port: 8080
  refresh_interval_seconds: 30
  snapshot_cache_ttl_seconds: 10
  source_bundle_cache_ttl_seconds: 60
  smart_cache_ttl_seconds: 300
  sg_ses_device_cache_ttl_seconds: 300
  log_level: INFO
  debug: false
```

## What The Cache TTL Really Means

`snapshot_cache_ttl_seconds` controls how long rendered inventory snapshots
stay warm.

`source_bundle_cache_ttl_seconds` controls how long the heavier API/SSH source
payload stays warm. Leave this at least as high as the snapshot cache on
appliances with low SSH `MaxStartups` limits.

`smart_cache_ttl_seconds` controls selected-slot SMART detail reuse.

`sg_ses_device_cache_ttl_seconds` controls how long discovered Linux/SCALE/SES
device paths stay warm before rediscovery.

Older configs can still use `cache_ttl_seconds`; when the newer split fields
are not set, that legacy value feeds the snapshot and source-bundle caches.

Smaller:

- fresher view
- more load

Larger:

- less load
- slightly more stale view

## Command Lists

Treat SSH command lists as:

- standing inventory commands

and treat things like these as:

- on-demand per-slot enrichment commands

Examples:

- `smartctl`
- `nvme smart-log`
- `nvme id-ctrl`
- `nvme id-ns`
- LED identify actions

That split helps keep the standing SSH probe lighter.

## SSH Refresh Load

Inventory refreshes batch configured commands and dynamic enrichment through one
SSH session per target where possible, but operators should still keep refreshes
friendly to storage appliances:

- keep `refresh_interval_seconds` at `30` or higher unless you are actively
  debugging
- prefer `source_bundle_cache_ttl_seconds` of `60` or higher for appliances with
  low SSH startup limits
- keep optional SMART and LED actions on demand instead of adding every possible
  per-disk command to the standing `ssh.commands` list
- SMART detail probes batch JSON plus text enrichment per device and serialize
  SSH sessions per saved system; after a connection startup failure, optional
  SSH batches pause briefly before retrying
- use `ssh.extra_hosts` only for real HA/peer fallback paths; stale or duplicate
  hosts can still add avoidable connection attempts
- watch app logs for SSH refresh command counts, failure counts, and duration
  after changing cache or command settings

## Persistent Mapping Storage

Mappings are stored in JSON on the bind-mounted data path.

That means:

- they survive container rebuilds
- they are easy to back up
- they can be exported and imported in the UI

## History Sidecar Retention Knobs

If you are running the optional history sidecar, the main retention knobs are:

- `HISTORY_BACKUP_DIR`
- `HISTORY_BACKUP_RETENTION_COUNT`
- `HISTORY_LONG_TERM_BACKUP_DIR`
- `HISTORY_WEEKLY_BACKUP_RETENTION_COUNT`
- `HISTORY_MONTHLY_BACKUP_RETENTION_COUNT`

The default behavior is:

- keep short-term rotating SQLite snapshots under `./history/backups`
- keep `4` weekly promoted copies
- keep `3` monthly promoted copies

If you want longer-lived copies on a different disk or NAS later, point
`HISTORY_LONG_TERM_BACKUP_DIR` at that mounted path and leave the short-term
local backup path alone.

## When To Use `enclosure_profiles`

Use `enclosure_profiles` when:

- a system has front and rear SES IDs
- you want deterministic profile selection
- you want to prevent generic runtime profile fallback

## When To Use A Custom Profile Instead Of Code

Prefer custom YAML when:

- the geometry is new
- the slot ordering is new
- only the presentation is different

Prefer code only when:

- a whole new inventory adapter is needed
- the host needs new parser logic
- the UI model itself must change
