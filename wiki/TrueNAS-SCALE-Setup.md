# TrueNAS SCALE Setup

Use this page for TrueNAS SCALE.

The app uses the middleware websocket API, not the deprecated REST `/api/v2.0`
path, so it is aligned with the JSON-RPC-over-websocket direction that SCALE is
keeping going forward.

## What SCALE Can Do Well Today

- API-driven disk and pool inventory
- Linux SES slot mapping through `sg_ses -p aes`
- live identify-state reads through `sg_ses -p ec`
- identify LED control through `sg_ses --set=ident` and `--clear=ident`
- rich SMART detail through on-demand `smartctl`

## 1. Make An API Key

Create a read-only or appropriately scoped API key in SCALE.

Use:

```text
https://scale.example.local
```

Do not add `/api/v2.0`.

## 2. Create The SSH User

Recommended:

- dedicated `jbodmap` user
- SSH key auth
- command-limited sudo

## 3. Minimal SCALE SSH Commands

Put these in the standing command list:

```yaml
ssh:
  enabled: true
  host: scale.example.local
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

`smartctl` does not need to live in the standing command list. The app runs it
on demand for the selected slot.

## 4. Required SCALE Sudo Access

The important pattern is:

```text
/usr/bin/sg_ses
/usr/sbin/smartctl
```

The app has been validated with narrow rules for:

- `sg_ses -p aes`
- `sg_ses -p ec`
- `sg_ses --dev-slot-num=... --set=ident`
- `sg_ses --dev-slot-num=... --clear=ident`
- `smartctl -x -j /dev/<disk>`

## 5. Example SCALE Multi-System Config

```yaml
systems:
  - id: offsite-scale
    label: Offsite SCALE
    enclosure_profiles:
      "5003048001c1043f": supermicro-ssg-6048r-front-24
      "500304801e977aff": supermicro-ssg-6048r-rear-12
    truenas:
      host: https://scale.example.local
      api_key: ""
      platform: scale
      verify_ssl: false
      enclosure_filter: ""
    ssh:
      enabled: true
      host: scale.example.local
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

## 6. Why `enclosure_profiles` Is Helpful

If the app can infer the enclosure profile from SES data, it will.

If you already know the front/rear enclosure IDs, explicitly pinning them is
even better because:

- it avoids generic runtime fallback
- it keeps the correct chassis wording
- it preserves the correct tray orientation and polish

## 7. Common SCALE Notes

- Some tested SCALE systems expose useful disk and pool data but no usable enclosure rows through the middleware API.
- That is why the app leans on Linux SES access over SSH for first-pass SCALE slot mapping.
- Advisory non-zero `smartctl` exit codes are normal on some disks; the app already tolerates that when the output is still valid.
