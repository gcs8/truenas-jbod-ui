# Forward the history publication address from legacy Compose

Use this explicit migration when an installation retains the v0.22.2 Compose
file but runs a newer image with the history publication guard. An image-only
update cannot discover this address. The old history service has no `env_file`
and forwards neither `HISTORY_BIND_ADDRESS` nor
`HISTORY_PUBLISHED_BIND_ADDRESS` to its process.

The migration adds one environment entry. It does not replace the base file,
change ports, select an image, enable profiles, change UID/GID or groups, repair
ownership, or change authentication defaults. Absent or empty
`HISTORY_BIND_ADDRESS` still publishes on `127.0.0.1`, and history refresh auth
still defaults to `network`. An exposed address must satisfy the existing token
and public-origin guard. Main UI and admin authentication are unchanged.

## Check the existing deployment definition first

Work in the installation directory. Keep a private backup of the deployment
files before editing. Do not paste `.env`, full resolved Compose output, or
credentials into an issue or chat.

Record the existing project name, project directory, ordered Compose file list,
ordered `--env-file` arguments, shell interpolation values, and active profiles
from your deployment command or service definition. Preserve all of them. When
you specify `-f`, Compose no longer implicitly adds its default override file.
Include that file explicitly if it was active. Do not substitute a simplified
file list for your actual chain, and do not invent a repository source path for
a local override.

This overlay supports the old publication expression:

```yaml
ports:
  - "${HISTORY_BIND_ADDRESS:-127.0.0.1}:${HISTORY_PORT:-8081}:8001"
```

Review every existing override. If history uses host networking, custom port
expressions, multiple host bindings, or a hard-coded host address unrelated to
`HISTORY_BIND_ADDRESS`, stop here. The supplied overlay does not discover those
addresses. Reconcile that deployment's publication and metadata explicitly;
never report loopback metadata for an exposed port. This is operator-supplied
publication metadata, not Docker discovery or a check of firewall reachability.

## Add the address-only overlay

Create a new `docker-compose.history-bind.yml` in the installation directory
with the following content. Do not overwrite an existing file of that name;
review it instead. This is the same mapping as the checked-in
`docker-compose.history-bind.yml`:

```yaml
services:
  enclosure-history:
    environment:
      HISTORY_PUBLISHED_BIND_ADDRESS: ${HISTORY_BIND_ADDRESS:-127.0.0.1}
```

Append it after the entire existing Compose chain. It deliberately derives the
metadata from the port's `HISTORY_BIND_ADDRESS` expression, not an independent
host-side `HISTORY_PUBLISHED_BIND_ADDRESS` value that could conceal exposure.
No other environment entry is changed.

For example, the following Bash array describes a synthetic installation whose
existing files are `compose.yaml` followed by a local `site.yml`. `site.yml` is
an operator file, not a file to download from this repository. Replace the
example with your complete existing invocation, then append the migration file:

```bash
compose=(docker compose --project-name enclosure-example --env-file .env
  -f compose.yaml -f site.yml
  -f docker-compose.history-bind.yml --profile history --profile backup)
"${compose[@]}" config --quiet
```

`config` renders locally without starting containers or querying the daemon.
Check the effective history publication without printing the full environment:

```bash
"${compose[@]}" config --format json | python3 -c '
import json, sys
h = json.load(sys.stdin)["services"]["enclosure-history"]
ports = h.get("ports", [])
address = h.get("environment", {}).get("HISTORY_PUBLISHED_BIND_ADDRESS")
print(json.dumps({"published_bind_address": address,
                  "network_mode": h.get("network_mode"), "ports": ports}))
if (h.get("network_mode") == "host" or len(ports) != 1
        or ports[0].get("target") != 8001
        or not address or ports[0].get("host_ip") != address):
    sys.exit("STOP: publication does not match the migration metadata")
'
```

The resolved model must differ only by the history metadata entry. Keep all
existing service identities, supplementary groups, mounts, image references,
labels, resource limits, and profiles. Do not add the non-root overlay or run an
ownership migration as part of this change.

## Exposed history needs the existing token policy

