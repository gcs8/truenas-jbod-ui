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

## Good default SSH material

On the Docker host:

- private key in `./config/ssh/id_truenas`
- pinned host keys in `./data/known_hosts` by default

The known-hosts location is derived from the runtime layout. Do not add
`known_hosts_path` to YAML; configured values are discarded. Inside the default
container layout the derived file is `/app/data/known_hosts`.

Strict checking rejects an unknown key, so preload and verify every SSH target
before enabling the system. Get each fingerprint through a trusted channel, then
compare it with the scan before installation. This example preserves the
configured non-root service ownership and group readability. Run it from the
deployment directory. If `.env` overrides `APP_UID` or `APP_GID`, export the
same values in this shell first:

```bash
app_uid="${APP_UID:-10001}"
app_gid="${APP_GID:-10001}"
ssh_host="storage-host.example.test"
known_hosts_tmp="$(mktemp)"
trap 'rm -f "$known_hosts_tmp"' EXIT
ssh-keyscan -H "$ssh_host" > "$known_hosts_tmp"
ssh-keygen -lf "$known_hosts_tmp"
# Compare the fingerprint out of band before installing the file.
sudo install -o "$app_uid" -g "$app_gid" -m 0660 "$known_hosts_tmp" data/known_hosts
rm -f "$known_hosts_tmp"
trap - EXIT
```

Repeat the scan for every configured host and HA node, appending verified keys
to the temporary file before installation. Do not use a root-owned `0600` file;
the non-root UI process cannot read it.

In app config:

```yaml
ssh:
  enabled: true
  host: storage-host.example.test
  port: 22
  user: jbodmap
  key_path: /run/ssh/id_truenas
  password: ""
  strict_host_key_checking: true
```

If the appliance only supports password SSH, set `ssh.password` and leave
`key_path` empty or unset. Strict host-key checking still needs the verified
preload above.

For example, a CORE config may use host: `truenas-core-a.example.test`. Verify
and preload the key for the actual host name in your own config.

For ESXi specifically, password-only auth is a normal supported case:

```yaml
ssh:
  enabled: true
  host: esxi-host.example.test
  user: root
  key_path: ""
  password: "your-esxi-root-password"
```

Later connections must match the preloaded key. A key mismatch fails closed;
verify the host before replacing its entry.

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

## Generated bootstrap permission previews

The admin sidecar's Sudoers Preview is the canonical copy/paste source. The
blocks below are generated from the same current-main policy used by the one-time
bootstrap. Do not split them into partial SMART and SES files or add command
wildcards. Linux-like policies must pass `visudo -cf` before installation.

Anchored command regexes require sudo 1.9.10 or newer. On older hosts, enumerate
exact per-device commands instead of replacing an argument regex with `*`.
TrueNAS CORE stores its list through middleware, so review the generated
`midclt` payload and the installed sudo version there rather than running
`visudo` against it.

### TrueNAS CORE

<!-- generated-bootstrap-policy:core:start -->
```text
midclt call user.update USER_ID '{"sudo":true,"sudo_nopasswd":true,"sudo_commands":["/usr/sbin/sesutil map","/usr/sbin/sesutil show","/usr/sbin/sesutil locate -u /dev/ses* * on","/usr/sbin/sesutil locate -u /dev/ses* * off","/sbin/camcontrol devlist -v","/usr/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$","/usr/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$","/usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$","/usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$","/usr/local/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$","/usr/local/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$","/usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$","/usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$","/usr/sbin/mprutil show adapter","/usr/sbin/mprutil show adapters","/usr/sbin/mprutil show all","/usr/sbin/mprutil show devices","/usr/sbin/mprutil show enclosures","/usr/sbin/mprutil show expanders","/usr/sbin/mprutil show iocfacts","/usr/sbin/mprutil -u * show adapter","/usr/sbin/mprutil -u * show all","/usr/sbin/mprutil -u * show devices","/usr/sbin/mprutil -u * show enclosures","/usr/sbin/mprutil -u * show expanders","/usr/sbin/mprutil -u * show iocfacts","/usr/local/sbin/dmidecode -t slot","/usr/bin/tail -n 4000 /var/log/messages","/usr/local/bin/midclt call disk.multipath_sync","/usr/local/bin/midclt call disk.sync_all","/usr/local/bin/midclt ^call core\\.get_jobs \\[\\[\\\"id\\\"\\,\\\"=\\\"\\,[0-9]+\\]\\]$"]}'
```
<!-- generated-bootstrap-policy:core:end -->

