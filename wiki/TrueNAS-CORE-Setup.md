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
- `mprutil`

## What CORE Can Do Well

- API-driven disk and pool inventory
- slot mapping from API or SSH enrichment
- SES identify LED control through `sesutil`
- multipath detail
- SAS SMART detail
- SAS fabric/topology diagnostics through read-only `mprutil` probes

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

Optional extra source:

```yaml
    - sudo -n /sbin/camcontrol devlist -v
```

## 4. Example Single-System CORE Config

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
      known_hosts_path: /app/data/known_hosts
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

## 5. Optional SAS Fabric / Topology Diagnostics

For dual-HBA, dual-path, or expander-heavy CORE systems, the topology work uses
read-only `mprutil` output. The standing runtime commands above collect the
non-unit summary, HBA PCI addresses through `pciconf -lv`, physical PCIe slot
names through `dmidecode -t slot`, kernel PCI topology hints through filtered
`sysctl dev.mpr.N.%location/%parent`, and filtered `/var/log/messages` MPR/CAM
event evidence with `dmesg` fallback. The `pciconf` and `sysctl` lines do not
need sudo. If `/var/log/messages` is root-only, timestamped syslog evidence
needs the narrow `/usr/bin/tail -n 4000 /var/log/messages` sudo entry; without
it, the same probe falls back to `dmesg -a` event order. The service account
also needs sudo permission for `dmidecode -t slot` and per-HBA
`mprutil` forms. The app discovers every adapter unit reported by
`mprutil show adapters`, so a host with `/dev/mpr10` would be probed as:

```bash
sudo -n /usr/sbin/mprutil -u 10 show adapter
sudo -n /usr/sbin/mprutil -u 10 show devices
sudo -n /usr/sbin/mprutil -u 10 show enclosures
sudo -n /usr/sbin/mprutil -u 10 show expanders
sudo -n /usr/sbin/mprutil -u 10 show iocfacts
```

Use wildcarded `sudo_commands` entries like
`/usr/sbin/mprutil -u * show expanders` so systems with one HBA or many HBAs
do not need a separate allow-list edit for each controller number.

For TrueNAS CORE middleware, the topology-capable one-liner shape is:

```bash
midclt call user.update USER_ID '{"sudo":true,"sudo_nopasswd":true,"sudo_commands":["/usr/sbin/sesutil map","/usr/sbin/sesutil show","/sbin/camcontrol devlist -v","/usr/sbin/sesutil locate -u /dev/ses* * on","/usr/sbin/sesutil locate -u /dev/ses* * off","/usr/local/sbin/smartctl -x -j *","/usr/local/sbin/smartctl -x *","/usr/sbin/mprutil show adapter","/usr/sbin/mprutil show adapters","/usr/sbin/mprutil show all","/usr/sbin/mprutil show devices","/usr/sbin/mprutil show enclosures","/usr/sbin/mprutil show expanders","/usr/sbin/mprutil show iocfacts","/usr/sbin/mprutil -u * show adapter","/usr/sbin/mprutil -u * show all","/usr/sbin/mprutil -u * show devices","/usr/sbin/mprutil -u * show enclosures","/usr/sbin/mprutil -u * show expanders","/usr/sbin/mprutil -u * show iocfacts","/usr/local/sbin/dmidecode -t slot","/usr/bin/tail -n 4000 /var/log/messages"]}'
```

## 6. Optional LED Control

If your CORE host allows it, the app can use:

```bash
sudo -n /usr/sbin/sesutil locate -u /dev/sesX <slot> on
sudo -n /usr/sbin/sesutil locate -u /dev/sesX <slot> off
```

Keep that sudo as narrow as possible.

## 7. Common CORE Notes

- API-only mode can still be useful if you only want disk and pool metadata.
- SSH is what usually unlocks the best slot correlation.
- `camcontrol devlist -v` is especially useful on dual-path SAS systems.
- `mprutil` is read-only here and only needed for the richer SAS fabric view.
- `dmidecode -t slot` is read-only and lets the app label HBAs with the
  motherboard PCIe slot designation, such as `CPU2 SLOT1 PCI-E 3.0 X8`.
- Filtered `/var/log/messages` MPR/CAM events are used as timestamped recent
  fault evidence, with `dmesg` order as a fallback. On CORE builds where
  `/var/log/messages` is root-only, add `/usr/bin/tail -n 4000 /var/log/messages`
  to the command-limited sudo list for timestamps; neither source is a
  persistent hardware counter.
- If a slot shows transport or cache fields as missing, check whether API SMART is sparse and whether SSH enrichment is allowed.