With `HISTORY_BIND_ADDRESS=0.0.0.0` or another non-loopback address, the newer
history process will now refuse `network` refresh mode. This is the existing
validator seeing the correct address, not a new default. Either explicitly
return the publication to loopback or configure token mode and an exact public
origin. Do not forge a loopback metadata value to bypass the guard.

The address-only overlay preserves any existing refresh policy. Stock v0.22.2
Compose does not forward the newer refresh settings to history, so setting them
only in `.env` is not enough. If you deliberately choose exposed token mode,
add these entries to your existing local environment override for **both**
services, after other overrides and before the address-only migration file.
This inline-token example requires `HISTORY_REFRESH_TOKEN_FILE` to be absent
from both resolved service environments. If that key exists (even blank), stop
and use the file-backed path below; an inline value cannot override it in the
application loader.

```yaml
services:
  enclosure-history:
    environment:
      HISTORY_REFRESH_AUTH_MODE: token
      HISTORY_REFRESH_TOKEN: ${HISTORY_REFRESH_TOKEN:?Set a nonempty history refresh token}
      HISTORY_PUBLIC_ORIGIN: ${HISTORY_PUBLIC_ORIGIN:?Set the exact history public origin}
  enclosure-ui:
    environment:
      HISTORY_REFRESH_TOKEN: ${HISTORY_REFRESH_TOKEN:?Set a nonempty history refresh token}
```

Set the actual secret privately, and use an origin such as
`https://history.example.test` with your own host and port. Both explicit token
entries use the same Compose interpolation source: shell values take precedence
over CLI `--env-file` inputs, later CLI files override earlier ones, and the
implicit project `.env` applies when no CLI env file is supplied. The old UI's
literal `env_file: .env` is a separate service environment source; it does not
follow your CLI env-file selection. Explicit `environment` entries above take
precedence over that service file, preventing divergent UI/history credentials.
A missing or empty interpolation token or origin refuses this example at
Compose render time; malformed origins still fail the history loader.

`HISTORY_PUBLIC_ORIGIN` configures the history service's browser-origin policy,
not the UI's origin. The UI loader does not consume that key and its backend
refresh sends the configured bearer token, not an Origin header. Preserve the
UI's independent `APP_PUBLIC_ORIGIN` and auth settings; do not overwrite them
with the history origin.

For file-backed tokens instead, preserve the existing scoped read-only secret
mounts and `HISTORY_REFRESH_TOKEN_FILE` in both UI and history. Both paths must
resolve to the same usable secret bytes; identical inline token values alone do
not prove this. Do not use the inline-token example or clear `_FILE` to an empty
string: the loaders prefer `_FILE` whenever present, and a blank path fails.
Keep the explicit history mode and required public-origin entries shown above,
but omit the inline token entry in both services. Never dump resolved token
values or mounted secret contents to compare them. Compose validation cannot
read container-mounted token files or prove application startup acceptance.

## Activate only after review

This migration does not start services. After reviewing the resolved model,
update the saved launch definition to retain the complete file chain with the
new overlay last. Use that same definition for every later recreate or update.
During an operator-approved maintenance window, recreate history with your
existing selected image and verify its health and refresh behavior. If you also
changed the optional token policy, recreate the UI as well so both processes
load the reviewed credentials; verify main-UI refresh authorization separately.
Do not activate the backup or admin profile merely because it appears in an
example.
Container startup, ownership, and network reachability need separate runtime
acceptance; daemon-free `config` checks do not prove them.

Removing the overlay reintroduces the old blind spot. For rollback, keep the
metadata while reverting the selected image, or explicitly return history to
loopback and verify the publication before removing the overlay. Retain the
private pre-migration files for recovery.

## Do not confuse this with a Compose replacement update

The updater shipped in v0.23.0, `scripts/update_immutable_deployment.py`,
downloads and replaces every declared Compose source. It is not image-only and
is not a customization-preserving way to apply this small migration. Do not use
it for this procedure or map a local override to a guessed upstream filename.
Later updater revisions may offer an explicit `--replace-compose` switch.
Check the exact helper revision and its `--help`; do not assume that behavior
exists in the released helper. This procedure needs no updater, no automatic
Compose replacement, and no Docker socket access.
