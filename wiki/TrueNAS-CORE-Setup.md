# TrueNAS CORE setup

Start with the middleware API. Add SSH only when you need stronger slot
correlation, SAS details, topology diagnostics, or LED control.

## 1. Connect through the API

Create a read-only or appropriately scoped API key in TrueNAS. Enter the base
host URL without `/api/v2.0`:

```text
https://truenas.example.local
```

An API-only system needs this shape:

```yaml
systems:
  - id: truenas-core-a
    label: TrueNAS CORE A
    default_profile_id: supermicro-cse-946-top-60
    truenas:
      host: https://truenas.example.test
      api_key: ""
      platform: core
      verify_ssl: true
    ssh:
      enabled: false
```

Supply the API key through the setup UI or the documented secret setting. Open
the main UI and confirm that disks and pools load before adding SSH.

## 2. Add optional SSH enrichment

Use a dedicated non-root account such as `jbodmap`, SSH key authentication, and
command-limited sudo. Keep strict host-key checking enabled.

The standing command list below adds pool, SES, multipath, HBA, PCI, and recent
MPR/CAM event data. Preserve the commands exactly when you use this example.

```yaml
ssh:
  enabled: true
  host: truenas.example.local
  port: 22
  user: jbodmap
  key_path: /run/ssh/id_truenas
  strict_host_key_checking: true
  commands:
    - /sbin/glabel status
    - /usr/local/sbin/zpool status -gP
    - gmultipath list
    - sudo -n /usr/sbin/sesutil map
    - sudo -n /usr/sbin/sesutil show
    - sudo -n /usr/sbin/mprutil show adapters
    - sudo -n /usr/sbin/mprutil show adapter
    - sudo -n /usr/sbin/mprutil show devices
    - sudo -n /usr/sbin/mprutil show enclosures
    - sudo -n /usr/sbin/mprutil show expanders
    - sudo -n /usr/sbin/mprutil show iocfacts
    - /usr/sbin/pciconf -lv
    - sysctl -a 2>/dev/null | egrep '^dev\.mpr\.[0-9]+\.%(location|parent):' || true
    - sudo -n /usr/local/sbin/dmidecode -t slot
    - messages=$({ tail -n 4000 /var/log/messages 2>/dev/null || sudo -n /usr/bin/tail -n 4000 /var/log/messages 2>/dev/null || true; } | egrep '(mpr[0-9]+:|\(da[0-9]+:mpr[0-9]+:)' || true); if [ -n "$messages" ]; then printf '%s\n' "$messages" | tail -n 400; else dmesg -a | egrep '(mpr[0-9]+:|\(da[0-9]+:mpr[0-9]+:)' | tail -n 400; fi
```

For dual-path SAS systems, you can also add:

```yaml
    - sudo -n /sbin/camcontrol devlist -v
```

Use the complete generated CORE policy in
[[SSH Setup and Sudo|SSH-Setup-and-Sudo]]. It includes the bounded bootstrap
rules, per-HBA forms, both supported `smartctl` paths, and disk-sync polling.
Do not replace its anchored SMART arguments with wildcards. The SMART regular
expressions require sudo 1.9.10 or newer. On older hosts, enumerate exact
per-device commands.

## 3. Configure the enriched system

```yaml
systems:
  - id: truenas-core-a
    label: TrueNAS CORE A
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
      strict_host_key_checking: true
      commands:
        - /sbin/glabel status
        - /usr/local/sbin/zpool status -gP
        - gmultipath list
        - sudo -n /usr/sbin/sesutil map
        - sudo -n /usr/sbin/sesutil show
        - sudo -n /sbin/camcontrol devlist -v
        - sudo -n /usr/sbin/mprutil show adapters
        - sudo -n /usr/sbin/mprutil show adapter
        - sudo -n /usr/sbin/mprutil show devices
        - sudo -n /usr/sbin/mprutil show enclosures
        - sudo -n /usr/sbin/mprutil show expanders
        - sudo -n /usr/sbin/mprutil show iocfacts
        - /usr/sbin/pciconf -lv
        - sysctl -a 2>/dev/null | egrep '^dev\.mpr\.[0-9]+\.%(location|parent):' || true
        - sudo -n /usr/local/sbin/dmidecode -t slot
        - messages=$({ tail -n 4000 /var/log/messages 2>/dev/null || sudo -n /usr/bin/tail -n 4000 /var/log/messages 2>/dev/null || true; } | egrep '(mpr[0-9]+:|\(da[0-9]+:mpr[0-9]+:)' || true); if [ -n "$messages" ]; then printf '%s\n' "$messages" | tail -n 400; else dmesg -a | egrep '(mpr[0-9]+:|\(da[0-9]+:mpr[0-9]+:)' | tail -n 400; fi
```

## Optional SAS topology

The app discovers every adapter unit returned by `mprutil show adapters` and
runs the per-unit read commands. For example, adapter unit 10 uses:

```bash
sudo -n /usr/sbin/mprutil -u 10 show adapter
sudo -n /usr/sbin/mprutil -u 10 show devices
sudo -n /usr/sbin/mprutil -u 10 show enclosures
sudo -n /usr/sbin/mprutil -u 10 show expanders
sudo -n /usr/sbin/mprutil -u 10 show iocfacts
```

`pciconf`, filtered `sysctl`, and `dmidecode` add PCI and motherboard slot
labels. The event probe reads filtered `/var/log/messages` when permitted and
falls back to ordered `dmesg` output. Neither source is a persistent hardware
counter.

## Optional identify LED control

CORE can use these `sesutil` forms when the host and enclosure support them:

```bash
sudo -n /usr/sbin/sesutil locate -u /dev/sesX <slot> on
sudo -n /usr/sbin/sesutil locate -u /dev/sesX <slot> off
```

Grant only the required commands. If API SMART data is sparse or slot fields are
missing, check the SSH source status before changing the profile.
