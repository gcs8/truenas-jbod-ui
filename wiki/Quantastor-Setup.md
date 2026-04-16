# Quantastor Setup

This page is the practical setup path for the current first-pass Quantastor
support.

Validated target so far:

- OSNexus Quantastor on a Supermicro `SSG-2028R-DE2CR24L`
- shared front `24`-slot face
- REST-first inventory with optional SSH, `qs`, `smartctl`, and `sg_ses`
  enrichment

## What Works Today

- storage-system and pool visibility
- shared-face slot map rendering
- per-slot detail with pool-owner and HA context
- SSH `qs` CLI enrichment when the appliance API is thin
- host `smartctl` enrichment for verified power-on, form factor, cache, and
  other SMART fields
- SSH `sg_ses` identify LED control when one HA node exposes a working SES path

## 1. Add The System To `config.yaml`

Use a block shaped like this:

```yaml
- id: qs-cryostorage
  label: QS CryoStorage
  default_profile_id: supermicro-ssg-2028r-shared-front-24
  truenas:
    host: https://10.13.37.40
    api_user: jbodmap
    api_password: replace_me
    platform: quantastor
    verify_ssl: true
    timeout_seconds: 15
  ssh:
    enabled: true
    host: 10.13.37.30
    extra_hosts:
      - 10.13.37.31
    port: 22
    user: jbodmap
    key_path: /run/ssh/id_truenas
    known_hosts_path: /app/data/known_hosts
    strict_host_key_checking: true
    timeout_seconds: 15
    commands: []
```

Notes:

- `truenas.host` should point at the Quantastor cluster or management VIP
- `ssh.host` should point at one HA node
- `ssh.extra_hosts` should include the peer node if LED or SES access may only
  work there
- keep `default_profile_id` set to
  `supermicro-ssg-2028r-shared-front-24` for the validated `1 x 24` face

## 2. Prepare The SSH User

The app works best when the SSH user has:

- a real home directory
- a real login shell
- a `~/.qs.cnf` file for local `qs` console authentication

Typical sanity checks:

```bash
sudo -u jbodmap -H bash -lc 'whoami && hostname && echo $HOME && which qs'
sudo -u jbodmap -H bash -lc 'cat ~/.qs.cnf'
```

Expected shape:

- the user prints as `jbodmap`
- `which qs` returns `/usr/bin/qs`
- `~/.qs.cnf` exists for local CLI auth

## 3. Add The Useful `sudoers` Rules

The app does not need blanket root, but Quantastor is much more useful when the
SSH user can run `smartctl` and `sg_ses` without a password.

SMART:

```bash
sudo tee /etc/sudoers.d/jbodmap-smartctl >/dev/null <<'EOF'
Defaults:jbodmap !requiretty
jbodmap ALL=(root) NOPASSWD: /usr/sbin/smartctl -x -j /dev/sd*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/smartctl -x /dev/sd*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/smartctl -x -j /dev/disk/by-id/scsi-*
jbodmap ALL=(root) NOPASSWD: /usr/sbin/smartctl -x /dev/disk/by-id/scsi-*
EOF

sudo chmod 440 /etc/sudoers.d/jbodmap-smartctl
sudo visudo -cf /etc/sudoers.d/jbodmap-smartctl
```

SES and identify LEDs:

```bash
sudo tee /etc/sudoers.d/jbodmap-sg_ses >/dev/null <<'EOF'
Defaults:jbodmap !requiretty
jbodmap ALL=(root) NOPASSWD: /usr/bin/sg_ses -p aes /dev/sg*
jbodmap ALL=(root) NOPASSWD: /usr/bin/sg_ses -p ec /dev/sg*
jbodmap ALL=(root) NOPASSWD: /usr/bin/sg_ses --dev-slot-num=* --set=ident /dev/sg*
jbodmap ALL=(root) NOPASSWD: /usr/bin/sg_ses --dev-slot-num=* --clear=ident /dev/sg*
EOF

sudo chmod 440 /etc/sudoers.d/jbodmap-sg_ses
sudo visudo -cf /etc/sudoers.d/jbodmap-sg_ses
```

## 4. Sanity-Check The Node Capabilities

CLI:

```bash
sudo -u jbodmap -H bash -lc 'qs disk-list --json | head'
sudo -u jbodmap -H bash -lc 'qs hw-disk-list --json | head'
sudo -u jbodmap -H bash -lc 'qs hw-enclosure-list --json | head'
```

SMART:

```bash
sudo -u jbodmap -H bash -lc 'sudo -n /usr/sbin/smartctl -x /dev/sdf | head -40'
```

SES:

```bash
sudo -u jbodmap -H bash -lc 'sudo -n /usr/bin/sg_ses -p aes /dev/sg11 | head -60'
sudo -u jbodmap -H bash -lc 'sudo -n /usr/bin/sg_ses -p ec /dev/sg11 | head -60'
```

If one node only shows a broken short-status SES path, keep it in
`ssh.extra_hosts` anyway. The app now prefers the working cached SES host when
it finds one.

## 5. Understand The Current HA Model

The current release is intentionally practical, not magical:

- each storage node is still a selectable view
- the app can now default back onto the active pool owner when there is no
  explicit node selection
- slot detail shows:
  - `Presented By`
  - `Pool Active On`
  - `I/O Fence On`
  - `Visible On`
  - `SES Host`

This is enough to be operationally useful on the validated cluster without
pretending the whole appliance is a single perfectly-abstracted enclosure.

## 6. Known Limits

- Quantastor support is still intentionally first-pass in the current release
- the documented REST and `qs` identify methods are still failing on the
  validated LSI path, so the app prefers `sg_ses`
- SES host discovery is still based on the configured SSH host plus
  `ssh.extra_hosts`
- the current validation is centered on one real shared-front `24`-bay cluster,
  not a broad hardware matrix

For deeper setup detail, use:

- [[SSH Setup and Sudo|SSH-Setup-and-Sudo]]
- [[Troubleshooting]]
