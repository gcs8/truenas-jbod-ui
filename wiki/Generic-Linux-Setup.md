# Generic Linux Setup

Use this page for non-TrueNAS hosts where the app should still render a physical
disk layout.

The current generic Linux path is SSH-only.

Today it is best for:

- `mdadm` hosts
- NVMe-heavy systems
- hosts where physical disk placement matters but no appliance API exists
- appliance-style Linux boxes such as UniFi UNVR where SSH works but the vendor
  API does not expose per-disk slot inventory

## What Generic Linux Can Do Today

- `lsblk` inventory
- `mdadm --detail --scan` topology hints
- `nvme list-subsys -o json` controller/subsystem mapping
- on-demand `smartctl`
- optional `nvme-cli` enrichment
- profile-driven slot rendering

## Important Limit

Generic Linux does not magically infer arbitrary chassis geometry.

You need either:

- a built-in profile
- or a custom profile with `slot_hints`

## 1. Install The Packages

Ubuntu example:

```bash
sudo apt update
sudo apt install -y sudo smartmontools sg3-utils lsscsi mdadm nvme-cli
```

## 2. Create The SSH User

```bash
sudo adduser --disabled-password --gecos "" jbodmap
sudo install -d -m 700 -o jbodmap -g jbodmap /home/jbodmap/.ssh
printf '%s\n' 'ssh-ed25519 REPLACE_WITH_YOUR_PUBLIC_KEY jbodmap@docker-host' | sudo tee /home/jbodmap/.ssh/authorized_keys > /dev/null
sudo chown jbodmap:jbodmap /home/jbodmap/.ssh/authorized_keys
sudo chmod 600 /home/jbodmap/.ssh/authorized_keys
```

## 3. Minimal Generic Linux Sudo

```bash
sudo tee /etc/sudoers.d/jbodmap-storage > /dev/null <<'EOF'
Defaults:jbodmap !requiretty
jbodmap ALL=(root) NOPASSWD: /usr/bin/lsblk -OJ
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mdadm --detail --scan
jbodmap ALL=(root) NOPASSWD: /usr/sbin/smartctl -x -j /dev/sd*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/smartctl -x -j /dev/nvme*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/nvme smart-log -o json /dev/nvme*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/nvme id-ctrl -o json /dev/nvme*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/nvme id-ns -o json /dev/nvme*
EOF

sudo chmod 440 /etc/sudoers.d/jbodmap-storage
sudo visudo -cf /etc/sudoers.d/jbodmap-storage
```

## 4. Example Generic Linux System Config

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
      known_hosts_path: /app/data/known_hosts
      strict_host_key_checking: true
      commands:
        - /usr/bin/lsblk -OJ
        - sudo -n /usr/sbin/mdadm --detail --scan
        - /usr/sbin/nvme list-subsys -o json
```

Password-only appliance example:

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
      known_hosts_path: /app/data/known_hosts
      strict_host_key_checking: true
      commands:
        - /bin/lsblk -OJ
        - /sbin/mdadm --detail --scan
        - /usr/sbin/ubntstorage disk inspect
        - /usr/sbin/ubntstorage space inspect
```

## 5. UniFi UNVR Family Notes

The current first-pass UniFi UNVR path is generic Linux, not a dedicated
vendor adapter.

What was validated on the tested UNVR and UNVR Pro units:

- Debian 11 userspace on the `alpine-unvr` kernel family
- direct `smartctl` access to SATA disks over SSH
- `mdadm` arrays for the appliance storage layout
- `ubntstorage disk inspect` as the most useful on-box vendor slot source
- no usable `/sys/class/enclosure` path on either tested unit

What is still missing:

- a documented per-disk Protect API endpoint for slot inventory
- a validated SES/LED path across the family

The current built-in profile is:

- `ubiquiti-unvr-front-4` for the regular `4`-bay UNVR front view
- `ubiquiti-unvr-pro-front-7` for the first-pass UNVR Pro `3-over-4` front view

## 6. SES On Generic Linux

If the host exposes SES devices like `/dev/sg*`, the app may be able to do
enclosure mapping or LED work.

If it does not, the app can still be very useful as an inventory-only physical
layout tool.

## 7. Common Generic Linux Notes

- `nvme list-subsys` is excellent for controller-to-slot hints.
- `smartctl` is still the base path for SMART detail.
- `nvme-cli` adds cleaner controller-native metadata on top.
- No `/dev/sg*` usually means no SES-driven LED path on that host.
