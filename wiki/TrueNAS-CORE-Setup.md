# TrueNAS CORE Setup

Use this page for the classic TrueNAS CORE path.

This is the smoothest starting point if your host exposes:

- `enclosure.query`
- `disk.query`
- `pool.query`

and optionally:

- `sesutil`
- `glabel`
- `zpool`
- `gmultipath`
- `camcontrol`

## What CORE Can Do Well

- API-driven disk and pool inventory
- slot mapping from API or SSH enrichment
- SES identify LED control through `sesutil`
- multipath detail
- SAS SMART detail

## 1. Make An API Key

Create a read-only or appropriately scoped API key in the TrueNAS UI.

Use the base host only in config:

```text
https://truenas.example.local
```

Do not add `/api/v2.0`.

## 2. Optional But Recommended: Create An SSH User

Use a dedicated non-root account such as `jbodmap`.

Recommended properties:

- SSH key only
- no shell if your workflow allows it
- command-limited sudo only for the exact inventory commands you need

## 3. Minimal CORE SSH Command Set

This is a good first pass:

```yaml
ssh:
  enabled: true
  host: truenas.example.local
  port: 22
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
```

Optional extra source:

```yaml
    - sudo -n /sbin/camcontrol devlist -v
```

## 4. Example Single-System CORE Config

```yaml
systems:
  - id: archive-core
    label: Archive CORE
    default_profile_id: supermicro-cse-946-top-60
    truenas:
      host: https://truenas.example.local
      api_key: ""
      platform: core
      verify_ssl: true
      enclosure_filter: ""
    ssh:
      enabled: true
      host: truenas.example.local
      user: jbodmap
      key_path: /run/ssh/id_truenas
      sudo_password: ""
      known_hosts_path: /app/data/known_hosts
      strict_host_key_checking: true
      commands:
        - /sbin/glabel status
        - /usr/local/sbin/zpool status -gP
        - gmultipath list
        - sudo -n /usr/sbin/sesutil map
        - sudo -n /usr/sbin/sesutil show
        - sudo -n /sbin/camcontrol devlist -v
```

## 5. Optional LED Control

If your CORE host allows it, the app can use:

```bash
sudo -n /usr/sbin/sesutil locate -u /dev/sesX <slot> on
sudo -n /usr/sbin/sesutil locate -u /dev/sesX <slot> off
```

Keep that sudo as narrow as possible.

## 6. Common CORE Notes

- API-only mode can still be useful if you only want disk and pool metadata.
- SSH is what usually unlocks the best slot correlation.
- `camcontrol devlist -v` is especially useful on dual-path SAS systems.
- If a slot shows transport or cache fields as missing, check whether API SMART is sparse and whether SSH enrichment is allowed.
