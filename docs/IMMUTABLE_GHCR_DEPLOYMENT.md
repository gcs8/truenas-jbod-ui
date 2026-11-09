# Immutable GHCR deployment

This maintainer and controlled-operations runbook pins an image and deployment helper to one source revision while preserving the live Compose chain by default. For a normal installation, use the [Docker and GHCR deployment](../wiki/Docker-and-GHCR-Deployment.md) guide instead.

## Pick an image reference

Every registry tag is a mutable pointer, including `latest`, version tags, and `sha-...` tags. Release automation intends version tags to stay stable, but the registry can still repoint them. Only a digest reference is immutable:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui@sha256:<64-hex-digest>
```

The GHCR publish workflow records this exact reference under **Immutable manifest digest** in its job summary. Use that receipt, or resolve and verify the tag locally, before a controlled deployment.

## Update the app by digest

An image digest does not freeze a Compose file downloaded from `main`. A controlled update must bind the image to the **Source revision** from the same GHCR workflow receipt. Use the repository's transaction helper instead of assembling separate shell snippets.

Download the helper from that exact source revision:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
release_revision='REPLACE_WITH_40_HEX_SOURCE_REVISION'
printf '%s\n' "$release_revision" | grep -Eq '^[0-9a-f]{40}$'
test ! -L scripts
install -d -m 700 scripts
helper_tmp="$(mktemp scripts/.update-immutable-deployment.XXXXXX)"
trap 'rm -f "$helper_tmp"' EXIT
curl -fsSL \
  -o "$helper_tmp" \
  "https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/$release_revision/scripts/update_immutable_deployment.py"
chmod 700 "$helper_tmp"
python3 "$helper_tmp" --help >/dev/null
mv -f "$helper_tmp" scripts/update_immutable_deployment.py
trap - EXIT
```

The helper must come from the image workflow's exact 40-hex source revision. Live Compose bytes are preserved, including operator customizations. Only explicit `--replace-compose` downloads Compose sources from that revision.

The update command takes the complete ordered Compose chain, active profiles, expected running services, and local health endpoints. This example uses the base file plus the non-root overlay with history active:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
release_revision='REPLACE_WITH_40_HEX_SOURCE_REVISION'
expected_image='REPLACE_WITH_WORKFLOW_IMMUTABLE_IMAGE'
candidate_tag='ghcr.io/gcs8/truenas-jbod-ui:v0.22.2'
python3 scripts/update_immutable_deployment.py update . \
  --project-name truenas-jbod-ui \
  --source-revision "$release_revision" \
  --expected-image "$expected_image" \
  --candidate-tag "$candidate_tag" \
  --compose docker-compose.yml=compose.yaml \
  --compose docker-compose.nonroot.yml=docker-compose.nonroot.yml \
  --profile history \
  --service enclosure-ui \
  --service enclosure-history \
  --health-url http://127.0.0.1:8080/livez \
  --health-url http://127.0.0.1:8080/healthz \
  --health-url http://127.0.0.1:8081/healthz
