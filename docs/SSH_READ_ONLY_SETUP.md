# Read-Only SSH Setup for Live Slot Mapping

## Goal

Give the Docker app enough SSH access to run inventory commands like:

- `glabel status`
- `gmultipath list`
- `zpool status -gP`
- `camcontrol devlist -v`
- `sesutil map`
- `sesutil show`

and, optionally, identify-drive commands like:

- `sesutil locate -u /dev/sesN <element> on`
- `sesutil locate -u /dev/sesN <element> off`

while avoiding `root` unless we prove it is actually necessary.

## Safety First

This is the recommended order of operations:

1. Create a dedicated non-root account.
2. Use SSH public-key authentication only.
3. Do not grant `Permit Sudo` yet.
4. Test the required commands one by one.
5. Only loosen permissions if a specific command fails and we understand why.

This is intentionally slower than "just use root", but it is much safer for a
production system.

## Official TrueNAS Guidance

As of April 12, 2026, the official TrueNAS documentation supports these core
principles:

- Use limited-permission accounts and avoid unnecessary root access:
  [Security Recommendations](https://www.truenas.com/docs/solutions/optimizations/security/)
- User accounts support an `SSH Public Key`, `Disable Password`, `Shell`, and
  `Permit Sudo` setting:
  [CORE 13.3 Print View](https://www.truenas.com/docs/core/13.3/coretutorials/printview/)
- The SSH service can disable password authentication globally, which means SSH
  keys are required for all users:
  [SSH Service](https://www.truenas.com/docs/core/13.3/uireference/services/servicesssh/)

Inference from those docs:

- the safest supported starting point is a dedicated user with an SSH public
  key, no sudo, and password authentication disabled
- `scponly` and `nologin` are not appropriate for this app because the app
  needs to execute shell commands remotely

## Recommended Baseline Configuration

### 1. Create a dedicated local group

In the TrueNAS CORE web UI:

- go to `Accounts > Groups`
- create a group such as `jbodmap`

Use a dedicated group so the SSH account does not inherit unrelated access.

### 2. Create a dedicated local user

In the TrueNAS CORE web UI:

- go to `Accounts > Users`
- create a user such as `jbodmap`
- set the primary group to `jbodmap`
- leave auxiliary groups empty unless a specific command later proves it needs one
- set the shell to `sh`
- leave `Permit Sudo` disabled
- paste the public key into `SSH Public Key`

Recommended first attempt:

- set `Disable Password` to `Yes`

Why:

- this blocks password-based logins for the account
- the app is meant to use key-based SSH only

Important caveat:

- the docs clearly describe the password being disabled for password-based access
- key-based SSH with an `SSH Public Key` is the intended companion setup, but on
  your exact build we should still test it before assuming success

If key-based SSH unexpectedly fails with `Disable Password = Yes`, the fallback
is:

- set a long random password
- keep using SSH keys only
- disable password authentication at the SSH service level instead

That is still fairly safe because the SSH daemon will reject password auth.

### 3. Configure the SSH service conservatively

In `Services > SSH`:

- enable SSH
- disable password authentication if your current workflow allows it
- do not enable root password login
- leave extra features off unless you actually need them

For this app, you do not need:

- TCP forwarding
- agent forwarding
- X11 forwarding

If those options are enabled today for other reasons, leave them alone for now.
The important part for the app is key-based login and no blanket sudo.

## Docker Host Key Setup

On the Docker host, keep a dedicated key for this app in:

- `./config/ssh/id_truenas`

and mount it read-only into the container, which the compose file already does.

Example OpenSSH key generation on the Docker host:

```bash
ssh-keygen -t ed25519 -f ./config/ssh/id_truenas -C "truenas-jbod-ui"
```

If you want maximum compatibility with older SSH tooling, RSA is also
acceptable:

```bash
ssh-keygen -t rsa -b 4096 -f ./config/ssh/id_truenas -C "truenas-jbod-ui"
```

Paste the public key from:

- `./config/ssh/id_truenas.pub`

into the TrueNAS user's `SSH Public Key` field.

## First Test Matrix

Before enabling SSH in the app, test each command manually from the Docker host.

Example:

```bash
ssh -i ./config/ssh/id_truenas jbodmap@TRUENAS_HOST 'zpool status -gP'
ssh -i ./config/ssh/id_truenas jbodmap@TRUENAS_HOST 'glabel status'
ssh -i ./config/ssh/id_truenas jbodmap@TRUENAS_HOST 'gmultipath list'
ssh -i ./config/ssh/id_truenas jbodmap@TRUENAS_HOST 'camcontrol devlist -v'
ssh -i ./config/ssh/id_truenas jbodmap@TRUENAS_HOST 'sesutil map'
ssh -i ./config/ssh/id_truenas jbodmap@TRUENAS_HOST 'sesutil show'
```

What we want:

- the login succeeds using the key
- each command returns output without prompting
- no command requires sudo

## If a Command Fails

Do not jump straight to `root`.

Instead:

1. Record the exact command.
2. Record the exact stderr or permission error.
3. Keep all other settings unchanged.
4. Decide on the narrowest possible adjustment from there.

Possible outcomes:

- only one hardware command needs more privilege
- all commands work fine as the non-root user
- a root-only path turns out to be required on this specific box

Only the third case justifies even considering root access.

## What Happened On This System

On this specific TrueNAS CORE system, the dedicated non-root account reached a
good middle ground:

- key-based SSH login works
- `/sbin/glabel status` works without sudo
- `gmultipath list` works without sudo
- `/usr/local/sbin/zpool status -gP` works without sudo
- `/usr/sbin/sesutil show` and `sesutil map` fail with `Permission denied`
- `/sbin/camcontrol devlist -v` also fails with `Permission denied`

The decisive detail was the device-node permissions:

- `/dev/ses*` and `/dev/xpt0` are owned by `root:operator`
- mode is `020600`

That means adding an auxiliary group alone does not help here because there are
no group read bits to take advantage of. Changing device permissions or devfs
rules would be a broader system-level change than we want for this app.

## Recommended Least-Privilege Escalation

For this hardware, the smallest practical next step is command-limited sudo for
`sesutil`, while still keeping the account non-root and using SSH keys for
login.

Why `sesutil` first:

- `sesutil show` and `sesutil map` are the missing pieces for automatic slot
  mapping
- `glabel` and `zpool` already work as the plain non-root user
- `camcontrol` is useful but optional for this app's first-pass mapping
- `sesutil locate` is only needed if you want the web UI to drive identify LEDs

Important caveat:

- this CORE build matches sudo rules against the full command string
- allowing `/usr/sbin/sesutil` alone was not enough on this box
- the working allow-list needs exact entries such as `/usr/sbin/sesutil map`,
  `/usr/sbin/sesutil show`, and the `sesutil locate` patterns you choose to allow

## CLI Update Path

If the web UI does not expose per-command sudo settings, use the TrueNAS shell
as `root` and update the user through middleware.

First, confirm the middleware user id:

```bash
midclt call user.query '[["username","=","jbodmap"]]'
```

Look for the returned `"id"` field and use that value in the later
`user.update` commands.

Example:

```json
[{"id": 54, "username": "jbodmap", ...}]
```

In that case, replace `USER_ID` below with `54`.

On this system as of April 12, 2026, the app saw:

- middleware user id `54`
- POSIX uid `1012`

Then apply the narrow sudo list and enable the user's sudo permission.

Important detail for this CORE build:

- the live middleware schema exposes `sudo_commands` plus a boolean
  `sudo_nopasswd`
- it does not expose a separate `sudo_commands_nopasswd` field

That means the current working-shaped payload for this system is:

```bash
midclt call user.update USER_ID '{"sudo":true,"sudo_nopasswd":false,"sudo_commands":["/usr/sbin/sesutil map","/usr/sbin/sesutil show"]}'
```

If you want the full current web UI feature set on this CORE box, use this
combined allow-list:

```bash
midclt call user.update USER_ID '{"sudo":true,"sudo_nopasswd":false,"sudo_commands":["/usr/sbin/sesutil map","/usr/sbin/sesutil show","/sbin/camcontrol devlist -v","/usr/sbin/sesutil locate -u /dev/ses* * on","/usr/sbin/sesutil locate -u /dev/ses* * off"]}'
```

That enables:

- SSH slot mapping through `sesutil map`
- SSH slot metadata overlay through `sesutil show`
- SSH identify LED control through `sesutil locate`
- multipath controller labels such as `mpr0` and `mpr1` through
  `camcontrol devlist -v`

This wildcard guidance for `sesutil locate` is an inference from standard
`sudoers` command matching, which supports shell-style wildcards in command
arguments. Validate it on your specific CORE build before relying on it
broadly.

## Important Behavior On This CORE Build

On this system, the middleware accepts:

- `sudo=true`
- `sudo_commands=["/usr/sbin/sesutil map","/usr/sbin/sesutil show"]`
- `sudo_nopasswd=false`

and the resulting SSH behavior still prompts for a password:

- `sudo /usr/sbin/sesutil show` requires the user's local password

Earlier failed attempts on this same system showed:

- `sudo_commands=["/usr/sbin/sesutil"]` was too broad in the wrong way and did
  not match the exact subcommands we needed
- `sesutil locate` also needs to be matched explicitly enough for your chosen
  sudo pattern
- `sudo=false` with `sudo_commands=[...]` still produced `not allowed to execute`
- `sudo_nopasswd=true` did not yield passwordless command-limited sudo on this
  build

Because of that, the safest remaining least-privilege path is:

1. keep the dedicated non-root key-based SSH user
2. set `sudo=true`
3. keep `sudo_commands` restricted to the exact SES commands needed
4. set a strong local password on the user
5. keep using SSH keys for login
6. have the app feed that password only to the limited `sudo` commands

This preserves the narrow command list and avoids broad passwordless sudo.

## Last-Resort Root Option

If one or more required commands provably cannot run under a non-root account on
your TrueNAS CORE system, the last-resort fallback is:

- a dedicated SSH key for `root`
- password login disabled
- the app configured to use only the documented inventory commands

Even then, do not test write actions from SSH. The app's SSH layer is intended
for read-only enrichment only, except for chassis identify LEDs when you
explicitly choose to enable `sesutil locate`.

This is not the preferred plan. It is the fallback only if the least-privilege
approach fails on your hardware.

## Suggested App Settings After SSH Works

Once manual SSH tests succeed, set:

```env
SSH_ENABLED=true
SSH_HOST=10.13.37.10
SSH_PORT=22
SSH_USER=jbodmap
SSH_KEY_PATH=/run/ssh/id_truenas
SSH_STRICT_HOST_KEY_CHECKING=true
```

If you enable strict host key checking, also mount a `known_hosts` file and
set:

```env
SSH_KNOWN_HOSTS_PATH=/run/ssh/known_hosts
```

For this system, the preferred SSH command list is:

```yaml
commands:
  - /sbin/glabel status
  - /usr/local/sbin/zpool status -gP
  - gmultipath list
  - sudo -n /usr/sbin/sesutil map
  - sudo -n /usr/sbin/sesutil show
```

Optional:

```yaml
  - sudo -n /sbin/camcontrol devlist -v
  - sudo -n /usr/sbin/sesutil locate -u /dev/ses4 16 on
  - sudo -n /usr/sbin/sesutil locate -u /dev/ses4 16 off
```

`camcontrol devlist -v` is the safest optional add-on if you want the app to
label multipath member paths with controller names such as `mpr0` and `mpr1`.

If your TrueNAS CORE build requires passworded command-limited sudo instead of
`nopasswd`, keep the same command list and also set:

```env
SSH_SUDO_PASSWORD=your-long-random-password
```

## Practical Recommendation for Your System

Start with this exact stance:

- non-root user
- key-only SSH
- `Permit Sudo` off at first
- no extra groups
- test the five inventory commands one at a time

If `sesutil` alone is the blocker, prefer command-limited sudo for the exact
`sesutil` subcommands over broader alternatives such as root SSH or devfs
changes.

If that works, it is the best outcome.

If it partly works, we adjust only where needed.

If it does not work at all, then and only then do we discuss a tightly
controlled root fallback.
