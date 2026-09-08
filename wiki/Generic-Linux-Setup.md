# Generic Linux setup

Generic Linux support uses SSH. It fits `mdadm` hosts, NVMe systems, and Linux
appliances that do not expose a useful disk inventory API. A profile supplies
the physical chassis layout.

## 1. Choose a profile

Use a built-in profile or create one with `slot_hints`. The app cannot infer an
arbitrary chassis shape from `lsblk` alone. See
[[Profiles and Custom Layouts|Profiles-and-Custom-Layouts]].

## 2. Install the host tools

On Ubuntu:

```bash
sudo apt update
sudo apt install -y sudo smartmontools sg3-utils lsscsi mdadm nvme-cli
```

Install only the tools needed by your command list and hardware.

## 3. Create an SSH account

This example creates a key-only `jbodmap` account:

```bash
sudo adduser --disabled-password --gecos "" jbodmap
sudo install -d -m 700 -o jbodmap -g jbodmap /home/jbodmap/.ssh
printf '%s\n' 'ssh-ed25519 REPLACE_WITH_YOUR_PUBLIC_KEY jbodmap@docker-host' | sudo tee /home/jbodmap/.ssh/authorized_keys > /dev/null
sudo chown jbodmap:jbodmap /home/jbodmap/.ssh/authorized_keys
sudo chmod 600 /home/jbodmap/.ssh/authorized_keys
```

Preload and verify the host key before enabling strict host-key checking.

## 4. Install bounded sudo rules

Use the complete generated Linux policy in
[[SSH Setup and Sudo|SSH-Setup-and-Sudo]]. It covers the supported SES reads,
identify commands, `smartctl` forms, `mdadm`, and NVMe probes.

The anchored command regular expressions require sudo 1.9.10 or newer. On an
older host, enumerate exact per-device commands. Do not use argument wildcards.
Save the generated policy as one mode-`0440` file and validate it with
`visudo -cf` before installation.

## 5. Add the system

A small NVMe system can start with `lsblk`, `mdadm`, and `nvme`:

```yaml
systems:
  - id: gpu-server
    label: GPU Server Linux
    default_profile_id: supermicro-sys-2029gp-tr-right-nvme-2
    truenas:
      host: http://gpu-server.example.local
      api_key: ""
      platform: linux
      verify_ssl: true
    ssh:
      enabled: true
      host: gpu-server.example.local
      user: jbodmap
      key_path: /run/ssh/id_truenas
      strict_host_key_checking: true
      commands:
        - /usr/bin/lsblk -OJ
        - sudo -n /usr/sbin/mdadm --detail --scan
        - /usr/sbin/nvme list-subsys -o json
```

The app runs `smartctl` on demand. `nvme list-subsys` adds controller and
subsystem hints. Add SES commands only if the host exposes the corresponding
`/dev/sg*` devices.

## UniFi UNVR

UNVR and UNVR Pro use the generic Linux path. Their built-in profiles are
`ubiquiti-unvr-front-4` and `ubiquiti-unvr-pro-front-7`. The tested inventory
path uses `lsblk`, `mdadm`, `smartctl`, and `ubntstorage` over SSH. It does not
rely on `/sys/class/enclosure`.

Some appliances permit only password authentication or a root SSH account. If
that is the only supported path, keep the credential outside tracked YAML and
restrict network access to the SSH service.

```yaml
systems:
  - id: unvr
    label: UniFi UNVR
    default_profile_id: ubiquiti-unvr-front-4
    truenas:
      host: https://unvr.example.local
      api_key: ""
      platform: linux
      verify_ssl: true
    ssh:
      enabled: true
      host: unvr.example.local
      port: 22
      user: root
      key_path: ""
      password: "REPLACE_ME"
      strict_host_key_checking: true
      commands:
        - /bin/lsblk -OJ
        - /sbin/mdadm --detail --scan
        - /usr/sbin/ubntstorage disk inspect
        - /usr/sbin/ubntstorage space inspect
```

Replace `REPLACE_ME` through the supported secret setting rather than committing
a real password.

## Capability limits

- A host without `/dev/sg*` usually has no SES mapping or SES identify path.
- `slot_hints` may be required to match NVMe controllers to physical bays.
- LED control appears only when the configured host path supports it.
- Vendor APIs may omit per-disk slot data even when SSH inventory works.
