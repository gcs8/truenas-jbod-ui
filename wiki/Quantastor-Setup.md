# Quantastor setup

Start with the Quantastor REST API. One system entry can represent a shared
front enclosure and up to three HA nodes. Add node SSH only for `qs`, SMART,
SES, SATADOM, or identify detail.

## 1. Connect through the API

In the admin sidecar, add one Quantastor system with the shared API or
management VIP as `truenas.host`:

```yaml
systems:
  - id: example-qs-ha
    label: ExampleQS HA
    default_profile_id: supermicro-ssg-2028r-shared-front-24
    truenas:
      host: https://quantastor.example.test
      api_user: jbodmap
      api_password: replace_me
      platform: quantastor
      verify_ssl: false
      timeout_seconds: 15
    ssh:
      enabled: false
```

Store the real API password through the supported secret setting. Save the
system, open the main UI, and confirm that REST inventory loads. API-only mode
can show the shared enclosure, disks, node context, metrics, and history when
the appliance returns those fields.

This first connection does not verify the appliance certificate. After the UI
works, [[Advanced Configuration|Advanced-Configuration]] explains how to enable
verification with the system trust store or a private CA bundle.

Start the admin sidecar when you need the setup form:

```bash
docker compose --profile admin up -d enclosure-admin
```

## 2. Add optional HA node SSH

Enable SSH only after API inventory works. In the admin UI:

1. Turn on Quantastor HA cluster mode.
2. Use `Load Nodes From Quantastor API` to load node IDs and labels.
3. Add a direct SSH host for each hardware node if the API does not return one.
4. Do not use the shared API or grid-management VIP as an HA node SSH target.
5. Ignore management-only helper VMs that do not share the enclosure hardware.

The equivalent SSH shape is:

```yaml
    ssh:
      enabled: true
      host: 192.0.2.30  # optional legacy/default node fallback; not the API VIP
      ha_enabled: true
      ha_nodes:
        - system_id: 11111111-1111-4111-8111-111111111111
          label: ExampleQS-Left
          host: 192.0.2.30  # recommended for node-targeted SSH
        - system_id: 22222222-2222-4222-8222-222222222222
          label: ExampleQS-Right
          host: 192.0.2.31  # recommended for node-targeted SSH
      port: 22
      user: jbodmap
      key_path: /run/ssh/id_jbodmap
      strict_host_key_checking: true
      timeout_seconds: 15
      commands: []
```

Quantastor owns its default SSH command list, so `commands` normally stays
empty. It can use API-advertised node addresses and `qs network-port-list` data
to choose node targets. A configured `ha_nodes[*].host` remains the fallback
when the API does not provide a usable address.

Strict checking does not learn keys on first connection. Preload and verify
every HA node in the shared derived `known_hosts` file before enabling SSH. Use
the ownership-safe procedure in [[SSH Setup and Sudo|SSH-Setup-and-Sudo]]. Keep
`strict_host_key_checking: true`.

## 3. Prepare the SSH account

The account needs a home directory, a login shell, and `~/.qs.cnf` for local
`qs` authentication. Check it on each node:

```bash
sudo -u jbodmap -H bash -lc 'whoami && hostname && echo $HOME && which qs'
sudo -u jbodmap -H bash -lc 'cat ~/.qs.cnf'
```

`which qs` should return `/usr/bin/qs`. Do not copy the contents of `~/.qs.cnf`
into documentation, tickets, or chat.

Use the complete generated QuantaStor policy in
[[SSH Setup and Sudo|SSH-Setup-and-Sudo]]. It includes bounded SES, identify,
and `smartctl` rules. It does not run `qs` as root. The app runs read-only `qs`
inventory commands as the service account.

The anchored command regular expressions require sudo 1.9.10 or newer. On older
nodes, enumerate exact per-device commands. Do not use argument wildcards. Save
the policy as one mode-`0440` file and validate it with `visudo -cf` before
installation.

## 4. Check optional node capabilities

Run only the checks for features you plan to enable.

```bash
sudo -u jbodmap -H bash -lc 'qs disk-list --json | head'
sudo -u jbodmap -H bash -lc 'qs hw-disk-list --json | head'
sudo -u jbodmap -H bash -lc 'qs hw-enclosure-list --json | head'
```

```bash
sudo -u jbodmap -H bash -lc 'sudo -n /usr/sbin/smartctl -x /dev/sdf | head -40'
```

```bash
sudo -u jbodmap -H bash -lc 'sudo -n /usr/bin/sg_ses -p aes /dev/sg11 | head -60'
sudo -u jbodmap -H bash -lc 'sudo -n /usr/bin/sg_ses -p ec /dev/sg11 | head -60'
```

If only one node exposes the working SES path, keep that node configured so the
app can target it for enclosure detail and supported LED actions.

## 5. Add node-specific storage views

Use `binding.target_system_id` to bind SATADOM or other internal-device views to
one HA node:

```yaml
storage_views:
  - id: boot-satadoms-left
    label: Boot SATADOMs Left
    kind: boot_devices
    template_id: satadom-pair-2
    enabled: true
    binding:
      mode: hybrid
      target_system_id: 11111111-1111-4111-8111-111111111111
      serials:
        - SANITIZED-SATADOM-1
        - SANITIZED-SATADOM-2
```

The shared face remains a live enclosure. Node-bound internal disks appear as
virtual storage views.

## Limits

- REST and `qs` identify methods do not work on every LSI path. The app uses
  `sg_ses` where that path has been validated.
- The API may return node IDs and labels without usable SSH addresses.
- Shared enclosure and node ownership data depend on what the appliance reports.

See [[Admin UI and System Setup|Admin-UI-and-System-Setup]] and
[[Troubleshooting]] for setup and source-status checks.
