# SSH Setup and Sudo

This page is the practical SSH and sudo guide.

The short version:

- use a dedicated non-root user
- use SSH keys, not passwords
- allow only the exact commands you need
- start narrow and widen only when a real feature needs it

## Recommended SSH User Pattern

- username: `jbodmap`
- shell access only if you need it
- public key auth
- command-limited sudo

## Good Default SSH Material

On the Docker host:

- private key in `./config/ssh/id_truenas`
- pinned host keys in `./data/known_hosts` by default

In app config:

```yaml
ssh:
  enabled: true
  host: storage-host.example.local
  port: 22
  user: jbodmap
  key_path: /run/ssh/id_truenas
  password: ""
  known_hosts_path: /app/data/known_hosts
  strict_host_key_checking: true
```

If the appliance only supports password SSH, set `ssh.password` and leave
`key_path` empty or unset.

With that default, the first successful SSH connection pins the observed host
key into `/app/data/known_hosts`, and later connections must match it unless
you intentionally clear the saved entry.

## CORE Command Ideas

```text
/sbin/glabel status
/usr/local/sbin/zpool status -gP
gmultipath list
sudo -n /usr/sbin/sesutil map
sudo -n /usr/sbin/sesutil show
sudo -n /sbin/camcontrol devlist -v
```

## SCALE Command Ideas

```text
/usr/sbin/zpool status -gP
/usr/bin/lsblk -o NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,HCTL
/usr/bin/lsscsi -g
sudo -n /usr/bin/sg_ses -p aes /dev/sg27
sudo -n /usr/bin/sg_ses -p aes /dev/sg38
sudo -n /usr/bin/sg_ses -p ec /dev/sg27
sudo -n /usr/bin/sg_ses -p ec /dev/sg38
```

## Generic Linux Command Ideas

```text
/usr/bin/lsblk -OJ
sudo -n /usr/sbin/mdadm --detail --scan
/usr/sbin/nvme list-subsys -o json
```

## On-Demand Commands The App Runs Separately

These do not have to live in the standing command list:

- `smartctl -x -j`
- `smartctl -x`
- `nvme smart-log -o json`
- `nvme id-ctrl -o json`
- `nvme id-ns -o json`
- LED identify actions such as `sesutil locate` or `sg_ses --set=ident`

But the SSH user still needs sudo permission for them if the host requires root.

## Example Narrow Sudoers Entries

CORE example:

```text
jbodmap ALL=(root) NOPASSWD: /usr/sbin/sesutil map
jbodmap ALL=(root) NOPASSWD: /usr/sbin/sesutil show
jbodmap ALL=(root) NOPASSWD: /usr/sbin/sesutil locate -u /dev/ses* * on
jbodmap ALL=(root) NOPASSWD: /usr/sbin/sesutil locate -u /dev/ses* * off
jbodmap ALL=(root) NOPASSWD: /sbin/camcontrol devlist -v
```

SCALE example:

```text
jbodmap ALL=(root) NOPASSWD: /usr/bin/sg_ses -p aes /dev/sg*
jbodmap ALL=(root) NOPASSWD: /usr/bin/sg_ses -p ec /dev/sg*
jbodmap ALL=(root) NOPASSWD: /usr/bin/sg_ses --dev-slot-num=* --set=ident /dev/sg*
jbodmap ALL=(root) NOPASSWD: /usr/bin/sg_ses --dev-slot-num=* --clear=ident /dev/sg*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/smartctl -x -j /dev/*
```

Generic Linux NVMe example:

```text
jbodmap ALL=(root) NOPASSWD: /usr/bin/lsblk -OJ
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mdadm --detail --scan
jbodmap ALL=(root) NOPASSWD: /usr/sbin/smartctl -x -j /dev/nvme*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/nvme smart-log -o json /dev/nvme*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/nvme id-ctrl -o json /dev/nvme*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/nvme id-ns -o json /dev/nvme*
```

## When To Widen Permissions

Only widen sudo when a real feature requires it:

- slot correlation is incomplete
- SMART fields are missing
- identify LED control is unavailable
- a Linux host needs controller-native `nvme-cli` data

Do not start with blanket root SSH if command-limited sudo works.