### TrueNAS SCALE

<!-- generated-bootstrap-policy:scale:start -->
```text
# Managed by truenas-jbod-ui admin bootstrap for jbodmap
Defaults:jbodmap !requiretty
Cmnd_Alias JBODMAP_SCALE_CMDS = /usr/bin/sg_ses ^-p aes /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^-p ec /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^--join --filter /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --set=ident /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --clear=ident /dev/sg[0-9]+$, \
  /usr/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/bin/midclt call disk.sync_all, \
  /usr/bin/midclt ^call core\.get_jobs \[\[\"id\"\,\"=\"\,[0-9]+\]\]$
jbodmap ALL=(root) NOPASSWD: JBODMAP_SCALE_CMDS
```
<!-- generated-bootstrap-policy:scale:end -->

### Generic Linux

<!-- generated-bootstrap-policy:linux:start -->
```text
# Managed by truenas-jbod-ui admin bootstrap for jbodmap
Defaults:jbodmap !requiretty
Cmnd_Alias JBODMAP_LINUX_CMDS = /usr/sbin/mdadm --detail --scan, \
  /usr/bin/sg_ses ^-p aes /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^-p ec /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^--join --filter /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --set=ident /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --clear=ident /dev/sg[0-9]+$, \
  /usr/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/nvme smart-log -o json /dev/nvme*, \
  /usr/sbin/nvme id-ctrl -o json /dev/nvme*, \
  /usr/sbin/nvme id-ns -o json /dev/nvme*
jbodmap ALL=(root) NOPASSWD: JBODMAP_LINUX_CMDS
```
<!-- generated-bootstrap-policy:linux:end -->

### QuantaStor

<!-- generated-bootstrap-policy:quantastor:start -->
```text
# Managed by truenas-jbod-ui admin bootstrap for jbodmap
Defaults:jbodmap !requiretty
Cmnd_Alias JBODMAP_QUANTASTOR_CMDS = /usr/bin/sg_ses ^-p aes /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^-p ec /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^--join --filter /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --set=ident /dev/sg[0-9]+$, \
  /usr/bin/sg_ses ^--dev-slot-num=[0-9]+ --clear=ident /dev/sg[0-9]+$, \
  /usr/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x -j /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$, \
  /usr/local/sbin/smartctl ^-d [A-Za-z0-9][A-Za-z0-9_:+./-]*(\,[A-Za-z0-9][A-Za-z0-9_:+./-]*)* -x /dev/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*(/[A-Za-z0-9_:+-][A-Za-z0-9_.:+-]*){0,2}$
jbodmap ALL=(root) NOPASSWD: JBODMAP_QUANTASTOR_CMDS
```
<!-- generated-bootstrap-policy:quantastor:end -->

For SCALE, Linux, and QuantaStor, save the matching block as one file with
mode `0440`, then run `sudo visudo -cf /etc/sudoers.d/<file>` before moving it
into service. The generated QuantaStor policy contains no `qs` root grant; its
read-only `qs` commands run as the service account with local CLI credentials.

## When To Widen Permissions

Only widen sudo when a real feature requires it:

- slot correlation is incomplete
- SMART fields are missing
- identify LED control is unavailable
- a Linux host needs controller-native `nvme-cli` data

Do not start with blanket root SSH if command-limited sudo works.
