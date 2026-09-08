# TrueNAS SCALE setup

Start with the middleware websocket API. The app does not use the deprecated
REST `/api/v2.0` path. Add SSH only when you need SES slot mapping, live identify
state, SMART detail, or LED control.

## 1. Connect through the API

Create a read-only or appropriately scoped API key in SCALE. Enter the base
host URL without `/api/v2.0`:

```text
https://scale.example.local
```

An API-only system needs this shape:

```yaml
systems:
  - id: scale-a
    label: SCALE A
    truenas:
      host: https://scale.example.test
      api_key: ""
      platform: scale
      verify_ssl: false
    ssh:
      enabled: false
```

Supply the API key through the setup UI or the documented secret setting. Open
the main UI and confirm that disk and pool inventory loads. Some SCALE systems
do not return usable enclosure rows through middleware. That limitation does
not prevent the API-only inventory from working.

This first connection does not verify the appliance certificate. After the UI
works, [[Advanced Configuration|Advanced-Configuration]] explains how to enable
verification with the system trust store or a private CA bundle.

## 2. Add optional SSH enrichment

Use a dedicated `jbodmap` account, SSH key authentication, strict host-key
checking, and command-limited sudo. Replace the example `/dev/sg*` paths with
the SES devices on your host.

```yaml
ssh:
  enabled: true
  host: scale.example.local
  user: jbodmap
  key_path: /run/ssh/id_truenas
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

The app runs `smartctl` on demand for the selected slot, so it does not belong
in the standing command list. The SSH account still needs the bounded sudo
rules for the supported `smartctl` forms.

The app also reads `/sys/class/enclosure` without sudo. Those bindings can map
SATA drives when SES AES pages report one shared SAS address for every bay.

Use the complete generated SCALE policy in
[[SSH Setup and Sudo|SSH-Setup-and-Sudo]]. It includes bounded rules for
`sg_ses -p aes`, `sg_ses -p ec`, `sg_ses --join --filter`, identify on and off,
both supported `smartctl` paths, and middleware disk-sync polling. Do not build
a partial sudo file from the standing command list above.

## 3. Assign enclosure profiles when needed

Pin known enclosure IDs when one SCALE host has several faces:

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
      verify_ssl: true
      enclosure_filter: ""
    ssh:
      enabled: true
      host: scale.example.local
      user: jbodmap
      key_path: /run/ssh/id_truenas
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

A pinned profile preserves the face, tray orientation, and bay labels when
automatic profile matching is not enough. See
[[Profiles and Custom Layouts|Profiles-and-Custom-Layouts]].

If middleware enclosure calls fail, the UI marks that source degraded while
keeping fresh disk and pool data. It retains the last trusted enclosure layout
when one is cached. Advisory non-zero `smartctl` exit codes do not discard valid
command output.
