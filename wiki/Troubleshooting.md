# Troubleshooting

This page is the short list of the failures you are most likely to hit.

## The App Starts But The UI Looks Empty

Check:

```bash
curl http://localhost:8080/healthz
docker compose logs -f
```

If `healthz` is not `ok`, fix that first.

## The App Is Up But Slot Mapping Looks Wrong

Common causes:

- SSH enrichment is off
- SES access is missing
- the wrong profile is selected
- a system needs `enclosure_profiles`
- a generic Linux host needs `slot_hints`

Good next steps:

- check the warning banner
- confirm the selected system and enclosure
- inspect `config/config.yaml`
- confirm the SSH user can run the exact inventory commands

## The UI Says A Sudo Command Is Not Allowed

That means the app tried to run a command the SSH user cannot execute.

Fix it by:

- adding the exact command to sudoers
- or deciding you do not want that feature on that host

Do not broaden sudo more than needed.

## SCALE Shows A Generic Runtime Profile

That usually means:

- no built-in profile was matched
- or the system config is missing explicit `enclosure_profiles`

Fix:

- bind the known enclosure IDs to the front/rear built-in profiles

Example:

```yaml
enclosure_profiles:
  "5003048001c1043f": supermicro-ssg-6048r-front-24
  "500304801e977aff": supermicro-ssg-6048r-rear-12
```

## A Linux Host Has No SES Devices

That is not fatal.

It means:

- no SES slot mapping from `/dev/sg*`
- likely no SES-driven LED control

The host can still be useful as:

- an SSH inventory target
- a profile-driven physical layout
- an `mdadm` or NVMe topology target

## SMART Fields Are Missing

Check whether:

- the platform exposes them at all
- SSH enrichment is enabled
- `smartctl` sudo is allowed
- `nvme-cli` sudo is allowed for Linux NVMe enhancement

## The Browser Keeps Showing Old UI

Try:

- hard refresh
- new incognito tab
- rebuild and restart the container

```bash
docker compose up -d --build
```

## Multipath Or Pool Grouping Looks Wrong On CORE

Check whether the SSH user can run:

```text
gmultipath list
camcontrol devlist -v
zpool status -gP
```

Those are the commands that usually fill in the missing context.
