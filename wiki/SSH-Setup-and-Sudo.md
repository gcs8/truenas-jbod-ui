# SSH Setup and Sudo

This page is the practical SSH and sudo guide.

The short version:

- use a dedicated non-root user
- use SSH keys, not passwords
- allow only the exact commands you need
- start narrow and widen only when a real feature needs it

Current ESXi support is the exception to that pattern: it is SSH-only,
read-only, and intentionally skips the Linux bootstrap/sudo flow. On the
validated ESXi host the saved SSH user stays `root`, and the app uses direct
read-only runtime commands instead of trying to synthesize Linux sudo rules.
If the host is using password auth, leave `key_path` blank and use the admin
sidecar's `Password Only / No Key` mode instead of forcing a fake key path.

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

For ESXi specifically, password-only auth is a normal supported case:

```yaml
ssh:
  enabled: true
  host: truenas-core-a.example.local
  user: root
  key_path: ""
  password: "your-esxi-root-password"
```

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
sudo -n /usr/sbin/mprutil show adapters
sudo -n /usr/sbin/mprutil show adapter
sudo -n /usr/sbin/mprutil show devices
sudo -n /usr/sbin/mprutil show enclosures
sudo -n /usr/sbin/mprutil show expanders
sudo -n /usr/sbin/mprutil show iocfacts
/usr/sbin/pciconf -lv
sysctl -a 2>/dev/null | egrep '^dev\.mpr\.[0-9]+\.%(location|parent):' || true
sudo -n /usr/local/sbin/dmidecode -t slot
messages=$({ tail -n 4000 /var/log/messages 2>/dev/null || sudo -n /usr/bin/tail -n 4000 /var/log/messages 2>/dev/null || true; } | egrep '(mpr[0-9]+:|\(da[0-9]+:mpr[0-9]+:)' || true); if [ -n "$messages" ]; then printf '%s\n' "$messages" | tail -n 400; else dmesg -a | egrep '(mpr[0-9]+:|\(da[0-9]+:mpr[0-9]+:)' | tail -n 400; fi
```

The `pciconf` and `sysctl` lines are normal read-only user commands.
`/var/log/messages` adds timestamped recent MPR/CAM kernel fault evidence when
the file is readable or the user has the narrow
`/usr/bin/tail -n 4000 /var/log/messages` sudo entry; otherwise the same probe
falls back to `dmesg -a` event order. `pciconf` gives the HBA PCI bus address,
filtered `sysctl` adds kernel PCI topology hints such as
`dbsf=pci0:130:0:0`, and `dmidecode -t slot` needs sudo on CORE so the app can
join that PCI address to the motherboard slot designation.

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

## ESXi Command Ideas

```text
vmware -v
esxcli system version get
esxcli software vib list
esxcli storage core adapter list
esxcli storage core device list
esxcli storage core path list
esxcli storage filesystem list
esxcli storage vmfs extent list
esxcli storage san sas list
/opt/lsi/storcli64/storcli64 /c0 show all J
/opt/lsi/storcli64/storcli64 /c0/vall show all J
/opt/lsi/storcli64/storcli64 /c0/eall/sall show all J
```

On validated Broadcom / AVAGO MegaRAID hosts, `lsi_mr3` and
`lsuv2-lsiv2-drivers-plugin` alone are not enough for the richer member-detail
path. If StorCLI is missing, the admin sidecar's `Host Prep / Vendor Tool
Upload` panel is the intended place to stage and install an operator-supplied
Broadcom bundle or VIB. The project does not ship that vendor package itself.

## On-Demand Commands The App Runs Separately

These do not have to live in the standing command list:

- `smartctl -x -j`
- `smartctl -x`
- `nvme smart-log -o json`
- `nvme id-ctrl -o json`
- `nvme id-ns -o json`
- LED identify actions such as `sesutil locate` or `sg_ses --set=ident`
- CORE SAS fabric probes such as `mprutil -u N show expanders`

But the SSH user still needs sudo permission for them if the host requires root.

ESXi does not use Linux sudo. Keep that path as direct read-only root or
key-based SSH instead of trying to reuse the CORE/SCALE/Linux sudoers model.
If you do not want to touch the host OS at all, the current Supermicro FatTwin
path can also run as `ipmi` / BMC-only inventory and skip ESXi SSH entirely,
with ESXi added later only as optional enrichment.

## Example Narrow Sudoers Entries

CORE example:

```text
jbodmap ALL=(root) NOPASSWD: /usr/sbin/sesutil map
jbodmap ALL=(root) NOPASSWD: /usr/sbin/sesutil show
jbodmap ALL=(root) NOPASSWD: /usr/sbin/sesutil locate -u /dev/ses* * on
jbodmap ALL=(root) NOPASSWD: /usr/sbin/sesutil locate -u /dev/ses* * off
jbodmap ALL=(root) NOPASSWD: /sbin/camcontrol devlist -v
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil show adapter
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil show adapters
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil show all
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil show devices
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil show enclosures
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil show expanders
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil show iocfacts
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil -u * show adapter
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil -u * show all
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil -u * show devices
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil -u * show enclosures
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil -u * show expanders
jbodmap ALL=(root) NOPASSWD: /usr/sbin/mprutil -u * show iocfacts
jbodmap ALL=(root) NOPASSWD: /usr/local/sbin/dmidecode -t slot
jbodmap ALL=(root) NOPASSWD: /usr/bin/tail -n 4000 /var/log/messages
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