```

Adjust the arguments to match the running deployment exactly:

- list every active tracked Compose file with one ordered `--compose SOURCE=LIVE` argument, and every operator overlay you keep with `--keep-overlay LIVE` in its place in the chain;
- pass the live `com.docker.compose.project` value with `--project-name`;
- list every active profile with `--profile`;
- list every expected running app service with `--service`;
- list each enabled health endpoint with `--health-url`, on loopback or on an address the deployment publishes (see [Health URLs on a published address](#health-urls-on-a-published-address)).

For example, add `docker-compose.secrets.yml=docker-compose.secrets.yml` when the secrets overlay is active. Add `--profile admin`, `--service enclosure-admin`, and its loopback health URL only when admin is expected to be running through the update. The helper stops before mutation if the declared service set differs from Compose's running service set.

The helper then performs one bounded transaction. It:

1. validates the Compose project name, working directory, ordered file chain, and exact running service set against every live container's Compose labels;
2. records each service's container image ID, immutable GHCR digest, health, and restart count;
3. requires all declared app services to share one rollback digest;
4. pulls the moving candidate tag and requires its sole GHCR `RepoDigest` to equal the full immutable image from the workflow receipt;
5. validates the existing Compose chain with the candidate image pin; only `--replace-compose` downloads and validates replacement sources from the selected revision, merged with any kept overlays;
6. writes a private `0700` `.jbod-ui-image-update` directory containing a strict `0600` JSON receipt plus hashed previous and candidate Compose and `.env` bytes;
7. activates the digest without rewriting live Compose files by default, then requires the exact service set, image IDs, health, zero restart counts, and health URLs to converge.

The receipt records `replace_compose` and hashes both Compose snapshots. In image-only mode the snapshots are identical, and rollback refuses later Compose drift rather than overwriting operator edits. Old receipts without the mode field retain their original replacement semantics.

Use `--replace-compose` only for a separately reviewed Compose migration. It replaces every `--compose` file with its source-revision file, so compare local customizations and retain configured auth, origins, network choices, and optional services before authorizing it. It is not a data migration or a permission-repair tool.

### Keep operator overlays

An operator's own overlay, such as a `compose.override.yaml` that only sets `restart: unless-stopped` on the admin service, has no file in the release to replace it with. Name it with `--keep-overlay` in its place in the ordered chain, so `--replace-compose` replaces only the shipped base:

```bash
python3 scripts/update_immutable_deployment.py update . \
  --project-name truenas-jbod-ui \
  --source-revision "$release_revision" \
  --expected-image "$expected_image" \
  --candidate-tag "$candidate_tag" \
  --replace-compose \
  --compose docker-compose.yml=compose.yaml \
  --keep-overlay compose.override.yaml \
  --profile history \
  --service enclosure-ui \
  --service enclosure-history \
  --health-url http://127.0.0.1:8080/healthz \
  --health-url http://127.0.0.1:8081/healthz
```

A kept overlay may set only `restart`, nothing else (#1101). It must be a plain list of services the shipped files already define, each with exactly one `restart:` line whose value is `no`, `always`, `on-failure`, `on-failure:N` or `unless-stopped`, optionally quoted; blank lines and `#` comments (separated from a value by a space) are fine:

```yaml
services:
  enclosure-ui:
    restart: always
```

Check what a restart policy does before you keep it. `enclosure-admin` and `enclosure-backup` ship with `restart: "no"`, and the admin service stops itself after `ADMIN_AUTO_STOP_SECONDS` (one hour by default) by exiting cleanly. If an overlay sets `restart: always` or `unless-stopped` on it, Docker starts it again each time it exits, so the auto-stop no longer limits how long the admin API stays reachable. `on-failure` does not restart it after that clean exit. The helper accepts any valid restart policy, so this is the operator's choice.

Every other setting is refused before any Docker command, including `environment` (for example `ADMIN_AUTO_STOP_SECONDS` or history retention settings), `ports`, `labels`, `logging`, resource limits, `healthcheck`, `profiles`, `depends_on`, added services, `x-` keys, top-level sections such as `name`, `version`, `include`, `volumes`, `networks`, `configs` and `secrets`, `extends`, `env_file` and `build`, and YAML forms that could hide a key: flow style (`{...}`), anchors, aliases, merge keys (`<<`), tags such as `!reset`, directives, `---` document markers, quoted keys, tabs used for indentation, and anything but plain ASCII text. The refusal names the overlay and the line, never a value. Make any other change in a reviewed Compose release instead; there is no flag to allow it.

The running containers' recorded Compose chain must still equal the whole ordered chain, overlays included, before and after the update. The update never changes a kept overlay's content.

