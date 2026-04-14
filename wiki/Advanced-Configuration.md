# Advanced Configuration

This page is for operators who want to tweak more than the defaults.

## Single-System vs Multi-System

The app supports:

- a simple single-system env-driven flow
- a richer multi-system YAML config

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
      verify_ssl: false
    ssh:
      enabled: true
      host: archive-core.example.local
      user: jbodmap
      key_path: /run/ssh/id_truenas
      known_hosts_path: /run/ssh/known_hosts
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
      verify_ssl: false
    ssh:
      enabled: true
      host: offsite-scale.example.local
      user: jbodmap
      key_path: /run/ssh/id_truenas
      known_hosts_path: /run/ssh/known_hosts
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
  cache_ttl_seconds: 10
  log_level: INFO
  debug: false
```

## What The Cache TTL Really Means

`cache_ttl_seconds` controls how long inventory snapshots stay warm.

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

## Persistent Mapping Storage

Mappings are stored in JSON on the bind-mounted data path.

That means:

- they survive container rebuilds
- they are easy to back up
- they can be exported and imported in the UI

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