Before any Docker command, the helper reads `.env` and every chain file inside the deployment directory without following links: a symlink, a missing file or anything but a regular file is refused. Compose later reads these files by path, so the deployment directory, `.env` and every chain file must be owned by root or the user running the helper (root under `sudo`) and must not be writable by group or others. The path is checked as well: every directory from / down to the deployment, its parent included, must be owned by root and not writable by group or others (#1100): write access to any of them would let another user rename the checked deployment away and put a different one at its path just before Compose reads it. Sticky world-writable directories such as `/tmp` count as writable, so keep the deployment under a root-owned directory such as `/opt` or `/srv`, not under `/tmp` or a home directory another user (or the helper's own non-root user) owns. Otherwise the helper stops before any Docker command and names the file or directory to fix with `chown` and `chmod go-w`. It keeps those exact bytes, modes and inodes, and copies them into the private `0700` receipt directory, which stays unpublished until validation passes.

The helper then:

- validates the merged chain, new shipped files plus kept overlays, with `docker compose config --quiet`. With `--replace-compose` this runs on the frozen copies in the receipt directory; in image-only mode it runs on the live files in place, and the live files must still be the frozen ones afterwards;
- compares the merged model from `docker compose config --format json` of the shipped files alone with that of the whole chain. Any difference other than a valid `restart` value on a shipped service is refused before anything is pulled, even if the text check passed. The refusal names only services and keys, never values.

In image-only mode the same overlay checks apply, and every file is kept anyway.

Once the receipt is published, `--replace-compose` writes `.env`, every shipped file and every kept overlay as a new file the helper owns, with the recorded mode; a kept overlay gets its own frozen bytes. Changing a file's owner or mode does not revoke a descriptor that another user opened earlier, and only a new file guarantees that such a descriptor cannot reach what Compose reads. Image-only mode rewrites only `.env` and keeps the identity of the operator's Compose files. The live inputs must still be the frozen bytes, modes, inodes and owners, with the same ownership and write rules for them, the deployment directory and every directory above it, when the receipt is published, after that replacement, before Compose pulls, before it activates, and after the candidate has converged; any difference fails the update, and after activation has begun it rolls back automatically. The receipt stores each kept overlay as a chain entry with a `null` source, with identical hashed previous and candidate snapshots and its file mode. Rollback after `--replace-compose` writes every file it restores, kept overlays included, as a new file with the recorded bytes and mode. Image-only rollback still refuses Compose content drift but restores a changed mode in place, and refuses a file another user owns. Neither restores group or other write permission that an older receipt may record. Rollback reads every restored file back, holds it to the receipt until the restored containers have converged, and reports `rolled_back` only if every file still matches. `rollback` and `verify` also refuse, before any change or Docker command, while a directory above the deployment is not root-owned or is writable by group or others; after an automatic rollback was refused for that reason, fix the directory and run `rollback`.

### Health URLs on a published address

Some installs publish the UI and admin service only on the host's LAN address, so nothing answers on loopback. `--health-url`, `--inventory-url` and `--history-health-url` therefore also accept an explicit IP address and port that the deployment itself publishes, for example `--health-url http://192.0.2.10:8080/healthz` when the UI is published on `192.0.2.10:8080`.

The helper reads the published port bindings of each declared service's running container with `docker inspect` (`.NetworkSettings.Ports`) and accepts only an exact TCP binding match, before any URL is fetched, again for the candidate containers after activation, and on `verify` and `rollback`. Any other host or port is refused, and host names other than `localhost` are always refused. A wildcard binding such as `0.0.0.0` or `::` admits no address; use loopback for it. IPv6 addresses go in brackets. Loopback URLs need no published binding and add no inspection. The URLs stay plain HTTP with an explicit port, redirects are refused, timeouts are unchanged, and the requests ignore any environment proxy so they reach the named address itself.

Add `--inventory-url http://127.0.0.1:8080/api/inventory` to also check disk
retention (#400). The helper reads only the four aggregate integers in the
inventory `summary` (`source_disk_count`, `rendered_unique_disk_count`,
`duplicate_disk_view_count`, `unplaced_disk_count`), before the update and
after the candidate is healthy. It requests them with `retention_scope=system`,
so a disk counts as placed when any enclosure or storage view of the system
shows it, not only the default enclosure (#911); images that predate the
parameter ignore it. It rolls back automatically when any source disk is
unplaced, or when the source disk count falls across the update. A predecessor
without these totals gives no baseline, but the candidate must still report
them. The receipt stores the counts under `retention`; it stores no disk
identifiers. Collecting the inventory contacts the monitored systems, so the
request may take up to two minutes.

A scope above the system-wide check's enclosure-option cap returns
`retention_scope_too_large`, with `enclosure_cap` and a lower bound named
`enclosure_count_minimum`, not an exact count. The helper names this refusal
rather than treating it as an unreadable or busy inventory. A refusal before
activation stops the update; a refusal after activation still rolls back.
Numeric `Retry-After` responses remain retryable within the existing wait
budget. The helper never silently disables a requested retention check.
`--inventory-url` remains optional, but omitting it explicitly forgoes disk
retention verification for that update; there is no `--skip-retention-check`
option.

Add `--history-health-url http://127.0.0.1:8081/healthz` to also accept
durable history state separately from image convergence (#463). History
`/healthz` answers HTTP 200 both when a monitored appliance is unreachable and
when the local store needs recovery, so a successful health probe alone does
not show that history is intact. After the image converges, the helper reads
only the `collector.history_recovery_required` and
`collector.history_collection_paused` booleans and stores them, with a state
name, under `durable_state` in the receipt; it stores no detail text or
timestamps. The state is `intact` when both are `false`, `collection_paused` or
`recovery_required` when either is set, and `unknown` when the body cannot be
read or a flag is missing or not a boolean. Appliance-only degradation stays
`intact`. Any other state is a distinct non-pass: the receipt stays `active`,
the helper prints the result and exits with status 3, and `verify` repeats the
same read and exit status. It does not roll back, restart or repair anything,
because an older image cannot restore damaged history. Follow
[Recover a damaged history database](../wiki/History-Maintenance-and-Recovery.md#recover-a-damaged-history-database)
and run the explicit acknowledgement there only after recovery is complete;
the `rollback` command remains a separate manual choice and does not repair
durable data.

The receipt is JSON data, not shell code. The helper rejects symlinks, wrong owners or modes, duplicate or extra keys, missing or extra files, hash changes, and cardinality mismatches before verify or rollback touches Docker. An existing receipt blocks another update so a rerun cannot erase rollback evidence.

Activation and rollback allow up to 120 seconds of Docker `starting` health,
polling every two seconds. Docker commands have separate bounded subprocess
timeouts; HTTP probes retain their bounded retry budget. Unhealthy or exited
containers, wrong images, changed Compose identity, and nonzero restarts fail
immediately. After HTTP readiness the helper inspects the exact service and
container identities, images, health, and restart counts again before recording
success. Cancellation or failed rollback leaves no new success receipt.

Image rollback does not revert database, catalog, configuration, or inventory
writes. Pin-only rollback is safe only across predecessor-compatible durable
formats. This helper does not establish the full automatic migration and
recovery contract tracked in #399. Keep verified backups and check release
compatibility evidence separately.

### Verify Runtime Convergence

Re-read the private receipt and verify the running deployment again:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
python3 scripts/update_immutable_deployment.py verify .
```

Keep `.env` pinned to the full `name@sha256` value. Record the helper's source revision, candidate digest, per-service image IDs, health results, and restart counts in the release evidence.

### Rollback To The Previous Digest

Any failure after receipt publication triggers automatic rollback. The helper preserves Compose in image-only mode, or restores prior Compose, including kept overlays, in replacement mode, pins `.env` to the recorded previous digest, recreates the exact recorded profiles and services, and verifies image IDs, health, restart counts, and health URLs before reporting rollback complete.

You can request the same receipt-validated rollback later:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
python3 scripts/update_immutable_deployment.py rollback .
```

Keep the receipt through operator acceptance. Before a later update, archive the whole `0700` receipt directory outside the deployment directory. Never retag or overwrite a failed candidate digest.
